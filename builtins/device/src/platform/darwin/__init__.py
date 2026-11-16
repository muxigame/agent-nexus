"""macOS: the common capabilities; no screen control yet. Notifications via ``osascript``."""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from ..common import CAPABILITIES as COMMON, SCREEN_CAPABILITIES, unavailable

REASON = "screen control is implemented on Windows only (platform=darwin)"
CAPABILITIES: dict[str, dict[str, Any]] = {**COMMON, **unavailable(SCREEN_CAPABILITIES, REASON)}
NOTIFY_BACKEND = "osascript"


def probe() -> dict[str, dict[str, Any]]:
    return {}


def form() -> str:
    return "desktop"


def notify(title: str, body: str, level: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
    binary = shutil.which("osascript")
    if binary is None:
        raise RuntimeError("osascript is not available")
    # json.dumps yields a double-quoted string literal AppleScript accepts.
    script = f"display notification {json.dumps(body)} with title {json.dumps(title)}"
    completed = subprocess.run([binary, "-e", script], capture_output=True, text=True, timeout=15)
    if completed.returncode != 0:
        raise RuntimeError("osascript failed: " + (completed.stderr or "").strip()[:300])
    return {"backend": NOTIFY_BACKEND}
