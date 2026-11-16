"""OAuth 2.1 for gateway-hosted connectors (design §7.3, M11).

The gateway is the OAuth *client* of a third-party MCP server:

1. discovery — RFC 9728 protected resource metadata (from the 401's
   ``WWW-Authenticate: Bearer resource_metadata=…`` or the well-known paths),
   then RFC 8414 authorization server metadata (OIDC discovery as fallback);
2. a client — RFC 7591 dynamic registration with the fixed redirect URI
   ``<public_url>/oauth/connectors/callback/<name>``, or a static client the
   owner entered when the provider has no DCR;
3. authorization code + PKCE S256 + ``state`` + RFC 8707 ``resource``;
4. tokens in ``<state_dir>/connectors/<name>/oauth.json`` (owner-only, atomic),
   refreshed under a per-connector lock before they expire (and once on a
   401), refresh-token rotation kept; ``invalid_grant`` clears the tokens
   (the client registration is kept) and the connector needs a login again.

This is deliberately our own small client rather than the SDK's
``OAuthClientProvider`` (which design §7.3 sketched): the console needs the
authorize URL *returned* from ``POST /api/connectors/<name>/login`` and the
code delivered by a callback route, and tool calls must never start an
interactive flow (§7.3 rule 6). Splitting login from the transport makes both
explicit; the transports only ever see an ``httpx2.Auth`` that adds a token.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import secrets
import time
from typing import Callable
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx2

from .registry import ConnectorError, SecretStore

log = logging.getLogger(__name__)

STATE_TTL_S = 300
REFRESH_MARGIN_S = 60
HTTP_TIMEOUT_S = 20
_RESOURCE_METADATA = re.compile(r'resource_metadata\s*=\s*"([^"]+)"|resource_metadata\s*=\s*([^\s,]+)', re.IGNORECASE)
_SCOPE = re.compile(r'scope\s*=\s*"([^"]*)"', re.IGNORECASE)


class LoginRequired(ConnectorError):
    def __init__(self, name: str, message: str | None = None):
        super().__init__("needs_login", message or f"连接器 {name} 需要登录：请在网关控制台的“连接器”页点“登录”", 409)
        self.name = name


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def resource_metadata_hint(www_authenticate: str | None) -> str | None:
    if not www_authenticate:
        return None
    match = _RESOURCE_METADATA.search(www_authenticate)
    return (match.group(1) or match.group(2)) if match else None


def canonical_resource(url: str) -> str:
    """RFC 8707 / MCP: the server URL without fragment, lower-case scheme and host."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def _well_known(base: str, document: str) -> list[str]:
    parts = urlsplit(base)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    urls = []
    if path:
        urls.append(f"{origin}/.well-known/{document}{path}")
    urls.append(f"{origin}/.well-known/{document}")
    if path and document == "openid-configuration":
        urls.append(f"{origin}{path}/.well-known/{document}")
    return urls


