"""``anx devices`` / ``anx exec`` / ``anx wait`` / ``anx kill`` / ``anx cp``: work on other devices from a shell.

Transport (docs/agent-chat.md, README 命令行): only this computer's running client, on loopback, with no credential.
Other devices are reached through the 本机 MCP (``/mcp``, the gateway's tools called as this device, so the lock,
revocation and the owner's device-MCP switch apply exactly as for the CLIs' own MCP connector); device tools go
through ``tooluse(mcp_id="<device_id>/device")`` (no handle, no mailbox per call). This computer itself (``.``) is
reached through ``/mcp/self``, the local device plugin, which works without the gateway. Bulk data (``anx cp``) is a
``file_copy`` job: the devices move the bytes themselves over direct P2P, a mesh relay or the gateway relay.

The exec commands are a loop over ``exec_command`` / ``exec_wait``: every call returns only new output, the wait
is capped at 50 s by the gateway, so the caller's ``--timeout`` (not the server's cap) decides how long anx waits.
Errors are Chinese on stderr; stdout carries only the remote output (or the session token with ``--detach``).
"""
from __future__ import annotations

import argparse
import errno
import json
import locale
import os
import re
import signal
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

PROG = "anx"
PROTOCOL = "2025-06-18"
GATEWAY_PATH = "/mcp"
SELF_PATH = "/mcp/self"
#: one call's output budget (the device cuts more in the middle: min(max_output_chars, tokens * 4))
MAX_OUTPUT_TOKENS = 15000
#: exec_wait windows while nothing arrives (ms); back to the first one when output came
WAIT_STEPS_MS = (1000, 2000, 5000, 10000)
WAIT_CAP_MS = 50_000
FIRST_YIELD_MS = 1000
DETACH_YIELD_MS = 250
#: how long a broken link to the client / gateway is retried during a wait before giving up
LOST_GRACE_S = 60.0
#: the first pause before such a retry (it grows to 5 s for a wait; a copy's status is asked every 2 s)
RETRY_S = 1.0
KILL_TIMEOUT_S = 10.0
CP_WAIT_MS = 5000
CP_PLAIN_EVERY_S = 10.0

EXIT_USAGE = 2
EXIT_TIMEOUT = 124
#: stdout closed by the reader (``anx exec nas -- journalctl -f | head``): the remote command is stopped
EXIT_PIPE = 1 if os.name == "nt" else 141
EXIT_INTERRUPTED = 130
EXIT_NO_CLIENT = 250
EXIT_UNAVAILABLE = 251
EXIT_DEVICE = 252
EXIT_SHELL = 253
EXIT_TOOL = 254
EXIT_PROTOCOL = 255
CP_EXIT = {"completed": 0, "completed_with_errors": 3, "failed": 4, "cancelled": 5, "interrupted": 6, "unknown": 6}
CP_ACTIVE = ("queued", "scanning", "copying", "finishing")
SHELLS = ("bash", "sh", "zsh", "pwsh", "powershell", "cmd")
#: messages of a wait that may pass (a device reconnecting, a slow relay)
_TRANSIENT = re.compile(r"offline|not connected|timed out|timeout|no response|不在线|未连接|中断|没有回应|超时",
                        re.IGNORECASE)
_HEAD_EXIT = re.compile(r"^\[exit (-?\d+)")
_HEAD_RUNNING = re.compile(r"^\[running · session (\d+)")

COMMANDS = ("devices", "exec", "wait", "kill", "cp")


