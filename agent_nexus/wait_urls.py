"""Wait URLs: a capability URL that streams a long job's output as plain text until it ends.

``exec_command`` / ``exec_wait`` results for a command still running (and ``file_copy`` /
``file_copy_status`` for a running copy) carry ``wait: curl -sN <url>``. Any client with a shell,
even one without the Agent Nexus client installed, can then wait for hours without polling: the
stream ends with one ``[anx] ...`` line (``[anx] exit 0``, ``[anx] copy done: ...``); a short
device disconnect is waited out (``OFFLINE_GRACE_S``), and the same URL fetched again continues
where the last stream left off.

Tokens (in memory on the gateway, lost on restart):

- random-looking, 192 bit, derived as HMAC(per-process secret, job + minter + the minter's
  generation) so repeated ``exec_wait`` calls hand out the same URL; stored only as their SHA-256.
  Locking or revoking a device bumps its generation, so a re-mint afterwards is a new token and an
  old copy of the URL stays dead;
- bound to one job on one device (the device's random job uid, so a session id reused after a
  plugin restart never shows another process), read-only;
- expire ``TTL_AFTER_END_S`` after the job ended, ``IDLE_MAX_S`` after their last use when nobody
  saw the job end, ``MAX_AGE_S`` at most;
- a token minted through ``/mcp/device`` (a device's 本机 MCP) works only with that device's
  credential and dies when that device is locked or revoked;
- never logged: :class:`RedactAccessLog` hides the path in uvicorn's access log.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

PATH_PREFIX = "/wait/"
TTL_AFTER_END_S = 600.0
MAX_AGE_S = 7 * 86400.0
#: A token whose job end nobody observed (an idle reap, a result never fetched) dies this long
#: after it was last minted or streamed.
IDLE_MAX_S = 24 * 3600.0
#: How long a stream waits for the job's device (or the minting device) to come back after a
#: disconnect before it ends with ``device offline``; keepalive lines are written meanwhile.
OFFLINE_GRACE_S = 180.0
OFFLINE_POLL_S = 1.0
MAX_PER_MINTER = 200
MAX_STREAMS = 4
#: Silence after which a stream writes ``[anx] still running (...)`` (proxies cut idle streams:
#: Cloudflare at about 100 s, the example nginx at 600 s); also how a dead client is noticed.
KEEPALIVE_S = 50.0
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


@dataclass
class WaitEntry:
    kind: str  # "exec" | "copy"
    agent_id: str | None
    minter: str  # the OAuth client_id, "device:<id>" through /mcp/device, or "owner"
    mcp: str | None = None
    session_id: int | None = None
    job: str | None = None  # the device's job uid (exec_tail); None on devices without it
    since: int = 0
    job_id: str | None = None  # file_copy
    created: float = field(default_factory=time.time)
    used: float = field(default_factory=time.time)
    ended_at: float | None = None
    revoked: bool = False
    streams: int = 0

    @property
    def device(self) -> str | None:
        """The minting device (a token from ``/mcp/device`` needs its credential)."""
        return self.minter[7:] if self.minter.startswith("device:") else None

    def end(self) -> None:
        if self.ended_at is None:
            self.ended_at = time.time()


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii", "replace")).digest()


class WaitTokens:
    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)
        self._entries: dict[bytes, WaitEntry] = {}
        #: minter -> generation, bumped by revoke_device (part of the HMAC input).
        self._generation: dict[str, int] = {}

    # -- minting ---------------------------------------------------------------------------------------------
    def mint(self, kind: str, *, minter: str, agent_id: str | None = None, mcp: str | None = None,
             session_id: int | None = None, job: str | None = None, since: int = 0,
             job_id: str | None = None) -> str | None:
        """The token for this job and minter (the same one again while it lives); None over the limit."""
        self.prune()
        key = json.dumps([kind, minter, self._generation.get(minter, 0), agent_id, mcp, session_id, job, job_id],
                         separators=(",", ":"))
        token = hmac.new(self._secret, key.encode(), hashlib.sha256).digest()[:24]
        text = base64.urlsafe_b64encode(token).decode("ascii").rstrip("=")
        digest = _digest(text)
        entry = self._entries.get(digest)
        if entry is not None and not entry.revoked:
            entry.used = time.time()
            return text
        if sum(1 for item in self._entries.values() if item.minter == minter) >= MAX_PER_MINTER:
            return None
        self._entries[digest] = WaitEntry(kind=kind, agent_id=agent_id, minter=minter, mcp=mcp,
                                          session_id=session_id, job=job, since=int(since or 0), job_id=job_id)
        return text

    def lookup(self, token: str) -> WaitEntry | None:
        if not isinstance(token, str) or not TOKEN_RE.match(token):
            return None
        digest = _digest(token)
        for stored, entry in self._entries.items():
            if hmac.compare_digest(stored, digest):
                break
        else:
            return None
        now = time.time()
        if entry.revoked or self._expired(entry, now):
            return None
        entry.used = now
        return entry

    def ended(self, kind: str, agent_id: str | None, *, session_id: int | None = None,
              job_id: str | None = None) -> None:
        """A result showed the job ended: its tokens start their expiry."""
        for entry in self._entries.values():
            if entry.kind == kind and entry.agent_id == agent_id and entry.ended_at is None and (
                    (kind == "exec" and entry.session_id == session_id) or (kind == "copy" and entry.job_id == job_id)):
                entry.end()

    def revoke_device(self, agent_id: str) -> int:
        """The device was locked or its credential revoked: every token it minted dies, streams included."""
        count = 0
        minter = f"device:{agent_id}"
        self._generation[minter] = self._generation.get(minter, 0) + 1
        for digest, entry in list(self._entries.items()):
            if entry.device == agent_id:
                entry.revoked = True
                self._entries.pop(digest, None)
                count += 1
        return count

    @staticmethod
    def _expired(entry: WaitEntry, now: float) -> bool:
        if now - entry.created > MAX_AGE_S:
            return True
        if entry.ended_at is None and entry.streams == 0 and now - entry.used > IDLE_MAX_S:
            return True
        return entry.ended_at is not None and now - entry.ended_at > TTL_AFTER_END_S and entry.streams == 0

    def prune(self) -> None:
        now = time.time()
        for digest, entry in list(self._entries.items()):
            if self._expired(entry, now):
                self._entries.pop(digest, None)

    def __len__(self) -> int:
        return len(self._entries)


def wait_hint(url: str, *, windows: bool = False) -> str:
    """How to wait on ``url``; ``curl.exe`` on Windows (in Windows PowerShell 5.1 ``curl`` is
    Invoke-WebRequest; curl.exe works in cmd, PowerShell and Git Bash alike)."""
    return f"{'curl.exe' if windows else 'curl'} -sN {url}"


#: The lines that end a stream for good (the job ended or the URL is dead); anything else as the
#: last line (``still running``, ``copying``, nothing) means the stream was cut: fetch it again.
FINAL_PREFIXES = ("[anx] exit ", "[anx] killed", "[anx] ended", "[anx] copy ", "[anx] revoked",
                  "[anx] job ended", "[anx] device offline", "[anx] error", "[anx] not found")


def structured(result: Any) -> dict | None:
    """A device tool result's structured content (a CallToolResult dict)."""
    if not isinstance(result, dict):
        return None
    data = result.get("structuredContent")
    if isinstance(data, dict):
        return data
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            with contextlib.suppress(ValueError, TypeError):
                parsed = json.loads(item.get("text") or "")
                if isinstance(parsed, dict):
                    return parsed
    return None


