"""Chat attachments (docs/agent-chat.md §18): files the console uploads to the device where the CLI runs.

They land in the run's managed temp folder, ``<root>/.anx/agents/tmp/<rid>/attachments/`` (a new chat that has no
run yet first gets a *stage* folder of the same shape, which ``agent_run_open`` then uses as the run's own), so they
share its owner (the run-as account), its listing in 临时目录, its 7-day / 2 GB sweep and its removal with the
session. ``agent_attach_put`` writes them in chunks (base64, at most :data:`CHUNK_MAX` raw bytes: under the
gateway's 1 MiB request limit), part file first, renamed when the last byte arrived.

The run-as account owns the run folder, so it could swap ``attachments`` for a link: every write and read here
walks the folders with ``O_NOFOLLOW`` directory descriptors (POSIX) and only ever opens regular files.

How each CLI gets them is the adapters' business (adapters.py: Codex ``--image``, Claude image blocks, ACP image
blocks); whatever is not delivered inline is named in the message text (:func:`name_files`)."""
from __future__ import annotations

import base64
import binascii
import errno
import os
import re
import secrets
import stat as stat_mod
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    import fcntl as _fcntl
except ImportError:  # Windows
    _fcntl = None

WINDOWS = sys.platform == "win32"
FOLDER = "attachments"
#: Raw bytes per ``agent_attach_put`` call (base64 in JSON stays under the gateway's 1 MiB body limit).
CHUNK_MAX = 512 * 1024
FILE_MAX = 50 * 1024 * 1024
MESSAGE_FILES = 10
MESSAGE_MAX = 100 * 1024 * 1024
FOLDER_MAX = 500 * 1024 * 1024
#: An upload that got no chunk for this long is forgotten (its part file removed).
UPLOAD_IDLE_S = 3600.0
STAGE_TTL_S = 24 * 3600.0
NAME_MAX = 120
#: Images a CLI can take inline (Claude image blocks, Codex --image, ACP image blocks).
INLINE_IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                 ".webp": "image/webp"}
#: Claude's API takes at most 5 MB of base64 per image: 3.75 MB raw.
CLAUDE_IMAGE_MAX = 3_932_160
ACP_IMAGE_MAX = 7 * 1024 * 1024
#: The text of a message that only has attachments.
EMPTY_PROMPT = "请查看附件。"
_MIME = {**INLINE_IMAGES, ".bmp": "image/bmp", ".svg": "image/svg+xml", ".pdf": "application/pdf",
         ".txt": "text/plain", ".md": "text/markdown", ".json": "application/json", ".csv": "text/csv",
         ".log": "text/plain", ".zip": "application/zip", ".html": "text/html", ".xml": "application/xml"}
# commas: codex splits --image values on ","; % ! ^ & | < > ": a Windows .cmd launcher (runner.command_line)
_BAD = re.compile(r'[\x00-\x1f\x7f,%!^&|<>"*?:/\\;`$\']')
_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$", re.IGNORECASE)
_O_DIR = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)
#: The run-as account owns the folder and can swap a file for a FIFO: a blocking open of one would hang the
#: worker (and every upload waiting on the store's lock), so files are opened non-blocking and fstat'ed first.
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_FD_WALK = not WINDOWS and bool(_O_NOFOLLOW) and os.open in getattr(os, "supports_dir_fd", set())


class AttachError(Exception):
    def __init__(self, message: str, code: str = "bad_attachment") -> None:
        super().__init__(message)
        self.code = code


def mime_of(name: str) -> str:
    return _MIME.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


