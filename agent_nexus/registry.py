"""Service discovery.

Two sources, merged per service name:

* **Self-registration** - any directory under a scan root that contains
  ``mcp-service.json`` is a service. The manifest lives with the server, so a
  new server only has to drop that file next to its code.
* **Central config** - ``agent-nexus.json`` in the manager root (or, until
  the first write, the legacy ``mcp-manager.json``; see ``paths``). Its
  ``services`` entries override manifest fields (port, autostart, ...), and an
  entry with no matching manifest defines a service on its own (it then needs
  ``dir`` and ``command``). Edits made from the web console land here, so the
  server repositories themselves are never rewritten.

A manifest may list ``aliases``: former names whose ``services.<alias>``
entry still configures it (``device`` reads ``services.workspace``). An entry
under the current name wins key by key.

A service is one of two kinds. ``"process"`` (the default) is a server mcpm
starts, stops and restarts. ``"observe"`` is something mcpm only watches - a
browser, say - through a detector in ``observers``; it has no command, and
``start`` / ``stop`` refuse it.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import observers, paths, sections
from .plugins import schema as plugin_schema
from .paths import CONFIG_NAME, LEGACY_CONFIG_NAME, STATE_DIR_NAME  # noqa: F401 - re-exported

MANIFEST_NAME = "mcp-service.json"
DEFAULT_MANAGER_PORT = 8399
#: Development layouts: the code checkout next to (or as) the root.
BUILTIN_DIRS = ("builtins", "agent-nexus/builtins", "mcp-manager/builtins")
KINDS = ("process", "observe")

EDITABLE_FIELDS = {
    "description", "dir", "cwd", "command", "port", "host", "env", "health",
    "logs", "platforms", "autostart", "autorestart", "enabled", "icon",
}
#: The only field that means anything for an observe-only service.
OBSERVE_EDITABLE_FIELDS = {"enabled"}

#: Icons are fetched on every console refresh; keep them tiny.
ICON_MAX_BYTES = 10 * 1024
ICON_TYPES = {
    ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
}
#: Looked up in the service directory when the manifest names no ``icon``.
ICON_DEFAULTS = ("favicon.png", "favicon.svg", "favicon.ico", "icon.png")


class RegistryError(ValueError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    # utf-8-sig: PowerShell 5.1 `Set-Content -Encoding UTF8` prepends a BOM,
    # which plain utf-8 + json.loads rejects.
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise RegistryError(f"{path}: top level must be a JSON object")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class ServiceSpec:
    name: str
    dir: Path
    command: list[str] = field(default_factory=list)
    port: int | None = None
    host: str = "127.0.0.1"
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)
    health_path: str | None = None
    #: seconds between HTTP probes; the port itself is checked every tick
    #: without a request, so servers that log each request stay quiet
    health_interval: float = 30.0
    logs: list[str] = field(default_factory=list)
    #: icon path relative to ``dir``; empty means look for ``ICON_DEFAULTS``
    icon: str = ""
    platforms: list[str] = field(default_factory=list)
    autostart: bool = False
    autorestart: bool = False
    enabled: bool = True
    description: str = ""
    kind: str = "process"
    #: detector options for ``kind == "observe"``
    observe: dict[str, Any] = field(default_factory=dict)
    source: str = "manifest"  # manifest | config | manifest+config
    error: str = ""
    #: former names (manifest ``aliases``)
    aliases: list[str] = field(default_factory=list)
    #: manifest ``schema_version``: 1 when absent (today's format)
    schema_version: int = 1
    #: manifest ``requires.mcpkit`` (v2), e.g. ``">=0.2"``; empty when not declared
    requires_mcpkit: str = ""
    #: False for a configuration entry with no code behind it: the installer's
    #: template lists plugins (``enabled: false``) that are installed later.
    installed: bool = True
    #: manifest ``version``; empty for v1 manifests that do not declare one
    version: str = ""
    #: required settings / secrets with no value (design §6.3: ``needs_config``)
    missing: list[str] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return not self.platforms or sys.platform in self.platforms

    @property
    def observed(self) -> bool:
        return self.kind == "observe"

    @property
    def runnable(self) -> bool:
        """Something mcpm could start: never true for an observe-only service."""
        return (self.enabled and self.supported and not self.error and not self.observed
                and self.installed and not self.missing)

    def _subst(self, value: str) -> str:
        return (value.replace("{port}", "" if self.port is None else str(self.port))
                     .replace("{host}", self.host)
                     .replace("{dir}", str(self.dir)))

    def resolved_command(self) -> list[str]:
        return [self._subst(a) for a in self.command]

    def resolved_env(self) -> dict[str, str]:
        return {k: self._subst(v) for k, v in self.env.items()}

    def resolved_cwd(self) -> Path:
        return (self.dir / self.cwd).resolve()

    def extra_log_files(self) -> list[Path]:
        return [(self.dir / p).resolve() for p in self.logs]

    def icon_file(self) -> tuple[Path | None, str]:
        """The icon to show, or ``None`` and why (empty when there simply is none)."""
        base = self.dir.resolve()
        for rel in ([self.icon] if self.icon else ICON_DEFAULTS):
            path = (base / rel).resolve()
            try:
                if not path.is_file():
                    continue
                # The console serves this file; a manifest must not point it elsewhere.
                if not path.is_relative_to(base):
                    return None, f"icon must be inside the service directory: {rel}"
                if path.suffix.lower() not in ICON_TYPES:
                    return None, f"unsupported icon type {path.suffix!r} (use {', '.join(ICON_TYPES)})"
                size = path.stat().st_size
            except OSError:
                continue
            if size > ICON_MAX_BYTES:
                return None, f"icon {rel} is {size / 1024:.1f}KB, limit is {ICON_MAX_BYTES // 1024}KB"
            return path, ""
        return None, (f"icon not found: {self.icon}" if self.icon else "")


@dataclass
class ManagerConfig:
    root: Path
    host: str = "127.0.0.1"
    port: int = DEFAULT_MANAGER_PORT
    scan: list[str] = field(default_factory=lambda: ["."])
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    builtins: bool = False
    #: ``terminal.enabled``: whether the gateway may open a remote terminal here (default on).
    terminal: bool = True
    #: ``desktop.enabled``: whether other devices may view and control this desktop (default on).
    desktop: bool = True
    #: ``relay.mode``: forward other devices' streams (docs/relay-mesh.md): on / off / auto.
    #: ``auto`` is off for now; it will pick well-reachable, always-on machines.
    relay: str = "auto"

    @property
    def path(self) -> Path:
        """The file configuration is read from (``agent-nexus.json`` when it exists)."""
        return paths.config_path(self.root)

    @property
    def write_path(self) -> Path:
        """Where edits are saved; never the legacy ``mcp-manager.json``."""
        return paths.write_path(self.root)

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIR_NAME

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def load_manager_config(root: Path) -> ManagerConfig:
    cfg = ManagerConfig(root=Path(root).resolve())
    if cfg.path.exists():
        data = read_json(cfg.path)
        mgr = data.get("manager") or {}
        cfg.host = str(mgr.get("host", cfg.host))
        cfg.port = int(mgr.get("port", cfg.port))
        cfg.scan = sections.plugin_paths(data) or cfg.scan
        cfg.overrides = sections.plugin_entries(data)
        cfg.builtins = bool(data.get("builtins", any((cfg.root / rel).is_dir() for rel in BUILTIN_DIRS)))
        terminal = data.get("terminal")
        cfg.terminal = not (isinstance(terminal, dict) and terminal.get("enabled") is False)
        desktop = data.get("desktop")
        cfg.desktop = not (isinstance(desktop, dict) and desktop.get("enabled") is False)
        relay = data.get("relay")
        if isinstance(relay, dict) and relay.get("mode") in ("on", "off", "auto"):
            cfg.relay = relay["mode"]
    return cfg


def save_override(cfg: ManagerConfig, name: str, updates: dict[str, Any]) -> None:
    unknown = set(updates) - EDITABLE_FIELDS
    if unknown:
        raise RegistryError(f"not editable: {sorted(unknown)}")
    if "port" in updates and updates["port"] is not None:
        updates["port"] = _check_port(updates["port"])
    if cfg.path.exists():
        data = read_json(cfg.path)
    else:
        data = {"manager": {"host": cfg.host, "port": cfg.port}, "scan": cfg.scan, "services": {}}
    # A migrated file keeps its ``plugins`` section; an older one its ``services``.
    entries = data.setdefault(sections.entry_section(data), {})
    entries.setdefault(name, {}).update(updates)
    write_json(cfg.write_path, data)
    cfg.overrides = sections.plugin_entries(data)


def _check_port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise RegistryError(f"invalid port: {value!r}") from None
    if not 0 < port < 65536:
        raise RegistryError(f"port out of range: {port}")
    return port


def discover(cfg: ManagerConfig) -> dict[str, ServiceSpec]:
    found: dict[str, tuple[dict[str, Any], Path, str]] = {}
    bundled = Path(__file__).parent / "builtins"
    if not bundled.is_dir():
        bundled = Path(__file__).parent.parent / "builtins"
    bases = ([bundled] if cfg.builtins else []) + [(cfg.root / rel).resolve() for rel in cfg.scan]
    seen: set[Path] = set()
    # Installed plugins (plugins/<name>/<current version>) and link-mode ones
    # (plugins/plugins.lock.json) are plugin directories themselves; they come
    # after the scanned ones.
    singles = sections.managed_dirs(cfg.root) + sections.link_dirs(cfg.root)
    groups = [(base, None) for base in bases] + [(None, path) for path in singles]
    for base, single in groups:
        if base is not None and not base.is_dir():
            continue
        children = [single] if single is not None else sorted(
            p for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))
        for child in children:
            if not child.is_dir():
                continue
            child = child.resolve()
            if child in seen:
                continue
            seen.add(child)
            manifest = child / MANIFEST_NAME
            if not manifest.is_file():
                continue
            try:
                data = read_json(manifest)
            except (OSError, ValueError) as exc:
                data = {"_error": f"{manifest.name}: {exc}"}
            found[str(data.get("name") or child.name)] = (data, child, "manifest")

    aliases = {}
    for name, (data, _, _) in found.items():
        for alias in data.get("aliases") or []:
            if isinstance(alias, str) and alias not in found:
                aliases.setdefault(alias, name)
    overrides: dict[str, dict[str, Any]] = {}
    for name, override in cfg.overrides.items():
        target = aliases.get(name, name)
        if target != name:  # former name: the current one wins key by key
            current = overrides.get(target, {})
            override = {**override, **current, "env": {**(override.get("env") or {}), **(current.get("env") or {})}}
        elif name in overrides:  # the alias was seen first
            previous = overrides[name]
            override = {**previous, **override, "env": {**(previous.get("env") or {}), **(override.get("env") or {})}}
        overrides[target] = override

    manifests = {name: data for name, (data, _, _) in found.items()}
    for name, override in overrides.items():
        if name in found:
            data, directory, _ = found[name]
            merged = {**data, **override}
            merged["env"] = {**(data.get("env") or {}), **(override.get("env") or {})}
            found[name] = (merged, directory, "manifest+config")
        else:
            directory = (cfg.root / str(override.get("dir", name))).resolve()
            found[name] = (dict(override), directory, "config")

    specs = {}
    for name, (data, directory, source) in found.items():
        if source == "config" and "command" not in data and not directory.is_dir():
            specs[name] = _not_installed(name, data, directory)
            continue
        spec = specs[name] = _build(name, data, directory, source)
        if not spec.observed and not data.get("_error"):
            try:
                spec.missing = plugin_schema.missing(cfg.root, name, manifests.get(name), overrides.get(name))
            except (OSError, ValueError, TypeError):
                spec.missing = []
    _flag_port_clashes(specs, cfg.port)
    return specs


def _not_installed(name: str, data: dict[str, Any], directory: Path) -> ServiceSpec:
    """A configuration entry for a plugin whose code is not here (yet): ``未安装``, not an error."""
    return ServiceSpec(name=name, dir=directory, source="config", installed=False,
                       enabled=bool(data.get("enabled", True)), autostart=bool(data.get("autostart", False)),
                       description=str(data.get("description", "")))


def _build(name: str, data: dict[str, Any], directory: Path, source: str) -> ServiceSpec:
    spec = ServiceSpec(name=name, dir=directory, source=source)
    errors: list[str] = []
    if data.get("_error"):
        errors.append(str(data["_error"]))
    spec.aliases = [str(a) for a in data.get("aliases") or [] if isinstance(a, str)]
    try:
        spec.schema_version = int(data.get("schema_version") or 1)
    except (TypeError, ValueError):
        errors.append(f"invalid schema_version: {data.get('schema_version')!r}")
    requires = data.get("requires")
    if isinstance(requires, dict) and isinstance(requires.get("mcpkit"), str):
        spec.requires_mcpkit = requires["mcpkit"]

    spec.kind = str(data.get("kind") or "process")
    if spec.kind not in KINDS:
        errors.append(f"unknown kind {spec.kind!r} (use {' or '.join(KINDS)})")
    if spec.observed:
        return _build_observed(spec, data, errors)

    command = data.get("command")
    if isinstance(command, dict):  # per-platform: {"win32": [...], "default": [...]}
        command = command.get(sys.platform) or command.get("default")
    if isinstance(command, list) and command and all(isinstance(a, str) for a in command):
        spec.command = list(command)
    elif not data.get("_error"):
        errors.append("command must be a non-empty list of strings")

    if data.get("port") is not None:
        try:
            spec.port = _check_port(data["port"])
        except RegistryError as exc:
            errors.append(str(exc))

    spec.host = str(data.get("host", spec.host))
    spec.cwd = str(data.get("cwd", spec.cwd))
    spec.env = {str(k): str(v) for k, v in (data.get("env") or {}).items()}
    health = data.get("health")
    spec.health_path = health.get("path") if isinstance(health, dict) else health
    if isinstance(health, dict) and health.get("interval") is not None:
        try:
            spec.health_interval = max(5.0, float(health["interval"]))
        except (TypeError, ValueError):
            errors.append(f"invalid health.interval: {health['interval']!r}")
    spec.logs = [str(p) for p in (data.get("logs") or [])]
    spec.icon = str(data.get("icon") or "")
    spec.platforms = [str(p) for p in (data.get("platforms") or [])]
    spec.autostart = bool(data.get("autostart", False))
    spec.autorestart = bool(data.get("autorestart", False))
    spec.enabled = bool(data.get("enabled", True))
    spec.description = str(data.get("description", ""))
    spec.version = str(data.get("version") or "")

    if not directory.is_dir():
        errors.append(f"directory not found: {directory}")
    spec.error = "; ".join(errors)
    return spec


def _build_observed(spec: ServiceSpec, data: dict[str, Any], errors: list[str]) -> ServiceSpec:
    """Only what describes and locates the thing being watched.

    ``command``, ``port``, ``autostart`` and ``autorestart`` are dropped rather
    than rejected, so an override saved against the service cannot turn a
    watched browser back into something mcpm might start, stop or treat as
    owning a port.
    """
    problem = observers.check(data.get("observe"))
    if problem:
        errors.append(problem)
    else:
        spec.observe = dict(data["observe"])
        interval = spec.observe.get("interval", observers.DEFAULT_INTERVAL_S)
        spec.health_interval = max(5.0, float(interval))
    spec.icon = str(data.get("icon") or "")
    spec.platforms = [str(p) for p in (data.get("platforms") or [])]
    spec.enabled = bool(data.get("enabled", True))
    spec.description = str(data.get("description", ""))
    if not spec.dir.is_dir():
        errors.append(f"directory not found: {spec.dir}")
    spec.error = "; ".join(errors)
    return spec


def _flag_port_clashes(specs: dict[str, ServiceSpec], manager_port: int) -> None:
    owners: dict[int, list[str]] = {}
    for spec in specs.values():
        if spec.port is not None and spec.enabled and spec.installed:
            owners.setdefault(spec.port, []).append(spec.name)
    for port, names in owners.items():
        clash = [n for n in names] if len(names) > 1 else []
        if port == manager_port:
            clash = names
        for n in clash:
            others = [o for o in names if o != n] or ["agent-nexus"]
            msg = f"port {port} also used by {', '.join(others)}"
            specs[n].error = f"{specs[n].error}; {msg}" if specs[n].error else msg
