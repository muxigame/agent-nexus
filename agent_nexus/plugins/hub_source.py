"""The device side of the gateway's plugin repository (插件库, design §6.12).

The local manager talks to ``<hub>/api/plugin-repo`` over HTTPS with **this
device's own credential** (the token the invite gave it, ``secrets/cloud/
device-token``; or the configured ``token_env`` / ``token_secret``). The same
token authorizes the ingress link, so a device revoked in the console can
neither connect nor download.

* :func:`packages` - the repository listing, annotated with what is installed here;
* :func:`download` - one package into a local file, size-capped, sha256-verified;
* :func:`publish` - ``anx plugin publish``: an HTTPS ``POST /api/plugin-repo``
  with the device token (chosen over a ``hub_call`` op: packages are up to
  300 MB and the link's messages are capped far below that).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from .. import paths
from . import installer
from .pack import semver_key

#: The ceiling of a download: the gateway's own upper bound for ``plugin_repo.max_package_mb``. Each download
#: is capped at the size the gateway's index gives for that package, so the owner raising the gateway's limit
#: never leaves devices refusing what it accepted.
HARD_MAX_BYTES = 4096 * 1024 * 1024


class HubError(Exception):
    def __init__(self, message: str, code: str = "hub_error", status: int = 502, **extra: Any):
        super().__init__(message)
        self.code, self.status, self.extra = code, status, extra

    def body(self) -> dict[str, Any]:
        return {"error": str(self), "code": self.code, **self.extra}


def link(root: Path) -> tuple[str, str]:
    """``(https origin of the joined gateway, this device's token)``; raises :class:`HubError`."""
    from mcpkit.config import ConfigError

    from ..agent import connection_settings
    from ..federation import RoutingError
    from ..join import hub_url
    data = paths.read_config(Path(root))
    cloud = data.get("cloud") if isinstance(data.get("cloud"), dict) else {}
    if not cloud.get("enabled") or not cloud.get("url"):
        raise HubError("这台设备还没有加入网关：先在“连接云端”页用邀请码加入", "not_joined", 409)
    try:
        _, url, token = connection_settings(cloud, Path(root))
    except (ConfigError, RoutingError, OSError, ValueError) as exc:
        raise HubError(f"读不到这台设备的网关凭据：{exc}", "no_credential", 409) from None
    base = hub_url(url)
    if not base:
        raise HubError("cloud.url 不是 ws/wss 地址", "not_joined", 409)
    return base, token


def _client(timeout: float):
    import httpx
    return httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False)


def _refusal(response) -> HubError:
    try:
        data = response.json()
    except ValueError:
        data = {}
    message = data.get("error") if isinstance(data, dict) else None
    code = data.get("code") if isinstance(data, dict) else None
    if response.status_code == 401:
        return HubError("网关拒绝了这台设备的凭据（可能已被吊销）", "unauthorized", 401)
    extra = {"findings": data["findings"]} if isinstance(data, dict) and isinstance(data.get("findings"), list) else {}
    return HubError(message or f"网关返回 HTTP {response.status_code}", code or "hub_error",
                    response.status_code if 400 <= response.status_code < 500 else 502, **extra)


