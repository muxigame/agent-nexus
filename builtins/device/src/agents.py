"""``agent_sessions``: find the newest Claude Code / Codex session for a working directory (design §8.3).

* Claude Code keeps one transcript per session in
  ``~/.claude/projects/<cwd with every non-alphanumeric character as "-">/<session id>.jsonl``.
* Codex keeps ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``; the first line
  is the ``session_meta`` record with the thread ``id`` and its ``cwd``.

Only ids and modification times are returned, never transcript contents.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

KINDS = ("claude", "codex")
MAX_CANDIDATES = 5
#: Codex keeps every rollout; only the newest files are opened.
MAX_CODEX_FILES = 400
#: The newest session is "high" confidence when it is this much newer than the next.
CLEAR_LEAD_S = 120.0


def claude_project_dir(home: Path, workdir: Path) -> Path:
    return home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workdir))


def _same_dir(left: str, right: Path) -> bool:
    try:
        a, b = os.path.normpath(left), os.path.normpath(str(right))
    except (TypeError, ValueError):
        return False
    if sys.platform == "win32":
        a, b = a.casefold(), b.casefold()
    return a == b


def _claude(home: Path, workdir: Path) -> list[tuple[float, str]]:
    folder = claude_project_dir(home, workdir)
    found = []
    for path in folder.glob("*.jsonl") if folder.is_dir() else ():
        try:
            found.append((path.stat().st_mtime, path.stem))
        except OSError:
            continue
    return found


def _codex_meta(path: Path) -> tuple[str | None, str | None]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            first = handle.readline(1_000_000)
        record = json.loads(first)
    except (OSError, ValueError):
        return None, None
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
    session_id, cwd = payload.get("id"), payload.get("cwd")
    return (session_id if isinstance(session_id, str) else None), (cwd if isinstance(cwd, str) else None)


def _codex(home: Path, workdir: Path) -> list[tuple[float, str]]:
    root = home / ".codex" / "sessions"
    if not root.is_dir():
        return []
    files = []
    for path in root.rglob("rollout-*.jsonl"):
        try:
            files.append((path.stat().st_mtime, path))
        except OSError:
            continue
    files.sort(key=lambda item: item[0], reverse=True)
    found = []
    for mtime, path in files[:MAX_CODEX_FILES]:
        session_id, cwd = _codex_meta(path)
        if session_id and cwd and _same_dir(cwd, workdir):
            found.append((mtime, session_id))
    return found


def find_sessions(kind: str, workdir: Path, home: Path | None = None) -> dict[str, Any]:
    """``{kind, workdir, id, confidence, candidates}``; ``id`` is ``None`` when nothing matches."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    home = home or Path.home()
    found = (_claude if kind == "claude" else _codex)(home, workdir)
    found.sort(key=lambda item: item[0], reverse=True)
    candidates = [{"id": session_id, "mtime": round(mtime, 3)} for mtime, session_id in found[:MAX_CANDIDATES]]
    if not candidates:
        confidence = "none"
    elif len(found) == 1 or found[0][0] - found[1][0] >= CLEAR_LEAD_S:
        confidence = "high"
    else:
        confidence = "low"
    return {"kind": kind, "workdir": str(workdir), "id": candidates[0]["id"] if candidates else None,
            "confidence": confidence, "candidates": candidates}
