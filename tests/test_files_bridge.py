"""The gateway relays file transfer (files/1) for devices that cannot punch a P2P tunnel."""
from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
import types
from urllib.parse import unquote

from starlette.testclient import TestClient

from agent_nexus import file_share
from agent_nexus.cloud import build_cloud_app

from test_console_m16 import ORIGIN
from test_remote_m19 import AS_A, TOKEN_A, TOKEN_B, connect, hub_call, network_config


class FileDevice:
    """A device on /ingress answering fx_* with a real FileShare over a temporary home."""

    def __init__(self, ws, home):
        account = types.SimpleNamespace(home=str(home), session=None, uid=0, gid=0)
        self.share = file_share.FileShare(account=lambda: account)
        self.handlers = self.share.handlers()
        self.ws = ws
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def serve(self):
        while True:
            try:
                message = self.ws.receive_json()
            except Exception:
                return
            if "id" in message and "method" in message:
                threading.Thread(target=self.answer, args=(message,), daemon=True).start()

    def answer(self, message):
        handler = self.handlers.get(message["method"])
        result = asyncio.run(handler(message.get("params", {}))) if handler else {"error": "unknown"}
        with self.lock:
            self.ws.send_json({"id": message["id"], "result": result})


def test_a_device_lists_reads_and_writes_another_devices_files_through_the_gateway(tmp_path, monkeypatch):
    home = tmp_path / "home-b"
    (home / "Downloads").mkdir(parents=True)
    payload = os.urandom(file_share.CHUNK + 4321)
    (home / "Downloads" / "report.bin").write_bytes(payload)
    with TestClient(build_cloud_app(network_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        a = connect(client, "pc-a", TOKEN_A, [])
        b = connect(client, "pc-b", TOKEN_B, ["files/1"])
        device = FileDevice(b, home)
        device.thread.start()
        try:
            devices = {d["device_id"]: d for d in client.get("/api/network/devices", headers=dict(AS_A)).json()["devices"]}
            assert devices["pc-b"]["files"] is True and devices["pc-a"]["files"] is False

            listing = client.get("/api/devices/pc-b/files", headers=dict(AS_A))
            assert listing.status_code == 200, listing.text
            assert [e["name"] for e in listing.json()["entries"]] == ["report.bin"]
            assert client.get("/api/devices/pc-a/files", headers=dict(AS_A)).status_code == 409  # no files/1

            path = str(home / "Downloads" / "report.bin")
            got, offset = b"", 0
            while True:
                chunk = client.get("/api/devices/pc-b/files/read", params={"path": path, "offset": offset}, headers=dict(AS_A))
                assert chunk.status_code == 200 and unquote(chunk.headers["x-anx-name"]) == "report.bin"
                got += chunk.content
                offset += len(chunk.content)
                if chunk.headers["x-anx-eof"] == "1":
                    break
            assert got == payload

            begin = client.post("/api/devices/pc-b/files/put/begin", json={"name": "sent.bin", "size": len(payload)},
                                headers=dict(AS_A)).json()
            for start in range(0, len(payload), file_share.CHUNK):
                part = payload[start:start + file_share.CHUNK]
                wrote = client.post("/api/devices/pc-b/files/put/write", params={"id": begin["id"], "offset": start},
                                    content=part, headers=dict(AS_A))
                assert wrote.status_code == 200, wrote.text
            done = client.post("/api/devices/pc-b/files/put/end", json={"id": begin["id"]}, headers=dict(AS_A))
            assert done.status_code == 200 and (home / "Downloads" / "sent.bin").read_bytes() == payload

            missing = client.get("/api/devices/pc-b/files/read", params={"path": str(home / "nope")}, headers=dict(AS_A))
            assert missing.status_code == 422 and "不是文件" in missing.json()["error"]
            assert client.get("/api/devices/pc-b/files", headers={"Authorization": f"Bearer {TOKEN_A}"}).status_code == 401

            assert hub_call(a, "lock_report", {"locked": True})["result"] == {"locked": True}
            assert client.get("/api/devices/pc-b/files", headers=dict(AS_A)).status_code == 423
        finally:
            for ws in (a, b):
                with contextlib.suppress(Exception):
                    ws.__exit__(None, None, None)
        time.sleep(0.2)
