"""In-app updates for the system packages: the Ubuntu .deb, and the Windows installer when the
core is the system service ("为所有用户安装"; a per-user Windows app updates itself through Tauri).

Windows: the installer (``agent-nexus_<v>_x64-setup.exe``, SHA-256 from the gateway) runs
silently for all users from a one-time scheduled task as SYSTEM -- outside the service's process
tree, since the installer stops the service -- and its post-install step restarts the service.

* check: the joined gateway's public ``/api/downloads`` names the latest ``.deb`` with its
  SHA-256; the result goes to the same ``.anx/desktop-update.json`` the Windows shell writes,
  so the settings page reads both the same way.
* apply: download the ``.deb`` to ``~/.cache/agent-nexus``, check its SHA-256, install it with
  ``pkexec apt-get install`` (the system's own password dialog), then restart: the systemd
  user service when that is how it runs, else the desktop app. Without a desktop session
  (no pkexec agent) the result carries the command to run instead.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .desktop import UPDATE_FILE

PACKAGE = "agent-nexus"
LAUNCHER = Path("/usr/lib/agent-nexus/ubuntu_launcher.py")


class UpdateError(RuntimeError):
    pass


WINDOWS = sys.platform == "win32"
TASK = "AgentNexusUpdate"


def windows_system() -> bool:
    """The Windows core is the system service (agent-nexus.exe --service sets ANX_SYSTEM_SERVICE)."""
    if not WINDOWS or os.environ.get("ANX_SYSTEM_SERVICE") != "1":
        return False
    from . import winsession
    return winsession.is_system()


def supported() -> bool:
    """Running from the installed Ubuntu package, or as the Windows system service."""
    if WINDOWS:
        return windows_system()
    return sys.platform.startswith("linux") and LAUNCHER.is_file() and shutil.which("dpkg-query") is not None


def _gateway(root: Path) -> str | None:
    from .join import cloud_config, hub_url
    return hub_url(cloud_config(root).get("url"))


def _write(root: Path, data: dict) -> None:
    path = Path(root) / UPDATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _version_key(text: str) -> tuple:
    parts = []
    for piece in text.replace("-", ".").split("."):
        parts.append((0, int(piece)) if piece.isdigit() else (1, piece))
    return tuple(parts)


def check(root: Path) -> dict:
    import httpx

    from . import __version__
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    gateway = _gateway(root)
    try:
        if not gateway:
            raise UpdateError("这台设备还没有加入网关，无法检查更新")
        response = httpx.get(f"{gateway}/api/downloads", timeout=20, trust_env=False)
        response.raise_for_status()
        items = response.json().get("items") or []
        if WINDOWS:
            deb = next((i for i in items if i.get("id") == "windows-x64"
                        or str(i.get("filename", "")).endswith("_x64-setup.exe")), None)
        else:
            deb = next((i for i in items if i.get("id") == "linux-deb"
                        or str(i.get("filename", "")).endswith("_amd64.deb")), None)
        if deb is None or not deb.get("version"):
            raise UpdateError("网关没有提供 Windows 安装包" if WINDOWS else "网关没有提供 Ubuntu 安装包")
        version = str(deb["version"])
        available = _version_key(version) > _version_key(__version__)
        data = {"available": available, "version": version, "checked_at": stamp,
                "deb": {"url": deb["url"], "sha256": deb.get("sha256"), "filename": deb.get("filename"), "size": deb.get("size")}}
    except (UpdateError, httpx.HTTPError, ValueError) as exc:
        data = {"available": False, "version": None, "checked_at": stamp, "error": str(exc)[:300]}
    _write(root, data)
    return data


def _download(deb: dict) -> Path:
    import httpx

    if WINDOWS:
        cache = Path(os.environ.get("ANX_ROOT") or os.environ.get("ProgramData", "C:/ProgramData")) / ".anx" / "updates"
    else:
        cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "agent-nexus"
    cache.mkdir(parents=True, exist_ok=True)
    name = Path(str(deb.get("filename") or "agent-nexus_amd64.deb")).name
    target = cache / name
    for old in cache.glob("agent-nexus_*"):  # earlier installers (~130 MB each) are not needed again
        if old.name != name:
            old.unlink(missing_ok=True)
    digest = hashlib.sha256()
    with httpx.stream("GET", deb["url"], timeout=120, follow_redirects=True, trust_env=False) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            for chunk in response.iter_bytes(1 << 20):
                handle.write(chunk)
                digest.update(chunk)
    if deb.get("sha256") and digest.hexdigest() != deb["sha256"]:
        target.unlink(missing_ok=True)
        raise UpdateError("下载的安装包校验失败（SHA-256 不一致），请稍后重试")
    return target


SYSTEM_UNIT_FILE = Path("/usr/lib/systemd/system/agent-nexus.service")


def _restart() -> str:
    """Restart into the new version: the user service if active, else the desktop app.

    The system service (the core as root) restarts nothing itself: the package's postinst
    schedules ``--setup-system``, which waits for apt to finish and restarts the service."""
    if hasattr(os, "geteuid") and os.geteuid() == 0 and SYSTEM_UNIT_FILE.is_file() \
            and os.environ.get("ANX_ROOT", "").rstrip("/") == "/var/lib/agent-nexus":
        return "system"
    active = subprocess.run(["systemctl", "--user", "is-active", "agent-nexus.service"],
                            capture_output=True, text=True).stdout.strip() == "active"
    if active:
        command = "sleep 2; systemctl --user restart agent-nexus.service"
    else:
        command = ("sleep 2; pkill -f /usr/lib/agent-nexus/ubuntu_launcher.py; sleep 3; "
                   "nohup agent-nexus >/dev/null 2>&1 &")
    subprocess.Popen(["setsid", "sh", "-c", command], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    return "service" if active else "desktop"


def _privileged(unattended: bool) -> list[str] | None:
    """How to run apt-get as root here: directly, with passwordless sudo, or (interactive only) pkexec."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    if shutil.which("sudo") and subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0:
        return ["sudo", "-n"]
    if not unattended and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and shutil.which("pkexec"):
        return ["pkexec"]
    return None


