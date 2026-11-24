"""This device's files for the other devices of the network (``files/1``).

Two ways in, the same operations behind both:

* over the P2P tunnel, the remote hub's ``files`` service (``remote_hub.py``) streams a file in
  frames: the fast path;
* through the gateway when no tunnel can be punched, reverse RPC ``fx_*`` moves a file in
  chunks of at most ``CHUNK`` bytes (``files_bridge.py`` on the gateway).

Who may ask: exactly who may open a remote terminal here (another joined device of the owner's
network, or the owner on the gateway console), so the files are those of the account that
terminal would log in as (``accounts.terminal``). A shell there could read and write them
anyway; this only makes moving them convenient. An upload is written next to its target as
``.<name>.anxpart`` and renamed when complete, never over an existing file (`` (1)`` is added).
"""
from __future__ import annotations

import base64
import os
import secrets
import string
import sys
import time
from pathlib import Path
from typing import Any, Callable

FEATURE = "files/1"
#: largest chunk one ``fx_read`` / ``fx_put_write`` moves (before base64)
CHUNK = 1024 * 1024
#: frames on the P2P stream
STREAM_CHUNK = 256 * 1024
#: an upload nobody finishes is dropped after this long
UPLOAD_IDLE_S = 600.0
MAX_ENTRIES = 5000
WINDOWS = sys.platform == "win32"


class FileShareError(Exception):
    pass


class _Upload:
    def __init__(self, target: Path, part: Path, size: int | None):
        self.target = target
        self.part = part
        self.size = size
        self.written = 0
        self.handle = open(part, "wb")
        self.touched = time.monotonic()


class FileShare:
    def __init__(self, account: Callable[[], Any] | None = None):
        self.account = account or (lambda: None)
        self.uploads: dict[str, _Upload] = {}

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
    def _roots() -> list[str]:
        if not WINDOWS:
            return ["/"]
        return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]

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

    # -- read -------------------------------------------------------------------------
    def stat_file(self, params: dict) -> tuple[Path, int]:
        path = self._path(params.get("path"))
        if not path.is_file():
            raise FileShareError(f"不是文件：{path}")
        return path, path.stat().st_size

    def read(self, params: dict) -> dict:
        path, size = self.stat_file(params)
        offset = max(0, int(params.get("offset") or 0))
        length = max(0, min(int(params.get("length") or CHUNK), CHUNK))
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                data = handle.read(length)
        except OSError as exc:
            raise FileShareError(f"读取失败：{exc}") from None
        return {"data": base64.b64encode(data).decode("ascii"), "offset": offset, "size": size,
                "name": path.name, "eof": offset + len(data) >= size}

    # -- write ------------------------------------------------------------------------
    def _reap(self) -> None:
        now = time.monotonic()
        for upload_id, upload in list(self.uploads.items()):
            if now - upload.touched > UPLOAD_IDLE_S:
                self._discard(upload_id)

    @staticmethod
    def _clean_name(name: Any) -> str:
        text = str(name or "").strip().replace("\\", "/").split("/")[-1]
        if text in ("", ".", ".."):
            raise FileShareError("文件名无效")
        return text[:255]

    def begin(self, params: dict) -> dict:
        self._reap()
        directory = self._path(params.get("dir"), default=self.default_dir())
        name = self._clean_name(params.get("name"))
        size = params.get("size")
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise FileShareError(f"无法创建文件夹：{exc}") from None
        upload_id = secrets.token_urlsafe(12)
        part = directory / f".{name}.{upload_id}.anxpart"
        try:
            self.uploads[upload_id] = _Upload(directory / name, part, int(size) if size is not None else None)
        except OSError as exc:
            raise FileShareError(f"无法写入 {directory}：{exc}") from None
        return {"id": upload_id, "dir": str(directory), "name": name, "chunk": CHUNK}

    def _upload(self, params: dict) -> tuple[str, _Upload]:
        upload_id = str(params.get("id") or "")
        upload = self.uploads.get(upload_id)
        if upload is None:
            raise FileShareError("上传已失效（超时或已结束），请重新发送")
        upload.touched = time.monotonic()
        return upload_id, upload

    def write_bytes(self, params: dict, data: bytes) -> dict:
        upload_id, upload = self._upload(params)
        offset = params.get("offset")
        if offset is not None and int(offset) != upload.written:
            raise FileShareError(f"数据顺序不对：期望偏移 {upload.written}，收到 {offset}")
        try:
            upload.handle.write(data)
        except OSError as exc:
            self._discard(upload_id)
            raise FileShareError(f"写入失败：{exc}") from None
        upload.written += len(data)
        return {"written": upload.written}

    def write(self, params: dict) -> dict:
        data = base64.b64decode(str(params.get("data") or ""))
        if len(data) > CHUNK:
            raise FileShareError("单块数据过大")
        return self.write_bytes(params, data)

    def finish(self, params: dict) -> dict:
        upload_id, upload = self._upload(params)
        self.uploads.pop(upload_id, None)
        upload.handle.close()
        if upload.size is not None and upload.written != upload.size:
            upload.part.unlink(missing_ok=True)
            raise FileShareError(f"文件不完整：收到 {upload.written} / {upload.size} 字节")
        target = self._free(upload.target)
        try:
            os.replace(upload.part, target)
        except OSError as exc:
            upload.part.unlink(missing_ok=True)
            raise FileShareError(f"保存失败：{exc}") from None
        self._give(target)
        return {"path": str(target), "size": upload.written}

    def abort(self, params: dict) -> dict:
        self._discard(str(params.get("id") or ""))
        return {"ok": True}

    def _discard(self, upload_id: str) -> None:
        upload = self.uploads.pop(upload_id, None)
        if upload is None:
            return
        try:
            upload.handle.close()
        finally:
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
            os.chown(path, account.uid, account.gid)
        except (OSError, AttributeError):
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
                    return {"error": str(exc)}
                except (OSError, ValueError, TypeError) as exc:
                    return {"error": f"{type(exc).__name__}: {exc}"}
            return handler

        return {"fx_list": wrap(self.list), "fx_read": wrap(self.read), "fx_put_begin": wrap(self.begin),
                "fx_put_write": wrap(self.write), "fx_put_end": wrap(self.finish), "fx_put_abort": wrap(self.abort)}
