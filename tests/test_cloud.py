import asyncio
import contextlib
import json
import socket
import sys
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.tools.base import ToolResult
from mcp_types import ImageContent, TextContent
from starlette.testclient import TestClient

from agent_nexus.agent import LocalGateway, run_agent
from agent_nexus.agent import connection_settings
from agent_nexus.cloud import build_cloud_app
from agent_nexus.gateway import HIDDEN_GATEWAY_TOOLS, LEGACY_TOOL_NAMES
from agent_nexus.federation import Hub, Peer, RESERVED_TOOLS, RoutingError
from agent_nexus.gateway import score_tool_search


def workspace_server(machine):
    server = FastMCP(f"workspace-{machine}")

    @server.tool
    async def execute(value: str, workdir: str | None = None) -> dict:
        return {"machine": machine, "value": value, "workdir": workdir}

    @server.tool
    async def picture() -> ToolResult:
        return ToolResult(content=[ImageContent(type="image", data="YWJj", mime_type="image/png")],
                          structured_content={"machine": machine}, meta={"origin": machine})

    @server.tool
    async def failure() -> ToolResult:
        return ToolResult(content=[TextContent(type="text", text="expected failure")], is_error=True)

    return server


def internal_server():
    server = FastMCP("private")

    @server.tool
    async def internal_action(number: int) -> dict:
        return {"answer": number * 2}

    return server


def server_config(monkeypatch):
    for key, value in {"SERVER_TOKEN": "s" * 40, "A_TOKEN": "a" * 40, "B_TOKEN": "b" * 40}.items():
        monkeypatch.setenv(key, value)
    return {"token_env": "SERVER_TOKEN", "agents": {"pc-a": "A_TOKEN", "pc-b": "B_TOKEN"}}


def test_toolsearch_scores_natural_language_instead_of_strict_and():
    tools = [
        {"name": "list_pages", "description": "List browser pages and select a page for inspection."},
        {"name": "performance_start_trace", "description": "Start a browser performance trace."},
        {"name": "find_gameobjects", "description": "Find objects in the current Unity scene hierarchy."},
        {"name": "manage_scene", "description": "Inspect or change the current Unity editor scene."},
    ]
    browser = [(score_tool_search("list browser pages tabs inspect page performance",
                                  "pc/chrome-devtools", tool), tool["name"]) for tool in tools[:2]]
    assert all(score > 0 for (score, _), _name in browser)
    assert sorted(browser, reverse=True)[0][1] == "list_pages"
    unity = [(score_tool_search("Unity editor current scene hierarchy", "pc/unity", tool), tool["name"])
             for tool in tools[2:]]
    assert {name for (score, matched), name in unity if score and matched} == {"find_gameobjects", "manage_scene"}
    score, matched = score_tool_search("list_pages", "pc/chrome-devtools", tools[0])
    assert score >= 200 and matched == ["list", "page"]
    false_score, false_matches = score_tool_search("unity", "pc/ones", {
        "name": "who_am_i", "description": "Return the current user and opportunity settings."})
    assert false_score == 0 and false_matches == []


def test_ingress_auth_and_reconnect_replaces_instance(tmp_path, monkeypatch):
    app = build_cloud_app(server_config(monkeypatch), tmp_path)
    catalog = {"workspaces": {}, "mcps": {}}
    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ingress/pc-a", headers={"Authorization": "Bearer wrong"}):
                pass
        with client.websocket_connect("/ingress/pc-a", headers={"Authorization": "Bearer " + "a" * 40}) as first:
            first.send_json({"protocol": 1, "agent_id": "pc-a", "catalog": catalog})
            assert first.receive_json()["method"] == "ready"
            stale = app.state.hub.peers["pc-a"]
            # A reconnect means the manager abandoned its old socket, which the
            # server may still believe is alive. The new connection wins.
            with client.websocket_connect("/ingress/pc-a", headers={"Authorization": "Bearer " + "a" * 40}) as again:
                again.send_json({"protocol": 1, "agent_id": "pc-a", "catalog": catalog})
                assert again.receive_json()["method"] == "ready"
                assert len(app.state.hub.peers) == 1 and app.state.hub.peers["pc-a"] is not stale
                with pytest.raises(Exception):
                    first.receive_json()
                assert app.state.hub.peers["pc-a"] is not stale
        assert client.get("/healthz").json()["status"] == "ok"
        # The site root is the owner's console (docs/ui-spec.md §2.2): sign in first.
        root = client.get("/", follow_redirects=False)
        assert root.status_code == 302 and root.headers["location"] == "/auth/login"
        # The management console is never mounted on the cloud endpoint.
        assert client.get("/api/services").status_code == 404
        assert client.post("/mcp", json={}).status_code in (401, 403)


