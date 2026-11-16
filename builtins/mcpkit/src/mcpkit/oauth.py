"""Single-owner OAuth authorization server for remote MCP clients (ChatGPT).

The SDK owns protocol parsing, client authentication, PKCE verification and the
token endpoints. This provider adds what a personal server needs on top: an
owner password on a consent page, limited scopes, redirect URIs pinned to known
clients, and durable state - so a restart never forces a reconnect.

Carried over from payment-mcp, where it was hardened against real ChatGPT and
browser behaviour; its SQLite layout is unchanged so existing grants survive:

* tokens are stored as SHA-256 digests, never raw;
* the store is bound to one resource URL and refuses to open for another;
* PKCE S256 is mandatory and every token is audience-bound to the resource;
* the login form is tied to its flow by a CSRF value and a ``__Host-`` cookie,
  rate limited, and served with a strict CSP;
* :class:`OAuthRouteGuard` caps request bodies, normalises HTTP Basic client
  credentials the SDK would otherwise reject, and adds ``iss`` to redirects.

It is not a multi-user identity service.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from fastmcp.server.auth import OAuthProvider
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
    RefreshToken, RegistrationError, TokenError,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .config import AuthConfig, ConfigError

FLOW_TTL = 600
CODE_TTL = 120
MAX_CLIENTS = 256
MAX_FLOWS = 256
# Inherit Uvicorn's handler and level; a standalone logger at INFO was silently
# discarded, leaving real browser failures undiagnosable.
LOG = logging.getLogger("uvicorn.error.mcpkit_oauth")
HEADERS = {
    "Cache-Control": "no-store", "Pragma": "no-cache",
    # no-referrer makes native browser form POSTs send Origin: null. Keep the
    # real same-origin signal without revealing paths or query parameters.
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "strict-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def redirect_allowed(uri: str, patterns: list[str]) -> bool:
    """Structurally clean HTTPS URI whose ``scheme://host/path`` fully matches a pattern."""
    try:
        p = urlsplit(uri)
        if (p.scheme != "https" or not p.hostname or p.port not in (None, 443)
                or p.username or p.password or p.query or p.fragment
                or "\\" in uri or any(c.isspace() for c in uri)):
            return False
        bare = f"https://{p.hostname}{p.path}"
        return any(re.fullmatch(pattern, bare) for pattern in patterns)
    except ValueError:
        return False


