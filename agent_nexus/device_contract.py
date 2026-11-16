"""The device capability contract ``anx.device/1`` as the manager and gateway see it (design §5.2).

The device plugin implements the contract (``builtins/device/src/platform``);
this module holds the gateway/manager side: the versioned schema
``schemas/device-contract.v1.json`` (shipped inside the wheel as
``agent_nexus/schemas``), the standard tool -> capability table, and the
helpers that read ``_meta`` from catalog tools.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

CONTRACT = "anx.device/1"
SCHEMA_NAME = "device-contract.v1.json"
#: Tool ``_meta`` keys (design §5.2 rule 2 and 4).
META_CAPABILITY = "anx/capability"
META_CONTRACT = "anx/contract"
META_INTERNAL = "anx/internal"


def schema_path(name: str = SCHEMA_NAME) -> Path:
    here = Path(__file__).resolve().parent
    for candidate in (here / "schemas" / name, here.parent / "schemas" / name):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"contract schema {name} is not installed")


@functools.lru_cache(maxsize=None)
def schema() -> dict:
    return json.loads(schema_path().read_text(encoding="utf-8"))


def core_capabilities() -> tuple[str, ...]:
    return tuple(schema()["x-anx-core-capabilities"])


def standard_tools() -> dict[str, list[str]]:
    """Capability -> the standard tools a device declaring it must offer."""
    return {name: list(tools) for name, tools in schema()["x-anx-standard-tools"].items()}


def optional_tools() -> dict[str, list[str]]:
    """Capability -> tools that belong to it but are not required."""
    return {name: list(tools) for name, tools in schema().get("x-anx-optional-tools", {}).items()}


@functools.lru_cache(maxsize=None)
def _tool_table() -> dict[str, str]:
    return {tool: capability for table in (standard_tools(), optional_tools())
            for capability, tools in table.items() for tool in tools}


def standard_capability(tool: str) -> str | None:
    """The capability a standard tool belongs to, from the contract table."""
    return _tool_table().get(tool)


def validate(block: Any) -> list[str]:
    """Problems with a device descriptor (empty when it conforms)."""
    validator = Draft202012Validator(schema())
    problems = []
    for error in sorted(validator.iter_errors(block), key=lambda item: [str(part) for part in item.path]):
        where = "/".join(str(part) for part in error.path)
        problems.append(f"{where}: {error.message}" if where else error.message)
    return problems


def tool_meta(descriptor: Any) -> dict:
    meta = descriptor.get("_meta") if isinstance(descriptor, dict) else None
    return meta if isinstance(meta, dict) else {}


def tool_capability(descriptor: Any) -> str | None:
    """A catalog tool's capability: its ``_meta`` tag, else the standard table."""
    value = tool_meta(descriptor).get(META_CAPABILITY)
    if isinstance(value, str) and value:
        return value
    name = descriptor.get("name") if isinstance(descriptor, dict) else None
    return standard_capability(name) if isinstance(name, str) else None


def is_internal(descriptor: Any) -> bool:
    """Internal tools (``_meta["anx/internal"] = true``) never appear in tools/list or toolsearch."""
    return tool_meta(descriptor).get(META_INTERNAL) is True
