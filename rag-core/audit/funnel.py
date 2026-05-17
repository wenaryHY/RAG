"""三级漏斗审计编排。

流程:
  Stage1  SiliconFlow bge-m3 embed      → cosine ≥ threshold 的 cross-doc pair
  Stage2  DeepSeek Flash 二分类         → yes/maybe/no
  Stage3  Claude Opus 终判              → 严重度+建议+原文引用
  单文档错误检测: 每文档采样 N 个 chunk → Opus

成本控制:
  - opus_calls_per_run_max 硬截断
  - audit_pair_cache (sha_a, sha_b) 跨周缓存

输入: dataset_id 列表 (默认全库)
输出: AuditRun 行 + reports/audit-*.json
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlmodel import select

import db
from config import Config, ProviderKey
from ragflow_client import RAGFlowClient
from scheduler.providers import ModelTarget, chat_completion, text_of

from .client import call_claude, parse_json
from .embed import cosine_pairs, embed_batch
from .prompts import render_conflict, render_error_check

logger = logging.getLogger("rag-core.audit.funnel")

FLASH_PROMPT = """判断下面两段文档片段是否存在事实冲突或矛盾。

片段A (来自 <<DOC_A>>):
<<TEXT_A>>

片段B (来自 <<DOC_B>>):
<<TEXT_B>>

