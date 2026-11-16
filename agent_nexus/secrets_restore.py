"""``anx secrets restore``: put the credentials vault back (docs/design.md section 6.9).

The vault (``WorkSpace/mcp/credentials-vault``) is a directory of copies with a
``README.md`` whose file list (``## 文件清单（N 个）`` followed by one
``- `path``` bullet per file) is the manifest. The plan is driven by that list
and by :data:`VAULT_MAP`; nothing in the vault is hard-coded beyond the rules.

* Default is a dry run: the plan is printed, nothing is written.
* Per file: the target missing -> written; identical (sha256) -> skipped;
  different -> listed as a **conflict and never overwritten**.
* Written files are restricted with mcpkit ``protect()``.
* No secret value, and no digest of one, is ever printed or logged.
* Every manifest entry appears in the plan; one no rule covers is listed as
  ``unmapped``, a listed file missing from the vault as ``missing`` and a vault
  file the list does not mention as ``unlisted`` - none is silently ignored.
"""
from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .migrate import DEVICE, LEGACY_DEVICE, Item, MigrationError, Plan, apply_items

MANIFEST_FILE = "README.md"
#: ``## 文件清单（42 个）`` / ``## 文件清单 (42)`` / ``## Files (42)``
HEADING = re.compile(r"^##\s*(?:文件清单|files|file list|manifest)\s*(?:[（(]\s*(\d+)\s*(?:个)?\s*[)）])?\s*$",
                     re.IGNORECASE)
ENTRY = re.compile(r"^\s*[-*]\s+`([^`]+)`")


class VaultError(MigrationError):
    pass


@dataclass
class Manifest:
    entries: list[str]
    declared: int | None = None
    warnings: list[str] = field(default_factory=list)


def _check_entry(path: str) -> str:
    pure = PurePosixPath(path)
    if (not path or "\\" in path or pure.is_absolute() or re.match(r"^[A-Za-z]:", path)
            or any(part in ("", ".", "..") for part in path.split("/"))):
        raise VaultError(f"vault manifest entry is not a safe relative path: {path!r}")
    return path


def parse_manifest(text: str) -> Manifest:
    entries: list[str] = []
    declared: int | None = None
    inside = False
    found = False
    for line in text.splitlines():
        heading = HEADING.match(line.strip())
        if heading:
            inside, found = True, True
            declared = int(heading.group(1)) if heading.group(1) else None
            continue
        if inside and line.startswith("#"):
            inside = False
            continue
        if inside:
            match = ENTRY.match(line)
            if match:
                entries.append(_check_entry(match.group(1).strip()))
    if not found:
        raise VaultError("vault README.md has no file list (a '## 文件清单' section of '- `path`' lines)")
    manifest = Manifest(entries, declared)
    duplicates = sorted({e for e in entries if entries.count(e) > 1})
    if duplicates:
        manifest.warnings.append(f"manifest lists {len(duplicates)} path(s) twice")
        manifest.entries = list(dict.fromkeys(entries))
    if declared is not None and declared != len(manifest.entries):
        manifest.warnings.append(f"manifest heading says {declared} files but lists {len(manifest.entries)}; "
                                 "the list is used")
    return manifest


def read_manifest(vault: Path) -> Manifest:
    path = Path(vault) / MANIFEST_FILE
    if not path.is_file():
        raise VaultError(f"no {MANIFEST_FILE} in {vault}")
    return parse_manifest(path.read_text(encoding="utf-8-sig"))


# -- rules --------------------------------------------------------------------------------

@dataclass
class Target:
    """What a rule decided for one manifest entry."""

    action: str  # restore | never | migrate-input | manual | optional | plugin-missing
    paths: list[Path] = field(default_factory=list)
    note: str = ""


@dataclass
class Context:
    root: Path
    vault: Path
    include: set[str]
    plugin_dir: Callable[[str], Path | None]


Rule = tuple[re.Pattern, Callable[[re.Match, Context], Target]]


def _secrets_dir(match: re.Match, ctx: Context) -> Target:
    service, rest = match.group(1), match.group(2)
    targets = [ctx.root / "secrets" / service / Path(*rest.split("/"))]
    if service == LEGACY_DEVICE:
        targets.append(ctx.root / "secrets" / DEVICE / Path(*rest.split("/")))
    return Target("restore", targets)


def _secrets_top(match: re.Match, ctx: Context) -> Target:
    return Target("restore", [ctx.root / "secrets" / match.group(1)], "agent token env file")


def _migrate_input(match: re.Match, ctx: Context) -> Target:
    return Target("migrate-input", [], f"not restored; input for `anx migrate --from-file <vault>/{match.group(0)}`")


def _never(match: re.Match, ctx: Context) -> Target:
    return Target("never", [], "never restored; a local private build references it with -Enrollment <vault path>")


def _yunxiao(match: re.Match, ctx: Context) -> Target:
    return Target("manual", [], "import when adding the connector: "
                                "anx secrets set connectors/yunxiao/key --from-file <vault path>")


def _device_optional(match: re.Match, ctx: Context) -> Target:
    target = ctx.root / "plugin-data" / DEVICE / "logs" / match.group(1)
    if "optional" not in ctx.include:
        return Target("optional", [target], "skipped; add --include optional to restore")
    return Target("restore", [target])


def _plugin_data(match: re.Match, ctx: Context) -> Target:
    legacy_dir, rest = match.group(1), match.group(2)
    directory = ctx.plugin_dir(legacy_dir)
    if directory is None:
        return Target("plugin-missing", [], f"install the plugin for {legacy_dir} first, then restore again")
    return Target("restore", [directory / Path(*rest.split("/"))])


