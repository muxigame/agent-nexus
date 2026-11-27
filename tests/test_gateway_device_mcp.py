"""The gateway's device MCP (``/mcp/device``, feature ``mcp_device/1``): the same MCP as ``/mcp`` for a joined device
with its own credential, never an owner (agent_nexus/device_mcp.py, docs/design.md "本机 MCP")."""
from __future__ import annotations

import json
import queue
import threading
import time

from fastmcp.server.dependencies import get_access_token
from starlette.testclient import TestClient
from test_agents_gateway import CATALOG
from test_console_m16 import ORIGIN, SERVER_TOKEN
from test_remote_m19 import TOKEN_A, TOKEN_B, hub_call, network_config

from agent_nexus.cloud import build_cloud_app

OWNER = {"Authorization": f"Bearer {SERVER_TOKEN}"}
AS_A = {"Authorization": f"Bearer {TOKEN_A}", "X-ANX-Device": "pc-a"}
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
DEVICE_CATALOG = {**CATALOG, "mcps": {"device": {"tools": [
    {"name": "file_read", "description": "Read a file", "inputSchema": {
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}]}}}


def connect(client, agent_id, token, catalog=None):
    ws = client.websocket_connect(f"/ingress/{agent_id}", headers={"Authorization": f"Bearer {token}"}).__enter__()
    ws.send_json({"protocol": 1, "agent_id": agent_id, "catalog": catalog or {"workspaces": {}, "mcps": {}},
                  "anx": {"version": "0.9.0", "features": ["agents/1"]}})
    ready = ws.receive_json()
    assert ready["method"] == "ready"
    return ws, ready["params"]["anx"]


class ToolDevice:
    """Answers the gateway's tool_call reverse RPCs with a fixed text."""

    def __init__(self, ws):
        self.ws = ws
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        self.replies: "queue.Queue[dict]" = queue.Queue()
        threading.Thread(target=self.serve, daemon=True).start()

    def hub_call(self, op, args):
        with self.lock:
            self.ws.send_json({"id": f"d-{time.monotonic_ns()}", "method": "hub_call", "params": {"op": op, "args": args}})
        return self.replies.get(timeout=10)

    def serve(self):
        while True:
            try:
                message = self.ws.receive_json()
            except Exception:  # noqa: BLE001 - socket closed
                return
            if message.get("method") == "tool_call":
                self.calls.append(message["params"])
                with self.lock:
                    self.ws.send_json({"id": message["id"], "result": {
                        "content": [{"type": "text", "text": f"read {message['params']['arguments'].get('path')}"}],
                        "isError": False}})
            elif "id" in message and "method" in message:
                with self.lock:
                    self.ws.send_json({"id": message["id"], "result": {}})
            elif "id" in message:
                self.replies.put(message)


def rpc(client, path, method, params=None, headers=None, rid=1):
    body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
    response = client.post(path, content=json.dumps(body), headers={**ACCEPT, **(headers or {})})
    if response.status_code != 200:
        return response, None
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
        return response, json.loads(data[-1])
    return response, response.json()


INIT = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}


def tool_names(client, path, headers):
    response, init = rpc(client, path, "initialize", INIT, headers)
    assert response.status_code == 200, response.text
    response, listed = rpc(client, path, "tools/list", {}, headers, rid=2)
    assert response.status_code == 200, response.text
    return sorted(tool["name"] for tool in listed["result"]["tools"]), init


def test_a_device_gets_the_same_mcp_as_the_owner_with_its_own_identity(tmp_path, monkeypatch):
    app = build_cloud_app(network_config(tmp_path, monkeypatch), tmp_path)
    seen: dict = {}

    @app.state.mcp.tool
    def whoami() -> str:
        """Report the caller (test only)."""
        token = get_access_token()
        seen["client_id"] = token.client_id if token else None
        return seen["client_id"] or ""

    with TestClient(app, base_url=ORIGIN) as client:
        a, anx = connect(client, "pc-a", TOKEN_A, DEVICE_CATALOG)
        device = ToolDevice(a)
        try:
            assert "mcp_device/1" in anx["features"]
            assert anx["mcp_path"] == "/mcp" and anx["device_mcp"] is True
            owner_tools, _ = tool_names(client, "/mcp", OWNER)
            device_tools, init = tool_names(client, "/mcp/device", AS_A)
            assert device_tools == owner_tools
            assert {"device_list", "use_device", "mail_send", "ask_user", "file_copy", "file_read"} <= set(device_tools)
            assert "device_list" in init["result"]["instructions"]

            response, called = rpc(client, "/mcp/device", "tools/call", {"name": "whoami", "arguments": {}}, AS_A, 3)
            assert response.status_code == 200 and seen["client_id"] == "device:pc-a"
            response, listed = rpc(client, "/mcp/device", "tools/call", {"name": "device_list", "arguments": {}}, AS_A, 4)
            assert "pc-a" in json.dumps(listed, ensure_ascii=False)

            # a console run's handle works as workspace_id at once: device tools and mail as the run
            bound = device.hub_call("agent_handle", {"device_id": "pc-a", "workdir": "/srv/work/app", "kind": "codex"})["result"]
            handle = bound["workspace_id"]
            response, read = rpc(client, "/mcp/device", "tools/call",
                                 {"name": "file_read", "arguments": {"path": "/srv/work/app/x", "workspace_id": handle}},
                                 AS_A, 5)
            assert read["result"]["isError"] is False, read
            assert device.calls and device.calls[-1]["arguments"] == {"path": "/srv/work/app/x"}
            response, sent = rpc(client, "/mcp/device", "tools/call",
                                 {"name": "mail_send", "arguments": {"to": "me", "body": "hi", "workspace_id": handle}},
                                 AS_A, 6)
            assert sent["result"]["isError"] is False, sent

            # use_device through a device records who asked
            response, used = rpc(client, "/mcp/device", "tools/call",
                                 {"name": "use_device", "arguments": {"device_id": "pc-a", "workdir": "app"}}, AS_A, 7)
            text = used["result"]["content"][0]["text"]
            new_handle = json.loads(text)["workspace_id"]
            record = app.state.store.touch_handle(new_handle)
            assert record and (record.get("meta") or {}).get("via") == "device:pc-a"
        finally:
            a.__exit__(None, None, None)


