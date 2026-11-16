"""The gateway's plugin repository (插件库, design §6.12; the owner's revision of decision D4).

The owner's private plugins are kept **on the gateway** as packages made by
``anx plugin pack``, so "安装到…" in the console installs straight from the
server: no source device has to be online, and no device needs git
credentials.

Storage: ``<plugin_repo.dir>/<name>/<version>/<name>-<version>.zip`` (default
``<state_dir>/plugin-repo``, created owner-only) plus the ``plugin_packages``
index in ``gateway.sqlite3``. Packages are private: they are never listed on
``/downloads`` and every route here needs the owner (cookie or bearer, like
the rest of ``/api/*``) or a device's own, non-revoked token.

Routes::

    GET    /api/plugin-repo                               owner or device: the packages
    POST   /api/plugin-repo[?replace=1]                   owner or device: upload a zip (raw body or multipart "file")
    DELETE /api/plugin-repo/{name}/{version}              owner
    GET    /api/plugin-repo/{name}/{version}/download     owner or device: the zip (streamed)
    POST   /api/devices/{id}/plugins/install {name, version?}   owner: reverse RPC plugin_repo_install
    GET    /api/devices/{id}/plugins/jobs/{job_id}?since=N      owner: reverse RPC plugin_repo_job

Every upload is checked again here, whoever made it: size, a safe zip, the
manifest's name and version, and the same secret scan as ``anx plugin pack``
(:func:`agent_nexus.plugins.pack.inspect_package`).

Who may write: the owner (console, bearer, ``anx-gateway plugin-repo``) may
add, replace and delete anything. A device may only publish a plugin name
that is its own: every version already in the repository under that name must
have been uploaded by the same device. Packages run their install steps and
their code on every device the owner installs them to, so one compromised
device must not be able to overwrite (or out-version) a package the owner or
another device uploaded.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets as _secrets
import time
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from .federation import RoutingError
from .inbox_web import NO_STORE
from .plugins import installer
from .plugins.pack import PackError, inspect_package, semver_key

log = logging.getLogger(__name__)

#: Reverse RPCs a device answers when it announces this feature (agent_nexus/features.py).
FEATURE = "plugin_repo/1"
DEFAULT_MAX_MB = 300
KNOWN_KEYS = frozenset({"dir", "max_package_mb"})
_NAME = installer.NAME_PATTERN
_VERSION = installer.VERSION_PATTERN
_JOB = re.compile(r"^j_[0-9a-f]{12}$")


class RepoError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def body(self) -> dict:
        return {"error": self.message, "code": self.code, **self.extra}


def repo_settings(config: dict, state_dir: Path, root: Path) -> dict:
    """``plugin_repo: {dir, max_package_mb}`` with defaults (``<state_dir>/plugin-repo``, 300 MB)."""
    from mcpkit.config import ConfigError
    section = config.get("plugin_repo") or {}
    if not isinstance(section, dict):
        raise ConfigError("plugin_repo must be an object")
    directory = section.get("dir")
    if directory is not None and (not isinstance(directory, str) or not directory.strip()):
        raise ConfigError("plugin_repo.dir must be a path string")
    limit = section.get("max_package_mb", DEFAULT_MAX_MB)
    if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not 1 <= limit <= 4096:
        raise ConfigError("plugin_repo.max_package_mb must be a number of megabytes (1-4096)")
    path = (Path(root) / directory).resolve() if directory else Path(state_dir) / "plugin-repo"
    return {"dir": path, "max_package_mb": limit}


def repo_warnings(section: Any) -> list[str]:
    if not isinstance(section, dict):
        return []
    return [f"unknown configuration key 'plugin_repo.{key}' (ignored)" for key in sorted(set(section) - KNOWN_KEYS)]


def owner_only_dir(path: Path) -> None:
    """Create ``path`` readable by its owner only (0700 on POSIX; the owner's ACL on Windows)."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        try:
            from mcpkit.config import protect
            protect(path)
        except Exception:  # pragma: no cover - best effort, as for secrets
            pass
        return
    os.chmod(path, 0o700)


class PluginRepo:
    """Packages on disk plus their index (``plugin_packages``)."""

    def __init__(self, store, directory: Path, *, max_package_mb: float = DEFAULT_MAX_MB):
        self.store = store
        self.dir = Path(directory)
        self.max_bytes = int(max_package_mb * 1024 * 1024)
        self.max_package_mb = max_package_mb

    # -- paths ---------------------------------------------------------------------
    @staticmethod
    def check_key(name: Any, version: Any) -> tuple[str, str]:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise RepoError(400, "invalid_name", "插件名不合法")
        if not isinstance(version, str) or not _VERSION.fullmatch(version) or ".." in version:
            raise RepoError(400, "invalid_version", "版本号不合法")
        return name, version

    def file(self, name: str, version: str) -> Path:
        name, version = self.check_key(name, version)
        path = (self.dir / name / version / f"{name}-{version}.zip").resolve()
        if not path.is_relative_to(self.dir.resolve()):  # belt and braces: the patterns already forbid it
            raise RepoError(400, "invalid_name", "插件名不合法")
        return path

    def incoming(self) -> Path:
        owner_only_dir(self.dir)
        path = self.dir / ".incoming"
        path.mkdir(exist_ok=True)
        return path / f"u-{_secrets.token_hex(8)}.zip"

    # -- the index -----------------------------------------------------------------
    def items(self) -> list[dict]:
        rows = self.store.packages()
        newest: dict[str, tuple] = {}
        for row in rows:
            key = semver_key(row["version"])
            if row["name"] not in newest or key > newest[row["name"]]:
                newest[row["name"]] = key
        result = []
        for row in rows:
            info = row.get("info") or {}
            result.append({"name": row["name"], "version": row["version"], "title": row.get("title") or row["name"],
                           "description": row.get("description") or "", "sha256": row["sha256"], "size": row["size"],
                           "files": row["files"], "uploaded_at": row["uploaded_at"], "uploaded_by": row.get("uploaded_by"),
                           "platforms": (info.get("manifest") or {}).get("platforms") or [],
                           "latest": semver_key(row["version"]) == newest[row["name"]]})
        result.sort(key=lambda item: semver_key(item["version"]), reverse=True)  # newest first ...
        result.sort(key=lambda item: item["name"])                                # ... within each name
        return result

    def pick(self, name: Any, version: Any = None) -> dict:
        """The package ``name`` at ``version``, or its newest version."""
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise RepoError(400, "invalid_name", "需要插件名")
        if version not in (None, ""):
            self.check_key(name, version)
            row = self.store.package(name, version)
            if row is None:
                raise RepoError(404, "unknown_package", f"插件库里没有 {name} {version}")
            return row
        rows = self.store.packages(name)
        if not rows:
            raise RepoError(404, "unknown_package", f"插件库里没有 {name}")
        return max(rows, key=lambda row: semver_key(row["version"]))

    # -- writes --------------------------------------------------------------------
    def add(self, upload: Path, *, replace: bool = False, by: str = "owner") -> dict:
        """Validate ``upload`` (a file this repo may move) and file it under its name and version."""
        upload = Path(upload)
        try:
            try:
                info = inspect_package(upload, max_bytes=self.max_bytes)
            except PackError as exc:
                status = 413 if exc.code == "too_large" else 422
                raise RepoError(status, exc.code, str(exc), **({"findings": exc.findings} if exc.findings else {})) from None
            name, version = info["name"], info["version"]
            if by.startswith("device:"):
                others = sorted({row.get("uploaded_by") or "owner" for row in self.store.packages(name)} - {by})
                if others:
                    who = "所有者" if any(not o.startswith("device:") for o in others) else "另一台设备"
                    raise RepoError(403, "not_your_package",
                                    f"插件库里的 {name} 是{who}上传的：设备不能覆盖它或给它加新版本。"
                                    "请在网关控制台（所有者）上传，或先由所有者删除旧版本", uploaded_by=others)
            existing = self.store.package(name, version)
            if existing is not None and not replace:
                raise RepoError(409, "exists", f"插件库里已经有 {name} {version}；换一个版本号，或用 replace 覆盖",
                                sha256=existing["sha256"])
            target = self.file(name, version)
            owner_only_dir(self.dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(upload, target)
            item = {"name": name, "version": version, "title": info["title"], "description": info["description"][:1000],
                    "sha256": info["sha256"], "size": info["size"], "files": info["files"], "uploaded_at": time.time(),
                    "uploaded_by": by, "info": {"pack": info["pack"], "manifest": info["manifest"]}}
            self.store.package_put(item)
            log.info("plugin repo: %s %s added by %s (%d bytes)", name, version, by, info["size"])
            return {**{k: v for k, v in item.items() if k != "info"}, "replaced": existing is not None}
        finally:
            upload.unlink(missing_ok=True)

    def remove(self, name: Any, version: Any) -> dict:
        name, version = self.check_key(name, version)
        if self.store.package(name, version) is None:
            raise RepoError(404, "unknown_package", f"插件库里没有 {name} {version}")
        path = self.file(name, version)
        path.unlink(missing_ok=True)
        for directory in (path.parent, path.parent.parent):
            try:
                directory.rmdir()
            except OSError:
                pass
        self.store.package_delete(name, version)
        return {"removed": True, "name": name, "version": version}


class PluginRepoWeb:
    """The HTTP side: owner APIs, device downloads, and "安装到…" through the device's link."""

    def __init__(self, repo: PluginRepo, console, *, agents: dict[str, bytes], groups: dict[str, bytes]):
        self.repo = repo
        self.console = console
        self.hub = console.hub
        self.facade = console.facade
        self.agents = agents
        self.groups = groups

    # -- who is asking ---------------------------------------------------------------
    def device(self, request: Request) -> str | None:
        """The agent_id behind a device credential in ``Authorization: Bearer``; ``"revoked"`` for a revoked one."""
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return None
        token = header.removeprefix("Bearer ").strip()
        if len(token) < 32:
            return None
        digest = hashlib.sha256(token.encode()).digest()
        owner = self.repo.store.device_token_owner(digest.hex())
        if owner is not None:
            return "revoked" if owner[1] else owner[0]
        for agent_id, expected in self.agents.items():
            if hmac.compare_digest(digest, expected):
                return agent_id
        for prefix, expected in self.groups.items():
            if hmac.compare_digest(digest, expected):
                return f"{prefix}*"
        return None

    async def caller(self, request: Request, *, devices: bool) -> str | None:
        """``"owner"``, ``"device:<agent_id>"`` or ``None`` (then answer 401)."""
        who = self.device(request) if devices else None
        if who == "revoked":
            return None
        if who:
            return f"device:{who}"
        if await self.console.owner(request) is not None:
            return "owner"
        return None

    @staticmethod
    def _json(data: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status, headers=NO_STORE)

    def _fail(self, exc: RepoError) -> JSONResponse:
        return self._json(exc.body(), exc.status)

    # -- the repository --------------------------------------------------------------
    async def listing(self, request: Request) -> Response:
        if await self.caller(request, devices=True) is None:
            return self.console.unauthorized()
        items = await run_in_threadpool(self.repo.items)
        return self._json({"packages": items, "max_package_mb": self.repo.max_package_mb})

    async def upload(self, request: Request) -> Response:
        who = await self.caller(request, devices=True)
        if who is None:
            return self.console.unauthorized()
        replace = request.query_params.get("replace") in ("1", "true")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.repo.max_bytes + 1024 * 1024:
            return self._json({"error": f"插件包超过上限 {self.repo.max_package_mb} MB", "code": "too_large"}, 413)
        kind = request.headers.get("content-type", "")
        if kind.startswith("multipart/form-data") and not (declared and declared.isdigit()):
            # Starlette's form parser spools the whole body before we could count it: insist on a length.
            return self._json({"error": "multipart 上传需要 Content-Length；或者直接把 zip 作为请求体发送",
                               "code": "length_required"}, 411)
        target = await run_in_threadpool(self.repo.incoming)
        try:
            if kind.startswith("multipart/form-data"):
                written = await self._save_multipart(request, target)
            else:
                written = await self._save_stream(request.stream(), target)
        except RepoError as exc:
            target.unlink(missing_ok=True)
            return self._fail(exc)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        if not written:
            target.unlink(missing_ok=True)
            return self._json({"error": "没有收到插件包", "code": "empty"}, 400)
        try:
            item = await run_in_threadpool(lambda: self.repo.add(target, replace=replace, by=who))
        except RepoError as exc:
            return self._fail(exc)
        return self._json({"package": item}, 200 if item["replaced"] else 201)

    async def _save_stream(self, chunks, target: Path) -> int:
        written = 0
        with open(target, "wb") as out:
            async for chunk in chunks:
                written += len(chunk)
                if written > self.repo.max_bytes:
                    raise RepoError(413, "too_large", f"插件包超过上限 {self.repo.max_package_mb} MB")
                out.write(chunk)
        return written

    async def _save_multipart(self, request: Request, target: Path) -> int:
        form = await request.form(max_files=1, max_fields=8, max_part_size=1024 * 1024)
        try:
            upload = form.get("file")
            if upload is None or isinstance(upload, str):
                raise RepoError(400, "empty", "multipart 里需要 file 字段")

            async def chunks():
                while chunk := await upload.read(1 << 20):
                    yield chunk
            return await self._save_stream(chunks(), target)
        finally:
            await form.close()

    async def delete(self, request: Request) -> Response:
        if await self.console.owner(request) is None:
            return self.console.unauthorized()
        try:
            result = await run_in_threadpool(self.repo.remove, request.path_params["name"], request.path_params["version"])
        except RepoError as exc:
            return self._fail(exc)
        return self._json(result)

    async def download(self, request: Request) -> Response:
        if await self.caller(request, devices=True) is None:
            return self.console.unauthorized()
        name, version = request.path_params["name"], request.path_params["version"]
        try:
            self.repo.check_key(name, version)
            row = self.repo.store.package(name, version)
            path = self.repo.file(name, version)
        except RepoError as exc:
            return self._fail(exc)
        if row is None or not path.is_file():
            return self._json({"error": f"插件库里没有 {name} {version}", "code": "unknown_package"}, 404)
        return FileResponse(path, media_type="application/zip", filename=path.name,
                            headers={**NO_STORE, "X-Content-Type-Options": "nosniff", "X-ANX-SHA256": row["sha256"]})

    # -- install on a device ---------------------------------------------------------------
    def _peer(self, device_id: str):
        try:
            agent_id = self.facade.devices.resolve(device_id)
        except RoutingError as exc:
            raise RepoError(404, "unknown_device", f"没有这台设备：{device_id}", detail=str(exc)) from None
        peer = self.hub.peers.get(agent_id)
        if peer is None and agent_id not in self.facade.devices._known():
            raise RepoError(404, "unknown_device", f"网关上没有这台设备：{device_id}（看看设备页里的名字或 ID）")
        name = self.facade.devices.display_name(agent_id)
        if peer is None:
            raise RepoError(409, "device_offline", f"{name} 不在线：等它连上网关后再安装")
        if FEATURE not in peer.features:
            raise RepoError(409, "unsupported_device",
                            f"{name} 的 Agent Nexus 版本太旧（{peer.anx_version or '旧版'}），不支持从插件库安装：先把它更新到最新版本")
        return agent_id, name, peer

    async def install(self, request: Request) -> Response:
        if await self.console.owner(request) is None:
            return self.console.unauthorized()
        body = await self.console._body(request)
        if body is None:
            return self._json({"error": "body must be a JSON object"}, 400)
        try:
            agent_id, name, peer = self._peer(request.path_params["device_id"])
            row = await run_in_threadpool(self.repo.pick, body.get("name"), body.get("version"))
            platforms = ((row.get("info") or {}).get("manifest") or {}).get("platforms") or []
            device_platform = (self.facade.devices.describe(agent_id).get("platform") or "")
            if platforms and device_platform and not any(_platform_ok(p, device_platform) for p in platforms):
                raise RepoError(409, "unsupported_platform",
                                f"{row['name']} 只支持 {', '.join(platforms)}，{name} 是 {device_platform}")
            params = {"name": row["name"], "version": row["version"], "sha256": row["sha256"], "size": row["size"],
                      "path": f"/api/plugin-repo/{row['name']}/{row['version']}/download",
                      "enable": body.get("enable", True) is not False}
            try:
                reply = await asyncio.wait_for(peer.call("plugin_repo_install", params), 30)
            except (RoutingError, asyncio.TimeoutError, OSError) as exc:
                raise RepoError(502, "device_error", f"{name} 没有响应安装请求：{_reason(exc)}") from None
        except RepoError as exc:
            return self._fail(exc)
        if not isinstance(reply, dict) or not reply.get("job_id"):
            error = reply.get("error") if isinstance(reply, dict) else None
            return self._json({"error": error or f"{name} 拒绝了安装请求", "code": (reply or {}).get("code") or "device_error"}, 409)
        return self._json({"job_id": reply["job_id"], "device_id": agent_id, "display_name": name,
                           "name": row["name"], "version": row["version"]}, 202)

    async def job(self, request: Request) -> Response:
        if await self.console.owner(request) is None:
            return self.console.unauthorized()
        job_id = request.path_params["job_id"]
        if not _JOB.fullmatch(job_id):
            return self._json({"error": "没有这个安装任务", "code": "unknown_job"}, 404)
        try:
            since = max(0, min(10 ** 6, int(request.query_params.get("since", "0"))))
        except ValueError:
            since = 0
        try:
            _, name, peer = self._peer(request.path_params["device_id"])
            try:
                reply = await asyncio.wait_for(peer.call("plugin_repo_job", {"job_id": job_id, "since": since}), 20)
            except (RoutingError, asyncio.TimeoutError, OSError) as exc:
                raise RepoError(502, "device_error", f"{name} 没有响应：{_reason(exc)}") from None
        except RepoError as exc:
            return self._fail(exc)
        if not isinstance(reply, dict) or reply.get("error") and not reply.get("id"):
            return self._json({"error": (reply or {}).get("error") or "没有这个安装任务", "code": "unknown_job"}, 404)
        return self._json(reply)

    def routes(self) -> list[Route]:
        return [
            Route("/api/plugin-repo", self.listing, methods=["GET"]),
            Route("/api/plugin-repo", self.upload, methods=["POST"]),
            Route("/api/plugin-repo/{name}/{version}", self.delete, methods=["DELETE"]),
            Route("/api/plugin-repo/{name}/{version}/download", self.download, methods=["GET", "HEAD"]),
            Route("/api/devices/{device_id}/plugins/install", self.install, methods=["POST"]),
            Route("/api/devices/{device_id}/plugins/jobs/{job_id}", self.job, methods=["GET"]),
        ]


def _platform_ok(wanted: str, platform: str) -> bool:
    """Manifest platforms are ``sys.platform`` names; devices report windows / linux / darwin."""
    aliases = {"win32": "windows", "windows": "windows", "linux": "linux", "darwin": "darwin", "macos": "darwin"}
    return aliases.get(str(wanted).lower(), str(wanted).lower()) == aliases.get(platform.lower(), platform.lower())


def _reason(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "超时"
    text = str(exc)
    return text[:200] if text else type(exc).__name__