class OAuthManager:
    """Login, token storage and refresh for every OAuth connector."""

    def __init__(self, secrets_store: SecretStore, *, redirect_base: Callable[[], str],
                 clock: Callable[[], float] = time.time, client_name: str = "Agent Nexus (hub)"):
        self.secrets = secrets_store
        self.redirect_base = redirect_base
        self.clock = clock
        self.client_name = client_name
        self.pending: dict[str, dict] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        #: Called with the connector name when its grant is gone (``invalid_grant``).
        self.on_login_lost: Callable[[str], None] | None = None
        #: Login failures raised inside a transport's auth flow (where the MCP client only reports
        #: "Connection closed"): ``ConnectorHost.call_tool`` reads them back to answer "please log in".
        self.refused: dict[str, LoginRequired] = {}

    # -- helpers -----------------------------------------------------------------
    def redirect_uri(self, name: str) -> str:
        return f"{self.redirect_base().rstrip('/')}/oauth/connectors/callback/{name}"

    def _lock(self, name: str) -> asyncio.Lock:
        return self.locks.setdefault(name, asyncio.Lock())

    @staticmethod
    def _http() -> httpx2.AsyncClient:
        return httpx2.AsyncClient(timeout=HTTP_TIMEOUT_S, follow_redirects=True,
                                  headers={"Accept": "application/json", "MCP-Protocol-Version": "2025-06-18"})

    def state(self, name: str) -> dict:
        return self.secrets.read(name, "oauth")

    def has_tokens(self, name: str) -> bool:
        return bool((self.state(name).get("tokens") or {}).get("access_token"))

    def summary(self, name: str) -> dict:
        """Non-secret facts for the console: logged in, expiry, issuer, client mode."""
        data = self.state(name)
        tokens = data.get("tokens") or {}
        return {"logged_in": bool(tokens.get("access_token")), "expires_at": data.get("expires_at"),
                "issuer": (data.get("as_metadata") or {}).get("issuer"),
                "has_refresh_token": bool(tokens.get("refresh_token")),
                "client_registered": bool((data.get("client_info") or {}).get("client_id"))}

    # -- discovery ---------------------------------------------------------------
    async def discover(self, url: str, *, www_authenticate: str | None = None) -> dict:
        """Protected resource metadata and authorization server metadata for an MCP URL."""
        async with self._http() as http:
            if www_authenticate is None:
                try:
                    probe = await http.post(url, json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                        "protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "agent-nexus", "version": "0"}}},
                        headers={"Accept": "application/json, text/event-stream"})
                    www_authenticate = probe.headers.get("www-authenticate")
                except httpx2.HTTPError:
                    www_authenticate = None
            prm, prm_url = None, None
            candidates = [resource_metadata_hint(www_authenticate)] + _well_known(url, "oauth-protected-resource")
            for candidate in [item for item in candidates if item]:
                try:
                    response = await http.get(candidate)
                except httpx2.HTTPError:
                    continue
                if response.status_code == 200:
                    try:
                        body = response.json()
                    except ValueError:
                        continue
                    if isinstance(body, dict) and body.get("authorization_servers"):
                        prm, prm_url = body, candidate
                        break
            if prm:
                issuer = str(prm["authorization_servers"][0]).rstrip("/")
                resource = str(prm.get("resource") or canonical_resource(url))
            else:
                parts = urlsplit(url)
                issuer = f"{parts.scheme}://{parts.netloc}"
                resource = canonical_resource(url)
            metadata = None
            for candidate in _well_known(issuer, "oauth-authorization-server") + _well_known(issuer, "openid-configuration"):
                try:
                    response = await http.get(candidate)
                except httpx2.HTTPError:
                    continue
                if response.status_code == 200:
                    try:
                        body = response.json()
                    except ValueError:
                        continue
                    if isinstance(body, dict) and body.get("authorization_endpoint") and body.get("token_endpoint"):
                        metadata = body
                        break
            if metadata is None:
                # 2025-03-26 servers without metadata: the default endpoints at the issuer.
                metadata = {"issuer": issuer, "authorization_endpoint": f"{issuer}/authorize",
                            "token_endpoint": f"{issuer}/token", "registration_endpoint": f"{issuer}/register",
                            "_assumed": True}
        scope_hint = None
        if www_authenticate:
            match = _SCOPE.search(www_authenticate)
            scope_hint = match.group(1) if match else None
        return {"prm": prm, "prm_url": prm_url, "as_metadata": metadata, "resource": resource,
                "scopes_supported": (prm or {}).get("scopes_supported") or metadata.get("scopes_supported"),
                "scope_hint": scope_hint}

    # -- login -------------------------------------------------------------------
    async def begin(self, connector: dict, *, client_secret: str | None = None, owner: str | None = None,
                    www_authenticate: str | None = None) -> str:
        """Prepare a login and return the provider's authorize URL (the console opens it)."""
        name = connector["name"]
        auth = connector.get("auth") or {}
        found = await self.discover(connector["url"], www_authenticate=www_authenticate)
        metadata = found["as_metadata"]
        redirect_uri = self.redirect_uri(name)
        data = self.state(name)
        client_cfg = auth.get("client") or {"mode": "dynamic"}
        scopes = list(auth.get("scopes") or [])
        if not scopes and found.get("scope_hint"):
            scopes = found["scope_hint"].split()
        if not scopes and (found.get("prm") or {}).get("scopes_supported"):
            # MCP authorization: without a challenge scope, ask for the resource's scopes_supported.
            scopes = [str(item) for item in found["prm"]["scopes_supported"]]
        supported = set(found.get("scopes_supported") or []) | set(metadata.get("scopes_supported") or [])
        if not auth.get("scopes") and "offline_access" in supported and "offline_access" not in scopes:
            scopes.append("offline_access")  # a refresh token keeps a 7x24 connector logged in
        if client_cfg.get("mode") == "static":
            client_info = {"client_id": client_cfg["client_id"], "redirect_uris": [redirect_uri],
                           "token_endpoint_auth_method": "client_secret_post" if client_secret else "none",
                           "static": True}
        else:
            client_info = data.get("client_info") or {}
            reusable = (client_info.get("client_id") and not client_info.get("static")
                        and client_info.get("issuer") == metadata.get("issuer")
                        and redirect_uri in (client_info.get("redirect_uris") or []))
            if not reusable:
                client_info = await self.register(name, metadata, redirect_uri, scopes)
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(32)
        self._prune()
        self.pending[state] = {"name": name, "verifier": verifier, "redirect_uri": redirect_uri,
                               "resource": found["resource"], "created": self.clock(), "owner": owner}
        data.update(client_info=client_info, as_metadata=metadata, resource=found["resource"],
                    redirect_uri=redirect_uri, prm_url=found.get("prm_url"), scopes=scopes)
        self.secrets.write(name, "oauth", data)
        query = {"response_type": "code", "client_id": client_info["client_id"], "redirect_uri": redirect_uri,
                 "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
                 "code_challenge_method": "S256", "state": state, "resource": found["resource"]}
        if scopes:
            query["scope"] = " ".join(scopes)
        endpoint = metadata["authorization_endpoint"]
        return endpoint + ("&" if "?" in endpoint else "?") + urlencode(query)

    async def register(self, name: str, metadata: dict, redirect_uri: str, scopes: list[str]) -> dict:
        endpoint = metadata.get("registration_endpoint")
        if not endpoint:
            raise ConnectorError("dcr_unsupported", "这个服务不支持动态注册客户端：请编辑连接器，在 OAuth 里填写预先注册的 Client ID"
                                 f"（回调地址填 {redirect_uri}）")
        body = {"client_name": self.client_name, "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                "token_endpoint_auth_method": "none"}
        if scopes:
            body["scope"] = " ".join(scopes)
        async with self._http() as http:
            try:
                response = await http.post(endpoint, json=body)
            except httpx2.HTTPError as exc:
                raise ConnectorError("dcr_failed", f"动态注册失败：无法连接 {urlsplit(endpoint).netloc}（{type(exc).__name__}）", 502) from None
        if response.status_code not in (200, 201):
            detail = _error_text(response)
            raise ConnectorError("dcr_failed", f"服务拒绝了动态注册（HTTP {response.status_code}{'：' + detail if detail else ''}）。"
                                 f"可以改用预先注册的 Client ID，回调地址填 {redirect_uri}", 502)
        info = response.json()
        if not isinstance(info, dict) or not info.get("client_id"):
            raise ConnectorError("dcr_failed", "动态注册的响应里没有 client_id", 502)
        info["issuer"] = metadata.get("issuer")
        info.setdefault("redirect_uris", [redirect_uri])
        return info

    def _prune(self) -> None:
        now = self.clock()
        self.pending = {key: value for key, value in self.pending.items() if now - value["created"] < STATE_TTL_S}

    def check_state(self, name: str, state: str | None) -> dict:
        """The pending login for this callback; refuses unknown, expired or mismatched state."""
        self._prune()
        flow = self.pending.get(state or "")
        if flow is None or flow["name"] != name:
            raise ConnectorError("invalid_state", "登录请求已过期或无效：回到控制台重新点“登录”", 400)
        return flow

    async def complete(self, name: str, *, code: str, state: str, client_secret: str | None = None) -> dict:
        flow = self.check_state(name, state)
        self.pending.pop(state, None)
        data = self.state(name)
        client = data.get("client_info") or {}
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": flow["redirect_uri"],
                "code_verifier": flow["verifier"], "resource": flow["resource"]}
        tokens = await self._token_request(name, data, client, form, client_secret)
        data = self.state(name)
        self._store_tokens(name, data, tokens)
        return self.summary(name)

    async def _token_request(self, name: str, data: dict, client: dict, form: dict, client_secret: str | None) -> dict:
        endpoint = (data.get("as_metadata") or {}).get("token_endpoint")
        if not endpoint:
            raise LoginRequired(name)
        form = {**form, "client_id": client.get("client_id", "")}
        auth = None
        secret = client_secret if client.get("static") else client.get("client_secret")
        method = client.get("token_endpoint_auth_method") or ("client_secret_post" if secret else "none")
        if secret and method == "client_secret_basic":
            auth = httpx2.BasicAuth(client["client_id"], secret)
        elif secret:
            form["client_secret"] = secret
        async with self._http() as http:
            try:
                response = await http.post(endpoint, data=form, auth=auth)
            except httpx2.HTTPError as exc:
                raise ConnectorError("token_failed", f"无法连接授权服务器（{type(exc).__name__}）", 502) from None
        if response.status_code != 200:
            error = ""
            try:
                error = str(response.json().get("error") or "")
            except (ValueError, AttributeError):
                pass
            if error in ("invalid_grant", "invalid_client", "unauthorized_client"):
                raise LoginRequired(name, f"连接器 {name} 的授权已失效（{error}）：请在控制台重新登录")
            raise ConnectorError("token_failed", f"授权服务器返回 HTTP {response.status_code}{'：' + error if error else ''}", 502)
        try:
            tokens = response.json()
        except ValueError:
            raise ConnectorError("token_failed", "授权服务器的令牌响应不是 JSON", 502) from None
        if not isinstance(tokens, dict) or not tokens.get("access_token"):
            raise ConnectorError("token_failed", "授权服务器的响应里没有 access_token", 502)
        return tokens

    def _store_tokens(self, name: str, data: dict, tokens: dict) -> None:
        old = data.get("tokens") or {}
        if not tokens.get("refresh_token") and old.get("refresh_token"):
            tokens["refresh_token"] = old["refresh_token"]  # no rotation: the old one stays valid
        now = self.clock()
        expires_in = tokens.get("expires_in")
        data["tokens"] = {key: tokens[key] for key in ("access_token", "token_type", "refresh_token", "scope") if tokens.get(key)}
        data["obtained_at"] = now
        data["expires_at"] = now + float(expires_in) if isinstance(expires_in, (int, float)) and expires_in > 0 else None
        self.secrets.write(name, "oauth", data)

    # -- tokens for calls ----------------------------------------------------------
    async def access_token(self, name: str, *, force_refresh: bool = False, client_secret: str | None = None) -> str:
        """A valid access token, refreshed when it is about to expire. Never interactive."""
        async with self._lock(name):
            data = self.state(name)
            tokens = data.get("tokens") or {}
            if not tokens.get("access_token"):
                raise LoginRequired(name)
            expires = data.get("expires_at")
            lifetime = (expires - data["obtained_at"]) if expires is not None and data.get("obtained_at") else None
            margin = min(REFRESH_MARGIN_S, 0.25 * lifetime) if lifetime else REFRESH_MARGIN_S
            stale = force_refresh or (expires is not None and expires - self.clock() < margin)
            if not stale:
                return tokens["access_token"]
            if not tokens.get("refresh_token"):
                if force_refresh or (expires is not None and expires <= self.clock()):
                    self._lose(name, data)
                    raise LoginRequired(name, f"连接器 {name} 的登录已过期：请在控制台重新登录")
                return tokens["access_token"]
            await self._refresh_locked(name, data, client_secret)
            return self.state(name)["tokens"]["access_token"]

    async def _refresh_locked(self, name: str, data: dict, client_secret: str | None) -> None:
        form = {"grant_type": "refresh_token", "refresh_token": data["tokens"]["refresh_token"]}
        if data.get("resource"):
            form["resource"] = data["resource"]
        try:
            tokens = await self._token_request(name, data, data.get("client_info") or {}, form, client_secret)
        except LoginRequired:
            self._lose(name, data)
            raise
        self._store_tokens(name, data, tokens)

    async def refresh_if_due(self, name: str, *, client_secret: str | None = None) -> bool:
        """Proactive refresh when less than 20% of the token lifetime remains (design §7.3 step 5)."""
        data = self.state(name)
        tokens = data.get("tokens") or {}
        expires, obtained = data.get("expires_at"), data.get("obtained_at")
        if not tokens.get("refresh_token") or expires is None or obtained is None:
            return False
        if expires - self.clock() > 0.2 * max(1.0, expires - obtained):
            return False
        async with self._lock(name):
            data = self.state(name)
            if data.get("expires_at") != expires:
                return False  # refreshed meanwhile
            await self._refresh_locked(name, data, client_secret)
        return True

    def _lose(self, name: str, data: dict) -> None:
        data.pop("tokens", None)
        data.pop("expires_at", None)
        self.secrets.write(name, "oauth", data)
        if self.on_login_lost is not None:
            try:
                self.on_login_lost(name)
            except Exception:
                log.exception("connector %s: login-lost hook failed", name)

    async def logout(self, name: str, *, client_secret: str | None = None) -> None:
        data = self.state(name)
        tokens = data.get("tokens") or {}
        endpoint = (data.get("as_metadata") or {}).get("revocation_endpoint")
        client = data.get("client_info") or {}
        if endpoint and tokens:
            async with self._http() as http:
                for kind in ("refresh_token", "access_token"):
                    if tokens.get(kind):
                        form = {"token": tokens[kind], "token_type_hint": kind, "client_id": client.get("client_id", "")}
                        secret = client_secret if client.get("static") else client.get("client_secret")
                        if secret:
                            form["client_secret"] = secret
                        try:
                            await http.post(endpoint, data=form)
                        except httpx2.HTTPError:
                            pass  # best effort; the tokens are deleted below regardless
        data.pop("tokens", None)
        data.pop("expires_at", None)
        data.pop("obtained_at", None)
        self.secrets.write(name, "oauth", data)

    def forget(self, name: str) -> None:
        self.pending = {key: value for key, value in self.pending.items() if value["name"] != name}
        self.locks.pop(name, None)


