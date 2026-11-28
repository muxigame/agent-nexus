"""Which shells exec_command can run on this device, and where they live.

Detection runs inside the device plugin, under the account that runs commands,
so per-user installs and PATH are what that account actually sees. Results are
cached for a few minutes: an install shows up without a restart.

Names are the ones a model passes as ``shell``: bash, sh, zsh, pwsh,
powershell, cmd. On Windows ``bash`` is never the bare ``System32\\bash.exe``
(the WSL launcher, which CreateProcess would find before Git Bash); it is Git
Bash, MSYS2 or Cygwin when installed, else a WSL distro (not under SYSTEM).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable

SHELL_NAMES = ("bash", "sh", "zsh", "pwsh", "powershell", "cmd")
#: A requested name that may be served by another shell when only that one exists.
ALIASES = {"powershell": "pwsh"}
CACHE_TTL_S = 300.0


@dataclass(frozen=True)
class ShellInfo:
    name: str
    path: str
    #: native, git-bash, msys2, cygwin or wsl:<distro>.
    kind: str = "native"

    @property
    def family(self) -> str:
        return "bash" if self.name in ("bash", "sh", "zsh") else self.name

    @property
    def wsl_distro(self) -> str | None:
        return self.kind[4:] if self.kind.startswith("wsl:") else None


def shell_name(value: str) -> str:
    """``C:\\...\\bash.exe`` / ``BASH`` / ``pwsh.exe`` -> ``bash`` / ``bash`` / ``pwsh``."""
    base = PureWindowsPath(value).name if "\\" in value else Path(value).name
    base = base.lower()
    return base[:-4] if base.endswith(".exe") else base


# -- probes (module level so tests can stub them) ------------------------------
def _which(name: str) -> str | None:
    return shutil.which(name)


def _is_file(path: str) -> bool:
    try:
        return Path(path).is_file()
    except OSError:
        return False


def _executable(path: str) -> bool:
    return _is_file(path) and os.access(path, os.X_OK)


def _is_system_account() -> bool:
    user = (os.environ.get("USERNAME") or "").upper()
    profile = (os.environ.get("USERPROFILE") or "").lower()
    return user == "SYSTEM" or user.endswith("$") or "systemprofile" in profile


def _wsl_distros() -> list[str]:
    try:
        out = subprocess.run(["wsl.exe", "-l", "-q"], capture_output=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    text = out.decode("utf-16-le", "ignore") if b"\x00" in out else out.decode("utf-8", "ignore")
    names: list[str] = []
    for line in text.replace("\x00", "").splitlines():
        name = line.strip()
        # docker-desktop / docker-desktop-data are Docker's minimal distros: no bash.
        if name and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name) and name not in names \
                and not name.lower().startswith("docker-desktop"):
            names.append(name)
    return names[:8]


def _wsl_has_bash(distro: str) -> bool:
    """Whether ``distro`` really has /bin/bash (probed once per cache period)."""
    try:
        done = subprocess.run(["wsl.exe", "-d", distro, "-e", "test", "-x", "/bin/bash"],
                              capture_output=True, timeout=10,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _system32_launcher(path: str) -> bool:
    lowered = path.replace("/", "\\").lower()
    return "\\windows\\system32\\" in lowered or "\\windowsapps\\bash.exe" in lowered


def is_wsl_launcher(path: str) -> bool:
    """System32\\bash.exe, wsl.exe and the like: they start a WSL distro, not a Windows bash."""
    return _system32_launcher(path) or shell_name(path) == "wsl"


# -- detection -----------------------------------------------------------------
def _windows(bash_path: str, env: dict[str, str]) -> dict[str, ShellInfo]:
    found: dict[str, ShellInfo] = {}
    root = env.get("SystemRoot") or env.get("SYSTEMROOT") or r"C:\Windows"
    powershell = _which("powershell") or str(PureWindowsPath(root, "System32", "WindowsPowerShell", "v1.0",
                                                               "powershell.exe"))
    if _is_file(powershell):
        found["powershell"] = ShellInfo("powershell", powershell)
    pwsh = _which("pwsh")
    if not pwsh:
        for base in filter(None, (env.get("ProgramFiles"), env.get("ProgramW6432"))):
            candidate = str(PureWindowsPath(base, "PowerShell", "7", "pwsh.exe"))
            if _is_file(candidate):
                pwsh = candidate
                break
    if pwsh and _is_file(pwsh):
        found["pwsh"] = ShellInfo("pwsh", pwsh)
    cmd = env.get("ComSpec") or env.get("COMSPEC") or str(PureWindowsPath(root, "System32", "cmd.exe"))
    if _is_file(cmd):
        found["cmd"] = ShellInfo("cmd", cmd)

    candidates: list[tuple[str, str]] = []
    if bash_path:
        candidates.append((bash_path, "git-bash"))
    git = _which("git")
    if git:
        git_root = PureWindowsPath(git).parent.parent  # ...\Git\cmd\git.exe -> ...\Git
        candidates += [(str(git_root / "bin" / "bash.exe"), "git-bash"),
                       (str(PureWindowsPath(git).parent / "bash.exe"), "git-bash")]
    for base in filter(None, (env.get("ProgramFiles"), env.get("ProgramW6432"), env.get("LOCALAPPDATA"))):
        candidates += [(str(PureWindowsPath(base, "Git", "bin", "bash.exe")), "git-bash"),
                       (str(PureWindowsPath(base, "Programs", "Git", "bin", "bash.exe")), "git-bash")]
    candidates += [(r"C:\msys64\usr\bin\bash.exe", "msys2"), (r"C:\cygwin64\bin\bash.exe", "cygwin")]
    for path, kind in candidates:
        if path and not _system32_launcher(path) and _is_file(path):
            lowered = path.lower()
            kind = "msys2" if "msys" in lowered else "cygwin" if "cygwin" in lowered else kind
            found["bash"] = ShellInfo("bash", path, kind)
            sh = str(PureWindowsPath(path).parent / "sh.exe")
            if _is_file(sh):
                found["sh"] = ShellInfo("sh", sh, kind)
            zsh = str(PureWindowsPath(path).parent / "zsh.exe")
            if _is_file(zsh):
                found["zsh"] = ShellInfo("zsh", zsh, kind)
            break
    if "bash" not in found and not _is_system_account():
        wsl = _which("wsl.exe") or _which("wsl")
        if wsl:
            # The default distro is listed first; only one that really has bash counts.
            for distro in _wsl_distros()[:3]:
                if _wsl_has_bash(distro):
                    found["bash"] = ShellInfo("bash", wsl, f"wsl:{distro}")
                    break
    return found


def _posix(bash_path: str) -> dict[str, ShellInfo]:
    found: dict[str, ShellInfo] = {}
    for name in ("bash", "zsh", "sh", "pwsh"):
        candidates = [bash_path] if name == "bash" and bash_path else []
        candidates += [_which(name) or "", f"/bin/{name}", f"/usr/bin/{name}", f"/usr/local/bin/{name}"]
        for path in candidates:
            if path and _executable(path):
                found[name] = ShellInfo(name, path)
                break
    return found


_cache: dict[tuple, tuple[float, dict[str, ShellInfo]]] = {}


def detect(bash_path: str = "", overrides: dict[str, str] | None = None, *,
           platform: str | None = None, env: dict[str, str] | None = None,
           now: Callable[[], float] = time.monotonic) -> dict[str, ShellInfo]:
    """name -> ShellInfo for every shell this device really has (cached)."""
    platform = platform or sys.platform
    overrides = dict(overrides or {})
    key = (platform, bash_path, tuple(sorted(overrides.items())))
    cached = _cache.get(key)
    if cached and now() - cached[0] < CACHE_TTL_S:
        return dict(cached[1])
    env = dict(os.environ if env is None else env)
    found = _windows(bash_path, env) if platform == "win32" else _posix(bash_path)
    for name, path in overrides.items():
        name = shell_name(name)
        if name in SHELL_NAMES and path and _is_file(path):
            kind = "native"
            if platform == "win32" and name in ("bash", "sh", "zsh"):
                kind = "wsl" if is_wsl_launcher(path) else "git-bash"
            found[name] = ShellInfo(name, path, kind)
    _cache[key] = (now(), dict(found))
    return found


def cached(bash_path: str = "", overrides: dict[str, str] | None = None, *,
           platform: str | None = None, now: Callable[[], float] = time.monotonic) -> dict[str, ShellInfo] | None:
    """The cached detection when still fresh, else None (never probes)."""
    key = (platform or sys.platform, bash_path, tuple(sorted(dict(overrides or {}).items())))
    hit = _cache.get(key)
    return dict(hit[1]) if hit and now() - hit[0] < CACHE_TTL_S else None


def clear_cache() -> None:
    _cache.clear()


def ordered(found: dict[str, ShellInfo], default: str | None, platform: str | None = None) -> list[str]:
    """Names, the default first, then a stable order per platform."""
    order = ["powershell", "pwsh", "cmd", "bash", "sh", "zsh"] if (platform or sys.platform) == "win32" else \
        ["bash", "zsh", "sh", "pwsh"]
    names = [name for name in order if name in found]
    names += [name for name in found if name not in names]
    if default in names:
        names.remove(default)
        names.insert(0, default)
    return names
