"""Linux (and other POSIX): the common capabilities; no screen control yet.

``screen``, ``uia`` and ``input`` are declared ``available: false`` with the
reason, so ``device_list`` shows the gap and the gateway's error names it.
Notifications go through ``notify-send`` (libnotify).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..common import CAPABILITIES as COMMON, SCREEN_CAPABILITIES, unavailable

REASON = "screen control is implemented on Windows only (platform=linux)"
CAPABILITIES: dict[str, dict[str, Any]] = {**COMMON, **unavailable(SCREEN_CAPABILITIES, REASON)}
NOTIFY_BACKEND = "notify-send"
_URGENCY = {"info": "normal", "warn": "normal", "urgent": "critical"}


def probe() -> dict[str, dict[str, Any]]:
    return {}


def _display_manager() -> bool:
    """A desktop machine even when this process has no display (the core is a system service)."""
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", "display-manager"], timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def form() -> str:
    if sys.platform == "android":  # the Android app (phone, tablet, headset)
        return "mobile"
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and not _display_manager():
        return "server"
    if any(Path("/sys/class/power_supply").glob("BAT*")):
        return "laptop"
    return "desktop"


def notify(title: str, body: str, level: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
    binary = shutil.which("notify-send")
    if binary is None:
        raise RuntimeError("notify-send is not installed (package libnotify-bin)")
    command = [binary, "--app-name=agent-nexus", f"--urgency={_URGENCY[level]}", title, body]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=15)
    if completed.returncode != 0:
        raise RuntimeError("notify-send failed: " + (completed.stderr or "").strip()[:300])
    return {"backend": NOTIFY_BACKEND}
