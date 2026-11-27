"""The file tools of an API model (docs/agent-chat.md §16.4): ``read_file``, ``list_dir``, ``glob``, ``grep``,
``write_file``, ``edit_file``.

Standard library only, so the same file also runs as a helper process *as the run-as account*
(``python -I fileops.py`` with one JSON request on stdin, one JSON line out): when the device service runs as
root / SYSTEM and the chat runs as another account, :class:`~.tools.Toolbox` never touches the user's paths with
the service's own rights (a symlink swapped in by the user then only reaches what the user could reach anyway).

Every path is confined to the work folder and the run's temp folder *after resolving symlinks*; anx's own state
folders (``<root>/.anx``: the API keys, the device credential) are refused even when they lie inside them. The
last path component is opened without following links (``O_NOFOLLOW``) and its parent is checked again right
before a write, and a file with more than one hard link is not written.
"""
from __future__ import annotations

import difflib
import fnmatch
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

READ_MAX_BYTES = 5 * 1024 * 1024
WRITE_MAX_BYTES = 5 * 1024 * 1024
READ_LINES = 2000
LINE_MAX = 2000
LIST_MAX = 500
GREP_MAX = 200
GREP_FILE_MAX = 2 * 1024 * 1024
DIFF_MAX = 20000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".hg", ".svn", ".mypy_cache", ".pytest_cache"}
FILE_TOOLS = ("read_file", "list_dir", "glob", "grep", "write_file", "edit_file")
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
BINARY = getattr(os, "O_BINARY", 0)


class ToolError(Exception):
    """A tool call that failed in a way the model should see (its message goes back as the result)."""


def trunc(text: Any, limit: int) -> str:
    text = text if isinstance(text, str) else ("" if text is None else str(text))
    return text if len(text) <= limit else text[:limit] + f"\n…（已截断，共 {len(text)} 个字符）"


def unified_diff(path: str, old: str, new: str) -> str:
    lines = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                 fromfile=f"a/{path}", tofile=f"b/{path}", n=2)
    return trunc("".join(lines), DIFF_MAX)


def _within(real: str, root: str) -> bool:
    if os.name == "nt":
        real, root = os.path.normcase(real), os.path.normcase(root)
    return real == root or real.startswith(root.rstrip(os.sep) + os.sep)


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


def _crlf(text: str) -> str:
    """``text`` with every line ending as ``\\r\\n`` (a CRLF file keeps its line endings)."""
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