# The consent and recovery pages carry their own styles: the CSP allows inline
# styles only, no scripts and no other resources. Light and dark follow the OS.
PAGE_STYLE = """
:root{color-scheme:light dark;--bg:#f6f7f9;--surface:#fff;--surface-2:#f3f4f7;--border:#e3e6eb;--border-strong:#cdd2da;
--text:#14181f;--text-2:#4f5866;--text-3:#616a7a;--accent:#0b7285;--accent-hover:#095f6f;--on-accent:#fff;
--accent-subtle:#e4f3f5;--danger:#c92a2a;--danger-subtle:#fdecec;--ring:rgba(11,114,133,.35)}
@media (prefers-color-scheme:dark){:root{--bg:#0d1014;--surface:#14181d;--surface-2:#1a1f26;--border:#262c35;
--border-strong:#353d48;--text:#e7eaee;--text-2:#a7b0bd;--text-3:#8d97a5;--accent:#3bc9db;--accent-hover:#66d9e8;
--on-accent:#04252b;--accent-subtle:rgba(59,201,219,.13);--danger:#ff8787;--danger-subtle:rgba(255,135,135,.12);
--ring:rgba(59,201,219,.4)}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;
padding:32px 16px;background:var(--bg);color:var(--text);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",sans-serif}
.card{width:100%;max-width:440px;padding:32px;background:var(--surface);border:1px solid var(--border);border-radius:16px;
box-shadow:0 12px 32px rgba(15,23,42,.08)}
.mark{display:flex;align-items:center;justify-content:center;width:48px;height:48px;margin:0 auto 16px;border-radius:12px;
background:var(--accent-subtle);color:var(--accent)}
h1{margin:0 0 8px;font-size:20px;line-height:1.4;text-align:center;font-weight:600}
.lead{margin:0 0 24px;color:var(--text-2);font-size:14px;text-align:center}
.facts{margin:0 0 24px;padding:4px 16px;border:1px solid var(--border);border-radius:10px;background:var(--surface-2)}
.facts div{display:flex;justify-content:space-between;gap:16px;padding:10px 0;font-size:14px}
.facts div+div{border-top:1px solid var(--border)}
.facts dt{color:var(--text-3)}.facts dd{margin:0;font-weight:600;text-align:right;word-break:break-all}
code,.mono{font-family:ui-monospace,"Cascadia Mono","SF Mono",Consolas,monospace;font-size:13px}
label{display:block;margin-bottom:6px;font-size:14px;font-weight:600}
input[type=password]{display:block;width:100%;height:44px;padding:0 12px;border:1px solid var(--border-strong);
border-radius:8px;background:var(--surface);color:var(--text);font:inherit}
input[type=password]:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--ring)}
.error{margin:0 0 16px;padding:10px 12px;border-radius:8px;background:var(--danger-subtle);color:var(--danger);font-size:14px}
.actions{display:flex;flex-direction:row-reverse;gap:8px;margin-top:20px}
button{height:44px;padding:0 20px;border-radius:8px;border:1px solid var(--border-strong);background:var(--surface);
color:var(--text);font:inherit;font-weight:600;cursor:pointer}
button:hover{background:var(--surface-2)}
button:focus-visible{outline:none;box-shadow:0 0 0 3px var(--ring)}
button.primary{flex:2;border-color:var(--accent);background:var(--accent);color:var(--on-accent)}
button.primary:hover{background:var(--accent-hover)}
button.secondary{flex:1}
.foot{color:var(--text-3);font-size:13px;text-align:center}
.note{margin:12px 0 0;color:var(--text-2);font-size:14px}
@media (max-width:480px){.card{padding:24px 20px;border-radius:12px}}
"""

# Where an authorization returns, as the owner knows it. The host is checked
# against the configured redirect URIs; the client's self-chosen name is not.
KNOWN_CLIENTS = {"chatgpt.com": "ChatGPT", "claude.ai": "Claude", "claude.com": "Claude"}

LINK_ICON = ('<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
             'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 17H7A5 5 0 0 1 7 7h2"/>'
             '<path d="M15 7h2a5 5 0 1 1 0 10h-2"/><line x1="8" x2="16" y1="12" y2="12"/></svg>')
ALERT_ICON = ('<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
              'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/>'
              '<line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/></svg>')


class State:
    """SQLite transactions; OAuth access/refresh token values are NOT stored raw."""

    def __init__(self, path: Path, resource: str):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS entries (kind TEXT, key TEXT, body TEXT NOT NULL, expires INTEGER, PRIMARY KEY(kind,key))")
            old = db.execute("SELECT body FROM entries WHERE kind='meta' AND key='resource'").fetchone()
            if old and json.loads(old[0]) != resource:
                raise ConfigError(f"OAuth store {path} belongs to another resource; use a separate store when changing the public endpoint.")
            db.execute("INSERT OR IGNORE INTO entries VALUES ('meta','resource',?,NULL)", (json.dumps(resource),))

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            with db:
                yield db

    def get(self, kind: str, key: str) -> dict | None:
        with self.transaction() as db:
            row = db.execute("SELECT body,expires FROM entries WHERE kind=? AND key=?", (kind, key)).fetchone()
        if row is None or (row[1] is not None and row[1] <= time.time()):
            return None
        return json.loads(row[0])


@dataclass
class Flow:
    client_id: str
    params: AuthorizationParams
    expires: float
    csrf: str
    cookie_hash: str = ""