def _elapsed(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _size(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class Streamer:
    """Writes one entry's stream: ``write(text)`` is awaited for every piece."""

    def __init__(self, gateway: Any, entry: WaitEntry, write: Callable[[str], Awaitable[None]], *,
                 quiet: bool = False, keepalive_s: float | None = None, grace_s: float | None = None,
                 poll_s: float | None = None):
        self.gateway = gateway
        self.entry = entry
        self.write = write
        self.quiet = quiet
        self.keepalive_s = KEEPALIVE_S if keepalive_s is None else keepalive_s
        self.started = time.monotonic()
        self.last_write = time.monotonic()
        self.partial = False
        self.grace_s = OFFLINE_GRACE_S if grace_s is None else grace_s
        self.poll_s = OFFLINE_POLL_S if poll_s is None else poll_s

    @property
    def segment_s(self) -> float:
        return max(1.0, float(getattr(self.gateway, "tool_wait_max_s", 50.0)))

    async def emit(self, text: str) -> None:
        if not text:
            return
        await self.write(text)
        self.partial = not text.endswith("\n")
        self.last_write = time.monotonic()

    async def final(self, line: str) -> None:
        await self.emit(("\n" if self.partial else "") + f"[anx] {line}\n")

    async def keepalive(self) -> None:
        if self.quiet or time.monotonic() - self.last_write < self.keepalive_s:
            return
        await self.final(f"still running ({_elapsed(time.monotonic() - self.started)})")

    OFFLINE = "device offline; job may still be running (call exec_wait)"

    async def await_peer(self, agent_id: str, *, stale: Any = None) -> Any:
        """The device's live peer; after a disconnect wait up to ``grace_s`` for it to come back
        (a WebSocket reconnect, a Wi-Fi blip), writing keepalives. None when it did not."""
        deadline = time.monotonic() + self.grace_s
        while True:
            peer = self.gateway.hub.peers.get(agent_id)
            if peer is not None and peer is not stale and not getattr(peer, "_closed", False):
                return peer
            if self.entry.revoked or time.monotonic() >= deadline:
                return None
            await asyncio.sleep(min(self.poll_s, max(0.0, deadline - time.monotonic())))
            await self.keepalive()

    async def problem(self) -> str | None:
        """Why the stream must stop now (checked before every segment)."""
        entry = self.entry
        if entry.revoked:
            return "revoked"
        device = entry.device
        if device is not None:
            minter = await self.await_peer(device)
            if entry.revoked:
                return "revoked"
            if minter is None:
                return self.OFFLINE
            if getattr(minter, "locked", False):
                return "revoked"
        return None

    async def run(self) -> None:
        if self.entry.kind == "copy":
            await self.copy()
        else:
            await self.exec()

    # -- exec ------------------------------------------------------------------------------------------------
    def _has_tool(self, peer: Any, name: str) -> bool:
        mcp = (getattr(peer, "catalog", None) or {}).get("mcps", {}).get(self.entry.mcp) or {}
        return any(isinstance(tool, dict) and tool.get("name") == name for tool in mcp.get("tools", []))

    async def exec(self) -> None:
        entry = self.entry
        since = entry.since
        stale = None
        while True:
            problem = await self.problem()
            if problem:
                await self.final(problem)
                return
            peer = await self.await_peer(entry.agent_id, stale=stale)
            if peer is None:
                await self.final(self.OFFLINE)
                return
            stale = None
            seg_ms = int(self.segment_s * 1000)
            tail = entry.job is not None and self._has_tool(peer, "exec_tail")
            if tail:
                tool, arguments = "exec_tail", {"session_id": entry.session_id, "job": entry.job, "since": since,
                                                "timeout_ms": seg_ms}
            elif self._has_tool(peer, "exec_wait"):
                # An older device: consuming waits (the MCP client then sees no new output for these).
                tool, arguments = "exec_wait", {"session_id": entry.session_id, "timeout_ms": seg_ms}
            else:
                tool, arguments = "write_stdin", {"session_id": entry.session_id, "chars": "", "yield_time_ms": seg_ms}
            try:
                raw = await peer.call("tool_call", {"mcp": entry.mcp, "tool": tool, "arguments": arguments,
                                                    "workspace": None, "scope": "wait"})
            except asyncio.TimeoutError:
                await self.keepalive()
                continue
            except Exception as exc:  # noqa: BLE001 - the link dropped, the device went away
                if self.gateway.hub.peers.get(entry.agent_id) is not peer or getattr(peer, "_closed", False):
                    stale = peer  # it reconnects as a new Peer: wait for that (await_peer), same ``since``
                    continue
                await self.final(f"error: {type(exc).__name__}")
                return
            data = structured(raw) or {}
            if data.get("status") == "error" or (isinstance(raw, dict) and raw.get("isError") and not data):
                message = str(data.get("error") or "failed")
                if "unknown exec" in message or "different job" in message:
                    entry.end()
                    await self.final("job ended or unknown (call exec_wait for its exit code)")
                else:
                    await self.final(f"error: {message[:300]}")
                return
            output = data.get("output") if isinstance(data.get("output"), str) else ""
            if data.get("skipped"):
                await self.emit(("\n" if self.partial else "") + "[anx] (older output was dropped)\n")
            await self.emit(output)
            if tail and isinstance(data.get("seq"), int):
                since = data["seq"]
                # Written: the same URL fetched again (the stream was cut) continues from here.
                entry.since = max(entry.since, since)
            code = data.get("exit_code")
            running = data.get("running") if tail else code is None
            if not running:
                entry.end()
                self.gateway.wait_urls.ended("exec", entry.agent_id, session_id=entry.session_id)
                if isinstance(code, int) and code < 0:
                    await self.final(f"killed (signal {-code})")
                elif code is None:
                    await self.final("ended (exit code unknown)")
                else:
                    await self.final(f"exit {code}")
                return
            await self.keepalive()

    # -- file copy -------------------------------------------------------------------------------------------
    async def copy(self) -> None:
        from .file_copy import TERMINAL
        entry = self.entry
        service = self.gateway.file_copy
        last = None
        unknown_since = None
        while True:
            problem = await self.problem()
            if problem:
                await self.final(problem)
                return
            seg_ms = int(self.segment_s * 1000)
            try:
                snap = await service.status(entry.job_id, seg_ms, cap_ms=seg_ms)
            except Exception as exc:  # noqa: BLE001
                await self.final(f"copy failed: {str(exc)[:300] or type(exc).__name__}")
                return
            state = snap.get("state")
            files = snap.get("files") if isinstance(snap.get("files"), dict) else {}
            size = snap.get("bytes") if isinstance(snap.get("bytes"), dict) else {}
            if state in TERMINAL:
                entry.end()
                self.gateway.wait_urls.ended("copy", entry.agent_id, job_id=entry.job_id)
                if state in ("completed", "completed_with_errors"):
                    extra = f", {snap.get('errors_total')} errors" if state == "completed_with_errors" else ""
                    await self.final(f"copy done: {files.get('done', 0)} files, {_size(size.get('done'))}{extra}")
                else:
                    await self.final(f"copy {state}: {str(snap.get('message') or state)[:300]}")
                return
            if state == "unknown":
                # Usually a device that is reconnecting: keep asking for the grace period.
                now = time.monotonic()
                unknown_since = now if unknown_since is None else unknown_since
                if now - unknown_since >= self.grace_s:
                    await self.final(f"copy unknown: {str(snap.get('message') or '')[:300]}")
                    return
                await asyncio.sleep(self.poll_s)
                await self.keepalive()
                continue
            unknown_since = None
            # Progress lines never start with "[anx] copy " (that shape is reserved for the outcome).
            label = "copying" if state == "running" else f"copying ({state})"
            line = (f"[anx] {label} {files.get('done', 0)}/{files.get('total', 0)} files, "
                    f"{_size(size.get('done'))}/{_size(size.get('total'))}")
            if line != last or time.monotonic() - self.last_write >= self.keepalive_s:
                await self.emit(line + "\n")
                last = line


class RedactAccessLog(logging.Filter):
    """uvicorn.access: never write a capability token (``/wait/<token>``, ``/media/<token>``) to a log."""

    PATTERN = re.compile(r"/(wait|media)/[^\s\"?]+")

    def filter(self, record: logging.LogRecord) -> bool:
        with contextlib.suppress(Exception):
            if isinstance(record.args, tuple):
                record.args = tuple(self.PATTERN.sub(r"/\1/***", arg) if isinstance(arg, str) else arg
                                    for arg in record.args)
            if isinstance(record.msg, str):
                record.msg = self.PATTERN.sub(r"/\1/***", record.msg)
        return True


def install_log_redaction() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, RedactAccessLog) for item in logger.filters):
        logger.addFilter(RedactAccessLog())