class CliError(Exception):
    """A failure with its exit code; ``transient``: a wait may retry it."""

    def __init__(self, code: int, message: str, *, state: str | None = None, transient: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.state = state
        self.transient = transient


class ToolFailure(Exception):
    """A device or gateway tool answered with an error (policy, workdir, unknown session...)."""


class Usage(Exception):
    pass


# ------------------------------------------------------------------------------------------------ output

class Out:
    """A stream written from several threads; UTF-8 bytes when it is not a terminal (pipes, files, the chat)."""

    def __init__(self, stream: Any):
        self.stream = stream
        self.lock = threading.Lock()
        #: set when the reader went away (``| head`` exited): nothing more is written, the run stops
        self.broken = threading.Event()
        try:
            self.tty = bool(stream.isatty())
        except (AttributeError, ValueError):
            self.tty = False

    def write(self, text: str) -> None:
        if not text or self.broken.is_set():
            return
        with self.lock:
            raw = getattr(self.stream, "buffer", None)
            try:
                if raw is not None and not self.tty:
                    self.stream.flush()
                    raw.write(text.encode("utf-8", "replace"))
                    raw.flush()
                else:
                    self.stream.write(text)
                    self.stream.flush()
            except (BrokenPipeError, ValueError):
                self.broken.set()  # the reader went away (| head) or the stream is closed
            except OSError as exc:
                # Windows reports a closed pipe as EINVAL (or EPIPE); anything else is not ours to hide either
                if exc.errno not in (errno.EPIPE, errno.EINVAL):
                    raise
                self.broken.set()

    def silence(self) -> None:
        """After a broken pipe: point the real stdout at the null device, so the interpreter's own flush at
        exit prints no "Exception ignored ... BrokenPipeError"."""
        if not self.broken.is_set():
            return
        try:
            fd = self.stream.fileno()
        except (AttributeError, ValueError, OSError):
            return
        try:
            null = os.open(os.devnull, os.O_WRONLY)
            os.dup2(null, fd)
            os.close(null)
        except OSError:
            pass

    def line(self, text: str) -> None:
        self.write(text + "\n")


def exit_status(code: Any, *, windows: bool | None = None) -> int:
    """The remote command's exit code as this process's own: on POSIX a negative code (killed by a signal) is
    128+signal and a code over 255 keeps its low byte (1 when that is 0); Windows passes the 32 bits through."""
    windows = os.name == "nt" if windows is None else windows
    try:
        value = int(code)
    except (TypeError, ValueError):
        return 1
    if windows:
        # a DWORD such as 0xC000013A: as the signed 32-bit value, which sys.exit hands to ExitProcess unchanged
        value &= 0xFFFFFFFF
        return value - 0x100000000 if value > 0x7FFFFFFF else value
    if value < 0:
        return min(128 + (-value), 255)
    if value > 255:
        return (value & 0xFF) or 1
    return value


# ------------------------------------------------------------------------------------------- the client

def console_base(explicit: str | None = None, root: str | None = None, env: dict | None = None) -> str:
    """This computer's client: ``--console``, else ``$ANX_CONSOLE_URL`` (set for CLIs our chat starts), else
    the root's manager port, else the default port. Only a loopback http address is accepted."""
    env = os.environ if env is None else env
    raw = explicit or env.get("ANX_CONSOLE_URL") or ""
    if raw:
        parts = urlsplit(raw if "://" in raw else f"http://{raw}")
        if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost", "::1") or not parts.port:
            raise CliError(EXIT_USAGE, f"客户端地址必须是本机的 http://127.0.0.1:<端口>（收到 {raw}）")
        host = "127.0.0.1" if parts.hostname == "localhost" else parts.hostname
        return f"http://[{host}]:{parts.port}" if ":" in host else f"http://{host}:{parts.port}"
    port = None
    try:
        from .paths import default_root
        from .registry import load_manager_config
        cfg = load_manager_config(Path(root).resolve() if root else default_root())
        port = int(cfg.port)
    except Exception:  # noqa: BLE001 - no root here (a CLI in some project): the default port
        port = None
    if port is None:
        from .registry import DEFAULT_MANAGER_PORT
        port = DEFAULT_MANAGER_PORT
    return f"http://127.0.0.1:{port}"


def _http():
    import httpx
    # never a proxy for loopback, whatever HTTP(S)_PROXY says
    return httpx.Client(trust_env=False)


def _no_client(base: str, *, later: bool = False) -> CliError:
    """``later``: the client answered before in this run (it is restarting or updating): a wait rides it out."""
    if later:
        return CliError(EXIT_NO_CLIENT, f"本机客户端（{base}）暂时连不上（正在重启或更新？）", transient=True)
    return CliError(EXIT_NO_CLIENT, f"连不上本机的 Agent Nexus 客户端（{base}）：客户端没有运行？先启动它")


def _off_or_old(base: str, http: Any) -> str:
    """Why ``/mcp`` answered 404 without a session: the 本机 MCP switched off, or a client without the route."""
    import httpx
    try:
        resp = http.get(base + "/api/mcp/local", timeout=httpx.Timeout(15.0, connect=5.0))
        data = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        data = None
    if isinstance(data, dict) and data.get("enabled") is False:
        return "本机 MCP 已关闭（本机控制台 → 设置 → 本机 MCP）"
    if isinstance(data, dict) and "enabled" in data:
        return ""
    return "（客户端版本过旧？）"


class _Expired(Exception):
    pass


class Mcp:
    """A minimal Streamable HTTP MCP client (JSON or SSE answers), synchronous."""

    def __init__(self, base: str, path: str, http: Any = None, link: dict | None = None):
        self.base = base
        self.url = base + path
        self.http = http or _http()
        #: shared by every client of one run: ``contacted`` once the local client answered anything
        self.link = link if link is not None else {"contacted": False}
        self.session: str | None = None
        self.version = PROTOCOL
        self.ready = False
        self._next = 0

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.ready:
            headers["MCP-Protocol-Version"] = self.version
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        return headers

    def _post(self, payload: dict, read: float) -> dict | None:
        import httpx
        timeout = httpx.Timeout(connect=5.0, read=read, write=30.0, pool=5.0)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            with self.http.stream("POST", self.url, content=body, headers=self._headers(), timeout=timeout) as resp:
                self.link["contacted"] = True
                if resp.status_code == 202:
                    return None
                if resp.status_code == 404 and self.session:
                    raise _Expired()
                if resp.status_code >= 400:
                    text = resp.read().decode("utf-8", "replace").strip()
                    code = EXIT_UNAVAILABLE if resp.status_code in (403, 404, 405, 413, 415) else EXIT_PROTOCOL
                    hint = "（客户端版本过旧？）" if resp.status_code == 405 else ""
                    if resp.status_code == 404:
                        hint = _off_or_old(self.base, self.http)
                        if hint.startswith("本机 MCP 已关闭"):
                            raise CliError(EXIT_UNAVAILABLE, hint, state="off")
                    raise CliError(code, f"本机 MCP 拒绝了请求（HTTP {resp.status_code}）：{text[:300] or '无说明'}{hint}")
                session = resp.headers.get("mcp-session-id")
                if session and not self.session:
                    self.session = session
                kind = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if kind == "text/event-stream":
                    return self._sse(resp, payload.get("id"))
                data = resp.read()
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise _no_client(self.base, later=bool(self.link.get("contacted"))) from None
        except httpx.TimeoutException:
            raise CliError(EXIT_UNAVAILABLE, "本机客户端没有及时回应", transient=True) from None
        except httpx.TransportError as exc:
            raise CliError(EXIT_UNAVAILABLE, f"与本机客户端的连接中断（{type(exc).__name__}）", transient=True) from None
        try:
            message = json.loads(data) if data else None
        except ValueError:
            raise CliError(EXIT_PROTOCOL, "本机 MCP 的回答不是 JSON") from None
        if isinstance(message, list):
            message = next((m for m in message if isinstance(m, dict) and m.get("id") == payload.get("id")), None)
        return message if isinstance(message, dict) else None

    @staticmethod
    def _sse(resp: Any, rid: Any) -> dict:
        lines: list[str] = []

        def event() -> dict | None:
            if not lines:
                return None
            text = "\n".join(lines)
            lines.clear()
            try:
                message = json.loads(text)
            except ValueError:
                return None
            if isinstance(message, dict) and message.get("id") == rid and ("result" in message or "error" in message):
                return message
            return None  # a notification (progress, log): ignored

        for line in resp.iter_lines():
            if line == "":
                found = event()
                if found is not None:
                    return found
                continue
            if line.startswith("data:"):
                value = line[5:]
                lines.append(value[1:] if value.startswith(" ") else value)
        found = event()
        if found is not None:
            return found
        raise CliError(EXIT_UNAVAILABLE, "回答中断（连接在结果到达前断开）", transient=True)

    @staticmethod
    def _degraded(meta: Any) -> None:
        if isinstance(meta, dict) and meta.get("anx/state"):
            state = str(meta["anx/state"])
            raise CliError(EXIT_UNAVAILABLE, str(meta.get("anx/reason") or state), state=state,
                           transient=state == "offline")

    def initialize(self) -> None:
        self._next += 1
        answer = self._post({"jsonrpc": "2.0", "id": self._next, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "anx-cli", "version": _version()}}}, 30.0)
        if not isinstance(answer, dict) or not isinstance(answer.get("result"), dict):
            error = (answer or {}).get("error") if isinstance(answer, dict) else None
            if isinstance(error, dict):
                self._degraded(error.get("data"))
            raise CliError(EXIT_PROTOCOL, "本机 MCP 没有正确回应 initialize")
        result = answer["result"]
        self._degraded(result.get("_meta"))
        if isinstance(result.get("protocolVersion"), str):
            self.version = result["protocolVersion"]
        self.ready = True
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, 30.0)

    def request(self, method: str, params: dict, read: float) -> dict:
        if not self.ready:
            self.initialize()
        self._next += 1
        payload = {"jsonrpc": "2.0", "id": self._next, "method": method, "params": params}
        try:
            answer = self._post(payload, read)
        except _Expired:
            # the server forgot the session before dispatching: safe to repeat, even exec_command
            self.session, self.ready = None, False
            self.initialize()
            answer = self._post(payload, read)
        if not isinstance(answer, dict):
            raise CliError(EXIT_PROTOCOL, f"本机 MCP 没有回答 {method}")
        if isinstance(answer.get("error"), dict):
            error = answer["error"]
            self._degraded(error.get("data"))
            raise CliError(EXIT_PROTOCOL, f"{method} 失败：{error.get('message') or error}")
        result = answer.get("result")
        if not isinstance(result, dict):
            raise CliError(EXIT_PROTOCOL, f"{method} 的回答格式不对")
        self._degraded(result.get("_meta"))
        return result

    def call(self, name: str, arguments: dict, read: float = 30.0) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments}, read)

    def close(self) -> None:
        try:
            self.http.close()
        except Exception:  # noqa: BLE001
            pass


