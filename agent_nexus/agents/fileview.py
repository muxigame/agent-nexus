"""File views for the 对话 page (docs/agent-chat.md §17): ``agent_file_stat`` and ``agent_file_read``.

A message names a path (``src/app.py:42``, ``~/a.log``, ``C:\\x\\y.png``); the console asks the device where the
CLI ran (or the gateway context's device) whether it is a file, and reads it for a preview, an inline image or a
download. Paths are resolved here, by this device's own OS rules: ``~`` is the run-as account's HOME, a relative
path is joined to the folder the message was rendered with.

Same trust as the device's file share (``files/1``) and its terminal: the core process reads as itself. Agent
Nexus' own state (keys, device credentials) is refused, except the managed per-run tmp area (``.anx/agents/tmp``,
attachments and the agents' own temp files live there)."""
from __future__ import annotations

import base64
import mimetypes
import os
import re
import stat as stat_mod
from pathlib import Path
from typing import Any, Callable, Iterable

MAX_PATHS = 200
MAX_RAW = 1024
#: A ``bytes`` read returns at most this much (base64 in a JSON reply still fits the relays' limits).
READ_MAX = 1024 * 1024
#: A ``text`` read scans at most this much of the file.
TEXT_SCAN_MAX = 8 * 1024 * 1024
#: Lines returned around the asked line.
TEXT_WINDOW = 1000
SNIFF = 8192

IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
             ".webp": "image/webp", ".bmp": "image/bmp", ".svg": "image/svg+xml", ".ico": "image/x-icon",
             ".avif": "image/avif"}
#: Never "text" even without NUL bytes in the first 8 KB.
BINARY_EXT = {".pdf", ".zip", ".gz", ".tgz", ".tar", ".7z", ".rar", ".xz", ".bz2", ".exe", ".dll", ".so", ".dylib",
              ".bin", ".class", ".jar", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".mp3", ".mp4", ".mov",
              ".wav", ".flac", ".ogg", ".webm", ".mkv", ".woff", ".woff2", ".ttf", ".otf", ".sqlite", ".db"}
WINDOWS = os.name == "nt"
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
#: UNC / network and device paths: two leading separators (``\\\\host\\share``, ``//host/share``, ``\\\\?\\UNC\\``,
#: ``\\\\.\\pipe\\``). Touching one on Windows opens an SMB connection to the named host and sends the account's NTLM
#: credentials (or opens a device), so they are refused before any realpath or stat.
_NETWORK = re.compile(r"^[\\/]{2}")


class FileViewError(Exception):
    def __init__(self, message: str, code: str = "bad_request") -> None:
        super().__init__(message)
        self.code = code


def _error(message: str, code: str) -> dict:
    return {"error": message, "code": code}


