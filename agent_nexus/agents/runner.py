"""Agent runs (docs/agent-chat.md §5): an agent CLI driven as a chat, on this device.

A run is one chat window: :meth:`Runs.open` starts the CLI in the work folder as the run-as account
(``accounts.effective(root, "terminal")``), every line it prints goes through its adapter
(:mod:`.adapters`) into a buffer of unified events, and :meth:`Runs.read` hands them out by cursor with a
long poll (≤ 20 s, never cancelling anything in flight). Further turns go through :meth:`Runs.send`
(stdin for ``claude-stream`` and ``acp``, a new ``exec resume`` process for ``codex-exec``).

* Buffer: at most :data:`MAX_EVENTS` events / :data:`MAX_BYTES` per run; the oldest go first and a reader
  whose cursor fell behind is told how many it missed (``dropped``).
* Idle: a process with no turn for :data:`IDLE_PROCESS_S` is closed (the CLI's session file stays, the next
  message resumes it); a run nobody reads and nothing happens in for as long is forgotten.
* Stop: interrupts the current turn -- claude's ``interrupt`` control request, ACP ``session/cancel``, SIGINT /
  kill of a per-turn process -- and kills the process when the CLI does not stop within :data:`STOP_GRACE_S`.
* Approvals: ACP agents ask in-band. Claude in ``ask`` mode gets ``--mcp-config`` with anx's approval MCP
  server (:mod:`.approve_mcp`), which POSTs each request to a loopback endpoint (:meth:`Runs.approval_url`,
  served here unless the web layer serves it and sets ``approval_url``); :meth:`Runs.approval_request`
  shows the card and waits for the answer, denying after :data:`APPROVAL_TIMEOUT_S`.
* Windows: npm's ``.cmd`` shims are resolved to ``node <script>`` so nothing passes through cmd.exe; a shim
  that cannot be resolved runs through ``cmd.exe /d /s /c`` with every argument checked, and free text moves
  to stdin or the first message. Processes start with ``CREATE_NO_WINDOW``; stop kills the process tree.

Every handler returns ``{"error": "<中文说明>", "code": "..."}`` instead of raising.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import adapters, attachments, registry, sessions, workdirs
from .adapters import AdapterOptions
from .registry import AgentDef

log = logging.getLogger(__name__)

WINDOWS = sys.platform == "win32"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

MAX_EVENTS = 2000
MAX_BYTES = 4 * 1024 * 1024
MAX_READ_EVENTS = 500
MAX_WAIT_S = 20.0
MAX_RUNS = 16
IDLE_PROCESS_S = 30 * 60
APPROVAL_TIMEOUT_S = 10 * 60
ACP_INIT_TIMEOUT_S = 20.0
STOP_GRACE_S = 5.0
REAP_EVERY_S = 60.0
#: A busy run with no event and no reader for this many idle periods is treated as stuck (see _reap).
STUCK_FACTOR = 4
#: How long shutdown waits for the CLIs to exit after SIGTERM before killing them.
SHUTDOWN_GRACE_S = 2.0
STDERR_LINES = 60
MAX_PROMPT = 200_000
#: What a CLI started from here must not inherit from the anx process (as for terminals), and markers that
#: make a nested Claude Code think it runs inside another one.
STRIP_ENV = ("VIRTUAL_ENV", "ANX_ROOT", "MCPM_ROOT", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_PYTHON_PREFERENCE",
             "ANX_SYSTEM_SERVICE", "ANX_GUI_EXE", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")
STATES = ("starting", "running", "idle", "done", "failed", "stopped", "closed")


def prepend_path(env: dict, folders: list[str], *, windows: bool = WINDOWS) -> None:
    """Put ``folders`` first on ``env``'s PATH (in place), found case-insensitively ("Path" on Windows), once."""
    key = next((k for k in env if k.upper() == "PATH"), "Path" if windows else "PATH")
    sep = ";" if windows else ":"

    def norm(path: str) -> str:
        path = path.rstrip("\\/") or path
        return path.lower() if windows else path

    first = [folder for folder in folders if folder]
    wanted = {norm(folder) for folder in first}
    rest = [part for part in str(env.get(key) or "").split(sep) if part and norm(part) not in wanted]
    env[key] = sep.join([*first, *rest])


class RunError(Exception):
    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------------------------- launching

_SHIM_SCRIPT = re.compile(r'"%~?dp0%?\\?([^"%*]+?\.(?:js|cjs|mjs))"', re.IGNORECASE)


def npm_shim_target(path: str, env: dict | None = None) -> list[str] | None:
    """``[node, script]`` behind an npm ``.cmd`` shim (``"%dp0%\\node_modules\\...\\cli.js" %*``), or None.

    Running node directly keeps cmd.exe -- and its quoting rules -- out of the way entirely. Only shims that
    point into ``node_modules`` are resolved (a desktop app's launcher may need its own runtime)."""
    try:
        if os.path.getsize(path) > 64 * 1024:
            return None
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _SHIM_SCRIPT.search(text)
    if not match or "node_modules" not in match.group(1).replace("/", "\\").lower():
        return None
    folder = os.path.dirname(os.path.abspath(path))
    script = os.path.normpath(os.path.join(folder, *re.split(r"[\\/]+", match.group(1).strip("\\/"))))
    if not os.path.isfile(script):
        return None
    for name in ("node.exe", "node"):
        local = os.path.join(folder, name)
        if os.path.isfile(local):
            return [local, script]
    search = next((value for key, value in (env or os.environ).items() if key.upper() == "PATH"), None)
    node = shutil.which("node", path=search)
    return [node, script] if node else None


def launcher(binary: str, env: dict | None = None, *, windows: bool = WINDOWS) -> tuple[list[str], bool]:
    """(argv prefix replacing the binary, whether it is an unresolved batch file run through cmd.exe)."""
    lower = binary.lower()
    if windows and lower.endswith((".cmd", ".bat")):
        target = npm_shim_target(binary, env)
        return (target, False) if target else ([binary], True)
    if windows and lower.endswith(".ps1"):
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", binary], False
    return [binary], False


def command_line(argv: list[str], batch: bool) -> list[str] | str:
    """What ``Popen`` gets: the argv, or for a batch file the whole ``cmd.exe /d /s /c "..."`` line, every
    argument checked against what cmd.exe would interpret (:class:`RunError` when one cannot be passed)."""
    if not batch:
        return argv
    from ..link.wake.adapters import WakeError, _batch_line
    try:
        line = _batch_line(argv)
    except WakeError as exc:
        raise RunError("启动参数里有 cmd.exe 无法安全传递的字符（如 % ! 或换行），请换一个工作目录或去掉这些字符",
                       "unsafe_args") from exc
    comspec = os.environ.get("ComSpec") or "cmd.exe"
    return f'"{comspec}" /d /s /c "{line}"'


