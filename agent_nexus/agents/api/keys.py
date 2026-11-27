"""API keys of the model providers, on this device only (docs/agent-chat.md §16.4).

``<root>/.anx/agents/api-keys.json`` = ``{"version": 1, "keys": {provider_id: {"key": "...", "updated": t}}}``,
mode 0600 (on Windows the owner-only ACL of ``mcpkit.config.protect``), written atomically. A key may also be
``env:VARNAME``: the run-as account's environment variable of that name is used at request time.

A key is bound to the *origin* (``scheme://host:port``) of the provider's address when it is set: if the address
changes later (an edit here, or a synced gateway copy that points elsewhere), the key is not sent there --
:meth:`KeyStore.resolve` gives None and :meth:`KeyStore.status` says ``origin_changed`` until it is entered again.

Nothing here ever returns a key to a caller other than the agent loop: :meth:`KeyStore.status` gives
``{set, last4}`` (or ``{set, env}`` for a reference), and keys are never logged.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

PROVIDER_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
ENV_REF = re.compile(r"env:([A-Za-z_][A-Za-z0-9_]{0,127})")
MAX_KEY = 8192


class KeyError_(ValueError):
    """A key that cannot be stored (bad provider id, bad value)."""


def _protect(path: Path) -> None:
    if os.name == "nt":  # pragma: no cover - Windows
        try:
            from mcpkit.config import protect
            protect(path)
        except Exception as exc:  # noqa: BLE001 - best effort, as for the connector secrets
            log.debug("could not restrict %s (%s)", path.name, type(exc).__name__)
        return
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700 if path.is_dir() else 0o600)


DEFAULT_PORTS = {"http": 80, "https": 443}


def origin_of(url: Any) -> str:
    """``scheme://host:port`` of ``url`` (lower case, the default port spelled out), "" when it has none."""
    try:
        parts = urlsplit(str(url or "").strip())
        scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
        port = parts.port or DEFAULT_PORTS.get(scheme)
    except ValueError:
        return ""
    if not scheme or not host:
        return ""
    return f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"


def env_ref(value: Any) -> str | None:
    """``VARNAME`` of an ``env:VARNAME`` reference, else None."""
    if isinstance(value, str):
        match = ENV_REF.fullmatch(value.strip())
        if match:
            return match.group(1)
    return None


class KeyStore:
    def __init__(self, root: Path, *, path: Path | None = None):
        self.path = Path(path) if path else Path(root) / ".anx" / "agents" / "api-keys.json"
        self.lock = threading.RLock()

    # -- file
    def _load(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.warning("agents: %s is unreadable; no API keys are available", self.path.name)
            return {}
        keys = raw.get("keys") if isinstance(raw, dict) else None
        if not isinstance(keys, dict):
            return {}
        return {str(k): dict(v) for k, v in keys.items()
                if isinstance(v, dict) and isinstance(v.get("key"), str) and v["key"]}

    def _save(self, keys: dict[str, dict]) -> None:
        folder = self.path.parent
        fresh = not folder.exists()
        folder.mkdir(parents=True, exist_ok=True)
        if fresh:
            _protect(folder)
        handle, temporary = tempfile.mkstemp(dir=folder, prefix=".api-keys.", suffix=".tmp")
        try:
            if os.name != "nt":
                os.fchmod(handle, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "keys": keys}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
        _protect(self.path)

    # -- API
    def set(self, provider_id: str, key: str | None, *, origin: str = "") -> dict:
        """Store (or with None / "" remove) the key of ``provider_id`` for the address ``origin``
        (:func:`origin_of` of the provider's base_url); returns its :meth:`status`."""
        if not isinstance(provider_id, str) or not PROVIDER_ID.fullmatch(provider_id):
            raise KeyError_("服务商 ID 不对")
        if key is not None and not isinstance(key, str):
            raise KeyError_("密钥必须是文字")
        key = (key or "").strip()
        if len(key) > MAX_KEY or any(ord(ch) < 32 for ch in key):
            raise KeyError_("密钥格式不对（太长或含有控制字符）")
        if key.startswith("env:") and env_ref(key) is None:
            raise KeyError_("环境变量引用的格式是 env:变量名")
        with self.lock:
            keys = self._load()
            if key:
                keys[provider_id] = {"key": key, "updated": round(time.time(), 3), **({"origin": origin} if origin else {})}
            elif provider_id in keys:
                keys.pop(provider_id)
            else:
                return self.status(provider_id)
            self._save(keys)
        return self.status(provider_id, origin=origin or None)

    def delete(self, provider_id: str) -> None:
        with self.lock:
            keys = self._load()
            if keys.pop(provider_id, None) is not None:
                self._save(keys)

    @staticmethod
    def _moved(entry: dict, origin: str | None) -> bool:
        """The key was set for another address than the provider's ``origin`` now (a key from before origins
        were recorded has none and is not refused)."""
        bound = entry.get("origin")
        return bool(origin) and isinstance(bound, str) and bool(bound) and bound != origin

    def status(self, provider_id: str, env: Mapping[str, str] | None = None, *, origin: str | None = None) -> dict:
        """``{set, last4?}`` / ``{set, env}``: what the console may know about a key (never the key);
        ``{set: false, origin_changed: true}`` when it was set for another address than ``origin``."""
        with self.lock:
            entry = self._load().get(provider_id)
        if not entry:
            return {"set": False}
        if self._moved(entry, origin):
            return {"set": False, "origin_changed": True, "updated": entry.get("updated")}
        value = entry["key"]
        name = env_ref(value)
        if name is not None:
            source = os.environ if env is None else env
            return {"set": bool(source.get(name)), "env": name, "updated": entry.get("updated")}
        out: dict[str, Any] = {"set": True, "updated": entry.get("updated")}
        if len(value) >= 8:
            out["last4"] = value[-4:]
        return out

    def resolve(self, provider_id: str, env: Mapping[str, str] | None = None, *, origin: str | None = None) -> str | None:
        """The key to send to ``origin`` (an ``env:`` reference read now), or None -- also when the key was set
        for another address. Only for the request itself."""
        with self.lock:
            entry = self._load().get(provider_id)
        if not entry or self._moved(entry, origin):
            return None
        name = env_ref(entry["key"])
        if name is not None:
            source = os.environ if env is None else env
            value = source.get(name)
            return value.strip() if isinstance(value, str) and value.strip() else None
        return entry["key"]


__all__ = ["KeyError_", "KeyStore", "env_ref", "origin_of"]
