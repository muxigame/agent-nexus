"""File transfer between devices (files/1): the share, the P2P stream, the relay's chunks."""
from __future__ import annotations

import asyncio
import base64
import os
import types

import pytest

from agent_nexus import file_share, file_transfer
from agent_nexus.remote_hub import RemoteHub


def share(tmp_path):
    account = types.SimpleNamespace(home=str(tmp_path), session=None, uid=os.getuid() if hasattr(os, "getuid") else 0,
                                    gid=os.getgid() if hasattr(os, "getgid") else 0)
    (tmp_path / "Downloads").mkdir()
    return file_share.FileShare(account=lambda: account)


def test_list_defaults_to_downloads_and_sorts_folders_first(tmp_path):
    files = share(tmp_path)
    (tmp_path / "Downloads" / "b.txt").write_bytes(b"12345")
    (tmp_path / "Downloads" / "a-folder").mkdir()
    listing = files.list({})
    assert listing["path"] == str(tmp_path / "Downloads")
    assert [e["name"] for e in listing["entries"]] == ["a-folder", "b.txt"]
    assert listing["entries"][1]["size"] == 5 and listing["entries"][0]["dir"] is True
    assert files.list({"path": "~"})["path"] == str(tmp_path)
    with pytest.raises(file_share.FileShareError):
        files.list({"path": "relative/path"})


def test_relay_chunks_round_trip_and_never_overwrite(tmp_path):
    files = share(tmp_path)
    handlers = files.handlers()
    payload = os.urandom(file_share.CHUNK + 1234)

    async def run():
        begin = await handlers["fx_put_begin"]({"name": "../evil/name.bin", "size": len(payload)})
        assert "error" not in begin and begin["name"] == "name.bin"
        offset = 0
        for start in range(0, len(payload), file_share.CHUNK):
            part = payload[start:start + file_share.CHUNK]
            result = await handlers["fx_put_write"]({"id": begin["id"], "offset": offset,
                                                     "data": base64.b64encode(part).decode()})
            assert "error" not in result
            offset += len(part)
        first = await handlers["fx_put_end"]({"id": begin["id"]})
        again = await handlers["fx_put_begin"]({"name": "name.bin", "size": 1})
        await handlers["fx_put_write"]({"id": again["id"], "offset": 0, "data": base64.b64encode(b"x").decode()})
        second = await handlers["fx_put_end"]({"id": again["id"]})
        read = await handlers["fx_read"]({"path": first["path"], "offset": file_share.CHUNK})
        short = await handlers["fx_put_begin"]({"name": "short.bin", "size": 10})
        incomplete = await handlers["fx_put_end"]({"id": short["id"]})
        return first, second, read, incomplete

    first, second, read, incomplete = asyncio.run(run())
    assert first["path"] == str(tmp_path / "Downloads" / "name.bin")
    assert (tmp_path / "Downloads" / "name.bin").read_bytes() == payload
    assert second["path"] == str(tmp_path / "Downloads" / "name (1).bin")
    assert base64.b64decode(read["data"]) == payload[file_share.CHUNK:] and read["eof"] is True
    assert "不完整" in incomplete["error"]
    assert not [p for p in (tmp_path / "Downloads").iterdir() if p.name.endswith(".anxpart")]


def test_p2p_stream_list_get_put(tmp_path):
    files = share(tmp_path)
    data = os.urandom(file_share.STREAM_CHUNK * 3 + 17)
    (tmp_path / "Downloads" / "big.bin").write_bytes(data)

    async def run():
        hub = RemoteHub(terminals=None, files=files)
        port = await hub.start()
        try:
            async def ticket():
                return (await hub.issue({"service": "files", "from": "test"}))["ticket"]

            listing = await file_transfer.p2p_list(port, await ticket(), "")
            meta, chunks = await file_transfer.p2p_get(port, await ticket(), str(tmp_path / "Downloads" / "big.bin"))
            got = b"".join([chunk async for chunk in chunks])

            async def body():
                for start in range(0, len(data), 100_000):
                    yield data[start:start + 100_000]

            put = await file_transfer.p2p_put(port, await ticket(), str(tmp_path / "in"), "up.bin", len(data), body())
            with pytest.raises(file_transfer.TransferError) as missing:
                await file_transfer.p2p_get(port, await ticket(), str(tmp_path / "nope.bin"))
            with pytest.raises(file_transfer.TransferError):
                await file_transfer.p2p_list(port, "not-a-ticket", "")
            return listing, meta, got, put, missing.value
        finally:
            await hub.close()

    listing, meta, got, put, missing = asyncio.run(run())
    assert [e["name"] for e in listing["entries"]] == ["big.bin"]
    assert meta["size"] == len(data) and got == data
    assert put["path"] == str(tmp_path / "in" / "up.bin") and (tmp_path / "in" / "up.bin").read_bytes() == data
    assert missing.status == 422


def test_files_ticket_needs_the_share():
    hub = RemoteHub(terminals=None)
    assert "error" in asyncio.run(hub.issue({"service": "files"}))
