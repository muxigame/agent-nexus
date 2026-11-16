"""``session_probe``: is the session in interactive use right now? (design §8.7, risk R1)

Any one of these makes it *live*, and the gateway then does not fork it:

(a) a claude / codex process whose command line carries the resume id;
(b) the session file was written in the last 30 s
    (``~/.claude/projects/*/<id>.jsonl``, ``~/.codex/sessions/**/rollout-*<id>*.jsonl``);
(c) a hook reported a prompt in this folder within ``idle_after_s``.

It is a heuristic: a session can be open and idle with none of these true.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

FRESH_FILE_S = 30.0
DEFAULT_IDLE_AFTER_S = 120.0
#: (kind, normalised cwd) -> (time, session id) of the last hook prompt; written by the
#: local console's ``/api/agents/presence`` (the hooks), read here.
_HOOKS: dict[tuple[str, str], tuple[float, str | None]] = {}
_HOOKS_LOCK = threading.Lock()


def norm_cwd(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)).replace("\\", "/").rstrip("/").casefold()


def hook_seen(kind: str, cwd: str, session_id: str | None, at: float | None = None) -> None:
    with _HOOKS_LOCK:
        _HOOKS[(kind, norm_cwd(cwd))] = (time.time() if at is None else at, session_id)


def _home(config: dict, kind: str) -> Path:
    configured = config.get(f"{kind}_home")
    if isinstance(configured, str) and configured:
        return Path(configured)
    return Path(os.path.expanduser("~")) / (".claude" if kind == "claude" else ".codex")


def session_files(kind: str, resume_id: str, config: dict) -> list[Path]:
    home = _home(config, kind)
    if kind == "claude":
        return list((home / "projects").glob(f"*/{resume_id}.jsonl"))
    root = home / "sessions"
    return list(root.rglob(f"rollout-*{resume_id}*.jsonl")) if root.is_dir() else []


def _processes(kind: str, resume_id: str) -> list[int]:
    try:
        import psutil
    except ImportError:
        return []
    found = []
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
        except Exception:
            continue
        if proc.info.get("pid") == me or not cmdline:
            continue
        joined = " ".join(cmdline)
        if resume_id in joined and kind in joined.lower():
            found.append(proc.info["pid"])
    return found


def probe(params: dict, config: dict | None = None, *, now: float | None = None) -> dict[str, Any]:
    config = config or {}
    now = time.time() if now is None else now
    kind = params.get("kind")
    resume_id = params.get("resume_id")
    workdir = params.get("workdir")
    if kind not in ("claude", "codex"):
        return {"live": False, "reason": "not a claude/codex session"}
    idle_after = float(config.get("idle_after_s") or DEFAULT_IDLE_AFTER_S)
    if isinstance(workdir, str) and workdir:
        with _HOOKS_LOCK:
            seen = _HOOKS.get((kind, norm_cwd(workdir)))
        if seen and now - seen[0] < idle_after and (not resume_id or not seen[1] or seen[1] == resume_id):
            return {"live": True, "reason": "a hook reported a prompt recently", "signal": "hook"}
    if not isinstance(resume_id, str) or not resume_id:
        return {"live": False, "reason": "no resume id to look for"}
    for path in session_files(kind, resume_id, config):
        try:
            if now - path.stat().st_mtime < FRESH_FILE_S:
                return {"live": True, "reason": f"session file written {int(now - path.stat().st_mtime)} s ago",
                        "signal": "file"}
        except OSError:
            continue
    if config.get("probe_processes", True):
        pids = _processes(kind, resume_id)
        if pids:
            return {"live": True, "reason": "a process is running this session", "signal": "process", "pids": pids[:5]}
    return {"live": False, "reason": "no sign of use"}
