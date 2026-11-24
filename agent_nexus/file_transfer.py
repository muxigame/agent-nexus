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
    def __init__(self, message: str, status: int = 502):
        super().__init__(status, message, "files_failed")


# -- P2P ------------------------------------------------------------------------------------

async def _open(port: int, ticket: str, request: dict):
    reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 10)
    writer.write(rh.hello("files", ticket, **request))
    await writer.drain()
    kind, payload = await asyncio.wait_for(rh.read_frame(reader), 30)
    if kind == rh.ERROR:
        writer.close()
        raise TransferError(json.loads(payload).get("error", "对方拒绝"), 422)
    if kind != rh.READY:
        writer.close()
        raise TransferError("对方的回应无法识别")
    return reader, writer, json.loads(payload)


async def p2p_list(port: int, ticket: str, path: str) -> dict:
    _reader, writer, listing = await _open(port, ticket, {"op": "list", "path": path})
    writer.close()
    return listing


async def p2p_get(port: int, ticket: str, path: str) -> tuple[dict, AsyncIterator[bytes]]:
    reader, writer, meta = await _open(port, ticket, {"op": "get", "path": path})

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
            if received != int(meta.get("size") or 0):
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
            raise TransferError(result.get("error", "保存失败"), 422)
        return result
    finally:
        writer.close()


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
