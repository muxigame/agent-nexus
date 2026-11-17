"""Opening the console: the desktop window when it is built, else a browser tab.

The window (``desktop/``) is a Tauri shell that loads the very page the manager
already serves on loopback, so there is one UI to maintain and the console's own
Host/Origin checks keep applying. It exposes no Tauri commands - the page gains
nothing it would not have in a browser tab.

A machine without the built window (another OS, a fresh checkout) keeps working:
this falls back to the browser rather than failing.
"""
from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path

#: Where `cargo build --release` puts it, relative to the manager package.
RELATIVE_EXE = Path("desktop/src-tauri/target/release/mcpm-desktop.exe")
ENV_OVERRIDE = "MCPM_DESKTOP_APP"


def desktop_app() -> Path | None:
    """The built window, or ``None`` when there is none for this machine."""
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        path = Path(override)
        return path if path.is_file() else None
    if sys.platform != "win32":  # only the Windows binary is built here
        return None
    root = os.environ.get("MCPM_ROOT")
    if root:
        for candidate in (Path(root) / "mcpm-desktop.exe", Path(root) / "mcp-manager/artifacts/mcpm-desktop.exe"):
            if candidate.is_file():
                return candidate
    path = (Path(__file__).resolve().parent.parent / RELATIVE_EXE).resolve()
    return path if path.is_file() else None


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
        print(f"mcpm: could not start {app.name} ({exc}); opening a browser instead", file=sys.stderr, flush=True)
        webbrowser.open(url)
        return "browser"
