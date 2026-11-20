"""In-app updates for the Ubuntu package (the Windows app updates itself through Tauri).

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


def supported() -> bool:
    """Running from the installed Ubuntu package."""
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
        deb = next((i for i in items if i.get("id") == "linux-deb" or str(i.get("filename", "")).endswith("_amd64.deb")), None)
        if deb is None or not deb.get("version"):
            raise UpdateError("网关没有提供 Ubuntu 安装包")
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

    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "agent-nexus"
    cache.mkdir(parents=True, exist_ok=True)
    name = Path(str(deb.get("filename") or "agent-nexus_amd64.deb")).name
    target = cache / name
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


def _restart() -> str:
    """Restart into the new version: the user service if active, else the desktop app."""
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


def apply(root: Path) -> dict:
    path = Path(root) / UPDATE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    deb = data.get("deb")
    if not data.get("available") or not isinstance(deb, dict) or not deb.get("url"):
        raise UpdateError("没有可用的更新；先检查更新")
    package = _download(deb)
    command = ["apt-get", "install", "-y", "--no-install-recommends", str(package)]
    manual = f"sudo apt install -y ./{package.name}  （在 {package.parent} 目录下执行）"
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) or not shutil.which("pkexec"):
        return {"installed": False, "manual": manual, "path": str(package)}
    result = subprocess.run(["pkexec", *command], capture_output=True, text=True)
    if result.returncode in (126, 127):  # the password dialog was dismissed / not authorized
        return {"installed": False, "cancelled": True, "manual": manual, "path": str(package)}
    if result.returncode != 0:
        raise UpdateError(f"安装失败：{(result.stderr or result.stdout).strip()[-300:]}")
    _write(Path(root), {"available": False, "version": data.get("version"), "checked_at": data.get("checked_at")})
    return {"installed": True, "version": data.get("version"), "restart": _restart()}
