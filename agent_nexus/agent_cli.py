"""Where Claude Code and Codex are on this machine, looked for as the person at the screen would
run them: the device list's ``agent_runtimes`` and the 终端 page's tool chips.

Looking only in this process's PATH missed them:

* On Windows the client runs as the system service (SYSTEM). Its PATH has none of the user's
  folders, so an npm install (``%APPDATA%\\npm\\codex.cmd``) was invisible. The user's own
  environment comes from their logon (:func:`winsession.environment`).
* The ChatGPT / Codex desktop app ships its own ``codex`` inside the app -- on Windows in its
  MSIX package under ``C:\\Program Files\\WindowsApps\\OpenAI.Codex_*\\app\\resources`` -- which
  is on no PATH at all, not even the user's. It is a complete Codex CLI (``codex --version``).
* macOS apps started from the Dock do not get the shell's PATH (Homebrew, nvm, ~/.local/bin).
* OpenAI's standalone installer (the 终端 page's 安装 Codex) puts it in the user's own folder:
  ``%LOCALAPPDATA%\\Programs\\OpenAI\\Codex\\bin`` on Windows, ``~/.local/bin`` elsewhere.

Results are cached for ten minutes: the manager asks on every catalog refresh.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import sys
import time

NAMES = ("claude", "codex")
CACHE_S = 600.0
WINDOWS = sys.platform == "win32"
_cache: tuple[float, dict[str, dict]] | None = None


def user_env() -> dict[str, str]:
    """This process's environment, or (as the Windows system service) the screen user's."""
    if WINDOWS:
        try:
            from . import winsession
            if winsession.is_system():
                session = winsession.active_session()
                if session:
                    return winsession.environment(int(session["uid"]))
        except Exception:
            pass
    return dict(os.environ)


def _path_of(env: dict[str, str]) -> str:
    return next((value for key, value in env.items() if key.upper() == "PATH"), "")


def _version_key(path: str) -> tuple:
    """``OpenAI.Codex_26.1117.3838.0_x64__…`` -> (26, 1117, 3838, 0): the newest package first."""
    match = re.search(r"_(\d+(?:\.\d+)+)_", os.path.basename(path.rstrip("\\/")))
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def candidates(name: str, env: dict[str, str]) -> list[tuple[str, str]]:
    """(path, how it got there) to try after the PATH, most likely first."""
    found: list[tuple[str, str]] = []
    if WINDOWS:
        home = env.get("USERPROFILE") or ""
        appdata = env.get("APPDATA") or (os.path.join(home, "AppData", "Roaming") if home else "")
        local = env.get("LOCALAPPDATA") or (os.path.join(home, "AppData", "Local") if home else "")
        if appdata:
            found.append((os.path.join(appdata, "npm", f"{name}.cmd"), "npm"))
        if home:
            found += [(os.path.join(home, ".local", "bin", f"{name}.exe"), "native"),
                      (os.path.join(home, "scoop", "shims", f"{name}.exe"), "scoop"),
                      (os.path.join(home, ".bun", "bin", f"{name}.exe"), "bun")]
        if local:
            if name == "codex":
                found.append((os.path.join(local, "Programs", "OpenAI", "Codex", "bin", "codex.exe"), "native"))
            found += [(os.path.join(local, "Microsoft", "WinGet", "Links", f"{name}.exe"), "winget"),
                      (os.path.join(local, "pnpm", f"{name}.cmd"), "pnpm")]
        if name == "codex":
            apps = os.path.join(os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles") or r"C:\Program Files",
                                "WindowsApps")
            packages = glob.glob(os.path.join(apps, "OpenAI.Codex_*")) + glob.glob(os.path.join(apps, "OpenAI.ChatGPT*"))
            for package in sorted(packages, key=_version_key, reverse=True):
                found.append((os.path.join(package, "app", "resources", "codex.exe"), "app"))
        return found
    home = env.get("HOME") or os.path.expanduser("~")
    folders = ["/opt/homebrew/bin", "/usr/local/bin", f"{home}/.local/bin", f"{home}/.npm-global/bin",
               f"{home}/.bun/bin", f"{home}/.volta/bin", f"{home}/bin"]
    nvm = sorted(glob.glob(f"{home}/.nvm/versions/node/*/bin"),
                 key=lambda p: tuple(int(x) for x in re.findall(r"\d+", p.split("/node/")[-1])[:3]), reverse=True)
    found += [(f"{folder}/{name}", "npm" if ".nvm" in folder or "npm" in folder else "path") for folder in folders + nvm]
    if name == "claude":
        found.append((f"{home}/.claude/local/claude", "native"))
    if name == "codex" and sys.platform == "darwin":
        for app in ("/Applications/Codex.app", f"{home}/Applications/Codex.app", "/Applications/ChatGPT.app"):
            found += [(f"{app}/Contents/Resources/codex", "app"), (f"{app}/Contents/Resources/bin/codex", "app")]
    return found


def find(name: str, env: dict[str, str] | None = None) -> dict:
    """{available, path_known, path, source, on_path}: ``on_path`` when the user can type the name."""
    env = env if env is not None else user_env()
    path = shutil.which(name, path=_path_of(env) or None)
    if path:
        return {"available": True, "path_known": True, "path": path, "source": "path", "on_path": True}
    for candidate, source in candidates(name, env):
        try:
            usable = os.path.isfile(candidate) and (WINDOWS or os.access(candidate, os.X_OK))
        except OSError:
            usable = False
        if usable:
            return {"available": True, "path_known": True, "path": candidate, "source": source, "on_path": False}
    return {"available": False, "path_known": False}


def codex_signed_in(env: dict[str, str] | None = None) -> bool | None:
    """Whether the screen user has signed Codex in: ``$CODEX_HOME/auth.json`` (``~/.codex``).
    None when it cannot tell (credentials kept in the OS keyring instead of the file)."""
    env = env if env is not None else user_env()
    home = env.get("USERPROFILE" if WINDOWS else "HOME") or os.path.expanduser("~")
    codex_home = env.get("CODEX_HOME") or os.path.join(home, ".codex")
    try:
        if os.path.isfile(os.path.join(codex_home, "auth.json")):
            return True
        with open(os.path.join(codex_home, "config.toml"), encoding="utf-8", errors="replace") as handle:
            if re.search(r'^\s*cli_auth_credentials_store\s*=\s*"(keyring|auto)"', handle.read(), re.M):
                return None
    except OSError:
        pass
    return False


def runtimes(*, fresh: bool = False) -> dict[str, dict]:
    """``find`` for Claude Code and Codex, cached for ten minutes."""
    global _cache
    if not fresh and _cache is not None and time.monotonic() - _cache[0] < CACHE_S:
        return _cache[1]
    env = user_env()
    result = {name: find(name, env) for name in NAMES}
    if result["codex"].get("available"):
        result["codex"]["signed_in"] = codex_signed_in(env)
    _cache = (time.monotonic(), result)
    return result
