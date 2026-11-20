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
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)
FEATURE = "p2p/1"
EXE = "muxi-p2p-agent.exe" if sys.platform == "win32" else "muxi-p2p-agent"
CONNECT_TIMEOUT_S = 25.0
IDLE_S = 600.0
NO_WINDOW = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0)


def binary(root: Path | None = None) -> str | None:
    explicit = os.environ.get("ANX_MUXI_P2P")
    if explicit and Path(explicit).is_file():
        return explicit
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


class AgentSidecar:
    """Keeps ``muxi-p2p-agent`` running for this device while the gateway link is up."""

    def __init__(self, exe: str):
        self.exe = exe
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
        env = {**os.environ, "MUXI_TUNNEL_TOKEN": config["token"], "MUXI_TCP_PUNCH": "1"}
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

    @property
    def alive(self) -> bool:
        return not self.lost and self.process.returncode is None


class Connector:
    """``muxi-p2p-agent connect`` processes, one per target device."""

    def __init__(self, exe: str):
        self.exe = exe
        self.links: dict[str, _Link] = {}
        self.locks: dict[str, asyncio.Lock] = {}

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
        env = {**os.environ, "MUXI_TCP_PUNCH": "1",
               "MUXI_CONTROL_PLANE_HEADERS": "\n".join(f"{k}: {v}" for k, v in headers.items())}
        process = await asyncio.create_subprocess_exec(
            self.exe, "connect", "--control-plane", config["control_plane"], "--server-id", target,
            "--reflector", ",".join(config.get("reflectors") or []), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            creationflags=NO_WINDOW)
        assert process.stdout is not None
        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("打洞超时")
                raw = await asyncio.wait_for(process.stdout.readline(), remaining)
                if not raw:
                    raise RuntimeError("打洞进程退出")
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
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
            if not link.alive or now - link.used > IDLE_S:
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

    def forget(self, target: str) -> None:
        link = self.links.pop(target, None)
        if link is not None:
            self._kill(link)

    def close(self) -> None:
        for link in self.links.values():
            self._kill(link)
        self.links.clear()
