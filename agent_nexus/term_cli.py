"""``anx term [device]``: a remote terminal on another device, in this terminal.

On a device that joined a gateway it uses that device's own credential (the gateway is a
private network: every joined device may open the others' terminals), and refuses while this
client is locked (``anx`` 锁定). Elsewhere -- another gateway with ``--gateway`` -- it signs in
as the owner: ``ANX_OWNER_TOKEN``, a token saved earlier (``<state>/owner-tokens.json``, this
user only), or the owner password asked once (``POST /api/auth/token``, 30 days).

Works in any VT terminal: Windows Terminal, the Windows console (VT mode), GNOME Terminal,
an SSH session on a server without a desktop. ``Ctrl+]`` then ``q`` leaves without closing the
remote shell's input.
"""
from __future__ import annotations

import asyncio
import contextlib
import getpass
import json
import os
import shutil
import signal
import sys
import threading
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from . import paths

ESCAPE = b"\x1d"  # Ctrl+]
WINDOWS = sys.platform == "win32"


class TermCliError(RuntimeError):
    pass


# --------------------------------------------------------------------------- gateway + auth

def gateway_origin(root: Path, explicit: str | None) -> str:
    raw = explicit or os.environ.get("ANX_GATEWAY") or ""
    if not raw:
        from .join import cloud_config
        raw = str(cloud_config(root).get("url") or "")
    if not raw:
        raise TermCliError("no gateway: join one (`agent-nexus join <invite>`) or pass --gateway https://<gateway>")
    parts = urlsplit(raw if "://" in raw else f"https://{raw}")
    scheme = {"wss": "https", "ws": "http"}.get(parts.scheme, parts.scheme)
    if scheme not in ("https", "http") or not parts.hostname:
        raise TermCliError(f"not a gateway address: {raw}")
    if scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise TermCliError("a remote gateway must use https")
    return f"{scheme}://{parts.netloc}"


def _token_file(root: Path) -> Path:
    return root / paths.STATE_DIR_NAME / "owner-tokens.json"


