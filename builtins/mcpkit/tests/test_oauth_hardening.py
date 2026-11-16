"""Hardening regressions for the OAuth server, ported from payment-mcp.

Each of these encodes something that actually went wrong against real ChatGPT
and real browsers: discovery paths clients invent, native form posts whose
Origin follows the referrer policy, copied authorization forms, code replay,
confidential clients authenticating two different ways, stores that must never
contain a raw token. The code they cover now serves every service, so the
tests live with the code.

Dummy credentials, temporary files, no network.
"""
from __future__ import annotations

import base64
import hashlib
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastmcp import FastMCP
from fastmcp.server.auth import MultiAuth
from starlette.testclient import TestClient

from mcpkit.auth import StaticBearer
from mcpkit.config import CHATGPT_REDIRECTS, AuthConfig, ConfigError
from mcpkit.oauth import PasswordOAuth, redirect_allowed

ORIGIN = "https://service.example.invalid"
RESOURCE = ORIGIN + "/mcp"
CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
PASSWORD = "test-only-owner-password-" + "p" * 40
STATIC = "test-only-local-bearer-" + "s" * 40
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
SCOPES = ["payment:read", "offline_access"]
TOOLS = ("alpha", "beta", "gamma", "delta")


def auth_config(**overrides) -> AuthConfig:
    return AuthConfig(mode="oauth", password_secret="x/password", scopes=list(SCOPES),
                      required_scopes=["payment:read"], subject="owner", title="Test Service", **overrides)


def provider(tmp_path, *, mcp_path: str = "/mcp", **overrides) -> PasswordOAuth:
    return PasswordOAuth(name="service", origin=ORIGIN, mcp_path=mcp_path, password=PASSWORD,
                         store=tmp_path / "oauth.sqlite3", auth=auth_config(**overrides))


def app_for(auth: PasswordOAuth):
    server = FastMCP("service", auth=MultiAuth(server=auth, verifiers=[StaticBearer([STATIC], SCOPES)]))
    for name in TOOLS:
        server.add_tool(_read_only_tool(name))

    @server.custom_route("/health", methods=["GET"])
    async def health(_):  # the unauthenticated check mcpm makes
        from starlette.responses import JSONResponse
        return JSONResponse({"status": "ok"})

    return server.http_app(path="/mcp", stateless_http=True, json_response=True)


def _read_only_tool(name: str):
    from mcp_types import ToolAnnotations

    from mcpkit.tools import SchemaTool

    return SchemaTool(name=name, description=f"{name} (test)", parameters={"type": "object", "properties": {}},
                      annotations=ToolAnnotations(read_only_hint=True), handler=lambda args: {"ok": name})


@pytest.fixture
def service(tmp_path):
    auth = provider(tmp_path)
    with TestClient(app_for(auth), base_url=ORIGIN) as http:
        yield http, auth, tmp_path


def register(http, **extra):
    data = {"client_name": "ChatGPT OAuth test", "redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "none", "scope": " ".join(SCOPES),
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}
    data.update(extra)
    result = http.post("/register", json=data)
    assert result.status_code == 201, result.text
    return result.json()


def begin(http, client, callback=CALLBACK, **extra):
    query = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": callback,
             "scope": " ".join(SCOPES), "resource": RESOURCE, "state": "test-state-only",
             "code_challenge": CHALLENGE, "code_challenge_method": "S256"}
    query.update(extra)
    result = http.get("/authorize", params=query, follow_redirects=False)
    assert result.status_code in (302, 303), result.text
    url = result.headers["location"]
    assert url.startswith(ORIGIN + "/login?")
    page = http.get(url)
    assert page.status_code == 200
    form = {"flow": parse_qs(urlsplit(url).query)["flow"][0],
            "csrf": re.search(r'name="csrf" value="([A-Za-z0-9_-]+)"', page.text).group(1),
            "password": PASSWORD, "decision": "approve"}
    return form, page


def approve(http, form):
    response = http.post("/login", data=form, headers={"Origin": ORIGIN}, follow_redirects=False)
    assert response.status_code == 303, response.text
    values = parse_qs(urlsplit(response.headers["location"]).query)
    assert values["iss"] == [ORIGIN] and values["state"] == ["test-state-only"]
    return values["code"][0]


def exchange(http, client, code, **extra):
    data = {"grant_type": "authorization_code", "client_id": client["client_id"], "redirect_uri": CALLBACK,
            "code": code, "code_verifier": VERIFIER, "resource": RESOURCE}
    if client.get("client_secret"):
        data["client_secret"] = client["client_secret"]
    data.update(extra)
    return http.post("/token", data=data)


