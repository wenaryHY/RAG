"""核心同步引擎。

线程模型：
- main 线程：FastAPI / uvicorn (asyncio)
- watchdog 线程：观察文件系统事件，事件丢入 _queue
- worker 线程：消费 _queue，调 RAGFlow client，写 state db
- poller 线程：30s 轮询 RAGFlow datasets，对照本地文件夹

为什么不用 asyncio：watchdog/blocking httpx 在线程里更直观，且免去 main 事件循环阻塞。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from config import Config
from ragflow_client import RAGFlowClient

from . import state as st
from .notify import toast

logger = logging.getLogger("rag-core.sync")

# 已索引扩展名（其他类型仍上传，但日志标注一下）
SUPPORTED_EXT = {
    ".pdf", ".txt", ".md", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".html", ".htm", ".csv", ".json", ".rtf", ".epub", ".png", ".jpg", ".jpeg",
}


def _library_for(path: Path, data_root: Path) -> Optional[str]:
    """返回文件所在的一级子目录名 = 知识库名。文件直接落 data_root 下则跳过。"""
    try:
        rel = path.relative_to(data_root)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 2:
        return None
    if parts[0].startswith("."):
        return None
    return parts[0]


class _Handler(FileSystemEventHandler):
    def __init__(self, engine: "SyncEngine"):
        self.engine = engine

    def on_created(self, event: FileSystemEvent):
        if event.is_directory:
            self.engine.enqueue_dir(Path(event.src_path))
        else:
            self.engine.enqueue_file(Path(event.src_path))

    def on_modified(self, event: FileSystemEvent):
        if not event.is_directory:
            self.engine.enqueue_file(Path(event.src_path))

    def on_moved(self, event):  # type: ignore[override]
        # 视作 created at dest
        dest = getattr(event, "dest_path", None)
        if dest:
            p = Path(dest)
            if not event.is_directory:
                self.engine.enqueue_file(p)
            else:
                self.engine.enqueue_dir(p)


class SyncEngine:
    DEBOUNCE_SEC = 5
    POLL_INTERVAL_SEC = 30

    def __init__(self, config: Config, ragflow: RAGFlowClient):
        self.config = config
        self.ragflow = ragflow
        self.data_root = config.data_root
        self.DEBOUNCE_SEC = int(config.raw["sync"].get("debounce_sec", 5))
        self.POLL_INTERVAL_SEC = int(config.raw["sync"].get("poll_interval_sec", 30))
        self._language = config.raw["sync"].get("default_language", "Chinese")
        self._chunk_tokens = int(config.raw["sync"].get("default_chunk_tokens", 256))
        self._embedding = config.raw["ragflow"].get(
            "default_embedding_model", "BAAI/bge-m3@SILICONFLOW"
        )

        self._queue: "Queue[tuple[str, Path]]" = Queue()
        self._pending: dict[Path, float] = {}
        self._lock = threading.Lock()

        self._observer: Optional[Observer] = None
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []

        # cache: library_name -> dataset id
        self._lib_cache: dict[str, str] = {}
        self._lib_cache_at = 0.0

        # in-memory tail
        self.recent_events: list[dict] = []

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def start(self):
        self.data_root.mkdir(parents=True, exist_ok=True)
        # initial reconcile (synchronous to detect what's there before we start watching)
        self._refresh_lib_cache(force=True)
        self.reconcile_local_to_remote()
        self.reconcile_remote_to_local()

        self._observer = Observer()
        self._observer.schedule(_Handler(self), str(self.data_root), recursive=True)
        self._observer.start()
        logger.info("watchdog observer watching %s", self.data_root)

        for name, fn in (
            ("worker", self._worker_loop),
            ("debouncer", self._debounce_loop),
            ("poller", self._poll_loop),
        ):
            t = threading.Thread(target=fn, name=f"sync-{name}", daemon=True)
            t.start()
            self._workers.append(t)

    def stop(self):
        self._stop.set()
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception:
                pass

    def status(self) -> dict:
        return {
            "data_root": str(self.data_root),
            "queue_size": self._queue.qsize(),
            "pending": len(self._pending),
            "libraries": list(self._lib_cache.keys()),
            "observer_alive": bool(self._observer and self._observer.is_alive()),
            "recent_events": self.recent_events[-20:],
        }

    def enqueue_file(self, p: Path):
        try:
            if not p.is_file():
                return
        except OSError:
            return
        if not _library_for(p, self.data_root):
            return
        with self._lock:
            self._pending[p] = time.time()

    def enqueue_dir(self, p: Path):
        # 只处理 data_root 直接子目录（一级 = 知识库）
        try:
            rel = p.relative_to(self.data_root)
        except ValueError:
            return
        if len(rel.parts) != 1 or rel.parts[0].startswith("."):
            return
        self._queue.put(("dir", p))

    # ------------------------------------------------------------------
    # internal loops
    # ------------------------------------------------------------------
    def _debounce_loop(self):
        while not self._stop.is_set():
            time.sleep(1.0)
            now = time.time()
            ready: list[Path] = []
            with self._lock:
                for p, ts in list(self._pending.items()):
                    if now - ts >= self.DEBOUNCE_SEC:
                        ready.append(p)
                        del self._pending[p]
            for p in ready:
                self._queue.put(("file", p))

    def _worker_loop(self):
        while not self._stop.is_set():
            try:
                kind, p = self._queue.get(timeout=1.0)
            except Empty:
                continue
            try:
                if kind == "file":
                    self._handle_file(p)
                elif kind == "dir":
                    self._handle_new_lib_dir(p)
            except Exception as e:  # noqa: BLE001
                logger.exception("sync worker error on %s: %s", p, e)
                self._record_event("error", str(p), str(e)[:200])

    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                self.reconcile_remote_to_local()
            except Exception as e:  # noqa: BLE001
                logger.warning("remote->local reconcile failed: %s", e)
            self._stop.wait(self.POLL_INTERVAL_SEC)

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def _refresh_lib_cache(self, *, force: bool = False) -> dict[str, str]:
        if not force and time.time() - self._lib_cache_at < 10:
            return self._lib_cache
        try:
            datasets = self.ragflow.list_datasets()
        except Exception as e:
            logger.warning("list_datasets failed: %s", e)
            return self._lib_cache
        self._lib_cache = {d["name"]: d["id"] for d in datasets if d.get("id")}
        self._lib_cache_at = time.time()
        return self._lib_cache

    def _ensure_dataset(self, name: str) -> Optional[str]:
        cache = self._refresh_lib_cache()
        if name in cache:
            return cache[name]
        logger.info("creating dataset for new library %s", name)
        try:
            data = self.ragflow.create_dataset(
                name=name,
                language=self._language,
                chunk_method="naive",
                chunk_token_num=self._chunk_tokens,
                embedding_model=self._embedding,
            )
        except Exception as e:
            logger.error("create_dataset(%s) failed: %s", name, e)
            return None
        ds_id = data.get("id") if isinstance(data, dict) else None
        if ds_id:
            self._lib_cache[name] = ds_id
            self._record_event("library_created", name, ds_id)
            toast("RAG: 新建知识库", f"已创建 {name}")
        return ds_id

    def _handle_new_lib_dir(self, p: Path):
        name = p.name
        self._ensure_dataset(name)

    def _metadata_for(self, p: Path, lib: str, sha: str) -> dict:
        """为上传文件构造 metadata dict（PLAN §5.1）。"""
        import re

        ingested_at = datetime.now().isoformat()
        try:
            rel = p.relative_to(self.data_root)
            ingest_dir = str(rel.parent).replace("\\", "/")
        except ValueError:
            ingest_dir = lib

        stem = p.stem
        tokens = [t for t in re.split(r"[\s\-_.,;:!@#$%^&()\[\]{}]+", stem) if t]
        if tokens == [stem]:
            tokens = [stem]

        return {
            "source_path": str(p),
            "ingest_dir": ingest_dir,
            "filename_tokens": tokens,
            "ingested_at": ingested_at,
            "sha256": sha,
        }

    def _handle_file(self, p: Path):
        if not p.exists() or not p.is_file():
            return
        lib = _library_for(p, self.data_root)
        if not lib:
            return
        if p.name.startswith(".") or p.name.startswith("~$"):
            return
        if p.suffix.lower() not in SUPPORTED_EXT:
            logger.info("skip unsupported %s", p.name)
            return

        size = p.stat().st_size
        sha = st.sha256_of(p)
        existing = st.get_record(str(p))
        if existing and existing.sha256 == sha and existing.status == "done":
            return  # 已处理过

        meta = self._metadata_for(p, lib, sha)

        ds_id = self._ensure_dataset(lib)
        if not ds_id:
            st.upsert(
                str(p), sha256=sha, library=lib, size=size, status="error",
                error="ensure_dataset failed",
            )
            return

        st.upsert(
            str(p), sha256=sha, library=lib, dataset_id=ds_id, size=size,
            status="uploading",
            source_path=meta["source_path"],
            ingest_dir=meta["ingest_dir"],
            filename_tokens=json.dumps(meta["filename_tokens"], ensure_ascii=False),
            ingested_at=meta["ingested_at"],
        )

        try:
            up = self.ragflow.upload_document(ds_id, p, metadata=meta)
        except Exception as e:
            logger.exception("upload %s failed", p.name)
            st.upsert(str(p), status="error", error=str(e)[:300])
            self._record_event("upload_failed", lib, p.name)
            return

        # API 返回结构: data 可能是 dict 或 list；不同 v0.25 小版本不同。
        doc_id: Optional[str] = None
        if isinstance(up, list) and up:
            doc_id = up[0].get("id") if isinstance(up[0], dict) else None
        elif isinstance(up, dict):
            doc_id = up.get("id") or (up.get("data", [{}])[0].get("id") if isinstance(up.get("data"), list) else None)

        st.upsert(
            str(p), ragflow_doc_id=doc_id, status="parsing",
            uploaded_at=datetime.now().isoformat(),
        )

        if doc_id:
            try:
                self.ragflow.parse_documents(ds_id, [doc_id])
            except Exception as e:
                logger.warning("parse trigger failed for %s: %s", p.name, e)

        st.upsert(str(p), status="done", parsed_at=datetime.now().isoformat(), error=None)
        self._record_event("uploaded", lib, p.name)
        toast("RAG: 文件已入库", f"{lib} ← {p.name}")

    def reconcile_local_to_remote(self):
        """启动期：扫盘把缺失文件补传。"""
        if not self.data_root.exists():
            return
        for sub in sorted([d for d in self.data_root.iterdir() if d.is_dir()]):
            if sub.name.startswith("."):
                continue
            self._ensure_dataset(sub.name)
            for f in sub.rglob("*"):
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXT:
                    self.enqueue_file(f)

    def reconcile_remote_to_local(self):
        """轮询：远端新建库 → 本地建文件夹。"""
        cache = self._refresh_lib_cache(force=True)
        for name in cache.keys():
            local = self.data_root / name
            if not local.exists():
                local.mkdir(parents=True, exist_ok=True)
                self._record_event("local_dir_created", name, str(local))
                toast("RAG: 远端新库已同步本地目录", name)

    # ------------------------------------------------------------------
    # tail buffer
    # ------------------------------------------------------------------
    def _record_event(self, kind: str, target: str, detail: str):
        self.recent_events.append({
            "ts": datetime.now().isoformat(),
            "kind": kind,
            "target": target,
            "detail": detail,
        })
        # cap
        if len(self.recent_events) > 500:
            self.recent_events = self.recent_events[-300:]