def _version() -> str:
    try:
        from . import __version__
        return str(__version__)
    except Exception:  # noqa: BLE001
        return "0"


def first_text(result: dict) -> str:
    """The first text item only: later items are notes (unread mail, a [device: ...] tag)."""
    content = result.get("content") if isinstance(result.get("content"), list) else []
    item = content[0] if content and isinstance(content[0], dict) else {}
    return item.get("text") if item.get("type") == "text" and isinstance(item.get("text"), str) else ""


def data_of(result: dict) -> Any:
    """A gateway tool's structured answer (raises :class:`ToolFailure` for an error result)."""
    if result.get("isError"):
        raise ToolFailure(first_text(result) or "工具调用失败")
    data = result.get("structuredContent")
    if data is None:
        text = first_text(result)
        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = None
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    if isinstance(data, dict) and data.get("status") == "error":
        raise ToolFailure(str(data.get("error") or "工具调用失败"))
    return data


def exec_fields(result: dict) -> dict:
    """One exec_command / exec_wait / exec_kill answer as ``{session_id?, exit_code?, output, note?...}``.

    Through the gateway the fields are in ``_meta["anx/exec"]`` and the output is the text after the header line
    (``(no output)`` when there was none); from ``/mcp/self`` the device's own ``structuredContent``. Raises
    :class:`ToolFailure` for an error."""
    text = first_text(result)
    meta = (result.get("_meta") or {}).get("anx/exec") if isinstance(result.get("_meta"), dict) else None
    if isinstance(meta, dict):
        fields = dict(meta)
        if fields.get("status") == "error" or result.get("isError"):
            raise ToolFailure(re.sub(r"^\[[^\]]*\]\s*", "", text) or str(fields.get("error") or "命令失败"))
        head, _, output = text.partition("\n")
        if fields.get("wait_url") and output.startswith("wait: "):  # the wait URL line of a running job
            output = output.partition("\n")[2]
        if output == "(no output)" and not fields.get("original_token_count"):
            output = ""
        fields["output"] = output
        return fields
    data = result.get("structuredContent")
    if isinstance(data, dict) and ("output" in data or "status" in data or "exit_code" in data):
        if data.get("status") == "error":
            raise ToolFailure(str(data.get("error") or "命令失败"))
        if data.get("exit_code") is None and data.get("error"):
            raise ToolFailure(str(data.get("output") or data["error"]))
        fields = dict(data)
        fields["output"] = data.get("output") if isinstance(data.get("output"), str) else ""
        return fields
    if result.get("isError"):
        raise ToolFailure(re.sub(r"^\[error[^\]]*\]\s*", "", text) or "命令失败")
    head, _, output = text.partition("\n")
    fields: dict = {"output": "" if output == "(no output)" else output}
    found = _HEAD_EXIT.match(head)
    if found:
        fields["exit_code"] = int(found.group(1))
        return fields
    found = _HEAD_RUNNING.match(head)
    if found:
        fields["session_id"] = int(found.group(1))
        return fields
    raise CliError(EXIT_PROTOCOL, f"看不懂设备的回答：{head[:200]}")


# ----------------------------------------------------------------------------------------- the devices

@dataclass
class Target:
    """A device to run tools on: this computer (``local``, through ``/mcp/self``) or another one."""
    ctx: "Ctx"
    local: bool
    device_id: str | None = None
    label: str = "本机"
    shells: list[str] = field(default_factory=list)
    shell_default: str | None = None
    workdir: str | None = None

    @property
    def token(self) -> str:
        return "." if self.local else str(self.device_id)

    def call(self, tool: str, arguments: dict, read: float = 30.0, *, fresh: bool = False) -> dict:
        mcp = self.ctx.mcp(SELF_PATH if self.local else GATEWAY_PATH, fresh=fresh)
        try:
            if self.local:
                return mcp.call(tool, arguments, read)
            return mcp.call("tooluse", {"tool": tool, "arguments": arguments, "mcp_id": f"{self.device_id}/device"},
                            read)
        finally:
            if fresh:
                mcp.close()


