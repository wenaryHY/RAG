"""sync/state 的 CRUD 正确性（脱机 SQLite）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import init_db, FileRecord, session
from sync import state as st


@pytest.fixture(autouse=True)
def _setup_db(tmp_path):
    import db as _db
    db_path = tmp_path / "test_state.sqlite"
    _db.init_db(db_path)
    yield
    try:
        engine = _db.get_engine()
        engine.dispose()
    except Exception:
        pass
    try:
        db_path.unlink(missing_ok=True)
    except PermissionError:
        pass


def test_upsert_new():
    rec = st.upsert("D:/test/file.pdf", sha256="abc", library="testlib", size=100)
    assert rec.path == "D:/test/file.pdf"
    assert rec.sha256 == "abc"
    assert rec.library == "testlib"
    assert rec.status == "pending"


def test_upsert_existing():
    st.upsert("D:/test/file.pdf", sha256="abc", library="testlib", status="uploading")
    st.upsert("D:/test/file.pdf", status="done")
    rec = st.get_record("D:/test/file.pdf")
    assert rec.status == "done"
    assert rec.sha256 == "abc"           # 未改字段应保留


def test_upsert_metadata_fields():
    st.upsert(
        "D:/test/foo.pdf", sha256="def", library="pharmacy",
        source_path="D:/RAG/RAGfiles/pharmacy/2024/foo.pdf",
        ingest_dir="pharmacy/2024",
        filename_tokens='["药二星","试卷3","2024"]',
        ingested_at="2026-05-17T00:00:00",
    )
    rec = st.get_record("D:/test/foo.pdf")
    assert rec.source_path == "D:/RAG/RAGfiles/pharmacy/2024/foo.pdf"
    assert rec.ingest_dir == "pharmacy/2024"
    assert rec.filename_tokens == '["药二星","试卷3","2024"]'


def test_list_recent():
    for i in range(5):
        st.upsert(f"D:/test/file{i}.pdf", sha256=f"sha{i}", library="lib")
    recent = st.list_recent(limit=3)
    assert len(recent) == 3


def test_list_by_status():
    st.upsert("D:/test/a.pdf", sha256="a", library="lib", status="done")
    st.upsert("D:/test/b.pdf", sha256="b", library="lib", status="error")
    st.upsert("D:/test/c.pdf", sha256="c", library="lib", status="parsing")
    done = st.list_by_status("done")
    assert len(done) == 1
    assert done[0].path == "D:/test/a.pdf"
