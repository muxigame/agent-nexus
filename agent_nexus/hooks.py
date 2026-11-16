"""``anx hook claude|codex``: what a Claude Code hook or a Codex hook / ``notify`` reports (design §8.3).

The payloads (verified against the clients, see docs/design.md §8.3):

* Claude Code command hooks get one JSON object on stdin: ``session_id``,
  ``transcript_path``, ``cwd``, ``hook_event_name`` (``SessionStart`` adds
  ``source``; ``UserPromptSubmit`` adds ``prompt``; both carry
  ``session_title`` when the session has a custom title).
* Codex ``notify`` passes its JSON as the last argument: ``type``
  (``agent-turn-complete``), ``thread-id``, ``turn-id``, ``cwd``,
  ``input-messages``, ``last-assistant-message``. Codex hooks
  (``~/.codex/hooks.json``) use the Claude-compatible stdin form.

The title: the client's own session title when one can be read (Claude's
``session_title``, else the last ``custom-title`` / ``ai-title`` /
``agent-name`` record of the transcript; Codex's ``session_index.jsonl``
``thread_name``), else the first line of the user's prompt, marked
``title_source: "prompt"`` so the gateway only uses it to fill an empty title.

Everything here is best effort and bounded: reads are capped, nothing is
printed (Claude injects a hook's stdout into the conversation), and every
error yields "no title".
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

#: Files up to this size are read whole; larger ones only at the head and the tail.
WHOLE_FILE_BYTES = 4 * 1024 * 1024
HEAD_TAIL_BYTES = 1024 * 1024
MAX_HOOK_TITLE_CHARS = 200
ACTIVE_EVENTS = ("prompt", "UserPromptSubmit", "SessionStart", "turn")


def claude_home() -> Path:
    """``$CLAUDE_CONFIG_DIR``, else ``~/.claude`` (``~`` is ``USERPROFILE`` on Windows, ``HOME`` elsewhere)."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured) if configured else Path(os.path.expanduser("~")) / ".claude"


def codex_home() -> Path:
    """``$CODEX_HOME``, else ``~/.codex``."""
    configured = os.environ.get("CODEX_HOME")
    return Path(configured) if configured else Path(os.path.expanduser("~")) / ".codex"


def first_line(text: Any) -> str | None:
    """The first non-empty line of a prompt, trimmed; ``None`` for markup-only prompts (slash commands)."""
    if not isinstance(text, str):
        return None
    for line in text.splitlines():
        line = line.strip()
        if line:
            if line.startswith(("<", "/")):
                return None  # /model, <command-name>…, injected context: not something to name a session by
            return line[:MAX_HOOK_TITLE_CHARS]
    return None


def _records(path: Path) -> Iterator[dict]:
    """The JSON objects of a ``.jsonl`` file, whole if small, else its first and last megabyte."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size <= WHOLE_FILE_BYTES:
                chunks = [handle.read()]
            else:
                head = handle.read(HEAD_TAIL_BYTES)
                handle.seek(size - HEAD_TAIL_BYTES)
                chunks = [head, handle.read()]
    except OSError:
        return
    for chunk in chunks:
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue  # a line cut at a chunk boundary
            if isinstance(record, dict):
                yield record


def _text(value: Any) -> str | None:
    return value.strip()[:MAX_HOOK_TITLE_CHARS] if isinstance(value, str) and value.strip() else None


def claude_title(data: dict) -> tuple[str | None, str | None]:
    title = _text(data.get("session_title"))
    if title:
        return title, "session"
    path = data.get("transcript_path")
    if isinstance(path, str) and path.endswith(".jsonl"):
        found: dict[str, str] = {}
        for record in _records(Path(path)):
            kind = record.get("type")
            field = {"custom-title": "customTitle", "ai-title": "aiTitle", "agent-name": "agentName"}.get(kind)
            value = _text(record.get(field)) if field else None
            if value:
                found[kind] = value  # the last one wins: titles are re-appended when they change
        for kind in ("custom-title", "ai-title", "agent-name"):
            if kind in found:
                return found[kind], "session"
    prompt = first_line(data.get("prompt"))
    return (prompt, "prompt") if prompt else (None, None)


def codex_title(data: dict, session_id: str | None) -> tuple[str | None, str | None]:
    if session_id:
        name = None
        for record in _records(codex_home() / "session_index.jsonl"):
            if record.get("id") == session_id and _text(record.get("thread_name")):
                name = _text(record["thread_name"])  # append-only: the last line is the current name
        if name:
            return name, "session"
    messages = data.get("input-messages")
    if isinstance(messages, list):
        for message in messages:
            prompt = first_line(message)
            if prompt:
                return prompt, "prompt"
    prompt = first_line(data.get("prompt"))
    return (prompt, "prompt") if prompt else (None, None)


def hook_payload(kind: str, raw: str | None, event: str | None = None) -> dict | None:
    """The body for the local ``/api/agents/presence``; ``None`` when the input is not a JSON object."""
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    session_id = data.get("session_id") or data.get("thread-id") or data.get("thread_id")
    session_id = session_id if isinstance(session_id, str) else None
    event = event or data.get("hook_event_name") or data.get("type") or ""
    if kind == "codex" and event in ("agent-turn-complete", ""):
        event = "turn"
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) and data.get("cwd") else os.getcwd()
    payload = {"kind": kind, "cwd": cwd, "event": str(event)[:64], "session_id": session_id}
    try:
        title, source = claude_title(data) if kind == "claude" else codex_title(data, session_id)
    except Exception:  # a title is never worth failing the hook
        title, source = None, None
    if title:
        payload["title"] = title
        payload["title_source"] = source
    return payload
