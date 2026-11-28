"""The console's CLI config editor (本机控制台 → 设置 → CLI 配置文件): view and edit the AI CLIs' own config files of
a run-as account, with validation, a diff to confirm, a backup on every save and one-click restore.

Only a fixed list of files (never a path from the browser), resolved from the account's own environment. Every
read and write runs *as that account*: in process for this process's own user, otherwise in a child
``python -I -m agent_nexus.cli mcp config <op> --file ID ...`` started as the user (the same machinery as the MCP
link writes, :func:`agent_nexus.agents.mcp_link.run_child`), with the text on stdin, never on the command line, so
files and their backups keep the user's owner and mode and the text never lands in the service's own folders.
Nothing here logs file content (tokens live in these files)."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

from .. import integrate
from . import mcp_link

#: id -> (label, format); the paths are resolved per account by :func:`path_of`
FILES: dict[str, tuple[str, str]] = {
    "codex": ("Codex：~/.codex/config.toml", "toml"),
    "claude_settings": ("Claude Code：~/.claude/settings.json", "json"),
    "claude_json": ("Claude Code：~/.claude.json", "json"),
    "gemini": ("Gemini CLI：~/.gemini/settings.json", "jsonc"),
}
#: ~/.claude.json holds every project's history and the account: read-only until 解锁编辑
READONLY_DEFAULT = frozenset({"claude_json"})
MAX_EDIT_BYTES = 4 * 1024 * 1024
KEEP_EDIT_BACKUPS = 10
_BACKUP_NAME = r"\.(anx-backup|anx-edit)-[0-9]{8}-[0-9]{6}(-[0-9]+)?"


class ConfigError(Exception):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra

    def body(self) -> dict:
        return {"ok": False, "code": self.code, "error": self.message, **self.extra}


def path_of(file_id: str, env: dict | None = None) -> Path:
    env = env if env is not None else dict(os.environ)
    if file_id not in FILES:
        raise ConfigError("unknown_file", f"不认识的配置文件：{file_id}")
    if file_id == "claude_settings":
        base = Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else mcp_link._home(env) / ".claude"
        return base / "settings.json"
    kind = {"codex": "codex", "claude_json": "claude", "gemini": "gemini"}[file_id]
    path = mcp_link.config_path(kind, env=env)
    assert path is not None
    return path


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _raw(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError("unreadable", f"无法读取 {path}：{exc.strerror or exc}") from None


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ConfigError("not_utf8", "文件不是 UTF-8 文本，不能在这里编辑") from None


def read(file_id: str, env: dict | None = None) -> dict:
    path = path_of(file_id, env)
    data = _raw(path)
    info: dict[str, Any] = {"ok": True, "id": file_id, "path": str(path), "exists": data is not None,
                            "size": len(data or b""), "sha256": sha256(data) if data is not None else None,
                            "mtime": None, "too_large": False}
    if data is None:
        info.update(text="", newline="\n", bom=False)
        return info
    try:
        info["mtime"] = path.stat().st_mtime
    except OSError:
        pass
    if len(data) > MAX_EDIT_BYTES:
        info.update(too_large=True, text=None, newline="\n", bom=data.startswith(b"\xef\xbb\xbf"))
        return info
    text = _decode(data)
    info.update(text=text.replace("\r\n", "\n"), newline="\r\n" if "\r\n" in text else "\n",
                bom=data.startswith(b"\xef\xbb\xbf"))
    return info


def strip_json_comments(text: str) -> str:
    """``//`` and ``/* */`` comments outside strings, replaced by spaces (positions kept for error lines)."""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append("".join(c if c == "\n" else " " for c in text[i:end]))
            i = end
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def validate(file_id: str, text: str) -> dict:
    """``{ok, error, line, col, warning}``; the error never quotes the content."""
    kind = FILES[file_id][1] if file_id in FILES else None
    if kind is None:
        raise ConfigError("unknown_file", f"不认识的配置文件：{file_id}")
    if not text.strip():
        return {"ok": True, "error": None, "line": None, "col": None, "warning": "文件将是空的"}
    if kind == "toml":
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            match = re.search(r"line (\d+), column (\d+)", str(exc))
            message = str(exc).split(" (at ", 1)[0]
            return {"ok": False, "error": f"TOML 语法错误：{message}", "line": int(match.group(1)) if match else None,
                    "col": int(match.group(2)) if match else None, "warning": None}
        return {"ok": True, "error": None, "line": None, "col": None, "warning": None}
    warning = None
    try:
        data = json.loads(text)
    except ValueError as exc:
        if kind == "jsonc":
            try:
                data = json.loads(strip_json_comments(text))
                warning = "含有注释：Gemini CLI 能读，但 Agent Nexus 不会自动修改带注释的文件"
            except ValueError as inner:
                return {"ok": False, "error": f"JSON 语法错误：{inner.msg}", "line": inner.lineno,
                        "col": inner.colno, "warning": None}
        else:
            return {"ok": False, "error": f"JSON 语法错误：{exc.msg}", "line": exc.lineno, "col": exc.colno,
                    "warning": None}
    if not isinstance(data, dict):
        return {"ok": False, "error": "顶层必须是一个 JSON 对象 {...}", "line": 1, "col": 1, "warning": None}
    return {"ok": True, "error": None, "line": None, "col": None, "warning": warning}


