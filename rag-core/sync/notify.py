"""桌面通知（winotify）。失败 silent，避免阻塞同步。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("rag-core.sync.notify")

try:
    from winotify import Notification, audio  # type: ignore
    _AVAILABLE = True
except Exception as e:  # pragma: no cover - import-time
    _AVAILABLE = False
    logger.warning("winotify unavailable: %s", e)


def toast(title: str, message: str, *, app_name: str = "RAG System") -> None:
    if not _AVAILABLE:
        return
    try:
        n = Notification(app_id=app_name, title=title, msg=message, duration="short")
        try:
            n.set_audio(audio.Default, loop=False)
        except Exception:
            pass
        n.show()
    except Exception as e:  # noqa: BLE001
        logger.debug("toast failed: %s", e)