只回答一个词: yes / maybe / no
- yes  : 明确冲突
- maybe: 看起来可能冲突但需要更多上下文
- no   : 不冲突或无关
"""


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "ignore")).hexdigest()[:32]


@dataclass
class FunnelStats:
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    finished_at: Optional[str] = None
    chunks_total: int = 0
    embedding_pairs: int = 0
    flash_calls: int = 0
    flash_yes: int = 0
    flash_maybe: int = 0
    opus_calls: int = 0
    opus_calls_capped: bool = False
    cache_hits: int = 0
    cost_estimate: float = 0.0
    findings: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    status: str = "running"


def _cache_get(sha_a: str, sha_b: str) -> Optional[db.AuditPairCache]:
    a, b = sorted([sha_a, sha_b])
    with db.session() as s:
        return s.get(db.AuditPairCache, (a, b))


def _cache_put(sha_a: str, sha_b: str, *, flash: Optional[str] = None, opus: Optional[str] = None):
    a, b = sorted([sha_a, sha_b])
    with db.session() as s:
        rec = s.get(db.AuditPairCache, (a, b))
        if rec is None:
            rec = db.AuditPairCache(sha_a=a, sha_b=b)
        if flash is not None:
            rec.flash_verdict = flash
        if opus is not None:
            rec.opus_verdict = opus
        rec.judged_at = datetime.now().isoformat()
        s.add(rec)
        s.commit()


def _flash_verdict(text: str) -> str:
    t = text.lower().strip()
    if t.startswith("yes") or "yes" == t[:3]:
        return "yes"
    if t.startswith("maybe"):
        return "maybe"
    return "no"


async def _stage2_flash(
    pair_text_a: str, pair_text_b: str,
    doc_a: str, doc_b: str,
    deepseek_key: ProviderKey,
) -> str:
    target = ModelTarget(provider="deepseek", model="deepseek-v4-flash", cost_label="audit/flash")
    prompt = (FLASH_PROMPT
              .replace("<<DOC_A>>", doc_a)
              .replace("<<TEXT_A>>", pair_text_a[:1500])
              .replace("<<DOC_B>>", doc_b)
              .replace("<<TEXT_B>>", pair_text_b[:1500]))
    resp = await chat_completion(
        target, deepseek_key,
        [{"role": "user", "content": prompt}],
        temperature=0.0, max_tokens=16,
        thinking={"type": "disabled"},
    )
    raw = text_of(resp) or ""
    return _flash_verdict(raw)


async def _stage3_opus(
    text_a: str, doc_a: str,
    text_b: str, doc_b: str,
    xstx_key: ProviderKey, debug_path: str,
) -> Optional[dict]:
    prompt = render_conflict(doc_a, text_a, doc_b, text_b)
    resp = await call_claude(xstx_key, [{"role": "user", "content": prompt}])
    raw = resp["choices"][0]["message"].get("content") or ""
    parsed = parse_json(raw, debug_path=debug_path)
    if parsed.get("has_conflict"):
        parsed["doc_a"] = doc_a
        parsed["doc_b"] = doc_b
        return parsed
    return None


def _collect_chunks(
    rag: RAGFlowClient, dataset_id: str, *, max_per_doc: int = 30,
) -> list[dict]:
    """返回 [{doc_name, chunk_id, text, sha}], 跨文档抽样后用于 embed。"""
    out: list[dict] = []
    docs = rag.list_documents(dataset_id, page=1, page_size=200)
    for d in docs:
        doc_id = d.get("id")
        doc_name = d.get("name") or doc_id
        if not doc_id:
            continue
        try:
            chs = rag.list_chunks(dataset_id, doc_id, page=1, page_size=max_per_doc)
        except Exception as e:
            logger.warning("list_chunks(%s) failed: %s", doc_id, e)
            continue
        for c in chs[:max_per_doc]:
            txt = (c.get("content") or c.get("content_with_weight") or "").strip()
            if not txt or len(txt) < 30:
                continue
            out.append({
                "doc": doc_name, "doc_id": doc_id,
                "chunk_id": c.get("id"), "text": txt, "sha": _sha(txt),
            })
    return out


async def run_funnel_audit(
    config: Config,
    rag: RAGFlowClient,
    dataset_ids: Optional[list[str]] = None,
    *,
    max_per_doc: int = 30,
    sample_for_error: int = 3,
) -> FunnelStats:
    """三级漏斗主流程。返回 FunnelStats; 同时落 reports/*.json + audit_runs 行。"""
    stats = FunnelStats()

    sf_key = config.keys.get("siliconflow")
    ds_key = config.keys.get("deepseek")
    xs_key = config.keys.get("xstx")
    if not (sf_key and ds_key and xs_key):
        stats.status = "failed"
        stats.errors.append({"stage": "init", "msg": "missing keys (need siliconflow/deepseek/xstx)"})
        return stats

    threshold = float(config.raw["audit"].get("similarity_threshold", 0.82))
    opus_max = int(config.raw["audit"].get("opus_calls_per_run_max", 100))
    yes_or_maybe_only = bool(config.raw["audit"].get("flash_yes_or_maybe_only", True))

    report_dir: Path = config.report_dir
    report_dir.mkdir(parents=True, exist_ok=True)
    debug_path = str(report_dir / "_debug_funnel_last.txt")

    # 写一行 running 记录
    with db.session() as s:
        run = db.AuditRun(started_at=stats.started_at, status="running")
        s.add(run); s.commit(); s.refresh(run)
        run_id = run.id

    # 收集 chunks
    if dataset_ids is None:
        try:
            datasets = rag.list_datasets()
            dataset_ids = [d["id"] for d in datasets if d.get("id")]
        except Exception as e:
            stats.status = "failed"
            stats.errors.append({"stage": "list_datasets", "msg": str(e)[:200]})
            return stats

    all_chunks: list[dict] = []
    for ds_id in dataset_ids:
        try:
            all_chunks.extend(_collect_chunks(rag, ds_id, max_per_doc=max_per_doc))
        except Exception as e:
            logger.warning("collect chunks for %s failed: %s", ds_id, e)
            stats.errors.append({"stage": "collect", "dataset": ds_id, "msg": str(e)[:200]})
    stats.chunks_total = len(all_chunks)
    if stats.chunks_total < 2:
        stats.status = "completed"
        stats.finished_at = datetime.now().isoformat()
        _persist_run(run_id, stats, report_dir)
        return stats

    # Stage 1: embed
    texts = [c["text"][:2000] for c in all_chunks]
    try:
        vecs = await embed_batch(sf_key, texts)
    except Exception as e:
        stats.status = "failed"
        stats.errors.append({"stage": "embed", "msg": str(e)[:300]})
        _persist_run(run_id, stats, report_dir)
        return stats
    if len(vecs) != len(all_chunks):
        stats.errors.append({"stage": "embed", "msg": f"vec/chunk count mismatch {len(vecs)}/{len(all_chunks)}"})

    # 仅 cross-doc pair
    pairs: list[tuple[int, int, float]] = []
    n = min(len(vecs), len(all_chunks))
    for i in range(n):
        for j in range(i + 1, n):
            if all_chunks[i]["doc_id"] == all_chunks[j]["doc_id"]:
                continue
            from .embed import _cosine
            score = _cosine(vecs[i], vecs[j])
            if score >= threshold:
                pairs.append((i, j, score))
    pairs.sort(key=lambda x: -x[2])
    stats.embedding_pairs = len(pairs)

    # Stage 2: Flash
    flash_pass: list[tuple[int, int, str]] = []
    for i, j, _score in pairs:
        a, b = all_chunks[i], all_chunks[j]
        cached = _cache_get(a["sha"], b["sha"])
        if cached and cached.flash_verdict:
            stats.cache_hits += 1
            verdict = cached.flash_verdict
        else:
            try:
                verdict = await _stage2_flash(a["text"], b["text"], a["doc"], b["doc"], ds_key)
                stats.flash_calls += 1
                _cache_put(a["sha"], b["sha"], flash=verdict)
            except Exception as e:
                logger.warning("flash failed: %s", e)
                stats.errors.append({"stage": "flash", "msg": str(e)[:200]})
                continue
        if verdict == "yes":
            stats.flash_yes += 1
            flash_pass.append((i, j, verdict))
        elif verdict == "maybe":
            stats.flash_maybe += 1
            flash_pass.append((i, j, verdict))

    # Stage 3: Opus (含硬上限)
    keep = flash_pass if yes_or_maybe_only else [(i, j, "all") for i, j, _ in pairs]
    keep = keep[:opus_max]
    if len(flash_pass) > opus_max:
        stats.opus_calls_capped = True

    for i, j, _v in keep:
        if stats.opus_calls >= opus_max:
            stats.opus_calls_capped = True
            break
        a, b = all_chunks[i], all_chunks[j]
        cached = _cache_get(a["sha"], b["sha"])
        if cached and cached.opus_verdict:
            stats.cache_hits += 1
            try:
                stats.findings.append(json.loads(cached.opus_verdict))
            except Exception:
                pass
            continue
        try:
            finding = await _stage3_opus(a["text"], a["doc"], b["text"], b["doc"], xs_key, debug_path)
            stats.opus_calls += 1
            if finding:
                stats.findings.append(finding)
                _cache_put(a["sha"], b["sha"], opus=json.dumps(finding, ensure_ascii=False))
            else:
                _cache_put(a["sha"], b["sha"], opus=json.dumps({"has_conflict": False}))
        except Exception as e:
            logger.warning("opus failed: %s", e)
            stats.errors.append({"stage": "opus", "msg": str(e)[:200]})

    # 单文档错误抽样: 每 doc 取 sample_for_error 个 chunk -> Opus
    by_doc: dict[str, list[dict]] = {}
    for c in all_chunks:
        by_doc.setdefault(c["doc_id"], []).append(c)
    for doc_id, items in by_doc.items():
        if stats.opus_calls >= opus_max:
            stats.opus_calls_capped = True
            break
        sample = random.sample(items, k=min(sample_for_error, len(items)))
        for c in sample:
            if stats.opus_calls >= opus_max:
                stats.opus_calls_capped = True
                break
            try:
                prompt = render_error_check(c["doc"], c["text"])
                resp = await call_claude(xs_key, [{"role": "user", "content": prompt}])
                stats.opus_calls += 1
                raw = resp["choices"][0]["message"].get("content") or ""
                parsed = parse_json(raw, debug_path=debug_path)
                if parsed.get("has_error"):
                    stats.findings.append({
                        "kind": "error_check",
                        "doc": c["doc"],
                        "errors": parsed.get("errors", []),
                    })
            except Exception as e:
                stats.errors.append({"stage": "error_check", "doc": c["doc"], "msg": str(e)[:200]})

    # 估算成本: flash≈¥0.001/call, opus≈¥0.05/call
    stats.cost_estimate = round(stats.flash_calls * 0.001 + stats.opus_calls * 0.05, 4)
    stats.status = "completed"
    stats.finished_at = datetime.now().isoformat()
    _persist_run(run_id, stats, report_dir)
    return stats


def _persist_run(run_id: int, stats: FunnelStats, report_dir: Path):
    report_id = f"audit-funnel-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    payload = {
        "report_id": report_id,
        "started_at": stats.started_at,
        "finished_at": stats.finished_at,
        "status": stats.status,
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
        "findings": stats.findings,
        "errors": stats.errors,
    }
    report_path = report_dir / f"{report_id}.json"
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with db.session() as s:
        run = s.get(db.AuditRun, run_id)
        if run:
            run.finished_at = stats.finished_at
            run.embedding_pairs = stats.embedding_pairs
            run.flash_calls = stats.flash_calls
            run.opus_calls = stats.opus_calls
            run.cost_estimate = stats.cost_estimate
            run.findings_count = len(stats.findings)
            run.report_path = str(report_path)
            run.status = stats.status
            s.add(run); s.commit()
