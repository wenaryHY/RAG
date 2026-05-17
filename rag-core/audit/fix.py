"""审计发现 → RAGFlow chunk 修正编排。

流程:
  1. 加载 funnel report finding
  2. 若 finding 已有 suggestion → 直接用作修正文本
  3. 若无 suggestion → 调 Claude Opus 生成修正
  4. PATCH RAGFlow chunk
  5. 写 audit_fixes 表留痕
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import db
from config import Config
from ragflow_client import RAGFlowClient
from .client import call_claude

logger = logging.getLogger("rag-core.audit.fix")


async def apply_fix(
    config: Config,
    rag: RAGFlowClient,
    report_path: Path,
    run_id: int,
    finding_idx: int,
) -> dict:
    """应用单条审计修正，兼容 conflict 和 error_check 两种 finding。

    Returns:
        {"status": "applied"|"skipped"|"error", "detail": str}
    """
    if "xstx" not in config.keys:
        return {"status": "error", "detail": "xstx (Claude) key missing"}

    xstx = config.keys["xstx"]

    if not report_path.exists():
        return {"status": "error", "detail": f"report not found: {report_path}"}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    findings = report.get("findings", [])
    if finding_idx < 0 or finding_idx >= len(findings):
        return {"status": "error", "detail": f"finding_idx {finding_idx} out of range (0-{len(findings)-1})"}

    finding = findings[finding_idx]
    is_error = finding.get("kind") == "error_check"
    is_conflict = finding.get("has_conflict") == True

    # ---- 确定目标文档和问题描述 ----
    if is_conflict:
        target_doc = finding.get("doc_a") or finding.get("doc_b") or ""
        issue_desc = f"{finding.get('conflict_type','')}: {finding.get('description','')}"[:500]
        recommendation = (finding.get("recommendation") or "")[:500]
    elif is_error:
        target_doc = finding.get("doc") or ""
        errors = finding.get("errors", [])
        issue_desc = "; ".join(
            f"[{e.get('type','?')}] {e.get('description','')}" for e in errors
        )[:500]
        recommendation = ""
    else:
        return {"status": "skipped", "detail": "finding is neither conflict nor error_check"}

    suggestion = (finding.get("suggestion") or "").strip()
    fix_text = suggestion if suggestion else ""

    if not fix_text:
        from .prompts import FIX_PROMPT
        content = finding.get("description") or issue_desc
        prompt = (
            FIX_PROMPT
            .replace("<<SOURCE>>", target_doc)
            .replace("<<CONTENT>>", content[:3000])
            .replace("<<ISSUE>>", issue_desc)
            .replace("<<RECOMMENDATION>>", recommendation)
        )
        try:
            resp = await call_claude(xstx, [{"role": "user", "content": prompt}], timeout=180.0)
            fix_text = (resp["choices"][0]["message"].get("content") or "").strip()
        except Exception as e:
            return {"status": "error", "detail": f"Opus fix generation failed: {e}"}

    if not fix_text:
        return {"status": "skipped", "detail": "empty fix text"}

    chunk_id = finding.get("chunk_id")
    dataset_id = finding.get("dataset_id")
    document_id = finding.get("document_id")
    if not all([chunk_id, dataset_id, document_id]):
        return {"status": "error", "detail": "finding lacks chunk tracking. Re-run audit funnel to populate"}

    original_text = ""
    try:
        chunks = rag.list_chunks(str(dataset_id), str(document_id), page=1, page_size=200)
        for ch in chunks:
            if ch.get("id") == chunk_id:
                original_text = (ch.get("content") or ch.get("content_with_weight") or "")[:5000]
                break
    except Exception:
        pass

    try:
        rag.update_chunk(str(dataset_id), str(document_id), str(chunk_id), fix_text)
    except Exception as e:
        return {"status": "error", "detail": f"RAGFlow PATCH chunk failed: {e}"}

    with db.session() as s:
        s.add(db.AuditFix(
            run_id=run_id,
            finding_idx=finding_idx,
            chunk_id=str(chunk_id),
            dataset_id=str(dataset_id),
            document_id=str(document_id),
            doc_name=target_doc,
            original_text=original_text,
            fixed_text=fix_text[:5000],
            suggestion=suggestion if suggestion else None,
            status="applied",
        ))
        s.commit()

    logger.info("audit fix applied: run=%d finding=%d chunk=%s", run_id, finding_idx, chunk_id)
    return {"status": "applied", "detail": f"chunk {chunk_id} updated"}
