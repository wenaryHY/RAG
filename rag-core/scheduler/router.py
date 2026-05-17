"""Scheduler FastAPI router."""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Optional

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


class QueryResponse(BaseModel):
    answer: str
    model_used: str
    provider: str
    complexity: str
    domain: str
    cost_estimate: str
    elapsed: float
    timestamp: str


@router.post("/query", response_model=QueryResponse)
async def query(req: QueryRequest, request: Request):
    config = request.app.state.config
    keys = config.keys

    if "deepseek" not in keys:
        raise HTTPException(status_code=503, detail="deepseek key missing")

    start = time.time()

    classification = await classify(req.query, JUDGE_TARGET, keys["deepseek"])
    target = select_target(classification, pharmacy_mode=req.pharmacy_mode)

    if target.provider not in keys:
        raise HTTPException(
            status_code=503, detail=f"provider {target.provider} key missing"
        )

    messages = []
    if req.system_prompt:
        messages.append({"role": "system", "content": req.system_prompt})
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
        # 免费 provider 偶尔返回空 content，给个明确提示而不是 500
        answer = f"[provider {target.provider}/{target.model} 返回空响应，请重试或换模型]"
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
