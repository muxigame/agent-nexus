"""Remote terminal (term/1): the device's pty sessions, the gateway bridge, the owner token API."""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import threading
import time

import pytest
from starlette.testclient import TestClient

from agent_nexus import terminal
from agent_nexus.cloud import build_cloud_app
from agent_nexus.features import AGENT_OPTIONAL_FEATURES, agent_anx

from test_console_m16 import ORIGIN, PASSWORD, SERVER_TOKEN, bearer_config, login, oauth_config

B64 = lambda data: base64.b64encode(data).decode()  # noqa: E731


# --------------------------------------------------------------------------- device side

def test_term_feature_is_optional_and_announced_only_when_asked():
    assert "term/1" in AGENT_OPTIONAL_FEATURES
    assert "term/1" in agent_anx(("term/1",))["features"]
    assert "term/1" not in agent_anx(())["features"]


def test_shell_and_cli_detection_find_this_machine():
    shells = terminal.detect_shells()
    assert shells and sum(shell.default for shell in shells) == 1
    if sys.platform == "win32":
        assert {"cmd"} <= {shell.id for shell in shells}
    else:
        assert all(shell.argv[0].startswith("/") for shell in shells)
    clis = terminal.detect_clis()
    assert any(cli["id"] == "git" for cli in clis), clis
    git = next(cli for cli in clis if cli["id"] == "git")
    assert git["version"] and git["version"][0].isdigit()


def test_dimensions_are_clamped():
    assert terminal._dimension("x", 120, 20, 500) == 120
    assert terminal._dimension(5, 120, 20, 500) == 20
    assert terminal._dimension(10_000, 120, 20, 500) == 500


@pytest.mark.skipif(not terminal.available(), reason="no pseudo-terminal support")
def test_a_real_shell_runs_input_and_exits():
    async def scenario():
        terms = terminal.Terminals()
        shell = "cmd" if sys.platform == "win32" else ""
        opened = await terms.open({"shell": shell, "cols": 90, "rows": 25})
        assert "sid" in opened, opened
        sid = opened["sid"]
        out = bytearray()

        async def pump(seconds, until=None):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                result = await terms.read({"sid": sid, "wait": 1})
                chunk = base64.b64decode(result["data"])
                out.extend(chunk)
                if b"\x1b[6n" in chunk:  # ConPTY asks where the cursor is; a terminal answers
                    await terms.write({"sid": sid, "data": B64(b"\x1b[1;1R")})
                if result.get("exited"):
                    return result
                if until is not None and until in out:
                    return result
            return None

        await pump(2)
        if sys.platform == "win32":
            command, marker = b"echo anx-term-ok%PATHEXT:~0,0%\r", b"anx-term-ok\r"
        else:
            command, marker = b"echo anx-term-$((6*7))\r", b"anx-term-42"
        assert (await terms.write({"sid": sid, "data": B64(command)}))["ok"]
        assert (await terms.resize({"sid": sid, "cols": 100, "rows": 30}))["ok"]
        await pump(15, marker)
        assert marker in out, bytes(out[-400:])
        await terms.write({"sid": sid, "data": B64(b"exit\r")})
        finished = await pump(15)
        assert finished is not None and finished.get("exited"), bytes(out[-400:])
        assert sid not in terms.sessions
        with pytest.raises(terminal.TermError):
            await terms.read({"sid": sid})

    asyncio.run(scenario())


def test_handlers_turn_errors_into_results_and_limit_sessions(monkeypatch):
    async def scenario():
        terms = terminal.Terminals()
        handlers = terms.handlers()
        assert set(handlers) == {"term_info", "term_open", "term_write", "term_read", "term_resize", "term_close"}
        assert "error" in await handlers["term_read"]({"sid": "missing"})
        assert "error" in await handlers["term_open"]({"shell": "no-such-shell"})
        monkeypatch.setattr(terminal, "MAX_SESSIONS", 0)
        assert "already has" in (await handlers["term_open"]({}))["error"]
        assert (await handlers["term_close"]({"sid": "missing"}))["ok"]

    asyncio.run(scenario())


