"""Remote terminal on the device (reverse RPC ``term_*``, feature ``term/1``).

The gateway opens a real pseudo-terminal here -- ConPTY through pywinpty on Windows, a pty
elsewhere -- running one of the shells this device actually has, and moves bytes with four
calls: ``term_open``, ``term_write``, ``term_read`` (a long poll) and ``term_resize`` /
``term_close``. ``term_info`` lists the shells and the command-line tools found on PATH so the
console can offer the right ones.

Nothing here is reachable except through the device's authenticated gateway link, and the
gateway only forwards it for the signed-in owner, who can already run commands on the
device through the MCP tools. ``terminal.enabled: false`` in the manager config turns it off.
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

FEATURE = "term/1"
MAX_SESSIONS = 8
#: a session nobody has read from for this long is closed (the browser went away)
IDLE_TTL_S = 15 * 60
MAX_READ_BYTES = 256 * 1024
#: output kept while nobody reads; older output is dropped (like a terminal's scrollback)
MAX_BUFFER_BYTES = 2 * 1024 * 1024
MAX_WAIT_S = 25.0
#: the CLI list a terminal page gets is rescanned behind it when older than this (a tool
#: installed from the page -- 安装 Codex -- shows up on the next visit)
CLI_RESCAN_S = 120.0
WINDOWS = sys.platform == "win32"


class TermError(ValueError):
    pass


# --------------------------------------------------------------------------- discovery

@dataclass
class Shell:
    id: str
    name: str
    argv: list[str]
    default: bool = False

    def view(self) -> dict:
        return {"id": self.id, "name": self.name, "command": " ".join(self.argv), "default": self.default}


def _wsl_distros() -> list[str]:
    try:
        out = subprocess.run(["wsl.exe", "-l", "-q"], capture_output=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    text = out.decode("utf-16-le", "ignore") if b"\x00" in out else out.decode("utf-8", "ignore")
    names = []
    for line in text.replace("\x00", "").splitlines():
        name = line.strip()
        if name and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name) and name not in names:
            names.append(name)
    return names[:8]


def _git_bash() -> str | None:
    candidates = []
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent  # ...\Git\cmd\git.exe -> ...\Git
        candidates.append(root / "bin" / "bash.exe")
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), os.environ.get("LOCALAPPDATA")):
        if base:
            candidates += [Path(base) / "Git" / "bin" / "bash.exe", Path(base) / "Programs" / "Git" / "bin" / "bash.exe"]
    return next((str(path) for path in candidates if path.is_file()), None)


def _current_user() -> str:
    import getpass
    try:
        return getpass.getuser()
    except Exception:
        return ""


def detect_shells(preferred: str | None = None) -> list[Shell]:
    shells: list[Shell] = []
    if WINDOWS:
        pwsh = shutil.which("pwsh")
        if pwsh:
            shells.append(Shell("pwsh", "PowerShell 7", [pwsh, "-NoLogo"]))
        powershell = shutil.which("powershell")
        if powershell:
            shells.append(Shell("powershell", "Windows PowerShell", [powershell, "-NoLogo"]))
        shells.append(Shell("cmd", "命令提示符", [os.environ.get("ComSpec") or "cmd.exe"]))
        bash = _git_bash()
        if bash:
            shells.append(Shell("git-bash", "Git Bash", [bash, "--login", "-i"]))
        if shutil.which("wsl.exe"):
            for distro in _wsl_distros():
                shells.append(Shell(f"wsl-{distro}", f"WSL · {distro}", ["wsl.exe", "-d", distro]))
    else:
        seen = set()
        preferred = preferred or os.environ.get("SHELL") or ""
        paths = [preferred] if preferred else []
        try:
            paths += [line.strip() for line in Path("/etc/shells").read_text().splitlines()
                      if line.strip().startswith("/")]
        except OSError:
            pass
        paths += ["/bin/bash", "/bin/zsh", "/usr/bin/fish", "/bin/sh"]
        names = {"bash": "Bash", "zsh": "Zsh", "fish": "Fish", "sh": "sh", "dash": "dash", "ksh": "ksh", "tcsh": "tcsh"}
        for path in paths:
            base = os.path.basename(path)
            if base in seen or base not in names or not os.access(path, os.X_OK):
                continue
            if base in ("sh", "dash") and seen:
                continue  # only offer plain sh when nothing better exists
            seen.add(base)
            shells.append(Shell(base, names[base], [path, "-l"] if base in ("bash", "zsh", "fish", "ksh") else [path]))
    if shells:
        shells[0].default = True
    return shells


#: (id, label, executables to look for, version arguments)
CLI_CATALOG: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("claude", "Claude Code", ("claude",), ("--version",)),
    ("codex", "Codex CLI", ("codex",), ("--version",)),
    ("gemini", "Gemini CLI", ("gemini",), ("--version",)),
    ("git", "Git", ("git",), ("--version",)),
    ("gh", "GitHub CLI", ("gh",), ("--version",)),
    ("node", "Node.js", ("node",), ("--version",)),
    ("npm", "npm", ("npm",), ("--version",)),
    ("pnpm", "pnpm", ("pnpm",), ("--version",)),
    ("bun", "Bun", ("bun",), ("--version",)),
    ("deno", "Deno", ("deno",), ("--version",)),
    ("python", "Python", ("python3", "python"), ("--version",)),
    ("uv", "uv", ("uv",), ("--version",)),
    ("conda", "Conda", ("conda",), ("--version",)),
    ("go", "Go", ("go",), ("version",)),
    ("cargo", "Rust (cargo)", ("cargo",), ("--version",)),
    ("dotnet", ".NET", ("dotnet",), ("--version",)),
    ("java", "Java", ("java",), ("-version",)),
    ("docker", "Docker", ("docker",), ("--version",)),
    ("kubectl", "kubectl", ("kubectl",), ("version", "--client")),
    ("helm", "Helm", ("helm",), ("version", "--short")),
    ("terraform", "Terraform", ("terraform",), ("--version",)),
    ("aliyun", "阿里云 CLI", ("aliyun",), ("version",)),
    ("az", "Azure CLI", ("az",), ("--version",)),
    ("aws", "AWS CLI", ("aws",), ("--version",)),
    ("gcloud", "Google Cloud CLI", ("gcloud",), ("--version",)),
    ("code", "VS Code", ("code",), ("--version",)),
    ("ffmpeg", "FFmpeg", ("ffmpeg",), ("-version",)),
    ("make", "make", ("make",), ("--version",)),
    ("cmake", "CMake", ("cmake",), ("--version",)),
    ("gcc", "GCC", ("gcc",), ("--version",)),
    ("clang", "Clang", ("clang",), ("--version",)),
    ("ssh", "OpenSSH", ("ssh",), ("-V",)),
    ("tmux", "tmux", ("tmux",), ("-V",)),
    ("nvim", "Neovim", ("nvim",), ("--version",)),
    ("vim", "Vim", ("vim",), ("--version",)),
    ("adb", "Android adb", ("adb",), ("version",)),
    ("flutter", "Flutter", ("flutter",), ("--version",)),
)
_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?(?:[-+._][0-9A-Za-z.]+)?")


def _version_of(path: str, args: tuple[str, ...]) -> str | None:
    argv = [path, *args]
    if WINDOWS and path.lower().endswith((".cmd", ".bat")):
        argv = [os.environ.get("ComSpec") or "cmd.exe", "/d", "/c", *argv]
    try:
        done = subprocess.run(argv, capture_output=True, timeout=6, stdin=subprocess.DEVNULL,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    text = (done.stdout + b"\n" + done.stderr).decode("utf-8", "replace")
    for line in text.splitlines():
        match = _VERSION.search(line)
        if match:
            return match.group(0)[:40]
    return None


def detect_clis() -> list[dict]:
    from . import agent_cli
    agents = agent_cli.runtimes(fresh=True)  # Claude Code / Codex also outside this process's PATH
    found = []
    for cli_id, label, names, args in CLI_CATALOG:
        agent = agents.get(cli_id) or {}
        if agent.get("available"):
            # ``command``: what to type -- the full path when it is on no PATH (the desktop app's codex)
            command = cli_id if agent.get("on_path") else agent["path"]
            found.append((cli_id, label + ("（ChatGPT / Codex 应用自带）" if agent.get("source") == "app" else ""),
                          agent["path"], args, command, agent.get("signed_in", "")))
            continue
        path = next((p for p in (shutil.which(name) for name in names) if p), None)
        if path:
            found.append((cli_id, label, path, args, cli_id, ""))
    results: list[dict | None] = [None] * len(found)

    def probe(index: int) -> None:
        cli_id, label, path, args, command, signed_in = found[index]
        results[index] = {"id": cli_id, "name": label, "path": path, "command": command,
                          "version": _version_of(path, args)}
        if signed_in != "":  # codex: True / False / None (cannot tell)
            results[index]["signed_in"] = signed_in

    threads = [threading.Thread(target=probe, args=(i,), daemon=True) for i in range(len(found))]
    for start in range(0, len(threads), 8):
        batch = threads[start:start + 8]
        for thread in batch:
            thread.start()
        for thread in batch:
            thread.join(10)
    return [item for item in results if item is not None]


# --------------------------------------------------------------------------- sessions

class _Pty:
    """One pseudo-terminal running ``argv``. ``on_data`` gets output bytes on the event loop."""

    def __init__(self, argv: list[str], cols: int, rows: int, cwd: str | None, env: dict, loop, on_data, on_exit,
                 run_as: dict | None = None):
        self.loop, self.on_data, self.on_exit = loop, on_data, on_exit
        self.exit_code: int | None = None
        if WINDOWS:
            if run_as and run_as.get("session") is not None:
                # The system service: the shell runs as the user at the screen (winsession.ConPty).
                from .winsession import ConPty
                self.proc = ConPty(run_as["session"], argv, cwd, env, cols, rows)
            else:
                from winpty import PtyProcess  # pywinpty; ConPTY on Windows 10 1809+

                self.proc = PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(rows, cols))
            threading.Thread(target=self._read_windows, daemon=True).start()
        else:
            import fcntl
            import pty
            import termios

            master, slave = pty.openpty()
            self._winsize(slave, cols, rows)

            def controlling_tty():  # runs after setsid (start_new_session)
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)

            if run_as:  # the account must own its terminal, or the shell cannot set its modes
                os.fchown(slave, run_as["user"], -1)
            self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
                                         start_new_session=True, preexec_fn=controlling_tty, close_fds=True,
                                         **(run_as or {}))
            os.close(slave)
            self.fd = master
            os.set_blocking(master, False)
            loop.add_reader(master, self._read_posix)

    @staticmethod
    def _winsize(fd: int, cols: int, rows: int) -> None:
        import fcntl
        import struct
        import termios

        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def _read_windows(self) -> None:
        while True:
            try:
                data = self.proc.read(65536)
            except EOFError:
                break
            except Exception:
                break
            if data:
                self.loop.call_soon_threadsafe(self.on_data, data if isinstance(data, bytes)
                                               else data.encode("utf-8", "replace"))
        try:
            self.proc.wait()
        except Exception:
            pass
        code = self.proc.exitstatus
        self.loop.call_soon_threadsafe(self.on_exit, code if isinstance(code, int) else None)

    def _read_posix(self) -> None:
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if data:
            self.on_data(data)
            return
        self.loop.remove_reader(self.fd)
        code = self.proc.poll()
        if code is None:
            try:
                code = self.proc.wait(2)
            except subprocess.TimeoutExpired:
                code = None
        self.on_exit(code)

    def write(self, data: bytes) -> None:
        if WINDOWS:
            from .winsession import ConPty
            self.proc.write(data if isinstance(self.proc, ConPty) else data.decode("utf-8", "replace"))
        else:
            view = memoryview(data)
            deadline = time.monotonic() + 5
            while view:
                try:
                    view = view[os.write(self.fd, view):]
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise TermError("terminal input is not being consumed")
                    time.sleep(0.01)

    def resize(self, cols: int, rows: int) -> None:
        if WINDOWS:
            self.proc.setwinsize(rows, cols)
        else:
            self._winsize(self.fd, cols, rows)

    def close(self) -> None:
        if WINDOWS:
            try:
                self.proc.terminate(force=True)
            except Exception:
                pass
            return
        import signal

        try:
            self.loop.remove_reader(self.fd)
        except Exception:
            pass
        try:
            os.killpg(self.proc.pid, signal.SIGHUP)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            self.proc.wait(2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                pass


@dataclass
class _Session:
    sid: str
    shell: Shell
    pty: Any = None
    buffer: bytearray = field(default_factory=bytearray)
    dropped: int = 0
    exited: bool = False
    exit_code: int | None = None
    touched: float = field(default_factory=time.monotonic)
    event: asyncio.Event = field(default_factory=asyncio.Event)


def available() -> bool:
    if WINDOWS:
        try:
            import winpty  # noqa: F401
        except ImportError:
            return False
        return True
    try:
        import pty  # noqa: F401
    except ImportError:
        return False
    return True


def _dimension(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


class Terminals:
    def __init__(self, *, home: str | None = None, account=None):
        self.sessions: dict[str, _Session] = {}
        self.home = home or str(Path.home())
        #: () -> accounts.Account | None: the 执行账户 for terminals (as root on Linux)
        self.account = account or (lambda: None)
        self._shells: list[Shell] | None = None
        self._clis: tuple[float, list[dict]] | None = None
        #: the CLI scan running in the background (it runs ~30 ``--version`` probes: seconds)
        self._clis_task: asyncio.Task | None = None

    # -- discovery
    async def shells(self) -> list[Shell]:
        account = self.account()
        key = account.name if account is not None else None
        if self._shells is None or getattr(self, "_shells_for", None) != key:
            self._shells = await asyncio.to_thread(detect_shells, account.shell if account is not None else None)
            self._shells_for = key
        return self._shells

    def _scan_clis(self) -> asyncio.Task:
        if self._clis_task is None or self._clis_task.done():
            async def scan() -> None:
                self._clis = (time.monotonic(), await asyncio.to_thread(detect_clis))
            self._clis_task = asyncio.ensure_future(scan())
        return self._clis_task

    async def warm(self) -> None:
        """Find the shells and scan the CLIs before anyone opens a terminal (on connecting)."""
        await self.shells()
        if self._clis is None:
            self._scan_clis()

    async def info(self, params: dict) -> dict:
        """Shells and CLIs at once: the CLI list is the last scan (empty until the first one is
        done) and a stale one is rescanned in the background. The scan probes ~30 tools'
        ``--version``, seconds on Windows (az, gcloud, java): opening a terminal waited for it.
        ``refresh`` waits for a fresh scan (the console's 刷新)."""
        refresh = bool(params.get("refresh"))
        if refresh:
            self._shells = None
        shells = await self.shells()
        if refresh:
            await self._scan_clis()
        elif self._clis is None or time.monotonic() - self._clis[0] > CLI_RESCAN_S:
            self._scan_clis()
        account = self.account()
        return {"shells": [shell.view() for shell in shells], "clis": self._clis[1] if self._clis else [],
                "clis_scanning": bool(self._clis_task and not self._clis_task.done()),
                "platform": sys.platform, "sessions": len(self.sessions), "max_sessions": MAX_SESSIONS,
                "account": account.name if account is not None else _current_user()}

    # -- lifecycle
    def _reap(self) -> None:
        now = time.monotonic()
        for session in list(self.sessions.values()):
            if now - session.touched > IDLE_TTL_S:
                self._close(session)

    def _close(self, session: _Session) -> None:
        self.sessions.pop(session.sid, None)
        if session.pty is not None and not session.exited:
            session.pty.close()
        session.exited = True
        session.event.set()

    def _session(self, params: dict) -> _Session:
        session = self.sessions.get(str(params.get("sid") or ""))
        if session is None:
            raise TermError("no such terminal session (it may have ended)")
        session.touched = time.monotonic()
        return session

    async def open(self, params: dict) -> dict:
        self._reap()
        if len(self.sessions) >= MAX_SESSIONS:
            raise TermError(f"this device already has {MAX_SESSIONS} terminals open")
        shells = await self.shells()
        if not shells:
            raise TermError("no shell found on this device")
        wanted = str(params.get("shell") or "")
        shell = next((s for s in shells if s.id == wanted), None) if wanted else next(s for s in shells if s.default)
        if shell is None:
            raise TermError(f"unknown shell {wanted!r}")
        cols = _dimension(params.get("cols"), 120, 20, 500)
        rows = _dimension(params.get("rows"), 32, 5, 200)
        account = self.account()
        home = account.home if account is not None else self.home
        cwd = params.get("cwd")
        cwd = cwd if isinstance(cwd, str) and cwd and os.path.isdir(cwd) else home
        env = account.env() if account is not None else dict(os.environ)
        env.update(TERM="xterm-256color", COLORTERM="truecolor", AGENT_NEXUS_TERMINAL="1")
        for key in ("VIRTUAL_ENV", "ANX_ROOT", "MCPM_ROOT", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_PYTHON_PREFERENCE",
                    "ANX_SYSTEM_SERVICE", "ANX_GUI_EXE"):
            env.pop(key, None)
        run_as = account.popen_args() if account is not None else None
        session = _Session(sid=uuid4().hex, shell=shell)
        loop = asyncio.get_running_loop()

        def on_data(data: bytes) -> None:
            session.buffer += data
            if len(session.buffer) > MAX_BUFFER_BYTES:
                cut = len(session.buffer) - MAX_BUFFER_BYTES
                del session.buffer[:cut]
                session.dropped += cut
            session.event.set()

        def on_exit(code: int | None) -> None:
            session.exited = True
            session.exit_code = code
            session.event.set()

        try:
            session.pty = await asyncio.to_thread(_Pty, shell.argv, cols, rows, cwd, env, loop, on_data, on_exit,
                                                  run_as) \
                if WINDOWS else _Pty(shell.argv, cols, rows, cwd, env, loop, on_data, on_exit, run_as)
        except (OSError, ImportError) as exc:
            raise TermError(f"could not start {shell.name}: {exc}") from exc
        self.sessions[session.sid] = session
        return {"sid": session.sid, "shell": shell.view(), "cols": cols, "rows": rows, "cwd": cwd}

    async def write(self, params: dict) -> dict:
        session = self._session(params)
        if session.exited:
            return {"ok": False, "exited": True}
        try:
            data = base64.b64decode(str(params.get("data") or ""), validate=True)
        except ValueError as exc:
            raise TermError("data must be base64") from exc
        if len(data) > 64 * 1024:
            raise TermError("input chunk too large")
        if data:
            await asyncio.to_thread(session.pty.write, data)
        return {"ok": True}

    async def read(self, params: dict) -> dict:
        session = self._session(params)
        wait = max(0.0, min(float(params.get("wait") or 0), MAX_WAIT_S))
        if not session.buffer and not session.exited and wait > 0:
            session.event.clear()
            try:
                await asyncio.wait_for(session.event.wait(), wait)
            except asyncio.TimeoutError:
                pass
        chunk = bytes(session.buffer[:MAX_READ_BYTES])
        del session.buffer[:len(chunk)]
        dropped, session.dropped = session.dropped, 0
        result: dict = {"data": base64.b64encode(chunk).decode("ascii"), "dropped": dropped}
        if session.exited and not session.buffer:
            result.update(exited=True, code=session.exit_code)
            self.sessions.pop(session.sid, None)
        return result

    async def resize(self, params: dict) -> dict:
        session = self._session(params)
        if not session.exited:
            cols = _dimension(params.get("cols"), 120, 20, 500)
            rows = _dimension(params.get("rows"), 32, 5, 200)
            await asyncio.to_thread(session.pty.resize, cols, rows)
        return {"ok": True}

    async def close(self, params: dict) -> dict:
        session = self.sessions.get(str(params.get("sid") or ""))
        if session is not None:
            await asyncio.to_thread(self._close, session) if WINDOWS else self._close(session)
        return {"ok": True}

    def close_all(self) -> None:
        for session in list(self.sessions.values()):
            self._close(session)

    def handlers(self) -> dict:
        def wrap(fn):
            async def handler(params: dict) -> dict:
                try:
                    return await fn(params if isinstance(params, dict) else {})
                except TermError as exc:
                    return {"error": str(exc)}
            return handler

        return {"term_info": wrap(self.info), "term_open": wrap(self.open), "term_write": wrap(self.write),
                "term_read": wrap(self.read), "term_resize": wrap(self.resize), "term_close": wrap(self.close)}
