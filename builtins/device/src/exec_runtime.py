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
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .settings import Config
from .policy import Policy

#: Longest a single exec_command or write_stdin call may block. The gateway
#: gives a device call 180 s (``rpc_timeout_s``); staying well under it means a
#: long wait returns a session_id instead of a gateway timeout.
MAX_WAIT_MS = 150_000
DEFAULT_EXEC_WAIT_MS = 10_000


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
        shell_path = shell or self.default_shell
        argv = self._shell_argv(shell_path, cmd, True if login is None else bool(login))

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        # Python children read and write files as UTF-8 too, not the ANSI code page.
        env["PYTHONUTF8"] = "1"
        env.update(self.config.env)
        env["TERM"] = "xterm-256color" if tty else "dumb"

        creationflags = 0
        if sys.platform == "win32":
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
        except OSError as exc:
            return {
                "wall_time_seconds": 0.0,
                "output": f"failed to start command: {exc}",
                "exit_code": None,
                "error": str(exc),
            }

        sid = await self._allocate_session_id()
        now = time.time()
        handle = _ExecHandle(sid, proc, cmd, cwd, now, now)
        handle.reader_tasks = [
            asyncio.create_task(self._read_stream(handle, proc.stdout)),
            asyncio.create_task(self._read_stream(handle, proc.stderr)),
        ]
        handle.completion_task = asyncio.create_task(self._track_completion(handle))

        wait_ms = DEFAULT_EXEC_WAIT_MS if yield_time_ms is None else float(yield_time_ms)
        # A command that finishes earlier returns at once; the window only
        # bounds how long a slow one blocks before it gets a session_id.
        wait_ms = min(max(wait_ms, 250), MAX_WAIT_MS)

        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=wait_ms / 1000.0)
        except asyncio.TimeoutError:
            self._handles[sid] = handle
            result = self._snapshot(handle, max_output_tokens)
            result["session_id"] = sid
            result["pid"] = proc.pid
            if tty and not self.pty_available:
                result["note"] = "tty=true requested; this server currently uses pipes on Windows"
            return result

        await self._finish_readers(handle)
        result = self._snapshot(handle, max_output_tokens, final=True)
        result["exit_code"] = proc.returncode
        self._close_pipes(proc)
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
                return {
                    "session_id": int(session_id),
                    "exit_code": completed["exit_code"],
                    "output": "",
                    "note": "session finished earlier and its uncollected output was released",
                }
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
        wait_ms = min(max(wait_ms, 0), MAX_WAIT_MS)

        if proc.returncode is None and wait_ms > 0:
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=wait_ms / 1000.0)
            except asyncio.TimeoutError:
                pass

        if proc.returncode is None:
            result = self._snapshot(handle, max_output_tokens)
            result["session_id"] = int(session_id)
            return result

        await self._finish_readers(handle)
        result = self._snapshot(handle, max_output_tokens, final=True)
        result["exit_code"] = proc.returncode
        self._handles.pop(int(session_id), None)
        self._close_pipes(proc)
        return result

    async def kill(self, session_id: int) -> dict[str, Any]:
        handle = self._handles.get(int(session_id))
        if handle is None:
            raise KeyError(f"unknown exec session_id {session_id}")
        self._kill_tree(handle.proc)
        try:
            await asyncio.wait_for(handle.proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        await self._finish_readers(handle)
        result = self._snapshot(handle, None, final=True)
        result["exit_code"] = handle.proc.returncode
        result["note"] = "process tree terminated"
        self._handles.pop(int(session_id), None)
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
        return dict(completed) if completed is not None else None

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
                if not data:
                    return
        except (asyncio.CancelledError, ValueError):
            return

    async def _track_completion(self, handle: _ExecHandle) -> None:
        try:
            code = await handle.proc.wait()
        except asyncio.CancelledError:
            return
        handle.finished = time.time()
        self._completed[handle.session_id] = {
            "session_id": handle.session_id,
            "pid": handle.proc.pid,
            "cmd": handle.cmd,
            "workdir": str(handle.cwd),
            "completed": True,
            "running": False,
            "exit_code": code,
            "started": handle.started,
            "finished": time.time(),
        }
        while len(self._completed) > 100:
            self._completed.pop(next(iter(self._completed)))

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
        }
        if final and handle.proc.returncode is not None:
            result["exit_code"] = handle.proc.returncode
        return result

    def _shell_argv(self, shell: str, cmd: str, login: bool) -> list[str]:
        path = Path(shell)
        name = path.name.lower()
        if name.endswith(".exe"):
            name = name[:-4]
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
                self._handles.pop(sid, None)
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