class PasswordOAuth(OAuthProvider):
    def __init__(self, *, name: str, origin: str, mcp_path: str, password: str,
                 store: Path, auth: AuthConfig):
        origin = origin.rstrip("/")
        p = urlsplit(origin)
        if not p.scheme or not p.hostname or p.path or p.query or p.fragment or p.username or p.password:
            raise ConfigError("OAuth requires a public origin without a path or credentials.")
        if len(password) < 32:
            raise ConfigError("The OAuth owner password must contain at least 32 characters.")
        self.origin = origin
        self.resource = origin + mcp_path
        self.config = auth
        self.title = auth.title or name
        self.cookie = "__Host-" + re.sub(r"[^A-Za-z0-9_]", "_", name) + "_oauth_flow"
        self.password_hash = digest(password)
        self.state = State(store, self.resource)
        self.flows: dict[str, Flow] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.attempts: dict[str, list[float]] = {}
        super().__init__(
            base_url=origin, issuer_url=origin, required_scopes=list(auth.required_scopes),
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=list(auth.scopes), default_scopes=list(auth.scopes),
            ),
            revocation_options=RevocationOptions(enabled=True),
        )

    # -- policy helpers -----------------------------------------------------------------

    def redirect_ok(self, uri: str) -> bool:
        return redirect_allowed(uri, self.config.redirect_uris)

    def scopes_ok(self, scopes: list[str], ceiling: list[str] | None = None) -> bool:
        allowed = set(ceiling if ceiling is not None else self.config.scopes)
        return set(self.config.required_scopes) <= set(scopes) and set(scopes) <= allowed

    # -- clients ---------------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        item = self.state.get("client", client_id)
        return OAuthClientInformationFull.model_validate(item) if item else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = client_info.redirect_uris or []
        if not uris or len(uris) > 8 or not all(self.redirect_ok(str(u)) for u in uris):
            raise RegistrationError(error="invalid_redirect_uri", error_description="This server only accepts its configured OAuth callbacks.")
        if set((client_info.scope or "").split()) - set(self.config.scopes):
            raise RegistrationError(error="invalid_client_metadata", error_description="Unsupported scopes.")
        # Registered clients outlive individual grants; don't expire their secrets.
        if client_info.client_secret:
            client_info.client_secret_expires_at = 0
        with self.state.transaction() as db:
            count = db.execute("SELECT COUNT(*) FROM entries WHERE kind='client'").fetchone()[0]
            if count >= MAX_CLIENTS:
                raise RegistrationError(error="invalid_client_metadata", error_description="Client registration limit reached; contact the owner.")
            db.execute("INSERT INTO entries VALUES ('client',?,?,NULL)", (
                client_info.client_id, client_info.model_dump_json(),
            ))

    # -- authorization -------------------------------------------------------------------

    def prune(self) -> None:
        now = time.time()
        self.flows = {k: v for k, v in self.flows.items() if v.expires > now}
        self.codes = {k: v for k, v in self.codes.items() if v.expires_at > now}
        self.attempts = {k: [t for t in values if t > now - 300]
                         for k, values in self.attempts.items() if any(t > now - 300 for t in values)}

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self.prune()
        if len(self.flows) >= MAX_FLOWS:
            raise AuthorizeError(error="temporarily_unavailable", error_description="Too many pending authorizations.")
        if not self.redirect_ok(str(params.redirect_uri)):
            raise AuthorizeError(error="invalid_request", error_description="Unsupported redirect URI.")
        if params.resource and str(params.resource) != self.resource:
            raise AuthorizeError(error="invalid_target", error_description="Token resource must be this MCP endpoint.")
        scopes = params.scopes or list(self.config.scopes)
        if not self.scopes_ok(scopes):
            raise AuthorizeError(error="invalid_scope", error_description=f"Scopes must include {' '.join(self.config.required_scopes)} and stay within {' '.join(self.config.scopes)}.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge or ""):
            raise AuthorizeError(error="invalid_request", error_description="PKCE S256 is required.")
        # Canonical audience is present even for a client omitting resource.
        params = params.model_copy(update={"resource": self.resource, "scopes": scopes})
        key = secrets.token_urlsafe(32)
        self.flows[key] = Flow(client.client_id, params, time.time() + FLOW_TTL, secrets.token_urlsafe(32))
        return self.origin + "/login?" + urlencode({"flow": key})

    def callback(self, params: AuthorizationParams, **values: str) -> str:
        query = dict(parse_qsl(urlsplit(str(params.redirect_uri)).query))
        query.update(values)
        query["iss"] = self.origin
        if params.state is not None:
            query["state"] = params.state
        p = urlsplit(str(params.redirect_uri))
        return urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), ""))

    def complete(self, flow: Flow) -> str:
        code = secrets.token_urlsafe(32)
        p = flow.params
        self.codes[digest(code)] = AuthorizationCode(
            code=code, client_id=flow.client_id, scopes=p.scopes,
            expires_at=time.time() + CODE_TTL, code_challenge=p.code_challenge,
            redirect_uri=p.redirect_uri,
            redirect_uri_provided_explicitly=p.redirect_uri_provided_explicitly,
            resource=self.resource, subject=self.config.subject,
        )
        return self.callback(p, code=code)

    # -- login page ---------------------------------------------------------------------------

    def page(self, key: str, flow: Flow, *, error: str = "", status: int = 200) -> HTMLResponse:
        # Every user-controlled value is escaped.
        callback_host = html.escape(urlsplit(str(flow.params.redirect_uri)).hostname or "")
        title = html.escape(self.title)
        consent = f'<p class="lead">{html.escape(self.config.consent)}</p>' if self.config.consent else ""
        scopes = html.escape(" / ".join(flow.params.scopes or []))
        raw_host = urlsplit(str(flow.params.redirect_uri)).hostname or ""
        client = html.escape(KNOWN_CLIENTS.get(raw_host.lower(), raw_host))
        error_html = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
        body = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} 授权</title>
