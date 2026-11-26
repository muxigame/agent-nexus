"""Turn each agent CLI's output into the unified events of docs/agent-chat.md §4.

The adapters are *sans-IO*: :class:`~agent_nexus.agents.runner.Runs` owns the processes and hands every stdout
line to :meth:`Adapter.feed`; an adapter answers with events and queues what must be written to the process's
stdin in :attr:`Adapter.outbox`. That keeps the protocol logic testable line by line and the process handling
(run-as account, Windows shims, kill trees) in one place.

* ``claude-stream`` -- one long-lived ``claude -p --input-format stream-json`` process; user turns go to stdin.
* ``codex-exec``    -- one ``codex exec --json`` process per turn, later turns ``exec resume <id>``.
* ``acp``           -- an Agent Client Protocol (JSON-RPC 2.0 over stdio) client for dsh / gemini (§12).
* ``text`` / ``jsonl`` -- one process per turn; stdout lines become assistant text (JSON lines read leniently).

Every parser is lenient: unknown event types and missing fields are ignored, never fatal.
"""
from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from typing import Any

from . import registry
from .registry import AgentDef

TOOL_OUTPUT_MAX = 8000
INPUT_MAX = 4000
DIFF_MAX = 20000
APPROVAL_TOOL = "mcp__anx__approve"


@dataclass
class AdapterOptions:
    binary: str
    workdir: str
    permission: str = "ask"
    system_prompt: str = ""
    resume_id: str = ""
    #: claude ``ask``: the path of the ``--mcp-config`` file holding anx's approval server
    mcp_config: str = ""
    #: The launcher is a ``.cmd``/``.bat`` shim that could not be resolved to the program behind it: every
    #: argument goes through cmd.exe, so free text (prompts, the system prompt) must avoid the command line.
    batch: bool = False
    #: claude: ask for token deltas (``--include-partial-messages``); dropped again if the CLI is too old
    partial: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


def trunc(text: Any, limit: int) -> str:
    text = text if isinstance(text, str) else ("" if text is None else str(text))
    return text if len(text) <= limit else text[:limit] + f"\n…（已截断，共 {len(text)} 个字符）"