def _target_text(text: str, newline: str) -> str:
    text = text.replace("\r\n", "\n")
    return text.replace("\n", "\r\n") if newline == "\r\n" else text


def save(file_id: str, text: str, base_sha256: str | None, env: dict | None = None,
         now: float | None = None) -> dict:
    """Write ``text`` if the file is still the one the editor loaded (``base_sha256``; None: it did not exist)."""
    path = path_of(file_id, env)
    data = _raw(path)
    current_sha = sha256(data) if data is not None else None
    if (base_sha256 or None) != current_sha:
        raise ConfigError("stale", "文件在你编辑期间被修改，请重新加载")
    if data is not None and len(data) > MAX_EDIT_BYTES:
        raise ConfigError("too_large", "文件太大，不能在这里编辑")
    checked = validate(file_id, text)
    if not checked["ok"]:
        raise ConfigError("invalid", checked["error"], line=checked["line"], col=checked["col"])
    original = _decode(data) if data is not None else ""
    newline = "\r\n" if "\r\n" in original else "\n"
    plan = integrate.Plan(path, bom=bool(data and data.startswith(b"\xef\xbb\xbf")))
    plan.text = _target_text(text, newline)
    if data is not None and plan.text == original:
        return {"ok": True, "changed": False, "backup": None, "sha256": current_sha, "path": str(path)}
    try:
        mtime = path.stat().st_mtime if data is not None else None
    except FileNotFoundError:
        mtime = None
    try:
        integrate.apply(plan, now=now, expect_mtime=mtime, keep_backups=KEEP_EDIT_BACKUPS, tag=integrate.EDIT_TAG)
    except InterruptedError:
        raise ConfigError("stale", "文件在你编辑期间被修改，请重新加载") from None
    except OSError as exc:
        raise ConfigError("write_failed", f"无法写入 {path}：{exc.strerror or exc}") from None
    written = _raw(path) or b""
    return {"ok": True, "changed": True, "backup": str(plan.backup) if plan.backup else None,
            "sha256": sha256(written), "path": str(path)}


def _backup_file(file_id: str, name: str, env: dict | None) -> Path:
    path = path_of(file_id, env)
    if not isinstance(name, str) or not re.fullmatch(re.escape(path.name) + _BACKUP_NAME, name):
        raise ConfigError("bad_backup", "不认识的备份")
    candidate = path.with_name(name)
    if not candidate.is_file():
        raise ConfigError("bad_backup", "备份不存在")
    return candidate


