"""本机 MCP (agent_nexus/local_mcp.py): the guard, the local answers while not ready, the pass-through."""
from __future__ import annotations

import asyncio
import json
import types

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_nexus.local_mcp import LocalMcp, is_loopback
from agent_nexus.web import LocalOnly

PORT = 8399
HOST = f"127.0.0.1:{PORT}"
JSON = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


def make(tmp_path, *, state="connected", features=("mcp_device/1",), locked=False, host="127.0.0.1",
         mcp_local=None, handler=None, accounts=None):
    cfg = types.SimpleNamespace(host=host, port=PORT, root=tmp_path, mcp_local=mcp_local or {})
    cloud_status = {"state": state, "gateway_mcp_path": "/mcp"}
    hub = types.SimpleNamespace(available=state == "connected", features=frozenset(features))
    lock = types.SimpleNamespace(locked=locked)
    seen: list[httpx.Request] = []

    def default(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream", "mcp-session-id": "s1",
                                            "set-cookie": "nope=1"},
                              content=b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n')

    def wrapped(request):
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped if handler else default)
    client = httpx.AsyncClient(transport=transport)
    mcp = LocalMcp(cfg, cloud_status, hub, lock, credentials=lambda root: ("https://gw.example", "pc-a", "tok" * 12),
                   http=lambda: client, accounts=accounts)
    return mcp, seen


def call(app, method="POST", path="/mcp", body=INIT, headers=None, client=("127.0.0.1", 50123)):
    async def go():
        transport = httpx.ASGITransport(app=app, client=client)
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{HOST}") as http:
            content = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
            return await http.request(method, path, content=content, headers={**JSON, **(headers or {})})
    return asyncio.run(go())


def test_loopback_names():
    assert is_loopback("127.0.0.1") and is_loopback("127.5.0.1") and is_loopback("::1") and is_loopback("[::1]")
    assert is_loopback("::ffff:127.0.0.1") and is_loopback("localhost")
    assert not is_loopback("0.0.0.0") and not is_loopback("10.0.0.5") and not is_loopback("evil.example")


def test_the_guard_refuses_browsers_foreign_hosts_and_other_machines(tmp_path):
    mcp, _ = make(tmp_path, state="disabled")
    assert call(mcp, headers={"Host": f"evil:{PORT}"}).status_code == 403
    assert call(mcp, headers={"Host": f"0.0.0.0:{PORT}"}).status_code == 403
    assert call(mcp, headers={"Origin": "https://evil"}).status_code == 403
    assert call(mcp, headers={"Origin": "null"}).status_code == 403
    assert call(mcp, headers={"Origin": f"http://127.0.0.1:{PORT}"}).status_code == 200
    assert call(mcp, headers={"Content-Type": "text/plain"}).status_code == 415
    got = call(mcp, method="GET", body=b"")
    assert got.status_code == 405 and got.headers["allow"] == "POST, DELETE"
    assert call(mcp, client=("10.0.0.5", 1234)).status_code == 403
    assert call(mcp, body=b"x" * (8 * 1024 * 1024 + 10)).status_code == 413
    assert call(mcp).status_code == 200


def test_off_and_exposed(tmp_path):
    off, _ = make(tmp_path, mcp_local={"enabled": False})
    assert call(off).status_code == 404 and off.status()["state"] == "off"
    exposed, _ = make(tmp_path, host="0.0.0.0")
    assert call(exposed).status_code == 403 and exposed.status()["state"] == "exposed"
    assert "manager.host" in exposed.status()["reason"]


def test_peers_accounts_lets_only_known_users_in(tmp_path, monkeypatch):
    mcp, _ = make(tmp_path, state="disabled", mcp_local={"peers": "accounts"}, accounts=lambda: {"user"})
    monkeypatch.setattr(mcp, "peer", lambda scope: {"pid": 42, "user": "user"})
    assert call(mcp).status_code == 200
    monkeypatch.setattr(mcp, "peer", lambda scope: {"pid": 43, "user": "WORKGROUP\\Guest"})
    assert call(mcp).status_code == 403
    monkeypatch.setattr(mcp, "peer", lambda scope: {})
    assert call(mcp).status_code == 403


def test_local_only_leaves_mcp_to_its_own_guard(tmp_path):
    mcp, _ = make(tmp_path, state="disabled")

    async def api(_request):
        return JSONResponse({"ok": True})
    app = LocalOnly(Starlette(routes=[Route("/mcp", mcp), Route("/api/x", api, methods=["POST"])]), "127.0.0.1", PORT)
    assert call(app, path="/api/x", body={}).status_code == 403  # no X-ANX
    assert call(app, path="/api/x", body={}, headers={"X-ANX": "1"}).status_code == 200
    assert call(app).status_code == 200  # an MCP client sends no X-ANX
    assert call(app, headers={"Host": f"evil:{PORT}"}).status_code == 403


