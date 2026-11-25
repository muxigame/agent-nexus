"""This client operating on other devices of its gateway (private-network model, ``remote/1``).

The local console's 设备 page and ``anx term`` use this device's own gateway credential: every
joined device may list, revoke and delete the others and open their terminals, unless this
client is locked (:mod:`agent_nexus.lock`). The gateway checks the credential and the lock again.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlencode

from .federation import RoutingError


class RemoteError(RoutingError):
    def __init__(self, status: int, message: str, code: str = "remote_failed"):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def body(self) -> dict:
        return {"error": self.message, "code": self.code}


def credentials(root: Path) -> tuple[str, str, str]:
    """(gateway https origin + base path, this device's id, its token) of a joined root."""
    from .agent import connection_settings
    from .join import cloud_config, hub_url, joined

    if not joined(root):
        raise RemoteError(409, "这台设备还没有加入网关", "not_joined")
    try:
        agent_id, url, token = connection_settings(cloud_config(root), root)
    except Exception as exc:  # ConfigError and friends: name the problem, never the token
        raise RemoteError(409, f"读取本机的网关凭据失败：{exc}", "no_credential") from None
    return hub_url(url) or url, agent_id, token


def headers(agent_id: str, token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-ANX-Device": agent_id}


def _gateway_error(response) -> RemoteError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    message = body.get("error") if isinstance(body, dict) else None
    code = body.get("code", "gateway_error") if isinstance(body, dict) else "gateway_error"
    if response.status_code == 401:
        return RemoteError(502, "网关不接受这台设备的凭据（网关需要 0.4.0 以上，或这台设备已被吊销）", "unauthorized")
    return RemoteError(response.status_code if response.status_code in (404, 409, 423) else 502,
                       message or f"网关返回 HTTP {response.status_code}", code)


async def terminal_info(root: Path, device_id: str, *, refresh: bool = False) -> dict:
    import httpx

    origin, agent_id, token = credentials(root)
    url = f"{origin}/api/devices/{quote(device_id, safe='')}/terminal" + ("?refresh=1" if refresh else "")
    async with httpx.AsyncClient(timeout=60, trust_env=False) as http:
        try:
            response = await http.get(url, headers=headers(agent_id, token))
        except httpx.HTTPError:
            raise RemoteError(502, "无法连接网关", "gateway_unreachable") from None
    if response.status_code != 200:
        raise _gateway_error(response)
    return response.json()


def terminal_socket_url(origin: str, device_id: str, query: dict) -> str:
    scheme, _, rest = origin.partition("://")
    ws = "wss" if scheme == "https" else "ws"
    return f"{ws}://{rest}/api/devices/{quote(device_id, safe='')}/terminal/ws?{urlencode(query)}"


async def proxy_terminal(local, root: Path, device_id: str, query: dict, relay_note: str = "网关中转") -> None:
    """Pump a local console WebSocket (starlette) to the gateway's terminal socket and back."""
    from starlette.websockets import WebSocketDisconnect
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus

    origin, agent_id, token = credentials(root)
    url = terminal_socket_url(origin, device_id, {k: v for k, v in query.items() if k in ("shell", "cols", "rows")})
    try:
        upstream_cm = connect(url, additional_headers=headers(agent_id, token), max_size=8 * 1024 * 1024,
                              proxy=None, open_timeout=20, ping_interval=20)
        upstream = await upstream_cm.__aenter__()
    except (OSError, InvalidStatus, asyncio.TimeoutError) as exc:
        await local.send_text(json.dumps({"type": "error", "error": f"无法连接网关的终端通道（{type(exc).__name__}）"},
                                         ensure_ascii=False))
        await local.close(code=4404)
        return

    async def down():
        async for message in upstream:
            if isinstance(message, bytes):
                await local.send_bytes(message)
            else:
                await local.send_text(message)

    async def up():
        while True:
            message = await local.receive()
            if message.get("type") == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                await upstream.send(message["bytes"])
            elif message.get("text") is not None:
                await upstream.send(message["text"])

    await local.send_text(json.dumps({"type": "route", "route": relay_note, "p2p": False}, ensure_ascii=False))
    tasks = [asyncio.create_task(down()), asyncio.create_task(up())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        with contextlib.suppress(Exception):
            await upstream_cm.__aexit__(None, None, None)
        with contextlib.suppress(Exception, ConnectionClosed, WebSocketDisconnect):
            await local.close()


async def p2p_terminal(local, port: int, ticket: str, query: dict, route: str) -> None:
    """Bridge a local console WebSocket to a terminal on the other device over the P2P tunnel.

    The browser side speaks the same messages as the relay (binary output, JSON ready / exit /
    error, JSON resize and input); the tunnel side speaks remote_hub frames."""
    from starlette.websockets import WebSocketDisconnect

    from . import remote_hub as rh

    def number(name, default):
        try:
            return int(query.get(name) or default)
        except (TypeError, ValueError):
            return default
    reader, writer = await rh.as_path(port).open("terminal", ticket, shell=str(query.get("shell") or ""),
                                                 cols=number("cols", 120), rows=number("rows", 32))
    try:
        await local.send_text(json.dumps({"type": "route", "route": route, "p2p": True}, ensure_ascii=False))

        async def down():
            while True:
                kind, payload = await rh.read_frame(reader)
                if kind == rh.DATA:
                    await local.send_bytes(payload)
                elif kind == rh.READY:
                    await local.send_text(json.dumps({"type": "ready", **json.loads(payload)}, ensure_ascii=False))
                elif kind == rh.EXIT:
                    await local.send_text(json.dumps({"type": "exit", **json.loads(payload)}))
                    return
                elif kind == rh.ERROR:
                    await local.send_text(json.dumps({"type": "error", **json.loads(payload)}, ensure_ascii=False))
                    return

        async def up():
            while True:
                message = await local.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    writer.write(rh.frame(rh.DATA, message["bytes"]))
                elif message.get("text") is not None:
                    try:
                        payload = json.loads(message["text"])
                    except ValueError:
                        continue
                    if payload.get("type") == "resize":
                        writer.write(rh.frame(rh.RESIZE, {"cols": payload.get("cols"), "rows": payload.get("rows")}))
                    elif payload.get("type") == "input" and isinstance(payload.get("data"), str):
                        writer.write(rh.frame(rh.DATA, payload["data"].encode("utf-8")))
                await writer.drain()

        tasks = [asyncio.create_task(down()), asyncio.create_task(up())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task
    except (asyncio.IncompleteReadError, ConnectionError) as exc:
        with contextlib.suppress(Exception):
            await local.send_text(json.dumps({"type": "error", "error": f"直连中断（{type(exc).__name__}）"}, ensure_ascii=False))
    finally:
        with contextlib.suppress(Exception):
            writer.close()
        with contextlib.suppress(Exception, WebSocketDisconnect):
            await local.close()


async def p2p_desktop(local, port: int, ticket: str) -> None:
    """Bridge noVNC (a local WebSocket carrying raw RFB) to the other device's VNC over the tunnel."""
    from starlette.websockets import WebSocketDisconnect

    from . import remote_hub as rh

    reader, writer = await rh.as_path(port).open("desktop", ticket)
    try:
        kind, payload = await asyncio.wait_for(rh.read_frame(reader), 20)
        if kind != rh.READY:
            reason = json.loads(payload).get("error", "remote desktop refused") if kind == rh.ERROR else "remote desktop refused"
            await local.close(code=4404, reason=str(reason)[:120])
            return

        async def down():
            while True:
                data = await reader.read(256 * 1024)
                if not data:
                    return
                await local.send_bytes(data)

        async def up():
            while True:
                message = await local.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    writer.write(message["bytes"])
                    await writer.drain()

        tasks = [asyncio.create_task(down()), asyncio.create_task(up())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task
    except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()
        with contextlib.suppress(Exception, WebSocketDisconnect):
            await local.close()


# --------------------------------------------------------------------------- 在本机终端中打开

def _console_python() -> str:
    """The runtime's python.exe, not pythonw.exe: the new window needs a console."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        return str(exe.parent / "python.exe")
    return str(exe)


def external_command(root: Path, device_id: str, shell: str | None = None) -> list[str]:
    command = [_console_python(), "-m", "agent_nexus.cli", "--root", str(root), "term", device_id]
    if shell:
        command += ["--shell", shell]
    return command


def open_external(root: Path, device_id: str, *, shell: str | None = None, title: str | None = None) -> dict:
    """Open ``anx term <device>`` in this computer's own terminal: Windows Terminal when installed."""
    command = external_command(root, device_id, shell)
    env = {**os.environ, "PYTHONUTF8": "1"}
    env.pop("VIRTUAL_ENV", None)
    title = (title or device_id)[:80]
    if sys.platform == "win32":
        wt = shutil.which("wt.exe") or shutil.which("wt")
        if wt:
            subprocess.Popen([wt, "new-tab", "--title", title, "--", *command], env=env, close_fds=True)
            return {"opened": "windows-terminal"}
        subprocess.Popen(command, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True)
        return {"opened": "console"}
    if sys.platform == "darwin":
        script = " ".join(_sh_quote(part) for part in command)
        subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{script}"'], env=env)
        return {"opened": "terminal-app"}
    for terminal, prefix in (("x-terminal-emulator", ["-e"]), ("gnome-terminal", ["--"]), ("konsole", ["-e"]),
                             ("xterm", ["-e"])):
        path = shutil.which(terminal)
        if path:
            subprocess.Popen([path, *prefix, *command], env=env, start_new_session=True)
            return {"opened": terminal}
    raise RemoteError(409, "这台电脑上没有找到终端程序；在命令行里运行 anx term " + device_id, "no_terminal")


def _sh_quote(text: str) -> str:
    import shlex
    return shlex.quote(text).replace('"', '\\"')
