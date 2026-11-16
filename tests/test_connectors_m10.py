"""Milestones M10 and M11: third-party MCP connectors hosted on the gateway (decision D10, design §7).

Over real HTTP on ephemeral ports: a gateway (uvicorn) with **no device
connected**, stub upstream MCP servers (Streamable HTTP with header auth, an
SSE-only server), and an OAuth-protected upstream whose authorization server
is mcpkit's ``PasswordOAuth`` (DCR + PKCE + refresh). The browser leg of the
OAuth login is played by httpx.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import socket
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from mcpkit.config import AuthConfig
from mcpkit.oauth import PasswordOAuth

from agent_nexus.cloud import build_cloud_app

SERVER_TOKEN = "s" * 40
API_KEY = "k" * 36
UP_PASSWORD = "u" * 40
SECRET_MARKERS = (API_KEY, "bearer-secret-value-1234567890", "client-secret-value-0987654321")


def free_listener() -> socket.socket:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))  # ephemeral: never a live service port
    listener.listen()
    return listener


@contextlib.asynccontextmanager
async def serve(app):
    listener = free_listener()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False,
                                           lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(.05)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 10)
        listener.close()


def upstream(name: str) -> FastMCP:
    server = FastMCP(f"stub-{name}")

    @server.tool
    async def echo(text: str) -> dict:
        """Echo text back, with the upstream name."""
        return {"upstream": name, "text": text}

    @server.tool
    async def weather_forecast(city: str) -> str:
        """Weather forecast for a city."""
        return f"{city}: sunny ({name})"

    return server


class HeaderGate:
    """ASGI wrapper: requires X-Api-Key and records the headers it saw."""

    def __init__(self, app, key: str):
        self.app, self.key, self.seen = app, key, []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            self.seen.append(headers)
            if headers.get("x-api-key") != self.key:
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"no key"})
                return
        await self.app(scope, receive, send)


@contextlib.asynccontextmanager
async def gateway(tmp_path, monkeypatch, *, mailbox=True):
    monkeypatch.setenv("SERVER_TOKEN", SERVER_TOKEN)
    listener = free_listener()
    port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    state = tmp_path / "gateway-state"
    config = {"token_env": "SERVER_TOKEN", "agents": {}, "public_url": origin, "state_dir": str(state)}
    if not mailbox:
        config["mailbox"] = {"enabled": False}
    app = build_cloud_app(config, tmp_path)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(.05)
    assert server.started
    http = httpx.AsyncClient(base_url=origin, trust_env=False, timeout=30,
                             headers={"Authorization": f"Bearer {SERVER_TOKEN}"})
    try:
        yield {"app": app, "origin": origin, "http": http, "state": state, "db": state / "gateway.sqlite3"}
    finally:
        await http.aclose()
        server.should_exit = True
        await asyncio.wait_for(task, 15)
        listener.close()


def mcp(gw) -> Client:
    return Client(StreamableHttpTransport(f"{gw['origin']}/mcp", auth=SERVER_TOKEN))


async def eventually(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        await asyncio.sleep(0.05)
    return False


def assert_no_secret(text: str):
    for marker in SECRET_MARKERS:
        assert marker not in text


def all_files(path: Path) -> str:
    return "\n".join(item.read_text(encoding="utf-8", errors="replace") for item in path.rglob("*")
                     if item.is_file() and item.suffix in (".json",))


# -- static connectors (M10) ------------------------------------------------------------

async def test_header_connector_and_sse_fallback_work_with_no_device(tmp_path, monkeypatch):
    gate = HeaderGate(upstream("http").http_app(path="/mcp"), API_KEY)
    async with serve(gate) as http_origin, serve(upstream("sse").http_app(path="/sse", transport="sse")) as sse_origin, \
            gateway(tmp_path, monkeypatch) as gw:
        api = gw["http"]
        # probe: the SSE-only server is detected through the POST 405 -> GET fallback
        probe = (await api.post("/api/connectors/probe", json={"url": f"{sse_origin}/sse"})).json()
        assert probe["transport"] == "sse" and probe["needs_oauth"] is False and probe["tools_count"] == 2
        probe = (await api.post("/api/connectors/probe", json={"url": f"{http_origin}/mcp"})).json()
        assert probe["auth_hint"] == "bearer" and probe["needs_oauth"] is False

        created = await api.post("/api/connectors", json={
            "name": "yunxiao", "title": "云效", "url": f"{http_origin}/mcp", "transport": "auto",
            "auth": {"type": "headers", "headers": {"X-Api-Key": API_KEY, "X-Region": "cn-hangzhou"}}})
        assert created.status_code == 201, created.text
        body = created.json()["connector"]
        assert body["run_on"] == "gateway" and body["mcp_id"] == "hub/yunxiao"
        assert body["auth"] == {"type": "headers", "secret_set": True, "header_names": ["X-Api-Key", "X-Region"]}
        assert (await api.post("/api/connectors", json={"name": "sse-only", "url": f"{sse_origin}/sse"})).status_code == 201
        conflict = await api.post("/api/connectors", json={"name": "yunxiao", "url": f"{http_origin}/mcp"})
        assert conflict.status_code == 409 and conflict.json()["code"] == "name_conflict"

        test = (await api.post("/api/connectors/yunxiao/test")).json()
        assert test["ok"] and test["tools_count"] == 2 and test["transport"] == "http", test
        assert {tool["name"] for tool in test["tools"]} == {"echo", "weather_forecast"}
        assert any(headers.get("x-region") == "cn-hangzhou" for headers in gate.seen)
        sse_test = (await api.post("/api/connectors/sse-only/test")).json()
        assert sse_test["ok"] and sse_test["transport"] == "sse"

        listing = await api.get("/api/connectors")
        assert_no_secret(listing.text)
        states = {item["name"]: item for item in listing.json()["connectors"]}
        assert states["yunxiao"]["state"] == "ok" and states["yunxiao"]["tools_count"] == 2
        assert states["sse-only"]["transport_detected"] == "sse"
        assert_no_secret((await api.get("/api/connectors/export")).text)

        # all clients, no device online: mcplist, toolsearch, mcpdetail and tooluse
        assert not gw["app"].state.hub.peers
        async with mcp(gw) as client:
            mcps = (await client.call_tool("mcplist", {})).structured_content["mcps"]
            ids = {item["mcp_id"]: item for item in mcps}
            assert ids["hub/yunxiao"]["kind"] == "connector" and ids["hub/yunxiao"]["run_on"] == "gateway"
            assert ids["hub/yunxiao"]["tool_count"] == 2 and ids["hub/sse-only"]["auth_state"] == "ok"
            only_hub = (await client.call_tool("mcplist", {"agent_id": "hub"})).structured_content["mcps"]
            assert {item["mcp_id"] for item in only_hub} == {"hub/yunxiao", "hub/sse-only"}
            found = (await client.call_tool("toolsearch", {"query": "weather forecast"})).structured_content
            assert found["tools"][0]["name"] == "weather_forecast" and found["tools"][0]["kind"] == "connector"
            detail = (await client.call_tool("mcpdetail", {"mcp_id": "hub/sse-only"})).structured_content
            assert {tool["name"] for tool in detail["tools"]} == {"echo", "weather_forecast"}
            assert_no_secret(json.dumps(mcps) + json.dumps(detail))
            used = await client.call_tool("tooluse", {"mcp_id": "hub/yunxiao", "tool": "echo", "arguments": {"text": "hi"}})
            assert used.structured_content == {"upstream": "http", "text": "hi"}
            used = await client.call_tool("tooluse", {"mcp_id": "hub/sse-only", "tool": "echo", "arguments": {"text": "yo"}})
            assert used.structured_content == {"upstream": "sse", "text": "yo"}
            bad = await client.call_tool("tooluse", {"mcp_id": "hub/yunxiao", "tool": "echo", "arguments": {}},
                                         raise_on_error=False)
            assert bad.is_error and "invalid tool arguments" in bad.content[0].text

        # the shared session: concurrent calls from new clients open no new upstream session
        sessions = lambda: {headers["mcp-session-id"] for headers in gate.seen if headers.get("mcp-session-id")}
        before = sessions()
        async with mcp(gw) as client:
            await asyncio.gather(*(client.call_tool("tooluse", {"mcp_id": "hub/yunxiao", "tool": "echo",
                                                                "arguments": {"text": str(i)}}) for i in range(8)))
        assert sessions() == before

        # secrets are write-only: an update without values keeps them; disk holds them, the db does not
        updated = await api.put("/api/connectors/yunxiao", json={"title": "云效 DevOps"})
        assert updated.status_code == 200 and updated.json()["connector"]["title"] == "云效 DevOps"
        assert (await api.post("/api/connectors/yunxiao/test")).json()["ok"]
        rows = sqlite3.connect(gw["db"]).execute("SELECT config_json, cache_json FROM connectors").fetchall()
        assert_no_secret(json.dumps(rows))
        secret_file = gw["state"] / "connectors" / "yunxiao" / "secrets.json"
        assert API_KEY in secret_file.read_text(encoding="utf-8")
        if sys.platform != "win32":
            assert secret_file.stat().st_mode & 0o777 == 0o600

        # disabled -> gone from mcplist; tooluse explains; delete removes secrets
        await api.put("/api/connectors/yunxiao", json={"enabled": False})
        async with mcp(gw) as client:
            ids = {item["mcp_id"] for item in (await client.call_tool("mcplist", {})).structured_content["mcps"]}
            assert "hub/yunxiao" not in ids
            off = await client.call_tool("tooluse", {"mcp_id": "hub/yunxiao", "tool": "echo", "arguments": {"text": "x"}},
                                         raise_on_error=False)
            assert off.is_error and "已停用" in off.content[0].text
        assert (await api.delete("/api/connectors/yunxiao")).status_code == 204
        assert not secret_file.exists()
        assert (await api.get("/api/connectors/yunxiao")).status_code == 404


async def test_validation_errors_and_coming_soon(tmp_path, monkeypatch):
    async with gateway(tmp_path, monkeypatch) as gw:
        api = gw["http"]
        cases = [({"name": "Bad Name", "url": "https://x.example/mcp"}, "invalid_name"),
                 ({"name": "hub", "url": "https://x.example/mcp"}, "reserved_name"),
                 ({"name": "ok", "url": "ftp://x.example/mcp"}, "invalid_url"),
                 ({"name": "ok", "url": "https://u:p@x.example/mcp"}, "invalid_url"),
                 ({"name": "ok", "url": "https://x.example/mcp", "auth": {"type": "bearer"}}, "missing_secret"),
                 ({"name": "ok", "url": "https://x.example/mcp", "run_on": "device:pc-main"}, "coming_soon"),
                 ({"name": "fs", "command": ["npx", "-y", "server"]}, "stdio_needs_device")]
        for body, code in cases:
            response = await api.post("/api/connectors", json=body)
            assert response.json()["code"] == code, (body, response.text)
        # owner-only
        anonymous = httpx.AsyncClient(base_url=gw["origin"], trust_env=False)
        assert (await anonymous.get("/api/connectors")).status_code == 401
        await anonymous.aclose()
        # an unreachable server gives a readable error, the console still lists it
        created = await api.post("/api/connectors", json={"name": "down", "url": "http://127.0.0.1:9/mcp",
                                                          "auth": {"type": "bearer", "token": "bearer-secret-value-1234567890"}})
        assert created.status_code == 201
        test = (await api.post("/api/connectors/down/test")).json()
        assert not test["ok"] and test["state"] == "error" and "无法连接" in test["error"]
        assert_no_secret(json.dumps(test))
        async with mcp(gw) as client:
            failed = await client.call_tool("tooluse", {"mcp_id": "hub/down", "tool": "x", "arguments": {}}, raise_on_error=False)
            assert failed.is_error and "连接失败" in failed.content[0].text
        summary = (await api.get("/api/console/summary")).json()
        assert summary["connectors"]["total"] == 1 and summary["connectors"]["error"] == 1
        assert summary["setup"]["connector_added"] is True
        assert any(alert["kind"] == "connector_error" for alert in summary["alerts"])


async def test_registry_through_hub_call(tmp_path, monkeypatch):
    """The local console writes the same registry (hub_call connector_registry)."""
    async with serve(upstream("hc").http_app(path="/mcp")) as origin, gateway(tmp_path, monkeypatch) as gw:
        facade = gw["app"].state.gateway
        created = await facade.hub_call(None, {"op": "connector_registry", "args": {
            "op": "create", "connector": {"name": "local-added", "url": f"{origin}/mcp"}}})
        assert created["connector"]["mcp_id"] == "hub/local-added"
        names = {item["name"] for item in (await gw["http"].get("/api/connectors")).json()["connectors"]}
        assert names == {"local-added"}
        tested = await facade.hub_call(None, {"op": "connector_registry", "args": {"op": "test", "name": "local-added"}})
        assert tested["ok"] and tested["tools_count"] == 2
        listed = await facade.hub_call(None, {"op": "connector_registry", "args": {"op": "list"}})
        assert listed["connectors"][0]["state"] == "ok"


# -- OAuth connectors (M11) ---------------------------------------------------------------

def oauth_upstream(tmp_path, origin_holder: dict, *, ttl: int = 3600, dcr: bool = True):
    """An MCP server protected by mcpkit's PasswordOAuth; redirects only to gateway callbacks."""
    listener = free_listener()
    port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    origin_holder["origin"] = origin
    auth = AuthConfig(mode="oauth", password_secret="up/password", title="Upstream",
                      redirect_uris=[r"http://127\.0\.0\.1:\d+/oauth/connectors/callback/[a-z0-9-]+"],
                      access_token_ttl_s=ttl)
    provider = PasswordOAuth(name="up", origin=origin, mcp_path="/mcp", password=UP_PASSWORD,
                             store=tmp_path / "up-oauth.sqlite3", auth=auth)
    # PasswordOAuth only accepts https callbacks; the test gateway is plain http on loopback.
    provider.redirect_ok = lambda uri: bool(re.fullmatch(r"http://127\.0\.0\.1:\d+/oauth/connectors/callback/[a-z0-9-]+", uri))
    server = upstream("oauth")
    server.auth = provider
    app = server.http_app(path="/mcp")
    if not dcr:
        inner = app

        async def no_register(scope, receive, send):
            if scope["type"] == "http" and scope["path"] == "/register":
                await send({"type": "http.response.start", "status": 404, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
            await inner(scope, receive, send)
        app = no_register
    return app, listener, provider


@contextlib.asynccontextmanager
async def serve_on(app, listener):
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 10)
        listener.close()


async def browser_approve(authorize_url: str, up_origin: str) -> str:
    """Play the owner's browser on the provider: returns the callback URL it redirects to."""
    async with httpx.AsyncClient(trust_env=False, timeout=15, follow_redirects=False) as browser:
        start = await browser.get(authorize_url)
        assert start.status_code == 302, start.text
        login_url = start.headers["location"]
        page = await browser.get(login_url)
        cookie = page.headers["set-cookie"].split(";", 1)[0]
        form = {"flow": parse_qs(urlsplit(login_url).query)["flow"][0],
                "csrf": re.search(r'name="csrf" value="([^"]+)"', page.text).group(1),
                "password": UP_PASSWORD, "decision": "approve"}
        done = await browser.post(f"{up_origin}/login", data=form, headers={"Origin": up_origin, "Cookie": cookie})
        assert done.status_code == 303, done.text
        return done.headers["location"]


async def test_oauth_dcr_pkce_login_refresh_and_revocation(tmp_path, monkeypatch):
    holder: dict = {}
    app, listener, provider = oauth_upstream(tmp_path, holder, ttl=3)
    async with serve_on(app, listener) as up, gateway(tmp_path, monkeypatch) as gw:
        api = gw["http"]
        probe = (await api.post("/api/connectors/probe", json={"url": f"{up}/mcp"})).json()
        assert probe["needs_oauth"] is True and probe["auth_hint"] == "oauth", probe
        created = await api.post("/api/connectors", json={"name": "aliyun", "title": "阿里云 MCP", "url": f"{up}/mcp",
                                                          "auth": {"type": "oauth", "client": {"mode": "dynamic"}}})
        assert created.status_code == 201
        assert created.json()["connector"]["state"] == "needs_login"
        async with mcp(gw) as client:
            listed = {item["mcp_id"]: item for item in (await client.call_tool("mcplist", {})).structured_content["mcps"]}
            assert listed["hub/aliyun"]["auth_state"] == "needs_login" and listed["hub/aliyun"]["tool_count"] == 0
            found = (await client.call_tool("toolsearch", {"query": "echo"})).structured_content
            assert found["connectors_needing_login"][0]["mcp_id"] == "hub/aliyun"
            started = time.monotonic()
            refused = await client.call_tool("tooluse", {"mcp_id": "hub/aliyun", "tool": "echo", "arguments": {"text": "x"}},
                                             raise_on_error=False)
            assert refused.is_error and "登录" in refused.content[0].text and time.monotonic() - started < 5

        login = (await api.post("/api/connectors/aliyun/login")).json()
        authorize = urlsplit(login["authorize_url"])
        query = parse_qs(authorize.query)
        assert authorize.path == "/authorize" and query["code_challenge_method"] == ["S256"]
        assert query["resource"] == [f"{up}/mcp"]
        assert query["redirect_uri"] == [f"{gw['origin']}/oauth/connectors/callback/aliyun"]
        # DCR registered the fixed gateway callback
        client_info = json.loads((gw["state"] / "connectors" / "aliyun" / "oauth.json").read_text(encoding="utf-8"))["client_info"]
        assert client_info["redirect_uris"] == [f"{gw['origin']}/oauth/connectors/callback/aliyun"]

        callback = await browser_approve(login["authorize_url"], up)
        assert callback.startswith(f"{gw['origin']}/oauth/connectors/callback/aliyun?")
        path = callback[len(gw["origin"]):]
        # wrong state, and a browser without the owner session, are refused
        anonymous = httpx.AsyncClient(base_url=gw["origin"], trust_env=False, follow_redirects=False)
        wrong = await anonymous.get(re.sub(r"state=[^&]+", "state=forged", path))
        assert wrong.status_code == 400
        bounce = await anonymous.get(path)
        assert bounce.status_code == 200 and 'http-equiv="refresh"' in bounce.text
        refused = await anonymous.get(path + "&b=1")
        assert refused.status_code == 403
        await anonymous.aclose()
        done = await api.get(path, follow_redirects=False)
        assert done.status_code == 303 and done.headers["location"] == "/#/connectors?login=aliyun&result=ok"
        replay = await api.get(path, follow_redirects=False)
        assert replay.status_code == 400  # state is one-time

        oauth_file = gw["state"] / "connectors" / "aliyun" / "oauth.json"
        stored = json.loads(oauth_file.read_text(encoding="utf-8"))
        assert stored["tokens"]["access_token"] and stored["tokens"]["refresh_token"]
        if sys.platform != "win32":
            assert oauth_file.stat().st_mode & 0o777 == 0o600
        view = (await api.get("/api/connectors/aliyun")).json()["connector"]
        assert view["state"] == "ok" and view["tools_count"] == 2 and view["oauth"]["logged_in"]
        assert stored["tokens"]["access_token"] not in json.dumps(view)

        async with mcp(gw) as client:
            used = await client.call_tool("tooluse", {"mcp_id": "hub/aliyun", "tool": "echo", "arguments": {"text": "1"}})
            assert used.structured_content == {"upstream": "oauth", "text": "1"}
            first_access = stored["tokens"]["access_token"]
            await asyncio.sleep(3.2)  # the access token expired: refreshed without any interaction
            used = await client.call_tool("tooluse", {"mcp_id": "hub/aliyun", "tool": "echo", "arguments": {"text": "2"}})
            assert used.structured_content["text"] == "2"
            refreshed = json.loads(oauth_file.read_text(encoding="utf-8"))
            assert refreshed["tokens"]["access_token"] != first_access
            assert refreshed["tokens"]["refresh_token"] != stored["tokens"]["refresh_token"]  # rotation kept

            # revoke every refresh token upstream; once the access token expires the grant is gone
            with sqlite3.connect(tmp_path / "up-oauth.sqlite3") as db:
                db.execute("DELETE FROM entries WHERE kind='refresh'")
            await asyncio.sleep(3.2)
            started = time.monotonic()
            gone = await client.call_tool("tooluse", {"mcp_id": "hub/aliyun", "tool": "echo", "arguments": {"text": "3"}},
                                          raise_on_error=False)
            assert gone.is_error and "登录" in gone.content[0].text and time.monotonic() - started < 10
        view = (await api.get("/api/connectors/aliyun")).json()["connector"]
        assert view["state"] == "needs_login" and not view["oauth"]["logged_in"]
        lost = json.loads(oauth_file.read_text(encoding="utf-8"))
        assert "tokens" not in lost and lost["client_info"]["client_id"] == client_info["client_id"]
        # the owner got a system mail with the console link
        with sqlite3.connect(gw["db"]) as db:
            mails = db.execute("SELECT m.subject, m.body FROM messages m JOIN deliveries d ON d.message_id = m.id "
                               "WHERE d.to_addr = 'me' AND m.kind = 'system'").fetchall()
        assert any("aliyun" in subject and "/#/connectors" in body for subject, body in mails), mails

        # logout clears tokens (state needs_login)
        login = (await api.post("/api/connectors/aliyun/login")).json()
        callback = await browser_approve(login["authorize_url"], up)
        assert (await api.get(callback[len(gw["origin"]):], follow_redirects=False)).status_code == 303
        assert (await api.get("/api/connectors/aliyun")).json()["connector"]["state"] == "ok"
        out = (await api.post("/api/connectors/aliyun/logout")).json()["connector"]
        assert out["state"] == "needs_login"
        assert "tokens" not in json.loads(oauth_file.read_text(encoding="utf-8"))


async def test_oauth_static_client_when_dcr_is_refused(tmp_path, monkeypatch):
    holder: dict = {}
    app, listener, provider = oauth_upstream(tmp_path, holder, dcr=False)
    async with serve_on(app, listener) as up, gateway(tmp_path, monkeypatch, mailbox=False) as gw:
        api = gw["http"]
        await api.post("/api/connectors", json={"name": "cf", "url": f"{up}/mcp", "auth": {"type": "oauth"}})
        refused = await api.post("/api/connectors/cf/login")
        assert refused.status_code == 502 and refused.json()["code"] in ("dcr_failed", "dcr_unsupported")
        assert "Client ID" in refused.json()["error"]
        # pre-register a client the way an owner would in the provider's console
        from mcp.shared.auth import OAuthClientInformationFull
        redirect = f"{gw['origin']}/oauth/connectors/callback/cf"
        await provider.register_client(OAuthClientInformationFull(
            client_id="static-client", client_secret="client-secret-value-0987654321", redirect_uris=[redirect],
            token_endpoint_auth_method="client_secret_post", grant_types=["authorization_code", "refresh_token"],
            response_types=["code"], scope="mcp offline_access"))
        updated = await api.put("/api/connectors/cf", json={"auth": {"type": "oauth", "client": {
            "mode": "static", "client_id": "static-client", "client_secret": "client-secret-value-0987654321"}}})
        assert updated.status_code == 200
        assert updated.json()["connector"]["auth"]["client"] == {"mode": "static", "client_id": "static-client", "secret_set": True}
        assert_no_secret(updated.text)
        login = (await api.post("/api/connectors/cf/login")).json()
        assert parse_qs(urlsplit(login["authorize_url"]).query)["client_id"] == ["static-client"]
        callback = await browser_approve(login["authorize_url"], up)
        done = await api.get(callback[len(gw["origin"]):], follow_redirects=False)
        assert done.headers["location"] == "/#/connectors?login=cf&result=ok"
        async with mcp(gw) as client:
            used = await client.call_tool("tooluse", {"mcp_id": "hub/cf", "tool": "echo", "arguments": {"text": "s"}})
            assert used.structured_content["text"] == "s"
        assert_no_secret((await api.get("/api/connectors")).text)


async def test_oauth_live_token_revoked_first_call_says_login(tmp_path, monkeypatch):
    """Verifier finding: the provider revokes a *live* access token (and the refresh token). The
    transport sees a 401, its refresh fails inside the httpx auth flow, and the very first call must
    already say "log in" -- not "MCPError: Connection closed / 执行结果未知"."""
    holder: dict = {}
    app, listener, provider = oauth_upstream(tmp_path, holder)  # long-lived access token
    async with serve_on(app, listener) as up, gateway(tmp_path, monkeypatch) as gw:
        api = gw["http"]
        await api.post("/api/connectors", json={"name": "idp", "url": f"{up}/mcp",
                                                "auth": {"type": "oauth", "client": {"mode": "dynamic"}}})
        login = (await api.post("/api/connectors/idp/login")).json()
        callback = await browser_approve(login["authorize_url"], up)
        assert (await api.get(callback[len(gw["origin"]):], follow_redirects=False)).status_code == 303
        oauth_file = gw["state"] / "connectors" / "idp" / "oauth.json"
        async with mcp(gw) as client:
            used = await client.call_tool("tooluse", {"mcp_id": "hub/idp", "tool": "echo", "arguments": {"text": "1"}})
            assert used.structured_content["text"] == "1"

            # only the access token is revoked: the transport refreshes on the 401 and the call goes through
            before = json.loads(oauth_file.read_text(encoding="utf-8"))["tokens"]["access_token"]
            with sqlite3.connect(tmp_path / "up-oauth.sqlite3") as db:
                db.execute("DELETE FROM entries WHERE kind='access'")
            used = await client.call_tool("tooluse", {"mcp_id": "hub/idp", "tool": "echo", "arguments": {"text": "2"}},
                                          raise_on_error=False)
            assert not used.is_error, used.content[0].text
            assert used.structured_content["text"] == "2"
            assert json.loads(oauth_file.read_text(encoding="utf-8"))["tokens"]["access_token"] != before

            # the whole grant is revoked while the access token is still unexpired locally
            with sqlite3.connect(tmp_path / "up-oauth.sqlite3") as db:
                db.execute("DELETE FROM entries WHERE kind IN ('access', 'refresh')")
            started = time.monotonic()
            first = await client.call_tool("tooluse", {"mcp_id": "hub/idp", "tool": "echo", "arguments": {"text": "3"}},
                                           raise_on_error=False)
            text = first.content[0].text
            assert first.is_error and "登录" in text, text
            assert "执行结果未知" not in text and "Connection closed" not in text, text
            assert time.monotonic() - started < 10
            second = await client.call_tool("tooluse", {"mcp_id": "hub/idp", "tool": "echo", "arguments": {"text": "4"}},
                                            raise_on_error=False)
            assert second.is_error and "登录" in second.content[0].text
        view = (await api.get("/api/connectors/idp")).json()["connector"]
        assert view["state"] == "needs_login" and not view["oauth"]["logged_in"]
        with sqlite3.connect(gw["db"]) as db:
            mails = db.execute("SELECT m.subject FROM messages m JOIN deliveries d ON d.message_id = m.id "
                               "WHERE d.to_addr = 'me' AND m.kind = 'system'").fetchall()
        assert sum("idp" in subject for (subject,) in mails) == 1, mails


async def test_tokens_survive_a_gateway_restart(tmp_path, monkeypatch):
    holder: dict = {}
    app, listener, provider = oauth_upstream(tmp_path, holder)
    async with serve_on(app, listener) as up:
        async with gateway(tmp_path, monkeypatch, mailbox=False) as gw:
            await gw["http"].post("/api/connectors", json={"name": "keep", "url": f"{up}/mcp", "auth": {"type": "oauth"}})
            login = (await gw["http"].post("/api/connectors/keep/login")).json()
            callback = await browser_approve(login["authorize_url"], up)
            first_origin = gw["origin"]
            assert (await gw["http"].get(callback[len(first_origin):], follow_redirects=False)).status_code == 303
        # a new gateway process on the same state directory (its port differs; tokens do not)
        async with gateway(tmp_path, monkeypatch, mailbox=False) as gw:
            async with mcp(gw) as client:
                used = await client.call_tool("tooluse", {"mcp_id": "hub/keep", "tool": "echo", "arguments": {"text": "r"}})
                assert used.structured_content["text"] == "r"


async def test_connector_auth_records_refusals_for_the_host():
    """ConnectorAuth leaves a LoginRequired in ``manager.refused`` for both ways a 401 ends a call:
    the refresh itself fails, or the refreshed token is refused again."""
    import httpx2

    from agent_nexus.connectors.oauth import ConnectorAuth, LoginRequired

    class Manager:
        def __init__(self, fail_refresh):
            self.fail_refresh, self.refused = fail_refresh, {}

        async def access_token(self, name, *, force_refresh=False, client_secret=None):
            if force_refresh and self.fail_refresh:
                raise LoginRequired(name, f"连接器 {name} 的授权已失效（invalid_grant）：请在控制台重新登录")
            return "fresh" if force_refresh else "stale"

    always_401 = httpx2.MockTransport(lambda request: httpx2.Response(401))
    for fail_refresh in (True, False):
        manager = Manager(fail_refresh)
        async with httpx2.AsyncClient(transport=always_401, auth=ConnectorAuth(manager, "idp", lambda: None)) as http:
            if fail_refresh:
                with pytest.raises(LoginRequired):
                    await http.post("http://upstream.invalid/mcp")
            else:
                assert (await http.post("http://upstream.invalid/mcp")).status_code == 401
        assert "登录" in manager.refused["idp"].message
    ok = Manager(False)
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(lambda request: httpx2.Response(
            200 if request.headers["Authorization"] == "Bearer fresh" else 401)),
            auth=ConnectorAuth(ok, "idp", lambda: None)) as http:
        assert (await http.post("http://upstream.invalid/mcp")).status_code == 200
    assert ok.refused == {}
