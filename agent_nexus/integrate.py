"""``anx integrate claude|codex [--dry-run] [--remove]``: install the session hooks (design §8.3).

* Claude Code: ``SessionStart`` and ``UserPromptSubmit`` command hooks in
  ``settings.json`` (``$CLAUDE_CONFIG_DIR`` or ``~/.claude``), each running
  ``anx hook claude --event <event>`` with a 5 s timeout, ``async`` (Claude
  does not wait for it; the hook prints nothing anyway).
* Codex: the top-level ``notify`` program in ``config.toml`` (``$CODEX_HOME``
  or ``~/.codex``), ``["<anx>", "hook", "codex"]``. Codex runs one notify
  program only, so an existing different one is never replaced: it is
  reported, and ``--hooks`` offers Codex's newer hooks file
  (``hooks.json``, Claude-compatible) instead, which Codex runs only once the
  owner trusts it in Codex.

Idempotent: a second run changes nothing. Every other key of the user's file
is kept; a timestamped copy (``<file>.anx-backup-<YYYYmmdd-HHMMSS>``) is
written before any change. ``--remove`` takes out exactly our entries.
``~`` follows ``USERPROFILE`` on Windows and ``HOME`` elsewhere, so tests
point every path at a temporary directory.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .hooks import claude_home, codex_home

CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit")
CODEX_HOOK_EVENTS = ("SessionStart", "UserPromptSubmit")
HOOK_TIMEOUT_S = 5
NOTIFY_COMMENT = "# agent-nexus: report Codex sessions to the local manager (anx integrate codex)"


class IntegrateError(Exception):
    pass


@dataclass
class Plan:
    path: Path
    changes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    text: str | None = None  # the new file content; None = nothing to write
    backup: Path | None = None
    refused: bool = False
    bom: bool = False  # the file starts with a UTF-8 BOM: it is written back with one
    delete: bool = False  # --remove left nothing but our own entries: the file goes (we created it)


def anx_argv(root: Path | None = None) -> list[str]:
    """How a hook runs this anx: the absolute executable (PATH inside a hook is unreliable on Windows)."""
    found = shutil.which("anx")
    if found:
        argv = [str(Path(found).resolve())]
    else:
        script = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
        if script is not None and script.stem.lower() == "anx" and script.exists():
            argv = [str(script.resolve())]
        else:
            argv = [sys.executable, "-m", "agent_nexus.cli"]
    if root is not None:
        argv += ["--root", str(Path(root).resolve())]
    return argv


def shell_command(argv: list[str]) -> str:
    """A command line for Claude Code's shell (Git Bash or PowerShell): forward slashes, quotes only if needed.

    An unquoted first word works in both shells; a quoted one would be a string, not a command, in PowerShell.
    """
    parts = []
    for item in argv:
        item = item.replace("\\", "/") if re.match(r"^[A-Za-z]:[\\/]", item) or "\\" in item else item
        parts.append(f'"{item}"' if re.search(r"[\s\"'&|;<>()$`]", item) else item)
    return " ".join(parts)


def _is_ours(command: Any, kind: str) -> bool:
    if isinstance(command, list):
        command = " ".join(str(item) for item in command)
    if not isinstance(command, str):
        return False
    lowered = command.replace("\\", "/").lower()
    return f"hook {kind}" in lowered and bool(re.search(r"(^|[/\s\"'])(anx|mcpm)(\.exe|\.cmd)?[\s\"']|agent_nexus",
                                                          lowered))


def _backup_path(path: Path, now: float) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    candidate = path.with_name(f"{path.name}.anx-backup-{stamp}")
    n = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.anx-backup-{stamp}-{n}")
        n += 1
    return candidate


def _has_bom(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(3) == b"\xef\xbb\xbf"
    except OSError:
        return False


def _read(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return handle.read()  # line endings as they are: config.toml keeps its CRLF
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise IntegrateError(f"cannot read {path}: {exc}") from None


# -- hooks JSON (Claude settings.json, Codex hooks.json) -------------------------------
def plan_hooks_json(path: Path, kind: str, base_argv: list[str], events: tuple[str, ...], *, remove: bool,
                    background: bool = False) -> Plan:
    """Our command handler under each event; ``background`` adds ``"async": true`` (the client runs it detached)."""
    plan = Plan(path, bom=_has_bom(path))
    original = _read(path)
    try:
        data = json.loads(original) if original and original.strip() else {}
    except ValueError as exc:
        raise IntegrateError(f"{path} is not valid JSON ({exc}); fix it first, nothing was changed") from None
    if not isinstance(data, dict):
        raise IntegrateError(f"{path} is not a JSON object; nothing was changed")
    hooks = data.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise IntegrateError(f"{path}: 'hooks' is not an object; nothing was changed")
    hooks = dict(hooks or {})
    for event in events:
        groups = hooks.get(event)
        if groups is not None and not isinstance(groups, list):
            raise IntegrateError(f"{path}: hooks.{event} is not a list; nothing was changed")
        groups = [dict(group) if isinstance(group, dict) else group for group in (groups or [])]
        command = shell_command([*base_argv, "hook", kind, "--event", event])
        wanted = {"type": "command", "command": command, "timeout": HOOK_TIMEOUT_S}
        if background:
            wanted["async"] = True
        found = False
        kept_groups = []
        for group in groups:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                kept_groups.append(group)
                continue
            kept = []
            for handler in handlers:
                if isinstance(handler, dict) and _is_ours(handler.get("command"), kind):
                    if remove:
                        plan.changes.append(f"remove hooks.{event}: {handler.get('command')}")
                        continue
                    if found:
                        plan.changes.append(f"remove duplicate hooks.{event}: {handler.get('command')}")
                        continue
                    found = True
                    if any(handler.get(key) != value for key, value in wanted.items()):
                        plan.changes.append(f"update hooks.{event}: {command}")
                        handler = {**handler, **wanted}
                kept.append(handler)
            if kept:
                kept_groups.append({**group, "hooks": kept})
            elif handlers:
                pass  # the group held only our entry: drop it
            else:
                kept_groups.append(group)
        if not remove and not found:
            kept_groups.append({"hooks": [dict(wanted)]})
            plan.changes.append(f"add hooks.{event}: {command}")
        if kept_groups:
            hooks[event] = kept_groups
        else:
            hooks.pop(event, None)
    if not plan.changes:
        return plan
    new = dict(data)
    if hooks:
        new["hooks"] = hooks
    else:
        new.pop("hooks", None)
    # A file left empty by --remove stays (as an empty object): integrate cannot tell a file it
    # created from one the owner already had, and deleting the owner's file is the worse mistake.
    plan.text = edit_json(original or "", data, new)
    return plan


# -- editing JSON in place ---------------------------------------------------------------------
# The user's settings.json is edited, not rewritten: only the spans that change are replaced,
# new entries follow the file's own indentation, separators and line endings, and everything
# else (compact lines, key order, CRLF) stays byte for byte. ``--remove`` after an add gives the
# original bytes back.

_JSON_WS = " \t\r\n"
_JSON_SCALAR = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null")


@dataclass
class _Span:
    start: int  # the value's first character
    end: int = 0  # one past its last
    kind: str = "scalar"  # "object" | "array" | "scalar"
    items: list = field(default_factory=list)  # children (_Span)
    keys: list = field(default_factory=list)  # objects: the member keys, in order
    lead: int = 0  # where the element starts in its container (a member: its key)


def _parse_spans(text: str) -> _Span:
    """The value tree of a valid JSON document, with the character span of every value."""

    def skip(i: int) -> int:
        while i < len(text) and text[i] in _JSON_WS:
            i += 1
        return i

    def string_end(i: int) -> int:
        i += 1
        while text[i] != '"':
            i += 2 if text[i] == "\\" else 1
        return i + 1

    def value(i: int) -> _Span:
        char = text[i]
        if char in "{[":
            node = _Span(i, kind="object" if char == "{" else "array")
            close = "}" if char == "{" else "]"
            i = skip(i + 1)
            if text[i] == close:
                node.end = i + 1
                return node
            while True:
                lead = i
                if node.kind == "object":
                    key_end = string_end(i)
                    node.keys.append(json.loads(text[i:key_end]))
                    i = skip(key_end)
                    assert text[i] == ":"
                    i = skip(i + 1)
                child = value(i)
                child.lead = lead
                node.items.append(child)
                i = skip(child.end)
                if text[i] == ",":
                    i = skip(i + 1)
                    continue
                assert text[i] == close
                node.end = i + 1
                return node
        if char == '"':
            return _Span(i, string_end(i))
        match = _JSON_SCALAR.match(text, i)
        assert match
        return _Span(i, match.end())

    start = skip(0)
    root = value(start)
    assert skip(root.end) == len(text)
    return root


def _same(a: Any, b: Any) -> bool:
    """JSON equality (``1 != true``; object key order matters, as it does in the written file)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return list(a) == list(b) and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def edit_json(text: str, old: Any, new: Any) -> str:
    """``text`` (which parses to ``old``) changed to parse to ``new``, touching only what differs."""
    newline = "\r\n" if "\r\n" in text else "\n"
    unit = "  "
    try:
        root = _parse_spans(text) if text.strip() else None
    except (AssertionError, IndexError, ValueError):
        root = None
    if root is not None and root.items:
        gap = text[root.start + 1:root.items[0].lead]
        if "\n" in gap and gap.rsplit("\n", 1)[1]:
            unit = gap.rsplit("\n", 1)[1]

    def dumps(value: Any, indent: str, compact: bool = False) -> str:
        if compact:
            return json.dumps(value, ensure_ascii=False)
        return json.dumps(value, indent=unit, ensure_ascii=False).replace("\n", newline + indent)

    def full() -> str:
        trailing = newline if not text or text.endswith(("\n", "\r")) else ""
        return dumps(new, "") + trailing

    if root is None:
        return full()
    edits: list[tuple[int, int, str]] = []

    def line_indent(pos: int) -> str:
        begin = text.rfind("\n", 0, pos) + 1
        end = begin
        while end < pos and text[end] in " \t":
            end += 1
        return text[begin:end]

    def replace(node: _Span, value: Any) -> None:
        edits.append((node.start, node.end, dumps(value, line_indent(node.start))))

    def edit(node: _Span, before: Any, after: Any) -> None:
        if _same(before, after):
            return
        if node.kind == "object" and isinstance(before, dict) and isinstance(after, dict):
            kept = [key for key in before if key in after]
            if [key for key in after if key in before] != kept:
                replace(node, after)  # reordered: not something integrate does
                return
            index = {key: n for n, key in enumerate(node.keys)}
            ops = [("keep", index[key], key) if key in after else ("del", index[key], key) for key in before]
            added = [(key, after[key]) for key in after if key not in before]
            container(node, ops, added, lambda op: (before[op[2]], after[op[2]]))
            return
        if node.kind == "array" and isinstance(before, list) and isinstance(after, list):
            ops, i, j = [], 0, 0
            while i < len(before) and j < len(after):
                if _same(before[i], after[j]):
                    ops.append(("keep", i, j))
                    i, j = i + 1, j + 1
                elif len(before) - i > len(after) - j:
                    ops.append(("del", i, None))
                    i += 1
                else:
                    ops.append(("keep", i, j))  # changed in place
                    i, j = i + 1, j + 1
            ops += [("del", n, None) for n in range(i, len(before))]
            container(node, ops, [(None, value) for value in after[j:]],
                      lambda op: (before[op[1]], after[op[2]]))
            return
        replace(node, after)

    def container(node: _Span, ops: list, added: list, pair) -> None:
        items = node.items
        if not items or all(op[0] == "del" for op in ops):
            replace(node, new_value(node, ops, added, pair))
            return
        for op in ops:
            if op[0] == "keep":
                edit(items[op[1]], *pair(op))
        deleted = [op[1] for op in ops if op[0] == "del"]
        runs: list[list[int]] = []
        for n in deleted:
            if runs and runs[-1][-1] == n - 1:
                runs[-1].append(n)
            else:
                runs.append([n])
        for run in runs:
            first, last = run[0], run[-1]
            if last + 1 < len(items):
                edits.append((items[first].lead, items[last + 1].lead, ""))
            else:
                edits.append((items[first - 1].end, items[last].end, ""))
        if added:
            gap = text[items[-2].end:items[-1].lead] if len(items) > 1 else "," + text[node.start + 1:items[0].lead]
            compact = "\n" not in gap
            if compact and gap == ",":
                gap = ", "
            indent = gap.rsplit("\n", 1)[1] if not compact else ""
            pieces = []
            for key, value in added:
                prefix = json.dumps(key, ensure_ascii=False) + ": " if node.kind == "object" else ""
                pieces.append(gap + prefix + dumps(value, indent, compact))
            edits.append((items[-1].end, items[-1].end, "".join(pieces)))

    def new_value(node: _Span, ops: list, added: list, pair) -> Any:
        if node.kind == "object":
            value = {op[2]: pair(op)[1] for op in ops if op[0] == "keep"}
            value.update(added)
            return value
        return [pair(op)[1] for op in ops if op[0] == "keep"] + [value for _, value in added]

    edit(root, old, new)
    result = text
    for start, end, replacement in sorted(edits, key=lambda item: (item[0], item[1]), reverse=True):
        result = result[:start] + replacement + result[end:]
    try:
        if _same(json.loads(result), new):
            return result
    except ValueError:
        pass
    return full()  # never write something that does not say exactly what was planned


