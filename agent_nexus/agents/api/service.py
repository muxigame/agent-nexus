"""The API side of the AgentHub (docs/agent-chat.md §16.4): the provider RPCs, the API agents in ``agent_info``,
their sessions, and what a run of one needs.

RPCs (feature ``agents/1``, the same table as the other ``agent_*`` handlers, so local, relayed and gateway calls
reach them alike):

* ``agent_api_providers {}`` -> ``{providers: [{id, label, protocol, base_url, headers?, models, enabled, source,
  key: {set, last4? | env?}}]}`` -- never a key;
* ``agent_api_provider_save {provider}`` -> ``{provider}`` (a key-like field is refused);
* ``agent_api_provider_delete {provider_id}``;
* ``agent_api_key_set {provider_id, key | null}`` -> ``{ok, key: {set, last4?}}`` (write only; ``env:NAME``);
* ``agent_api_test {provider_id, model?}`` -> ``{ok, latency_ms, model, reply}`` or ``{error, code, latency_ms}``;
* ``agent_api_models {provider_id}`` -> ``{models: [{id, label?, context_window?, owned_by?}]}``.

The gateway's synced copy is pulled (``hub_call api_config_get``) at most every :data:`PULL_EVERY_S` when the
console lists agents or providers; a gateway that is away or too old leaves the cached copy as it is.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..registry import AgentDef
from . import config as cfg_mod
from . import models as model_table
from . import protocols
from .config import ApiConfig, ConfigError
from .keys import KeyError_, KeyStore, origin_of
from .protocols import ApiError
from .store import SessionStore

log = logging.getLogger(__name__)

PULL_EVERY_S = 60.0
PULL_TIMEOUT_S = 8.0
TEST_TIMEOUT_S = 30.0
OPS = ("agent_api_providers", "agent_api_provider_save", "agent_api_provider_delete", "agent_api_key_set",
       "agent_api_test", "agent_api_models")


def _error(message: str, code: str, **extra: Any) -> dict:
    return {"error": message, "code": code, **extra}


class KeyOriginChanged(PermissionError):
    """The provider's address changed since its key was set: the key is not sent to the new one."""

    code = "key_origin_changed"


ORIGIN_CHANGED = "服务商地址变了，请重新输入密钥"


