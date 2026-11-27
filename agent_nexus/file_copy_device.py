"""Copy jobs run by this device (``fxjob/1``; docs/design.md §18).

The gateway starts a job here with reverse RPC ``fxjob_start`` when this device is the source
(``push``), the destination (``pull``) or both (``local``) of a ``file_copy``. This device then
moves the bytes itself: its own disk on one side, and the other device through the P2P tunnel
(or a relay device, end-to-end encrypted) on the other -- one ``putx`` / ``getx`` stream per
worker, many files per stream -- falling back to the gateway relay with this device's own
credential when no tunnel comes up. The gateway only keeps a pointer and asks for the status.

Who may: the gateway asks, and this device acts with its own credential, as when a person here
uses the 文件 page: refused while this client is locked (``lock.require_unlocked``), and the
gateway refuses tickets and relay calls of a locked device too.

Jobs survive nothing: a snapshot is written every few seconds to ``.anx/fxjobs/<id>.json``, so
after a restart the job is listed as ``interrupted``; running the same copy again skips the
identical files and resumes the parts.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from . import file_transfer
from .file_copy import (TERMINAL, CopyError, CopyJob, Endpoint, LocalEndpoint, Reader, RelayEndpoint, RelaySession,
                        Session, TransportError, classify)
from .file_share import FileShare, FileShareError

log = logging.getLogger(__name__)
FEATURE = "fxjob/1"
MAX_RUNNING = 4
KEEP_S = 24 * 3600
KEEP_JOBS = 50
STATUS_WAIT_MAX_S = 25.0
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


class Links:
    """How this device reaches another one (the local console's own route choice, ``web.py``):
    ``link(device) -> ((HubPath, label, ticket) | None, label)``, ``drop(device)`` after a broken
    P2P transfer, ``hold(device)`` keeps the tunnel while a job uses it."""

    def __init__(self, link: Callable, drop: Callable[[str], None], hold: Callable[[str], Any] | None = None):
        self.link = link
        self.drop = drop
        self.hold = hold or (lambda device_id: contextlib.nullcontext())


class LinkEndpoint(Endpoint):
    """Another device: P2P (direct or relayed) streams, else the gateway relay."""

    def __init__(self, device_id: str, features, links: Links, client, *, p2p_only: bool = False):
        self.device_id = device_id
        #: ``route="p2p"``: never through the gateway relay (fail instead)
        self.p2p_only = p2p_only
        self.v2 = "files/2" in set(features or ())
        self.resumable = self.v2
        self.links = links
        self.client = client
        self.relay = RelayEndpoint(client, v2=self.v2, label="网关中转")
        self.label = "网关中转"
        self.p2p_failures = 0

    async def _meta(self, op: str, method: str, params: dict) -> dict:
        if self.v2 and self.p2p_failures < 2:
            try:
                link, _label = await self.links.link(self.device_id)
            except Exception:
                link = None
            if link is not None:
                try:
                    return await file_transfer.p2p_call(link[0], link[2], op, params)
                except file_transfer.TransferError as exc:
                    if exc.status == 422:
                        raise classify(exc) from None
                except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError):
                    pass
                self.p2p_failures += 1
                self.links.drop(self.device_id)
        if self.p2p_only:
            raise TransportError("直连不可用（route=p2p 不走网关中转）")
        return await self.relay.rpc(method, params)

    async def walk(self, path: str) -> AsyncIterator[dict]:
        if not self.v2:
            if self.p2p_only:
                raise CopyError("对方客户端版本过旧，无法按 route=p2p 复制（需要 0.8.31 以上）", "needs_files2")
            async for page in self.relay.walk(path):
                yield page
            return
        cursor = None
        while True:
            page = await self._meta("walk", "fx_walk", {"path": path, "cursor": cursor, "limit": 2000})
            if page.get("root") and page["root"].get("platform"):
                self.platform = page["root"]["platform"]
            yield page
            cursor = page.get("next")
            if not cursor:
                return

    async def stat_many(self, base: str, rels: list, keys: list) -> dict:
        if not self.v2:
            return await self.relay.stat_many(base, rels, keys)
        answer = await self._meta("stat", "fx_stat_many", {"base": base, "rels": rels, "keys": keys})
        self.platform = answer.get("platform") or self.platform
        return answer

    async def mkdirs(self, base: str, rels: list) -> None:
        if not self.v2:
            await self.relay.mkdirs(base, rels)
            return
        await self._meta("mkdirs", "fx_mkdirs", {"base": base, "rels": rels})

    def session(self) -> Session:
        return LinkSession(self)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.client.close()


class LinkSession(Session):
    """One worker's way to the other device: a long-lived stream while it works, else the relay."""

    def __init__(self, endpoint: LinkEndpoint):
        self.endpoint = endpoint
        self.relay = RelaySession(endpoint.relay)
        self.stream = None
        self.holding = contextlib.ExitStack()
        self.failures = 0
        self.route = "网关中转"
        self.on_p2p = False

    async def _link(self):
        if self.failures >= 2:  # this route broke twice: the gateway relay for the rest
            self.route = "网关中转（直连多次中断）"
            return None
        try:
            link, label = await self.endpoint.links.link(self.endpoint.device_id)
        except Exception as exc:
            link, label = None, f"网关中转（{str(exc)[:60]}）"
        self.route = label
        return link

    async def _stream(self, kind):
        if self.stream is not None and not self.stream.broken:
            return self.stream
        await self._drop_stream()
        link = await self._link()
        if link is None:
            self.on_p2p = False
            if self.endpoint.p2p_only:
                raise TransportError(f"直连不可用（route=p2p 不走网关中转）：{self.route}")
            return None
        self.holding.enter_context(self.endpoint.links.hold(self.endpoint.device_id))
        try:
            self.stream = await kind().open(link[0], link[2])
        except Exception as exc:
            log.info("copy stream to %s failed to open (%s); using the gateway relay", self.endpoint.device_id, exc)
            self.endpoint.links.drop(self.endpoint.device_id)
            self.failures += 1
            self.holding.close()
            self.holding = contextlib.ExitStack()
            self.route = "网关中转（直连打不开）"
            self.on_p2p = False
            if self.endpoint.p2p_only:
                raise TransportError(f"直连打不开（route=p2p 不走网关中转）：{exc}")
            return None
        self.on_p2p = True
        return self.stream

    async def _drop_stream(self) -> None:
        stream, self.stream = self.stream, None
        if stream is not None:
            # A cancelled job closes its streams while being cancelled (twice: the job and its worker): the
            # close must still finish, since it is what lets the far side discard an abandoned part.
            closing = asyncio.ensure_future(stream.close())
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(asyncio.shield(closing), 8)
                self.holding.close()
                self.holding = contextlib.ExitStack()
                raise
            except Exception:  # noqa: BLE001
                pass
        self.holding.close()
        self.holding = contextlib.ExitStack()

    async def reset(self) -> None:
        if self.on_p2p:
            self.failures += 1
            self.endpoint.links.drop(self.endpoint.device_id)
        await self._drop_stream()
        self.on_p2p = False

    async def close(self) -> None:
        await self._drop_stream()

    # -- reading the other device
    async def read(self, base: str, rel: list, offset: int) -> Reader:
        endpoint = self.endpoint
        if not endpoint.v2:
            link = await self._link()
            if link is None:
                return await self.relay.read(base, rel, 0)
            self.on_p2p = True
            try:
                meta, chunks = await file_transfer.p2p_get(link[0], link[2], endpoint.relay.v1_path(base, rel))
            except Exception as exc:
                raise classify(exc) from None
            import hashlib
            hasher = hashlib.sha256()

            async def hashed() -> AsyncIterator[bytes]:
                async with contextlib.aclosing(chunks) as body:
                    async for data in body:
                        hasher.update(data)
                        yield data
            return Reader({"size": meta.get("size"), "mtime_ns": None, "name": meta.get("name")}, hashed(),
                          hasher.hexdigest)
        stream = await self._stream(file_transfer.P2PGetStream)
        if stream is None:
            return await self.relay.read(base, rel, offset)
        try:
            meta, chunks, final = await stream.read({"base": base, "rel": rel, "offset": offset})
        except Exception as exc:
            raise classify(exc) from None

        def after() -> dict | None:
            return {"size": final.get("size"), "mtime_ns": final.get("mtime_ns")} if final else None
        return Reader(meta, chunks, lambda: final.get("sha256"), after)

    # -- writing to the other device
    async def put(self, base, rel, *, size, mtime_ns, offset, on_exists, resume_key, open_source, mode=None) -> dict:
        endpoint = self.endpoint
        if not endpoint.v2:
            link = await self._link()
            if link is None:
                return await self.relay.put(base, rel, size=size, mtime_ns=mtime_ns, offset=0, on_exists=on_exists,
                                            resume_key=None, open_source=open_source)
            self.on_p2p = True
            folder, name = endpoint.relay.v1_split(base, rel)
            reader = await open_source(0)
            try:
                return await file_transfer.p2p_put(link[0], link[2], folder, name, size, reader.chunks)
            except Exception as exc:
                raise classify(exc) from None
        stream = await self._stream(file_transfer.P2PPutStream)
        if stream is None:
            return await self.relay.put(base, rel, size=size, mtime_ns=mtime_ns, offset=offset, on_exists=on_exists,
                                        resume_key=resume_key, open_source=open_source, mode=mode)
        for _ in range(3):
            # hashing a resumed prefix can take minutes: keep the idle stream alive meanwhile
            reader = await keep_alive(stream, open_source(offset))
            meta = {"base": base, "rel": rel, "size": size, "mtime_ns": mtime_ns, "on_exists": on_exists,
                    "resume_key": resume_key, "offset": offset}
            if mode is not None:
                meta["mode"] = mode
            try:
                return await stream.send_file(meta, reader.chunks, reader.digest)
            except file_transfer.TransferError as exc:
                if exc.status == 422 and exc.reason == "offset":
                    offset = int(exc.extra.get("have") or 0)  # the part holds something else: go on from there
                    continue
                raise classify(exc) from None
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
                raise classify(exc) from None
            finally:
                await reader.aclose()
        raise CopyError("续传位置反复不一致", "offset", have=offset)


#: how often an idle putx stream is told the caller is still there (the far side gives up at 300 s)
KEEPALIVE_S = 30.0


async def keep_alive(stream, work):
    """Await ``work`` while sending ``stream`` a keepalive every ``KEEPALIVE_S``."""
    task = asyncio.ensure_future(work)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=KEEPALIVE_S)
            if done:
                return task.result()
            await stream.keepalive()
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


