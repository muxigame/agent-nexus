"""A private ``anx`` for the CLIs our chat starts: ``<run tmpdir>/.bin/anx``, first on the run's PATH.

The owner's own terminals get ``anx`` from the installers (Windows ``<install dir>\\bin``, the deb's ``/usr/bin/anx``,
macOS ``<root>/bin/anx`` linked by the console, agent_nexus/owner_cli.py), but a run cannot rely on those, and the
venv's own bin folder must not be put on PATH either: its ``python`` and dependency
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


#: The extensionless ``anx`` beside a copied ``anx.exe`` (Git Bash runs it; PowerShell and cmd.exe ignore it). The
#: desktop shell writes the same text into ``<install dir>\\bin`` (desktop/src-tauri/src/cli_path.rs, GIT_BASH_SHIM).
GIT_BASH_SHIM = "#!/bin/sh\n" + NO_PATHCONV + 'exec "${0%/*}/anx.exe" "$@"\n'


def owner_script(root: str | os.PathLike) -> str:
    """``<root>/bin/anx`` for the owner's own terminals (macOS, see agent_nexus/owner_cli.py): reads
    ``runtime/current`` on every run, so it never names a runtime an update pruned."""
    quoted = _sh_quote(str(root))
    return ("#!/bin/sh\n"
            "# Agent Nexus: anx in your terminal (written by the app; the console's 移除 takes the link away;\n"
            "# deleting the app does not: then remove the link to this file by hand)\n"
            f"root={quoted}\n"
            'tag=$(cat "$root/runtime/current" 2>/dev/null)\n'
            'case "$tag" in ""|*/*|*..*) tag="" ;; esac\n'
            'py="$root/runtime/$tag/.venv/bin/python"\n'
            'if [ -z "$tag" ] || [ ! -x "$py" ]; then\n'
            '  echo "anx: 找不到 Agent Nexus 运行环境，请先打开 Agent Nexus（已删除 Agent Nexus 的话，rm \\"$0\\" 移除这个命令）" >&2\n'
            '  exit 127\n'
            'fi\n'
            'exec "$py" -P -m agent_nexus.cli --root "$root" "$@"\n')


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
                script.write_text(GIT_BASH_SHIM, encoding="utf-8")
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