# --------------------------------------------------------------------------- gateway bridge

class FakeDevice:
    """A device on /ingress that answers term_* like the real one, echoing input back as output."""

    def __init__(self, ws):
        self.ws = ws
        self.lock = threading.Lock()
        self.output: list[bytes] = [b"welcome\r\n"]
        self.calls: list[tuple[str, dict]] = []
        self.exited = False
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def send(self, message):
        with self.lock:
            self.ws.send_json(message)

    def serve(self):
        while True:
            try:
                message = self.ws.receive_json()
            except Exception:
                return
            if "id" not in message:
                continue
            threading.Thread(target=self.answer, args=(message,), daemon=True).start()

    def answer(self, message):
        method, params = message["method"], message.get("params", {})
        self.calls.append((method, params))
        if method == "term_info":
            result = {"shells": [{"id": "bash", "name": "Bash", "command": "/bin/bash -l", "default": True}],
                      "clis": [{"id": "git", "name": "Git", "path": "/usr/bin/git", "version": "2.43.0"}]}
        elif method == "term_open":
            result = {"sid": "s1", "shell": {"id": "bash", "name": "Bash"}, "cols": params["cols"], "rows": params["rows"]}
        elif method == "term_write":
            data = base64.b64decode(params["data"])
            if data == b"exit\r":
                self.exited = True
            else:
                self.output.append(b"echo:" + data)
            result = {"ok": True}
        elif method == "term_read":
            for _ in range(20):
                if self.output or self.exited:
                    break
                time.sleep(0.05)
            data = b"".join(self.output)
            self.output.clear()
            result = {"data": B64(data), "dropped": 0}
            if self.exited and not data:
                result.update(exited=True, code=0)
        else:
            result = {"ok": True}
        self.send({"id": message["id"], "result": result})


def connect_device(client, features):
    ws = client.websocket_connect("/ingress/pc-main", headers={"Authorization": "Bearer " + "a" * 40}).__enter__()
    ws.send_json({"protocol": 1, "agent_id": "pc-main", "catalog": {"workspaces": {}, "mcps": {}},
                  "anx": {"version": "0.3.0", "features": features}})
    assert ws.receive_json()["method"] == "ready"
    return ws


