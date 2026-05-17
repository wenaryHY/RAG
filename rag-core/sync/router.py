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
      - 之后只要 engine.recent_events 增量出现新事件就 event: sync
      - 空闲每 15 秒发一个 ": ping" 心跳防超时

    实现: engine.recent_events 在长度 > 500 时会被截断到 300,
    所以下游 cursor 用整数 index, 发现 cur_len 比上次还小就 reset。
    """
    engine = request.app.state.sync_engine

    async def gen():
        events = engine.recent_events
        history = list(events[-5:])
        yield f"event: hello\ndata: {json.dumps({'history': history}, ensure_ascii=False)}\n\n"

        last_len = len(events)
        idle_count = 0
        try:
            while True:
                if await request.is_disconnected():
                    break
                events = engine.recent_events
                cur_len = len(events)
                if cur_len < last_len:
                    last_len = max(0, cur_len - 50)
                if cur_len > last_len:
                    for ev in events[last_len:cur_len]:
                        yield f"event: sync\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    last_len = cur_len
                    idle_count = 0
                else:
                    idle_count += 1
                    if idle_count >= 15:
                        yield ": ping\n\n"
                        idle_count = 0
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
