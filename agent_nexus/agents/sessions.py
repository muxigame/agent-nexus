"""Session discovery and management for the agent CLIs (docs/agent-chat.md §3, §4, §13).

Runs on the runner device, against the run-as user's HOME (``home``) and environment (``env``):

* Claude Code -- ``~/.claude/projects/<mangled cwd>/<id>.jsonl`` (``$CLAUDE_CONFIG_DIR`` wins). ``cwd`` comes from the
  records; the title is the ``custom-title`` / ``summary`` record, else the first real user message.
* Codex -- ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`` (``$CODEX_HOME`` wins) plus ``archived_sessions/``. The
  ``session_meta`` record gives ``id`` / ``cwd``; titles come from ``session_index.jsonl`` (``thread_name``) first.
* DeepSeek Harness (dsh) -- the index ``~/.dsh/storages/session_projcache/sessions/<id>.json`` (``$DSH_HOME`` wins)
  for listing, ``sessions/<encoded cwd>/<id>/session.v4.jsonl.zstd`` for history (zstd is optional). Plain
  ``*.jsonl`` / ``*.json`` files under ``sessions/`` or ``profiles/*/sessions/`` are listed too, parsed leniently.
* Gemini CLI -- ``~/.gemini/tmp/<project>/chats/*.json`` (and ``logs.json``, the per-project prompt log).
* Any other kind (``agents.custom``) -- its ``session_globs``, parsed leniently (``*.jsonl`` / ``*.json``).

Reads are bounded (whole file up to :data:`FULL_SCAN_BYTES`, else head + tail) and summaries are cached by
``(path, mtime, size)``. :func:`read` normalizes a transcript into the unified events of §4. :class:`SessionMeta`
keeps the console's own metadata (alias, pin, archive, hide) in ``<root>/.anx/agents/sessions.json``; the CLI's
files are never modified, and :func:`delete_files` only removes files that the discovery globs matched.

``defs`` is ``{kind: AgentDef}`` from ``registry.definitions()`` -- only ``kind``, ``label`` and ``session_globs`` are
used -- or ``{kind: [glob, ...]}``, or ``None`` for the built-in defaults (:data:`DEFAULT_GLOBS`).
"""
from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat as stat_mod
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

WINDOWS = sys.platform == "win32"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: Listing: a file up to this size is parsed whole; a larger one only its head and tail.
FULL_SCAN_BYTES = 2 * 1024 * 1024
#: Listing: files not yet in the cache are parsed for at most this long per call; the rest are listed from their
#: names for now (and parsed by a later call), so a first listing of a huge history never hangs the sidebar.
PARSE_BUDGET_S = 5.0
HEAD_BYTES = 512 * 1024
TAIL_BYTES = 256 * 1024
#: History: at most this much of a transcript is read (the newest part when larger).
READ_BYTES = 32 * 1024 * 1024
#: A JSON (not JSONL) session file larger than this is not parsed for the listing.
JSON_MAX_BYTES = 16 * 1024 * 1024
INDEX_MAX_BYTES = 8 * 1024 * 1024
CACHE_SIZE = 4000
TITLE_MAX = 80
TEXT_MAX = 20000
OUTPUT_MAX = 4000
INPUT_STR_MAX = 2000
DIFF_MAX = 8000
META_MAX_ENTRIES = 5000
ALIAS_MAX = 200

DEFAULT_LABELS = {"claude": "Claude Code", "codex": "Codex", "dsh": "DeepSeek Harness", "gemini": "Gemini CLI"}
KIND_ORDER = ("claude", "codex", "dsh", "gemini")

#: Built-in discovery globs, relative to HOME (same shape as ``AgentDef.session_globs``).
DEFAULT_GLOBS: dict[str, list[str]] = {
    "claude": [".claude/projects/*/*.jsonl"],
    "codex": [".codex/sessions/*/*/*/rollout-*.jsonl"],
    "dsh": [".dsh/sessions/*/*/session.v*.jsonl.zstd", ".dsh/storages/session_projcache/sessions/*.json"],
    "gemini": [".gemini/tmp/*/chats/*.json", ".gemini/tmp/*/logs.json"],
}
#: Always searched for these kinds in addition to the registry's globs (archived Codex rollouts; the lenient dsh
#: layouts of other versions and profiles).
EXTRA_GLOBS: dict[str, list[str]] = {
    "codex": [".codex/archived_sessions/rollout-*.jsonl", ".codex/archived_sessions/*/*/*/rollout-*.jsonl"],
    "dsh": [".dsh/sessions/*/*/session.v*.jsonl", ".dsh/sessions/*.jsonl", ".dsh/sessions/*/*.jsonl",
            ".dsh/profiles/*/sessions/*.jsonl", ".dsh/profiles/*/sessions/*/*.jsonl",
            ".dsh/profiles/*/sessions/*/*/session.v*.jsonl.zstd"],
    "gemini": [".gemini/tmp/*/chats/*.jsonl"],
}
#: A leading HOME-relative folder that an environment variable relocates.
HOME_VARS = {".claude": "CLAUDE_CONFIG_DIR", ".codex": "CODEX_HOME", ".dsh": "DSH_HOME", ".gemini": "GEMINI_CLI_HOME"}

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_TEMPLATE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")
_COMMAND = re.compile(r"<command-name>\s*([^<]*?)\s*</command-name>(?:.*?<command-args>\s*([^<]*?)\s*</command-args>)?",
                      re.DOTALL)
_GENERIC_NAMES = re.compile(r"^(session|rollout|chat|history|transcript|logs?|index|meta)([._-].*)?$", re.IGNORECASE)


# ============================================================================================ small helpers

def _ts(value: Any) -> float | None:
    """Epoch seconds from an ISO string, epoch seconds or epoch milliseconds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return None
        return number / 1000.0 if number > 1e11 else number
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            return _ts(float(text))
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    return None


def _clip(text: Any, limit: int) -> str:
    text = text if isinstance(text, str) else ("" if text is None else str(text))
    return text if len(text) <= limit else text[:limit] + f"…（已截断，共 {len(text)} 字）"


def _title(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= TITLE_MAX else text[:TITLE_MAX - 1].rstrip() + "…"


def _shrink(value: Any, depth: int = 0) -> Any:
    """A tool input with long strings cut, for the event stream."""
    if isinstance(value, str):
        return _clip(value, INPUT_STR_MAX)
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        return {str(k): _shrink(v, depth + 1) for k, v in list(value.items())[:100]}
    if isinstance(value, list):
        return [_shrink(v, depth + 1) for v in value[:100]]
    return value


def _text_of(content: Any) -> str:
    """Plain text of a message content: a string, or a list of ``{type: text|input_text|output_text, text}``."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in ("text", "content", "value"):
            if isinstance(content.get(key), str):
                return content[key]
            if isinstance(content.get(key), (list, dict)):
                return _text_of(content[key])
        return ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                kind = item.get("type")
                if kind in (None, "text", "input_text", "output_text", "summary_text", "markdown") \
                        and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif kind in ("image", "input_image", "image_url"):
                    parts.append("[图片]")
                elif kind is None and isinstance(item.get("content"), (str, list)):
                    parts.append(_text_of(item["content"]))
        return "\n".join(p for p in parts if p)
    return ""


def _json_loads(line: bytes | str) -> Any:
    try:
        return json.loads(line)
    except (ValueError, TypeError, RecursionError):
        return None


def _records(data: bytes, *, skip_first: bool = False) -> list[dict]:
    lines = data.split(b"\n")
    if skip_first and lines:
        lines = lines[1:]
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        record = _json_loads(line)
        if isinstance(record, dict):
            out.append(record)
    return out


def _read_jsonl(path: Path, size: int) -> tuple[list[dict], list[dict], bool]:
    """``(head, tail, complete)``: the whole file when small, else whole lines from its head and its tail."""
    with path.open("rb") as handle:
        if size <= FULL_SCAN_BYTES:
            return _records(handle.read(FULL_SCAN_BYTES + 1)), [], True
        head = handle.read(HEAD_BYTES)
        cut = head.rfind(b"\n")
        head = head[:cut] if cut > 0 else head
        handle.seek(max(0, size - TAIL_BYTES))
        tail = handle.read(TAIL_BYTES)
    return _records(head), _records(tail, skip_first=True), False


def _first_line(path: Path, limit: int = 4 * 1024 * 1024) -> dict | None:
    try:
        with path.open("rb") as handle:
            record = _json_loads(handle.readline(limit))
    except OSError:
        return None
    return record if isinstance(record, dict) else None