class FileView:
    """``home()`` -> the run-as account's HOME; ``state_dirs()`` -> folders never read; ``allowed_dirs()`` ->
    folders inside those that may be read anyway (the managed tmp area)."""

    def __init__(self, home: Callable[[], Path], state_dirs: Callable[[], Iterable[str]],
                 allowed_dirs: Callable[[], Iterable[str]] = lambda: (), *, confined: bool = False) -> None:
        self.home = home
        self.state_dirs = state_dirs
        self.allowed_dirs = allowed_dirs
        #: Only the allowed (managed tmp) area may be read: remote callers of a device whose file share is off.
        self.confined = confined

    def confined_view(self) -> "FileView":
        """The same view, limited to the managed tmp area (attachments stay viewable, nothing else does)."""
        return FileView(self.home, self.state_dirs, self.allowed_dirs, confined=True)

    # ------------------------------------------------------------------ resolving
    @staticmethod
    def is_network(text: str) -> bool:
        """True for ``\\\\host\\share``, ``//host/share`` and ``\\\\?\\UNC\\host`` (any OS: such a string is never
        a local path a model may name on purpose, and on Windows a stat of it would leak credentials)."""
        return bool(_NETWORK.match(text))

    def resolve(self, raw: Any, base: Any = None) -> Path:
        if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_RAW or "\x00" in raw:
            raise FileViewError("路径无效", "bad_path")
        text = raw.strip()
        if text.lower().startswith("file://"):
            from urllib.parse import unquote
            text = unquote(re.sub(r"^file://(?:localhost)?", "", text, flags=re.I))
            if re.match(r"^/[A-Za-z]:/", text):
                text = text[1:]
            elif not text.startswith("/"):   # file://host/share/x names a network host
                raise FileViewError("不能打开网络路径", "network")
        if self.is_network(text):
            raise FileViewError("不能打开网络路径", "network")
        if text == "~" or text.startswith("~/") or text.startswith("~\\"):
            path = Path(self.home()) / text[2:] if len(text) > 1 else Path(self.home())
        elif os.path.isabs(text) or (WINDOWS and _DRIVE.match(text)):
            path = Path(text)
        elif not WINDOWS and _DRIVE.match(text):
            raise FileViewError("这是 Windows 路径，不在这台设备上", "missing")
        else:
            if not isinstance(base, str) or not base.strip():
                raise FileViewError("相对路径需要所在的目录", "bad_path")
            b = base.strip()
            if b == "~" or b.startswith("~/") or b.startswith("~\\"):
                b = str(Path(self.home()) / b[2:])
            if self.is_network(b):
                raise FileViewError("不能打开网络路径", "network")
            if not (os.path.isabs(b) or (WINDOWS and _DRIVE.match(b))):
                raise FileViewError("目录无效", "bad_path")
            path = Path(b) / text
        out = os.path.normpath(str(path))
        if self.is_network(out):
            raise FileViewError("不能打开网络路径", "network")
        return Path(out)

    def _refused(self, path: Path) -> bool:
        """True when ``path`` (links followed) is inside Agent Nexus' own state, outside the allowed tmp area."""
        try:
            real = os.path.realpath(path)
        except (OSError, ValueError):
            return True
        norm = os.path.normcase
        def inside(p: str, d: str) -> bool:
            d = norm(os.path.realpath(d))
            p = norm(p)
            return p == d or p.startswith(d.rstrip("\\/") + os.sep)
        if any(inside(real, d) for d in self.allowed_dirs()):
            return False
        if self.confined:
            return True
        return any(inside(real, d) for d in self.state_dirs())

    def _checked(self, raw: Any, base: Any) -> Path:
        path = self.resolve(raw, base)
        if self._refused(path):
            if self.confined:
                raise FileViewError("这台设备关闭了文件共享，只能查看对话的附件", "files_disabled")
            raise FileViewError("不能查看 Agent Nexus 自己的数据目录", "not_allowed")
        return path

    # ------------------------------------------------------------------ stat
    @staticmethod
    def kind_of(path: Path, head: bytes | None) -> tuple[str, str]:
        ext = path.suffix.lower()
        if ext in IMAGE_EXT:
            return "image", IMAGE_EXT[ext]
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ext in BINARY_EXT or (head is not None and b"\x00" in head):
            return "binary", mime
        if head is None:
            return "binary", mime
        return "text", mime if mime.startswith("text/") else "text/plain"

    def stat_one(self, raw: Any, base: Any) -> dict:
        out: dict[str, Any] = {"raw": raw if isinstance(raw, str) else ""}
        try:
            path = self._checked(raw, base)
        except FileViewError as exc:
            return {**out, "exists": False, "error": str(exc), "code": exc.code}
        out["path"] = str(path)
        try:
            st = os.stat(path)
        except (OSError, ValueError):
            return {**out, "exists": False}
        if stat_mod.S_ISDIR(st.st_mode):
            return {**out, "exists": True, "dir": True}
        if not stat_mod.S_ISREG(st.st_mode):
            return {**out, "exists": False, "code": "not_file"}
        head = None
        if path.suffix.lower() not in IMAGE_EXT:
            try:
                with open(path, "rb") as handle:
                    head = handle.read(SNIFF)
            except OSError:
                head = None
        kind, mime = self.kind_of(path, head)
        return {**out, "exists": True, "dir": False, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                "kind": kind, "mime": mime, "name": path.name}

    def op_stat(self, params: dict) -> dict:
        paths = params.get("paths")
        if not isinstance(paths, list) or not paths:
            return _error("缺少 paths", "bad_request")
        if len(paths) > MAX_PATHS:
            return _error(f"一次最多查 {MAX_PATHS} 个路径", "bad_request")
        base = params.get("base")
        return {"files": [self.stat_one(raw, base) for raw in paths]}

    # ------------------------------------------------------------------ read
    def op_read(self, params: dict) -> dict:
        try:
            path = self._checked(params.get("path"), params.get("base"))
        except FileViewError as exc:
            return _error(str(exc), exc.code)
        try:
            st = os.stat(path)
        except (OSError, ValueError):
            return _error(f"文件不存在：{path}", "missing")
        if not stat_mod.S_ISREG(st.st_mode):
            return _error(f"不是文件：{path}", "not_file")
        mode = params.get("mode") or "bytes"
        try:
            if mode == "text":
                return {"path": str(path), "name": path.name, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                        **self._text(path, params.get("line"))}
            if mode != "bytes":
                return _error("mode 只能是 text 或 bytes", "bad_request")
            try:
                offset = max(0, int(params.get("offset") or 0))
                length = int(params.get("length") if params.get("length") is not None else READ_MAX)
            except (TypeError, ValueError):
                return _error("offset / length 无效", "bad_request")
            length = max(0, min(length, READ_MAX))
            with open(path, "rb") as handle:
                handle.seek(offset)
                data = handle.read(length)
            kind, mime = self.kind_of(path, None if path.suffix.lower() in IMAGE_EXT else data[:SNIFF] if offset == 0 else None)
            return {"path": str(path), "name": path.name, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                    "offset": offset, "data": base64.b64encode(data).decode("ascii"), "mime": mime,
                    "eof": offset + len(data) >= st.st_size}
        except FileViewError as exc:
            return _error(str(exc), exc.code)
        except PermissionError:
            return _error(f"没有权限读取：{path}", "denied")
        except OSError as exc:
            return _error(f"读取失败：{exc}", "io")

    @staticmethod
    def decode(data: bytes) -> tuple[str, str]:
        for encoding in ("utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8", "gb18030"):
            try:
                return data.decode(encoding), encoding.replace("-sig", "")
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", "replace"), "utf-8"

    def _text(self, path: Path, line: Any) -> dict:
        with open(path, "rb") as handle:
            data = handle.read(TEXT_SCAN_MAX + 1)
        scanned_all = len(data) <= TEXT_SCAN_MAX
        data = data[:TEXT_SCAN_MAX]
        if b"\x00" in data[:SNIFF]:
            raise FileViewError("这是二进制文件", "binary")
        if not scanned_all:   # do not cut a multi-byte character in two
            cut = data.rfind(b"\n")
            data = data[:cut + 1] if cut > 0 else data
        text, encoding = self.decode(data)
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        try:
            want = max(1, int(line)) if line not in (None, "") else 1
        except (TypeError, ValueError):
            want = 1
        first = max(1, min(want, len(lines) or 1) - TEXT_WINDOW // 4)
        window = lines[first - 1:first - 1 + TEXT_WINDOW]
        return {"text": "\n".join(l.rstrip("\r") for l in window), "first_line": first, "total_lines": len(lines),
                "truncated": (not scanned_all) or first > 1 or first - 1 + TEXT_WINDOW < len(lines),
                "scanned_all": scanned_all, "encoding": encoding}

    def handlers(self) -> dict[str, Callable[[dict], dict]]:
        def guarded(fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
            def run(params: Any) -> dict:
                params = params if isinstance(params, dict) else {}
                try:
                    return fn(params)
                except FileViewError as exc:
                    return _error(str(exc), exc.code)
            return run
        return {"agent_file_stat": guarded(self.op_stat), "agent_file_read": guarded(self.op_read)}


__all__ = ["FileView", "FileViewError", "READ_MAX", "MAX_PATHS"]