async def watch_disconnect(receive: Callable[[], Awaitable[dict]]) -> None:
    """Returns when the HTTP client went away (a silent StreamingResponse would not notice)."""
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            return


PLAIN_HEADERS = [(b"content-type", b"text/plain; charset=utf-8"), (b"cache-control", b"no-store"),
                 (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
                 (b"x-accel-buffering", b"no")]


async def plain(send: Callable[[dict], Awaitable[None]], status: int, text: str) -> None:
    data = text.encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [*PLAIN_HEADERS, (b"content-length", str(len(data)).encode())]})
    await send({"type": "http.response.body", "body": data})


async def stream_with(receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]],
                      produce: Callable[[Callable[[str], Awaitable[None]]], Awaitable[None]]) -> str:
    """Start a 200 text/plain response and run ``produce(write)`` until it ends or the client leaves."""
    await send({"type": "http.response.start", "status": 200, "headers": list(PLAIN_HEADERS)})

    async def write(text: str) -> None:
        await send({"type": "http.response.body", "body": text.encode("utf-8"), "more_body": True})

    producer = asyncio.ensure_future(produce(write))
    watcher = asyncio.ensure_future(watch_disconnect(receive))
    try:
        await asyncio.wait({producer, watcher}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (producer, watcher):
            if not task.done():
                task.cancel()
        for task in (producer, watcher):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    if producer.cancelled() or not producer.done():
        return "client_gone"
    with contextlib.suppress(Exception):
        await send({"type": "http.response.body", "body": b""})
    if producer.exception() is not None:
        log.info("wait stream ended (%s)", type(producer.exception()).__name__)
        return "error"
    return "ok"


NOT_FOUND = "[anx] not found or expired (call exec_wait or file_copy_status)\n"


class WaitEndpoint:
    """``GET /wait/{token}`` on the gateway (raw ASGI, mounted as a Route endpoint)."""

    def __init__(self, gateway: Any, *, device_credential: Callable[[str, str], bool]):
        self.gateway = gateway
        self.device_credential = device_credential

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            return
        if scope.get("method") not in ("GET", "HEAD"):
            await plain(send, 405, "method not allowed")
            return
        path = scope.get("path") or ""
        token = path[len(PATH_PREFIX):] if path.startswith(PATH_PREFIX) else ""
        tokens: WaitTokens = self.gateway.wait_urls
        entry = tokens.lookup(token)
        if entry is None:
            await plain(send, 404, NOT_FOUND)
            return
        device = entry.device
        if device is not None:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
            auth = headers.get("authorization", "")
            credential = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
            ok = False
            with contextlib.suppress(Exception):
                ok = headers.get("x-anx-device", "").strip() == device and bool(self.device_credential(device, credential))
            if not ok:
                await plain(send, 404, NOT_FOUND)
                return
            peer = self.gateway.hub.peers.get(device)
            if peer is not None and getattr(peer, "locked", False):
                await plain(send, 423, "locked\n")
                return
        if entry.streams >= MAX_STREAMS:
            await plain(send, 429, "too many streams for this URL\n")
            return
        query = (scope.get("query_string") or b"").decode("latin-1")
        quiet = "quiet=1" in query.split("&")
        if scope.get("method") == "HEAD":
            await send({"type": "http.response.start", "status": 200, "headers": list(PLAIN_HEADERS)})
            await send({"type": "http.response.body", "body": b""})
            return
        entry.streams += 1
        try:
            await stream_with(receive, send, lambda write: Streamer(self.gateway, entry, write, quiet=quiet).run())
        finally:
            entry.streams -= 1