def _zstd_decompress(path: Path, limit: int) -> bytes | None:
    """Up to ``limit`` bytes of a zstd file: the ``zstandard`` module, ``compression.zstd`` (3.14) or the ``zstd``
    program; ``None`` when none is available. Raises ``ValueError`` for a damaged file."""
    try:
        import zstandard  # type: ignore[import-not-found]
        with path.open("rb") as handle:
            reader = zstandard.ZstdDecompressor().stream_reader(handle)
            chunks, total = [], 0
            while total < limit:
                chunk = reader.read(min(1 << 20, limit - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            return b"".join(chunks)
    except ImportError:
        pass
    except OSError:
        raise
    except Exception as error:
        raise ValueError(str(error)) from error
    try:
        from compression import zstd as _zstd  # type: ignore[import-not-found]
        with _zstd.open(path, "rb") as handle:
            return handle.read(limit)
    except ImportError:
        pass
    except OSError:
        raise
    except Exception as error:
        raise ValueError(str(error)) from error
    program = shutil.which("zstd")
    if not program:
        return None
    try:
        process = subprocess.run([program, "-dc", "--", str(path)], capture_output=True, timeout=30, check=False,
                                 creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if process.returncode != 0:
        raise ValueError(process.stderr.decode("utf-8", "replace").strip() or "zstd failed")
    return process.stdout[:limit]


# ============================================================================================ globs

def _labels(defs: Any) -> dict[str, str]:
    labels = dict(DEFAULT_LABELS)
    if isinstance(defs, Mapping):
        for kind, value in defs.items():
            label = getattr(value, "label", None)
            if isinstance(label, str) and label:
                labels[kind] = label
    return labels


def kind_globs(defs: Any) -> dict[str, list[str]]:
    """``{kind: [glob, ...]}`` from ``defs`` (``AgentDef`` objects, glob lists, or ``None``) plus :data:`EXTRA_GLOBS`."""
    if defs is None:
        source: dict[str, list[str]] = {k: list(v) for k, v in DEFAULT_GLOBS.items()}
    else:
        source = {}
        for kind, value in dict(defs).items():
            if isinstance(value, (list, tuple)):
                globs = [g for g in value if isinstance(g, str)]
            else:
                globs = [g for g in (getattr(value, "session_globs", None) or []) if isinstance(g, str)]
            if not globs and kind in DEFAULT_GLOBS and not hasattr(value, "session_globs"):
                globs = list(DEFAULT_GLOBS[kind])
            source[str(kind)] = globs
    out = {}
    for kind, globs in source.items():
        merged = list(globs)
        if globs or kind in DEFAULT_GLOBS:
            merged += [g for g in EXTRA_GLOBS.get(kind, []) if g not in merged]
        out[kind] = merged
    return out


def _expand(pattern: str, home: Path, env: Mapping[str, str]) -> tuple[Path, str] | None:
    """``(base, relative glob)``: templates ``{VAR:default}`` resolved, ``.codex`` etc. moved by their variable."""
    pattern = pattern.replace("\\", "/").strip()
    if not pattern or ".." in pattern.split("/"):
        return None

    def template(match: re.Match) -> str:
        value = env.get(match.group(1))
        if value:
            return value.replace("\\", "/")
        if match.group(2) is not None:
            return match.group(2)
        return match.group(0)
    pattern = _TEMPLATE.sub(template, pattern)
    parts = [p for p in pattern.split("/") if p not in ("", ".")]
    if not parts:
        return None
    drive = re.match(r"^([A-Za-z]:)", pattern)
    if drive:
        base, parts = Path(drive.group(1) + "/"), parts[1:]
    elif pattern.startswith("/"):
        base = Path("/")
    else:
        base = home
        variable = HOME_VARS.get(parts[0])
        relocated = env.get(variable, "") if variable else ""
        if relocated:
            base, parts = Path(relocated).expanduser(), parts[1:]
    literal = []
    for part in parts:
        if any(ch in part for ch in "*?["):
            break
        literal.append(part)
    rest = parts[len(literal):]
    base = base.joinpath(*literal) if literal else base
    if not rest:
        return base.parent, base.name
    return base, "/".join(rest)


def required_owner(home: Path) -> int | None:
    """The uid every session file must belong to, or None when no check is needed.

    The core may run as root (the Ubuntu system service) while sessions are the run-as account's: then
    discovery, reading and deleting follow symlinks the account controls (``~/.claude/projects/x -> /root``),
    so only files that account owns count. Running as that account itself, there is nothing to escalate."""
    if os.name == "nt" or not hasattr(os, "geteuid"):
        return None
    try:
        if os.geteuid() != 0:
            return None
        uid = os.stat(home).st_uid
    except OSError:
        return None
    return uid if uid != 0 else None


def _owned(path: Path, owner: int | None) -> bool:
    """A regular file (not a symlink) belonging to ``owner`` (any owner when None)."""
    if owner is None:
        return True
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat_mod.S_ISREG(info.st_mode) and info.st_uid == owner


def _scan(globs: Iterable[str], home: Path, env: Mapping[str, str]) -> dict[str, tuple[Path, os.stat_result]]:
    found: dict[str, tuple[Path, os.stat_result]] = {}
    owner = required_owner(home)
    for pattern in globs:
        expanded = _expand(pattern, home, env)
        if not expanded:
            continue
        base, rest = expanded
        try:
            if not base.is_dir():
                continue
            matches = base.glob(rest)
            for path in matches:
                key = str(path)
                if key in found:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if not os.path.isfile(path) or not _owned(path, owner):
                    continue
                found[key] = (path, stat)
        except (OSError, ValueError, NotImplementedError):
            continue
    return found


def _matches_globs(path: Path, globs: Iterable[str], home: Path, env: Mapping[str, str]) -> bool:
    for pattern in globs:
        expanded = _expand(pattern, home, env)
        if not expanded:
            continue
        base, rest = expanded
        try:
            relative = path.relative_to(base).as_posix()
        except ValueError:
            continue
        if ".." in relative.split("/"):
            continue
        if len(relative.split("/")) == len(rest.split("/")) and fnmatch.fnmatchcase(relative, rest):
            return True
    return False


def _guess_id(kind: str, path: Path) -> str:
    """The session id a file most likely belongs to, without opening it."""
    name = path.name
    if kind == "codex":
        match = _UUID.search(name)
        return match.group(0) if match else path.stem
    if name.startswith("session.") or name in ("meta.json", "session.json"):
        return path.parent.name
    for suffix in (".jsonl.zstd", ".jsonl", ".json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


# ============================================================================================ summaries (listing)

class _Cache:
    def __init__(self, size: int = CACHE_SIZE):
        self.size = size
        self.lock = threading.Lock()
        self.items: OrderedDict[tuple, Any] = OrderedDict()

    def get(self, key: tuple) -> Any:
        with self.lock:
            if key in self.items:
                self.items.move_to_end(key)
                return self.items[key]
        return None

    def put(self, key: tuple, value: Any) -> None:
        with self.lock:
            self.items[key] = value
            self.items.move_to_end(key)
            while len(self.items) > self.size:
                self.items.popitem(last=False)

    def clear(self) -> None:
        with self.lock:
            self.items.clear()


_CACHE = _Cache()


def clear_cache() -> None:
    _CACHE.clear()


def _summary(**fields: Any) -> dict:
    base = {"id": None, "title": "", "title_rank": 0, "cwd": None, "created_at": None, "updated_at": None,
            "messages": None, "blank": False, "primary": False, "shared": False, "cli_archived": False}
    base.update(fields)
    return base


# --- Claude Code ---------------------------------------------------------------------------------------------

def _claude_user_text(record: dict) -> str | None:
    """The text a person typed, or ``None`` for meta records, tool results and command output."""
    if record.get("isMeta") or record.get("isSidechain"):
        return None
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, list) and any(isinstance(i, dict) and i.get("type") == "tool_result" for i in content):
        return None
    text = _text_of(content).strip()
    if not text:
        return None
    command = _COMMAND.search(text)
    if command:
        return " ".join(p for p in command.groups() if p).strip() or None
    if text.startswith(("<", "Caveat:", "[Request interrupted")):
        return None
    return text


def _claude_summary(path: Path, stat: os.stat_result) -> list[dict]:
    head, tail, complete = _read_jsonl(path, stat.st_size)
    records = head + tail
    out = _summary(id=path.stem, primary=True)
    first_user = None
    custom, summary = None, None
    messages, seen_assistant = 0, set()
    for record in records:
        kind = record.get("type")
        if not out["cwd"] and isinstance(record.get("cwd"), str):
            out["cwd"] = record["cwd"]
        if isinstance(record.get("sessionId"), str) and record.get("sessionId") and out["id"] == path.stem \
                and not _UUID.fullmatch(path.stem):
            out["id"] = record["sessionId"]
        stamp = _ts(record.get("timestamp"))
        if stamp and (out["created_at"] is None or stamp < out["created_at"]):
            out["created_at"] = stamp
        if kind == "summary" and isinstance(record.get("summary"), str):
            summary = record["summary"]
        elif kind == "custom-title" and isinstance(record.get("customTitle"), str):
            custom = record["customTitle"]
        elif kind == "user":
            text = _claude_user_text(record)
            if text is not None:
                messages += 1
                if first_user is None:
                    first_user = text
        elif kind == "assistant" and not record.get("isSidechain"):
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            key = message.get("id") or record.get("uuid")
            if key not in seen_assistant and _text_of(message.get("content")).strip():
                seen_assistant.add(key)
                messages += 1
    if custom:
        out["title"], out["title_rank"] = _title(custom), 3
    elif summary:
        out["title"], out["title_rank"] = _title(summary), 2
    elif first_user:
        out["title"], out["title_rank"] = _title(first_user), 1
    out["messages"] = messages if complete else None
    out["blank"] = complete and first_user is None and not seen_assistant
    return [out]


# --- Codex ---------------------------------------------------------------------------------------------------

def _codex_payload(record: dict) -> tuple[str | None, dict]:
    payload = record.get("payload")
    if isinstance(payload, dict):
        return record.get("type"), payload
    return record.get("type"), record


def _codex_user_text(payload: dict) -> str | None:
    if payload.get("type") != "message" or payload.get("role") != "user":
        return None
    parts = []
    for item in payload.get("content") or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            text = item["text"].strip()
            if text and not text.startswith("<") and not text.startswith("# AGENTS.md"):
                parts.append(text)
    return "\n".join(parts) or None


def _codex_compacted_user(payload: dict) -> str | None:
    """The first plain user text kept in a ``compacted`` record's ``replacement_history``."""
    for item in payload.get("replacement_history") or []:
        if isinstance(item, dict):
            text = _codex_user_text({"type": "message", **item}) if item.get("role") == "user" else None
            if text:
                return text
    return None


def _codex_scan_title(path: Path, compacted_user: str | None) -> tuple[str | None, str | None]:
    """``(first own user text, first compacted user text)`` found by streaming the file line by line (at most
    ``READ_BYTES``); used only when the head and the tail held no prompt. The summary cache keeps the result."""
    read = 0
    try:
        with path.open("rb") as handle:
            for raw in handle:
                read += len(raw)
                if read > READ_BYTES:
                    break
                start = raw[:400]
                if b'"compacted"' in start:
                    if compacted_user is None:
                        record = _json_loads(raw)
                        if isinstance(record, dict):
                            compacted_user = _codex_compacted_user(_codex_payload(record)[1])
                    continue
                if b'"user"' not in raw[:2000] and b'"user_message"' not in raw[:2000]:
                    continue
                record = _json_loads(raw)
                if not isinstance(record, dict):
                    continue
                kind, payload = _codex_payload(record)
                if kind == "event_msg" and payload.get("type") == "user_message":
                    text = str(payload.get("message") or "").strip()
                    if text and not text.startswith("<"):
                        return text, compacted_user
                elif kind in ("response_item", None):
                    text = _codex_user_text(payload)
                    if text:
                        return text, compacted_user
    except OSError:
        pass
    return None, compacted_user


def _codex_summary(path: Path, stat: os.stat_result) -> list[dict]:
    match = _UUID.search(path.name)
    out = _summary(id=match.group(0) if match else path.stem, primary=True,
                   cli_archived="archived_sessions" in path.parts)
    first = _first_line(path)
    if first:
        kind, payload = _codex_payload(first)
        if kind in ("session_meta", None) or "cwd" in payload or "instructions" in payload:
            if isinstance(payload.get("id"), str) and payload["id"]:
                out["id"] = payload["id"]
            if isinstance(payload.get("cwd"), str):
                out["cwd"] = payload["cwd"]
            out["created_at"] = _ts(payload.get("timestamp")) or _ts(first.get("timestamp"))
    head, tail, complete = _read_jsonl(path, stat.st_size)
    first_user, messages, event_user = None, 0, None
    for record in head + tail:
        kind, payload = _codex_payload(record)
        if not out["cwd"] and kind == "turn_context" and isinstance(payload.get("cwd"), str):
            out["cwd"] = payload["cwd"]
        if kind == "event_msg" and payload.get("type") == "user_message" and event_user is None:
            text = str(payload.get("message") or "").strip()
            if text and not text.startswith("<"):
                event_user = text
        if kind in ("response_item", None):
            text = _codex_user_text(payload)
            if text:
                messages += 1
                first_user = first_user or text
            elif payload.get("type") == "message" and payload.get("role") == "assistant" \
                    and _text_of(payload.get("content")).strip():
                messages += 1
    compacted_user = None
    for record in head:
        kind, payload = _codex_payload(record)
        if kind == "compacted" and compacted_user is None:
            compacted_user = _codex_compacted_user(payload)
    title = event_user or first_user
    if not title and not complete:
        # The Codex desktop app writes a multi-megabyte ``compacted`` record near the top of a forked / resumed
        # session, so the first prompt of the session itself sits past the head (seen on a real Windows device).
        title, compacted_user = _codex_scan_title(path, compacted_user)
    title = title or compacted_user
    if title:
        out["title"], out["title_rank"] = _title(title), 1
    out["messages"] = messages if complete else None
    out["blank"] = complete and not title and messages == 0
    return [out]


def _codex_index(home: Path, env: Mapping[str, str]) -> dict[str, dict]:
    """``session_index.jsonl``: ``{id: {thread_name, updated_at}}``, the newest line per id."""
    root = Path(env["CODEX_HOME"]).expanduser() if env.get("CODEX_HOME") else home / ".codex"
    path = root / "session_index.jsonl"
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = ("codex-index", str(path), stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    try:
        with path.open("rb") as handle:
            if stat.st_size > INDEX_MAX_BYTES:
                handle.seek(stat.st_size - INDEX_MAX_BYTES)
                records = _records(handle.read(), skip_first=True)
            else:
                records = _records(handle.read())
    except OSError:
        return {}
    index: dict[str, dict] = {}
    for record in records:
        session_id = record.get("id")
        if isinstance(session_id, str) and session_id:
            index[session_id] = {**index.get(session_id, {}), **record}
    _CACHE.put(key, index)
    return index


# --- DeepSeek Harness ------------------------------------------------------------------------------------------

def _row(rows: dict, name: str) -> Any:
    value = rows.get(name)
    return value.get("val") if isinstance(value, dict) and "val" in value else value


def _dsh_index_summary(path: Path, stat: os.stat_result) -> list[dict]:
    if stat.st_size > INDEX_MAX_BYTES:
        return [_summary(id=path.stem)]
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return [_summary(id=path.stem)]
    record = data.get("record") if isinstance(data, dict) and isinstance(data.get("record"), dict) else data
    if not isinstance(record, dict):
        return [_summary(id=path.stem)]
    identity = record.get("identity") if isinstance(record.get("identity"), dict) else {}
    rows = record.get("rows") if isinstance(record.get("rows"), dict) else {}
    out = _summary(id=str(identity.get("id") or identity.get("sessionId") or path.stem))
    out["created_at"] = _ts(identity.get("createdAt"))
    cwd = identity.get("cwd") or _row(rows, "workingDirectory")
    out["cwd"] = cwd if isinstance(cwd, str) and cwd else None
    title = _row(rows, "title")
    title_input = _row(rows, "titleInput")
    if isinstance(title, str) and title.strip():
        out["title"], out["title_rank"] = _title(title), 3
    elif isinstance(title_input, dict) and isinstance(title_input.get("first"), str) and title_input["first"].strip():
        out["title"], out["title_rank"] = _title(title_input["first"]), 2
    meta = _row(rows, "sessionListMetadata")
    if isinstance(meta, dict):
        out["blank"] = bool(meta.get("blank"))
        out["updated_at"] = _ts(meta.get("lastPromptAt"))
    stats = _row(rows, "sessionStats")
    if isinstance(stats, dict):
        count = stats.get("messages") if isinstance(stats.get("messages"), int) else stats.get("turns")
        out["messages"] = count if isinstance(count, int) else None
    return [out]


def _generic_summary(kind: str, path: Path, stat: os.stat_result) -> list[dict]:
    """Any JSONL / JSON transcript: id from the records or the file name, title from the first user text."""
    out = _summary(id=_guess_id(kind, path), primary=True)
    name = path.name
    if name.endswith((".zstd", ".zst")):
        return [out]  # listed from the index; decompressing every file is not worth it
    records: list[dict] = []
    complete = True
    try:
        if name.endswith(".json"):
            if stat.st_size > JSON_MAX_BYTES:
                return [out]
            data = json.loads(path.read_bytes())
            records, meta = _json_records(data)
            _apply_meta_fields(out, meta)
        else:
            head, tail, complete = _read_jsonl(path, stat.st_size)
            records = head + tail
            if records:
                _apply_meta_fields(out, records[0])
    except (OSError, ValueError):
        return [out]
    builder = _Builder()
    _generic_feed(builder, records)
    users = [e for e in builder.events if e["type"] == "user" and e.get("text", "").strip()]
    if users and out["title_rank"] < 1:
        out["title"], out["title_rank"] = _title(users[0]["text"]), 1
    stamps = [e["t"] for e in builder.events if e.get("t")]
    if stamps and out["created_at"] is None:
        out["created_at"] = min(stamps)
    count = sum(1 for e in builder.events if e["type"] in ("user", "message"))
    out["messages"] = count if complete else None
    out["blank"] = complete and count == 0 and not records
    return [out]


def _json_records(data: Any) -> tuple[list[dict], dict]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)], {}
    if isinstance(data, dict):
        for key in ("messages", "history", "events", "items", "turns", "records"):
            if isinstance(data.get(key), list):
                return [r for r in data[key] if isinstance(r, dict)], data
        return [data], data
    return [], {}


def _apply_meta_fields(out: dict, meta: dict) -> None:
    if not isinstance(meta, dict):
        return
    for key in ("sessionId", "session_id", "id"):
        value = meta.get(key)
        if isinstance(value, str) and value and (key != "id" or _UUID.fullmatch(value)):
            out["id"] = value
            break
    for key in ("cwd", "workdir", "workingDirectory", "projectRoot", "project_root"):
        if isinstance(meta.get(key), str) and meta[key]:
            out["cwd"] = meta[key]
            break
    for key in ("title", "summary", "name", "thread_name"):
        if isinstance(meta.get(key), str) and meta[key].strip():
            out["title"], out["title_rank"] = _title(meta[key]), 2
            break
    created = _ts(meta.get("startTime") or meta.get("createdAt") or meta.get("created_at"))
    if created:
        out["created_at"] = created
    updated = _ts(meta.get("lastUpdated") or meta.get("updatedAt") or meta.get("updated_at"))
    if updated:
        out["updated_at"] = updated


# --- Gemini CLI ------------------------------------------------------------------------------------------------

def _gemini_project_root(folder: Path) -> str | None:
    for name in (".project_root", "project_root"):
        try:
            text = (folder / name).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if text:
            return text.splitlines()[0]
    return None


def _gemini_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text_of(p) if not isinstance(p, str) else p for p in content if p)
    if isinstance(message.get("parts"), list):
        return _text_of(message["parts"])
    return _text_of(content) if content else str(message.get("message") or "")


def _gemini_summary(path: Path, stat: os.stat_result) -> list[dict]:
    project = path.parent.parent if path.parent.name == "chats" else path.parent
    cwd = _gemini_project_root(project)
    if path.name == "logs.json":
        return _gemini_logs_summary(path, stat, cwd)
    if path.suffix == ".jsonl":
        out = _generic_summary("gemini", path, stat)
        for item in out:
            item["cwd"] = item["cwd"] or cwd
            item["project_hash"] = project.name
        return out
    out = _summary(id=_guess_id("gemini", path), primary=True, cwd=cwd, project_hash=project.name)
    if stat.st_size > JSON_MAX_BYTES:
        return [out]
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return [out]
    if not isinstance(data, dict):
        return [out]
    if isinstance(data.get("sessionId"), str) and data["sessionId"]:
        out["id"] = data["sessionId"]
    out["created_at"] = _ts(data.get("startTime"))
    out["updated_at"] = _ts(data.get("lastUpdated"))
    for key in ("projectRoot", "cwd", "workingDirectory"):
        if isinstance(data.get(key), str) and data[key]:
            out["cwd"] = data[key]
    if isinstance(data.get("summary"), str) and data["summary"].strip():
        out["title"], out["title_rank"] = _title(data["summary"]), 2
    messages = [m for m in data.get("messages") or [] if isinstance(m, dict)]
    count = 0
    for message in messages:
        if message.get("type") in ("user", "gemini", "model", "assistant"):
            count += 1
        if message.get("type") == "user" and out["title_rank"] < 1:
            text = _gemini_text(message).strip()
            if text and not text.startswith("/"):
                out["title"], out["title_rank"] = _title(text), 1
    out["messages"] = count
    out["blank"] = count == 0
    return [out]


def _gemini_logs_summary(path: Path, stat: os.stat_result, cwd: str | None) -> list[dict]:
    if stat.st_size > JSON_MAX_BYTES:
        return []
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return []
    sessions: dict[str, dict] = {}
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("sessionId"), str):
            continue
        item = sessions.setdefault(entry["sessionId"], _summary(id=entry["sessionId"], cwd=cwd, shared=True,
                                                                 messages=0, project_hash=path.parent.name))
        text = str(entry.get("message") or "").strip()
        stamp = _ts(entry.get("timestamp"))
        if stamp:
            item["created_at"] = min(item["created_at"] or stamp, stamp)
            item["updated_at"] = max(item["updated_at"] or stamp, stamp)
        if entry.get("type", "user") == "user" and text:
            item["messages"] += 1
            if not item["title"] and not text.startswith("/"):
                item["title"], item["title_rank"] = _title(text), 1
    return list(sessions.values())