class _Proc:
    """A child process with piped stdio; output lines are delivered on the event loop.

    Threads rather than asyncio subprocesses: the same code works with any event loop on Windows and with
    the Windows service's start-as-the-user path, which yields plain handles."""

    def __init__(self, launch: list[str] | str, *, cwd: str, env: dict, run_as: dict | None,
                 loop: asyncio.AbstractEventLoop, on_line: Callable[[str, str], None],
                 on_exit: Callable[[int | None], None], stdin_data: bytes | None = None, keep_stdin: bool = True):
        self.loop, self.on_line, self.on_exit = loop, on_line, on_exit
        self._queue: queue.Queue = queue.Queue()
        self.killed = False
        if WINDOWS and run_as and run_as.get("session") is not None:  # pragma: no cover - Windows service
            self.p = _session_popen(int(run_as["session"]), launch, cwd, env)
        else:
            extra: dict[str, Any] = {}
            if WINDOWS:
                extra["creationflags"] = NO_WINDOW | NEW_GROUP
            else:
                extra["start_new_session"] = True
                if run_as and "user" in run_as:
                    extra.update({key: run_as[key] for key in ("user", "group", "extra_groups") if key in run_as})
            self.p = subprocess.Popen(launch, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      cwd=cwd, env=env, **extra)
        self.pid = self.p.pid
        if stdin_data:
            self._queue.put(stdin_data)
        if not keep_stdin:
            self._queue.put(None)
        self._err = threading.Thread(target=self._read, args=(self.p.stderr, "stderr"), daemon=True)
        self._err.start()
        threading.Thread(target=self._read_out, daemon=True).start()
        threading.Thread(target=self._write, daemon=True).start()

    def _post(self, fn, *args) -> None:
        try:
            self.loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # the loop is closed: nobody is listening any more
            pass

    def _read(self, stream, name: str) -> None:
        try:
            for raw in iter(stream.readline, b""):
                self._post(self.on_line, name, raw.decode("utf-8", "replace").rstrip("\r\n"))
        except (OSError, ValueError):
            pass

    def _read_out(self) -> None:
        self._read(self.p.stdout, "stdout")
        self._err.join(5)
        try:
            code = self.p.wait()
        except Exception:  # noqa: BLE001 - a process handle that cannot be waited on has ended for us
            code = None
        self._queue.put(None)
        self._post(self.on_exit, code)

    def _write(self) -> None:
        stdin = self.p.stdin
        while True:
            data = self._queue.get()
            if data is None:
                break
            try:
                stdin.write(data)
                stdin.flush()
            except (OSError, ValueError):
                break
        try:
            stdin.close()
        except (OSError, ValueError):
            pass

    def write(self, data: bytes) -> None:
        self._queue.put(data)

    def interrupt(self) -> None:
        """Ctrl-C for the process group (POSIX); Windows has no gentle way for a windowless child: kill."""
        if WINDOWS:
            self.kill()
            return
        try:
            os.killpg(self.p.pid, signal.SIGINT)
        except (OSError, ProcessLookupError):
            pass

    def kill(self) -> None:
        self.killed = True
        threading.Thread(target=self._kill, daemon=True).start()

    def terminate_now(self) -> None:
        """Shutdown, step 1 (synchronous): SIGTERM to the process group; on Windows kill the tree at once
        (``taskkill /T /F``) -- there is no gentler signal for a windowless child."""
        self.killed = True
        try:
            if WINDOWS:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.p.pid)], check=False, capture_output=True,
                               creationflags=NO_WINDOW, timeout=5)
            else:
                os.killpg(self.p.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError, subprocess.SubprocessError):
            pass

    def kill_now(self) -> None:
        """Shutdown, step 2: whatever is still alive is killed outright."""
        try:
            if not WINDOWS:
                os.killpg(self.p.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        try:
            if self.p.poll() is None:
                self.p.kill()
        except OSError:
            pass

    def _kill(self) -> None:
        try:
            if WINDOWS:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.p.pid)], check=False, capture_output=True,
                               creationflags=NO_WINDOW, timeout=10)
                if self.p.poll() is None:
                    self.p.kill()
                return
            os.killpg(self.p.pid, signal.SIGTERM)
            try:
                self.p.wait(3)
            except subprocess.TimeoutExpired:
                os.killpg(self.p.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError, subprocess.SubprocessError):
            try:
                self.p.kill()
            except OSError:
                pass


def _session_popen(session: int, launch: list[str] | str, cwd: str, env: dict):  # pragma: no cover - Windows
    """As the Windows service: start ``launch`` as the user at the screen with piped stdio (no window)."""
    import ctypes
    import msvcrt

    from .. import winsession as ws
    from ..winspawn import HeldProcess

    line = launch if isinstance(launch, str) else subprocess.list2cmdline([str(item) for item in launch])
    fds = [os.pipe(), os.pipe(), os.pipe()]  # (read, write) for stdin, stdout, stderr
    child = [fds[0][0], fds[1][1], fds[2][1]]
    handles = [msvcrt.get_osfhandle(fd) for fd in child]
    for handle in handles:
        os.set_handle_inheritable(handle, True)
    attributes = ws._attribute_list(1)
    try:
        handle_list = (ws.HANDLE * 3)(*handles)
        if not ws._k32.UpdateProcThreadAttribute(attributes, 0, ws.PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handle_list,
                                                 ctypes.sizeof(handle_list), None, None):
            raise ws._error()
        startup = ws._StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(ws._StartupInfoEx)
        startup.StartupInfo.lpDesktop = "winsta0\\default"
        startup.StartupInfo.dwFlags = ws.STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput, startup.StartupInfo.hStdError = handles
        startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
        info = ws._ProcessInformation()
        command = ctypes.create_unicode_buffer(line)
        block = ws._environment_block(env) if env is not None else None
        with ws._Token(session) as token:
            if not ws._adv.CreateProcessAsUserW(token, None, command, None, None, True,
                                                ws.CREATE_NO_WINDOW | ws.EXTENDED_STARTUPINFO_PRESENT
                                                | ws.CREATE_UNICODE_ENVIRONMENT, block, cwd or None,
                                                ctypes.byref(startup), ctypes.byref(info)):
                raise ws._error()
        ws._k32.CloseHandle(info.hThread)
    finally:
        ws._k32.DeleteProcThreadAttributeList(attributes)
        for fd in child:
            os.close(fd)
    held = HeldProcess(int(info.dwProcessId), info.hProcess)
    held.stdin = os.fdopen(fds[0][1], "wb", buffering=0)
    held.stdout = os.fdopen(fds[1][0], "rb")
    held.stderr = os.fdopen(fds[2][0], "rb")
    return held


