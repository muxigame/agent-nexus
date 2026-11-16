"""``anx migrate`` and ``anx-gateway migrate`` (docs/design.md sections 4.2-4.4).

Copy, never move: every source file is only read. ``mcp-manager.json``, the
old ``secrets`` and state directories and the old gateway files keep their
bytes and their mtimes, so the previous release can still be started as the
rollback. Nothing existing in the target is overwritten either: a target file
that is already there with the same content is skipped, one with different
content is reported as a conflict and left alone. Running the same migration
twice therefore changes nothing the second time.

Without ``--apply`` only the plan is printed. Plans, output and the report
(``.anx/migration-report.json``) carry paths and, for non-secret files, sha256
digests - never a value read from a secret or a plain-text password.
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths, sections

REPORT = Path(paths.STATE_DIR_NAME) / "migration-report.json"
#: Where the business plugins are published (docs/design.md section 6.8).
PLUGIN_REPOS = {
    "wechat": "anx-plugin-wecom", "payment": "anx-plugin-payment", "jubian-go": "anx-plugin-jubian-go",
    "ones": "anx-plugin-ones", "unity": "anx-plugin-unity", "chrome-devtools": "anx-plugin-chrome-devtools",
}
REPO_URL = "https://github.com/muxigame/{repo}.git"
DEVICE, LEGACY_DEVICE = "device", "workspace"
LEGACY_DEVICE_DIR = "codex-mcp"
#: Where an old root keeps plugin code (installed client, then development checkouts).
BUILTIN_DIRS = ("builtins", "mcp-manager/builtins", "agent-nexus/builtins")
#: Settings keys whose plain value is a credential (section 4.2 key-name rule) ...
SECRET_KEY = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|apikey|access[_-]?key)$")
#: ... except keys that only *name* a secret, a variable or a file.
REFERENCE_KEY = re.compile(r"(?i)(^(token|password)_secret$|_secrets$|_(env|file|path|url|dir|name|ttl_s|mode|id)$)")
SECRET_NAME_PART = re.compile(r"[^A-Za-z0-9_.-]")


class MigrationError(Exception):
    """A problem that stops the migration, phrased so it is safe to print."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- plan ------------------------------------------------------------------------------------

@dataclass
class Item:
    """One step. ``op`` is ``copy`` / ``write`` (will happen), ``same`` (already
    there, identical), ``conflict`` (target differs; left alone) or ``skip``."""

    op: str
    kind: str
    dst: Path | None = None
    src: Path | None = None
    note: str = ""
    #: digest of the content, recorded for non-secret files only
    sha256: str = ""
    secret: bool = False
    #: content for ``write`` (bytes, or a callable producing them at apply time)
    content: bytes | Callable[[], bytes] | None = field(default=None, repr=False)
    #: custom copier (sqlite backup) for ``copy``
    copier: Callable[[Path, Path], None] | None = field(default=None, repr=False)
    #: what the step came from, for display (a vault manifest path, say)
    ref: str = ""

    def public(self, base: Path | None = None, source_base: Path | None = None) -> dict[str, Any]:
        """Paths relative to the target (``dst``) and source (``src``) roots where possible."""
        def show(path: Path | None, relative_to: Path | None) -> str:
            if path is None:
                return ""
            if relative_to is not None:
                try:
                    return Path(path).relative_to(relative_to).as_posix()
                except ValueError:
                    pass
            return str(path)
        entry = {"op": self.op, "kind": self.kind, "src": show(self.src, source_base), "dst": show(self.dst, base)}
        if self.note:
            entry["note"] = self.note
        if self.sha256 and not self.secret:
            entry["sha256"] = self.sha256
        return entry


