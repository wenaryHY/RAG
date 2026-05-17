"""FileSync FastAPI router."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

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


@router.get("/events")
async def events_sse(request: Request):
    """SSE: 把 sync engine 新增事件推给前端。

    协议:
      - 连接建立先发一条 event: hello 带最近 5 条历史
      - 之后只要有新事件就 event: sync
      - 空闲每 15 秒发一个 ": ping" 心跳防超时

    线程安全: 用 engine.snapshot_events() 原子快照替代直接读 recent_events。
    """
    engine = request.app.state.sync_engine

    async def gen():
        new_events, cursor = engine.snapshot_events(since=0)
        history = new_events[-5:] if new_events else []
        yield f"event: hello\ndata: {json.dumps({'history': history}, ensure_ascii=False)}\n\n"

        idle_iterations = 0
        try:
            while True:
                if await request.is_disconnected():
                    break
                new_events, cursor = engine.snapshot_events(since=cursor)
                if new_events:
                    for ev in new_events:
                        yield f"event: sync\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    idle_iterations = 0
                else:
                    idle_iterations += 1
                    if idle_iterations >= 15:
                        yield ": ping\n\n"
                        idle_iterations = 0
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
