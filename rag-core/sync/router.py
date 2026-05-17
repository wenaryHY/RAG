"""FileSync FastAPI router."""
from __future__ import annotations

from fastapi import APIRouter, Request

from . import state as st

router = APIRouter(prefix="/sync", tags=["sync"])


@router.get("/status")
async def status(request: Request):
    engine = request.app.state.sync_engine
    return engine.status()


@router.get("/recent")
async def recent(limit: int = 20):
    rows = st.list_recent(limit=limit)
    return {"recent": [r.model_dump() for r in rows]}


@router.post("/reconcile")
async def reconcile(request: Request):
    engine = request.app.state.sync_engine
    engine.reconcile_local_to_remote()
    engine.reconcile_remote_to_local()
    return {"status": "triggered"}