def apply(root: Path, *, unattended: bool = False) -> dict:
    path = Path(root) / UPDATE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    deb = data.get("deb")
    if not data.get("available") or not isinstance(deb, dict) or not deb.get("url"):
        raise UpdateError("没有可用的更新；先检查更新")
    package = _download(deb)
    if WINDOWS:
        return _apply_windows(Path(root), data, package)
    command = ["apt-get", "install", "-y", "--no-install-recommends", str(package)]
    manual = f"sudo apt install -y ./{package.name}  （在 {package.parent} 目录下执行）"
    prefix = _privileged(unattended)
    if prefix is None:
        return {"installed": False, "manual": manual, "path": str(package)}
    result = subprocess.run([*prefix, *command], capture_output=True, text=True)
    if result.returncode in (126, 127):  # the password dialog was dismissed / not authorized
        return {"installed": False, "cancelled": True, "manual": manual, "path": str(package)}
    if result.returncode != 0:
        raise UpdateError(f"安装失败：{(result.stderr or result.stdout).strip()[-300:]}")
    _write(Path(root), {"available": False, "version": data.get("version"), "checked_at": data.get("checked_at")})
    return {"installed": True, "version": data.get("version"), "restart": _restart()}


def _apply_windows(root: Path, data: dict, package: Path) -> dict:
    """Run the installer for all users from a one-time SYSTEM task (it stops and restarts this service)."""
    hidden = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    command = f'"{package}" /S /AllUsers /UPDATE'
    when = time.strftime("%H:%M", time.localtime(time.time() + 120))
    created = subprocess.run(["schtasks", "/Create", "/TN", TASK, "/TR", command, "/SC", "ONCE", "/ST", when,
                              "/RU", "SYSTEM", "/RL", "HIGHEST", "/F"], capture_output=True, text=True,
                             creationflags=hidden)
    if created.returncode != 0:
        raise UpdateError(f"无法安排安装任务：{(created.stderr or created.stdout).strip()[-200:]}")
    started = subprocess.run(["schtasks", "/Run", "/TN", TASK], capture_output=True, text=True, creationflags=hidden)
    if started.returncode != 0:
        raise UpdateError(f"无法启动安装任务：{(started.stderr or started.stdout).strip()[-200:]}")
    _write(root, {"available": False, "version": data.get("version"), "checked_at": data.get("checked_at")})
    return {"installed": True, "version": data.get("version"), "restart": "system"}