def local_endpoint(base_url: str) -> bool:
    """A provider on this machine or the LAN (Ollama, vLLM): it usually needs no key."""
    try:
        host = (urlsplit(base_url).hostname or "").lower()
    except ValueError:
        return False
    if host in ("localhost", "") or host.endswith((".local", ".localhost", ".lan")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


class ApiAgents:
    """``root``: the anx root; ``config``: () -> ``agent-nexus.json``; ``gateway``: the hub's ``hub_call`` (async,
    None when there is none); ``env``: () -> the run-as account's environment (for ``env:`` key references)."""

    def __init__(self, root: Path, *, config: Callable[[], dict] | None = None,
                 gateway: Callable[[str, dict], Awaitable[dict | None]] | None = None,
                 env: Callable[[], dict] | None = None, client: Callable[[], Any] | None = None):
        self.root = Path(root)
        self.config = ApiConfig(self.root, config=config)
        self.keys = KeyStore(self.root)
        self.store = SessionStore(self.root)
        self.gateway = gateway
        self.env = env or (dict)
        self.client = client or protocols.client
        self._pulled = 0.0
        self._pulling: asyncio.Task | None = None

    # -- the gateway's copy
    async def pull(self, *, force: bool = False) -> bool:
        """Fetch the gateway's providers now and then; True when a copy came."""
        if self.gateway is None or (not force and time.monotonic() - self._pulled < PULL_EVERY_S):
            return False
        self._pulled = time.monotonic()
        try:
            result = await asyncio.wait_for(self.gateway("api_config_get", {}), PULL_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - no gateway is a normal state
            log.info("agents: api_config_get unavailable (%s)", type(exc).__name__)
            return False
        if not isinstance(result, dict) or "error" in result or not isinstance(result.get("providers"), list):
            return False
        try:
            await asyncio.to_thread(self.config.set_gateway, result["providers"])
        except OSError as exc:
            log.warning("agents: could not cache the gateway's API providers: %s", exc)
            return False
        return True

    # -- agents
    def defs(self) -> dict[str, AgentDef]:
        try:
            return self.config.defs()
        except Exception:
            log.exception("agents: reading the API providers failed")
            return {}

    def _env(self) -> dict:
        try:
            return dict(self.env() or {})
        except Exception:  # noqa: BLE001
            return {}

    def _key_status(self, provider: dict, env: dict) -> dict:
        return self.keys.status(provider["id"], env, origin=origin_of(provider.get("base_url")))

    def login(self, provider: dict, env: dict | None = None) -> tuple[str, str]:
        status = self._key_status(provider, env if env is not None else self._env())
        optional = provider.get("key_optional")  # unset: a provider on this machine / the LAN needs none
        keyless = bool(optional or (optional is None and local_endpoint(provider.get("base_url") or "")))
        if status.get("origin_changed"):
            # the key is kept for the address it was entered for, never sent to the new one
            return ("ok", "不需要密钥") if keyless else ("missing", ORIGIN_CHANGED)
        if status.get("set"):
            return "ok", f"环境变量 {status['env']}" if status.get("env") else "已设置密钥"
        if status.get("env"):
            return "missing", f"环境变量 {status['env']} 没有设置"
        if keyless:
            return "ok", "不需要密钥"
        return "missing", "还没有设置密钥"

    def info(self, kinds: set[str] | None = None) -> list[dict]:
        """``agent_info`` entries of the API agents."""
        env = self._env()
        out = []
        for provider in self.config.providers():
            if not provider.get("enabled", True):
                continue
            login, detail = self.login(provider, env)
            for model in provider.get("models") or []:
                if not model.get("enabled", True):
                    continue
                kind = cfg_mod.kind_of(provider["id"], model["id"])
                if kinds is not None and kind not in kinds:
                    continue
                out.append({
                    "kind": kind, "label": cfg_mod.model_label(model), "installed": True, "path": None, "version": None,
                    "login": login, "login_detail": detail, "install": None, "adapter": "api", "builtin": False,
                    "supports": {"resume": True, "approvals": True, "stream": True, "tools": model.get("tools", True)},
                    "login_command": [], "install_hint": "", "permissions": list(cfg_mod.PERMISSIONS),
                    "group": "api", "provider": provider["id"], "provider_label": provider["label"],
                    "protocol": provider["protocol"], "model": model["id"],
                    "context_window": model.get("context_window") or model_table.context_window(model["id"]),
                    "source": provider.get("source", "local"), "gateway_mcp": None,
                })
        return out

    def run_spec(self, kind: str, env: dict) -> dict:
        """``opts.extra["api"]`` of a run (raises ``LookupError`` with a message for people)."""
        found = self.config.model(kind)
        if found is None:
            raise LookupError(f"API 模型 {kind[4:]} 没有启用，或者服务商已被删除")
        provider, model = found
        login, detail = self.login(provider, env)
        if login != "ok":
            error = KeyOriginChanged if detail == ORIGIN_CHANGED else PermissionError
            raise error(f"{provider['label']} {detail}：先在 Agent 设置 → API 里设置密钥")
        provider_id, origin = provider["id"], origin_of(provider["base_url"])
        return {"provider": provider, "model": model, "store": self.store, "client": self.client,
                "key": lambda: self.keys.resolve(provider_id, env, origin=origin),
                # anx's own state (the API keys, the device credential) is never a tool's to read
                "deny": [str(self.root / ".anx"), str(self.root / ".mcp-manager")]}

    # -- sessions
    def sessions(self, kind: str | None, **kwargs: Any) -> list[dict]:
        if kind and not (kind == "api" or cfg_mod.is_api_kind(kind)):
            return []
        labels = {k: d.label for k, d in self.defs().items()}
        return self.store.discover(kind=kind, labels=labels, **kwargs)

    # -- RPCs
    def _view(self, provider: dict, env: dict) -> dict:
        view = {k: v for k, v in provider.items()}
        view["key"] = {k: v for k, v in self._key_status(provider, env).items() if k != "updated"}
        view["login"] = self.login(provider, env)[0]
        view["protocol_label"] = cfg_mod.PROTOCOL_LABELS.get(provider["protocol"], provider["protocol"])
        return view

    async def providers(self, params: dict) -> dict:
        await self.pull(force=bool(params.get("refresh")))
        env = self._env()
        providers = await asyncio.to_thread(self.config.providers)
        return {"providers": [self._view(p, env) for p in providers]}

    async def provider_save(self, params: dict) -> dict:
        raw = params.get("provider") if isinstance(params.get("provider"), dict) else None
        if raw is None:
            return _error("缺少 provider", "bad_request")
        try:
            saved = await asyncio.to_thread(self.config.save, raw)
        except ConfigError as exc:
            return _error(str(exc), "bad_request")
        return {"ok": True, "provider": self._view({**saved, "source": "local"}, self._env())}

    async def provider_delete(self, params: dict) -> dict:
        provider_id = str(params.get("provider_id") or params.get("id") or "")
        try:
            deleted = await asyncio.to_thread(self.config.delete, provider_id)
        except ConfigError as exc:
            return _error(str(exc), "io_error")
        if not deleted:
            if any(p["id"] == provider_id for p in self.config.gateway()):
                return _error("这个服务商是从网关同步来的，要在云端控制台删除", "gateway_provider")
            return _error("没有这个服务商", "not_found")
        if not any(p["id"] == provider_id for p in self.config.gateway()):
            await asyncio.to_thread(self.keys.delete, provider_id)  # nothing uses its key any more
        return {"ok": True, "deleted": provider_id}

    async def key_set(self, params: dict) -> dict:
        provider_id = str(params.get("provider_id") or "")
        provider = self.config.get(provider_id)
        if provider is None:
            return _error("没有这个服务商，先保存服务商再设置密钥", "not_found")
        key = params.get("key")
        if key is not None and not isinstance(key, str):
            return _error("密钥必须是文字", "bad_request")
        try:
            status = await asyncio.to_thread(self.keys.set, provider_id, key, origin=origin_of(provider["base_url"]))
        except KeyError_ as exc:
            return _error(str(exc), "bad_request")
        except OSError as exc:
            return _error(f"保存密钥失败：{exc.strerror or exc}", "io_error")
        status = self._key_status(provider, self._env())
        return {"ok": True, "provider_id": provider_id, "key": {k: v for k, v in status.items() if k != "updated"}}

    def _provider_model(self, params: dict) -> tuple[dict, dict] | dict:
        provider = self.config.get(str(params.get("provider_id") or ""))
        if provider is None:
            return _error("没有这个服务商", "not_found")
        wanted = params.get("model") if isinstance(params.get("model"), str) and params.get("model") else None
        models = provider.get("models") or []
        model = next((m for m in models if m["id"] == wanted), None) if wanted else \
            next((m for m in models if m.get("enabled", True)), models[0] if models else None)
        if model is None:
            if not wanted:
                return _error("这个服务商还没有模型", "no_model")
            try:
                model = cfg_mod.normalize_model({"id": wanted})
            except ConfigError as exc:
                return _error(str(exc), "bad_request")
        return provider, model

    async def test(self, params: dict) -> dict:
        found = self._provider_model(params)
        if isinstance(found, dict):
            return found
        provider, model = found
        env = self._env()
        if self._key_status(provider, env).get("origin_changed"):
            return _error(ORIGIN_CHANGED, "key_origin_changed", ok=False, latency_ms=0)
        key = self.keys.resolve(provider["id"], env, origin=origin_of(provider["base_url"]))
        started = time.monotonic()
        try:
            async with self.client() as http:
                result = await asyncio.wait_for(protocols.stream_turn(
                    http, provider, {**model, "thinking": False}, key, "",
                    [{"role": "user", "text": "Reply with the single word: pong"}], [], max_output=16), TEST_TIMEOUT_S)
        except ApiError as exc:
            return {"error": str(exc), "code": exc.code, "ok": False, "message": str(exc),
                    "latency_ms": round((time.monotonic() - started) * 1000)}
        except TimeoutError:
            return _error(f"{protocols.host_of(provider['base_url'])} {int(TEST_TIMEOUT_S)} 秒内没有回答", "timeout",
                          ok=False)
        return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000), "model": model["id"],
                "reply": (result.text or result.thinking).strip()[:200], "usage": result.usage}

    async def models(self, params: dict) -> dict:
        provider = self.config.get(str(params.get("provider_id") or ""))
        if provider is None:
            return _error("没有这个服务商", "not_found")
        env = self._env()
        if self._key_status(provider, env).get("origin_changed"):
            return _error(ORIGIN_CHANGED, "key_origin_changed")
        key = self.keys.resolve(provider["id"], env, origin=origin_of(provider["base_url"]))
        try:
            async with self.client(TEST_TIMEOUT_S) as http:
                listed = await protocols.list_models(http, provider, key)
        except ApiError as exc:
            return _error(str(exc), exc.code)
        for item in listed:
            item.setdefault("context_window", model_table.context_window(item["id"]))
            if item["context_window"] is None:
                item.pop("context_window")
        return {"provider_id": provider["id"], "models": listed}

    def handlers(self) -> dict[str, Callable[[dict], Awaitable[dict]]]:
        return {"agent_api_providers": self.providers, "agent_api_provider_save": self.provider_save,
                "agent_api_provider_delete": self.provider_delete, "agent_api_key_set": self.key_set,
                "agent_api_test": self.test, "agent_api_models": self.models}


__all__ = ["OPS", "ApiAgents", "KeyOriginChanged", "local_endpoint"]