def backups(file_id: str, env: dict | None = None) -> dict:
    path = path_of(file_id, env)
    found = []
    try:
        entries = list(path.parent.iterdir())
    except OSError:
        entries = []
    for item in entries:
        match = re.fullmatch(re.escape(path.name) + _BACKUP_NAME, item.name)
        if not match or not item.is_file():
            continue
        try:
            stat = item.stat()
        except OSError:
            continue
        found.append({"name": item.name, "source": "edit" if match.group(1) == "anx-edit" else "auto",
                      "size": stat.st_size, "mtime": stat.st_mtime})
    found.sort(key=lambda row: row["mtime"], reverse=True)
    return {"ok": True, "id": file_id, "path": str(path), "backups": found}


def backup_text(file_id: str, name: str, env: dict | None = None) -> dict:
    data = _raw(_backup_file(file_id, name, env)) or b""
    if len(data) > MAX_EDIT_BYTES:
        raise ConfigError("too_large", "备份太大，不能在这里恢复")
    return {"ok": True, "text": _decode(data).replace("\r\n", "\n")}


def restore(file_id: str, name: str, base_sha256: str | None, env: dict | None = None) -> dict:
    """Put a backup back through :func:`save` (so the current file gets a backup first)."""
    text = backup_text(file_id, name, env)["text"]
    return save(file_id, text, base_sha256, env)


def diff(old: str, new: str, path: str = "") -> str:
    lines = difflib.unified_diff(old.replace("\r\n", "\n").splitlines(keepends=True),
                                 new.replace("\r\n", "\n").splitlines(keepends=True),
                                 fromfile=f"{path}（当前）", tofile=f"{path}（保存后）", n=3)
    return "".join(lines)


OPS = ("read", "validate", "save", "backups", "backup_text", "restore")


def dispatch(op: str, file_id: str, *, text: str | None = None, backup: str | None = None,
             base_sha256: str | None = None, env: dict | None = None) -> dict:
    try:
        if op == "read":
            return read(file_id, env)
        if op == "validate":
            checked = validate(file_id, text or "")
            return {**checked, "valid": checked["ok"], "ok": True}
        if op == "save":
            return save(file_id, text or "", base_sha256, env)
        if op == "backups":
            return backups(file_id, env)
        if op == "backup_text":
            return backup_text(file_id, backup or "", env)
        if op == "restore":
            return restore(file_id, backup or "", base_sha256, env)
        raise ConfigError("bad_op", f"不支持的操作：{op}")
    except ConfigError as exc:
        return exc.body()


def run_as(account: Any, op: str, file_id: str, *, text: str | None = None, backup: str | None = None,
           base_sha256: str | None = None, runner: Any = None) -> dict:
    """:func:`dispatch` as ``account`` (None: this process's own user, in process)."""
    if op not in OPS:
        return ConfigError("bad_op", f"不支持的操作：{op}").body()
    if account is None:
        return dispatch(op, file_id, text=text, backup=backup, base_sha256=base_sha256)
    args = ["mcp", "config", op, "--file", file_id, "--json"]
    if backup:
        args += ["--backup", backup]
    if base_sha256:
        args += ["--base-sha256", base_sha256]
    result = mcp_link.run_child(account, args, stdin=(text or "").encode("utf-8") if text is not None else b"",
                                runner=runner)
    if "code" not in result and result.get("ok") is False:
        result["code"] = "child_failed"
    return result


def cli_main(args: Any, out=print) -> int:
    """``anx mcp config <op> --file ID [--backup NAME] [--base-sha256 H] --json`` (text on stdin)."""
    text = None
    if args.config_op in ("validate", "save"):
        text = sys.stdin.buffer.read().decode("utf-8", "replace")
    result = dispatch(args.config_op, args.file, text=text, backup=getattr(args, "backup", None),
                      base_sha256=getattr(args, "base_sha256", None))
    out(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("ok") else 1