<style>{PAGE_STYLE}</style>
<form class="card" method="post" action="/login">
<div class="mark">{LINK_ICON}</div>
<h1>授权 {client} 访问 {title}</h1>
<p class="lead">仅在你刚刚从客户端发起连接时授权。</p>{consent}
<dl class="facts"><div><dt>授权返回</dt><dd class="mono">{callback_host}</dd></div><div><dt>权限</dt><dd class="mono">{scopes}</dd></div></dl>
{error_html}
<input type="hidden" name="flow" value="{html.escape(key, quote=True)}">
<input type="hidden" name="csrf" value="{html.escape(flow.csrf, quote=True)}">
<label for="password">{title} 授权密码</label>
<input id="password" name="password" type="password" autocomplete="current-password" maxlength="4096" autofocus>
<div class="actions"><button class="primary" name="decision" value="approve" type="submit">授权</button>
<button class="secondary" name="decision" value="deny" type="submit">拒绝</button></div></form>
<p class="foot">{title}</p></html>'''
        headers = dict(HEADERS)
        callback = str(flow.params.redirect_uri)
        if self.redirect_ok(callback):
            # Chromium can apply form-action to the 303 redirect as well as the
            # initial /login POST. Allow ONLY this already-validated callback.
            headers["Content-Security-Policy"] = HEADERS["Content-Security-Policy"].replace(
                "form-action 'self'", "form-action 'self' " + callback,
            )
        return HTMLResponse(body, status_code=status, headers=headers)

    def reject_login(self, request: Request, reason: str, *, status: int = 403) -> HTMLResponse:
        """Human-readable recovery, with categorical diagnostics and no secrets."""
        descriptions = {
            "origin_mismatch": "授权页面的来源校验未通过。请关闭旧页面，从客户端重新点击连接。",
            "cross_site_post": "授权表单必须由本站页面提交。请从客户端重新打开授权页面。",
            "flow_expired": "授权会话已过期、已使用或服务已重启。请关闭旧页面，从客户端重新点击连接。",
            "cookie_missing": "浏览器没有带回授权 Cookie。请允许本站 Cookie，并从客户端重新打开授权页面。",
            "cookie_mismatch": "授权页面与浏览器会话不匹配。请关闭旧的授权标签页，从客户端重新连接。",
            "csrf_mismatch": "授权表单校验未通过。请关闭旧页面，从客户端重新连接。",
        }
        origin = request.headers.get("origin")
        origin_class = "missing" if origin is None else "same_origin" if origin == self.origin else "null" if origin == "null" else "other"
        fetch_site = request.headers.get("sec-fetch-site", "missing")
        if fetch_site not in {"missing", "same-origin", "same-site", "cross-site", "none"}:
            fetch_site = "other"
        # Never log request URLs, flow/CSRF/cookie values, credentials or bodies.
        LOG.warning("OAuth login rejected reason=%s origin=%s fetch_site=%s", reason, origin_class, fetch_site)
        body = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(self.title)} 授权未完成</title>
<style>{PAGE_STYLE}</style>
<main class="card"><div class="mark">{ALERT_ICON}</div>
<h1>授权未完成</h1><p class="lead">{html.escape(descriptions[reason])}</p>
<p class="note">不需要重置授权密码。请不要在此错误页重复提交。</p>
<p class="note">诊断代码：<code>{html.escape(reason)}</code></p></main>
<p class="foot">{html.escape(self.title)}</p></html>'''
        return HTMLResponse(body, status_code=status, headers=HEADERS)

    async def login(self, request: Request) -> Response:
        self.prune()
        if request.method == "GET":
            key = request.query_params.get("flow", "")
            flow = self.flows.get(key)
            if flow is None:
                return self.reject_login(request, "flow_expired", status=400)
            response = self.page(key, flow)
            binding = secrets.token_urlsafe(32)
            flow.cookie_hash = digest(binding)
            response.set_cookie(self.cookie, binding, max_age=FLOW_TTL, path="/", secure=True, httponly=True, samesite="lax")
            return response
        origin = request.headers.get("origin")
        if origin and origin != self.origin:
            return self.reject_login(request, "origin_mismatch")
        if request.headers.get("sec-fetch-site") == "cross-site":
            return self.reject_login(request, "cross_site_post")
        form = await request.form()
        key = str(form.get("flow", ""))
        flow = self.flows.get(key)
        if flow is None:
            return self.reject_login(request, "flow_expired")
        binding = request.cookies.get(self.cookie, "")
        if not binding:
            return self.reject_login(request, "cookie_missing")
        if not flow.cookie_hash or not hmac.compare_digest(flow.cookie_hash, digest(binding)):
            return self.reject_login(request, "cookie_mismatch")
        if not hmac.compare_digest(digest(flow.csrf), digest(str(form.get("csrf", "")))):
            return self.reject_login(request, "csrf_mismatch")
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        total = sum(len(v) for v in self.attempts.values())
        if len(self.attempts.get(ip, [])) >= 8 or total >= 64:
            return self.page(key, flow, error="尝试过于频繁，请稍后重新授权。", status=429)
        if form.get("decision") == "deny":
            self.flows.pop(key, None)
            response = RedirectResponse(self.callback(flow.params, error="access_denied"), status_code=303, headers=HEADERS)
        else:
            candidate = str(form.get("password", ""))
            if form.get("decision") != "approve" or not hmac.compare_digest(self.password_hash, digest(candidate)):
                self.attempts.setdefault(ip, []).append(now)
                return self.page(key, flow, error="授权密码不正确。", status=401)
            self.flows.pop(key, None)
            response = RedirectResponse(self.complete(flow), status_code=303, headers=HEADERS)
        response.delete_cookie(self.cookie, path="/", secure=True, httponly=True, samesite="lax")
        return response

    # -- tokens ------------------------------------------------------------------------------

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        item = self.codes.get(digest(authorization_code))
        return item if item and item.expires_at > time.time() and item.client_id == client.client_id else None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        item = self.codes.pop(digest(authorization_code.code), None)
        if item is None or item.expires_at <= time.time() or item.client_id != client.client_id:
            raise TokenError(error="invalid_grant", error_description="Authorization code is invalid or already used.")
        return self.issue(client.client_id, item.scopes)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        item = self.state.get("refresh", digest(refresh_token))
        if not item or item["client_id"] != client.client_id or item.get("resource") != self.resource:
            return None
        return RefreshToken.model_validate({**item, "token": refresh_token})

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        chosen = scopes or refresh_token.scopes
        if not self.scopes_ok(chosen, ceiling=refresh_token.scopes) or refresh_token.client_id != client.client_id:
            raise TokenError(error="invalid_scope", error_description="Refresh may not add permissions.")
        return self.issue(client.client_id, chosen, consume_refresh=digest(refresh_token.token))

    def issue(self, client_id: str, scopes: list[str], consume_refresh: str | None = None) -> OAuthToken:
        now = int(time.time())
        access_ttl, refresh_ttl = self.config.access_token_ttl_s, self.config.refresh_token_ttl_s
        # access_token_ttl_s == 0: the token never expires (NULL expiry, no expires_in).
        access_expires = now + access_ttl if access_ttl > 0 else None
        access = secrets.token_urlsafe(32)
        # A server that advertises offline_access uses it to gate refresh
        # tokens; one that does not (codex's grants carry only "tools") would
        # otherwise hand out a grant that can never be renewed, and the client
        # would have to re-authorise the first time its access token expired.
        offline = "offline_access" in self.config.scopes
        refresh = secrets.token_urlsafe(32) if ("offline_access" in scopes or not offline) else None
        common = {"client_id": client_id, "scopes": scopes, "resource": self.resource, "subject": self.config.subject}
        with self.state.transaction() as db:
            if consume_refresh is not None:
                removed = db.execute("DELETE FROM entries WHERE kind='refresh' AND key=? AND expires>?", (consume_refresh, now))
                if removed.rowcount != 1:
                    raise TokenError(error="invalid_grant", error_description="Refresh token is invalid or already used.")
            db.execute("DELETE FROM entries WHERE expires IS NOT NULL AND expires<=?", (now,))
            db.execute("INSERT INTO entries VALUES ('access',?,?,?)", (
                digest(access), json.dumps({**common, "expires_at": access_expires}), access_expires,
            ))
            if refresh:
                db.execute("INSERT INTO entries VALUES ('refresh',?,?,?)", (
                    digest(refresh), json.dumps({**common, "expires_at": now + refresh_ttl}), now + refresh_ttl,
                ))
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=access_ttl if access_ttl > 0 else None,
                          refresh_token=refresh, scope=" ".join(scopes))

    async def load_access_token(self, token: str) -> AccessToken | None:
        item = self.state.get("access", digest(token))
        if not item or item.get("resource") != self.resource or not set(self.config.required_scopes) <= set(item.get("scopes", [])):
            return None
        return AccessToken.model_validate({**item, "token": token})

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        kind = "refresh" if isinstance(token, RefreshToken) else "access"
        with self.state.transaction() as db:
            db.execute("DELETE FROM entries WHERE kind=? AND key=?", (kind, digest(token.token)))

    # -- discovery and routes ---------------------------------------------------------------

    def metadata(self) -> dict[str, Any]:
        return {
            "issuer": self.origin,
            "authorization_endpoint": self.origin + "/authorize",
            "token_endpoint": self.origin + "/token",
            "registration_endpoint": self.origin + "/register",
            "revocation_endpoint": self.origin + "/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "scopes_supported": list(self.config.scopes), "code_challenge_methods_supported": ["S256"],
            "authorization_response_iss_parameter_supported": True,
            "client_id_metadata_document_supported": False,
        }

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = super().get_routes(mcp_path)
        routes = [r for r in routes if not getattr(r, "path", "").startswith("/.well-known/")]

        async def metadata(request: Request) -> Response:
            headers = {**HEADERS, "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, OPTIONS"}
            if request.method == "OPTIONS":
                return Response(status_code=204, headers=headers)
            if "oauth-protected-resource" in request.url.path:
                body = {"resource": self.resource, "authorization_servers": [self.origin],
                        "scopes_supported": list(self.config.scopes), "resource_name": self.title}
            else:
                body = self.metadata()
            return JSONResponse(body, headers=headers)

        path = mcp_path or "/mcp"
        for doc in ("oauth-protected-resource", "oauth-authorization-server", "openid-configuration"):
            for route in (f"/.well-known/{doc}", f"/.well-known/{doc}{path}", f"{path}/.well-known/{doc}"):
                routes.append(Route(route, metadata, methods=["GET", "OPTIONS"]))
        routes.append(Route("/login", self.login, methods=["GET", "POST"]))
        # Keep SDK protocol/security handlers intact; add size/audience guards,
        # issuer identification on error redirects, and query-free diagnostics.
        return [Route(r.path, OAuthRouteGuard(r.app, self), methods=r.methods, name=r.name) for r in routes]


