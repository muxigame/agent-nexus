"""System tray icon.

The icon carries one status dot for all services (green: running, amber:
starting, red: something crashed or is unhealthy) and the tooltip names the
broken ones. Observe-only services (a browser mcpm watches) never turn the dot
red: a problem there is amber, and the watched thing being closed is normal. Left click opens the console; the right-click menu lists every
service with its status and start / restart / stop. Quitting stops the manager
and all owned or safely adopted service process trees.

The desktop shell's state shows up here too (design section 9.2, decision D7):
the bottom-right dot is green while the gateway link is up and grey while it is
not (red / amber still win when a service is broken), and an orange up-arrow in
the top-left corner means the shell found an update. The menu offers
"更新到 vX.Y.Z" (or "检查更新"), the "开机自启" switch and "收件箱（N）". The
shell does the work: the tray only runs ``agent-nexus.exe <verb>``.

pystray is imported here, not by the CLI, so a box without a desktop session
can still run ``mcpm serve`` - the CLI falls back to running without a tray.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import pystray
from PIL import Image, ImageDraw

from .desktop import autostart_enabled, open_console, read_update, run_verb
from .supervisor import Supervisor, SupervisorError

ICON_PATH = Path(__file__).parent / "static" / "icon.png"
REFRESH_S = 2.0
TITLE = "Agent Nexus"
STATUS_TEXT = {
    "running": "运行中", "starting": "启动中", "unhealthy": "不健康", "crashed": "已崩溃",
    "conflict": "端口冲突", "external": "外部进程", "stopped": "已停止", "disabled": "已禁用",
    "unsupported": "不支持此平台", "invalid": "配置错误",
    "not_installed": "未安装", "needs_config": "需要配置",
}
PROBLEMS = {"crashed", "conflict", "invalid", "unhealthy"}
ALERTS = {"crashed", "conflict"}
TONES = {"ok": (34, 197, 94), "warn": (245, 158, 11), "bad": (239, 68, 68)}
#: Link dot when the gateway is not connected, and the update arrow's disc.
OFFLINE_GREY = (148, 163, 184)
UPDATE_ORANGE = (249, 115, 22)
#: How often the inbox count is asked for (it is served by the gateway).
INBOX_REFRESH_S = 30.0


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
    runnable = [s for s in services if s["enabled"] and s["supported"] and s.get("installed", True) and not observed(s)]
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


def corner_radius(size: int) -> int:
    # Small enough that the two corner badges never touch, even at 16 px.
    return max(2, min(round(size * 0.2), (size - 8) // 4))


def dot_box(size: int) -> tuple[int, int, int, int]:
    """Bounding box of the status dot (white ring included), bottom-right."""
    r, ring = corner_radius(size), max(1, size // 24)
    c = size - r - 2
    return (c - r - ring, c - r - ring, c + r + ring, c + r + ring)


def arrow_box(size: int) -> tuple[int, int, int, int]:
    """Bounding box of the update badge (white ring included), top-left."""
    r, ring = corner_radius(size), max(1, size // 24)
    c = r + 2
    return (c - r - ring, c - r - ring, c + r + ring, c + r + ring)


def compose(base: Image.Image, connected: bool, update_available: bool, tone: str | None = None) -> Image.Image:
    """The tray icon: ``base`` plus the link dot (bottom-right: green when
    connected, grey when not; a broken or starting service turns it red or amber)
    and, with an update, an orange up-arrow badge (top-left). The two corners
    never overlap; without an update the top-left corner is the base image."""
    image = base.copy()
    size = image.width
    r, ring = corner_radius(size), max(1, size // 24)
    draw = ImageDraw.Draw(image)
    colour = TONES[tone] if tone in ("bad", "warn") else (TONES["ok"] if connected else OFFLINE_GREY)
    c = size - r - 2
    draw.ellipse(dot_box(size), fill=(255, 255, 255, 255))
    draw.ellipse((c - r, c - r, c + r, c + r), fill=(*colour, 255))
    if update_available:
        a = r + 2
        draw.ellipse(arrow_box(size), fill=(255, 255, 255, 255))
        draw.ellipse((a - r, a - r, a + r, a + r), fill=(*UPDATE_ORANGE, 255))
        head = max(2, round(r * 0.7))
        shaft = max(1, round(r * 0.22))
        top, bottom = a - round(r * 0.65), a + round(r * 0.6)
        draw.polygon([(a, top), (a - head, top + head), (a + head, top + head)], fill=(255, 255, 255, 255))
        draw.rectangle((a - shaft, top + head - 1, a + shaft, bottom), fill=(255, 255, 255, 255))
    return image


_compose_cache: dict[tuple, Image.Image] = {}


def tray_icon(base: Image.Image, connected: bool, update_available: bool, tone: str | None = None) -> Image.Image:
    """``compose`` cached per base image and (connected, update_available, tone)."""
    key = (id(base), bool(connected), bool(update_available), tone if tone in ("bad", "warn") else None)
    if key not in _compose_cache:
        _compose_cache[key] = compose(base, *key[1:])
    return _compose_cache[key]


def update_label(update: dict[str, Any]) -> str:
    """"更新到 vX.Y.Z" when the shell knows of a newer version, else "检查更新"."""
    version = update.get("version") if update.get("available") else None
    return f"更新到 v{str(version).lstrip('vV')}" if version else "检查更新"


def inbox_label(count: int | None) -> str:
    return f"收件箱（{count}）" if count else "收件箱"


class Tray:
    def __init__(self, sup: Supervisor, url: str, request_exit: Callable[[], None]):
        self.sup, self.url, self.request_exit = sup, url, request_exit
        self.base = Image.open(ICON_PATH).convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
        self.services = sup.snapshots()
        self.root = Path(sup.cfg.root)
        self.update = read_update(self.root)
        self.inbox_count: int | None = None
        self._inbox_at = 0.0
        self._check_requested: float | None = None
        self._key: list[tuple] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.icon = pystray.Icon("agent-nexus", self.current_icon(),
                                 summary(self.services), menu=pystray.Menu(self._items))

    def connected(self) -> bool:
        """The gateway link (web.build_app shares its cloud status on the supervisor)."""
        return (getattr(self.sup, "cloud_status", None) or {}).get("state") == "connected"

    def current_icon(self) -> Image.Image:
        return tray_icon(self.base, self.connected(), bool(self.update.get("available")), overall_tone(self.services))

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

    def hide(self) -> None:
        """Take the icon out of the notification area now (before the process ends without a stop)."""
        try:
            self.icon.visible = False
        except Exception:
            pass

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
            update = read_update(self.root)
            if self._check_requested is not None and (update.get("checked_at") or 0) >= self._check_requested - 1:
                self._check_requested = None
                if update.get("available"):
                    self._notify(f"发现新版本 v{update['version']}，在菜单中点击“{update_label(update)}”安装")
                elif update.get("error"):
                    self._notify(f"检查更新失败：{str(update['error'])[:120]}")
                else:
                    self._notify("已是最新版本")
            self.update = update
            if self.connected() and time.monotonic() - self._inbox_at >= INBOX_REFRESH_S:
                self._inbox_at = time.monotonic()
                self.inbox_count = self._fetch_inbox()
            key = [(s["name"], s["status"], s["pid"], s["message"]) for s in services]
            key.append(("~desktop", self.connected(), update.get("version"), self.inbox_count, autostart_enabled()))
            if force or key != self._key:
                self._key = key
                self.icon.icon = self.current_icon()
                self.icon.title = summary(services)
                self.icon.update_menu()

    def _fetch_inbox(self) -> int | None:
        """``me``'s unread mail plus open asks, from this manager's own API."""
        request = urllib.request.Request(self.url + "/api/inbox/summary", headers={"X-ANX": "1"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
            return int(data.get("unread") or 0) + int(data.get("open_asks") or 0)
        except Exception:  # no gateway inbox: the plain label
            return None

    # -- menu ---------------------------------------------------------------------------

    def _items(self) -> list[pystray.MenuItem]:
        item, sep = pystray.MenuItem, pystray.Menu.SEPARATOR
        items = [item("打开控制台", self._open_console, default=True),
                 item(inbox_label(self.inbox_count), self._open_inbox), sep]
        if not self.services:
            items.append(item("没有发现服务", None, enabled=False))
        for svc in self.services:
            label = f"{svc['name']}  ·  {status_text(svc)}"
            items.append(item(label, pystray.Menu(*self._service_items(svc))))
        autostart = autostart_enabled()
        items += [sep, item(update_label(self.update), self._update_action)]
        if autostart is not None:
            items.append(item("开机自启", self._toggle_autostart, checked=lambda _item: bool(autostart_enabled())))
        items += [item("重新扫描服务", self._rescan), item("打开 MCP 目录", self._open_folder),
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

    def _open_inbox(self) -> None:
        open_console(f"{self.url}/#/inbox")

    def _update_action(self) -> None:
        """Install the known update, or ask the shell to check (decision D7)."""
        if self.update.get("available"):
            if run_verb("--apply-update"):
                self._notify(f"正在更新到 v{self.update['version']}：管理器和服务会先停止，完成后自动重启")
            else:
                self._notify("没有找到 Agent Nexus 桌面程序，无法安装更新")
            return
        self._check_requested = time.time()
        if not run_verb("--check-update"):
            self._check_requested = None
            self._notify("没有找到 Agent Nexus 桌面程序，无法检查更新")

    def _toggle_autostart(self) -> None:
        wanted = not autostart_enabled()
        if not run_verb(f"--set-autostart={'on' if wanted else 'off'}"):
            self._notify("没有找到 Agent Nexus 桌面程序，无法修改开机自启")
            return

        def settle() -> None:  # the shell writes the Run value; follow it
            for _ in range(20):
                if autostart_enabled() == wanted:
                    break
                time.sleep(0.25)
            try:
                self.refresh(force=True)
            except Exception:
                pass
        threading.Thread(target=settle, daemon=True).start()

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
