"""Client side of the P2P tunnels (muxi-p2p, ``p2p/1``).

Every device runs ``muxi-p2p-agent`` as a sidecar: it registers with the gateway's signaling
(``<gateway>/p2p``) under the device's id and answers hole punching, carrying the streams it
accepts to this device's :class:`~agent_nexus.remote_hub.RemoteHub`.

A device that opens a terminal or desktop on another one starts ``muxi-p2p-agent connect``:
direct routes first (IPv6, LAN, a router mapping), then UDP + QUIC and TCP hole punching. It
listens on a loopback port; each connection to it is one stream to the other device. One
connect process per target device is kept while in use (and a few minutes after).

The binary comes from ``ANX_MUXI_P2P``, ``<root>/tools/``, or PATH. Without it everything
still works through the gateway relay, only slower.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)
FEATURE = "p2p/1"
EXE = "muxi-p2p-agent.exe" if sys.platform == "win32" else "muxi-p2p-agent"
CONNECT_TIMEOUT_S = 25.0
IDLE_S = 1800.0
NO_WINDOW = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0)


ANDROID_LIBS_ENV = "ANX_ANDROID_NATIVE_LIBS"   # the APK's nativeLibraryDir, set by the Android app
ANDROID_EXE = "libmuxip2p.so"                  # Android only runs lib*.so files extracted there
# .NET's OpenSSL shim on bionic dlopens these exact names (not .so.3; without them it gets the
# system's BoringSSL and aborts on a missing symbol). The app ships OpenSSL for Python under its
# own names, so they are linked in a writable folder put first on LD_LIBRARY_PATH.
ANDROID_SSL_LINKS = {
    "libssl.so": "libssl_python.so", "libcrypto.so": "libcrypto_python.so",
    "libssl.so.3": "libssl_python.so", "libcrypto.so.3": "libcrypto_python.so",
}


def binary(root: Path | None = None) -> str | None:
    explicit = os.environ.get("ANX_MUXI_P2P")
    if explicit and Path(explicit).is_file():
        return explicit
    native = os.environ.get(ANDROID_LIBS_ENV)
    if native:
        exe = Path(native) / ANDROID_EXE
        return str(exe) if exe.is_file() else None
    for base in ([Path(root) / "tools"] if root else []) + [Path(sys.prefix).parent / "tools", Path(__file__).resolve().parents[1] / "tools"]:
        if (base / EXE).is_file():
            return str(base / EXE)
    return shutil.which(EXE)


def _free_port(preferred: int) -> int:
    """``preferred`` when nothing listens there (a stable port helps router mappings), else any."""
    import socket
    for port in (preferred, 0):
        family = socket.AF_INET6 if socket.has_ipv6 else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("::" if family == socket.AF_INET6 else "0.0.0.0", port))
            except OSError:
                continue
            return probe.getsockname()[1]
    return preferred


def android_ssl_links(native: str, env: dict) -> str | None:
    """A writable folder holding :data:`ANDROID_SSL_LINKS` into ``native``; None if none could be made.

    Links are rewritten when they point elsewhere: an app update moves nativeLibraryDir.
    """
    import tempfile
    candidates = [env.get("ANX_ANDROID_LINK_DIR"), env.get("TMPDIR"), env.get("HOME"), tempfile.gettempdir()]
    for parent in filter(None, candidates):
        folder = Path(parent) / "anx-p2p-libs"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            for name, target in ANDROID_SSL_LINKS.items():
                link, source = folder / name, str(Path(native) / target)
                if link.is_symlink() and os.readlink(link) == source:
                    continue
                if link.is_symlink() or link.exists():
                    link.unlink()
                link.symlink_to(source)
            return str(folder)
        except OSError as error:
            log.debug("p2p: cannot link OpenSSL in %s: %s", folder, error)
    log.warning("p2p: no writable folder for the OpenSSL links; QUIC will fall back to TCP")
    return None


def child_env(exe: str, base: dict | None = None, *, platform: str | None = None) -> dict:
    """The environment for a muxi-p2p-agent process.

    On macOS the QUIC stack needs libmsquic and OpenSSL, shipped next to the binary. msquic is
    found there, but .NET's OpenSSL shim (statically linked into the single-file agent) looks for
    ``libssl.3.dylib`` by name, and macOS does not search the executable's folder for that: the
    client side aborted in InitializeOpenSSLShim right after a successful punch (seen on an
    arm64 Mac). With the folder on DYLD_LIBRARY_PATH the same build completes the QUIC handshake.

    On Android (``ANX_ANDROID_NATIVE_LIBS``) libmsquic.so and the app's OpenSSL sit in the APK's
    nativeLibraryDir; the OpenSSL links (:func:`android_ssl_links`) and that folder go first on
    LD_LIBRARY_PATH. Checked on a headset with ``libmuxip2p.so selftest``.
    """
    env = dict(os.environ if base is None else base)
    native = env.get(ANDROID_LIBS_ENV)
    if native:
        folders = [folder for folder in (android_ssl_links(native, env), native) if folder]
        current = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = ":".join(folders + ([current] if current else []))
        return env
    if (platform or sys.platform) == "darwin":
        from pathlib import PurePosixPath  # macOS paths (also when tested elsewhere); binary() gives it absolute
        folder = str(PurePosixPath(exe).parent)
        current = env.get("DYLD_LIBRARY_PATH", "")
        if folder not in current.split(":"):
            env["DYLD_LIBRARY_PATH"] = f"{folder}:{current}" if current else folder
    return env


class AgentSidecar:
    """Keeps ``muxi-p2p-agent`` running for this device while the gateway link is up."""

    def __init__(self, exe: str, log_file: Path | None = None):
        self.exe = exe
        #: the agent's own log (both halves of a punch are needed to debug it; its stdout only
        #: reaches our debug log)
        self.log_file = log_file
        self.process: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.config: dict | None = None
        self.target_port = 0
        self.status: dict = {"state": "stopped"}

    def apply(self, config: dict, target_port: int) -> None:
        """(Re)start with this gateway configuration; same configuration keeps the running agent."""
        if self.task is not None and not self.task.done() and config == self.config and target_port == self.target_port:
            return
        self.config, self.target_port = config, target_port
        if self.task is not None:
            self.task.cancel()
        self.task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        delay = 2.0
        warned: dict[str, float] = {}
        config = self.config or {}
        args = [self.exe, "--port", str(_free_port(int(config.get("tunnel_port") or 0) or 45740)),
                "--target", f"127.0.0.1:{self.target_port}",
                "--control-plane", config["control_plane"], "--server-id", config["server_id"],
                "--reflector", ",".join(config.get("reflectors") or [])]
        env = {**child_env(self.exe), "MUXI_TUNNEL_TOKEN": config["token"], "MUXI_TCP_PUNCH": "1"}
        if self.log_file is not None:
            env["MUXI_AGENT_LOG"] = str(self.log_file)
        while True:
            started = time.monotonic()
            try:
                self.process = await asyncio.create_subprocess_exec(
                    *args, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT, creationflags=NO_WINDOW)
                self.status = {"state": "running", "pid": self.process.pid, "since": time.time()}
                log.info("p2p agent started (pid %s)", self.process.pid)
                assert self.process.stdout is not None
                tail: list[str] = []
                async for raw in self.process.stdout:
                    line = raw.decode("utf-8", "replace").rstrip()
                    if line:
                        log.debug("p2p agent: %s", line)
                        self.status["last"] = line[-200:]
                        tail = (tail + [line[-300:]])[-12:]
                        # Failures to register make the device unreachable directly: say so in
                        # the manager log (once per 10 minutes per kind), not only in debug output.
                        if "上报失败" in line or "Unhandled exception" in line:
                            kind = line.split("：", 1)[-1][:60]
                            if time.monotonic() - warned.get(kind, -1e9) > 600:
                                warned[kind] = time.monotonic()
                                log.warning("p2p agent: %s", line[-300:])
                code = await self.process.wait()
                log.warning("p2p agent exited (%s); last output:\n  %s", code, "\n  ".join(tail))
                self.status = {"state": "exited", "code": code, "last": self.status.get("last")}
            except asyncio.CancelledError:
                await self.stop_process()
                raise
            except OSError as exc:
                log.warning("p2p agent could not start: %s", exc)
                self.status = {"state": "error", "error": str(exc)}
            delay = 2.0 if time.monotonic() - started > 60 else min(delay * 2, 120.0)
            await asyncio.sleep(delay)

    async def stop_process(self) -> None:
        process, self.process = self.process, None
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), 5)

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(BaseException):
                await self.task
        await self.stop_process()
        self.status = {"state": "stopped"}


class _Link:
    def __init__(self, process: asyncio.subprocess.Process, port: int, route: str):
        self.process, self.port, self.route = process, port, route
        self.used = time.monotonic()
        self.lost = False
        #: streams using the tunnel now (an open terminal or desktop): never idle meanwhile
        self.streams = 0

    @property
    def alive(self) -> bool:
        return not self.lost and self.process.returncode is None


def _safe_name(target: str) -> str:
    """A device id as a file name (ids are already tame; this only guards the path)."""
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in target)[:80] or "target"


class Connector:
    """``muxi-p2p-agent connect`` processes, one per target device."""

    def __init__(self, exe: str, log_dir: Path | None = None):
        self.exe = exe
        #: one file per target with the forwarder's own log of its last attempt (NAT type,
        #: decoys, port sweep, candidates): what a failed punch is diagnosed from.
        self.log_dir = log_dir
        self.links: dict[str, _Link] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        #: standing tunnels (relay mesh): never closed for being idle, only when dead
        self.pinned: set[str] = set()

    def pin(self, target: str) -> None:
        self.pinned.add(target)

    def unpin(self, target: str) -> None:
        self.pinned.discard(target)

    async def port_for(self, config: dict, target: str, headers: dict) -> tuple[int, str]:
        """A loopback port whose connections are streams to ``target``; raises OSError/RuntimeError."""
        lock = self.locks.setdefault(target, asyncio.Lock())
        async with lock:
            self.reap()
            link = self.links.get(target)
            if link is not None and link.alive:
                link.used = time.monotonic()
                return link.port, link.route
            link = await self._open(config, target, headers)
            self.links[target] = link
            return link.port, link.route

    async def _open(self, config: dict, target: str, headers: dict) -> _Link:
        env = {**child_env(self.exe), "MUXI_TCP_PUNCH": "1",
               "MUXI_CONTROL_PLANE_HEADERS": "\n".join(f"{k}: {v}" for k, v in headers.items())}
        # the forwarder's own log (MUXI_LOG_FILE), and its stderr next to it: a native abort
        # (e.g. a crypto library that does not load) writes there and nowhere else
        stderr = subprocess.DEVNULL
        if self.log_dir is not None:
            env["MUXI_LOG_FILE"] = str(self.log_dir / f"{_safe_name(target)}.log")
            with contextlib.suppress(OSError):
                self.log_dir.mkdir(parents=True, exist_ok=True)
                stderr = open(self.log_dir / f"{_safe_name(target)}.stderr.log", "wb")
        try:
            process = await asyncio.create_subprocess_exec(
                self.exe, "connect", "--control-plane", config["control_plane"], "--server-id", target,
                "--reflector", ",".join(config.get("reflectors") or []), env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=stderr,
                creationflags=NO_WINDOW)
        finally:
            if stderr is not subprocess.DEVNULL:
                stderr.close()  # the child has its own copy
        assert process.stdout is not None
        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        last = ""  # the forwarder's last progress line: where it was when it gave up
        try:
            while True:
                remaining = deadline - time.monotonic()
                try:
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    raw = await asyncio.wait_for(process.stdout.readline(), remaining)
                except asyncio.TimeoutError:
                    raise RuntimeError(f"打洞超时（{CONNECT_TIMEOUT_S:.0f} 秒）" + (f"，停在：{last}" if last else "")) from None
                if not raw:
                    raise RuntimeError("打洞进程退出" + (f"，最后：{last}" if last else ""))
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if event.get("event") == "progress" and event.get("text"):
                    last = str(event["text"])[:200]
                    log.info("p2p connect %s: %s", target, last)
                if event.get("event") == "ready":
                    link = _Link(process, int(event["port"]), str(event.get("route") or "P2P"))
                    asyncio.ensure_future(self._watch(link))
                    return link
                if event.get("event") == "error":
                    raise RuntimeError(str(event.get("error") or "打洞失败"))
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            raise

    async def _watch(self, link: _Link) -> None:
        assert link.process.stdout is not None
        with contextlib.suppress(Exception):
            async for raw in link.process.stdout:
                if b'"lost"' in raw:
                    link.lost = True
        link.lost = True

    def reap(self) -> None:
        now = time.monotonic()
        for target, link in list(self.links.items()):
            idle = now - link.used > IDLE_S and target not in self.pinned and link.streams <= 0
            if not link.alive or idle:
                self.links.pop(target, None)
                self._kill(link)

    @staticmethod
    def _kill(link: _Link) -> None:
        if link.process.returncode is None:
            with contextlib.suppress(Exception):
                if link.process.stdin is not None:
                    link.process.stdin.close()  # the forwarder exits when its stdin closes
            with contextlib.suppress(ProcessLookupError):
                link.process.terminate()

    @contextlib.contextmanager
    def hold(self, target: str):
        """A stream uses ``target``'s tunnel meanwhile: it is not closed for being idle (a terminal
        left open longer than IDLE_S was cut when any other tunnel was asked for)."""
        link = self.links.get(target)
        if link is not None:
            link.streams += 1
        try:
            yield
        finally:
            if link is not None:
                link.streams -= 1
                link.used = time.monotonic()

    def forget(self, target: str) -> None:
        link = self.links.pop(target, None)
        if link is not None:
            self._kill(link)

    def close(self) -> None:
        for link in self.links.values():
            self._kill(link)
        self.links.clear()
