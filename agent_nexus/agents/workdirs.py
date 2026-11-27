"""Working-directory picker (docs/agent-chat.md §7).

* :func:`list_dirs` lists the sub-directories of ``path`` -- directories only -- inside a set of roots (the run-as
  user's HOME and the device's registered workspaces). Paths are checked after resolving symlinks, so neither ``..``
  nor a link pointing outside lets a listing escape the roots; a symlinked directory whose target is outside is not
  listed. Hidden entries (dot names; on Windows also the hidden/system attributes) are left out unless asked for.
  Without ``path`` the result is the roots themselves (the browser's starting point).
* :func:`default_roots` builds the roots; on Windows it can add the drive roots (``C:\\``…) when the caller allows it.
* :class:`Recent` keeps recently used directories in ``<root>/.anx/agents/recent.json``;
  :func:`frequent_cwds` ranks the working directories found in discovered sessions.

Functions return dicts and never raise; errors are ``{"error": "...", "code": "..."}``.
"""
from __future__ import annotations

import json
import os
import stat
import string
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

WINDOWS = sys.platform == "win32"
MAX_ENTRIES = 2000
RECENT_MAX = 50
#: Windows folders nobody picks as a project (left out like hidden ones).
WINDOWS_SKIP = {"$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws", "config.msi",
                "recovery", "$sysreset", "msocache"}
_HIDDEN_ATTRS = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 2) | getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 4)


def _norm(path: str) -> str:
    """Comparison key: case-insensitive on Windows and macOS (their default file systems), no trailing separator."""
    path = os.path.normpath(path)
    return os.path.normcase(path) if WINDOWS else (path.lower() if sys.platform == "darwin" else path)


def _real(path: str) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return os.path.abspath(path)


def _within(path: str, root: str) -> bool:
    """``path`` is ``root`` or below it (both resolved)."""
    p, r = _norm(path), _norm(root)
    if p == r:
        return True
    prefix = r if r.endswith(os.sep) else r + os.sep
    return p.startswith(prefix)


def drive_roots() -> list[str]:
    """Existing drive roots on Windows (``C:\\``, ``D:\\``…); ``[]`` elsewhere."""
    if not WINDOWS:
        return []
    drives: list[str] = []
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
        drives = [f"{letter}:\\" for i, letter in enumerate(string.ascii_uppercase) if mask & (1 << i)]
    except Exception:  # noqa: BLE001
        drives = [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.isdir(f"{letter}:\\")]
    return [d for d in drives if os.path.isdir(d)]


def default_roots(home: str | os.PathLike | None, workspaces: Iterable[Any] = (), *, drives: bool = False) -> list[dict]:
    """``[{name, path, kind: home|workspace|drive}]``: HOME first, then workspaces (``str`` or ``{name, path}``),
    then (``drives``, Windows only) the drive roots. Missing directories and duplicates are dropped."""
    roots: list[dict] = []
    seen: set[str] = set()

    def add(path: Any, name: str, kind: str) -> None:
        if not path:
            return
        path = os.path.abspath(os.path.expanduser(str(path)))
        if not os.path.isdir(path):
            return
        key = _norm(_real(path))
        if key in seen:
            return
        seen.add(key)
        roots.append({"name": name or os.path.basename(path.rstrip("\\/")) or path, "path": path, "kind": kind})

    if home:
        add(home, "主目录", "home")
    for workspace in workspaces or ():
        if isinstance(workspace, Mapping):
            add(workspace.get("path"), str(workspace.get("name") or ""), "workspace")
        else:
            add(workspace, "", "workspace")
    if drives:
        for drive in drive_roots():
            add(drive, drive.rstrip("\\"), "drive")
    return roots


def _root_entries(roots: Iterable[Any]) -> list[dict]:
    entries: list[dict] = []
    for root in roots or ():
        if isinstance(root, Mapping):
            path = root.get("path")
            entry = {"name": str(root.get("name") or ""), "path": str(path or ""), "kind": root.get("kind") or "workspace"}
        else:
            entry = {"name": "", "path": str(root or ""), "kind": "workspace"}
        if not entry["path"]:
            continue
        entry["path"] = os.path.abspath(os.path.expanduser(entry["path"]))
        entry["name"] = entry["name"] or os.path.basename(entry["path"].rstrip("\\/")) or entry["path"]
        entry["real"] = _real(entry["path"])
        entries.append(entry)
    return entries