@dataclass
class Plan:
    source: Path
    target: Path
    items: list[Item] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def conflicts(self) -> list[Item]:
        return [item for item in self.items if item.op == "conflict"]

    @property
    def pending(self) -> list[Item]:
        return [item for item in self.items if item.op in ("copy", "write")]

    def add_file(self, kind: str, src: Path, dst: Path, *, secret: bool = False, note: str = "",
                 copier: Callable[[Path, Path], None] | None = None,
                 same: Callable[[Path, Path], bool] | None = None) -> Item:
        digest = sha256_file(src)
        if dst.exists():
            equal = dst.is_file() and (sha256_file(dst) == digest or (same is not None and same(src, dst)))
            op = "same" if equal else "conflict"
        else:
            op = "copy"
        item = Item(op, kind, dst, src, note, digest, secret, copier=copier)
        self.items.append(item)
        return item

    def add_content(self, kind: str, dst: Path, content: bytes, *, secret: bool = False, note: str = "",
                    same: Callable[[bytes], bool] | None = None) -> Item:
        digest = sha256_bytes(content)
        if dst.exists():
            current = dst.read_bytes() if dst.is_file() else None
            equal = current is not None and (same(current) if same else current == content)
            op = "same" if equal else "conflict"
        else:
            op = "write"
        item = Item(op, kind, dst, None, note, digest, secret, content=content)
        self.items.append(item)
        return item


# -- reading the old root ------------------------------------------------------------------

