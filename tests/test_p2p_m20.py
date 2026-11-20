"""P2P tunnels (p2p/1): gateway signaling and tokens, the device's remote hub, the built-in VNC."""
from __future__ import annotations

import asyncio
import io
import json
import struct

import pytest
from starlette.testclient import TestClient

from agent_nexus import remote_hub as rh
from agent_nexus.cloud import build_cloud_app

from test_console_m16 import ORIGIN, SERVER_TOKEN
from test_remote_m19 import AS_A, TOKEN_A, TOKEN_B, connect, hub_call, network_config


# --------------------------------------------------------------------------- gateway

def test_gateway_hands_each_device_its_own_tunnel_token_and_guards_the_client_side(tmp_path, monkeypatch):
    config = network_config(tmp_path, monkeypatch)
    config["p2p"] = {"reflector_hosts": ["203.0.113.10", "203.0.113.11"], "udp_ports": [0], "tcp_ports": [0]}
    with TestClient(build_cloud_app(config, tmp_path), base_url=ORIGIN) as client:
        a = connect(client, "pc-a", TOKEN_A, [])
        b = connect(client, "pc-b", TOKEN_B, [])
        try:
            first = hub_call(a, "p2p_config", {})["result"]
            assert first["control_plane"] == f"{ORIGIN}/p2p" and first["server_id"] == "pc-a"
            assert first["reflectors"] == ["203.0.113.10", "203.0.113.11"] and len(first["token"]) >= 40
            assert hub_call(a, "p2p_config", {})["result"]["token"] == first["token"]  # stable
            assert hub_call(b, "p2p_config", {})["result"]["token"] != first["token"]  # one per device
            p2p = client.app.state.p2p if hasattr(client.app, "state") else None
            # the client side of the signaling: a device of the network, or the owner
            url = "/p2p/api/v1/tunnel/client-token?server_id=pc-b"
            assert client.get(url).status_code == 401
            assert client.get(url, headers={"Authorization": f"Bearer {TOKEN_A}", "X-ANX-Device": "pc-a"}).status_code == 200
            assert client.get(url, headers={"Authorization": f"Bearer {TOKEN_B}", "X-ANX-Device": "pc-a"}).status_code == 401
            assert client.get(url, headers={"Authorization": f"Bearer {SERVER_TOKEN}"}).status_code == 200
            hub_call(a, "lock_report", {"locked": True})
            assert client.get(url, headers=dict(AS_A)).status_code == 423
            assert "locked" in hub_call(a, "remote_ticket", {"device_id": "pc-b", "service": "terminal"})["error"]
            hub_call(a, "lock_report", {"locked": False})
            # pc-b has no remote hub: a clear refusal
            assert "0.5.0" in hub_call(a, "remote_ticket", {"device_id": "pc-b", "service": "terminal"})["error"]
            assert client.get("/p2p/api/v1/tunnel/diag").status_code == 404  # unauthenticated diag is off
            assert p2p is None or p2p.secret_for("unknown") is None
        finally:
            for ws in (a, b):
                ws.__exit__(None, None, None)