class Files:
    """The file tools of one run: paths must be in ``roots`` (the work folder first) and not in ``deny``."""

    def __init__(self, workdir: str, roots: list[str] | None = None, deny: list[str] | None = None):
        self.workdir = os.path.realpath(workdir)
        self.roots = [self.workdir] + [os.path.realpath(r) for r in roots or [] if r and
                                       os.path.realpath(r) != self.workdir]
        self.deny = [os.path.realpath(d) for d in deny or [] if d]

    # -- paths
    def _allowed(self, real: str) -> bool:
        return any(_within(real, root) for root in self.roots) and not self._denied(real)

    def _denied(self, real: str) -> bool:
        return any(_within(real, d) for d in self.deny)

    def resolve(self, value: Any, *, default: str | None = None) -> Path:
        if value is None or (isinstance(value, str) and not value.strip()):
            if default is None:
                raise ToolError("缺少 path")
            value = default
        if not isinstance(value, str) or "\0" in value:
            raise ToolError("path 必须是文字")
        raw = os.path.expanduser(value.strip())
        path = raw if os.path.isabs(raw) else os.path.join(self.workdir, raw)
        real = os.path.realpath(path)
        if not any(_within(real, root) for root in self.roots):
            raise ToolError(f"路径不在工作目录或临时目录内（符号链接解析后）：{value}")
        if self._denied(real):
            raise ToolError(f"这个位置存放 Agent Nexus 自己的数据（含密钥），不能访问：{value}")
        return Path(real)

    def show(self, path: Path) -> str:
        text = str(path)
        if _within(text, self.workdir):
            rel = os.path.relpath(text, self.workdir)
            return "." if rel == "." else rel.replace(os.sep, "/")
        return text

    def confined(self, path: Path) -> bool:
        return self._allowed(os.path.realpath(path))

    # -- opening without following a link swapped in after resolve()
    def _recheck(self, path: Path) -> str:
        """The file's path with its folder resolved again now; refused when that left the allowed folders."""
        parent = os.path.realpath(path.parent)
        if not self._allowed(parent):
            raise ToolError("路径在检查之后变了（符号链接），已拒绝")
        return os.path.join(parent, path.name)

    def _read(self, path: Path, limit: int) -> bytes:
        fd = os.open(self._recheck(path), os.O_RDONLY | NOFOLLOW | BINARY)
        with os.fdopen(fd, "rb") as handle:
            return handle.read(limit + 1)

    def _write(self, path: Path, data: bytes) -> None:
        fd = os.open(self._recheck(path), os.O_WRONLY | os.O_CREAT | NOFOLLOW | BINARY, 0o666)
        try:
            info = os.fstat(fd)
            if info.st_nlink > 1:
                raise ToolError("这个文件有多个硬链接，为安全起见不写入")
            os.ftruncate(fd, 0)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)

    # -- dispatch
    def run(self, name: str, args: Any) -> dict:
        """``{output, error, events}`` of one call (never raises for the model's mistakes)."""
        handler = getattr(self, f"_t_{name}", None) if name in FILE_TOOLS else None
        if handler is None:
            return {"output": f"没有这个工具：{name}", "error": True, "events": []}
        args = args if isinstance(args, dict) else {}
        try:
            output, events = handler(args)
            return {"output": output, "error": False, "events": events}
        except ToolError as exc:
            return {"output": str(exc), "error": True, "events": []}
        except UnicodeDecodeError:
            return {"output": "文件不是 UTF-8 文本", "error": True, "events": []}
        except OSError as exc:
            if getattr(exc, "errno", None) in (40, 62) and NOFOLLOW:  # ELOOP (Linux 40, BSD/macOS 62): a link
                return {"output": "路径的最后一段是符号链接，已拒绝", "error": True, "events": []}
            return {"output": f"{exc.strerror or exc}：{exc.filename or ''}".rstrip("："), "error": True, "events": []}

    # -- read tools
    def _t_read_file(self, args: dict) -> tuple[str, list]:
        path = self.resolve(args.get("path"))
        if path.is_dir():
            raise ToolError(f"{self.show(path)} 是目录，用 list_dir")
        data = self._read(path, READ_MAX_BYTES)
        if len(data) > READ_MAX_BYTES:
            raise ToolError(f"文件太大（超过 {READ_MAX_BYTES // 1024 // 1024} MB）")
        if b"\0" in data[:8192]:
            raise ToolError("这是二进制文件")
        lines = data.decode("utf-8", "replace").splitlines()
        offset = max(1, _int(args.get("offset"), 1))
        limit = max(1, min(_int(args.get("limit"), READ_LINES), READ_LINES))
        chunk = lines[offset - 1:offset - 1 + limit]
        body = "\n".join(f"{n:>6}\t{line[:LINE_MAX]}" for n, line in enumerate(chunk, offset))
        more = len(lines) - (offset - 1 + len(chunk))
        if more > 0:
            body += f"\n…（还有 {more} 行，用 offset={offset + len(chunk)} 继续读）"
        if not lines:
            body = "（空文件）"
        return body, []

    def _t_list_dir(self, args: dict) -> tuple[str, list]:
        path = self.resolve(args.get("path"), default=".")
        if not path.is_dir():
            raise ToolError(f"不是目录：{self.show(path)}")
        entries = []
        for entry in sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower())):
            entries.append(entry.name + ("/" if entry.is_dir() else ""))
        more = len(entries) - LIST_MAX
        text = "\n".join(entries[:LIST_MAX]) or "（空目录）"
        return text + (f"\n…（还有 {more} 项）" if more > 0 else ""), []

    def _t_glob(self, args: dict) -> tuple[str, list]:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            raise ToolError("缺少 pattern")
        pattern = pattern.strip().replace("\\", "/")
        if pattern.startswith("/") or re.match(r"^[A-Za-z]:", pattern) or ".." in pattern.split("/"):
            raise ToolError("pattern 必须是相对路径，不能含 ..")
        base = self.resolve(args.get("path"), default=".")
        found = []
        for match in base.glob(pattern):
            if any(part in SKIP_DIRS for part in match.relative_to(base).parts[:-1]):
                continue
            if self.confined(match):
                found.append(self.show(match) + ("/" if match.is_dir() else ""))
            if len(found) > LIST_MAX:
                break
        found.sort()
        more = len(found) > LIST_MAX
        return ("\n".join(found[:LIST_MAX]) + ("\n…（结果太多，只列出前 500 个）" if more else "") if found
                else "没有匹配的文件"), []

    def _t_grep(self, args: dict) -> tuple[str, list]:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ToolError("缺少 pattern")
        try:
            regex = re.compile(pattern, re.IGNORECASE if args.get("ignore_case") else 0)
        except re.error as exc:
            raise ToolError(f"正则表达式不对：{exc}") from exc
        name_glob = args.get("glob") if isinstance(args.get("glob"), str) and args.get("glob") else None
        base = self.resolve(args.get("path"), default=".")
        files: list[Path] = []
        if base.is_file():
            files = [base]
        else:
            for folder, dirs, names in os.walk(base):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS
                                 and not self._denied(os.path.realpath(os.path.join(folder, d))))
                for name in sorted(names):
                    if name_glob and not fnmatch.fnmatch(name, name_glob):
                        continue
                    files.append(Path(folder) / name)
        hits: list[str] = []
        for path in files:
            if len(hits) >= GREP_MAX:
                break
            try:
                if not self.confined(path) or path.stat().st_size > GREP_FILE_MAX:
                    continue
                data = self._read(Path(os.path.realpath(path)), GREP_FILE_MAX)
            except (OSError, ToolError):
                continue
            if b"\0" in data[:4096]:
                continue
            for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{self.show(path)}:{number}: {line.strip()[:300]}")
                    if len(hits) >= GREP_MAX:
                        break
        if not hits:
            return "没有匹配", []
        return "\n".join(hits) + (f"\n…（只列出前 {GREP_MAX} 处）" if len(hits) >= GREP_MAX else ""), []

    # -- write tools
    def _t_write_file(self, args: dict) -> tuple[str, list]:
        path = self.resolve(args.get("path"))
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("content 必须是文字")
        if path.is_dir():
            raise ToolError(f"{self.show(path)} 是目录")
        existed = path.exists()
        old = self._read(path, WRITE_MAX_BYTES).decode("utf-8", "replace") if existed else ""
        if existed and "\r\n" in old and "\n" in content and "\r\n" not in content:
            content = _crlf(content)  # overwriting a CRLF file keeps its line endings
        data = content.encode("utf-8")
        if len(data) > WRITE_MAX_BYTES:
            raise ToolError("内容太大")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write(path, data)
        shown = self.show(path)
        return f"已写入 {shown}（{len(data)} 字节）", [{"type": "file_change", "path": shown,
                                                    "kind": "edit" if existed else "add",
                                                    "diff": unified_diff(shown, old, content)}]

    def _t_edit_file(self, args: dict) -> tuple[str, list]:
        path = self.resolve(args.get("path"))
        old, new = args.get("old_string"), args.get("new_string")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ToolError("old_string 不能为空，new_string 必须是文字")
        if not path.is_file():
            raise ToolError(f"文件不存在：{self.show(path)}")
        raw = self._read(path, WRITE_MAX_BYTES)
        if len(raw) > WRITE_MAX_BYTES:
            raise ToolError("文件太大")
        text = raw.decode("utf-8")
        count = text.count(old)
        if count == 0 and "\r\n" in text and "\n" in old:
            # read_file shows lines without their \r: the model writes \n; match (and write) the file's CRLF
            old, new = _crlf(old), _crlf(new)
            count = text.count(old)
        if count == 0:
            raise ToolError("文件里找不到 old_string（要逐字一致，包括空白和缩进）")
        if count > 1 and not args.get("replace_all"):
            raise ToolError(f"old_string 出现了 {count} 次：多带一些上下文让它唯一，或者设 replace_all")
        updated = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
        self._write(path, updated.encode("utf-8"))
        shown = self.show(path)
        return f"已修改 {shown}（替换 {count if args.get('replace_all') else 1} 处）", [
            {"type": "file_change", "path": shown, "kind": "edit", "diff": unified_diff(shown, text, updated)}]


def main() -> int:
    """The helper: ``{workdir, roots, deny, name, args}`` on stdin -> ``{output, error, events}`` on stdout."""
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        files = Files(request["workdir"], request.get("roots") or [], request.get("deny") or [])
        result = files.run(str(request.get("name") or ""), request.get("args"))
    except Exception as exc:  # noqa: BLE001 - always one answer
        result = {"output": f"文件工具出错了：{exc}", "error": True, "events": []}
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=True) + "\n").encode("ascii"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
