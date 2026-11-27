"""This device's files for the other devices of the network (``files/1``, ``files/2``).

Two ways in, the same operations behind both:

* over the P2P tunnel, the remote hub's ``files`` service (``remote_hub.py``) streams a file in
  frames: the fast path;
* through the gateway when no tunnel can be punched, reverse RPC ``fx_*`` moves a file in
  chunks of at most ``CHUNK`` bytes (``files_bridge.py`` on the gateway).

Who may ask: exactly who may open a remote terminal here (another joined device of the owner's
network, or the owner on the gateway console), so the files are those of the account that
terminal would log in as (``accounts.terminal``). A shell there could read and write them
anyway; this only makes moving them convenient. An upload is written next to its target as
``.<name>.<id>.anxpart`` and renamed when complete; by default never over an existing file
(`` (1)`` is added).

``files/2`` (copy jobs, ``file_copy.py``; docs/design.md §18) adds, all optional and additive:

* ``begin``: ``on_exists`` (``rename`` | ``overwrite`` | ``error`` | ``skip_same``), ``resume_key``
  (a deterministic part name that survives an aborted or idle upload, so a later job appends to
  it: ``have`` comes back), ``mtime_ns``; a target addressed as ``base`` + ``rel`` (a list of name
  components joined here with this OS's own rules, never a joined string from the caller);
* ``finish``: ``sha256`` (checked against the bytes on disk; a mismatch deletes the part),
  ``mtime_ns``, ``wait_s`` (a resumed upload is re-hashed in a thread: the call answers
  ``{"state": "hashing"}`` until that is done, and is simply called again);
* ``walk`` (a sorted, paged depth-first walk), ``stat_many``, ``mkdirs``, ``hash``.

A copy job addresses files as ``base`` + ``rel``: no component under ``base`` may be a symlink or
a junction (checked with ``lstat`` right before every read or write, and the last component is
opened with ``O_NOFOLLOW``), so swapping a file or folder for a link while a long copy runs does
not lead it outside the tree it was given. A part file is created anew (``O_EXCL``, mode 0600)
or resumed only when it is a plain file this process owns; it gets its final mode just before
it is renamed: the existing file's mode and owner on an overwrite, else the source's mode
(``mode``) under this process's umask.
"""
from __future__ import annotations

import base64
import bisect
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat as stat_mod
import string
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

FEATURE = "files/1"
#: the copy-job extensions above
FEATURE_V2 = "files/2"
#: largest chunk one ``fx_read`` / ``fx_put_write`` moves (before base64)
CHUNK = 1024 * 1024
#: frames on the P2P stream
STREAM_CHUNK = 256 * 1024
#: an upload nobody finishes is dropped after this long (a keyed one only closes its handle)
UPLOAD_IDLE_S = 600.0
MAX_ENTRIES = 5000
#: entries one ``walk`` page returns at most, items one ``stat_many`` takes at most
WALK_LIMIT = 2000
STAT_LIMIT = 1000
WINDOWS = sys.platform == "win32"
ON_EXISTS = ("rename", "overwrite", "error", "skip_same")
RESUME_KEY = re.compile(r"[A-Za-z0-9_-]{8,64}")
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_WINDOWS_BAD = set('<>:"|?*')
#: Windows ERROR_DISK_FULL / ERROR_HANDLE_DISK_FULL
_WIN_DISK_FULL = (112, 39)
_REPARSE_POINT = 0x400
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_BINARY = getattr(os, "O_BINARY", 0)
O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
#: a walk page answers early after this long (a slow disk or network share)
WALK_PAGE_S = 20.0
#: the sorted names of a big folder are kept this long for the next pages of a walk
NAMES_TTL_S = 120.0
_NAMES_MIN = 512
_NAMES_CACHED = 8
#: keyed parts nobody resumed for this long are deleted (when a job looks at their folder)
PART_MAX_AGE_S = 7 * 24 * 3600.0
_SWEEP_EVERY_S = 3600.0


def _read_umask() -> int:
    if WINDOWS:
        return 0o022
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


#: the process umask (read once at import, while nothing else runs)
UMASK = _read_umask()


class FileShareError(Exception):
    """A refusal the caller should see as is; ``code`` tells a program what happened
    (``exists``, ``checksum``, ``nospace``, ``name``, ``missing``, ``offset``, ``busy``)."""

    def __init__(self, message: str, code: str = "", **extra: Any):
        super().__init__(message)
        self.code = code
        self.extra = extra

    def body(self) -> dict:
        body: dict = {"error": str(self)}
        if self.code:
            body["code"] = self.code
        body.update(self.extra)
        return body


