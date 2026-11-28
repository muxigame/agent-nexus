"""Where the configuration root is, and which file in it is the configuration.

The same rules are implemented by ``find_root`` in the desktop shell
(``desktop/src-tauri/src/bootstrap.rs``):

1. ``ANX_ROOT``, else the legacy ``MCPM_ROOT``.
2. Development: the nearest directory upward (from the working directory, then
   from this package) holding ``agent-nexus.json`` or ``mcp-manager.json``. A
   versioned runtime (``<root>/runtime/<tag>/`` with ``.anx-runtime-complete``)
   is never the root: its bundled config stands for ``<root>``.
3. Installed: ``%LOCALAPPDATA%\\AgentNexus`` / ``$XDG_DATA_HOME/agent-nexus``.
   The legacy installed root (``%LOCALAPPDATA%\\Roc\\MCPManager\\client``,
   ``~/.local/share/roc-mcp-manager/client``) is never used in place: it stays
   the rollback target, and copying it over is ``anx migrate`` (M3).

Inside a root ``agent-nexus.json`` wins over ``mcp-manager.json``. A root that
only has the legacy file is read but never written: the first write creates
``agent-nexus.json`` from it and leaves ``mcp-manager.json`` byte-for-byte
unchanged, so the previous release can still be started as a rollback.
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from . import env

CONFIG_NAME = "agent-nexus.json"
LEGACY_CONFIG_NAME = "mcp-manager.json"
CONFIG_NAMES = (CONFIG_NAME, LEGACY_CONFIG_NAME)
STATE_DIR_NAME = ".anx"
LEGACY_STATE_DIR_NAME = ".mcp-manager"

WINDOWS_DATA_DIR = "AgentNexus"
LINUX_DATA_DIR = "agent-nexus"
LEGACY_WINDOWS_DATA_DIR = Path("Roc/MCPManager/client")
LEGACY_LINUX_DATA_DIR = Path("roc-mcp-manager/client")


def has_config(directory: Path) -> bool:
    return any((Path(directory) / name).is_file() for name in CONFIG_NAMES)


def config_path(root: Path) -> Path:
    """The file to read: ``agent-nexus.json``, else ``mcp-manager.json``.

    When neither exists this is the new name, which is also where a write goes.
    """
    root = Path(root)
    for name in CONFIG_NAMES:
        if (root / name).is_file():
            return root / name
    return root / CONFIG_NAME


def write_path(root: Path) -> Path:
    """Writes always go to ``agent-nexus.json``; the legacy file stays untouched."""
    return Path(root) / CONFIG_NAME


def is_legacy_only(root: Path) -> bool:
    root = Path(root)
    return not (root / CONFIG_NAME).is_file() and (root / LEGACY_CONFIG_NAME).is_file()


def read_config(root: Path) -> dict[str, Any]:
    """The configuration object, ``{}`` when the root has none."""
    path = config_path(root)
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a JSON object")
    return data


def find_root(starts: Iterable[Path]) -> Path | None:
    for start in starts:
        start = Path(start).resolve()
        for directory in (start, *start.parents):
            if has_config(directory):
                return directory
    return None


def env_root(environ: Mapping[str, str] | None = None) -> Path | None:
    value = env.get("ROOT", environ=environ)
    return Path(value).resolve() if value else None


def _data_home(environ: Mapping[str, str]) -> Path | None:
    configured = env.get("DATA_HOME", environ=environ)
    if configured:
        return Path(configured)
    if sys.platform == "win32":
        local = environ.get("LOCALAPPDATA")
        return Path(local) if local else None
    xdg = environ.get("XDG_DATA_HOME")
    return Path(xdg) if xdg else Path.home() / ".local" / "share"


def installed_root(environ: Mapping[str, str] | None = None) -> Path | None:
    environ = os.environ if environ is None else environ
    base = _data_home(environ)
    if base is None:
        return None
    return base / (WINDOWS_DATA_DIR if sys.platform == "win32" else LINUX_DATA_DIR)


def legacy_installed_root(environ: Mapping[str, str] | None = None) -> Path | None:
    environ = os.environ if environ is None else environ
    base = _data_home(environ)
    if base is None:
        return None
    return base / (LEGACY_WINDOWS_DATA_DIR if sys.platform == "win32" else LEGACY_LINUX_DATA_DIR)


#: Written by the desktop shell into a finished versioned runtime (``<root>/runtime/<version>-<hash>/``).
RUNTIME_MARKER = ".anx-runtime-complete"


def runtime_root(path: Path) -> Path | None:
    """The data root when ``path`` lies inside an installed versioned runtime (``<root>/runtime/<tag>/...`` with the
    completion marker), else None. Such a runtime also holds the bundled ``agent-nexus.json``, which must not be
    taken for the root (an ``anx`` launched from the runtime's venv, e.g. the per-run or PATH shim)."""
    for parent in [path, *path.parents]:
        if parent.parent.name == "runtime" and (parent / RUNTIME_MARKER).is_file():
            return parent.parent.parent
    return None


def default_root(environ: Mapping[str, str] | None = None, cwd: Path | None = None,
                 package: Path | None = None) -> Path:
    explicit = env_root(environ)
    if explicit is not None:
        return explicit
    package = package or Path(__file__).resolve().parent
    found = find_root([cwd or Path.cwd()])
    if found is not None:
        return runtime_root(found) or found
    runtime = runtime_root(package)
    if runtime is not None:
        return runtime
    found = find_root([package])
    if found is not None:
        return found
    installed = installed_root(environ)
    if installed is not None:
        return installed.resolve()
    return Path(__file__).resolve().parents[1]
