"""sha256 + 状态管理。"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlmodel import select

import db


def sha256_of(path: Path, *, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def get_record(path: str) -> Optional[db.FileRecord]:
    with db.session() as s:
        return s.get(db.FileRecord, path)


def upsert(path: str, **fields) -> db.FileRecord:
    with db.session() as s:
        rec = s.get(db.FileRecord, path)
        if rec is None:
            # 兜底必填字段
            fields.setdefault("sha256", "")
            fields.setdefault("library", "")
            rec = db.FileRecord(path=path, **fields)
        else:
            for k, v in fields.items():
                setattr(rec, k, v)
        rec.updated_at = datetime.now().isoformat()
        s.add(rec)
        s.commit()
        s.refresh(rec)
        return rec


def list_by_status(status: str) -> list[db.FileRecord]:
    with db.session() as s:
        return list(s.exec(select(db.FileRecord).where(db.FileRecord.status == status)))


def list_recent(limit: int = 20) -> list[db.FileRecord]:
    with db.session() as s:
        return list(
            s.exec(
                select(db.FileRecord)
                .order_by(db.FileRecord.updated_at.desc())  # type: ignore[attr-defined]
                .limit(limit)
            )
        )