def test_the_device_mcp_refuses_without_a_valid_unlocked_online_device(tmp_path, monkeypatch):
    app = build_cloud_app(network_config(tmp_path, monkeypatch), tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        # offline: the credential is right but the device is not connected
        response, _ = rpc(client, "/mcp/device", "initialize", INIT, AS_A)
        assert response.status_code == 409 and response.json()["code"] == "offline"
        a, _ = connect(client, "pc-a", TOKEN_A)
        try:
            # the owner bearer without X-ANX-Device is not a device
            response, _ = rpc(client, "/mcp/device", "initialize", INIT, OWNER)
            assert response.status_code == 401 and "www-authenticate" not in response.headers
            # a wrong token for the claimed device
            response, _ = rpc(client, "/mcp/device", "initialize", INIT,
                              {"Authorization": f"Bearer {TOKEN_B}", "X-ANX-Device": "pc-a"})
            assert response.status_code == 401 and response.json()["code"] == "device_credential"
            assert "www-authenticate" not in response.headers
            # the device credential is not the owner anywhere else
            assert rpc(client, "/mcp", "initialize", INIT, AS_A)[0].status_code == 401
            assert client.get("/api/settings", headers=AS_A).status_code == 401
            assert client.get("/api/inbox/summary", headers=AS_A).status_code in (401, 403, 404)
            # the owner's switch
            assert client.put("/api/settings", json={"mcp_devices": False}, headers=OWNER).json() == {"mcp_devices": False}
            response, _ = rpc(client, "/mcp/device", "initialize", INIT, AS_A)
            assert response.status_code == 403 and response.json()["code"] == "mcp_devices_off"
            # a caller without a valid device credential never learns the switch's position
            response, _ = rpc(client, "/mcp/device", "initialize", INIT,
                              {"Authorization": "Bearer guess", "X-ANX-Device": "pc-a"})
            assert response.status_code == 401 and response.json()["code"] == "device_credential"
            assert client.get("/api/settings", headers=OWNER).json()["mcp_devices"] is False
            client.put("/api/settings", json={"mcp_devices": True}, headers=OWNER)
            assert rpc(client, "/mcp/device", "initialize", INIT, AS_A)[0].status_code == 200
            # locked: no gateway tools at all
            assert hub_call(a, "lock_report", {"locked": True})["result"] == {"locked": True}
            response, _ = rpc(client, "/mcp/device", "initialize", INIT, AS_A)
            assert response.status_code == 423 and response.json()["code"] == "locked"
            hub_call(a, "lock_report", {"locked": False})
        finally:
            a.__exit__(None, None, None)


def test_a_revoked_device_is_refused(tmp_path, monkeypatch):
    app = build_cloud_app(network_config(tmp_path, monkeypatch), tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        a, _ = connect(client, "pc-a", TOKEN_A)
        try:
            assert rpc(client, "/mcp/device", "initialize", INIT, AS_A)[0].status_code == 200
            monkeypatch.setattr(app.state.enrollment, "check_token", lambda agent_id, token: "revoked")
            # agents-map devices are not revocable per device: drop it from the map to simulate the revocation
            import agent_nexus.device_mcp as device_mcp
            route = next(r for r in app.router.routes if getattr(r, "path", "") == "/mcp/device")
            assert isinstance(route.endpoint, device_mcp.DeviceMcpAuth)
            route.endpoint.check = lambda agent_id, token: False
            response, _ = rpc(client, "/mcp/device", "initialize", INIT, AS_A)
            assert response.status_code == 401 and "www-authenticate" not in response.headers
        finally:
            a.__exit__(None, None, None)