def _saved_tokens(root: Path) -> dict:
    try:
        data = json.loads(_token_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_token(root: Path, origin: str, token: str | None) -> None:
    path = _token_file(root)
    data = _saved_tokens(root)
    if token:
        data[origin] = token
    else:
        data.pop(origin, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    temporary.replace(path)


def sign_in(origin: str) -> str:
    if not sys.stdin.isatty():
        raise TermCliError("not signed in: set ANX_OWNER_TOKEN or run this in an interactive terminal")
    password = getpass.getpass(f"{origin} 的 owner 密码: ")
    response = httpx.post(f"{origin}/api/auth/token", json={"password": password, "remember": True},
                          timeout=20, trust_env=False)
    if response.status_code == 429:
        raise TermCliError("尝试过于频繁，请 5 分钟后再试")
    if response.status_code != 200:
        raise TermCliError("密码不正确" if response.status_code == 401 else f"sign-in failed ({response.status_code})")
    return response.json()["access_token"]


class Gateway:
    def __init__(self, root: Path, origin: str, device: str | None = None, device_token: str | None = None):
        self.root, self.origin, self.device = root, origin, device
        self.token = device_token or os.environ.get("ANX_OWNER_TOKEN") or _saved_tokens(root).get(origin)

    @property
    def headers(self) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"}
        if self.device:
            headers["X-ANX-Device"] = self.device
        return headers

    def get(self, path: str) -> dict:
        for attempt in range(2):
            if not self.token:
                self.token = sign_in(self.origin)
                _save_token(self.root, self.origin, self.token)
            response = httpx.get(f"{self.origin}{path}", headers=self.headers, timeout=60, trust_env=False)
            if response.status_code == 401 and self.device:
                raise TermCliError("网关不接受这台设备的凭据（网关需要 0.4.0 以上，或这台设备已被吊销）")
            if response.status_code == 423:
                raise TermCliError("这台设备已锁定，解锁后才能操作其他设备")
            if response.status_code == 401 and attempt == 0 and not os.environ.get("ANX_OWNER_TOKEN"):
                _save_token(self.root, self.origin, None)
                self.token = None
                continue
            body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            if response.status_code != 200:
                raise TermCliError(body.get("error") or f"{path}: HTTP {response.status_code}")
            return body
        raise TermCliError("unauthorized")


# --------------------------------------------------------------------------- local terminal

class RawTerminal:
    """stdin in raw/VT-input mode, stdout with VT processing; restored on exit."""

    def __enter__(self):
        if WINDOWS:
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.windll.kernel32
            self._in, self._out = kernel.GetStdHandle(-10), kernel.GetStdHandle(-11)
            # bytes in and out are UTF-8 on the wire
            self._codepages = (kernel.GetConsoleCP(), kernel.GetConsoleOutputCP())
            kernel.SetConsoleCP(65001)
            kernel.SetConsoleOutputCP(65001)
            self._modes = []
            for handle, add, remove in ((self._in, 0x0200, 0x0001 | 0x0002 | 0x0004),  # VT input; no processed/line/echo
                                        (self._out, 0x0004 | 0x0001, 0)):              # VT processing
                mode = wintypes.DWORD()
                if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                    self._modes.append((handle, mode.value))
                    kernel.SetConsoleMode(handle, (mode.value | add) & ~remove)
        else:
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setraw(self._fd)
        return self

    def __exit__(self, *exc):
        if WINDOWS:
            import ctypes
            for handle, mode in self._modes:
                ctypes.windll.kernel32.SetConsoleMode(handle, mode)
            ctypes.windll.kernel32.SetConsoleCP(self._codepages[0])
            ctypes.windll.kernel32.SetConsoleOutputCP(self._codepages[1])
        else:
            import termios
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)


def _size() -> tuple[int, int]:
    size = shutil.get_terminal_size((120, 32))
    return size.columns, size.lines


async def run_session(origin: str, auth: dict, device_id: str, shell: str) -> int:
    from websockets.asyncio.client import connect

    cols, rows = _size()
    scheme = "wss" if origin.startswith("https") else "ws"
    url = f"{scheme}://{urlsplit(origin).netloc}/api/devices/{quote(device_id, safe='')}/terminal/ws?shell={quote(shell)}&cols={cols}&rows={rows}"
    loop = asyncio.get_running_loop()
    keys: asyncio.Queue[bytes] = asyncio.Queue()
    stop = threading.Event()

    def read_keys():
        while not stop.is_set():
            try:
                data = os.read(sys.stdin.fileno(), 4096)
            except OSError:
                data = b""
            if not data:
                loop.call_soon_threadsafe(keys.put_nowait, b"")
                return
            loop.call_soon_threadsafe(keys.put_nowait, data)

    exit_code = 0
    async with connect(url, additional_headers=auth, max_size=8 * 1024 * 1024,
                       proxy=None, open_timeout=20) as socket:
        out = sys.stdout.buffer

        async def downstream():
            nonlocal exit_code
            async for message in socket:
                if isinstance(message, bytes):
                    out.write(message)
                    out.flush()
                    continue
                payload = json.loads(message)
                if payload.get("type") == "exit":
                    code = payload.get("code")
                    exit_code = code if isinstance(code, int) else 0
                    return
                if payload.get("type") == "error":
                    raise TermCliError(payload.get("error") or "terminal failed")

        async def upstream():
            pending_escape = False
            while True:
                data = await keys.get()
                if not data:
                    return
                if pending_escape:
                    pending_escape = False
                    if data[:1] in (b"q", b"Q"):
                        return
                    data = ESCAPE + data
                elif data == ESCAPE:
                    pending_escape = True
                    continue
                await socket.send(data)

        async def resizes():
            last = (cols, rows)
            changed = asyncio.Event()
            if not WINDOWS:
                loop.add_signal_handler(signal.SIGWINCH, changed.set)
            while True:
                if WINDOWS:
                    await asyncio.sleep(0.5)
                else:
                    await changed.wait()
                    changed.clear()
                now = _size()
                if now != last:
                    last = now
                    await socket.send(json.dumps({"type": "resize", "cols": now[0], "rows": now[1]}))

        threading.Thread(target=read_keys, daemon=True).start()
        tasks = [asyncio.create_task(downstream()), asyncio.create_task(upstream()), asyncio.create_task(resizes())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task
    return exit_code


# --------------------------------------------------------------------------- command

def _pick_device(devices: list[dict], wanted: str | None) -> dict:
    usable = [d for d in devices if d.get("online") and d.get("terminal")]
    if wanted:
        match = next((d for d in devices if wanted in (d.get("device_id"), d.get("agent_id"), d.get("display_name"))), None)
        if match is None:
            raise TermCliError(f"no device {wanted!r}; `agent-nexus term --list` shows them")
        if not match.get("online"):
            raise TermCliError(f"{match.get('display_name')} is offline")
        if not match.get("terminal"):
            raise TermCliError(f"{match.get('display_name')} ({match.get('version') or 'old client'}) has no remote terminal; upgrade it to 0.3.0+")
        return match
    if not usable:
        raise TermCliError("no online device offers a remote terminal")
    if len(usable) == 1 or not sys.stdin.isatty():
        return usable[0]
    for index, device in enumerate(usable, 1):
        print(f"  {index}. {device.get('display_name')}  ({device.get('device_id')}, {device.get('os') or device.get('platform') or ''})")
    choice = input("设备编号: ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(usable):
        raise TermCliError("no device chosen")
    return usable[int(choice) - 1]


def cmd_term(args, root: Path) -> int:
    try:
        from .join import joined
        if not args.gateway and joined(root):
            from .lock import Lock
            from .remote import RemoteError, credentials
            if Lock(root).locked:
                raise TermCliError("这台设备已锁定，解锁后才能操作其他设备（本机控制台右上角的锁）")
            try:
                origin, agent_id, token = credentials(root)
            except RemoteError as exc:
                raise TermCliError(exc.message) from None
            gateway = Gateway(root, origin, agent_id, token)
        else:
            origin = gateway_origin(root, args.gateway)
            gateway = Gateway(root, origin)
        devices = gateway.get("/api/network/devices" if gateway.device else "/api/devices").get("devices", [])
        if args.list:
            for device in devices:
                state = "online" if device.get("online") else "offline"
                term = "terminal" if device.get("terminal") else "-"
                print(f"{device.get('device_id'):<32} {state:<8} {term:<9} {device.get('display_name')}")
            return 0
        device = _pick_device(devices, args.device)
        device_id = device["device_id"]
        info = gateway.get(f"/api/devices/{quote(device_id, safe='')}/terminal")
        if args.shells:
            for shell in info.get("shells", []):
                print(f"{shell['id']:<20} {shell['name']}{'  (default)' if shell.get('default') else ''}")
            print()
            for cli in info.get("clis", []):
                print(f"{cli['id']:<12} {cli.get('version') or '':<18} {cli['path']}")
            return 0
        shell = args.shell or ""
        if shell and shell not in {s["id"] for s in info.get("shells", [])}:
            raise TermCliError(f"{device.get('display_name')} has no shell {shell!r}; try --shells")
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise TermCliError("anx term needs an interactive terminal")
        print(f"\x1b[90m→ {device.get('display_name')} ({device_id}) · Ctrl+] q 退出\x1b[0m", flush=True)
        with RawTerminal():
            code = asyncio.run(run_session(origin, gateway.headers, device_id, shell))
        print(f"\r\n\x1b[90m[{device.get('display_name')}: 会话结束]\x1b[0m", flush=True)
        return code
    except TermCliError as exc:
        print(f"anx term: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"anx term: gateway unreachable ({type(exc).__name__})", file=sys.stderr)
        return 1


def add_parser(sub) -> None:
    p = sub.add_parser("term", help="open a remote terminal on another device of the gateway (owner sign-in)")
    p.add_argument("device", nargs="?", help="device id or display name (asks when omitted)")
    p.add_argument("--shell", help="shell id on that device (see --shells); default: the device's default shell")
    p.add_argument("--gateway", help="gateway address (default: the gateway this device joined)")
    p.add_argument("--list", action="store_true", help="list devices and whether they offer a terminal")
    p.add_argument("--shells", action="store_true", help="list the device's shells and command-line tools")
