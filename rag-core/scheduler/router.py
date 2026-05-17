"""Scheduler FastAPI router."""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import select, desc

import db
from .classify import JUDGE_TARGET, classify, select_target
from .providers import ProviderError, chat_completion, text_of

router = APIRouter(prefix="/scheduler", tags=["scheduler"])


class QueryRequest(BaseModel):
    query: str
    pharmacy_mode: bool = False
    system_prompt: Optional[str] = None
    max_tokens: int = 2048
    filters: Optional[dict] = None
    top_k: int = 8


class Reference(BaseModel):
    doc_name: str
    chunk_text: str
    similarity: float


class QueryResponse(BaseModel):
    answer: str
    model_used: str
    provider: str
    complexity: str
    domain: str
    cost_estimate: str
    elapsed: float
    timestamp: str
    references: list[Reference] = []


def _resolve_filters_to_dataset_ids(filters: dict) -> tuple[list[str], list[dict]]:
    """将 metadata filters 解析为 RAGFlow dataset_id 列表。

    查询 local state.sqlite.files，按 filters 条件匹配 FileRecord：
      - ingest_dir: 模糊匹配（子串）
      - filename_tokens: 包含任一词条即可
      - library: 精确匹配
    返回 (dataset_id 列表, 匹配行信息)。
    """
    dataset_ids: list[str] = []
    matched: list[dict] = []
    with db.session() as s:
        stmt = select(db.FileRecord).where(db.FileRecord.status == "done")
        rows = s.exec(stmt).all()
        for r in rows:
            if not r.dataset_id:
                continue
            keep = True
            if "ingest_dir" in filters:
                if not (r.ingest_dir and filters["ingest_dir"] in r.ingest_dir):
                    keep = False
            if "library" in filters:
                if r.library != filters["library"]:
                    keep = False
            if "filename_tokens" in filters:
                if isinstance(filters["filename_tokens"], list):
                    if not r.filename_tokens:
                        keep = False
                    else:
                        try:
                            tokens = json.loads(r.filename_tokens)
                        except Exception:
                            tokens = []
                        if not any(t in tokens for t in filters["filename_tokens"]):
                            keep = False
            if keep:
                if r.dataset_id not in dataset_ids:
                    dataset_ids.append(r.dataset_id)
                matched.append(r.model_dump())
    return dataset_ids, matched


async def _retrieve_context(ragflow, query: str, dataset_ids: list[str], top_k: int) -> tuple[str, list[Reference]]:
    """调用 RAGFlow 检索并返回 (格式化上下文, 引用列表)。"""
    if not dataset_ids:
        return "", []
    try:
        result = ragflow.retrieve(query, dataset_ids, top_k=top_k)
    except Exception:
        return "", []
    chunks = result.get("chunks", []) if isinstance(result, dict) else []
    refs: list[Reference] = []
    context_parts: list[str] = []
    for ch in chunks[:top_k]:
        content = ch.get("content") or ch.get("content_with_weight") or ""
        if not content.strip():
            continue
        doc_name = ch.get("document_name") or ch.get("doc_name") or "unknown"
        similarity = float(ch.get("similarity", 0))
        refs.append(Reference(doc_name=doc_name, chunk_text=content[:500], similarity=round(similarity, 4)))
        context_parts.append(f"[来源: {doc_name}] {content.strip()}")
    context = "\n\n---\n\n".join(context_parts) if context_parts else ""
    return context, refs


@router.post("/query", response_model=QueryResponse)
async def query(req: QueryRequest, request: Request):
    config = request.app.state.config
    keys = config.keys
    ragflow = request.app.state.ragflow

    if "deepseek" not in keys:
        raise HTTPException(status_code=503, detail="deepseek key missing")

    start = time.time()

    # 解析 filters → 检索上下文
    references: list[Reference] = []
    retrieval_context = ""
    if req.filters:
        dataset_ids, _matched = _resolve_filters_to_dataset_ids(req.filters)
        if dataset_ids and ragflow:
            retrieval_context, references = await _retrieve_context(
                ragflow, req.query, dataset_ids, req.top_k,
            )

    classification = await classify(req.query, JUDGE_TARGET, keys["deepseek"])
    target = select_target(classification, pharmacy_mode=req.pharmacy_mode)

    if target.provider not in keys:
        raise HTTPException(
            status_code=503, detail=f"provider {target.provider} key missing"
        )

    messages = []
    if req.system_prompt:
        messages.append({"role": "system", "content": req.system_prompt})
    if retrieval_context:
        messages.append({"role": "system", "content": f"参考以下检索到的文档片段回答用户问题，并在回答中注明引用来源：\n\n{retrieval_context}"})
    messages.append({"role": "user", "content": req.query})

    try:
        result = await chat_completion(
            target,
            keys[target.provider],
            messages,
            max_tokens=req.max_tokens,
        )
    except ProviderError as e:
        raise HTTPException(status_code=502, detail=str(e))

    answer = text_of(result) or ""
    if not answer.strip():
        answer = "[模型服务返回空响应，请稍后重试]"
    elapsed = round(time.time() - start, 2)
    ts = datetime.now().isoformat()

    # persist log
    with db.session() as s:
        s.add(
            db.QueryLog(
                ts=ts,
                query=req.query[:500],
                complexity=classification.get("complexity"),
                domain=classification.get("domain"),
                model=target.model,
                elapsed=elapsed,
                pharmacy_mode=req.pharmacy_mode,
                cost_estimate=target.cost_label,
                filters_used=json.dumps(req.filters, ensure_ascii=False) if req.filters else None,
            )
        )
        s.commit()

    return QueryResponse(
        answer=answer,
        model_used=target.model,
        provider=target.provider,
        complexity=classification.get("complexity", "unknown"),
        domain=classification.get("domain", "unknown"),
        cost_estimate=target.cost_label,
        elapsed=elapsed,
        timestamp=ts,
        references=references,
    )


class ClassifyRequest(BaseModel):
    query: str


@router.post("/classify")
async def classify_only(req: ClassifyRequest, request: Request):
    keys = request.app.state.config.keys
    if "deepseek" not in keys:
        raise HTTPException(status_code=503, detail="deepseek key missing")
    cls = await classify(req.query, JUDGE_TARGET, keys["deepseek"])
    target = select_target(cls)
    return {
        "classification": cls,
        "target": {
            "provider": target.provider,
            "model": target.model,
            "cost": target.cost_label,
        },
    }


@router.get("/health")
async def health(request: Request):
    config = request.app.state.config
    return {
        "status": "ok",
        "providers": sorted(config.keys.keys()),
    }


@router.get("/logs")
async def logs(limit: int = 20):
    with db.session() as s:
        rows = s.exec(
            select(db.QueryLog).order_by(desc(db.QueryLog.id)).limit(limit)
        ).all()
    return {"logs": [r.model_dump() for r in rows]}
