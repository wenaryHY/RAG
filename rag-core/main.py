"""rag-core entrypoint.

合并 Scheduler / Audit / FileSync / Panel 为单 FastAPI app。
端口/路径全部从 D:/RAG/config.toml 读，禁止硬编码。

阶段 1.A：仅骨架 + /health（含 RAGFlow 连通性）。
阶段 1.B：挂载 /scheduler/*。
阶段 1.C：挂载 /sync/*、/audit/*。
阶段 2  ：挂载 /api/*、/ui。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

import config as cfg_mod
import db
from ragflow_client import RAGFlowClient
from scheduler.router import router as scheduler_router

logger = logging.getLogger("rag-core")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = cfg_mod.load_config()
    app.state.config = config

    # ensure dirs
    for p in (config.data_root, config.report_dir, config.log_dir):
        p.mkdir(parents=True, exist_ok=True)

    # init sqlite
    db.init_db(config.state_db)
    logger.info("state db ready: %s", config.state_db)

    # ragflow client
    app.state.ragflow = RAGFlowClient(
        base_url=config.ragflow_base_url,
        api_key=config.ragflow_api_key,
        timeout=config.ragflow_timeout,
    )
    logger.info("ragflow client targeting %s", config.ragflow_base_url)

    yield

    app.state.ragflow.close()


app = FastAPI(title="rag-core", version="0.1.0", lifespan=lifespan)
app.include_router(scheduler_router)


@app.get("/health")
def health():
    config: cfg_mod.Config = app.state.config
    ragflow_ok = app.state.ragflow.ping()
    return JSONResponse(
        {
            "service": "rag-core",
            "version": "0.1.0",
            "status": "ok" if ragflow_ok else "degraded",
            "checks": {
                "ragflow": "ok" if ragflow_ok else "fail",
                "state_db": str(config.state_db),
                "data_root": str(config.data_root),
            },
            "providers_configured": sorted(config.keys.keys()),
        }
    )


@app.get("/")
def root():
    return {"service": "rag-core", "docs": "/docs", "health": "/health"}


if __name__ == "__main__":
    import uvicorn

    config = cfg_mod.load_config()
    uvicorn.run(
        "main:app",
        host=config.server_host,
        port=config.server_port,
        log_level="info",
        reload=False,
    )
