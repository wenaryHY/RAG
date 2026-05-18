"""freeze.py 测试 — 全mock，零副作用。"""
import json
import subprocess
from unittest.mock import MagicMock
from pathlib import Path
import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import freeze


@pytest.fixture(autouse=True)
def isolate_tombstone(tmp_path, monkeypatch):
    fake = tmp_path / "state.frozen"
    monkeypatch.setattr(freeze, "TOMBSTONE", fake)
    yield fake


# ---- 纯查询函数 ----

def test_is_frozen_false_when_absent(isolate_tombstone):
    assert freeze.is_frozen() is False

def test_is_frozen_true_when_present(isolate_tombstone):
    isolate_tombstone.write_text("{}", encoding="utf-8")
    assert freeze.is_frozen() is True

def test_read_tombstone_none_when_absent(isolate_tombstone):
    assert freeze.read_tombstone() is None

def test_read_tombstone_returns_dict(isolate_tombstone):
    isolate_tombstone.write_text(json.dumps({"a": 1}), encoding="utf-8")
    assert freeze.read_tombstone() == {"a": 1}

def test_read_tombstone_none_when_corrupt(isolate_tombstone):
    isolate_tombstone.write_text("not json {", encoding="utf-8")
    assert freeze.read_tombstone() is None


# ---- _run 异常处理 ----

def test_run_handles_timeout(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)
    monkeypatch.setattr(subprocess, "run", boom)
    rc, _, err = freeze._run(["x"])
    assert rc == -1 and err == "timeout"

def test_run_handles_missing_cmd(monkeypatch):
    monkeypatch.setattr(subprocess, "run", MagicMock(side_effect=FileNotFoundError()))
    rc, _, err = freeze._run(["nonexistent"])
    assert rc == -1 and "not found" in err

def test_run_uses_create_no_window(monkeypatch):
    captured = {}
    def cap(*a, **kw):
        captured.update(kw)
        m = MagicMock(); m.returncode = 0; m.stdout = ""; m.stderr = ""
        return m
    monkeypatch.setattr(subprocess, "run", cap)
    freeze._run(["echo", "hi"])
    assert captured.get("creationflags", 0) & 0x08000000


# ---- freeze 行为 ----

def test_freeze_writes_tombstone_first(isolate_tombstone, monkeypatch):
    monkeypatch.setattr(freeze, "_run", lambda *a, **kw: (-1, "", "fail"))
    monkeypatch.setattr(freeze.requests, "get",
                        MagicMock(side_effect=Exception("down")))
    freeze.freeze()
    assert isolate_tombstone.exists()
    data = json.loads(isolate_tombstone.read_text(encoding="utf-8"))
    assert "frozen_at" in data

def test_freeze_records_rag_core_alive(isolate_tombstone, monkeypatch):
    monkeypatch.setattr(freeze, "_run", lambda *a, **kw: (0, "", ""))
    fake_resp = MagicMock(); fake_resp.status_code = 200
    monkeypatch.setattr(freeze.requests, "get", MagicMock(return_value=fake_resp))
    freeze.freeze()
    data = json.loads(isolate_tombstone.read_text(encoding="utf-8"))
    assert data["rag_core_was_running"] is True

def test_freeze_records_rag_core_dead(isolate_tombstone, monkeypatch):
    monkeypatch.setattr(freeze, "_run", lambda *a, **kw: (0, "", ""))
    monkeypatch.setattr(freeze.requests, "get",
                        MagicMock(side_effect=Exception("down")))
    freeze.freeze()
    data = json.loads(isolate_tombstone.read_text(encoding="utf-8"))
    assert data["rag_core_was_running"] is False

def test_freeze_status_ok_all_succeed(isolate_tombstone, monkeypatch):
    monkeypatch.setattr(freeze, "_run", lambda *a, **kw: (0, "", ""))
    monkeypatch.setattr(freeze.requests, "get",
                        MagicMock(return_value=MagicMock(status_code=200)))
    assert freeze.freeze()["status"] == "ok"

def test_freeze_status_partial_when_step_fails(isolate_tombstone, monkeypatch):
    calls = {"n": 0}
    def stub(*a, **kw):
        calls["n"] += 1
        return (0, "", "") if calls["n"] == 1 else (-1, "", "boom")
    monkeypatch.setattr(freeze, "_run", stub)
    monkeypatch.setattr(freeze.requests, "get",
                        MagicMock(return_value=MagicMock(status_code=200)))
    assert freeze.freeze()["status"] == "partial"


# ---- thaw 行为 ----

def test_thaw_skipped_when_not_frozen(isolate_tombstone):
    result = freeze.thaw()
    assert result["status"] == "skipped"

def test_thaw_calls_docker_then_nssm(isolate_tombstone, monkeypatch):
    isolate_tombstone.write_text("{}", encoding="utf-8")
    order: list[str] = []
    def stub(args, **kw):
        order.append(args[0])
        return (0, "", "")
    monkeypatch.setattr(freeze, "_run", stub)
    monkeypatch.setattr(freeze.requests, "get",
                        MagicMock(return_value=MagicMock(status_code=200)))
    monkeypatch.setattr(freeze, "HEALTH_TIMEOUT_SEC", 1)
    freeze.thaw()
    assert order[0] == "docker", f"docker should be first, got {order}"
    assert any("nssm" in a.lower() for a in order[1:]), order

def test_thaw_removes_tombstone_on_success(isolate_tombstone, monkeypatch):
    isolate_tombstone.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(freeze, "_run", lambda *a, **kw: (0, "", ""))
    monkeypatch.setattr(freeze.requests, "get",
                        MagicMock(return_value=MagicMock(status_code=200)))
    monkeypatch.setattr(freeze, "HEALTH_TIMEOUT_SEC", 1)
    freeze.thaw()
    assert not isolate_tombstone.exists()

def test_thaw_keeps_tombstone_on_health_timeout(isolate_tombstone, monkeypatch):
    isolate_tombstone.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(freeze, "_run", lambda *a, **kw: (0, "", ""))
    monkeypatch.setattr(freeze.requests, "get",
                        MagicMock(side_effect=Exception("not ready")))
    monkeypatch.setattr(freeze, "HEALTH_TIMEOUT_SEC", 0.5)
    result = freeze.thaw()
    assert isolate_tombstone.exists(), "tombstone should remain on partial recovery"
    assert result["status"] == "partial"

def test_thaw_idempotent(isolate_tombstone, monkeypatch):
    isolate_tombstone.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(freeze, "_run", lambda *a, **kw: (0, "", ""))
    monkeypatch.setattr(freeze.requests, "get",
                        MagicMock(return_value=MagicMock(status_code=200)))
    monkeypatch.setattr(freeze, "HEALTH_TIMEOUT_SEC", 1)
    r1 = freeze.thaw()
    r2 = freeze.thaw()
    assert r1["status"] == "ok"
    assert r2["status"] == "skipped"