# -- Codex config.toml notify ------------------------------------------------------------
def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def plan_codex_notify(path: Path, base_argv: list[str], *, remove: bool) -> Plan:
    plan = Plan(path, bom=_has_bom(path))
    original = _read(path)
    text = original or ""
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise IntegrateError(f"{path} is not valid TOML ({exc}); nothing was changed") from None
    wanted = [*base_argv, "hook", "codex"]
    current = parsed.get("notify")
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"

    def notify_line_index(replacement: list[str]) -> int | None:
        """The line holding the top-level ``notify``: the one whose replacement parses as intended.

        A line scan cannot tell a table header from a line inside a multi-line string, so every
        ``notify =`` line is tried and the TOML parser decides.
        """
        for index, line in enumerate(lines):
            if not re.match(r"notify\s*=", line.strip()) or not line.rstrip().endswith("]"):
                continue
            candidate = [*lines[:index], *replacement, *lines[index + 1:]]
            with contextlib.suppress(tomllib.TOMLDecodeError):
                reparsed = tomllib.loads("".join(candidate))
                if {k: v for k, v in reparsed.items() if k != "notify"} == \
                        {k: v for k, v in parsed.items() if k != "notify"} \
                        and reparsed.get("notify") == (tomllib.loads(replacement[0]).get("notify")
                                                       if replacement else None):
                    return index
        return None

    if remove:
        if current is None:
            plan.notes.append("no notify program is set")
            return plan
        if not _is_ours(current, "codex"):
            plan.notes.append(f"notify is another program ({' '.join(map(str, current))}); left as it is")
            return plan
        index = notify_line_index([])
        if index is None:
            raise IntegrateError(f"{path}: could not find our notify entry on one line; remove it by hand")
        drop = {index}
        if index > 0 and lines[index - 1].strip() == NOTIFY_COMMENT:
            drop.add(index - 1)
            if index + 1 < len(lines) and not lines[index + 1].strip():
                drop.add(index + 1)  # the blank line we put before the first table
        lines = [line for n, line in enumerate(lines) if n not in drop]
        plan.changes.append(f"remove notify = {current}")
    else:
        if current is not None and not _is_ours(current, "codex"):
            plan.refused = True
            plan.notes.append(
                f"Codex already runs another notify program: {current!r}. Codex runs only one, so it was not "
                "replaced. Use `anx integrate codex --hooks` (Codex's hooks.json; trust it once in Codex), "
                "or chain the two programs by hand.")
            return plan
        if current == wanted:
            return plan
        entry = f"notify = [{', '.join(_toml_string(item) for item in wanted)}]{newline}"
        if current is not None:
            index = notify_line_index([entry])
            if index is None:
                raise IntegrateError(f"{path}: could not find our notify entry on one line; fix it by hand")
            lines[index] = entry
            plan.changes.append(f"update notify = {wanted}")
        else:
            # The very top of the file is top level whatever follows (a line scan for the first
            # [table] is fooled by a multi-line string holding a line that starts with "[").
            block = [f"{NOTIFY_COMMENT}{newline}", entry]
            if lines:
                block.append(newline)
            lines[0:0] = block
            plan.changes.append(f"add notify = {wanted}")
    new_text = "".join(lines)
    reparsed = tomllib.loads(new_text)  # never write a file Codex could not read
    before = {k: v for k, v in parsed.items() if k != "notify"}
    after = {k: v for k, v in reparsed.items() if k != "notify"}
    if before != after or (reparsed.get("notify") != (None if remove else wanted)):
        raise IntegrateError(f"{path}: could not place notify without changing other settings; nothing was "
                             "changed (add it by hand)")
    plan.text = new_text
    return plan


