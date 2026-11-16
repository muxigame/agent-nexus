"""Cloud MCP endpoint and authenticated manager ingress."""
from __future__ import annotations

import argparse
import asyncio
import base64
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

from .federation import Hub, MAX_MESSAGE_BYTES, RoutingError, valid_id, valid_media_token
from .gateway import Gateway
from .registry import read_json

log = logging.getLogger(__name__)
# Keepalive for manager ingress sockets: a silently dead path is noticed in
# about 25 s. Reconnects replace the old peer at once regardless.
WS_PING_INTERVAL_S = 10.0
WS_PING_TIMEOUT_S = 15.0


def env_secret(name: str) -> str:
    value = os.environ.get(name, "")
    if len(value) < 32:
        raise ConfigError(f"environment variable {name} must contain a token of at least 32 characters")
    return value


def build_cloud_app(config: dict, root: Path):
    agent_envs = config.get("agents", {})
    if not isinstance(agent_envs, dict) or not agent_envs:
        raise ConfigError("cloud config requires agents: {instance_id: token_environment_variable}")
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
    hub = Hub(default_workspace=default, timeout=timeout)
    server = Gateway(hub).build()
    if "auth" in config:
        service_config = ServiceConfig(name="workspace", root=root, dir=root,
                                       path=config.get("path", "/mcp"), public_url=config.get("public_url"), auth=config["auth"])
        if service_config.auth.mode == "none":
            raise ConfigError("cloud endpoint requires bearer or OAuth authentication")
        server.auth = build_auth(service_config)
    else:
        server.auth = StaticBearer([env_secret(config.get("token_env", "MCPM_SERVER_TOKEN"))],
                                   ["tools"], required_scopes=["tools"])

    async def ingress(socket: WebSocket):
        agent_id = socket.path_params["agent_id"]
        expected = agents.get(agent_id)
        if expected is None:
            try:
                valid_id(agent_id)
            except RoutingError:
                await socket.close(code=1008)
                return
            matches = [prefix for prefix in groups if agent_id.startswith(prefix) and len(agent_id) > len(prefix)]
            if matches:
                expected = groups[max(matches, key=len)]
        header = socket.headers.get("authorization", "")
        token = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
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
            peer = hub.register(agent_id, socket, hello.get("catalog"))
            await peer.send({"method": "ready", "params": {"agent_id": agent_id}})
            while True:
                message = await socket.receive_json()
                if message.get("method") == "catalog":
                    catalog = message.get("params")
                    hub.validate_catalog(catalog)
                    peer.catalog = catalog
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
                hub.unregister(peer)

    async def health(_):
        return JSONResponse({"status": "ok", "service": "workspace-cloud"})

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
            try:
                return await asyncio.wait_for(peer.call("media_get", {"mcp": "workspace", "token": token}), 20)
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
    app.state.hub = hub
    app.state.mcp = server
    return app


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Cloud Workspace MCP and reverse manager ingress")
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
            print("cloud configuration valid")
            return 0
        import uvicorn
        uvicorn.run(app, host=host, port=port, ws_max_size=MAX_MESSAGE_BYTES,
                    ws_ping_interval=WS_PING_INTERVAL_S, ws_ping_timeout=WS_PING_TIMEOUT_S,
                    access_log=bool(config.get("log_requests", False)))
        return 0
    except (ConfigError, RoutingError, ValueError, OSError) as exc:
        print(f"mcpm-server: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