def disk_full(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and (exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in _WIN_DISK_FULL)


def _os_error(exc: OSError, what: str) -> FileShareError:
    if disk_full(exc):
        return FileShareError("目标磁盘已满", "nospace")
    if isinstance(exc, PermissionError):
        return FileShareError(f"{what}：没有权限（{exc.strerror or exc}）", "permission")
    return FileShareError(f"{what}：{exc}")


def invalid_name(name: Any, windows: bool = WINDOWS) -> str | None:
    """Why ``name`` cannot be one path component here (None when it can). A pure function: the
    copy engine checks a Windows destination's names with ``windows=True`` from any platform."""
    if not isinstance(name, str) or name in ("", ".", ".."):
        return f"文件名无效：{name!r}"
    if "/" in name or "\x00" in name or (windows and "\\" in name):
        return f"文件名无效：{name!r}"
    if len(name.encode("utf-8", "surrogateescape")) > 255 and not windows:
        return f"文件名过长：{name[:40]}…"
    if windows:
        if len(name) > 255:
            return f"文件名过长：{name[:40]}…"
        if any(ch in _WINDOWS_BAD or ord(ch) < 32 for ch in name) or name[-1] in ". ":
            return f"文件名在 Windows 上无效：{name}"
        if name.split(".")[0].upper().rstrip() in _WINDOWS_RESERVED:
            return f"文件名在 Windows 上无效：{name}"
    return None


def _long(path: Path) -> Path:
    """Windows: the ``\\\\?\\`` prefix for absolute paths near MAX_PATH."""
    text = str(path)
    if not WINDOWS or len(text) <= 240 or text.startswith("\\\\?\\"):
        return path
    if text.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + text[2:])
    return Path("\\\\?\\" + text)


def _is_link(entry: os.DirEntry) -> bool:
    """A symlink, or a Windows junction / other reparse point: reported, never followed."""
    try:
        if entry.is_symlink():
            return True
        junction = getattr(entry, "is_junction", None)
        if junction is not None and junction():
            return True
        if WINDOWS:
            info = entry.stat(follow_symlinks=False)
            return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)
    except OSError:
        return False
    return False


def part_name(name: str, key: str) -> str:
    return f".{name[:120]}.{key}.anxpart"


def _is_link_info(info: os.stat_result) -> bool:
    """An ``lstat`` result of a symlink, or of a Windows junction / other reparse point."""
    return stat_mod.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _own_plain_file(info: os.stat_result) -> bool:
    """A part this process may resume: a plain file, one link, owned by this process (POSIX)."""
    if not stat_mod.S_ISREG(info.st_mode) or _is_link_info(info):
        return False
    if WINDOWS:
        return True
    return info.st_nlink == 1 and info.st_uid == os.geteuid()


def same_mtime(src_ns: Any, dst_ns: Any) -> bool:
    """Whether a destination mtime is the source's once the destination's file system stored it:
    NTFS keeps 100 ns, SMB shares and HFS+ 1 s, FAT / exFAT 2 s (rounded either way)."""
    a, b = int(src_ns), int(dst_ns)
    if a == b:
        return True
    if b % 1_000_000_000 == 0:
        return abs(a - b) <= 2_000_000_000
    if b % 100 == 0:
        return abs(a - b) < 100
    return False


def open_read(path: Path, nofollow: bool):
    """A file for reading; ``nofollow``: refused when the last component is a symlink (POSIX)."""
    flags = os.O_RDONLY | O_BINARY | O_CLOEXEC | (O_NOFOLLOW if nofollow else 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if nofollow and exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1)):
            raise FileShareError(f"是符号链接，未跟随：{path}", "link") from None
        raise
    return os.fdopen(fd, "rb")


def _open_part(part: Path, resume: bool):
    """(handle, resumed): append to ``part`` when ``resume`` and it is a plain file of ours, else
    create it anew (a link or anything else at that name is removed first, never followed)."""
    flags = os.O_WRONLY | O_BINARY | O_CLOEXEC | O_NOFOLLOW
    if resume:
        try:
            info = os.lstat(part)
        except FileNotFoundError:
            info = None
        if info is not None and _own_plain_file(info):
            fd = os.open(part, flags | os.O_APPEND)
            opened = os.fstat(fd)
            if stat_mod.S_ISREG(opened.st_mode) and (opened.st_dev, opened.st_ino) == (info.st_dev, info.st_ino):
                handle = os.fdopen(fd, "ab")
                handle.seek(0, os.SEEK_END)
                return handle, True
            os.close(fd)
    try:
        os.unlink(part)
    except FileNotFoundError:
        pass
    fd = os.open(part, flags | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "wb"), False


class _Upload:
    def __init__(self, target: Path, part: Path, size: int | None, *, key: str = "", resume: bool = False,
                 on_exists: str = "rename", mtime_ns: int | None = None, mode: int | None = None):
        self.target = target
        self.part = part
        self.size = size
        self.keep = bool(key)
        self.on_exists = on_exists
        self.mtime_ns = mtime_ns
        self.mode = mode
        #: the new file goes to the person (a root core); not an overwrite, which keeps its owner
        self.give = True
        self.handle, resumed = _open_part(part, resume)
        self.written = self.handle.tell() if resumed else 0
        self.resumed = self.written > 0
        info = os.fstat(self.handle.fileno())
        self.ident = (info.st_dev, info.st_ino)
        self.hasher = hashlib.sha256()
        self.touched = time.monotonic()
        #: re-hashing a resumed part (``finish`` with ``wait_s``)
        self.hash_thread: threading.Thread | None = None
        self.hashed = 0
        self.digest: str | None = None
        self.hash_error: BaseException | None = None