def text_of(content: Any) -> str:
    """The text in a content value: a string, ``{type: text, text}``, or a list of those."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        if "content" in content:
            return text_of(content["content"])
        return ""
    if isinstance(content, list):
        return "".join(text_of(item) for item in content)
    return ""


def _small(value: Any) -> Any:
    """A tool input as an event carries it: big strings cut, the shape kept."""
    try:
        raw = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)[:INPUT_MAX]
    if len(raw) <= INPUT_MAX:
        return value
    if isinstance(value, dict):
        return {key: (trunc(item, 1000) if isinstance(item, str) else item) for key, item in value.items()}
    return trunc(raw, INPUT_MAX)


def tool_title(name: str, data: Any) -> str:
    """One line saying what a tool call does: the command, the file, the pattern..."""
    if isinstance(data, dict):
        for key in ("command", "cmd", "file_path", "path", "notebook_path", "pattern", "url", "query",
                    "description", "prompt", "title"):
            value = data.get(key)
            if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
                value = " ".join(value)
            if isinstance(value, str) and value.strip():
                return " ".join(value.split())[:200]
    return name or ""


def unified_diff(path: str, old: str, new: str) -> str:
    lines = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                 fromfile=f"a/{path}", tofile=f"b/{path}", n=2)
    return trunc("".join(lines), DIFF_MAX)


def _json(line: str) -> Any:
    line = line.strip()
    if not line or line[0] not in "{[":
        return None
    try:
        return json.loads(line)
    except ValueError:
        return None


def _dumps(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def _usage(raw: Any, cost: Any = None) -> dict:
    raw = raw if isinstance(raw, dict) else {}

    def num(*keys: str) -> int:
        total = 0
        for key in keys:
            value = raw.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total += int(value)
        return total

    usage = {"input": num("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "inputTokens"),
             "output": num("output_tokens", "outputTokens")}
    cached = num("cached_input_tokens", "cache_read_input_tokens")
    if cached:
        usage["cached"] = cached
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        usage["cost_usd"] = round(float(cost), 6)
    return usage


class Adapter:
    """The common surface. ``persistent`` adapters keep one process for the whole run; the others start a
    process per turn (:meth:`argv` with the turn's prompt, plus :meth:`turn_stdin`)."""
    name = "text"
    persistent = False
    #: approvals arrive in-band (ACP) or through the approval MCP server (claude ``ask``)
    in_band_stop = False

    def __init__(self, d: AgentDef, opts: AdapterOptions):
        self.d, self.opts = d, opts
        self.session_id: str | None = opts.resume_id or None
        self.outbox: list[bytes] = []
        self.turn_active = False
        self.stopping = False
        self.ready = True
        self.saw_output = False
        self.turns = 0
        self.notices: list[str] = []
        self._prefix_pending = bool(opts.system_prompt) and not self._system_prompt_in_argv()

    # -- system prompt
    def _system_prompt_in_argv(self) -> bool:
        """Whether the system prompt travels as arguments (else it is prefixed to the first message)."""
        return bool(self.d.system_prompt_args) and not self.opts.batch

    def _with_prefix(self, prompt: str) -> str:
        if not self._prefix_pending:
            return prompt
        self._prefix_pending = False
        return f"{self.opts.system_prompt}\n\n---\n\n{prompt}"

    def _values(self, prompt: str | None = None) -> dict[str, Any]:
        values: dict[str, Any] = {"workdir": self.opts.workdir, "resume_id": self.session_id or "",
                                  "prompt": prompt or "", "approval_tool": APPROVAL_TOOL}
        if self._system_prompt_in_argv():
            values["system_prompt"] = self.opts.system_prompt
        return values

    # -- the process
    def argv(self, prompt: str | None = None) -> list[str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def turn_stdin(self) -> bytes | None:
        """What a per-turn process gets on stdin (None: stdin is closed at once)."""
        return None

    def start(self) -> list[dict]:
        """Right after the process started."""
        return []

    def begin_turn(self, prompt: str) -> list[dict]:
        """A persistent adapter's next turn (queues stdin); per-turn adapters are begun by :meth:`argv`."""
        raise NotImplementedError  # pragma: no cover

    def feed(self, line: str) -> list[dict]:
        return []

    def feed_err(self, line: str) -> list[dict]:
        return []

    def stop(self) -> list[dict]:
        """Interrupt the turn in-band when the protocol can (queues stdin); the runner kills otherwise."""
        self.stopping = True
        return []

    def answer(self, approval_id: str, allow: bool, remember: bool) -> list[dict] | None:
        """An in-band approval's answer (queues stdin); None when this adapter does not hold ``approval_id``."""
        return None

    def pending_approvals(self) -> list[str]:
        return []

    def exited(self, code: int | None, stderr_tail: str) -> list[dict]:
        """The process ended; closes the turn it was running."""
        if not self.turn_active:
            return []
        self.turn_active = False
        if self.stopping:
            self.stopping = False
            return [{"type": "status", "state": "stopped", "session_id": self.session_id}]
        if code == 0:
            return [{"type": "status", "state": "done", "session_id": self.session_id}]
        return [{"type": "error", "text": _exit_text(self.d.label, code, stderr_tail)},
                {"type": "status", "state": "failed", "session_id": self.session_id}]

    def retry_without(self, stderr_tail: str) -> bool:
        """Whether the process died for want of an optional flag (then it is started again without it)."""
        return False

    def reset_process(self) -> None:
        """The runner ended the process on purpose (idle close, fallback, shutdown): its exit is not fed to
        :meth:`exited`, so forget whatever belonged to that process. The session id stays (it is resumed)."""

    def replay(self) -> list[bytes]:
        """What to write again after such a restart."""
        return []


def _exit_text(label: str, code: int | None, stderr_tail: str) -> str:
    tail = stderr_tail.strip()
    head = f"{label} 意外退出" + (f"（退出码 {code}）" if code is not None else "")
    return f"{head}：\n{trunc(tail, 2000)}" if tail else head


# ---------------------------------------------------------------------------------------------- claude-stream

class ClaudeStream(Adapter):
    """``claude -p --output-format stream-json --verbose --input-format stream-json``: one process, turns on stdin.

    Output lines: ``system/init`` (session id), ``stream_event`` (token deltas with
    ``--include-partial-messages``), ``assistant`` (content blocks text / thinking / tool_use), ``user``
    (tool_result), ``result`` (end of turn, usage and cost), ``control_request`` / ``control_response``."""
    name = "claude-stream"
    persistent = True
    in_band_stop = True

    def __init__(self, d: AgentDef, opts: AdapterOptions):
        super().__init__(d, opts)
        self.tools: dict[str, dict] = {}
        self._streamed: dict[str, str] = {}  # block id -> text sent as deltas
        self._block_ids: dict[int, str] = {}
        self._message_id = ""
        self._last_user: bytes | None = None
        self._approvals: dict[str, dict] = {}  # can_use_tool control requests (stdio permission prompts)
        self._interrupt_n = 0

    def argv(self, prompt: str | None = None) -> list[str]:
        template = "resume_command" if self.session_id and self.d.resume_command else "command"
        values = self._values()
        argv = registry.command_argv(self.d, self.opts.binary, permission=self.opts.permission, values=values,
                                     template=template)
        if self.opts.permission == "ask":
            if self.opts.mcp_config:
                argv += ["--mcp-config", self.opts.mcp_config]
            else:  # no approval server: plain -p mode refuses what would need a prompt
                argv = _drop_flag(argv, "--permission-prompt-tool")
        if self.opts.partial and "--include-partial-messages" not in argv:
            argv.append("--include-partial-messages")
        return argv

    def begin_turn(self, prompt: str) -> list[dict]:
        self.turn_active, self.stopping = True, False
        self.turns += 1
        self._streamed.clear()
        message = {"type": "user", "message": {"role": "user", "content": [{"type": "text",
                                                                            "text": self._with_prefix(prompt)}]}}
        if self.session_id:
            message["session_id"] = self.session_id
        data = _dumps(message)
        self._last_user = data
        self.outbox.append(data)
        return []

    def stop(self) -> list[dict]:
        self.stopping = True
        if self.turn_active:
            self._interrupt_n += 1
            self.outbox.append(_dumps({"type": "control_request", "request_id": f"anx_interrupt_{self._interrupt_n}",
                                       "request": {"subtype": "interrupt"}}))
        events = []
        for approval_id in list(self._approvals):
            events += self._settle(approval_id, False, False, "已停止")
        return events

    def retry_without(self, stderr_tail: str) -> bool:
        if self.opts.partial and not self.saw_output and "include-partial-messages" in stderr_tail:
            self.opts.partial = False
            return True
        return False

    def replay(self) -> list[bytes]:
        return [self._last_user] if self._last_user and self.turn_active else []

    # -- output
    def feed(self, line: str) -> list[dict]:
        obj = _json(line)
        if not isinstance(obj, dict):
            return []
        self.saw_output = True
        kind = obj.get("type")
        events: list[dict] = []
        if isinstance(obj.get("session_id"), str) and obj["session_id"] and obj["session_id"] != self.session_id:
            self.session_id = obj["session_id"]
            events.append({"type": "status", "state": "running" if self.turn_active else "idle",
                           "session_id": self.session_id})
        if kind == "system":
            if obj.get("subtype") == "init" and not events:
                events.append({"type": "status", "state": "running" if self.turn_active else "idle",
                               "session_id": self.session_id, "model": obj.get("model")})
            return events
        if kind == "stream_event":
            return events + self._stream_event(obj.get("event"))
        if kind == "assistant":
            return events + self._assistant(obj.get("message"))
        if kind == "user":
            return events + self._user(obj.get("message"))
        if kind == "result":
            return events + self._result(obj)
        if kind == "control_request":
            return events + self._control_request(obj)
        return events

    def _stream_event(self, event: Any) -> list[dict]:
        if not isinstance(event, dict):
            return []
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            self._message_id = str(message.get("id") or "")
            self._block_ids.clear()
            return []
        if kind == "content_block_start":
            index = event.get("index") if isinstance(event.get("index"), int) else 0
            self._block_ids[index] = f"{self._message_id}:{index}"
            return []
        if kind != "content_block_delta" or not isinstance(event.get("delta"), dict):
            return []
        delta = event["delta"]
        index = event.get("index") if isinstance(event.get("index"), int) else 0
        block_id = self._block_ids.get(index) or f"{self._message_id}:{index}"
        if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str) and delta["text"]:
            self._streamed[block_id] = self._streamed.get(block_id, "") + delta["text"]
            return [{"type": "message", "role": "assistant", "text": delta["text"], "delta": True, "id": block_id}]
        if delta.get("type") == "thinking_delta" and isinstance(delta.get("thinking"), str) and delta["thinking"]:
            self._streamed[block_id] = self._streamed.get(block_id, "") + delta["thinking"]
            return [{"type": "thinking", "text": delta["thinking"], "delta": True, "id": block_id}]
        return []

    def _assistant(self, message: Any) -> list[dict]:
        if not isinstance(message, dict):
            return []
        message_id = str(message.get("id") or self._message_id or "")
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        events: list[dict] = []
        for position, block in enumerate(content if isinstance(content, list) else []):
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            block_id = self._find_block(message_id, kind, block, position)
            if kind == "text":
                text = block.get("text") if isinstance(block.get("text"), str) else ""
                if text and self._streamed.get(block_id) != text:
                    events.append({"type": "message", "role": "assistant", "text": text, "id": block_id})
            elif kind == "thinking":
                text = block.get("thinking") if isinstance(block.get("thinking"), str) else ""
                if text and self._streamed.get(block_id) != text:
                    events.append({"type": "thinking", "text": text, "id": block_id})
            elif kind in ("tool_use", "server_tool_use"):
                tool_id = str(block.get("id") or block_id)
                name = str(block.get("name") or "")
                data = block.get("input") if isinstance(block.get("input"), dict) else {}
                self.tools[tool_id] = {"name": name, "input": data}
                events.append({"type": "tool", "id": tool_id, "name": name, "title": tool_title(name, data),
                               "input": _small(data), "status": "running"})
        return events

    def _find_block(self, message_id: str, kind: Any, block: dict, position: int) -> str:
        """The id the deltas of this block used. Partial assistant messages carry one block each, so the
        block's position in the message is not its index: match on the streamed text instead."""
        text = block.get("text") if kind == "text" else block.get("thinking") if kind == "thinking" else None
        if isinstance(text, str):
            for block_id, streamed in self._streamed.items():
                if block_id.startswith(f"{message_id}:") and streamed == text:
                    return block_id
        return f"{message_id}:{position}"

    def _user(self, message: Any) -> list[dict]:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            return []
        events: list[dict] = []
        for block in message["content"]:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_id = str(block.get("tool_use_id") or "")
            error = bool(block.get("is_error"))
            output = text_of(block.get("content"))
            events.append({"type": "tool", "id": tool_id, "status": "error" if error else "ok",
                           "output": trunc(output, TOOL_OUTPUT_MAX)})
            tool = self.tools.pop(tool_id, None)
            if tool and not error:
                events += _claude_file_change(tool["name"], tool["input"])
        return events

    def _result(self, obj: dict) -> list[dict]:
        usage = _usage(obj.get("usage"), obj.get("total_cost_usd", obj.get("cost_usd")))
        events: list[dict] = []
        was_stopping = self.stopping
        self.turn_active, self.stopping = False, False
        if was_stopping:
            return [{"type": "status", "state": "stopped", "session_id": self.session_id, "usage": usage}]
        if obj.get("is_error") or (isinstance(obj.get("subtype"), str) and obj["subtype"].startswith("error")):
            text = obj.get("result") if isinstance(obj.get("result"), str) else ""
            reason = {"error_max_turns": "达到了最大轮数", "error_during_execution": "执行中出错"}.get(
                str(obj.get("subtype")), "")
            events.append({"type": "error", "text": text or reason or f"{self.d.label} 这一轮没有成功完成"})
            events.append({"type": "status", "state": "failed", "session_id": self.session_id, "usage": usage})
            return events
        return [{"type": "status", "state": "done", "session_id": self.session_id, "usage": usage}]

    # -- stdio permission prompts (``--permission-prompt-tool stdio``); harmless when they never come
    def _control_request(self, obj: dict) -> list[dict]:
        request = obj.get("request") if isinstance(obj.get("request"), dict) else {}
        request_id = str(obj.get("request_id") or "")
        if request.get("subtype") != "can_use_tool" or not request_id:
            if request_id:
                self.outbox.append(_dumps({"type": "control_response", "response": {
                    "subtype": "error", "request_id": request_id, "error": "not supported"}}))
            return []
        approval_id = f"cr_{request_id}"
        name = str(request.get("tool_name") or "")
        data = request.get("input") if isinstance(request.get("input"), dict) else {}
        self._approvals[approval_id] = {"request_id": request_id, "input": data}
        return [{"type": "approval", "id": approval_id, "tool": name, "summary": tool_title(name, data),
                 "input": _small(data), "state": "pending"}]

    def answer(self, approval_id: str, allow: bool, remember: bool) -> list[dict] | None:
        if approval_id not in self._approvals:
            return None
        return self._settle(approval_id, allow, remember, "")

    def _settle(self, approval_id: str, allow: bool, remember: bool, reason: str) -> list[dict]:
        entry = self._approvals.pop(approval_id)
        decision = {"behavior": "allow", "updatedInput": entry["input"]} if allow else \
            {"behavior": "deny", "message": reason or "用户拒绝了这个操作"}
        self.outbox.append(_dumps({"type": "control_response", "response": {
            "subtype": "success", "request_id": entry["request_id"], "response": decision}}))
        return [{"type": "approval", "id": approval_id, "state": "allowed" if allow else "denied",
                 "remembered": remember}]

    def pending_approvals(self) -> list[str]:
        return list(self._approvals)

    def reset_process(self) -> None:
        self._approvals.clear()
        self.tools.clear()
        self._streamed.clear()
        self._block_ids.clear()

    def exited(self, code: int | None, stderr_tail: str) -> list[dict]:
        events = super().exited(code, stderr_tail)
        self._approvals.clear()
        return events


def _drop_flag(argv: list[str], flag: str) -> list[str]:
    out, skip = [], False
    for item in argv:
        if skip:
            skip = False
            continue
        if item == flag:
            skip = True
            continue
        out.append(item)
    return out


def _claude_file_change(name: str, data: dict) -> list[dict]:
    path = data.get("file_path") or data.get("notebook_path") or data.get("path")
    if not isinstance(path, str) or not path:
        return []
    if name == "Write":
        content = data.get("content") if isinstance(data.get("content"), str) else ""
        return [{"type": "file_change", "path": path, "kind": "add", "diff": unified_diff(path, "", content)}]
    if name == "Edit":
        old = data.get("old_string") if isinstance(data.get("old_string"), str) else ""
        new = data.get("new_string") if isinstance(data.get("new_string"), str) else ""
        return [{"type": "file_change", "path": path, "kind": "edit", "diff": unified_diff(path, old, new)}]
    if name == "MultiEdit" and isinstance(data.get("edits"), list):
        parts = [unified_diff(path, str(edit.get("old_string") or ""), str(edit.get("new_string") or ""))
                 for edit in data["edits"] if isinstance(edit, dict)]
        return [{"type": "file_change", "path": path, "kind": "edit", "diff": trunc("".join(parts), DIFF_MAX)}]
    if name == "NotebookEdit":
        return [{"type": "file_change", "path": path, "kind": "edit", "diff": ""}]
    return []


# ---------------------------------------------------------------------------------------------- codex-exec

class CodexExec(Adapter):
    """``codex exec --json``, a process per turn; ``exec resume <thread id>`` continues the thread.

    Output: ``thread.started`` (thread id), ``turn.started`` / ``turn.completed`` (usage) / ``turn.failed``,
    ``item.started`` / ``item.updated`` / ``item.completed`` with item types ``agent_message``, ``reasoning``,
    ``command_execution``, ``file_change``, ``mcp_tool_call``, ``web_search``, ``todo_list``, ``error``.
    The older ``{"msg": {...}}`` format is read too."""
    name = "codex-exec"
    STDIN_PROMPT_OVER = 24000  # longer prompts go through stdin ("-")

    def __init__(self, d: AgentDef, opts: AdapterOptions):
        super().__init__(d, opts)
        self._stdin: bytes | None = None
        self._turn_done = False
        self._items_with_text: set[str] = set()

    def _values(self, prompt: str | None = None) -> dict[str, Any]:
        values = super()._values(prompt)
        if "system_prompt" in values:
            # -c takes a TOML value; a JSON string is a valid TOML basic string.
            values["system_prompt"] = json.dumps(self.opts.system_prompt, ensure_ascii=False)
        return values

    def argv(self, prompt: str | None = None) -> list[str]:
        self.turn_active, self.stopping, self._turn_done = True, False, False
        self.turns += 1
        text = self._with_prefix(prompt or "")
        self._stdin = None
        if self.opts.batch or len(text) > self.STDIN_PROMPT_OVER or text.startswith("-"):
            self._stdin, text = text.encode("utf-8"), "-"
        template = "resume_command" if self.session_id and self.d.resume_command else "command"
        argv = registry.command_argv(self.d, self.opts.binary, permission=self.opts.permission,
                                     values=self._values(text), template=template)
        return _codex_approval_flag(argv)

    def turn_stdin(self) -> bytes | None:
        return self._stdin

    def feed(self, line: str) -> list[dict]:
        obj = _json(line)
        if not isinstance(obj, dict):
            return []
        self.saw_output = True
        if isinstance(obj.get("msg"), dict):
            return self._legacy(obj["msg"])
        kind = obj.get("type")
        if kind == "thread.started" and isinstance(obj.get("thread_id"), str):
            self.session_id = obj["thread_id"]
            return [{"type": "status", "state": "running", "session_id": self.session_id}]
        if kind == "turn.started":
            self._warned = set()
            return [{"type": "status", "state": "running", "session_id": self.session_id}]
        if kind == "turn.completed":
            self._turn_done = True
            stopping, self.turn_active, self.stopping = self.stopping, False, False
            return [{"type": "status", "state": "stopped" if stopping else "done",
                     "session_id": self.session_id, "usage": _usage(obj.get("usage"))}]
        if kind == "turn.failed":
            self._turn_done, self.turn_active, self.stopping = True, False, False
            error = obj.get("error") if isinstance(obj.get("error"), dict) else {}
            return [{"type": "error", "text": str(error.get("message") or "Codex 这一轮失败了")},
                    {"type": "status", "state": "failed", "session_id": self.session_id}]
        if kind == "error":
            return self._warn(str(obj.get("message") or "Codex 报告了一个错误"))
        if kind in ("item.started", "item.updated", "item.completed") and isinstance(obj.get("item"), dict):
            return self._item(obj["item"], kind.split(".")[1])
        return []

    def _item(self, item: dict, phase: str) -> list[dict]:
        kind = item.get("type") or item.get("item_type")
        item_id = str(item.get("id") or "")
        done = phase == "completed"
        if kind in ("agent_message", "assistant_message"):
            text = item.get("text") if isinstance(item.get("text"), str) else text_of(item.get("content"))
            if not done or not text:
                return []
            return [{"type": "message", "role": "assistant", "text": text, "id": item_id or None}]
        if kind == "reasoning":
            text = item.get("text") if isinstance(item.get("text"), str) else text_of(item.get("summary"))
            return [{"type": "thinking", "text": text, "id": item_id or None}] if done and text else []
        if kind == "command_execution":
            command = item.get("command")
            command = " ".join(command) if isinstance(command, list) else str(command or "")
            status = _codex_status(item.get("status"), item.get("exit_code"), done)
            event = {"type": "tool", "id": item_id, "name": "shell", "title": " ".join(command.split())[:200],
                     "input": {"command": command}, "status": status}
            output = item.get("aggregated_output")
            if isinstance(output, str) and (output or done):
                event["output"] = trunc(output, TOOL_OUTPUT_MAX)
            if done and isinstance(item.get("exit_code"), int):
                event["exit_code"] = item["exit_code"]
            return [event]
        if kind == "file_change":
            if not done:
                return []
            events = []
            for change in item.get("changes") if isinstance(item.get("changes"), list) else []:
                if isinstance(change, dict) and isinstance(change.get("path"), str):
                    change_kind = {"update": "edit", "modify": "edit", "add": "add", "create": "add",
                                   "delete": "delete", "remove": "delete"}.get(str(change.get("kind")), "edit")
                    diff = change.get("diff") or change.get("unified_diff") or ""
                    events.append({"type": "file_change", "path": change["path"], "kind": change_kind,
                                   "diff": trunc(diff if isinstance(diff, str) else "", DIFF_MAX)})
            if str(item.get("status")) == "failed":
                events.append({"type": "error", "text": "Codex 没能应用文件改动"})
            return events
        if kind == "mcp_tool_call":
            server, tool = str(item.get("server") or ""), str(item.get("tool") or "")
            arguments = item.get("arguments")
            event = {"type": "tool", "id": item_id, "name": f"{server}.{tool}" if server else tool,
                     "title": tool_title(tool, arguments) if isinstance(arguments, dict) else tool,
                     "input": _small(arguments), "status": _codex_status(item.get("status"), None, done)}
            if item.get("error"):
                error = item["error"]
                event["output"] = trunc(error.get("message") if isinstance(error, dict) else error, TOOL_OUTPUT_MAX)
                event["status"] = "error"
            elif done and item.get("result") is not None:
                result = item["result"]
                text = text_of(result.get("content")) if isinstance(result, dict) else text_of(result)
                event["output"] = trunc(text or json.dumps(result, ensure_ascii=False, default=str), TOOL_OUTPUT_MAX)
            return [event]
        if kind == "web_search":
            query = str(item.get("query") or "")
            return [{"type": "tool", "id": item_id, "name": "web_search", "title": query[:200],
                     "input": {"query": query}, "status": "ok" if done else "running"}]
        if kind == "todo_list":
            entries = item.get("items") if isinstance(item.get("items"), list) else []
            lines = [("[x] " if entry.get("completed") else "[ ] ") + str(entry.get("text") or "")
                     for entry in entries if isinstance(entry, dict)]
            return [{"type": "tool", "id": item_id or "todo", "name": "todo_list", "title": "任务清单",
                     "status": "ok", "output": "\n".join(lines)}]
        if kind == "error":
            return self._warn(str(item.get("message") or "Codex 报告了一个错误")) if done else []
        return []

    def _warn(self, text: str) -> list[dict]:
        """Codex's ``error`` events and items are non-fatal (a failed turn comes as ``turn.failed``) and the same
        notice (e.g. "ignoring 1 unrecognized configuration setting") arrives as both: once per turn, as a warning."""
        seen = getattr(self, "_warned", None)
        if seen is None:
            seen = self._warned = set()
        if text in seen:
            return []
        seen.add(text)
        return [{"type": "error", "level": "warn", "text": text}]

    def _legacy(self, msg: dict) -> list[dict]:
        kind = msg.get("type")
        if kind == "session_configured" and isinstance(msg.get("session_id"), str):
            self.session_id = msg["session_id"]
            return [{"type": "status", "state": "running", "session_id": self.session_id}]
        if kind == "agent_message" and isinstance(msg.get("message"), str):
            return [{"type": "message", "role": "assistant", "text": msg["message"]}]
        if kind == "agent_reasoning" and isinstance(msg.get("text"), str):
            return [{"type": "thinking", "text": msg["text"]}]
        if kind == "exec_command_begin":
            command = msg.get("command")
            command = " ".join(command) if isinstance(command, list) else str(command or "")
            return [{"type": "tool", "id": str(msg.get("call_id") or ""), "name": "shell",
                     "title": command[:200], "input": {"command": command}, "status": "running"}]
        if kind == "exec_command_end":
            code = msg.get("exit_code")
            output = msg.get("aggregated_output") or msg.get("stdout") or ""
            return [{"type": "tool", "id": str(msg.get("call_id") or ""), "status": "ok" if code == 0 else "error",
                     "output": trunc(output, TOOL_OUTPUT_MAX)}]
        if kind == "task_complete":
            self._turn_done, self.turn_active, self.stopping = True, False, False
            return [{"type": "status", "state": "done", "session_id": self.session_id}]
        if kind == "error":
            return [{"type": "error", "text": str(msg.get("message") or "Codex 报告了一个错误")}]
        return []

    def exited(self, code: int | None, stderr_tail: str) -> list[dict]:
        if self._turn_done:  # turn.completed / turn.failed already closed the turn
            return []
        return super().exited(code, stderr_tail)


def _codex_status(status: Any, exit_code: Any, done: bool) -> str:
    status = str(status or "")
    if status in ("failed", "declined") or (done and isinstance(exit_code, int) and exit_code != 0):
        return "error"
    if done or status == "completed":
        return "ok"
    return "running"


def _codex_approval_flag(argv: list[str]) -> list[str]:
    """``codex exec`` has no ``--ask-for-approval`` (exec never asks); the same setting as a config override."""
    out: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in ("--ask-for-approval", "-a") and index + 1 < len(argv) and index > 0:
            out += ["-c", f'approval_policy="{argv[index + 1]}"']
            index += 2
            continue
        out.append(item)
        index += 1
    return out


# ---------------------------------------------------------------------------------------------- acp

class Acp(Adapter):
    """An Agent Client Protocol client (§12): ``initialize`` → ``session/new`` | ``session/load`` →
    ``session/prompt`` per turn; ``session/update`` notifications become events; the agent's
    ``session/request_permission`` becomes an approval answered later; ``session/cancel`` stops a turn."""
    name = "acp"
    persistent = True
    in_band_stop = True

    def __init__(self, d: AgentDef, opts: AdapterOptions):
        super().__init__(d, opts)
        self.ready = False
        self.failed: str | None = None  # why the handshake failed (the runner falls back)
        self._next_id = 0
        self._calls: dict[int, str] = {}
        self._queued: list[str] = []
        self.embedded_context = False
        self._loading = False
        self._permissions: dict[str, dict] = {}
        self._approval_n = 0
        self._tools: dict[str, dict] = {}
        self.can_load = False

    def argv(self, prompt: str | None = None) -> list[str]:
        return registry.command_argv(self.d, self.opts.binary, permission=self.opts.permission,
                                     values=self._values(), template="command")

    def _system_prompt_in_argv(self) -> bool:
        return False  # ACP has no system prompt: it goes in front of the first message

    def _call(self, method: str, params: dict) -> None:
        self._next_id += 1
        self._calls[self._next_id] = method
        self.outbox.append(_dumps({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}))

    def start(self) -> list[dict]:
        # A fresh process: the first prompt waits for session/new | session/load, whatever an earlier
        # process of this run had reached.
        self.ready, self.failed, self._loading = False, None, False
        self._calls.clear()
        self._call("initialize", {"protocolVersion": 1,
                                  "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                                         "terminal": False},
                                  "clientInfo": {"name": "agent-nexus", "version": "1"}})
        return []

    def begin_turn(self, prompt: str) -> list[dict]:
        self.turn_active, self.stopping = True, False
        self.turns += 1
        if not self.ready:
            self._queued.append(prompt)
            return []
        self._prompt(prompt)
        return []

    def _prompt(self, text: str) -> None:
        """One ``session/prompt``. ACP has no system-prompt field: on the first turn the console's system prompt
        goes as an embedded ``resource`` block when the agent accepts them (so the agent's own title and history
        show the user's words, seen with dsh), else in front of the text."""
        blocks: list[dict] = []
        if self._prefix_pending and getattr(self, "embedded_context", False):
            self._prefix_pending = False
            blocks.append({"type": "resource", "resource": {"uri": "anx://system-prompt", "mimeType": "text/markdown",
                                                            "text": self.opts.system_prompt}})
        blocks.insert(0, {"type": "text", "text": self._with_prefix(text)})
        self._call("session/prompt", {"sessionId": self.session_id, "prompt": blocks})

    def stop(self) -> list[dict]:
        self.stopping = True
        events: list[dict] = []
        for approval_id in list(self._permissions):
            entry = self._permissions.pop(approval_id)
            self.outbox.append(_dumps({"jsonrpc": "2.0", "id": entry["rpc_id"],
                                       "result": {"outcome": {"outcome": "cancelled"}}}))
            events.append({"type": "approval", "id": approval_id, "state": "denied", "reason": "已停止"})
        if self.turn_active and self.session_id:
            self.outbox.append(_dumps({"jsonrpc": "2.0", "method": "session/cancel",
                                       "params": {"sessionId": self.session_id}}))
        if self._queued:  # stopped before the session was ready
            self._queued.clear()
            self.turn_active, self.stopping = False, False
            events.append({"type": "status", "state": "stopped", "session_id": self.session_id})
        return events

    def feed(self, line: str) -> list[dict]:
        obj = _json(line)
        if not isinstance(obj, dict) or obj.get("jsonrpc") not in ("2.0", None):
            return []
        self.saw_output = True
        if "method" in obj:
            if "id" in obj and obj["id"] is not None:
                return self._request(obj)
            return self._notification(obj)
        if "id" in obj:
            return self._response(obj)
        return []

    # -- responses to our calls
    def _response(self, obj: dict) -> list[dict]:
        method = self._calls.pop(obj.get("id"), None) if isinstance(obj.get("id"), int) else None
        error = obj.get("error") if isinstance(obj.get("error"), dict) else None
        result = obj.get("result")
        if method == "initialize":
            if error is not None:
                self.failed = str(error.get("message") or "initialize 失败")
                return []
            caps = result.get("agentCapabilities") if isinstance(result, dict) else None
            self.can_load = bool(isinstance(caps, dict) and caps.get("loadSession"))
            prompt_caps = caps.get("promptCapabilities") if isinstance(caps, dict) else None
            self.embedded_context = bool(isinstance(prompt_caps, dict) and prompt_caps.get("embeddedContext"))
            cwd = self.opts.workdir
            if self.session_id and self.can_load:
                self._loading = True
                self._call("session/load", {"sessionId": self.session_id, "cwd": cwd, "mcpServers": []})
            else:
                if self.session_id:
                    self.notices.append(f"{self.d.label} 不支持续接旧会话，已开始一个新会话")
                    self.session_id = None
                self._call("session/new", {"cwd": cwd, "mcpServers": []})
            return []
        if method in ("session/new", "session/load"):
            self._loading = False
            if error is not None:
                if method == "session/load":  # the old session is gone: start afresh rather than fail
                    self.notices.append(f"无法续接会话 {self.session_id}：{error.get('message') or '代理拒绝了'}；"
                                        "已开始一个新会话")
                    self.session_id = None
                    self._call("session/new", {"cwd": self.opts.workdir, "mcpServers": []})
                    return self._drain_notices()
                self.failed = str(error.get("message") or "无法创建会话")
                return []
            if method == "session/new":
                session_id = result.get("sessionId") if isinstance(result, dict) else None
                self.session_id = str(session_id) if session_id else None
            self.ready = True
            events = self._drain_notices()
            events.append({"type": "status", "state": "running" if self._queued else "idle",
                           "session_id": self.session_id})
            for text in self._queued:
                self._prompt(text)
            self._queued.clear()
            return events
        if method == "session/prompt":
            was_stopping = self.stopping
            self.turn_active, self.stopping = False, False
            events: list[dict] = []
            for approval_id in list(self._permissions):  # the turn ended while asking: the request is moot
                self._permissions.pop(approval_id)
                events.append({"type": "approval", "id": approval_id, "state": "denied", "reason": "这一轮已结束"})
            if error is not None:
                if was_stopping:
                    return events + [{"type": "status", "state": "stopped", "session_id": self.session_id}]
                return events + [{"type": "error", "text": str(error.get("message") or "这一轮失败了")},
                                 {"type": "status", "state": "failed", "session_id": self.session_id}]
            reason = result.get("stopReason") if isinstance(result, dict) else None
            usage = _usage(result.get("usage")) if isinstance(result, dict) and result.get("usage") else None
            state = "stopped" if reason == "cancelled" or was_stopping else "done"
            status = {"type": "status", "state": state, "session_id": self.session_id}
            if usage:
                status["usage"] = usage
            if reason in ("refusal",):
                events.append({"type": "error", "text": f"{self.d.label} 拒绝了这个请求"})
            elif reason in ("max_tokens", "max_turn_requests"):
                events.append({"type": "error", "text": f"{self.d.label} 这一轮达到了上限（{reason}）"})
            return events + [status]
        return []

    def _drain_notices(self) -> list[dict]:
        events = [{"type": "error", "text": text, "level": "warn"} for text in self.notices]
        self.notices.clear()
        return events

    # -- requests from the agent
    def _request(self, obj: dict) -> list[dict]:
        method, rpc_id = obj.get("method"), obj.get("id")
        params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
        if method == "session/request_permission":
            self._approval_n += 1
            approval_id = f"ap_{self._approval_n}"
            call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
            options = [option for option in params.get("options") or [] if isinstance(option, dict)]
            self._permissions[approval_id] = {"rpc_id": rpc_id, "options": options}
            raw = call.get("rawInput")
            known = self._tools.get(str(call.get("toolCallId") or ""), {})
            title = str(call.get("title") or known.get("title") or "")
            tool = (_acp_tool_name(call.get("kind"), title) if call.get("kind") else known.get("name")) or title or "tool"
            summary = tool_title("", raw) if isinstance(raw, dict) and tool_title("", raw) else title
            return [{"type": "approval", "id": approval_id, "tool": tool, "summary": summary,
                     "input": _small(raw) if raw is not None else None, "state": "pending",
                     "options": [{"id": o.get("optionId"), "name": o.get("name"), "kind": o.get("kind")}
                                 for o in options]}]
        self.outbox.append(_dumps({"jsonrpc": "2.0", "id": rpc_id,
                                   "error": {"code": -32601, "message": f"method not supported: {method}"}}))
        return []

    def answer(self, approval_id: str, allow: bool, remember: bool) -> list[dict] | None:
        entry = self._permissions.pop(approval_id, None)
        if entry is None:
            return None
        options = entry["options"]
        wanted = (["allow_always", "allow_once"] if remember else ["allow_once", "allow_always"]) if allow else \
            (["reject_always", "reject_once"] if remember else ["reject_once", "reject_always"])
        chosen = next((o for kind in wanted for o in options if o.get("kind") == kind), None)
        if chosen is None:
            prefix = "allow" if allow else "reject"
            chosen = next((o for o in options if str(o.get("kind") or "").startswith(prefix)), None)
        if chosen is None and not allow:
            outcome: dict = {"outcome": "cancelled"}
        elif chosen is None:
            chosen = options[0] if options else {"optionId": "allow"}
            outcome = {"outcome": "selected", "optionId": chosen.get("optionId")}
        else:
            outcome = {"outcome": "selected", "optionId": chosen.get("optionId")}
        self.outbox.append(_dumps({"jsonrpc": "2.0", "id": entry["rpc_id"], "result": {"outcome": outcome}}))
        return [{"type": "approval", "id": approval_id, "state": "allowed" if allow else "denied",
                 "remembered": remember}]

    def pending_approvals(self) -> list[str]:
        return list(self._permissions)

    # -- notifications
    def _notification(self, obj: dict) -> list[dict]:
        if obj.get("method") != "session/update" or self._loading:
            return []
        params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = text_of(update.get("content"))
            return [{"type": "message", "role": "assistant", "text": text, "delta": True}] if text else []
        if kind == "agent_thought_chunk":
            text = text_of(update.get("content"))
            return [{"type": "thinking", "text": text, "delta": True}] if text else []
        if kind in ("tool_call", "tool_call_update"):
            return self._tool(update, kind == "tool_call")
        if kind == "plan":
            entries = update.get("entries") if isinstance(update.get("entries"), list) else []
            marks = {"completed": "[x] ", "in_progress": "[-] "}
            lines = [marks.get(str(entry.get("status")), "[ ] ") + str(entry.get("content") or "")
                     for entry in entries if isinstance(entry, dict)]
            return [{"type": "tool", "id": "plan", "name": "plan", "title": "计划", "status": "ok",
                     "output": "\n".join(lines)}]
        return []

    def _tool(self, update: dict, new: bool) -> list[dict]:
        tool_id = str(update.get("toolCallId") or "")
        known = self._tools.setdefault(tool_id, {})
        event: dict[str, Any] = {"type": "tool", "id": tool_id}
        if update.get("title") is not None:
            known["title"] = event["title"] = str(update["title"])[:200]
        if update.get("kind") is not None:
            known["name"] = event["name"] = _acp_tool_name(update.get("kind"), update.get("title") or known.get("title"))
        elif new:
            event["name"] = known.get("name") or "tool"
        if update.get("rawInput") is not None:
            event["input"] = _small(update["rawInput"])
            if not event.get("title") and not known.get("title"):
                known["title"] = event["title"] = tool_title("", update["rawInput"])
        status = update.get("status")
        if status is not None:
            event["status"] = {"completed": "ok", "failed": "error"}.get(str(status), "running")
        elif new:
            event["status"] = "running"
        events: list[dict] = []
        texts: list[str] = []
        for item in update.get("content") if isinstance(update.get("content"), list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "diff" and isinstance(item.get("path"), str):
                old = item.get("oldText") if isinstance(item.get("oldText"), str) else None
                new_text = item.get("newText") if isinstance(item.get("newText"), str) else ""
                events.append({"type": "file_change", "path": item["path"],
                               "kind": "add" if old is None else "delete" if not new_text else "edit",
                               "diff": unified_diff(item["path"], old or "", new_text)})
            elif item.get("type") == "content":
                texts.append(text_of(item.get("content")))
            elif item.get("type") == "terminal":
                texts.append(f"[终端 {item.get('terminalId') or ''}]")
        raw_output = update.get("rawOutput")
        if texts:
            event["output"] = trunc("\n".join(t for t in texts if t), TOOL_OUTPUT_MAX)
        elif raw_output is not None and status in ("completed", "failed"):
            event["output"] = trunc(raw_output if isinstance(raw_output, str)
                                    else json.dumps(raw_output, ensure_ascii=False, default=str), TOOL_OUTPUT_MAX)
        return [event, *events]

    def reset_process(self) -> None:
        self.ready, self._loading = False, False
        self._calls.clear()
        self._queued.clear()
        self._permissions.clear()
        self._tools.clear()

    def exited(self, code: int | None, stderr_tail: str) -> list[dict]:
        events: list[dict] = []
        for approval_id in list(self._permissions):
            self._permissions.pop(approval_id)
            events.append({"type": "approval", "id": approval_id, "state": "denied", "reason": "进程已退出"})
        self._queued.clear()
        self.ready = False
        return events + super().exited(code, stderr_tail)


# ---------------------------------------------------------------------------------------------- text / jsonl

_SESSION_KEYS = ("sessionId", "session_id", "threadId", "thread_id")
_TEXT_KEYS = ("text", "delta", "content", "message", "output", "result")


class Text(Adapter):
    """A command per turn; stdout lines are the answer. Lines that are JSON objects are read leniently
    (``headless --json`` style): a session id is remembered, text-like fields are shown, the rest skipped."""
    name = "text"

    def __init__(self, d: AgentDef, opts: AdapterOptions, *, template: str = "command"):
        self.template = template  # before the base reads _system_prompt_in_argv()
        super().__init__(d, opts)
        self._stdin: bytes | None = None

    def _system_prompt_in_argv(self) -> bool:
        return self.template == "command" and super()._system_prompt_in_argv()

    def argv(self, prompt: str | None = None) -> list[str]:
        self.turn_active, self.stopping = True, False
        self.turns += 1
        text = self._with_prefix(prompt or "")
        if self.opts.batch:
            from ..link.wake.adapters import safe_prompt
            text = safe_prompt(text, [self.opts.binary])
        if text.startswith("-"):
            # The prompt is its own argv item: a message such as "--yolo …" must not read as an option.
            # Not every CLI takes "--", and a leading space changes nothing for the model.
            text = " " + text
        values = self._values(text)
        argv = registry.command_argv(self.d, self.opts.binary, permission=self.opts.permission,
                                     values=values, template=self.template)
        if self.session_id and "{resume_id}" not in " ".join(getattr(self.d, self.template, None) or []) \
                and self.d.kind == "dsh" and "--session-id" not in argv:
            argv[-1:-1] = ["--session-id", self.session_id]  # dsh headless resumes with --session-id (§14)
        return argv

    def feed(self, line: str) -> list[dict]:
        self.saw_output = True
        obj = _json(line)
        if isinstance(obj, dict):
            return self._json_line(obj)
        if obj is not None:
            return []
        return [{"type": "message", "role": "assistant", "text": line + "\n", "delta": True}]

    def _json_line(self, obj: dict) -> list[dict]:
        events: list[dict] = []
        for key in _SESSION_KEYS:
            value = obj.get(key)
            if isinstance(value, str) and value and value != self.session_id:
                self.session_id = value
                events.append({"type": "status", "state": "running", "session_id": value})
                break
        kind = str(obj.get("type") or obj.get("event") or "").lower()
        text = ""
        for key in _TEXT_KEYS:
            value = obj.get(key)
            if isinstance(value, str) and value:
                text = value
                break
            if isinstance(value, (dict, list)):
                text = text_of(value)
                if text:
                    break
        if not text:
            return events
        if "error" in kind:
            return events + [{"type": "error", "text": text}]
        if "think" in kind or "reason" in kind:
            return events + [{"type": "thinking", "text": text, "delta": True}]
        if "user" in kind or obj.get("role") == "user":
            return events
        # A chunk continues the answer; a whole message (no "delta"/"chunk" about it) starts a new block.
        chunk = "delta" in kind or "chunk" in kind or isinstance(obj.get("delta"), str)
        event = {"type": "message", "role": "assistant", "text": text}
        if chunk:
            event["delta"] = True
        return events + [event]


def make(d: AgentDef, opts: AdapterOptions, *, adapter: str | None = None) -> Adapter:
    """The adapter for ``d`` (or the named one)."""
    name = adapter or d.adapter
    if name == "claude-stream":
        return ClaudeStream(d, opts)
    if name == "codex-exec":
        return CodexExec(d, opts)
    if name == "acp":
        return Acp(d, opts)
    return Text(d, opts)


def _acp_tool_name(kind: Any, title: Any) -> str:
    """ACP's ``kind`` (read / edit / execute / ... / other) names the tool, except ``other``: then the title does
    (dsh reports its ``read`` tool as kind ``other``, title ``read``)."""
    kind = str(kind or "")
    if kind in ("", "other") and title:
        return str(title)[:60]
    return kind or "tool"


def fallback(d: AgentDef, opts: AdapterOptions, session_id: str | None) -> Adapter | None:
    """What an ``acp`` CLI degrades to when the protocol does not start: its ``fallback_command`` as text."""
    if not d.fallback_command:
        return None
    adapter = Text(d, opts, template="fallback_command")
    adapter.session_id = session_id
    return adapter


__all__ = [
    "APPROVAL_TOOL",
    "Acp",
    "Adapter",
    "AdapterOptions",
    "ClaudeStream",
    "CodexExec",
    "Text",
    "fallback",
    "make",
    "text_of",
    "tool_title",
    "trunc",
    "unified_diff",
]

