"""Private-network model (remote/1): a joined device operates on the others with its own
credential -- terminals, revoke, delete -- unless it is locked (the client's 锁定)."""
from __future__ import annotations

import contextlib
import json
import socket
import time

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_nexus.cloud import build_cloud_app
from agent_nexus.lock import Lock, LockError

from test_console_m16 import ORIGIN, SERVER_TOKEN
from test_terminal import FakeDevice

TOKEN_A, TOKEN_B = "a" * 40, "b" * 40


def network_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_TOKEN", SERVER_TOKEN)
    monkeypatch.setenv("TOKEN_A", TOKEN_A)
    monkeypatch.setenv("TOKEN_B", TOKEN_B)
    return {"token_env": "SERVER_TOKEN", "agents": {"pc-a": "TOKEN_A", "pc-b": "TOKEN_B"}, "public_url": ORIGIN,
            "state_dir": str(tmp_path / "state")}


def connect(client, agent_id, token, features):
    ws = client.websocket_connect(f"/ingress/{agent_id}", headers={"Authorization": f"Bearer {token}"}).__enter__()
    ws.send_json({"protocol": 1, "agent_id": agent_id, "catalog": {"workspaces": {}, "mcps": {}},
                  "anx": {"version": "0.4.0", "features": features}})
    ready = ws.receive_json()
    assert ready["method"] == "ready" and "remote/1" in ready["params"]["anx"]["features"]
    return ws


def hub_call(ws, op, args):
    ws.send_json({"id": f"d-{time.monotonic_ns()}", "method": "hub_call", "params": {"op": op, "args": args}})
    return ws.receive_json()


AS_A = {"Authorization": f"Bearer {TOKEN_A}", "X-ANX-Device": "pc-a"}


