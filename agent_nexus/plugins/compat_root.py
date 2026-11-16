"""The shadow root for plugins built on mcpkit < 0.2 (docs/design.md section 4.5).

An old mcpkit reads ``$MCPM_ROOT/mcp-manager.json`` and nothing else: it knows
neither ``agent-nexus.json`` nor the ``plugins`` section nor ``ANX_ROOT``.
Rather than writing a mirror into the real ``mcp-manager.json`` (which must
stay byte-for-byte the rollback target), such a plugin is started with

* ``MCPM_ROOT=<root>/.anx/compat-root``, holding a generated
  ``mcp-manager.json`` with only a ``services`` section projected from the
  current configuration, and a ``secrets`` junction (a symlink on POSIX) to
  ``<root>/secrets``;
* ``ANX_ROOT=<root>``, which a current mcpkit prefers, so a plugin that has
  already moved on is not affected.

Only plugins that really load an old mcpkit get this treatment. A manifest
without ``requires.mcpkit`` (every v1 manifest) is looked at more closely: the
mcpkit its project resolves to (a path/editable source, ``uv.lock``, or the
plugin's own ``.venv``) decides, and a plugin that does not use mcpkit at all
is started with the real root. The projection likewise carries only the
entries of those plugins, so no other plugin's secrets are resolved into it.

``{"$secret": ...}`` references are resolved in the projection, because an old
mcpkit would hand the plugin the reference object instead of the password the
legacy file used to hold. The generated file is therefore restricted with
``protect()`` like any secret. Settings an old plugin writes back land in the
shadow file and are regenerated away; the console is where settings change.
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .. import paths, sections

DIR = Path(paths.STATE_DIR_NAME) / "compat-root"
GENERATED_BY = "agent-nexus"
MIN_MODERN_MCPKIT = (0, 2)


def compat_dir(root: Path) -> Path:
    return Path(root) / DIR


def _version(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", text or "")
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


#: ``mcpkit_version`` result for a project that does not use mcpkit at all
NOT_USED = "not-used"


def _toml(path: Path) -> dict[str, Any]:
    import tomllib

    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except (OSError, ValueError):
        return {}


def _project_version(directory: Path) -> tuple[int, ...] | None:
    project = _toml(directory / "pyproject.toml").get("project")
    if isinstance(project, dict) and isinstance(project.get("version"), str):
        return _version(project["version"])
    return None


def _source_path(source: Any, base: Path) -> Path | None:
    """The directory of a path/editable source (``tool.uv.sources`` or a ``uv.lock`` entry)."""
    if not isinstance(source, dict):
        return None
    for key in ("editable", "path", "directory"):
        value = source.get(key)
        if isinstance(value, str) and value:
            return (base / value).resolve()
    return None


def _requirement_names(lines: Iterable[Any]) -> set[str]:
    names = set()
    for line in lines:
        if isinstance(line, str):
            match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line)
            if match:
                names.add(re.sub(r"[-_.]+", "-", match.group(1)).lower())
    return names


def mcpkit_version(directory: Path) -> tuple[int, ...] | str | None:
    """The mcpkit version a plugin project actually loads.

    Returns a version tuple; ``None`` when the project uses mcpkit but the
    version cannot be told (treated as old); or ``NOT_USED``.
    """
    directory = Path(directory)
    pyproject = _toml(directory / "pyproject.toml")
    lock = _toml(directory / "uv.lock")
    used = False

    tool = pyproject.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    sources = uv.get("sources") if isinstance(uv, dict) else None
    if isinstance(sources, dict) and "mcpkit" in sources:
        used = True
        source = _source_path(sources["mcpkit"], directory)
        if source is not None and (found := _project_version(source)) is not None:
            return found  # a path source runs the code on disk, whatever the lock says

    for package in lock.get("package") or []:
        if isinstance(package, dict) and package.get("name") == "mcpkit":
            used = True
            source = _source_path(package.get("source"), directory)
            if source is not None and (found := _project_version(source)) is not None:
                return found
            if isinstance(package.get("version"), str) and (found := _version(package["version"])):
                return found

    venv = directory / ".venv"
    if venv.is_dir():
        for info in sorted([*venv.glob("Lib/site-packages/mcpkit-*.dist-info"),
                            *venv.glob("lib/python*/site-packages/mcpkit-*.dist-info")]):
            used = True
            if (found := _version(info.name[len("mcpkit-"):])) is not None:
                return found

    project = pyproject.get("project")
    if isinstance(project, dict):
        lines = list(project.get("dependencies") or [])
        for extra in (project.get("optional-dependencies") or {}).values():
            lines += list(extra or [])
        used = used or "mcpkit" in _requirement_names(lines)
    for requirements in (directory / "requirements.txt", directory / "requirements.in"):
        try:
            used = used or "mcpkit" in _requirement_names(requirements.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeError):
            pass
    for vendored in (directory / "mcpkit", directory / "src" / "mcpkit"):
        used = used or (vendored / "__init__.py").is_file()
    return None if used else NOT_USED


def is_legacy_plugin(spec: Any) -> bool:
    """True when the plugin runs an mcpkit older than 0.2.

    A declared ``requires.mcpkit`` decides. Otherwise a v2 manifest is current,
    and a v1 manifest is legacy only when its project really loads an old (or an
    unidentifiable) mcpkit; one that does not use mcpkit at all is not.
    """
    declared = _version(getattr(spec, "requires_mcpkit", "") or "")
    if declared is not None:
        return declared[:2] < MIN_MODERN_MCPKIT
    if int(getattr(spec, "schema_version", 1) or 1) >= 2:
        return False
    directory = getattr(spec, "dir", None)
    if directory is None:
        return True  # nothing to look at: keep the conservative answer
    found = mcpkit_version(Path(directory))
    if found == NOT_USED:
        return False
    return found is None or found[:2] < MIN_MODERN_MCPKIT


def needed(root: Path, spec: Any) -> bool:
    """Only when there is an ``agent-nexus.json``: with the legacy file alone an
    old mcpkit already reads the real thing."""
    return (Path(root) / paths.CONFIG_NAME).is_file() and is_legacy_plugin(spec)


def projection(root: Path, specs: Iterable[Any] = ()) -> dict[str, Any]:
    """The generated file's content: ``services`` only, and only for the legacy
    plugins among ``specs`` (under their former names too)."""
    from mcpkit.config import ConfigError, ServiceConfig, read_secret

    root = Path(root)
    data = paths.read_config(root)
    entries = sections.plugin_entries(data)
    services: dict[str, Any] = {}
    reader = ServiceConfig(name="manager", root=root, dir=root)

    def read(name: str) -> Any:
        try:
            return read_secret(reader, name, min_length=1)
        except ConfigError:
            return {sections.SECRET_REF: name}  # left as it was; the plugin reports it

    legacy = [spec for spec in specs if is_legacy_plugin(spec)]
    for spec in legacy:
        for name in (spec.name, *(getattr(spec, "aliases", []) or [])):
            if name in entries:
                services[spec.name] = sections.resolve_secret_refs(entries[name], read)
                break
    # A renamed plugin running old code reads its former name (codex-mcp: workspace).
    for spec in legacy:
        entry = services.get(spec.name)
        if entry is None:
            continue
        for alias in getattr(spec, "aliases", []) or []:
            services.setdefault(alias, entry)
    return {"_generated_by": GENERATED_BY, "services": services}


def _link_secrets(directory: Path, target: Path) -> None:
    link = directory / "secrets"
    if os.path.lexists(link) or not target.is_dir():
        return
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))  # no administrator rights needed
    else:
        os.symlink(target, link, target_is_directory=True)


def ensure(root: Path, specs: Iterable[Any] = ()) -> Path:
    """Write (or refresh) the shadow root and return it."""
    from mcpkit.config import protect

    root = Path(root)
    directory = compat_dir(root)
    created = not directory.is_dir()
    directory.mkdir(parents=True, exist_ok=True)
    if created:
        protect(directory)
    _link_secrets(directory, root / "secrets")
    target = directory / paths.LEGACY_CONFIG_NAME
    text = json.dumps(projection(root, specs), ensure_ascii=False, indent=2) + "\n"
    try:
        if target.read_text(encoding="utf-8") == text:
            return directory
    except OSError:
        pass
    temporary = directory / f".{paths.LEGACY_CONFIG_NAME}.{uuid.uuid4().hex}.tmp"
    temporary.write_text(text, encoding="utf-8")
    protect(temporary)
    os.replace(temporary, target)
    return directory
