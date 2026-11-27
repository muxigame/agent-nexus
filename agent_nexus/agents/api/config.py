"""The API providers of this device (docs/agent-chat.md §16.4).

``agent-nexus.json`` → ``agents.api.providers[]``::

    {"id": "deepseek", "label": "DeepSeek", "protocol": "openai", "base_url": "https://api.deepseek.com/v1",
     "headers": {"X-Title": "anx"}, "enabled": true,
     "models": [{"id": "deepseek-chat", "label": "DeepSeek V3", "context_window": 128000, "max_output": 8192,
                 "tools": true, "enabled": true}]}

The gateway keeps a synced copy of the same non-secret structure (``hub_call api_config_get / api_config_put``);
the device caches the last copy it pulled in ``<root>/.anx/agents/api-gateway.json`` and merges it under its own
providers (a local provider with the same id wins). Keys are never part of either: any key-like field is refused
(:func:`find_secret`), keys live in :mod:`.keys`.

Each enabled model of an enabled provider is an agent of kind ``api:<provider>/<model>`` (:func:`agent_defs`).
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..registry import AgentDef

log = logging.getLogger(__name__)

PROTOCOLS = ("openai", "anthropic", "gemini")
PROTOCOL_LABELS = {"openai": "OpenAI 兼容", "anthropic": "Anthropic", "gemini": "Gemini"}
PROVIDER_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")
HEADER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
MAX_PROVIDERS = 64
MAX_MODELS = 300
MAX_HEADERS = 20
KIND_PREFIX = "api:"
PERMISSIONS = ("ask", "edits", "readonly", "full")
#: Field / header names that carry a secret; such a field is refused outright, never silently dropped.
SECRET_NAMES = {"key", "api_key", "apikey", "token", "access_token", "auth_token", "bearer", "secret",
                "client_secret", "authorization", "proxy_authorization", "password", "passwd", "cookie",
                "x_api_key", "x_goog_api_key"}
SECRET_SUFFIXES = ("_key", "_token", "_secret", "_password")
#: ...and any name containing one of these (X-Auth, X-Custom-Auth, X-Session-Token, Proxy-Credential...)
SECRET_PARTS = ("auth", "token", "secret", "passw", "credential", "cookie", "apikey")
_LOCK = threading.RLock()


class ConfigError(ValueError):
    """A provider that cannot be saved; the message is for people (中文)."""


def _secret_name(name: str) -> bool:
    norm = name.strip().lower().replace("-", "_")
    return norm in SECRET_NAMES or norm.endswith(SECRET_SUFFIXES) or any(part in norm for part in SECRET_PARTS)


def find_secret(value: Any, path: str = "") -> str | None:
    """The path of the first key-like field in ``value`` (dict keys at any depth), or None."""
    if isinstance(value, dict):
        for name, item in value.items():
            here = f"{path}.{name}" if path else str(name)
            if isinstance(name, str) and _secret_name(name):
                return here
            found = find_secret(item, here)
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = find_secret(item, f"{path}[{index}]")
            if found:
                return found
    return None


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())
    return value[:limit]


def _positive(value: Any, name: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 or value > 100_000_000:
        raise ConfigError(f"{name} 必须是正整数")
    return int(value)


def normalize_model(raw: Any) -> dict:
    if isinstance(raw, str):
        raw = {"id": raw}
    if not isinstance(raw, dict):
        raise ConfigError("模型必须是对象")
    model_id = raw.get("id").strip() if isinstance(raw.get("id"), str) else ""
    if not MODEL_ID.fullmatch(model_id):
        raise ConfigError(f"模型 ID 不对：{model_id or '（空）'}")
    out: dict[str, Any] = {"id": model_id}
    label = _text(raw.get("label"), 80)
    if label:
        out["label"] = label
    for name in ("context_window", "max_output"):
        number = _positive(raw.get(name), f"模型 {model_id} 的 {name}")
        if number:
            out[name] = number
    out["tools"] = raw.get("tools") is not False
    out["enabled"] = raw.get("enabled") is not False
    if isinstance(raw.get("thinking"), bool):
        out["thinking"] = raw["thinking"]
    return out


def normalize_provider(raw: Any) -> dict:
    """A provider as stored (unknown fields dropped). Raises :class:`ConfigError`, also for any key-like field."""
    if not isinstance(raw, dict):
        raise ConfigError("服务商必须是对象")
    secret = find_secret(raw)
    if secret:
        raise ConfigError(f"配置里不能有密钥（{secret}）：密钥请用「API 密钥」单独设置，它只存在这台设备上")
    provider_id = raw.get("id").strip() if isinstance(raw.get("id"), str) else ""
    if not PROVIDER_ID.fullmatch(provider_id):
        raise ConfigError("服务商 ID 只能用小写字母、数字、- 或 _，最多 32 个字符")
    protocol = raw.get("protocol") if raw.get("protocol") in PROTOCOLS else None
    if protocol is None:
        raise ConfigError("协议只能是 openai、anthropic 或 gemini")
    base_url = raw.get("base_url").strip() if isinstance(raw.get("base_url"), str) else ""
    if not re.fullmatch(r"https?://[^\s/?#]+[^\s?#]*", base_url) or len(base_url) > 500:
        raise ConfigError("地址必须以 http:// 或 https:// 开头")
    try:
        parts = urlsplit(base_url)
        userinfo = parts.username is not None or parts.password is not None or "@" in parts.netloc
        parts.port  # noqa: B018 - raises on a bad port
    except ValueError as exc:
        raise ConfigError("地址的格式不对") from exc
    if userinfo:
        raise ConfigError("地址里不能带账号密码，请把密钥放到「API 密钥」里")
    headers: dict[str, str] = {}
    source = raw.get("headers") if isinstance(raw.get("headers"), dict) else {}
    for name, value in list(source.items())[:MAX_HEADERS]:
        if not isinstance(name, str) or not HEADER_NAME.fullmatch(name):
            raise ConfigError(f"请求头名称不对：{name}")
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise ConfigError(f"请求头 {name} 的值必须是文字")
        text = str(value)
        if len(text) > 500 or any(ch in text for ch in "\r\n\0"):
            raise ConfigError(f"请求头 {name} 的值太长或含有换行")
        headers[name] = text
    models: list[dict] = []
    seen: set[str] = set()
    for item in (raw.get("models") if isinstance(raw.get("models"), list) else [])[:MAX_MODELS]:
        model = normalize_model(item)
        if model["id"] in seen:
            raise ConfigError(f"模型 {model['id']} 重复了")
        seen.add(model["id"])
        models.append(model)
    out: dict[str, Any] = {"id": provider_id, "label": _text(raw.get("label"), 80) or provider_id,
                           "protocol": protocol, "base_url": base_url.rstrip("/"), "models": models,
                           "enabled": raw.get("enabled") is not False}
    if headers:
        out["headers"] = headers
    if isinstance(raw.get("key_optional"), bool):
        out["key_optional"] = raw["key_optional"]
    return out


def normalize_list(raw: Any, *, strict: bool = False) -> list[dict]:
    """Providers from a list; ``strict`` raises on the first bad one, else bad ones are skipped (and logged)."""
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        try:
            provider = normalize_provider(item)
        except ConfigError as exc:
            if strict:
                raise
            log.warning("agents: ignoring API provider %r: %s", (item or {}).get("id") if isinstance(item, dict)
                        else item, exc)
            continue
        if provider["id"] in seen:
            if strict:
                raise ConfigError(f"服务商 {provider['id']} 重复了")
            continue
        seen.add(provider["id"])
        out.append(provider)
        if len(out) >= MAX_PROVIDERS:
            break
    return out


def _api_section(cfg: dict | None) -> dict:
    cfg = cfg if isinstance(cfg, dict) else {}
    agents = cfg.get("agents") if isinstance(cfg.get("agents"), dict) else {}
    api = agents.get("api") if isinstance(agents.get("api"), dict) else {}
    return api


def local_providers(cfg: dict | None) -> list[dict]:
    return normalize_list(_api_section(cfg).get("providers"))


def merge(local: list[dict], remote: list[dict]) -> list[dict]:
    """Local providers (``source: local``) then the gateway's that have no local namesake (``source: gateway``)."""
    ids = {p["id"] for p in local}
    return [{**p, "source": "local"} for p in local] + \
        [{**p, "source": "gateway"} for p in remote if p["id"] not in ids]


