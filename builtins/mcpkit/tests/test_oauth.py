"""The OAuth authorization server end to end, over real HTTP, across a restart."""
import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from conftest import free_port
from test_service import call_echo, mcp_post

PASSWORD = "p" * 40
BEARER = "b" * 40
REDIRECT = "https://client.example/callback"


def oauth_workspace(workspace, port, **auth):
    workspace.secret("svc/password", PASSWORD)
    workspace.secret("svc/bearer", BEARER)
    return workspace.service("svc", manifest={"port": port}, entry={
        "public_url": f"http://127.0.0.1:{port}",
        "auth": {"mode": "oauth", "password_secret": "svc/password", "bearer_secrets": ["svc/bearer"],
                 "redirect_uris": [r"https://client\.example/callback"], "title": "Test MCP", **auth},
    })


class Flow:
    def __init__(self, origin: str):
        self.origin = origin
        self.http = httpx.Client(base_url=origin, trust_env=False, timeout=15, follow_redirects=False)

    def register(self, redirect: str = REDIRECT) -> httpx.Response:
        return self.http.post("/register", json={
            "redirect_uris": [redirect], "token_endpoint_auth_method": "none", "client_name": "test",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "scope": "mcp offline_access"})

    def authorize(self, client_id: str, password: str = PASSWORD,
                  scope: str = "mcp offline_access") -> tuple[httpx.Response, str]:
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        start = self.http.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": "st8",
            "code_challenge": challenge, "code_challenge_method": "S256", "scope": scope})
        assert start.status_code == 302, start.text
        login_url = start.headers["location"]
        assert login_url.startswith(self.origin + "/login?flow=")
        page = self.http.get(login_url)
        assert page.status_code == 200 and "Test MCP" in page.text
        cookie = page.headers["set-cookie"].split(";", 1)[0]
        assert cookie.startswith("__Host-svc_oauth_flow=")
        form = {"flow": parse_qs(urlsplit(login_url).query)["flow"][0],
                "csrf": re.search(r'name="csrf" value="([^"]+)"', page.text).group(1),
                "password": password, "decision": "approve"}
        done = self.http.post("/login", data=form, headers={"Origin": self.origin, "Cookie": cookie})
        return done, verifier

    def token(self, **form) -> httpx.Response:
        return self.http.post("/token", data=form)


def test_full_flow_refresh_rotation_and_restart(workspace, run):
    port = free_port()
    directory = oauth_workspace(workspace, port)
    svc = run(directory, port=port)
    origin = svc.url
    flow = Flow(origin)

    # discovery points at this origin, and the MCP endpoint demands a token
    meta = flow.http.get("/.well-known/oauth-authorization-server").json()
    assert meta["issuer"] == origin and meta["code_challenge_methods_supported"] == ["S256"]
    challenge = mcp_post(origin + "/mcp")
    assert challenge.status_code == 401
    assert f'resource_metadata="{origin}/.well-known/oauth-protected-resource/mcp"' in challenge.headers["www-authenticate"]

    # unknown callbacks are refused at registration
    assert flow.register("https://evil.example/callback").status_code == 400

    client_id = flow.register().json()["client_id"]
    wrong, _ = flow.authorize(client_id, password="w" * 40)
    assert wrong.status_code == 401 and "授权密码不正确" in wrong.text
    # the styled page names the callback host, shows the error as an alert, and runs no script
    assert "授权 client.example 访问" in wrong.text and 'role="alert"' in wrong.text and "<script" not in wrong.text

    done, verifier = flow.authorize(client_id)
    assert done.status_code == 303
    callback = urlsplit(done.headers["location"])
    query = parse_qs(callback.query)
    assert f"https://{callback.hostname}{callback.path}" == REDIRECT
    assert query["state"] == ["st8"] and query["iss"] == [origin]

    tokens = flow.token(grant_type="authorization_code", code=query["code"][0], redirect_uri=REDIRECT,
                        client_id=client_id, code_verifier=verifier).json()
    assert tokens["expires_in"] == 3600 and tokens["refresh_token"]
    assert call_echo(origin + "/mcp", "oauth", auth=tokens["access_token"]) == "echo:oauth"
    assert call_echo(origin + "/mcp", "bearer", auth=BEARER) == "echo:bearer"  # static token beside OAuth

    rotated = flow.token(grant_type="refresh_token", refresh_token=tokens["refresh_token"], client_id=client_id).json()
    assert rotated["access_token"] != tokens["access_token"]
    reused = flow.token(grant_type="refresh_token", refresh_token=tokens["refresh_token"], client_id=client_id)
    # FastMCP answers invalid_grant with 401 (the MCP spec's rule for invalid tokens), not RFC 6749's 400.
    assert reused.status_code == 401 and reused.json()["error"] == "invalid_grant"

    # a restart keeps clients, access tokens and refresh tokens
    svc.stop()
    banner = svc.output()
    assert PASSWORD not in banner and BEARER not in banner and "auth=oauth" in banner
    svc = run(directory, port=port)
    assert call_echo(origin + "/mcp", "again", auth=rotated["access_token"]) == "echo:again"
    after = flow.token(grant_type="refresh_token", refresh_token=rotated["refresh_token"], client_id=client_id)
    assert after.status_code == 200, after.text
    with httpx.Client(trust_env=False) as http:
        assert http.get(origin + "/healthz").status_code == 200


def test_access_tokens_can_be_made_non_expiring(workspace, run):
    port = free_port()
    svc = run(oauth_workspace(workspace, port, access_token_ttl_s=0), port=port)
    flow = Flow(svc.url)
    client_id = flow.register().json()["client_id"]
    done, verifier = flow.authorize(client_id)
    code = parse_qs(urlsplit(done.headers["location"]).query)["code"][0]
    tokens = flow.token(grant_type="authorization_code", code=code, redirect_uri=REDIRECT,
                        client_id=client_id, code_verifier=verifier).json()
    assert "expires_in" not in tokens or tokens["expires_in"] is None
    assert call_echo(svc.url + "/mcp", auth=tokens["access_token"]) == "echo:你好"


def test_store_refuses_a_different_resource(workspace, run):
    port = free_port()
    directory = oauth_workspace(workspace, port)
    svc = run(directory, port=port)
    svc.stop()
    other = free_port()
    workspace.central["services"]["svc"]["public_url"] = f"http://127.0.0.1:{other}"
    workspace.save()
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "app.py", "--transport", "http", "--port", str(other), "--check-config"], cwd=directory,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 2 and "belongs to another resource" in result.stderr


def test_refresh_tokens_exist_even_without_an_offline_access_scope(workspace, run):
    """A server that does not advertise offline_access still issues refresh
    tokens: otherwise the first expiry forces the user to reconnect."""
    port = free_port()
    svc = run(oauth_workspace(workspace, port, scopes=["tools"], required_scopes=["tools"]), port=port)
    flow = Flow(svc.url)
    registered = flow.http.post("/register", json={
        "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none", "client_name": "test",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "scope": "tools"})
    client_id = registered.json()["client_id"]

    done, verifier = flow.authorize(client_id, scope="tools")
    code = parse_qs(urlsplit(done.headers["location"]).query)["code"][0]
    tokens = flow.token(grant_type="authorization_code", code=code, redirect_uri=REDIRECT,
                        client_id=client_id, code_verifier=verifier).json()
    assert tokens["refresh_token"], "no refresh token was issued"
    rotated = flow.token(grant_type="refresh_token", refresh_token=tokens["refresh_token"], client_id=client_id).json()
    assert rotated["refresh_token"] and rotated["refresh_token"] != tokens["refresh_token"]
