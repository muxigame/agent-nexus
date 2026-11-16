"""Process supervision: start / stop / restart, adoption, health, crash restart.

Normal manager exit stops its owned and adopted server process trees.
After an abnormal manager exit, pid + create time records allow safe recovery.

A port held by a process mcpm did not start is reported as ``external`` and
left alone; only an explicit ``takeover`` stops it - and never when that
process belongs to something an observe-only service is watching.

Observe-only services (``kind: "observe"``) are refreshed by their detector
instead. They never get ``Runtime.pid``, which is the field every kill path
goes through; what they found lives in ``Runtime.observation``.
"""
from __future__ import annotations

import contextlib
from concurrent.futures import ThreadPoolExecutor
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from . import logs, observers
from .registry import ServiceSpec, discover, load_manager_config, read_json, write_json

log = logging.getLogger(__name__)

TICK_S = 2.0
STOP_TIMEOUT_S = 8.0
START_GRACE_S = 90.0
CRASH_WINDOW_S = 300.0
CRASH_LIMIT = 5
#: detector state -> service status shown everywhere else
OBSERVED_STATUS = {observers.RUNNING: "running", observers.DEGRADED: "unhealthy", observers.ABSENT: "stopped"}

# Health probes hit 127.0.0.1; never let a system/TUN proxy intercept them.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class SupervisorError(RuntimeError):
    pass


@dataclass
class Runtime:
    status: str = "stopped"
    message: str = ""
    pid: int | None = None
    create_time: float | None = None
    started_at: float | None = None
    proc: subprocess.Popen | None = None
    exit_code: int | None = None
    listener_pid: int | None = None
    health: dict[str, Any] = field(default_factory=dict)
    next_probe: float = 0.0
    want_running: bool = False
    crashes: list[float] = field(default_factory=list)
    restart_at: float | None = None
    observation: observers.Observation | None = None
    observed_key: tuple | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)


# -- process helpers ---------------------------------------------------------

def _loopback(host: str) -> str:
    return "127.0.0.1" if host in ("", "0.0.0.0", "::") else host


def listener_of(host: str, port: int) -> tuple[bool, int | None]:
    """(is something listening on ``port``, its pid if visible)."""
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port:
                return True, conn.pid
        return False, None
    except (psutil.AccessDenied, OSError):
        # macOS needs root for the system-wide socket table.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex((_loopback(host), port)) == 0, None


def describe(pid: int | None) -> str:
    if pid is None:
        return "an unidentified process"
    try:
        return f"pid {pid} ({psutil.Process(pid).name()})"
    except psutil.Error:
        return f"pid {pid}"


def tree_pids(pid: int) -> set[int]:
    try:
        root = psutil.Process(pid)
        return {pid, *(c.pid for c in root.children(recursive=True))}
    except psutil.Error:
        return set()


def kill_tree(pid: int) -> list[int]:
    try:
        root = psutil.Process(pid)
        # Collect first: once the root dies, orphans lose their parent link.
        procs = [root, *root.children(recursive=True)]
    except psutil.Error:
        return []
    for proc in procs:
        with contextlib.suppress(psutil.Error):
            proc.terminate()
    _, alive = psutil.wait_procs(procs, timeout=STOP_TIMEOUT_S / 2)
    for proc in alive:
        with contextlib.suppress(psutil.Error):
            proc.kill()
    psutil.wait_procs(alive, timeout=STOP_TIMEOUT_S / 2)
    return [p.pid for p in procs]


def _listens(proc: psutil.Process) -> bool:
    getter = getattr(proc, "net_connections", None) or proc.connections
    return any(c.status == psutil.CONN_LISTEN for c in getter(kind="tcp"))


def _belongs(proc: psutil.Process, needle: str) -> bool:
    try:
        if _listens(proc):
            return False  # a server of its own, e.g. the agent host that launched it
        if proc.name().lower() in ("uv", "uv.exe"):
            return True
        if needle in os.path.normcase(" ".join(proc.cmdline())):
            return True
        cwd = os.path.normcase(proc.cwd())
        return cwd == needle or cwd.startswith(needle + os.sep)
    except psutil.Error:
        return False


