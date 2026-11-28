"""Per-run temporary directories (docs/agent-chat.md §8).

Every run gets ``$tmpdir`` = ``<root>/.anx/agents/tmp/<rid>``, created for the run-as account (Unix: chowned to it,
the directory private to it; Windows: the account is granted modify rights). An index beside the directories,
``<root>/.anx/agents/tmp.json``, remembers each run's CLI, session name and when it was created and closed.

Cleaning never touches a run that is still active (``active()`` -- supplied by the run manager -- names them):

* :meth:`TmpDirs.clean` -- one run's directory, or every directory idle for more than N days;
* :meth:`TmpDirs.sweep` -- the automatic pass: directories of runs closed more than 7 days ago, then the oldest ones
  while the total is over 2 GB, plus ``.anx/wake/*.log`` older than 14 days.

The age of a directory is its last activity: the newest of its close time and the newest modification inside it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

WINDOWS = sys.platform == "win32"
DAY = 86400.0
KEEP_DAYS = 7
CAP_BYTES = 2 * 1024 ** 3
WAKE_LOG_DAYS = 14
#: A size walk stops counting after this many entries (the size is then a lower bound, ``partial``).
WALK_MAX = 200_000
_RID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def valid_rid(rid: Any) -> bool:
    return isinstance(rid, str) and bool(_RID.fullmatch(rid)) and rid not in (".", "..")


def measure(path: Path, *, limit: int = WALK_MAX) -> dict:
    """``{size, files, updated_at, partial}`` of a tree; symlinks are counted, never followed."""
    size = files = seen = 0
    try:
        newest = path.lstat().st_mtime
    except OSError:
        return {"size": 0, "files": 0, "updated_at": None, "partial": False}
    stack = [str(path)]
    partial = False
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    seen += 1
                    if seen > limit:
                        partial = True
                        stack.clear()
                        break
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    newest = max(newest, info.st_mtime)
                    if stat.S_ISDIR(info.st_mode):
                        stack.append(entry.path)
                    else:
                        files += 1
                        size += info.st_size
        except OSError:
            continue
    return {"size": size, "files": files, "updated_at": newest, "partial": partial}


def _force_remove(function: Callable, path: str, _excinfo: Any) -> None:
    """rmtree helper: clear the read-only bit (Windows, git objects) and try once more."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | (stat.S_IEXEC if os.path.isdir(path) else 0))
        function(path)
    except OSError:
        pass


def remove_tree(path: Path) -> bool:
    """Delete ``path`` (a link is unlinked, never followed). True when it is gone."""
    try:
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
        else:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=_force_remove)
            else:  # pragma: no cover - 3.11
                shutil.rmtree(path, onerror=_force_remove)
    except OSError:
        pass
    return not os.path.lexists(path)


