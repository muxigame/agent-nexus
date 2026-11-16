"""The manager's side of ``session_wake`` / ``session_probe`` (design §8.7).

``session_wake`` returns at once -- it never holds one of the gateway's RPC
slots for the length of a run. A process adapter (claude, codex) starts the
command detached with stdin closed (an interactive prompt can then only end
the process, never hang it), writes stdout/stderr to ``.anx/wake/<job>.log``
and records ``.anx/wake/<job>.json``. When the process exits the runner sends
``hub_call wake_report {job_id, state, exit_code}``; reports wait in the job
file until the gateway is reachable. After a manager restart ``adopt`` picks up
the job files: a process still running is watched by pid, one that ended is
reported (exit code unknown).

The same job id twice (the gateway retrying after an unacknowledged wake)
does not start a second process while the first one still runs.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from ...federation import RoutingError
from . import probe as probe_module
from ...ids import RESUME_ID_RULE, valid_resume_id
from .adapters import PROCESS_KINDS, WakeError, build_argv, popen_args, wake_env

log = logging.getLogger(__name__)
POLL_S = 0.2
FLUSH_INTERVAL_S = 2.0
_JOB_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _spawn(argv: list[str], cwd: str, env: dict, log_file) -> subprocess.Popen:
    common = dict(cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT)
    args = popen_args(argv)  # a .cmd shim gets a command line quoted for cmd.exe
    if os.name != "nt":
        return subprocess.Popen(args, start_new_session=True, **common)
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        # Out of the manager's job object, as the supervisor does: a manager restart must not kill the run.
        return subprocess.Popen(args, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **common)
    except OSError:
        return subprocess.Popen(args, creationflags=flags, **common)


def _alive(pid: int, create_time: float | None) -> bool:
    try:
        import psutil
        proc = psutil.Process(pid)
        if create_time is not None and abs(proc.create_time() - create_time) > 1.0:
            return False  # the pid was reused
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def _create_time(pid: int) -> float | None:
    with contextlib.suppress(Exception):
        import psutil
        return psutil.Process(pid).create_time()
    return None


class WakeRunner:
    def __init__(self, root: Path, config: dict | None = None, *,
                 report: Callable[[dict], Awaitable[Any]] | None = None,
                 companion: Callable[[dict], Awaitable[dict]] | None = None,
                 notify: Callable[[dict], Awaitable[Any]] | None = None):
        self.dir = Path(root) / ".anx" / "wake"
        self.config = config or {}
        self.report_fn = report
        self.companion = companion
        self.notify = notify
        self.procs: dict[str, subprocess.Popen] = {}
        self.monitors: dict[str, asyncio.Task] = {}
        self._flushing = asyncio.Lock()
        self._loop_task: asyncio.Task | None = None
        self._wakeup: asyncio.Event | None = None

    # -- job files ------------------------------------------------------------------
    def _path(self, job_id: str, suffix: str) -> Path:
        return self.dir / f"{job_id}{suffix}"

    def _read(self, job_id: str) -> dict | None:
        with contextlib.suppress(OSError, ValueError):
            data = json.loads(self._path(job_id, ".json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        return None

    def _write(self, job_id: str, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(job_id, ".json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)

    def job_files(self) -> list[dict]:
        if not self.dir.is_dir():
            return []
        found = []
        for path in sorted(self.dir.glob("*.json")):
            data = self._read(path.stem)
            if data:
                found.append(data)
        return found

    # -- session_wake ------------------------------------------------------------------
    async def wake(self, params: dict) -> dict:
        if not isinstance(params, dict):
            raise RoutingError("session_wake needs parameters")
        job_id = str(params.get("job_id") or "")
        if not job_id or len(job_id) > 64 or not set(job_id) <= _JOB_ID_CHARS:
            raise RoutingError("session_wake needs a job_id")
        adapter = params.get("adapter")
        if params.get("resume_id") and not valid_resume_id(params["resume_id"]):
            # It becomes an argument of the woken CLI, possibly through cmd.exe (§8.7).
            return {"accepted": False, "error": RESUME_ID_RULE}
        if adapter in PROCESS_KINDS:
            result = await asyncio.to_thread(self._start, job_id, adapter, params)
            if self._wakeup is not None:
                self._wakeup.set()  # watch the new process at the fast poll rate
            return result
        if adapter == "companion":
            if self.companion is None:
                return {"accepted": False, "error": "no GPT Companion on this device"}
            try:
                result = await self.companion(params)
            except Exception as exc:
                return {"accepted": False, "error": f"companion_unavailable: {type(exc).__name__}: {exc}"[:300]}
            if not result.get("delivered"):
                return {"accepted": False, "error": f"companion_unavailable: {result.get('error') or 'not delivered'}"[:300],
                        "event_id": result.get("event_id")}
            return {"accepted": True, "event_id": result.get("event_id")}
        if adapter == "notify":
            if self.notify is None:
                return {"accepted": False, "error": "no notifier on this device"}
            await self.notify({"tag": f"anx-wake-{job_id}", "title": str(params.get("address_display") or "agent-nexus"),
                               "body": str(params.get("prompt") or "")[:500], "link": "#/inbox", "level": "info",
                               "actions": []})
            return {"accepted": True}
        return {"accepted": False, "error": f"unsupported wake adapter {adapter!r}"}

    def _start(self, job_id: str, kind: str, params: dict) -> dict:
        current = self._read(job_id)
        if current and current.get("state") == "running" and _alive(int(current["pid"]), current.get("create_time")):
            return {"accepted": True, "pid": current["pid"], "already_running": True}
        workdir = params.get("workdir")
        if not isinstance(workdir, str) or not workdir or not Path(workdir).is_dir():
            # Sessions are stored per folder: resuming elsewhere would not find them.
            return {"accepted": False, "error": f"workdir {workdir!r} does not exist on this device"}
        try:
            argv = build_argv(kind, params, self.config)
        except WakeError as exc:
            return {"accepted": False, "error": str(exc)}
        self.dir.mkdir(parents=True, exist_ok=True)
        log_path = self._path(job_id, ".log")
        env = wake_env({"ANX_WAKE_JOB": job_id, "ANX_WAKE_ADDRESS": str(params.get("address") or "")})
        try:
            with open(log_path, "ab") as log_file:
                proc = _spawn(argv, workdir, env, log_file)
        except OSError as exc:
            return {"accepted": False, "error": f"could not start {kind}: {exc}"[:300]}
        self.procs[job_id] = proc
        self._write(job_id, {"job_id": job_id, "pid": proc.pid, "create_time": _create_time(proc.pid),
                             "adapter": kind, "address": params.get("address"), "cwd": workdir,
                             "started_at": time.time(), "state": "running", "log": str(log_path)})
        log.info("wake %s: started %s (pid %s) for %s", job_id, kind, proc.pid, params.get("address"))
        return {"accepted": True, "pid": proc.pid, "log": str(log_path)}

    # -- session_probe ---------------------------------------------------------------------
    async def probe(self, params: dict) -> dict:
        if not isinstance(params, dict):
            raise RoutingError("session_probe needs parameters")
        return await asyncio.to_thread(probe_module.probe, params, self.config)

    # -- watching and reporting --------------------------------------------------------------
    def _exited(self, job_id: str, exit_code: int | None) -> None:
        data = self._read(job_id) or {"job_id": job_id}
        data.update(state="exited", exit_code=exit_code, finished_at=time.time())
        self._write(job_id, data)
        self.procs.pop(job_id, None)

    def check(self) -> None:
        """Mark finished processes (own children by exit code, adopted ones by pid)."""
        for job_id, proc in list(self.procs.items()):
            code = proc.poll()
            if code is not None:
                self._exited(job_id, code)
        for data in self.job_files():
            job_id = data.get("job_id")
            if data.get("state") == "running" and job_id not in self.procs:
                if not _alive(int(data.get("pid") or 0), data.get("create_time")):
                    self._exited(job_id, None)  # adopted after a restart: exit code unknown

    async def flush(self) -> None:
        """Send the reports of finished jobs; keep them while the gateway is unreachable."""
        if self.report_fn is None:
            return
        async with self._flushing:
            for data in self.job_files():
                if data.get("state") != "exited":
                    continue
                code = data.get("exit_code")
                report = {"job_id": data["job_id"], "state": "done" if code in (0, None) else "failed",
                          "exit_code": code}
                try:
                    await self.report_fn(report)
                except RoutingError as exc:
                    if "unknown wake job" not in str(exc):
                        return  # not connected (or a gateway without wake/1): later
                except Exception:
                    return
                with contextlib.suppress(OSError):
                    self._path(data["job_id"], ".json").unlink()

    async def run(self) -> None:
        """Watch processes and send reports, until cancelled. Adopts job files left by a previous manager."""
        self._wakeup = asyncio.Event()
        while True:
            try:
                await asyncio.to_thread(self.check)
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("wake runner check failed (%s)", type(exc).__name__)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), POLL_S if self.procs else FLUSH_INTERVAL_S)
            self._wakeup.clear()

    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.get_running_loop().create_task(self.run())

    async def close(self) -> None:
        """Stop watching; running processes keep running and are adopted by the next manager."""
        task, self._loop_task = self._loop_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
