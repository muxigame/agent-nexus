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


# -- files/2: many files on one stream (putx / getx), walk and stat over P2P ---------------------------

def _hub_run(files, body):
    async def run():
        hub = RemoteHub(terminals=None, files=files)
        port = await hub.start()
        try:
            async def ticket():
                return (await hub.issue({"service": "files", "from": "test"}))["ticket"]
            return await body(port, ticket)
        finally:
            await hub.close()
    return asyncio.run(run())


async def _chunks(data, size=70_000):
    for start in range(0, len(data), size):
        yield data[start:start + size]


def test_putx_many_files_on_one_ticket_and_per_file_errors(tmp_path):
    import hashlib
    files = share(tmp_path)
    small = {f"f{i:03d}.csv": os.urandom(i * 13) for i in range(200)}
    big = os.urandom(file_share.STREAM_CHUNK * 3 + 5)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "taken.bin").write_bytes(b"old")

    async def body(port, ticket):
        stream = await file_transfer.P2PPutStream().open(port, await ticket())
        results = []
        for name, data in small.items():
            digest = hashlib.sha256(data).hexdigest()
            results.append(await stream.send_file({"base": str(tmp_path / "out"), "rel": ["small", name], "size": len(data),
                                                   "on_exists": "error", "offset": 0}, _chunks(data), lambda d=digest: d))
        with pytest.raises(file_transfer.TransferError) as exists:  # refused early; the stream lives on
            await stream.send_file({"base": str(tmp_path / "out"), "rel": ["taken.bin"], "size": len(big),
                                    "on_exists": "error", "offset": 0}, _chunks(big), lambda: None)
        with pytest.raises(file_transfer.TransferError) as checksum:
            await stream.send_file({"base": str(tmp_path / "out"), "rel": ["bad.bin"], "size": 3, "offset": 0},
                                   _chunks(b"abc"), lambda: "0" * 64)
        digest = hashlib.sha256(big).hexdigest()
        results.append(await stream.send_file({"base": str(tmp_path / "out"), "rel": ["big.bin"], "size": len(big),
                                               "mtime_ns": 1_500_000_000_000_000_000, "offset": 0},
                                              _chunks(big), lambda: digest))
        await stream.close()
        return results, exists.value, checksum.value

    results, exists, checksum = _hub_run(files, body)
    assert len(results) == 201 and all("error" not in r for r in results)
    assert all((tmp_path / "out" / "small" / name).read_bytes() == data for name, data in small.items())
    assert (tmp_path / "out" / "big.bin").read_bytes() == big
    assert (tmp_path / "out" / "big.bin").stat().st_mtime_ns == 1_500_000_000_000_000_000
    assert exists.status == 422 and exists.reason == "exists" and (tmp_path / "out" / "taken.bin").read_bytes() == b"old"
    assert checksum.reason == "checksum" and not (tmp_path / "out" / "bad.bin").exists()
    assert not [p for p in (tmp_path / "out").rglob("*.anxpart")]


def test_putx_offset_mismatch_and_keepalive_while_rehashing(tmp_path, monkeypatch):
    import hashlib
    import time as _time

    from agent_nexus import remote_hub
    files = share(tmp_path)
    data = os.urandom(file_share.CHUNK * 2 + 3)
    key = "r" * 20
    (tmp_path / "dst").mkdir()
    (tmp_path / "dst" / file_share.part_name("big.bin", key)).write_bytes(data[:1000])
    monkeypatch.setattr(remote_hub, "HASH_KEEPALIVE_S", 0.05)
    slow = files._rehash

    def rehash(upload):
        _time.sleep(0.4)  # a slow disk: several keepalives go out meanwhile
        slow(upload)
    monkeypatch.setattr(files, "_rehash", rehash)
    digest = hashlib.sha256(data).hexdigest()

    async def body(port, ticket):
        stream = await file_transfer.P2PPutStream().open(port, await ticket())
        meta = {"base": str(tmp_path / "dst"), "rel": ["big.bin"], "size": len(data), "resume_key": key}
        with pytest.raises(file_transfer.TransferError) as wrong:
            await stream.send_file({**meta, "offset": 500}, _chunks(data[500:]), lambda: digest)
        have = wrong.value.extra["have"]
        seen = stream.alive_at
        result = await stream.send_file({**meta, "offset": have}, _chunks(data[have:]), lambda: digest)
        await stream.close()
        return wrong.value, result, seen, stream.alive_at

    wrong, result, before, after = _hub_run(files, body)
    assert wrong.reason == "offset" and wrong.extra["have"] == 1000
    assert result["sha256"] == digest and (tmp_path / "dst" / "big.bin").read_bytes() == data
    assert after > before


def test_getx_many_files_resume_and_missing(tmp_path):
    import hashlib
    files = share(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    blobs = {f"{i}.bin": os.urandom(i * 1000 + 1) for i in range(30)}
    for name, data in blobs.items():
        (src / name).write_bytes(data)
    big = os.urandom(file_share.STREAM_CHUNK * 2 + 9)
    (src / "big.bin").write_bytes(big)

    async def body(port, ticket):
        stream = await file_transfer.P2PGetStream().open(port, await ticket())
        got = {}
        for name in blobs:
            meta, chunks, final = await stream.read({"base": str(src), "rel": [name], "offset": 0})
            got[name] = (b"".join([c async for c in chunks]), final["sha256"], meta["size"])
        meta, chunks, final = await stream.read({"base": str(src), "rel": ["big.bin"], "offset": 1234})
        tail = b"".join([c async for c in chunks])
        with pytest.raises(file_transfer.TransferError) as missing:
            await stream.read({"base": str(src), "rel": ["nope"], "offset": 0})
        meta2, chunks2, final2 = await stream.read({"path": str(src / "0.bin"), "offset": 0})  # still usable
        again = b"".join([c async for c in chunks2])
        walked = await file_transfer.p2p_call(port, await ticket(), "walk", {"path": str(src)})
        stats = await file_transfer.p2p_call(port, await ticket(), "stat", {"base": str(src), "rels": [["big.bin"], ["x"]]})
        await stream.close()
        return got, tail, final, missing.value, again, walked, stats

    got, tail, final, missing, again, walked, stats = _hub_run(files, body)
    for name, data in blobs.items():
        assert got[name] == (data, hashlib.sha256(data).hexdigest(), len(data))
    assert tail == big[1234:] and final["sha256"] == hashlib.sha256(big).hexdigest()
    assert missing.status == 422 and again == blobs["0.bin"]
    assert len(walked["entries"]) == 31
    assert stats["items"][0]["size"] == len(big) and stats["items"][1] == {"exists": False}
