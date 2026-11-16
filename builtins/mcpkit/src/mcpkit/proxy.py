"""Serve a stdio-only MCP server through mcpkit, sharing ONE upstream session.

Sharing is the point, not an optimisation. chrome-devtools-mcp opens a browser
debugging connection per session, and a browser whose remote debugging was
switched on at ``edge://inspect`` asks the user to approve each one - so a
session per client would mean a dialog per client.

FastMCP reuses a session when the proxy's client factory hands back an already
connected :class:`fastmcp.Client`; the connection is opened in the server's
lifespan, before the first request. If the upstream process goes away the
whole service exits, so mcpm restarts both together instead of leaving an HTTP
endpoint that answers every call with an error.
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.server.providers.proxy import FastMCPProxy

from .service import Service

#: Seconds between liveness pings to the upstream process.
WATCHDOG_INTERVAL_S = 15.0
EXIT_UPSTREAM_LOST = 3


def stdio_proxy(service: Service, *, command: str, args: list[str], env: dict[str, str] | None = None,
                cwd: str | Path | None = None, watchdog_interval: float = WATCHDOG_INTERVAL_S,
                **server_kwargs: Any) -> FastMCPProxy:
    transport = StdioTransport(command=command, args=args, env=env, cwd=str(cwd) if cwd else None, keep_alive=True)
    client = Client(transport)

    async def watchdog() -> None:
        while True:
            await anyio.sleep(watchdog_interval)
            try:
                with anyio.fail_after(watchdog_interval):
                    # MCP 2026 removed the protocol ping method. A cache-bypassed
                    # tools/list is a real upstream roundtrip on both protocol eras.
                    await client.list_tools_mcp(cache_mode="bypass")
                    alive = True
            except Exception as exc:  # closed transport, timeout, crashed process
                alive, reason = False, repr(exc)
            else:
                reason = "ping returned false"
            if not alive:
                print(f"{service.name}: upstream MCP process lost ({reason}); exiting so the supervisor restarts it",
                      file=sys.stderr, flush=True)
                os._exit(EXIT_UPSTREAM_LOST)

    @asynccontextmanager
    async def lifespan(_server: Any):
        async with client:
            async with anyio.create_task_group() as tg:
                tg.start_soon(watchdog)
                try:
                    yield {}
                finally:
                    tg.cancel_scope.cancel()

    proxy = FastMCPProxy(client_factory=lambda: client, name=service.name, version=service.version or None,
                         lifespan=lifespan, **server_kwargs)
    service.add_health_route(proxy)
    return proxy
