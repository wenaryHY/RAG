"""墓碑机制：冻结/恢复 RAG 后台服务。

冻结 → 写 state.frozen → nssm stop → docker compose stop
恢复 → docker compose start → nssm start → 等待健康 → 删墓碑
失败不删墓碑，可幂等重试。
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

import requests

logger = logging.getLogger("rag-tray.freeze")

TOMBSTONE = Path("D:/RAG/state.frozen")
CORE_URL = "http://127.0.0.1:8840"
COMPOSE_FILE = "D:/RAG/RAGFlow/docker/docker-compose.yml"
NSSM_PATH = "D:/RAG/tools/nssm.exe"
HEALTH_TIMEOUT_SEC = 180

_CREATE_NO_WINDOW = 0x08000000


def is_frozen() -> bool:
    return TOMBSTONE.exists()


def read_tombstone() -> dict | None:
    if not TOMBSTONE.exists():
        return None
    try:
        return json.loads(TOMBSTONE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_tombstone(data: dict):
    TOMBSTONE.parent.mkdir(parents=True, exist_ok=True)
    TOMBSTONE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _run(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            args, capture_output=True, text=True,
            timeout=timeout, creationflags=_CREATE_NO_WINDOW,
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", f"command not found: {args[0]}"


def freeze() -> dict:
    """冻结所有后台服务。返回 {"status": "ok"|"partial"|"error", ...}"""
    result = {"status": "ok", "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "steps": []}

    rag_was_alive = False
    try:
        r = requests.get(f"{CORE_URL}/health", timeout=3)
        rag_was_alive = r.status_code == 200
    except Exception:
        pass

    # 墓碑必须在停服务前写入（原子性）
    _write_tombstone({
        "frozen_at": result["frozen_at"],
        "ragflow_was_running": True,
        "rag_core_was_running": rag_was_alive,
    })
    result["steps"].append("tombstone written")

    rc, out, err = _run([NSSM_PATH, "stop", "rag-core"], timeout=15)
    if rc == 0:
        result["steps"].append("rag-core stopped")
    else:
        rc2, _, _ = _run(["sc", "stop", "rag-core"], timeout=15)
        if rc2 == 0:
            result["steps"].append("rag-core stopped (via sc)")
        else:
            result["steps"].append(f"rag-core stop failed: {err[:80]}")
            if result["status"] == "ok":
                result["status"] = "partial"

    rc, out, err = _run(["docker", "compose", "-f", COMPOSE_FILE, "stop"], timeout=30)
    if rc == 0:
        result["steps"].append("docker compose stop OK")
    else:
        result["steps"].append(f"docker compose stop: {err[:80]}")
        if result["status"] == "ok":
            result["status"] = "partial"

    return result


def thaw() -> dict:
    """恢复所有后台服务。失败不删墓碑，可幂等重试。"""
    result = {"status": "ok", "thawed_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "steps": []}

    if not is_frozen():
        result["status"] = "skipped"
        result["steps"].append("not frozen")
        return result

    # docker 先启
    rc, out, err = _run(["docker", "compose", "-f", COMPOSE_FILE, "start"], timeout=60)
    if rc == 0:
        result["steps"].append("docker compose start OK")
    else:
        result["steps"].append(f"docker compose start: {err[:80]}")

    # rag-core
    rc, out, err = _run([NSSM_PATH, "start", "rag-core"], timeout=15)
    if rc == 0:
        result["steps"].append("rag-core started")
    else:
        rc2, _, _ = _run(["sc", "start", "rag-core"], timeout=15)
        if rc2 == 0:
            result["steps"].append("rag-core started (via sc)")
        else:
            result["steps"].append(f"rag-core start failed: {err[:80]}")
            if result["status"] == "ok":
                result["status"] = "partial"

    # 等待健康
    deadline = time.time() + HEALTH_TIMEOUT_SEC
    healthy = False
    while time.time() < deadline:
        try:
            r = requests.get(f"{CORE_URL}/health", timeout=3)
            if r.status_code == 200:
                healthy = True
                result["steps"].append("rag-core healthy")
                break
        except Exception:
            pass
        time.sleep(3)

    if not healthy:
        result["steps"].append(f"rag-core health timeout ({HEALTH_TIMEOUT_SEC}s)")
        if result["status"] == "ok":
            result["status"] = "partial"

    # 只有全部成功才删墓碑
    if result["status"] == "ok":
        try:
            TOMBSTONE.unlink(missing_ok=True)
            result["steps"].append("tombstone removed")
        except Exception:
            pass

    return result