def launcher_root(pid: int, service_dir: Path) -> int:
    """Climb from a port listener through its launcher chain (venv python,
    ``uv run``, the service's own start script) and stop at anything else."""
    needle = os.path.normcase(str(service_dir))
    me = psutil.Process()
    protected = {me.pid, *(p.pid for p in me.parents())}
    try:
        cur = psutil.Process(pid)
    except psutil.Error:
        return pid
    for _ in range(6):
        try:
            parent = cur.parent()
        except psutil.Error:
            break
        if parent is None or parent.pid in protected or not _belongs(parent, needle):
            break
        cur = parent
    return cur.pid


def describe_exit(code: int | None) -> str:
    """Exit code plus the Windows status it encodes (0xC0000005 = access violation)."""
    if code is None:
        return "exit code unknown (process was not started by this manager)"
    if code < 0 or code > 0xFFFF:
        status = code & 0xFFFFFFFF
        known = {0xC0000005: "access violation", 0xC000013A: "Ctrl+C / console closed",
                 0xC00000FD: "stack overflow", 0xC0000409: "stack buffer overrun", 0x40010004: "terminated"}
        return f"exit code {code} (0x{status:08X}{', ' + known[status] if status in known else ''})"
    if code == 1 and sys.platform == "win32":
        return "exit code 1 (also what taskkill /F leaves)"
    return f"exit code {code}"


def probe_http(host: str, port: int, path: str, timeout: float = 3.0) -> dict[str, Any]:
    started = time.monotonic()
    # JSON only: a GET that negotiates text/event-stream may hold an MCP stream open.
    req = urllib.request.Request(f"http://{_loopback(host)}:{port}{path}",
                                 headers={"Accept": "application/json", "User-Agent": "mcpm-health"})
    code: int | None = None
    error = ""
    try:
        with _DIRECT.open(req, timeout=timeout) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except (OSError, ValueError) as exc:
        error = str(exc)[:200]
    ms = int((time.monotonic() - started) * 1000)
    # Any answer below 500 means the server is up: MCP endpoints legitimately
    # reply 400 (no session), 401 (auth) or 406 to a bare GET.
    return {"ok": code is not None and code < 500, "code": code, "error": error, "ms": ms, "at": time.time()}


def child_env(spec: ServiceSpec) -> dict[str, str]:
    env = dict(os.environ)
    # The manager usually runs inside its own venv; don't leak it into servers
    # that `uv run` against theirs.
    venv = env.pop("VIRTUAL_ENV", None)
    if venv:
        prefix = os.path.normcase(venv)
        env["PATH"] = os.pathsep.join(
            p for p in env.get("PATH", "").split(os.pathsep) if not os.path.normcase(p).startswith(prefix))
    env.pop("UV_RUN_RECURSION_DEPTH", None)
    env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    env.update(spec.resolved_env())
    return env


def resolve_executable(command: str, env: dict[str, str], root: Path) -> str:
    """Resolve launchers, preferring the portable client's bundled uv."""
    if Path(command).name.lower() in {"uv", "uv.exe"}:
        bundled = root / "tools" / ("uv.exe" if os.name == "nt" else "uv")
        if bundled.is_file():
            return str(bundled)
    return shutil.which(command, path=env.get("PATH")) or command


