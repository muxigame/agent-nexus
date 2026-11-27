"""Copying a file or a folder from one device to another: the job engine (docs/design.md §18).

One :class:`CopyJob` reads from a source :class:`Endpoint` and writes to a destination one. The
same engine runs

* on a device (``fxjob/1``, ``file_copy_device.py``): its own disk (:class:`LocalEndpoint`) on one
  side, the other device through the P2P tunnel or a relay device, else the gateway relay
  (``LinkEndpoint``) on the other -- the bytes never pass the gateway when a tunnel exists;
* on the gateway when neither device can run jobs (``file_copy_gateway.py``): both sides through
  their ``fx_*`` reverse RPCs (:class:`RelayEndpoint` over the device link). Slow, but correct.

Relative paths travel as lists of names; each device joins them with its own rules, so ``/`` and
``\\`` never mix between Windows and macOS / Linux. Every file goes to a temporary part that the
destination renames when complete, after checking the sha256 the reading side computed. A part
left by a broken or interrupted copy is kept under a deterministic name (``resume_key``), so
running the same copy again skips identical files and resumes partial ones.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from .file_share import CHUNK, FileShare, FileShareError, disk_full, invalid_name, open_read, same_mtime

log = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 4
MAX_CONCURRENCY = 8
ATTEMPTS = 4
BACKOFF_S = (2.0, 5.0, 15.0)
STAT_BATCH = 1000
MAX_ERRORS_KEPT = 50
MAX_CURRENT = 8
TERMINAL = ("completed", "completed_with_errors", "failed", "cancelled", "interrupted")
#: per-file refusals that end the whole job
FATAL_CODES = ("nospace", "locked")
#: per-file refusals worth one more attempt
RETRY_CODES = ("offset", "gone", "busy")
#: the copy is ours: a cancelled or failed copy may delete its own part
DISCARD_CODES = ("nospace", "checksum", "changed", "unverified")
#: this many files in a row lost to a broken route (every attempt) stop the job: the far device
#: is offline or asleep, and grinding through the rest would take days
OFFLINE_AFTER = 4


class CopyError(Exception):
    """A refusal or failure with a message for people and a ``code`` for programs."""

    def __init__(self, message: str, code: str = "", **extra: Any):
        super().__init__(message)
        self.code = code
        self.extra = extra


class TransportError(CopyError):
    """The route broke (a tunnel, the relay, the gateway): drop it and try again."""

    def __init__(self, message: str):
        super().__init__(message, "transport")


def classify(exc: BaseException) -> CopyError:
    """Any exception of an endpoint as a :class:`CopyError`."""
    from . import remote
    from .federation import RoutingError
    from .file_transfer import TransferError
    if isinstance(exc, CopyError):
        return exc
    if isinstance(exc, FileShareError):
        return CopyError(str(exc), exc.code, **exc.extra)
    if isinstance(exc, TransferError):
        if exc.status == 422:
            return CopyError(exc.message, exc.reason, **exc.extra)
        return TransportError(exc.message)
    if isinstance(exc, remote.RemoteError):
        if exc.status == 423 or exc.code == "locked":
            return CopyError(exc.message, "locked")
        if exc.code == "needs_files2":
            return CopyError(exc.message, "needs_files2")
        return TransportError(exc.message)
    if isinstance(exc, RoutingError):
        text = str(exc)
        return CopyError(text.removeprefix("locked: "), "locked") if text.startswith("locked") else TransportError(text)
    if disk_full(exc):
        return CopyError("目标磁盘已满", "nospace")
    if isinstance(exc, PermissionError):
        return CopyError(f"没有权限：{exc.filename or exc}", "permission")
    if isinstance(exc, (ConnectionError, asyncio.TimeoutError, asyncio.IncompleteReadError, TimeoutError)):
        return TransportError(f"连接中断（{type(exc).__name__}）")
    if isinstance(exc, OSError):
        return CopyError(f"{type(exc).__name__}: {exc}", "io")
    return CopyError(f"{type(exc).__name__}: {exc}", "internal")


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# -- reading --------------------------------------------------------------------------------

class Reader:
    """One source file being read from ``offset``: its chunks, and afterwards the sha256 of the
    WHOLE file as the reading side saw it (None when it could not tell) and its facts after."""

    def __init__(self, meta: dict, chunks: AsyncIterator[bytes], digest: Callable[[], str | None],
                 after: Callable[[], dict | None] = lambda: None, release: Callable[[], None] = lambda: None):
        self.meta = meta
        self.chunks = chunks
        self.digest = digest
        self.after = after
        self.release = release

    async def aclose(self) -> None:
        """Drop a reader that will not be (fully) consumed."""
        with contextlib.suppress(BaseException):
            await self.chunks.aclose()
        with contextlib.suppress(BaseException):
            self.release()


def local_reader(path: Path, offset: int, chunk: int = CHUNK, nofollow: bool = False) -> Awaitable[Reader]:
    """A reader over this device's own disk. A resumed prefix is hashed here, before the reader is
    returned (in a thread): a stream that is about to carry the file is not left idle meanwhile."""
    async def opened() -> Reader:
        try:
            handle = await asyncio.to_thread(open_read, path, nofollow)
            info = await asyncio.to_thread(os.fstat, handle.fileno())
        except Exception as exc:
            raise classify(exc) from None
        hasher = hashlib.sha256()
        facts: dict = {}

        def prefix() -> None:
            left = offset
            while left > 0:
                block = handle.read(min(CHUNK, left))
                if not block:
                    break
                hasher.update(block)
                left -= len(block)
            handle.seek(offset)

        def block() -> bytes:
            data = handle.read(chunk)
            hasher.update(data)
            return data

        if offset:
            try:
                await asyncio.to_thread(prefix)
            except BaseException as exc:
                handle.close()
                if isinstance(exc, Exception):
                    raise classify(exc) from None
                raise

        async def chunks() -> AsyncIterator[bytes]:
            try:
                while True:
                    data = await asyncio.to_thread(block)
                    if not data:
                        break
                    yield data
                after = await asyncio.to_thread(os.stat, path)
                facts.update(size=after.st_size, mtime_ns=after.st_mtime_ns)
            except OSError as exc:
                raise classify(exc) from None
            finally:
                handle.close()

        return Reader({"size": info.st_size, "mtime_ns": info.st_mtime_ns, "name": path.name}, chunks(),
                      hasher.hexdigest, lambda: facts or None, handle.close)

    return opened()


# -- endpoints --------------------------------------------------------------------------------

class Session:
    """What one worker uses for one side: kept across the files it copies (an open stream)."""
    route = ""

    async def read(self, base: str, rel: list, offset: int) -> Reader:
        raise NotImplementedError

    async def put(self, base: str, rel: list, *, size: int, mtime_ns: int | None, offset: int, on_exists: str,
                  resume_key: str | None, open_source: Callable[[int], Awaitable[Reader]],
                  mode: int | None = None) -> dict:
        """Write one file. ``offset``: where the caller believes the destination's part ends
        (the destination's own answer wins); ``mode``: the source's permission bits."""
        raise NotImplementedError

    async def reset(self) -> None:
        """The last call failed in transport: drop the route; the next call picks one anew."""

    async def close(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


class Endpoint:
    """One side of a copy. ``v2``: the far side is ``files/2`` (resume, overwrite, sha256 at the
    destination, mtime kept); ``resumable``: reads can start at an offset with a whole-file sha256."""
    v2 = True
    resumable = True
    platform: str | None = None
    label = ""

    async def walk(self, path: str) -> AsyncIterator[dict]:
        raise NotImplementedError
        yield {}  # pragma: no cover

    async def stat_many(self, base: str, rels: list, keys: list) -> dict:
        raise NotImplementedError

    async def mkdirs(self, base: str, rels: list) -> None:
        raise NotImplementedError

    def session(self) -> Session:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class LocalEndpoint(Endpoint):
    """This device's own disk, through the same FileShare the other devices use."""
    label = "本机"

    def __init__(self, share: FileShare):
        self.share = share
        self.platform = sys.platform

    async def _call(self, fn, params: dict):
        try:
            return await asyncio.to_thread(fn, params)
        except Exception as exc:
            raise classify(exc) from None

    async def walk(self, path: str) -> AsyncIterator[dict]:
        cursor = None
        while True:
            page = await self._call(self.share.walk, {"path": path, "cursor": cursor, "limit": 2000})
            yield page
            cursor = page.get("next")
            if not cursor:
                return

    async def stat_many(self, base: str, rels: list, keys: list) -> dict:
        return await self._call(self.share.stat_many, {"base": base, "rels": rels, "keys": keys})

    async def mkdirs(self, base: str, rels: list) -> None:
        await self._call(self.share.mkdirs, {"base": base, "rels": rels})

    def session(self) -> Session:
        return LocalSession(self)


async def write_through(share: FileShare, params: dict, open_source: Callable[[int], Awaitable[Reader]]) -> dict:
    """Write one file into ``share`` (begin, chunks, finish) from the reader ``open_source`` gives."""
    try:
        started = await asyncio.to_thread(share.begin, params)
    except Exception as exc:
        raise classify(exc) from None
    if started.get("skipped"):
        return started
    upload = {"id": started["id"]}
    discard = False
    reader = None

    async def hold() -> None:
        # the source may hash a resumed prefix for minutes before its first byte (a pull: the far
        # side's getx): this upload is busy, not idle, and must not be reaped meanwhile
        while True:
            await asyncio.sleep(30)
            held = share.uploads.get(started["id"])
            if held is not None:
                held.touched = time.monotonic()

    holding = asyncio.ensure_future(hold())
    try:
        reader = await open_source(int(started.get("have") or 0))
        async with contextlib.aclosing(reader.chunks) as chunks:
            async for data in chunks:
                await asyncio.to_thread(share.write_bytes, upload, data)
        result = await asyncio.to_thread(share.finish, {**upload, "sha256": reader.digest()})
        upload = None
        return result
    except asyncio.CancelledError:
        discard = True
        raise
    except Exception as exc:
        error = classify(exc)
        discard = error.code in DISCARD_CODES
        raise error from None
    finally:
        holding.cancel()
        if reader is not None:
            reader.release()
        if upload is not None:
            share.abort({**upload, "discard": discard})


class LocalSession(Session):
    route = "本机"

    def __init__(self, endpoint: LocalEndpoint):
        self.share = endpoint.share

    async def read(self, base: str, rel: list, offset: int) -> Reader:
        try:
            path, _size = await asyncio.to_thread(self.share.stat_file, {"base": base, "rel": rel})
        except Exception as exc:
            raise classify(exc) from None
        return await local_reader(path, offset, nofollow=bool(rel))

    async def put(self, base, rel, *, size, mtime_ns, offset, on_exists, resume_key, open_source, mode=None) -> dict:
        params = {"base": base, "rel": rel, "size": size, "mtime_ns": mtime_ns, "on_exists": on_exists,
                  "resume_key": resume_key, "resume": offset > 0, "mode": mode}
        return await write_through(self.share, params, open_source)


class RelayEndpoint(Endpoint):
    """A device reached through ``fx_*`` calls: by the gateway itself (``PeerTransport``) or by a
    device through the gateway's HTTP relay (``file_transfer.RelayClient``).

    ``transport``: ``rpc(method, params) -> dict`` (raises on a refusal), ``read(params) -> (bytes,
    meta)``, ``write(id, offset, bytes)``. A ``files/1`` device is walked with ``fx_list`` and
    stat'ed by listing parents; it cannot overwrite, resume or check a sha256."""

    def __init__(self, transport, *, v2: bool, label: str = "网关中转"):
        self.transport = transport
        self.v2 = v2
        self.resumable = v2
        self.label = label
        #: files/1: the walk's root as the device spelled it, and its separator
        self.v1_root: dict[str, tuple[str, str, bool]] = {}

    async def rpc(self, method: str, params: dict, timeout: float | None = None) -> dict:
        try:
            if timeout is None:
                return await self.transport.rpc(method, params)
            return await self.transport.rpc(method, params, timeout=timeout)
        except Exception as exc:
            raise classify(exc) from None

    # -- files/1 path spelling
    @staticmethod
    def _sep(text: str) -> str:
        windows = ("\\" in text and "/" not in text) or (len(text) > 1 and text[1] == ":")
        return "\\" if windows else "/"

    def v1_path(self, base: str, rel: list) -> str:
        root, sep, _is_file = self.v1_root.get(base) or (base, self._sep(base), False)
        if not rel:
            return root
        return root.rstrip("/\\") + sep + sep.join(rel)

    def v1_split(self, base: str, rel: list) -> tuple[str, str]:
        path = base.rstrip("/\\") if not rel else self.v1_path(base, rel)
        if not rel:
            cut = max(path.rfind("/"), path.rfind("\\"))
            return path[:cut] or path[:cut + 1], path[cut + 1:]
        if len(rel) > 1:
            return self.v1_path(base, rel[:-1]), rel[-1]
        root = self.v1_root.get(base, (base,))[0]
        folder = root.rstrip("/\\")
        if not folder or folder.endswith(":"):  # "/" or "C:\\": keep the root's separator
            folder = root
        return folder, rel[-1]

    async def walk(self, path: str) -> AsyncIterator[dict]:
        if self.v2:
            cursor = None
            while True:
                page = await self.rpc("fx_walk", {"path": path, "cursor": cursor, "limit": 2000})
                yield page
                cursor = page.get("next")
                if not cursor:
                    return
        try:
            listing = await self.rpc("fx_list", {"path": path})
        except CopyError as exc:
            if "不是文件夹" not in str(exc):
                raise
            parent, name = self.v1_split(path, [])
            listing = await self.rpc("fx_list", {"path": parent})
            found = next((e for e in listing.get("entries", []) if e.get("name") == name and not e.get("dir")), None)
            if found is None:
                raise CopyError(f"路径不存在：{path}", "missing") from None
            sep = listing.get("sep") or self._sep(path)
            full = listing["path"].rstrip("/\\") + sep + name
            self.v1_root[path] = (full, sep, True)
            mtime_ns = int(found.get("mtime") or 0) * 1_000_000_000
            yield {"root": {"path": full, "name": name, "dir": False, "size": found.get("size", 0), "mtime_ns": mtime_ns},
                   "entries": [{"rel": [], "dir": False, "size": found.get("size", 0), "mtime_ns": mtime_ns}]}
            return
        sep = listing.get("sep") or self._sep(path)
        root = listing["path"]
        self.v1_root[path] = (root, sep, False)
        name = root.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1] or root
        stack: list[tuple[dict, list]] = [(listing, [])]
        first = True
        while stack:
            current, rel = stack.pop()
            entries = []
            if current.get("truncated"):
                entries.append({"rel": rel, "dir": True, "error": "文件夹内容太多，旧版客户端无法完整列出（请升级该设备）"})
            children = []
            for entry in sorted(current.get("entries", []), key=lambda e: e.get("name", "")):
                child = rel + [entry["name"]]
                entries.append({"rel": child, "dir": bool(entry.get("dir")), "size": entry.get("size", 0),
                                "mtime_ns": int(entry.get("mtime") or 0) * 1_000_000_000, "link": False})
                if entry.get("dir"):
                    children.append(child)
            for child in reversed(children):
                try:
                    stack.append((await self.rpc("fx_list", {"path": self.v1_path(path, child)}), child))
                except CopyError as exc:
                    if isinstance(exc, TransportError):
                        raise
                    entries.append({"rel": child, "dir": True, "error": str(exc)})
            yield {"root": {"path": root, "name": name, "dir": True, "size": 0, "mtime_ns": 0} if first else None,
                   "entries": entries}
            first = False

    async def stat_many(self, base: str, rels: list, keys: list) -> dict:
        if self.v2:
            return await self.rpc("fx_stat_many", {"base": base, "rels": rels, "keys": keys})
        listings: dict[str, dict | None] = {}
        items = []
        for rel in rels:
            folder, name = self.v1_split(base, rel)
            if folder not in listings:
                try:
                    listings[folder] = await self.rpc("fx_list", {"path": folder})
                except CopyError as exc:
                    if isinstance(exc, TransportError):
                        raise
                    listings[folder] = None
            listing = listings[folder]
            found = next((e for e in (listing or {}).get("entries", []) if e.get("name") == name), None)
            if found is None and (listing or {}).get("truncated"):
                # the old client lists at most 5000 entries: absent from the list proves nothing,
                # and its uploads never overwrite (they would land as "name (1)")
                items.append({"exists": False, "error": "目标文件夹内容太多，旧版客户端无法确认文件是否已存在（请升级该设备到 0.8.31 以上）"})
            elif found is None:
                items.append({"exists": False})
            else:
                items.append({"exists": True, "dir": bool(found.get("dir")), "size": found.get("size", 0),
                              "mtime_ns": None})
        return {"items": items, "free": None, "platform": None}

    async def mkdirs(self, base: str, rels: list) -> None:
        if not self.v2:
            raise CopyError("目标设备客户端版本过旧，无法创建空文件夹（需要 0.8.31 以上）", "needs_files2")
        await self.rpc("fx_mkdirs", {"base": base, "rels": rels})

    def session(self) -> Session:
        return RelaySession(self)