# --- dispatch ----------------------------------------------------------------------------------------------------

def _parse(kind: str, path: Path, stat: os.stat_result) -> list[dict]:
    if kind == "claude" and path.suffix == ".jsonl":
        return _claude_summary(path, stat)
    if kind == "codex" and path.suffix == ".jsonl":
        return _codex_summary(path, stat)
    if kind == "dsh" and path.suffix == ".json" and "session_projcache" in path.parts:
        return _dsh_index_summary(path, stat)
    if kind == "gemini":
        return _gemini_summary(path, stat)
    return _generic_summary(kind, path, stat)


def _summaries(kind: str, path: Path, stat: os.stat_result, deadline: float | None = None) -> list[dict]:
    key = (kind, str(path), stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    if deadline is not None and time.monotonic() > deadline:
        quick = _summary(id=_guess_id(kind, path), primary=not (kind == "dsh" and path.suffix == ".json"))
        quick.update(file=str(path), mtime=stat.st_mtime, fsize=stat.st_size, shared=path.name == "logs.json")
        return [] if quick["shared"] else [quick]  # not cached: parsed properly next time
    try:
        found = _parse(kind, path, stat)
    except Exception:  # noqa: BLE001 - one odd file never breaks the listing
        found = [_summary(id=_guess_id(kind, path), primary=True)]
    for item in found:
        item["file"] = str(path)
        item["mtime"] = stat.st_mtime
        item["fsize"] = stat.st_size
    _CACHE.put(key, found)
    return found


def _merge(kind: str, parts: list[dict], label: str) -> dict:
    """One session from every file that names it: metadata from the best-titled part, history from the primary."""
    primary = [p for p in parts if p.get("primary")]
    reader = max(primary, key=lambda p: (p["file"].endswith(".zstd"), p["fsize"]), default=None)
    best = max(parts, key=lambda p: (p["title_rank"], p.get("primary", False)))
    session_id = str(best.get("id") or parts[0]["id"])

    def first(key: str) -> Any:
        for part in [best] + parts:
            if part.get(key) is not None and part.get(key) != "":
                return part[key]
        return None
    updated = max((p["updated_at"] or 0) for p in parts) or None
    mtime = max(p["mtime"] for p in parts)
    title = best["title"] or ""
    if not title:
        stem = Path(reader["file"]).name.split(".")[0] if reader else ""
        title = stem if stem and not _GENERIC_NAMES.match(stem) and stem != session_id else ""
    counts = [p["messages"] for p in parts if isinstance(p.get("messages"), int)]
    created = [p["created_at"] for p in parts if p.get("created_at")]
    return {
        "kind": kind, "id": session_id, "title": title, "cwd": first("cwd"),
        "updated_at": round(max(updated or 0, mtime), 3), "created_at": round(min(created), 3) if created else None,
        "messages": max(counts) if counts else None, "size": sum(p["fsize"] for p in parts if not p.get("shared")),
        "source": "disk", "managed": False, "pinned": False, "archived": any(p.get("cli_archived") for p in parts),
        "alias": None, "hidden": False, "blank": all(p.get("blank") for p in parts),
        "label": label, "file": reader["file"] if reader else None,
        "files": sorted({p["file"] for p in parts}),
        "own_files": sorted({p["file"] for p in parts if not p.get("shared")}),
        "readable": bool(reader) or any(p.get("shared") for p in parts),
        **({"project_hash": first("project_hash")} if first("project_hash") else {}),
    }


def _discover_kind(kind: str, globs: list[str], home: Path, env: Mapping[str, str], limit: int,
                   want: set[str], label: str, deadline: float) -> list[dict]:
    files = _scan(globs, home, env)
    ordered = sorted(files.values(), key=lambda item: item[1].st_mtime, reverse=True)
    # Newest first, a little past the limit: blank and hidden sessions are dropped later, and dsh keeps two files
    # (index + transcript) per session.
    window = limit * 2 + 20 if kind == "dsh" else limit + 20
    chosen = ordered[:window] + [item for item in ordered[window:] if _guess_id(kind, item[0]) in want]
    groups: dict[str, list[dict]] = {}
    for path, stat in chosen:
        for part in _summaries(kind, path, stat, deadline):
            if part.get("id"):
                groups.setdefault(str(part["id"]), []).append(dict(part))
    sessions = [_merge(kind, parts, label) for parts in groups.values()]
    if kind == "codex":
        index = _codex_index(home, env)
        for session in sessions:
            entry = index.get(session["id"])
            if entry and isinstance(entry.get("thread_name"), str) and entry["thread_name"].strip():
                session["title"] = _title(entry["thread_name"])
    return sessions


def _resolve_gemini_cwds(sessions: list[dict], known_dirs: Iterable[str]) -> None:
    """Gemini names its project folder ``sha256(cwd)``: match it against the directories we already know."""
    pending = [s for s in sessions if s["kind"] == "gemini" and not s.get("cwd") and s.get("project_hash")]
    if not pending:
        return
    hashes: dict[str, str] = {}
    for directory in known_dirs:
        if isinstance(directory, str) and directory:
            hashes.setdefault(hashlib.sha256(directory.encode("utf-8")).hexdigest(), directory)
    for session in pending:
        if session["project_hash"] in hashes:
            session["cwd"] = hashes[session["project_hash"]]


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def discover(defs: Any, home: Path, *, kind: str | None = None, limit: int = 200, env: Mapping[str, str] | None = None,
             meta: SessionMeta | None = None, include_hidden: bool = False, include_blank: bool = False,
             query: str | None = None, known_dirs: Iterable[str] = ()) -> list[dict]:
    """Sessions on disk: per kind every pinned one plus the ``limit`` most recent others (renamed or console-started
    ones are kept beyond that), sorted pinned first, then by ``updated_at`` (newest first).

    Each: ``{kind, id, title, cwd, updated_at, created_at, messages, size, source, managed, pinned, archived, alias,
    hidden, label, file, files}``. Hidden (removed from the list) and blank (never prompted) sessions are left out
    unless asked for. Never raises for unreadable files; ``kind`` that is unknown gives ``[]``."""
    home = Path(home)
    env = _env(env)
    limit = max(1, min(int(limit or 200), 5000))
    globs = kind_globs(defs)
    labels = _labels(defs)
    kinds = [kind] if kind else sorted(globs, key=lambda k: (KIND_ORDER.index(k) if k in KIND_ORDER else 99, k))
    entries = meta.all() if meta else {}
    found: dict[str, list[dict]] = {}
    deadline = time.monotonic() + PARSE_BUDGET_S
    for name in kinds:
        if name not in globs:
            continue
        want = {key.split(":", 1)[1] for key, value in entries.items()
                if key.startswith(f"{name}:") and (value.get("pinned") or value.get("alias") or value.get("managed"))}
        found[name] = _discover_kind(name, globs[name], home, env, limit, want, labels.get(name, name), deadline)
    every = [s for items in found.values() for s in items]
    _resolve_gemini_cwds(every, list(known_dirs) + [s["cwd"] for s in every if s.get("cwd")])
    if meta:
        meta.apply(every)
    needle = (query or "").strip().casefold()
    out: list[dict] = []
    for items in found.values():
        if not include_hidden:
            items = [s for s in items if not s["hidden"]]
        if not include_blank:
            items = [s for s in items if not s["blank"] or s["pinned"] or s["alias"] or s["managed"]]
        if needle:
            items = [s for s in items if any(needle in str(s.get(k) or "").casefold()
                                             for k in ("alias", "title", "cwd", "id"))]
        items.sort(key=lambda s: -(s["updated_at"] or 0))
        rest = [s for s in items if not s["pinned"]]
        # pinned sessions never take the place of recent ones; renamed / console-started ones are never cut off
        out += [s for s in items if s["pinned"]] + rest[:limit] + [s for s in rest[limit:] if s["alias"] or s["managed"]]
    out.sort(key=lambda s: (not s["pinned"], -(s["updated_at"] or 0)))
    return out


def grouped(sessions: list[dict], defs: Any = None) -> list[dict]:
    """``[{kind, label, count, sessions}]`` in the console's CLI order (the order inside each group is kept)."""
    labels = _labels(defs)
    groups: dict[str, list[dict]] = {}
    for session in sessions:
        groups.setdefault(session["kind"], []).append(session)
    order = sorted(groups, key=lambda k: (KIND_ORDER.index(k) if k in KIND_ORDER else 99, k))
    return [{"kind": k, "label": labels.get(k, k), "count": len(groups[k]), "sessions": groups[k]} for k in order]


def recent_cwds(sessions: list[dict], *, kind: str | None = None, limit: int = 10) -> list[dict]:
    """The working directories the discovered sessions used, most recent first: ``[{path, kind, count, updated_at}]``."""
    seen: dict[str, dict] = {}
    for session in sessions:
        cwd = session.get("cwd")
        if not cwd or (kind and session["kind"] != kind):
            continue
        key = cwd.casefold() if WINDOWS else cwd
        item = seen.setdefault(key, {"path": cwd, "kinds": [], "count": 0, "updated_at": 0})
        item["count"] += 1
        item["updated_at"] = max(item["updated_at"], session.get("updated_at") or 0)
        if session["kind"] not in item["kinds"]:
            item["kinds"].append(session["kind"])
    return sorted(seen.values(), key=lambda i: -i["updated_at"])[:limit]


def find(defs: Any, home: Path, kind: str, session_id: str, *, env: Mapping[str, str] | None = None) -> dict | None:
    """The discovered session ``kind`` / ``session_id`` (hidden and blank ones included), or ``None``."""
    if not isinstance(session_id, str) or not session_id:
        return None
    for session in discover(defs, home, kind=kind, limit=5000, env=env, include_hidden=True, include_blank=True):
        if session["id"] == session_id:
            return session
    return None


# ============================================================================================ context usage (§16.3)

#: Context windows by model-name prefix (longest prefix wins) when the CLI does not report one.
MODEL_WINDOWS: dict[str, int] = {
    "claude": 200_000,
    "gpt-5": 272_000,
    "gpt-4.1": 1_047_576,
    "gpt-4o": 128_000,
    "o3": 200_000,
    "o4-mini": 200_000,
    "codex-mini": 200_000,
}
#: A Codex rollout is read only this far from its end for its latest ``token_count``.
ROLLOUT_TAIL_BYTES = 256 * 1024


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def context_window(model: Any, used: int | None = None) -> int | None:
    """The built-in context window of ``model`` (None when unknown). A ``[1m]`` model, or a Claude context that
    already holds more than 200K tokens, has the 1M window."""
    if not isinstance(model, str) or not model.strip():
        return None
    name = model.strip().lower()
    if name.endswith("[1m]"):
        return 1_000_000
    name = name.split("/")[-1]
    best = max((prefix for prefix in MODEL_WINDOWS if name.startswith(prefix)), key=len, default=None)
    if best is None:
        return None
    window = MODEL_WINDOWS[best]
    if best == "claude" and used and used > window:
        return 1_000_000
    return window


def usage_event(used: int | None, limit: int | None, model: Any = None, turn: dict | None = None) -> dict:
    """The §16.3 ``usage`` event."""
    event: dict[str, Any] = {"type": "usage", "context": {
        "used": int(used) if used is not None else None, "limit": int(limit) if limit else None,
        "model": str(model) if isinstance(model, str) and model else None}}
    if turn:
        event["turn"] = dict(turn)
    return event


def claude_context_used(usage: Any) -> int:
    """What an assistant message's ``usage`` says the context holds: input + cache read + cache creation + output."""
    usage = usage if isinstance(usage, dict) else {}
    return sum(_int(usage.get(key)) for key in ("input_tokens", "cache_read_input_tokens",
                                                "cache_creation_input_tokens", "output_tokens"))


def codex_token_info(info: Any) -> dict | None:
    """``{used, limit, turn}`` from a Codex ``token_count`` ``info`` (None when it carries nothing)."""
    if not isinstance(info, dict):
        return None
    last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else None
    limit = _int(info.get("model_context_window")) or None
    out: dict[str, Any] = {"limit": limit, "used": None, "turn": None}
    if last is not None:
        out["used"] = _int(last.get("input_tokens")) + _int(last.get("output_tokens"))
        turn = {"input": _int(last.get("input_tokens")), "output": _int(last.get("output_tokens"))}
        if _int(last.get("cached_input_tokens")):
            turn["cached"] = _int(last.get("cached_input_tokens"))
        out["turn"] = turn
    if out["used"] is None and limit is None:
        return None
    return out


def codex_rollout(home: Path, env: Mapping[str, str] | None, session_id: str) -> Path | None:
    """The rollout file of Codex thread ``session_id`` (the newest when several match), or None."""
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{7,127}", session_id):
        return None
    env = _env(env)
    globs = [g.replace("rollout-*.jsonl", f"rollout-*{session_id}.jsonl") for g in kind_globs(None)["codex"]]
    found = _scan(globs, Path(home), env)
    if not found:
        return None
    return max(found.values(), key=lambda item: item[1].st_mtime)[0]


def codex_context(home: Path, env: Mapping[str, str] | None, session_id: str) -> dict | None:
    """The latest context figures in a Codex thread's rollout, read from its tail only:
    ``{used, limit, model, turn}`` (fields None when unknown), or None when there is no rollout / no figures."""
    path = codex_rollout(home, env, session_id)
    return codex_context_file(path) if path is not None else None


def codex_context_file(path: Path) -> dict | None:
    """:func:`codex_context` for a known rollout file."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > ROLLOUT_TAIL_BYTES:
                handle.seek(size - ROLLOUT_TAIL_BYTES)
            data = handle.read(ROLLOUT_TAIL_BYTES)
    except OSError:
        return None
    return _codex_context_of(_records(data, skip_first=size > ROLLOUT_TAIL_BYTES))


def _codex_context_of(records: list[dict]) -> dict | None:
    info: dict | None = None
    model = None
    for record in records:
        kind, payload = _codex_payload(record)
        if kind == "turn_context" and isinstance(payload.get("model"), str):
            model = payload["model"]
        elif kind == "event_msg" and payload.get("type") == "token_count":
            found = codex_token_info(payload.get("info"))
            if found is not None:
                info = found
    if info is None:
        return None
    return {**info, "model": model}


# ============================================================================================ unified events (read)

class _Builder:
    """Collects §4 events; tool results update their call in place, streamed chunks are joined."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.tools: dict[str, dict] = {}
        self.usage: dict[str, int] = {}
        #: the latest context figures (§16.3): ``{used, limit, model, turn}``; one ``usage`` event at the end
        self.context: dict | None = None

    def add(self, event: dict, t: float | None = None, *, join: bool = False) -> dict | None:
        if event.get("type") in ("user", "message", "thinking") and not str(event.get("text") or "").strip() \
                and not join:
            return None
        if join and self.events:
            last = self.events[-1]
            if last["type"] == event["type"] and last.get("role") == event.get("role") and last.get("_join"):
                last["text"] = last.get("text", "") + str(event.get("text") or "")
                return last
        if t:
            event["t"] = round(t, 3)
        if join:
            event["_join"] = True
        if "text" in event:
            event["text"] = str(event["text"])
        self.events.append(event)
        return event

    def tool(self, call_id: str | None, t: float | None = None, **fields: Any) -> dict:
        key = str(call_id) if call_id else f"tool_{len(self.events)}"
        existing = self.tools.get(key)
        clean = {k: v for k, v in fields.items() if v is not None}
        if existing is not None:
            existing.update(clean)
            return existing
        event = {"type": "tool", "id": key, "name": clean.pop("name", "tool"), "title": "", "status": "running"}
        event.update(clean)
        if t:
            event["t"] = round(t, 3)
        self.events.append(event)
        self.tools[key] = event
        return event

    def count(self, **usage: int | None) -> None:
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.usage[key] = self.usage.get(key, 0) + int(value)

    def finish(self, session_id: str, *, max_events: int, dropped: int = 0, usage: dict | None = None) -> list[dict]:
        events = []
        for event in self.events:
            event.pop("_join", None)
            if event["type"] in ("user", "message", "thinking"):
                event["text"] = _clip(event.get("text", "").strip("\n"), TEXT_MAX)
                if not event["text"].strip():
                    continue
            if event["type"] == "tool":
                if event.get("status") == "running":
                    event["status"] = "ok"  # a finished transcript: no call is still running
                if "output" in event:
                    event["output"] = _clip(event["output"], OUTPUT_MAX)
            events.append(event)
        max_events = max(1, int(max_events or 2000))
        tail: list[dict] = []
        if self.context and (self.context.get("used") is not None or self.context.get("limit")) and max_events > 1:
            tail.append(usage_event(self.context.get("used"), self.context.get("limit"), self.context.get("model"),
                                    self.context.get("turn")))
        keep = max_events - 1 - len(tail)
        if len(events) > keep:
            dropped += len(events) - keep
            events = events[-keep:] if keep else []
        stamps = [e["t"] for e in events if e.get("t")]
        if tail and stamps:
            tail[0]["t"] = max(stamps)
        events += tail
        status: dict[str, Any] = {"type": "status", "state": "idle", "session_id": session_id}
        usage = usage if usage is not None else self.usage
        if usage:
            status["usage"] = {"input": int(usage.get("input", 0)), "output": int(usage.get("output", 0)),
                               "cost_usd": usage.get("cost_usd", 0)}
        if dropped:
            status["dropped"] = dropped
        if stamps:
            status["t"] = max(stamps)
        events.append(status)
        for seq, event in enumerate(events, 1):
            event["seq"] = seq
        return events


def _read_bytes(path: Path) -> tuple[bytes, int]:
    """``(data, dropped_bytes)``: the whole file, or its newest :data:`READ_BYTES` (starting on a whole line)."""
    if path.name.endswith(".zstd") or path.name.endswith(".zst"):
        data = _zstd_decompress(path, READ_BYTES * 4)
        if data is None:
            raise LookupError("zstd")
        if len(data) > READ_BYTES:
            cut = data.find(b"\n", len(data) - READ_BYTES)
            return data[cut + 1:], cut + 1
        return data, 0
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size <= READ_BYTES:
            return handle.read(), 0
        handle.seek(size - READ_BYTES)
        data = handle.read()
    cut = data.find(b"\n")
    return data[cut + 1:], size - READ_BYTES + cut + 1


def _title_for_tool(name: str, data: Any) -> str:
    if not isinstance(data, dict):
        return _title(str(data)) if isinstance(data, str) else ""
    for key in ("command", "cmd", "description", "file_path", "path", "pattern", "url", "query", "prompt", "notebook_path"):
        value = data.get(key)
        if isinstance(value, list):
            value = _shell_command(value)
        if isinstance(value, str) and value.strip():
            return _title(value)
    return ""


def _shell_command(argv: list) -> str:
    parts = [str(p) for p in argv]
    if len(parts) >= 3 and parts[1] in ("-lc", "-c") and os.path.basename(parts[0]) in ("bash", "sh", "zsh"):
        return parts[2]
    if len(parts) >= 3 and parts[0].lower().endswith(("powershell", "powershell.exe", "pwsh", "pwsh.exe")) \
            and parts[-2].lower() == "-command":
        return parts[-1]
    return " ".join(parts)


def _diff(path: str, old: str, new: str) -> str:
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), fromfile=path, tofile=path,
                                 lineterm="", n=2)
    return _clip("\n".join(lines), DIFF_MAX)


