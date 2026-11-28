"""A private ``anx`` for the CLIs our chat starts: ``<run tmpdir>/.bin/anx``, first on the run's PATH.

Installed clients never put ``anx`` on PATH (Windows and macOS run the runtime venv's python directly, the deb ships
only ``agent-nexus``), and the venv's own bin folder must not be put there either: its ``python`` and dependency
scripts would shadow the user's tools. So each run gets a folder holding only ``anx``, pointing at this very client
(its python, its root):

- POSIX: a ``#!/bin/sh`` script that execs this process's python with ``-P -m agent_nexus.cli --root <root>``.
- Windows: a copy of the venv's ``anx.exe`` (uv's launcher, which names its python by absolute path), so PowerShell
  and cmd.exe find it and no ``.cmd`` re-parses ``& | ^ %`` in the arguments; ``anx.cmd`` instead only when there
  is no usable launcher (a dev checkout without one). Beside it an extensionless sh script ``anx`` for Git Bash
  (Claude Code's shell on Windows), as npm does for its own commands: bash runs it in preference to ``anx.exe``,
  PowerShell and cmd.exe ignore it (not in PATHEXT). It turns off MSYS path conversion before it starts the native
  program, which would otherwise rewrite ``--cwd /srv/app -- ls /var/log`` into ``C:/Program Files/Git/srv/app``...
  and run the wrong command on the other device.

``--root`` is passed because the runner strips ANX_ROOT; the device commands find the client through
``ANX_CONSOLE_URL`` and never need to read the root.
"""
from __future__ import annotations

import os
import shutil
import sys
import sysconfig
from pathlib import Path

WINDOWS = os.name == "nt"
FOLDER = ".bin"
#: Git for Windows (MSYS2) leaves the arguments of a native program alone with these set
NO_PATHCONV = "export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'\n"


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def launcher_exe(python: str | None = None) -> Path | None:
    """The venv's ``anx.exe`` when it is a launcher for ``python`` (its absolute path embedded at the end)."""
    python = python or sys.executable
    for folder in {sysconfig.get_path("scripts"), os.path.dirname(python)}:
        if not folder:
            continue
        candidate = Path(folder) / "anx.exe"
        try:
            if not candidate.is_file():
                continue
            with open(candidate, "rb") as handle:
                handle.seek(max(0, candidate.stat().st_size - 8192))
                tail = handle.read().lower()
        except OSError:
            continue
        needles = {os.path.normcase(os.path.abspath(python)).encode("utf-8"),
                   os.path.abspath(python).lower().encode("utf-8")}
        if any(needle in tail for needle in needles):
            return candidate
    return None


def write(tmpdir: str | os.PathLike, root: str | os.PathLike, *, python: str | None = None,
          windows: bool = WINDOWS) -> str | None:
    """Write the shim into ``<tmpdir>/.bin``; returns that folder, or None (Android, or it could not be written)."""
    if sys.platform == "android":
        return None  # nothing in app data may be executed there, and no CLI agent runs on Android
    python = python or sys.executable
    if not python:
        return None
    folder = Path(tmpdir) / FOLDER
    try:
        folder.mkdir(parents=True, exist_ok=True)
        script = folder / "anx"
        if windows:
            exe = launcher_exe(python)
            if exe is not None:
                shutil.copyfile(exe, folder / "anx.exe")
                script.write_text("#!/bin/sh\n" + NO_PATHCONV + 'exec "${0%/*}/anx.exe" "$@"\n', encoding="utf-8")
                return str(folder)
            (folder / "anx.cmd").write_text(
                f'@"{python}" -P -m agent_nexus.cli --root "{root}" %*\r\n', encoding="utf-8")
        script.write_text("#!/bin/sh\n" + (NO_PATHCONV if windows else "") +
                          f"exec {_sh_quote(str(python))} -P -m agent_nexus.cli --root {_sh_quote(str(root))} \"$@\"\n",
                          encoding="utf-8")
        if not windows:
            os.chmod(folder, 0o755)
            os.chmod(script, 0o755)
        return str(folder)
    except OSError:
        return None