class RelaySession(Session):
    def __init__(self, endpoint: RelayEndpoint):
        self.endpoint = endpoint
        self.transport = endpoint.transport
        self.route = endpoint.label

    async def read(self, base: str, rel: list, offset: int) -> Reader:
        endpoint = self.endpoint
        where = {"base": base, "rel": rel} if endpoint.v2 else {"path": endpoint.v1_path(base, rel)}
        if not endpoint.resumable:
            offset = 0
        try:
            first, meta = await self.transport.read({**where, "offset": offset, "length": CHUNK})
        except Exception as exc:
            raise classify(exc) from None
        size = int(meta.get("size") or 0)
        hasher = hashlib.sha256() if offset == 0 else None
        whole: dict = {}
        queue: asyncio.Queue = asyncio.Queue(maxsize=3)

        async def produce() -> None:
            position = offset + len(first)
            eof = meta.get("eof") or position >= size or not first
            try:
                await queue.put(first)
                while not eof:
                    data, more = await self.transport.read({**where, "offset": position, "length": CHUNK})
                    position += len(data)
                    eof = more.get("eof") or position >= size or not data
                    await queue.put(data)
                await queue.put(None)
            except Exception as exc:
                await queue.put(classify(exc))

        async def chunks() -> AsyncIterator[bytes]:
            task = asyncio.ensure_future(produce())
            received = offset
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    if isinstance(item, BaseException):
                        raise item
                    received += len(item)
                    if hasher is not None:
                        hasher.update(item)
                    yield item
                if received != size:
                    raise TransportError(f"文件不完整：收到 {received} / {size} 字节")
                if hasher is None and endpoint.v2:
                    # resumed: the source hashes the whole file on its own disk (no transfer). When
                    # that fails the digest stays None, and the destination refuses to keep the
                    # resumed file unverified ("unverified"): the engine then sends it from 0.
                    with contextlib.suppress(CopyError):
                        from .file_transfer import answer_timeout
                        whole.update(await endpoint.rpc("fx_hash", {**where}, timeout=answer_timeout(size)))
            finally:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

        def digest() -> str | None:
            return hasher.hexdigest() if hasher is not None else whole.get("sha256")

        return Reader({"size": size, "mtime_ns": meta.get("mtime_ns"), "name": meta.get("name")}, chunks(), digest)

    async def put(self, base, rel, *, size, mtime_ns, offset, on_exists, resume_key, open_source, mode=None) -> dict:
        endpoint = self.endpoint
        if endpoint.v2:
            params = {"base": base, "rel": rel, "size": size, "mtime_ns": mtime_ns, "on_exists": on_exists,
                      "resume_key": resume_key, "resume": offset > 0}
            if mode is not None:
                params["mode"] = mode
        else:
            offset = 0
            folder, name = endpoint.v1_split(base, rel)
            params = {"dir": folder, "name": name, "size": size}
        # the source first: hashing a resumed prefix may take minutes, and an upload left idle
        # that long on the far device would be reaped
        reader = await open_source(offset)
        beginning = asyncio.ensure_future(endpoint.rpc("fx_put_begin", params))
        try:
            started = await asyncio.shield(beginning)
        except asyncio.CancelledError:
            # cancelled while the far side opens the part: let it answer, then drop that part (else it stays)
            with contextlib.suppress(BaseException):
                opened = await asyncio.wait_for(asyncio.shield(beginning), 15)
                if opened.get("id"):
                    await _settled(endpoint.rpc("fx_put_abort", {"id": opened["id"], "discard": True}), 15)
            await reader.aclose()
            raise
        except BaseException:
            await reader.aclose()
            raise
        if started.get("skipped"):
            await reader.aclose()
            return started
        upload_id = started["id"]
        discard = False
        done = False
        try:
            position = int(started.get("have") or 0)
            if position != offset:  # the part holds something else than the caller thought
                await reader.aclose()
                reader = await open_source(position)
            async with contextlib.aclosing(reader.chunks) as chunks:
                async for data in chunks:
                    for start in range(0, len(data), CHUNK):
                        piece = data[start:start + CHUNK]
                        try:
                            await self.transport.write(upload_id, position, piece)
                        except Exception as exc:
                            raise classify(exc) from None
                        position += len(piece)
            if endpoint.v2:
                end = {"id": upload_id, "sha256": reader.digest(), "mtime_ns": mtime_ns, "wait_s": 20}
                while True:
                    result = await endpoint.rpc("fx_put_end", end)
                    if result.get("state") != "hashing":
                        break
                    await asyncio.sleep(2)
            else:
                result = await endpoint.rpc("fx_put_end", {"id": upload_id})
            done = True
            return result
        except asyncio.CancelledError:
            discard = True
            raise
        except CopyError as exc:
            discard = exc.code in DISCARD_CODES
            raise
        finally:
            await reader.aclose()
            if not done:
                await _settled(endpoint.rpc("fx_put_abort", {"id": upload_id, "discard": discard}), 15)


