"""Persistent filesystem/process watchers with GPT Companion notifications."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .exec_runtime import UnifiedExecManager
from .settings import Config
from .policy import Policy, PolicyError

WATCH_KINDS = {"session", "log", "file", "glob"}
DEFAULT_TRAINING_ERROR_PATTERNS = [
    r"CUDA out of memory",
    r"OutOfMemoryError",
    r"Traceback \(most recent call last\)",
    r"\bNaN\b",
    r"\bnan\b",
]


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


def _loads(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return default


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)).casefold()


class WatchManager:
    def __init__(self, config: Config, policy: Policy, exec_manager: UnifiedExecManager) -> None:
        self.config = config
        self.policy = policy
        self.exec_manager = exec_manager
        self.db_path = config.watch_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._init_db()
        self._thread = threading.Thread(target=self._worker, name="device-watchers", daemon=True)
        self._thread.start()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._db_lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS watches (
                    id TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL,
                    target TEXT NOT NULL,
                    workdir TEXT NOT NULL DEFAULT '',
                    patterns_json TEXT NOT NULL DEFAULT '[]',
                    glob_pattern TEXT NOT NULL DEFAULT '',
                    companion_project_id TEXT NOT NULL DEFAULT '',
                    poll_interval_s REAL NOT NULL DEFAULT 1.0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    state_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_watches_status ON watches(status);
                CREATE INDEX IF NOT EXISTS idx_watches_group ON watches(group_id);
                CREATE TABLE IF NOT EXISTS watch_events (
                    id TEXT PRIMARY KEY,
                    watch_id TEXT NOT NULL,
                    group_id TEXT NOT NULL DEFAULT '',
                    companion_project_id TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    path TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    delivery_error TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_watch_events_created ON watch_events(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_watch_events_delivery ON watch_events(delivered, last_attempt_at);
                """
            )

    def _watch_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "group_id": row["group_id"], "kind": row["kind"],
            "label": row["label"], "status": row["status"], "target": row["target"],
            "workdir": row["workdir"], "patterns": _loads(row["patterns_json"], []),
            "glob_pattern": row["glob_pattern"], "companion_project_id": row["companion_project_id"],
            "poll_interval_s": row["poll_interval_s"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "state": _loads(row["state_json"], {}),
        }

    def _event_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "watch_id": row["watch_id"], "group_id": row["group_id"],
            "companion_project_id": row["companion_project_id"], "event_type": row["event_type"],
            "summary": row["summary"], "path": row["path"], "created_at": row["created_at"],
            "delivered": bool(row["delivered"]), "delivery_error": row["delivery_error"],
            "attempts": row["attempts"], "last_attempt_at": row["last_attempt_at"],
        }

    def _http_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        base = self.config.companion_url.rstrip("/")
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{base}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.config.watch_notify_timeout_s) as response:
            raw = response.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw or "{}")
        if not isinstance(parsed, dict):
            raise RuntimeError(f"unexpected GPT Companion response: {raw[:200]}")
        return parsed

    def companion_routes(self) -> dict[str, Any]:
        current_error = ""
        current: dict[str, Any] = {}
        try:
            current = self._http_json("GET", "/current-route")
        except Exception as exc:
            current_error = str(exc)
        try:
            routes = self._http_json("GET", "/routes").get("routes") or []
        except Exception as exc:
            if current_error:
                raise RuntimeError(
                    f"GPT Companion is unavailable at {self.config.companion_url}: {exc}"
                ) from exc
            routes = []
        return {
            "status": "ok",
            "companion_url": self.config.companion_url,
            "current_url": current.get("currentUrl"),
            "current_bookmark": current.get("bookmark"),
            "routes": routes,
            "current_route_error": current_error,
        }

    # -- asking the person ----------------------------------------------------
    #
    # The reverse direction of an event: an event nudges the assistant, a
    # question blocks the assistant until a human replies. It deliberately does
    # not go through the conversation bookmarks -- there is nothing to route,
    # the question is for whoever is at this machine.

    def ask_question(
        self,
        question: str,
        *,
        title: str | None = None,
        kind: str = "text",
        options: list[dict[str, Any]] | None = None,
        allow_custom: bool = False,
        source: str = "workspace-mcp",
    ) -> dict[str, Any]:
        """Post a question to GPT Companion; returns it with its id."""
        payload: dict[str, Any] = {
            "question": question,
            "kind": kind,
            "options": options or [],
            "allowCustom": allow_custom,
            "source": source,
        }
        if title:
            payload["title"] = title
        try:
            answer = self._http_json("POST", "/ask", payload)
        except Exception as exc:
            raise RuntimeError(
                f"GPT Companion is unavailable at {self.config.companion_url}: {exc}. "
                "Start chatx before asking the user anything."
            ) from exc
        if answer.get("status") != "ok":
            raise RuntimeError(str(answer.get("error") or answer))
        return answer

    def question_state(self, question_id: str) -> dict[str, Any]:
        """Current state of one question: pending, answered or cancelled."""
        quoted = urllib.parse.quote(question_id, safe="")
        try:
            answer = self._http_json("GET", f"/ask?id={quoted}")
        except Exception as exc:
            raise RuntimeError(
                f"GPT Companion is unavailable at {self.config.companion_url}: {exc}"
            ) from exc
        if answer.get("status") != "ok":
            raise RuntimeError(str(answer.get("error") or answer))
        return answer

    def resolve_companion_project(self, requested: str | None, workdir: str | None = None) -> str:
        if requested and requested.strip():
            return requested.strip()
        payload = self.companion_routes()
        current = payload.get("current_bookmark")
        if isinstance(current, dict) and current.get("id"):
            return str(current["id"])
        routes = [r for r in payload.get("routes", []) if isinstance(r, dict) and r.get("id")]
        if workdir:
            wanted = _norm_path(workdir)
            matched = [
                route for route in routes
                if route.get("workdir") and _norm_path(str(route["workdir"])) == wanted
            ]
            if len(matched) == 1:
                return str(matched[0]["id"])
        if len(routes) == 1:
            return str(routes[0]["id"])
        if not routes:
            raise RuntimeError(
                "GPT Companion has no saved conversation bookmark. Open the desired conversation "
                "in GPT Companion and click the bookmark star first."
            )
        choices = ", ".join(f"{r.get('name', r['id'])} ({r['id']})" for r in routes[:8])
        raise RuntimeError(
            "Could not choose a GPT Companion route automatically. Keep the desired bookmarked "
            f"conversation open, or pass companion_project_id. Available: {choices}"
        )

    def _resolve_target_path(self, target: str, workdir: str | None) -> Path:
        cwd = self.policy.resolve_cwd(workdir)
        raw = Path(target).expanduser()
        path = (raw if raw.is_absolute() else cwd / raw).resolve()
        roots = self.config.resolved_roots()
        if roots:
            allowed = False
            for root in roots:
                try:
                    path.relative_to(root.resolve())
                    allowed = True
                    break
                except ValueError:
                    continue
            if not allowed:
                raise PolicyError(f"watch target {path} is outside allowed roots")
        return path

    def create_watch(
        self, *, kind: str, target: str | None = None, session_id: int | None = None,
        workdir: str | None = None, patterns: list[str] | None = None,
        glob_pattern: str | None = None, label: str | None = None,
        companion_project_id: str | None = None, poll_interval_s: float | None = None,
        group_id: str | None = None, from_start: bool = False,
    ) -> dict[str, Any]:
        kind = kind.strip().lower()
        if kind not in WATCH_KINDS:
            raise ValueError(f"kind must be one of {sorted(WATCH_KINDS)}")
        interval = float(poll_interval_s or self.config.watch_poll_interval_s)
        if not 0.25 <= interval <= 3600:
            raise ValueError("poll_interval_s must be between 0.25 and 3600")
        clean_patterns = [str(value) for value in (patterns or []) if str(value).strip()]
        for pattern in clean_patterns:
            re.compile(pattern, re.IGNORECASE)

        state: dict[str, Any] = {"last_poll": 0.0}
        resolved_workdir = ""
        target_text = ""
        if kind == "session":
            if session_id is None:
                raise ValueError("session watch requires session_id")
            info = self.exec_manager.session_status(int(session_id))
            if info is None:
                raise KeyError(f"unknown exec session_id {session_id}")
            if info.get("completed"):
                raise ValueError(
                    f"exec session {session_id} already completed (exit_code={info.get('exit_code')})"
                )
            target_text = str(int(session_id))
            resolved_workdir = str(info.get("workdir") or workdir or "")
            label = label or f"exec session {session_id}"
        else:
            if not target:
                raise ValueError(f"{kind} watch requires target")
            path = self._resolve_target_path(target, workdir)
            target_text = str(path)
            resolved_workdir = str(self.policy.resolve_cwd(workdir))
            label = label or f"{kind}: {path.name or path}"
            if kind == "log":
                size = path.stat().st_size if path.is_file() else 0
                state.update({"offset": 0 if from_start else size, "matched_patterns": []})
            elif kind == "file":
                if path.exists():
                    stat = path.stat()
                    state.update({"exists": True, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size})
                else:
                    state.update({"exists": False, "mtime_ns": 0, "size": 0})
            elif kind == "glob":
                if not glob_pattern:
                    raise ValueError("glob watch requires glob_pattern")
                if path.exists() and not path.is_dir():
                    raise ValueError("glob watch target must be a directory")
                known = sorted(
                    str(item.resolve()) for item in path.glob(glob_pattern) if item.is_file()
                ) if path.exists() else []
                state["known"] = known

        project_id = self.resolve_companion_project(companion_project_id, resolved_workdir or workdir)
        watch_id = _new_id("watch")
        now = _now()
        with self._db_lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO watches(
                    id, group_id, kind, label, status, target, workdir, patterns_json,
                    glob_pattern, companion_project_id, poll_interval_s, created_at,
                    updated_at, state_json
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    watch_id, group_id or "", kind, label, target_text, resolved_workdir,
                    json.dumps(clean_patterns), glob_pattern or "", project_id, interval,
                    now, now, json.dumps(state),
                ),
            )
        self._wake.set()
        return self.get_watch(watch_id)

    def get_watch(self, watch_id: str) -> dict[str, Any]:
        with self._db_lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown watch_id {watch_id}")
        return self._watch_dict(row)

    def list_watches(
        self, *, include_inactive: bool = False, group_id: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM watches"
        clauses: list[str] = []
        args: list[Any] = []
        if not include_inactive:
            clauses.append("status = 'active'")
        if group_id:
            clauses.append("group_id = ?")
            args.append(group_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC"
        with self._db_lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._watch_dict(row) for row in rows]

    def cancel_watch(self, watch_id: str) -> dict[str, Any]:
        now = _now()
        with self._db_lock, self._connect() as conn:
            result = conn.execute(
                "UPDATE watches SET status='cancelled', updated_at=? WHERE id=?",
                (now, watch_id),
            )
            if result.rowcount == 0:
                raise KeyError(f"unknown watch_id {watch_id}")
        self._wake.set()
        return self.get_watch(watch_id)

    def cancel_group(self, group_id: str) -> list[dict[str, Any]]:
        now = _now()
        with self._db_lock, self._connect() as conn:
            conn.execute(
                "UPDATE watches SET status='cancelled', updated_at=? WHERE group_id=?",
                (now, group_id),
            )
        self._wake.set()
        return self.list_watches(include_inactive=True, group_id=group_id)

    def events(
        self, *, watch_id: str | None = None, group_id: str | None = None, limit: int = 50,
    ) -> list[dict[str, Any]]:
        limit = min(max(int(limit), 1), 500)
        sql = "SELECT * FROM watch_events"
        clauses: list[str] = []
        args: list[Any] = []
        if watch_id:
            clauses.append("watch_id = ?")
            args.append(watch_id)
        if group_id:
            clauses.append("group_id = ?")
            args.append(group_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._db_lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._event_dict(row) for row in rows]

    def event(self, event_id: str) -> dict[str, Any]:
        with self._db_lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM watch_events WHERE id = ?", (event_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown event_id {event_id}")
            watch_row = conn.execute("SELECT * FROM watches WHERE id = ?", (row["watch_id"],)).fetchone()
        return {
            "event": self._event_dict(row),
            "watch": self._watch_dict(watch_row) if watch_row is not None else None,
        }

    def create_training_watch(
        self, *, workdir: str, session_id: int | None = None,
        log_path: str | None = None, checkpoint_glob: str | None = None,
        error_patterns: list[str] | None = None, label: str = "model training",
        companion_project_id: str | None = None, poll_interval_s: float = 2.0,
    ) -> dict[str, Any]:
        cwd = str(self.policy.resolve_cwd(workdir))
        project_id = self.resolve_companion_project(companion_project_id, cwd)
        group_id = _new_id("training")
        created: list[dict[str, Any]] = []
        if session_id is not None:
            created.append(self.create_watch(
                kind="session", session_id=session_id, workdir=cwd,
                label=f"{label} · process", companion_project_id=project_id,
                group_id=group_id, poll_interval_s=poll_interval_s,
            ))
        if log_path:
            created.append(self.create_watch(
                kind="log", target=log_path, workdir=cwd,
                patterns=error_patterns or DEFAULT_TRAINING_ERROR_PATTERNS,
                label=f"{label} · log", companion_project_id=project_id,
                group_id=group_id, poll_interval_s=poll_interval_s, from_start=False,
            ))
        if checkpoint_glob:
            created.append(self.create_watch(
                kind="glob", target=cwd, workdir=cwd, glob_pattern=checkpoint_glob,
                label=f"{label} · checkpoint", companion_project_id=project_id,
                group_id=group_id, poll_interval_s=poll_interval_s,
            ))
        if not created:
            raise ValueError(
                "watch_training needs at least one of session_id, log_path, or checkpoint_glob"
            )
        return {
            "status": "ok", "group_id": group_id,
            "companion_project_id": project_id, "workdir": cwd, "watches": created,
        }

    def _save_state(
        self, watch_id: str, state: dict[str, Any], status: str | None = None,
    ) -> None:
        now = _now()
        with self._db_lock, self._connect() as conn:
            if status:
                conn.execute(
                    "UPDATE watches SET state_json=?, status=?, updated_at=? WHERE id=?",
                    (json.dumps(state), status, now, watch_id),
                )
            else:
                conn.execute(
                    "UPDATE watches SET state_json=?, updated_at=? WHERE id=?",
                    (json.dumps(state), now, watch_id),
                )

    def _emit(self, watch: dict[str, Any], event_type: str, summary: str, path: str = "") -> str:
        event_id = _new_id("evt")
        now = _now()
        with self._db_lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO watch_events(
                    id, watch_id, group_id, companion_project_id, event_type,
                    summary, path, created_at, delivered, delivery_error,
                    attempts, last_attempt_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, '', 0, ?)""",
                (
                    event_id, watch["id"], watch.get("group_id", ""),
                    watch["companion_project_id"], event_type, summary[:1000], path, now,
                    # the delivery below is the first attempt: stamping it now keeps the background
                    # _retry_pending (rows idle for 10 s) from POSTing the same event at the same time
                    now,
                ),
            )
        self._deliver_event_id(event_id)
        return event_id

    def emit_mail_event(
        self, *, project_id: str | None, workdir: str | None, summary: str,
        event_type: str = "mail_received",
    ) -> dict[str, Any]:
        """The manager's companion wake adapter (agent-nexus design §8.7).

        Same path as a watcher event: a ``watch_events`` row (``watch_id='mail'``)
        delivered to chatx ``POST /event``; GPT then calls ``watch_event(evt)``
        and reads the summary, which is the wake prompt. An event chatx did not
        take is not retried here: the gateway retries the wake with backoff and
        would otherwise deliver it twice.
        """
        requested = None if project_id in (None, "", "auto") else str(project_id)
        try:
            project = self.resolve_companion_project(requested, workdir or None)
        except Exception as exc:
            return {"status": "ok", "delivered": False, "event_id": None, "error": str(exc)[:500]}
        watch = {"id": "mail", "group_id": "", "companion_project_id": project}
        event_id = self._emit(watch, event_type, summary, workdir or "")
        with self._db_lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM watch_events WHERE id=?", (event_id,)).fetchone()
            if row is not None and not row["delivered"]:
                conn.execute("UPDATE watch_events SET attempts=100 WHERE id=?", (event_id,))
        delivered = bool(row is not None and row["delivered"])
        return {"status": "ok", "delivered": delivered, "event_id": event_id, "project_id": project,
                "error": "" if delivered or row is None else row["delivery_error"]}

    def _deliver_event_id(self, event_id: str) -> None:
        with self._db_lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM watch_events WHERE id=?", (event_id,)).fetchone()
        if row is None or row["delivered"]:
            return
        payload = {
            "id": row["id"],
            "projectId": row["companion_project_id"],
            "eventType": row["event_type"],
            "summary": row["summary"],
        }
        delivered = 0
        error = ""
        try:
            response = self._http_json("POST", "/event", payload)
            if response.get("status") != "ok":
                raise RuntimeError(str(response))
            delivered = 1
        except Exception as exc:
            error = str(exc)[:1000]
        now = _now()
        with self._db_lock, self._connect() as conn:
            conn.execute(
                """UPDATE watch_events
                SET delivered=?, delivery_error=?, attempts=attempts+1, last_attempt_at=?
                WHERE id=?""",
                (delivered, error, now, event_id),
            )

    def _retry_pending(self) -> None:
        cutoff = _now() - 10.0
        with self._db_lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT id FROM watch_events
                WHERE delivered=0 AND attempts < 100 AND last_attempt_at <= ?
                ORDER BY created_at ASC LIMIT 20""",
                (cutoff,),
            ).fetchall()
        for row in rows:
            self._deliver_event_id(row["id"])

    def _poll_session(self, watch: dict[str, Any]) -> None:
        state = dict(watch["state"])
        if state.get("notified_exit"):
            return
        info = self.exec_manager.session_status(int(watch["target"]))
        if info is None:
            self._emit(
                watch, "watch_stale",
                f"{watch['label']}: exec session {watch['target']} is no longer known after restart",
                watch.get("workdir", ""),
            )
            state["notified_exit"] = True
            self._save_state(watch["id"], state, status="stale")
            return
        if not info.get("completed"):
            return
        code = info.get("exit_code")
        event_type = "process_completed" if code == 0 else "process_failed"
        summary = f"{watch['label']}: process exited with code {code}"
        self._emit(watch, event_type, summary, str(info.get("workdir") or watch.get("workdir", "")))
        state["notified_exit"] = True
        state["exit_code"] = code
        self._save_state(watch["id"], state, status="completed")

    def _poll_log(self, watch: dict[str, Any]) -> None:
        path = Path(watch["target"])
        state = dict(watch["state"])
        if not path.is_file():
            return
        size = path.stat().st_size
        offset = int(state.get("offset", 0))
        if size < offset:
            offset = 0
        if size == offset:
            return
        read_to = min(size, offset + 2_000_000)
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(read_to - offset)
        text = data.decode("utf-8", errors="replace")
        state["offset"] = read_to
        matched = set(state.get("matched_patterns", []))
        for pattern in watch["patterns"]:
            if pattern in matched:
                continue
            hit = re.search(pattern, text, re.IGNORECASE)
            if not hit:
                continue
            matched.add(pattern)
            line = text[hit.start():].splitlines()[0][:240]
            self._emit(
                watch, "log_match",
                f"{watch['label']}: matched {pattern!r} ({line})", str(path),
            )
        state["matched_patterns"] = sorted(matched)
        self._save_state(watch["id"], state)

    def _poll_file(self, watch: dict[str, Any]) -> None:
        path = Path(watch["target"])
        state = dict(watch["state"])
        existed = bool(state.get("exists", False))
        if not path.exists():
            if existed:
                self._emit(watch, "file_deleted", f"{watch['label']}: file disappeared", str(path))
                state.update({"exists": False, "mtime_ns": 0, "size": 0})
                self._save_state(watch["id"], state)
            return
        stat = path.stat()
        if not existed:
            self._emit(watch, "file_created", f"{watch['label']}: file created", str(path))
        elif stat.st_mtime_ns != int(state.get("mtime_ns", 0)) or stat.st_size != int(state.get("size", 0)):
            self._emit(watch, "file_changed", f"{watch['label']}: file changed", str(path))
        state.update({"exists": True, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size})
        self._save_state(watch["id"], state)

    def _poll_glob(self, watch: dict[str, Any]) -> None:
        root = Path(watch["target"])
        state = dict(watch["state"])
        if not root.is_dir():
            return
        current = {str(item.resolve()) for item in root.glob(watch["glob_pattern"]) if item.is_file()}
        known = set(state.get("known", []))
        for path in sorted(current - known)[:50]:
            self._emit(
                watch, "file_created",
                f"{watch['label']}: new file {Path(path).name}", path,
            )
        state["known"] = sorted(current)
        self._save_state(watch["id"], state)

    def _poll_watch(self, watch: dict[str, Any]) -> None:
        now = _now()
        state = dict(watch["state"])
        last_poll = float(state.get("last_poll", 0.0))
        if now - last_poll < float(watch["poll_interval_s"]):
            return
        state["last_poll"] = now
        self._save_state(watch["id"], state)
        watch["state"] = state
        if watch["kind"] == "session":
            self._poll_session(watch)
        elif watch["kind"] == "log":
            self._poll_log(watch)
        elif watch["kind"] == "file":
            self._poll_file(watch)
        elif watch["kind"] == "glob":
            self._poll_glob(watch)

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                for watch in self.list_watches(include_inactive=False):
                    try:
                        self._poll_watch(watch)
                    except Exception as exc:
                        state = dict(watch.get("state") or {})
                        if _now() - float(state.get("last_error_at", 0.0)) >= 60:
                            state["last_error_at"] = _now()
                            state["last_error"] = str(exc)[:1000]
                            self._save_state(watch["id"], state)
                self._retry_pending()
            except Exception:
                pass
            self._wake.wait(timeout=0.5)
            self._wake.clear()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