class Ctx:
    def __init__(self, base: str, out: Out, err: Out):
        self.base = base
        self.out = out
        self.err = err
        self._mcps: dict[str, Mcp] = {}
        #: shared with every Mcp of this run (see :func:`_no_client`)
        self.link: dict = {"contacted": False}
        self._me: dict | None = None
        self._devices: list[dict] | None = None

    def mcp(self, path: str, *, fresh: bool = False) -> Mcp:
        if fresh:
            return Mcp(self.base, path, link=self.link)
        if path not in self._mcps:
            self._mcps[path] = Mcp(self.base, path, link=self.link)
        return self._mcps[path]

    def me(self) -> dict:
        """``GET /api/cloud``: this device's agent_id, whether it joined, its names."""
        if self._me is None:
            import httpx
            try:
                with _http() as http:
                    resp = http.get(self.base + "/api/cloud", timeout=httpx.Timeout(10.0, connect=5.0))
                self.link["contacted"] = True
                data = resp.json() if resp.status_code == 200 else {}
            except (httpx.ConnectError, httpx.ConnectTimeout):
                raise _no_client(self.base, later=bool(self.link.get("contacted"))) from None
            except (httpx.HTTPError, ValueError):
                data = {}
            self._me = data if isinstance(data, dict) else {}
        return self._me

    def devices(self) -> list[dict]:
        if self._devices is None:
            try:
                data = data_of(self.mcp(GATEWAY_PATH).call("device_list", {"include_offline": True}))
            except ToolFailure as exc:
                raise CliError(EXIT_UNAVAILABLE, f"网关没有列出设备：{exc}") from None
            rows = data.get("devices") if isinstance(data, dict) else None
            self._devices = [row for row in rows or [] if isinstance(row, dict)]
        return self._devices

    def self_target(self) -> Target:
        return Target(self, True)

    def resolve(self, name: str | None) -> Target:
        """``.`` / nothing / this device's own id or name: this computer; else a device of ``device_list``
        (its id first, then its display name, case-insensitive)."""
        name = (name or "").strip()
        if name in ("", "."):
            return self.self_target()
        me = self.me()
        if me.get("agent_id") and name == me["agent_id"]:
            return self.self_target()
        try:
            devices = self.devices()
        except CliError as exc:
            own = {str(me.get(key) or "").casefold() for key in ("display_name", "hostname")} - {""}
            if exc.state == "not_joined" and name.casefold() in own:
                return self.self_target()
            raise
        found = [row for row in devices if row.get("device_id") == name]
        if not found:
            folded = name.casefold()
            found = [row for row in devices if str(row.get("display_name") or "").casefold() == folded]
            if not found:
                found = [row for row in devices if str(row.get("hostname") or "").casefold() == folded]
        if len(found) > 1:
            names = "、".join(f"{row.get('display_name')}（{row.get('device_id')}）" for row in found)
            raise CliError(EXIT_DEVICE, f"有多台设备叫 {name}：{names}；请改用设备 ID")
        if not found:
            raise CliError(EXIT_DEVICE, f"没有叫 {name} 的设备（anx devices 列出全部设备）")
        row = found[0]
        if me.get("agent_id") and row.get("device_id") == me["agent_id"]:
            return self.self_target()
        label = f"{row.get('display_name') or row.get('device_id')}"
        if not row.get("online"):
            raise CliError(EXIT_DEVICE, f"设备 {label} 不在线")
        shells = [str(s) for s in row.get("shells") or [] if isinstance(s, str)]
        workdirs = [w for w in row.get("workdirs") or [] if isinstance(w, dict) and w.get("path")]
        default = next((w for w in workdirs if w.get("name") == "default"), workdirs[0] if workdirs else None)
        return Target(self, False, device_id=str(row["device_id"]), label=label, shells=shells,
                      shell_default=row.get("shell_default") or (shells[0] if shells else None),
                      workdir=str(default["path"]) if default else None)

    def close(self) -> None:
        for mcp in self._mcps.values():
            mcp.close()


# ------------------------------------------------------------------------------- running interruptibly

class Job:
    def __init__(self) -> None:
        self.cancel = threading.Event()
        self.cancel_code = EXIT_INTERRUPTED
        self.done = threading.Event()
        self.code: int | None = None
        #: what to tell the user when a second interrupt abandons the work
        self.abandon_hint = ""


def run_job(work: Callable[[Job], int], on_cancel: Callable[[Job], None], err: Out) -> int:
    """``work`` in a worker thread; the main thread only waits, so Ctrl+C (and SIGTERM / SIGHUP / SIGBREAK) is
    seen at once even while a socket read blocks (Windows does not interrupt one). The first signal sets
    ``job.cancel`` and calls ``on_cancel``; a second one gives up at once."""
    job = Job()

    def worker() -> None:
        try:
            job.code = work(job)
        except CliError as exc:
            err.line(f"{PROG}: {exc.message}")
            job.code = exc.code
        except Exception as exc:  # noqa: BLE001 - never a traceback for the caller to parse
            err.line(f"{PROG}: 内部错误：{type(exc).__name__}: {exc}")
            job.code = EXIT_PROTOCOL
        finally:
            job.done.set()

    hits = [0]

    def interrupt(code: int) -> None:
        hits[0] += 1
        if hits[0] == 1:
            job.cancel_code = code
            job.cancel.set()
            try:
                on_cancel(job)
            except Exception:  # noqa: BLE001
                pass

    previous: dict = {}
    if threading.current_thread() is threading.main_thread():
        wanted = {"SIGINT": EXIT_INTERRUPTED, "SIGTERM": 143, "SIGHUP": 129, "SIGBREAK": 149}
        for name, code in wanted.items():
            number = getattr(signal, name, None)
            if number is None:
                continue
            try:
                previous[number] = signal.signal(number, lambda _n, _f, code=code: interrupt(code))
            except (ValueError, OSError):
                pass
    thread = threading.Thread(target=worker, name="anx-remote", daemon=True)
    thread.start()
    try:
        while not job.done.wait(0.2):
            if hits[0] >= 2:
                if job.abandon_hint:
                    err.line(f"{PROG}: {job.abandon_hint}")
                return job.cancel_code
    except KeyboardInterrupt:  # no handler of ours (not the main thread): give up as a second Ctrl+C would
        return EXIT_INTERRUPTED
    finally:
        for number, handler in previous.items():
            try:
                signal.signal(number, handler)
            except (ValueError, OSError, TypeError):
                pass
    return job.code if job.code is not None else EXIT_PROTOCOL


# ---------------------------------------------------------------------------------------------- exec

def _shell_name(value: str) -> str:
    name = value.strip().lower()
    return name[:-4] if name.endswith(".exe") else name


def tool_failure(exc: ToolFailure, target: Target) -> CliError:
    text = str(exc)
    if re.search(r"unknown exec session", text, re.IGNORECASE):
        return CliError(EXIT_TOOL, "会话不存在或已过期（命令结束 10 分钟后、闲置 1 小时后会被清理）")
    if re.search(r"shell .* is not available|not available on .*available:", text, re.IGNORECASE):
        return CliError(EXIT_SHELL, f"{target.label}：{text}")
    if re.search(r"manager .* is offline|unknown MCP", text):
        return CliError(EXIT_DEVICE, f"设备 {target.label} 不在线（{text}）")
    return CliError(EXIT_TOOL, f"{target.label}：{text}")


