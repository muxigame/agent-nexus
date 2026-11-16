"""Running a WorkSpace/mcp server: one entry point, one command line, one set of rules.

A service provides a function that builds its FastMCP server and hands it to
:func:`main`::

    def build(service: Service) -> FastMCP:
        mcp = service.create_server(instructions=...)
        @mcp.tool
        def hello() -> str: ...
        return mcp

    def cli(argv=None) -> int:
        return mcpkit.main(__file__, build, version=__version__, argv=argv)

and gets, for free and the same everywhere:

* ``--transport http|stdio --host --port --check-config`` (mcpm passes
  ``--host {host} --port {port}``);
* configuration from ``agent-nexus.json`` (or legacy ``mcp-manager.json``) and secrets from ``secrets/``;
* authentication from ``auth`` - applied to HTTP only; a stdio client is a
  local process the user started;
* refusal to listen beyond loopback without authentication;
* Host/Origin validation (DNS rebinding) for loopback names and ``public_url``;
* an unauthenticated ``/healthz`` for mcpm and tunnels;
* a startup line that names the endpoint and auth mode, never a secret.
"""
from __future__ import annotations

import argparse
import inspect
import sys
from typing import Any, Awaitable, Callable, TypeVar
from urllib.parse import urlsplit

import anyio
from fastmcp import FastMCP
from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import config as _config
from .auth import build_auth
from .config import LOOPBACK_HOSTS, ConfigError, ServiceConfig

ModelT = TypeVar("ModelT", bound=BaseModel)
Builder = Callable[["Service"], "FastMCP | Awaitable[FastMCP]"]
UNSPECIFIED_HOSTS = ("0.0.0.0", "::", "")


class Service:
    def __init__(self, config: ServiceConfig, version: str = ""):
        self.config = config
        self.version = version

    @classmethod
    def load(cls, anchor: Any, *, version: str = "", **overrides: Any) -> Service:
        return cls(_config.load(anchor, **overrides), version)

    @property
    def name(self) -> str:
        return self.config.name

    # -- configuration -------------------------------------------------------------------

    def settings(self, model: type[ModelT]) -> ModelT:
        """``services.<name>.settings`` validated as ``model``."""
        try:
            return model.model_validate(self.config.settings)
        except ValidationError as exc:
            problems = "; ".join(f"settings.{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
            raise ConfigError(f"service {self.name!r}: {problems}") from None

    def secret(self, name: str, *, min_length: int = 32) -> str:
        return _config.read_secret(self.config, name, min_length=min_length)

    def update_settings(self, **values: Any) -> None:
        _config.update_settings(self.config, **values)
        self.config.settings.update(values)

    # -- server ------------------------------------------------------------------------------

    def create_server(self, *, instructions: str | None = None, **kwargs: Any) -> FastMCP:
        server = FastMCP(self.name, version=self.version or None, instructions=instructions, **kwargs)
        self.add_health_route(server)
        return server

    def add_health_route(self, server: FastMCP) -> None:
        name, version = self.name, self.version

        @server.custom_route("/healthz", methods=["GET"])
        async def healthz(_: Request) -> JSONResponse:
            # Unauthenticated on purpose (mcpm, tunnels): name and version only.
            return JSONResponse({"status": "ok", "service": name, "version": version},
                                headers={"Cache-Control": "no-store"})

    def http_options(self, server: FastMCP) -> dict[str, Any]:
        """Validate the HTTP deployment, install auth on ``server``, and return
        the keyword arguments for ``FastMCP.run_async``."""
        cfg = self.config
        if cfg.port is None:
            raise ConfigError(f"service {self.name!r} has no port; set it in mcp-service.json or pass --port")
        loopback = cfg.host in LOOPBACK_HOSTS
        if cfg.auth.mode == "none" and not loopback:
            raise ConfigError(
                f"refusing to listen on {cfg.host or 'all interfaces'} without authentication; "
                f"bind to {', '.join(LOOPBACK_HOSTS)} or set auth.mode")
        server.auth = build_auth(cfg)

        hosts = ["127.0.0.1", "localhost", "[::1]"]
        if cfg.host not in LOOPBACK_HOSTS + UNSPECIFIED_HOSTS:
            hosts.append(cfg.host)
        origins = [f"http://{h}:{cfg.port}" for h in ("127.0.0.1", "localhost", "[::1]")]
        if cfg.public_url:
            public = urlsplit(cfg.public_url)
            hosts.append(public.hostname or "")
            origins.append(cfg.public_url)
        return {
            "host": cfg.host, "port": cfg.port, "path": cfg.path,
            "stateless_http": cfg.stateless,
            "host_origin_protection": True,
            "allowed_hosts": [h for host in hosts for h in (host, f"{host}:*")],
            "allowed_origins": origins,
            "uvicorn_config": {"access_log": cfg.log_requests},
        }

    def describe(self, transport: str) -> str:
        cfg = self.config
        if transport == "stdio":
            return f"{self.name} {self.version}: stdio"
        text = f"{self.name} {self.version}: http://{cfg.host}:{cfg.port}{cfg.path}  auth={cfg.auth.mode}"
        if cfg.resource:
            text += f"  public={cfg.resource}"
        return text

    async def serve(self, server: FastMCP, transport: str) -> None:
        if transport == "stdio":
            await server.run_async("stdio", show_banner=False)
            return
        options = self.http_options(server)
        print(self.describe(transport), file=sys.stderr, flush=True)
        await server.run_async("http", show_banner=False, **options)


def main(anchor: Any, build: Builder, *, version: str = "", argv: list[str] | None = None,
         description: str | None = None) -> int:
    """Command line for a service. ``build`` may be a plain or an async function."""
    from .runtime import install_windows_socket_cleanup
    install_windows_socket_cleanup()
    parser = argparse.ArgumentParser(description=description)
    # stdio by default: a client that starts this process (Claude Desktop) passes
    # no arguments, and nothing should open a listening socket unless asked.
    parser.add_argument("--transport", choices=["http", "stdio"], default="stdio")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--check-config", action="store_true",
                        help="validate configuration and secrets, print a summary without secrets, and exit")
    args = parser.parse_args(argv)
    try:
        service = Service.load(anchor, version=version, host=args.host, port=args.port)
        if args.check_config:
            if args.transport == "http":
                service.http_options(FastMCP(service.name))
            print(f"configuration ok: {service.describe(args.transport)}")
            return 0

        async def run() -> None:
            server = build(service)
            if inspect.isawaitable(server):
                server = await server
            await service.serve(server, args.transport)

        anyio.run(run)
        return 0
    except ConfigError as exc:
        print(f"mcpkit: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