def _snapshot_name(job_id: str) -> str:
    return _SAFE.sub("_", job_id)[:120] + ".json"


class CopyJobs:
    """The jobs this device runs, and the reverse RPCs the gateway drives them with."""

    def __init__(self, root: Path, share: FileShare, *, links: Links, me: Callable[[], str] = lambda: "",
                 lock: Any = None, client: Callable[[str], Any] | None = None):
        self.root = Path(root)
        self.dir = self.root / ".anx" / "fxjobs"
        self.share = share
        self.links = links
        self.me = me
        self.lock = lock
        self.client = client or (lambda device_id: file_transfer.RelayClient(self.root, device_id))
        self.jobs: dict[str, CopyJob] = {}
        self.tasks: set[asyncio.Task] = set()
        self.history: dict[str, dict] = {}
        self._load()

    # -- persistence
    def _load(self) -> None:
        try:
            files = sorted(self.dir.glob("*.json"))
        except OSError:
            return
        now = time.time()
        for path in files:
            try:
                snap = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(snap, dict) or not snap.get("job_id"):
                    continue
                if now - float(snap.get("finished_at") or snap.get("created_at") or now) > KEEP_S:
                    path.unlink(missing_ok=True)
                    continue
                if snap.get("state") not in TERMINAL:
                    snap.update(state="interrupted", finished_at=snap.get("finished_at") or now, current=[],
                                message="设备重启中断；重新调用 file_copy 会跳过已完成的文件并续传未完成的文件")
                    path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
                self.history[snap["job_id"]] = snap
            except (OSError, ValueError):
                continue

    def _save(self, snap: dict) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            target = self.dir / _snapshot_name(str(snap["job_id"]))
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, target)
        except OSError as exc:
            log.debug("copy job snapshot not written: %s", exc)

    def _prune(self) -> None:
        done = sorted((snap for snap in self.history.values()), key=lambda s: s.get("finished_at") or 0)
        now = time.time()
        for snap in done[:-KEEP_JOBS] + [s for s in done[-KEEP_JOBS:] if now - (s.get("finished_at") or now) > KEEP_S]:
            self.history.pop(snap["job_id"], None)
            with contextlib.suppress(OSError):
                (self.dir / _snapshot_name(snap["job_id"])).unlink(missing_ok=True)

    # -- state
    def running(self) -> bool:
        return any(not job.done for job in self.jobs.values())

    def _finished(self, job: CopyJob) -> None:
        snap = job.snapshot()
        self.history[job.id] = snap
        self.jobs.pop(job.id, None)
        self._prune()

    # -- reverse RPC
    async def start(self, params: dict) -> dict:
        if self.lock is not None:
            try:
                self.lock.require_unlocked()
            except Exception as exc:
                return {"error": getattr(exc, "message", str(exc)), "code": "locked"}
        spec = params.get("spec") if isinstance(params.get("spec"), dict) else {}
        role = params.get("role")
        if role not in ("push", "pull", "local"):
            return {"error": "role must be push, pull or local", "code": "bad_request"}
        for key in ("src_device", "src_path", "dst_device", "dst_path"):
            if not isinstance(spec.get(key), str) or not spec[key]:
                return {"error": f"缺少 {key}", "code": "bad_request"}
        if sum(1 for job in self.jobs.values() if not job.done) >= MAX_RUNNING:
            return {"error": f"这台设备上正在运行的复制任务太多（最多 {MAX_RUNNING} 个）", "code": "busy"}
        local_path = spec["src_path"] if role in ("push", "local") else None
        if local_path is not None:
            try:
                path = self.share._path(local_path)
                exists = await asyncio.wait_for(asyncio.to_thread(os.path.exists, path), 5)
            except FileShareError as exc:
                return {"error": str(exc), "code": "bad_path"}
            except asyncio.TimeoutError:
                exists = True  # a slow network share: the walk will tell
            if not exists:
                return {"error": self.share._missing(path).args[0], "code": "missing"}
        features = params.get("peer_features") if isinstance(params.get("peer_features"), list) else []
        local = LocalEndpoint(self.share)
        p2p_only = spec.get("route") == "p2p"
        if role == "push":
            src, dst = local, LinkEndpoint(spec["dst_device"], features, self.links, self.client(spec["dst_device"]),
                                           p2p_only=p2p_only)
        elif role == "pull":
            src, dst = LinkEndpoint(spec["src_device"], features, self.links, self.client(spec["src_device"]),
                                    p2p_only=p2p_only), local
        else:
            src, dst = local, LocalEndpoint(self.share)
            spec = {**spec, "same_device": True}
        job_id = params.get("job_id") if isinstance(params.get("job_id"), str) and params["job_id"] else \
            f"{self.me() or 'device'}:{secrets.token_urlsafe(9)}"
        if job_id in self.jobs or job_id in self.history:
            return {"error": "任务编号重复", "code": "bad_request"}
        info = {"mode": role, "runner": {"push": "source", "pull": "destination", "local": "local"}[role],
                "runner_device": self.me()}
        job = CopyJob({**spec, "id": job_id}, src, dst, snapshot=self._save, info=info)
        self.jobs[job_id] = job
        task = job.start()
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(lambda _done, job=job: self._finished(job))
        return {"job_id": job_id, "state": job.state, **info}

    def _find(self, job_id: str) -> CopyJob | dict | None:
        return self.jobs.get(job_id) or self.history.get(job_id)

    async def status(self, params: dict) -> dict:
        job_id = str(params.get("job_id") or params.get("id") or "")
        found = self._find(job_id)
        if found is None:
            return {"error": "没有这个复制任务（记录保留 24 小时）", "code": "unknown"}
        if isinstance(found, dict):
            return found
        try:
            wait = max(0.0, min(float(params.get("wait_ms") or 0) / 1000.0, STATUS_WAIT_MAX_S))
        except (TypeError, ValueError):
            wait = 0.0
        await found.wait_done(wait)
        return found.snapshot()

    async def cancel(self, params: dict) -> dict:
        job_id = str(params.get("job_id") or params.get("id") or "")
        found = self._find(job_id)
        if found is None:
            return {"error": "没有这个复制任务", "code": "unknown"}
        if isinstance(found, dict):
            return found
        await found.cancel()
        return found.snapshot()

    async def list(self, _params: dict | None = None) -> dict:
        jobs = [job.snapshot() for job in self.jobs.values()] + list(self.history.values())
        summary = [{key: snap.get(key) for key in ("job_id", "state", "src", "dst", "files", "bytes", "mode",
                                                   "runner_device", "created_at", "finished_at", "message")}
                   for snap in jobs]
        summary.sort(key=lambda s: -(s.get("created_at") or 0))
        return {"jobs": summary}

    async def close(self) -> None:
        """The manager is stopping: running jobs end as ``interrupted`` (their snapshots say so)."""
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.wait(set(self.tasks), timeout=10)

    def handlers(self) -> dict:
        def safe(fn):
            async def handler(params: dict) -> dict:
                try:
                    return await fn(params if isinstance(params, dict) else {})
                except CopyError as exc:
                    return {"error": str(exc), "code": exc.code or "failed"}
                except Exception as exc:
                    log.exception("fxjob call failed")
                    return {"error": f"{type(exc).__name__}: {exc}", "code": "failed"}
            return handler

        return {"fxjob_start": safe(self.start), "fxjob_status": safe(self.status),
                "fxjob_cancel": safe(self.cancel), "fxjob_list": safe(self.list)}


__all__ = ["CopyJobs", "Links", "LinkEndpoint", "LinkSession", "TransportError", "FEATURE"]
