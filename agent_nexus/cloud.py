"""The gateway: cloud MCP endpoint and authenticated device ingress (``anx-gateway``)."""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from mcpkit.auth import StaticBearer, build_auth
from mcpkit.config import ConfigError, ServiceConfig
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import __version__
from . import env as anx_env
from . import features as anx_features
from .devices import catalog_hostname, device_block
from .federation import Hub, MAX_MESSAGE_BYTES, RoutingError, valid_id, valid_media_token
from .connectors import ConnectorHost
from .connectors.web import ConnectorsWeb
from .console import Console
from .downloads import Downloads
from .gateway import LEGACY_TOOLS_MODES, Gateway, client_kind_from_name
from .inbox_web import InboxWeb, oauth_provider
from .invites import CLOSE_REVOKED, Enrollment
from .mailbox import ME, SYSTEM_SENDER, mailbox_config
from .plugin_repo import PluginRepo, PluginRepoWeb, owner_only_dir, repo_settings, repo_warnings
from .registry import read_json
from .store import GatewayStore
from .ui_assets import ui_route

log = logging.getLogger(__name__)
# Keepalive for manager ingress sockets: a silently dead path is noticed in
# about 25 s. Reconnects replace the old peer at once regardless.
WS_PING_INTERVAL_S = 10.0
WS_PING_TIMEOUT_S = 15.0
#: ``service`` field of ``/healthz``; release probes accept the legacy value too.
HEALTH_SERVICE = "agent-nexus-gateway"
LEGACY_HEALTH_SERVICE = "workspace-cloud"
#: Top-level gateway.json/cloud.json keys this gateway reads or knows about.
#: Anything else is reported by --check-config as a warning, never an error
#: (design §4.3): an older or newer config must not block an upgrade.
KNOWN_CONFIG_KEYS = frozenset({
    "host", "port", "path", "public_url", "stateless", "log_requests", "token_env", "agents", "agent_groups",
    "default_workspace", "rpc_timeout_s", "auth", "media_cache_dir", "state_dir", "gateway", "mailbox", "p2p",
    "migrated_from", "downloads", "invites", "plugin_repo"})
KNOWN_INVITE_KEYS = frozenset({"default_ttl_s", "max_ttl_s"})
KNOWN_GATEWAY_KEYS = frozenset({"legacy_tools"})
STATE_DB_NAME = "gateway.sqlite3"


def config_warnings(config: dict) -> list[str]:
    """Unknown keys: reported, never fatal."""
    warnings = [f"unknown configuration key {key!r} (ignored)" for key in sorted(set(config) - KNOWN_CONFIG_KEYS)]
    section = config.get("gateway")
    if isinstance(section, dict):
        warnings += [f"unknown configuration key 'gateway.{key}' (ignored)"
                     for key in sorted(set(section) - KNOWN_GATEWAY_KEYS)]
    if isinstance(config.get("mailbox"), dict):
        try:
            warnings += mailbox_config(config["mailbox"])[1]
        except ConfigError:
            pass  # reported as an error by build_cloud_app
    warnings += Downloads.warnings(config.get("downloads"))
    warnings += repo_warnings(config.get("plugin_repo"))
    if isinstance(config.get("invites"), dict):
        warnings += [f"unknown configuration key 'invites.{key}' (ignored)"
                     for key in sorted(set(config["invites"]) - KNOWN_INVITE_KEYS)]
    return warnings


def invite_settings(config: dict) -> dict:
    """``invites: {default_ttl_s, max_ttl_s}`` (design §11.5: 24 h default, 7 days at most)."""
    section = config.get("invites") or {}
    if not isinstance(section, dict):
        raise ConfigError("invites must be an object")
    result = {"default_ttl_s": 86400, "max_ttl_s": 7 * 86400}
    for key in result:
        value = section.get(key, result[key])
        if isinstance(value, bool) or not isinstance(value, int) or value < 60:
            raise ConfigError(f"invites.{key} must be a number of seconds (at least 60)")
        result[key] = value
    if result["default_ttl_s"] > result["max_ttl_s"]:
        raise ConfigError("invites.default_ttl_s must not exceed invites.max_ttl_s")
    return result


