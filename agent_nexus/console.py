"""The gateway's web console (docs/ui-spec.md §2-§3; decision D3, design §11.5).

* ``/`` — the console single-page app (``static/console.html`` + ``static/console/*.js``
  on the shared design system ``/ui/``) for the signed-in owner; anyone else is
  sent to ``/auth/login``.
* ``/auth/login`` — the owner password (the same one the OAuth consent page asks
  for), or the gateway token on bearer-only gateways. It sets the inbox's
  existing session cookie (``__Host-anx_inbox``: HttpOnly, Secure, SameSite=Strict),
  so one sign-in covers the console, ``/api/*`` and the ``/inbox/`` PWA.
* ``/api/console/summary``, ``/api/devices``, ``/api/invites``, ``/api/settings``
  — owner APIs (bearer or cookie; cookie writes need ``X-ANX: 1``).
* ``/api/downloads``, ``/downloads``, ``/downloads/<file>`` and ``POST /enroll``
  are public: a new device fetches the installer and redeems its invite before
  it has any credential.
"""
from __future__ import annotations

import hmac
import html
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import __version__
from . import features as anx_features
from .federation import RESERVED_TOOLS, RoutingError
from .inbox_web import CLIENT_ID, COOKIE, NO_STORE, PAGE_CSP
from .invites import EnrollError

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
CONSOLE_DIR = STATIC_DIR / "console"
SESSION_TTL_S = 12 * 3600
REMEMBER_TTL_S = 30 * 86400
MAX_LOGIN_ATTEMPTS_PER_IP = 8
JSON_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


def _safe_next(value: Any) -> str:
    """Only same-site paths: ``/`` and ``/inbox/``-style, never ``//host`` or a scheme."""
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(ch) < 32 for ch in value) or len(value) > 512:
        return "/"
    return value


class Console:
    def __init__(self, *, config: dict, facade, hub, store, server, inbox_web, enrollment, downloads):
        self.config = config
        self.facade = facade
        self.hub = hub
        self.store = store
        self.server = server
        self.inbox_web = inbox_web
        self.enrollment = enrollment
        self.downloads = downloads
        self.public_url = (config.get("public_url") or "").rstrip("/")
        self.mcp_path = config.get("path", "/mcp")
        self.provider = inbox_web.provider
        self.attempts: dict[str, list[float]] = {}

    # -- helpers -------------------------------------------------------------------
    async def owner(self, request: Request) -> str | None:
        return await self.inbox_web.owner(request)

    @staticmethod
    def unauthorized() -> JSONResponse:
        return JSONResponse({"error": "unauthorized"}, status_code=401,
                            headers={**NO_STORE, "WWW-Authenticate": "Bearer"})

    @staticmethod
    def _json(data: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status, headers=NO_STORE)

    @staticmethod
    async def _body(request: Request) -> dict | None:
        try:
            body = await request.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def origin(self, request: Request) -> str:
        if self.public_url:
            return self.public_url
        return f"{request.url.scheme}://{request.url.netloc}"

    def mcp_url(self, request: Request) -> str:
        return self.origin(request) + self.mcp_path

    # -- login ---------------------------------------------------------------------
    def _login_page(self, request: Request, *, error: str = "", status: int = 200, next_path: str = "/") -> HTMLResponse:
        password_mode = self.provider is not None
        host = urlsplit(self.origin(request)).netloc
        field = ('<label for="password">Owner 密码</label>'
                 '<input class="input input-lg" id="password" name="password" type="password" '
                 'autocomplete="current-password" maxlength="4096" required autofocus>') if password_mode else (
                 '<label for="token">网关令牌</label>'
                 '<input class="input input-lg mono" id="token" name="token" type="password" '
                 'autocomplete="current-password" maxlength="4096" required autofocus>'
                 '<span class="hint">即 gateway.env 中的 ANX_SERVER_TOKEN（或 token_env 指定的变量）。</span>')
        intro = ("输入网关的 owner 密码。它与 ChatGPT、Claude 连接时授权页使用的密码相同。" if password_mode
                 else "这个网关只配置了静态令牌：输入网关令牌登录。")
        error_html = (f'<span class="error-text" role="alert">{html.escape(error)}</span>' if error else "")
        invalid = ' aria-invalid="true"' if error else ""
        if invalid:
            field = field.replace('maxlength="4096"', 'maxlength="4096"' + invalid)
        body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>登录 · Agent Nexus</title>
