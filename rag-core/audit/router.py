"""Audit FastAPI router."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .client import call_claude, parse_json
from . import fix
from .funnel import run_funnel_audit
from .prompts import render_conflict, render_error_check

import db
from sqlmodel import select, desc

router = APIRouter(prefix="/audit", tags=["audit"])


class DocInput(BaseModel):
    name: str
    content: str
    source: Optional[str] = None


class AuditRequest(BaseModel):
    documents: list[DocInput]
    check_types: list[str] = ["conflict", "error"]


class AuditResponse(BaseModel):
    report_id: str
    timestamp: str
    conflicts: list[dict]
    errors: list[dict]
    summary: str
    report_path: str


@router.get("/health")
async def health(request: Request):
    config = request.app.state.config
    report_dir: Path = config.report_dir
    reports = sorted(report_dir.glob("*.json"), reverse=True) if report_dir.exists() else []
    return {
        "status": "ok",
        "claude_model": "claude-opus-4-7",
        "report_dir": str(report_dir),
        "reports_count": len(reports),
        "latest_report": reports[0].name if reports else None,
        "xstx_configured": "xstx" in config.keys,
    }


@router.post("/run", response_model=AuditResponse)
async def run_audit(req: AuditRequest, request: Request):
    """[已弃用] 全量 Opus 两两比对。
    请改用 POST /audit/run-funnel (三级漏斗, 成本可控)。
    此端点保留用于 <=MAX_DOCS 的小批量快速检测。
    """
    MAX_DOCS = 8
    if len(req.documents) > MAX_DOCS:
        raise HTTPException(
            413,
            f"/audit/run 最多 {MAX_DOCS} 篇文档 (当前 {len(req.documents)} 篇)。"
            " 请改用 POST /audit/run-funnel 运行全库三级漏斗审计。",
        )
    config = request.app.state.config
    if "xstx" not in config.keys:
        raise HTTPException(503, "xstx (Claude) key missing")
    xstx = config.keys["xstx"]
    report_dir: Path = config.report_dir
    report_dir.mkdir(parents=True, exist_ok=True)
    debug_path = str(report_dir / "_debug_last_response.txt")

    docs = req.documents
    conflicts: list[dict] = []
    errors: list[dict] = []

    if "conflict" in req.check_types:
        for i in range(len(docs)):
            for j in range(i + 1, len(docs)):
                prompt = render_conflict(
                    docs[i].name, docs[i].content, docs[j].name, docs[j].content
                )
                try:
                    result = await call_claude(xstx, [{"role": "user", "content": prompt}])
                    text = result["choices"][0]["message"]["content"]
                    parsed = parse_json(text, debug_path=debug_path)
                    if parsed.get("has_conflict"):
                        parsed["doc_a"] = docs[i].name
                        parsed["doc_b"] = docs[j].name
                        conflicts.append(parsed)
                except Exception as e:
                    conflicts.append({
                        "doc_a": docs[i].name,
                        "doc_b": docs[j].name,
                        "has_conflict": True,
                        "conflict_type": "检测失败",
                        "severity": "未知",
                        "description": str(e)[:300],
                    })

    if "error" in req.check_types:
        for d in docs:
            prompt = render_error_check(d.name, d.content)
            try:
                result = await call_claude(xstx, [{"role": "user", "content": prompt}])
                text = result["choices"][0]["message"]["content"]
                parsed = parse_json(text, debug_path=debug_path)
                if parsed.get("has_error"):
                    errors.append({"source": d.name, "errors": parsed.get("errors", [])})
            except Exception as e:
                errors.append({
                    "source": d.name,
                    "errors": [{"type": "检测失败", "description": str(e)[:300], "severity": "未知"}],
                })

    high_c = sum(1 for c in conflicts if c.get("severity") == "高")
    high_e = sum(1 for e in errors for err in e.get("errors", []) if err.get("severity") == "高")
    summary = f"审计完成：{len(conflicts)}处冲突（{high_c}处高危），{len(errors)}个文档有误（{high_e}处高危）"

    report_id = datetime.now().strftime("audit-%Y%m%d-%H%M%S")
    ts = datetime.now().isoformat()
    report = {
        "report_id": report_id,
        "timestamp": ts,
        "conflicts": conflicts,
        "errors": errors,
        "summary": summary,
    }
    report_path = report_dir / f"{report_id}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return AuditResponse(report_path=str(report_path), **report)


@router.post("/single", response_model=AuditResponse)
async def audit_single(doc: DocInput, request: Request):
    return await run_audit(AuditRequest(documents=[doc], check_types=["error"]), request)


@router.get("/reports")
async def list_reports(request: Request, limit: int = 20):
    report_dir: Path = request.app.state.config.report_dir
    if not report_dir.exists():
        return {"reports": []}
    reports = sorted(report_dir.glob("audit-*.json"), reverse=True)[:limit]
    return {"reports": [{"name": r.name, "size": r.stat().st_size} for r in reports]}


@router.get("/reports/{name}")
async def get_report(name: str, request: Request):
    report_dir: Path = request.app.state.config.report_dir
    fp = report_dir / name
    if not fp.exists() or not fp.is_file():
        raise HTTPException(404, "report not found")
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"failed to read report: {e}")


# ----------------------------------------------------------------------
# Phase 4: 三级漏斗 (embedding -> Flash -> Opus)
# ----------------------------------------------------------------------

class FunnelRunRequest(BaseModel):
    dataset_ids: Optional[list[str]] = None
    max_per_doc: int = 30
    sample_for_error: int = 3


@router.post("/run-funnel")
async def run_funnel(req: FunnelRunRequest, request: Request):
    """同步执行三级漏斗审计 (可能耗时数分钟)。

    用 dataset_ids=None 跑全部知识库。
    返回 FunnelStats 摘要 + 报告路径。
    """
    config = request.app.state.config
    rag = request.app.state.ragflow
    missing = [k for k in ("siliconflow", "deepseek", "xstx") if k not in config.keys]
    if missing:
        raise HTTPException(503, f"missing keys: {missing}")
    stats = await run_funnel_audit(
        config, rag,
        dataset_ids=req.dataset_ids,
        max_per_doc=req.max_per_doc,
        sample_for_error=req.sample_for_error,
    )
    return {
        "status": stats.status,
        "started_at": stats.started_at,
        "finished_at": stats.finished_at,
        "chunks_total": stats.chunks_total,
        "embedding_pairs": stats.embedding_pairs,
        "flash_calls": stats.flash_calls,
        "flash_yes": stats.flash_yes,
        "flash_maybe": stats.flash_maybe,
        "opus_calls": stats.opus_calls,
        "opus_calls_capped": stats.opus_calls_capped,
        "cache_hits": stats.cache_hits,
        "cost_estimate": stats.cost_estimate,
        "findings_count": len(stats.findings),
        "errors": stats.errors,
    }


@router.get("/runs")
async def list_runs(limit: int = 30):
    with db.session() as s:
        rows = s.exec(
            select(db.AuditRun).order_by(desc(db.AuditRun.id)).limit(limit)
        ).all()
    return {"runs": [r.model_dump() for r in rows]}


@router.post("/runs/{run_id}/seen")
async def mark_run_seen(run_id: int):
    """标记审计报告为"已读" (去掉仪表盘红点)。

    安全注意: 无认证 — 假定 rag-core 仅监听 127.0.0.1，
    不会被外部访问。若日后改为公网绑定，需加共享密钥校验。
    """
    with db.session() as s:
        run = s.get(db.AuditRun, run_id)
        if not run:
            raise HTTPException(404, "run not found")
        run.seen = True
        s.add(run); s.commit()
    return {"status": "ok"}


class FixRequest(BaseModel):
    run_id: int
    finding_idx: int
    report_name: str  # e.g. "audit-funnel-20260518-020000.json"


@router.post("/fix")
async def apply_fix_endpoint(req: FixRequest, request: Request):
    """对一条审计发现应用修正。"""
    config = request.app.state.config
    rag = request.app.state.ragflow
    report_path = config.report_dir / req.report_name
    result = await fix.apply_fix(config, rag, report_path, req.run_id, req.finding_idx)
    return result


@router.get("/fixes")
async def list_fixes(run_id: Optional[int] = None, limit: int = 50):
    """列出历史修正记录。"""
    with db.session() as s:
        stmt = select(db.AuditFix).order_by(db.AuditFix.applied_at.desc())  # type: ignore[attr-defined]
        if run_id is not None:
            stmt = stmt.where(db.AuditFix.run_id == run_id)
        rows = s.exec(stmt.limit(limit)).all()
    return {"fixes": [r.model_dump() for r in rows]}
