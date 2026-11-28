"""The gateway's side of ``file_copy`` (docs/design.md §18): pick who moves the bytes, start the
job there, and answer for it.

Who runs the job (the "runner"), in this order:

1. the source device (``push``) when it runs copy jobs (``fxjob/1``) and is not locked;
2. otherwise the destination device (``pull``) on the same terms;
3. both are the same device: a local copy job there;
4. otherwise the gateway itself pumps ``fx_read`` from the source into ``fx_put_*`` on the
   destination: any ``files/1`` device works, but every byte crosses the gateway (slow).

Between 1 and 2: when the side that would not run the job has no P2P hub (``remote-hub/1``) but
the other has, the other runs it, so the bytes can still go straight between the devices.

A device job's id starts with the runner's device id, so its status is asked from that device
even after the gateway restarted; a pump job lives in this process only.

Every call to a device has its own time limit, well inside an MCP client's patience (about 60 s):
a device that does not answer ``fxjob_start`` in time counts as a refusal (the next candidate,
else the pump, takes over, and the late job is cancelled), and a status that does not come back
answers ``unknown`` instead of hanging.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import secrets

from .federation import RoutingError
from .features import FILES, FILES2, FXJOB, REMOTE_HUB
from .file_copy import TERMINAL, CopyJob, RelayEndpoint

log = logging.getLogger(__name__)
MAX_PUMPS = 2
PUMP_CONCURRENCY = 2
STATUS_WAIT_MAX_MS = 50000
DEVICE_WAIT_MAX_MS = 25000
#: time limits of the calls to a device (seconds)
START_TIMEOUT_S = 15.0
STATUS_GRACE_S = 5.0
CANCEL_TIMEOUT_S = 40.0
LIST_TIMEOUT_S = 15.0
#: a device's status long poll answered early without an end: wait at least this before asking again
POLL_MIN_S = 2.0
PUMP_NOTE = ("两端客户端都不支持后台复制任务或都已锁定，数据经网关中转，速度较慢（约 100 KB/s 量级）；"
             "升级客户端到 0.8.31 以上可走直连")


class PeerTransport:
    """``fx_*`` on a device over its link to this gateway (the pump's transport)."""

    def __init__(self, hub, agent_id: str):
        self.hub, self.agent_id = hub, agent_id

    async def rpc(self, method: str, params: dict, timeout: float | None = None) -> dict:
        from .file_transfer import TransferError
        result = await self.hub.peer(self.agent_id).call(method, params)
        if not isinstance(result, dict):
            raise TransferError("设备没有回应")
        if "error" in result:
            raise TransferError(str(result["error"]), 422, str(result.get("code") or ""),
                                {k: v for k, v in result.items() if k not in ("error", "code")})
        return result

    async def read(self, params: dict) -> tuple[bytes, dict]:
        result = await self.rpc("fx_read", params)
        return base64.b64decode(result.get("data") or ""), result

    async def write(self, upload_id: str, offset: int, data: bytes) -> dict:
        return await self.rpc("fx_put_write", {"id": upload_id, "offset": offset,
                                               "data": base64.b64encode(data).decode("ascii")})

    async def close(self) -> None:
        pass


class FileCopyService:
    def __init__(self, hub, devices):
        self.hub = hub
        self.devices = devices
        self.pumps: dict[str, CopyJob] = {}
        self.tasks: set[asyncio.Task] = set()
        self.start_timeout = START_TIMEOUT_S
        self.status_grace = STATUS_GRACE_S
        self.cancel_timeout = CANCEL_TIMEOUT_S
        self.list_timeout = LIST_TIMEOUT_S

    async def _ask(self, peer, method: str, params: dict, timeout: float):
        """``peer.call`` within ``timeout``; a silent device is a RoutingError that says so."""
        try:
            return await asyncio.wait_for(peer.call(method, params), timeout)
        except asyncio.TimeoutError:
            raise RoutingError(f"{self.devices.display_name(peer.agent_id)} 在 {timeout:.0f} 秒内没有回应"
                               "（可能休眠或网络中断）") from None

    def _background(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # -- devices
    def _online(self, value: str):
        agent_id = self.devices.resolve(value)
        peer = self.hub.peers.get(agent_id)
        if peer is None:
            raise RoutingError(f"设备 {self.devices.label(agent_id)} 不在线")
        features = getattr(peer, "features", frozenset())
        if FILES not in features:
            raise RoutingError(f"{self.devices.label(agent_id)}：这台设备的客户端版本不支持文件传输（升级到 0.8.8 以上），"
                               "或设备上关闭了文件传输")
        return agent_id, peer

    def _names(self, snap: dict) -> dict:
        for side in ("src", "dst"):
            entry = snap.get(side)
            if isinstance(entry, dict) and entry.get("device"):
                entry["display_name"] = self.devices.display_name(entry["device"])
        if snap.get("runner_device"):
            snap["runner_display_name"] = self.devices.display_name(snap["runner_device"])
        return snap

    @staticmethod
    def _can_run(peer) -> bool:
        return FXJOB in getattr(peer, "features", frozenset()) and not getattr(peer, "locked", False)

    # -- start
    async def start(self, src: str, src_path: str, dst: str, dst_path: str, *, overwrite: bool = False,
                    route: str = "auto", concurrency: int | None = None) -> dict:
        if route not in ("auto", "p2p"):
            raise RoutingError("route must be auto or p2p")
        for name, value in (("src_path", src_path), ("dst_path", dst_path)):
            if not isinstance(value, str) or not value.strip():
                raise RoutingError(f"{name} is required (an absolute path on that device)")
        if concurrency is not None and not 1 <= int(concurrency) <= 8:
            raise RoutingError("concurrency must be 1-8")
        src_id, src_peer = self._online(src)
        dst_id, dst_peer = self._online(dst)
        spec = {"src_device": src_id, "src_path": src_path.strip(), "dst_device": dst_id, "dst_path": dst_path.strip(),
                "overwrite": bool(overwrite), "route": route}
        if concurrency is not None:
            spec["concurrency"] = int(concurrency)
        if src_id == dst_id:
            candidates = [("local", src_peer, [])] if self._can_run(src_peer) else []
        else:
            push = ("push", src_peer, sorted(dst_peer.features))
            pull = ("pull", dst_peer, sorted(src_peer.features))
            candidates = [c for c in (push, pull) if self._can_run(c[1])]
            if REMOTE_HUB not in dst_peer.features and REMOTE_HUB in src_peer.features:
                candidates.sort(key=lambda c: c[0] != "pull")  # only a pull can use a tunnel
        refusals = []
        for role, runner, peer_features in candidates:
            job_id = f"{runner.agent_id}:{secrets.token_urlsafe(9)}"
            try:
                answer = await self._ask(runner, "fxjob_start", {"spec": spec, "role": role,
                                                                 "peer_features": peer_features, "job_id": job_id},
                                         self.start_timeout)
            except RoutingError as exc:
                refusals.append(f"{self.devices.display_name(runner.agent_id)}: {exc}")
                # it may still start the job late: make sure it does not run beside the next try
                self._background(self._cancel_quietly(runner, job_id))
                continue
            if not isinstance(answer, dict):
                refusals.append(f"{self.devices.display_name(runner.agent_id)}: 没有回应")
                continue
            if "error" in answer:
                if answer.get("code") in ("locked", "busy"):
                    refusals.append(f"{self.devices.display_name(runner.agent_id)}: {answer['error']}")
                    continue
                raise RoutingError(str(answer["error"]))
            return self._names({**answer, "job_id": answer.get("job_id") or job_id, "src": {"device": src_id, "path": spec["src_path"]},
                                "dst": {"device": dst_id, "path": spec["dst_path"]},
                                "note": "数据在两台设备之间直接传输（直连或经中转设备，端到端加密），不经过网关时最快；"
                                        "用 file_copy_status 查看进度和实际线路",
                                **({"refused": refusals} if refusals else {})})
        if route == "p2p":
            reason = "；".join(refusals) or "两端客户端都不支持后台复制任务（需要 0.8.31 以上）"
            raise RoutingError(f"无法直连复制（route=p2p 不经网关中转）：{reason}")
        return self._pump(spec, src_peer, dst_peer, refusals)

    async def _cancel_quietly(self, runner, job_id: str) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(runner.call("fxjob_cancel", {"job_id": job_id}), self.cancel_timeout)

    def _pump(self, spec: dict, src_peer, dst_peer, refusals: list) -> dict:
        running = sum(1 for job in self.pumps.values() if not job.done)
        if running >= MAX_PUMPS:
            raise RoutingError(f"网关上经中转的复制任务已有 {MAX_PUMPS} 个在运行，请稍后再试或升级设备客户端")
        job_id = f"gateway:{secrets.token_urlsafe(9)}"
        src = RelayEndpoint(PeerTransport(self.hub, spec["src_device"]), v2=FILES2 in src_peer.features)
        dst = RelayEndpoint(PeerTransport(self.hub, spec["dst_device"]), v2=FILES2 in dst_peer.features)
        same = spec["src_device"] == spec["dst_device"]
        job = CopyJob({**spec, "id": job_id, "same_device": same, "max_concurrency": PUMP_CONCURRENCY,
                       "concurrency": min(int(spec.get("concurrency") or PUMP_CONCURRENCY), PUMP_CONCURRENCY)},
                      src, dst, info={"mode": "gateway", "runner": "gateway", "runner_device": None})
        job.warn(PUMP_NOTE)
        self.pumps[job_id] = job
        task = job.start()
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        self._prune()
        result = {"job_id": job_id, "state": job.state, "mode": "gateway", "runner": "gateway", "runner_device": None,
                  "src": {"device": spec["src_device"], "path": spec["src_path"]},
                  "dst": {"device": spec["dst_device"], "path": spec["dst_path"]}, "note": PUMP_NOTE,
                  "warning": "经网关中转：慢；大量数据请先升级两端客户端"}
        if refusals:
            result["refused"] = refusals
        return self._names(result)

    def _prune(self) -> None:
        done = [job_id for job_id, job in self.pumps.items() if job.done]
        for job_id in done[:-50]:
            self.pumps.pop(job_id, None)

    # -- status / cancel / list
    def _runner(self, job_id: str):
        if not isinstance(job_id, str) or ":" not in job_id:
            raise RoutingError("job_id is unknown; it is returned by file_copy")
        return job_id.split(":", 1)[0]

    async def status(self, job_id: str, wait_ms: int = 0) -> dict:
        runner = self._runner(job_id)
        wait_ms = max(0, min(int(wait_ms or 0), int(getattr(self, "wait_max_ms", STATUS_WAIT_MAX_MS))))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_ms / 1000.0
        if runner == "gateway":
            job = self.pumps.get(job_id)
            if job is None:
                return {"job_id": job_id, "state": "unknown",
                        "message": "网关重启后不再知道这个经网关中转的任务；重新调用 file_copy 会跳过已复制的相同文件"}
            await job.wait_done(wait_ms / 1000.0)
            return self._names(job.snapshot())
        while True:
            peer = self.hub.peers.get(runner)
            if peer is None:
                return {"job_id": job_id, "state": "unknown", "runner_device": runner,
                        "runner_display_name": self.devices.display_name(runner),
                        "message": "运行任务的设备离线；任务在设备上继续/中断，设备上线后可再查询"}
            left_ms = max(0, int((deadline - loop.time()) * 1000))
            asked = min(left_ms, DEVICE_WAIT_MAX_MS)
            began = loop.time()
            try:
                snap = await self._ask(peer, "fxjob_status", {"job_id": job_id, "wait_ms": asked},
                                       asked / 1000.0 + self.status_grace)
            except RoutingError as exc:
                return {"job_id": job_id, "state": "unknown", "runner_device": runner,
                        "runner_display_name": self.devices.display_name(runner),
                        "message": f"查询失败：{exc}；稍后再查询，任务在设备上继续"}
            if not isinstance(snap, dict):
                raise RoutingError("设备没有回应")
            if "error" in snap:
                raise RoutingError(str(snap["error"]))
            if snap.get("state") in TERMINAL or loop.time() >= deadline - 0.05:
                return self._names(snap)
            took = loop.time() - began
            if took < POLL_MIN_S:  # an early answer (an older runner): do not busy-poll the device
                await asyncio.sleep(min(POLL_MIN_S - took, max(0.0, deadline - loop.time())))

    async def cancel(self, job_id: str) -> dict:
        runner = self._runner(job_id)
        if runner == "gateway":
            job = self.pumps.get(job_id)
            if job is None:
                raise RoutingError("没有这个复制任务（网关重启后经网关中转的任务不再保留）")
            await job.cancel()
            return self._names(job.snapshot())
        peer = self.hub.peers.get(runner)
        if peer is None:
            raise RoutingError(f"运行任务的设备 {self.devices.label(runner)} 不在线，暂时无法取消")
        answer = await self._ask(peer, "fxjob_cancel", {"job_id": job_id}, self.cancel_timeout)
        if not isinstance(answer, dict):
            raise RoutingError("设备没有回应")
        if "error" in answer:
            raise RoutingError(str(answer["error"]))
        return self._names(answer)

    async def list(self) -> dict:
        jobs = [self._names(job.snapshot()) for job in self.pumps.values()]
        peers = [peer for peer in list(self.hub.peers.values()) if FXJOB in getattr(peer, "features", frozenset())]

        async def ask(peer) -> list:
            with contextlib.suppress(Exception):
                answer = await asyncio.wait_for(peer.call("fxjob_list", {}), self.list_timeout)
                if isinstance(answer, dict) and isinstance(answer.get("jobs"), list):
                    return answer["jobs"]
            return []

        for found in await asyncio.gather(*(ask(peer) for peer in peers)):
            jobs.extend(self._names(snap) for snap in found if isinstance(snap, dict))
        jobs.sort(key=lambda snap: -(snap.get("created_at") or 0))
        summary = []
        for snap in jobs:
            summary.append({key: snap.get(key) for key in (
                "job_id", "state", "mode", "runner_device", "runner_display_name", "src", "dst", "files", "bytes",
                "created_at", "finished_at", "message") if key in snap})
        return {"jobs": summary}

    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.wait(set(self.tasks), timeout=10)


__all__ = ["FileCopyService", "PeerTransport"]