def is_inline_image(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in INLINE_IMAGES


def safe_name(name: Any) -> str:
    """A file name every CLI and launcher takes: no separators, control or shell-special characters, no commas
    (codex ``--image``), no whitespace (the chat finds a path in a message up to the first space, so a named
    attachment stays a clickable file link), no leading dot or dash, at most :data:`NAME_MAX` characters, the
    extension kept."""
    text = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    text = _BAD.sub("_", text).strip().strip(".")
    text = re.sub(r"\s+", "_", text).lstrip("-_ ") or "file"
    stem, ext = os.path.splitext(text)
    ext = ext[:16]
    stem = stem[:max(1, NAME_MAX - len(ext))] or "file"
    if _RESERVED.match(stem):
        stem = f"_{stem}"
    return f"{stem}{ext}"


def _numbered(name: str, n: int) -> str:
    stem, ext = os.path.splitext(name)
    return f"{stem}-{n}{ext}"   # not " (n)": spaces and brackets end a path the chat finds in a message


def name_files(text: str, files: Iterable[dict]) -> str:
    """``text`` plus a list of the uploaded paths (what a CLI did not get inline)."""
    files = [f for f in files if f.get("path")]
    if not files:
        return text
    lines = "\n".join(f"- {f['path']}" for f in files)
    note = f"附件（已上传到这台设备，可以直接读取）：\n{lines}"
    return f"{text}\n\n{note}" if text.strip() else note


def public(files: Iterable[dict]) -> list[dict]:
    """What the run's ``user`` event shows of the attachments (never their bytes)."""
    return [{k: f[k] for k in ("path", "name", "size", "mime") if k in f} for f in files]


# ------------------------------------------------------------------------------------------ the folder walk

class _Dir:
    """``<base>/<rid>/attachments`` opened without following links (POSIX); a plain path on Windows."""

    def __init__(self, base: Path, rid: str, *, create: bool, owner: tuple[int, int] | None = None) -> None:
        self.path = Path(base) / rid / FOLDER
        self.fd: int | None = None
        if not _FD_WALK:
            run_dir = Path(base) / rid
            if run_dir.is_symlink() or not run_dir.is_dir():
                raise AttachError("这个对话的临时目录不存在", "not_found")
            if self.path.is_symlink():
                raise AttachError("附件目录不对", "bad_attachment")
            if create:
                self.path.mkdir(exist_ok=True)
            elif not self.path.is_dir():
                raise AttachError("找不到附件", "not_found")
            return
        base_fd = os.open(str(base), os.O_RDONLY | _O_DIR)
        try:
            try:
                run_fd = os.open(rid, os.O_RDONLY | _O_DIR | _O_NOFOLLOW, dir_fd=base_fd)
            except OSError as exc:
                raise AttachError("这个对话的临时目录不存在", "not_found") from exc
        finally:
            os.close(base_fd)
        try:
            if create:
                try:
                    os.mkdir(FOLDER, 0o700, dir_fd=run_fd)
                except FileExistsError:
                    pass
            try:
                self.fd = os.open(FOLDER, os.O_RDONLY | _O_DIR | _O_NOFOLLOW, dir_fd=run_fd)
            except OSError as exc:
                raise AttachError("找不到附件" if not create else "附件目录不对", "not_found") from exc
        finally:
            os.close(run_fd)
        if owner is not None:
            try:
                os.fchown(self.fd, *owner)
            except OSError:
                pass

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> "_Dir":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def open(self, name: str, flags: int, mode: int = 0o600) -> int:
        """A regular file only (AttachError otherwise); never blocks on a FIFO or device swapped in."""
        flags |= _O_NOFOLLOW | _O_BINARY | _O_NONBLOCK
        if self.fd is not None:
            try:
                fd = os.open(name, flags, mode, dir_fd=self.fd)
            except OSError as exc:
                if exc.errno in (errno.ENXIO, errno.ELOOP, errno.EWOULDBLOCK):   # FIFO with no reader / a link
                    raise AttachError("附件不是普通文件", "bad_attachment") from exc
                raise
        else:
            if (self.path / name).is_symlink():
                raise AttachError("附件不是普通文件", "bad_attachment")
            fd = os.open(str(self.path / name), flags, mode)
        try:
            if not stat_mod.S_ISREG(os.fstat(fd).st_mode):
                raise AttachError("附件不是普通文件", "bad_attachment")
            if _O_NONBLOCK and _fcntl is not None:
                _fcntl.fcntl(fd, _fcntl.F_SETFL, _fcntl.fcntl(fd, _fcntl.F_GETFL) & ~_O_NONBLOCK)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def lstat(self, name: str) -> os.stat_result:
        if self.fd is not None:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        return os.lstat(self.path / name)

    def rename(self, old: str, new: str) -> None:
        if self.fd is not None:
            os.rename(old, new, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        else:
            os.rename(self.path / old, self.path / new)

    def unlink(self, name: str) -> None:
        if self.fd is not None:
            os.unlink(name, dir_fd=self.fd)
        else:
            os.unlink(self.path / name)

    def names(self) -> list[str]:
        try:
            return os.listdir(self.fd) if self.fd is not None else os.listdir(self.path)
        except OSError:
            return []

    def used(self) -> int:
        total = 0
        for name in self.names():
            try:
                info = self.lstat(name)
            except OSError:
                continue
            if stat_mod.S_ISREG(info.st_mode):
                total += info.st_size
        return total


def _split(base: Path, path: Any) -> tuple[str, str]:
    """``(rid, name)`` of ``<base>/<rid>/attachments/<name>``; AttachError for anything else."""
    if not isinstance(path, str) or not path or "\x00" in path:
        raise AttachError("附件的路径不对")
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(str(base)))
    parts = Path(rel).parts
    if len(parts) != 3 or parts[1] != FOLDER or parts[0] in (".", "..") or parts[2] in (".", "..") \
            or parts[2].startswith("."):
        raise AttachError("附件必须是这个对话上传的文件")
    return parts[0], parts[2]


# ------------------------------------------------------------------------------------------ the store

class Attachments:
    """``base``: ``.anx/agents/tmp``. ``owner()`` -> ``(uid, gid)`` the files are handed to (root serving a run-as
    account), or None."""

    def __init__(self, base: Path, *, owner: Callable[[], tuple[int, int] | None] = lambda: None) -> None:
        self.base = Path(base)
        self.owner = owner
        self.lock = threading.RLock()
        #: upload id -> {rid, part, name, display, size, have, at}
        self.uploads: dict[str, dict] = {}

    def _owner(self) -> tuple[int, int] | None:
        if WINDOWS or not hasattr(os, "geteuid") or os.geteuid() != 0:
            return None
        try:
            return self.owner()
        except Exception:  # noqa: BLE001
            return None

    def _expire(self) -> None:
        now = time.monotonic()
        for upload_id, item in list(self.uploads.items()):
            if now - item["at"] > UPLOAD_IDLE_S:
                self.uploads.pop(upload_id, None)
                self._remove_part(item)

    def _remove_part(self, item: dict) -> None:
        try:
            with _Dir(self.base, item["rid"], create=False) as folder:
                folder.unlink(item["part"])
        except (OSError, AttachError):
            pass

    def put(self, rid: str, params: dict) -> dict:
        """One chunk: ``{name, size, offset, data, id?}``; offset 0 without ``id`` starts an upload. Returns
        ``{id, have, size, done}`` and, once done, ``{path, name, mime}``."""
        try:
            offset = int(params.get("offset") or 0)
            size = int(params.get("size"))
        except (TypeError, ValueError) as exc:
            raise AttachError("附件的大小或位置不对", "bad_request") from exc
        try:
            data = base64.b64decode(str(params.get("data") or ""), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AttachError("附件数据不对", "bad_request") from exc
        if len(data) > CHUNK_MAX:
            raise AttachError("一次上传的数据太多", "too_large")
        upload_id = params.get("id")
        with self.lock:
            self._expire()
            if not upload_id:
                if offset != 0:
                    raise AttachError("上传要从头开始", "bad_request")
                if size < 0 or size > FILE_MAX:
                    raise AttachError(f"单个附件不能超过 {FILE_MAX // (1024 * 1024)} MB", "too_large")
                display = str(params.get("name") or "file")[:255]
                name = safe_name(display)
                with _Dir(self.base, rid, create=True, owner=self._owner()) as folder:
                    if folder.used() + size > FOLDER_MAX:
                        raise AttachError(f"这个对话的附件已经超过 {FOLDER_MAX // (1024 * 1024)} MB", "too_large")
                    upload_id = secrets.token_hex(8)
                    part = f".{upload_id}.part"
                    fd = folder.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                    try:
                        owner = self._owner()
                        if owner is not None:
                            try:
                                os.fchown(fd, *owner)  # the CLI runs as the run-as account and reads it
                            except OSError:
                                pass
                    finally:
                        os.close(fd)
                item = {"rid": rid, "part": part, "name": name, "display": display, "size": size, "have": 0,
                        "at": time.monotonic()}
                self.uploads[upload_id] = item
            else:
                item = self.uploads.get(str(upload_id))
                if item is None or item["rid"] != rid:
                    raise AttachError("这个上传已经过期，请重新添加附件", "not_found")
            if offset != item["have"]:
                raise AttachError("上传的位置不连续，请重新添加附件", "bad_offset")
            if item["have"] + len(data) > item["size"]:
                raise AttachError("上传的数据比声明的大小多", "bad_request")
            with _Dir(self.base, rid, create=False) as folder:
                fd = folder.open(item["part"], os.O_WRONLY)
                try:
                    info = os.fstat(fd)
                    if not stat_mod.S_ISREG(info.st_mode) or info.st_size != item["have"]:
                        raise AttachError("上传的文件被改动了，请重新添加附件", "bad_attachment")
                    os.lseek(fd, item["have"], os.SEEK_SET)
                    view = memoryview(data)
                    while view:
                        written = os.write(fd, view)
                        view = view[written:]
                finally:
                    os.close(fd)
                item["have"] += len(data)
                item["at"] = time.monotonic()
                result = {"id": upload_id, "have": item["have"], "size": item["size"], "done": False}
                if item["have"] < item["size"]:
                    return result
                final = item["name"]
                n = 1
                while True:
                    try:
                        folder.lstat(final)
                    except FileNotFoundError:
                        break
                    n += 1
                    final = _numbered(item["name"], n)
                folder.rename(item["part"], final)
                self.uploads.pop(upload_id, None)
            return {**result, "done": True, "path": str(self.base / rid / FOLDER / final), "name": final,
                    "display": item["display"], "mime": mime_of(final)}

    def drop(self, rid: str, params: dict) -> dict:
        """Remove an uploaded file (``path``) or an unfinished upload (``id``) before it is sent."""
        with self.lock:
            upload_id = params.get("id")
            if upload_id and str(upload_id) in self.uploads:
                item = self.uploads.pop(str(upload_id))
                if item["rid"] == rid:
                    self._remove_part(item)
                    return {"ok": True}
            path = params.get("path")
            if not path:
                return {"ok": True}
            owner_rid, name = _split(self.base, path)
            if owner_rid != rid:
                raise AttachError("附件必须是这个对话上传的文件")
            try:
                with _Dir(self.base, rid, create=False) as folder:
                    info = folder.lstat(name)
                    if not stat_mod.S_ISREG(info.st_mode):
                        raise AttachError("附件不是普通文件")
                    folder.unlink(name)
            except FileNotFoundError:
                pass
            return {"ok": True}

    def check(self, files: Any, rids: Iterable[str]) -> list[dict]:
        """The attachments a message names (``[path | {path}]``), each a regular file uploaded to one of ``rids``:
        ``[{path, name, size, mime}]``. AttachError for anything else, or past the per-message limits."""
        if files is None:
            return []
        if not isinstance(files, list):
            raise AttachError("附件列表不对", "bad_request")
        if len(files) > MESSAGE_FILES:
            raise AttachError(f"一条消息最多 {MESSAGE_FILES} 个附件", "too_many")
        allowed = {r for r in rids if r}
        out: list[dict] = []
        total = 0
        for item in files:
            path = item.get("path") if isinstance(item, dict) else item
            rid, name = _split(self.base, path)
            if rid not in allowed:
                raise AttachError("附件必须是这个对话上传的文件")
            try:
                with _Dir(self.base, rid, create=False) as folder:
                    info = folder.lstat(name)
            except (OSError, AttachError) as exc:
                raise AttachError(f"附件 {name} 已经不在了，请重新添加", "not_found") from exc
            if not stat_mod.S_ISREG(info.st_mode):
                raise AttachError(f"附件 {name} 不是普通文件")
            total += info.st_size
            if total > MESSAGE_MAX:
                raise AttachError(f"一条消息的附件合计不能超过 {MESSAGE_MAX // (1024 * 1024)} MB", "too_large")
            full = str(self.base / rid / FOLDER / name)
            if any(f["path"] == full for f in out):
                continue
            out.append({"path": full, "name": name, "size": info.st_size, "mime": mime_of(name)})
        return out

    def move(self, files: list[dict], rid: str) -> list[dict]:
        """Files staged for a new chat that turned out to belong to a run already going (another window): into
        that run's own folder, where its CLI is allowed to read."""
        out = []
        with _Dir(self.base, rid, create=True, owner=self._owner()) as target:
            for f in files:
                src_rid, name = _split(self.base, f["path"])
                if src_rid == rid:
                    out.append(f)
                    continue
                final, n = name, 1
                while True:
                    try:
                        target.lstat(final)
                    except FileNotFoundError:
                        break
                    n += 1
                    final = _numbered(name, n)
                with _Dir(self.base, src_rid, create=False) as source:
                    if source.fd is not None and target.fd is not None:
                        os.rename(name, final, src_dir_fd=source.fd, dst_dir_fd=target.fd)
                    else:
                        os.rename(source.path / name, target.path / final)
                out.append({**f, "path": str(self.base / rid / FOLDER / final), "name": final})
        return out


def load_images(files: list[dict], limit: int) -> list[dict]:
    """``files`` with ``data`` (base64) filled in for the inline images of at most ``limit`` bytes; read without
    following links. ``limit`` 0: nothing is read."""
    out = []
    for f in files:
        f = dict(f)
        if limit and is_inline_image(f.get("name") or "") and 0 < int(f.get("size") or 0) <= limit:
            try:
                base = Path(f["path"]).parent.parent.parent
                rid, name = _split(base, f["path"])
                with _Dir(base, rid, create=False) as folder:
                    fd = folder.open(name, os.O_RDONLY)
                    try:
                        if stat_mod.S_ISREG(os.fstat(fd).st_mode):
                            chunks, left = [], limit + 1
                            while left > 0:
                                chunk = os.read(fd, min(left, 1 << 20))
                                if not chunk:
                                    break
                                chunks.append(chunk)
                                left -= len(chunk)
                            raw = b"".join(chunks)
                            if len(raw) <= limit:
                                f["data"] = base64.b64encode(raw).decode("ascii")
                    finally:
                        os.close(fd)
            except (OSError, AttachError, ValueError):
                pass
        out.append(f)
    return out


__all__ = [
    "ACP_IMAGE_MAX", "CHUNK_MAX", "CLAUDE_IMAGE_MAX", "EMPTY_PROMPT", "FILE_MAX", "FOLDER", "FOLDER_MAX",
    "MESSAGE_FILES", "MESSAGE_MAX", "AttachError", "Attachments", "is_inline_image", "load_images", "mime_of",
    "name_files", "public", "safe_name",
]