class ExecRun:
    """One remote command followed to its end: output written as it comes, the exit code returned."""

    def __init__(self, target: Target, out: Out, err: Out, *, timeout: float | None = None, detach: bool = False):
        self.target = target
        self.out = out
        self.err = err
        self.deadline = time.monotonic() + timeout if timeout else None
        self.detach = detach
        self.session: int | None = None
        self.lock = threading.Lock()
        self.killing = False
        self.killed = threading.Event()
        self.warned = False
        #: a wait failed after it may have reached the device: its output may be lost (told once)
        self.gap = False
        self.gap_told = False

    @property
    def token(self) -> str:
        return f"{self.target.token}:{self.session}"

    def emit(self, text: str, stream: Out | None = None) -> None:
        if not text:
            return
        if "[output truncated" in text and not self.warned:
            self.warned = True
            self.err.line(f"{PROG}: 注意：输出太多，中间一部分被截掉了（每次最多约 60000 个字符）；"
                          "需要完整输出时让命令写进远程文件，再用 anx cp 取回")
        (stream or self.out).write(text)

    # -- cancel
    def request_kill(self) -> None:
        with self.lock:
            if self.killing or self.session is None:
                return
            self.killing = True
        threading.Thread(target=self._kill, name="anx-kill", daemon=True).start()

    def _kill(self) -> None:
        try:
            result = self.target.call("exec_kill", {"session_id": self.session}, KILL_TIMEOUT_S, fresh=True)
            try:
                self.emit(exec_fields(result).get("output") or "")
            except ToolFailure as exc:
                if not re.search(r"unknown exec session", str(exc), re.IGNORECASE):
                    # the device offline, a policy refusal...: the command may well still run
                    raise CliError(EXIT_TOOL, str(exc)) from None
                self.err.line(f"{PROG}: 远程命令已经结束（{self.token}）")
                return
            self.err.line(f"{PROG}: 已停止远程命令（{self.token}）")
        except CliError as exc:
            self.err.line(f"{PROG}: 没能停止远程命令（{exc.message}），它可能仍在运行：anx kill {self.token}")
        finally:
            self.killed.set()

    def cancelled(self, job: Job) -> int:
        self.request_kill()
        if self.killing:
            self.killed.wait(KILL_TIMEOUT_S + 5)
        return job.cancel_code

    def pipe_closed(self) -> int:
        """stdout's reader went away (``| head``): stop the remote command, as a local pipeline would."""
        self.request_kill()
        if self.killing:
            self.killed.wait(KILL_TIMEOUT_S + 5)
        return EXIT_PIPE

    # -- the loop
    def start(self, job: Job, arguments: dict) -> int:
        job.abandon_hint = "远程命令可能仍在运行（anx exec 的会话号还不知道；用 anx devices 确认设备，必要时登录设备处理）"
        yield_ms = DETACH_YIELD_MS if self.detach else FIRST_YIELD_MS
        arguments = {**arguments, "stdin": "", "max_output_tokens": MAX_OUTPUT_TOKENS, "yield_time_ms": yield_ms}
        try:
            try:
                result = self.target.call("exec_command", arguments, yield_ms / 1000 + 30)
                fields = exec_fields(result)
            except ToolFailure as exc:
                if "stdin" not in str(exc) or "invalid tool arguments" not in str(exc):
                    raise
                arguments.pop("stdin", None)  # a device older than exec_command(stdin=...)
                fields = exec_fields(self.target.call("exec_command", arguments, yield_ms / 1000 + 30))
        except ToolFailure as exc:
            raise tool_failure(exc, self.target) from None
        except CliError as exc:
            if exc.transient and exc.code != EXIT_NO_CLIENT:  # a refused connection never reached the device
                raise CliError(EXIT_UNAVAILABLE, f"{exc.message}：不知道命令是否已经启动") from None
            if exc.code == EXIT_NO_CLIENT:
                raise CliError(EXIT_NO_CLIENT, f"{exc.message}；命令没有启动") from None
            raise
        if fields.get("session_id") is not None:
            self.session = int(fields["session_id"])
            job.abandon_hint = f"远程命令可能仍在运行：anx kill {self.token}"
        if fields.get("exit_code") is None and self.session is not None and self.detach:
            self.emit(fields.get("output") or "", self.err)
            self.out.line(self.token)
            return 0
        self.emit(fields.get("output") or "")
        return self.follow(job, fields)

    def follow(self, job: Job, fields: dict) -> int:
        step = 0
        lost_since: float | None = None
        while True:
            if self.out.broken.is_set():
                return self.pipe_closed()
            if fields.get("exit_code") is not None and job.cancel.is_set():
                return self.cancelled(job)  # ended by our exec_kill: interrupted, not the command's own code
            if fields.get("exit_code") is not None:
                if isinstance(fields.get("note"), str) and "finished earlier" in fields["note"]:
                    self.err.line(f"{PROG}: 这个命令早已结束（退出码 {fields['exit_code']}），没取走的输出已经清理")
                return exit_status(fields["exit_code"])
            if self.session is None:
                raise CliError(EXIT_PROTOCOL, "设备既没有给出退出码，也没有给出会话号")
            if job.cancel.is_set():
                return self.cancelled(job)
            wait_ms = WAIT_STEPS_MS[min(step, len(WAIT_STEPS_MS) - 1)]
            if self.deadline is not None:
                left = self.deadline - time.monotonic()
                if left <= 0:
                    self.err.line(f"{PROG}: 已到 --timeout，命令仍在运行：anx wait {self.token} / anx kill {self.token}")
                    return EXIT_TIMEOUT
                wait_ms = min(wait_ms, max(100, int(left * 1000)))
            wait_ms = min(wait_ms, WAIT_CAP_MS)
            try:
                result = self.target.call("exec_wait", {"session_id": self.session, "timeout_ms": wait_ms,
                                                        "max_output_tokens": MAX_OUTPUT_TOKENS}, wait_ms / 1000 + 30)
                fields = exec_fields(result)
                lost_since = None
                if self.gap and not self.gap_told:
                    self.gap_told = True
                    self.err.line(f"{PROG}: 注意：连接中断后恢复，中间可能缺了一段输出")
            except (CliError, ToolFailure) as exc:
                transient = exc.transient if isinstance(exc, CliError) else bool(_TRANSIENT.search(str(exc))) \
                    and "unknown exec session" not in str(exc)
                if not transient:
                    raise tool_failure(exc, self.target) if isinstance(exc, ToolFailure) else exc from None
                # the device may have answered (and counted that output as delivered) before the answer was lost;
                # a refused connection or a degraded local MCP never got as far as the device
                if not (isinstance(exc, CliError) and (exc.code == EXIT_NO_CLIENT or exc.state)):
                    self.gap = True
                now = time.monotonic()
                lost_since = lost_since or now
                if now - lost_since > LOST_GRACE_S:
                    raise CliError(EXIT_UNAVAILABLE, f"连接中断，命令可能仍在运行：anx wait {self.token}") from None
                job.cancel.wait(min(5.0, RETRY_S + (now - lost_since) / 4))
                fields = {}
                continue
            output = fields.get("output") or ""
            if output:
                self.emit(output)
                step = 0
            else:
                step += 1