def _error_text(response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        return str(body.get("error_description") or body.get("error") or "")[:200]
    return ""


class ConnectorAuth(httpx2.Auth):
    """Adds the connector's bearer token; on a 401 refreshes once and retries the request."""

    requires_response_body = False

    def __init__(self, manager: OAuthManager, name: str, client_secret: Callable[[], str | None]):
        self.manager = manager
        self.name = name
        self.client_secret = client_secret

    async def async_auth_flow(self, request):
        token = await self.manager.access_token(self.name, client_secret=self.client_secret())
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            try:
                token = await self.manager.access_token(self.name, force_refresh=True, client_secret=self.client_secret())
            except LoginRequired as exc:
                self.manager.refused[self.name] = exc
                raise
            request.headers["Authorization"] = f"Bearer {token}"
            response = yield request
            if response.status_code == 401:
                self.manager.refused[self.name] = LoginRequired(
                    self.name, f"连接器 {self.name} 的令牌被拒绝：请在控制台重新登录")

    def sync_auth_flow(self, request):  # pragma: no cover - the transports are async
        raise RuntimeError("ConnectorAuth is async only")


def expand(value: str) -> str:
    """``{env:NAME}`` references in header values and tokens (design §7.2)."""
    import os
    return re.sub(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}", lambda match: os.environ.get(match.group(1), ""), value)


__all__ = ["OAuthManager", "ConnectorAuth", "LoginRequired", "expand", "resource_metadata_hint", "canonical_resource"]