def state_db_path(config: dict, root: Path) -> Path:
    """Where ``gateway.sqlite3`` lives (design §8.4, §8.10).

    ``mailbox.db`` (written by ``anx-gateway migrate``), else ``state_dir``,
    else next to the OAuth store (the legacy state directory), else the
    config directory.
    """
    mailbox = config.get("mailbox") or {}
    if mailbox.get("db"):
        return (root / str(mailbox["db"])).resolve()
    if config.get("state_dir"):
        return (root / str(config["state_dir"])).resolve() / STATE_DB_NAME
    auth = config.get("auth")
    store = auth.get("store") if isinstance(auth, dict) else None
    if isinstance(store, str) and store:
        return (root / store).resolve().parent / STATE_DB_NAME
    return root.resolve() / STATE_DB_NAME


def env_secret(name: str) -> str:
    value = os.environ.get(name, "")
    if len(value) < 32:
        raise ConfigError(f"environment variable {name} must contain a token of at least 32 characters")
    return value


def build_cloud_app(config: dict, root: Path):
    agent_envs = config.get("agents", {})
    # An empty map is fine since M16: devices can join with invites only (design §11.5).
    if not isinstance(agent_envs, dict):
        raise ConfigError("cloud config agents must map instance ids to token environment variables")
    agents = {valid_id(key): hashlib.sha256(env_secret(value).encode()).digest()
              for key, value in agent_envs.items()}
    group_envs = config.get("agent_groups", {})
    if not isinstance(group_envs, dict):
        raise ConfigError("agent_groups must map instance prefixes to token environment variables")
    groups = {}
    for prefix, variable in group_envs.items():
        valid_id(prefix)
        if len(prefix) < 6 or not prefix.endswith("-"):
            raise ConfigError("agent group prefixes must be at least 6 characters and end in '-' ")
        groups[prefix] = hashlib.sha256(env_secret(variable).encode()).digest()
    default = config.get("default_workspace")
    if default is not None:
        if not isinstance(default, dict) or "agent_id" not in default or "workspace" not in default:
            raise ConfigError("default_workspace requires agent_id and workspace")
        valid_id(default["agent_id"])
        valid_id(default["workspace"])
    timeout = float(config.get("rpc_timeout_s", 180))
    if timeout <= 0:
        raise ConfigError("rpc_timeout_s must be positive")
    section = config.get("gateway", {})
    if not isinstance(section, dict):
        raise ConfigError("gateway must be an object")
    legacy_tools = section.get("legacy_tools", "listed")
    if legacy_tools not in LEGACY_TOOLS_MODES:
        raise ConfigError(f"gateway.legacy_tools must be one of {', '.join(LEGACY_TOOLS_MODES)}")
    if "mailbox" in config and not isinstance(config["mailbox"], dict):
        raise ConfigError("mailbox must be an object")
    if "state_dir" in config and not isinstance(config["state_dir"], str):
        raise ConfigError("state_dir must be a path string")
    hub = Hub(default_workspace=default, timeout=timeout)
    # Opened lazily: --check-config builds the app but never touches the database.
    store = GatewayStore(state_db_path(config, root))
    facade = Gateway(hub, store, legacy_tools=legacy_tools, mailbox=config.get("mailbox"))
    # Invites and per-device credentials (M16, decision D3, design §11.5).
    enrollment = Enrollment(store, hub, public_url=config.get("public_url"), agents=agents.keys(),
                            groups=groups.keys(), settings=invite_settings(config))
    facade.enrollment = enrollment
    # Third-party MCP connectors hosted on the gateway (decision D10, design §7):
    # hub/<name> for every client, with no device online.
    connectors = ConnectorHost(store, state_dir=state_db_path(config, root).parent, public_url=config.get("public_url"))
    hub.connectors = connectors

    def notify_owner(subject: str, body: str) -> None:
        if not facade.mailbox_config["enabled"]:
            return
        sent = facade.mailbox.send(SYSTEM_SENDER, ME, body, subject=subject, kind="system",
                                   meta={"connector": subject.split(" ", 1)[0]})
        facade.mail_sent(sent)
    connectors.notify_owner = notify_owner
    downloads = Downloads(config.get("downloads"), state_db_path(config, root).parent / "downloads", root)
    # The plugin repository (插件库, design §6.12): private packages, installed from the server.
    repo_config = repo_settings(config, state_db_path(config, root).parent, root)
    plugin_repo = PluginRepo(store, repo_config["dir"], max_package_mb=repo_config["max_package_mb"])
    repo_dir, public_dir = Path(repo_config["dir"]).resolve(), Path(downloads.dir).resolve()
    if repo_dir == public_dir or repo_dir.is_relative_to(public_dir) or public_dir.is_relative_to(repo_dir):
        raise ConfigError("plugin_repo.dir must not overlap downloads.dir: packages are private")
    try:
        owner_only_dir(repo_dir)
    except OSError as exc:
        log.warning("plugin repo: cannot create %s: %s", repo_dir, exc)
    server = facade.build()
    if "auth" in config:
        service_config = ServiceConfig(name="workspace", root=root, dir=root,
                                       path=config.get("path", "/mcp"), public_url=config.get("public_url"), auth=config["auth"])
        if service_config.auth.mode == "none":
            raise ConfigError("cloud endpoint requires bearer or OAuth authentication")
        server.auth = build_auth(service_config)
    else:
        server.auth = StaticBearer([env_secret(anx_env.resolve_token_env(config.get("token_env"), "SERVER_TOKEN"))],
                                   ["tools"], required_scopes=["tools"])

    def device_credential(agent_id: str, token: str) -> bool:
        """``token`` is ``agent_id``'s current, unrevoked credential (same rules as /ingress)."""
        if not token:
            return False
        try:
            valid_id(agent_id)
        except RoutingError:
            return False
        digest = hashlib.sha256(token.encode()).digest()
        expected = agents.get(agent_id)
        if expected is None:
            verdict = enrollment.check_token(agent_id, token)
            if verdict == "ok":
                return True
            if verdict == "revoked":
                return False
            matches = [prefix for prefix in groups if agent_id.startswith(prefix) and len(agent_id) > len(prefix)]
            expected = groups[max(matches, key=len)] if matches else None
        return expected is not None and hmac.compare_digest(digest, expected)

    async def ingress(socket: WebSocket):
        # Credentials (design §11.5): the agents env map first, then per-device
        # tokens from invites (device_tokens), then the agent_groups prefixes.
        agent_id = socket.path_params["agent_id"]
        header = socket.headers.get("authorization", "")
        token = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
        expected = agents.get(agent_id)
        via = "agents_map"
        if expected is None:
            try:
                valid_id(agent_id)
            except RoutingError:
                await socket.close(code=1008)
                return
            verdict = enrollment.check_token(agent_id, token) if token else None
            if verdict == "ok":
                expected, via = hashlib.sha256(token.encode()).digest(), "invite"
            elif verdict == "revoked":
                # A distinct close code, so the device stops reconnecting (§11.5).
                await socket.accept()
                await socket.close(code=CLOSE_REVOKED, reason="revoked")
                return
            else:
                matches = [prefix for prefix in groups if agent_id.startswith(prefix) and len(agent_id) > len(prefix)]
                if matches:
                    expected, via = groups[max(matches, key=len)], "agent_groups"
        origin = socket.headers.get("origin")
        public = config.get("public_url")
        if expected is None or not hmac.compare_digest(hashlib.sha256(token.encode()).digest(), expected) or (origin and origin != public):
            await socket.close(code=1008)
            return
        await socket.accept()
        peer = None
        try:
            hello = await asyncio.wait_for(socket.receive_json(), 15)
            if hello.get("protocol") != 1 or hello.get("agent_id") != agent_id:
                raise RoutingError("invalid manager handshake")
            # Feature exchange (design §3.2): a legacy agent sends no "anx"
            # and so is never sent any method beyond the legacy set.
            anx = hello.get("anx")
            peer = hub.register(agent_id, socket, hello.get("catalog"),
                                features=anx_features.parse_features(anx), anx_version=anx_features.peer_version(anx))
            peer.enrolled_via = via
            store.device_connected(agent_id, hostname=catalog_hostname(peer.catalog), features=sorted(peer.features),
                                   device=device_block(peer.catalog) or None, enrolled_via=via,
                                   version=peer.anx_version)
            # Legacy agents only check method == "ready"; params.anx is ignored.
            await peer.send({"method": "ready", "params": {
                "agent_id": agent_id, "anx": anx_features.gateway_anx(facade.devices.display_name(agent_id))}})
            # on_device_online (design §8.7): its queued wakes go out now, after "ready".
            facade.wake.device_online(agent_id)
            # With p2p.reflector_hosts configured the reflector starts with the first device
            # (otherwise on the first p2p_config).
            p2p = getattr(facade, "p2p", None)
            if p2p is not None and p2p.enabled and p2p.eager and p2p._reflector is None:
                starting = asyncio.ensure_future(p2p.ensure_reflector())
                starting.add_done_callback(lambda task: task.cancelled() or (task.exception() and log.warning(
                    "p2p reflector did not start: %s", task.exception())))
            while True:
                message = await socket.receive_json()
                method = message.get("method")
                if method == "catalog":
                    catalog = message.get("params")
                    hub.validate_catalog(catalog)
                    peer.catalog = catalog
                    store.device_catalog(agent_id, hostname=catalog_hostname(catalog),
                                         device=device_block(catalog) or None)
                elif method == "hub_call":
                    await hub_call(peer, message)
                else:
                    peer.resolve(message)
        except WebSocketDisconnect as exc:
            log.warning("manager %s ingress closed (code %s)", agent_id, exc.code)
        except asyncio.TimeoutError:
            log.warning("manager %s ingress handshake timed out", agent_id)
        except (RoutingError, ValueError, TypeError, AttributeError) as exc:
            log.warning("manager %s ingress rejected (%s)", agent_id, type(exc).__name__)
            await socket.close(code=1008)
        finally:
            if peer:
                current = hub.peers.get(agent_id) is peer
                hub.unregister(peer)
                if current:  # a reconnect that replaced this socket keeps the device online
                    with contextlib.suppress(Exception):
                        store.device_disconnected(agent_id)

    async def hub_call(peer, message):
        """Device -> gateway request (``hub_call/1``); always answered, never fatal to the link."""
        request_id = message.get("id")
        if not isinstance(request_id, str) or not 0 < len(request_id) <= 100:
            raise RoutingError("hub_call requires a string id")
        try:
            reply = {"id": request_id, "result": await facade.hub_call(peer, message.get("params"))}
        except RoutingError as exc:
            reply = {"id": request_id, "error": str(exc)}
        except Exception as exc:
            log.warning("hub_call from %s failed (%s)", peer.agent_id, type(exc).__name__)
            reply = {"id": request_id, "error": "hub_call failed"}
        await peer.send(reply)

    async def health(request):
        # Release probes and the upgrade script read version/features/db_schema (design §10).
        try:
            db_schema = store.schema_version()
        except Exception:  # a broken database must not hide the process behind a 500
            db_schema = None
        body = {"status": "ok", "service": HEALTH_SERVICE, "version": __version__,
                "features": list(anx_features.GATEWAY_FEATURES), "db_schema": db_schema}
        if request.query_params.get("detail") == "1":
            # Counters of missed notices (design §8.6) and the database size (§8.4).
            size = facade.mailbox.db_size_mb()
            warn = facade.mailbox_config["retention"]["warn_db_mb"]
            body["detail"] = {"mailbox": {"enabled": facade.mailbox_config["enabled"],
                                          "counters": facade.mailbox.counters(), "db_mb": size,
                                          "db_warning": bool(size is not None and warn and size > warn)},
                              "devices_online": len(hub.peers)}
        return JSONResponse(body)

    async def media(request):
        try:
            token = valid_media_token(request.path_params.get("token"))
        except RoutingError:
            return PlainTextResponse("Not found", status_code=404)
        cache_dir = config.get("media_cache_dir")
        if cache_dir:
            cached = Path(cache_dir) / token
            metadata = cached.with_suffix(".json")
            try:
                info = json.loads(metadata.read_text(encoding="utf-8"))
                if float(info["expires_at"]) > time.time():
                    payload = cached.read_bytes()
                    return Response(payload, media_type=info.get("content_type", "application/octet-stream"),
                                    headers={"Cache-Control": "private, max-age=600", "X-Content-Type-Options": "nosniff"})
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if not hub.peers:
            return PlainTextResponse("Not found", status_code=404)

        async def fetch(peer):
            # Media belongs to the device plugin: "device" on agent-nexus
            # devices, "workspace" on devices still running the old agent.
            catalog = getattr(peer, "catalog", None) or {}
            mcps = catalog.get("mcps", {})
            named = device_block(catalog).get("mcp")
            if isinstance(named, str) and named in mcps:
                name = named  # the contract block names the plugin's local MCP
            else:
                name = "device" if "device" in mcps or "workspace" not in mcps else "workspace"
            try:
                return await asyncio.wait_for(peer.call("media_get", {"mcp": name, "token": token}), 20)
            except Exception:
                return {"status": 502}

        responses = await asyncio.gather(*(fetch(peer) for peer in list(hub.peers.values())))
        for item in responses:
            if item.get("status") == 200:
                try:
                    payload = base64.b64decode(item["data"], validate=True)
                except (ValueError, TypeError):
                    continue
                if len(payload) > 20 * 1024 * 1024:
                    return PlainTextResponse("Media too large", status_code=413)
                return Response(payload, media_type=item.get("content_type", "application/octet-stream"),
                                headers={"Cache-Control": "private, max-age=600", "X-Content-Type-Options": "nosniff"})
        status = 410 if any(item.get("status") == 410 for item in responses) else 404
        return PlainTextResponse("Expired" if status == 410 else "Not found", status_code=status)

    # Preserve the SDK's lifespan and OAuth routes by adding ingress directly
    # to its app, rather than mounting it under a second session manager.
    public = urlsplit(config.get("public_url", ""))
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    if public.hostname:
        hosts += [public.hostname, f"{public.hostname}:*"]
    app = server.http_app(path=config.get("path", "/mcp"), stateless_http=bool(config.get("stateless", True)), session_idle_timeout=3600,
                          host_origin_protection=True, allowed_hosts=hosts,
                          allowed_origins=[config["public_url"]] if public.hostname else [])
    app.router.routes.insert(0, WebSocketRoute("/ingress/{agent_id}", ingress))
    app.router.routes.insert(0, Route("/media/{token}", media))
    app.router.routes.insert(0, Route("/healthz", health))
    # The shared design system for the console, login and inbox pages (docs/ui-spec.md).
    app.router.routes.insert(0, ui_route())
    # The owner's web inbox and PWA (decision D11, design §8.12).
    inbox_web = InboxWeb(facade, server, config)
    app.router.routes[0:0] = inbox_web.routes()
    # The web console at the site root: login, devices and invites, downloads,
    # settings; plus the public POST /enroll (docs/ui-spec.md §2-§3, design §11.5).
    console = Console(config=config, facade=facade, hub=hub, store=store, server=server, inbox_web=inbox_web,
                      enrollment=enrollment, downloads=downloads)
    app.router.routes[0:0] = console.routes()
    # Remote terminal: GET /api/devices/{id}/terminal and its WebSocket (term/1 devices).
    from .terminal_bridge import TerminalBridge
    app.router.routes[0:0] = TerminalBridge(console, device_credential=device_credential).routes()
    # The agent chat: /api/devices/{id}/agents/{op} to agents/1 devices, and run handles (docs/agent-chat.md §9).
    from .agents_bridge import AgentsBridge
    app.router.routes[0:0] = AgentsBridge(console, device_credential=device_credential).routes()
    # File transfer through the gateway when no tunnel can be punched (files/1 devices).
    from .files_bridge import FilesBridge
    app.router.routes[0:0] = FilesBridge(console, device_credential=device_credential).routes()
    # P2P tunnels between devices (muxi-p2p): signaling under /p2p, the reflector, tunnel tokens.
    from .p2p_gateway import P2PGateway
    p2p = P2PGateway(config, state_db_path(config, root).parent, hub=hub, store=store,
                     device_credential=device_credential, owner=console.owner)
    facade.p2p = p2p
    app.state.p2p = p2p
    # Where devices roughly are (the 设备 page's map): ip2region, offline, on the gateway only.
    from .geoip import GeoLocator
    facade.devices.locator = GeoLocator(state_db_path(config, root).parent,
                                        ip_of=p2p.device_ip if p2p.enabled else None)
    from urllib.parse import urlsplit as _split
    facade.devices.gateway_host = (p2p.reflector_hosts[0] if p2p.reflector_hosts
                                   else _split(str(config.get("public_url") or "")).hostname)
    if p2p.enabled:
        try:
            from starlette.routing import Mount
            app.router.routes.insert(0, Mount("/p2p", app=p2p.app()))
        except ImportError as exc:  # muxi-p2p-signaling missing: devices fall back to the relay
            log.warning("p2p signaling unavailable (%s)", exc)
            p2p.enabled = False
    # Connectors: the console APIs and the OAuth callback (docs/ui-spec.md §3.4, design §7.3).
    app.router.routes[0:0] = ConnectorsWeb(connectors, console).routes()
    # 插件库: owner APIs, device downloads, and "安装到…" (design §6.12).
    app.router.routes[0:0] = PluginRepoWeb(plugin_repo, console, agents=agents, groups=groups).routes()
    provider = oauth_provider(server.auth)
    if provider is not None:
        def client_name(client_id):
            item = provider.state.get("client", client_id) if client_id else None
            name = item.get("client_name") if isinstance(item, dict) else None
            return name if isinstance(name, str) else None

        def client_kind(client_id):
            return client_kind_from_name(client_name(client_id))
        facade.client_kind = client_kind
        facade.client_name = client_name
    inner_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(scope_app):
        async with inner_lifespan(scope_app) as state:
            tasks = [asyncio.create_task(facade.maintenance()),
                     # Links revoked from the CLI are closed within seconds (design §11.5).
                     asyncio.create_task(enrollment.run()),
                     # Connector sessions, token refresh and reconnects (design §7.3).
                     asyncio.create_task(connectors.run())]
            if facade.mailbox_config["enabled"] and facade.mailbox_config["wake"]["enabled"]:
                tasks.append(asyncio.create_task(facade.wake.run()))       # design §8.7
            if facade.mailbox_config["enabled"]:
                tasks.append(asyncio.create_task(facade.escalation.run()))  # decision D9, §8.11
            try:
                yield state
            finally:
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                with contextlib.suppress(Exception):
                    await connectors.close()
    app.router.lifespan_context = lifespan
    app.state.hub = hub
    app.state.mcp = server
    app.state.gateway = facade
    app.state.store = store
    app.state.inbox_web = inbox_web
    app.state.console = console
    app.state.enrollment = enrollment
    app.state.connectors = connectors
    app.state.plugin_repo = plugin_repo
    return app


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["migrate"]:  # anx-gateway migrate --from ... (design 4.3)
        from .migrate import gateway_main
        return gateway_main(argv[1:])
    if argv[:1] in (["invite"], ["device"], ["plugin-repo"]):  # invite/device (§11.5), plugin-repo (§6.12)
        from .gateway_admin import main as admin_main
        return admin_main(argv)
    parser = argparse.ArgumentParser(prog="anx-gateway", description="agent-nexus gateway: cloud MCP endpoint and device ingress")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args(argv)
    try:
        path = args.config.resolve()
        config = read_json(path)
        app = build_cloud_app(config, path.parent)
        host = args.host or config.get("host", "127.0.0.1")
        port = args.port or int(config.get("port", 8440))
        if not 1 <= port <= 65535:
            raise ConfigError("invalid cloud port")
        if args.check_config:
            for warning in config_warnings(config):
                print(f"anx-gateway: warning: {warning}", file=sys.stderr)
            print("cloud configuration valid")
            return 0
        import uvicorn
        uvicorn.run(app, host=host, port=port, ws_max_size=MAX_MESSAGE_BYTES,
                    ws_ping_interval=WS_PING_INTERVAL_S, ws_ping_timeout=WS_PING_TIMEOUT_S,
                    access_log=bool(config.get("log_requests", False)))
        return 0
    except (ConfigError, RoutingError, ValueError, OSError) as exc:
        print(f"anx-gateway: {exc}", file=sys.stderr)
        return 2


def deprecated_main() -> int:
    """The ``mcpm-server`` console script: same command, one deprecation line."""
    print("mcpm-server: deprecated command name; use `anx-gateway` (same arguments)", file=sys.stderr, flush=True)
    return main()


if __name__ == "__main__":
    sys.exit(main())
