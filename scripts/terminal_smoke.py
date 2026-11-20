"""Open a real remote terminal through a gateway and run one command in it.

    uv run python scripts/terminal_smoke.py --gateway http://127.0.0.1:8443 --token <owner token> [--device ID]

Checks GET /api/devices/{id}/terminal (shells, CLIs), then the WebSocket: ready, a prompt, an
`echo` whose answer can only come from the shell, a resize, and `exit` ending the session.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from urllib.parse import quote, urlsplit

import httpx
from websockets.asyncio.client import connect


async def run(gateway: str, token: str, device: str | None) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    devices = httpx.get(f"{gateway}/api/devices", headers=headers, timeout=20).json()["devices"]
    usable = [d for d in devices if d["online"] and d.get("terminal")]
    target = next((d for d in usable if device in (None, d["device_id"])), None)
    assert target, f"no online device with a terminal: {[(d['device_id'], d['online'], d.get('terminal')) for d in devices]}"
    device_id = quote(target["device_id"], safe="")
    info = httpx.get(f"{gateway}/api/devices/{device_id}/terminal", headers=headers, timeout=60).json()
    print("shells:", [s["id"] for s in info["shells"]], "| clis:", [f"{c['id']} {c['version']}" for c in info["clis"]])
    windows = info.get("platform") == "win32"
    shell = "cmd" if windows else next((s["id"] for s in info["shells"] if s["id"] == "bash"), info["shells"][0]["id"])
    command, marker = ((b"echo anx-term-ok%PATHEXT:~0,0%\r", b"anx-term-ok\r") if windows
                       else (b"echo anx-term-$((6*7))\r", b"anx-term-42"))
    scheme = "wss" if gateway.startswith("https") else "ws"
    url = f"{scheme}://{urlsplit(gateway).netloc}/api/devices/{device_id}/terminal/ws?shell={shell}&cols=100&rows=30"
    async with connect(url, additional_headers=headers, proxy=None) as socket:
        ready = json.loads(await asyncio.wait_for(socket.recv(), 30))
        assert ready["type"] == "ready", ready
        print("ready:", ready["shell"]["name"], ready.get("cwd"))
        out = bytearray()

        async def until(predicate, seconds):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                try:
                    message = await asyncio.wait_for(socket.recv(), max(0.1, end - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                if isinstance(message, bytes):
                    out.extend(message)
                    if b"\x1b[6n" in message:
                        await socket.send(b"\x1b[1;1R")
                else:
                    payload = json.loads(message)
                    if predicate(payload):
                        return payload
                    assert payload.get("type") != "error", payload
                if predicate(None):
                    return True
            return None

        await until(lambda m: False, 3)  # the prompt
        await socket.send(command)
        await socket.send(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
        assert await until(lambda m: m is None and marker in out, 20), bytes(out[-400:])
        print("command output seen")
        await socket.send(b"exit\r")
        finished = await until(lambda m: isinstance(m, dict) and m.get("type") == "exit", 20)
        assert finished, bytes(out[-400:])
        print("session ended:", finished)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--device")
    args = parser.parse_args()
    asyncio.run(run(args.gateway.rstrip("/"), args.token, args.device))
    print("ok: remote terminal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