# -- running -------------------------------------------------------------------------------
def plans(kind: str, *, remove: bool, hooks: bool = False, base_argv: list[str] | None = None) -> list[Plan]:
    base_argv = base_argv or anx_argv()
    if kind == "claude":
        return [plan_hooks_json(claude_home() / "settings.json", "claude", base_argv, CLAUDE_EVENTS, remove=remove,
                                background=True)]
    if kind == "codex":
        if hooks:
            return [plan_hooks_json(codex_home() / "hooks.json", "codex", base_argv, CODEX_HOOK_EVENTS,
                                    remove=remove, background=True)]
        return [plan_codex_notify(codex_home() / "config.toml", base_argv, remove=remove)]
    raise IntegrateError(f"unknown client {kind!r}")


def apply(plan: Plan, *, now: float | None = None) -> None:
    if plan.delete:
        # Only our entries were in it: no backup (nothing of the owner's is lost).
        with contextlib.suppress(FileNotFoundError):
            plan.path.unlink()
        return
    if plan.text is None:
        return
    now = time.time() if now is None else now
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    if plan.path.exists():
        plan.backup = _backup_path(plan.path, now)
        shutil.copy2(plan.path, plan.backup)
    temp = plan.path.with_name(plan.path.name + ".anx-tmp")
    # A BOM the file had stays in front (the notify block goes after it, not before).
    temp.write_text(plan.text, encoding="utf-8-sig" if plan.bom else "utf-8", newline="")
    os.replace(temp, plan.path)


def run(kind: str, *, dry_run: bool = False, remove: bool = False, hooks: bool = False,
        root: Path | None = None, out=print) -> int:
    try:
        planned = plans(kind, remove=remove, hooks=hooks, base_argv=anx_argv(root))
        for plan in planned:
            for note in plan.notes:
                out(f"{plan.path}: {note}")
            if not plan.changes:
                if not plan.refused:
                    out(f"{plan.path}: already {'clean' if remove else 'set up'}, nothing to change")
                continue
            for change in plan.changes:
                out(f"{plan.path}: {'would ' if dry_run else ''}{change}")
            if not dry_run:
                apply(plan)
                if plan.delete:
                    out(f"{plan.path}: deleted (it held only the agent-nexus entries)")
                if plan.backup is not None:
                    out(f"{plan.path}: backup written to {plan.backup}")
        if kind == "codex" and hooks and not remove and not dry_run and any(p.changes for p in planned):
            out("Codex runs hooks only after you trust them: open Codex once and approve the agent-nexus hooks.")
        return 1 if any(plan.refused for plan in planned) else 0
    except (IntegrateError, OSError) as exc:
        out(f"anx integrate {kind}: {exc}")
        return 1
