"""Reading the sections of a configuration file, old shape or new.

``agent-nexus.json`` (docs/design.md section 4.2) renames three sections of
the legacy ``mcp-manager.json``:

* ``services.<x>`` -> ``plugins.<x>`` (``services.workspace`` -> ``plugins.device``).
  A plugin entry is a service entry plus an optional ``install`` object that
  only records how the plugin is (to be) installed.
* ``scan`` -> ``plugin_paths``.
* ``cloud.mcps`` -> top-level ``connectors`` (section 7.2).

Both shapes are accepted everywhere until 1.0: a file written by an older
console may still carry ``services``, and the console keeps writing into the
section a file already uses. When both are present the new section wins key
by key (``env``, ``settings`` and ``auth`` one level deeper), the same rule the
registry applies to a renamed plugin's former name.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

#: Plugin-entry keys merged one level deeper rather than replaced.
DEEP_KEYS = ("env", "settings", "auth")
#: Keys of a plugin entry that describe installation, not configuration.
INSTALL_KEY = "install"
LOCK_PATH = Path("plugins") / "plugins.lock.json"
#: ``{"$secret": "<name>"}`` stands where a plain secret value used to be.
SECRET_REF = "$secret"


def merge_entry(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    merged = {**base, **over}
    for key in DEEP_KEYS:
        if isinstance(base.get(key), dict) and isinstance(over.get(key), dict):
            merged[key] = {**base[key], **over[key]}
    return merged


def plugin_entries(data: Mapping[str, Any], *, keep_install: bool = False) -> dict[str, dict[str, Any]]:
    """``services`` overlaid by ``plugins``: every configured plugin by name."""
    result: dict[str, dict[str, Any]] = {}
    for section in ("services", "plugins"):
        entries = data.get(section) or {}
        if not isinstance(entries, dict):
            continue
        for name, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            entry = dict(entry)
            if not keep_install:
                entry.pop(INSTALL_KEY, None)
            result[str(name)] = merge_entry(result.get(str(name), {}), entry)
    return result


def entry_section(data: Mapping[str, Any]) -> str:
    """Where the console writes: the section the file already uses."""
    return "plugins" if "plugins" in data else "services"


def plugin_paths(data: Mapping[str, Any]) -> list[str] | None:
    value = data.get("plugin_paths", data.get("scan"))
    return [str(item) for item in value] if isinstance(value, list) and value else None


# -- connectors <-> cloud.mcps --------------------------------------------------------

def connector_from_mcp(entry: Mapping[str, Any]) -> dict[str, Any]:
    """A legacy ``cloud.mcps`` entry as a section 7.2 connector."""
    entry = dict(entry)
    connector: dict[str, Any] = {}
    if "url" in entry:
        connector["transport"] = entry.pop("transport", "http")
        connector["url"] = entry.pop("url")
        token_env = entry.pop("token_env", None)
        connector["auth"] = {"type": "bearer", "token_env": token_env} if token_env else {"type": "none"}
    else:
        connector["transport"] = "stdio"
        for key in ("command", "cwd", "env"):
            if key in entry:
                connector[key] = entry.pop(key)
    if "description" in entry:
        connector["title"] = entry["description"]
    connector.update(entry)  # enabled, description and anything newer pass through
    return connector


def mcp_from_connector(connector: Mapping[str, Any]) -> dict[str, Any] | None:
    """The legacy shape the current agent understands, or ``None`` when it cannot be expressed.

    Header and OAuth connectors arrive with M10/M11; until then they are not
    offered rather than offered without their credentials.
    """
    connector = dict(connector)
    transport = connector.pop("transport", "auto")
    auth = connector.pop("auth", None) or {"type": "none"}
    connector.pop("title", None)
    if transport == "stdio":
        return {k: v for k, v in connector.items() if k in ("command", "cwd", "env", "enabled", "description")}
    if "url" not in connector or transport not in ("http", "sse", "auto"):
        return None
    entry = {"url": connector["url"], "transport": "http" if transport == "auto" else transport}
    if auth.get("type") == "bearer" and auth.get("token_env"):
        entry["token_env"] = auth["token_env"]
    elif auth.get("type") == "bearer" and auth.get("token_secret"):
        entry["token_secret"] = auth["token_secret"]  # the client's 连接器 page (local_connectors.py)
    elif auth.get("type") == "headers" and auth.get("headers_secret"):
        entry["headers_secret"] = auth["headers_secret"]
    elif auth.get("type") not in (None, "none"):
        return None
    for key in ("enabled", "description"):
        if key in connector:
            entry[key] = connector[key]
    return entry


def extra_mcps(data: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``cloud.mcps`` plus the connectors the current agent can serve; connectors win."""
    result = dict((data.get("cloud") or {}).get("mcps") or {})
    for name, connector in (data.get("connectors") or {}).items():
        if isinstance(connector, dict):
            entry = mcp_from_connector(connector)
            if entry is not None:
                result[str(name)] = entry
    return result


# -- link plugins -----------------------------------------------------------------------

def read_lock(root: Path) -> dict[str, Any]:
    path = Path(root) / LOCK_PATH
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def link_dirs(root: Path) -> list[Path]:
    """Directories of plugins registered in link mode (code stays where it is)."""
    result = []
    for entry in read_lock(root).values():
        if isinstance(entry, dict) and entry.get("mode") == "link" and isinstance(entry.get("path"), str):
            result.append(Path(entry["path"]))
    return result


def managed_dirs(root: Path) -> list[Path]:
    """``plugins/<name>/<version>`` for every installed plugin, by its ``current`` file (design §6.4)."""
    base = Path(root) / "plugins"
    if not base.is_dir():
        return []
    result = []
    for child in sorted(base.iterdir()):
        current = child / "current"
        if child.name.startswith(".") or not current.is_file():
            continue
        try:
            version = current.read_text(encoding="utf-8-sig").strip()
        except OSError:
            continue
        if version and "/" not in version and "\\" not in version and not version.startswith("."):
            result.append(child / version)
    return result


# -- secret references ------------------------------------------------------------------

def is_secret_ref(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {SECRET_REF} and isinstance(value[SECRET_REF], str)


def resolve_secret_refs(value: Any, read: Callable[[str], str]) -> Any:
    """``value`` with every ``{"$secret": name}`` replaced by ``read(name)``."""
    if is_secret_ref(value):
        return read(value[SECRET_REF])
    if isinstance(value, dict):
        return {k: resolve_secret_refs(v, read) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_secret_refs(v, read) for v in value]
    return value
