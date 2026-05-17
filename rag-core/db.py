"""SQLite state 持久化：files / query_logs / audit_runs / audit_pair_cache。

替代调度层的内存日志（档案痛点 8）和审计的散落 JSON。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine


class FileRecord(SQLModel, table=True):
    __tablename__ = "files"

    path: str = Field(primary_key=True)
    sha256: str
    library: str
    dataset_id: Optional[str] = None
    ragflow_doc_id: Optional[str] = None
    status: str = Field(default="pending", index=True)  # pending/uploading/parsing/done/error
    error: Optional[str] = None
    size: Optional[int] = None
    uploaded_at: Optional[str] = None
    parsed_at: Optional[str] = None
    source_path: Optional[str] = None
    ingest_dir: Optional[str] = None
    filename_tokens: Optional[str] = None
    ingested_at: Optional[str] = None
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class QueryLog(SQLModel, table=True):
    __tablename__ = "query_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: str = Field(default_factory=lambda: datetime.now().isoformat(), index=True)
    query: str
    complexity: Optional[str] = None
    domain: Optional[str] = None
    model: Optional[str] = None
    elapsed: Optional[float] = None
    pharmacy_mode: Optional[bool] = None
    cost_estimate: Optional[str] = None
    filters_used: Optional[str] = None


class AuditRun(SQLModel, table=True):
    __tablename__ = "audit_runs"

    id: Optional[int] = Field(default=None, primary_key=True)
    started_at: str
    finished_at: Optional[str] = None
    embedding_pairs: int = 0
    flash_calls: int = 0
    opus_calls: int = 0
    cost_estimate: Optional[float] = None
    findings_count: int = 0
    seen: bool = False
    report_path: Optional[str] = None
    status: str = "running"   # running/completed/failed


class AuditPairCache(SQLModel, table=True):
    __tablename__ = "audit_pair_cache"

    sha_a: str = Field(primary_key=True)
    sha_b: str = Field(primary_key=True)
    flash_verdict: Optional[str] = None
    opus_verdict: Optional[str] = None
    judged_at: str = Field(default_factory=lambda: datetime.now().isoformat())


_engine = None


def init_db(state_db: Path):
    """Initialise the sqlite engine and create tables. Safe to call multiple times."""
    global _engine
    state_db.parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(f"sqlite:///{state_db.as_posix()}", echo=False)
    SQLModel.metadata.create_all(_engine)
    return _engine


def get_engine():
    if _engine is None:
        raise RuntimeError("db not initialised; call init_db() first")
    return _engine


def session() -> Session:
    return Session(get_engine())
