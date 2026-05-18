"""rag-tray: 系统托盘代理。

- 15s 轮询 rag-core /api/services/health -> 三色图标
- 右键菜单: 打开面板/立即同步/运行审计/服务状态/重连/退出
- 双击托盘 = 打开浏览器 http://127.0.0.1:8840/

设计:
- pystray 在主线程跑事件循环, 后台线程做轮询
- 不能用 NSSM (托盘必须在用户会话) -> Phase 3 用 Task Scheduler
"""
from __future__ import annotations

import sys
import time
import threading
import webbrowser
from io import BytesIO

import requests
from PIL import Image, ImageDraw
import pystray
from pystray import Menu, MenuItem

import freeze

CORE_BASE = "http://127.0.0.1:8840"
PANEL_URL = CORE_BASE + "/"
POLL_SEC = 15

try:
    from winotify import Notification
except Exception:
    Notification = None


def toast(title: str, msg: str):
    if Notification is None:
        return
    try:
        Notification(app_id="RAG System", title=title, msg=msg, duration="short").show()
    except Exception:
        pass


def make_icon(color: str) -> Image.Image:
    """64x64 圆点图标。"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    palette = {
        "green":  (34, 197, 94),
        "yellow": (234, 179, 8),
        "red":    (239, 68, 68),
        "gray":   (148, 163, 184),
        "blue":   (59, 130, 246),
    }
    rgb = palette.get(color, palette["gray"])
    d.ellipse((6, 6, 58, 58), fill=rgb + (255,), outline=(15, 23, 42, 255), width=3)
    return img


class TrayApp:
    def __init__(self):
        self._stop = threading.Event()
        self._state = {"overall": "gray", "checks": {}, "last_err": None}
        self._icon: pystray.Icon | None = None
        self._last_overall = None

    # ------------------------------------------------------------------
    def fetch_health(self) -> dict:
        r = requests.get(f"{CORE_BASE}/api/services/health", timeout=5)
        r.raise_for_status()
        return r.json()

    def _poll_loop(self):
        while not self._stop.is_set():
            if freeze.is_frozen():
                self._state["overall"] = "blue"
                self._update_icon()
                self._stop.wait(POLL_SEC)
                continue
            try:
                data = self.fetch_health()
                self._state.update(data)
                self._state["last_err"] = None
            except Exception as e:
                self._state["overall"] = "red"
                self._state["last_err"] = str(e)[:120]
            self._update_icon()
            self._stop.wait(POLL_SEC)

    def _update_icon(self):
        if not self._icon:
            return
        overall = self._state.get("overall", "gray")
        self._icon.icon = make_icon(overall)
        checks = self._state.get("checks") or {}
        title = f"RAG: {overall}"
        if checks:
            title += " | " + " ".join(f"{k}={v}" for k, v in checks.items())
        if self._state.get("last_err"):
            title = f"RAG: down ({self._state['last_err']})"
        self._icon.title = title[:127]

        # 红->绿/绿->红 边沿提示
        if self._last_overall and self._last_overall != overall:
            if overall == "red":
                toast("RAG 服务异常", self._state.get("last_err") or "health red")
            elif overall == "green" and self._last_overall == "red":
                toast("RAG 已恢复", "all green")
        self._last_overall = overall

    # ------------------------------------------------------------------
    # menu actions
    def _open_panel(self, *_):
        webbrowser.open(PANEL_URL)

    def _trigger_reconcile(self, *_):
        try:
            requests.post(f"{CORE_BASE}/sync/reconcile", timeout=5)
            toast("RAG", "已触发同步")
        except Exception as e:
            toast("RAG", f"同步失败: {e}")

    def _show_status(self, *_):
        d = self._state
        msg = f"overall={d.get('overall')}\n"
        for k, v in (d.get("checks") or {}).items():
            msg += f"{k}: {v}\n"
        if d.get("last_err"):
            msg += f"err: {d['last_err']}\n"
        toast("RAG 状态", msg)

    def _refresh_now(self, *_):
        try:
            data = self.fetch_health()
            self._state.update(data); self._state["last_err"] = None
        except Exception as e:
            self._state["overall"] = "red"; self._state["last_err"] = str(e)[:120]
        self._update_icon()

    def _quit(self, icon, *_):
        self._stop.set()
        icon.stop()

    def _freeze(self, *_):
        self._state["overall"] = "blue"  # 立即设蓝，避免闪烁
        self._update_icon()
        def _run():
            result = freeze.freeze()
            toast("RAG 已冻结", result.get("status", "?"))
        threading.Thread(target=_run, name="tray-freeze", daemon=True).start()

    def _thaw(self, *_):
        if not freeze.is_frozen():
            toast("RAG", "当前未冻结")
            return
        def _run():
            result = freeze.thaw()
            if result.get("status") == "skipped":
                return
            toast("RAG 恢复中", result.get("status", "?"))
            self._refresh_now()
        threading.Thread(target=_run, name="tray-thaw", daemon=True).start()

    def _is_frozen(self) -> bool:
        return freeze.is_frozen()

    def _build_menu(self):
        return Menu(
            MenuItem("打开面板", self._open_panel, default=True),
            MenuItem("立即同步", self._trigger_reconcile),
            MenuItem("查看状态", self._show_status),
            MenuItem("立即刷新", self._refresh_now),
            Menu.SEPARATOR,
            MenuItem(
                "冻结后台 (释放资源)",
                self._freeze,
                enabled=lambda item: not freeze.is_frozen(),
            ),
            MenuItem(
                "恢复后台",
                self._thaw,
                enabled=lambda item: freeze.is_frozen(),
            ),
            Menu.SEPARATOR,
            MenuItem("退出", self._quit),
        )

    # ------------------------------------------------------------------
    def run(self):
        init_color = "blue" if freeze.is_frozen() else "gray"
        init_title = "RAG: frozen" if freeze.is_frozen() else "RAG: starting"
        self._icon = pystray.Icon(
            "rag-tray",
            icon=make_icon(init_color),
            title=init_title,
            menu=self._build_menu(),
        )
        if freeze.is_frozen():
            self._state["overall"] = "blue"
        t = threading.Thread(target=self._poll_loop, name="tray-poll", daemon=True)
        t.start()
        self._icon.run()


if __name__ == "__main__":
    TrayApp().run()
