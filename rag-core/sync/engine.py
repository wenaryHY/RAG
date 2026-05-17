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
from .ocr import needs_ocr, ocr_file

logger = logging.getLogger("rag-core.sync")

# 已索引扩展名（其他类型仍上传，但日志标注一下）
SUPPORTED_EXT = {
    ".pdf", ".txt", ".md", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".html", ".htm", ".csv", ".json", ".rtf", ".epub",
    ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp",
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
        self._lock = threading.Lock()  # _pending 用

        self._observer: Optional[Observer] = None
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []

        # cache: library_name -> dataset id（多线程: worker / poller / FastAPI 都读）
        self._lib_cache: dict[str, str] = {}
        self._lib_cache_at = 0.0
        self._cache_lock = threading.RLock()

        # cache: library_name -> {doc_name: file_size}（防止重复上传）
        self._doc_cache: dict[str, dict[str, int]] = {}
        self._doc_lock = threading.Lock()

        # in-memory tail（worker 写, SSE/HTTP 读）
        self.recent_events: list[dict] = []
        self._events_lock = threading.Lock()

        self._initialized = False

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def start(self):
        self.data_root.mkdir(parents=True, exist_ok=True)

        self._observer = Observer()
        self._observer.schedule(_Handler(self), str(self.data_root), recursive=True)
        self._observer.start()
        logger.info("watchdog observer watching %s", self.data_root)

        for name, fn in (
            ("worker", self._worker_loop),
            ("debouncer", self._debounce_loop),
            ("poller", self._poll_loop),
            ("parse-poller", self._parse_poll_loop),
            ("init", self._init_loop),
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
        with self._cache_lock:
            libraries = list(self._lib_cache.keys())
        with self._events_lock:
            recent = list(self.recent_events[-20:])
        return {
            "data_root": str(self.data_root),
            "queue_size": self._queue.qsize(),
            "pending": len(self._pending),
            "libraries": libraries,
            "observer_alive": bool(self._observer and self._observer.is_alive()),
            "recent_events": recent,
            "initialized": self._initialized,
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

    def _parse_poll_loop(self):
        """后台轮询 RAGFlow 文档解析状态。

        每 60s 查一次 status="parsing" 的文件，通过 RAGFlow API 获取
        真实 run_status，RUNNING→继续等，DONE→更新为 completed，
        FAIL/CANCEL→记录错误。
        DONE + chunk_count=0 → 检测是否需 OCR 回退。
        """
        POLL_SEC = 60
        while not self._stop.is_set():
            self._stop.wait(POLL_SEC)
            if self._stop.is_set():
                return
            try:
                parsing_rows = st.list_by_status("parsing")
            except Exception as e:
                logger.warning("parse poll: list parsing failed: %s", e)
                continue
            for rec in parsing_rows:
                if not rec.dataset_id or not rec.ragflow_doc_id:
                    continue
                try:
                    doc = self.ragflow.get_document(rec.dataset_id, rec.ragflow_doc_id)
                except Exception as e:
                    logger.warning("parse poll: get_document %s failed: %s", rec.path, e)
                    continue
                run_status = doc.get("run_status", "") if isinstance(doc, dict) else ""
                chunk_count = doc.get("chunk_count", -1) if isinstance(doc, dict) else -1

                if run_status == "DONE":
                    if chunk_count == 0 and rec.path:
                        self._handle_zero_chunk(rec, doc)
                    else:
                        st.upsert(
                            rec.path, status="done",
                            parsed_at=datetime.now().isoformat(), error=None,
                        )
                        self._record_event("parse_complete", rec.library, rec.path)
                        toast("RAG: 解析完成", f"{rec.library} ← {Path(rec.path).name}")
                elif run_status in ("FAIL", "CANCEL"):
                    err_msg = doc.get("error", run_status) if isinstance(doc, dict) else run_status
                    self._handle_zero_chunk(rec, doc) if chunk_count == 0 else (
                        st.upsert(rec.path, status="error", error=str(err_msg)[:300]),
                        self._record_event("parse_failed", rec.library, rec.path),
                        logger.warning("parse failed for %s: %s", rec.path, err_msg),
                    )

    def _handle_zero_chunk(self, rec, doc: dict):
        """处理 RAGFlow 解析完成但 0 chunk 的文件。

        尝试 OCR 回退（仅图片/EPUB），失败则标记 error。
        """
        p = Path(rec.path)
        if not p.exists():
            st.upsert(rec.path, status="error", error="original file missing")
            return

        suff = p.suffix.lower()
        if needs_ocr(p) or suff in {".pdf", ".docx", ".pptx", ".xlsx"}:
            logger.info("parse poll: 0-chunk, trying OCR for %s", p.name)
            ocr_txt = ocr_file(p)
            if ocr_txt and ocr_txt.exists():
                # 删掉 RAGFlow 中 0-chunk 的文档，改为上传 OCR 产物
                try:
                    self.ragflow.delete_documents(rec.dataset_id, [rec.ragflow_doc_id])
                except Exception as e:
                    logger.warning("failed to delete 0-chunk doc %s: %s", rec.ragflow_doc_id, e)
                st.upsert(rec.path, status="error", error="RAGFlow produced 0 chunks")
                self.enqueue_file(ocr_txt)
                return

        st.upsert(rec.path, status="error",
                  error="RAGFlow produced 0 chunks (文件可能为纯扫描图片, 需手动 OCR)")
        self._record_event("parse_failed", rec.library, rec.path)

    def _init_loop(self):
        """后台线程：自适应等待 RAGFlow 就绪后完成首次 reconcile。

        指数退避检测 list_datasets() 可达性。
        就绪则立即 reconcile，不阻塞 FastAPI 启动。
        poll_loop 独立持续兜底，初始化失败不崩溃。
        """
        if self._wait_for_ragflow(timeout=300):
            self._refresh_lib_cache(force=True)
            self.reconcile_local_to_remote()
            self.reconcile_remote_to_local()
            self._initialized = True
            logger.info("sync engine fully initialised")
        else:
            logger.warning("sync engine started but RAGFlow unreachable; poll_loop will retry")
        # 即使失败也标 initialized（不让 Panel 一直转圈）
        self._initialized = True

    def _wait_for_ragflow(self, timeout: int = 300) -> bool:
        """指数退避检测 RAGFlow 可达性。

        每轮调 list_datasets()，成功立即返回 True。
        失败则按 3→4.5→6.8→...→60s 递增等待，最长 timeout 秒。
        """
        deadline = time.time() + timeout
        interval = 3.0
        while time.time() < deadline:
            try:
                self.ragflow.list_datasets()
                elapsed = timeout - (deadline - time.time())
                logger.info("RAGFlow ready after %.1fs", elapsed)
                return True
            except Exception:
                remaining = max(0.0, deadline - time.time())
                wait = min(interval, remaining)
                if wait <= 0:
                    break
                logger.info("RAGFlow not ready, retry in %.1fs (%.0fs remaining)", wait, remaining)
                if self._stop.wait(wait):
                    return False  # 被 stop 中断
                interval = min(interval * 1.5, 60.0)

        logger.error("RAGFlow unreachable after %ds", timeout)
        return False
    def _refresh_lib_cache(self, *, force: bool = False) -> dict[str, str]:
        with self._cache_lock:
            if not force and time.time() - self._lib_cache_at < 10:
                return dict(self._lib_cache)
        # 网络调用放锁外, 避免阻塞其他读
        try:
            datasets = self.ragflow.list_datasets()
        except Exception as e:
            logger.warning("list_datasets failed: %s", e)
            with self._cache_lock:
                return dict(self._lib_cache)
        with self._cache_lock:
            self._lib_cache = {d["name"]: d["id"] for d in datasets if d.get("id")}
            self._lib_cache_at = time.time()
            lib_cache_snapshot = dict(self._lib_cache)
        # 异步刷新文档列表缓存（用于去重）
        self._refresh_doc_cache()
        return lib_cache_snapshot

    def _refresh_doc_cache(self):
        """拉取各知识库的文档列表，缓存 {库名: {文档名: 文件大小}} 用于去重。"""
        with self._cache_lock:
            libs = dict(self._lib_cache)
        for lib_name, ds_id in libs.items():
            try:
                docs = self.ragflow.list_documents(ds_id, page=1, page_size=200)
            except Exception:
                continue
            local: dict[str, int] = {}
            for d in docs:
                name = d.get("name", "")
                size = d.get("size", 0)
                if name and size:
                    local[name] = size
                elif name:
                    local[name] = 0
            with self._doc_lock:
                self._doc_cache[lib_name] = local

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
            with self._cache_lock:
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
        if p.name in ("Thumbs.db", "desktop.ini", ".DS_Store"):
            return
        if p.suffix.lower() in (".tmp", ".lock", ".part"):
            return
        if p.suffix.lower() not in SUPPORTED_EXT:
            logger.info("skip unsupported %s", p.name)
            return

        # ---- OCR 预处理: 图片/扫描版 EPUB/PDF → .md ----
        ocr_txt: Optional[Path] = None
        if needs_ocr(p):
            ocr_txt = ocr_file(p)
            if ocr_txt and ocr_txt.exists():
                self.enqueue_file(ocr_txt)  # FileSync 自动拾取 .md 上传
        # ---- end OCR ----

        size = p.stat().st_size
        sha = st.sha256_of(p)
        existing = st.get_record(str(p))
        if existing and existing.sha256 == sha and existing.status == "done":
            return  # 已处理过

        # ---- 服务端去重: 同名同大小 → 跳过上传 ----
        with self._doc_lock:
            doc_sizes = self._doc_cache.get(lib, {})
        cached_size = doc_sizes.get(p.name)
        if cached_size is not None and cached_size == size:
            st.upsert(str(p), sha256=sha, library=lib, size=size, status="done")
            self._record_event("dedup_skipped", lib, p.name)
            return
        # ---- end 去重 ----

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
                st.upsert(str(p), status="error", error=f"parse trigger: {e}"[:300])
                self._record_event("parse_failed", lib, p.name)
                return

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
        with self._events_lock:
            self.recent_events.append({
                "ts": datetime.now().isoformat(),
                "kind": kind,
                "target": target,
                "detail": detail,
            })
            # cap
            if len(self.recent_events) > 500:
                self.recent_events = self.recent_events[-300:]

    def snapshot_events(self, since: int = 0) -> tuple[list[dict], int]:
        """SSE 用的原子快照: 返回 (新事件列表, 当前 cursor)。

        - since: 上次 cursor，0 表示首次
        - 内部处理 ring-buffer 截断: 若 since > 当前总长说明被截断, 退一档
        - 返回的 cursor 等于"已发送总数"
        """
        with self._events_lock:
            total_seen = getattr(self, "_events_seq", 0)
            if not hasattr(self, "_events_seq"):
                self._events_seq = len(self.recent_events)
                total_seen = self._events_seq
            # 若调用方落后过多 (被 ring-buffer 截断)，从手头最早事件开始
            buf_len = len(self.recent_events)
            backlog_start = max(0, total_seen - buf_len)
            if since < backlog_start:
                since = backlog_start
            offset = since - backlog_start
            new_events = list(self.recent_events[offset:])
            return new_events, backlog_start + buf_len
