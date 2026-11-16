"""Linux (and other POSIX): the common capabilities; no screen control yet.

``screen``, ``uia`` and ``input`` are declared ``available: false`` with the
reason, so ``device_list`` shows the gap and the gateway's error names it.
Notifications go through ``notify-send`` (libnotify).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..common import CAPABILITIES as COMMON, SCREEN_CAPABILITIES, unavailable

REASON = "screen control is implemented on Windows only (platform=linux)"
CAPABILITIES: dict[str, dict[str, Any]] = {**COMMON, **unavailable(SCREEN_CAPABILITIES, REASON)}
NOTIFY_BACKEND = "notify-send"
_URGENCY = {"info": "normal", "warn": "normal", "urgent": "critical"}


def probe() -> dict[str, dict[str, Any]]:
    return {}


def form() -> str:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
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