class OAuthRouteGuard:
    def __init__(self, app, provider: PasswordOAuth):
        self.app, self.provider = app, provider

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        body = b""
        if scope["method"] == "POST":
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body += message.get("body", b"")
                if len(body) > 32768:
                    return await Response(status_code=413, headers=HEADERS)(scope, receive, send)
                if not message.get("more_body", False):
                    break
        params = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))
        if scope["method"] == "POST" and path in ("/authorize", "/token"):
            params = parse_qs(body.decode("utf-8", "replace"))
        # SDK 2.2's Basic authenticator still expects client_id in the form.
        # RFC 6749 clients correctly put it only in Authorization: Basic.
        # Normalise the identifier (not the secret), then leave authentication
        # to the SDK; reject mismatched/duplicate credentials before delegation.
        if scope["method"] == "POST" and path in ("/token", "/revoke"):
            authorization = dict(scope.get("headers", [])).get(b"authorization", b"").decode("latin1")
            if authorization.lower().startswith("basic "):
                try:
                    decoded = base64.b64decode(authorization.split(" ", 1)[1], validate=True).decode("utf-8")
                    raw_id, raw_secret = decoded.split(":", 1)
                    client_id = unquote(raw_id)
                    form = parse_qs(body.decode("utf-8", "strict"), keep_blank_values=True)
                    if "client_secret" in form or ("client_id" in form and form["client_id"] != [client_id]):
                        raise ValueError
                    form["client_id"] = [client_id]
                    # SDK revocation request parsing requires this form field
                    # even when authentication itself uses the Basic header.
                    if path == "/revoke":
                        form["client_secret"] = [unquote(raw_secret)]
                    body = urlencode(form, doseq=True).encode("utf-8")
                    scope = {**scope, "headers": [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"content-length"] + [(b"content-length", str(len(body)).encode())]}
                except (ValueError, UnicodeError, binascii.Error):
                    return await JSONResponse({"error": "invalid_client"}, status_code=401, headers=HEADERS)(scope, receive, send)
            elif path == "/revoke":
                form = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
                ids = form.get("client_id", [])
                if len(ids) == 1 and "client_secret" not in form:
                    client = await self.provider.get_client(ids[0])
                    if client and client.token_endpoint_auth_method == "none":
                        # A registered public client has no secret. This
                        # satisfies the SDK's request schema without granting
                        # anything; the SDK still binds the token to the client.
                        form["client_secret"] = [""]
                        body = urlencode(form, doseq=True).encode("utf-8")
                        scope = {**scope, "headers": [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"content-length"] + [(b"content-length", str(len(body)).encode())]}
        error = None
        if path == "/authorize" and params.get("code_challenge_method", [""]) != ["S256"]:
            error = "PKCE S256 is required."
        if path in ("/authorize", "/token") and "resource" in params and params["resource"] != [self.provider.resource]:
            error = "The resource must be this MCP endpoint."
        if error:
            return await JSONResponse({"error": "invalid_request", "error_description": error}, status_code=400, headers=HEADERS)(scope, receive, send)
        sent = False

        async def replay():
            nonlocal sent
            if scope["method"] == "POST" and not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        async def safe_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers = [(k, v) for k, v in headers if k.lower() not in {b"cache-control", b"pragma", b"referrer-policy"}]
                headers += [(b"cache-control", b"no-store"), (b"pragma", b"no-cache"),
                            (b"referrer-policy", HEADERS["Referrer-Policy"].encode("ascii"))]
                for index, (key, value) in enumerate(headers):
                    if key.lower() == b"location":
                        uri = value.decode("latin1")
                        p = urlsplit(uri)
                        clean = urlunsplit((p.scheme, p.netloc, p.path, "", ""))
                        query = dict(parse_qsl(p.query))
                        if self.provider.redirect_ok(clean) and ("code" in query or "error" in query):
                            query["iss"] = self.provider.origin
                            headers[index] = (key, urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), "")).encode("latin1"))
                message = {**message, "headers": headers}
                # Only fixed route/method/status. Never log query strings or bodies.
                LOG.info("OAuth %s %s -> %d", scope["method"], path, message["status"])
            await send(message)
        await self.app(scope, replay, safe_send)
