"""Panel API 聚合层。

端点：
- GET  /api/services/health   服务总览（rag-tray 用）
- GET  /api/datasets          datasets 列表（代理 RAGFlow）
- GET  /api/datasets/{id}/documents   dataset 内文档列表
- POST /api/upload            手动上传（拷贝到对应 lib 目录后由 watcher 处理）
- GET  /api/recent-syncs      最近同步记录（state.sqlite）
- GET  /api/stats             统计：各库文档数、查询数、审计次数
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from sqlmodel import select, func

import db
from sync import state as sync_state

router = APIRouter(prefix="/api", tags=["panel"])


@router.get("/services/health")
async def services_health(request: Request):
    """聚合所有子服务健康状态（rag-tray 用，绿/黄/红判定依据）。"""
    config = request.app.state.config
    ragflow = request.app.state.ragflow
    sync_engine = getattr(request.app.state, "sync_engine", None)

    ragflow_ok = ragflow.ping()
    sync_ok = bool(sync_engine and sync_engine._observer and sync_engine._observer.is_alive())

    checks = {
        "ragflow": "ok" if ragflow_ok else "fail",
        "sync_watcher": "ok" if sync_ok else "fail",
        "scheduler": "ok",
        "audit": "ok" if "xstx" in config.keys else "missing-key",
    }
    bad = [k for k, v in checks.items() if v != "ok"]
    if not bad:
        overall = "green"
    elif "ragflow" in bad:
        overall = "red"
    else:
        overall = "yellow"

    return {
        "overall": overall,
        "checks": checks,
        "providers_configured": sorted(config.keys.keys()),
        "data_root": str(config.data_root),
        "ts": datetime.now().isoformat(),
    }


@router.get("/datasets")
async def list_datasets(request: Request):
    """RAGFlow datasets 列表代理（前端不持有 API key）。"""
    try:
        items = request.app.state.ragflow.list_datasets()
    except Exception as e:
        raise HTTPException(502, f"ragflow list_datasets failed: {e}")
    return {"datasets": items}


@router.get("/datasets/{dataset_id}/documents")
async def list_documents(dataset_id: str, request: Request, limit: int = 100):
    try:
        docs = request.app.state.ragflow.list_documents(
            dataset_id, page=1, page_size=limit
        )
    except Exception as e:
        raise HTTPException(502, f"ragflow list_documents failed: {e}")
    return {"documents": docs}


@router.post("/upload")
async def manual_upload(
    request: Request,
    file: UploadFile = File(...),
    library: str = Form(...),
):
    """手动上传：把文件落到 RAGfiles/<library>/ 后由 watcher 处理。"""
    config = request.app.state.config
    lib_dir: Path = config.data_root / library
    if not lib_dir.exists():
        lib_dir.mkdir(parents=True, exist_ok=True)

    safe_name = Path(file.filename or "upload.bin").name
    dest = lib_dir / safe_name

    # 重名加时间戳
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = lib_dir / f"{stem}-{ts}{suffix}"

    with dest.open("wb") as fp:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            fp.write(chunk)

    return {
        "status": "queued",
        "path": str(dest),
        "library": library,
        "size": dest.stat().st_size,
        "note": "file dropped into watcher inbox; check /sync/recent for progress",
    }


@router.get("/recent-syncs")
async def recent_syncs(limit: int = 30):
    rows = sync_state.list_recent(limit=limit)
    return {"items": [r.model_dump() for r in rows]}


@router.get("/stats")
async def stats(request: Request):
    config = request.app.state.config

    with db.session() as s:
        total_files = s.exec(
            select(func.count()).select_from(db.FileRecord)
        ).one()
        done_files = s.exec(
            select(func.count()).select_from(db.FileRecord).where(
                db.FileRecord.status == "done"
            )
        ).one()
        error_files = s.exec(
            select(func.count()).select_from(db.FileRecord).where(
                db.FileRecord.status == "error"
            )
        ).one()
        total_queries = s.exec(
            select(func.count()).select_from(db.QueryLog)
        ).one()
        total_audits = s.exec(
            select(func.count()).select_from(db.AuditRun)
        ).one()

    # per-library file count
    with db.session() as s:
        lib_counts = s.exec(
            select(db.FileRecord.library, func.count()).group_by(db.FileRecord.library)
        ).all()

    # ragflow side
    try:
        datasets = request.app.state.ragflow.list_datasets()
        rag_libs = [
            {
                "name": d.get("name"),
                "id": d.get("id"),
                "document_count": d.get("document_count", 0),
                "chunk_count": d.get("chunk_count", 0),
            }
            for d in datasets
        ]
    except Exception:
        rag_libs = []

    return {
        "files": {
            "total": total_files,
            "done": done_files,
            "error": error_files,
            "by_library": [{"library": l, "count": c} for l, c in lib_counts],
        },
        "queries": {"total": total_queries},
        "audits": {"total_runs": total_audits},
        "ragflow_libraries": rag_libs,
        "data_root": str(config.data_root),
    }
