"""The device's remote entry point behind its P2P tunnel (``remote-hub/1``).

``muxi-p2p-agent`` (the tunnel agent) carries every stream another device opens to this
loopback server. A stream starts with one line::

    ANX-REMOTE/1 {"service": "terminal", "ticket": "...", ...}\\n

The ticket comes from the gateway (``hub_call remote_ticket`` on the caller's side, reverse
RPC ``remote_ticket`` here): single use, valid for a minute, bound to one service. Without it
nothing on this machine -- not even another local program -- gets a shell through the port.

After the line both sides speak frames: ``type (1 byte) | length (4 bytes, big endian) | payload``.
Types: 0 data, 1 resize ``{"cols","rows"}``, 2 ready ``{...}``, 3 exit ``{"code"}``, 4 error ``{"error"}``.

``files`` (``file_share.py``): the hello carries ``op``.
``list`` ``{"path"}`` -> ready ``{listing}``.
``get`` ``{"path", "offset"}`` -> ready ``{"name", "size"}``, data frames, exit ``{"code": 0}``.
``put`` ``{"dir", "name", "size"}`` -> ready ``{"id", ...}``; the caller sends data frames and then
exit; the reply is exit ``{"code": 0, "path", "size"}`` (or error).

Through a relay (docs/relay-mesh.md) the caller first opens the relay's ``relay`` service::

    ANX-REMOTE/1 {"service": "relay", "ticket": "<relay's ticket>", "target": "<device>", "via": "<relay>"?}

and the relay splices the stream onto the target's hub (or the next relay) byte for byte. The
session inside is end-to-end encrypted (``e2e.py``) and starts with::

    ANX-REMOTE/2 {"service": "...", "tid": "<sha256(ticket)[:32]>", ...}

so the relay never sees the ticket or the content.
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
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from . import e2e

log = logging.getLogger(__name__)
FEATURE = "remote-hub/1"
MAGIC = b"ANX-REMOTE/1 "
MAGIC2 = b"ANX-REMOTE/2 "
#: round-trip probe through a tunnel (relay mesh); answered without a ticket, says nothing.
#: Padded on purpose: the tunnel agent holds a new stream until its first 12+ bytes arrived
#: (it sniffs for a bandwidth-test request), and a bare 9-byte line would just sit there.
PING = b"ANX-PING/1 " + b"-" * 32 + b"\n"
PONG = b"ANX-PONG\n"
TICKET_TTL_S = 60.0
MAX_LINE = 4096
MAX_FRAME = 1024 * 1024
DATA, RESIZE, READY, EXIT, ERROR = 0, 1, 2, 3, 4
SERVICES = ("terminal", "desktop", "files", "relay")
#: streams one relay forwards at the same time
RELAY_MAX_STREAMS = 32
#: (target, via, caller) -> reader/writer onto the target's hub (or the next relay)
RelayConnect = Callable[[str, str, str], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


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


def hello2(service: str, ticket: str, **extra: Any) -> bytes:
    """The end-to-end hello: the ticket stays secret, only its digest is sent."""
    return MAGIC2 + json.dumps({"service": service, "tid": e2e.ticket_id(ticket), **extra}).encode("utf-8") + b"\n"


class RouteError(ConnectionError):
    """The relay could not reach the target (the target itself was not asked yet)."""


@dataclass
class Path:
    """How to reach a device's hub: straight through a tunnel, or through a relay.

    ``port`` is the local end of a P2P tunnel -- to the target itself, or to the first relay
    when ``relay_ticket`` is set (then ``target``/``via`` tell the relay where to go on).
    """
    port: int
    relay_ticket: str = ""
    target: str = ""
    via: str = ""
    label: str = ""

    @property
    def relayed(self) -> bool:
        return bool(self.relay_ticket)

    async def open(self, service: str, ticket: str, **extra: Any):
        """A reader/writer pair with the hello for ``service`` already sent."""
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.port), 10)
        if not self.relayed:
            writer.write(hello(service, ticket, **extra))
            await writer.drain()
            return reader, writer
        try:
            writer.write(hello("relay", self.relay_ticket, target=self.target, via=self.via))
            await writer.drain()
            kind, payload = await asyncio.wait_for(read_frame(reader), 45)
            if kind != READY:
                message = json.loads(payload).get("error", "relay refused") if kind == ERROR else "relay: bad answer"
                raise RouteError(message)
        except BaseException:
            writer.close()
            raise
        secure_reader, secure_writer = e2e.wrap(reader, writer, ticket, e2e.CALLER)
        writer.write(hello2(service, ticket, **extra))  # plain line, then everything is sealed
        await writer.drain()
        return secure_reader, secure_writer


def as_path(route: "int | Path") -> Path:
    """Callers may still pass a bare tunnel port."""
    return route if isinstance(route, Path) else Path(int(route))


async def splice(a_reader, a_writer, b_reader, b_writer, chunk: int = 256 * 1024) -> int:
    """Copy both ways until either side ends; returns the bytes moved."""
    moved = 0

    async def pump(reader, writer):
        nonlocal moved
        try:
            while True:
                data = await reader.read(chunk)
                if not data:
                    break
                moved += len(data)
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    tasks = [asyncio.create_task(pump(a_reader, b_writer)), asyncio.create_task(pump(b_reader, a_writer))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return moved


class RemoteHub:
    def __init__(self, terminals, *, desktop=None, files=None, relay: RelayConnect | None = None):
        self.terminals = terminals
        self.desktop = desktop
        self.files = files
        #: set on a relay device: connects onward for the ``relay`` service
        self.relay = relay
        #: set on a relay device: keep a standing tunnel to a member that chose us (``warm``)
        self.relay_warm: Callable[[str], Awaitable[None]] | None = None
        self.relaying = 0
        self.tickets: dict[str, tuple[float, str, str]] = {}  # ticket -> (expires, service, caller)
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    # -- tickets (reverse RPC from the gateway)
    async def issue(self, params: dict) -> dict:
        service = str(params.get("service") or "")
        if service not in SERVICES or (service == "desktop" and self.desktop is None) \
                or (service == "files" and self.files is None) or (service == "relay" and self.relay is None):
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

    def _find(self, tid: str) -> str | None:
        """The ticket whose digest an end-to-end hello names."""
        for ticket in self.tickets:
            if secrets.compare_digest(e2e.ticket_id(ticket), tid):
                return ticket
        return None

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
            pings = 0
            while line.startswith(b"ANX-PING") and pings < 16:
                pings += 1
                writer.write(PONG)
                await writer.drain()
                line = await asyncio.wait_for(reader.readline(), 15)
            if pings:
                return
            if len(line) > MAX_LINE or not (line.startswith(MAGIC) or line.startswith(MAGIC2)):
                return
            secure = line.startswith(MAGIC2)
            request = json.loads(line[len(MAGIC2 if secure else MAGIC):])
            service = str(request.get("service") or "")
            ticket = self._find(str(request.get("tid") or "")) if secure else str(request.get("ticket") or "")
            caller = self._redeem(ticket or "", service)
            if caller is None:
                writer.write(frame(ERROR, {"error": "ticket invalid or expired"}))
                await writer.drain()
                return
            if secure:
                reader, writer = e2e.wrap(reader, writer, ticket, e2e.TARGET)
            log.info("remote %s session from %s%s", service, caller or "?", " (end-to-end, relayed)" if secure else "")
            if service == "relay" and self.relay is not None and not secure:
                await self._relay(request, caller, reader, writer)
            elif service == "terminal":
                await self._terminal(request, reader, writer)
            elif service == "desktop" and self.desktop is not None:
                await self.desktop.serve(request, reader, writer)
            elif service == "files" and self.files is not None:
                await self._files(request, reader, writer)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, ValueError):
            pass
        except Exception:
            log.exception("remote session failed")
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _relay(self, request: dict, caller: str, reader, writer) -> None:
        target = str(request.get("target") or "")
        via = str(request.get("via") or "")
        if not target:
            writer.write(frame(ERROR, {"error": "relay: no target"}))
            await writer.drain()
            return
        if request.get("warm"):
            # a member made us one of its home relays: keep a tunnel back to it standing
            if self.relay_warm is not None:
                try:
                    await asyncio.wait_for(self.relay_warm(target), 40)
                except Exception as exc:
                    writer.write(frame(ERROR, {"error": f"relay: cannot reach {target}: {str(exc)[:120]}"}))
                    await writer.drain()
                    return
            writer.write(frame(READY, {"warm": target}))
            await writer.drain()
            return
        if self.relaying >= RELAY_MAX_STREAMS:
            writer.write(frame(ERROR, {"error": "relay: busy"}))
            await writer.drain()
            return
        self.relaying += 1
        try:
            try:
                onward_reader, onward_writer = await asyncio.wait_for(self.relay(target, via, caller), 40)
            except Exception as exc:
                writer.write(frame(ERROR, {"error": f"relay: cannot reach {via or target}: {str(exc)[:120]}"}))
                await writer.drain()
                return
            writer.write(frame(READY, {"relay": target}))
            await writer.drain()
            moved = await splice(reader, writer, onward_reader, onward_writer)
            log.info("relayed %s -> %s%s: %d bytes", caller or "?", target, f" via {via}" if via else "", moved)
        finally:
            self.relaying -= 1

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

    async def _files(self, request: dict, reader, writer) -> None:
        from .file_share import STREAM_CHUNK, FileShareError

        files = self.files
        op = str(request.get("op") or "")

        async def fail(message: str) -> None:
            writer.write(frame(ERROR, {"error": message}))
            await writer.drain()

        try:
            if op == "list":
                writer.write(frame(READY, await asyncio.to_thread(files.list, request)))
                await writer.drain()
            elif op == "get":
                path, size = await asyncio.to_thread(files.stat_file, request)
                offset = max(0, int(request.get("offset") or 0))
                writer.write(frame(READY, {"name": path.name, "size": size, "offset": offset}))
                await writer.drain()
                handle = await asyncio.to_thread(open, path, "rb")
                try:
                    await asyncio.to_thread(handle.seek, offset)
                    while True:
                        data = await asyncio.to_thread(handle.read, STREAM_CHUNK)
                        if not data:
                            break
                        writer.write(frame(DATA, data))
                        await writer.drain()
                finally:
                    handle.close()
                writer.write(frame(EXIT, {"code": 0}))
                await writer.drain()
            elif op == "put":
                started = await asyncio.to_thread(files.begin, request)
                writer.write(frame(READY, started))
                await writer.drain()
                upload = {"id": started["id"]}
                done = False
                try:
                    while True:
                        kind, payload = await read_frame(reader)
                        if kind == DATA:
                            await asyncio.to_thread(files.write_bytes, upload, payload)
                        elif kind == EXIT:
                            result = await asyncio.to_thread(files.finish, upload)
                            done = True
                            writer.write(frame(EXIT, {"code": 0, **result}))
                            await writer.drain()
                            return
                finally:
                    if not done:
                        files.abort(upload)
            else:
                await fail(f"unknown files op {op!r}")
        except FileShareError as exc:
            await fail(str(exc))
        except OSError as exc:
            await fail(f"{type(exc).__name__}: {exc}")

    def handlers(self) -> dict:
        return {"remote_ticket": self.issue}