def kind_of(provider_id: str, model_id: str) -> str:
    return f"{KIND_PREFIX}{provider_id}/{model_id}"


def parse_kind(kind: Any) -> tuple[str, str] | None:
    """``(provider_id, model_id)`` of ``api:<provider>/<model>``, or None."""
    if not isinstance(kind, str) or not kind.startswith(KIND_PREFIX):
        return None
    provider_id, _, model_id = kind[len(KIND_PREFIX):].partition("/")
    if not PROVIDER_ID.fullmatch(provider_id) or not MODEL_ID.fullmatch(model_id):
        return None
    return provider_id, model_id


def is_api_kind(kind: Any) -> bool:
    return isinstance(kind, str) and kind.startswith(KIND_PREFIX)


def model_label(model: dict) -> str:
    return model.get("label") or model["id"]


def agent_defs(providers: list[dict]) -> dict[str, AgentDef]:
    """An agent per enabled model of every enabled provider (adapter ``api``, no binary)."""
    defs: dict[str, AgentDef] = {}
    for provider in providers:
        if not provider.get("enabled", True):
            continue
        for model in provider.get("models") or []:
            if not model.get("enabled", True):
                continue
            kind = kind_of(provider["id"], model["id"])
            defs[kind] = AgentDef(kind=kind, label=model_label(model), binaries=(), adapter="api", command=[],
                                  resume=True, approvals=True, permission_flags={mode: [] for mode in PERMISSIONS},
                                  session_globs=[], builtin=False, stream=True,
                                  docs_url="")
    return defs