def _is_hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith("."):
        return True
    if WINDOWS:
        if entry.name.lower() in WINDOWS_SKIP:
            return True
        try:
            return bool(getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & _HIDDEN_ATTRS)
        except OSError:
            return False
    return False


def resolve(path: str | os.PathLike, roots: Iterable[Any]) -> dict:
    """Check ``path`` against ``roots``: ``{path, real, root}`` or an error dict. ``~`` is the first root
    (HOME); a relative path is taken relative to it too."""
    entries = _root_entries(roots)
    if not entries:
        return {"error": "没有可浏览的目录（执行账户的主目录不存在，也没有登记工作区）", "code": "no_roots"}
    raw = str(path).strip()
    if not raw:
        return {"error": "目录为空", "code": "bad_path"}
    if "\x00" in raw:
        return {"error": "目录里有非法字符", "code": "bad_path"}
    base = entries[0]["path"]
    if raw == "~" or raw.startswith(("~/", "~\\")):
        raw = os.path.join(base, raw[2:]) if len(raw) > 1 else base
    elif not os.path.isabs(raw):
        raw = os.path.join(base, raw)
    shown = os.path.abspath(raw)
    real = _real(shown)
    owner = None
    for entry in entries:
        if _within(real, entry["real"]) and (owner is None or len(entry["real"]) > len(owner["real"])):
            owner = entry
    if owner is None:
        return {"error": f"{shown} 不在可选范围内：只能选执行账户的主目录或已登记的工作区里的目录", "code": "outside"}
    # what we show: the path as typed when it lies under its root's displayed path, else the resolved one
    if not _within(shown, owner["path"]):
        shown = real  # reached through a link from elsewhere: show where it really is
    return {"path": shown, "real": real, "root": owner}


def list_dirs(path: str | os.PathLike | None, roots: Iterable[Any], *, hidden: bool = False,
              limit: int = MAX_ENTRIES) -> dict:
    """``{path, parent, root, dirs: [{name, path, link?}], roots, hidden, truncated}``.

    ``path`` ``None``/empty: ``path`` is ``None`` and ``dirs`` are the roots. ``parent`` is ``None`` at a root (the
    picker then goes back to the roots). ``hidden`` is the number of hidden directories left out (or listed, when
    ``hidden=True``)."""
    entries = _root_entries(roots)
    public_roots = [{"name": e["name"], "path": e["path"], "kind": e["kind"]} for e in entries]
    if path in (None, ""):
        if not entries:
            return {"error": "没有可浏览的目录（执行账户的主目录不存在，也没有登记工作区）", "code": "no_roots"}
        return {"path": None, "parent": None, "root": None, "dirs": [dict(r) for r in public_roots],
                "roots": public_roots, "hidden": 0, "truncated": False}
    checked = resolve(path, entries)
    if "error" in checked:
        return {**checked, "roots": public_roots}
    shown, real, owner = checked["path"], checked["real"], checked["root"]
    if not os.path.isdir(real):
        what = "不是目录" if os.path.exists(real) else "不存在"
        return {"error": f"{shown} {what}", "code": "not_found", "roots": public_roots}
    dirs: list[dict] = []
    hidden_count = 0
    truncated = False
    try:
        with os.scandir(real) as iterator:
            for entry in iterator:
                try:
                    if not entry.is_dir(follow_symlinks=True):
                        continue
                except OSError:
                    continue
                link = entry.is_symlink()
                if link and not any(_within(_real(entry.path), e["real"]) for e in entries):
                    continue  # a link out of the roots is not offered
                if _is_hidden(entry):
                    hidden_count += 1
                    if not hidden:
                        continue
                if len(dirs) >= limit:
                    truncated = True
                    continue
                item = {"name": entry.name, "path": os.path.join(shown, entry.name)}
                if link:
                    item["link"] = True
                dirs.append(item)
    except PermissionError:
        return {"error": f"没有权限读取 {shown}", "code": "denied", "roots": public_roots}
    except OSError as error:
        return {"error": f"读取 {shown} 失败：{error.strerror or error}", "code": "io", "roots": public_roots}
    dirs.sort(key=lambda d: (d["name"].startswith("."), d["name"].casefold()))
    at_root = _norm(real) == _norm(owner["real"])
    parent = None
    if not at_root:
        candidate = os.path.dirname(shown.rstrip("\\/")) or shown
        parent = candidate if _norm(candidate) != _norm(shown) else None
        if parent and "error" in resolve(parent, entries):
            parent = owner["path"]
    return {"path": shown, "parent": parent, "root": {"name": owner["name"], "path": owner["path"], "kind": owner["kind"]},
            "dirs": dirs, "roots": public_roots, "hidden": hidden_count, "truncated": truncated}