def grant(http, **registration):
    client = register(http, **registration)
    form, page = begin(http, client)
    assert PASSWORD not in page.text and STATIC not in page.text
    result = exchange(http, client, approve(http, form))
    assert result.status_code == 200, result.text
    return client, result.json()


def rpc(http, token=None, method="tools/list"):
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    return http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 7, "method": method, "params": {}})


# -- discovery ---------------------------------------------------------------------------

@pytest.mark.parametrize("prefix,suffix", [("", ""), ("", "/mcp"), ("/mcp", "")])
@pytest.mark.parametrize("doc", ["oauth-protected-resource", "oauth-authorization-server", "openid-configuration"])
def test_discovery_aliases(service, prefix, suffix, doc):
    """Clients invent all three shapes; answering only the RFC one strands them."""
    http, _, _ = service
    url = f"{prefix}/.well-known/{doc}{suffix}"
    response = http.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert PASSWORD not in response.text and STATIC not in response.text
    body = response.json()
    if doc == "oauth-protected-resource":
        assert body["resource"] == RESOURCE and body["authorization_servers"] == [ORIGIN]
    else:
        assert body["issuer"] == ORIGIN
        assert body["code_challenge_methods_supported"] == ["S256"]
        assert body["grant_types_supported"] == ["authorization_code", "refresh_token"]
        assert "offline_access" in body["scopes_supported"]
        assert body["authorization_response_iss_parameter_supported"] is True
        assert body["client_id_metadata_document_supported"] is False
    assert http.options(url).status_code == 204


# -- redirect policy ----------------------------------------------------------------------

@pytest.mark.parametrize("uri,valid", [
    (CALLBACK, True), ("https://chat.openai.com/connector_platform_oauth_redirect", True),
    ("https://chatgpt.com/connector/oauth/abc-DEF_123", True),
    ("http://chatgpt.com/connector_platform_oauth_redirect", False),
    ("https://chatgpt.com.evil.invalid/connector_platform_oauth_redirect", False),
    ("https://chatgpt.com@evil.invalid/connector_platform_oauth_redirect", False),
    ("https://owner@chatgpt.com/connector_platform_oauth_redirect", False),
    (CALLBACK + "?redirect=evil", False), (CALLBACK + "#fragment", False),
    ("https://chatgpt.com:444/connector_platform_oauth_redirect", False),
    ("https://chatgpt.com/other", False), ("https://evil.invalid/callback", False),
])
def test_callback_allowlist(uri, valid):
    assert redirect_allowed(uri, list(CHATGPT_REDIRECTS)) is valid


@pytest.mark.parametrize("uri", ["https://evil.invalid/callback", "http://localhost/callback", CALLBACK + "#frag"])
def test_dcr_rejects_untrusted_callbacks(service, uri):
    http, _, _ = service
    assert http.post("/register", json={"redirect_uris": [uri], "token_endpoint_auth_method": "none"}).status_code == 400


# -- the happy path and the credentials around it -----------------------------------------

def test_full_authorization_and_tools(service):
    http, _, _ = service
    _, tokens = grant(http)
    assert tokens["expires_in"] == 3600 and tokens["refresh_token"]
    result = rpc(http, tokens["access_token"])
    assert result.status_code == 200
    assert {t["name"] for t in result.json()["result"]["tools"]} == set(TOOLS)


def test_anonymous_static_and_invalid_credentials(service):
    http, _, _ = service
    for token in (None, "bad-token", PASSWORD):
        response = rpc(http, token)
        assert response.status_code == 401
        assert ORIGIN in response.headers["www-authenticate"]
        assert "oauth-protected-resource" in response.headers["www-authenticate"]
    assert rpc(http, STATIC).status_code == 200  # the local bearer beside OAuth
    assert http.get("/health").status_code == 200


# -- the browser half ----------------------------------------------------------------------

def test_browser_csrf_origin_and_password(service):
    http, _, _ = service
    form, page = begin(http, register(http))
    cookie = page.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert http.post("/login", data=form, headers={"Origin": "https://evil.invalid"}).status_code == 403
    assert http.post("/login", data={**form, "csrf": "bad"}, headers={"Origin": ORIGIN}).status_code == 403
    assert http.post("/login", data={**form, "csrf": "中文"}, headers={"Origin": ORIGIN}).status_code == 403
    assert http.post("/login", data={**form, "password": "bad"}, headers={"Origin": ORIGIN}).status_code == 401
    assert approve(http, form)
    assert http.post("/login", data=form, headers={"Origin": ORIGIN}).status_code == 403  # single use


def test_copied_form_without_browser_cookie_denied(service):
    http, _, _ = service
    form, _ = begin(http, register(http))
    http.cookies.clear()
    assert http.post("/login", data=form, headers={"Origin": ORIGIN}).status_code == 403