# --- Claude ------------------------------------------------------------------------------------------------------

def _claude_feed(builder: _Builder, records: list[dict]) -> None:
    seen_usage: set[str] = set()
    for record in records:
        kind = record.get("type")
        if record.get("isSidechain") or kind not in ("user", "assistant"):
            continue
        t = _ts(record.get("timestamp"))
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        content = message.get("content")
        if kind == "user":
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        output = item.get("content")
                        output = output if isinstance(output, str) else _text_of(output)
                        builder.tool(item.get("tool_use_id"), t, output=output,
                                     status="error" if item.get("is_error") else "ok")
            text = _claude_user_text(record)
            if text is not None:
                builder.add({"type": "user", "text": text}, t)
            continue
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else None
        key = message.get("id") or record.get("uuid")
        if usage and claude_context_used(usage) and message.get("model") != "<synthetic>":
            used = claude_context_used(usage)
            model = message.get("model") if isinstance(message.get("model"), str) and message["model"] else \
                (builder.context or {}).get("model")
            turn = {"input": sum(_int(usage.get(k)) for k in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
                "output": _int(usage.get("output_tokens"))}
            if _int(usage.get("cache_read_input_tokens")):
                turn["cached"] = _int(usage.get("cache_read_input_tokens"))
            builder.context = {"used": used, "limit": context_window(model, used), "model": model, "turn": turn}
        if usage and key not in seen_usage:
            seen_usage.add(key)
            builder.count(input=sum(int(usage.get(k) or 0) for k in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
                output=usage.get("output_tokens"))
        if isinstance(content, str):
            builder.add({"type": "message", "role": "assistant", "text": content}, t)
            continue
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "text":
                builder.add({"type": "message", "role": "assistant", "text": item.get("text") or ""}, t)
            elif itype in ("thinking", "redacted_thinking"):
                builder.add({"type": "thinking", "text": item.get("thinking") or ""}, t)
            elif itype in ("tool_use", "server_tool_use"):
                name = str(item.get("name") or "tool")
                data = item.get("input") if isinstance(item.get("input"), dict) else {}
                builder.tool(item.get("id"), t, name=name, title=_title_for_tool(name, data), input=_shrink(data))
                _claude_file_change(builder, name, data, t)


def _claude_file_change(builder: _Builder, name: str, data: dict, t: float | None) -> None:
    path = data.get("file_path") or data.get("notebook_path")
    if not isinstance(path, str) or not path:
        return
    if name == "Edit":
        diff = _diff(path, str(data.get("old_string") or ""), str(data.get("new_string") or ""))
        builder.add({"type": "file_change", "path": path, "kind": "edit", "diff": diff}, t)
    elif name == "MultiEdit":
        diffs = [_diff(path, str(e.get("old_string") or ""), str(e.get("new_string") or ""))
                 for e in data.get("edits") or [] if isinstance(e, dict)]
        builder.add({"type": "file_change", "path": path, "kind": "edit", "diff": _clip("\n".join(diffs), DIFF_MAX)}, t)
    elif name == "Write":
        diff = _diff(path, "", str(data.get("content") or ""))
        builder.add({"type": "file_change", "path": path, "kind": "add", "diff": diff}, t)
    elif name == "NotebookEdit":
        builder.add({"type": "file_change", "path": path, "kind": "edit", "diff": ""}, t)


# --- Codex -------------------------------------------------------------------------------------------------------

def _patch_changes(patch: str) -> list[dict]:
    """``apply_patch`` envelopes: ``*** Add File: / *** Update File: / *** Delete File:`` blocks."""
    changes: list[dict] = []
    current: dict | None = None
    for line in patch.splitlines():
        match = re.match(r"^\*\*\* (Add|Update|Delete) File: (.+)$", line)
        if match:
            current = {"type": "file_change", "path": match.group(2).strip(),
                       "kind": {"Add": "add", "Update": "edit", "Delete": "delete"}[match.group(1)], "diff": []}
            changes.append(current)
            continue
        if line.startswith("*** Move to: ") and current:
            current["path"] = line[len("*** Move to: "):].strip()
            continue
        if line.startswith("*** ") or current is None:
            continue
        current["diff"].append(line)
    for change in changes:
        change["diff"] = _clip("\n".join(change["diff"]), DIFF_MAX)
    return changes


def _codex_args(raw: Any) -> Any:
    if isinstance(raw, str):
        parsed = _json_loads(raw)
        return parsed if parsed is not None else raw
    return raw


def _codex_output(raw: Any) -> tuple[str, bool]:
    """``(text, failed)`` of a ``function_call_output`` / ``custom_tool_call_output``."""
    failed = False
    if isinstance(raw, str):
        parsed = _json_loads(raw) if raw.lstrip().startswith("{") else None
        if isinstance(parsed, dict):
            raw = parsed
        else:
            return raw, False
    if isinstance(raw, dict):
        if raw.get("success") is False:
            failed = True
        meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        code = meta.get("exit_code", raw.get("exit_code"))
        if isinstance(code, int) and code != 0:
            failed = True
        text = raw.get("output", raw.get("content"))
        return (text if isinstance(text, str) else _text_of(text) or json.dumps(raw, ensure_ascii=False)), failed
    if isinstance(raw, list):
        return _text_of(raw), False
    return "" if raw is None else str(raw), False


def _codex_feed(builder: _Builder, records: list[dict]) -> dict:
    usage: dict = {}
    model = None
    for record in records:
        kind, payload = _codex_payload(record)
        t = _ts(record.get("timestamp"))
        if kind == "turn_context" and isinstance(payload.get("model"), str):
            model = payload["model"]
            if builder.context is not None:
                builder.context["model"] = model
            continue
        if kind == "event_msg":
            if payload.get("type") == "token_count" and isinstance(payload.get("info"), dict):
                total = payload["info"].get("total_token_usage")
                if isinstance(total, dict):
                    usage = {"input": int(total.get("input_tokens") or 0), "output": int(total.get("output_tokens") or 0)}
                found = codex_token_info(payload["info"])
                if found is not None:
                    limit = found["limit"] or (builder.context or {}).get("limit") or context_window(model)
                    builder.context = {**found, "limit": limit, "model": model}
            elif payload.get("type") == "error" and payload.get("message"):
                builder.add({"type": "error", "text": str(payload["message"])}, t)
            continue
        if kind not in ("response_item", None):
            continue
        ptype = payload.get("type")
        if ptype == "message":
            role = payload.get("role")
            if role == "user":
                text = _codex_user_text(payload)
                if text:
                    builder.add({"type": "user", "text": text}, t)
            elif role == "assistant":
                builder.add({"type": "message", "role": "assistant", "text": _text_of(payload.get("content"))}, t)
        elif ptype == "reasoning":
            text = _text_of(payload.get("summary")) or _text_of(payload.get("content"))
            builder.add({"type": "thinking", "text": text}, t)
        elif ptype in ("function_call", "custom_tool_call", "local_shell_call"):
            name = str(payload.get("name") or ("shell" if ptype == "local_shell_call" else "tool"))
            data = _codex_args(payload.get("arguments") if ptype == "function_call" else payload.get("input"))
            if ptype == "local_shell_call":
                data = payload.get("action") if isinstance(payload.get("action"), dict) else {}
            call_id = payload.get("call_id") or payload.get("id")
            patch = None
            if name == "apply_patch":
                patch = data if isinstance(data, str) else (data.get("input") if isinstance(data, dict) else None)
                title = ", ".join(c["path"] for c in _patch_changes(patch or ""))[:TITLE_MAX] or "apply_patch"
            elif isinstance(data, dict) and isinstance(data.get("command"), list):
                argv = data["command"]
                if argv[:1] == ["apply_patch"] and len(argv) > 1:
                    patch = str(argv[1])
                title = _title(_shell_command(argv))
            else:
                title = _title_for_tool(name, data)
            status = None
            if payload.get("status") in ("completed", "failed", "incomplete"):
                status = "error" if payload["status"] == "failed" else None
            builder.tool(call_id, t, name=name, title=title,
                         input=_shrink(data if isinstance(data, dict) else {"input": data}), status=status)
            for change in _patch_changes(patch or ""):
                builder.add(change, t)
        elif ptype in ("function_call_output", "custom_tool_call_output"):
            text, failed = _codex_output(payload.get("output"))
            builder.tool(payload.get("call_id"), t, output=text, status="error" if failed else "ok")
        elif ptype == "web_search_call":
            action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
            builder.tool(payload.get("id"), t, name="web_search", title=_title(str(action.get("query") or "")),
                         input=_shrink(action), status="error" if payload.get("status") == "failed" else "ok")
    if builder.context is not None and not builder.context.get("limit"):
        builder.context["limit"] = context_window(builder.context.get("model"))
    return usage


# --- Gemini ------------------------------------------------------------------------------------------------------

def _gemini_feed(builder: _Builder, records: list[dict]) -> None:
    for message in records:
        mtype = message.get("type") or message.get("role")
        t = _ts(message.get("timestamp"))
        if mtype == "user":
            text = _gemini_text(message)
            builder.add({"type": "user", "text": text}, t)
        elif mtype in ("gemini", "model", "assistant"):
            for thought in message.get("thoughts") or []:
                if isinstance(thought, dict):
                    subject, body = thought.get("subject"), thought.get("description")
                    text = f"**{subject}**\n{body}" if subject and body else (subject or body or "")
                    builder.add({"type": "thinking", "text": text}, _ts(thought.get("timestamp")) or t)
            text = _gemini_text(message)
            builder.add({"type": "message", "role": "assistant", "text": text}, t)
            for call in message.get("toolCalls") or []:
                if not isinstance(call, dict):
                    continue
                args = call.get("args") if isinstance(call.get("args"), dict) else {}
                status = str(call.get("status") or "").lower()
                output = call.get("resultDisplay")
                if not isinstance(output, str):
                    output = _gemini_result(call.get("result"))
                builder.tool(call.get("id"), _ts(call.get("timestamp")) or t, name=str(call.get("name") or "tool"),
                             title=_title(str(call.get("description") or "")) or _title_for_tool("", args),
                             input=_shrink(args), output=output,
                             status="error" if status in ("error", "failed", "cancelled") else "ok")
            tokens = message.get("tokens") if isinstance(message.get("tokens"), dict) else {}
            builder.count(input=tokens.get("input"), output=tokens.get("output"))
        elif mtype == "error":
            builder.add({"type": "error", "text": _gemini_text(message)}, t)


def _gemini_result(result: Any) -> str:
    texts = []
    for part in result if isinstance(result, list) else [result]:
        if isinstance(part, dict):
            response = part.get("functionResponse", {}).get("response") if isinstance(
                part.get("functionResponse"), dict) else None
            if isinstance(response, dict):
                value = response.get("output", response.get("content", response.get("error")))
                texts.append(value if isinstance(value, str) else json.dumps(response, ensure_ascii=False))
            elif isinstance(part.get("text"), str):
                texts.append(part["text"])
        elif isinstance(part, str):
            texts.append(part)
    return "\n".join(texts)


# --- lenient (dsh / custom / ACP-shaped) ---------------------------------------------------------------------------

_ACP_STATUS = {"pending": "running", "in_progress": "running", "completed": "ok", "failed": "error"}
_USER_TYPES = ("user", "user_message", "human", "prompt", "input")
_ASSISTANT_TYPES = ("assistant", "assistant_message", "agent_message", "ai", "model", "response", "output")


def _generic_feed(builder: _Builder, records: list[dict]) -> None:
    for record in records:
        try:
            _generic_one(builder, record)
        except (TypeError, ValueError, AttributeError):
            continue


def _dsh_text(content: Any) -> str:
    """Only the ``text`` blocks: an embedded ``resource`` (the console's system prompt) is not the user's words."""
    if isinstance(content, str):
        return content
    return "".join(str(b.get("text") or "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def _dsh_v4_one(builder: _Builder, record: dict) -> bool:
    """DeepSeek Harness ``session.v4`` records (checked on a real device): ``user/message`` (only ``source.kind ==
    "user"`` is the person; runtime-context / skill-catalog are injected), ``assistant/message`` with ``reasoning`` /
    ``text`` / ``tool-call`` blocks, ``tool/result`` and ``turn/end``. Returns False for anything else."""
    rtype = record.get("type")
    data = record.get("data")
    if not isinstance(rtype, str) or "/" not in rtype or not isinstance(data, dict):
        return False
    t = _ts(record.get("time"))
    if rtype == "user/message":
        if (data.get("source") or {}).get("kind") == "user":
            text = _dsh_text(data.get("content")).strip()
            if text:
                builder.add({"type": "user", "text": text}, t)
        return True
    if rtype == "assistant/message":
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "reasoning" and str(block.get("text") or "").strip():
                builder.add({"type": "thinking", "text": block["text"]}, t)
            elif kind == "text" and str(block.get("text") or "").strip():
                builder.add({"type": "message", "role": "assistant", "text": block["text"]}, t)
            elif kind == "tool-call":
                name = str(block.get("name") or "tool")
                args = _codex_args(block.get("arguments"))
                builder.tool(block.get("id"), t, name=name, title=_title_for_tool(name, args),
                             input=_shrink(args if isinstance(args, dict) else {"input": args}))
        return True
    if rtype == "tool/result":
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        builder.tool(message.get("toolCallId") or (message.get("source") or {}).get("callId"), t,
                     output=_text_of(message.get("content")), status="error" if message.get("isError") else "ok")
        return True
    if rtype == "turn/end":
        reason = str((data.get("reason") or {}).get("kind") or "")
        if reason and reason != "completed":
            builder.add({"type": "status", "state": "stopped" if "cancel" in reason or "interrupt" in reason
                         else "failed", "reason": reason}, t)
        return True
    return rtype in ("tool/call", "session/title", "turn/start", "step/start", "step/end", "system/message",
                     "agent/inbox/spliced", "request/header", "request/context", "permission/preset", "sandbox/mode",
                     "approval/policy") or rtype.startswith("session-log-")


def _generic_one(builder: _Builder, record: dict) -> None:
    if _dsh_v4_one(builder, record):
        return
    t = _ts(record.get("timestamp") or record.get("ts") or record.get("time") or record.get("createdAt"))
    for key in ("payload", "data", "event", "record"):
        inner = record.get(key)
        if isinstance(inner, dict) and ("type" in inner or "role" in inner or "sessionUpdate" in inner):
            t = t or _ts(inner.get("timestamp"))
            record = inner
            break
    params = record.get("params") if isinstance(record.get("params"), dict) else None
    update = record.get("update") if isinstance(record.get("update"), dict) else (
        params.get("update") if params and isinstance(params.get("update"), dict) else None)
    if update is None and "sessionUpdate" in record:
        update = record
    if update is not None:
        _acp_update(builder, update, t)
        return
    message = record.get("message") if isinstance(record.get("message"), dict) else record
    role = str(message.get("role") or "").lower()
    rtype = str(record.get("type") or message.get("type") or "").lower()
    content = message.get("content", message.get("text", message.get("parts")))
    if isinstance(message.get("message"), str):
        content = message["message"]
    if rtype in ("thinking", "reasoning", "thought"):
        builder.add({"type": "thinking", "text": _text_of(content) or str(record.get("text") or "")}, t)
        return
    if rtype in ("tool_call", "tool_use", "function_call", "tool"):
        name = str(record.get("name") or record.get("tool") or "tool")
        data = _codex_args(record.get("input", record.get("arguments", record.get("args"))))
        builder.tool(record.get("id") or record.get("call_id") or record.get("toolCallId"), t, name=name,
                     title=_title(str(record.get("title") or "")) or _title_for_tool(name, data),
                     input=_shrink(data if isinstance(data, dict) else {"input": data}),
                     **({"output": _text_of(record["output"]) if not isinstance(record["output"], str) else record["output"]}
                        if "output" in record else {}))
        return
    if rtype in ("tool_result", "function_call_output", "tool_output"):
        output = record.get("output", record.get("content", record.get("result")))
        builder.tool(record.get("tool_use_id") or record.get("call_id") or record.get("id"), t,
                     output=output if isinstance(output, str) else _text_of(output),
                     status="error" if record.get("is_error") or record.get("error") else "ok")
        return
    if rtype == "error":
        builder.add({"type": "error", "text": str(record.get("message") or record.get("text") or "出错了")}, t)
        return
    if role in ("user", "human") or (not role and rtype in _USER_TYPES):
        text = _text_of(content)
        if text and not text.lstrip().startswith("<"):
            builder.add({"type": "user", "text": text}, t)
    elif role in ("assistant", "model", "ai", "agent") or (not role and rtype in _ASSISTANT_TYPES):
        for key in ("reasoning", "thinking", "reasoning_content"):
            if isinstance(message.get(key), str):
                builder.add({"type": "thinking", "text": message[key]}, t)
        builder.add({"type": "message", "role": "assistant", "text": _text_of(content)}, t)


def _acp_update(builder: _Builder, update: dict, t: float | None) -> None:
    kind = update.get("sessionUpdate")
    content = update.get("content")
    if kind == "agent_message_chunk":
        builder.add({"type": "message", "role": "assistant", "text": _text_of(content)}, t, join=True)
    elif kind == "user_message_chunk":
        builder.add({"type": "user", "text": _text_of(content)}, t, join=True)
    elif kind == "agent_thought_chunk":
        builder.add({"type": "thinking", "text": _text_of(content)}, t, join=True)
    elif kind in ("tool_call", "tool_call_update"):
        output, changes = None, []
        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "diff" and isinstance(item.get("path"), str):
                    changes.append({"type": "file_change", "path": item["path"],
                                    "kind": "add" if item.get("oldText") in (None, "") else "edit",
                                    "diff": _diff(item["path"], str(item.get("oldText") or ""),
                                                  str(item.get("newText") or ""))})
                else:
                    texts.append(_text_of(item.get("content") if isinstance(item, dict) else item))
            output = "\n".join(x for x in texts if x) or None
        raw = update.get("rawInput")
        builder.tool(update.get("toolCallId"), t, name=str(update.get("kind") or "tool") if kind == "tool_call" else None,
                     title=_title(str(update.get("title") or "")) or None,
                     input=_shrink(raw) if isinstance(raw, dict) else None, output=output,
                     status=_ACP_STATUS.get(str(update.get("status") or ""), None))
        for change in changes:
            builder.add(change, t)


# --- read --------------------------------------------------------------------------------------------------------

def read(defs: Any, home: Path, kind: str, session_id: str, *, max_events: int = 2000,
         env: Mapping[str, str] | None = None) -> list[dict]:
    """The session's transcript as §4 events (``seq`` from 1, ``t`` in epoch seconds), oldest first, ending with a
    ``status`` event (``state: idle``, ``session_id``, ``usage`` when known, ``dropped`` when older records were left
    out). Raises ``LookupError`` when the session does not exist; unreadable content gives an ``error`` event."""
    session = find(defs, Path(home), kind, session_id, env=env)
    if session is None:
        raise LookupError(f"找不到会话 {session_id}")
    return read_session(session, max_events=max_events)


def read_session(session: dict, *, max_events: int = 2000) -> list[dict]:
    """:func:`read` for a session dict from :func:`discover`."""
    kind, session_id = session["kind"], session["id"]
    builder = _Builder()
    path = Path(session["file"]) if session.get("file") else None
    if path is None:
        logs = [Path(f) for f in session.get("files") or [] if f.endswith("logs.json")]
        if kind == "gemini" and logs:
            _gemini_logs_feed(builder, logs[0], session_id)
            return builder.finish(session_id, max_events=max_events)
        builder.add({"type": "error", "text": "这个会话只有索引信息，没有找到记录文件，无法显示历史；仍然可以发消息续接。"})
        return builder.finish(session_id, max_events=max_events)
    try:
        data, dropped_bytes = _read_bytes(path)
    except LookupError:
        builder.add({"type": "error", "text": "会话记录是 zstd 压缩的，这台设备缺少解压支持（安装 Python 包 zstandard 或 zstd "
                                                 "命令后可显示），暂时只能显示会话信息；仍然可以发消息续接。"})
        return builder.finish(session_id, max_events=max_events)
    except ValueError:
        builder.add({"type": "error", "text": "会话记录文件已损坏或格式不受支持，无法显示历史；仍然可以发消息续接。"})
        return builder.finish(session_id, max_events=max_events)
    except OSError as error:
        builder.add({"type": "error", "text": f"读取会话记录失败：{error.strerror or error}"})
        return builder.finish(session_id, max_events=max_events)
    dropped = 1 if dropped_bytes else 0
    usage = None
    if path.suffix == ".json":
        parsed = _json_loads(data)
        records, _meta = _json_records(parsed)
        if kind == "gemini":
            _gemini_feed(builder, records)
        else:
            _generic_feed(builder, records)
    else:
        records = _records(data)
        if kind == "claude":
            _claude_feed(builder, records)
        elif kind == "codex":
            usage = _codex_feed(builder, records) or None
        else:
            _generic_feed(builder, records)
    if not builder.events and not records and data.strip():
        builder.add({"type": "error", "text": "无法识别这个会话文件的格式，只能显示会话信息；仍然可以发消息续接。"})
    return builder.finish(session_id, max_events=max_events, dropped=dropped, usage=usage)


def _gemini_logs_feed(builder: _Builder, path: Path, session_id: str) -> None:
    try:
        data = json.loads(path.read_bytes()) if path.stat().st_size <= JSON_MAX_BYTES else []
    except (OSError, ValueError):
        data = []
    for entry in data if isinstance(data, list) else []:
        if isinstance(entry, dict) and entry.get("sessionId") == session_id and entry.get("type", "user") == "user":
            builder.add({"type": "user", "text": str(entry.get("message") or "")}, _ts(entry.get("timestamp")))


# ============================================================================================ deleting files

def delete_files(defs: Any, home: Path, kind: str, session_id: str, *,
                 env: Mapping[str, str] | None = None) -> list[str]:
    """Delete the session's own files -- only those the discovery globs match, never a file shared with other
    sessions (Gemini's ``logs.json``). Returns the deleted paths; raises ``LookupError`` when the session is unknown,
    ``OSError`` when a file could not be removed."""
    home = Path(home)
    env = _env(env)
    session = find(defs, home, kind, session_id, env=env)
    if session is None:
        raise LookupError(f"找不到会话 {session_id}")
    globs = kind_globs(defs).get(kind, [])
    owner = required_owner(home)
    deleted = []
    for name in session.get("own_files") or []:
        path = Path(name)
        if not _matches_globs(path, globs, home, env) or not _owned(path, owner):
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        deleted.append(str(path))
        if path.parent.name == session_id:
            try:
                path.parent.rmdir()  # dsh keeps one folder per session; only when nothing else is left in it
            except OSError:
                pass
    clear_cache()
    return deleted


# ============================================================================================ SessionMeta

class SessionMeta:
    """The console's own metadata per session in ``<root>/.anx/agents/sessions.json``:
    ``{"version": 1, "sessions": {"<kind>:<id>": {alias, pinned, archived, hidden, managed, rid, updated}}}``.
    Only keys that were set are stored; writes are atomic (temporary file + ``os.replace``)."""

    FIELDS = ("alias", "pinned", "archived", "hidden", "managed")

    def __init__(self, root: Path, *, path: Path | None = None):
        self.path = Path(path) if path else Path(root) / ".anx" / "agents" / "sessions.json"
        self.lock = threading.RLock()
        self._data: dict[str, dict] | None = None
        self._stamp: tuple | None = None

    @staticmethod
    def key(kind: str, session_id: str) -> str:
        return f"{kind}:{session_id}"

    def _load(self) -> dict[str, dict]:
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        if self._data is not None and stamp == self._stamp:
            return self._data
        data: dict[str, dict] = {}
        if stamp is not None:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                sessions = raw.get("sessions") if isinstance(raw, dict) else None
                if isinstance(sessions, dict):
                    data = {str(k): dict(v) for k, v in sessions.items() if isinstance(v, dict)}
            except (OSError, ValueError):
                self._quarantine()
        self._data, self._stamp = data, stamp
        return data

    def _quarantine(self) -> None:
        """A damaged file is kept beside as ``sessions.json.bad`` and the store starts empty."""
        try:
            os.replace(self.path, self.path.with_name(self.path.name + ".bad"))
        except OSError:
            pass

    def _save(self, data: dict[str, dict]) -> None:
        if len(data) > META_MAX_ENTRIES:
            ordered = sorted(data.items(), key=lambda item: item[1].get("updated", 0), reverse=True)
            data = dict(ordered[:META_MAX_ENTRIES])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        body = json.dumps({"version": 1, "sessions": data}, ensure_ascii=False, indent=1, sort_keys=True)
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass
        self._data = data
        try:
            stat = self.path.stat()
            self._stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            self._stamp = None

    def all(self) -> dict[str, dict]:
        with self.lock:
            return {k: dict(v) for k, v in self._load().items()}

    def get(self, kind: str, session_id: str) -> dict:
        with self.lock:
            return dict(self._load().get(self.key(kind, session_id), {}))

    def set(self, kind: str, session_id: str, **patch: Any) -> dict:
        """Apply ``alias`` (text; empty clears it), ``pinned`` / ``archived`` / ``hidden`` / ``managed`` (bools; false
        clears), ``rid``, ``address``, ``first_prompt`` (kept once set). Unknown keys are ignored. Returns the stored entry. Raises ``ValueError`` for bad input."""
        if not isinstance(kind, str) or not kind or not isinstance(session_id, str) or not session_id:
            raise ValueError("缺少 kind 或 id")
        with self.lock:
            data = dict(self._load())
            key = self.key(kind, session_id)
            entry = dict(data.get(key, {}))
            for name, value in patch.items():
                if name == "alias":
                    if value is None:
                        entry.pop("alias", None)
                        continue
                    if not isinstance(value, str):
                        raise ValueError("名称必须是文字")
                    value = " ".join(value.split())
                    if len(value) > ALIAS_MAX:
                        raise ValueError(f"名称不能超过 {ALIAS_MAX} 个字")
                    if value:
                        entry["alias"] = value
                    else:
                        entry.pop("alias", None)
                elif name in ("pinned", "archived", "hidden", "managed"):
                    if value is None:
                        continue
                    if not isinstance(value, (bool, int)):
                        raise ValueError(f"{name} 必须是 true 或 false")
                    if name == "archived":
                        entry["archived"] = bool(value)  # an explicit false also un-archives what the CLI archived
                    elif value:
                        entry[name] = True
                    else:
                        entry.pop(name, None)
                elif name == "first_prompt" and isinstance(value, str):
                    # the first message typed in the console for a session it started: its title, since an ACP CLI
                    # (dsh) gets the system prompt in front of that message and titles the session from the mix
                    if value.strip() and "first_prompt" not in entry:
                        entry["first_prompt"] = " ".join(value.split())[:TITLE_MAX * 2]
                elif name in ("rid", "address") and (value is None or isinstance(value, str)):
                    # rid: the console run using it; address: its mailbox address (§10), shown again on reopen
                    if value:
                        entry[name] = value[:200]
                    else:
                        entry.pop(name, None)
            fields = {k for k in entry if k != "updated"}
            if fields:
                entry["updated"] = round(time.time(), 3)
                data[key] = entry
            else:
                data.pop(key, None)
            self._save(data)
            return {k: v for k, v in entry.items()}

    def forget(self, kind: str, session_id: str) -> None:
        with self.lock:
            data = dict(self._load())
            if data.pop(self.key(kind, session_id), None) is not None:
                self._save(data)

    def apply(self, sessions: list[dict]) -> list[dict]:
        """Fill ``alias / pinned / archived / hidden / managed`` of discovered sessions in place."""
        entries = self.all()
        for session in sessions:
            entry = entries.get(self.key(session["kind"], session["id"]))
            if not entry:
                continue
            session["alias"] = entry.get("alias") or None
            session["pinned"] = bool(entry.get("pinned"))
            if "archived" in entry:
                session["archived"] = bool(entry["archived"])
            session["hidden"] = bool(entry.get("hidden"))
            session["managed"] = bool(entry.get("managed"))
            if entry.get("first_prompt"):
                session["title"] = _title(entry["first_prompt"])
            if entry.get("address"):
                session["address"] = entry["address"]
        return sessions


# ============================================================================================ handler helpers

def public(session: dict) -> dict:
    """A session as sent to the console (internal file lists left out)."""
    return {k: v for k, v in session.items() if k not in ("files", "own_files", "project_hash", "blank")}


def meta_op(defs: Any, home: Path, meta: SessionMeta, params: Mapping[str, Any], *,
            env: Mapping[str, str] | None = None,
            busy: Callable[[str, str], bool] | None = None) -> dict:
    """``agent_session_meta``: ``{kind, id, alias?, pinned?, archived?, hidden?, delete_file?}``. Never raises.

    ``delete_file: true`` deletes the session's files (the console asks for confirmation first) and forgets its
    metadata; ``busy(kind, id)`` -- e.g. a run is using the session -- refuses it."""
    try:
        kind, session_id = params.get("kind"), params.get("id")
        if not isinstance(kind, str) or not kind or not isinstance(session_id, str) or not session_id:
            return {"error": "缺少会话的 kind 或 id", "code": "bad_request"}
        if params.get("delete_file"):
            if busy and busy(kind, session_id):
                return {"error": "这个会话正在运行，先停止再删除", "code": "busy"}
            try:
                deleted = delete_files(defs, home, kind, session_id, env=env)
            except LookupError:
                return {"error": "找不到这个会话的文件，可能已经被删除", "code": "not_found"}
            except OSError as error:
                return {"error": f"删除会话文件失败：{error.strerror or error}", "code": "io_error"}
            if not deleted:
                return {"error": "没有可以删除的会话文件", "code": "not_found"}
            meta.forget(kind, session_id)
            return {"ok": True, "deleted": len(deleted)}
        patch = {k: params[k] for k in ("alias", "pinned", "archived", "hidden") if k in params}
        if not patch:
            return {"error": "没有要修改的内容", "code": "bad_request"}
        entry = meta.set(kind, session_id, **patch)
        return {"ok": True, "kind": kind, "id": session_id, "alias": entry.get("alias"),
                "pinned": bool(entry.get("pinned")), "archived": entry.get("archived"),
                "hidden": bool(entry.get("hidden"))}
    except ValueError as error:
        return {"error": str(error), "code": "bad_request"}
    except OSError as error:
        return {"error": f"保存会话设置失败：{error.strerror or error}", "code": "io_error"}
    except Exception as error:  # noqa: BLE001 - handlers never raise
        return {"error": f"处理会话设置失败：{error}", "code": "internal"}