def parse_exec(argv: list[str]) -> dict:
    """``exec [device] [opts] [--] <command...>``: everything after ``--`` (or after the device, like ssh) is the
    command; options only before it."""
    opts: dict[str, Any] = {"device": None, "shell": None, "cwd": None, "timeout": None, "detach": False,
                            "stdin": False, "console": None, "command": None}
    values = {"--shell": "shell", "--cwd": "cwd", "--timeout": "timeout", "--console": "console"}
    flags = {"--detach": "detach", "--stdin": "stdin"}
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--":
            opts["command"] = argv[index + 1:]
            break
        if item in ("-h", "--help"):
            raise Usage("")
        if item.startswith("--") and len(item) > 2:
            name, eq, value = item.partition("=")
            if name in values:
                if not eq:
                    index += 1
                    if index >= len(argv):
                        raise Usage(f"{name} 需要一个值")
                    value = argv[index]
                opts[values[name]] = value
            elif name in flags and not eq:
                opts[flags[name]] = True
            else:
                raise Usage(f"不认识的选项 {item}")
        elif item.startswith("-") and len(item) > 1:
            raise Usage(f"不认识的选项 {item}（命令前加 -- 隔开）")
        elif opts["device"] is None:
            opts["device"] = item
        else:
            opts["command"] = argv[index:]
            break
        index += 1
    if opts["timeout"] is not None:
        try:
            opts["timeout"] = float(opts["timeout"])
        except ValueError:
            raise Usage("--timeout 要写秒数") from None
        if opts["timeout"] <= 0:
            raise Usage("--timeout 要大于 0")
    if opts["shell"] is not None and not opts["shell"].strip():
        raise Usage("--shell 不能是空的")
    if opts["stdin"] and opts["command"]:
        raise Usage("--stdin 和 -- <命令> 只能选一个")
    if not opts["stdin"] and not opts["command"]:
        raise Usage("没有要运行的命令")
    return opts


EXEC_USAGE = ("用法：anx exec [设备] [--shell bash|sh|zsh|pwsh|powershell|cmd] [--cwd 目录] [--timeout 秒] "
              "[--detach] [--stdin] [--console URL] [--] <命令…>\n"
              "  设备写名称或 ID，. 或省略表示本机；输出实时显示，退出码就是远程命令的退出码。\n"
              "  --stdin：从标准输入读命令原文（不经本地 shell 再解析一遍）；--detach：立即返回会话号。")


def read_stdin(stdin: Any) -> str:
    raw = stdin.buffer.read() if hasattr(stdin, "buffer") else stdin.read()
    if isinstance(raw, str):
        return raw.removeprefix("\ufeff")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False) or "utf-8", "replace")


def cmd_exec(argv: list[str], root: str | None, io: "Io") -> int:
    try:
        opts = parse_exec(argv)
    except Usage as exc:
        if str(exc):
            io.err.line(f"{PROG} exec: {exc}")
        io.err.line(EXEC_USAGE)
        return EXIT_USAGE if str(exc) else 0
    command = read_stdin(io.stdin) if opts["stdin"] else " ".join(opts["command"])
    if not command.strip():
        io.err.line(f"{PROG} exec: 命令是空的")
        return EXIT_USAGE
    ctx = Ctx(console_base(opts["console"], root, io.env), io.out, io.err)
    run: dict = {}

    def work(job: Job) -> int:
        target = ctx.resolve(opts["device"])
        arguments: dict = {"cmd": command}
        if opts["shell"]:
            shell = _shell_name(opts["shell"])
            if target.shells and shell in SHELLS and shell not in target.shells:
                raise CliError(EXIT_SHELL, f"设备 {target.label} 上没有 {shell}；可用：{', '.join(target.shells)}"
                               f"（默认 {target.shell_default or target.shells[0]}）")
            arguments["shell"] = opts["shell"]
        workdir = opts["cwd"] or target.workdir
        if workdir:
            arguments["workdir"] = workdir
        run["exec"] = ExecRun(target, io.out, io.err, timeout=opts["timeout"], detach=opts["detach"])
        if job.cancel.is_set():
            return job.cancel_code
        return run["exec"].start(job, arguments)

    def on_cancel(_job: Job) -> None:
        if "exec" in run:
            run["exec"].request_kill()

    try:
        return run_job(work, on_cancel, io.err)
    finally:
        ctx.close()


def parse_token(value: str) -> tuple[str, int]:
    device, sep, session = value.strip().rpartition(":")
    if not sep:
        device = "."
    if not re.fullmatch(r"\d{1,12}", session):
        raise Usage(f"会话要写成 <设备>:<会话号>（anx exec --detach 打印的那样），收到 {value}")
    return device or ".", int(session)