#: First match wins. Each pattern is matched against the whole manifest path.
VAULT_MAP: list[Rule] = [
    (re.compile(r"repo/secrets/build-enrollment\.json"), _never),
    (re.compile(r"root/secrets/(cloud-agent-[^/]+\.env)"), _secrets_top),
    (re.compile(r"root/secrets/([^/]+)/(.+)"), _secrets_dir),
    (re.compile(r"root/mcp-manager\.json(?:\.bak-[^/]+)?"), _migrate_input),
    (re.compile(r"root/\.mcp-manager/[^/]+\.json"), _migrate_input),
    (re.compile(r"repo/builtins/codex-mcp/logs/(oauth-store\.json\.bak-domain)"), _device_optional),
    (re.compile(r"repo/builtins/((?!codex-mcp/)[^/]+)/(.+)"), _plugin_data),
    (re.compile(r"root/云效apikey"), _yunxiao),
]


def match_rule(entry: str, ctx: Context) -> Target | None:
    for pattern, decide in VAULT_MAP:
        match = pattern.fullmatch(entry)
        if match:
            return decide(match, ctx)
    return None


def installed_plugin_dirs(root: Path) -> Callable[[str], Path | None]:
    """Find the installed plugin that took over a legacy ``builtins/<dir>``.

    A manifest says so with ``"legacy": {"dirs": [...]}``; failing that the
    directory name itself, or the plugin id it was named after
    (``payment-mcp`` -> ``payment``), identifies it.
    """
    from .registry import discover, load_manager_config, read_json

    specs = discover(load_manager_config(root))

    def find(legacy_dir: str) -> Path | None:
        by_dir = None
        for spec in specs.values():
            if spec.observed:
                continue
            try:
                manifest = read_json(spec.dir / "mcp-service.json")
            except (OSError, ValueError):
                manifest = {}
            legacy = manifest.get("legacy") if isinstance(manifest.get("legacy"), dict) else {}
            if legacy_dir in (legacy.get("dirs") or []):
                return spec.dir
            if by_dir is None and (spec.dir.name == legacy_dir or spec.name == legacy_dir.removesuffix("-mcp")):
                by_dir = spec.dir
        return by_dir

    return find


# -- plan / apply -----------------------------------------------------------------------------

def plan_restore(vault: Path, root: Path, *, include: set[str] | None = None,
                 plugin_dir: Callable[[str], Path | None] | None = None) -> Plan:
    vault, root = Path(vault).resolve(), Path(root).resolve()
    if not vault.is_dir():
        raise VaultError(f"vault not found: {vault}")
    if root == vault or root.is_relative_to(vault):
        raise VaultError("the root must not be inside the vault")
    manifest = read_manifest(vault)
    ctx = Context(root, vault, set(include or ()), plugin_dir or installed_plugin_dirs(root))
    plan = Plan(vault, root)
    plan.warnings.extend(manifest.warnings)
    for entry in manifest.entries:
        source = vault.joinpath(*entry.split("/"))
        if not source.is_file():
            plan.items.append(Item("skip", "missing", None, source, "listed but not in the vault", ref=entry))
            continue
        target = match_rule(entry, ctx)
        if target is None:
            plan.items.append(Item("skip", "unmapped", None, source, "no rule in VAULT_MAP; not restored", ref=entry))
            continue
        if target.action != "restore":
            plan.items.append(Item("skip", target.action, target.paths[0] if target.paths else None, source,
                                   target.note, ref=entry))
            continue
        for dst in target.paths:
            item = plan.add_file("secret", source, dst, secret=True, note=target.note)
            item.ref = entry
    listed = set(manifest.entries)
    for path in sorted(p for p in vault.rglob("*") if p.is_file()):
        rel = path.relative_to(vault).as_posix()
        if rel != MANIFEST_FILE and rel not in listed:
            plan.items.append(Item("skip", "unlisted", None, path, "in the vault but not in the manifest; not restored",
                                   ref=rel))
    return plan


def print_restore(plan: Plan, *, applied: bool, stream=None) -> None:
    stream = stream or sys.stdout
    print(f"{'restored' if applied else 'restore plan (dry run; add --apply to write)'}: "
          f"{plan.source} -> {plan.target}", file=stream)
    for item in plan.items:
        dst = ""
        if item.dst is not None:
            try:
                dst = item.dst.relative_to(plan.target).as_posix()
            except ValueError:
                dst = str(item.dst)
        label = item.kind if item.op == "skip" else item.op
        arrow = f" -> {dst}" if dst else ""
        note = f"  ({item.note})" if item.note else ""
        print(f"  {label:<15} {item.ref}{arrow}{note}", file=stream)
    for warning in plan.warnings:
        print(f"  warning: {warning}", file=stream)
    counts: dict[str, int] = {}
    for item in plan.items:
        label = item.kind if item.op == "skip" else item.op
        counts[label] = counts.get(label, 0) + 1
    print("  summary: " + ", ".join(f"{n} {label}" for label, n in sorted(counts.items())), file=stream)


def restore(vault: Path, root: Path, *, apply: bool = False, include: set[str] | None = None,
            plugin_dir: Callable[[str], Path | None] | None = None, stream=None) -> Plan:
    plan = plan_restore(vault, root, include=include, plugin_dir=plugin_dir)
    if apply:
        apply_items(plan.items)
    print_restore(plan, applied=apply, stream=stream)
    return plan
