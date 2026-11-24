"""Gateway side of the P2P tunnels (``p2p/1``): muxi-p2p signaling and reflector.

* ``/p2p/api/v1/tunnel/*`` -- the muxi-p2p-signaling router. Each device's tunnel agent
  registers with its own token (``hub_call p2p_config`` hands it out over the device's
  authenticated link); a revoked or unknown device has none. The client side
  (``/endpoints``, ``/client-token``, ``/punch``) needs a device of this gateway's private
  network (its credential, online, not locked) or the owner.
* ``MUXI-REFLECT/1`` on UDP 45701-45703 and TCP 45711-45712: started the first time a device
  asks for its configuration. ``p2p.reflector_hosts`` lists the public addresses devices
  should use -- the gateway's own IPs, never a CDN in front of it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit

from .federation import RoutingError

log = logging.getLogger(__name__)
FEATURE = "p2p/1"


#: Punch requests per source IP per 30 s (muxi_p2p_signaling's public default is 4).
PUNCH_QUOTA = 30

class P2PGateway:
    def __init__(self, config: dict, state_dir: Path, *, hub, store, device_credential, owner):
        settings = config.get("p2p") if isinstance(config.get("p2p"), dict) else {}
        self.enabled = settings.get("enabled", True) is not False
        public = urlsplit(str(config.get("public_url") or ""))
        self.public_url = str(config.get("public_url") or "").rstrip("/")
        hosts = settings.get("reflector_hosts")
        self.reflector_hosts = [str(h) for h in hosts] if isinstance(hosts, list) and hosts else \
            ([public.hostname] if public.hostname else [])
        #: explicitly configured reflector addresses: start the reflector with the first device
        self.eager = isinstance(hosts, list) and bool(hosts)
        self.bind = str(settings.get("reflector_bind") or "0.0.0.0")
        self.udp_ports = [int(p) for p in settings.get("udp_ports") or (45701, 45702, 45703)]
        self.tcp_ports = [int(p) for p in settings.get("tcp_ports") or (45711, 45712)]
        self.tunnel_port = int(settings.get("tunnel_port") or 45740)
        self.path = Path(state_dir) / "p2p-tokens.json"
        self.hub, self.store = hub, store
        self.device_credential, self.owner = device_credential, owner
        self._lock = threading.Lock()
        self._reflector = None
        self._reflector_lock = asyncio.Lock()

    # -- per-device tunnel tokens
    def _tokens(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def token_for(self, agent_id: str) -> str:
        with self._lock:
            tokens = self._tokens()
            token = tokens.get(agent_id)
            if not isinstance(token, str):
                token = tokens[agent_id] = secrets.token_urlsafe(32)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(".tmp")
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(tokens, handle)
                temporary.replace(self.path)
            return token

    def secret_for(self, server_id: str) -> str | None:
        """The signaling's secret_provider: only devices that are not revoked."""
        row = self.store.device(server_id) if self.store is not None else None
        if row is not None and row.get("revoked_at"):
            return None
        return self._tokens().get(server_id)

    # -- who may use the client side
    async def guard(self, request, server_id: str) -> None:
        from fastapi import HTTPException

        agent_id = request.headers.get("x-anx-device", "")
        header = request.headers.get("authorization", "")
        if agent_id and header.startswith("Bearer "):
            if not self.device_credential(agent_id, header.removeprefix("Bearer ")):
                raise HTTPException(status_code=401, detail="unknown device credential")
            peer = self.hub.peers.get(agent_id)
            if peer is None:
                raise HTTPException(status_code=401, detail="only a connected device may connect")
            if getattr(peer, "locked", False):
                raise HTTPException(status_code=423, detail="这台设备已锁定，解锁后才能操作其他设备")
            return
        if await self.owner(request) is None:
            raise HTTPException(status_code=401, detail="sign in or use a device credential")

    def app(self):
        from fastapi import FastAPI
        from muxi_p2p_signaling import configure, router

        # Callers are devices the guard authenticated, often several behind one office NAT:
        # a higher punch quota than the public default (4 per 30 s per IP).
        configure(secret_provider=self.secret_for, access_guard=self.guard, diagnostics=False, relay_host="",
                  punch_quota=PUNCH_QUOTA)
        app = FastAPI(title="Agent Nexus P2P signaling", docs_url=None, redoc_url=None, openapi_url=None)
        app.include_router(router)
        return app

    # -- reflector
    async def ensure_reflector(self) -> None:
        if self._reflector is not None or not self.enabled:
            return
        async with self._reflector_lock:
            if self._reflector is not None:
                return
            from muxi_p2p_signaling.reflector import Reflector
            self._reflector = await Reflector(self.bind, self.udp_ports, self.tcp_ports,
                                              addresses=self.reflector_hosts).start()
            log.info("p2p reflector listening: %s", self._reflector.bound)

    async def close(self) -> None:
        if self._reflector is not None:
            await self._reflector.stop()
            self._reflector = None

    # -- hub_call ops
    async def device_config(self, agent_id: str) -> dict:
        if not self.enabled:
            raise RoutingError("p2p is disabled on this gateway")
        await self.ensure_reflector()
        return {"control_plane": f"{self.public_url}/p2p", "server_id": agent_id, "token": self.token_for(agent_id),
                "reflectors": self.reflector_hosts, "tunnel_port": self.tunnel_port}

    async def ticket(self, caller, args: dict) -> dict:
        """``remote_ticket``: a one-time ticket from the target device for the caller's stream."""
        if getattr(caller, "locked", False):
            raise RoutingError("locked: 这台设备已锁定，解锁后才能操作其他设备")
        target_id = str(args.get("device_id") or "")
        service = str(args.get("service") or "")
        peer = self.hub.peer(target_id)
        from .remote_hub import FEATURE as HUB
        if HUB not in getattr(peer, "features", ()):
            raise RoutingError("unsupported: 对方客户端版本不支持直连（需要 0.5.0 以上）")
        result = await peer.call("remote_ticket", {"service": service, "from": caller.agent_id})
        if not isinstance(result, dict) or "ticket" not in result:
            raise RoutingError(str((result or {}).get("error") or "the device gave no ticket"))
        return {"ticket": result["ticket"], "server_id": target_id, "expires_in": result.get("expires_in")}
