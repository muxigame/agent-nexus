"""The device contract ``anx.device/1`` and its per-platform implementations (design §5.2).

``platform/<name>/`` exports ``CAPABILITIES`` (the capabilities this platform
declares, each with the standard tools providing it), ``probe()`` (runtime
availability: a locked screen makes UIA unavailable) and ``notify()`` (the
platform's notification backend). The server registers tools from that table:
it is the old ``sys.platform == "win32"`` gate, generalised.

Absolute ``import platform`` elsewhere still reaches the standard library;
this package is only ever imported as ``src.platform``.
"""
from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any

from .common import CAPABILITIES as COMMON_CAPABILITIES, CORE, SCREEN_CAPABILITIES

CONTRACT = "anx.device/1"
PLATFORMS = ("windows", "linux", "darwin")

#: Every standard tool on any platform -> its capability. A tool missing here
#: is a programming error (the server refuses to register it).
TOOL_CAPABILITY: dict[str, str] = {
    tool: name
    for table in (COMMON_CAPABILITIES, SCREEN_CAPABILITIES)
    for name, spec in table.items()
    for tool in spec["tools"]
}


def current_platform(value: str | None = None) -> str:
    """The contract's platform name for ``sys.platform`` (or for ``value``)."""
    value = value or sys.platform
    if value in PLATFORMS:
        return value
    if value == "win32":
        return "windows"
    if value == "darwin":
        return "darwin"
    return "linux"  # linux, freebsd, ...: the POSIX implementation


def load(name: str | None = None) -> ModuleType:
    return importlib.import_module(f"{__name__}.{current_platform(name)}")


class Contract:
    """The declared capabilities of one platform, with runtime availability."""

    def __init__(self, name: str | None = None):
        self.platform = current_platform(name)
        self.module = load(self.platform)
        self.declared: dict[str, dict[str, Any]] = self.module.CAPABILITIES
        self._tools = {tool: capability for capability, spec in self.declared.items()
                       for tool in spec.get("tools", [])}

    def capability_of(self, tool: str) -> str | None:
        """The capability ``tool`` belongs to when this platform provides it, else ``None``."""
        if tool not in TOOL_CAPABILITY:
            raise KeyError(f"tool {tool!r} is not in the anx.device/1 capability table")
        return self._tools.get(tool)

    def describe(self) -> dict[str, dict[str, Any]]:
        """Capabilities as ``device_info`` publishes them: ``v`` plus runtime facts."""
        overrides = self.module.probe()
        result: dict[str, dict[str, Any]] = {}
        for name, spec in self.declared.items():
            entry = {key: value for key, value in spec.items() if key not in ("tools", "tier")}
            entry.update(overrides.get(name, {}))
            result[name] = entry
        return result

    def notify(self, title: str, body: str, level: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
        return self.module.notify(title, body, level, actions)

    @property
    def notify_backend(self) -> str:
        return self.module.NOTIFY_BACKEND


__all__ = ["CONTRACT", "CORE", "PLATFORMS", "TOOL_CAPABILITY", "Contract", "current_platform", "load"]
