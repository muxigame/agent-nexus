"""HTTP API and web console."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route

from . import __version__, desktop, logs
from .registry import ICON_TYPES, OBSERVE_EDITABLE_FIELDS, RegistryError, save_override
from .supervisor import Supervisor, SupervisorError
from .ui_assets import static_dir_route, ui_route, vendor_route

STATIC_DIR = Path(__file__).parent / "static"
ACTIONS = {"start", "stop", "restart"}
CONFIG_FIELDS: dict[str, type] = {"port": int, "autostart": bool, "autorestart": bool, "enabled": bool}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ICON_HEADERS = {
    "Cache-Control": "max-age=300",
    "X-Content-Type-Options": "nosniff",
    # An SVG opened directly would otherwise run its scripts with the
    # console's origin, which is allowed to start and kill processes.
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
}

log = logging.getLogger(__name__)


class LocalOnly:
    """Same-machine, same-origin use only.

    The console starts and kills processes, and binding to 127.0.0.1 does not
    stop a web page in the user's browser from sending requests to localhost
    (DNS rebinding can even make them look same-origin). So the Host header
    must be a loopback name, and state-changing requests must carry
    ``X-ANX: 1`` (or the legacy ``X-MCPM: 1``) - a custom header forces a CORS
    preflight this server never approves - plus, when the browser sends one, a
    matching Origin.
    """

    def __init__(self, app: Any, host: str, port: int):
        self.app = app
        self.hosts = {f"{name}:{port}" for name in ("127.0.0.1", "localhost", "[::1]", host)}

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "websocket":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            origin = headers.get("origin")
            if headers.get("host") not in self.hosts or (origin and origin.split("://", 1)[-1] not in self.hosts):
                await send({"type": "websocket.close", "code": 1008})
                return
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            reason = self._reject(scope["method"], headers)
            if reason:
                await PlainTextResponse(reason, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _reject(self, method: str, headers: dict[str, str]) -> str | None:
        if headers.get("host") not in self.hosts:
            return "forbidden: unexpected Host header"
        if method not in SAFE_METHODS:
            if headers.get("x-anx") != "1" and headers.get("x-mcpm") != "1":
                return "forbidden: missing X-ANX header"
            origin = headers.get("origin")
            if origin and origin.split("://", 1)[-1] not in self.hosts:
                return "forbidden: foreign Origin"
        return None


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, SupervisorError) and str(exc).startswith("unknown "):
        status = 404
    elif isinstance(exc, RegistryError):
        status = 400
    else:
        status = 409
    return JSONResponse({"error": str(exc)}, status_code=status)


def _bounded_int(raw: str | None, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(raw))) if raw is not None else default
    except ValueError:
        return default


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def manager_info(sup: Supervisor) -> dict[str, Any]:
    return {
        # Both keys: the legacy desktop shell waits for "mcpm" before it shows the console.
        "anx": __version__,
        "mcpm": __version__,
        "root": str(sup.cfg.root),
        "config": str(sup.cfg.path),
        "config_exists": sup.cfg.path.exists(),
        "scan": sup.cfg.scan,
        "url": sup.cfg.base_url,
        "platform": sys.platform,
        "pid": os.getpid(),
        "builtins": sup.cfg.builtins,
        "tray_visible": bool(getattr(sup, "tray_visible", False)),
        # The core is the system service (Ubuntu root / Windows LocalSystem): it starts with the
        # machine and its windows are per user; quitting it only restarts it.
        "system_service": _system_service(),
    }


def _system_service() -> bool:
    from . import accounts
    try:
        return accounts.switching_supported()
    except Exception:
        return False


def build_app(sup: Supervisor, request_exit: Callable[..., None] | None = None, lifecycle: Any = None) -> LocalOnly:
    cloud_status: dict[str, Any] = {"state": "disabled"}
    # The tray shows the gateway link as its green / grey dot (decision D7).
    sup.cloud_status = cloud_status
    from .agent import HubLink
    from . import join as joining
    hub = HubLink()  # device -> gateway requests (hub_call/1), across reconnects
    #: The cloud link task; joining or leaving a gateway restarts it (design §11.5).
    link: dict[str, Any] = {"task": None}

    async def connector():
        from . import agent as agent_module
        while True:
            try:
                if sup.cfg.path.exists():
                    await agent_module.run_configured_agent(sup.cfg.root, cloud_status, hub=hub)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # This used to end the agent silently: the tray kept
                # running while the machine stayed offline for good.
                cloud_status.update(state="error", error=type(exc).__name__)
                log.exception("cloud agent stopped (%s); restarting it in 30s", type(exc).__name__)
                await asyncio.sleep(30)

    async def stop_link() -> None:
        task = link.get("task")
        link["task"] = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait((task,), timeout=15)

    async def restart_link() -> None:
        await stop_link()
        cloud_status.clear()  # the tray holds this very dict
        cloud_status["state"] = "connecting"
        link["task"] = asyncio.create_task(connector())

    @asynccontextmanager
    async def lifespan(_app):
        link["task"] = asyncio.create_task(connector())
        tasks = []
        # desktop.update.mode = auto (the default without a desktop): check and install by itself
        from . import auto_update
        tasks.append(asyncio.create_task(auto_update.loop(sup.cfg.root)))
        if lifecycle is not None:
            tasks.append(asyncio.create_task(lifecycle.beat_loop()))
        if p2p_state.get("sessions") is not None:
            try:
                await p2p_state["sessions"].start()
            except OSError as exc:
                log.warning("session helpers unavailable: %s", exc)
        if sys.platform == "win32":
            from . import accounts as anx_accounts
            if anx_accounts.switching_supported():
                tasks.append(asyncio.create_task(follow_active_session()))
        try:
            yield
        finally:
            if link.get("task") is not None:
                tasks.append(link["task"])
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            # No orphaned tunnel processes after the manager exits.
            if p2p_state.get("sidecar") is not None:
                with contextlib.suppress(Exception):
                    await p2p_state["sidecar"].stop()
            if p2p_state.get("connector") is not None:
                p2p_state["connector"].close()
            if p2p_state.get("hub") is not None:
                with contextlib.suppress(Exception):
                    await p2p_state["hub"].close()
            if p2p_state.get("sessions") is not None:
                with contextlib.suppress(Exception):
                    await p2p_state["sessions"].stop()

    def cloud_view(info: dict) -> dict:
        """``GET /api/cloud`` (docs/ui-spec.md §4.3): the live link plus what is configured."""
        live = cloud_status.get("state", "disabled")
        state = live
        if live != "revoked" and (not info["joined"] or live == "disabled"):
            state = "not_joined"
        elif live == "disconnected":
            state = "error" if cloud_status.get("error") not in (None, "") else "connecting"
        return {**cloud_status, **info, "state": state, "link_state": live,
                "features": cloud_status.get("gateway_features", []), "display_name": cloud_status.get("display_name"),
                "hostname": joining.local_hostname()}

    async def cloud_info(_request):
        return JSONResponse(cloud_view(await run_in_threadpool(joining.describe, sup.cfg.root)))

    async def cloud_join(request: Request) -> JSONResponse:
        """``POST /api/cloud/join {invite, agent_id?}``: redeem an invite and restart the link (design §11.5)."""
        body = await json_body(request)
        if body is None or not isinstance(body.get("invite"), str):
            return JSONResponse({"error": "需要邀请码", "code": "invalid_invite"}, status_code=400)
        agent_id = body.get("agent_id") if isinstance(body.get("agent_id"), str) and body.get("agent_id").strip() else None
        live = cloud_status.get("state")
        # A link that is up (or trying) must be left first; a revoked or broken one is simply replaced.
        replace = live not in ("connected", "connecting")
        try:
            result = await run_in_threadpool(lambda: joining.join(sup.cfg.root, body["invite"],
                                                                  agent_id=agent_id and agent_id.strip(),
                                                                  replace=replace))
        except joining.JoinError as exc:
            return JSONResponse(exc.body(), status_code=exc.status)
        await restart_link()
        for _ in range(40):  # report the first outcome of the new link (connected, revoked, error)
            if cloud_status.get("state") not in ("connecting", None):
                break
            await asyncio.sleep(0.25)
        info = await run_in_threadpool(joining.describe, sup.cfg.root)
        return JSONResponse({**cloud_view(info), **result, "state": cloud_view(info)["state"]})

    async def cloud_leave(_request: Request) -> JSONResponse:
        try:
            await run_in_threadpool(joining.leave, sup.cfg.root)
        except joining.JoinError as exc:
            return JSONResponse(exc.body(), status_code=exc.status)
        await stop_link()
        hub.detach()
        cloud_status.clear()
        cloud_status["state"] = "disabled"
        return JSONResponse({"state": "not_joined"})

    from .lock import Lock, LockError
    from . import remote

    lock = Lock(sup.cfg.root)

    async def report_lock() -> None:
        from .features import REMOTE
        if hub.available and REMOTE in hub.features:
            with contextlib.suppress(Exception):
                await hub.call("lock_report", {"locked": lock.locked}, timeout=10)

    hub.on_attach.append(report_lock)

    def lock_error(exc: LockError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.status)

    async def device_admin(args: dict) -> JSONResponse:
        """The console's device page, served by the gateway (``hub_call device_admin``)."""
        from .federation import RoutingError
        if args.get("op") != "list":
            try:
                lock.require_unlocked()  # 锁定: this client may be operated on, not operate on others
            except LockError as exc:
                return lock_error(exc)
        if not hub.available:
            return JSONResponse({"error": "the gateway is not connected or does not support device administration"},
                                status_code=409)
        try:
            return JSONResponse(await hub.call("device_admin", args))
        except RoutingError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except asyncio.TimeoutError:
            return JSONResponse({"error": "the gateway did not answer"}, status_code=504)

    async def list_devices(_request: Request) -> JSONResponse:
        response = await device_admin({"op": "list", "include_offline": True})
        if response.status_code != 200:
            return response
        body = json.loads(response.body)
        me = joining.cloud_config(sup.cfg.root).get("agent_id")
        for device in body.get("devices", []):
            device["self"] = device.get("device_id") == me or device.get("agent_id") == me
        body["lock"] = lock.state()
        from .features import REMOTE
        body["remote"] = REMOTE in hub.features
        return JSONResponse(body)

    # -- 锁定 and operating on other devices (private-network model, remote/1) --
    async def lock_api(request: Request) -> JSONResponse:
        if request.method == "GET":
            return JSONResponse(lock.state())
        body = await json_body(request) or {}
        try:
            if request.method == "PUT":  # /api/lock/password
                state = await run_in_threadpool(lock.set_password, body.get("new") or None, body.get("current") or None)
            elif body.get("action") == "lock":
                state = await run_in_threadpool(lock.lock)
            elif body.get("action") == "unlock":
                state = await run_in_threadpool(lock.unlock, body.get("password") or None)
            else:
                return JSONResponse({"error": "action must be lock or unlock"}, status_code=400)
        except LockError as exc:
            return lock_error(exc)
        await report_lock()
        return JSONResponse(state)

    # -- 执行账户: who plugins and remote terminals run as, when the core is root (accounts.py) --
    def restart_running_plugins() -> None:
        """Running plugins start again as the account they should run as now."""
        for name in list(sup.specs):
            try:
                if sup._alive(sup.rt[name]):
                    sup.restart(name)
            except Exception as exc:  # one plugin failing must not stop the rest
                log.warning("restart %s after the account change: %s", name, exc)

    async def follow_active_session() -> None:
        """Windows system service: plugins follow the user at the screen (log on, log off, switch user)."""
        from . import accounts as anx_accounts, winsession
        last = await asyncio.to_thread(winsession.active_session)
        if last is not None:  # an update closed the windows: bring back the one at the screen
            await asyncio.to_thread(winsession.ensure_window, os.environ.get("ANX_GUI_EXE"), last["uid"])
        while True:
            await asyncio.sleep(5)
            try:
                now = await asyncio.to_thread(winsession.active_session)
                if (now or {}).get("uid") != (last or {}).get("uid"):
                    log.info("the active session changed (%s -> %s)", (last or {}).get("user"), (now or {}).get("user"))
                    last = now
                    if anx_accounts.configured(sup.cfg.root).get("mcp", anx_accounts.ACTIVE).upper() != "SYSTEM":
                        await run_in_threadpool(restart_running_plugins)
            except Exception as exc:
                log.warning("following the active session: %s", exc)

    async def accounts_api(request: Request) -> JSONResponse:
        from . import accounts as anx_accounts
        if request.method == "PUT":
            if not anx_accounts.switching_supported():
                return JSONResponse({"error": "只有以系统服务（root）运行的 Linux 核心可以切换执行账户"}, status_code=409)
            body = await json_body(request) or {}
            changes = {key: body[key] for key in ("default", *anx_accounts.PURPOSES) if key in body}
            try:
                before = anx_accounts.configured(sup.cfg.root)
                after = await run_in_threadpool(anx_accounts.save, sup.cfg.root, changes)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            if before.get("mcp") != after.get("mcp"):
                await run_in_threadpool(restart_running_plugins)  # running plugins move to the new account now
        return JSONResponse({"supported": anx_accounts.switching_supported(),
                             "accounts": anx_accounts.configured(sup.cfg.root),
                             "users": await run_in_threadpool(anx_accounts.local_users)})

    async def remote_terminal_info(request: Request) -> JSONResponse:
        try:
            lock.require_unlocked()
            return JSONResponse(await remote.terminal_info(sup.cfg.root, request.path_params["device_id"],
                                                           refresh=request.query_params.get("refresh") == "1"))
        except LockError as exc:
            return lock_error(exc)
        except remote.RemoteError as exc:
            return JSONResponse(exc.body(), status_code=exc.status)

    async def remote_terminal_socket(socket) -> None:
        await socket.accept()
        try:
            lock.require_unlocked()
        except LockError as exc:
            await socket.send_text(json.dumps({"type": "error", "error": exc.message}, ensure_ascii=False))
            await socket.close(code=4423)
            return
        device_id = socket.path_params["device_id"]
        query = dict(socket.query_params)
        relay_note = "网关中转"
        connector = p2p_state["connector"]
        if connector is not None and query.get("route") != "relay":
            # Direct first: a ticket from the device, then the P2P tunnel to its remote hub.
            try:
                config = await p2p_config()
                if config is None:
                    raise RuntimeError("网关未开启直连")
                ticket = await hub.call("remote_ticket", {"device_id": device_id, "service": "terminal"}, timeout=20)
                _origin, agent_id, token = remote.credentials(sup.cfg.root)
                port, route = await connector.port_for(config, ticket["server_id"], remote.headers(agent_id, token))
                await remote.p2p_terminal(socket, port, ticket["ticket"], query, route)
                return
            except Exception as exc:  # any failure: the relay still works
                log.info("p2p terminal to %s unavailable (%s); using the gateway relay", device_id, exc)
                relay_note = f"网关中转（直连失败：{str(exc)[:80]}）"
                connector.forget(device_id)
        try:
            await remote.proxy_terminal(socket, sup.cfg.root, device_id, query, relay_note)
        except remote.RemoteError as exc:
            with contextlib.suppress(Exception):
                await socket.send_text(json.dumps({"type": "error", "error": exc.message}, ensure_ascii=False))
                await socket.close(code=4404)

    async def p2p_link(device_id: str, service: str) -> tuple[int, str, str]:
        """(loopback port, route, ticket) for a stream to ``device_id``'s remote hub."""
        connector = p2p_state["connector"]
        if connector is None:
            raise remote.RemoteError(409, "这台电脑缺少直连组件（muxi-p2p-agent）", "no_p2p")
        config = await p2p_config()
        if config is None:
            raise remote.RemoteError(409, "网关未开启直连（需要 0.5.0 以上）", "no_p2p")
        ticket = await hub.call("remote_ticket", {"device_id": device_id, "service": service}, timeout=20)
        _origin, agent_id, token = remote.credentials(sup.cfg.root)
        try:
            port, route = await connector.port_for(config, ticket["server_id"], remote.headers(agent_id, token))
        except (RuntimeError, OSError, asyncio.TimeoutError) as exc:
            connector.forget(ticket["server_id"])
            raise remote.RemoteError(502, f"无法直连：{exc}", "p2p_failed") from None
        return port, route, ticket["ticket"]

    async def remote_desktop_prepare(request: Request) -> JSONResponse:
        """Build (or reuse) the tunnel first, so the page can say which route it got."""
        from .federation import RoutingError
        try:
            lock.require_unlocked()
            connector = p2p_state["connector"]
            if connector is None:
                raise remote.RemoteError(409, "这台电脑缺少直连组件（muxi-p2p-agent）", "no_p2p")
            config = await p2p_config()
            if config is None:
                raise remote.RemoteError(409, "网关未开启直连（需要 0.5.0 以上）", "no_p2p")
            _origin, agent_id, token = remote.credentials(sup.cfg.root)
            device_id = request.path_params["device_id"]
            try:
                _port, route = await connector.port_for(config, device_id, remote.headers(agent_id, token))
            except (RuntimeError, OSError, asyncio.TimeoutError) as exc:
                connector.forget(device_id)
                raise remote.RemoteError(502, f"无法直连：{exc}", "p2p_failed") from None
            return JSONResponse({"route": route})
        except LockError as exc:
            return lock_error(exc)
        except remote.RemoteError as exc:
            return JSONResponse(exc.body(), status_code=exc.status)
        except RoutingError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    async def remote_desktop_socket(socket) -> None:
        from .federation import RoutingError
        protocols = socket.scope.get("subprotocols") or []
        await socket.accept(subprotocol="binary" if "binary" in protocols else None)
        try:
            lock.require_unlocked()
            port, _route, ticket = await p2p_link(socket.path_params["device_id"], "desktop")
        except (LockError, remote.RemoteError) as exc:
            await socket.close(code=4423 if isinstance(exc, LockError) else 4404, reason=exc.message[:120])
            return
        except RoutingError as exc:
            await socket.close(code=4404, reason=str(exc)[:120])
            return
        await remote.p2p_desktop(socket, port, ticket)

    async def remote_terminal_external(request: Request) -> JSONResponse:
        body = await json_body(request) or {}
        try:
            lock.require_unlocked()
            remote.credentials(sup.cfg.root)  # joined, with a readable credential
            shell = body.get("shell") if isinstance(body.get("shell"), str) and len(body.get("shell")) <= 64 else None
            title = body.get("title") if isinstance(body.get("title"), str) else None
            result = await run_in_threadpool(remote.open_external, sup.cfg.root, request.path_params["device_id"],
                                             shell=shell, title=title)
        except LockError as exc:
            return lock_error(exc)
        except remote.RemoteError as exc:
            return JSONResponse(exc.body(), status_code=exc.status)
        except OSError as exc:
            return JSONResponse({"error": f"无法打开终端：{exc}"}, status_code=500)
        return JSONResponse(result)

    async def delete_device(request: Request) -> JSONResponse:
        return await device_admin({"op": "delete", "device_id": request.path_params["device_id"]})

    async def revoke_device(request: Request) -> JSONResponse:
        return await device_admin({"op": "revoke", "device_id": request.path_params["device_id"]})

    async def invites_api(request: Request) -> JSONResponse:
        """The local console's invite list and "邀请新设备" (``hub_call device_admin``, design §11.5)."""
        if request.method == "GET":
            return await device_admin({"op": "invite_list", "state": request.query_params.get("state", "unused")})
        if request.method == "DELETE":
            return await device_admin({"op": "invite_revoke", "invite_id": request.path_params["invite_id"]})
        body = await json_body(request)
        if body is None:
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        return await device_admin({"op": "invite_create", "label": body.get("label"), "ttl_s": body.get("ttl_s"),
                                   "agent_id": body.get("agent_id")})

    async def put_device_alias(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        if not isinstance(body, dict) or "alias" not in body:
            return JSONResponse({"error": "body must be an object with an alias (string or null)"}, status_code=400)
        return await device_admin({"op": "set_alias", "device_id": request.path_params["device_id"],
                                   "alias": body["alias"]})

    # -- the inbox page and asks (decision D11, design §8.12), served by the gateway --
    async def gateway_call(op: str, args: dict, feature: str) -> JSONResponse:
        from .federation import RoutingError
        if not hub.available or feature not in hub.features:
            return JSONResponse({"error": "the gateway is not connected or has no inbox"}, status_code=409)
        try:
            return JSONResponse(await hub.call(op, args))
        except RoutingError as exc:
            status = 404 if str(exc).startswith("unknown ") else 400
            return JSONResponse({"error": str(exc)}, status_code=status)
        except asyncio.TimeoutError:
            return JSONResponse({"error": "the gateway did not answer"}, status_code=504)

    async def json_body(request: Request) -> dict | None:
        try:
            body = await request.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    async def inbox_api(request: Request) -> JSONResponse:
        name = request.scope["route_name"]
        params, query = request.path_params, request.query_params
        args: dict[str, Any] = {}
        if "viewer" in query:
            args["viewer"] = query["viewer"]
        if request.method in ("POST", "PUT"):
            body = await json_body(request)
            if body is None:
                return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        else:
            body = {}
        if name == "thread":
            args.update(thread_id=params["thread_id"], mark_read=query.get("mark_read", "1") not in ("0", "false"))
        elif name == "send":
            args.update(body)
        elif name == "asks":
            args["state"] = query.get("state", "open")
        elif name == "ask":
            args["ask_id"] = params["ask_id"]
        elif name == "answer":
            args.update(body, ask_id=params["ask_id"])
        elif name == "addresses":
            args["include_ended"] = query.get("include_ended") in ("1", "true")
        elif name == "set_address":
            args.update(address=params["address"], fields=body)
        args["op"] = name
        return await gateway_call("mail_local", args, "inbox/1")

    def inbox_route(path: str, name: str, methods: list[str]) -> Route:
        async def endpoint(request: Request, name=name):
            request.scope["route_name"] = name
            return await inbox_api(request)
        return Route(path, endpoint, methods=methods)

    async def asks_status(_request: Request) -> JSONResponse:
        """Whether the device plugin's request_user_input can go through the gateway."""
        return JSONResponse({"available": hub.available and "ask/1" in hub.features})

    async def asks_create(request: Request) -> JSONResponse:
        body = await json_body(request)
        if body is None:
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        allowed = {"question", "title", "kind", "options", "allow_custom", "recommended", "context"}
        return await gateway_call("ask_create", {**{k: v for k, v in body.items() if k in allowed},
                                                 "origin": "request_user_input"}, "ask/1")

    async def agents_presence(request: Request) -> JSONResponse:
        """``anx hook claude|codex``: a session's id and prompt time (design §8.3 source 1).

        Recorded locally for ``session_probe`` and reported to the gateway
        (``hub_call presence_report``) when it takes them (``wake/1``).
        """
        body = await json_body(request)
        if body is None or body.get("kind") not in ("claude", "codex") or not isinstance(body.get("cwd"), str):
            return JSONResponse({"error": "body needs kind (claude or codex) and cwd"}, status_code=400)
        session_id = body.get("session_id") if isinstance(body.get("session_id"), str) else None
        event = str(body.get("event") or "")[:64]
        from .link.wake import probe as wake_probe
        if event in ("prompt", "UserPromptSubmit", "SessionStart", "turn"):
            wake_probe.hook_seen(body["kind"], body["cwd"], session_id)
        args = {"kind": body["kind"], "cwd": body["cwd"][:1000], "session_id": session_id, "event": event}
        if isinstance(body.get("title"), str) and body["title"].strip():
            # The session title (design §8.3); the gateway cleans and clips it.
            args["title"] = body["title"][:200]
            args["title_source"] = "prompt" if body.get("title_source") == "prompt" else "session"
        return await gateway_call("presence_report", args, "wake/1")

    async def asks_get(request: Request) -> JSONResponse:
        return await gateway_call("ask_get", {"ask_id": request.path_params["ask_id"]}, "ask/1")

    async def toast_answer(request: Request):
        """A toast button: a signed link that answers the question (via: toast)."""
        from starlette.responses import HTMLResponse
        from . import toast as toast_mod
        import html as html_mod
        ask_id, choice = request.query_params.get("ask", ""), request.query_params.get("choice", "")
        secret = await run_in_threadpool(toast_mod.link_secret, sup.cfg.state_dir)
        if not ask_id or not toast_mod.verify(secret, ask_id, choice, request.query_params.get("sig", "")):
            message = "这个回答链接无效；请在收件箱里作答。"
        else:
            response = await gateway_call("ask_answer", {"ask_id": ask_id, "choice": choice, "via": "toast"}, "ask/1")
            data = json.loads(response.body)
            if response.status_code != 200:
                message = f"没有记录回答：{data.get('error', '')}"
            elif data.get("already"):
                message = data.get("message") or "这个问题已经回答过了。"
            else:
                message = f"已回答：{choice}"
        page = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" '
                'content="width=device-width,initial-scale=1"><title>agent-nexus</title>'
                f'<p>{html_mod.escape(message)}</p><p><a href="/#/inbox">打开收件箱</a></p></html>')
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    async def inbox_js(request: Request) -> FileResponse:
        return FileResponse(STATIC_DIR / "inbox.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})
    async def index(request: Request) -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    async def favicon(request: Request) -> FileResponse:
        return FileResponse(STATIC_DIR / "icon.png", media_type="image/png", headers={"Cache-Control": "max-age=3600"})

    async def ping(request: Request) -> JSONResponse:
        return JSONResponse(manager_info(sup))

    async def shutdown(request: Request) -> JSONResponse:
        if request_exit is None:
            return JSONResponse({"error": "this manager has no shutdown hook"}, status_code=409)
        # Answer first; the server stops after the response has been sent.
        return JSONResponse({"message": "mcpm stopping; managed servers are stopping too"},
                            background=BackgroundTask(request_exit, "console API /api/manager/shutdown"))

    async def list_services(request: Request) -> JSONResponse:
        services = await run_in_threadpool(sup.snapshots)
        update = await run_in_threadpool(desktop.read_update, sup.cfg.root)
        return JSONResponse({"manager": {**manager_info(sup), "cloud": dict(cloud_status), "app_update": update},
                             "services": services})

    # -- the desktop shell (design sections 9.2, 9.4, 9.6): read what it records,
    # ask it to act through `agent-nexus.exe <verb>` --
    async def app_update(_request: Request) -> JSONResponse:
        return JSONResponse(await run_in_threadpool(desktop.update_status, sup.cfg.root))

    # -- 连接器: local MCP servers agent-nexus does not run, offered to the gateway --
    from . import local_connectors

    async def connectors_api(request: Request) -> JSONResponse:
        name = request.path_params.get("name")
        try:
            if request.method == "GET":
                return JSONResponse({"connectors": await run_in_threadpool(local_connectors.list_connectors, sup.cfg.root)})
            if request.method == "DELETE":
                await run_in_threadpool(local_connectors.delete, sup.cfg.root, name)
                return JSONResponse({"deleted": name})
            body = await json_body(request)
            if body is None:
                return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
            return JSONResponse({"connector": await run_in_threadpool(local_connectors.save, sup.cfg.root, name, body)})
        except local_connectors.ConnectorError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)

    async def connector_test(request: Request) -> JSONResponse:
        try:
            return JSONResponse(await local_connectors.test(sup.cfg.root, request.path_params["name"]))
        except local_connectors.ConnectorError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)

    async def app_update_mode(request: Request) -> JSONResponse:
        """``PUT /api/app/update/mode`` {mode: auto|notify|off}: the 自动更新 switch."""
        from . import auto_update
        body = await json_body(request) or {}
        try:
            mode = await run_in_threadpool(auto_update.set_mode, sup.cfg.root, str(body.get("mode") or ""))
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"mode": mode})

    async def app_update_verb(request: Request) -> JSONResponse:
        verb = "--apply-update" if request.url.path.endswith("/apply") else "--check-update"
        from . import linux_update
        if desktop.desktop_app() is None and linux_update.supported():
            # The Ubuntu package: check through the gateway, install with pkexec apt-get.
            try:
                if verb == "--check-update":
                    return JSONResponse({"started": "check-update",
                                         **await run_in_threadpool(linux_update.check, sup.cfg.root)})
                return JSONResponse(await run_in_threadpool(linux_update.apply, sup.cfg.root))
            except linux_update.UpdateError as exc:
                return JSONResponse({"error": str(exc)}, status_code=409)
        if verb == "--apply-update" and not desktop.read_update(sup.cfg.root).get("available"):
            return JSONResponse({"error": "no update is known; check for updates first"}, status_code=409)
        if not await run_in_threadpool(desktop.run_verb, verb):
            return JSONResponse({"error": "the Agent Nexus desktop app was not found"}, status_code=409)
        return JSONResponse({"started": verb.removeprefix("--")})

    async def app_autostart(request: Request) -> JSONResponse:
        if _system_service():
            # The system service starts with the machine, and the window at every logon.
            return JSONResponse({"enabled": True, "system": True})
        if request.method == "PUT":
            body = await json_body(request)
            if body is None or not isinstance(body.get("enabled"), bool):
                return JSONResponse({"error": "body must be {\"enabled\": true|false}"}, status_code=400)
            switch = "on" if body["enabled"] else "off"
            if not await run_in_threadpool(desktop.run_verb, f"--set-autostart={switch}"):
                return JSONResponse({"error": "the Agent Nexus desktop app was not found"}, status_code=409)
            for _ in range(20):  # the shell writes the Run value; report what it did
                if await run_in_threadpool(desktop.autostart_enabled) == body["enabled"]:
                    break
                await asyncio.sleep(0.25)
        return JSONResponse({"enabled": await run_in_threadpool(desktop.autostart_enabled)})

    async def rescan(request: Request) -> JSONResponse:
        await run_in_threadpool(sup.rescan)
        return JSONResponse({"services": await run_in_threadpool(sup.snapshots)})

    # -- plugins (design §6.5, docs/ui-spec.md §4.2): the user view of /api/services --
    from .plugins import installer as plugin_installer, manage as plugin_manage
    jobs = plugin_manage.Jobs(sup)

    def plugin_failure(exc: Exception) -> JSONResponse:
        if isinstance(exc, plugin_manage.PluginError):
            return JSONResponse({"error": str(exc), **exc.extra}, status_code=exc.status)
        if isinstance(exc, plugin_installer.InstallError):
            status = 404 if exc.code == "unknown" else 409 if exc.code in ("builtin", "not_managed", "busy", "name_taken") else 400
            return JSONResponse({"error": str(exc), "code": exc.code}, status_code=status)
        return _error(exc)

    async def plugins_list(_request: Request) -> JSONResponse:
        return JSONResponse({"plugins": await run_in_threadpool(plugin_manage.plugin_views, sup)})

    # -- the gateway's plugin repository (插件库, design §6.12) --
    from .plugins import hub_source

    async def repo_install_rpc(params: dict) -> dict:
        """Reverse RPC ``plugin_repo_install`` from the gateway console's "安装到…"."""
        try:
            job_id = jobs.start_hub(str(params.get("name") or ""), str(params.get("version") or ""),
                                    sha256=str(params.get("sha256") or ""), path=str(params.get("path") or ""),
                                    enable=params.get("enable", True) is not False, origin="gateway",
                                    size=params.get("size"))
        except plugin_manage.PluginError as exc:
            return {"error": str(exc), "code": "invalid_request"}
        return {"job_id": job_id}

    async def repo_job_rpc(params: dict) -> dict:
        """Reverse RPC ``plugin_repo_job``: the progress of an install the gateway started."""
        job = jobs.get(str(params.get("job_id") or ""), _bounded_int(str(params.get("since", 0)), 0, 0, 10**6))
        return job if job is not None else {"error": "没有这个安装任务（设备重启后任务记录会清空）"}

    hub.handlers.update(plugin_repo_install=repo_install_rpc, plugin_repo_job=repo_job_rpc)
    hub.extra_features = ("plugin_repo/1",)

    # -- remote terminal (term/1): the gateway console's 终端 and `agent-nexus term` --
    from . import terminal as anx_terminal

    if getattr(sup.cfg, "terminal", True) and anx_terminal.available():
        from . import accounts as anx_accounts
        terminals = anx_terminal.Terminals(account=lambda: anx_accounts.effective(sup.cfg.root, "terminal"))
        hub.handlers.update(terminals.handlers())
        hub.extra_features = (*hub.extra_features, anx_terminal.FEATURE)

    # -- P2P tunnels (muxi-p2p): this device's remote hub + tunnel agent, and connect-side links --
    from . import p2p as p2p_mod
    from .features import P2P as P2P_FEATURE
    from .remote_hub import FEATURE as REMOTE_HUB_FEATURE, RemoteHub

    p2p_exe = p2p_mod.binary(sup.cfg.root)
    p2p_state: dict[str, Any] = {"config": None, "sidecar": None, "connector": None, "hub": None, "sessions": None}
    if p2p_exe and getattr(sup.cfg, "terminal", True) and anx_terminal.available():
        from . import vnc
        desktop_ok, desktop_why = vnc.available()
        from . import session_helper
        desktop_service = None
        if getattr(sup.cfg, "desktop", True):
            if session_helper.supported() and session_helper.graphical_target():
                # The core is the root system service: the person's own session shares the screen.
                desktop_service = p2p_state["sessions"] = session_helper.SessionRegistry(root=sup.cfg.root)
            elif desktop_ok:
                desktop_service = vnc.DesktopService(joining.local_hostname())
        if desktop_service is None:
            log.info("remote desktop not offered here: %s", desktop_why or "turned off")
        remote_hub = RemoteHub(terminals, desktop=desktop_service)
        if desktop_service is not None:
            hub.extra_features = (*hub.extra_features, "desktop/1")
        p2p_state["hub"] = remote_hub
        hub.handlers.update(remote_hub.handlers())
        hub.extra_features = (*hub.extra_features, REMOTE_HUB_FEATURE)
        p2p_state["sidecar"] = p2p_mod.AgentSidecar(p2p_exe)
    if p2p_exe:
        p2p_state["connector"] = p2p_mod.Connector(p2p_exe)

    async def p2p_config() -> dict | None:
        if not (hub.available and P2P_FEATURE in hub.features):
            return None
        if p2p_state["config"] is None:
            p2p_state["config"] = await hub.call("p2p_config", {}, timeout=20)
        return p2p_state["config"]

    async def start_tunnel_agent() -> None:
        remote_hub, sidecar = p2p_state["hub"], p2p_state["sidecar"]
        if remote_hub is None or sidecar is None:
            return
        p2p_state["config"] = None  # a reconnect may mean another gateway
        try:
            config = await p2p_config()
            if config is None:
                return
            if not remote_hub.port:
                await remote_hub.start()
            sidecar.apply(config, remote_hub.port)
        except Exception as exc:
            log.warning("p2p tunnel agent not started: %s", exc)

    hub.on_attach.append(start_tunnel_agent)

    async def repo_listing() -> tuple[dict | None, JSONResponse | None]:
        try:
            hub_source.link(sup.cfg.root)
        except hub_source.HubError as exc:
            if exc.code == "not_joined":  # the install dialog then has no 插件库 tab
                return None, JSONResponse({"available": False, "packages": [], **exc.body()}, status_code=exc.status)
        if not hub.available:
            return None, JSONResponse({"available": False, "packages": [], "code": "not_connected",
                                       "error": "没有连上网关：连上之后才能看到插件库"}, status_code=409)
        if "plugin_repo/1" not in hub.features:
            return None, JSONResponse({"available": False, "packages": [], "code": "unsupported_gateway",
                                       "error": "网关版本太旧，还没有插件库"}, status_code=409)
        views = await run_in_threadpool(plugin_manage.plugin_views, sup)
        try:
            return await run_in_threadpool(hub_source.packages, sup.cfg.root, views), None
        except hub_source.HubError as exc:
            return None, JSONResponse({"available": False, "packages": [], **exc.body()}, status_code=exc.status)

    async def repo_list(_request: Request) -> JSONResponse:
        data, failure = await repo_listing()
        return failure if failure is not None else JSONResponse(data)

    async def repo_install(request: Request) -> JSONResponse:
        """``POST /api/plugin-repo/install {name, version?}``: install from the 插件库 on this device."""
        body = await json_body(request)
        if body is None or not isinstance(body.get("name"), str):
            return JSONResponse({"error": "需要插件名"}, status_code=400)
        data, failure = await repo_listing()
        if failure is not None:
            return failure
        matches = [p for p in data["packages"] if p["name"] == body["name"]
                   and (not body.get("version") or p["version"] == body["version"])]
        if body.get("version") is None:
            matches = [p for p in matches if p.get("latest")] or matches
        if not matches:
            return JSONResponse({"error": f"插件库里没有 {body['name']} {body.get('version') or ''}".strip(),
                                 "code": "unknown_package"}, status_code=404)
        package = matches[0]
        try:
            job_id = jobs.start_hub(package["name"], package["version"], sha256=package["sha256"],
                                    path=f"/api/plugin-repo/{package['name']}/{package['version']}/download",
                                    enable=body.get("enable", True) is not False, size=package.get("size"))
        except plugin_manage.PluginError as exc:
            return plugin_failure(exc)
        return JSONResponse({"job_id": job_id, "name": package["name"], "version": package["version"]}, status_code=202)

    async def plugin_get(request: Request) -> JSONResponse:
        try:
            return JSONResponse({"plugin": await run_in_threadpool(plugin_manage.plugin_view, sup, request.path_params["name"])})
        except SupervisorError as exc:
            return _error(exc)

    async def plugin_upload(request: Request) -> JSONResponse:
        """``POST /api/plugins/uploads?filename=x.zip`` with the archive as the body → ``{upload_id}``."""
        data = await request.body()
        if not data:
            return JSONResponse({"error": "没有收到文件"}, status_code=400)
        try:
            upload_id = await run_in_threadpool(jobs.save_upload, request.query_params.get("filename", ""), data)
        except plugin_manage.PluginError as exc:
            return plugin_failure(exc)
        return JSONResponse({"upload_id": upload_id, "size": len(data)})

    async def plugin_suggestions(_request: Request) -> JSONResponse:
        return JSONResponse({"folders": await run_in_threadpool(plugin_manage.suggestions, sup.cfg.root)})

    async def plugin_inspect(request: Request) -> JSONResponse:
        body = await json_body(request)
        if body is None:
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        try:
            source = plugin_installer.source_from_body(body, jobs.uploads_dir())
            return JSONResponse(await run_in_threadpool(plugin_installer.inspect, sup.cfg.root, source))
        except (plugin_installer.InstallError, plugin_manage.PluginError) as exc:
            return plugin_failure(exc)

    async def plugin_install(request: Request) -> JSONResponse:
        body = await json_body(request)
        if body is None:
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        try:
            source = plugin_installer.source_from_body(body, jobs.uploads_dir())
            port = body.get("port")
            port = int(port) if port not in (None, "") else None
        except plugin_installer.InstallError as exc:
            return plugin_failure(exc)
        except (TypeError, ValueError):
            return JSONResponse({"error": "端口必须是数字"}, status_code=400)
        job_id = jobs.start(source, enable=body.get("enable", True) is not False, port=port)
        return JSONResponse({"job_id": job_id}, status_code=202)

    async def plugin_job(request: Request) -> JSONResponse:
        job = jobs.get(request.path_params["job_id"], _bounded_int(request.query_params.get("since"), 0, 0, 10**6))
        if job is None:
            return JSONResponse({"error": "没有这个安装任务"}, status_code=404)
        return JSONResponse(job)

    async def plugin_act(request: Request) -> JSONResponse:
        name, action = request.path_params["name"], request.path_params["action"]
        try:
            if action in ("enable", "disable"):
                return JSONResponse(await run_in_threadpool(plugin_manage.set_enabled, sup, name, action == "enable"))
            if action not in ACTIONS:
                return JSONResponse({"error": f"unknown action: {action}"}, status_code=404)
            kwargs = {} if action == "start" else {"takeover": request.query_params.get("takeover") in ("1", "true")}
            message = await run_in_threadpool(getattr(sup, action), name, **kwargs)
            await run_in_threadpool(sup.tick)
            return JSONResponse({"message": message, "plugin": await run_in_threadpool(plugin_manage.plugin_view, sup, name)})
        except (SupervisorError, plugin_manage.PluginError) as exc:
            return plugin_failure(exc)

    async def plugins_enable_many(request: Request) -> JSONResponse:
        body = await json_body(request)
        names = body.get("names") if body else None
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            return JSONResponse({"error": "body must be {\"names\": [...]}"}, status_code=400)
        done, errors = [], []
        for name in names:
            try:
                done.append((await run_in_threadpool(plugin_manage.set_enabled, sup, name, True))["plugin"])
            except (SupervisorError, plugin_manage.PluginError) as exc:
                errors.append({"name": name, "error": str(exc)})
        return JSONResponse({"plugins": done, "errors": errors})

    async def plugin_config(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            if request.method == "GET":
                return JSONResponse(await run_in_threadpool(plugin_manage.get_config, sup, name))
            body = await json_body(request)
            if body is None:
                return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
            return JSONResponse(await run_in_threadpool(plugin_manage.put_config, sup, name, body))
        except (SupervisorError, plugin_manage.PluginError) as exc:
            return plugin_failure(exc)

    async def plugin_delete(request: Request) -> JSONResponse:
        purge = request.query_params.get("purge") in ("1", "true")
        try:
            return JSONResponse(await run_in_threadpool(jobs.uninstall, request.path_params["name"], purge))
        except (plugin_installer.InstallError, SupervisorError) as exc:
            return plugin_failure(exc)

    async def local_page(_request: Request) -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


    async def get_service(request: Request) -> JSONResponse:
        try:
            return JSONResponse(await run_in_threadpool(sup.snapshot, request.path_params["name"]))
        except SupervisorError as exc:
            return _error(exc)

    async def get_icon(request: Request) -> FileResponse | JSONResponse:
        try:
            spec = sup.spec(request.path_params["name"])
        except SupervisorError as exc:
            return _error(exc)
        path, problem = await run_in_threadpool(spec.icon_file)
        if path is None:
            return JSONResponse({"error": problem or "no icon"}, status_code=404)
        return FileResponse(path, media_type=ICON_TYPES[path.suffix.lower()], headers=ICON_HEADERS)

    async def act(request: Request) -> JSONResponse:
        name, action = request.path_params["name"], request.path_params["action"]
        if action not in ACTIONS:
            return JSONResponse({"error": f"unknown action: {action}"}, status_code=404)
        kwargs = {} if action == "start" else {"takeover": request.query_params.get("takeover") in ("1", "true")}
        try:
            message = await run_in_threadpool(getattr(sup, action), name, **kwargs)
            await run_in_threadpool(sup.tick)
            return JSONResponse({"message": message, "service": await run_in_threadpool(sup.snapshot, name)})
        except SupervisorError as exc:
            return _error(exc)

    async def put_config(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            spec = sup.spec(name)
            body = await request.json()
        except SupervisorError as exc:
            return _error(exc)
        except ValueError:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        if not isinstance(body, dict) or not body:
            return JSONResponse({"error": "body must be a non-empty object"}, status_code=400)
        if spec.observed and set(body) - OBSERVE_EDITABLE_FIELDS:
            return JSONResponse({"error": f"{name} is observe-only: only 'enabled' can be changed"}, status_code=409)
        for key, value in body.items():
            kind = CONFIG_FIELDS.get(key)
            if kind is None:
                return JSONResponse({"error": f"not editable here: {key}"}, status_code=400)
            if kind is bool and not isinstance(value, bool):
                return JSONResponse({"error": f"{key} must be true or false"}, status_code=400)
            if kind is int and (isinstance(value, bool) or not isinstance(value, (int, str))):
                return JSONResponse({"error": f"{key} must be a number"}, status_code=400)
        try:
            await run_in_threadpool(save_override, sup.cfg, name, dict(body))
            await run_in_threadpool(sup.rescan)
            await run_in_threadpool(sup.tick)
            snap = await run_in_threadpool(sup.snapshot, name)
        except (RegistryError, SupervisorError) as exc:
            return _error(exc)
        return JSONResponse({"service": snap, "restart_needed": bool(snap.get("pending_restart"))})

    async def get_logs(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            path = sup.log_path(name, request.query_params.get("source", "stdout"))
        except SupervisorError as exc:
            return _error(exc)
        lines = _bounded_int(request.query_params.get("lines"), 400, 1, 5000)
        text, offset = await run_in_threadpool(logs.tail, path, lines)
        return JSONResponse({"path": str(path), "text": text, "offset": offset})

    async def stream_logs(request: Request) -> StreamingResponse | JSONResponse:
        name = request.path_params["name"]
        try:
            path = sup.log_path(name, request.query_params.get("source", "stdout"))
        except SupervisorError as exc:
            return _error(exc)
        lines = _bounded_int(request.query_params.get("lines"), 400, 1, 5000)

        def size() -> int:
            try:
                return path.stat().st_size
            except OSError:
                return 0

        async def events():
            text, offset = await run_in_threadpool(logs.tail, path, lines)
            yield _sse({"reset": True, "text": text, "offset": offset, "path": str(path)})
            quiet = 0.0
            while not await request.is_disconnected():
                if await run_in_threadpool(size) < offset:  # rotated or truncated
                    text, offset = await run_in_threadpool(logs.tail, path, lines)
                    yield _sse({"reset": True, "text": text, "offset": offset, "path": str(path)})
                    quiet = 0.0
                    continue
                text, offset = await run_in_threadpool(logs.read_from, path, offset)
                if text:
                    yield _sse({"text": text, "offset": offset})
                    quiet = 0.0
                else:
                    quiet += 0.5
                    if quiet >= 15:
                        yield ": keep-alive\n\n"
                        quiet = 0.0
                await asyncio.sleep(0.5)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    routes = [
        Route("/", index),
        Route("/favicon.png", favicon),
        Route("/favicon.ico", favicon),
        Route("/api/ping", ping),
        Route("/api/cloud", cloud_info),
        Route("/api/cloud/join", cloud_join, methods=["POST"]),
        Route("/api/cloud/leave", cloud_leave, methods=["POST"]),
        Route("/cloud", local_page),
        Route("/api/devices", list_devices),
        Route("/api/devices/{device_id}/alias", put_device_alias, methods=["PUT"]),
        Route("/api/devices/{device_id}/revoke", revoke_device, methods=["POST"]),
        Route("/api/devices/{device_id}", delete_device, methods=["DELETE"]),
        Route("/api/invites", invites_api, methods=["GET", "POST"]),
        Route("/api/invites/{invite_id}", invites_api, methods=["DELETE"]),
        Route("/inbox.js", inbox_js),
        ui_route(),  # the shared design system (docs/ui-spec.md)
        static_dir_route("local", STATIC_DIR / "local"),  # the local console's pages
        Route("/toast/answer", toast_answer),
        inbox_route("/api/inbox/summary", "summary", ["GET"]),
        inbox_route("/api/inbox/threads", "threads", ["GET"]),
        inbox_route("/api/inbox/threads/{thread_id}", "thread", ["GET"]),
        inbox_route("/api/inbox/send", "send", ["POST"]),
        inbox_route("/api/inbox/asks", "asks", ["GET"]),
        inbox_route("/api/inbox/asks/{ask_id}", "ask", ["GET"]),
        inbox_route("/api/inbox/asks/{ask_id}/answer", "answer", ["POST"]),
        inbox_route("/api/inbox/addresses", "addresses", ["GET"]),
        inbox_route("/api/inbox/addresses/{address}", "set_address", ["PUT"]),
        Route("/api/agents/presence", agents_presence, methods=["POST"]),
        Route("/api/asks/status", asks_status),
        Route("/api/asks", asks_create, methods=["POST"]),
        Route("/api/asks/{ask_id}", asks_get),
        Route("/api/manager/shutdown", shutdown, methods=["POST"]),
        Route("/api/app/update", app_update),
        Route("/api/app/update/check", app_update_verb, methods=["POST"]),
        Route("/api/app/update/apply", app_update_verb, methods=["POST"]),
        Route("/api/app/update/mode", app_update_mode, methods=["PUT"]),
        Route("/api/local-connectors", connectors_api, methods=["GET"]),
        Route("/api/local-connectors/{name}", connectors_api, methods=["PUT", "DELETE"]),
        Route("/api/local-connectors/{name}/test", connector_test, methods=["POST"]),
        Route("/api/app/autostart", app_autostart, methods=["GET", "PUT"]),
        Route("/api/services", list_services),
        Route("/api/rescan", rescan, methods=["POST"]),
        Route("/api/services/{name}", get_service),
        Route("/api/services/{name}/config", put_config, methods=["PUT"]),
        Route("/api/services/{name}/icon", get_icon),
        Route("/api/services/{name}/logs", get_logs),
        Route("/api/services/{name}/logs/stream", stream_logs),
        Route("/api/services/{name}/{action}", act, methods=["POST"]),
        Route("/api/plugins", plugins_list),
        Route("/api/plugin-repo", repo_list),
        Route("/api/plugin-repo/install", repo_install, methods=["POST"]),
        Route("/api/plugins/uploads", plugin_upload, methods=["POST"]),
        Route("/api/plugins/inspect", plugin_inspect, methods=["POST"]),
        Route("/api/plugins/install", plugin_install, methods=["POST"]),
        Route("/api/plugins/enable", plugins_enable_many, methods=["POST"]),
        Route("/api/plugins/suggestions", plugin_suggestions),
        Route("/api/plugins/jobs/{job_id}", plugin_job),
        Route("/api/plugins/{name}", plugin_get),
        Route("/api/plugins/{name}", plugin_delete, methods=["DELETE"]),
        Route("/api/plugins/{name}/config", plugin_config, methods=["GET", "PUT"]),
        Route("/api/plugins/{name}/logs", get_logs),
        Route("/api/plugins/{name}/logs/stream", stream_logs),
        Route("/api/plugins/{name}/{action}", plugin_act, methods=["POST"]),
    ]
    from starlette.routing import WebSocketRoute
    routes += [
        Route("/api/lock", lock_api, methods=["GET", "POST"]),
        Route("/api/accounts", accounts_api, methods=["GET", "PUT"]),
        Route("/api/lock/password", lock_api, methods=["PUT"]),
        Route("/api/remote/devices/{device_id}/terminal", remote_terminal_info),
        Route("/api/remote/devices/{device_id}/terminal/external", remote_terminal_external, methods=["POST"]),
        WebSocketRoute("/api/remote/devices/{device_id}/terminal/ws", remote_terminal_socket),
        Route("/api/remote/devices/{device_id}/desktop/prepare", remote_desktop_prepare, methods=["POST"]),
        WebSocketRoute("/api/remote/devices/{device_id}/desktop/ws", remote_desktop_socket),
        vendor_route("novnc", STATIC_DIR / "vendor" / "novnc"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.sup = sup
    app.state.hub = hub
    app.state.lock = lock
    app.state.p2p = p2p_state
    return LocalOnly(app, sup.cfg.host, sup.cfg.port)