def _session_parser(name: str, help_text: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{PROG} {name}", description=help_text)
    parser.add_argument("session", help="<设备>:<会话号>，anx exec --detach 或超时提示里打印的那样")
    parser.add_argument("--console", help="本机客户端地址（默认 $ANX_CONSOLE_URL 或配置的端口）")
    return parser


def cmd_wait(argv: list[str], root: str | None, io: "Io") -> int:
    parser = _session_parser("wait", "等一个已分离（--detach）或超时的远程命令结束：输出和退出码同 anx exec")
    parser.add_argument("--timeout", type=float, help="最多等多少秒（默认一直等）")
    args = parser.parse_args(argv)
    try:
        device, session = parse_token(args.session)
    except Usage as exc:
        io.err.line(f"{PROG} wait: {exc}")
        return EXIT_USAGE
    if args.timeout is not None and args.timeout <= 0:
        io.err.line(f"{PROG} wait: --timeout 要大于 0")
        return EXIT_USAGE
    ctx = Ctx(console_base(args.console, root, io.env), io.out, io.err)
    run: dict = {}

    def work(job: Job) -> int:
        target = ctx.resolve(device)
        execution = ExecRun(target, io.out, io.err, timeout=args.timeout)
        execution.session = session
        job.abandon_hint = f"远程命令仍在运行：anx kill {execution.token}"
        run["exec"] = execution
        return execution.follow(job, {})

    def on_cancel(_job: Job) -> None:
        # Ctrl+C during a wait stops the command, as it would have in the anx exec that started it
        if "exec" in run:
            run["exec"].request_kill()

    try:
        return run_job(work, on_cancel, io.err)
    finally:
        ctx.close()


def cmd_kill(argv: list[str], root: str | None, io: "Io") -> int:
    parser = _session_parser("kill", "停止一个远程命令（连同它启动的所有进程），打印它剩下的输出")
    args = parser.parse_args(argv)
    try:
        device, session = parse_token(args.session)
    except Usage as exc:
        io.err.line(f"{PROG} kill: {exc}")
        return EXIT_USAGE
    ctx = Ctx(console_base(args.console, root, io.env), io.out, io.err)
    try:
        target = ctx.resolve(device)
        try:
            fields = exec_fields(target.call("exec_kill", {"session_id": session}, KILL_TIMEOUT_S + 20))
        except ToolFailure as exc:
            raise tool_failure(exc, target) from None
        io.out.write(fields.get("output") or "")
        code = fields.get("exit_code")
        io.err.line(f"{PROG}: 已停止 {target.token}:{session}" + (f"（退出码 {code}）" if code is not None else ""))
        return 0
    except CliError as exc:
        io.err.line(f"{PROG}: {exc.message}")
        return exc.code
    finally:
        ctx.close()


# -------------------------------------------------------------------------------------------- devices

def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def table(rows: list[list[str]]) -> str:
    widths = [max(_width(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = []
    for row in rows:
        cells = [cell + " " * (widths[i] - _width(cell)) for i, cell in enumerate(row)]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines) + "\n"


def device_row(entry: dict, me: str | None) -> list[str]:
    shells = [str(s) for s in entry.get("shells") or []]
    default = entry.get("shell_default") or (shells[0] if shells else None)
    ordered = ([default] if default in shells else []) + [s for s in shells if s != default]
    shell_text = ",".join(f"{s}*" if s == default else s for s in ordered) or "-"
    workdirs = ",".join(f"{w.get('name')}={w.get('path')}" for w in entry.get("workdirs") or []
                        if isinstance(w, dict)) or "-"
    name = str(entry.get("display_name") or entry.get("hostname") or entry.get("device_id") or "-")
    if me and entry.get("device_id") == me:
        name += "（本机）"
    os_text = str(entry.get("os_family") or entry.get("platform") or "-")
    return [name, str(entry.get("device_id") or "-"), "在线" if entry.get("online") else "离线", os_text,
            shell_text, workdirs]


HEADER = ["NAME", "ID", "ONLINE", "OS", "SHELLS", "WORKDIRS"]


def cmd_devices(argv: list[str], root: str | None, io: "Io") -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} devices",
                                     description="列出设备：在线状态、系统、shell（默认的标 *）、工作目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON（device_list 的原样结果）")
    parser.add_argument("--console", help="本机客户端地址（默认 $ANX_CONSOLE_URL 或配置的端口）")
    args = parser.parse_args(argv)
    ctx = Ctx(console_base(args.console, root, io.env), io.out, io.err)
    try:
        me = ctx.me()
        try:
            devices = ctx.devices()
        except CliError as exc:
            if exc.state != "not_joined":
                raise
            info = data_of(ctx.mcp(SELF_PATH).call("device_info", {}))
            info = info if isinstance(info, dict) else {}
            entry = {"device_id": me.get("agent_id") or "-", "display_name": me.get("display_name")
                     or info.get("hostname") or me.get("hostname"), "online": True,
                     "platform": info.get("platform"), "shells": info.get("shells") or [],
                     "shell_default": info.get("shell_default"), "workdirs": [], "self": True}
            io.err.line(f"{PROG}: 未加入网关：只列出本机")
            devices = [entry]
            me = {**me, "agent_id": entry["device_id"]}
        if args.json:
            mine = me.get("agent_id")
            io.out.line(json.dumps({"devices": [{**d, "self": d.get("self") or d.get("device_id") == mine}
                                                for d in devices]}, ensure_ascii=False, indent=1))
            return 0
        rows = [HEADER] + [device_row(entry, me.get("agent_id")) for entry in devices]
        io.out.write(table(rows))
        return 0
    except ToolFailure as exc:
        io.err.line(f"{PROG}: {exc}")
        return EXIT_TOOL
    except CliError as exc:
        io.err.line(f"{PROG}: {exc.message}")
        return exc.code
    finally:
        ctx.close()


# ------------------------------------------------------------------------------------------------- cp

def parse_place(value: str) -> tuple[str, str]:
    device, sep, path = value.partition(":")
    if not sep or not device or not path:
        raise Usage(f"要写成 <设备>:<路径>（本机用 .:<路径>），收到 {value}")
    if len(device) == 1 and device.isalpha() and path[:1] in ("\\", "/"):
        raise Usage(f"{value} 看起来是 Windows 路径；本机的写成 .:{value}，其他设备的写成 <设备>:{value}")
    return device, path


def human(size: Any) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def progress_text(snap: dict) -> str:
    files = snap.get("files") if isinstance(snap.get("files"), dict) else {}
    size = snap.get("bytes") if isinstance(snap.get("bytes"), dict) else {}
    text = (f"{snap.get('state')}：{files.get('done', 0)}/{files.get('total', 0)} 个文件，"
            f"{human(size.get('done', 0))}/{human(size.get('total', 0))}")
    if snap.get("rate_bps"):
        text += f"，{human(snap['rate_bps'])}/s"
    if snap.get("eta_s"):
        text += f"，还要约 {snap['eta_s']} 秒"
    return text


#: a job that may still run: a second one now would write the same files alongside it
CP_LATER = "等它结束后再重新运行同一条命令即可续传（别同时再开一个）"


def cmd_cp(argv: list[str], root: str | None, io: "Io") -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} cp", description=(
        "在设备之间复制文件或文件夹（后台任务，数据在两台设备之间直接传）：anx cp <设备>:<路径> <设备>:<路径>。"
        "目标以分隔符结尾时复制到那个文件夹里；中断后重新运行同一条命令即可续传。"))
    parser.add_argument("source", help="<设备>:<路径>，本机用 .:<路径>")
    parser.add_argument("dest", help="<设备>:<路径>")
    parser.add_argument("--overwrite", action="store_true", help="覆盖目标上内容不同的同名文件")
    parser.add_argument("--console", help="本机客户端地址（默认 $ANX_CONSOLE_URL 或配置的端口）")
    args = parser.parse_args(argv)
    try:
        src = parse_place(args.source)
        dst = parse_place(args.dest)
    except Usage as exc:
        io.err.line(f"{PROG} cp: {exc}")
        return EXIT_USAGE
    ctx = Ctx(console_base(args.console, root, io.env), io.out, io.err)
    state: dict = {}

    def device_id(name: str) -> str:
        if name == ".":
            me = ctx.me()
            ctx.devices()  # not joined: the reason, before anything else
            if not me.get("agent_id"):
                raise CliError(EXIT_UNAVAILABLE, "这台设备还没加入网关，anx cp 不可用")
            return str(me["agent_id"])
        target = ctx.resolve(name)
        return str(target.device_id) if not target.local else str(ctx.me().get("agent_id"))

    def work(job: Job) -> int:
        gateway = ctx.mcp(GATEWAY_PATH)
        source, dest = device_id(src[0]), device_id(dst[0])
        try:
            started = data_of(gateway.call("file_copy", {"src_device": source, "src_path": src[1],
                                                         "dst_device": dest, "dst_path": dst[1],
                                                         "overwrite": bool(args.overwrite)}, 90.0))
        except ToolFailure as exc:
            raise CliError(EXIT_TOOL, f"复制没有开始：{exc}") from None
        if not isinstance(started, dict) or not started.get("job_id"):
            raise CliError(EXIT_PROTOCOL, "file_copy 没有返回任务号")
        state["job"] = job_id = str(started["job_id"])
        job.abandon_hint = f"复制任务仍在后台运行（{job_id}）；{CP_LATER}"
        if started.get("warning"):
            io.err.line(f"{PROG}: {started['warning']}")
        last_plain = time.monotonic()
        snap = started
        lost_since: float | None = None
        told = ""

        def lost(message: str) -> bool:
            """A status that may pass (the runner reconnecting, the client restarting): retried for a while;
            False once that grace is over."""
            nonlocal lost_since, told
            lost_since = lost_since or time.monotonic()
            if message and message != told:
                told = message
                if io.err.tty:
                    io.err.write("\r\033[K")
                io.err.line(f"{PROG}: {message}")
            if time.monotonic() - lost_since > LOST_GRACE_S:
                return False
            job.cancel.wait(RETRY_S * 2)
            return True

        while True:
            if job.cancel.is_set():
                if "stopped" in state:
                    state["stopped"].wait(KILL_TIMEOUT_S + 5)
                if io.err.tty:
                    io.err.write("\n")
                if state.get("cancel_error"):
                    io.err.line(f"{PROG}: 取消失败（{state['cancel_error']}），任务仍在后台运行（{job_id}）")
                else:
                    io.err.line(f"{PROG}: 已取消复制（已复制的文件保留）")
                return job.cancel_code
            try:
                snap = data_of(gateway.call("file_copy_status", {"job_id": job_id, "wait_ms": CP_WAIT_MS}, 40.0))
            except CliError as exc:
                if not exc.transient:
                    raise
                if lost(exc.message):
                    continue
                raise CliError(EXIT_UNAVAILABLE, f"查不到复制进度，任务可能仍在后台运行（{job_id}）；{CP_LATER}") from None
            except ToolFailure as exc:
                if not _TRANSIENT.search(str(exc)):
                    raise CliError(EXIT_TOOL, f"查询复制进度失败：{exc}") from None
                if lost(f"查询复制进度失败：{exc}"):
                    continue
                raise CliError(EXIT_UNAVAILABLE, f"查不到复制进度，任务可能仍在后台运行（{job_id}）；{CP_LATER}") from None
            snap = snap if isinstance(snap, dict) else {}
            current = str(snap.get("state") or "unknown")
            if current == "unknown" and snap.get("runner_device"):
                # the runner offline or a status ask that failed: the job goes on there
                if lost(str(snap.get("message") or "暂时查不到复制进度")):
                    continue
                snap = {**snap, "job_id": snap.get("job_id") or job_id, "message": None}  # already told
                if io.err.tty:
                    io.err.write("\r\033[K")
                return cp_summary(snap, io)
            lost_since, told = None, ""
            if current in CP_ACTIVE:
                if io.err.tty:
                    io.err.write("\r" + progress_text(snap) + "\033[K")
                elif time.monotonic() - last_plain >= CP_PLAIN_EVERY_S:
                    last_plain = time.monotonic()
                    io.err.line(progress_text(snap))
                continue
            if io.err.tty:
                io.err.write("\r\033[K")
            return cp_summary(snap, io)

    def on_cancel(_job: Job) -> None:
        job_id = state.get("job")
        if not job_id:
            return
        stopped = state["stopped"] = threading.Event()

        def cancel() -> None:
            mcp = ctx.mcp(GATEWAY_PATH, fresh=True)
            try:
                data_of(mcp.call("file_copy_cancel", {"job_id": job_id}, KILL_TIMEOUT_S))
            except ToolFailure as exc:
                state["cancel_error"] = str(exc)
            except CliError as exc:
                state["cancel_error"] = exc.message
            finally:
                mcp.close()
                stopped.set()
        threading.Thread(target=cancel, daemon=True).start()

    try:
        return run_job(work, on_cancel, io.err)
    finally:
        ctx.close()