class ApiConfig:
    """The provider list of a device: ``config()`` reads ``agent-nexus.json`` (default: the root's)."""

    def __init__(self, root: Path, *, config: Callable[[], dict] | None = None):
        self.root = Path(root)
        self._config = config
        self.gateway_path = self.root / ".anx" / "agents" / "api-gateway.json"
        self.pulled = 0.0  # monotonic time of the last pull attempt (the hub's)

    # -- reading
    def _read_config(self) -> dict:
        if self._config is not None:
            try:
                data = self._config()
            except Exception:
                log.exception("agents: reading agent-nexus.json failed")
                return {}
            return data if isinstance(data, dict) else {}
        from .. import registry
        return registry.load_config(self.root)

    def local(self) -> list[dict]:
        return local_providers(self._read_config())

    def gateway(self) -> list[dict]:
        try:
            raw = json.loads(self.gateway_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return normalize_list(raw.get("providers") if isinstance(raw, dict) else None)

    def providers(self) -> list[dict]:
        return merge(self.local(), self.gateway())

    def get(self, provider_id: str) -> dict | None:
        return next((p for p in self.providers() if p["id"] == provider_id), None)

    def defs(self) -> dict[str, AgentDef]:
        return agent_defs(self.providers())

    def model(self, kind: str) -> tuple[dict, dict] | None:
        """``(provider, model)`` of an API agent kind (enabled ones only)."""
        parsed = parse_kind(kind)
        if parsed is None:
            return None
        provider = self.get(parsed[0])
        if provider is None or not provider.get("enabled", True):
            return None
        model = next((m for m in provider.get("models") or [] if m["id"] == parsed[1]), None)
        if model is None or not model.get("enabled", True):
            return None
        return provider, model

    # -- the gateway's copy
    def set_gateway(self, providers: Any) -> list[dict]:
        clean = normalize_list(providers)
        self.gateway_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.gateway_path.with_name(f".{self.gateway_path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps({"version": 1, "fetched_at": round(time.time(), 3), "providers": clean},
                                        ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, self.gateway_path)
        return clean

    # -- writing agent-nexus.json
    def _write(self, mutate: Callable[[list[dict]], list[dict]]) -> None:
        from ... import paths
        with _LOCK:
            try:
                data = paths.read_config(self.root)
            except (OSError, ValueError) as exc:
                raise ConfigError(f"读取 agent-nexus.json 失败：{exc}") from exc
            agents = data.get("agents") if isinstance(data.get("agents"), dict) else {}
            api = agents.get("api") if isinstance(agents.get("api"), dict) else {}
            api = {**api, "providers": mutate(normalize_list(api.get("providers")))}
            data = {**data, "agents": {**agents, "api": api}}
            target = paths.write_path(self.root)
            temporary = target.with_suffix(".json.api.tmp")
            try:
                temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temporary.replace(target)
            except OSError as exc:
                with contextlib.suppress(OSError):
                    temporary.unlink()
                raise ConfigError(f"写入 agent-nexus.json 失败：{exc.strerror or exc}") from exc

    def save(self, raw: Any) -> dict:
        provider = normalize_provider(raw)

        def mutate(current: list[dict]) -> list[dict]:
            if len(current) >= MAX_PROVIDERS and all(p["id"] != provider["id"] for p in current):
                raise ConfigError(f"最多 {MAX_PROVIDERS} 个服务商")
            replaced = [provider if p["id"] == provider["id"] else p for p in current]
            return replaced if any(p["id"] == provider["id"] for p in current) else [*current, provider]

        self._write(mutate)
        return provider

    def delete(self, provider_id: str) -> bool:
        found = any(p["id"] == provider_id for p in self.local())
        if found:
            self._write(lambda current: [p for p in current if p["id"] != provider_id])
        return found


__all__ = ["KIND_PREFIX", "PROTOCOLS", "ApiConfig", "ConfigError", "agent_defs", "find_secret", "is_api_kind",
           "kind_of", "local_providers", "merge", "normalize_list", "normalize_provider", "parse_kind"]