async def _settled(call: Awaitable, timeout: float) -> None:
    """Run a clean-up call to its end even while the caller is being cancelled (a cancelled job is
    cancelled again by its own shutdown): the far side must hear it, or an abandoned part stays."""
    task = asyncio.ensure_future(call)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(task), timeout)
        raise
    except Exception:  # noqa: BLE001 - best effort: the far side reaps an idle part after 10 minutes
        pass


# -- the job ------------------------------------------------------------------------------------

@dataclass
class Item:
    rel: list
    dst_rel: list
    size: int
    mtime_ns: int
    key: str = ""
    mode: int | None = None
    on_exists: str = "error"
    have: int = 0
    counted: int = 0
    tries: int = 0


@dataclass
class Plan:
    files: list = field(default_factory=list)
    dirs: list = field(default_factory=list)
    nonempty: set = field(default_factory=set)
    #: ``[source name]`` when dst_path ends with a separator (copy into that folder)
    prefix: list = field(default_factory=list)
    #: the source's top as its device spells it (``~`` expanded)
    root_path: str = ""


def resume_key(spec: dict, item_rel: list, size: int, mtime_ns: int, dst_rel: list) -> str:
    raw = json.dumps([spec.get("src_device"), spec.get("src_path"), item_rel, size, mtime_ns,
                      spec.get("dst_device"), spec.get("dst_path"), dst_rel], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _norm(path: str, fold: bool) -> str:
    text = path.replace("\\", "/").rstrip("/")
    return text.casefold() if fold else text


def overlaps(src: str, dst: str, fold: bool) -> bool:
    """Whether ``dst`` is ``src`` or lies inside it (paths as one device spells them)."""
    a, b = _norm(src, fold), _norm(dst, fold)
    return b == a or b.startswith(a + "/")


class CopyJob:
    """One copy: plan (walk + what the destination has), check, copy with N workers, report.

    ``spec``: ``id``, ``src_device``, ``src_path``, ``dst_device``, ``dst_path``, ``overwrite``,
    ``concurrency``. ``snapshot(dict)`` is called every few seconds and at the end."""

    def __init__(self, spec: dict, src: Endpoint, dst: Endpoint, *,
                 snapshot: Callable[[dict], None] | None = None, info: dict | None = None):
        self.spec = dict(spec)
        self.id = str(spec.get("id") or secrets.token_urlsafe(9))
        self.src, self.dst = src, dst
        self.on_snapshot = snapshot
        self.info = dict(info or {})
        self.state = "queued"
        self.message = ""
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.files = {"total": 0, "done": 0, "skipped": 0, "failed": 0, "in_progress": 0}
        self.bytes = {"total": 0, "done": 0}
        self.errors: list[dict] = []
        self.errors_total = 0
        self.skipped_sample: list[dict] = []
        self.warnings: list[str] = []
        self.current: dict[int, dict] = {}
        self.routes: dict[str, int] = {}
        self.verified: set[str] = set()
        self.rate_bps = 0.0
        self._rate_at = time.monotonic()
        self._rate_bytes = 0
        self._event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._cancelled = False
        self._fatal: CopyError | None = None
        self._workers: list[asyncio.Task] = []
        #: files lost in a row to a broken route (see OFFLINE_AFTER)
        self._lost_in_a_row = 0
        concurrency = spec.get("concurrency")
        limit = int(spec.get("max_concurrency") or MAX_CONCURRENCY)
        self.concurrency = max(1, min(int(concurrency or DEFAULT_CONCURRENCY), limit))

    # -- state
    def _changed(self) -> None:
        self._event.set()
        self._event = asyncio.Event()

    @property
    def done(self) -> bool:
        return self.state in TERMINAL

    async def wait(self, timeout: float) -> None:
        """Until the next change (a file started or finished) or ``timeout``."""
        if self.done or timeout <= 0:
            return
        event = self._event
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(event.wait(), timeout)

    async def wait_done(self, timeout: float) -> None:
        """Until the job ends or ``timeout`` (a status long poll: one answer per ``wait_ms``)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self.done:
            left = deadline - loop.time()
            if left <= 0:
                return
            await self.wait(left)

    def error(self, rel: list, message: str) -> None:
        self.errors_total += 1
        if len(self.errors) < MAX_ERRORS_KEPT:
            self.errors.append({"rel": "/".join(rel) or "(根)", "error": message})
        self._changed()

    def warn(self, message: str) -> None:
        if message not in self.warnings and len(self.warnings) < 20:
            self.warnings.append(message)

    def _skip(self, rel: list, reason: str, count: bool = True) -> None:
        if count:
            self.files["skipped"] += 1
        if len(self.skipped_sample) < 20:
            self.skipped_sample.append({"rel": "/".join(rel) or "(根)", "reason": reason})

    def _tick_rate(self) -> None:
        now = time.monotonic()
        elapsed = now - self._rate_at
        if elapsed >= 1.0:
            sample = (self.bytes["done"] - self._rate_bytes) / elapsed
            alpha = min(1.0, elapsed / 10.0)  # an EMA over about ten seconds
            self.rate_bps = sample if self.rate_bps == 0 else self.rate_bps + alpha * (sample - self.rate_bps)
            self._rate_at, self._rate_bytes = now, self.bytes["done"]

    def snapshot(self) -> dict:
        self._tick_rate()
        left = max(0, self.bytes["total"] - self.bytes["done"])
        verified = "sha256" if self.verified == {"sha256"} else ("size" if self.verified == {"size"} else
                                                                 ("mixed" if self.verified else None))
        return {"job_id": self.id, "state": self.state, **self.info,
                "src": {"device": self.spec.get("src_device"), "path": self.spec.get("src_path")},
                "dst": {"device": self.spec.get("dst_device"), "path": self.spec.get("dst_path")},
                "overwrite": bool(self.spec.get("overwrite")), "concurrency": self.concurrency,
                "files": dict(self.files), "bytes": dict(self.bytes),
                "rate_bps": round(self.rate_bps), "eta_s": round(left / self.rate_bps) if self.rate_bps > 1 and left else None,
                "current": list(self.current.values())[:MAX_CURRENT],
                "routes": [{"route": route, "files": count} for route, count in self.routes.items()],
                "verified": verified, "errors": list(self.errors), "errors_total": self.errors_total,
                "skipped": list(self.skipped_sample), "warnings": list(self.warnings), "message": self.message,
                "created_at": self.created_at, "finished_at": self.finished_at}

    # -- control
    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self.run())
        return self._task

    async def cancel(self) -> None:
        if self.done:
            return
        self._cancelled = True
        for worker in self._workers:
            worker.cancel()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait({self._task}, timeout=30)

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(5)
            self._report()

    def _report(self) -> None:
        if self.on_snapshot is not None:
            with contextlib.suppress(Exception):
                self.on_snapshot(self.snapshot())

    # -- the run
    async def run(self) -> dict:
        ticker = asyncio.create_task(self._ticker())
        try:
            self.state = "scanning"
            self._changed()
            plan = await self._plan()
            await self._decide(plan)
            self.state = "copying"
            self._changed()
            await self._copy(plan)
            if self._fatal is not None:
                raise self._fatal
            self.state = "finishing"
            await self._empty_dirs(plan)
            self.state = "completed_with_errors" if self.errors_total or self.files["failed"] else "completed"
            if self.state == "completed_with_errors" and not self.message:
                self.message = f"{self.errors_total} 个问题，详见 errors"
        except asyncio.CancelledError:
            self.state = "cancelled" if self._cancelled else "interrupted"
            self.message = self.message or ("已取消；已完成的文件保留" if self._cancelled else
                                            "设备重启中断；重新调用 file_copy 会跳过已完成的文件并续传未完成的文件")
        except CopyError as exc:
            self.state = "failed"
            self.message = str(exc)
        except Exception as exc:
            log.exception("copy job %s failed", self.id)
            self.state = "failed"
            self.message = f"{type(exc).__name__}: {exc}"
        finally:
            ticker.cancel()
            self.finished_at = time.time()
            self.current.clear()
            self._changed()
            self._report()
            with contextlib.suppress(Exception):
                await self.src.close()
            with contextlib.suppress(Exception):
                await self.dst.close()
        return self.snapshot()

    async def _plan(self) -> Plan:
        spec = self.spec
        src_path, dst_path = str(spec["src_path"]), str(spec["dst_path"])
        into = dst_path.endswith(("/", "\\"))
        plan = Plan()
        prefix: list = []
        root_seen = False
        async for page in self.src.walk(src_path):
            root = page.get("root")
            if root and not root_seen:
                root_seen = True
                plan.root_path = str(root.get("path") or src_path)
                if into:
                    prefix = plan.prefix = [root.get("name") or "copy"]
                if spec.get("same_device"):
                    fold = (self.dst.platform or sys.platform) in ("win32", "darwin")
                    target = dst_path + (prefix[0] if prefix else "")
                    if overlaps(root.get("path") or src_path, target, fold) or overlaps(src_path, target, fold):
                        raise CopyError("目标在源文件夹之内（或就是源本身），不能复制到自身里面", "overlap")
                if root.get("dir"):
                    plan.dirs.append([])
            for entry in page.get("entries", []):
                rel = list(entry.get("rel") or [])
                if entry.get("error"):
                    self.error(rel, entry["error"])
                    continue
                if entry.get("link"):
                    self._skip(rel, "符号链接未复制", count=False)
                    continue
                if entry.get("special"):
                    self._skip(rel, "特殊文件（设备、管道等）未复制", count=False)
                    continue
                if entry.get("dir"):
                    plan.dirs.append(rel)
                    continue
                size, mtime_ns = int(entry.get("size") or 0), int(entry.get("mtime_ns") or 0)
                mode = entry.get("mode")
                plan.files.append(Item(rel, prefix + rel, size, mtime_ns,
                                       mode=mode if isinstance(mode, int) and 0 <= mode <= 0o7777 else None))
                for depth in range(len(rel)):
                    plan.nonempty.add(tuple(rel[:depth]))
                self.files["total"] += 1
            self._changed()
        if not root_seen:
            raise CopyError(f"路径不存在：{src_path}", "missing")
        return plan

    async def _decide(self, plan: Plan) -> None:
        """Against what the destination already has: skip identical files, refuse or overwrite
        different ones, resume parts; then check the free space and name clashes."""
        spec = self.spec
        dst_path = str(spec["dst_path"])
        overwrite = bool(spec.get("overwrite"))
        keep: list[Item] = []
        free = None
        platform = self.dst.platform
        if spec.get("same_device"):
            # the destination as the device spells it ("~" expanded): never into the source itself
            answer = await self.dst.stat_many(dst_path, [], [])
            base = str(answer.get("base") or dst_path)
            fold = (answer.get("platform") or self.dst.platform or sys.platform) in ("win32", "darwin")
            sep = "\\" if (answer.get("platform") == "win32") else "/"
            target = base.rstrip("/\\") + sep + plan.prefix[0] if plan.prefix else base
            if overlaps(plan.root_path or str(spec["src_path"]), target, fold):
                raise CopyError("目标在源文件夹之内（或就是源本身），不能复制到自身里面", "overlap")
        for start in range(0, len(plan.files), STAT_BATCH):
            batch = plan.files[start:start + STAT_BATCH]
            for item in batch:
                item.key = resume_key(spec, item.rel, item.size, item.mtime_ns, item.dst_rel)
            answer = await self.dst.stat_many(dst_path, [item.dst_rel for item in batch],
                                              [item.key if self.dst.v2 else None for item in batch])
            if start == 0:
                free = answer.get("free")
                platform = answer.get("platform") or platform
            for item, found in zip(batch, answer.get("items") or []):
                if found.get("error"):
                    self.error(item.dst_rel, found["error"])
                    self.files["failed"] += 1
                    continue
                if found.get("exists"):
                    if found.get("dir"):
                        self.error(item.dst_rel, "目标位置是一个同名文件夹")
                        self.files["failed"] += 1
                        continue
                    if found.get("link"):
                        self.error(item.dst_rel, "目标位置是符号链接，不覆盖也不跟随")
                        self.files["failed"] += 1
                        continue
                    same_size = int(found.get("size") or 0) == item.size
                    same_time = found.get("mtime_ns") is None or same_mtime(item.mtime_ns, found["mtime_ns"])
                    if same_size and same_time:
                        self._skip(item.dst_rel, "目标已有相同文件")
                        continue
                    if not overwrite:
                        self.error(item.dst_rel, "目标已存在且内容不同（overwrite=false）")
                        self.files["failed"] += 1
                        continue
                    if not self.dst.v2:
                        self.error(item.dst_rel, "目标设备客户端版本过旧，不支持覆盖（需要 0.8.31 以上）")
                        self.files["failed"] += 1
                        continue
                    item.on_exists = "overwrite"
                else:
                    item.on_exists = "error" if self.dst.v2 else "rename"
                have = int(found.get("have") or 0)
                if self.src.resumable and self.dst.v2 and 0 < have <= item.size:
                    item.have = have
                keep.append(item)
        if platform in ("win32", "darwin"):
            seen: dict[str, list] = {}
            clean: list[Item] = []
            for item in keep:
                folded = "/".join(item.dst_rel).casefold()
                if folded in seen:
                    self.error(item.dst_rel, f"与 {'/'.join(seen[folded])} 仅大小写不同，目标系统不区分大小写")
                    self.files["failed"] += 1
                    continue
                seen[folded] = item.dst_rel
                clean.append(item)
            keep = clean
        if platform == "win32":
            clean = []
            for item in keep:
                problem = next((p for p in (invalid_name(n, windows=True) for n in item.dst_rel) if p), None)
                if problem:
                    self.error(item.dst_rel, problem)
                    self.files["failed"] += 1
                    continue
                clean.append(item)
            keep = clean
        plan.files = keep
        self.bytes["total"] = sum(item.size for item in keep)
        need = sum(item.size - item.have for item in keep)
        for item in keep:
            item.counted = item.have
        self.bytes["done"] = sum(item.have for item in keep)
        self._rate_bytes = self.bytes["done"]
        if free is not None and need > free:
            raise CopyError(f"目标磁盘空间不足：需要 {human(need)}，可用 {human(free)}", "nospace")
        if not self.dst.v2:
            self.warn("目标设备客户端版本较旧：只按大小校验（verified=size），不能覆盖或续传；升级到 0.8.31 以上可用 sha256 校验")
        elif not self.src.resumable:
            self.warn("源设备客户端版本较旧：中断后的文件从头重新传输")
        self._changed()

    async def _copy(self, plan: Plan) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        # big files first: the long transfers start early, small files fill the gaps
        for item in sorted(plan.files, key=lambda i: -i.size):
            queue.put_nowait(item)
        workers = min(self.concurrency, max(1, len(plan.files)))
        self._workers = [asyncio.create_task(self._worker(n, queue)) for n in range(workers)]
        try:
            outcomes = await asyncio.gather(*self._workers, return_exceptions=True)
        finally:
            for worker in self._workers:
                worker.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, Exception):  # a bug, not a file's problem (those are recorded)
                raise outcome

    async def _worker(self, number: int, queue: asyncio.Queue) -> None:
        async with self.src.session() as source, self.dst.session() as target:
            while self._fatal is None:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await self._one(number, item, source, target)

    async def _open(self, source: Session, item: Item, offset: int, number: int) -> Reader:
        reader = await source.read(str(self.spec["src_path"]), item.rel, offset)
        meta = reader.meta
        if int(meta.get("size") or 0) != item.size or (
                meta.get("mtime_ns") not in (None, 0) and item.mtime_ns and int(meta["mtime_ns"]) != item.mtime_ns):
            await reader.aclose()
            raise CopyError("源文件在复制过程中被修改", "changed")
        self.bytes["done"] += offset - item.counted
        item.counted = offset
        inner = reader.chunks

        async def counted() -> AsyncIterator[bytes]:
            async with contextlib.aclosing(inner) as chunks:
                async for data in chunks:
                    item.counted += len(data)
                    self.bytes["done"] += len(data)
                    self.current[number]["done"] = item.counted
                    self._tick_rate()
                    yield data
            after = reader.after()
            if after and (after.get("size") != item.size or after.get("mtime_ns") != item.mtime_ns):
                raise CopyError("源文件在复制过程中被修改", "changed")

        reader.chunks = counted()
        return reader

    async def _have(self, item: Item, offset: int) -> int:
        """How much of ``item`` the destination's part holds now (after a broken attempt): the
        next attempt goes on from there instead of starting the part over. When the destination
        cannot be asked, ``offset`` stays (a wrong guess > 0 is corrected by the destination)."""
        if not (self.src.resumable and self.dst.v2):
            return 0
        try:
            answer = await self.dst.stat_many(str(self.spec["dst_path"]), [item.dst_rel], [item.key])
            have = int(((answer.get("items") or [{}])[0]).get("have") or 0)
        except CopyError:
            return offset
        return have if 0 < have <= item.size else 0

    def _stop(self, error: CopyError) -> None:
        self._fatal = error
        for worker in self._workers:
            if worker is not asyncio.current_task():
                worker.cancel()

    async def _one(self, number: int, item: Item, source: Session, target: Session) -> None:
        name = "/".join(item.dst_rel) or "(根)"
        self.current[number] = {"rel": name, "done": item.counted, "size": item.size}
        self.files["in_progress"] += 1
        self._changed()
        offset = item.have
        checksum_retried = changed_retried = False
        attempt = 0
        try:
            while True:
                attempt += 1
                try:
                    result = await target.put(
                        str(self.spec["dst_path"]), item.dst_rel, size=item.size, mtime_ns=item.mtime_ns or None,
                        offset=offset if self.src.resumable else 0, on_exists=item.on_exists,
                        resume_key=item.key if self.dst.v2 else None, mode=item.mode,
                        open_source=lambda at: self._open(source, item, at, number))
                    self._lost_in_a_row = 0
                    if result.get("skipped"):
                        self._skip(item.dst_rel, "目标已有相同文件")
                    else:
                        self.files["done"] += 1
                        self.verified.add("sha256" if self.dst.v2 and result.get("checked") else "size")
                        route = f"{source.route} → {target.route}" if source.route != "本机" or target.route != "本机" else "本机"
                        self.routes[route] = self.routes.get(route, 0) + 1
                    return
                except CopyError as exc:
                    if exc.code in FATAL_CODES:
                        self.files["failed"] += 1
                        self.error(item.dst_rel, str(exc))
                        self._stop(exc)
                        return
                    retry = False
                    if isinstance(exc, TransportError) and attempt < ATTEMPTS:
                        retry = True
                        await source.reset()
                        await target.reset()
                        await asyncio.sleep(BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)] * self.spec.get("backoff_scale", 1.0))
                        if self._fatal is not None:
                            return
                        offset = await self._have(item, offset)
                    elif exc.code in ("checksum", "unverified") and not checksum_retried:
                        checksum_retried, retry, offset = True, True, 0
                    elif exc.code == "changed" and not changed_retried:
                        changed_retried, retry, offset = True, True, 0
                        try:  # the source changed: copy what it is now
                            fresh = await self.src.stat_many(str(self.spec["src_path"]), [item.rel], [None])
                            now = (fresh.get("items") or [{}])[0]
                            if now.get("error"):
                                raise CopyError(str(now["error"]), "changed")
                            if not now.get("exists"):
                                raise CopyError("源文件在复制过程中被删除", "changed")
                            if now.get("link") or now.get("dir"):
                                raise CopyError("源文件在复制过程中变成了符号链接或文件夹，未复制", "changed")
                            size = int(now.get("size") or 0)
                            self.bytes["total"] += size - item.size
                            item.size, item.mtime_ns = size, int(now.get("mtime_ns") or 0)
                            item.key = resume_key(self.spec, item.rel, item.size, item.mtime_ns, item.dst_rel)
                            self.current[number]["size"] = item.size
                        except CopyError as again:
                            exc = again
                            retry = False
                    elif exc.code in RETRY_CODES and attempt < ATTEMPTS:
                        retry = True
                        if exc.code == "offset":
                            offset = int(exc.extra.get("have") or 0)
                    if not retry:
                        self.files["failed"] += 1
                        self.error(item.dst_rel, str(exc))
                        self.bytes["done"] -= item.counted
                        item.counted = 0
                        if isinstance(exc, TransportError):
                            self._lost_in_a_row += 1
                            if self._lost_in_a_row >= max(OFFLINE_AFTER, self.concurrency + 1) and self._fatal is None:
                                self._stop(CopyError(
                                    f"连续 {self._lost_in_a_row} 个文件因连接中断失败（最后一次：{exc}）：对方设备可能已离线、"
                                    "休眠或网络中断，任务已停止。恢复后用相同参数再次调用 file_copy：已完成的文件会跳过，"
                                    "未完成的会续传", "offline"))
                        else:
                            self._lost_in_a_row = 0
                        return
        finally:
            self.files["in_progress"] -= 1
            self.current.pop(number, None)
            self._changed()

    async def _empty_dirs(self, plan: Plan) -> None:
        empty = [rel for rel in plan.dirs if tuple(rel) not in plan.nonempty]
        if not empty:
            return
        rels = [plan.prefix + rel for rel in empty]
        try:
            for start in range(0, len(rels), STAT_BATCH):
                await self.dst.mkdirs(str(self.spec["dst_path"]), rels[start:start + STAT_BATCH])
        except CopyError as exc:
            if exc.code == "needs_files2":
                self.warn(str(exc))
            else:
                self.error([], f"创建空文件夹失败：{exc}")
