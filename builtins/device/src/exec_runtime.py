"""Resumable command execution behind ``exec_command`` / ``write_stdin``.

A process either finishes inside the yield window or is retained behind an
integer session id that later calls continue, poll or kill.
"""
from __future__ import annotations

import asyncio
import codecs
import locale
import math
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import shells as shells_mod
from .settings import Config
from .policy import Policy

#: Default for the longest a single exec_command / write_stdin / exec_wait call
#: may block (``limits.call_wait_max_s``). MCP clients such as Claude Code cut a
#: tool call at about 60 s, so a long wait returns a session_id well before.
MAX_WAIT_MS = 50_000
DEFAULT_EXEC_WAIT_MS = 10_000
DEFAULT_EXEC_WAIT_TIMEOUT_MS = 30_000
#: Output kept with a finished session after its exit was collected, so a
#: non-consuming reader (exec_tail, behind the gateway's wait URL) still gets the
#: last part that exec_wait took.
COMPLETED_TAIL_CHARS = 64_000
#: Default and ceiling of one exec_tail answer, in characters.
TAIL_MAX_CHARS = 60_000
#: Environment variable that carries the command to a bash on Windows (see _shell_launch).
CMD_ENV = "ANX_EXEC_CMD"
# The variable is unset inside the evaluated text, so the command sees no extra
# shell variable (not even a temporary one) and its children no extra env entry.
_BASH_EVAL = f'eval "unset {CMD_ENV}; ${CMD_ENV}"'


class ShellUnavailable(ValueError):
    """A named shell this device does not have."""


def cmd_command_line(cmd_exe: str, cmd: str) -> str:
    """cmd.exe's command line: /d (no AutoRun hooks) /s /c and the command as typed.

    With /s cmd strips exactly the outer pair of quotes and runs the rest
    verbatim, so ``echo "a b"`` reaches cmd unchanged.
    """
    return f'"{cmd_exe}" /d /s /c "{cmd}"'


async def _spawn_command_line(cmdline: str, *, executable: str, cwd: str, env: dict[str, str],
                              creationflags: int = 0) -> asyncio.subprocess.Process:
    """Start ``executable`` with ``cmdline`` handed to CreateProcess unchanged.

    asyncio.create_subprocess_exec always joins argv with list2cmdline, and
    create_subprocess_shell always builds '<comspec> /c "<args>"' (no /d), so
    this goes one level down: Popen with a str args and shell=False passes it
    verbatim (lpApplicationName = executable).
    """
    loop = asyncio.get_running_loop()
    protocol = asyncio.subprocess.SubprocessStreamProtocol(limit=2 ** 16, loop=loop)
    transport = await loop._make_subprocess_transport(  # noqa: SLF001 - see docstring
        protocol, cmdline, False, asyncio.subprocess.PIPE, asyncio.subprocess.PIPE,
        asyncio.subprocess.PIPE, 0, executable=executable, cwd=cwd, env=env, creationflags=creationflags)
    return asyncio.subprocess.Process(transport, protocol, loop)


def resolve_default_shell(bash_path: str) -> str:
    """The shell exec_command uses when the caller names none.

    The configured bash when it exists. On Windows without it: Git Bash found
    through ``git`` on PATH (Git for Windows installed somewhere else), else
    PowerShell, so commands still run on a machine without Git.
    """
    if sys.platform != "win32" or Path(bash_path).is_file():
        return bash_path
    git = shutil.which("git")
    if git:
        for candidate in (Path(git).parent.parent / "bin" / "bash.exe", Path(git).parent / "bash.exe"):
            if candidate.is_file():
                return str(candidate)
    return shutil.which("powershell") or "powershell"