def test_a_device_opens_another_devices_terminal_until_it_locks(tmp_path, monkeypatch):
    with TestClient(build_cloud_app(network_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        a = connect(client, "pc-a", TOKEN_A, [])
        b = connect(client, "pc-b", TOKEN_B, ["term/1"])
        target = FakeDevice(b)
        target.thread.start()
        try:
            listed = client.get("/api/network/devices", headers=dict(AS_A))
            assert listed.status_code == 200
            assert {d["device_id"]: d["terminal"] for d in listed.json()["devices"]} == {"pc-a": False, "pc-b": True}
            # a wrong token, another device's token, or no device header is not a device caller
            assert client.get("/api/network/devices", headers={**AS_A, "Authorization": f"Bearer {TOKEN_B}"}).status_code == 401
            assert client.get("/api/devices/pc-b/terminal", headers={"Authorization": f"Bearer {TOKEN_A}"}).status_code == 401
            assert client.get("/api/devices", headers=dict(AS_A)).status_code == 401  # the owner API stays owner-only

            assert client.get("/api/devices/pc-b/terminal", headers=dict(AS_A)).json()["shells"][0]["id"] == "bash"
            with client.websocket_connect("/api/devices/pc-b/terminal/ws?shell=bash", headers=dict(AS_A)) as term:
                assert json.loads(term.receive_text())["type"] == "ready"
                assert term.receive_bytes() == b"welcome\r\n"
                term.send_bytes(b"whoami\r")
                assert term.receive_bytes() == b"echo:whoami\r"

            # 锁定: A may still be listed and operated on, but not operate on others
            assert hub_call(a, "lock_report", {"locked": True})["result"] == {"locked": True}
            devices = {d["device_id"]: d for d in client.get("/api/network/devices", headers=dict(AS_A)).json()["devices"]}
            assert devices["pc-a"]["locked"] is True and devices["pc-b"]["locked"] is False
            assert client.get("/api/devices/pc-b/terminal", headers=dict(AS_A)).status_code == 423
            with client.websocket_connect("/api/devices/pc-b/terminal/ws", headers=dict(AS_A)) as term:
                message = json.loads(term.receive_text())
                assert message["type"] == "error" and "锁定" in message["error"]
            refused = hub_call(a, "device_admin", {"op": "revoke", "device_id": "pc-b"})
            assert "locked" in refused["error"]
            assert "devices" in hub_call(a, "device_admin", {"op": "list"})["result"]
            assert hub_call(a, "lock_report", {"locked": False})["result"] == {"locked": False}
            again = client.get("/api/devices/pc-b/terminal", headers=dict(AS_A))
            assert again.status_code == 200, again.text
        finally:
            for ws in (a, b):
                with contextlib.suppress(Exception):  # B's reader thread may still hold its socket
                    ws.__exit__(None, None, None)
        time.sleep(0.2)
        # an offline device's credential opens nothing
        assert client.get("/api/devices/pc-b/terminal", headers=dict(AS_A)).status_code == 401


# --------------------------------------------------------------------------- the client's lock

def test_lock_toggles_without_a_password_and_needs_it_once_set(tmp_path):
    lock = Lock(tmp_path)
    assert lock.state() == {"locked": False, "has_password": False}
    assert lock.lock()["locked"] and lock.unlock()["locked"] is False
    with pytest.raises(LockError):
        lock.set_password("abc")  # too short
    lock.set_password("open-sesame")
    stored = (tmp_path / ".anx" / "lock.json").read_text(encoding="utf-8")
    assert "open-sesame" not in stored and "hash" in stored
    lock.lock()
    with pytest.raises(LockError) as wrong:
        lock.unlock("nope")
    assert wrong.value.status == 403
    with pytest.raises(LockError):
        lock.require_unlocked()
    assert lock.unlock("open-sesame")["locked"] is False
    with pytest.raises(LockError):
        lock.set_password(None, current="nope")  # removing needs the current password
    assert lock.set_password(None, current="open-sesame") == {"locked": False, "has_password": False}


def test_unlock_attempts_are_limited(tmp_path):
    lock = Lock(tmp_path)
    lock.set_password("open-sesame")
    lock.lock()
    for _ in range(5):
        with pytest.raises(LockError):
            lock.unlock("nope")
    with pytest.raises(LockError) as limited:
        lock.unlock("open-sesame")
    assert limited.value.status == 429


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def local(tmp_path):
    from agent_nexus.supervisor import Supervisor
    from agent_nexus.web import build_app
    root = tmp_path / "root"
    root.mkdir()
    (root / "agent-nexus.json").write_text(json.dumps({"manager": {"port": _free_port()}, "builtins": False, "scan": [],
                                                       "cloud": {"enabled": False}}), encoding="utf-8")
    sup = Supervisor(root)
    with TestClient(build_app(sup), base_url=sup.cfg.base_url) as client:
        client.local_host = {"Host": f"127.0.0.1:{sup.cfg.port}"}  # the test client sends "testserver" on WebSockets
        yield client, root


def test_client_lock_api_blocks_operating_on_others(local):
    client, root = local
    write = {"X-ANX": "1"}
    assert client.get("/api/lock").json() == {"locked": False, "has_password": False}
    assert client.post("/api/lock", json={"action": "lock"}).status_code == 403  # no X-ANX
    assert client.post("/api/lock", json={"action": "lock"}, headers=write).json()["locked"] is True
    # locked: no revoke/delete/terminal from this client
    assert client.post("/api/devices/pc-b/revoke", headers=write).status_code == 423
    assert client.delete("/api/devices/pc-b", headers=write).status_code == 423
    assert client.get("/api/remote/devices/pc-b/terminal").status_code == 423
    assert client.post("/api/remote/devices/pc-b/terminal/external", json={}, headers=write).status_code == 423
    with client.websocket_connect("/api/remote/devices/pc-b/terminal/ws", headers=client.local_host) as term:
        assert "锁定" in json.loads(term.receive_text())["error"]
    # a password, then unlocking needs it
    assert client.put("/api/lock/password", json={"new": "open-sesame"}, headers=write).json()["has_password"] is True
    assert client.post("/api/lock", json={"action": "unlock", "password": "x"}, headers=write).status_code == 403
    assert client.post("/api/lock", json={"action": "unlock", "password": "open-sesame"}, headers=write).json()["locked"] is False
    # unlocked but not joined: a clear answer, not a crash
    not_joined = client.get("/api/remote/devices/pc-b/terminal")
    assert not_joined.status_code == 409 and not_joined.json()["code"] == "not_joined"


def test_client_websockets_refuse_foreign_origins(local):
    client, _root = local
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect("/api/remote/devices/pc-b/terminal/ws", headers={**client.local_host, "Origin": "https://evil.test"}) as term:
            term.receive_text()
    assert refused.value.code == 1008


def test_external_command_runs_anx_term_with_the_console_python(tmp_path, monkeypatch):
    from agent_nexus import remote
    command = remote.external_command(tmp_path, "pc-b", "pwsh")
    assert command[1:] == ["-m", "agent_nexus.cli", "--root", str(tmp_path), "term", "pc-b", "--shell", "pwsh"]
    assert not command[0].lower().endswith("pythonw.exe")
