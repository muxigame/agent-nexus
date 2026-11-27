"""Gateway side of the agent chat (agent_nexus/agents_bridge.py, gateway.agent_handle, mailbox.conversation).

Modeled on the terminal bridge tests: a fake device on /ingress answers the agent_* reverse RPCs.
"""
from __future__ import annotations

import threading
import time

from starlette.testclient import TestClient
from test_console_m16 import ORIGIN, SERVER_TOKEN, login, oauth_config
from test_remote_m19 import AS_A, TOKEN_A, TOKEN_B, hub_call, network_config

from agent_nexus import agents_bridge
from agent_nexus.cloud import build_cloud_app

OWNER = {"Authorization": f"Bearer {SERVER_TOKEN}"}
CATALOG = {"workspaces": {"default": {"path": "/srv/work", "mcp": "device"},
                          "app": {"path": "/srv/work/app", "mcp": "device"}},
           "mcps": {"device": {"tools": []}}}


class AgentDevice:
    """A device on /ingress answering agent_* (``slow`` ops wait ``delay`` seconds first)."""

    def __init__(self, ws, delay: float = 0.0):
        self.ws, self.delay = ws, delay
        self.lock = threading.Lock()
        self.calls: list[tuple[str, dict]] = []
        self.answered: list[str] = []
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                message = self.ws.receive_json()
            except Exception:  # noqa: BLE001 - the socket closed: the test is over
                return
            if "method" in message and "id" in message:
                threading.Thread(target=self.answer, args=(message,), daemon=True).start()
            elif "method" in message:  # notices, e.g. a cancel
                self.calls.append((message["method"], message.get("params", {})))

    def answer(self, message):
        method, params = message["method"], message.get("params", {})
        self.calls.append((method, params))
        if method == "agent_run_read" and params.get("slow"):
            time.sleep(self.delay)
        if method == "agent_info":
            result = {"agents": [{"kind": "codex", "installed": True, "login": "ok"}]}
        elif method == "agent_run_read":
            result = {"error": "这个对话已经结束或不存在", "code": "not_found"} if params.get("rid") == "gone" else \
                {"events": [{"seq": 1, "type": "message", "text": "hi"}], "cursor": 1, "state": "idle"}
        else:
            result = {"ok": True, "op": method}
        with self.lock:
            try:
                self.ws.send_json({"id": message["id"], "result": result})
                self.answered.append(method)
            except Exception:  # noqa: BLE001, S110 - the socket closed: the test is over
                pass


def connect(client, agent_id, token, features, catalog=None):
    ws = client.websocket_connect(f"/ingress/{agent_id}", headers={"Authorization": f"Bearer {token}"}).__enter__()
    ws.send_json({"protocol": 1, "agent_id": agent_id, "catalog": catalog or {"workspaces": {}, "mcps": {}},
                  "anx": {"version": "0.9.0", "features": features}})
    ready = ws.receive_json()
    assert ready["method"] == "ready" and "agents/1" in ready["params"]["anx"]["features"]
    return ws