class _StreamDecoder:
    """Incremental decoding of a process stream.

    Decoding each 8 KiB read on its own breaks a multi-byte character that
    straddles two reads. Output is UTF-8 until a byte sequence proves otherwise;
    on Windows the stream then switches to the console code page (cp936 on
    Chinese Windows), which is what native tools such as ``ipconfig`` print.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._strict = True

    def _fallback(self) -> None:
        encoding = "utf-8"
        if sys.platform == "win32":
            encoding = locale.getpreferredencoding(False) or "utf-8"
        try:
            self._decoder = codecs.getincrementaldecoder(encoding)("replace")
        except LookupError:
            self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._strict = False

    def decode(self, data: bytes, final: bool = False) -> str:
        if self._strict:
            pending = self._decoder.getstate()[0]
            try:
                return self._decoder.decode(data, final)
            except UnicodeDecodeError:
                self._fallback()
                data = pending + data
        return self._decoder.decode(data, final)


@dataclass
class _ExecHandle:
    session_id: int
    proc: asyncio.subprocess.Process
    cmd: str
    cwd: Path
    started: float
    last_used: float
    chunks: list[tuple[int, str]] = field(default_factory=list)
    seq: int = 0
    delivered: int = 0
    reader_tasks: list[asyncio.Task[None]] = field(default_factory=list)
    completion_task: asyncio.Task[None] | None = None
    finished: float | None = None
    shell: str | None = None
    stdin_task: asyncio.Task[None] | None = None
    #: Random id of this process. Session ids restart at 1000 after a plugin
    #: restart, so whoever must not mix two processes up (a wait URL) binds to this.
    uid: str = field(default_factory=lambda: secrets.token_hex(8))
    #: Set (and replaced) whenever output arrives or the process ends; waiters
    #: take the current event before checking, so no wake-up is lost.
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    #: Highest seq dropped from ``chunks`` by the buffer bound (0: nothing lost).
    base_seq: int = 0
    buffered: int = 0

    def notify(self) -> None:
        event, self.changed = self.changed, asyncio.Event()
        event.set()


class UnifiedExecManager:
    """Own the resumable command processes behind the exec tools."""

    def __init__(self, config: Config, policy: Policy) -> None:
        self.config = config
        self.policy = policy
        self._handles: dict[int, _ExecHandle] = {}
        self._completed: dict[int, dict[str, Any]] = {}
        self._next_session_id = 1000
        self.default_shell = resolve_default_shell(config.bash_path)
        self._lock = asyncio.Lock()

    @property
    def pty_available(self) -> bool:
        # The legacy terminal server already established that a reliable PTY
        # path is not available in this Windows setup. We still accept tty so
        # callers can pass it, but execute with pipes.
        return False

    async def exec_command(
        self,
        cmd: str,
        workdir: str | None = None,
        tty: bool = False,
        yield_time_ms: float | None = None,
        max_output_tokens: float | None = None,
        shell: str | None = None,
        login: bool | None = None,
        stdin: str | None = None,
    ) -> dict[str, Any]:
        await self._reap_idle()
        # Only live processes count. A finished session waiting for its final
        # read (often one whose caller was disconnected) must not block work.
        running = sum(1 for h in self._handles.values() if h.proc.returncode is None)
        if running >= self.config.limits.max_sessions:
            raise RuntimeError(
                f"exec session limit reached ({self.config.limits.max_sessions} running); "
                "finish or terminate a running session first"
            )
        self.policy.check_command(cmd)
        cwd = self.policy.resolve_cwd(workdir)
        info = self.resolve_shell(shell, await self.shells_async())
        launch = self._shell_launch(info, cmd, True if login is None else bool(login))

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        # Python children read and write files as UTF-8 too, not the ANSI code page.
        env["PYTHONUTF8"] = "1"
        env.update(self.config.env)
        env["TERM"] = "xterm-256color" if tty else "dumb"
        extra = dict(launch.get("env") or {})
        if extra.get("WSLENV") and env.get("WSLENV"):
            extra["WSLENV"] = f'{env["WSLENV"]}:{extra["WSLENV"]}'
        env.update(extra)

        creationflags = 0
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            # Its own process group, so exec_kill and \x03 reach grandchildren too.
            kwargs["start_new_session"] = True

        try:
            if launch.get("cmdline") is not None:
                # cmd.exe: the exact command line, '/d /s /c' as in 0.8.34 but
                # without list2cmdline's backslash escaping of the quotes.
                proc = await _spawn_command_line(launch["cmdline"], executable=info.path, cwd=str(cwd),
                                                 env=env, creationflags=creationflags)
            else:
                proc = await asyncio.create_subprocess_exec(
                    *launch["argv"],
                    cwd=str(cwd),
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=creationflags,
                    **kwargs,
                )
        except OSError as exc:
            return {
                "wall_time_seconds": 0.0,
                "output": f"failed to start command: {exc}",
                "exit_code": None,
                "error": str(exc),
                "shell": info.name,
            }

        sid = await self._allocate_session_id()
        now = time.time()
        handle = _ExecHandle(sid, proc, cmd, cwd, now, now, shell=info.name)
        handle.reader_tasks = [
            asyncio.create_task(self._read_stream(handle, proc.stdout)),
            asyncio.create_task(self._read_stream(handle, proc.stderr)),
        ]
        handle.completion_task = asyncio.create_task(self._track_completion(handle))
        # Registered at once: a call cancelled while it waits (the client gave
        # up) leaves a session that exec_list, exec_wait and exec_kill still see.
        self._handles[sid] = handle

        if stdin is not None and proc.stdin is not None:
            # Fed in the background: a command that does not read its input
            # must not hold this call past the wait window (drain would block).
            handle.stdin_task = asyncio.create_task(self._feed_stdin(proc, stdin))

        wait_ms = DEFAULT_EXEC_WAIT_MS if yield_time_ms is None else float(yield_time_ms)
        # A command that finishes earlier returns at once; the window only
        # bounds how long a slow one blocks before it gets a session_id.
        wait_ms = min(max(wait_ms, 250), self.max_wait_ms)

        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=wait_ms / 1000.0)
        except asyncio.TimeoutError:
            result = self._snapshot(handle, max_output_tokens)
            result["session_id"] = sid
            result["pid"] = proc.pid
            result["shell"] = info.name
            if tty and not self.pty_available:
                result["note"] = "tty=true requested; this server currently uses pipes on Windows"
            return result

        await self._finish_readers(handle)
        result = self._snapshot(handle, max_output_tokens, final=True)
        result["exit_code"] = proc.returncode
        result["shell"] = info.name
        self._release(handle)
        self._close_pipes(proc)
        return result

    @property
    def max_wait_ms(self) -> float:
        return float(getattr(self.config.limits, "call_wait_max_s", MAX_WAIT_MS / 1000)) * 1000.0

    def available_shells(self) -> dict[str, shells_mod.ShellInfo]:
        """Detected shells (blocking on a cache miss; async callers use shells_async)."""
        return shells_mod.detect(self.config.bash_path, getattr(self.config, "shells", None))

    async def shells_async(self) -> dict[str, shells_mod.ShellInfo]:
        """Detected shells without blocking the event loop (wsl.exe can take seconds)."""
        hit = shells_mod.cached(self.config.bash_path, getattr(self.config, "shells", None))
        if hit is not None:
            return hit
        return await asyncio.to_thread(self.available_shells)

    @staticmethod
    async def _feed_stdin(proc: asyncio.subprocess.Process, text: str) -> None:
        stream = proc.stdin
        if stream is None:
            return
        try:
            stream.write(text.encode("utf-8"))
            await stream.drain()
        except (BrokenPipeError, ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            try:
                stream.close()  # input then end of file, like `cmd < file`
            except Exception:
                pass

    def default_shell_name(self) -> str:
        return shells_mod.shell_name(self.default_shell)

    def resolve_shell(self, shell: str | None,
                      found: dict[str, shells_mod.ShellInfo] | None = None) -> shells_mod.ShellInfo:
        """``shell`` as given by the caller -> the executable that runs it.

        None is the default shell (unchanged). A known name (bash, sh, zsh,
        pwsh, powershell, cmd; any case, optional .exe) maps to the detected
        absolute path, never to whatever CreateProcess would find first. An
        existing path keeps working as before.
        """
        if found is None:
            found = self.available_shells()
        if shell is None or not str(shell).strip():
            path = self.default_shell
            name = shells_mod.shell_name(path)
            item = found.get(name)
            if item is not None and os.path.normcase(item.path) == os.path.normcase(path):
                return item
            return shells_mod.ShellInfo(name, path)
        shell = str(shell).strip()
        name = shells_mod.shell_name(shell)
        looks_like_path = any(sep in shell for sep in ("/", "\\")) or os.path.isabs(shell)
        if looks_like_path:
            if Path(shell).is_file():
                return shells_mod.ShellInfo(name, shell)
            raise ShellUnavailable(self._missing_shell(shell, found))
        if name in shells_mod.SHELL_NAMES:
            if name in found:
                return found[name]
            alias = shells_mod.ALIASES.get(name)
            if alias in found:
                return found[alias]
            raise ShellUnavailable(self._missing_shell(name, found))
        located = shutil.which(shell)
        if located:  # any other program on PATH, as before
            return shells_mod.ShellInfo(name, located)
        raise ShellUnavailable(self._missing_shell(shell, found))

    def _missing_shell(self, shell: str, found: dict[str, shells_mod.ShellInfo]) -> str:
        names = shells_mod.ordered(found, self.default_shell_name())
        return (f"shell '{shell}' is not available on {socket.gethostname()}; "
                f"available: {', '.join(names) or 'none'} (default: {self.default_shell_name()})")

    def exec_wait(self, session_id: int, timeout_ms: float | None = None,
                  max_output_tokens: float | None = None):
        """Wait for a running session: new output and exit_code once it exits, or the timeout."""
        wait = DEFAULT_EXEC_WAIT_TIMEOUT_MS if timeout_ms is None else timeout_ms
        return self.write_stdin(session_id, "", yield_time_ms=wait, max_output_tokens=max_output_tokens)

    async def exec_tail(self, session_id: int, job: str, since: int = 0,
                        timeout_ms: float | None = None,
                        max_chars: int | None = None) -> dict[str, Any]:
        """Read output after ``since`` without consuming it (exec_wait still gets it).

        Long-polls until there is output with a higher seq, the process exits,
        or ``timeout_ms`` (at most the call cap). ``job`` must be the uid the
        exec result carried, so a reused session id never shows another
        process. Each call counts as use: a tailed job is not reaped as idle.
        """
        sid = int(session_id)
        since = max(0, int(since or 0))
        limit = TAIL_MAX_CHARS if not max_chars else max(1024, min(int(max_chars), TAIL_MAX_CHARS))
        wait_ms = DEFAULT_EXEC_WAIT_TIMEOUT_MS if timeout_ms is None else float(timeout_ms)
        deadline = time.monotonic() + min(max(wait_ms, 0), self.max_wait_ms) / 1000.0
        handle = self._handles.get(sid)
        if handle is None:
            return self._tail_completed(sid, job, since, limit)
        if handle.uid != str(job):
            raise ValueError(f"exec session {sid} is a different job now")
        while True:
            handle.last_used = time.time()
            event = handle.changed
            exited = handle.proc.returncode is not None
            if exited:
                # Its pipes may still hold the last output: let the readers drain.
                pending = [task for task in handle.reader_tasks if not task.done()]
                if pending:
                    await asyncio.wait(pending, timeout=max(0.0, min(5.0, deadline - time.monotonic())))
            result = self._tail_slice(handle.chunks, handle.base_seq, handle.seq, since, limit)
            if result["output"] or exited:
                break
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                await asyncio.wait_for(event.wait(), timeout=left)
            except asyncio.TimeoutError:
                pass
            if self._handles.get(sid) is not handle:
                return self._tail_completed(sid, job, since, limit)
        handle.last_used = time.time()
        code = handle.proc.returncode
        done = code is not None and result["seq"] >= handle.seq
        result["running"] = not done
        result["session_id"] = sid
        result["job"] = handle.uid
        if done:
            result["exit_code"] = code
        return result

    def _tail_completed(self, sid: int, job: str, since: int, limit: int) -> dict[str, Any]:
        record = self._completed.get(sid)
        if record is None or record.get("_job") != str(job):
            raise KeyError(f"unknown exec job {job} (session {sid})")
        chunks = record.get("_tail") or []
        result = self._tail_slice(chunks, int(record.get("_base_seq") or 0),
                                  int(record.get("_seq") or 0), since, limit)
        done = result["seq"] >= int(record.get("_seq") or 0)
        result.update({"session_id": sid, "job": record["_job"], "running": not done})
        if done:
            result["exit_code"] = record.get("exit_code")
        return result

    @staticmethod
    def _tail_slice(chunks: list[tuple[int, str]], base_seq: int, top: int, since: int,
                    limit: int) -> dict[str, Any]:
        parts: list[str] = []
        size = 0
        last = since
        for seq, text in chunks:
            if seq <= since:
                continue
            if parts and size + len(text) > limit:
                break
            parts.append(text)
            size += len(text)
            last = seq
        if not parts:
            last = max(since, top)
        result: dict[str, Any] = {"seq": last, "output": "".join(parts)}
        if since < base_seq:
            result["skipped"] = True
        return result

    async def write_stdin(
        self,
        session_id: int,
        chars: str = "",
        yield_time_ms: float | None = None,
        max_output_tokens: float | None = None,
    ) -> dict[str, Any]:
        await self._reap_idle()
        handle = self._handles.get(int(session_id))
        if handle is None:
            completed = self._completed.get(int(session_id))
            if completed is not None:
                result = {
                    "session_id": int(session_id),
                    "exit_code": completed["exit_code"],
                    "output": "",
                    "note": "session finished earlier and its uncollected output was released",
                }
                if completed.get("_job"):
                    result["job"] = completed["_job"]
                return result
            raise KeyError(f"unknown exec session_id {session_id}")
        handle.last_used = time.time()

        proc = handle.proc
        if proc.returncode is None and chars:
            if chars == "\x03":
                # Pipes do not deliver terminal Ctrl-C semantics. Treat the
                # canonical control character as an interrupt request.
                self._kill_tree(proc)
            else:
                if proc.stdin is None:
                    raise RuntimeError("process stdin is unavailable")
                try:
                    proc.stdin.write(chars.encode("utf-8"))
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError) as exc:
                    raise RuntimeError(f"process is no longer accepting stdin: {exc}") from exc

        if yield_time_ms is None:
            wait_ms = 250 if chars else 5_000
        else:
            wait_ms = float(yield_time_ms)
        wait_ms = min(max(wait_ms, 0), self.max_wait_ms)

        if proc.returncode is None and wait_ms > 0:
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=wait_ms / 1000.0)
            except asyncio.TimeoutError:
                pass

        if proc.returncode is None:
            result = self._snapshot(handle, max_output_tokens)
            result["session_id"] = int(session_id)
            if handle.shell:
                result["shell"] = handle.shell
            return result

        await self._finish_readers(handle)
        result = self._snapshot(handle, max_output_tokens, final=True)
        result["exit_code"] = proc.returncode
        if handle.shell:
            result["shell"] = handle.shell
        self._release(handle)
        self._close_pipes(proc)
        return result

    async def kill(self, session_id: int) -> dict[str, Any]:
        handle = self._handles.get(int(session_id))
        if handle is None:
            raise KeyError(f"unknown exec session_id {session_id}")
        if handle.stdin_task is not None:
            handle.stdin_task.cancel()
        self._kill_tree(handle.proc)
        try:
            await asyncio.wait_for(handle.proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        await self._finish_readers(handle)
        result = self._snapshot(handle, None, final=True)
        result["exit_code"] = handle.proc.returncode
        result["note"] = "process tree terminated"
        self._release(handle)
        self._close_pipes(handle.proc)
        return result

    def session_status(self, session_id: int) -> dict[str, Any] | None:
        sid = int(session_id)
        handle = self._handles.get(sid)
        if handle is not None:
            return {
                "session_id": sid,
                "pid": handle.proc.pid,
                "cmd": handle.cmd,
                "workdir": str(handle.cwd),
                "completed": handle.proc.returncode is not None,
                "running": handle.proc.returncode is None,
                "exit_code": handle.proc.returncode,
                "started": handle.started,
            }
        completed = self._completed.get(sid)
        if completed is None:
            return None
        return {key: value for key, value in completed.items() if not key.startswith("_")}

    def list_sessions(self) -> list[dict[str, Any]]:
        now = time.time()
        return [
            {
                "session_id": h.session_id,
                "cmd": h.cmd,
                "workdir": str(h.cwd),
                "running": h.proc.returncode is None,
                "exit_code": h.proc.returncode,
                "wall_time_seconds": round(now - h.started, 3),
            }
            for h in self._handles.values()
        ]

    async def close(self) -> None:
        for sid in list(self._handles):
            try:
                await self.kill(sid)
            except Exception:
                pass

    async def _allocate_session_id(self) -> int:
        async with self._lock:
            sid = self._next_session_id
            self._next_session_id += 1
            return sid

    async def _read_stream(
        self, handle: _ExecHandle, stream: asyncio.StreamReader | None
    ) -> None:
        if stream is None:
            return
        decoder = _StreamDecoder()
        try:
            while True:
                data = await stream.read(8192)
                text = decoder.decode(data, final=not data)
                if text:
                    handle.seq += 1
                    handle.chunks.append((handle.seq, text))
                    handle.buffered += len(text)
                    self._trim(handle)
                    handle.notify()
                if not data:
                    return
        except (asyncio.CancelledError, ValueError):
            return

    def _trim(self, handle: _ExecHandle) -> None:
        """Keep at most ``limits.max_buffer_chars`` of output (the oldest goes first)."""
        limit = int(getattr(self.config.limits, "max_buffer_chars", 0) or 0)
        if limit <= 0:
            return
        while handle.buffered > limit and len(handle.chunks) > 1:
            seq, text = handle.chunks.pop(0)
            handle.buffered -= len(text)
            handle.base_seq = seq

    def _release(self, handle: _ExecHandle) -> None:
        """Drop a finished handle, keeping its last output for exec_tail."""
        self._handles.pop(handle.session_id, None)
        record = self._completed.get(handle.session_id)
        if record is None or record.get("_job") != handle.uid:
            if handle.proc.returncode is None:
                return
            record = self._store_completed(handle, handle.proc.returncode)
        tail: list[tuple[int, str]] = []
        size = 0
        for seq, text in reversed(handle.chunks):
            if size + len(text) > COMPLETED_TAIL_CHARS and tail:
                break
            tail.insert(0, (seq, text))
            size += len(text)
        record["_tail"] = tail
        record["_seq"] = handle.seq
        record["_base_seq"] = tail[0][0] - 1 if tail else handle.seq

    async def _track_completion(self, handle: _ExecHandle) -> None:
        try:
            code = await handle.proc.wait()
        except asyncio.CancelledError:
            return
        handle.finished = time.time()
        self._store_completed(handle, code)
        handle.notify()

    def _store_completed(self, handle: _ExecHandle, code: int | None) -> dict[str, Any]:
        record = self._completed[handle.session_id] = {
            "session_id": handle.session_id,
            "pid": handle.proc.pid,
            "cmd": handle.cmd,
            "workdir": str(handle.cwd),
            "completed": True,
            "running": False,
            "exit_code": code,
            "started": handle.started,
            "finished": time.time(),
            "_job": handle.uid,
        }
        while len(self._completed) > 100:
            self._completed.pop(next(iter(self._completed)))
        return record

    async def _finish_readers(self, handle: _ExecHandle) -> None:
        if not handle.reader_tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*handle.reader_tasks, return_exceptions=True), timeout=5.0
            )
        except asyncio.TimeoutError:
            for task in handle.reader_tasks:
                task.cancel()
            await asyncio.gather(*handle.reader_tasks, return_exceptions=True)

    def _snapshot(
        self,
        handle: _ExecHandle,
        max_output_tokens: float | None,
        final: bool = False,
    ) -> dict[str, Any]:
        parts: list[str] = []
        for seq, text in handle.chunks:
            if seq > handle.delivered:
                parts.append(text)
        lost = handle.delivered < handle.base_seq
        handle.delivered = handle.seq
        raw = "".join(parts)

        approx_tokens = math.ceil(len(raw) / 4) if raw else 0
        requested = 10_000 if max_output_tokens is None else max(1, int(max_output_tokens))
        max_chars = min(self.config.limits.max_output_chars, requested * 4)
        output = raw
        if len(raw) > max_chars:
            head = max_chars * 3 // 5
            tail = max_chars - head
            omitted = len(raw) - head - tail
            output = (
                raw[:head]
                + f"\n... [output truncated: {omitted} characters omitted] ...\n"
                + raw[-tail:]
            )

        result: dict[str, Any] = {
            "wall_time_seconds": round(time.time() - handle.started, 3),
            "output": output,
            "original_token_count": approx_tokens,
            "job": handle.uid,
            "seq": handle.seq,
        }
        if lost:
            result["note"] = "older output exceeded the buffer and was dropped"
        if final and handle.proc.returncode is not None:
            result["exit_code"] = handle.proc.returncode
        return result

    def _shell_argv(self, shell: str, cmd: str, login: bool) -> list[str]:
        """The POSIX-style argv for ``shell`` (kept for callers; Windows uses _shell_launch)."""
        name = shells_mod.shell_name(shell)
        if name in {"bash", "sh", "zsh"}:
            return [shell, "-lc" if login else "-c", cmd]
        if name in {"pwsh", "powershell"}:
            args = [shell, "-NoLogo"]
            if not login:
                args.append("-NoProfile")
            return [*args, "-Command", cmd]
        if name == "cmd":
            return [shell, "/d", "/s", "/c", cmd]
        return [shell, "-c", cmd]

    def _shell_launch(self, info: shells_mod.ShellInfo, cmd: str, login: bool,
                      platform: str | None = None) -> dict[str, Any]:
        """How to start ``cmd`` in ``info`` without a second quoting layer.

        POSIX: execve has no command-line parsing, so argv is exact. Windows
        joins argv into one command line that each program re-parses:
        - bash/sh/zsh (Git Bash, MSYS2, Cygwin, WSL) re-parse it with their own
          quote and glob rules, so the command travels in an environment
          variable and a constant script evals it;
        - cmd.exe gets the command line exactly as a person types it;
        - PowerShell round-trips -Command through CommandLineToArgvW, as before.
        """
        platform = platform or sys.platform
        if platform != "win32":
            return {"argv": self._shell_argv(info.path, cmd, login)}
        flag = "-lc" if login else "-c"
        if info.family == "bash":
            wsl_env = {CMD_ENV: cmd, "WSLENV": f"{CMD_ENV}/u"}
            if info.wsl_distro:
                return {"argv": [info.path, "-d", info.wsl_distro, "-e", info.name, flag, _BASH_EVAL],
                        "env": wsl_env}
            if shells_mod.is_wsl_launcher(info.path):
                # A configured or explicit WSL launcher: the variable must cross
                # into Linux (WSLENV), else bash would eval an empty string.
                if shells_mod.shell_name(info.path) == "wsl":
                    return {"argv": [info.path, "-e", "bash", flag, _BASH_EVAL], "env": wsl_env}
                return {"argv": [info.path, flag, _BASH_EVAL], "env": wsl_env}
            return {"argv": [info.path, flag, _BASH_EVAL], "env": {CMD_ENV: cmd}}
        if info.name == "cmd":
            return {"cmdline": cmd_command_line(info.path, cmd)}
        return {"argv": self._shell_argv(info.path, cmd, login)}

    async def _reap_idle(self) -> None:
        now = time.time()
        cutoff = now - self.config.limits.idle_session_ttl_s
        finished_cutoff = now - self.config.limits.finished_session_ttl_s
        stale = [sid for sid, handle in self._handles.items()
                 if handle.last_used < cutoff
                 or (handle.finished is not None and handle.finished < finished_cutoff)]
        for sid in stale:
            handle = self._handles.get(sid)
            if handle is None:
                continue
            if handle.proc.returncode is None:
                try:
                    await self.kill(sid)
                except Exception:
                    self._handles.pop(sid, None)
            else:
                await self._finish_readers(handle)
                self._release(handle)
                self._close_pipes(handle.proc)

    @staticmethod
    def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
            else:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except Exception:
                    proc.terminate()
        except Exception:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    @staticmethod
    def _close_pipes(proc: asyncio.subprocess.Process) -> None:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