def fetch_listing(root: Path, *, timeout: float = 15.0) -> dict[str, Any]:
    import httpx
    base, token = link(root)
    try:
        with _client(timeout) as http:
            response = http.get(f"{base}/api/plugin-repo", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise HubError(f"连不上网关 {base}：{type(exc).__name__}", "hub_unreachable") from None
    if response.status_code == 404:
        raise HubError("这个网关还没有插件库（需要更新网关）", "unsupported_gateway", 409)
    if response.status_code != 200:
        raise _refusal(response)
    return response.json()


def annotate(items: list[dict[str, Any]], lock: dict[str, Any], installed: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark each package with what this device has: ``installed_version``, ``from_hub``, ``update_available``."""
    newest: dict[str, str] = {}
    for item in items:
        if item["name"] not in newest or semver_key(item["version"]) > semver_key(newest[item["name"]]):
            newest[item["name"]] = item["version"]
    result = []
    for item in items:
        record = lock.get(item["name"]) if isinstance(lock.get(item["name"]), dict) else {}
        view = installed.get(item["name"]) or {}
        version = record.get("version") or (view.get("version") if view.get("installed") else None)
        source = record.get("source") or {}
        from_hub = source.get("type") == "hub"
        result.append({**item, "installed_version": version or None, "from_hub": from_hub,
                       "installed": bool(view.get("installed")) or bool(record),
                       "registered": bool(view),  # an entry in agent-nexus.json, maybe 未安装
                       "update_available": bool(version and item["version"] == newest[item["name"]]
                                                and semver_key(item["version"]) > semver_key(version))})
    return result


def packages(root: Path, views: list[dict[str, Any]]) -> dict[str, Any]:
    data = fetch_listing(root)
    items = data.get("packages") if isinstance(data.get("packages"), list) else []
    lock = installer.read_lock(root)
    installed = {view["name"]: view for view in views}
    return {"available": True, "packages": annotate(items, lock, installed),
            "max_package_mb": data.get("max_package_mb")}


def download_cap(size: Any) -> int:
    """Bytes a download may take: the package's indexed ``size`` when it is sane, else :data:`HARD_MAX_BYTES`."""
    if isinstance(size, int) and not isinstance(size, bool) and 0 < size <= HARD_MAX_BYTES:
        return size
    return HARD_MAX_BYTES


def download(root: Path, path: str, dest: Path, *, sha256: str, log: Callable[[str], None],
             size: int | None = None, timeout: float = 60.0) -> Path:
    """Stream ``<hub><path>`` into ``dest``; raises :class:`installer.InstallError` on any problem.

    At most ``size`` bytes (the gateway's index) are accepted; without one, :data:`HARD_MAX_BYTES`.
    """
    import httpx
    max_bytes = download_cap(size)
    if not path.startswith("/api/plugin-repo/") or ".." in path or "//" in path:
        raise installer.InstallError("下载地址不合法", "invalid_source")
    try:
        base, token = link(root)
    except HubError as exc:
        raise installer.InstallError(str(exc), exc.code) from None
    log(f"从网关插件库下载 {base}{path}")
    digest = hashlib.sha256()
    size = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _client(timeout) as http, http.stream("GET", f"{base}{path}", headers={"Authorization": f"Bearer {token}"}) as response:
            if response.status_code != 200:
                response.read()
                error = _refusal(response)
                raise installer.InstallError(f"下载失败：{error}", error.code)
            with open(dest, "wb") as out:
                for chunk in response.iter_bytes(1 << 16):
                    size += len(chunk)
                    if size > max_bytes:
                        raise installer.InstallError(
                            f"插件包比网关登记的 {max_bytes / 1048576:.1f} MB 大，已停止下载", "too_large")
                    digest.update(chunk)
                    out.write(chunk)
    except httpx.HTTPError as exc:
        raise installer.InstallError(f"下载中断：{type(exc).__name__}", "hub_unreachable") from None
    actual = digest.hexdigest()
    log(f"已下载 {size / 1048576:.1f} MB，sha256 {actual[:16]}…")
    if actual.lower() != sha256.lower():
        dest.unlink(missing_ok=True)
        raise installer.InstallError(f"sha256 不匹配：期望 {sha256}，实际 {actual}；插件包可能被篡改，已拒绝安装",
                                     "checksum_mismatch")
    return dest


def publish(root: Path, archive: Path, *, replace: bool = False, timeout: float = 300.0) -> dict[str, Any]:
    """Upload a package to the joined gateway's 插件库 with this device's token."""
    import httpx
    base, token = link(root)
    query = "?replace=1" if replace else ""

    def body():
        with open(archive, "rb") as fh:
            while chunk := fh.read(1 << 20):
                yield chunk
    try:
        with _client(timeout) as http:
            response = http.post(f"{base}/api/plugin-repo{query}", content=body(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/zip",
                                          "Content-Length": str(Path(archive).stat().st_size)})
    except httpx.HTTPError as exc:
        raise HubError(f"连不上网关 {base}：{type(exc).__name__}", "hub_unreachable") from None
    if response.status_code not in (200, 201):
        raise _refusal(response)
    return response.json()["package"]