def frequent_cwds(sessions: Iterable[Mapping], *, kind: str | None = None, limit: int = 8) -> list[dict]:
    """The working directories of discovered sessions, most used first (ties: most recent):
    ``[{path, count, updated_at, kinds}]``."""
    counts: Counter = Counter()
    latest: dict[str, float] = {}
    shown: dict[str, str] = {}
    kinds: dict[str, list[str]] = {}
    for session in sessions or ():
        cwd = session.get("cwd")
        if not cwd or (kind and session.get("kind") != kind):
            continue
        key = _norm(str(cwd))
        counts[key] += 1
        shown.setdefault(key, str(cwd))
        try:
            updated = float(session.get("updated_at") or 0)
        except (TypeError, ValueError):
            updated = 0.0
        latest[key] = max(latest.get(key, 0.0), updated)
        if session.get("kind") and session["kind"] not in kinds.setdefault(key, []):
            kinds[key].append(session["kind"])
    ranked = sorted(counts, key=lambda k: (-counts[k], -latest.get(k, 0.0)))
    return [{"path": shown[k], "count": counts[k], "updated_at": latest.get(k) or None, "kinds": kinds.get(k, [])}
            for k in ranked[:limit]]


class Recent:
    """Recently used working directories: ``{"version": 1, "dirs": [{path, kinds, used_at, count}]}``,
    newest first, at most :data:`RECENT_MAX`."""

    def __init__(self, root: Path, *, path: Path | None = None, limit: int = RECENT_MAX):
        self.path = Path(path) if path else Path(root) / ".anx" / "agents" / "recent.json"
        self.max = limit
        self.lock = threading.Lock()

    def _load(self) -> list[dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = raw.get("dirs") if isinstance(raw, dict) else None
        return [dict(item) for item in items or [] if isinstance(item, dict) and isinstance(item.get("path"), str)]

    def _save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            temporary.write_text(json.dumps({"version": 1, "dirs": items[:self.max]}, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    def list(self, *, kind: str | None = None, limit: int = 20, existing: bool = True) -> list[dict]:
        """Newest first; ``kind`` puts that CLI's directories first; ``existing`` drops vanished directories."""
        with self.lock:
            items = self._load()
        if existing:
            items = [item for item in items if os.path.isdir(item["path"])]
        if kind:
            items.sort(key=lambda item: kind not in (item.get("kinds") or []))  # stable: keeps recency order
        return items[:limit]

    def add(self, path: str, *, kind: str | None = None, now: float | None = None) -> list[dict]:
        path = os.path.abspath(os.path.expanduser(str(path)))
        key = _norm(path)
        with self.lock:
            items = self._load()
            current = next((item for item in items if _norm(item["path"]) == key), None)
            items = [item for item in items if item is not current]
            entry = current or {"path": path, "kinds": [], "count": 0}
            entry["path"] = path
            entry["used_at"] = time.time() if now is None else now
            entry["count"] = int(entry.get("count") or 0) + 1
            kinds = [k for k in entry.get("kinds") or [] if isinstance(k, str)]
            if kind and kind not in kinds:
                kinds.append(kind)
            entry["kinds"] = kinds
            items.insert(0, entry)
            items = items[:self.max]
            try:
                self._save(items)
            except OSError:
                pass
            return items

    def remove(self, path: str) -> list[dict]:
        key = _norm(os.path.abspath(os.path.expanduser(str(path))))
        with self.lock:
            items = [item for item in self._load() if _norm(item["path"]) != key]
            try:
                self._save(items)
            except OSError:
                pass
            return items