def cp_summary(snap: dict, io: "Io") -> int:
    state = str(snap.get("state") or "unknown")
    files = snap.get("files") if isinstance(snap.get("files"), dict) else {}
    size = snap.get("bytes") if isinstance(snap.get("bytes"), dict) else {}
    names = {"completed": "完成", "completed_with_errors": "完成，但有错误", "failed": "失败", "cancelled": "已取消",
             "interrupted": "中断", "unknown": "状态未知"}
    io.out.line(f"{names.get(state, state)}：复制 {files.get('done', 0)} 个文件（{human(size.get('done', 0))}），"
                f"跳过 {files.get('skipped', 0)}，失败 {files.get('failed', 0)}")
    for error in (snap.get("errors") or [])[:20]:
        if isinstance(error, dict):
            io.err.line(f"  {error.get('rel')}: {error.get('error')}")
    if snap.get("errors_total", 0) > 20:
        io.err.line(f"  ……共 {snap['errors_total']} 个错误")
    if snap.get("message"):
        io.err.line(f"{PROG}: {snap['message']}")
    if state == "unknown" and snap.get("runner_device"):
        io.err.line(f"{PROG}: 任务可能仍在后台运行（{snap.get('job_id')}）；{CP_LATER}")
    elif state in ("interrupted", "unknown"):
        io.err.line(f"{PROG}: 重新运行同一条命令即可续传")
    return CP_EXIT.get(state, 6)


# -------------------------------------------------------------------------------------------- entry

@dataclass
class Io:
    out: Out
    err: Out
    stdin: Any
    env: dict


def main(argv: list[str], root: str | None = None, *, stdout: Any = None, stderr: Any = None, stdin: Any = None,
         env: dict | None = None) -> int:
    """``argv`` starts with the command name (devices, exec, wait, kill, cp)."""
    io = Io(Out(stdout or sys.stdout), Out(stderr or sys.stderr), stdin if stdin is not None else sys.stdin,
            dict(os.environ) if env is None else env)
    if not argv or argv[0] not in COMMANDS:
        io.err.line(f"{PROG}: 命令应为 {' / '.join(COMMANDS)}")
        return EXIT_USAGE
    handler = {"devices": cmd_devices, "exec": cmd_exec, "wait": cmd_wait, "kill": cmd_kill, "cp": cmd_cp}[argv[0]]
    try:
        return handler(argv[1:], root, io)
    except SystemExit as exc:  # argparse: --help (0) or a usage error (2)
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    except CliError as exc:
        io.err.line(f"{PROG}: {exc.message}")
        return exc.code
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    finally:
        if stdout is None:
            io.out.silence()