def test_explicit_denial_returns_issuer_and_state(service):
    http, _, _ = service
    form, _ = begin(http, register(http))
    response = http.post("/login", data={**form, "decision": "deny", "password": ""},
                         headers={"Origin": ORIGIN}, follow_redirects=False)
    assert response.status_code == 303
    assert parse_qs(urlsplit(response.headers["location"]).query) == {
        "error": ["access_denied"], "iss": [ORIGIN], "state": ["test-state-only"]}


def test_login_rate_limit(service):
    http, _, _ = service
    form, _ = begin(http, register(http))
    for _ in range(8):
        assert http.post("/login", data={**form, "password": "bad"}, headers={"Origin": ORIGIN}).status_code == 401
    assert http.post("/login", data=form, headers={"Origin": ORIGIN}).status_code == 429


@pytest.mark.parametrize("callback", [CALLBACK, "https://chatgpt.com/connector/oauth/dummy-callback-123"])
def test_native_form_policy_and_callback_csp(service, callback):
    """A native HTML form's Origin follows the referrer policy; the CSP must
    still allow exactly the one validated callback, and nothing else."""
    http, _, _ = service
    client = register(http, redirect_uris=[callback])
    form, page = begin(http, client, callback=callback)
    assert page.headers["referrer-policy"] == "strict-origin"
    csp = page.headers["content-security-policy"]
    form_action = next(part.strip() for part in csp.split(";") if part.strip().startswith("form-action"))
    assert form_action.split() == ["form-action", "'self'", callback]
    assert "frame-ancestors 'none'" in csp and "default-src 'none'" in csp
    assert all(flag in page.headers["set-cookie"].lower() for flag in ("secure", "httponly", "samesite=lax", "path=/"))

    approved = http.post("/login", data=form, headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
                         follow_redirects=False)
    assert approved.status_code == 303 and approved.headers["referrer-policy"] == "strict-origin"
    location = urlsplit(approved.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == callback
    params = parse_qs(location.query)
    tokens = http.post("/token", data={"grant_type": "authorization_code", "client_id": client["client_id"],
                                       "redirect_uri": callback, "code": params["code"][0],
                                       "code_verifier": VERIFIER, "resource": RESOURCE})
    assert tokens.status_code == 200
    assert rpc(http, tokens.json()["access_token"]).status_code == 200


@pytest.mark.parametrize("case,reason", [
    ("null_origin", "origin_mismatch"), ("foreign_origin", "origin_mismatch"),
    ("cross_site", "cross_site_post"), ("no_cookie", "cookie_missing"),
    ("bad_cookie", "cookie_mismatch"), ("bad_csrf", "csrf_mismatch"),
    ("expired_flow", "flow_expired"),
])
def test_rejected_forms_stay_rejected_and_have_safe_diagnostics(service, caplog, case, reason):
    http, auth, _ = service
    form, _ = begin(http, register(http))
    headers = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
    original = dict(form)
    if case == "null_origin":
        headers["Origin"] = "null"
    elif case == "foreign_origin":
        headers["Origin"] = "https://untrusted.example.invalid"
    elif case == "cross_site":
        headers["Sec-Fetch-Site"] = "cross-site"
    elif case == "no_cookie":
        http.cookies.clear()
    elif case == "bad_cookie":
        http.cookies.clear()
        http.cookies.set(auth.cookie, "dummy-wrong-binding")
    elif case == "bad_csrf":
        form["csrf"] = "dummy-wrong-csrf"
    elif case == "expired_flow":
        auth.flows.clear()
    response = http.post("/login", data=form, headers=headers, follow_redirects=False)
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/html")
    assert reason in response.text and "客户端" in response.text
    assert "location" not in response.headers and not auth.codes
    assert reason in caplog.text
    for secret in (PASSWORD, original["flow"], original["csrf"]):
        assert secret not in response.text and secret not in caplog.text


def test_wrong_password_retry_keeps_browser_compatible_headers(service):
    http, _, _ = service
    form, _ = begin(http, register(http))
    failed = http.post("/login", data={**form, "password": "wrong-dummy-password"},
                       headers={"Origin": ORIGIN}, follow_redirects=False)
    assert failed.status_code == 401
    assert failed.headers["referrer-policy"] == "strict-origin"
    assert CALLBACK in failed.headers["content-security-policy"]
    assert http.post("/login", data=form, headers={"Origin": ORIGIN}, follow_redirects=False).status_code == 303


def test_old_authorization_page_explains_expiry(service, caplog):
    http, _, _ = service
    result = http.get("/login", params={"flow": "dummy-old-flow-not-to-log"})
    assert result.status_code == 400
    assert "flow_expired" in result.text and "客户端" in result.text
    assert "dummy-old-flow-not-to-log" not in caplog.text


def test_expired_login_flow_cannot_authorize(service):
    http, auth, _ = service
    form, _ = begin(http, register(http))
    auth.flows[form["flow"]].expires = 1
    assert http.post("/login", data=form, headers={"Origin": ORIGIN}).status_code == 403


# -- protocol rules ------------------------------------------------------------------------

@pytest.mark.parametrize("overrides", [
    {"code_challenge_method": "plain"}, {"code_challenge_method": ""},
    {"resource": "https://elsewhere.invalid/mcp"},
])
def test_wrong_pkce_or_resource_rejected_early(service, overrides):
    http, _, _ = service
    client = register(http)
    query = {"client_id": client["client_id"], "response_type": "code", "redirect_uri": CALLBACK,
             "code_challenge": CHALLENGE, "code_challenge_method": "S256", "resource": RESOURCE}
    query.update(overrides)
    assert http.get("/authorize", params=query, follow_redirects=False).status_code == 400


def test_pkce_redirect_client_and_code_replay(service):
    http, _, _ = service
    client = register(http)
    form, _ = begin(http, client)
    code = approve(http, form)
    assert exchange(http, client, code, code_verifier="wrong" * 12).status_code in (400, 401)
    assert exchange(http, client, code, resource=ORIGIN + "/wrong").status_code == 400
    assert exchange(http, client, code, redirect_uri="https://chatgpt.com/connector/oauth/different").status_code in (400, 401)
    assert exchange(http, register(http), code).status_code in (400, 401)  # another client
    assert exchange(http, client, code).status_code == 200
    assert exchange(http, client, code).status_code in (400, 401)  # replay


@pytest.mark.parametrize("auth_method", ["client_secret_post", "client_secret_basic"])
def test_confidential_dcr_clients(service, auth_method):
    http, _, _ = service
    client = register(http, token_endpoint_auth_method=auth_method)
    assert client["client_secret"] and client["client_secret_expires_at"] == 0
    form, _ = begin(http, client)
    code = approve(http, form)
    if auth_method == "client_secret_post":
        result = exchange(http, client, code)
    else:
        result = http.post("/token", data={"grant_type": "authorization_code", "code": code,
                                           "code_verifier": VERIFIER, "redirect_uri": CALLBACK, "resource": RESOURCE},
                           auth=(client["client_id"], client["client_secret"]))
    assert result.status_code == 200, result.text


def test_refresh_rotation_persistence_and_revocation(service):
    http, _, tmp_path = service
    client, original = grant(http)
    data = {"grant_type": "refresh_token", "client_id": client["client_id"],
            "refresh_token": original["refresh_token"], "resource": RESOURCE}
    assert http.post("/token", data={**data, "scope": "payment:write"}).status_code == 400
    assert http.post("/token", data={**data, "resource": "https://evil.invalid"}).status_code == 400
    renewed = http.post("/token", data=data).json()
    assert renewed["refresh_token"] != original["refresh_token"]
    assert http.post("/token", data=data).status_code in (400, 401)  # rotated away
    assert rpc(http, renewed["access_token"]).status_code == 200

    stored = (tmp_path / "oauth.sqlite3").read_bytes()
    for secret in (PASSWORD, STATIC, original["access_token"], renewed["refresh_token"]):
        assert secret.encode() not in stored, "the store must hold digests, not tokens"

    with TestClient(app_for(provider(tmp_path)), base_url=ORIGIN) as after:
        assert rpc(after, renewed["access_token"]).status_code == 200, "a restart must not drop grants"
        latest = after.post("/token", data={**data, "refresh_token": renewed["refresh_token"]}).json()
        revoke = after.post("/revoke", data={"client_id": client["client_id"], "token": latest["access_token"],
                                             "token_type_hint": "access_token"})
        assert revoke.status_code == 200, revoke.text
        assert rpc(after, latest["access_token"]).status_code == 401


def test_expired_grants_and_store_audience(service):
    http, auth, tmp_path = service
    client, tokens = grant(http)
    with auth.state.transaction() as db:
        db.execute("UPDATE entries SET expires=1 WHERE kind IN ('access','refresh')")
    assert rpc(http, tokens["access_token"]).status_code == 401
    assert http.post("/token", data={"grant_type": "refresh_token", "client_id": client["client_id"],
                                     "refresh_token": tokens["refresh_token"]}).status_code in (400, 401)
    with pytest.raises(ConfigError, match="another resource"):
        provider(tmp_path, mcp_path="/other")


def test_size_limits(service):
    http, _, _ = service
    for path in ("/register", "/token", "/login"):
        assert http.post(path, content=b"x" * 32769).status_code == 413