class FileShare:
    def __init__(self, account: Callable[[], Any] | None = None):
        self.account = account or (lambda: None)
        self.uploads: dict[str, _Upload] = {}
        self._mutex = threading.Lock()
        #: folder -> (when, sorted names): big folders between the pages of a walk
        self._names_cache: dict[str, tuple[float, list[str]]] = {}
        #: folder -> when its old parts were last swept
        self._swept: dict[str, float] = {}

    # -- where ------------------------------------------------------------------------
    def home(self) -> Path:
        account = self.account()
        if account is not None and getattr(account, "home", ""):
            return Path(account.home)
        return Path.home()

    def default_dir(self) -> Path:
        downloads = self.home() / "Downloads"
        return downloads if downloads.is_dir() else self.home()

    def _path(self, text: Any, *, default: Path | None = None) -> Path:
        value = str(text or "").strip()
        if not value:
            if default is None:
                raise FileShareError("缺少路径")
            return default
        if value == "~" or value.startswith(("~/", "~\\")):
            value = str(self.home()) + value[1:]
        path = Path(value)
        if not path.is_absolute():
            raise FileShareError("路径必须是绝对路径")
        return path

    @staticmethod
    def _child(base: Path, rel: Any) -> Path:
        """``base`` joined with the components of ``rel`` by this OS's rules; nothing renamed."""
        if not isinstance(rel, list):
            raise FileShareError("rel 必须是文件名列表", "name")
        path = base
        for name in rel:
            problem = invalid_name(name)
            if problem:
                raise FileShareError(problem, "name")
            path = path / name
        return _long(path)

    @staticmethod
    def _check_links(base: Path, rel: list) -> None:
        """Refuse when a component of ``rel`` under ``base`` is a symlink or a junction (what does
        not exist yet is fine: it will be created as a plain folder or file)."""
        path = base
        for name in rel:
            path = path / name
            try:
                info = os.lstat(_long(path))
            except OSError:
                return
            if _is_link_info(info):
                raise FileShareError(f"路径中有符号链接或联接点，未跟随：{path}", "link")

    def _target(self, params: dict) -> Path:
        """``base`` + ``rel`` (files/2), else ``path``."""
        if params.get("base") not in (None, ""):
            return self._child(self._path(params.get("base")), params.get("rel") or [])
        return _long(self._path(params.get("path")))

    @staticmethod
    def _roots() -> list[str]:
        if not WINDOWS:
            return ["/"]
        return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]

    def _missing(self, path: Path) -> FileShareError:
        hint = ""
        if WINDOWS and len(str(path)) >= 2 and str(path)[1] == ":":
            hint = "（服务账户看不到映射的网络驱动器，请用 UNC 路径 \\\\server\\share）"
        return FileShareError(f"路径不存在：{path}{hint}", "missing")

    @staticmethod
    def _denied(path: Path, exc: OSError) -> FileShareError:
        if sys.platform == "darwin" and "not permitted" in str(exc).lower():
            return FileShareError(f"没有权限：{path}（需要在 系统设置→隐私与安全性→完全磁盘访问权限 中允许 Agent Nexus）",
                                  "permission")
        return FileShareError(f"没有权限：{path}", "permission")

    # -- list -------------------------------------------------------------------------
    def list(self, params: dict) -> dict:
        path = self._path(params.get("path"), default=self.default_dir())
        if not path.is_dir():
            raise FileShareError(f"不是文件夹：{path}")
        entries = []
        try:
            with os.scandir(path) as scan:
                for entry in scan:
                    if len(entries) >= MAX_ENTRIES:
                        break
                    try:
                        is_dir = entry.is_dir()
                        info = entry.stat()
                    except OSError:
                        continue
                    entries.append({"name": entry.name, "dir": is_dir, "size": 0 if is_dir else info.st_size,
                                    "mtime": int(info.st_mtime)})
        except PermissionError:
            raise FileShareError(f"没有权限读取：{path}") from None
        except OSError as exc:
            raise FileShareError(f"读取失败：{exc}") from None
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        parent = str(path.parent) if path.parent != path else ""
        return {"path": str(path), "parent": parent, "entries": entries, "home": str(self.home()),
                "downloads": str(self.default_dir()), "roots": self._roots(), "sep": os.sep,
                "truncated": len(entries) >= MAX_ENTRIES}

    # -- walk (files/2) -----------------------------------------------------------------
    def _names(self, folder: Path, fresh: bool) -> list[str]:
        """The names in ``folder``, sorted. Raises OSError. A big folder's list is kept for a
        while so the next pages of a walk continue in it instead of listing it again; a walk's
        first page (``fresh``) always lists anew."""
        key = str(folder)
        now = time.monotonic()
        if not fresh:
            with self._mutex:
                hit = self._names_cache.get(key)
            if hit is not None and now - hit[0] < NAMES_TTL_S:
                return hit[1]
        names = sorted(os.listdir(folder))
        with self._mutex:
            if len(names) >= _NAMES_MIN:
                self._names_cache.pop(key, None)
                self._names_cache[key] = (now, names)
                while len(self._names_cache) > _NAMES_CACHED:
                    self._names_cache.pop(next(iter(self._names_cache)))
            else:
                self._names_cache.pop(key, None)
        return names

    @staticmethod
    def _lfacts(path: Path) -> dict:
        """What a walk says about one entry, from ``lstat`` (a link is reported, not followed)."""
        try:
            info = os.lstat(path)
        except OSError as exc:
            return {"dir": False, "link": False, "size": 0, "mtime_ns": 0, "error": f"无法读取：{exc.strerror or exc}"}
        if _is_link_info(info):
            return {"dir": False, "link": True, "size": 0, "mtime_ns": info.st_mtime_ns}
        is_dir = stat_mod.S_ISDIR(info.st_mode)
        facts = {"dir": is_dir, "link": False, "size": 0 if is_dir else info.st_size, "mtime_ns": info.st_mtime_ns,
                 "mode": stat_mod.S_IMODE(info.st_mode)}
        if not is_dir and not stat_mod.S_ISREG(info.st_mode):
            facts["special"] = True
        return facts

    @staticmethod
    def _descends(facts: dict) -> bool:
        return bool(facts.get("dir")) and not facts.get("link") and not facts.get("error")

    def _dfs(self, folder: Path, names: list[str], prefix: list, cursor: list | None, fresh: bool):
        """Entries under ``folder`` in sorted pre-order, after ``cursor`` (a rel under it). The
        cursor is found by bisection and only the entries returned are stat'ed, so a page costs
        about its own size even in a folder of a million files."""
        index = 0
        if cursor:
            index = bisect.bisect_left(names, cursor[0])
            if index < len(names) and names[index] == cursor[0]:
                name = names[index]
                child = folder / name
                if self._descends(self._lfacts(child)):
                    try:
                        sub = self._names(child, fresh)
                    except OSError:
                        sub = None  # its error was reported with its entry
                    if sub is not None:
                        yield from self._dfs(child, sub, prefix + [name], cursor[1:] or None, fresh)
                index += 1
        for position in range(index, len(names)):
            name = names[position]
            child = folder / name
            facts = self._lfacts(child)
            entry = {"rel": prefix + [name], **facts}
            if not self._descends(facts):
                yield entry
                continue
            try:
                sub = self._names(child, fresh)
            except PermissionError:
                entry["error"] = f"没有权限读取文件夹：{child}"
                yield entry
                continue
            except OSError as exc:
                entry["error"] = f"无法读取文件夹：{exc.strerror or exc}"
                yield entry
                continue
            yield entry
            yield from self._dfs(child, sub, prefix + [name], None, fresh)

    def walk(self, params: dict) -> dict:
        """One page of a sorted depth-first walk under ``path`` (folders before their contents).

        ``cursor`` is the ``rel`` of the last entry of the previous page (``next``); the walk
        goes on from there (big folders' name lists are kept between pages for a while). A page
        answers early when it took ``WALK_PAGE_S``. Symlinks and junctions inside are reported
        with ``link`` and not followed; a folder that cannot be read is an entry with ``error``.
        """
        path = self._target(params)
        try:
            limit = max(1, min(int(params.get("limit") or 1000), WALK_LIMIT))
        except (TypeError, ValueError):
            limit = 1000
        cursor = params.get("cursor")
        if isinstance(cursor, str):
            cursor = json.loads(cursor) if cursor else None
        if cursor is not None and (not isinstance(cursor, list) or not all(isinstance(c, str) for c in cursor)):
            raise FileShareError("cursor 无效")
        try:
            info = os.stat(path)  # a symlink given as the top is followed
        except FileNotFoundError:
            raise self._missing(path) from None
        except PermissionError as exc:
            raise self._denied(path, exc) from None
        except OSError as exc:
            raise _os_error(exc, "读取失败") from None
        is_dir = stat_mod.S_ISDIR(info.st_mode)
        root = {"path": str(path), "name": path.name or str(path), "dir": is_dir,
                "size": 0 if is_dir else info.st_size, "mtime_ns": info.st_mtime_ns,
                "platform": sys.platform, "sep": os.sep}
        if not is_dir:
            entries = [] if cursor is not None else [{"rel": [], "dir": False, "link": False, "size": info.st_size,
                                                      "mtime_ns": info.st_mtime_ns,
                                                      "mode": stat_mod.S_IMODE(info.st_mode)}]
            return {"root": root, "entries": entries, "next": None}
        fresh = not cursor
        try:
            names = self._names(path, fresh)
        except PermissionError as exc:
            raise self._denied(path, exc) from None
        except OSError as exc:
            raise _os_error(exc, "读取失败") from None
        entries: list = []
        more = False
        started = time.monotonic()
        for entry in self._dfs(path, names, [], cursor or None, fresh):
            if len(entries) >= limit or (entries and time.monotonic() - started > WALK_PAGE_S):
                more = True
                break
            entries.append(entry)
        return {"root": root, "entries": entries, "next": entries[-1]["rel"] if more and entries else None}

    def stat_many(self, params: dict) -> dict:
        """What exists under ``base`` for each ``rels`` item (``lstat``: a link is ``link``, not
        followed), the bytes of a resumable part for its ``keys`` item (0 unless it is a plain
        file of ours), the free space where ``base`` is or would be created, and ``base`` as
        this device spells it (``~`` expanded). Keyed parts older than ``PART_MAX_AGE_S`` in
        the folders looked at are deleted (at most once an hour per folder)."""
        base = self._path(params.get("base"))
        rels = params.get("rels") or []
        keys = params.get("keys") or []
        if not isinstance(rels, list) or len(rels) > STAT_LIMIT:
            raise FileShareError(f"rels 最多 {STAT_LIMIT} 项")
        items = []
        checked: dict[tuple, str | None] = {}
        for index, rel in enumerate(rels):
            key = keys[index] if isinstance(keys, list) and index < len(keys) else None
            try:
                target = self._child(base, rel)
                parents = tuple(rel[:-1])
                if parents not in checked:
                    try:
                        self._check_links(base, list(parents))
                        checked[parents] = None
                    except FileShareError as exc:
                        checked[parents] = str(exc)
                if checked[parents]:
                    raise FileShareError(checked[parents], "link")
            except FileShareError as exc:
                items.append({"exists": False, "error": str(exc), "code": exc.code})
                continue
            item: dict = {"exists": False}
            try:
                info = os.lstat(target)
                if _is_link_info(info):
                    item = {"exists": True, "dir": False, "link": True, "size": 0, "mtime_ns": info.st_mtime_ns}
                else:
                    is_dir = stat_mod.S_ISDIR(info.st_mode)
                    item = {"exists": True, "dir": is_dir, "size": 0 if is_dir else info.st_size,
                            "mtime_ns": info.st_mtime_ns}
            except (FileNotFoundError, NotADirectoryError):
                pass
            except OSError as exc:
                item = {"exists": False, "error": f"无法读取：{exc.strerror or exc}"}
            if isinstance(key, str) and RESUME_KEY.fullmatch(key):
                self._sweep(target.parent)
                try:
                    part = os.lstat(target.parent / part_name(target.name, key))
                    item["have"] = part.st_size if _own_plain_file(part) else 0
                except OSError:
                    item["have"] = 0
            items.append(item)
        free = None
        probe = base
        while True:
            try:
                free = shutil.disk_usage(probe).free
                break
            except OSError:
                if probe.parent == probe:
                    break
                probe = probe.parent
        return {"items": items, "free": free, "platform": sys.platform, "sep": os.sep, "base": str(base)}

    def _sweep(self, folder: Path) -> None:
        """Delete parts (``.*.anxpart``) in ``folder`` untouched for ``PART_MAX_AGE_S``: left by
        copies that were never repeated, or whose source changed since."""
        key = str(folder)
        now = time.monotonic()
        with self._mutex:
            if now - self._swept.get(key, -_SWEEP_EVERY_S) < _SWEEP_EVERY_S:
                return
            if len(self._swept) > 4096:
                self._swept.clear()
            self._swept[key] = now
            active = {str(upload.part) for upload in self.uploads.values()}
        cutoff = time.time() - PART_MAX_AGE_S
        try:
            with os.scandir(folder) as scan:
                for entry in scan:
                    if not (entry.name.startswith(".") and entry.name.endswith(".anxpart")):
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if stat_mod.S_ISREG(info.st_mode) and info.st_mtime < cutoff and entry.path not in active:
                            os.unlink(entry.path)
                    except OSError:
                        continue
        except OSError:
            return

    def mkdirs(self, params: dict) -> dict:
        base = self._path(params.get("base"))
        made = 0
        for rel in params.get("rels") or [[]]:
            target = self._child(base, rel)
            self._check_links(base, rel)
            try:
                made += self._mkdirs(target)
            except FileExistsError:
                raise FileShareError(f"同名文件已存在，无法创建文件夹：{target}", "exists") from None
            except OSError as exc:
                raise _os_error(exc, "无法创建文件夹") from None
        return {"made": made}

    def hash(self, params: dict) -> dict:
        path, _size = self.stat_file(params)
        offset = max(0, int(params.get("offset") or 0))
        length = params.get("length")
        digest, read = hash_file(path, offset, None if length is None else max(0, int(length)),
                                 nofollow=self.nofollow(params))
        return {"sha256": digest, "size": read, "offset": offset}

    # -- read -------------------------------------------------------------------------
    def stat_file(self, params: dict) -> tuple[Path, int]:
        """The file to read and its size. Addressed as ``base`` + ``rel`` (a copy job): no
        component under ``base`` may be a link (the reader then opens it with ``nofollow``)."""
        path = self._target(params)
        if params.get("base") not in (None, ""):
            rel = params.get("rel") or []
            self._check_links(self._path(params.get("base")), rel)
        if not path.is_file():
            if not path.exists():
                raise FileShareError(f"不是文件：{path}（不存在）", "missing")
            raise FileShareError(f"不是文件：{path}")
        return path, path.stat().st_size

    def read(self, params: dict) -> dict:
        path, size = self.stat_file(params)
        offset = max(0, int(params.get("offset") or 0))
        length = max(0, min(int(params.get("length") or CHUNK), CHUNK))
        try:
            with open_read(path, self.nofollow(params)) as handle:
                handle.seek(offset)
                data = handle.read(length)
                mtime_ns = os.fstat(handle.fileno()).st_mtime_ns
        except PermissionError as exc:
            raise self._denied(path, exc) from None
        except OSError as exc:
            raise FileShareError(f"读取失败：{exc}") from None
        return {"data": base64.b64encode(data).decode("ascii"), "offset": offset, "size": size,
                "name": path.name, "eof": offset + len(data) >= size, "mtime_ns": mtime_ns}

    @staticmethod
    def nofollow(params: dict) -> bool:
        """Whether reading what ``params`` address must not follow a link at the end: a file
        inside the tree of a copy job (``base`` + a non-empty ``rel``)."""
        return params.get("base") not in (None, "") and bool(params.get("rel"))

    # -- write ------------------------------------------------------------------------
    def _reap(self) -> None:
        now = time.monotonic()
        for upload_id, upload in list(self.uploads.items()):
            if now - upload.touched > UPLOAD_IDLE_S and upload.hash_thread is None:
                self._discard(upload_id)

    @staticmethod
    def _clean_name(name: Any) -> str:
        text = str(name or "").strip().replace("\\", "/").split("/")[-1]
        if text in ("", ".", ".."):
            raise FileShareError("文件名无效")
        return text[:255]

    def _mkdirs(self, directory: Path) -> int:
        """``mkdir -p`` that gives each folder it creates to the person (a root core)."""
        missing = []
        probe = directory
        while not probe.exists():
            missing.append(probe)
            if probe.parent == probe:
                break
            probe = probe.parent
        if probe.exists() and not probe.is_dir():
            raise FileExistsError(str(probe))
        directory.mkdir(parents=True, exist_ok=True)
        for folder in reversed(missing):
            self._give(folder)
        return len(missing)

    @staticmethod
    def _facts(path: Path) -> dict | None:
        try:
            info = os.stat(path)
        except OSError:
            try:
                info = os.lstat(path)  # a dangling link
            except OSError:
                return None
        link = False
        try:
            link = _is_link_info(os.lstat(path))
        except OSError:
            pass
        return {"size": info.st_size, "mtime_ns": info.st_mtime_ns, "dir": stat_mod.S_ISDIR(info.st_mode),
                **({"link": True} if link else {})}

    def begin(self, params: dict) -> dict:
        self._reap()
        on_exists = str(params.get("on_exists") or "rename")
        if on_exists not in ON_EXISTS:
            raise FileShareError(f"on_exists 必须是 {' / '.join(ON_EXISTS)}")
        key = params.get("resume_key")
        if key is not None and (not isinstance(key, str) or not RESUME_KEY.fullmatch(key)):
            raise FileShareError("resume_key 无效")
        size = params.get("size")
        mtime_ns = params.get("mtime_ns")
        mode = params.get("mode")
        if mode is not None and (not isinstance(mode, int) or not 0 <= mode <= 0o7777):
            raise FileShareError("mode 无效")
        keyed = params.get("base") not in (None, "")
        if keyed:
            target = self._target(params)
            directory, name = target.parent, target.name
            self._check_links(self._path(params.get("base")), params.get("rel") or [])
        else:
            directory = self._path(params.get("dir"), default=self.default_dir())
            name = self._clean_name(params.get("name"))
            target = directory / name
        exists = self._facts(target)
        if keyed and exists is not None and exists.get("link") and on_exists != "rename":
            raise FileShareError(f"目标是符号链接，不覆盖：{target}", "exists", exists=exists)
        if exists is not None:
            if exists["dir"] and on_exists != "rename":
                raise FileShareError(f"目标是文件夹：{target}", "exists", exists=exists)
            if on_exists == "error":
                raise FileShareError(f"目标已存在：{target}", "exists", exists=exists)
            if on_exists == "skip_same":
                if exists["size"] == size and (mtime_ns is None or same_mtime(mtime_ns, exists["mtime_ns"])):
                    return {"skipped": True, "path": str(target), "exists": exists, "have": 0}
                raise FileShareError(f"目标已存在且内容不同：{target}", "exists", exists=exists)
        try:
            self._mkdirs(directory)
        except FileExistsError:
            raise FileShareError(f"无法创建文件夹：{directory}（同名文件已存在）", "exists") from None
        except OSError as exc:
            raise _os_error(exc, "无法创建文件夹") from None
        if keyed:  # a folder swapped for a link meanwhile
            self._check_links(self._path(params.get("base")), (params.get("rel") or [])[:-1])
        upload_id = secrets.token_urlsafe(12)
        part = directory / (part_name(name, key) if key else f".{name}.{upload_id}.anxpart")
        with self._mutex:
            if key:  # an earlier stream for the same file that died: let go of its handle, keep the part
                for other_id, other in list(self.uploads.items()):
                    if other.part == part:
                        self.uploads.pop(other_id, None)
                        try:
                            other.handle.close()
                        except OSError:
                            pass
            try:
                upload = _Upload(target, part, int(size) if size is not None else None, key=key or "",
                                 resume=bool(key and params.get("resume")), on_exists=on_exists,
                                 mtime_ns=int(mtime_ns) if mtime_ns is not None else None, mode=mode)
            except OSError as exc:
                raise _os_error(exc, f"无法写入 {directory}") from None
            self.uploads[upload_id] = upload
        return {"id": upload_id, "dir": str(directory), "name": name, "chunk": CHUNK, "have": upload.written,
                "exists": None if exists is None else {"size": exists["size"], "mtime_ns": exists["mtime_ns"]}}

    def _upload(self, params: dict) -> tuple[str, _Upload]:
        upload_id = str(params.get("id") or "")
        upload = self.uploads.get(upload_id)
        if upload is None:
            raise FileShareError("上传已失效（超时或已结束），请重新发送", "gone")
        upload.touched = time.monotonic()
        return upload_id, upload

    def write_bytes(self, params: dict, data: bytes) -> dict:
        upload_id, upload = self._upload(params)
        offset = params.get("offset")
        if offset is not None and int(offset) != upload.written:
            raise FileShareError(f"数据顺序不对：期望偏移 {upload.written}，收到 {offset}", "offset", have=upload.written)
        try:
            upload.handle.write(data)
        except OSError as exc:
            self._discard(upload_id, discard=True)
            raise _os_error(exc, "写入失败") from None
        if not upload.resumed:
            upload.hasher.update(data)
        upload.written += len(data)
        return {"written": upload.written}

    def write(self, params: dict) -> dict:
        data = base64.b64decode(str(params.get("data") or ""))
        if len(data) > CHUNK:
            raise FileShareError("单块数据过大")
        return self.write_bytes(params, data)

    def _rehash(self, upload: _Upload) -> None:
        try:
            hasher = hashlib.sha256()
            with open_read(upload.part, True) as handle:
                info = os.fstat(handle.fileno())
                if (info.st_dev, info.st_ino) != upload.ident:
                    raise FileShareError("临时文件被替换，已放弃", "checksum")
                while True:
                    block = handle.read(CHUNK)
                    if not block:
                        break
                    hasher.update(block)
                    upload.hashed += len(block)
                    upload.touched = time.monotonic()
            upload.digest = hasher.hexdigest()
        except BaseException as exc:  # reported by finish
            upload.hash_error = exc

    def _place(self, upload: _Upload) -> Path:
        """Move the finished part onto its target, as ``on_exists`` says."""
        target = upload.target
        if upload.on_exists == "rename":
            target = self._free(target)
        elif upload.on_exists in ("error", "skip_same") and os.path.lexists(target):
            raise FileShareError(f"目标已存在：{target}", "exists")
        elif upload.on_exists == "overwrite" and os.path.isdir(target):
            raise FileShareError(f"目标是文件夹：{target}", "exists")
        for attempt in range(4):
            try:
                os.replace(upload.part, target)
                return target
            except PermissionError:
                if not WINDOWS or attempt == 3:
                    if WINDOWS:
                        raise FileShareError(f"目标文件被占用：{target}", "busy") from None
                    raise
                time.sleep(0.2)  # antivirus or an open handle on the target: try again
        return target

    @staticmethod
    def _settle(upload: _Upload) -> None:
        """The part's final mode (and owner) while its handle is still open: an overwrite keeps
        the replaced file's; a new file gets the source's mode under this process's umask."""
        if WINDOWS:
            return
        fd = upload.handle.fileno()
        existing = None
        if upload.on_exists == "overwrite":
            try:
                info = os.lstat(upload.target)
                existing = info if stat_mod.S_ISREG(info.st_mode) else None
            except OSError:
                existing = None
        try:
            if existing is not None:
                os.fchmod(fd, stat_mod.S_IMODE(existing.st_mode))
                upload.give = False
                if (existing.st_uid, existing.st_gid) != (os.geteuid(), os.getegid()):
                    try:
                        os.fchown(fd, existing.st_uid, existing.st_gid)
                    except OSError:
                        pass
            else:
                mode = upload.mode if upload.mode is not None else 0o666
                os.fchmod(fd, mode & 0o777 & ~UMASK)
        except (OSError, AttributeError):
            pass

    def _verify_part(self, upload_id: str, upload: _Upload) -> None:
        """The part on disk is still the file this upload wrote (not swapped for a link)."""
        try:
            info = os.lstat(upload.part)
            same = stat_mod.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == upload.ident
        except OSError:
            same = False
        if not same:
            with self._mutex:
                self.uploads.pop(upload_id, None)
            raise FileShareError("临时文件被替换或删除，已放弃", "checksum")

    def finish(self, params: dict) -> dict:
        upload_id, upload = self._upload(params)
        if upload.size is not None and upload.written != upload.size:
            with self._mutex:
                self.uploads.pop(upload_id, None)
            upload.handle.close()
            upload.part.unlink(missing_ok=True)
            raise FileShareError(f"文件不完整：收到 {upload.written} / {upload.size} 字节")
        expected = params.get("sha256")
        checked = isinstance(expected, str) and bool(expected)
        if upload.resumed and not checked:
            # the bytes before the resume came from an earlier attempt: without the source's digest
            # nothing proves they belong to this file. Start over (the caller sends it from 0).
            self._discard(upload_id, discard=True)
            raise FileShareError("续传的文件缺少源文件的 sha256，无法校验，需从头重传", "unverified")
        if not upload.handle.closed:
            self._settle(upload)
            try:
                upload.handle.close()
            except OSError as exc:
                self._discard(upload_id, discard=True)
                raise _os_error(exc, "写入失败") from None
        if upload.resumed:
            # the bytes written before the resume were never seen here: hash the whole part
            if upload.hash_thread is None:
                upload.hash_thread = threading.Thread(target=self._rehash, args=(upload,), daemon=True)
                upload.hash_thread.start()
            wait = params.get("wait_s")
            upload.hash_thread.join(None if wait is None else max(0.0, float(wait)))
            if upload.hash_thread.is_alive():
                return {"state": "hashing", "hashed": upload.hashed, "size": upload.written}
            if upload.hash_error is not None:
                self._discard(upload_id, discard=True)
                error = upload.hash_error
                if isinstance(error, FileShareError):
                    raise error
                raise _os_error(error, "校验读取失败") if isinstance(error, OSError) else FileShareError(str(error))
            digest = upload.digest or ""
        else:
            digest = upload.hasher.hexdigest()
        self._verify_part(upload_id, upload)
        with self._mutex:
            self.uploads.pop(upload_id, None)
        if checked and expected.lower() != digest:
            upload.part.unlink(missing_ok=True)
            raise FileShareError("校验失败：sha256 不一致", "checksum")
        try:
            target = self._place(upload)
        except FileShareError:
            upload.part.unlink(missing_ok=True)
            raise
        except OSError as exc:
            upload.part.unlink(missing_ok=True)
            raise _os_error(exc, "保存失败") from None
        mtime_ns = params.get("mtime_ns", upload.mtime_ns)
        if mtime_ns is not None:
            try:
                os.utime(target, ns=(int(mtime_ns), int(mtime_ns)))
            except (OSError, ValueError, OverflowError):
                pass
        if upload.give:
            self._give(target)
        return {"path": str(target), "size": upload.written, "sha256": digest, "checked": checked}

    def abort(self, params: dict) -> dict:
        self._discard(str(params.get("id") or ""), discard=bool(params.get("discard")))
        return {"ok": True}

    def _discard(self, upload_id: str, discard: bool = False) -> None:
        """Drop an upload. A keyed one (``resume_key``) keeps its part unless ``discard``."""
        with self._mutex:
            upload = self.uploads.pop(upload_id, None)
        if upload is None:
            return
        try:
            upload.handle.close()
        except OSError:
            pass
        finally:
            if discard or not upload.keep:
                upload.part.unlink(missing_ok=True)

    @staticmethod
    def _free(target: Path) -> Path:
        if not target.exists():
            return target
        stem, suffix = target.stem, target.suffix
        for number in range(1, 10000):
            candidate = target.with_name(f"{stem} ({number}){suffix}")
            if not candidate.exists():
                return candidate
        raise FileShareError("同名文件太多")

    def _give(self, path: Path) -> None:
        """A file the root core wrote into a person's folder belongs to that person (Linux)."""
        account = self.account()
        if account is None or WINDOWS or getattr(account, "session", None) is not None:
            return
        try:
            os.chown(path, account.uid, account.gid, follow_symlinks=False)
        except (OSError, AttributeError, NotImplementedError):
            pass

    def close_all(self) -> None:
        for upload_id in list(self.uploads):
            self._discard(upload_id)

    # -- reverse RPC (the relay path) ---------------------------------------------------
    def handlers(self) -> dict:
        def wrap(fn):
            async def handler(params: dict) -> dict:
                import asyncio
                try:
                    return await asyncio.to_thread(fn, params if isinstance(params, dict) else {})
                except FileShareError as exc:
                    return exc.body()
                except OSError as exc:
                    return _os_error(exc, type(exc).__name__).body()
                except (ValueError, TypeError) as exc:
                    return {"error": f"{type(exc).__name__}: {exc}"}
            return handler

        return {"fx_list": wrap(self.list), "fx_read": wrap(self.read), "fx_put_begin": wrap(self.begin),
                "fx_put_write": wrap(self.write), "fx_put_end": wrap(self.finish), "fx_put_abort": wrap(self.abort),
                # files/2
                "fx_walk": wrap(self.walk), "fx_stat_many": wrap(self.stat_many), "fx_mkdirs": wrap(self.mkdirs),
                "fx_hash": wrap(self.hash)}


def hash_file(path: Path, offset: int = 0, length: int | None = None,
              progress: Callable[[int], None] | None = None, nofollow: bool = False) -> tuple[str, int]:
    """sha256 of ``length`` bytes (all when None) from ``offset``; (hex digest, bytes read)."""
    hasher = hashlib.sha256()
    read = 0
    try:
        with open_read(path, nofollow) as handle:
            handle.seek(offset)
            while length is None or read < length:
                want = CHUNK if length is None else min(CHUNK, length - read)
                block = handle.read(want)
                if not block:
                    break
                hasher.update(block)
                read += len(block)
                if progress is not None:
                    progress(read)
    except FileNotFoundError:
        raise FileShareError(f"不是文件：{path}（不存在）", "missing") from None
    except OSError as exc:
        raise _os_error(exc, "读取失败") from None
    return hasher.hexdigest(), read
