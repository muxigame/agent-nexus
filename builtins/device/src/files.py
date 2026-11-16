"""The ``files`` capability: list, stat, read and write files under the policy's allowed roots.

These are the contract's portable file tools (design §5.2). On a PC the shell
does the same and more; they exist so that every device -- including ones
without a shell -- answers the same four calls. Every path goes through
``Policy.resolve_path``, so ``allowed_roots`` bounds them exactly as it bounds
``exec_command``'s working directory.
"""
from __future__ import annotations

import fnmatch
import os
import stat as stat_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .policy import Policy

WRITE_MODES = ("overwrite", "append", "create")
MAX_LIST_ENTRIES = 1000
DEFAULT_READ_LINES = 2000
MAX_WRITE_CHARS = 10_000_000


class FileToolError(ValueError):
    pass


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _kind(mode: int) -> str:
    if stat_mod.S_ISDIR(mode):
        return "dir"
    if stat_mod.S_ISREG(mode):
        return "file"
    if stat_mod.S_ISLNK(mode):
        return "symlink"
    return "other"


def file_stat(policy: Policy, path: str) -> dict[str, Any]:
    target = policy.resolve_path(path)
    try:
        info = target.stat()
    except FileNotFoundError:
        return {"status": "ok", "path": str(target), "exists": False}
    return {"status": "ok", "path": str(target), "exists": True, "type": _kind(info.st_mode),
            "size": info.st_size, "modified": _iso(info.st_mtime),
            "readonly": not os.access(target, os.W_OK)}


def file_list(policy: Policy, path: str | None, pattern: str | None, limit: int) -> dict[str, Any]:
    target = policy.resolve_path(path or ".")
    if not target.is_dir():
        raise FileToolError(f"not a directory: {target}")
    limit = max(1, min(int(limit), MAX_LIST_ENTRIES))
    entries, total = [], 0
    for entry in sorted(os.scandir(target), key=lambda item: (not item.is_dir(follow_symlinks=False), item.name.lower())):
        if pattern and not fnmatch.fnmatch(entry.name, pattern):
            continue
        total += 1
        if len(entries) >= limit:
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            entries.append({"name": entry.name, "type": "other"})
            continue
        item = {"name": entry.name, "type": _kind(info.st_mode), "modified": _iso(info.st_mtime)}
        if item["type"] == "file":
            item["size"] = info.st_size
        entries.append(item)
    return {"status": "ok", "path": str(target), "entries": entries, "total": total, "truncated": total > len(entries)}


MAX_READ_BYTES = 64 * 1024 * 1024
BOM = b"\xef\xbb\xbf"


def decode_text(data: bytes) -> tuple[str, str]:
    """(text, encoding) for file bytes: UTF-8 (BOM dropped) or, failing that, GB18030.

    GB18030 is the fallback because the Windows machines this runs on default to
    the Chinese code page, and it decodes the GBK files they produce.
    """
    if data.startswith(BOM):
        return data[len(BOM):].decode("utf-8", errors="replace"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gb18030"), "gb18030"
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "utf-8 (invalid bytes replaced)"


def file_read(policy: Policy, path: str, offset: int, limit: int, max_chars: int,
              line_numbers: bool = False) -> dict[str, Any]:
    """Lines ``offset`` (0-based) .. ``offset+limit`` of a text file, capped at ``max_chars``.

    Content always uses LF line endings; ``line_ending`` says when the file uses
    CRLF (``edit_file`` and ``patch_file`` keep the file's own style). A single
    line longer than ``max_chars`` is cut and reported in ``long_line``, so a
    minified file still makes progress instead of returning nothing.
    """
    target = policy.resolve_path(path)
    if not target.is_file():
        raise FileToolError(f"not a file: {target}")
    if offset < 0 or limit < 1:
        raise FileToolError("offset must be >= 0 and limit >= 1")
    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        raise FileToolError(f"{target} is {size} bytes; read it in pieces with exec_command (head, tail, sed -n)")
    data = target.read_bytes()
    if b"\0" in data[:8192]:
        raise FileToolError(f"{target} looks binary; use view_image for images or exec_command for other tools")
    text, encoding = decode_text(data)
    crlf = "\r\n" in text
    if crlf:
        text = text.replace("\r\n", "\n")
    # Split on LF only (not str.splitlines, which also breaks at \f, \x1c and
    # U+2028), so line numbers agree with edit_file and patch_file.
    all_lines = text.split("\n")
    if all_lines[-1] == "":
        all_lines.pop()
    all_lines = [line + "\n" for line in all_lines]
    if all_lines and not text.endswith("\n"):
        all_lines[-1] = all_lines[-1][:-1]
    total = len(all_lines)
    lines: list[str] = []
    chars = 0
    truncated = False
    long_line = None
    for number in range(offset, min(total, offset + limit)):
        line = all_lines[number]
        if chars + len(line) > max_chars:
            if not lines:
                long_line = {"line": number + 1, "chars": len(line), "shown": max_chars}
                lines.append(line[:max_chars] + "\n")
            truncated = True
            break
        lines.append(line)
        chars += len(line)
    returned = len(lines)
    if line_numbers:
        content = "".join(f"{offset + index + 1:>6}\t{line}" for index, line in enumerate(lines))
    else:
        content = "".join(lines)
    result: dict[str, Any] = {
        "status": "ok", "path": str(target), "content": content, "offset": offset,
        "lines": returned, "total_lines": total,
        "next_offset": offset + returned if offset + returned < total else None,
        "truncated": truncated,
    }
    if crlf:
        result["line_ending"] = "crlf"
    if encoding not in ("utf-8", "utf-8-sig"):
        result["encoding"] = encoding
    if long_line:
        result["long_line"] = long_line
    return result


def file_write(policy: Policy, path: str, content: str, mode: str) -> dict[str, Any]:
    if mode not in WRITE_MODES:
        raise FileToolError(f"mode must be one of {', '.join(WRITE_MODES)}")
    if len(content) > MAX_WRITE_CHARS:
        raise FileToolError(f"content is larger than {MAX_WRITE_CHARS} characters")
    target = policy.resolve_path(path)
    if target.is_dir():
        raise FileToolError(f"{target} is a directory")
    existed = target.exists()
    if mode == "create" and existed:
        raise FileToolError(f"{target} already exists (mode=create never overwrites)")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a" if mode == "append" else "x" if mode == "create" else "w",
                     encoding="utf-8", newline="") as handle:
        handle.write(content)
    return {"status": "ok", "path": str(target), "mode": mode, "created": not existed,
            "size": target.stat().st_size}