def test_a_ticket_comes_from_the_target_device(tmp_path, monkeypatch):
    import threading
    with TestClient(build_cloud_app(network_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        a = connect(client, "pc-a", TOKEN_A, [])
        b = connect(client, "pc-b", TOKEN_B, ["remote-hub/1"])

        def answer():
            message = b.receive_json()
            assert message["method"] == "remote_ticket"
            assert message["params"] == {"service": "terminal", "from": "pc-a"}
            b.send_json({"id": message["id"], "result": {"ticket": "t-123", "expires_in": 60}})

        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        try:
            result = hub_call(a, "remote_ticket", {"device_id": "pc-b", "service": "terminal"})["result"]
            assert result == {"ticket": "t-123", "server_id": "pc-b", "expires_in": 60}
        finally:
            thread.join(5)
            for ws in (a, b):
                ws.__exit__(None, None, None)


# --------------------------------------------------------------------------- remote hub

class FakeTerminals:
    def __init__(self):
        self.written, self.closed = [], []

    async def open(self, params):
        return {"sid": "s1", "shell": {"id": params["shell"] or "bash"}, "cwd": "/home"}

    async def read(self, params):
        await asyncio.sleep(0.05)
        if self.written:
            return {"data": __import__("base64").b64encode(b"echo:" + self.written.pop(0)).decode()}
        return {"data": ""}

    async def write(self, params):
        self.written.append(__import__("base64").b64decode(params["data"]))
        return {"ok": True}

    async def resize(self, params):
        return {"ok": True}

    async def close(self, params):
        self.closed.append(params["sid"])
        return {"ok": True}


def test_remote_hub_needs_a_one_time_ticket_for_the_right_service():
    async def scenario():
        terms = FakeTerminals()
        hub = rh.RemoteHub(terms)
        port = await hub.start()
        try:
            assert "error" in await hub.issue({"service": "desktop"})  # no desktop on this device
            ticket = (await hub.issue({"service": "terminal", "from": "pc-a"}))["ticket"]

            async def session(line):
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(line)
                await writer.drain()
                return reader, writer

            reader, writer = await session(rh.hello("terminal", "forged"))
            assert (await rh.read_frame(reader))[0] == rh.ERROR
            writer.close()

            reader, writer = await session(rh.hello("terminal", ticket, shell="zsh", cols=90, rows=20))
            kind, payload = await rh.read_frame(reader)
            assert kind == rh.READY and json.loads(payload)["shell"]["id"] == "zsh"
            writer.write(rh.frame(rh.DATA, b"ls\r"))
            await writer.drain()
            while True:
                kind, payload = await asyncio.wait_for(rh.read_frame(reader), 5)
                if kind == rh.DATA and payload:
                    break
            assert payload == b"echo:ls\r"
            writer.close()
            await asyncio.sleep(0.2)
            assert terms.closed == ["s1"]

            reader, writer = await session(rh.hello("terminal", ticket))  # used once only
            assert (await rh.read_frame(reader))[0] == rh.ERROR
            writer.close()
        finally:
            await hub.close()

    asyncio.run(scenario())


def test_frames_round_trip_and_refuse_oversized():
    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(rh.frame(rh.RESIZE, {"cols": 80, "rows": 24}) + struct.pack(">BI", 0, rh.MAX_FRAME + 1))
        assert await rh.read_frame(reader) == (rh.RESIZE, b'{"cols": 80, "rows": 24}')
        with pytest.raises(ValueError):
            await rh.read_frame(reader)

    asyncio.run(scenario())


# --------------------------------------------------------------------------- VNC

class FakeScreen:
    left = top = 0
    width, height = 64, 48

    def __init__(self):
        from PIL import Image
        self.frames = [Image.new("RGB", (64, 48), (10, 20, 30)), Image.new("RGB", (64, 48), (10, 20, 30))]
        self.frames[1].paste((200, 0, 0), (8, 8, 24, 16))

    def grab(self):
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]

    def close(self):
        pass


def test_vnc_serves_rfb_with_jpeg_updates_of_changed_areas(monkeypatch):
    from agent_nexus import vnc
    monkeypatch.setattr(vnc, "Screen", FakeScreen)
    monkeypatch.setattr(vnc, "make_input", lambda screen: None)

    async def scenario():
        server_side: list = []

        async def serve(reader, writer):
            server_side.append(writer)
            with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
                await vnc.Session(reader, writer, "host-beta").run()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        assert await reader.readexactly(12) == b"RFB 003.008\n"
        writer.write(b"RFB 003.008\n")
        assert await reader.readexactly(2) == b"\x01\x01"  # security: None
        writer.write(b"\x01")
        assert await reader.readexactly(4) == b"\x00\x00\x00\x00"
        writer.write(b"\x01")  # shared
        width, height = struct.unpack(">HH", await reader.readexactly(4))
        await reader.readexactly(16)
        name = await reader.readexactly(struct.unpack(">I", await reader.readexactly(4))[0])
        assert (width, height, name) == (64, 48, b"host-beta")
        writer.write(struct.pack(">BxH", 2, 2) + struct.pack(">ii", vnc.TIGHT, vnc.QUALITY_BASE + 5))
        updates = []
        for incremental in (0, 1):
            writer.write(struct.pack(">BBHHHH", 3, incremental, 0, 0, 64, 48))
            await writer.drain()
            head = await asyncio.wait_for(reader.readexactly(4), 5)
            assert head[0] == 0 and struct.unpack(">H", head[2:])[0] == 1
            x, y, w, h, encoding = struct.unpack(">HHHHi", await reader.readexactly(12))
            assert encoding == vnc.TIGHT and (await reader.readexactly(1)) == b"\x90"
            size, shift = 0, 0
            while True:
                byte = (await reader.readexactly(1))[0]
                size |= (byte & 0x7F) << shift
                shift += 7
                if not byte & 0x80 or shift >= 21:
                    break
            jpeg = await reader.readexactly(size)
            assert jpeg[:2] == b"\xff\xd8"
            updates.append((x, y, w, h))
        assert updates[0] == (0, 0, 64, 48)  # the full first frame
        assert updates[1] == (8, 8, 16, 8)  # then only what changed
        writer.close()
        server.close()

    asyncio.run(scenario())


def test_vnc_raw_follows_the_viewers_pixel_format():
    from PIL import Image
    from agent_nexus.vnc import PixelFormat
    image = Image.new("RGB", (1, 1), (1, 2, 3))
    assert PixelFormat().raw(image) == bytes([3, 2, 1, 0])  # server default: blue at bit 0
    assert PixelFormat(red_shift=0, green_shift=8, blue_shift=16).raw(image) == bytes([1, 2, 3, 0])  # noVNC