def _spawn(argv: list[str], cwd: Path, env: dict[str, str], log) -> subprocess.Popen:
    common = dict(cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    if os.name != "nt":
        return subprocess.Popen(argv, start_new_session=True, **common)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    try:
        # `uv run` and venv launchers put us in a kill-on-close job object;
        # break away so servers survive a manager restart.
        return subprocess.Popen(argv, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **common)
    except OSError:
        return subprocess.Popen(argv, creationflags=flags, **common)


# -- supervisor ----------------------------------------------------------------

class Supervisor:
    def __init__(self, root: Path):
        self.cfg = load_manager_config(root)
        self.specs: dict[str, ServiceSpec] = {}
        self.rt: dict[str, Runtime] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._shutdown_lock = threading.Lock()
        self._shutdown_report: list[str] | None = None
        self.rescan()

    @property
    def log_dir(self) -> Path:
        return self.cfg.state_dir / "logs"

    def stdout_log(self, name: str) -> Path:
        return self.log_dir / f"{name}.log"

    def _pidfile(self, name: str) -> Path:
        return self.cfg.state_dir / "run" / f"{name}.json"

    def rescan(self) -> None:
        cfg = load_manager_config(self.cfg.root)
        specs = discover(cfg)
        for name, spec in specs.items():
            rt = self.rt.setdefault(name, Runtime())
            if rt.pid is None and not spec.observed:
                self._adopt(name, rt)
        self.cfg, self.specs = cfg, specs

    def spec(self, name: str) -> ServiceSpec:
        try:
            return self.specs[name]
        except KeyError:
            raise SupervisorError(f"unknown service: {name}") from None

    # -- lifecycle --------------------------------------------------------------

    @staticmethod
    def _refuse_observed(spec: ServiceSpec) -> None:
        if spec.observed:
            raise SupervisorError(f"{spec.name} is observe-only: mcpm shows it but never starts or stops it")

    def start(self, name: str) -> str:
        spec, rt = self.spec(name), self.rt[name]
        self._refuse_observed(spec)
        with rt.lock:
            if self._stop.is_set():
                raise SupervisorError("manager is shutting down")
            if self._alive(rt):
                return f"{name} already running (pid {rt.pid})"
            if not spec.enabled:
                raise SupervisorError(f"{name} is disabled")
            if not spec.supported:
                raise SupervisorError(f"{name} does not run on {sys.platform} (platforms: {spec.platforms})")
            if spec.error:
                raise SupervisorError(f"{name}: {spec.error}")
            if spec.port is not None:
                busy, owner = listener_of(spec.host, spec.port)
                if busy:
                    raise SupervisorError(f"port {spec.port} is held by {describe(owner)}; restart with takeover to replace it")
            argv, cwd, env = spec.resolved_command(), spec.resolved_cwd(), child_env(spec)
            env["MCPM_ROOT"] = str(self.cfg.root)
            exe = resolve_executable(argv[0], env, self.cfg.root)
            if not Path(exe).exists():
                raise SupervisorError(f"command not found: {argv[0]}")
            if not cwd.is_dir():
                raise SupervisorError(f"working directory not found: {cwd}")

            log_path = self.stdout_log(name)
            logs.rotate(log_path)
            with open(log_path, "ab") as log:
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                log.write(f"\n===== mcpm start {stamp} :: {' '.join(argv)}  (cwd {cwd})\n".encode("utf-8"))
                log.flush()
                proc = _spawn([exe, *argv[1:]], cwd, env, log)

            try:
                create_time = psutil.Process(proc.pid).create_time()
            except psutil.Error:
                create_time = None
            rt.proc, rt.pid, rt.create_time = proc, proc.pid, create_time
            rt.started_at, rt.exit_code, rt.restart_at = time.time(), None, None
            rt.want_running, rt.status, rt.message, rt.health, rt.next_probe = True, "starting", "", {}, 0.0
            write_json(self._pidfile(name), {"pid": proc.pid, "create_time": create_time,
                                             "started_at": rt.started_at, "command": argv,
                                             "fingerprint": self._fingerprint(spec)})
            return f"{name} started (pid {proc.pid})"

    def stop(self, name: str, takeover: bool = False) -> str:
        spec, rt = self.spec(name), self.rt[name]
        self._refuse_observed(spec)
        with rt.lock:
            rt.want_running, rt.restart_at = False, None
            killed: list[int] = []
            if self._alive(rt):
                killed = kill_tree(rt.pid)
            elif takeover and spec.port is not None:
                busy, owner = listener_of(spec.host, spec.port)
                if busy and owner is None:
                    raise SupervisorError(f"port {spec.port} is in use but its owner is not visible (needs admin/root)")
                if owner is not None:
                    root = launcher_root(owner, spec.dir)
                    watcher = self._watched_by(root)
                    if watcher:
                        raise SupervisorError(
                            f"port {spec.port} is held by {describe(owner)}, which the observe-only service "
                            f"{watcher} is watching; refusing to kill it")
                    killed = kill_tree(root)
            self._forget(name, rt)
            rt.status, rt.message, rt.crashes = "stopped", "", []
            if spec.port is not None and not self._wait_port_free(spec):
                rt.message = f"port {spec.port} still in use after stop"
            return f"{name} stopped" + (f" ({len(killed)} processes)" if killed else "")

    def restart(self, name: str, takeover: bool = False) -> str:
        return f"{self.stop(name, takeover=takeover)}; {self.start(name)}"

    def _watched_by(self, root: int) -> str | None:
        """The observe-only service whose process tree overlaps the tree rooted
        at ``root``, i.e. what a takeover of ``root`` would take down with it."""
        doomed = tree_pids(root) or {root}
        for name, rt in self.rt.items():
            spec = self.specs.get(name)
            obs = rt.observation
            if spec is None or not spec.observed or obs is None or obs.pid is None:
                continue
            if doomed & (tree_pids(obs.pid) or {obs.pid}):
                return name
        return None

    def autostart(self) -> list[str]:
        report = []
        for name, spec in self.specs.items():
            if not (spec.autostart and spec.runnable):
                continue
            rt = self.rt[name]
            if self._alive(rt):
                report.append(f"{name}: running (adopted pid {rt.pid})")
            elif spec.port is not None and listener_of(spec.host, spec.port)[0]:
                report.append(f"{name}: port {spec.port} already served by another process, left alone")
            else:
                try:
                    report.append(self.start(name))
                except SupervisorError as exc:
                    report.append(f"{name}: {exc}")
        return report

    # -- monitoring ---------------------------------------------------------------

    def run_background(self) -> None:
        self.tick()

        def loop() -> None:
            while not self._stop.wait(TICK_S):
                self.tick()

        self._thread = threading.Thread(target=loop, name="mcpm-supervisor", daemon=True)
        self._thread.start()

    def shutdown(self) -> list[str]:
        with self._shutdown_lock:
            if self._shutdown_report is not None:
                return list(self._shutdown_report)
            self._stop.set()
            if self._thread and self._thread is not threading.current_thread():
                self._thread.join(timeout=5)
            # Never take over an unrelated port listener or an observe-only
            # browser. Ownership comes from our pid/create-time records.
            names = [name for name, rt in list(self.rt.items()) if rt.pid is not None
                     and not (self.specs.get(name) and self.specs[name].observed)]
            def stop_owned(name: str) -> str:
                try:
                    if name in self.specs:
                        return self.stop(name)
                    rt = self.rt[name]
                    with rt.lock:
                        rt.want_running, rt.restart_at = False, None
                        if self._alive(rt):
                            kill_tree(rt.pid)
                        self._forget(name, rt)
                    return f"{name} stopped"
                except Exception as exc:
                    return f"{name}: shutdown failed ({exc})"
            with ThreadPoolExecutor(max_workers=max(1, min(8, len(names)))) as pool:
                self._shutdown_report = list(pool.map(stop_owned, names))
            return list(self._shutdown_report)

    def tick(self) -> None:
        now = time.time()
        for name, spec in list(self.specs.items()):
            if self._stop.is_set():
                return
            rt = self.rt.setdefault(name, Runtime())
            if not rt.lock.acquire(blocking=False):
                continue  # a start/stop is in flight
            try:
                self._refresh(name, spec, rt, now)
            except Exception as exc:  # one broken service must not stop the loop
                rt.message = f"refresh failed: {exc}"
            finally:
                rt.lock.release()
        for name, rt in list(self.rt.items()):
            if self._stop.is_set():
                return
            if rt.restart_at is not None and now >= rt.restart_at:
                rt.restart_at = None
                try:
                    self.start(name)
                except SupervisorError as exc:
                    rt.status, rt.message = "crashed", f"auto-restart failed: {exc}"

    def _refresh(self, name: str, spec: ServiceSpec, rt: Runtime, now: float) -> None:
        if spec.observed:
            self._observe(name, spec, rt, now)
            return
        busy, owner = listener_of(spec.host, spec.port) if spec.port is not None else (False, None)
        rt.listener_pid = owner

        if self._alive(rt):
            ours = busy and (owner is None or owner in tree_pids(rt.pid))
            if spec.port is None or ours:
                self._probe(spec, rt, now)
                ok = not rt.health or rt.health.get("ok")
                rt.status = "running" if ok else "unhealthy"
                rt.message = "" if ok else f"health check: {rt.health.get('code') or rt.health.get('error')}"
            elif busy:
                rt.status, rt.message = "conflict", f"port {spec.port} is held by {describe(owner)}"
            else:
                waited = now - (rt.started_at or now)
                rt.status = "starting" if waited < START_GRACE_S else "unhealthy"
                rt.message = "" if waited < START_GRACE_S else f"not listening on {spec.port} after {int(waited)}s"
            return

        if rt.pid is not None:  # ours, and gone
            code, wanted = rt.exit_code, rt.want_running
            self._forget(name, rt)
            if not wanted:
                rt.status, rt.message = "stopped", ""
                return
            self._record_exit(name, code)
            rt.crashes = [t for t in rt.crashes if now - t < CRASH_WINDOW_S] + [now]
            rt.status = "crashed"
            rt.message = f"exited with code {code}" if code is not None else "process exited"
            if spec.autorestart and len(rt.crashes) < CRASH_LIMIT:
                delay = min(2 ** len(rt.crashes), 30)
                rt.restart_at = now + delay
                rt.message += f"; restarting in {delay}s"
            elif spec.autorestart:
                rt.message += f"; gave up after {CRASH_LIMIT} crashes in {int(CRASH_WINDOW_S)}s"
            return

        if busy:
            self._probe(spec, rt, now)
            rt.status, rt.message = "external", f"port {spec.port} served by {describe(owner)}, not started by mcpm"
        elif rt.status == "crashed" or rt.restart_at is not None:
            pass
        elif not spec.enabled:
            rt.status, rt.message = "disabled", ""
        elif not spec.supported:
            rt.status, rt.message = "unsupported", f"platforms: {', '.join(spec.platforms)}"
        elif spec.error:
            rt.status, rt.message = "invalid", spec.error
        else:
            rt.status, rt.message, rt.health = "stopped", "", {}

    def _observe(self, name: str, spec: ServiceSpec, rt: Runtime, now: float) -> None:
        rt.pid = rt.proc = rt.listener_pid = None
        if not spec.enabled:
            rt.status, rt.message, rt.observation, rt.observed_key = "disabled", "", None, None
            return
        if not spec.supported:
            rt.status, rt.message = "unsupported", f"platforms: {', '.join(spec.platforms)}"
            return
        if spec.error:
            rt.status, rt.message = "invalid", spec.error
            return
        if rt.observation is not None and now < rt.next_probe:
            return
        rt.next_probe = now + spec.health_interval  # schedule first, as in _probe
        try:
            obs = observers.observe(spec.observe)
        except Exception as exc:  # a detector bug must not take the loop down
            obs = observers.Observation(observers.DEGRADED, f"detector failed: {exc}")
        rt.observation = obs
        rt.status, rt.message = OBSERVED_STATUS.get(obs.state, "unhealthy"), obs.message
        if obs.signature() != rt.observed_key:
            rt.observed_key = obs.signature()
            self._log_observation(name, obs)

    def _log_observation(self, name: str, obs: observers.Observation) -> None:
        """One line per change, so the log tab reads as the thing's history."""
        path = self.stdout_log(name)
        logs.rotate(path)
        facts = "; ".join(f"{k}: {v}" for k, v in obs.facts)
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  [{obs.state}] {obs.message or 'ok'}  |  {facts}\n"
        with contextlib.suppress(OSError), open(path, "a", encoding="utf-8") as fh:
            fh.write(line)

    def _probe(self, spec: ServiceSpec, rt: Runtime, now: float) -> None:
        if spec.health_path and spec.port is not None and now >= rt.next_probe:
            # Schedule first: if the probe ever raises, the next tick must not
            # retry immediately and hammer a server that logs every request.
            rt.next_probe = now + spec.health_interval
            rt.health = probe_http(spec.host, spec.port, spec.health_path)

    def _alive(self, rt: Runtime) -> bool:
        if rt.pid is None:
            return False
        if rt.proc is not None:
            code = rt.proc.poll()
            if code is None:
                return True
            rt.exit_code = code
            return False
        try:
            proc = psutil.Process(rt.pid)
            same = rt.create_time is None or abs(proc.create_time() - rt.create_time) < 1.0
            return same and proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    def _adopt(self, name: str, rt: Runtime) -> None:
        path = self._pidfile(name)
        if not path.exists():
            return
        try:
            info = read_json(path)
            proc = psutil.Process(int(info["pid"]))
            if abs(proc.create_time() - float(info["create_time"])) < 1.0:
                rt.pid, rt.create_time = proc.pid, proc.create_time()
                rt.started_at = float(info.get("started_at") or rt.create_time)
                rt.want_running, rt.status = True, "starting"
                return
        except (OSError, ValueError, KeyError, TypeError, psutil.Error):
            pass
        with contextlib.suppress(OSError):
            path.unlink()

    def _forget(self, name: str, rt: Runtime) -> None:
        rt.pid = rt.proc = rt.create_time = rt.started_at = rt.listener_pid = None
        with contextlib.suppress(OSError):
            self._pidfile(name).unlink()

    def _wait_port_free(self, spec: ServiceSpec) -> bool:
        deadline = time.monotonic() + STOP_TIMEOUT_S
        while time.monotonic() < deadline:
            if not listener_of(spec.host, spec.port)[0]:
                return True
            time.sleep(0.25)
        return False

    # -- views --------------------------------------------------------------------

    def log_sources(self, name: str) -> list[dict[str, Any]]:
        spec = self.spec(name)
        stdout = self.stdout_log(name)
        sources = [{"id": "stdout", "label": "stdout / stderr", "path": str(stdout), "exists": stdout.exists()}]
        for i, path in enumerate(spec.extra_log_files()):
            sources.append({"id": str(i), "label": spec.logs[i], "path": str(path), "exists": path.exists()})
        return sources

    def _record_exit(self, name: str, code: int | None) -> None:
        """Unexpected exits leave no trace of their own; the exit code says how it ended."""
        described = describe_exit(code)
        log.warning("%s exited unexpectedly (%s)", name, described)
        with contextlib.suppress(OSError):
            with open(self.stdout_log(name), "ab") as out:
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                out.write(f"\n===== mcpm exit {stamp} :: {described}\n".encode("utf-8"))

    def log_path(self, name: str, source: str) -> Path:
        for item in self.log_sources(name):
            if item["id"] == source:
                return Path(item["path"])
        raise SupervisorError(f"unknown log source {source!r} for {name}")

    @staticmethod
    def _fingerprint(spec: ServiceSpec) -> str:
        """Everything a restart would change: command, working dir, injected env."""
        import hashlib
        import json

        blob = json.dumps([spec.resolved_command(), str(spec.resolved_cwd()), spec.resolved_env()], sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def _pending_restart(self, name: str, spec: ServiceSpec, rt: Runtime) -> bool:
        if rt.pid is None:
            return False
        try:
            recorded = read_json(self._pidfile(name)).get("fingerprint")
        except (OSError, ValueError):
            return False
        return bool(recorded) and recorded != self._fingerprint(spec)

    def snapshot(self, name: str) -> dict[str, Any]:
        spec = self.spec(name)
        rt = self.rt.get(name) or Runtime()
        running = rt.pid is not None and rt.started_at is not None
        obs = rt.observation if spec.observed else None
        port, uptime = spec.port, int(time.time() - rt.started_at) if running else None
        if spec.observed:
            port = obs.port if obs else None
            seen = obs is not None and obs.state != observers.ABSENT and obs.started_at
            uptime = int(time.time() - obs.started_at) if seen else None
        return {
            "name": name,
            "description": spec.description,
            "status": rt.status,
            "pending_restart": self._pending_restart(name, spec, rt),
            "message": rt.message,
            "pid": rt.pid,
            "listener_pid": rt.listener_pid,
            "host": spec.host,
            "port": port,
            "uptime_s": uptime,
            "kind": spec.kind,
            "observe": spec.observe,
            "observed_pid": obs.pid if obs else None,
            "facts": [list(fact) for fact in obs.facts] if obs else [],
            "exit_code": rt.exit_code,
            "crashes": len(rt.crashes),
            "health": rt.health,
            "health_path": spec.health_path,
            "health_interval": spec.health_interval,
            "autostart": spec.autostart,
            "autorestart": spec.autorestart,
            "enabled": spec.enabled,
            "supported": spec.supported,
            "platforms": spec.platforms,
            "source": spec.source,
            "error": spec.error,
            "dir": str(spec.dir),
            "cwd": str(spec.resolved_cwd()),
            "command": spec.resolved_command(),
            "env_keys": sorted(spec.env),  # values may be secrets
            "logs": self.log_sources(name),
            **self._icon_fields(name, spec),
        }

    @staticmethod
    def _icon_fields(name: str, spec: ServiceSpec) -> dict[str, Any]:
        from urllib.parse import quote

        path, problem = spec.icon_file()
        if path is None:
            return {"icon_url": None, "icon_error": problem}
        try:
            version = int(path.stat().st_mtime)  # busts the browser cache when the file is replaced
        except OSError:
            return {"icon_url": None, "icon_error": ""}
        return {"icon_url": f"/api/services/{quote(name, safe='')}/icon?v={version}", "icon_error": ""}

    def snapshots(self) -> list[dict[str, Any]]:
        return [self.snapshot(name) for name in sorted(self.specs)]
