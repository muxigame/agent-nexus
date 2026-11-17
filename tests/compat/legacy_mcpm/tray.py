"""System tray icon.

The icon carries one status dot for all services (green: running, amber:
starting, red: something crashed or is unhealthy) and the tooltip names the
broken ones. Observe-only services (a browser mcpm watches) never turn the dot
red: a problem there is amber, and the watched thing being closed is normal. Left click opens the console; the right-click menu lists every
service with its status and start / restart / stop. Quitting stops the manager
and all owned or safely adopted service process trees.

pystray is imported here, not by the CLI, so a box without a desktop session
can still run ``mcpm serve`` - the CLI falls back to running without a tray.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import pystray
from PIL import Image, ImageDraw

from .desktop import open_console
from .supervisor import Supervisor, SupervisorError

ICON_PATH = Path(__file__).parent / "static" / "icon.png"
REFRESH_S = 2.0
TITLE = "MCP 管理器"
STATUS_TEXT = {
    "running": "运行中", "starting": "启动中", "unhealthy": "不健康", "crashed": "已崩溃",
    "conflict": "端口冲突", "external": "外部进程", "stopped": "已停止", "disabled": "已禁用",
    "unsupported": "不支持此平台", "invalid": "配置错误",
}
PROBLEMS = {"crashed", "conflict", "invalid", "unhealthy"}
ALERTS = {"crashed", "conflict"}
TONES = {"ok": (34, 197, 94), "warn": (245, 158, 11), "bad": (239, 68, 68)}


def observed(svc: dict[str, Any]) -> bool:
    return svc.get("kind") == "observe"


def status_text(svc: dict[str, Any]) -> str:
    if observed(svc) and svc["status"] == "stopped":
        return "未运行"
    return STATUS_TEXT.get(svc["status"], svc["status"])


def overall_tone(services: list[dict[str, Any]]) -> str | None:
    managed = {s["status"] for s in services if not observed(s)}
    watched = {s["status"] for s in services if observed(s)}
    if managed & PROBLEMS:
        return "bad"
    if "starting" in managed or watched & PROBLEMS:
        return "warn"
    if "running" in managed | watched:
        return "ok"
    return None


def summary(services: list[dict[str, Any]]) -> str:
    runnable = [s for s in services if s["enabled"] and s["supported"] and not observed(s)]
    running = sum(1 for s in runnable if s["status"] == "running")
    text = f"{TITLE} · {running}/{len(runnable)} 运行中"
    broken = [s["name"] for s in services if s["status"] in PROBLEMS]
    if broken:
        text += " · 异常: " + ", ".join(broken)
    return text[:63]  # Windows rejects tooltips longer than 63 characters


def badge(base: Image.Image, tone: str | None) -> Image.Image:
    """``base`` with a status dot in the bottom-right corner."""
    image = base.copy()
    if tone is None:
        return image
    size = image.width
    r = max(3, round(size * 0.2))
    c = size - r - 2
    ring = max(1, size // 24)
    draw = ImageDraw.Draw(image)
    draw.ellipse((c - r - ring, c - r - ring, c + r + ring, c + r + ring), fill=(255, 255, 255, 255))
    draw.ellipse((c - r, c - r, c + r, c + r), fill=(*TONES[tone], 255))
    return image


class Tray:
    def __init__(self, sup: Supervisor, url: str, request_exit: Callable[[], None]):
        self.sup, self.url, self.request_exit = sup, url, request_exit
        self.base = Image.open(ICON_PATH).convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
        self.services = sup.snapshots()
        self._key: list[tuple] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.icon = pystray.Icon("mcpm", badge(self.base, overall_tone(self.services)),
                                 summary(self.services), menu=pystray.Menu(self._items))

    # -- lifecycle ------------------------------------------------------------------

    def run(self) -> None:
        """Blocks; must be called on the main thread (macOS requires it)."""
        def setup(icon: pystray.Icon) -> None:
            try:
                icon.visible = True
                self.sup.tray_visible = True
            except Exception as exc:  # pystray runs setup in its own thread and would swallow this
                print(f"tray icon could not be shown: {exc}", file=sys.stderr, flush=True)
                return
            print("tray started", flush=True)  # pystray ignores Shell_NotifyIcon failures, so this is not proof the icon is visible
            threading.Thread(target=self._loop, name="mcpm-tray", daemon=True).start()

        self.icon.run(setup)

    def stop(self) -> None:
        self.sup.tray_visible = False
        self._stop.set()
        try:
            self.icon.stop()
        except Exception:  # not running yet, or already stopped
            pass

    def _loop(self) -> None:
        while not self._stop.wait(REFRESH_S):
            try:
                self.refresh()
            except Exception:  # a bad refresh must not kill the tray
                pass

    def refresh(self, force: bool = False) -> None:
        with self._lock:
            services = self.sup.snapshots()
            before = {s["name"]: s["status"] for s in self.services}
            self.services = services
            for s in services:
                if s["status"] in ALERTS and before.get(s["name"]) not in (None, s["status"]):
                    self._notify(f"{s['name']} {STATUS_TEXT[s['status']]}: {s['message']}")
            key = [(s["name"], s["status"], s["pid"], s["message"]) for s in services]
            if force or key != self._key:
                self._key = key
                self.icon.icon = badge(self.base, overall_tone(services))
                self.icon.title = summary(services)
                self.icon.update_menu()

    # -- menu ---------------------------------------------------------------------------

    def _items(self) -> list[pystray.MenuItem]:
        item, sep = pystray.MenuItem, pystray.Menu.SEPARATOR
        items = [item("打开控制台", self._open_console, default=True), sep]
        if not self.services:
            items.append(item("没有发现服务", None, enabled=False))
        for svc in self.services:
            label = f"{svc['name']}  ·  {status_text(svc)}"
            items.append(item(label, pystray.Menu(*self._service_items(svc))))
        items += [sep, item("重新扫描服务", self._rescan), item("打开 MCP 目录", self._open_folder),
                  sep, item("退出管理器并停止所有服务", self.request_exit)]
        return items

    def _service_items(self, svc: dict[str, Any]) -> list[pystray.MenuItem]:
        item, name = pystray.MenuItem, svc["name"]
        items = [item("查看状态和日志", self._open_tab(name))]
        if svc["message"]:
            items.append(item(svc["message"][:80], None, enabled=False))
        if observed(svc):
            return [*items, item("仅观察：管理器不会启动或停止它", None, enabled=False)]
        if svc["pid"] is not None:
            items += [item("重启", self._action(name, "restart")), item("停止", self._action(name, "stop"))]
        elif svc["status"] in ("stopped", "crashed") and svc["enabled"] and svc["supported"] and not svc["error"]:
            items.append(item("启动", self._action(name, "start")))
        elif svc["status"] == "external":
            # Killing a process mcpm did not start deserves the console's confirmation.
            items.append(item("被外部进程占用，到控制台接管", self._open_tab(name)))
        return items

    # -- actions ------------------------------------------------------------------------
    # pystray passes (icon, item) only to callbacks that declare them, so these
    # are zero-argument closures; a lambda default argument would receive the icon.

    def _open_console(self) -> None:
        open_console(self.url)

    def _open_tab(self, name: str) -> Callable[[], None]:
        def open_tab() -> None:
            # The window keeps the hash, so it lands on the service's own tab.
            open_console(f"{self.url}/#tab={quote(name, safe='')}")
        return open_tab

    def _action(self, name: str, verb: str) -> Callable[[], None]:
        def run() -> None:
            # Stopping waits for the port to close; keep the menu responsive.
            threading.Thread(target=self._perform, args=(name, verb), daemon=True).start()
        return run

    def _perform(self, name: str, verb: str) -> None:
        try:
            message = getattr(self.sup, verb)(name)
            self.sup.tick()
        except SupervisorError as exc:
            message = f"{name}: {exc}"
        self._notify(message)
        try:
            self.refresh(force=True)
        except Exception:
            pass

    def _rescan(self) -> None:
        def run() -> None:
            self.sup.rescan()
            self.sup.tick()
            self.refresh(force=True)
        threading.Thread(target=run, daemon=True).start()

    def _open_folder(self) -> None:
        root = str(self.sup.cfg.root)
        if sys.platform == "win32":
            os.startfile(root)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", root])

    def _notify(self, message: str) -> None:
        if not pystray.Icon.HAS_NOTIFICATION:
            return
        try:
            self.icon.notify(message[:250], TITLE)
        except Exception:  # notifications are best effort
            pass
