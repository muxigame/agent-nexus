"""The gateway's web inbox: ``/api/inbox/*`` and the mobile PWA at ``/inbox/`` (decision D11, §8.12).

Owner authentication is the gateway's existing one. API callers send the same
bearer credential as the MCP endpoint. The PWA signs in through the gateway's
own OAuth authorization server (the one ChatGPT and Claude connectors use,
with the owner password): an authorization-code + PKCE flow run by the
gateway for a built-in public client whose only redirect is
``<public_url>/inbox/callback``. The access token it receives is kept in an
``HttpOnly; Secure; SameSite=Strict`` cookie. A gateway configured with
static bearer tokens only (no OAuth) shows a token form instead. No new
account system, no Web Push (0.2), no WeCom or e-mail bridge.

Cookie-authenticated writes additionally require ``X-ANX: 1`` (a custom
header a cross-site form cannot send); ``SameSite=Strict`` already keeps the
cookie off cross-site requests.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .federation import RoutingError

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
COOKIE = "__Host-anx_inbox"
CLIENT_ID = "anx-inbox"
LOGIN_TTL_S = 600
MAX_PENDING_LOGINS = 64
NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
PAGE_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def oauth_provider(auth: Any):
    """The gateway's :class:`mcpkit.oauth.PasswordOAuth`, if it has one."""
    try:
        from mcpkit.oauth import PasswordOAuth
    except Exception:  # pragma: no cover - mcpkit always ships it
        return None
    if isinstance(auth, PasswordOAuth):
        return auth
    inner = getattr(auth, "server", None)
    return inner if isinstance(inner, PasswordOAuth) else None


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class InboxWeb:
    def __init__(self, facade, server, config: dict):
        self.facade = facade
        self.server = server
        self.public_url = (config.get("public_url") or "").rstrip("/")
        self.provider = oauth_provider(server.auth)
        self.pending: dict[str, tuple[str, float]] = {}
        self.enabled = bool(facade.mailbox_config["enabled"])
        self.pwa = self.enabled and bool(facade.mailbox_config["inbox"]["pwa"])
        if self.provider is not None and self.public_url:
            # The PWA's callback is the only redirect the built-in client may use.
            pattern = re.escape(self.callback_url)
            if pattern not in self.provider.config.redirect_uris:
                self.provider.config.redirect_uris.append(pattern)

    @property
    def callback_url(self) -> str:
        return self.public_url + "/inbox/callback"

    # -- authentication ----------------------------------------------------------
    async def _verify(self, token: str) -> bool:
        if not token or self.server.auth is None:
            return False
        try:
            verified = await self.server.auth.verify_token(token)
        except Exception:
            return False
        required = set(getattr(self.server.auth, "required_scopes", None) or [])
        return verified is not None and required <= set(getattr(verified, "scopes", None) or [])

    async def owner(self, request: Request) -> str | None:
        """``"bearer"`` or ``"cookie"`` for the authenticated owner, else ``None``."""
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer "):
            return "bearer" if await self._verify(header.removeprefix("Bearer ")) else None
        cookie = request.cookies.get(COOKIE, "")
        if cookie and await self._verify(cookie):
            if request.method not in ("GET", "HEAD") and request.headers.get("x-anx") != "1":
                return None
            return "cookie"
        return None

    def _set_session(self, response: Response, token: str, max_age: int | None) -> Response:
        response.set_cookie(COOKIE, token, max_age=max_age or 30 * 86400, path="/", secure=True,
                            httponly=True, samesite="strict")
        return response

    # -- the PWA shell -------------------------------------------------------------
    async def page(self, request: Request) -> Response:
        if not self.pwa:
            return HTMLResponse("Not found", status_code=404)
        if await self.owner(request) is None:
            return RedirectResponse("/inbox/login", status_code=302, headers=NO_STORE)
        return FileResponse(STATIC_DIR / "pwa.html", media_type="text/html",
                            headers={**NO_STORE, "Content-Security-Policy": PAGE_CSP})

    async def asset(self, request: Request) -> Response:
        if not self.pwa:
            return HTMLResponse("Not found", status_code=404)
        name = request.path_params["name"]
        files = {"manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
                 "sw.js": ("sw.js", "text/javascript"), "inbox.js": ("inbox.js", "text/javascript"),
                 "icon.png": ("icon.png", "image/png")}
        if name not in files:
            return HTMLResponse("Not found", status_code=404)
        filename, media_type = files[name]
        headers = {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"}
        if name == "sw.js":
            headers["Service-Worker-Allowed"] = "/inbox/"
        return FileResponse(STATIC_DIR / filename, media_type=media_type, headers=headers)

    async def login(self, request: Request) -> Response:
        if not self.pwa:
            return HTMLResponse("Not found", status_code=404)
        if self.provider is None or not self.public_url:
            return await self._token_login(request)
        if request.method != "GET":
            return HTMLResponse("Method not allowed", status_code=405)
        await self._ensure_client()
        now = time.time()
        self.pending = {key: value for key, value in self.pending.items() if value[1] > now}
        if len(self.pending) >= MAX_PENDING_LOGINS:
            return HTMLResponse("Too many pending sign-ins; try again in a few minutes.", status_code=429,
                                headers=NO_STORE)
        verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(24)
        self.pending[state] = (verifier, now + LOGIN_TTL_S)
        query = urlencode({"response_type": "code", "client_id": CLIENT_ID, "redirect_uri": self.callback_url,
                           "scope": " ".join(self.provider.config.scopes), "state": state,
                           "code_challenge": _challenge(verifier), "code_challenge_method": "S256",
                           "resource": self.provider.resource})
        return RedirectResponse(f"/authorize?{query}", status_code=302, headers=NO_STORE)

    async def _ensure_client(self) -> None:
        if await self.provider.get_client(CLIENT_ID) is not None:
            return
        from mcp.shared.auth import OAuthClientInformationFull
        await self.provider.register_client(OAuthClientInformationFull(
            client_id=CLIENT_ID, client_name="agent-nexus inbox", redirect_uris=[self.callback_url],
            token_endpoint_auth_method="none", grant_types=["authorization_code", "refresh_token"],
            response_types=["code"], scope=" ".join(self.provider.config.scopes)))

    async def callback(self, request: Request) -> Response:
        if not self.pwa or self.provider is None:
            return HTMLResponse("Not found", status_code=404)
        state = request.query_params.get("state", "")
        entry = self.pending.pop(state, None)
        if entry is None or entry[1] < time.time():
            return self._failed("登录会话已过期或无效，请重新打开收件箱。")
        if request.query_params.get("error"):
            return self._failed("登录被拒绝。")
        verifier = entry[0]
        client = await self.provider.get_client(CLIENT_ID)
        code = await self.provider.load_authorization_code(client, request.query_params.get("code", "")) if client else None
        if code is None or not hmac.compare_digest(code.code_challenge or "", _challenge(verifier)) \
                or str(code.redirect_uri) != self.callback_url:
            return self._failed("登录未完成，请重新打开收件箱。")
        try:
            token = await self.provider.exchange_authorization_code(client, code)
        except Exception:
            return self._failed("登录未完成，请重新打开收件箱。")
        response = RedirectResponse("/inbox/", status_code=303, headers=NO_STORE)
        return self._set_session(response, token.access_token, token.expires_in)

    @staticmethod
    def _failed(message: str) -> HTMLResponse:
        body = (f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<title>收件箱</title><p>{html.escape(message)}</p><p><a href="/inbox/login">重新登录</a></p>')
        return HTMLResponse(body, status_code=400, headers=NO_STORE)

    async def _token_login(self, request: Request) -> Response:
        """Bearer-only gateways: paste the gateway token once; it is kept in the session cookie."""
        error = ""
        if request.method == "POST":
            if request.headers.get("sec-fetch-site") == "cross-site":
                return HTMLResponse("Forbidden", status_code=403)
            form = await request.form()
            token = str(form.get("token", ""))
            if await self._verify(token):
                return self._set_session(RedirectResponse("/inbox/", status_code=303, headers=NO_STORE), token, None)
            error = "令牌不正确。"
        body = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1"><title>登录收件箱</title>'
                '<style>body{font:16px/1.6 system-ui;max-width:420px;margin:10vh auto;padding:16px}'
                'input,button{box-sizing:border-box;width:100%;padding:12px;margin-top:8px}</style>'
                '<form method="post" action="/inbox/login"><h1>agent-nexus 收件箱</h1>'
                f'<p style="color:#a51818">{html.escape(error)}</p>'
                '<label>网关令牌<input name="token" type="password" autocomplete="current-password"></label>'
                '<button type="submit">登录</button></form></html>')
        return HTMLResponse(body, status_code=401 if error else 200,
                            headers={**NO_STORE, "Content-Security-Policy": PAGE_CSP})

    async def logout(self, request: Request) -> Response:
        if request.headers.get("x-anx") != "1":
            return JSONResponse({"error": "missing X-ANX header"}, status_code=403)
        response = JSONResponse({"ok": True}, headers=NO_STORE)
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response

    # -- the API ---------------------------------------------------------------------
    async def api(self, request: Request) -> Response:
        if not self.enabled:
            return JSONResponse({"error": "the mailbox is disabled"}, status_code=404)
        how = await self.owner(request)
        if how is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        params = dict(request.path_params)
        query = request.query_params
        body: dict = {}
        if request.method in ("POST", "PUT"):
            try:
                body = await request.json()
            except ValueError:
                return JSONResponse({"error": "body must be JSON"}, status_code=400)
            if not isinstance(body, dict):
                return JSONResponse({"error": "body must be an object"}, status_code=400)
        route = request.scope.get("route_name") or ""
        args: dict[str, Any] = {}
        if "viewer" in query:
            args["viewer"] = query["viewer"]
        if route == "summary":
            op = "summary"
        elif route == "threads":
            op = "threads"
        elif route == "conversation":
            op = "conversation"
            args.update(address=query.get("address", ""), limit=query.get("limit") or 200,
                        mark_read=query.get("mark_read") in ("1", "true"))
        elif route == "thread":
            op, args["thread_id"] = "thread", params["thread_id"]
            args["mark_read"] = query.get("mark_read", "1") not in ("0", "false")
        elif route == "send":
            op, args = "send", {**body}
        elif route == "asks":
            op, args["state"] = "asks", query.get("state", "open")
        elif route == "ask":
            op, args["ask_id"] = "ask", params["ask_id"]
        elif route == "answer":
            op, args = "answer", {**body, "ask_id": params["ask_id"]}
        elif route == "addresses":
            op, args["include_ended"] = "addresses", query.get("include_ended") in ("1", "true")
        elif route == "address":
            op, args = "set_address", {"address": params["address"], "fields": body}
        else:
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            result = await self.facade.inbox.call(op, args, via="pwa" if how == "cookie" else "inbox")
        except RoutingError as exc:
            status = 404 if str(exc).startswith("unknown ") else 400
            return JSONResponse({"error": str(exc)}, status_code=status, headers=NO_STORE)
        return JSONResponse(result, headers=NO_STORE)

    def routes(self) -> list[Route]:
        def api(path: str, name: str, methods: list[str]) -> Route:
            async def endpoint(request: Request, name=name):
                request.scope["route_name"] = name
                return await self.api(request)
            return Route(path, endpoint, methods=methods)

        return [
            Route("/inbox", lambda request: RedirectResponse("/inbox/", status_code=308)),
            Route("/inbox/", self.page),
            Route("/inbox/login", self.login, methods=["GET", "POST"]),
            Route("/inbox/callback", self.callback),
            Route("/inbox/logout", self.logout, methods=["POST"]),
            Route("/inbox/{name}", self.asset),
            api("/api/inbox/summary", "summary", ["GET"]),
            api("/api/inbox/threads", "threads", ["GET"]),
            api("/api/inbox/threads/{thread_id}", "thread", ["GET"]),
            api("/api/inbox/conversation", "conversation", ["GET"]),
            api("/api/inbox/send", "send", ["POST"]),
            api("/api/inbox/asks", "asks", ["GET"]),
            api("/api/inbox/asks/{ask_id}", "ask", ["GET"]),
            api("/api/inbox/asks/{ask_id}/answer", "answer", ["POST"]),
            api("/api/inbox/addresses", "addresses", ["GET"]),
            api("/api/inbox/addresses/{address}", "address", ["PUT"]),
            api("/api/addresses/{address}", "address", ["PUT"]),
        ]