def test_owner_calls_forward_to_the_device_and_only_allowlisted_ops(tmp_path, monkeypatch):
    with TestClient(build_cloud_app(network_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        ws = connect(client, "pc-b", TOKEN_B, ["agents/1"])
        device = AgentDevice(ws)
        try:
            url = "/api/devices/pc-b/agents"
            assert client.post(f"{url}/agent_info", json={}).status_code == 401
            info = client.post(f"{url}/agent_info", json={"fresh": True}, headers=OWNER)
            assert info.status_code == 200 and info.json()["agents"][0]["kind"] == "codex"
            assert ("agent_info", {"fresh": True}) in device.calls
            # a handler's own error comes back as an answer
            gone = client.post(f"{url}/agent_run_read", json={"rid": "gone", "wait": 20}, headers=OWNER)
            assert gone.status_code == 200 and gone.json()["code"] == "not_found"
            # ops outside agents/1 never reach the device
            for op in ("term_open", "fx_read", "plugin_repo_install", "agent_handle"):
                refused = client.post(f"{url}/{op}", json={}, headers=OWNER)
                assert refused.status_code == 404 and refused.json()["code"] == "unknown_op"
            assert not [m for m, _ in device.calls if not m.startswith("agent_")]
            assert client.post(f"{url}/agent_info", content=b"[]", headers=OWNER).status_code == 400
            empty = client.post(f"{url}/agent_run_list", headers=OWNER)
            assert empty.status_code == 200 and device.calls[-1] == ("agent_run_list", {})
            offline = client.post("/api/devices/pc-a/agents/agent_info", json={}, headers=OWNER)
            assert offline.status_code == 409 and offline.json()["code"] == "unavailable"
        finally:
            ws.__exit__(None, None, None)


def test_cookie_needs_the_console_header_and_old_devices_are_named(tmp_path, monkeypatch):
    with TestClient(build_cloud_app(oauth_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        ws = connect(client, "pc-main", "a" * 40, [])  # a client without agents/1
        try:
            assert login(client).status_code == 303
            url = "/api/devices/pc-main/agents/agent_info"
            assert client.post(url, json={}).status_code == 401  # cookie without X-ANX: 1 (CSRF)
            old = client.post(url, json={}, headers={"X-ANX": "1"})
            assert old.status_code == 409 and "Agent 对话" in old.json()["error"]
        finally:
            ws.__exit__(None, None, None)


def test_devices_call_with_their_credential_until_locked(tmp_path, monkeypatch):
    with TestClient(build_cloud_app(network_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        a = connect(client, "pc-a", TOKEN_A, [])
        b = connect(client, "pc-b", TOKEN_B, ["agents/1"])
        device = AgentDevice(b)
        try:
            url = "/api/devices/pc-b/agents/agent_sessions"
            assert client.post(url, json={}, headers={"Authorization": f"Bearer {TOKEN_A}"}).status_code == 401
            assert client.post(url, json={"kind": "codex"}, headers=dict(AS_A)).json() == {"ok": True, "op": "agent_sessions"}
            assert ("agent_sessions", {"kind": "codex"}) in device.calls
            assert hub_call(a, "lock_report", {"locked": True})["result"] == {"locked": True}
            locked = client.post(url, json={}, headers=dict(AS_A))
            assert locked.status_code == 423 and locked.json()["code"] == "locked"
        finally:
            a.__exit__(None, None, None)
            b.__exit__(None, None, None)


def test_a_new_console_run_never_takes_over_an_idle_live_runs_address(tmp_path, monkeypatch):
    """Two chats in one folder: the first one sitting idle between turns keeps its mailbox (e2e finding)."""
    with TestClient(build_cloud_app(network_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        a = connect(client, "pc-a", TOKEN_A, ["agents/1"], CATALOG)
        try:
            gateway = client.app.state.gateway
            first = hub_call(a, "agent_handle", {"device_id": "pc-a", "workdir": "/srv/work/app", "kind": "codex"})["result"]
            with gateway.store._lock:  # its handle last used two hours ago: idle, but not expired
                gateway.mailbox._db().execute("UPDATE handles SET expires_at = expires_at - 7200 WHERE handle = ?",
                                              (first["workspace_id"],))
            second = hub_call(a, "agent_handle", {"device_id": "pc-a", "workdir": "/srv/work/app", "kind": "codex"})["result"]
            assert second["address"] != first["address"]
            assert gateway.store.touch_handle(first["workspace_id"]) is not None
            assert gateway.mailbox.address_row(first["address"])["last_handle"] == first["workspace_id"]
        finally:
            a.__exit__(None, None, None)


def test_a_slow_device_call_times_out_without_being_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(agents_bridge, "call_timeout", lambda op, params: 0.3)
    with TestClient(build_cloud_app(network_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        ws = connect(client, "pc-b", TOKEN_B, ["agents/1"])
        device = AgentDevice(ws, delay=1.0)
        try:
            slow = client.post("/api/devices/pc-b/agents/agent_run_read", json={"rid": "r", "slow": True}, headers=OWNER)
            assert slow.status_code == 504 and slow.json()["code"] == "timeout"
            deadline = time.monotonic() + 5
            while "agent_run_read" not in device.answered and time.monotonic() < deadline:
                time.sleep(0.05)
            assert "agent_run_read" in device.answered
            # the link is still up: the device was not retired by a cancel
            again = client.post("/api/devices/pc-b/agents/agent_info", json={}, headers=OWNER)
            assert again.status_code == 200
            assert not [c for c in device.calls if c[0] == "cancel"]
        finally:
            ws.__exit__(None, None, None)


def test_agent_handle_binds_an_address_and_the_conversation_shows_mail_and_asks(tmp_path, monkeypatch):
    with TestClient(build_cloud_app(network_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        a = connect(client, "pc-a", TOKEN_A, ["agents/1"], CATALOG)
        b = connect(client, "pc-b", TOKEN_B, ["agents/1"], CATALOG)
        try:
            # the device asks for its own run (hub_call), in a folder under a registered workdir
            bound = hub_call(a, "agent_handle", {"device_id": "pc-a", "workdir": "/srv/work/app/sub", "kind": "codex"})
            result = bound["result"]
            assert result["workspace"] == "app" and result["match"] == "inside"
            assert result["address"].startswith("codex-sub") and result["address"].endswith("@pc-a")
            assert result["workspace_id"] and result["path"] == "/srv/work/app/sub"
            gateway = client.app.state.gateway
            record = gateway.store.touch_handle(result["workspace_id"])
            assert record["agent_id"] == "pc-a" and record["workspace"] == "app"
            row = gateway.mailbox.address_row(result["address"])
            assert row["kind"] == "codex" and row["wake_policy"] == "never"
            # dsh / gemini are session kinds now; a chosen name sticks
            named = hub_call(a, "agent_handle", {"device_id": "pc-a", "workdir": "/elsewhere", "kind": "dsh",
                                                 "name": "reviewer"})["result"]
            assert named["address"] == "reviewer@pc-a" and named["match"] == "default"
            assert gateway.mailbox.address_row("reviewer@pc-a")["kind"] == "dsh"

            # the owner's console can ask too; a locked device may not bind on another device
            owner = client.post("/api/agents/handle", json={"device_id": "pc-b", "workdir": "/srv/work", "kind": "claude"},
                                headers=OWNER)
            assert owner.status_code == 200 and owner.json()["match"] == "exact"
            assert client.post("/api/agents/handle", json={"device_id": "pc-b"}).status_code == 401
            assert hub_call(a, "lock_report", {"locked": True})["result"] == {"locked": True}
            refused = hub_call(a, "agent_handle", {"device_id": "pc-b", "workdir": "/srv/work", "kind": "codex"})
            assert "locked" in refused["error"]
            assert "result" in hub_call(a, "agent_handle", {"workdir": "/srv/work", "kind": "codex"})  # its own: fine

            # mail both ways and an ask, then the conversation in time order
            address = result["address"]
            mailbox = gateway.mailbox
            mailbox.send(address, "me", "进度：测试都过了", subject="进度")
            mailbox.send("me", address, "好的，继续", subject="回复")
            mailbox.send(address, "someone@pc-b", "不相关", subject="x") if mailbox.address_row("someone@pc-b") else None
            asked = mailbox.ask_create(address, "要不要推送？", options=["推送", "先别"])
            conversation = client.get(f"/api/inbox/conversation?address={address}", headers=OWNER)
            assert conversation.status_code == 200
            body = conversation.json()
            assert [m["direction"] for m in body["messages"]][:2] == ["in", "out"]
            assert body["messages"][0]["body"] == "进度：测试都过了"
            assert [a_["ask_id"] for a_ in body["asks"]] == [asked["ask_id"]]
            assert any(m.get("ask_id") == asked["ask_id"] for m in body["messages"])
            # the display form works too, and unknown addresses are a 404
            display = mailbox.display(address)
            assert client.get(f"/api/inbox/conversation?address={display}", headers=OWNER).json()["address"] == address
            assert client.get("/api/inbox/conversation?address=nobody@pc-a", headers=OWNER).status_code == 404
            # the local console's route: hub_call mail_local op=conversation
            local = hub_call(b, "mail_local", {"op": "conversation", "address": address, "mark_read": True})["result"]
            assert local["address"] == address and local["unread"] == 0
            assert mailbox.unread_count("me") == 0 or all(m["state"] == "read" for m in local["messages"]
                                                          if m["direction"] == "in")
        finally:
            a.__exit__(None, None, None)
            b.__exit__(None, None, None)


def test_a_released_console_run_frees_its_address_for_the_next_chat(tmp_path, monkeypatch):
    with TestClient(build_cloud_app(network_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        a = connect(client, "pc-a", TOKEN_A, ["agents/1"], CATALOG)
        b = connect(client, "pc-b", TOKEN_B, ["agents/1"], CATALOG)
        try:
            gateway = client.app.state.gateway
            first = hub_call(a, "agent_handle", {"device_id": "pc-a", "workdir": "/srv/work/app", "kind": "codex"})["result"]
            assert "error" in hub_call(b, "agent_release", {"workspace_id": first["workspace_id"]}), "not b's handle"
            released = hub_call(a, "agent_release", {"workspace_id": first["workspace_id"]})["result"]
            assert released == {"released": True, "address": first["address"], "ended": True}
            assert gateway.store.touch_handle(first["workspace_id"]) is None
            assert gateway.mailbox.address_row(first["address"])["ended_at"]
            second = hub_call(a, "agent_handle", {"device_id": "pc-a", "workdir": "/srv/work/app", "kind": "codex"})["result"]
            assert second["address"] == first["address"], "no claude-proj-2, -3 … drift"
            assert hub_call(a, "agent_release", {"workspace_id": "unknown"})["result"] == {"released": False}
        finally:
            a.__exit__(None, None, None)
            b.__exit__(None, None, None)