class TmpDirs:
    def __init__(self, root: Path, *, active: Callable[[], Iterable[str]] | None = None,
                 keep_days: float = KEEP_DAYS, cap_bytes: int = CAP_BYTES, wake_log_days: float = WAKE_LOG_DAYS):
        self.root = Path(root)
        self.base = self.root / ".anx" / "agents" / "tmp"
        self.index_path = self.root / ".anx" / "agents" / "tmp.json"
        self.active = active
        self.keep_days, self.cap_bytes, self.wake_log_days = keep_days, cap_bytes, wake_log_days
        self.lock = threading.RLock()

    # -- index
    def _load(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        runs = raw.get("runs") if isinstance(raw, dict) else None
        return {k: dict(v) for k, v in (runs or {}).items() if isinstance(v, dict) and valid_rid(k)}

    def _save(self, runs: dict[str, dict]) -> None:
        try:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.index_path.with_name(f".tmp.json.{os.getpid()}.{threading.get_ident()}.tmp")
            temporary.write_text(json.dumps({"version": 1, "runs": runs}, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
            os.replace(temporary, self.index_path)
        except OSError:
            pass

    def _active(self) -> set[str]:
        if not self.active:
            return set()
        try:
            return {str(rid) for rid in self.active() or ()}
        except Exception:  # noqa: BLE001 - an unknown state means "nothing is known to be active"
            return set()

    # -- runs
    def path(self, rid: str) -> Path:
        if not valid_rid(rid):
            raise ValueError(f"invalid run id: {rid!r}")
        return self.base / rid

    def create(self, rid: str, account: Any = None, *, kind: str | None = None, session: str | None = None) -> Path:
        """Create (or reuse) the run's directory, owned by ``account`` (an ``accounts.Account``; ``None`` = this
        process's user). Raises ``ValueError`` for a bad id and ``OSError`` when it cannot be created."""
        path = self.path(rid)
        with self.lock:
            path.mkdir(parents=True, exist_ok=True)
            if account is not None:
                self._hand_over(account, path)
            elif not WINDOWS:
                try:
                    os.chmod(path, 0o700)
                except OSError:
                    pass
            runs = self._load()
            entry = runs.get(rid, {})
            entry.update(created_at=entry.get("created_at") or time.time(), closed_at=None)
            if kind:
                entry["kind"] = kind
            if session:
                entry["session"] = session
            runs[rid] = entry
            self._save(runs)
        return path

    def _hand_over(self, account: Any, path: Path) -> None:
        from .. import accounts
        if getattr(account, "session", None) is None and not WINDOWS:
            # the account must reach its directory through .anx/agents/tmp, without listing the others
            for directory in (self.root / ".anx", self.root / ".anx" / "agents", self.base):
                accounts.traversable(directory)
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                try:
                    os.chown(path, account.uid, account.gid)
                except OSError:
                    pass
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
            return
        accounts.hand_over(account, path)

    def update(self, rid: str, **fields: Any) -> None:
        """Remember a run's ``session`` / ``kind`` once they are known."""
        if not valid_rid(rid):
            return
        with self.lock:
            runs = self._load()
            entry = runs.setdefault(rid, {"created_at": time.time(), "closed_at": None})
            entry.update({k: v for k, v in fields.items() if k in ("session", "kind") and v})
            self._save(runs)

    def close(self, rid: str, *, now: float | None = None) -> None:
        """The run ended: its directory is kept for :data:`KEEP_DAYS` days from now."""
        if not valid_rid(rid):
            return
        with self.lock:
            runs = self._load()
            if rid in runs or self.path(rid).exists():
                runs.setdefault(rid, {"created_at": None})["closed_at"] = time.time() if now is None else now
                self._save(runs)

    # -- listing
    def list(self) -> dict:
        """``{items: [{rid, path, size, files, updated_at, created_at, closed_at, session, kind, active}], total}``,
        newest first."""
        with self.lock:
            runs = self._load()
        active = self._active()
        items: list[dict] = []
        try:
            entries = list(os.scandir(self.base))
        except OSError:
            entries = []
        for entry in entries:
            if not valid_rid(entry.name):
                continue
            path = Path(entry.path)
            meta = runs.get(entry.name, {})
            measured = measure(path) if entry.is_dir(follow_symlinks=False) else {"size": 0, "files": 0,
                                                                                    "updated_at": None, "partial": False}
            updated = max(filter(None, (measured["updated_at"], meta.get("closed_at"))), default=None)
            items.append({"rid": entry.name, "path": str(path), "size": measured["size"], "files": measured["files"],
                          "partial": measured["partial"], "updated_at": updated, "created_at": meta.get("created_at"),
                          "closed_at": meta.get("closed_at"), "session": meta.get("session") or "",
                          "kind": meta.get("kind") or "", "active": entry.name in active})
        items.sort(key=lambda item: item["updated_at"] or 0, reverse=True)
        return {"items": items, "total": sum(item["size"] for item in items), "path": str(self.base)}

    # -- cleaning
    def _remove(self, rids: Iterable[str]) -> list[str]:
        removed: list[str] = []
        with self.lock:
            for rid in rids:
                if remove_tree(self.base / rid):
                    removed.append(rid)
            if removed:
                runs = self._load()
                for rid in removed:
                    runs.pop(rid, None)
                self._save(runs)
        return removed

    def clean(self, rid: str | None = None, older_than_days: float | None = None, *, now: float | None = None) -> dict:
        """Remove one run's directory, or every inactive directory idle for more than ``older_than_days`` days.
        ``{removed, freed, rids}`` or an error dict; never raises."""
        try:
            if rid is not None:
                if not valid_rid(rid):
                    return {"error": "临时目录的编号不对", "code": "bad_request"}
                if rid in self._active():
                    return {"error": "这个对话还在进行，结束后才能清理它的临时目录", "code": "active"}
                if not os.path.lexists(self.base / rid):
                    self._remove([])
                    return {"error": "这个临时目录已经不存在了", "code": "not_found"}
                size = measure(self.base / rid)["size"]
                removed = self._remove([rid])
                if not removed:
                    return {"error": "临时目录没有删干净，可能有文件正被占用，稍后再试", "code": "busy"}
                return {"removed": 1, "freed": size, "rids": removed}
            if older_than_days is None:
                return {"error": "要指定要清理的临时目录，或清理多少天前的", "code": "bad_request"}
            try:
                days = float(older_than_days)
            except (TypeError, ValueError):
                return {"error": "天数要是数字", "code": "bad_request"}
            if days < 0:
                return {"error": "天数不能是负数", "code": "bad_request"}
            cutoff = (time.time() if now is None else now) - days * DAY
            victims = [item for item in self.list()["items"]
                       if not item["active"] and (item["updated_at"] or 0) < cutoff]
            removed = self._remove(item["rid"] for item in victims)
            freed = sum(item["size"] for item in victims if item["rid"] in removed)
            return {"removed": len(removed), "freed": freed, "rids": removed}
        except Exception as error:  # noqa: BLE001
            return {"error": f"清理失败：{error}", "code": "failed"}

    def clean_session(self, session: str, kind: str | None = None) -> list[str]:
        """Remove the inactive directories of runs (and attachment stages) of ``session``: deleting a session takes
        its attachments and temp files with it (§18). Returns the removed ids; never raises."""
        if not isinstance(session, str) or not session:
            return []
        try:
            with self.lock:
                runs = self._load()
            active = self._active()
            victims = [rid for rid, meta in runs.items() if meta.get("session") == session and rid not in active
                       and (not kind or not meta.get("kind") or meta.get("kind") == kind)]
            return self._remove(victims)
        except Exception:  # noqa: BLE001
            return []

    def sweep(self, *, now: float | None = None) -> dict:
        """The automatic pass (§8). ``{removed, freed, rids, logs}``; never raises."""
        now = time.time() if now is None else now
        try:
            items = self.list()["items"]
            victims: list[dict] = []
            keep: list[dict] = []
            for item in items:
                if item["active"]:
                    keep.append(item)
                    continue
                # last activity: the close time, or (never closed: a crash, an unknown directory) the newest write
                stale = (item["updated_at"] or 0) < now - self.keep_days * DAY
                (victims if stale else keep).append(item)
            total = sum(item["size"] for item in keep)
            if total > self.cap_bytes:
                for item in sorted((i for i in keep if not i["active"]), key=lambda i: i["updated_at"] or 0):
                    if total <= self.cap_bytes:
                        break
                    victims.append(item)
                    total -= item["size"]
            removed = self._remove(item["rid"] for item in victims)
            freed = sum(item["size"] for item in victims if item["rid"] in removed)
            self._prune_index()
            return {"removed": len(removed), "freed": freed, "rids": removed, "logs": self.prune_wake_logs(now=now)}
        except Exception as error:  # noqa: BLE001
            return {"error": f"自动清理失败：{error}", "code": "failed"}

    def _prune_index(self) -> None:
        """Forget runs whose directory is gone and that are not active."""
        with self.lock:
            runs = self._load()
            active = self._active()
            kept = {rid: meta for rid, meta in runs.items() if rid in active or os.path.lexists(self.base / rid)}
            if len(kept) != len(runs):
                self._save(kept)

    def prune_wake_logs(self, *, now: float | None = None) -> int:
        """Delete ``.anx/wake/*.log`` not written for :data:`WAKE_LOG_DAYS` days. Returns how many."""
        cutoff = (time.time() if now is None else now) - self.wake_log_days * DAY
        count = 0
        try:
            entries = list(os.scandir(self.root / ".anx" / "wake"))
        except OSError:
            return 0
        for entry in entries:
            if not entry.name.endswith(".log"):
                continue
            try:
                if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.unlink(entry.path)
                    count += 1
            except OSError:
                continue
        return count

    # -- RPC ops (never raise)
    def op_list(self, params: dict | None = None) -> dict:
        try:
            return self.list()
        except Exception as error:  # noqa: BLE001
            return {"error": f"读取临时目录失败：{error}", "code": "failed"}

    def op_clean(self, params: dict | None = None) -> dict:
        params = params if isinstance(params, dict) else {}
        rid = params.get("rid")
        return self.clean(str(rid) if rid else None, params.get("older_than_days"))