<link rel="icon" href="/console/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/ui/tokens.css"><link rel="stylesheet" href="/ui/ui.css">
</head><body>
<main class="auth">
  <form class="auth-card" method="post" action="/auth/login">
    <div class="auth-head">
      {LOGO_SVG}
      <h1>登录 Agent Nexus</h1>
      <p>{intro}</p>
    </div>
    <div class="form">
      <input type="hidden" name="next" id="next" value="{html.escape(next_path)}">
      <div class="field">{field}{error_html}</div>
      <label class="check"><input type="checkbox" name="remember" value="1" checked> 在这台设备上保持登录 30 天</label>
      <button class="btn btn-primary btn-lg btn-block" type="submit">登录</button>
    </div>
  </form>
  <div class="auth-foot"><span>{html.escape(host)}</span><a href="/downloads">下载客户端</a></div>
</main>
<script>
// The fragment (#/devices) never reaches the server: carry it through the sign-in.
(function () {{ var n = document.getElementById("next"); if (location.hash && n.value === "/") n.value = "/" + location.hash; }})();
</script>
</body></html>"""
        # no-referrer would make the browser's form POST send "Origin: null", which the
        # gateway's origin protection refuses; strict-origin keeps the real origin only.
        return HTMLResponse(body, status_code=status, headers={**NO_STORE, "Referrer-Policy": "strict-origin",
                                                               "Content-Security-Policy": PAGE_CSP})

    def _issue(self, ttl: int) -> str:
        """An owner access token for the console cookie, valid ``ttl`` seconds (same kind as the PWA's)."""
        provider = self.provider
        original = provider.config
        # issue() reads the TTL from its config; swap a copy in for this one call (no await in between).
        provider.config = original.model_copy(update={"access_token_ttl_s": ttl})
        try:
            return provider.issue(CLIENT_ID, list(original.scopes)).access_token
        finally:
            provider.config = original

    async def login(self, request: Request) -> Response:
        next_path = _safe_next(request.query_params.get("next", "/"))
        if request.method == "GET":
            if await self.owner(request) == "cookie":
                return RedirectResponse(next_path, status_code=303, headers=NO_STORE)
            return self._login_page(request, next_path=next_path)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return HTMLResponse("Forbidden", status_code=403, headers=NO_STORE)
        origin = request.headers.get("origin")
        if origin and origin not in ("null", self.origin(request), f"{request.url.scheme}://{request.url.netloc}"):
            return HTMLResponse("Forbidden", status_code=403, headers=NO_STORE)
        form = await request.form()
        next_path = _safe_next(form.get("next", "/"))
        remember = form.get("remember") in ("1", "on", "true")
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        self.attempts = {key: [t for t in values if t > now - 300] for key, values in self.attempts.items()
                         if any(t > now - 300 for t in values)}
        if len(self.attempts.get(ip, [])) >= MAX_LOGIN_ATTEMPTS_PER_IP:
            return self._login_page(request, error="尝试过于频繁，请 5 分钟后再试", status=429, next_path=next_path)
        ttl = REMEMBER_TTL_S if remember else SESSION_TTL_S
        if self.provider is not None:
            from mcpkit.oauth import digest
            candidate = str(form.get("password", ""))
            if not candidate or not hmac.compare_digest(self.provider.password_hash, digest(candidate)):
                self.attempts.setdefault(ip, []).append(now)
                return self._login_page(request, error="密码不正确", status=401, next_path=next_path)
            token = self._issue(ttl)
        else:
            token = str(form.get("token", ""))
            if not await self.inbox_web._verify(token):
                self.attempts.setdefault(ip, []).append(now)
                return self._login_page(request, error="令牌不正确", status=401, next_path=next_path)
        self.attempts.pop(ip, None)
        response = RedirectResponse(next_path, status_code=303, headers=NO_STORE)
        response.set_cookie(COOKIE, token, max_age=ttl if remember else None, path="/", secure=True,
                            httponly=True, samesite="strict")
        return response

    async def token(self, request: Request) -> Response:
        """``POST /api/auth/token`` {password, remember}: an owner bearer token for ``anx term``.

        Same password, same per-IP attempt limit and same token kind as the login page; JSON
        only, so a browser page elsewhere cannot post it without a CORS preflight we never allow.
        """
        if request.headers.get("sec-fetch-site") == "cross-site":
            return self._json({"error": "forbidden"}, 403)
        if not request.headers.get("content-type", "").startswith("application/json"):
            return self._json({"error": "send JSON"}, 415)
        body = await self._body(request) or {}
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        self.attempts = {key: [t for t in values if t > now - 300] for key, values in self.attempts.items()
                         if any(t > now - 300 for t in values)}
        if len(self.attempts.get(ip, [])) >= MAX_LOGIN_ATTEMPTS_PER_IP:
            return self._json({"error": "尝试过于频繁，请 5 分钟后再试"}, 429)
        ttl = REMEMBER_TTL_S if body.get("remember") else SESSION_TTL_S
        if self.provider is not None:
            from mcpkit.oauth import digest
            candidate = str(body.get("password") or "")
            if not candidate or not hmac.compare_digest(self.provider.password_hash, digest(candidate)):
                self.attempts.setdefault(ip, []).append(now)
                return self._json({"error": "密码不正确"}, 401)
            token = self._issue(ttl)
        else:
            token = str(body.get("password") or body.get("token") or "")
            if not await self.inbox_web._verify(token):
                self.attempts.setdefault(ip, []).append(now)
                return self._json({"error": "令牌不正确"}, 401)
        self.attempts.pop(ip, None)
        return self._json({"access_token": token, "token_type": "Bearer", "expires_in": ttl})

    async def logout(self, request: Request) -> Response:
        if request.headers.get("x-anx") != "1":
            return JSONResponse({"error": "missing X-ANX header"}, status_code=403)
        token = request.cookies.get(COOKIE, "")
        if token and self.provider is not None:
            try:
                access = await self.provider.load_access_token(token)
                if access is not None:
                    await self.provider.revoke_token(access)
            except Exception:  # pragma: no cover - best effort
                pass
        response = Response(status_code=204, headers=NO_STORE)
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response

    # -- pages -----------------------------------------------------------------------
    async def index(self, request: Request) -> Response:
        if await self.owner(request) is None:
            return RedirectResponse("/auth/login", status_code=302, headers=NO_STORE)
        return FileResponse(STATIC_DIR / "console.html", media_type="text/html",
                            headers={**NO_STORE, "Content-Security-Policy": PAGE_CSP})

    async def downloads_page(self, request: Request) -> Response:
        return FileResponse(STATIC_DIR / "downloads.html", media_type="text/html",
                            headers={**NO_STORE, "Content-Security-Policy": PAGE_CSP})

    async def asset(self, request: Request) -> Response:
        name = request.path_params["name"]
        path = CONSOLE_DIR / name
        media = JSON_TYPES.get(path.suffix) or ("image/svg+xml" if path.suffix == ".svg" else None)
        if "/" in name or "\\" in name or ".." in name or media is None or not path.is_file():
            return HTMLResponse("Not found", status_code=404)
        return FileResponse(path, media_type=media, headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    # -- owner APIs --------------------------------------------------------------------
    def _clients(self) -> list[dict]:
        if self.provider is None:
            return []
        from .gateway import client_kind_from_name
        try:
            with self.provider.state.transaction() as db:
                rows = db.execute("SELECT key, body FROM entries WHERE kind='client'").fetchall()
        except Exception:
            return []
        clients = []
        for key, body in rows:
            if key == CLIENT_ID:
                continue
            try:
                item = json.loads(body)
            except ValueError:
                continue
            name = item.get("client_name") or "未命名客户端"
            clients.append({"client_id": key, "client_name": name, "name": name,
                            "kind": client_kind_from_name(name) or "other",
                            "created_at": item.get("client_id_issued_at")})
        return sorted(clients, key=lambda item: item.get("created_at") or 0, reverse=True)

    def _gateway_info(self, request: Request) -> dict:
        size = self.facade.mailbox.db_size_mb()
        warn = self.facade.mailbox_config["retention"]["warn_db_mb"]
        return {"version": __version__, "public_url": self.origin(request), "public_url_configured": bool(self.public_url),
                "mcp_url": self.mcp_url(request), "features": list(anx_features.GATEWAY_FEATURES),
                "db_mb": size, "db_warning": bool(size is not None and warn and size > warn),
                "state_dir": str(self.store.path.parent) if self.store.path else None,
                "auth_mode": "oauth" if self.provider is not None else "bearer"}

    async def summary(self, request: Request) -> Response:
        if await self.owner(request) is None:
            return self.unauthorized()
        devices = sorted(self.facade.devices.console_list(),
                         key=lambda device: (not device["online"], bool(device.get("revoked")), device["display_name"].casefold()))
        online = [device for device in devices if device["online"]]
        inbox = {"unread": 0, "open_asks": 0, "enabled": bool(self.facade.mailbox_config["enabled"])}
        recent: list[dict] = []
        if inbox["enabled"]:
            try:
                data = await self.facade.inbox.call("summary", {}, via="inbox")
                inbox.update(unread=data.get("unread", 0), open_asks=data.get("open_asks", 0))
                threads = await self.facade.inbox.call("threads", {}, via="inbox")
                recent = (threads.get("threads") or [])[:3]
            except RoutingError:
                pass
        tool_count = len(RESERVED_TOOLS)
        for peer in list(self.hub.peers.values()):
            for mcp in (peer.catalog.get("mcps") or {}).values():
                tool_count += len(mcp.get("tools") or [])
        downloads = await run_in_threadpool(self.downloads.items)
        clients = self._clients()
        gateway = self._gateway_info(request)
        alerts = []
        if gateway["db_warning"]:
            alerts.append({"kind": "db_warning", "title": "网关数据库超过警戒大小", "level": "warn"})
        if not self.public_url:
            alerts.append({"kind": "no_public_url", "title": "gateway.json 没有设置 public_url",
                           "text": "邀请码需要网关的公网地址；设置 public_url 后重启网关。", "level": "warn"})
        host = getattr(self.hub, "connectors", None)
        connectors = host.summary() if host is not None else {"total": 0, "needs_login": 0, "error": 0, "available": False, "tools": 0, "items": []}
        tool_count += connectors.get("tools", 0)
        for item in connectors["items"]:
            if item["state"] == "needs_login":
                alerts.insert(0, {"kind": "connector_needs_login", "target": item["name"], "level": "warn",
                                  "title": f"{item['title'] or item['name']} 需要登录",
                                  "text": "Agent 暂时无法调用这个连接器的工具。到“连接器”页点“登录”。", "action": "login"})
            elif item["state"] == "error":
                alerts.append({"kind": "connector_error", "target": item["name"], "level": "bad",
                               "title": f"{item['title'] or item['name']} 连接失败", "text": "到“连接器”页查看错误并测试。"})
        setup = {"installer_available": any(item["kind"] == "desktop" for item in downloads["items"]) or bool(downloads["links"]),
                 "device_joined": bool(devices), "client_authorized": bool(clients),
                 "connector_added": bool(connectors["total"])}
        return self._json({
            "gateway": gateway, "devices": {"online": len(online), "total": len(devices), "items": devices[:5]},
            "connectors": connectors,
            "inbox": inbox, "recent_threads": recent, "tools": {"total": tool_count, "connectors": connectors.get("tools", 0)},
            "clients": clients, "setup": setup, "alerts": alerts,
            "downloads": {"latest": downloads["latest"], "count": len(downloads["items"])}})

    async def devices(self, request: Request) -> Response:
        if await self.owner(request) is None:
            return self.unauthorized()
        if request.method == "GET":
            return self._json({"devices": self.facade.devices.console_list()})
        if request.method == "DELETE":
            try:
                agent_id = self.facade.devices.delete(request.path_params["device_id"])
            except RoutingError as exc:
                message = str(exc)
                code, _, text = message.partition(": ") if message.startswith(("device_online", "not_revoked")) else ("unknown_device", "", message)
                return self._json({"error": text, "code": code}, 404 if code == "unknown_device" else 409)
            return self._json({"deleted": agent_id})
        body = await self._body(request)
        if body is None or "alias" not in body:
            return self._json({"error": "body must be an object with an alias (string or null)"}, 400)
        try:
            device = self.facade.devices.set_alias(request.path_params["device_id"], body["alias"])
        except RoutingError as exc:
            status = 404 if str(exc).startswith("unknown device") else 400
            return self._json({"error": str(exc)}, status)
        return self._json({"device": self.facade.devices.console_describe(device["device_id"])})

    async def revoke(self, request: Request) -> Response:
        if await self.owner(request) is None:
            return self.unauthorized()
        agent_id = self.facade.devices.resolve(request.path_params["device_id"])
        try:
            result = await self.enrollment.revoke_device(agent_id)
        except EnrollError as exc:
            return self._json(exc.body(), exc.status)
        return self._json({"device": self.facade.devices.console_describe(agent_id), "closed": result["closed"]})

    async def invites(self, request: Request) -> Response:
        if await self.owner(request) is None:
            return self.unauthorized()
        try:
            if request.method == "GET":
                state = request.query_params.get("state", "unused")
                return self._json({"invites": self.enrollment.list_invites(state)})
            if request.method == "DELETE":
                return self._json({"invite": self.enrollment.revoke_invite(request.path_params["invite_id"])})
            body = await self._body(request)
            if body is None:
                return self._json({"error": "body must be a JSON object"}, 400)
            created = self.enrollment.create_invite(label=body.get("label"), ttl_s=body.get("ttl_s"),
                                                    agent_id=body.get("agent_id"),
                                                    hub_url=None if self.public_url else self.origin(request))
            return self._json(created, 201)
        except EnrollError as exc:
            return self._json(exc.body(), exc.status)

    async def settings(self, request: Request) -> Response:
        if await self.owner(request) is None:
            return self.unauthorized()
        mailbox = self.facade.mailbox_config
        return self._json({
            "gateway": self._gateway_info(request),
            "auth": {"mode": "oauth" if self.provider is not None else "bearer"},
            "mailbox": {"enabled": mailbox["enabled"], "retention": {"read_days": mailbox["retention"].get("read_days")},
                        "escalation": mailbox.get("escalation")},
            "invites": {"default_ttl_s": self.enrollment.default_ttl_s, "max_ttl_s": self.enrollment.max_ttl_s},
            "clients": self._clients(),
            "downloads": {"dir": str(self.downloads.dir), "release_url": self.downloads.release_url}})

    # -- public ------------------------------------------------------------------------
    async def downloads_api(self, request: Request) -> Response:
        data = await run_in_threadpool(self.downloads.items)
        return JSONResponse(data, headers={"Cache-Control": "no-cache"})

    async def download_file(self, request: Request) -> Response:
        name = request.path_params["filename"]
        path = self.downloads.file(name)
        if path is None:
            from .downloads import ARTIFACT_SUFFIXES
            if self.downloads.release_url and name.lower().endswith(ARTIFACT_SUFFIXES) and ".." not in name:
                return RedirectResponse(f"{self.downloads.release_url}/{quote(name)}", status_code=302)
            return HTMLResponse("Not found", status_code=404)
        return FileResponse(path, media_type="application/octet-stream", filename=name,
                            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    #: ``/downloads/latest/<asset>``: a link that always names the newest installer,
    #: for copy-paste install commands (download page, invite dialog).
    LATEST_ASSETS = {"amd64.deb": "_amd64.deb", "x64-setup.exe": "_x64-setup.exe"}

    async def download_latest(self, request: Request) -> Response:
        suffix = self.LATEST_ASSETS.get(request.path_params["asset"])
        if suffix is None:
            return HTMLResponse("Not found", status_code=404)
        items = (await run_in_threadpool(self.downloads.items))["items"]
        for item in items:  # newest first
            if item["filename"].endswith(suffix) and item["filename"].startswith("agent-nexus_"):
                return RedirectResponse(item["url"], status_code=302, headers={"Cache-Control": "no-cache"})
        return HTMLResponse("Not found", status_code=404)

    async def enroll(self, request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            body = None
        try:
            result = self.enrollment.enroll(body)
        except EnrollError as exc:
            return self._json(exc.body(), exc.status)
        if not result.get("hub"):
            result["hub"] = self.origin(request)
        return self._json(result, 201)

    def routes(self) -> list[Route]:
        return [
            Route("/", self.index, methods=["GET", "HEAD"]),
            Route("/auth/login", self.login, methods=["GET", "POST"]),
            Route("/auth/logout", self.logout, methods=["POST"]),
            Route("/api/auth/token", self.token, methods=["POST"]),
            Route("/console/{name}", self.asset, methods=["GET", "HEAD"]),
            Route("/api/console/summary", self.summary, methods=["GET"]),
            Route("/api/devices", self.devices, methods=["GET"]),
            Route("/api/devices/{device_id}", self.devices, methods=["PUT", "DELETE"]),
            Route("/api/devices/{device_id}/revoke", self.revoke, methods=["POST"]),
            Route("/api/invites", self.invites, methods=["GET", "POST"]),
            Route("/api/invites/{invite_id}", self.invites, methods=["DELETE"]),
            Route("/api/settings", self.settings, methods=["GET"]),
            Route("/api/downloads", self.downloads_api, methods=["GET"]),
            Route("/downloads", self.downloads_page, methods=["GET", "HEAD"]),
            Route("/downloads/latest/{asset}", self.download_latest, methods=["GET", "HEAD"]),
            Route("/downloads/{filename}", self.download_file, methods=["GET", "HEAD"]),
            Route("/enroll", self.enroll, methods=["POST"]),
        ]


#: The Agent Nexus mark (same drawing as ``logoSVG()`` in ui.js), for pages without scripts.
LOGO_SVG = ('<svg class="brand-mark" width="44" height="44" viewBox="0 0 32 32" aria-hidden="true" focusable="false">'
            '<rect width="32" height="32" rx="9" fill="var(--accent)"/>'
            '<g stroke="var(--text-on-accent)" stroke-width="2.2" stroke-linecap="round" fill="none">'
            '<path d="M16 16 L16 8.5 M16 16 L9.5 20.5 M16 16 L22.5 20.5" opacity=".75"/></g>'
            '<g fill="var(--text-on-accent)"><circle cx="16" cy="16" r="3.6"/><circle cx="16" cy="7.6" r="2.4"/>'
            '<circle cx="8.7" cy="21.2" r="2.4"/><circle cx="23.3" cy="21.2" r="2.4"/></g></svg>')
