"""Relay devices (docs/relay-mesh.md): end-to-end sealed sessions forwarded byte for byte."""
from __future__ import annotations

import asyncio
import os
import types

import pytest

from agent_nexus import e2e, file_share, file_transfer
from agent_nexus.remote_hub import Path, RemoteHub, RouteError, hello, read_frame, READY


def share(tmp_path):
    account = types.SimpleNamespace(home=str(tmp_path), session=None, uid=os.getuid() if hasattr(os, "getuid") else 0,
                                    gid=os.getgid() if hasattr(os, "getgid") else 0)
    (tmp_path / "Downloads").mkdir()
    return file_share.FileShare(account=lambda: account)


def test_sealed_streams_round_trip_and_reject_tampering():
    async def run():
        received: list[bytes] = []
        done = asyncio.Event()

        async def target(reader, writer):
            secure_reader, secure_writer = e2e.wrap(reader, writer, "the-ticket", e2e.TARGET)
            received.append(await secure_reader.readexactly(11))
            secure_writer.write(b"pong" * 1000)
            await secure_writer.drain()
            done.set()

        server = await asyncio.start_server(target, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        secure_reader, secure_writer = e2e.wrap(reader, writer, "the-ticket", e2e.CALLER)
        secure_writer.write(b"hello world")
        await secure_writer.drain()
        answer = await secure_reader.readexactly(4000)
        await done.wait()
        writer.close()

        # a wrong key (another ticket) cannot read the stream
        async def liar(reader, writer):
            _, w = e2e.wrap(reader, writer, "the-ticket", e2e.TARGET)
            w.write(b"secret")
            await w.drain()

        server2 = await asyncio.start_server(liar, "127.0.0.1", 0)
        r2, w2 = await asyncio.open_connection("127.0.0.1", server2.sockets[0].getsockname()[1])
        bad_reader, _ = e2e.wrap(r2, w2, "another-ticket", e2e.CALLER)
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(bad_reader.readexactly(6), 5)
        w2.close()
        server.close()
        server2.close()
        return received, answer

    received, answer = asyncio.run(run())
    assert received == [b"hello world"]
    assert answer == b"pong" * 1000


class Tap:
    """Records what a relay forwards, to prove it only ever sees ciphertext."""

    def __init__(self):
        self.seen = bytearray()

    def wrap(self, reader, writer):
        tap = self

        class Reader:
            async def read(self, n):
                data = await reader.read(n)
                tap.seen.extend(data)
                return data

        class Writer:
            def write(self, data):
                tap.seen.extend(data)
                writer.write(data)

            async def drain(self):
                await writer.drain()

            def close(self):
                writer.close()

        return Reader(), Writer()


def test_files_through_one_and_two_relays_are_end_to_end(tmp_path):
    files = share(tmp_path)
    secret = b"TOP-SECRET-CONTENT-" * 4000
    (tmp_path / "Downloads" / "plan.txt").write_bytes(secret)
    tap = Tap()

    async def run():
        target = RemoteHub(terminals=None, files=files)
        target_port = await target.start()

        async def to_target(target_id, via, caller):
            assert target_id == "T"
            if via:  # relay A -> relay B, which then goes to T
                reader, writer = await asyncio.open_connection("127.0.0.1", relay_b_port)
                ticket = (await relay_b.issue({"service": "relay", "from": "A"}))["ticket"]
                writer.write(hello("relay", ticket, target=target_id))
                await writer.drain()
                kind, _ = await read_frame(reader)
                assert kind == READY
                return reader, writer
            reader, writer = await asyncio.open_connection("127.0.0.1", target_port)
            return tap.wrap(reader, writer)

        relay_b = RemoteHub(terminals=None, relay=to_target)
        relay_b_port = await relay_b.start()
        relay_a = RemoteHub(terminals=None, relay=to_target)
        relay_a_port = await relay_a.start()
        try:
            async def files_ticket():
                return (await target.issue({"service": "files", "from": "caller"}))["ticket"]

            # one hop: caller -> B -> T (relay tickets are single use too: one per stream)
            async def via_b():
                return Path(relay_b_port, relay_ticket=(await relay_b.issue({"service": "relay"}))["ticket"], target="T")

            ticket = await files_ticket()
            listing = await file_transfer.p2p_list(await via_b(), ticket, "")
            meta, chunks = await file_transfer.p2p_get(await via_b(), await files_ticket(),
                                                       str(tmp_path / "Downloads" / "plan.txt"))
            got = b"".join([c async for c in chunks])
            # two hops: caller -> A -> B -> T
            two = Path(relay_a_port, relay_ticket=(await relay_a.issue({"service": "relay"}))["ticket"],
                       target="T", via="B")
            body = b"uploaded through two relays"

            async def source():
                yield body

            put = await file_transfer.p2p_put(two, await files_ticket(), "", "up.txt", len(body), source())
            # a relay that cannot reach the target says so before anything else
            async def unreachable(target_id, via, caller):
                raise OSError("no tunnel")
            dead = RemoteHub(terminals=None, relay=unreachable)
            dead_port = await dead.start()
            broken = Path(dead_port, relay_ticket=(await dead.issue({"service": "relay"}))["ticket"], target="T")
            with pytest.raises(RouteError):
                await file_transfer.p2p_list(broken, await files_ticket(), "")
            await dead.close()
            return ticket, listing, meta, got, put
        finally:
            await relay_a.close()
            await relay_b.close()
            await target.close()

    ticket, listing, meta, got, put = asyncio.run(run())
    assert [e["name"] for e in listing["entries"]] == ["plan.txt"]
    assert got == secret and meta["size"] == len(secret)
    assert (tmp_path / "Downloads" / "up.txt").read_bytes() == b"uploaded through two relays"
    assert put["size"] == len(b"uploaded through two relays")
    # the relay forwarded the whole file and the hello, yet saw neither the content nor the ticket
    assert len(tap.seen) > len(secret)
    assert b"TOP-SECRET" not in bytes(tap.seen)
    assert ticket.encode() not in bytes(tap.seen)


def test_relay_service_only_where_enabled():
    async def run():
        plain = RemoteHub(terminals=None)
        relay = RemoteHub(terminals=None, relay=lambda *a: None)
        return await plain.issue({"service": "relay"}), await relay.issue({"service": "relay"})

    refused, issued = asyncio.run(run())
    assert "error" in refused and "ticket" in issued
