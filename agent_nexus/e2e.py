"""End-to-end encryption for remote-hub sessions carried through relay devices.

A relay forwards bytes between the caller and the target and must not be able to read them.
The ticket the target issued travels only through the gateway (TLS), never through the relay,
so both ends can derive keys from it:

    key(direction) = HKDF-SHA256(ticket, info=b"anx-remote/2 " + direction)

with ``direction`` b"c2t" (caller to target) and b"t2c". Each record on the wire is
``length (4 bytes, big endian) | ChaCha20-Poly1305(nonce=counter, plaintext)``; the counter
starts at 0 per direction and never repeats (a key is used by exactly one session: tickets
are single use). A record that fails to authenticate ends the session.

``wrap(reader, writer, ticket, role)`` returns a reader/writer pair with the subset of the
asyncio stream API the remote-hub code uses (``readexactly``, ``readline``, ``read``,
``at_eof``; ``write``, ``drain``, ``close``, ``wait_closed``, ``is_closing``, ``get_extra_info``),
so the services work unchanged on top of it.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAX_RECORD = 1024 * 1024 + 64
CALLER, TARGET = "caller", "target"


def ticket_id(ticket: str) -> str:
    """What the hello line carries instead of the ticket itself."""
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()[:32]


def _key(ticket: str, direction: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=b"anx-remote/2 " + direction).derive(ticket.encode("utf-8"))


class _Sealer:
    def __init__(self, key: bytes):
        self.aead = ChaCha20Poly1305(key)
        self.counter = 0

    def nonce(self) -> bytes:
        nonce = struct.pack(">4xQ", self.counter)
        self.counter += 1
        return nonce


class SecureWriter:
    def __init__(self, writer: asyncio.StreamWriter, key: bytes):
        self._writer = writer
        self._sealer = _Sealer(key)

    def write(self, data: bytes) -> None:
        if not data:
            return
        view = memoryview(bytes(data))
        while view:
            chunk, view = view[:MAX_RECORD - 64], view[MAX_RECORD - 64:]
            sealed = self._sealer.aead.encrypt(self._sealer.nonce(), bytes(chunk), None)
            self._writer.write(struct.pack(">I", len(sealed)) + sealed)

    async def drain(self) -> None:
        await self._writer.drain()

    def close(self) -> None:
        self._writer.close()

    async def wait_closed(self) -> None:
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()

    def is_closing(self) -> bool:
        return self._writer.is_closing()

    def get_extra_info(self, name, default=None):
        return self._writer.get_extra_info(name, default)

    def can_write_eof(self) -> bool:
        return False


class SecureReader(asyncio.StreamReader):
    """A StreamReader fed by a task that opens records from the raw reader."""

    def __init__(self, raw: asyncio.StreamReader, key: bytes):
        super().__init__(limit=MAX_RECORD * 2)
        self._raw = raw
        self._opener = _Sealer(key)
        self._pump = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                head = await self._raw.readexactly(4)
                (size,) = struct.unpack(">I", head)
                if size > MAX_RECORD:
                    raise ValueError("record too large")
                sealed = await self._raw.readexactly(size)
                self.feed_data(self._opener.aead.decrypt(self._opener.nonce(), sealed, None))
        except asyncio.IncompleteReadError:
            pass
        except Exception as exc:  # tampering, oversize, connection errors
            self.set_exception(ConnectionError(f"end-to-end stream broken: {type(exc).__name__}"))
            return
        finally:
            if self.exception() is None:
                self.feed_eof()

    def cancel(self) -> None:
        self._pump.cancel()


def wrap(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ticket: str,
         role: str) -> tuple[SecureReader, SecureWriter]:
    send, receive = (b"c2t", b"t2c") if role == CALLER else (b"t2c", b"c2t")
    return SecureReader(reader, _key(ticket, receive)), SecureWriter(writer, _key(ticket, send))