def test_bridge_runs_a_session_end_to_end(tmp_path, monkeypatch):
    with TestClient(build_cloud_app(bearer_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        auth = {"Authorization": f"Bearer {SERVER_TOKEN}"}
        ws = connect_device(client, ["term/1"])
        device = FakeDevice(ws)
        device.thread.start()
        try:
            listed = client.get("/api/devices", headers=auth).json()["devices"][0]
            assert listed["terminal"] is True
            assert client.get("/api/devices/pc-main/terminal").status_code == 401
            info = client.get("/api/devices/pc-main/terminal", headers=auth).json()
            assert info["shells"][0]["id"] == "bash" and info["clis"][0]["version"] == "2.43.0"

            with client.websocket_connect("/api/devices/pc-main/terminal/ws?shell=bash&cols=90&rows=20", headers=auth) as term:
                ready = json.loads(term.receive_text())
                assert ready["type"] == "ready" and ready["shell"]["name"] == "Bash"
                assert term.receive_bytes() == b"welcome\r\n"
                term.send_bytes(b"ls\r")
                assert term.receive_bytes() == b"echo:ls\r"
                term.send_text(json.dumps({"type": "input", "data": "pwd\r"}))
                assert term.receive_bytes() == b"echo:pwd\r"
                term.send_text(json.dumps({"type": "resize", "cols": 100, "rows": 40}))
                term.send_bytes(b"exit\r")
                assert json.loads(term.receive_text()) == {"type": "exit", "code": 0}
            time.sleep(0.2)
            methods = [method for method, _ in device.calls]
            assert methods[:2] == ["term_info", "term_open"]
            assert ("term_open", {"shell": "bash", "cols": 90, "rows": 20}) in device.calls
            assert ("term_resize", {"sid": "s1", "cols": 100, "rows": 40}) in device.calls
            assert "term_close" in methods
        finally:
            ws.__exit__(None, None, None)


def test_bridge_refuses_without_owner_or_feature(tmp_path, monkeypatch):
    with TestClient(build_cloud_app(oauth_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        ws = connect_device(client, [])  # an older client: no term/1
        try:
            from starlette.websockets import WebSocketDisconnect
            with pytest.raises(WebSocketDisconnect) as refused:
                with client.websocket_connect("/api/devices/pc-main/terminal/ws") as term:
                    term.receive_text()
            assert refused.value.code == 4401
            assert login(client).status_code == 303
            from agent_nexus.inbox_web import COOKIE
            cookie = {"Cookie": f"{COOKIE}={client.cookies.get(COOKIE)}"}  # the test client keeps Secure cookies off ws://
            # the cookie works from the console's own origin only
            with pytest.raises(WebSocketDisconnect) as foreign:
                with client.websocket_connect("/api/devices/pc-main/terminal/ws", headers={**cookie, "Origin": "https://evil.test"}) as term:
                    term.receive_text()
            assert foreign.value.code in (4401, 1008, 403)
            with client.websocket_connect("/api/devices/pc-main/terminal/ws", headers={**cookie, "Origin": ORIGIN}) as term:
                message = json.loads(term.receive_text())
                assert message["type"] == "error" and "0.3.0" in message["error"]
            assert client.get("/api/devices", headers={"Origin": ORIGIN}).json()["devices"][0]["terminal"] is False
            assert client.get("/api/devices/pc-main/terminal").status_code == 409
        finally:
            ws.__exit__(None, None, None)


# --------------------------------------------------------------------------- owner token for `anx term`

def test_token_api_issues_an_owner_bearer_token(tmp_path, monkeypatch):
    with TestClient(build_cloud_app(oauth_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        assert client.post("/api/auth/token", data={"password": PASSWORD}).status_code == 415
        assert client.post("/api/auth/token", json={"password": "nope"}).status_code == 401
        assert client.post("/api/auth/token", json={"password": PASSWORD},
                           headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        issued = client.post("/api/auth/token", json={"password": PASSWORD, "remember": True})
        assert issued.status_code == 200 and issued.json()["expires_in"] == 30 * 86400
        token = issued.json()["access_token"]
        client.cookies.clear()
        assert client.get("/api/devices", headers={"Authorization": f"Bearer {token}"}).status_code == 200
        for _ in range(8):
            client.post("/api/auth/token", json={"password": "nope"})
        assert client.post("/api/auth/token", json={"password": PASSWORD}).status_code == 429


def test_cli_gateway_origin_rules(tmp_path, monkeypatch):
    from agent_nexus.term_cli import TermCliError, gateway_origin, _pick_device

    monkeypatch.delenv("ANX_GATEWAY", raising=False)
    assert gateway_origin(tmp_path, "wss://hub.example.test") == "https://hub.example.test"
    assert gateway_origin(tmp_path, "hub.example.test") == "https://hub.example.test"
    assert gateway_origin(tmp_path, "http://127.0.0.1:8443") == "http://127.0.0.1:8443"
    with pytest.raises(TermCliError):
        gateway_origin(tmp_path, "http://hub.example.test")
    with pytest.raises(TermCliError):
        gateway_origin(tmp_path, None)
    devices = [{"device_id": "a", "display_name": "A", "online": True, "terminal": True},
               {"device_id": "b", "display_name": "B", "online": True, "terminal": False},
               {"device_id": "c", "display_name": "C", "online": False, "terminal": True}]
    assert _pick_device(devices, "A")["device_id"] == "a"
    for wanted in ("b", "c", "zzz"):
        with pytest.raises(TermCliError):
            _pick_device(devices, wanted)
