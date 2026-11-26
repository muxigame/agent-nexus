"""The local console's agent chat routes (agent_nexus/web.py, docs/agent-chat.md §9) and the relay helper.

POST /api/agents/local/{op} runs the in-process AgentHub; POST /api/agents/remote/{device_id}/{op} goes through
the gateway with this device's credential (remote.agents_call) and is refused while the client is locked.
"""
from __future__ import annotations

import json
import socket
import sys
import time

import httpx
import pytest
from starlette.testclient import TestClient
from test_agents_hub import shim

from agent_nexus import agent_cli, remote
from agent_nexus.agents import registry
from agent_nexus.agents.hub import OPS

WRITE = {"X-ANX": "1"}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _config(root, **extra) -> None:
    data = {"manager": {"port": _free_port()}, "builtins": False, "scan": [], "cloud": {"enabled": False}, **extra}
    (root / "agent-nexus.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def local(tmp_path, monkeypatch):
    from agent_nexus.supervisor import Supervisor
    from agent_nexus.web import build_app
    home, bin_dir, root = tmp_path / "home", tmp_path / "bin", tmp_path / "root"
    for folder in (home / "proj", bin_dir, root):
        folder.mkdir(parents=True)
    shim(bin_dir, "codex", "codex")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(agent_cli, "candidates", lambda name, env: [])
    registry.invalidate()
    _config(root)
    sup = Supervisor(root)
    with TestClient(build_app(sup), base_url=sup.cfg.base_url) as client:
        yield client, root, home


def post(client, path, body=None, headers=WRITE):
    return client.post(path, json=body if body is not None else {}, headers=headers)


@pytest.mark.skipif(sys.platform == "win32", reason="the fake CLIs are /bin/sh shims")
def test_local_ops_run_in_process_without_a_gateway(local):
    client, _root, home = local
    info = post(client, "/api/agents/local/agent_info").json()
    codex = next(a for a in info["agents"] if a["kind"] == "codex")
    assert codex["installed"] and codex["version"] == "2.3.4"

    opened = post(client, "/api/agents/local/agent_run_open",
                  {"kind": "codex", "workdir": str(home / "proj"), "prompt": "hello"}).json()
    assert "error" not in opened, opened
    assert opened["gateway"] is False and "ask_user" in opened["note"]  # not joined: no mailbox, chat still works
    events, cursor, deadline = [], 0, time.monotonic() + 15
    while time.monotonic() < deadline and not any(e["type"] == "status" and e["state"] == "done" for e in events):
        read = post(client, "/api/agents/local/agent_run_read", {"rid": opened["rid"], "cursor": cursor, "wait": 1}).json()
        events += read["events"]
        cursor = read["cursor"]
    assert any(e["type"] == "message" for e in events) and events[-1]["state"] == "done"
    assert post(client, "/api/agents/local/agent_run_list").json()["runs"][0]["rid"] == opened["rid"]
    # a handler's own error is an answer (HTTP 200), so the chat can show it
    gone = post(client, "/api/agents/local/agent_run_read", {"rid": "nope"})
    assert gone.status_code == 200 and gone.json()["code"] == "not_found"
    assert post(client, "/api/agents/local/agent_run_close", {"rid": opened["rid"]}).json() == {"ok": True}
    tmp = post(client, "/api/agents/local/agent_tmp_list").json()
    assert tmp["items"][0]["rid"] == opened["rid"] and not tmp["items"][0]["active"]


def test_local_routes_check_the_op_the_body_and_the_header(local):
    client, _root, _home = local
    assert post(client, "/api/agents/local/term_open").status_code == 404
    assert post(client, "/api/agents/local/agent_handle").status_code == 404
    assert client.post("/api/agents/local/agent_prompts", json={}).status_code == 403  # no X-ANX
    assert client.get("/api/agents/local/agent_prompts").status_code == 405
    bad = client.post("/api/agents/local/agent_prompts", content=b"[1, 2]", headers={**WRITE, "Content-Type": "application/json"})
    assert bad.status_code == 400 and bad.json()["code"] == "bad_request"
    empty = client.post("/api/agents/local/agent_prompts", headers=WRITE)
    assert empty.status_code == 200 and empty.json()["fragments"]
    assert post(client, "/api/agents/remote/pc-b/rm_rf").status_code == 404
    # the conversation op exists locally (served by the gateway: not joined here)
    conversation = client.get("/api/inbox/conversation?address=codex-x@pc")
    assert conversation.status_code == 409


def test_remote_relay_needs_unlock_and_a_joined_device(local, monkeypatch):
    client, _root, _home = local
    assert post(client, "/api/lock", {"action": "lock"}).json()["locked"] is True
    locked = post(client, "/api/agents/remote/pc-b/agent_info")
    assert locked.status_code == 423
    assert post(client, "/api/lock", {"action": "unlock"}).json()["locked"] is False
    not_joined = post(client, "/api/agents/remote/pc-b/agent_info")
    assert not_joined.status_code == 409 and not_joined.json()["code"] == "not_joined"

    calls = []

    async def fake_call(root, device_id, op, params):
        calls.append((device_id, op, params))
        return {"error": "这个对话已经结束或不存在", "code": "not_found"} if op == "agent_run_read" else {"agents": []}

    monkeypatch.setattr(remote, "agents_call", fake_call)
    assert post(client, "/api/agents/remote/pc-b/agent_info", {"fresh": True}).json() == {"agents": []}
    answered = post(client, "/api/agents/remote/pc-b/agent_run_read", {"rid": "x", "wait": 20})
    assert answered.status_code == 200 and answered.json()["code"] == "not_found"
    assert calls == [("pc-b", "agent_info", {"fresh": True}), ("pc-b", "agent_run_read", {"rid": "x", "wait": 20})]


def test_agents_can_be_turned_off(tmp_path):
    from agent_nexus.supervisor import Supervisor
    from agent_nexus.web import build_app
    _config(tmp_path, agents={"enabled": False})
    sup = Supervisor(tmp_path)
    assert sup.cfg.agents is False
    client = TestClient(build_app(sup), base_url=sup.cfg.base_url)
    off = post(client, "/api/agents/local/agent_info")
    assert off.status_code == 409 and off.json()["code"] == "disabled"


async def test_agents_call_posts_to_the_gateway_with_the_device_credential(tmp_path, monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/agent_info"):
            return httpx.Response(200, json={"agents": [{"kind": "codex"}]})
        if request.url.path.endswith("/agent_run_open"):
            return httpx.Response(200, json={"error": "工作目录不存在", "code": "bad_workdir"})
        if request.url.path.endswith("/agent_run_stop"):
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(409, json={"error": "设备不在线", "code": "unavailable"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(remote, "gateway_http", lambda: client)
    monkeypatch.setattr(remote, "credentials", lambda root: ("https://gw.example/anx", "pc-a", "t" * 40))
    assert await remote.agents_call(tmp_path, "pc b", "agent_info", {}) == {"agents": [{"kind": "codex"}]}
    request = seen[0]
    assert str(request.url) == "https://gw.example/anx/api/devices/pc%20b/agents/agent_info"
    assert request.headers["authorization"] == "Bearer " + "t" * 40 and request.headers["x-anx-device"] == "pc-a"
    assert (await remote.agents_call(tmp_path, "pc-b", "agent_run_open", {}))["code"] == "bad_workdir"
    with pytest.raises(remote.RemoteError) as old:
        await remote.agents_call(tmp_path, "pc-b", "agent_run_stop", {})
    assert old.value.code == "gateway_too_old" and old.value.status == 409
    with pytest.raises(remote.RemoteError) as offline:
        await remote.agents_call(tmp_path, "pc-b", "agent_sessions", {})
    assert offline.value.status == 409 and offline.value.message == "设备不在线"
    await client.aclose()


def test_every_op_is_routable_locally(local):
    client, _root, _home = local
    for op in OPS:
        response = post(client, f"/api/agents/local/{op}", {})
        assert response.status_code == 200, op
        assert isinstance(response.json(), dict)
