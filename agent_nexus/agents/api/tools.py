"""The local tools an API model may call (docs/agent-chat.md §16.4), executed on the runner device.

``read_file``, ``list_dir``, ``glob``, ``grep`` read; ``write_file``, ``edit_file`` (exact replacement) write
(both in :mod:`.fileops`); ``run_command`` runs a shell command in the work folder as the run-as account (the same
process path as the agent CLIs: :class:`~agent_nexus.agents.runner._Proc`), at most :data:`COMMAND_TIMEOUT_S`,
output truncated. With a gateway handle the run also gets ``ask_user`` / ``mail_send`` (:data:`GATEWAY_TOOLS`,
executed by the loop through the gateway as the run's mailbox address).

Every path is confined to the work folder and the run's temp folder *after resolving symlinks*: a link inside the
work folder that points elsewhere is refused like ``../../etc/passwd`` is; anx's own state folder is refused even
inside them. When the service runs as another account than the chat's run-as account, the file tools run in a
helper process as that account (:meth:`Toolbox._delegated`). Which tools a permission mode offers and which need
an approval is decided by :func:`specs` / :func:`needs_approval`:

=========  ==========================  =========================  ======================
mode       offered                     write_file / edit_file /   run_command
                                       mail_send
=========  ==========================  =========================  ======================
readonly   read tools (+ ask_user)     —                          —
ask        all                         approval                   approval
edits      all                         automatic                  approval
full       all                         automatic                  automatic
=========  ==========================  =========================  ======================
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..adapters import trunc
from . import fileops

WINDOWS = sys.platform == "win32"
READ_TOOLS = ("read_file", "list_dir", "glob", "grep")
WRITE_TOOLS = ("write_file", "edit_file")
EXEC_TOOLS = ("run_command",)
ALL_TOOLS = READ_TOOLS + WRITE_TOOLS + EXEC_TOOLS
#: through the gateway, as the run's mailbox address; only offered when the run has a handle
GATEWAY_TOOLS = ("ask_user", "mail_send")
COMMAND_TIMEOUT_S = 120.0
HELPER_TIMEOUT_S = 60.0
OUTPUT_MAX = 30_000
CAPTURE_MAX = 2 * 1024 * 1024
READ_MAX_BYTES = fileops.READ_MAX_BYTES
WRITE_MAX_BYTES = fileops.WRITE_MAX_BYTES


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


SPECS: list[dict] = [
    {"name": "read_file", "description": "Read a text file in the work folder. Returns numbered lines. Use offset/limit "
     "(1-based line numbers) for big files.",
     "parameters": _schema({"path": {"type": "string", "description": "File path, relative to the work folder or absolute"},
                            "offset": {"type": "integer", "description": "First line to read (1-based)"},
                            "limit": {"type": "integer", "description": "How many lines (default 2000)"}}, ["path"])},
    {"name": "list_dir", "description": "List the entries of a folder (folders end with /).",
     "parameters": _schema({"path": {"type": "string", "description": "Folder, relative to the work folder (default: the work folder)"}}, [])},
    {"name": "glob", "description": "Find files by glob pattern, e.g. **/*.py or src/*.ts.",
     "parameters": _schema({"pattern": {"type": "string"},
                            "path": {"type": "string", "description": "Folder to search in (default: the work folder)"}}, ["pattern"])},
    {"name": "grep", "description": "Search file contents with a regular expression. Returns path:line: text.",
     "parameters": _schema({"pattern": {"type": "string", "description": "Python regular expression"},
                            "path": {"type": "string", "description": "File or folder to search (default: the work folder)"},
                            "glob": {"type": "string", "description": "Only files whose name matches, e.g. *.py"},
                            "ignore_case": {"type": "boolean"}}, ["pattern"])},
    {"name": "write_file", "description": "Create or overwrite a file with the given content.",
     "parameters": _schema({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])},
    {"name": "edit_file", "description": "Replace an exact piece of text in a file. old_string must occur exactly once "
     "unless replace_all is true; include enough context to make it unique.",
     "parameters": _schema({"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"},
                            "replace_all": {"type": "boolean"}}, ["path", "old_string", "new_string"])},
    {"name": "run_command", "description": "Run a shell command in the work folder (timeout 120 s). Returns its output "
     "(stdout and stderr) and exit code.",
     "parameters": _schema({"command": {"type": "string"},
                            "timeout": {"type": "integer", "description": "Seconds, at most 120"}}, ["command"])},
]

GATEWAY_SPECS: list[dict] = [
    {"name": "ask_user", "description": "Ask the owner a question and wait for the answer (it appears as a question "
     "card in the chat and the inbox). Give options when the answer is a choice.",
     "parameters": _schema({"question": {"type": "string"},
                            "options": {"type": "array", "items": {"type": "string"},
                                        "description": "Choices to offer (the owner may still answer freely)"},
                            "recommended": {"type": "string", "description": "The option you recommend"},
                            "context": {"type": "string", "description": "Background the owner needs"}}, ["question"])},
    {"name": "mail_send", "description": "Send mail to another agent session (name@device, a bare name, a group such "
     "as @all) or to the owner (\"me\").",
     "parameters": _schema({"to": {"type": "string"}, "body": {"type": "string"}, "subject": {"type": "string"},
                            "kind": {"type": "string", "enum": ["message", "task", "result"]}}, ["to", "body"])},
]


def specs(permission: str, *, gateway: bool = False) -> list[dict]:
    """The tools offered in ``permission`` mode (readonly: read tools only, and ask_user); ``gateway``: the run
    has a handle, so ask_user / mail_send work."""
    offered = [spec for spec in SPECS if allowed(spec["name"], permission)]
    if gateway:
        offered += [spec for spec in GATEWAY_SPECS if allowed(spec["name"], permission)]
    return offered


def allowed(name: str, permission: str) -> bool:
    if permission == "readonly":
        return name in READ_TOOLS or name == "ask_user"
    return name in ALL_TOOLS or name in GATEWAY_TOOLS


def needs_approval(name: str, permission: str) -> bool:
    if name in READ_TOOLS or name == "ask_user" or permission == "full":
        return False
    if name in WRITE_TOOLS or name == "mail_send":
        return permission == "ask"
    return True  # run_command in ask / edits


ToolError = fileops.ToolError


@dataclass
class Outcome:
    output: str
    error: bool = False
    events: list[dict] = field(default_factory=list)  # file_change events for the chat


def other_account(run_as: dict | None) -> bool:
    """Whether ``run_as`` is another account than this process's (a root / SYSTEM service running the chat
    as a user): then the file tools run as that account (:meth:`Toolbox._delegated`), never with our rights."""
    if not run_as:
        return False
    if WINDOWS:  # pragma: no cover - Windows service: the user's session token
        return run_as.get("session") is not None
    if "user" not in run_as or not hasattr(os, "geteuid"):
        return False
    try:
        return int(run_as["user"]) != os.geteuid()
    except (TypeError, ValueError):
        return True


class Toolbox:
    """The tools of one run: ``workdir`` and ``tmpdir`` are the folders paths may be in, ``deny`` folders inside
    them that are never touched (anx's own state); commands (and, for another account, the file tools) run with
    ``env`` as ``run_as`` (``Account.popen_args()``, None = this process's user)."""

    def __init__(self, workdir: str, tmpdir: str = "", *, env: dict | None = None, run_as: dict | None = None,
                 timeout_s: float = COMMAND_TIMEOUT_S, deny: list[str] | tuple = (), delegate: bool | None = None):
        self.files = fileops.Files(workdir, [tmpdir] if tmpdir else [], list(deny))
        self.workdir = self.files.workdir
        self.roots = self.files.roots
        self.tmpdir = os.path.realpath(tmpdir) if tmpdir else ""
        self.env = dict(env) if env is not None else dict(os.environ)
        self.run_as = run_as
        self.timeout_s = timeout_s
        self.delegate = other_account(run_as) if delegate is None else delegate
        self._helper: str | None = None

    # -- paths
    def resolve(self, value: Any, *, default: str | None = None) -> Path:
        return self.files.resolve(value, default=default)

    def show(self, path: Path) -> str:
        return self.files.show(path)

    # -- dispatch
    async def run(self, name: str, args: dict) -> Outcome:
        if name not in ALL_TOOLS:
            return Outcome(f"没有这个工具：{name}", error=True)
        args = args if isinstance(args, dict) else {}
        if name == "run_command":
            try:
                return await self._t_run_command(args)
            except ToolError as exc:
                return Outcome(str(exc), error=True)
        if self.delegate:
            result = await self._delegated(name, args)
        else:
            result = await asyncio.to_thread(self.files.run, name, args)
        return Outcome(str(result.get("output") or ""), bool(result.get("error")),
                       [e for e in result.get("events") or [] if isinstance(e, dict)])

    def _helper_script(self) -> str:
        """A copy of :mod:`.fileops` the run-as account can read (in the run's temp folder)."""
        if self._helper and os.path.isfile(self._helper):
            return self._helper
        source = Path(fileops.__file__)
        if not self.tmpdir:
            return str(source)
        target = Path(self.tmpdir) / "anx_fileops.py"
        shutil.copyfile(source, target)
        with contextlib.suppress(OSError):
            os.chmod(target, 0o600)
            if not WINDOWS and self.run_as and "user" in self.run_as:
                os.chown(target, int(self.run_as["user"]), int(self.run_as.get("group", -1)))
        self._helper = str(target)
        return self._helper

    async def _delegated(self, name: str, args: dict) -> dict:
        """One file tool call in a helper process as the run-as account."""
        from ..runner import _Proc
        python = sys.executable
        if WINDOWS and python.lower().endswith("pythonw.exe"):  # pragma: no cover - Windows
            console = python[:-len("pythonw.exe")] + "python.exe"
            python = console if os.path.isfile(console) else python
        request = json.dumps({"workdir": self.workdir, "roots": self.roots[1:], "deny": self.files.deny,
                              "name": name, "args": args}, ensure_ascii=False).encode("utf-8")
        loop = asyncio.get_running_loop()
        exited: asyncio.Future = loop.create_future()
        out: list[str] = []
        err: list[str] = []

        def on_line(stream: str, line: str) -> None:
            (out if stream == "stdout" else err).append(line)

        def on_exit(code: int | None) -> None:
            if not exited.done():
                exited.set_result(code)

        try:
            script = await asyncio.to_thread(self._helper_script)
            proc = _Proc([python, "-I", script], cwd=self.workdir, env=self.env, run_as=self.run_as, loop=loop,
                         on_line=on_line, on_exit=on_exit, stdin_data=request, keep_stdin=False)
        except (OSError, ValueError) as exc:
            return {"output": f"无法以执行账户运行文件工具：{exc}", "error": True}
        try:
            await asyncio.wait_for(asyncio.shield(exited), HELPER_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            return {"output": f"文件工具超过 {int(HELPER_TIMEOUT_S)} 秒没有完成，已结束", "error": True}
        except asyncio.CancelledError:
            proc.kill()
            raise
        for line in reversed(out):
            try:
                result = json.loads(line)
            except ValueError:
                continue
            if isinstance(result, dict):
                return result
        detail = (err[-1] if err else "").strip()[:300]
        return {"output": "以执行账户运行文件工具失败" + (f"：{detail}" if detail else ""), "error": True}

    # -- run_command
    async def _t_run_command(self, args: dict) -> Outcome:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolError("缺少 command")
        if "\0" in command:
            raise ToolError("command 里不能有 NUL")
        timeout = max(1.0, min(float(_int(args.get("timeout"), int(self.timeout_s))), self.timeout_s))
        from ..runner import (
            _Proc,  # the same spawn path as the agent CLIs (run-as account, Windows service)
        )
        if WINDOWS:  # pragma: no cover - Windows
            comspec = os.environ.get("ComSpec") or "cmd.exe"
            # the console code page is the OEM one (cp936...): switch the hidden console to UTF-8 first, so the
            # output of cmd built-ins and console tools decodes as the UTF-8 that _Proc reads
            launch: list[str] | str = f'"{comspec}" /d /s /c "chcp 65001>nul & {command}"'
        else:
            launch = ["/bin/sh", "-c", command]
        loop = asyncio.get_running_loop()
        exited: asyncio.Future = loop.create_future()
        lines: list[str] = []
        captured = [0, False]

        def on_line(_stream: str, line: str) -> None:
            if captured[0] > CAPTURE_MAX:
                captured[1] = True
                return
            captured[0] += len(line) + 1
            lines.append(line)

        def on_exit(code: int | None) -> None:
            if not exited.done():
                exited.set_result(code)

        try:
            proc = _Proc(launch, cwd=self.workdir, env=self.env, run_as=self.run_as, loop=loop, on_line=on_line,
                         on_exit=on_exit, keep_stdin=False)
        except (OSError, ValueError) as exc:
            raise ToolError(f"无法启动命令：{exc}") from exc
        timed_out = False
        try:
            code = await asyncio.wait_for(asyncio.shield(exited), timeout)
        except TimeoutError:
            timed_out = True
            proc.kill()
            try:
                code = await asyncio.wait_for(asyncio.shield(exited), 5)
            except TimeoutError:
                code = None
        except asyncio.CancelledError:
            proc.kill()
            raise
        output = "\n".join(lines)
        if captured[1]:
            output += "\n…（输出太多，后面的没有收集）"
        output = clip_output(output)
        if timed_out:
            return Outcome(f"{output}\n[命令超过 {int(timeout)} 秒，已结束]".lstrip("\n"), error=True)
        return Outcome(f"{output}\n[退出码 {code}]".lstrip("\n"), error=code != 0)


def clip_output(text: str, limit: int = OUTPUT_MAX) -> str:
    """The head and the tail of a long output."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n…（省略 {len(text) - 2 * half} 个字符）…\n{text[-half:]}"


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


__all__ = ["ALL_TOOLS", "EXEC_TOOLS", "GATEWAY_SPECS", "GATEWAY_TOOLS", "READ_TOOLS", "SPECS", "WRITE_TOOLS", "Outcome",
           "ToolError", "Toolbox", "allowed", "clip_output", "needs_approval", "other_account", "specs", "trunc"]