def _read_json_bytes(raw: bytes, where: Path) -> dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise MigrationError(f"{where} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise MigrationError(f"{where}: top level must be a JSON object")
    return data


def scan_manifests(root: Path, data: Mapping[str, Any]) -> dict[str, tuple[Path, dict[str, Any]]]:
    """Plugin manifests reachable from an old root: builtins dirs, then ``scan``.

    The old device plugin (``codex-mcp``, manifest name ``workspace``) is
    reported as ``device``. Read-only.
    """
    bases = [root / rel for rel in BUILTIN_DIRS]
    for rel in sections.plugin_paths(data) or []:
        bases.append(root / rel)
    found: dict[str, tuple[Path, dict[str, Any]]] = {}
    seen: set[Path] = set()
    for base in bases:
        if not base.is_dir():
            continue
        for child in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")):
            resolved = child.resolve()
            manifest = child / "mcp-service.json"
            if resolved in seen or not manifest.is_file():
                continue
            seen.add(resolved)
            try:
                content = json.loads(manifest.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if not isinstance(content, dict):
                continue
            name = str(content.get("name") or child.name)
            if name == LEGACY_DEVICE or child.name == LEGACY_DEVICE_DIR:
                name = DEVICE
            found.setdefault(name, (resolved, content))
    return found


def _is_builtin_dir(root: Path, directory: Path) -> bool:
    return any(directory.parent == (root / rel).resolve() for rel in BUILTIN_DIRS)


def suggested_source(name: str) -> str | None:
    repo = PLUGIN_REPOS.get(name)
    return REPO_URL.format(repo=repo) if repo else None


def _declared_secret_keys(manifest: Mapping[str, Any] | None) -> set[str]:
    """Settings keys a (v2) manifest declares as secrets."""
    keys: set[str] = set()
    for item in (manifest or {}).get("secrets") or []:
        if isinstance(item, str):
            keys.add(item)
        elif isinstance(item, dict):
            for field_name in ("setting", "key", "name"):
                if isinstance(item.get(field_name), str):
                    keys.add(item[field_name])
                    break
    return keys


def is_secret_setting(key: str, declared: set[str] = frozenset()) -> bool:
    return key in declared or (bool(SECRET_KEY.search(key)) and not REFERENCE_KEY.search(key))


def _absolute(value: str, base: Path) -> str:
    path = Path(value)
    return str(path if path.is_absolute() else (base / path).resolve())


# -- building agent-nexus.json --------------------------------------------------------------

@dataclass
class SecretMove:
    plugin: str
    key: str
    name: str
    value: str


def build_config(data: Mapping[str, Any], source: Path, target: Path, *, manifests=None,
                 port_offset: int = 0, agent_id: str | None = None, cloud_url: str | None = None,
                 linked: Mapping[str, Path] | None = None) -> tuple[dict[str, Any], list[SecretMove], list[str]]:
    """The new configuration (without ``migrated_from``), plain secrets to move, warnings."""
    manifests = manifests or {}
    linked = linked or {}
    warnings: list[str] = []
    relocate = source.resolve() != target.resolve()
    old = copy.deepcopy(dict(data))
    new: dict[str, Any] = {}
    for key, value in old.items():
        if key not in ("services", "plugins", "scan", "plugin_paths", "cloud", "connectors", "migrated_from"):
            new[key] = value

    # plugins
    entries = sections.plugin_entries(old, keep_install=True)
    plugins: dict[str, dict[str, Any]] = {}
    if LEGACY_DEVICE in entries:
        plugins[DEVICE] = entries.pop(LEGACY_DEVICE)
    for name, entry in entries.items():
        plugins[name] = sections.merge_entry(plugins.get(name, {}), entry)
    for name in manifests:  # a plugin that only had its manifest still needs a port when offset
        if port_offset and name not in plugins and manifests[name][1].get("port") is not None:
            plugins[name] = {}
    moves: list[SecretMove] = []
    for name, entry in plugins.items():
        manifest = manifests.get(name, (None, {}))[1]
        if name != DEVICE and "install" not in entry:
            install: dict[str, Any] = {"state": "pending_install"}
            if name in linked:
                install = {"state": "linked", "path": str(linked[name])}
            source_url = suggested_source(name)
            if source_url:
                install["source"] = source_url
            entry["install"] = install
        settings = entry.get("settings")
        if isinstance(settings, dict):
            declared = _declared_secret_keys(manifest)
            for key, value in list(settings.items()):
                if isinstance(value, str) and value and is_secret_setting(key, declared):
                    secret_name = f"{SECRET_NAME_PART.sub('_', name)}/{SECRET_NAME_PART.sub('_', key)}"
                    moves.append(SecretMove(name, key, secret_name, value))
                    settings[key] = {sections.SECRET_REF: secret_name}
        if port_offset:
            port = entry.get("port", manifest.get("port"))
            if isinstance(port, int):
                entry["port"] = port + port_offset
    if plugins:
        new["plugins"] = plugins

    paths_value = sections.plugin_paths(old)
    if paths_value:
        new["plugin_paths"] = [_absolute(p, source) for p in paths_value] if relocate else paths_value

    # cloud and connectors
    connectors = dict(old.get("connectors") or {})
    cloud = old.get("cloud")
    if isinstance(cloud, dict):
        cloud = dict(cloud)
        for name, entry in (cloud.pop("mcps", None) or {}).items():
            if isinstance(entry, dict):
                if name in connectors:
                    warnings.append(f"cloud.mcps.{name}: a connector of that name exists; kept the connector")
                    continue
                connectors[name] = sections.connector_from_mcp(entry)
                if relocate and "cwd" in connectors[name]:
                    connectors[name]["cwd"] = _absolute(str(connectors[name]["cwd"]), source)
        workspaces = cloud.get("workspaces")
        if isinstance(workspaces, dict):
            fixed = {}
            for key, value in workspaces.items():
                value = {"path": value} if isinstance(value, str) else dict(value)
                if value.get("mcp") == LEGACY_DEVICE:
                    value["mcp"] = DEVICE
                if relocate and isinstance(value.get("path"), str):
                    value["path"] = _absolute(value["path"], source)
                fixed[key] = value
            cloud["workspaces"] = fixed
        if agent_id:
            cloud["agent_id"] = agent_id
        if cloud_url:
            cloud["url"] = cloud_url
        new["cloud"] = cloud
    elif agent_id or cloud_url:
        warnings.append("--agent-id/--cloud-url given but the source has no cloud section; ignored")
    if connectors:
        new["connectors"] = connectors

    if port_offset:
        manager = dict(new.get("manager") or {})
        manager["port"] = int(manager.get("port", 8399)) + port_offset
        new["manager"] = manager
    return new, moves, warnings


def _without_timestamp(data: Any) -> Any:
    if isinstance(data, dict):
        data = dict(data)
        if isinstance(data.get("migrated_from"), dict):
            data["migrated_from"] = {k: v for k, v in data["migrated_from"].items() if k != "at"}
    return data


def _config_bytes(data: Mapping[str, Any]) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _same_config(new: Mapping[str, Any]) -> Callable[[bytes], bool]:
    def same(current: bytes) -> bool:
        try:
            existing = json.loads(current.decode("utf-8-sig"))
        except (UnicodeError, ValueError):
            return False
        return _without_timestamp(existing) == _without_timestamp(dict(new))
    return same


def plan_migration(source: Path, target: Path, *, source_file: Path | None = None, port_offset: int = 0,
                   agent_id: str | None = None, cloud_url: str | None = None) -> Plan:
    source, target = Path(source).resolve(), Path(target).resolve()
    config_file = Path(source_file).resolve() if source_file else source / paths.LEGACY_CONFIG_NAME
    if not config_file.is_file():
        raise MigrationError(f"no {config_file.name} to migrate from in {config_file.parent}")
    if config_file == (target / paths.CONFIG_NAME):
        raise MigrationError("the source file is the target agent-nexus.json")
    raw = config_file.read_bytes()
    data = _read_json_bytes(raw, config_file)
    plan = Plan(source, target)

    manifests = scan_manifests(source, data)
    # Old plugin code stays where it is: registered as link plugins (section 6.4).
    linked = {name: directory for name, (directory, _) in manifests.items()
              if name != DEVICE and _is_builtin_dir(source, directory)}

    config, moves, warnings = build_config(data, source, target, manifests=manifests, port_offset=port_offset,
                                           agent_id=agent_id, cloud_url=cloud_url, linked=linked)
    plan.warnings.extend(warnings)

    # plain-text secrets -> secrets/<plugin>/<key>; a conflicting file keeps the value in place
    for move in moves:
        dst = target / "secrets" / Path(*move.name.split("/"))
        item = plan.add_content("plain secret", dst, (move.value + "\n").encode("utf-8"), secret=True,
                                note=f"plugins.{move.plugin}.settings.{move.key}",
                                same=lambda current, value=move.value: current.decode("utf-8-sig", "replace").strip() == value)
        if item.op == "conflict":
            config["plugins"][move.plugin]["settings"][move.key] = move.value
            plan.warnings.append(f"plugins.{move.plugin}.settings.{move.key}: {dst} exists with other content; "
                                 "the value stays in the configuration")

    config["migrated_from"] = {"path": str(config_file), "sha256": sha256_bytes(raw), "at": _now()}
    plan.config = config
    plan.add_content("config", target / paths.CONFIG_NAME, _config_bytes(config), same=_same_config(config))

    # secrets/** as they are, plus workspace/* again as device/*
    old_secrets = source / "secrets"
    if old_secrets.is_dir():
        for path in sorted(p for p in old_secrets.rglob("*") if p.is_file()):
            rel = path.relative_to(old_secrets)
            if source != target:
                plan.add_file("secret", path, target / "secrets" / rel, secret=True)
            if rel.parts[0] == LEGACY_DEVICE and len(rel.parts) > 1:
                twin = Path(DEVICE, *rel.parts[1:])
                if not (old_secrets / twin).exists():
                    plan.add_file("secret", path, target / "secrets" / twin, secret=True,
                                  note="workspace/* is also device/*")

    # .mcp-manager/state*.json -> .anx/ (run/ pid records are not carried over)
    old_state = source / paths.LEGACY_STATE_DIR_NAME
    if old_state.is_dir():
        for path in sorted(old_state.glob("state*.json")):
            if path.is_file():
                plan.add_file("state", path, target / paths.STATE_DIR_NAME / path.name)

    # device watchers database -> plugin-data/device/ (audit.jsonl stays behind)
    device_dirs = [source / rel / LEGACY_DEVICE_DIR for rel in BUILTIN_DIRS]
    if DEVICE in manifests:
        device_dirs.insert(0, manifests[DEVICE][0])
    for directory in device_dirs:
        database = directory / "logs" / "watchers.sqlite3"
        if database.is_file():
            # The plugin may be running: sqlite's online backup gives a consistent
            # copy that includes what is still in -wal (copying the files would not).
            plan.add_file("device data", database, target / "plugin-data" / DEVICE / database.name,
                          copier=_sqlite_backup, same=_same_database, note="sqlite backup")
            break

    # link registrations in plugins/plugins.lock.json
    if linked:
        lock_path = target / sections.LOCK_PATH
        lock = sections.read_lock(target) if lock_path.is_file() else {}
        merged = dict(lock)
        for name, directory in sorted(linked.items()):
            entry = {"mode": "link", "path": str(directory)}
            version = manifests[name][1].get("version")
            if isinstance(version, str):
                entry["version"] = version
            if name in lock and lock[name] != entry:
                plan.warnings.append(f"plugins.lock.json already has {name}; kept the existing entry")
                continue
            merged[name] = entry
        if merged != lock or not lock_path.is_file():
            content = _config_bytes(merged)
            if lock_path.is_file():
                # an update that only adds entries; existing ones are untouched
                plan.items.append(Item("write", "plugin lock", lock_path, None, "adds link entries",
                                       sha256_bytes(content), content=content))
            else:
                plan.add_content("plugin lock", lock_path, content)
        else:
            plan.items.append(Item("same", "plugin lock", lock_path, None, "", sha256_bytes(_config_bytes(lock))))

    if (source / ".private" / "enrollment.json").is_file():
        plan.items.append(Item("skip", "enrollment profile", None, source / ".private" / "enrollment.json",
                               "not copied: an enrolled device keeps its cloud section and token secret"))
    return plan


# -- applying --------------------------------------------------------------------------------

def _protect(path: Path) -> None:
    from mcpkit.config import protect

    protect(path)


def _write_bytes(dst: Path, content: bytes, *, secret: bool) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    temporary = dst.with_name(f".{dst.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(content)
    if secret:
        _protect(temporary)
    os.replace(temporary, dst)


def _copy(src: Path, dst: Path, *, secret: bool) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    temporary = dst.with_name(f".{dst.name}.{uuid.uuid4().hex}.tmp")
    shutil.copyfile(src, temporary)  # reading only: the source's mtime and bytes stay as they are
    shutil.copystat(src, temporary)
    if secret:
        _protect(temporary)
    os.replace(temporary, dst)


def _secured_dirs(items: list[Item]) -> set[Path]:
    dirs = set()
    for item in items:
        if item.secret and item.dst is not None:
            for parent in item.dst.parents:
                if parent.name == "secrets":
                    dirs.add(parent)
                    break
    return dirs


def apply_items(items: list[Item]) -> None:
    pending = [item for item in items if item.op in ("copy", "write")]
    for directory in sorted(_secured_dirs(pending)):
        if not directory.exists():
            directory.mkdir(parents=True)
            _protect(directory)
    for item in pending:
        assert item.dst is not None
        if item.dst.exists() and item.kind not in ("plugin lock",):
            # Appeared since the plan was made: never overwrite.
            item.op, item.note = "conflict", "appeared after planning; left alone"
            continue
        if item.op == "write":
            content = item.content() if callable(item.content) else item.content
            _write_bytes(item.dst, content or b"", secret=item.secret)
        elif item.copier is not None:
            item.dst.parent.mkdir(parents=True, exist_ok=True)
            item.copier(item.src, item.dst)
            if item.secret:
                _protect(item.dst)
        else:
            _copy(item.src, item.dst, secret=item.secret)
        item.op = "done-" + item.op


def apply_migration(plan: Plan) -> Path:
    apply_items(plan.items)
    report = {"from": str(plan.source), "to": str(plan.target), "at": _now(),
              "items": [item.public(plan.target, plan.source) for item in plan.items], "warnings": plan.warnings}
    path = plan.target / REPORT
    _write_bytes(path, _config_bytes(report), secret=False)
    return path


def print_plan(plan: Plan, *, applied: bool, stream=None) -> None:
    stream = stream or sys.stdout
    print(f"{'migrated' if applied else 'migration plan (dry run; add --apply to write)'}: "
          f"{plan.source} -> {plan.target}", file=stream)
    for item in plan.items:
        entry = item.public(plan.target, plan.source)
        src = f"{entry['src']} -> " if entry["src"] else ""
        note = f"  ({entry['note']})" if entry.get("note") else ""
        print(f"  {entry['op']:<12} {item.kind:<16} {src}{entry['dst']}{note}", file=stream)
    for warning in plan.warnings:
        print(f"  warning: {warning}", file=stream)
    counts = {op: sum(1 for item in plan.items if item.op == op) for op in sorted({i.op for i in plan.items})}
    print("  summary: " + ", ".join(f"{n} {op}" for op, n in counts.items()), file=stream)


# -- export back to the legacy shape (section 4.4) ------------------------------------------

def export_legacy(root: Path, destination: Path) -> list[str]:
    """Write the current configuration as a *new* ``mcp-manager.json``-shaped file.

    For merging by hand after a rollback; ``destination`` must not exist.
    Returns what could not be expressed in the old format.
    """
    from mcpkit.config import ConfigError, ServiceConfig, read_secret, write_json

    root, destination = Path(root).resolve(), Path(destination).resolve()
    if destination.exists():
        raise MigrationError(f"{destination} exists; choose a new file (nothing is ever overwritten)")
    data = paths.read_config(root)
    reader = ServiceConfig(name="manager", root=root, dir=root)
    skipped: list[str] = []

    def read(name: str) -> Any:
        try:
            return read_secret(reader, name, min_length=1)
        except ConfigError:
            skipped.append(f"secret {name} could not be read; left as a reference")
            return {sections.SECRET_REF: name}

    legacy: dict[str, Any] = {k: v for k, v in data.items()
                              if k not in ("plugins", "services", "plugin_paths", "scan", "connectors", "migrated_from")}
    services = {}
    for name, entry in sections.plugin_entries(data).items():
        services[LEGACY_DEVICE if name == DEVICE else name] = sections.resolve_secret_refs(entry, read)
    if services:
        legacy["services"] = services
    scan = sections.plugin_paths(data)
    if scan:
        legacy["scan"] = scan
    cloud = dict(data.get("cloud") or {})
    if cloud or data.get("connectors"):
        workspaces = {}
        for key, value in (cloud.get("workspaces") or {}).items():
            value = {"path": value} if isinstance(value, str) else dict(value)
            if value.get("mcp") == DEVICE:
                value["mcp"] = LEGACY_DEVICE
            workspaces[key] = value
        if workspaces:
            cloud["workspaces"] = workspaces
        mcps = dict(cloud.get("mcps") or {})
        for name, connector in (data.get("connectors") or {}).items():
            entry = sections.mcp_from_connector(connector) if isinstance(connector, dict) else None
            if entry is None:
                skipped.append(f"connector {name} uses an auth type the old format cannot express")
            else:
                mcps[name] = entry
        if mcps:
            cloud["mcps"] = mcps
        legacy["cloud"] = cloud
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    write_json(temporary, legacy)
    _protect(temporary)
    if destination.exists():
        temporary.unlink()
        raise MigrationError(f"{destination} appeared meanwhile; nothing written")
    os.replace(temporary, destination)
    return skipped


# -- first start of an installed root (section 4.1 step 4) ----------------------------------

def migrate_legacy_root(environ: Mapping[str, str] | None = None, *, stream=None) -> Path | None:
    """Copy the legacy installed root into the new one when the new one has no configuration."""
    new, old = paths.installed_root(environ), paths.legacy_installed_root(environ)
    if new is None or old is None or paths.has_config(new) or not (old / paths.LEGACY_CONFIG_NAME).is_file():
        return None
    plan = plan_migration(old, new)
    apply_migration(plan)
    print(f"agent-nexus: copied the configuration from {old} (left unchanged) to {new}", file=stream or sys.stderr)
    return new


# -- gateway (section 4.3) -------------------------------------------------------------------

GATEWAY_FILES = {"cloud.json": "gateway.json", "server.env": "gateway.env"}
OLD_STATE, NEW_STATE = "/var/lib/mcpmanager", "/var/lib/agent-nexus"


def _replace_prefix(value: Any, old: str, new: str) -> Any:
    if isinstance(value, str):
        if value == old or value.startswith(old.rstrip("/") + "/"):
            return new.rstrip("/") + value[len(old.rstrip("/")):]
        return value
    if isinstance(value, dict):
        return {k: _replace_prefix(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_prefix(v, old, new) for v in value]
    return value


def _sqlite_read_only(path: Path) -> sqlite3.Connection:
    """Open a database read-only without leaving anything next to it.

    A plain ``mode=ro`` open of a WAL database whose ``-wal``/``-shm`` are absent
    (closed cleanly) creates both files beside it, which would change the old
    root. With no ``-wal`` everything is in the main file, so ``immutable=1``
    reads it without locks or side files. A live database (``-wal`` present)
    is read through the normal shared-memory protocol so its WAL is included.
    """
    path = path.resolve()
    live = path.with_name(path.name + "-wal").exists()
    query = "mode=ro" if live else "mode=ro&immutable=1"
    return sqlite3.connect(f"{path.as_uri()}?{query}", uri=True)


def _sqlite_backup(src: Path, dst: Path) -> None:
    """A consistent copy of a live database that leaves the source untouched."""
    temporary = dst.with_name(f".{dst.name}.{uuid.uuid4().hex}.tmp")
    source = _sqlite_read_only(src)
    try:
        target = sqlite3.connect(temporary)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    shutil.copymode(src, temporary)
    os.replace(temporary, dst)


def _sqlite_dump(path: Path) -> list[str]:
    connection = _sqlite_read_only(path)
    try:
        return list(connection.iterdump())
    finally:
        connection.close()


def _same_database(src: Path, dst: Path) -> bool:
    """A backup is not byte-identical to its source; compare the content instead."""
    try:
        return _sqlite_dump(src) == _sqlite_dump(dst)
    except sqlite3.Error:
        return False


def plan_gateway_migration(config_dir: Path, state_dir: Path | None, target_dir: Path,
                           target_state: Path | None = None, *, old_state_path: str | None = None,
                           new_state_path: str | None = None) -> Plan:
    config_dir, target_dir = Path(config_dir).resolve(), Path(target_dir).resolve()
    old_cloud = config_dir / "cloud.json"
    if not old_cloud.is_file():
        raise MigrationError(f"no cloud.json in {config_dir}")
    raw = old_cloud.read_bytes()
    data = _read_json_bytes(raw, old_cloud)
    old_prefix = old_state_path or (str(state_dir) if state_dir else OLD_STATE)
    new_prefix = new_state_path or (str(target_state) if target_state else NEW_STATE)
    gateway = _replace_prefix(data, old_prefix, new_prefix)
    if old_prefix != OLD_STATE:  # absolute paths written for the standard layout, too
        gateway = _replace_prefix(gateway, OLD_STATE, new_prefix)
    gateway.setdefault("mailbox", {"enabled": True, "db": new_prefix.rstrip("/") + "/gateway.sqlite3"})
    # Installers the console offers on its public download page (docs/ui-spec.md §3.7).
    gateway.setdefault("downloads", {"dir": new_prefix.rstrip("/") + "/downloads"})
    # The plugin repository (design §6.12): private packages, never under downloads/.
    gateway.setdefault("plugin_repo", {"dir": new_prefix.rstrip("/") + "/plugin-repo", "max_package_mb": 300})
    gateway["migrated_from"] = {"path": str(old_cloud), "sha256": sha256_bytes(raw), "at": _now()}
    plan = Plan(config_dir, target_dir, config=gateway)
    plan.add_content("gateway config", target_dir / "gateway.json", _config_bytes(gateway), same=_same_config(gateway))
    env_file = config_dir / "server.env"
    if env_file.is_file():
        # Variable names are data (token_env, agents): the file is copied as it is.
        plan.add_file("gateway env", env_file, target_dir / "gateway.env", secret=True)
    secrets = config_dir / "secrets"
    if secrets.is_dir():
        # auth.password_secret / auth.bearer_secrets resolve against <config dir>/secrets:
        # without them the migrated gateway.json fails --check-config.
        for path in sorted(p for p in secrets.rglob("*") if p.is_file()):
            plan.add_file("gateway secret", path, target_dir / "secrets" / path.relative_to(secrets), secret=True)
    if state_dir is not None and Path(state_dir).is_dir():
        state_dir = Path(state_dir).resolve()
        target_state = Path(target_state or NEW_STATE).resolve()
        for path in sorted(p for p in state_dir.rglob("*") if p.is_file()):
            rel = path.relative_to(state_dir)
            if path.name.endswith(("-wal", "-shm", "-journal")):
                continue  # folded into the backup of the database itself
            if path.suffix == ".sqlite3":
                plan.add_file("gateway state", path, target_state / rel, secret=True, copier=_sqlite_backup,
                              same=_same_database, note="sqlite backup")
            else:
                plan.add_file("gateway state", path, target_state / rel)
    return plan


def chown_tree(paths_: list[Path], owner: str, roots: tuple[Path, ...] = ()) -> None:
    """Hand the copied files to ``owner``, with the directories created for them.

    Directories between a file and the first of ``roots`` that contains it are
    included (for example ``secrets/workspace``: created owner-only, the gateway
    user must be able to enter them); a root itself is included only when it is
    a state directory, i.e. listed in ``roots`` after the config directory.
    """
    if os.name == "nt":
        return
    resolved = [Path(root).resolve() for root in roots]
    done: set[Path] = set()
    for path in paths_:
        shutil.chown(path, user=owner, group=owner)
        for parent in Path(path).resolve().parents:
            inside = [index for index, root in enumerate(resolved) if parent == root or root in parent.parents]
            if not inside or parent in done:
                break
            if parent == resolved[inside[0]] and inside[0] == 0:
                break  # the config directory itself stays root's
            shutil.chown(parent, user=owner, group=owner)
            done.add(parent)


def gateway_main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="anx-gateway migrate",
                                     description="copy a legacy gateway (cloud.json, server.env, state) to the new layout")
    parser.add_argument("--from", dest="source", type=Path, default=Path("/etc/mcpmanager"))
    parser.add_argument("--state", type=Path, default=Path(OLD_STATE))
    parser.add_argument("--to", type=Path, default=Path("/etc/agent-nexus"))
    parser.add_argument("--state-to", type=Path, default=Path(NEW_STATE))
    parser.add_argument("--owner", help="chown copied files to this user and group (POSIX only)")
    parser.add_argument("--apply", action="store_true", help="write; without it only the plan is printed")
    args = parser.parse_args(argv)
    try:
        plan = plan_gateway_migration(args.source, args.state, args.to, args.state_to)
        if args.apply:
            pending = list(plan.pending)
            apply_items(plan.items)
            # The plugin repository (design §6.12): created owner-only, before chown hands it over.
            from .plugin_repo import owner_only_dir
            repo_dir = Path(args.state_to) / "plugin-repo"
            owner_only_dir(repo_dir)
            if args.owner:
                chown_tree([item.dst for item in pending if item.dst is not None and item.dst.exists()] + [repo_dir],
                           args.owner, (args.to, args.state_to))
        print_plan(plan, applied=args.apply)
        return 3 if plan.conflicts else 0
    except (MigrationError, OSError, sqlite3.Error) as exc:
        print(f"anx-gateway migrate: {exc}", file=sys.stderr)
        return 2
