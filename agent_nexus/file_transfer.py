"""This device moving files to and from another device of its network (the caller of ``files/1``).

P2P first: the other device's remote hub, ``files`` service, over the tunnel (``remote_hub.py``).
When no tunnel comes up, the gateway relays chunk by chunk (``files_bridge.py``). The local
console decides which (``web.py``); both paths offer the same three calls here.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import quote, unquote

from . import remote
from . import remote_hub as rh
from .file_share import CHUNK, STREAM_CHUNK


class TransferError(remote.RemoteError):
    """``status`` 422: the far side answered (a logical error: its ``reason`` code says which, e.g.
    ``exists``, ``checksum``, ``nospace``, ``offset``); anything else is the route (drop it, retry)."""

    def __init__(self, message: str, status: int = 502, reason: str = "", extra: dict | None = None):
        super().__init__(status, message, "files_failed")
        self.reason = reason
        self.extra = extra or {}


def _refused(body: dict) -> TransferError:
    return TransferError(str(body.get("error") or "对方拒绝"), 422, str(body.get("code") or body.get("reason") or ""),
                         {k: v for k, v in body.items() if k not in ("error", "code", "reason")})


# -- P2P ------------------------------------------------------------------------------------

async def _open(port: int, ticket: str, request: dict):
    reader, writer = await rh.as_path(port).open("files", ticket, **request)
    kind, payload = await asyncio.wait_for(rh.read_frame(reader), 30)
    if kind == rh.ERROR:
        writer.close()
        raise _refused(json.loads(payload))
    if kind != rh.READY:
        writer.close()
        raise TransferError("对方的回应无法识别")
    return reader, writer, json.loads(payload)


async def p2p_list(port: int, ticket: str, path: str) -> dict:
    _reader, writer, listing = await _open(port, ticket, {"op": "list", "path": path})
    writer.close()
    return listing


async def p2p_get(port: int, ticket: str, path: str, offset: int = 0) -> tuple[dict, AsyncIterator[bytes]]:
    reader, writer, meta = await _open(port, ticket, {"op": "get", "path": path, "offset": offset})

    async def chunks() -> AsyncIterator[bytes]:
        received = 0
        try:
            while True:
                kind, payload = await asyncio.wait_for(rh.read_frame(reader), 60)
                if kind == rh.DATA:
                    received += len(payload)
                    yield payload
                elif kind == rh.EXIT:
                    break
                elif kind == rh.ERROR:
                    raise TransferError(json.loads(payload).get("error", "传输中断"))
            if received + offset != int(meta.get("size") or 0):
                raise TransferError(f"文件不完整：收到 {received} / {meta.get('size')} 字节")
        finally:
            writer.close()

    return meta, chunks()


async def p2p_put(port: int, ticket: str, directory: str, name: str, size: int | None,
                  body: AsyncIterator[bytes]) -> dict:
    reader, writer, _started = await _open(port, ticket, {"op": "put", "dir": directory, "name": name, "size": size})
    try:
        pending = b""
        async for part in body:
            pending += part
            while len(pending) >= STREAM_CHUNK:
                writer.write(rh.frame(rh.DATA, pending[:STREAM_CHUNK]))
                pending = pending[STREAM_CHUNK:]
                await writer.drain()
        if pending:
            writer.write(rh.frame(rh.DATA, pending))
        writer.write(rh.frame(rh.EXIT, {"code": 0}))
        await writer.drain()
        kind, payload = await asyncio.wait_for(rh.read_frame(reader), 120)
        result = json.loads(payload)
        if kind == rh.ERROR:
            raise _refused({"error": "保存失败", **result})
        return result
    finally:
        writer.close()


# -- P2P, files/2: one call, and many files on one stream -----------------------------------

#: a stream's answer may take this long per byte of the file (a slow disk re-hashing it)
_BYTES_PER_S = 50 * 1024 * 1024


def answer_timeout(size: int | None) -> float:
    return 120.0 + (size or 0) / _BYTES_PER_S


async def p2p_call(route, ticket: str, op: str, params: dict, timeout: float = 120.0) -> dict:
    """``walk`` / ``stat`` / ``mkdirs`` / ``hash`` on a files/2 device (the parameters go in a begin frame)."""
    reader, writer = await rh.as_path(route).open("files", ticket, op=op)
    try:
        writer.write(rh.frame(rh.BEGIN, params))
        await writer.drain()
        kind, payload = await asyncio.wait_for(rh.read_frame(reader), timeout)
        body = json.loads(payload)
        if kind == rh.ERROR:
            raise _refused(body)
        if kind != rh.READY:
            raise TransferError("对方的回应无法识别")
        return body
    finally:
        writer.close()


class _Stream:
    def __init__(self):
        self.reader = self.writer = None
        self.broken = False

    async def _open(self, route, ticket: str, op: str) -> None:
        self.reader, self.writer, _ready = await _open(route, ticket, {"op": op})

    async def close(self) -> None:
        writer, self.writer = self.writer, None
        if writer is None:
            return
        try:
            if not self.broken:
                writer.write(rh.frame(rh.EXIT, {"end": True}))
                await asyncio.wait_for(writer.drain(), 5)
        except Exception:
            pass
        finally:
            writer.close()


class P2PPutStream(_Stream):
    """Many uploads on one ``putx`` stream (one ticket). One file at a time; each answered with
    its own result, which may come early (a refusal): then the rest of that file is not sent."""

    def __init__(self):
        super().__init__()
        self.seq = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.alive_at = 0.0
        self.task: asyncio.Task | None = None

    async def open(self, route, ticket: str) -> "P2PPutStream":
        await self._open(route, ticket, "putx")
        self.task = asyncio.create_task(self._read())
        return self

    async def _read(self) -> None:
        import time
        failure: BaseException = TransferError("连接已关闭")
        try:
            while True:
                kind, payload = await rh.read_frame(self.reader)
                body = json.loads(payload) if kind != rh.DATA else {}
                self.alive_at = time.monotonic()
                if kind == rh.RESULT:
                    future = self.pending.pop(body.get("seq"), None)
                    if future is not None and not future.done():
                        future.set_result(body)
                elif kind == rh.EXIT:
                    break
                elif kind == rh.ERROR:
                    failure = TransferError(str(body.get("error") or "传输中断"))
                    break
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ValueError) as exc:
            failure = TransferError(f"直连中断（{type(exc).__name__}）")
        finally:
            self.broken = True
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(failure)
            self.pending.clear()

    async def keepalive(self) -> None:
        """Between files: tell the far side the caller is still there (resets its idle timer)."""
        if self.broken or self.writer is None:
            return
        try:
            self.writer.write(rh.frame(rh.READY, {"keepalive": True}))
            await asyncio.wait_for(self.writer.drain(), 30)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            self.broken = True

    async def send_file(self, meta: dict, chunks: AsyncIterator[bytes], sha256) -> dict:
        """Upload one file: ``meta`` is the begin frame (base, rel, size, mtime_ns, on_exists,
        resume_key, offset); ``sha256()`` gives the source's digest once ``chunks`` is exhausted.
        Returns the result, or raises TransferError (422 = refused by the far side)."""
        import contextlib
        import time
        if self.broken or self.writer is None:
            raise TransferError("连接已关闭")
        self.seq += 1
        seq = self.seq
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda done: done.cancelled() or done.exception())
        self.pending[seq] = future
        writer = self.writer
        finished = False
        try:
            writer.write(rh.frame(rh.BEGIN, {**meta, "seq": seq}))
            async with contextlib.aclosing(chunks) as body:
                async for part in body:
                    if future.done():
                        break
                    for start in range(0, len(part), STREAM_CHUNK):
                        writer.write(rh.frame(rh.DATA, part[start:start + STREAM_CHUNK]))
                        await writer.drain()
            digest = sha256() if not future.done() else None
            writer.write(rh.frame(rh.EXIT, {"seq": seq, **({"sha256": digest} if digest else {})}))
            await writer.drain()
            finished = True
        except asyncio.CancelledError:
            # a cancelled job leaves no part behind
            with contextlib.suppress(Exception):
                writer.write(rh.frame(rh.EXIT, {"seq": seq, "abort": True, "discard": True}))
            self.broken = True
            raise
        except (ConnectionError, OSError, asyncio.IncompleteReadError) as exc:
            self.broken = True
            if isinstance(exc, (ConnectionError, asyncio.IncompleteReadError)) or not getattr(exc, "filename", None):
                raise TransferError(f"直连中断（{type(exc).__name__}）") from None
            raise
        finally:
            if not finished:
                self.pending.pop(seq, None)
                self.broken = True  # the far side is in the middle of a file: this stream is done
        deadline = time.monotonic() + answer_timeout(meta.get("size"))
        while True:
            done, _ = await asyncio.wait({future}, timeout=5)
            if done:
                break
            now = time.monotonic()
            if now - self.alive_at < 30:  # keepalives while the far side re-hashes a resumed file
                deadline = max(deadline, now + 60)
            if now > deadline:
                self.broken = True
                raise TransferError("等待对方保存超时")
        body = future.result()
        if body.get("error"):
            raise _refused(body)
        return body

    async def close(self) -> None:
        await super().close()
        if self.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 5)
            except Exception:
                self.task.cancel()


class P2PGetStream(_Stream):
    """Many downloads on one ``getx`` stream (one ticket), one file at a time."""

    async def open(self, route, ticket: str) -> "P2PGetStream":
        await self._open(route, ticket, "getx")
        self.seq = 0
        return self

    async def read(self, params: dict) -> tuple[dict, AsyncIterator[bytes], dict]:
        """(meta, chunks, final): ``final`` gets ``sha256`` / ``size`` / ``mtime_ns`` once the
        chunks are exhausted. Stopping early leaves the stream unusable (it is then closed)."""
        if self.broken or self.writer is None:
            raise TransferError("连接已关闭")
        self.seq += 1
        seq = self.seq
        reader, writer = self.reader, self.writer
        try:
            writer.write(rh.frame(rh.BEGIN, {**params, "seq": seq}))
            await writer.drain()
            kind, payload = await asyncio.wait_for(rh.read_frame(reader), 60)
        except (ConnectionError, OSError, asyncio.IncompleteReadError, asyncio.TimeoutError) as exc:
            self.broken = True
            raise TransferError(f"直连中断（{type(exc).__name__}）") from None
        body = json.loads(payload)
        if kind == rh.RESULT and body.get("error"):
            raise _refused(body)
        if kind == rh.ERROR:
            self.broken = True
            raise TransferError(str(body.get("error") or "传输中断"))
        if kind != rh.READY:
            self.broken = True
            raise TransferError("对方的回应无法识别")
        final: dict = {}

        async def chunks() -> AsyncIterator[bytes]:
            complete = False
            try:
                while True:
                    try:
                        kind, payload = await asyncio.wait_for(rh.read_frame(reader), 60)
                    except (ConnectionError, OSError, asyncio.IncompleteReadError, asyncio.TimeoutError) as exc:
                        raise TransferError(f"直连中断（{type(exc).__name__}）") from None
                    if kind == rh.DATA:
                        yield payload
                    elif kind == rh.EXIT:
                        final.update(json.loads(payload))
                        complete = True
                        return
                    elif kind == rh.ERROR:
                        raise TransferError(str(json.loads(payload).get("error") or "传输中断"))
                    elif kind == rh.RESULT:
                        complete = True
                        raise _refused(json.loads(payload))
                    # READY {"hashing"}: a keepalive while the far side hashes the prefix
            finally:
                if not complete:
                    self.broken = True

        return body, chunks(), final


# -- relay through the gateway -------------------------------------------------------------

def _base(root: Path, device_id: str) -> tuple[str, dict]:
    origin, agent_id, token = remote.credentials(root)
    return f"{origin}/api/devices/{quote(device_id, safe='')}/files", remote.headers(agent_id, token)


def _client():
    import httpx
    return httpx.AsyncClient(timeout=120, trust_env=False)


def _check(response) -> None:
    if response.status_code != 200:
        error = remote._gateway_error(response)
        if response.status_code == 422:
            error.status = 422
        raise error


async def relay_list(root: Path, device_id: str, path: str) -> dict:
    import httpx
    url, headers = _base(root, device_id)
    async with _client() as http:
        try:
            response = await http.get(url, params={"path": path}, headers=headers)
        except httpx.HTTPError:
            raise TransferError("无法连接网关") from None
    _check(response)
    return response.json()


async def relay_get(root: Path, device_id: str, path: str) -> tuple[dict, AsyncIterator[bytes]]:
    import httpx
    url, headers = _base(root, device_id)
    http = _client()
    try:
        first = await http.get(url + "/read", params={"path": path, "offset": 0, "length": CHUNK}, headers=headers)
    except httpx.HTTPError:
        await http.aclose()
        raise TransferError("无法连接网关") from None
    if first.status_code != 200:
        await http.aclose()
        _check(first)
    size = int(first.headers.get("x-anx-size") or 0)
    meta = {"name": unquote(first.headers.get("x-anx-name") or ""), "size": size}

    async def chunks() -> AsyncIterator[bytes]:
        try:
            response, offset = first, 0
            while True:
                data = response.content
                offset += len(data)
                if data:
                    yield data
                if response.headers.get("x-anx-eof") == "1" or offset >= size or not data:
                    break
                try:
                    response = await http.get(url + "/read", params={"path": path, "offset": offset, "length": CHUNK},
                                              headers=headers)
                except httpx.HTTPError:
                    raise TransferError("与网关的连接中断") from None
                _check(response)
            if offset != size:
                raise TransferError(f"文件不完整：收到 {offset} / {size} 字节")
        finally:
            await http.aclose()

    return meta, chunks()


async def relay_put(root: Path, device_id: str, directory: str, name: str, size: int | None,
                    body: AsyncIterator[bytes]) -> dict:
    import httpx
    url, headers = _base(root, device_id)
    async with _client() as http:
        try:
            begin = await http.post(url + "/put/begin", json={"dir": directory, "name": name, "size": size},
                                    headers=headers)
            _check(begin)
            upload_id = begin.json()["id"]
            done = False
            try:
                offset, pending = 0, b""

                async def send(data: bytes) -> None:
                    nonlocal offset
                    response = await http.post(url + "/put/write", params={"id": upload_id, "offset": offset},
                                               content=data, headers=headers)
                    _check(response)
                    offset += len(data)

                async for part in body:
                    pending += part
                    while len(pending) >= CHUNK:
                        await send(pending[:CHUNK])
                        pending = pending[CHUNK:]
                if pending:
                    await send(pending)
                end = await http.post(url + "/put/end", json={"id": upload_id}, headers=headers)
                _check(end)
                done = True
                return end.json()
            finally:
                if not done:
                    try:
                        await http.post(url + "/put/abort", json={"id": upload_id}, headers=headers)
                    except httpx.HTTPError:
                        pass
        except httpx.HTTPError:
            raise TransferError("与网关的连接中断") from None


class RelayClient:
    """The gateway relay with this device's credential, as ``rpc`` / ``read`` / ``write`` calls
    named like the device's ``fx_*`` reverse RPCs (the copy engine's relay transport)."""

    ROUTES = {"fx_walk": "/walk", "fx_stat_many": "/stat", "fx_mkdirs": "/mkdirs", "fx_hash": "/hash",
              "fx_put_begin": "/put/begin", "fx_put_end": "/put/end", "fx_put_abort": "/put/abort"}

    def __init__(self, root: Path, device_id: str):
        self.root, self.device_id = root, device_id
        self.http = None

    def _client(self):
        if self.http is None:
            self.http = _client()
        return self.http

    async def _send(self, method: str, url: str, **kwargs):
        import httpx
        base, headers = _base(self.root, self.device_id)
        try:
            response = await self._client().request(method, base + url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise TransferError(f"与网关的连接中断（{type(exc).__name__}）") from None
        if response.status_code == 422:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise _refused({"error": body.get("error"), "code": body.get("reason") or "", **(body.get("detail") or {})})
        _check(response)
        return response

    async def rpc(self, method: str, params: dict, timeout: float = 120.0) -> dict:
        if method == "fx_list":
            return (await self._send("GET", "", params={"path": params.get("path", "")}, timeout=timeout)).json()
        route = self.ROUTES.get(method)
        if route is None:
            raise TransferError(f"网关中转不支持 {method}", 422)
        return (await self._send("POST", route, json=params, timeout=timeout)).json()

    async def read(self, params: dict) -> tuple[bytes, dict]:
        query = {"offset": params.get("offset", 0), "length": params.get("length", CHUNK)}
        if params.get("base") not in (None, ""):
            query.update(base=params["base"], rel=json.dumps(params.get("rel") or [], ensure_ascii=False))
        else:
            query["path"] = params.get("path", "")
        response = await self._send("GET", "/read", params=query)
        meta = {"size": int(response.headers.get("x-anx-size") or 0), "name": unquote(response.headers.get("x-anx-name") or ""),
                "eof": response.headers.get("x-anx-eof") == "1"}
        if response.headers.get("x-anx-mtime"):
            meta["mtime_ns"] = int(response.headers["x-anx-mtime"])
        return response.content, meta

    async def write(self, upload_id: str, offset: int, data: bytes) -> dict:
        return (await self._send("POST", "/put/write", params={"id": upload_id, "offset": offset}, content=data)).json()

    async def close(self) -> None:
        if self.http is not None:
            http, self.http = self.http, None
            await http.aclose()