# ---------------------------------------------------------------------------------------------- runs

@dataclass
class _Approval:
    future: asyncio.Future
    tool: str
    data: dict


@dataclass
class Run:
    rid: str
    d: AgentDef
    adapter: adapters.Adapter
    opts: AdapterOptions
    workdir: str
    env: dict
    run_as: dict | None
    prefix: list[str]
    batch: bool
    permission: str
    address: str = ""
    title: str = ""
    created: float = field(default_factory=time.time)
    active: float = field(default_factory=time.monotonic)  # last turn activity
    seen: float = field(default_factory=time.monotonic)  # last read
    events: collections.deque = field(default_factory=collections.deque)
    seq: int = 0
    size: int = 0
    dropped: int = 0
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    state: str = "starting"
    proc: _Proc | None = None
    gen: int = 0
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    tmpdir: str = ""
    own_tmpdir: bool = False
    approvals: dict[str, _Approval] = field(default_factory=dict)
    approval_n: int = 0
    remembered: set = field(default_factory=set)
    stderr: collections.deque = field(default_factory=lambda: collections.deque(maxlen=STDERR_LINES))
    last_prompt: str = ""
    last_files: list = field(default_factory=list)  # the last turn's attachments (§18), re-sent by the fallback
    closed: bool = False
    timers: list = field(default_factory=list)
    known_session: str = ""  # the session id last reported to Runs.on_session
    context: dict = field(default_factory=dict)  # the hub's notes (kind of context, handle, preset…)

    @property
    def kind(self) -> str:
        return self.d.kind

    @property
    def session_id(self) -> str | None:
        return self.adapter.session_id

    def view(self) -> dict:
        return {"rid": self.rid, "kind": self.kind, "label": self.d.label, "session_id": self.session_id,
                "workdir": self.workdir, "state": self.state, "permission": self.permission,
                "adapter": self.adapter.name, "started_at": self.created, "address": self.address,
                "title": self.title, "busy": self.adapter.turn_active, "process": self.proc is not None,
                "pending_approvals": len(self.approvals) + len(self.adapter.pending_approvals()),
                "tmpdir": self.tmpdir, "model": self.opts.model or None}


def _model(params: dict) -> str | None:
    """The console's ``model`` (None: not given; "" = the CLI's default). RunError when it is malformed."""
    if "model" not in params or params.get("model") is None:
        return None
    value = params.get("model")
    if not isinstance(value, str):
        raise RunError(registry.MODEL_ERROR, "bad_model")
    value = value.strip()
    if value and not registry.model_ok(value):
        raise RunError(registry.MODEL_ERROR, "bad_model")
    return value


SHELL_TOOLS = ("Bash", "shell", "execute", "BashOutput", "run_command")  # run_command: API models (§16.4)


def _remember_key(tool: str, data: dict) -> str:
    """What "always allow" covers: the tool, or for a shell exactly this command (whitespace normalised).

    Never the command's program alone: remembering ``git status`` must not let ``git push --force`` or
    ``ls && curl … | sh`` through without a card."""
    if tool in SHELL_TOOLS:
        command = data.get("command")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        return f"{tool}\0{' '.join(str(command or '').split())}"
    return tool


def _codex_lookup(account: Any, env: dict) -> Callable[[str], dict | None]:
    """``session_id -> context figures`` from the run-as account's Codex rollouts (the same HOME the hub uses)."""
    home = getattr(account, "home", None) or (env.get("USERPROFILE") if WINDOWS else None) or env.get("HOME") \
        or str(Path.home())
    found: dict[str, Path] = {}

    def lookup(session_id: str) -> dict | None:
        path = found.get(session_id)
        if path is None or not path.exists():
            path = sessions.codex_rollout(Path(home), env, session_id)
            if path is None:
                return None
            found[session_id] = path
        return sessions.codex_context_file(path)
    return lookup


def _within(path: str, roots: list[str]) -> bool:
    """``path`` is inside one of ``roots`` (one implementation with the picker: :func:`workdirs.allowed`)."""
    return workdirs.allowed(path, roots)