@pytest.mark.parametrize("state,kwargs,needle", [
    ("not_joined", {"state": "disabled"}, "还没加入网关"),
    ("offline", {"state": "connecting"}, "网关未连接"),
    ("revoked", {"state": "revoked"}, "吊销"),
    ("locked", {"locked": True}, "已锁定"),
    ("gateway_old", {"features": ()}, "网关版本过旧"),
])
def test_not_ready_answers_locally_with_only_the_status_tool(tmp_path, state, kwargs, needle):
    mcp, seen = make(tmp_path, **kwargs)
    assert mcp.state() == state
    init = call(mcp).json()
    assert init["result"]["serverInfo"]["name"] == "Agent Nexus" and needle in init["result"]["instructions"]
    listed = call(mcp, body={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()
    assert [t["name"] for t in listed["result"]["tools"]] == ["nexus_status"]
    other = call(mcp, body={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "device_list", "arguments": {}}})
    assert other.status_code == 200 and other.json()["result"]["isError"] is True
    assert needle in other.json()["result"]["content"][0]["text"]
    status = call(mcp, body={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                             "params": {"name": "nexus_status", "arguments": {}}}).json()
    assert status["result"]["isError"] is False and state in status["result"]["content"][0]["text"]
    note = call(mcp, body={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202
    batch = call(mcp, body=[{"jsonrpc": "2.0", "id": 5, "method": "ping"}, {"jsonrpc": "2.0", "id": 6, "method": "x/y"}]).json()
    assert batch[0]["result"] == {} and batch[1]["error"]["code"] == -32001
    assert not seen  # nothing went to the gateway


def test_ready_passes_through_with_the_device_credential_only(tmp_path):
    mcp, seen = make(tmp_path)
    assert mcp.state() == "ready"
    got = call(mcp, headers={"Authorization": "Bearer stolen", "Cookie": "a=b", "Origin": f"http://localhost:{PORT}",
                             "X-ANX-Device": "pc-evil", "Mcp-Protocol-Version": "2025-06-18"})
    assert got.status_code == 200 and got.headers["content-type"].startswith("text/event-stream")
    assert got.headers["mcp-session-id"] == "s1" and "set-cookie" not in got.headers
    assert b'"ok":true' in got.content
    request = seen[-1]
    assert str(request.url) == "https://gw.example/mcp/device"
    assert request.headers["authorization"] == "Bearer " + "tok" * 12
    assert request.headers["x-anx-device"] == "pc-a"
    assert "cookie" not in request.headers and "origin" not in request.headers
    assert request.headers["host"] == "gw.example" and request.headers["mcp-protocol-version"] == "2025-06-18"
    assert json.loads(request.content) == INIT


@pytest.mark.parametrize("status,body,needle", [
    (401, {"code": "device_credential"}, "不接受这台设备的凭据"),
    (403, {"code": "mcp_devices_off"}, "关闭了设备 MCP"),
    (404, {}, "网关版本过旧"),
    (409, {"code": "offline"}, "网关未连接"),
    (423, {"code": "locked"}, "已锁定"),
])
def test_upstream_refusals_become_local_answers_never_a_401(tmp_path, status, body, needle):
    mcp, _ = make(tmp_path, handler=lambda request: httpx.Response(status, json=body))
    got = call(mcp)
    assert got.status_code == 200 and needle in got.json()["result"]["instructions"]
    if status == 403:
        assert mcp.state() == "device_access_off"


def test_network_errors_read_as_offline(tmp_path):
    def broken(request):
        raise httpx.ConnectError("down")
    mcp, _ = make(tmp_path, handler=broken)
    got = call(mcp, body={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "x", "arguments": {}}})
    assert got.status_code == 200 and "网关未连接" in got.json()["result"]["content"][0]["text"]


def test_the_audit_line_has_no_arguments(tmp_path):
    mcp, _ = make(tmp_path, state="disabled")
    call(mcp, body={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "file_read", "arguments": {"path": "/secret/plan.txt",
                                                                  "workspace_id": "0123456789abcdef"}}})
    line = (tmp_path / ".anx" / "logs" / "local-mcp.jsonl").read_text().strip()
    entry = json.loads(line)
    assert entry["tool"] == "file_read" and entry["ws"] == "01234567" and entry["state"] == "not_joined"
    assert "secret" not in line and "plan" not in line
