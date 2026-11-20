"""The device's remote entry point behind its P2P tunnel (``remote-hub/1``).

``muxi-p2p-agent`` (the tunnel agent) carries every stream another device opens to this
loopback server. A stream starts with one line::

    ANX-REMOTE/1 {"service": "terminal", "ticket": "...", ...}\\n

The ticket comes from the gateway (``hub_call remote_ticket`` on the caller's side, reverse
RPC ``remote_ticket`` here): single use, valid for a minute, bound to one service. Without it
nothing on this machine -- not even another local program -- gets a shell through the port.

After the line both sides speak frames: ``type (1 byte) | length (4 bytes, big endian) | payload``.
Types: 0 data, 1 resize ``{"cols","rows"}``, 2 ready ``{...}``, 3 exit ``{"code"}``, 4 error ``{"error"}``.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import secrets
import struct
import time
from typing import Any

log = logging.getLogger(__name__)
FEATURE = "remote-hub/1"
MAGIC = b"ANX-REMOTE/1 "
TICKET_TTL_S = 60.0
MAX_LINE = 4096
MAX_FRAME = 1024 * 1024
DATA, RESIZE, READY, EXIT, ERROR = 0, 1, 2, 3, 4
SERVICES = ("terminal", "desktop")


def frame(kind: int, payload: bytes | dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if isinstance(payload, dict) else payload
    return struct.pack(">BI", kind, len(body)) + body


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    head = await reader.readexactly(5)
    kind, size = struct.unpack(">BI", head)
    if size > MAX_FRAME:
        raise ValueError("frame too large")
    return kind, await reader.readexactly(size)


def hello(service: str, ticket: str, **extra: Any) -> bytes:
    return MAGIC + json.dumps({"service": service, "ticket": ticket, **extra}).encode("utf-8") + b"\n"


class RemoteHub:
    def __init__(self, terminals, *, desktop=None):
        self.terminals = terminals
        self.desktop = desktop
        self.tickets: dict[str, tuple[float, str, str]] = {}  # ticket -> (expires, service, caller)
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    # -- tickets (reverse RPC from the gateway)
    async def issue(self, params: dict) -> dict:
        service = str(params.get("service") or "")
        if service not in SERVICES or (service == "desktop" and self.desktop is None):
            return {"error": f"this device does not offer {service or 'that service'}"}
        now = time.monotonic()
        self.tickets = {k: v for k, v in self.tickets.items() if v[0] > now}
        ticket = secrets.token_urlsafe(24)
        self.tickets[ticket] = (now + TICKET_TTL_S, service, str(params.get("from") or ""))
        return {"ticket": ticket, "expires_in": TICKET_TTL_S, "service": service}

    def _redeem(self, ticket: str, service: str) -> str | None:
        entry = self.tickets.pop(ticket, None)
        if entry is None or entry[0] < time.monotonic() or entry[1] != service:
            return None
        return entry[2]

    # -- the loopback server
    async def start(self) -> int:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 15)
            if not line.startswith(MAGIC) or len(line) > MAX_LINE:
                return
            request = json.loads(line[len(MAGIC):])
            service = str(request.get("service") or "")
            caller = self._redeem(str(request.get("ticket") or ""), service)
            if caller is None:
                writer.write(frame(ERROR, {"error": "ticket invalid or expired"}))
                await writer.drain()
                return
            log.info("remote %s session from %s", service, caller or "?")
            if service == "terminal":
                await self._terminal(request, reader, writer)
            elif service == "desktop" and self.desktop is not None:
                await self.desktop.serve(request, reader, writer)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, ValueError):
            pass
        except Exception:
            log.exception("remote session failed")
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _terminal(self, request: dict, reader, writer) -> None:
        opened = await self.terminals.open({"shell": request.get("shell") or "", "cols": request.get("cols"),
                                            "rows": request.get("rows")})
        if "sid" not in opened:
            writer.write(frame(ERROR, {"error": opened.get("error", "could not open a terminal")}))
            await writer.drain()
            return
        sid = opened["sid"]
        writer.write(frame(READY, {"shell": opened.get("shell"), "cwd": opened.get("cwd")}))
        await writer.drain()

        async def output():
            while True:
                result = await self.terminals.read({"sid": sid, "wait": 20})
                data = base64.b64decode(result.get("data") or "")
                if data:
                    writer.write(frame(DATA, data))
                    await writer.drain()
                if result.get("exited"):
                    writer.write(frame(EXIT, {"code": result.get("code")}))
                    await writer.drain()
                    return

        async def input_():
            while True:
                kind, payload = await read_frame(reader)
                if kind == DATA:
                    await self.terminals.write({"sid": sid, "data": base64.b64encode(payload).decode("ascii")})
                elif kind == RESIZE:
                    size = json.loads(payload)
                    await self.terminals.resize({"sid": sid, "cols": size.get("cols"), "rows": size.get("rows")})

        tasks = [asyncio.create_task(output()), asyncio.create_task(input_())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task
            with contextlib.suppress(Exception):
                await self.terminals.close({"sid": sid})

    def handlers(self) -> dict:
        return {"remote_ticket": self.issue}