class Runs:
    """The runs on this device. ``definitions``: the registry's (a dict, or a callable returning one);
    ``account``: () -> the run-as ``accounts.Account`` or None (this process's user); ``roots``: () -> the
    folders a work folder must be inside (None = anywhere that exists); ``tmp_root``: where runs without a
    ``tmpdir`` from the caller get one."""

    def __init__(self, definitions: dict[str, AgentDef] | Callable[[], dict[str, AgentDef]] | None = None, *,
                 account: Callable[[], Any] | None = None, roots: Callable[[], list[str]] | None = None,
                 tmp_root: str | Path | None = None, approval_url: str | None = None,
                 max_runs: int = MAX_RUNS, idle_s: float = IDLE_PROCESS_S, approval_timeout_s: float = APPROVAL_TIMEOUT_S,
                 acp_timeout_s: float = ACP_INIT_TIMEOUT_S, stop_grace_s: float = STOP_GRACE_S,
                 max_events: int = MAX_EVENTS, max_bytes: int = MAX_BYTES):
        self._definitions = definitions
        self.account = account or (lambda: None)
        self.roots = roots
        self.tmp_root = Path(tmp_root) if tmp_root else None
        #: The loopback URL the approval MCP server POSTs to; set by the web layer when it serves
        #: :meth:`approval_request` itself, else served by :meth:`_approval_server` on first need.
        self.approval_url = approval_url
        self.max_runs, self.idle_s = max_runs, idle_s
        self.approval_timeout_s, self.acp_timeout_s, self.stop_grace_s = approval_timeout_s, acp_timeout_s, stop_grace_s
        self.max_events, self.max_bytes = max_events, max_bytes
        self.runs: dict[str, Run] = {}
        #: Hooks for the hub (docs/agent-chat.md §8, §3): ``on_session(run)`` when a run learns or changes its
        #: session id, ``on_close(run)`` when a run is closed (by the console, the idle reaper or shutdown).
        self.on_session: Callable[[Run], None] | None = None
        self.on_close: Callable[[Run], None] | None = None
        self._server: asyncio.AbstractServer | None = None
        self._reaper: asyncio.Task | None = None

    def definitions(self) -> dict[str, AgentDef]:
        source = self._definitions
        if callable(source):
            return source()
        return source if isinstance(source, dict) else registry.definitions()

    # -- events
    def _push(self, run: Run, event: dict) -> dict:
        event = {key: value for key, value in event.items() if value is not None}
        run.seq += 1
        event = {"seq": run.seq, "t": round(time.time(), 3), **event}
        size = len(json.dumps(event, ensure_ascii=False, default=str))
        run.events.append((event, size))
        run.size += size
        while run.events and (len(run.events) > self.max_events or run.size > self.max_bytes):
            _, gone = run.events.popleft()
            run.size -= gone
            run.dropped += 1
        if event.get("type") == "status" and event.get("state") in STATES:
            run.state = event["state"]
        run.active = time.monotonic()
        self._wake(run)
        session_id = run.adapter.session_id
        if session_id and session_id != run.known_session and self.on_session is not None:
            run.known_session = session_id
            try:
                self.on_session(run)
            except Exception:
                log.exception("agent run on_session hook failed")
        return event

    def _push_all(self, run: Run, events: list[dict]) -> None:
        for event in events:
            self._push(run, event)
            if event.get("type") == "approval" and event.get("state") == "pending" and event.get("id"):
                self._time_out_in_band(run, str(event["id"]))
        self._flush(run)

    def _time_out_in_band(self, run: Run, approval_id: str) -> None:
        """An approval the CLI asked in-band (ACP ``session/request_permission``, claude ``can_use_tool``) is
        denied after :attr:`approval_timeout_s`, as the approval-MCP path is: nobody answering must not hold
        the process (and a run slot) for ever."""
        adapter, gen = run.adapter, run.gen

        def expire() -> None:
            if run.closed or run.adapter is not adapter or run.gen != gen:
                return
            if approval_id not in adapter.pending_approvals():
                return
            events = adapter.answer(approval_id, False, False) or []
            for event in events:
                if event.get("type") == "approval" and event.get("id") == approval_id:
                    event["reason"] = "timeout"
            self._push_all(run, events)

        try:
            run.timers.append(asyncio.get_running_loop().call_later(self.approval_timeout_s, expire))
        except RuntimeError:  # no running loop (a unit test feeding lines by hand)
            pass

    @staticmethod
    def _wake(run: Run) -> None:
        changed, run.changed = run.changed, asyncio.Event()
        changed.set()

    def _flush(self, run: Run) -> None:
        outbox, run.adapter.outbox = run.adapter.outbox, []
        if run.proc is not None:
            for data in outbox:
                run.proc.write(data)

    # -- processes
    def _spawn(self, run: Run, argv: list[str], *, stdin_data: bytes | None = None, keep_stdin: bool = True) -> None:
        if argv and argv[0] == run.opts.binary:
            argv = [*run.prefix, *argv[1:]]
        launch = command_line(argv, run.batch)
        run.gen += 1
        gen = run.gen
        run.stderr.clear()
        loop = asyncio.get_running_loop()
        try:
            run.proc = _Proc(launch, cwd=run.workdir, env=run.env, run_as=run.run_as, loop=loop,
                             on_line=lambda stream, line: self._on_line(run, gen, stream, line),
                             on_exit=lambda code: self._on_exit(run, gen, code),
                             stdin_data=stdin_data, keep_stdin=keep_stdin)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            run.proc = None
            raise RunError(f"无法启动 {run.d.label}：{exc}", "spawn_failed") from exc

    def _kill(self, run: Run, *, sync: bool = False) -> _Proc | None:
        """End the process; its exit is no longer reported (the generation moves on), so the adapter forgets
        the process's protocol state here (an ACP client must handshake again with the next process).
        ``sync``: only signal it now and return it (shutdown waits for every process at once)."""
        proc, run.proc = run.proc, None
        run.gen += 1
        if proc is not None:
            run.adapter.reset_process()
            if sync:
                proc.terminate_now()
            else:
                proc.kill()
        return proc

    def _on_line(self, run: Run, gen: int, stream: str, line: str) -> None:
        if gen != run.gen or run.closed:
            return
        if stream == "stderr":
            if line.strip():
                run.stderr.append(line)
            events = run.adapter.feed_err(line)
        else:
            events = run.adapter.feed(line)
        self._push_all(run, events)
        adapter = run.adapter
        if isinstance(adapter, adapters.Acp) and adapter.failed and not adapter.ready:
            self._fallback(run, adapter.failed)

    def _on_exit(self, run: Run, gen: int, code: int | None) -> None:
        if gen != run.gen or run.closed:
            return
        run.proc = None
        tail = "\n".join(run.stderr)
        adapter = run.adapter
        if adapter.persistent and adapter.retry_without(tail):
            try:
                self._spawn(run, adapter.argv())
                for data in adapter.replay():
                    run.proc.write(data)
                return
            except RunError as exc:
                self._push_all(run, [{"type": "error", "text": str(exc)}])
        if isinstance(adapter, adapters.Acp) and not adapter.ready and adapter.turn_active and not adapter.stopping:
            self._fallback(run, f"进程退出（退出码 {code}）" + (f"：{tail[-300:]}" if tail else ""))
            return
        self._deny_all(run, "进程已退出")
        events = adapter.exited(code, tail)
        if not events and not adapter.persistent:
            return
        self._push_all(run, events)

    def _fallback(self, run: Run, reason: str) -> None:
        """An ACP agent that did not start: run its ``fallback_command`` as text, a process per turn."""
        old = run.adapter
        self._kill(run)
        if run.opts.strict_resume and old.session_id:
            # a resume in another folder that the agent refused: neither a fresh session nor the text fallback
            old.turn_active = False
            self._push_all(run, [{"type": "error", "text": reason},
                                 {"type": "status", "state": "failed", "session_id": old.session_id}])
            return
        new = adapters.fallback(run.d, run.opts, old.session_id)
        if new is None:
            old.turn_active = False
            self._push_all(run, [{"type": "error", "text": f"{run.d.label} 没能以对话模式（ACP）启动：{reason}"},
                                 {"type": "status", "state": "failed", "session_id": old.session_id}])
            return
        run.adapter = new
        self._push(run, {"type": "error", "level": "warn",
                         "text": f"{run.d.label} 没能以对话模式（ACP）启动（{reason}），改用普通文本方式：能对话，"
                                 "但看不到工具调用详情，也不能逐项审批"})
        if run.opts.model and not run.d.model_args:
            self._push(run, {"type": "error", "level": "notice",
                             "text": f"{run.d.label} 的这种运行方式不能选择模型，沿用默认模型（{run.opts.model} 没有生效）"})
        if old.turn_active and run.last_prompt:
            self._start_turn(run, run.last_prompt, run.last_files)

    def _start_turn(self, run: Run, prompt: str, files: list[dict] | None = None) -> None:
        adapter = run.adapter
        files = list(files or [])
        if files and not adapter.takes_files:  # this adapter cannot attach them: their paths in the text (§18)
            prompt, files = attachments.name_files(prompt, files), []
        adapter.turn_files = files
        if getattr(adapter, "in_process", False):  # an API model's loop: a task on this event loop
            self._push_all(run, adapter.begin_turn(prompt))
            return
        try:
            if adapter.persistent:
                spawned = run.proc is None
                if spawned:
                    self._spawn(run, adapter.argv())
                    self._push_all(run, adapter.start())
                    if isinstance(adapter, adapters.Acp):
                        self._watch_acp(run)
                self._push_all(run, adapter.begin_turn(prompt))
                if not spawned and run.state not in ("starting", "running"):
                    # the next turn on a live process: say so, or the chat shows "idle" (no stop button)
                    self._push(run, {"type": "status", "state": "running", "session_id": adapter.session_id})
            else:
                argv = adapter.argv(prompt)
                self._spawn(run, argv, stdin_data=adapter.turn_stdin(), keep_stdin=False)
                self._push(run, {"type": "status", "state": "running", "session_id": adapter.session_id})
        except RunError as exc:
            adapter.turn_active = False
            self._push_all(run, [{"type": "error", "text": str(exc)},
                                 {"type": "status", "state": "failed", "session_id": adapter.session_id}])
            raise

    def _watch_acp(self, run: Run) -> None:
        gen = run.gen

        def check() -> None:
            adapter = run.adapter
            if gen == run.gen and not run.closed and isinstance(adapter, adapters.Acp) and not adapter.ready:
                self._fallback(run, f"{int(self.acp_timeout_s)} 秒内没有完成握手")

        run.timers.append(asyncio.get_running_loop().call_later(self.acp_timeout_s, check))

    # -- approvals
    def _deny_all(self, run: Run, reason: str) -> None:
        for approval_id, pending in list(run.approvals.items()):
            run.approvals.pop(approval_id, None)
            if not pending.future.done():
                pending.future.set_result((False, False, reason))
            self._push(run, {"type": "approval", "id": approval_id, "state": "denied", "reason": reason})

    def _find_token(self, token: str) -> Run | None:
        for run in self.runs.values():
            if token and secrets.compare_digest(run.token.encode(), token.encode()):
                return run
        return None

    async def approval_request(self, token: str, payload: dict) -> dict:
        """One request from the approval MCP server: an approval card, then Claude's permission-prompt
        answer ``{"behavior": "allow", "updatedInput"}`` / ``{"behavior": "deny", "message"}``."""
        run = self._find_token(str(token or ""))
        payload = payload if isinstance(payload, dict) else {}
        tool = str(payload.get("tool_name") or "")
        data = payload.get("input") if isinstance(payload.get("input"), dict) else {}
        if run is None or run.closed:
            return {"behavior": "deny", "message": "这个对话已经结束"}
        key = _remember_key(tool, data)
        run.approval_n += 1
        approval_id = f"ap_{run.approval_n}"
        summary = adapters.tool_title(tool, data)
        if key in run.remembered:
            self._push(run, {"type": "approval", "id": approval_id, "tool": tool, "summary": summary,
                             "state": "allowed", "remembered": True, "auto": True})
            return {"behavior": "allow", "updatedInput": data}
        future = asyncio.get_running_loop().create_future()
        run.approvals[approval_id] = _Approval(future, tool, data)
        self._push(run, {"type": "approval", "id": approval_id, "tool": tool, "summary": summary,
                         "input": adapters._small(data), "tool_use_id": payload.get("tool_use_id") or None,
                         "state": "pending", "remember": True,
                         "remember_scope": "command" if tool in SHELL_TOOLS else "tool"})
        try:
            allow, _remember, reason = await asyncio.wait_for(asyncio.shield(future), self.approval_timeout_s)
        except TimeoutError:
            run.approvals.pop(approval_id, None)
            minutes = max(1, round(self.approval_timeout_s / 60))
            self._push(run, {"type": "approval", "id": approval_id, "state": "denied", "reason": "timeout"})
            return {"behavior": "deny", "message": f"用户 {minutes} 分钟内没有回应，已自动拒绝"}
        if allow:
            return {"behavior": "allow", "updatedInput": data}
        return {"behavior": "deny", "message": reason or "用户拒绝了这个操作"}

    async def approval_endpoint_url(self) -> str:
        """The loopback URL for the approval MCP server, serving it here on first need."""
        if self.approval_url:
            return self.approval_url
        if self._server is None:
            self._server = await asyncio.start_server(self._serve_approval, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/approve"

    async def _serve_approval(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status, body = 200, {}
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            lines = head.decode("latin-1").split("\r\n")
            method, path = (lines[0].split(" ") + ["", ""])[:2]
            headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:] if line)}
            length = int(headers.get("content-length") or 0)
            if method != "POST" or path.split("?")[0] != "/approve":
                status, body = 404, {"error": "not found"}
            elif length <= 0 or length > 1024 * 1024:
                status, body = 400, {"error": "bad body"}
            else:
                payload = json.loads((await asyncio.wait_for(reader.readexactly(length), 10)).decode("utf-8"))
                token = payload.get("token") if isinstance(payload, dict) else None
                auth = headers.get("authorization", "")
                token = token or (auth[7:] if auth.lower().startswith("bearer ") else "")
                if self._find_token(str(token or "")) is None:
                    status, body = 403, {"error": "unknown token"}
                else:
                    body = await self.approval_request(str(token), payload)
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError, UnicodeDecodeError, ConnectionError):
            status, body = 400, {"error": "bad request"}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found"}[status]
        try:
            writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(data)}"
                         f"\r\nConnection: close\r\n\r\n".encode("latin-1") + data)
            await writer.drain()
            writer.close()
        except ConnectionError:
            pass

    def _approval_files(self, run: Run, url: str | None, attach: dict | None = None) -> str:
        """Write the run's ``--mcp-config``: anx's approval server (``url``, claude ``ask``; plus a copy of its script
        the run-as account can read) and/or the 本机 MCP as ``attach["name"]`` (a run attached by flags). Only this
        run's temp folder is written; the user's own config is never touched."""
        folder = Path(run.tmpdir)
        servers: dict = {}
        written = []
        if url:
            script = folder / "anx_approve_mcp.py"
            shutil.copyfile(Path(__file__).with_name("approve_mcp.py"), script)
            written.append(script)
            python = sys.executable
            if WINDOWS and python.lower().endswith("pythonw.exe"):
                console = python[:-len("pythonw.exe")] + "python.exe"
                python = console if os.path.isfile(console) else python
            servers["anx"] = {"type": "stdio", "command": python, "args": ["-I", str(script), "--url", url],
                              "env": {"ANX_APPROVE_TOKEN": run.token}}
        if isinstance(attach, dict) and attach.get("url"):
            servers[str(attach.get("name") or "agent-nexus")] = {"type": "http", "url": str(attach["url"])}
        config = {"mcpServers": servers}
        path = folder / "anx-mcp.json"
        path.write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")
        written.append(path)
        for item in written:
            try:
                os.chmod(item, 0o600)
                if run.run_as and "user" in run.run_as and not WINDOWS:
                    os.chown(item, int(run.run_as["user"]), int(run.run_as.get("group", -1)))
            except OSError:
                pass
        return str(path)

    # -- handlers
    def _run(self, params: dict) -> Run:
        run = self.runs.get(str(params.get("rid") or ""))
        if run is None:
            raise RunError("这个对话已经结束或不存在", "not_found")
        return run

    def _env(self, account: Any, extra: dict | None, path_first: list[str] | None = None) -> dict:
        env = account.env() if account is not None else dict(os.environ)
        for key in STRIP_ENV:
            env.pop(key, None)
        env.update({key: str(value) for key, value in (extra or {}).items() if value is not None})
        if path_first:
            prepend_path(env, path_first)
        return env

    def _workdir(self, value: Any, account: Any, *, checked: bool = False) -> str:
        if not isinstance(value, str) or not value.strip():
            home = getattr(account, "home", None) or os.path.expanduser("~")
            value = home
        path = os.path.abspath(os.path.expanduser(value.strip()))
        if not os.path.isdir(path):
            raise RunError(f"工作目录不存在：{path}", "bad_workdir")
        roots = self.roots() if self.roots and not checked else None
        if roots is not None and not _within(path, roots):
            raise RunError(f"工作目录不在允许的范围内：{path}", "workdir_not_allowed")
        return path

    async def open(self, params: dict, *, binary: str | None = None, env: dict | None = None,
                   run_as: dict | None | bool = False, system_prompt: str | None = None, tmpdir: str | None = None,
                   address: str = "", rid: str | None = None, events: list[dict] | None = None,
                   extra: dict | None = None, workdir_ok: bool = False, path_first: list[str] | None = None,
                   files: list[dict] | None = None, strict_resume: bool = False) -> dict:
        """Start a run. ``params`` (from the console): ``kind, workdir, prompt, resume_id?, permission?,
        title?``. The keyword arguments come from the hub, never from the console: an explicit ``binary``,
        extra ``env``, ``run_as`` (``Account.popen_args()``; False = the account's), the rendered
        ``system_prompt``, the run's ``tmpdir`` and mailbox ``address``, and ``events`` to show right after
        the starting status (the hub's notes, e.g. that there is no gateway). ``workdir_ok``: the hub already allowed
        the work folder (it also accepts folders this device ran a CLI in), so the roots are not checked again.
        ``path_first``: folders put first on the run's PATH (the hub's private ``anx``, agents/anx_shim.py).
        ``files``: the first message's attachments, already checked by the hub (§18; ``params`` never carries them).
        ``strict_resume``: the session is continued in another folder; an agent that refuses fails the run."""
        self._reap()
        self._ensure_reaper()
        if len(self.runs) >= self.max_runs:
            raise RunError(f"这台设备上已经有 {self.max_runs} 个对话在运行，先关掉一些", "too_many_runs")
        defs = self.definitions()
        kind = str(params.get("kind") or "")
        d = defs.get(kind)
        if d is None:
            raise RunError(f"不认识的 Agent：{kind or '（未选择）'}", "unknown_kind")
        prompt = params.get("prompt") if isinstance(params.get("prompt"), str) else ""
        if len(prompt) > MAX_PROMPT:
            raise RunError("消息太长了", "too_long")
        resume_id = str(params.get("resume_id") or "").strip()
        # Never a leading "-": the id is its own argv item (``--resume <id>``, ``resume <id>``) and must not
        # read as an option such as --dangerously-bypass-approvals-and-sandbox.
        if resume_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", resume_id):
            raise RunError("会话 ID 格式不对", "bad_resume_id")
        if resume_id and not d.resume:
            raise RunError(f"{d.label} 不支持续接已有会话", "resume_unsupported")
        model = _model(params) or ""
        notices: list[str] = []
        if model and d.adapter == "api":
            model = ""  # an API model: the kind is the model
        elif model and not d.model_args and d.adapter not in ("acp",):
            notices.append(f"{d.label} 不能选择模型，沿用它自己的设置（{model} 没有生效）")
            model = ""
        permission = str(params.get("permission") or "ask")
        if permission not in d.permission_flags:
            if permission in ("edits", "readonly") and "ask" in d.permission_flags:
                notices.append(f"{d.label} 没有“{_MODE_NAMES.get(permission, permission)}”模式，"
                               "改为逐项审批：每个需要权限的操作都会先问你")
                permission = "ask"
            else:
                raise RunError(f"{d.label} 不支持“{_MODE_NAMES.get(permission, permission)}”权限模式", "bad_permission")
        account = self.account()
        workdir = self._workdir(params.get("workdir"), account, checked=workdir_ok)
        run_env = self._env(account, env, path_first)
        popen = (account.popen_args() if account is not None else None) if run_as is False else (run_as or None)
        if d.adapter == "api":  # an API model: no binary, the loop runs in process (§16.4)
            binary = binary or "api"
        if binary is None:
            binary = await asyncio.to_thread(registry.locate, d, run_env)
        if not binary:
            raise RunError(f"这台设备上没有找到 {d.label}，可以先在 Agent 菜单里安装", "not_installed")
        prefix, batch = launcher(binary, run_env)
        if system_prompt is None:
            system_prompt = params.get("system_prompt") if isinstance(params.get("system_prompt"), str) else ""
        opts = AdapterOptions(binary=binary, workdir=workdir, permission=permission, system_prompt=system_prompt or "",
                              resume_id=resume_id, batch=batch, extra=dict(extra or {}), model=model,
                              strict_resume=bool(strict_resume and resume_id))
        if d.adapter == "codex-exec":  # the context window lives in the thread's rollout (§16.3)
            opts.context_lookup = _codex_lookup(account, run_env)
        rid = rid or uuid.uuid4().hex[:12]
        own = not tmpdir
        if own:
            base = self.tmp_root
            if base is not None:
                base.mkdir(parents=True, exist_ok=True)
            tmpdir = tempfile.mkdtemp(prefix=f"anx-run-{rid}-", dir=str(base) if base else None)
            if popen and "user" in popen and not WINDOWS:
                try:
                    os.chown(tmpdir, int(popen["user"]), int(popen.get("group", -1)))
                except OSError:
                    pass
        opts.tmpdir = str(tmpdir)
        run = Run(rid=rid, d=d, adapter=adapters.make(d, opts), opts=opts, workdir=workdir, env=run_env,
                  run_as=popen, prefix=prefix, batch=batch, permission=permission, address=address,
                  title=str(params.get("title") or "")[:200], tmpdir=str(tmpdir), own_tmpdir=own)
        attach = opts.extra.get("mcp_attach") if isinstance(opts.extra.get("mcp_attach"), dict) else None
        if d.adapter == "claude-stream" and (permission == "ask" or attach):
            url = None
            if permission == "ask":
                try:
                    url = await self.approval_endpoint_url()
                except OSError as exc:
                    notices.append(f"审批服务没能启动（{exc}），需要权限的操作会被拒绝")
            try:
                if url or attach:
                    opts.mcp_config = self._approval_files(run, url, attach)
            except OSError as exc:
                notices.append(f"本次对话的 MCP 配置没能写入（{exc}）" if not url else
                               f"审批服务没能启动（{exc}），需要权限的操作会被拒绝")
        if getattr(run.adapter, "in_process", False):
            run.adapter.attach(self, run)
        self.runs[rid] = run
        self._push(run, {"type": "status", "state": "starting", "session_id": resume_id or None})
        for event in events or ():
            if isinstance(event, dict) and event.get("type"):
                self._push(run, event)
        for text in notices:
            self._push(run, {"type": "error", "level": "warn", "text": text})
        try:
            files = list(files or [])
            if prompt.strip() or files:
                files = await self._load_files(run, files)
                self._push(run, self._user_event(prompt, files))
                # kept without the image bytes: only the text fallback re-sends it, and it names the paths
                run.last_prompt, run.last_files = (prompt if prompt.strip() else attachments.EMPTY_PROMPT,
                                                   attachments.public(files))
                self._start_turn(run, run.last_prompt, files)
            elif run.adapter.persistent:  # warm start: the process waits for the first message
                self._spawn(run, run.adapter.argv())
                self._push_all(run, run.adapter.start())
                if isinstance(run.adapter, adapters.Acp):
                    self._watch_acp(run)
            else:
                self._push(run, {"type": "status", "state": "idle", "session_id": resume_id or None})
        except RunError:
            self._close(run)
            raise
        return {"rid": rid, "session_id": run.session_id, "state": run.state, "address": address,
                "adapter": run.adapter.name, "tmpdir": run.tmpdir, "permission": permission,
                "model": model or None}

    @staticmethod
    def _user_event(prompt: str, files: list[dict]) -> dict:
        event: dict = {"type": "user", "text": prompt}
        if files:
            event["attachments"] = attachments.public(files)
        return event

    async def _load_files(self, run: Run, files: list[dict]) -> list[dict]:
        """The inline images' bytes for adapters that send them (Claude, ACP), read in a thread."""
        limit = run.adapter.inline_image_max if run.adapter.takes_files else 0
        if not files or not limit:
            return files
        return await asyncio.to_thread(attachments.load_images, files, limit)

    async def send(self, params: dict, *, files: list[dict] | None = None) -> dict:
        """The next message. ``files``: its attachments, already checked by the hub (§18)."""
        run = self._run(params)
        prompt = params.get("prompt") if isinstance(params.get("prompt"), str) else ""
        files = list(files or [])
        if not prompt.strip() and not files:
            raise RunError("消息是空的", "empty")
        if len(prompt) > MAX_PROMPT:
            raise RunError("消息太长了", "too_long")
        model = _model(params)
        if run.adapter.turn_active:
            raise RunError("上一轮还没有结束：等它结束，或者先停止", "busy")
        if model is not None and model != (run.opts.model or "") and run.d.adapter != "api":
            self.set_model(run, model)
        files = await self._load_files(run, files)
        if run.adapter.turn_active or run.closed:  # a turn began while the images were read
            raise RunError("上一轮还没有结束：等它结束，或者先停止", "busy")
        self._push(run, self._user_event(prompt, files))
        run.last_prompt, run.last_files = (prompt if prompt.strip() else attachments.EMPTY_PROMPT,
                                           attachments.public(files))
        self._start_turn(run, run.last_prompt, files)
        return {"ok": True, "session_id": run.session_id, "state": run.state, "model": run.opts.model or None}

    def set_model(self, run: Run, model: str) -> None:
        """Switch ``run`` to ``model`` ("" = the CLI's default) from its next turn on (§16.7)."""
        # the adapter announces the model ({type: model}) only once the switch is final: at once for a per-turn
        # or flag-chosen CLI, from the CLI's answer for a live Claude / ACP process
        events = run.adapter.set_model(model, live=run.proc is not None)
        self._push_all(run, events)
        if run.proc is not None and run.adapter.outbox:
            self._flush(run)

    async def read(self, params: dict) -> dict:
        run = self._run(params)
        run.seen = time.monotonic()
        try:
            cursor = max(0, int(params.get("cursor") or 0))
            wait = max(0.0, min(float(params.get("wait") or 0), MAX_WAIT_S))
        except (TypeError, ValueError):
            raise RunError("cursor / wait 参数不对", "bad_params") from None
        if cursor > run.seq:
            cursor = 0  # a cursor from somewhere else: start over
        if run.seq <= cursor and wait > 0 and not run.closed:
            changed = run.changed
            try:
                await asyncio.wait_for(changed.wait(), wait)
            except TimeoutError:
                pass
        run.seen = time.monotonic()
        first = run.events[0][0]["seq"] if run.events else run.seq + 1
        dropped = max(0, first - cursor - 1)
        events = [event for event, _ in run.events if event["seq"] > cursor][:MAX_READ_EVENTS]
        result = {"events": events, "cursor": events[-1]["seq"] if events else max(cursor, first - 1),
                  "state": "closed" if run.closed else run.state, "session_id": run.session_id,
                  "busy": run.adapter.turn_active}
        if dropped:
            result["dropped"] = dropped
        return result

    async def answer(self, params: dict) -> dict:
        run = self._run(params)
        approval_id = str(params.get("approval_id") or params.get("id") or "")
        allow, remember = bool(params.get("allow")), bool(params.get("remember"))
        pending = run.approvals.pop(approval_id, None)
        if pending is not None:
            if allow and remember:
                run.remembered.add(_remember_key(pending.tool, pending.data))
            if not pending.future.done():
                pending.future.set_result((allow, remember, ""))
            self._push(run, {"type": "approval", "id": approval_id, "state": "allowed" if allow else "denied",
                             "remembered": remember})
            return {"ok": True}
        events = run.adapter.answer(approval_id, allow, remember)
        if events is None:
            raise RunError("这个审批已经处理过或已经过期", "not_found")
        self._push_all(run, events)
        return {"ok": True}

    async def stop(self, params: dict) -> dict:
        run = self._run(params)
        self._deny_all(run, "已停止")
        adapter = run.adapter
        if not adapter.turn_active:
            return {"ok": True, "idle": True}
        self._push_all(run, adapter.stop())
        if not adapter.turn_active:
            return {"ok": True}
        proc, turn, gen = run.proc, adapter.turns, run.gen
        if proc is None:
            adapter.turn_active = adapter.stopping = False
            self._push(run, {"type": "status", "state": "stopped", "session_id": adapter.session_id})
            return {"ok": True}
        if not adapter.in_band_stop:
            proc.interrupt()

        def force() -> None:
            if run.gen == gen and run.proc is proc and adapter.turn_active and adapter.turns == turn:
                proc.kill()  # its exit closes the turn as "stopped"

        run.timers.append(asyncio.get_running_loop().call_later(self.stop_grace_s, force))
        return {"ok": True}

    def _close(self, run: Run, *, sync: bool = False) -> _Proc | None:
        run.closed = True
        if getattr(run.adapter, "in_process", False):
            run.adapter.shutdown()
        self.runs.pop(run.rid, None)
        self._deny_all(run, "对话已关闭")
        for timer in run.timers:
            timer.cancel()
        run.timers.clear()
        proc = self._kill(run, sync=sync)
        run.state = "closed"
        self._wake(run)
        if run.own_tmpdir and run.tmpdir:
            shutil.rmtree(run.tmpdir, ignore_errors=True)
        if self.on_close is not None:
            try:
                self.on_close(run)
            except Exception:
                log.exception("agent run on_close hook failed")
        return proc

    async def close(self, params: dict) -> dict:
        run = self.runs.get(str(params.get("rid") or ""))
        if run is not None:
            self._close(run)
        return {"ok": True}

    async def list(self, params: dict | None = None) -> dict:
        self._reap()
        return {"runs": [run.view() for run in self.runs.values()]}

    # -- housekeeping
    def _reap(self) -> None:
        now = time.monotonic()
        for run in list(self.runs.values()):
            busy = run.adapter.turn_active or run.approvals or run.adapter.pending_approvals()
            if busy:
                # A turn that has shown nothing for a long time while nobody reads (the tab was closed on
                # a stuck CLI) gives its process and its run slot back; the session can be resumed.
                if run.proc is not None and now - max(run.active, run.seen) > self.idle_s * STUCK_FACTOR:
                    self._deny_all(run, "长时间没有动静，已结束")
                    self._kill(run)
                    run.adapter.turn_active = run.adapter.stopping = False
                    self._push(run, {"type": "status", "state": "stopped", "session_id": run.session_id,
                                     "note": "这一轮长时间没有任何输出，也没有人查看，进程已结束；再发消息会续接这个会话"})
                continue
            if run.proc is not None and now - run.active > self.idle_s:
                self._kill(run)
                self._push(run, {"type": "status", "state": "idle", "session_id": run.session_id,
                                 "note": "空闲超过 30 分钟，进程已关闭；再发消息会续接这个会话"})
            elif run.proc is None and now - max(run.active, run.seen) > self.idle_s:
                self._close(run)

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            async def loop() -> None:
                while True:
                    await asyncio.sleep(REAP_EVERY_S)
                    self._reap()
            self._reaper = asyncio.ensure_future(loop())

    def close_all(self) -> None:
        """Shutdown: no agent CLI outlives the manager. Synchronous on purpose -- the interpreter exits right
        after, so daemon threads would die before their SIGKILL / taskkill: SIGTERM every process group, wait
        up to :data:`SHUTDOWN_GRACE_S` for all of them together, then SIGKILL what is left."""
        procs = [proc for proc in (self._close(run, sync=True) for run in list(self.runs.values())) if proc]
        deadline = time.monotonic() + SHUTDOWN_GRACE_S
        for proc in procs:
            try:
                proc.p.wait(max(0.0, deadline - time.monotonic()))
            except Exception:  # noqa: BLE001, S110 - still running, or a handle that cannot be waited on: killed below
                pass
        for proc in procs:
            try:
                alive = proc.p.poll() is None
            except Exception:  # noqa: BLE001
                alive = True
            if alive or not WINDOWS:
                proc.kill_now()  # POSIX: also the group's children that outlived their leader
        if self._reaper is not None:
            self._reaper.cancel()
            self._reaper = None
        if self._server is not None:
            self._server.close()
            self._server = None

    def handlers(self) -> dict:
        """The reverse-RPC handlers; only console parameters pass (no binary / env / run_as)."""
        def wrap(fn):
            async def handler(params: dict) -> dict:
                try:
                    return await fn(params if isinstance(params, dict) else {})
                except RunError as exc:
                    return {"error": str(exc), "code": exc.code}
                except Exception as exc:  # noqa: BLE001 - handlers never raise
                    return {"error": f"出错了：{exc}", "code": "internal"}
            return handler

        return {"agent_run_open": wrap(self.open), "agent_run_send": wrap(self.send), "agent_run_read": wrap(self.read),
                "agent_run_answer": wrap(self.answer), "agent_run_stop": wrap(self.stop),
                "agent_run_close": wrap(self.close), "agent_run_list": wrap(self.list)}


_MODE_NAMES = {"ask": "需要审批", "edits": "自动改文件", "readonly": "只读", "full": "完全信任"}

__all__ = ["MAX_BYTES", "MAX_EVENTS", "Run", "RunError", "Runs", "command_line", "launcher", "npm_shim_target"]