def test_portable_group_does_not_override_fixed_machine_credentials(tmp_path, monkeypatch):
    config = server_config(monkeypatch)
    monkeypatch.setenv("GROUP_TOKEN", "g" * 40)
    config["agent_groups"] = {"portable-": "GROUP_TOKEN"}
    config["agents"]["portable-reserved"] = "A_TOKEN"
    app = build_cloud_app(config, tmp_path)
    with TestClient(app) as client:
        for agent in ("pc-a", "portable-reserved", "other-machine", "portable-"):
            with pytest.raises(Exception):
                with client.websocket_connect(f"/ingress/{agent}", headers={"Authorization": "Bearer " + "g" * 40}):
                    pass
        with contextlib.ExitStack() as stack:
            for agent in ("portable-one", "portable-two"):
                peer = stack.enter_context(client.websocket_connect(f"/ingress/{agent}", headers={"Authorization": "Bearer " + "g" * 40}))
                peer.send_json({"protocol": 1, "agent_id": agent, "catalog": {"workspaces": {}, "mcps": {}}})
                assert peer.receive_json()["method"] == "ready"
            assert len(app.state.hub.peers) == 2


async def test_two_computers_full_reverse_mcp_roundtrip(tmp_path, monkeypatch):
    app = build_cloud_app(server_config(monkeypatch), tmp_path)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    agents = []
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(.05)
        assert server.started
        for machine, token in (("pc-a", "A_TOKEN"), ("pc-b", "B_TOKEN")):
            directory = tmp_path / machine
            directory.mkdir()
            ready = asyncio.Event()
            cloud = {"agent_id": machine, "url": f"ws://127.0.0.1:{port}", "token_env": token,
                     "workspaces": {"project": {"path": str(directory)}}, "refresh_interval_s": 5}
            task = asyncio.create_task(run_agent(tmp_path, cloud, transports={
                "workspace": workspace_server(machine), "extra": internal_server()}, ready=ready))
            agents.append(task)
            await asyncio.wait_for(ready.wait(), 15)

        def client():
            return Client(StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp", auth="s" * 40))

        async with client() as first, client() as second:
            names = {tool.name for tool in await first.list_tools()}
            # 0.8.35 defaults: legacy aliases and rare gateway tools are hidden from tools/list (still callable).
            assert names == (RESERVED_TOOLS - LEGACY_TOOL_NAMES - HIDDEN_GATEWAY_TOOLS) | {"execute", "picture", "failure"}
            assert "internal_action" not in names
            choices = await first.call_tool("workspace_list")
            assert len(choices.data["workspaces"]) == 2
            selected_a = (await first.call_tool("set_workspace", {"agent_id": "pc-a", "workspace": "project"})).data
            selected_b = (await second.call_tool("set_workspace", {"agent_id": "pc-b", "workspace": "project"})).data
            args_a = {"value": "A", "workspace_id": selected_a["workspace_id"]}
            args_b = {"value": "B", "workspace_id": selected_b["workspace_id"]}
            result_a, result_b = await asyncio.gather(first.call_tool("execute", args_a), second.call_tool("execute", args_b))
            assert result_a.data == {"machine": "pc-a", "value": "A", "workdir": str(tmp_path / "pc-a")}
            assert result_b.data == {"machine": "pc-b", "value": "B", "workdir": str(tmp_path / "pc-b")}
            inventory = (await first.call_tool("mcplist")).data["mcps"]
            assert {item["mcp_id"] for item in inventory} == {"pc-a/workspace", "pc-b/workspace", "pc-a/extra", "pc-b/extra"}
            detail = (await first.call_tool("mcpdetail", {"mcp_id": "pc-a/extra"})).data
            assert detail["tools"][0]["name"] == "internal_action"
            search = (await first.call_tool("toolsearch", {"query": "internal_action"})).data
            assert search["total"] == 2
            used = await first.call_tool("tooluse", {"mcp_id": "pc-b/extra", "tool": "internal_action", "arguments": {"number": 21}})
            assert used.data == {"answer": 42}
            image = await first.call_tool_mcp("picture", {"workspace_id": selected_a["workspace_id"]})
            assert image.content[0].type == "image" and image.content[0].data == "YWJj"
            assert image.structured_content == {"machine": "pc-a"}
            assert image.meta["origin"] == "pc-a"
            error = await first.call_tool_mcp("failure", {"workspace_id": selected_a["workspace_id"]})
            assert error.is_error
            invalid = await first.call_tool_mcp("execute", {"value": "test", "workspace_id": "unknown"})
            assert invalid.is_error

            agents[0].cancel()
            await asyncio.gather(agents[0], return_exceptions=True)
            for _ in range(100):
                if "pc-a" not in app.state.hub.peers:
                    break
                await asyncio.sleep(.02)
            offline = await first.call_tool_mcp("execute", args_a)
            assert offline.is_error
            assert (await second.call_tool("execute", args_b)).data["machine"] == "pc-b"
    finally:
        for task in agents:
            task.cancel()
        await asyncio.gather(*agents, return_exceptions=True)
        server.should_exit = True
        await asyncio.wait_for(server_task, 10)
        listener.close()


async def test_disconnect_and_timeout_never_retries():
    class Socket:
        def __init__(self):
            self.sent = []
        async def send_json(self, message):
            self.sent.append(message)
    socket = Socket()
    peer = Peer("a", socket, {}, timeout=.01)
    with pytest.raises(asyncio.TimeoutError):
        await peer.call("tool_call", {})
    assert [item["method"] for item in socket.sent] == ["tool_call", "cancel"]
    peer.timeout = 10
    task = asyncio.create_task(peer.call("tool_call", {}))
    await asyncio.sleep(0)
    peer.disconnect()
    with pytest.raises(RoutingError, match="disconnected"):
        await task
    assert not peer.pending


async def test_invalid_workspace_does_not_mutate_configuration(tmp_path):
    gateway = LocalGateway(tmp_path, {"workspaces": {"bad": str(tmp_path / "missing")}}, transports={})
    with pytest.raises(Exception, match="does not exist"):
        await gateway.refresh()
    assert not (tmp_path / "mcp-manager.json").exists()


async def test_agent_fetches_only_bounded_media_capability(tmp_path):
    token = "a" * 43
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == f"/media/{token}":
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.end_headers()
                self.wfile.write(b"png-bytes")
            else:
                self.send_response(404)
                self.end_headers()
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gateway = LocalGateway(tmp_path, {}, transports={})
    gateway.media_endpoints["workspace"] = f"http://127.0.0.1:{server.server_port}"
    try:
        result = await gateway.dispatch("media_get", {"mcp": "workspace", "token": token})
        assert result == {"status": 200, "content_type": "image/png", "data": "cG5nLWJ5dGVz"}
        with pytest.raises(RoutingError, match="invalid media token"):
            await gateway.dispatch("media_get", {"mcp": "workspace", "token": "../secret"})
    finally:
        server.shutdown()
        server.server_close()


def test_cloud_media_route_uses_peer_and_durable_cache(tmp_path, monkeypatch):
    config = server_config(monkeypatch)
    cache = tmp_path / "media"
    cache.mkdir()
    config["media_cache_dir"] = str(cache)
    app = build_cloud_app(config, tmp_path)
    token, cached_token = "b" * 43, "c" * 43
    (cache / cached_token).write_bytes(b"cached-png")
    (cache / f"{cached_token}.json").write_text(json.dumps({
        "content_type": "image/png", "expires_at": time.time() + 3600}), encoding="utf-8")
    class Peer:
        async def call(self, method, params):
            assert method == "media_get" and params["token"] == token
            return {"status": 200, "content_type": "image/png", "data": "cGVlci1wbmc="}
    app.state.hub.peers["pc"] = Peer()
    with TestClient(app) as client:
        response = client.get(f"/media/{token}")
        assert response.status_code == 200 and response.content == b"peer-png"
        response = client.get(f"/media/{cached_token}")
        assert response.status_code == 200 and response.content == b"cached-png"
        assert client.get("/media/not-a-token").status_code == 404


async def test_registry_refresh_adds_and_revokes_mcp(tmp_path):
    gateway = LocalGateway(tmp_path, {"workspaces": {}}, transports={"extra": internal_server()})
    try:
        assert set((await gateway.refresh())["mcps"]) == {"extra"}
        gateway.transports = {}
        assert not (await gateway.refresh())["mcps"]
        assert not gateway.sessions
        with pytest.raises(RoutingError, match="unknown local MCP"):
            await gateway.dispatch("tool_call", {"mcp": "extra", "tool": "internal_action", "arguments": {"number": 1}})
        gateway.transports = {"new": internal_server()}
        assert set((await gateway.refresh())["mcps"]) == {"new"}
    finally:
        await gateway.close()


async def test_extra_stdio_sessions_are_isolated_and_reaped(tmp_path):
    script = tmp_path / "local_provider.py"
    script.write_text('''from fastmcp import FastMCP, Context
mcp = FastMCP("extra")
last_value = None
@mcp.tool
async def remember(value: str, ctx: Context) -> dict:
    global last_value
    previous = last_value
    last_value = value
    return {"previous": previous, "value": value}
mcp.run("stdio", show_banner=False)
''', encoding="utf-8")
    cloud = {"workspaces": {}, "session_idle_ttl_s": -1,
             "mcps": {"extra": {"command": [sys.executable, str(script)]}}}
    gateway = LocalGateway(tmp_path, cloud)
    try:
        await gateway.refresh()
        assert gateway.catalog["mcps"]["extra"]["tools"][0]["name"] == "remember"
        async def call(scope, value):
            return await gateway.dispatch("tool_call", {"mcp": "extra", "tool": "remember", "scope": scope,
                                                        "arguments": {"value": value}})
        a, b = await asyncio.gather(call("A", "one"), call("B", "two"))
        assert json.loads(a["content"][0]["text"])["previous"] is None
        assert json.loads(b["content"][0]["text"])["previous"] is None
        assert json.loads((await call("A", "three"))["content"][0]["text"])["previous"] == "one"
        await gateway.reap()
        assert set(gateway.sessions) == {("extra", "catalog")}
        assert json.loads((await call("A", "new"))["content"][0]["text"])["previous"] is None
    finally:
        await gateway.close()


def test_cloud_credentials_are_bound_to_instance(tmp_path, monkeypatch):
    app = build_cloud_app(server_config(monkeypatch), tmp_path)
    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ingress/pc-b", headers={"Authorization": "Bearer " + "a" * 40}):
                pass
        with pytest.raises(Exception):
            with client.websocket_connect("/ingress/pc-a", headers={"Authorization": "Bearer " + "a" * 40,
                                                                     "Origin": "https://untrusted.example"}):
                pass
        assert not app.state.hub.peers


def test_workspace_oauth_metadata_and_secret_file(tmp_path, monkeypatch):
    config=server_config(monkeypatch)
    secret_dir=tmp_path/'secrets/workspace'
    secret_dir.mkdir(parents=True)
    (secret_dir/'oauth-password').write_text('p'*40)
    (secret_dir/'bearer-token').write_text('b'*40)
    config.update(public_url='https://hub.example.test',stateless=True,log_requests=False,
        auth={'mode':'oauth','password_secret':'workspace/oauth-password',
              'bearer_secrets':['workspace/bearer-token'],'scopes':['tools'],
              'required_scopes':['tools'],'access_token_ttl_s':2592000,
              'subject':'operator','title':'Workspace MCP'})
    app=build_cloud_app(config,tmp_path)
    with TestClient(app,base_url='https://hub.example.test') as client:
        meta=client.get('/.well-known/oauth-authorization-server').json()
        assert meta['issuer']=='https://hub.example.test'
        assert meta['registration_endpoint']=='https://hub.example.test/register'
        assert meta['scopes_supported']==['tools']
        assert meta['code_challenge_methods_supported']==['S256']
        resource=client.get('/.well-known/oauth-protected-resource/mcp').json()
        assert resource['resource']=='https://hub.example.test/mcp'
        assert resource['resource_name']=='Workspace MCP'
        denied=client.post('/register',json={'redirect_uris':['https://invalid.example/callback']})
        assert denied.status_code==400 and denied.json()['error']=='invalid_redirect_uri'
    assert connection_settings({'agent_id':'pc','url':'wss://hub.example.test',
                                'token_secret':'workspace/bearer-token'},tmp_path)[2]=='b'*40


def test_workspace_example_accepts_claude_and_chatgpt_callbacks(tmp_path, monkeypatch):
    config = json.loads((Path(__file__).parents[1] / 'config/server.workspace.example.json').read_text(encoding='utf-8'))
    monkeypatch.setenv('ANX_OFFICE_TOKEN', 'a' * 40)
    config['auth']['store'] = str(tmp_path / 'oauth.sqlite3')
    secret_dir = tmp_path / 'secrets/workspace'
    secret_dir.mkdir(parents=True)
    (secret_dir / 'oauth-password').write_text('p' * 40)
    (secret_dir / 'bearer-token').write_text('b' * 40)
    with TestClient(build_cloud_app(config, tmp_path), base_url=config['public_url']) as client:
        for callback in ('https://claude.ai/api/mcp/auth_callback',
                         'https://chatgpt.com/connector/oauth/workspace-validation'):
            registered = client.post('/register', json={
                'redirect_uris': [callback], 'token_endpoint_auth_method': 'none',
                'grant_types': ['authorization_code', 'refresh_token'],
                'response_types': ['code'], 'scope': 'tools'})
            assert registered.status_code == 201, registered.text
            assert registered.json()['token_endpoint_auth_method'] == 'none'
            authorize = client.get('/authorize', params={
                'response_type': 'code', 'client_id': registered.json()['client_id'],
                'redirect_uri': callback, 'scope': 'tools', 'state': 'validation',
                'code_challenge': 'a' * 43, 'code_challenge_method': 'S256'},
                follow_redirects=False)
            assert authorize.status_code == 302
            assert '/login?' in authorize.headers['location']
        for callback in ('https://claude.ai.evil.invalid/api/mcp/auth_callback',
                         'https://claude.ai/api/mcp/auth_callback?redirect=evil',
                         'https://claude.ai/other',
                         'http://claude.ai/api/mcp/auth_callback'):
            denied = client.post('/register', json={
                'redirect_uris': [callback], 'token_endpoint_auth_method': 'none'})
            assert denied.status_code == 400
            assert denied.json()['error'] == 'invalid_redirect_uri'


async def test_parallel_reads_share_workspace_without_blocking_or_response_mixup(tmp_path, monkeypatch):
    """Exercise HTTP, reverse WebSocket and the real FastMCP client concurrently."""
    from collections import Counter

    config = server_config(monkeypatch)
    config["rpc_timeout_s"] = 5
    app = build_cloud_app(config, tmp_path)
    upstream = FastMCP("parallel workspace fixture")
    calls = Counter()
    slow_started = asyncio.Event()

    @upstream.tool
    async def parallel_read(value: str, workdir: str = "", wait_ms: int = 0) -> dict:
        calls[value] += 1
        if value == "cancelled":
            slow_started.set()
        if wait_ms:
            await asyncio.sleep(wait_ms / 1000)
        return {"value": value, "workdir": workdir}

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                          log_level="error", access_log=False))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    agent_task = None
    slow = None
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(.025)
        assert server.started
        ready = asyncio.Event()
        agent_task = asyncio.create_task(run_agent(tmp_path, {
            "agent_id": "pc-a", "url": f"ws://127.0.0.1:{port}", "token_env": "A_TOKEN",
            "rpc_timeout_s": 5, "workspaces": {"project": {"path": str(tmp_path)}}},
            transports={"workspace": upstream}, ready=ready))
        await asyncio.wait_for(ready.wait(), 15)

        def client():
            return Client(StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp", auth="s" * 40),
                          timeout=10)

        async with client() as main, client() as branch:
            selected = (await main.call_tool("set_workspace",
                                            {"agent_id": "pc-a", "workspace": "project"})).data
            other = (await branch.call_tool("set_workspace",
                                           {"agent_id": "pc-a", "workspace": "project"})).data
            handle = selected["workspace_id"]
            slow = asyncio.create_task(branch.call_tool("parallel_read", {
                "value": "cancelled", "wait_ms": 1500, "workspace_id": handle}))
            await asyncio.wait_for(slow_started.wait(), 5)
            requests = [
                (main if index % 2 else branch).call_tool("parallel_read", {
                    "value": f"read-{index}", "wait_ms": (index % 4) * 15,
                    "workspace_id": handle if index < 20 else other["workspace_id"]})
                for index in range(24)
            ]
            slow.cancel()
            with pytest.raises(asyncio.CancelledError):
                await slow
            replies = await asyncio.wait_for(asyncio.gather(*requests), 5)
            assert [reply.data for reply in replies] == [
                {"value": f"read-{index}", "workdir": str(tmp_path)} for index in range(24)]
            followup = await main.call_tool("parallel_read", {"value": "after", "workspace_id": handle})
            assert followup.data == {"value": "after", "workdir": str(tmp_path)}
            assert calls == Counter({**{f"read-{index}": 1 for index in range(24)},
                                     "cancelled": 1, "after": 1})
    finally:
        if slow is not None and not slow.done():
            slow.cancel()
            await asyncio.gather(slow, return_exceptions=True)
        if agent_task is not None:
            agent_task.cancel()
            await asyncio.gather(agent_task, return_exceptions=True)
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()

