"""Where this computer's own MCP plugins answer: endpoint URL and bearer token of a managed plugin.

Shared by the gateway link (``LocalGateway.configured_targets``, which reaches every plugin) and the 本机 MCP's
``/mcp/self`` route (which reaches only the device plugin, for ``anx exec .``). The token never leaves the process.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from .federation import MCP_ALIASES

#: The device plugin's names (renamed from ``workspace``).
DEVICE_NAMES = ("device", MCP_ALIASES["device"])
CACHE_TTL_S = 5.0


class Unsupported(Exception):
    """The plugin needs an interactive login (OAuth): not usable unattended."""


def plugin_endpoint(spec: Any, root: Path) -> tuple[str, str, str | None] | None:
    """``(mcp endpoint, http base, bearer token)`` of one discovered plugin, None when it cannot be called now
    (disabled, observed, not installed, no port). Raises :class:`Unsupported` for an OAuth-only plugin."""
    from mcpkit import Service
    from mcpkit.config import read_secret
    if (spec.observed or not spec.enabled or not spec.supported or spec.error or spec.port is None
            or not spec.installed or spec.missing):
        return None
    service = Service.load(spec.dir, root=root)
    token = None
    if service.config.auth.bearer_secrets:
        token = read_secret(service.config, service.config.auth.bearer_secrets[0])
    elif service.config.auth.mode != "none":
        raise Unsupported(spec.name)
    host = "127.0.0.1" if spec.host in ("0.0.0.0", "::") else spec.host
    if ":" in host:
        host = f"[{host}]"
    base = f"http://{host}:{spec.port}"
    return base + service.config.path, base, token


def device_plugin(root: Path) -> tuple[str, str | None] | None:
    """``(endpoint, token)`` of this computer's device plugin, None when there is none to call."""
    from .registry import discover, load_manager_config
    specs = discover(load_manager_config(Path(root)))
    for name in DEVICE_NAMES:
        spec = specs.get(name)
        if spec is None:
            continue
        try:
            found = plugin_endpoint(spec, Path(root))
        except Unsupported:
            continue
        if found is not None:
            return found[0], found[2]
    return None


class DevicePluginCache:
    """:func:`device_plugin`, read again at most every :data:`CACHE_TTL_S` seconds (config, secret, port)."""

    def __init__(self, root: Path, lookup=device_plugin):
        self.root = Path(root)
        self.lookup = lookup
        self._at = float("-inf")
        self._value: tuple[str, str | None] | None = None
        self._lock = threading.Lock()

    def get(self) -> tuple[str, str | None] | None:
        with self._lock:
            if time.monotonic() - self._at < CACHE_TTL_S:
                return self._value
        try:
            value = self.lookup(self.root)
        except Exception:  # noqa: BLE001 - a broken config: no device plugin to call
            value = None
        with self._lock:
            self._at, self._value = time.monotonic(), value
        return value

    def forget(self) -> None:
        with self._lock:
            self._at = float("-inf")
