"""Single version source for agent-nexus (design §10).

The canonical version is ``__version__`` in ``agent_nexus/__init__.py``.  Everything
else either derives from it at build time or is rewritten by ``set``:

* ``pyproject.toml``            -- ``dynamic = ["version"]`` read by hatch from ``__init__.py``
* ``scripts/build_ubuntu.py``   -- reads ``__init__.py`` at import time (no literal)
* ``desktop/src-tauri/tauri.conf.json`` -- has *no* ``version``; Tauri v2 falls back to Cargo.toml
* ``desktop/src-tauri/Cargo.toml`` ``[package].version`` and its ``Cargo.lock`` entry -- rewritten
* ``plugins/device/mcp-service.json`` ``version`` -- rewritten when that file exists (M9 layout)

Usage::

    python scripts/version.py show
    python scripts/version.py set 0.2.1
    python scripts/version.py check [--tag v0.2.1]

``check`` exits non-zero on any drift.  ``--tag`` (or, in GitHub Actions, a ``refs/tags/v*``
``GITHUB_REF``) additionally requires the tag to equal ``v<version>``; release workflows use it.
Standard library only, so CI can run it with ``uv run --no-project``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

INIT = "agent_nexus/__init__.py"
PYPROJECT = "pyproject.toml"
UV_LOCK = "uv.lock"
CARGO_TOML = "desktop/src-tauri/Cargo.toml"
CARGO_LOCK = "desktop/src-tauri/Cargo.lock"
TAURI_CONF = "desktop/src-tauri/tauri.conf.json"
UBUNTU = "scripts/build_ubuntu.py"
DEVICE_MANIFEST = "plugins/device/mcp-service.json"

SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")
INIT_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)
# [package] table up to the next table header; version line inside it.
CARGO_PACKAGE_RE = re.compile(r"(^\[package\][^\[]*?^version\s*=\s*\")([^\"]+)(\")", re.M | re.S)
CARGO_NAME_RE = re.compile(r"^\[package\][^\[]*?^name\s*=\s*\"([^\"]+)\"", re.M | re.S)


class VersionError(Exception):
    pass


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def _write(root: Path, rel: str, text: str) -> None:
    # newline="" keeps the file's existing line endings untouched.
    with open(root / rel, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def read_version(root: Path = ROOT) -> str:
    """The canonical version from ``agent_nexus/__init__.py``."""
    match = INIT_RE.search(_read(root, INIT))
    if not match:
        raise VersionError(f"{INIT}: no __version__ assignment")
    return match.group(1)


def _cargo_lock_re(name: str) -> re.Pattern:
    return re.compile(r'(\[\[package\]\]\r?\nname = "' + re.escape(name) + r'"\r?\nversion = ")([^"]+)(")')


def _cargo_name(root: Path) -> str:
    match = CARGO_NAME_RE.search(_read(root, CARGO_TOML))
    if not match:
        raise VersionError(f"{CARGO_TOML}: no [package].name")
    return match.group(1)


def problems(root: Path = ROOT, tag: str | None = None) -> list[str]:
    """Every place that disagrees with the canonical version (empty list means consistent)."""
    issues: list[str] = []
    try:
        version = read_version(root)
    except (OSError, VersionError) as exc:
        return [str(exc)]
    if not SEMVER.match(version):
        issues.append(f"{INIT}: __version__ {version!r} is not X.Y.Z")

    pyproject = _read(root, PYPROJECT)
    project = re.search(r"^\[project\](.*?)(?=^\[)", pyproject, re.M | re.S)
    body = project.group(1) if project else ""
    if re.search(r"^version\s*=", body, re.M):
        issues.append(f"{PYPROJECT}: [project] must not hard-code version (use dynamic)")
    if not re.search(r'^dynamic\s*=\s*\[[^\]]*"version"', body, re.M):
        issues.append(f'{PYPROJECT}: [project] needs dynamic = ["version"]')
    hatch = re.search(r"^\[tool\.hatch\.version\](.*?)(?=^\[|\Z)", pyproject, re.M | re.S)
    if not hatch or not re.search(r'^path\s*=\s*"' + re.escape(INIT) + '"', hatch.group(1), re.M):
        issues.append(f'{PYPROJECT}: [tool.hatch.version] path must be "{INIT}"')

    if (root / UV_LOCK).exists():
        pinned = re.search(r'\[\[package\]\]\r?\nname = "agent-nexus"\r?\nversion = "([^"]+)"', _read(root, UV_LOCK))
        if pinned:
            issues.append(f"{UV_LOCK}: agent-nexus pinned to {pinned.group(1)}; run 'uv lock' (dynamic version)")

    cargo = CARGO_PACKAGE_RE.search(_read(root, CARGO_TOML))
    if not cargo:
        issues.append(f"{CARGO_TOML}: no [package].version")
    elif cargo.group(2) != version:
        issues.append(f"{CARGO_TOML}: [package].version {cargo.group(2)} != {version}")

    if (root / CARGO_LOCK).exists():
        name = _cargo_name(root)
        lock = _cargo_lock_re(name).search(_read(root, CARGO_LOCK))
        if not lock:
            issues.append(f"{CARGO_LOCK}: no entry for {name}")
        elif lock.group(2) != version:
            issues.append(f"{CARGO_LOCK}: {name} {lock.group(2)} != {version}")

    if "version" in json.loads(_read(root, TAURI_CONF)):
        issues.append(f"{TAURI_CONF}: must not set version (Tauri falls back to Cargo.toml)")

    ubuntu = _read(root, UBUNTU)
    if re.search(r'^VERSION\s*=\s*["\']', ubuntu, re.M):
        issues.append(f"{UBUNTU}: VERSION must be read from {INIT}, not a literal")

    if (root / DEVICE_MANIFEST).exists():
        manifest = json.loads(_read(root, DEVICE_MANIFEST))
        if manifest.get("version") != version:
            issues.append(f"{DEVICE_MANIFEST}: version {manifest.get('version')} != {version}")

    if tag is not None and tag != f"v{version}":
        issues.append(f"tag {tag} does not match source version v{version}")
    return issues


def set_version(new: str, root: Path = ROOT) -> list[str]:
    """Rewrite every managed location to ``new``; returns the files changed."""
    if not SEMVER.match(new):
        raise VersionError(f"{new!r} is not X.Y.Z")
    changed: list[str] = []

    def rewrite(rel: str, pattern: re.Pattern, replacement) -> None:
        text = _read(root, rel)
        updated, count = pattern.subn(replacement, text, count=1)
        if count == 0:
            raise VersionError(f"{rel}: version field not found")
        if updated != text:
            _write(root, rel, updated)
            changed.append(rel)

    rewrite(INIT, INIT_RE, f'__version__ = "{new}"')
    rewrite(CARGO_TOML, CARGO_PACKAGE_RE, lambda m: m.group(1) + new + m.group(3))
    if (root / CARGO_LOCK).exists():
        rewrite(CARGO_LOCK, _cargo_lock_re(_cargo_name(root)), lambda m: m.group(1) + new + m.group(3))
    if (root / DEVICE_MANIFEST).exists():
        rewrite(DEVICE_MANIFEST, re.compile(r'("version"\s*:\s*")([^"]*)(")'),
                lambda m: m.group(1) + new + m.group(3))
    return changed


def _ci_tag() -> str | None:
    ref = os.environ.get("GITHUB_REF", "")
    return ref[len("refs/tags/"):] if ref.startswith("refs/tags/v") else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show", help="print the canonical version")
    setter = sub.add_parser("set", help="bump every managed location to X.Y.Z")
    setter.add_argument("version")
    checker = sub.add_parser("check", help="fail on version drift")
    checker.add_argument("--tag", help="also require this git tag to equal v<version>")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "show":
            print(read_version(root))
            return 0
        if args.command == "set":
            for rel in set_version(args.version, root):
                print(f"updated {rel}")
            issues = problems(root)
        else:
            issues = problems(root, args.tag if args.tag is not None else _ci_tag())
    except VersionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if issues:
        for issue in issues:
            print(f"version drift: {issue}", file=sys.stderr)
        return 1
    if args.command == "check":
        print(f"version {read_version(root)} consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
