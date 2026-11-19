"""Invite codes and per-device credentials (M16, decision D3, design §11.5).

The owner creates a **one-time, expiring** invite in the gateway console, the
local console (``hub_call device_admin``) or with ``anx-gateway invite
create``. Its code ``anx1.<base64url(json{hub, id, secret})>`` carries the
gateway address, so a freshly installed client only needs the code. The
device redeems it with ``POST <hub>/enroll`` and receives its own random token;
the gateway keeps sha256 digests only (``invites``, ``device_tokens``).

Ingress accepts these tokens next to the ``agents`` environment map. Revoking
a device closes its link with WebSocket code 4401 (reason ``revoked``); the
device then stops reconnecting.

The pure helpers (:func:`encode_invite`, :func:`decode_invite`,
:func:`slugify`) are shared with the client side (``agent_nexus.join``).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .federation import Hub, RoutingError, valid_id
from .store import GatewayStore, clean_hostname

log = logging.getLogger(__name__)

INVITE_PREFIX = "anx1."
LINK_PREFIX = "agentnexus://enroll?invite="
#: WebSocket close code for a revoked device credential (design §11.5).
CLOSE_REVOKED = 4401
DEFAULT_TTL_S = 86400
MAX_TTL_S = 7 * 86400
MIN_TTL_S = 60
MAX_LABEL_CHARS = 64
#: agent_ids an invite never hands out: mailbox names, routing words, the hub itself.
RESERVED_AGENT_IDS = frozenset({
    "hub", "me", "all", "any", "anon", "system", "gateway", "owner", "admin", "root", "local", "localhost",
    "device", "devices", "default", "api", "inbox", "mcp", "ingress", "enroll", "null", "none", "unknown"})
TOKEN_ID_PREFIX = "dt_"
INVITE_ID_PREFIX = "inv_"
_INVITE_ID = re.compile(r"inv_[A-Za-z0-9_-]{6,40}")


class EnrollError(Exception):
    """A refusal with an HTTP status and a stable ``code`` for the UI."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def body(self) -> dict:
        return {"error": self.message, "code": self.code}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def hub_origin(url: Any) -> str | None:
    """``https://host[:port][/base]`` (or loopback http) without a trailing slash; ``None`` if unusable."""
    if not isinstance(url, str) or not url.isascii() or any(ch.isspace() for ch in url):
        return None
    parts = urlsplit(url.rstrip("/"))
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password \
            or parts.query or parts.fragment:
        return None
    if parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        return None
    return url.rstrip("/")


def encode_invite(hub: str, invite_id: str, secret: str) -> str:
    payload = json.dumps({"hub": hub, "id": invite_id, "secret": secret}, separators=(",", ":"))
    return INVITE_PREFIX + _b64(payload.encode("utf-8"))


def invite_link(code: str) -> str:
    return LINK_PREFIX + code


