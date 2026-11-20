"""Opening the console: the desktop window when it is built, else a browser tab.

The window (``desktop/``) is a Tauri shell that loads the very page the manager
already serves on loopback, so there is one UI to maintain and the console's own
Host/Origin checks keep applying. It exposes no Tauri commands - the page gains
nothing it would not have in a browser tab.

A machine without the built window (another OS, a fresh checkout) keeps working:
this falls back to the browser rather than failing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import env

#: The desktop shell's executable (Tauri ``mainBinaryName``), and the name the
#: legacy shell had. Both are looked for, so a portable legacy exe keeps working.
EXE_NAME = "agent-nexus.exe"
LEGACY_EXE_NAME = "mcpm-desktop.exe"
#: Where `cargo build --release` puts it, relative to the manager package.
RELATIVE_EXE = Path("desktop/src-tauri/target/release") / EXE_NAME
LEGACY_RELATIVE_EXE = Path("desktop/src-tauri/target/release") / LEGACY_EXE_NAME
#: ``ANX_DESKTOP_APP``, else the legacy ``MCPM_DESKTOP_APP``.
ENV_OVERRIDE = "ANX_DESKTOP_APP"
LEGACY_ENV_OVERRIDE = "MCPM_DESKTOP_APP"


def desktop_app() -> Path | None:
    """The built window, or ``None`` when there is none for this machine."""
    override = env.get("DESKTOP_APP", warn=False)
    if override:
        path = Path(override)
        return path if path.is_file() else None
    if sys.platform != "win32":  # only the Windows binary is built here
        return None
    root = env.get("ROOT", warn=False)
    if root:
        for name in (EXE_NAME, LEGACY_EXE_NAME):
            for candidate in (Path(root) / name, Path(root) / "agent-nexus/artifacts" / name,
                              Path(root) / "mcp-manager/artifacts" / name):
                if candidate.is_file():
                    return candidate
    base = Path(__file__).resolve().parent.parent
    for relative in (RELATIVE_EXE, LEGACY_RELATIVE_EXE):
        path = (base / relative).resolve()
        if path.is_file():
            return path
    return None


def open_console(url: str) -> str:
    """Show the console; returns what was used, for the caller's log."""
    app = desktop_app()
    if app is None:
        webbrowser.open(url)
        return "browser"
    try:
        # Detached: the window outlives the manager process that opened it. A
        # second launch is caught by the app's single-instance plugin, which
        # navigates the existing window to this URL - that is how the tray's
        # per-service link lands on the right tab instead of just focusing.
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS} if sys.platform == "win32" else {"start_new_session": True}
        subprocess.Popen([str(app), url], close_fds=True, **options)
        return "window"
    except OSError as exc:
        print(f"anx: could not start {app.name} ({exc}); opening a browser instead", file=sys.stderr, flush=True)
        webbrowser.open(url)
        return "browser"


# -- the desktop shell's verbs, updates and autostart (design sections 9.2, 9.4, 9.6) --
# The shell (Rust) owns these; the manager only reads what it records and asks it
# to act by starting ``agent-nexus.exe <verb>``, which the single-instance plugin
# forwards to the running shell.

#: Written by the shell after every update check.
UPDATE_FILE = Path(".anx") / "desktop-update.json"
UPDATE_MODES = ("notify", "off")
#: ``HKCU\...\Run`` value the shell writes for login autostart.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "Agent Nexus"

_update_cache: dict[Path, tuple[float, dict]] = {}


def update_mode(config: dict | None) -> tuple[str, str | None]:
    """``desktop.update.mode``: ``notify`` (default) or ``off``; the retired
    ``auto`` and unknown values mean ``notify`` with a warning (decision D7)."""
    value = (((config or {}).get("desktop") or {}).get("update") or {}).get("mode")
    if value in (None, "notify"):
        return "notify", None
    if value == "off":
        return "off", None
    if value == "auto":
        return "notify", 'desktop.update.mode "auto" is no longer supported; using "notify" (updates install only when you click)'
    return "notify", f"unknown desktop.update.mode {value!r}; using \"notify\""


def read_update(root: Path, current: str | None = None) -> dict:
    """What the shell last found: ``available`` is true only for a version other
    than the running one (a record left from before an update is stale)."""
    from . import __version__
    current = current or __version__
    path = Path(root) / UPDATE_FILE
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {"available": False, "version": None, "current": current, "checked_at": None}
    cached = _update_cache.get(path)
    if cached is None or cached[0] != mtime:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        cached = (mtime, data if isinstance(data, dict) else {})
        _update_cache[path] = cached
    data = cached[1]
    version = data.get("version") if isinstance(data.get("version"), str) else None
    available = bool(data.get("available")) and bool(version) and version != current
    return {"available": available, "version": version if available else None, "notes": data.get("notes") if available else None,
            "current": current, "checked_at": data.get("checked_at"), "error": data.get("error")}


def update_status(root: Path) -> dict:
    """``GET /api/app/update``: the recorded check, the mode and whether the shell exists."""
    from .paths import config_path
    try:
        config = json.loads(config_path(Path(root)).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        config = {}
    mode, warning = update_mode(config if isinstance(config, dict) else {})
    from . import linux_update
    updater = "tauri" if desktop_app() is not None else "deb" if linux_update.supported() else None
    return {**read_update(root), "mode": mode, "warning": warning, "desktop_app": desktop_app() is not None,
            "updater": updater}


def autostart_enabled() -> bool | None:
    """Whether the login Run value exists; ``None`` where there is no such thing."""
    if sys.platform != "win32":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
        return True
    except OSError:
        return False


def run_verb(*args: str) -> bool:
    """Start ``agent-nexus.exe <args>`` detached; False when there is no shell."""
    app = desktop_app()
    if app is None:
        return False
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS} if sys.platform == "win32" else {"start_new_session": True}
    try:
        subprocess.Popen([str(app), *args], close_fds=True, **options)
        return True
    except OSError as exc:
        print(f"anx: could not run {app.name} {' '.join(args)} ({exc})", file=sys.stderr, flush=True)
        return False
