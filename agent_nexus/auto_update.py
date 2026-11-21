"""Automatic updates of the client (``desktop.update.mode``: ``auto`` / ``notify`` / ``off``).

* ``auto``: check 2 minutes after start and every 6 hours, and install what is found -- the
  Windows app through its updater (``agent-nexus.exe --check-update`` / ``--apply-update``), the
  Ubuntu package with apt (as root, or with passwordless sudo; never a password prompt).
* ``notify`` (desktop default): the settings page shows the update, installing is a click.
* ``off``: no checks.

A device without a desktop (an Ubuntu server running the service) defaults to ``auto``:
nobody is there to click.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from . import desktop, linux_update, paths

log = logging.getLogger(__name__)
MODES = ("auto", "notify", "off")
FIRST_CHECK_S = 120
INTERVAL_S = 6 * 3600


def headless() -> bool:
    return sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _config(root: Path) -> dict:
    try:
        data = json.loads(paths.config_path(Path(root)).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def mode(root: Path) -> str:
    value = (((_config(root).get("desktop") or {}).get("update") or {}).get("mode"))
    if value in MODES:
        return value
    return "auto" if headless() else "notify"


def set_mode(root: Path, value: str) -> str:
    if value not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    data = _config(root)
    section = data.setdefault("desktop", {}) if isinstance(data.get("desktop", {}), dict) else {}
    data["desktop"] = section
    update = section.setdefault("update", {}) if isinstance(section.get("update", {}), dict) else {}
    section["update"] = update
    update["mode"] = value
    target = paths.write_path(Path(root))
    temporary = target.with_suffix(".json.update.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return value


async def run_once(root: Path) -> dict:
    """One automatic check-and-install; what happened, for the log and the tests."""
    if desktop.desktop_app() is not None:
        before = desktop.read_update(root).get("checked_at")
        if not await asyncio.to_thread(desktop.run_verb, "--check-update"):
            return {"result": "no-app"}
        for _ in range(60):
            await asyncio.sleep(2)
            state = desktop.read_update(root)
            if state.get("checked_at") != before:
                break
        state = desktop.read_update(root)
        if not state.get("available"):
            return {"result": "current"}
        await asyncio.to_thread(desktop.run_verb, "--apply-update")
        return {"result": "installing", "version": state.get("version")}
    if linux_update.supported():
        found = await asyncio.to_thread(linux_update.check, root)
        if not found.get("available"):
            return {"result": "current" if not found.get("error") else "error", "error": found.get("error")}
        result = await asyncio.to_thread(linux_update.apply, root, unattended=True)
        return {"result": "installed" if result.get("installed") else "needs-root", **result}
    return {"result": "unsupported"}


async def loop(root: Path) -> None:
    await asyncio.sleep(FIRST_CHECK_S)
    while True:
        if mode(root) == "auto":
            try:
                outcome = await run_once(root)
                if outcome["result"] in ("installing", "installed"):
                    log.warning("auto update: installing %s", outcome.get("version"))
                elif outcome["result"] in ("error", "needs-root"):
                    log.warning("auto update: %s", outcome.get("error") or outcome.get("manual"))
            except Exception as exc:  # never let an update problem stop the manager
                log.warning("auto update failed: %s", exc)
        await asyncio.sleep(INTERVAL_S)


def status(root: Path) -> dict:
    return {"mode": mode(root), "headless": headless(), "checked": time.time()}
