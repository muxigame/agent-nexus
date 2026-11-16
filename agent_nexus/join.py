"""Joining a gateway with an invite code: the device side of design §11.5 ("使用邀请").

``join()`` decodes the code (``anx1.…`` or ``agentnexus://enroll?invite=…``),
redeems it with ``POST <hub>/enroll``, writes the per-device token to
``secrets/cloud/device-token`` (owner-only ACL, ``protect()``) and the
``cloud`` section of ``agent-nexus.json``::

    "cloud": {"enabled": true, "url": "wss://<hub>", "agent_id": "<id>",
              "token_secret": "cloud/device-token", ...kept: workspaces, mcps, refresh_interval_s}

The local console (``POST /api/cloud/join``) and ``anx join <code>`` both
call it; the console then restarts its cloud link. A failed join changes
nothing on disk and never retries the same invite.
"""
from __future__ import annotations

import json
import platform
import socket
import sys
from pathlib import Path
from typing import Any

from mcpkit.config import ConfigError, ServiceConfig, secret_path, write_secret

from . import paths
from .invites import EnrollError, decode_invite

TOKEN_SECRET = "cloud/device-token"
PRIVATE_PROFILE = ".private/enrollment.json"
#: Keys of an existing ``cloud`` section that survive a (re)join.
KEPT_KEYS = ("workspaces", "mcps", "refresh_interval_s", "rpc_timeout_s", "wake", "hostname", "console_url")


class JoinError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def body(self) -> dict:
        return {"error": self.message, "code": self.code}


def ingress_url(hub: str) -> str:
    """``https://h/base`` -> ``wss://h/base`` (``http://`` loopback -> ``ws://``)."""
    if hub.startswith("https://"):
        return "wss://" + hub[len("https://"):]
    if hub.startswith("http://"):
        return "ws://" + hub[len("http://"):]
    return hub


def hub_url(ingress: Any) -> str | None:
    if not isinstance(ingress, str):
        return None
    if ingress.startswith("wss://"):
        return "https://" + ingress[len("wss://"):].rstrip("/")
    if ingress.startswith("ws://"):
        return "http://" + ingress[len("ws://"):].rstrip("/")
    return None


def local_hostname() -> str:
    return (platform.node() or socket.gethostname() or "device").strip()[:255]


def device_facts() -> dict:
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux" if sys.platform.startswith("linux") else sys.platform)
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(machine, machine or "unknown")
    release = f"{platform.system()} {platform.release()}".strip()
    return {"platform": system, "os": release, "arch": arch, "form": "pc"}


def managed(root: Path) -> bool:
    """A private-distribution profile owns the cloud identity; it is never replaced here."""
    return (Path(root) / PRIVATE_PROFILE).is_file()


def cloud_config(root: Path) -> dict:
    data = paths.read_config(root)
    cloud = data.get("cloud") if isinstance(data.get("cloud"), dict) else {}
    return cloud


def joined(root: Path) -> bool:
    cloud = cloud_config(root)
    return bool(cloud.get("enabled")) and bool(cloud.get("url")) and bool(cloud.get("agent_id"))


def describe(root: Path) -> dict:
    """What the console's ``#/cloud`` page shows besides the live link state."""
    cloud = cloud_config(root)
    return {"joined": joined(root), "hub": hub_url(cloud.get("url")), "agent_id": cloud.get("agent_id"),
            "managed": managed(root), "enrolled_via": "invite" if cloud.get("token_secret") == TOKEN_SECRET
            else ("token_env" if cloud.get("token_env") else ("secret" if cloud.get("token_secret") else None))}


def _write_config(root: Path, data: dict) -> None:
    path = paths.write_path(root)
    temporary = path.with_suffix(".json.join.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def join(root: Path, invite: Any, *, agent_id: str | None = None, hostname: str | None = None,
         replace: bool = False, http=None, timeout: float = 20.0) -> dict:
    """Redeem ``invite`` and configure this root's cloud link. Raises :class:`JoinError`."""
    root = Path(root)
    try:
        decoded = decode_invite(invite)
    except EnrollError as exc:
        raise JoinError(400, "invalid_invite", exc.message) from None
    if managed(root):
        raise JoinError(409, "managed", "这台设备由私有入网配置管理，不能用邀请码更换网关")
    if joined(root) and not replace:
        raise JoinError(409, "already_joined", "这台设备已经加入了网关；先退出再用新的邀请码加入")
    name = (hostname or local_hostname()).strip()[:255] or "device"
    body = {"invite_id": decoded["id"], "secret": decoded["secret"], "hostname": name, "device": device_facts()}
    if agent_id:
        body["agent_id"] = agent_id
    import httpx
    client = http or httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False)
    try:
        response = client.post(decoded["hub"].rstrip("/") + "/enroll", json=body)
    except httpx.HTTPError:
        raise JoinError(502, "hub_unreachable", f"无法连接网关 {decoded['hub']}") from None
    finally:
        if http is None:
            client.close()
    try:
        result = response.json()
    except ValueError:
        result = {}
    if response.status_code not in (200, 201):
        code = result.get("code") if isinstance(result, dict) else None
        message = result.get("error") if isinstance(result, dict) else None
        status = {403: 400}.get(response.status_code, response.status_code)
        if status >= 500 or not code:
            status, code = 502, code or "hub_error"
            message = message or f"网关返回 HTTP {response.status_code}"
        raise JoinError(status, code, message or "加入失败")
    token, new_id = result.get("token"), result.get("agent_id")
    if not isinstance(token, str) or len(token) < 32 or not isinstance(new_id, str):
        raise JoinError(502, "hub_error", "网关的响应不完整")
    config = ServiceConfig(name="manager", root=root, dir=root)
    try:
        write_secret(config, TOKEN_SECRET, token)
        data = paths.read_config(root)
        previous = data.get("cloud") if isinstance(data.get("cloud"), dict) else {}
        cloud = {key: previous[key] for key in KEPT_KEYS if key in previous}
        cloud.update(enabled=True, url=ingress_url(decoded["hub"]), agent_id=new_id, token_secret=TOKEN_SECRET)
        if hostname:
            cloud["hostname"] = name  # an explicit name is what the catalog reports too
        data["cloud"] = cloud
        _write_config(root, data)
    except (OSError, ConfigError) as exc:
        raise JoinError(500, "write_failed", f"写入本机配置失败：{type(exc).__name__}") from None
    return {"state": "joined", "hub": decoded["hub"], "agent_id": new_id,
            "display_name": result.get("display_name") or name}


def leave(root: Path) -> dict:
    """Forget the gateway: remove the device token and disable the ``cloud`` section."""
    root = Path(root)
    if managed(root):
        raise JoinError(409, "managed", "这台设备由私有入网配置管理，不能在这里退出")
    data = paths.read_config(root)
    cloud = data.get("cloud") if isinstance(data.get("cloud"), dict) else {}
    if cloud.get("token_secret") == TOKEN_SECRET:
        try:
            secret_path(ServiceConfig(name="manager", root=root, dir=root), TOKEN_SECRET).unlink(missing_ok=True)
        except (OSError, ConfigError):
            pass
    if cloud:
        kept = {key: cloud[key] for key in KEPT_KEYS if key in cloud}
        data["cloud"] = {**kept, "enabled": False}
        _write_config(root, data)
    return {"state": "not_joined"}