def decode_invite(text: Any) -> dict:
    """``{hub, id, secret}`` from a code or an ``agentnexus://enroll?invite=`` link; raises ``EnrollError``."""
    if not isinstance(text, str):
        raise EnrollError(400, "invalid_invite", "邀请码格式不正确")
    text = "".join(text.split())  # pasted codes may be wrapped
    if text.lower().startswith("agentnexus://"):
        query = parse_qs(urlsplit(text).query)
        text = (query.get("invite") or [""])[0]
    if not text.startswith(INVITE_PREFIX) or len(text) > 2048:
        raise EnrollError(400, "invalid_invite", "邀请码格式不正确")
    body = text[len(INVITE_PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise EnrollError(400, "invalid_invite", "邀请码格式不正确") from None
    if not isinstance(data, dict) or not all(isinstance(data.get(key), str) for key in ("hub", "id", "secret")):
        raise EnrollError(400, "invalid_invite", "邀请码格式不正确")
    hub = hub_origin(data["hub"])
    if hub is None or not _INVITE_ID.fullmatch(data["id"]) or not 16 <= len(data["secret"]) <= 128:
        raise EnrollError(400, "invalid_invite", "邀请码格式不正确")
    return {"hub": hub, "id": data["id"], "secret": data["secret"]}


def slugify(hostname: Any) -> str:
    """A hostname as an agent_id: lowercase letters, digits and hyphens."""
    text = hostname if isinstance(hostname, str) else ""
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")[:60].strip("-")
    return slug or "device"


def parse_ttl(value: Any) -> int | None:
    """``3600``, ``"90m"``, ``"24h"``, ``"7d"`` -> seconds; ``None`` for the default."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise EnrollError(400, "invalid_ttl", "有效期格式不正确")
    if isinstance(value, (int, float)):
        return int(value)
    match = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", str(value))
    if not match:
        raise EnrollError(400, "invalid_ttl", "有效期格式不正确，例如 3600、90m、24h、7d")
    return int(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]


class Enrollment:
    """Invites, enrollment and revocation on top of :class:`GatewayStore`."""

    def __init__(self, store: GatewayStore, hub: Hub | None = None, *, public_url: str | None = None,
                 agents: Any = (), groups: Any = (), settings: dict | None = None,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.hub = hub
        self.public_url = hub_origin(public_url) if public_url else None
        self.agents = frozenset(agents)
        self.groups = tuple(groups)
        settings = settings or {}
        self.default_ttl_s = int(settings.get("default_ttl_s", DEFAULT_TTL_S))
        self.max_ttl_s = int(settings.get("max_ttl_s", MAX_TTL_S))
        self.clock = clock
        #: called with an agent_id after its credential was revoked (closes the live link).
        self.on_revoked: Callable[[str], Any] | None = None

    # -- invites --------------------------------------------------------------
    def create_invite(self, *, label: Any = None, ttl_s: Any = None, agent_id: Any = None,
                      hub_url: str | None = None) -> dict:
        hub = hub_origin(hub_url) if hub_url else self.public_url
        if hub is None:
            raise EnrollError(409, "no_public_url", "网关没有配置 public_url，无法生成带地址的邀请码")
        ttl = parse_ttl(ttl_s)
        ttl = self.default_ttl_s if ttl is None else ttl
        if not MIN_TTL_S <= ttl <= self.max_ttl_s:
            raise EnrollError(400, "invalid_ttl", f"有效期必须在 {MIN_TTL_S} 秒到 {self.max_ttl_s // 3600} 小时之间")
        if label is not None and not isinstance(label, str):
            raise EnrollError(400, "invalid_label", "备注必须是文字")
        label = " ".join((label or "").split())[:MAX_LABEL_CHARS] or None
        if agent_id in ("", None):
            agent_id = None
        else:
            agent_id = self._check_requested_id(agent_id)
        invite_id = INVITE_ID_PREFIX + secrets.token_urlsafe(9)
        secret = secrets.token_urlsafe(32)
        invite = self.store.invite_insert(invite_id, _sha(secret), label=label, agent_id=agent_id,
                                          expires_at=self.clock() + ttl)
        code = encode_invite(hub, invite_id, secret)
        return {"invite": invite, "code": code, "link": invite_link(code)}

    def list_invites(self, state: str | None = "unused") -> list[dict]:
        if state not in (None, "all", "unused", "used", "expired", "revoked"):
            raise EnrollError(400, "invalid_state", "state 必须是 unused、used、expired、revoked 或 all")
        return self.store.invites(state)

    def revoke_invite(self, invite_id: Any) -> dict:
        if not isinstance(invite_id, str) or self.store.invite(invite_id) is None:
            raise EnrollError(404, "unknown_invite", "找不到这个邀请")
        if not self.store.invite_revoke(invite_id):
            invite = self.store.invite(invite_id)
            if invite["state"] == "revoked":
                return invite
            raise EnrollError(409, invite["state"], "邀请已使用，不能作废" if invite["state"] == "used" else "邀请已过期")
        return self.store.invite(invite_id)

    # -- enrollment -------------------------------------------------------------
    def _taken(self) -> set[str]:
        """agent_ids in use: the env map, every known device, every credential (revoked ones may be reused)."""
        revoked = {row["agent_id"] for row in self.store.devices() if row.get("revoked_at")}
        active = self.store.device_token_agents(active=True)
        known = {row["agent_id"] for row in self.store.devices()} - (revoked - active)
        if self.hub is not None:
            known |= set(self.hub.peers)
        return known | set(self.agents) | active

    def _check_requested_id(self, value: Any) -> str:
        try:
            agent_id = valid_id(value)
        except RoutingError:
            raise EnrollError(400, "invalid_agent_id", "设备 ID 只能包含字母、数字、点、下划线和连字符") from None
        if agent_id.lower() in RESERVED_AGENT_IDS:
            raise EnrollError(400, "reserved_agent_id", f"设备 ID {agent_id} 是保留名，请换一个")
        if agent_id in self.agents or agent_id in self.store.device_token_agents(active=True) \
                or (self.hub is not None and agent_id in self.hub.peers):
            raise EnrollError(409, "agent_id_taken", f"设备 ID {agent_id} 已被占用")
        return agent_id

    def _pick_agent_id(self, hostname: str | None) -> str:
        base = slugify(hostname)
        if base in RESERVED_AGENT_IDS:
            base = f"{base}-pc"
        taken = self._taken()
        if base not in taken:
            return base
        for number in range(2, 1000):
            candidate = f"{base}-{number}"
            if candidate not in taken:
                return candidate
        return f"{base}-{secrets.token_hex(3)}"

    def enroll(self, body: Any) -> dict:
        """``POST /enroll`` (design §11.5 steps 2-4). The token is in the response only."""
        if not isinstance(body, dict):
            raise EnrollError(400, "invalid_request", "请求体必须是 JSON 对象")
        invite_id, secret = body.get("invite_id"), body.get("secret")
        if (not invite_id or not secret) and body.get("invite"):
            decoded = decode_invite(body["invite"])
            invite_id, secret = decoded["id"], decoded["secret"]
        if not isinstance(invite_id, str) or not isinstance(secret, str):
            raise EnrollError(400, "invalid_invite", "邀请码格式不正确")
        stored = self.store.invite_secret(invite_id)
        # Unknown id and wrong secret look the same to the caller.
        if stored is None or not hmac.compare_digest(stored, _sha(secret)):
            raise EnrollError(403, "invalid_invite", "邀请码无效")
        invite = self.store.invite(invite_id)
        if invite["state"] != "unused":
            message = {"used": "邀请已经使用过了", "expired": "邀请已过期", "revoked": "邀请已作废"}[invite["state"]]
            raise EnrollError(410, invite["state"], message)
        hostname = clean_hostname(body.get("hostname"))
        device = body.get("device") if isinstance(body.get("device"), dict) else None
        if device is not None:
            device = {key: str(value)[:100] for key, value in device.items()
                      if key in ("platform", "os", "arch", "form", "distro") and isinstance(value, (str, int, float))}
        if invite.get("agent_id"):
            agent_id = invite["agent_id"]
            if agent_id in self.agents or agent_id in self.store.device_token_agents(active=True):
                raise EnrollError(409, "agent_id_taken", f"设备 ID {agent_id} 已被占用")
        elif body.get("agent_id"):
            agent_id = self._check_requested_id(body["agent_id"])
        else:
            agent_id = self._pick_agent_id(hostname)
        token = secrets.token_urlsafe(32)
        if not self.store.enroll_device(invite_id=invite_id, agent_id=agent_id,
                                        token_id=TOKEN_ID_PREFIX + secrets.token_urlsafe(9),
                                        token_sha256=_sha(token), hostname=hostname,
                                        device={"device": device} if device else None):
            state = (self.store.invite(invite_id) or {}).get("state", "used")
            raise EnrollError(410, state if state != "unused" else "used", "邀请已经使用过了")
        log.info("device %s enrolled with invite %s", agent_id, invite_id)
        hub = self.public_url
        return {"agent_id": agent_id, "token": token, "hub": hub, "display_name": hostname or agent_id,
                "token_type": "device"}

    # -- ingress authentication ---------------------------------------------------
    def check_token(self, agent_id: str, token: str) -> str | None:
        """``"ok"`` (active credential), ``"revoked"`` (a revoked one) or ``None`` (not a device token)."""
        digest = _sha(token)
        verdict = self.store.device_token_match(agent_id, digest)
        if verdict == "ok":
            with contextlib.suppress(Exception):
                self.store.device_token_used(digest)
        return verdict

    def kind(self, agent_id: str) -> str | None:
        if agent_id in self.agents:
            return "agents_map"
        if agent_id in self.store.device_token_agents():
            return "invite"
        if any(agent_id.startswith(prefix) and len(agent_id) > len(prefix) for prefix in self.groups):
            return "agent_groups"
        return None

    # -- revocation ----------------------------------------------------------------
    def revoke_credentials(self, agent_id: Any) -> dict:
        """Revoke ``agent_id``'s per-device credential in the database (no live link involved)."""
        if not isinstance(agent_id, str) or not agent_id:
            raise EnrollError(400, "invalid_agent_id", "需要设备 ID")
        kind = self.kind(agent_id)
        if kind == "agents_map":
            raise EnrollError(409, "agents_map", "这台设备通过 gateway.env 的令牌映射入网，需要从 gateway.env 删除该 token 后重启网关")
        if kind == "agent_groups":
            raise EnrollError(409, "agents_map", "这台设备通过 agent_groups 共享令牌入网，需要在 gateway.env 中更换该组令牌")
        if kind is None:
            raise EnrollError(404, "unknown_device", f"找不到设备 {agent_id}")
        self.store.revoke_device(agent_id)
        with contextlib.suppress(Exception):
            self.store.end_device_addresses(agent_id)
        return {"agent_id": agent_id, "closed": False}

    async def revoke_device(self, agent_id: Any) -> dict:
        """Revoke and close the live link with 4401 (the console's 吊销 button)."""
        result = self.revoke_credentials(agent_id)
        result["closed"] = await self.close_link(agent_id)
        log.info("device %s revoked (link closed: %s)", agent_id, result["closed"])
        return result

    async def close_link(self, agent_id: str) -> bool:
        """Close a live ingress link with 4401 and take the device offline at once."""
        peer = self.hub.peers.get(agent_id) if self.hub is not None else None
        if peer is None:
            return False
        self.hub.unregister(peer)
        close = getattr(peer.socket, "close", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(close(code=CLOSE_REVOKED, reason="revoked"), 2)
        return True

    async def sweep(self) -> list[str]:
        """Close links whose credential was revoked elsewhere (``anx-gateway device revoke``)."""
        if self.hub is None:
            return []
        closed = []
        for agent_id in list(self.hub.peers):
            if agent_id in self.agents or getattr(self.hub.peers[agent_id], "enrolled_via", None) != "invite":
                continue
            active, _ = self.store.device_token_hashes(agent_id)
            if not active and await self.close_link(agent_id):
                with contextlib.suppress(Exception):
                    self.store.end_device_addresses(agent_id)
                closed.append(agent_id)
        return closed

    async def run(self, interval: float = 3.0) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.sweep()
            except Exception as exc:  # never let the sweeper die
                log.warning("revocation sweep failed (%s)", type(exc).__name__)
