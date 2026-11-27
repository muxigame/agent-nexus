"""API sessions on disk (docs/agent-chat.md §16.4): ``<root>/.anx/agents/api-sessions/<id>.jsonl``.

One JSON object per line:

* ``{"type": "meta", "v": 1, "id", "kind", "provider", "model", "cwd", "created"}`` -- the first line;
* ``{"type": "event", "event": {...}}`` -- a finished unified event (§4) for the history view (no deltas);
* ``{"type": "msg", "msg": {...}}`` -- the neutral conversation message (:mod:`.protocols`) the next turn resends.

Discovery (:meth:`SessionStore.discover`) gives the same session dicts as ``sessions.discover`` (kind
``api:<provider>/<model>``), so the hub lists them with the CLI sessions; :meth:`SessionStore.read_events` gives
the same transcript shape as ``sessions.read`` (``seq`` from 1, a last ``usage`` event, a final ``status idle``).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .. import sessions as cli_sessions

SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
READ_LIMIT = 64 * 1024 * 1024
TITLE_MAX = 80
TEXT_MAX = 100_000


def new_id() -> str:
    return uuid.uuid4().hex


def _clip(text: Any, limit: int) -> str:
    text = text if isinstance(text, str) else ""
    return text if len(text) <= limit else text[:limit] + "…"


class SessionStore:
    def __init__(self, root: Path, *, folder: Path | None = None):
        self.dir = Path(folder) if folder else Path(root) / ".anx" / "agents" / "api-sessions"
        self.lock = threading.RLock()
        self._cache: dict[str, tuple[tuple, dict]] = {}

    # -- files
    def path(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise LookupError("会话 ID 格式不对")
        return self.dir / f"{session_id}.jsonl"

    def exists(self, session_id: str) -> bool:
        try:
            return self.path(session_id).is_file()
        except LookupError:
            return False

    def append(self, session_id: str, records: Iterable[dict], *, meta: dict | None = None) -> None:
        """Add ``records``; a new file starts with ``meta``."""
        path = self.path(session_id)
        lines = []
        with self.lock:
            if not path.exists():
                self.dir.mkdir(parents=True, exist_ok=True)
                with contextlib.suppress(OSError):
                    os.chmod(self.dir, 0o700)
                lines.append({"type": "meta", "v": 1, "id": session_id, "created": round(time.time(), 3), **(meta or {})})
            lines += [record for record in records if isinstance(record, dict)]
            if not lines:
                return
            text = "".join(json.dumps(line, ensure_ascii=False, default=str) + "\n" for line in lines)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(text)

    def _records(self, path: Path) -> list[dict]:
        out: list[dict] = []
        with path.open("rb") as handle:
            data = handle.read(READ_LIMIT)
        for raw in data.splitlines():
            try:
                record = json.loads(raw)
            except ValueError:
                continue  # a torn last line (the process died mid-write)
            if isinstance(record, dict):
                out.append(record)
        return out

    def load(self, session_id: str) -> tuple[dict, list[dict]]:
        """``(meta, messages)`` for resuming; ``LookupError`` when there is no such session."""
        path = self.path(session_id)
        if not path.is_file():
            raise LookupError(f"找不到会话 {session_id}")
        records = self._records(path)
        meta = next((r for r in records if r.get("type") == "meta"), {})
        messages = [r["msg"] for r in records if r.get("type") == "msg" and isinstance(r.get("msg"), dict)]
        return meta, repair(messages)

    def delete(self, session_id: str) -> bool:
        path = self.path(session_id)
        with self.lock:
            try:
                path.unlink()
            except FileNotFoundError:
                return False
            self._cache.pop(session_id, None)
        return True

    # -- discovery
    def _summary(self, path: Path, stat: os.stat_result) -> dict | None:
        key = path.stem
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = self._cache.get(key)
        if cached and cached[0] == stamp:
            return dict(cached[1])
        try:
            records = self._records(path)
        except OSError:
            return None
        meta = next((r for r in records if r.get("type") == "meta"), None)
        if not meta or not isinstance(meta.get("kind"), str):
            return None
        events = [r["event"] for r in records if r.get("type") == "event" and isinstance(r.get("event"), dict)]
        users = [e for e in events if e.get("type") == "user" and str(e.get("text") or "").strip()]
        title = " ".join(str(users[0]["text"]).split())[:TITLE_MAX] if users else ""
        stamps = [e.get("t") for e in events if isinstance(e.get("t"), (int, float))]
        summary = {
            "kind": meta["kind"], "id": str(meta.get("id") or key), "title": title, "cwd": meta.get("cwd"),
            "updated_at": round(max([stat.st_mtime, *stamps]), 3), "created_at": meta.get("created"),
            "messages": sum(1 for e in events if e.get("type") in ("user", "message")), "size": stat.st_size,
            "source": "disk", "managed": True, "pinned": False, "archived": False, "alias": None, "hidden": False,
            "blank": not users, "label": meta.get("label") or meta["kind"], "provider": meta.get("provider"),
            "model": meta.get("model"), "readable": True, "file": str(path), "files": [str(path)],
            "own_files": [str(path)],
        }
        self._cache[key] = (stamp, summary)
        return dict(summary)

    def discover(self, *, kind: str | None = None, meta: cli_sessions.SessionMeta | None = None,
                 include_hidden: bool = False, include_blank: bool = False, query: str | None = None,
                 limit: int = 200, labels: dict[str, str] | None = None) -> list[dict]:
        """The API sessions (``kind``: one ``api:…`` kind, ``"api"`` or None = all), newest first, pinned first."""
        try:
            entries = [(p, p.stat()) for p in self.dir.glob("*.jsonl") if p.is_file() and not p.is_symlink()]
        except OSError:
            return []
        entries.sort(key=lambda item: item[1].st_mtime, reverse=True)
        out: list[dict] = []
        for path, stat in entries:
            summary = self._summary(path, stat)
            if summary is None:
                continue
            if kind and kind != "api" and summary["kind"] != kind:
                continue
            if labels and summary["kind"] in labels:
                summary["label"] = labels[summary["kind"]]
            out.append(summary)
        if meta is not None:
            meta.apply(out)
        if not include_hidden:
            out = [s for s in out if not s["hidden"]]
        if not include_blank:
            out = [s for s in out if not s["blank"] or s["pinned"] or s["alias"]]
        needle = (query or "").strip().casefold()
        if needle:
            out = [s for s in out if any(needle in str(s.get(k) or "").casefold()
                                         for k in ("alias", "title", "cwd", "id", "model"))]
        limit = max(1, min(int(limit or 200), 5000))
        pinned = [s for s in out if s["pinned"]]
        rest = [s for s in out if not s["pinned"]]
        return pinned + rest[:limit] + [s for s in rest[limit:] if s["alias"]]

    def find(self, session_id: str) -> dict | None:
        try:
            path = self.path(session_id)
            return self._summary(path, path.stat())
        except (LookupError, OSError):
            return None

    # -- history
    def read_events(self, session_id: str, *, max_events: int = 2000) -> list[dict]:
        """The transcript as §4 events, ending with ``status idle``. ``LookupError`` when missing."""
        path = self.path(session_id)
        if not path.is_file():
            raise LookupError(f"找不到会话 {session_id}")
        records = self._records(path)
        events: list[dict] = []
        usage: dict | None = None
        totals = {"input": 0, "output": 0}
        for record in records:
            event = record.get("event") if record.get("type") == "event" else None
            if not isinstance(event, dict) or not event.get("type"):
                continue
            event = {k: v for k, v in event.items() if k not in ("seq", "delta")}
            if event["type"] == "usage":
                usage = event
                continue
            if event["type"] == "status":
                turn = event.get("usage") if isinstance(event.get("usage"), dict) else {}
                for name in totals:
                    totals[name] += int(turn.get(name) or 0)
                continue
            if event["type"] in ("user", "message", "thinking"):
                event["text"] = _clip(event.get("text"), TEXT_MAX)
            events.append(event)
        max_events = max(2, int(max_events or 2000))
        dropped = max(0, len(events) - (max_events - 2))
        if dropped:
            events = events[dropped:]
        if usage is not None:
            events.append(usage)
        status: dict[str, Any] = {"type": "status", "state": "idle", "session_id": session_id}
        if totals["input"] or totals["output"]:
            status["usage"] = {**totals, "cost_usd": 0}
        if dropped:
            status["dropped"] = dropped
        stamps = [e["t"] for e in events if isinstance(e.get("t"), (int, float))]
        if stamps:
            status["t"] = max(stamps)
        events.append(status)
        for seq, event in enumerate(events, 1):
            event["seq"] = seq
        return events


def repair(messages: list[dict]) -> list[dict]:
    """A history the providers accept: every assistant tool call answered (a run stopped between the call and its
    result gets a "stopped" result), no tool results without their call."""
    out: list[dict] = []
    pending: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        if role == "tool":
            ids = {c["id"] for c in pending}
            results = [r for r in msg.get("results") or [] if isinstance(r, dict) and r.get("id") in ids]
            answered = {r["id"] for r in results}
            results += [{"id": c["id"], "name": c["name"], "output": "（已停止，没有执行）", "error": True}
                        for c in pending if c["id"] not in answered]
            pending = []
            if results:
                out.append({"role": "tool", "results": results})
            continue
        if pending:
            out.append({"role": "tool", "results": [{"id": c["id"], "name": c["name"], "output": "（已停止，没有执行）",
                                                     "error": True} for c in pending]})
            pending = []
        if role == "assistant":
            pending = [c for c in msg.get("tool_calls") or [] if isinstance(c, dict) and c.get("id")]
        if role in ("user", "assistant"):
            out.append(msg)
    if pending:
        out.append({"role": "tool", "results": [{"id": c["id"], "name": c["name"], "output": "（已停止，没有执行）",
                                                 "error": True} for c in pending]})
    return out


__all__ = ["SessionStore", "new_id", "repair"]
