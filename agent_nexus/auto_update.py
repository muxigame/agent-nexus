"""Automatic updates of the client (``desktop.update.mode``: ``auto`` / ``notify`` / ``off``).

* ``auto`` (the default everywhere): check 2 minutes after start and every 5 minutes, and
  install what is found -- the Windows app through its updater (``agent-nexus.exe
  --check-update`` / ``--apply-update``, a silent installer: no window), the Ubuntu package
  with apt (as root, or with passwordless sudo; never a password prompt), the Android app by
  downloading the APK and offering it to the system installer (Android always asks the user).
* ``notify``: the settings page shows the update, installing is a click.
* ``off``: no checks.

The default used to be ``notify`` on a desktop. Devices of one network must run versions that
understand each other (P2P, file transfer), and a desktop nobody clicks on fell behind for days.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable

from . import desktop, linux_update, paths

log = logging.getLogger(__name__)
MODES = ("auto", "notify", "off")
FIRST_CHECK_S = 120
INTERVAL_S = 5 * 60


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
    return "auto"


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
        if result.get("prompted"):  # Android: downloaded; the app asks the user to install it
            return {"result": "prompted", **result}
        if result.get("deferred"):  # Android on a metered network: download later, on Wi-Fi
            return {"result": "deferred", **result}
        return {"result": "installed" if result.get("installed") else "needs-root", **result}
    return {"result": "unsupported"}


#: a running copy job postpones installing an update (which restarts the manager) at most this long
BUSY_POSTPONE_MAX_S = 6 * 3600


async def loop(root: Path, busy: Callable[[], bool] | None = None) -> None:
    await asyncio.sleep(FIRST_CHECK_S)
    busy_since: float | None = None
    while True:
        postpone = False
        if busy is not None and mode(root) == "auto":
            try:
                running = bool(busy())
            except Exception:
                running = False
            if not running:
                busy_since = None
            else:
                busy_since = busy_since if busy_since is not None else time.monotonic()
                postpone = time.monotonic() - busy_since < BUSY_POSTPONE_MAX_S
                if postpone:
                    log.info("auto update: copy job running, update postponed")
        if mode(root) == "auto" and not postpone:
            try:
                outcome = await run_once(root)
                if outcome["result"] == "prompted":
                    log.info("auto update: %s downloaded, waiting for the user to install it", outcome.get("version"))
                elif outcome["result"] == "deferred":
                    log.info("auto update: %s available, waiting for an unmetered network", outcome.get("version"))
                elif outcome["result"] in ("installing", "installed"):
                    log.warning("auto update: installing %s", outcome.get("version"))
                elif outcome["result"] in ("error", "needs-root"):
                    log.warning("auto update: %s", outcome.get("error") or outcome.get("manual"))
            except Exception as exc:  # never let an update problem stop the manager
                log.warning("auto update failed: %s", exc)
        await asyncio.sleep(INTERVAL_S)


def status(root: Path) -> dict:
    return {"mode": mode(root), "headless": headless(), "checked": time.time()}
