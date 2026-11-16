"""Append-only JSONL audit log for every tool call and shell command."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class AuditLog:
    """Thread-safe JSONL writer.

    One line per event, so the file stays greppable and survives a crash
    mid-write. Failures to log are swallowed: auditing must never break a
    tool call, but a warning is printed to stderr once.
    """

    def __init__(self, path: Path, enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled
        self._lock = threading.Lock()
        self._warned = False
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> None:
        if not self.enabled:
            return
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()), "ts_unix": round(time.time(), 3), "event": event}
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False, default=str)
        try:
            with self._lock:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError as exc:  # pragma: no cover - disk problems
            if not self._warned:
                self._warned = True
                print(f"[audit] failed to write {self.path}: {exc}", flush=True)

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"event": "unparsed", "raw": line})
        return out
