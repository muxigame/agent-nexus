"""The agent loop of an API model (docs/agent-chat.md §16.4), as a runner adapter without a process.

:class:`ApiAdapter` is what :func:`agents.adapters.make` returns for adapter ``api``. The runner attaches it to its
run (:meth:`ApiAdapter.attach`) and, instead of spawning a CLI, calls :meth:`begin_turn`, which starts an asyncio
task on the runner's loop. One turn:

1. the user message goes into the conversation (and the session file, :mod:`.store`);
2. the provider is asked, streaming (:func:`.protocols.stream_turn`); text / thinking deltas become ``message`` /
   ``thinking`` events as they arrive;
3. each tool call becomes a ``tool`` event; in the modes that need it (:func:`.tools.needs_approval`) it waits for
   an approval card through the runner's own mechanism (:meth:`Runs.approval_request`: the same ``approval``
   events, ``agent_run_answer``, "always allow" scope and 10-minute timeout as the CLIs), runs locally
   (:class:`.tools.Toolbox`) and its result goes back to the model;
4. repeat until the model calls no tool (at most :data:`MAX_STEPS` requests), then a ``usage`` event (§16.3) and
   ``status done``.

Errors (§16.4): 401/403 say the key is wrong; 429 is shown and retried once after ``retry-after``; network errors
name the provider's host. :meth:`stop` cancels the task (a running command is killed); the tool calls of the
step that already ran keep their results and the rest are answered "stopped", so the next turn's history is
valid (and :func:`.store.repair` runs over it at the start of every turn as a backstop).

Every step's finished text and thinking are also emitted whole (non-delta, the same ``id`` as its deltas), so a
console that re-attaches after the run's event buffer dropped the oldest deltas still shows the full reply.
Tool-call ids are made unique in the session (a provider that numbers its calls per request, or gives none).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from ..adapters import Adapter, AdapterOptions, _small, tool_title, trunc
from ..registry import AgentDef
from . import models as model_table
from . import protocols, tools
from .protocols import ApiError, TurnResult
from .store import SessionStore, new_id, repair

log = logging.getLogger(__name__)

MAX_STEPS = 50
RETRY_AFTER_DEFAULT_S = 5.0
RETRY_AFTER_MAX_S = 60.0
TOOL_OUTPUT_MAX = 8000  # in the chat event; the model gets up to tools.OUTPUT_MAX
ASK_WAIT_S = 600.0  # ask_user waits this long for the owner's answer, like an approval card
ASK_POLL_S = 3.0
STOPPED = "（用户停止了这一轮，没有执行）"


class ApiAdapter(Adapter):
    name = "api"
    persistent = False
    in_band_stop = True
    #: driven in-process by the runner: no argv, no process
    in_process = True

    def __init__(self, d: AgentDef, opts: AdapterOptions):
        super().__init__(d, opts)
        self._prefix_pending = False  # the system prompt goes into the protocol's system field
        spec = opts.extra.get("api") if isinstance(opts.extra.get("api"), dict) else {}
        self.provider: dict = spec.get("provider") or {}
        self.model: dict = spec.get("model") or {}
        self._key: Callable[[], str | None] = spec.get("key") or (lambda: None)
        self.store: SessionStore | None = spec.get("store")
        self._client_factory: Callable[[], Any] = spec.get("client") or protocols.client
        self.retry_cap_s: float = float(spec.get("retry_cap_s") or RETRY_AFTER_MAX_S)
        self.max_steps: int = int(spec.get("max_steps") or MAX_STEPS)
        self.deny: list[str] = list(spec.get("deny") or [])
        #: the run's gateway handle (its mailbox address) and ``hub_call``: ask_user / mail_send
        self.handle: str = str(spec.get("handle") or "")
        self._gateway: Callable[[str, dict], Any] | None = spec.get("gateway") if self.handle else None
        self.ask_wait_s: float = float(spec.get("ask_wait_s") or ASK_WAIT_S)
        self.ask_poll_s: float = float(spec.get("ask_poll_s") or ASK_POLL_S)
        self._uid = uuid.uuid4().hex[:6]  # event ids of this run never collide with a resumed session's
        self.session_id = opts.resume_id or new_id()
        self._history: list[dict] | None = None
        self._task: asyncio.Task | None = None
        self._stopping_task: asyncio.Task | None = None
        self._runs = None
        self._run = None
        self._toolbox: tools.Toolbox | None = None
        self.last_used: int | None = None
        self._last_usage: dict | None = None

    # -- wiring
    def attach(self, runs: Any, run: Any) -> None:
        self._runs, self._run = runs, run
        self._toolbox = tools.Toolbox(run.workdir, run.tmpdir, env=run.env, run_as=run.run_as, deny=self.deny)

    def argv(self, prompt: str | None = None) -> list[str]:  # never spawned
        return []

    def _emit(self, events: list[dict]) -> None:
        if self._runs is not None and self._run is not None and not self._run.closed:
            self._runs._push_all(self._run, events)

    def _save(self, *, events: list[dict] = (), messages: list[dict] = ()) -> None:
        if self.store is None:
            return
        records = [{"type": "event", "event": {**e, "t": e.get("t") or round(time.time(), 3)}} for e in events] + \
                  [{"type": "msg", "msg": m} for m in messages]
        try:
            self.store.append(self.session_id, records, meta={
                "kind": self.d.kind, "label": self.d.label, "provider": self.provider.get("id"),
                "model": self.model.get("id"), "cwd": self.opts.workdir})
        except (OSError, LookupError) as exc:
            log.warning("agents: could not save API session %s: %s", self.session_id, exc)

    def _load(self) -> list[dict]:
        if self._history is None:
            self._history = []
            if self.opts.resume_id and self.store is not None:
                try:
                    _meta, self._history = self.store.load(self.opts.resume_id)
                except (LookupError, OSError):
                    self._history = []
        return self._history

    # -- turns
    def begin_turn(self, prompt: str) -> list[dict]:
        self.turn_active, self.stopping = True, False
        self.turns += 1
        previous, self._stopping_task = self._stopping_task, None
        self._task = asyncio.get_running_loop().create_task(self._turn(prompt, self.turns, previous))
        return [{"type": "status", "state": "running", "session_id": self.session_id}]

    def stop(self) -> list[dict]:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            self._stopping_task = task  # the next turn waits until it has tidied the history up
        was = self.turn_active
        self.turn_active = self.stopping = False
        return [{"type": "status", "state": "stopped", "session_id": self.session_id}] if was else []

    def shutdown(self) -> None:
        """The run closed: end the turn without telling anybody."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
        self.turn_active = False

    def exited(self, code: int | None, stderr_tail: str) -> list[dict]:
        return []

    def _limit(self) -> int | None:
        return self.model.get("context_window") or model_table.context_window(self.model.get("id"))

    def _offered(self) -> list[dict]:
        if self.model.get("tools") is False:
            return []
        return tools.specs(self.opts.permission, gateway=self._gateway is not None)

    async def _request(self, http: Any, history: list[dict], ids: dict, partial: dict) -> TurnResult:
        """One streamed request, retried once on 429 after ``retry-after``."""
        def on_delta(kind: str, text: str) -> None:
            partial[kind] = partial.get(kind, "") + text
            if kind == "text":
                self._emit([{"type": "message", "role": "assistant", "text": text, "delta": True, "id": ids["text"]}])
            else:
                self._emit([{"type": "thinking", "text": text, "delta": True, "id": ids["thinking"]}])

        for attempt in (1, 2):
            try:
                return await protocols.stream_turn(http, self.provider, self.model, self._key(),
                                                   self.opts.system_prompt, history, self._offered(), on_delta)
            except ApiError as exc:
                if exc.status != 429 or attempt == 2:
                    raise
                wait = exc.retry_after if exc.retry_after is not None else RETRY_AFTER_DEFAULT_S
                wait = max(0.0, min(wait, self.retry_cap_s))
                self._emit([{"type": "error", "level": "warn",
                             "text": f"{exc}；{wait:.0f} 秒后自动重试一次"}])
                await asyncio.sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _turn(self, prompt: str, turn_no: int, previous: asyncio.Task | None = None) -> None:
        if previous is not None and not previous.done():
            await asyncio.wait([previous], timeout=10)
        history = self._load()
        history[:] = repair(history)  # a tool call left without its result never reaches the provider
        user_msg = {"role": "user", "text": prompt}
        history.append(user_msg)
        self._save(events=[{"type": "user", "text": prompt}], messages=[user_msg])
        totals = {"input": 0, "output": 0, "cached": 0}
        # the step in progress, where the stop handler sees it: the calls still to run and the results so far
        step_state: dict = {"pending": [], "results": []}
        partial: dict = {"text": "", "thinking": ""}
        seen = {c.get("id") for m in history if m.get("role") == "assistant" for c in m.get("tool_calls") or []}
        try:
            async with self._client_factory() as http:
                for step in range(1, self.max_steps + 1):
                    ids = {"text": f"{self._uid}.t{turn_no}.{step}:text",
                           "thinking": f"{self._uid}.t{turn_no}.{step}:thinking"}
                    partial["text"] = partial["thinking"] = ""
                    result = await self._request(http, history, ids, partial)
                    for call in result.tool_calls:  # unique in the session: tool cards are keyed by it
                        if not call.id or call.id in seen:
                            call.id = protocols.call_id()
                        seen.add(call.id)
                    usage = result.usage or {}
                    for name in totals:
                        totals[name] += int(usage.get(name) or 0)
                    if usage.get("input") or usage.get("output"):
                        self.last_used = int(usage.get("input") or 0) + int(usage.get("output") or 0)
                    message = result.message()
                    history.append(message)
                    partial["text"] = partial["thinking"] = ""
                    finished = [e for e in ({"type": "thinking", "text": result.thinking, "id": ids["thinking"]}
                                            if result.thinking else None,
                                            {"type": "message", "role": "assistant", "text": result.text,
                                             "id": ids["text"]} if result.text else None) if e]
                    self._save(events=finished, messages=[message])
                    # whole, after the deltas: a console re-reading a buffer that lost the first deltas still
                    # gets the full text (chat.js replaces the block of the same id)
                    self._emit(finished + self._usage_events(totals, result.model))
                    if not result.tool_calls:
                        if result.stop_reason in ("length", "max_tokens", "MAX_TOKENS"):
                            self._emit([{"type": "error", "level": "warn", "text": "回答达到了输出长度上限，被截断了"}])
                        break
                    step_state["pending"] = [call.neutral() for call in result.tool_calls]
                    step_state["results"] = []
                    while step_state["pending"]:
                        step_state["results"].append(await self._call_tool(step_state["pending"][0]))
                        step_state["pending"].pop(0)
                    tool_msg = {"role": "tool", "results": step_state["results"]}
                    step_state["results"] = []
                    history.append(tool_msg)
                    self._save(messages=[tool_msg])
                else:
                    self._emit([{"type": "error", "level": "warn",
                                 "text": f"已经连续调用工具 {self.max_steps} 步，这一轮先停在这里；可以发消息让它继续"}])
            self._finish("done", totals)
        except asyncio.CancelledError:
            self._stopped(history, step_state, partial)
            raise
        except ApiError as exc:
            self._fail(str(exc), totals, code=exc.code)
        except Exception as exc:
            log.exception("agents: API turn failed")
            self._fail(f"出错了：{exc}", totals, code="internal")

    def _usage_events(self, totals: dict, model: str) -> list[dict]:
        from .. import sessions
        turn = {k: v for k, v in totals.items() if v}
        self._last_usage = sessions.usage_event(self.last_used, self._limit(), model or self.model.get("id"), turn or None)
        return [self._last_usage]

    def _finish(self, state: str, totals: dict) -> None:
        self.turn_active = False
        self._task = None
        status = {"type": "status", "state": state, "session_id": self.session_id,
                  "usage": {"input": totals["input"], "output": totals["output"], "cost_usd": 0}}
        usage, self._last_usage = self._last_usage, None
        self._save(events=[usage, status] if usage else [status])
        self._emit([status])

    def _fail(self, text: str, totals: dict, *, code: str) -> None:
        error = {"type": "error", "text": text, "code": code}
        self._save(events=[error])
        self._emit([error])
        self._finish("failed", totals)

    def _stopped(self, history: list[dict], step_state: dict, partial: dict) -> None:
        """Cancelled (stop / close): keep the history valid for the next turn (and what was already shown).

        The streamed part of the answer is kept (an assistant turn only when it has text: providers refuse empty
        ones); the calls of the step that already ran keep their real results, the rest are answered "stopped"
        -- one tool message matching the assistant's tool_calls."""
        events = [e for e in ({"type": "thinking", "text": partial["thinking"]} if partial.get("thinking") else None,
                              {"type": "message", "role": "assistant", "text": partial["text"]}
                              if partial.get("text") else None) if e]
        messages: list[dict] = []
        if partial.get("text"):
            messages.append({"role": "assistant", "text": partial["text"]})
        done = list(step_state.get("results") or [])
        pending = list(step_state.get("pending") or [])
        if done or pending:
            answered = {r["id"] for r in done}
            messages.append({"role": "tool", "results": done + [
                {"id": c["id"], "name": c["name"], "output": STOPPED, "error": True}
                for c in pending if c["id"] not in answered]})
        history.extend(messages)
        step_state["pending"], step_state["results"] = [], []
        self._save(events=[*events, {"type": "status", "state": "stopped", "session_id": self.session_id}],
                   messages=messages)

    async def _call_tool(self, call: dict) -> dict:
        name, args, call_id = call["name"], call.get("args") or {}, call["id"]
        title = tool_title(name, args)
        running = {"type": "tool", "id": call_id, "name": name, "title": title, "input": _small(args),
                   "status": "running"}
        self._emit([running])
        permission = self.opts.permission
        if not tools.allowed(name, permission) or (self.model.get("tools") is False):
            outcome = tools.Outcome(f"当前权限模式下不能使用 {name}", error=True)
        else:
            denied = None
            if tools.needs_approval(name, permission):
                denied = await self._approve(name, args, call_id)
            if denied is not None:
                outcome = tools.Outcome(denied, error=True)
            elif name in tools.GATEWAY_TOOLS:
                outcome = await self._gateway_tool(name, args)
            else:
                outcome = await self._toolbox.run(name, args) if self._toolbox else \
                    tools.Outcome("工具不可用", error=True)
        done = {"type": "tool", "id": call_id, "name": name, "title": title, "status": "error" if outcome.error else "ok",
                "output": trunc(outcome.output, TOOL_OUTPUT_MAX)}
        self._emit([done, *outcome.events])
        self._save(events=[{**running, **done}, *outcome.events])
        return {"id": call_id, "name": name, "output": outcome.output, "error": outcome.error}

    async def _gateway_tool(self, name: str, args: dict) -> tools.Outcome:
        """ask_user / mail_send through the gateway, as this run's mailbox address (§10, §16.4)."""
        if self._gateway is None:
            return tools.Outcome("这个对话没有网关邮箱，ask_user / mail_send 不可用", error=True)
        if name == "mail_send":
            sent = await self._gateway("agent_mail_send", {
                "workspace_id": self.handle, "to": args.get("to"), "body": args.get("body"),
                "subject": args.get("subject") or "", "kind": args.get("kind") or "message"})
            if not isinstance(sent, dict) or "error" in sent:
                return tools.Outcome(f"发送失败：{(sent or {}).get('error') or '网关不可达'}", error=True)
            to = "、".join(str(d.get("to_display") or d.get("to")) for d in sent.get("deliveries") or []
                          if isinstance(d, dict)) or str(args.get("to"))
            unknown = sent.get("unknown_recipients") or []
            return tools.Outcome(f"已发送给 {to}（message_id {sent.get('message_id') or '?'}）"
                                 + (f"；这些地址不存在：{'、'.join(map(str, unknown))}" if unknown else ""))
        question = args.get("question")
        if not isinstance(question, str) or not question.strip():
            return tools.Outcome("缺少 question", error=True)
        options = [str(o) for o in args.get("options") or [] if isinstance(o, (str, int, float))] \
            if isinstance(args.get("options"), list) else []
        created = await self._gateway("agent_ask", {
            "workspace_id": self.handle, "question": question, "options": options or None,
            "recommended": args.get("recommended"), "context": args.get("context") or ""})
        if not isinstance(created, dict) or "error" in created or not created.get("ask_id"):
            return tools.Outcome(f"提问失败：{(created or {}).get('error') or '网关不可达'}", error=True)
        ask_id = created["ask_id"]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.ask_wait_s
        while loop.time() < deadline:
            await asyncio.sleep(self.ask_poll_s)
            state = await self._gateway("ask_get", {"ask_id": ask_id})
            if not isinstance(state, dict) or "error" in state or state.get("state") == "open":
                continue
            answer = state.get("answer") if isinstance(state.get("answer"), dict) else {}
            if state.get("state") != "answered":
                return tools.Outcome(f"这个问题已{state.get('state')}，没有回答", error=True)
            text = answer.get("text") or answer.get("choice") or "、".join(answer.get("choices") or []) or ""
            if answer.get("choice") and answer.get("text"):
                text = f"{answer['choice']}：{answer['text']}"
            return tools.Outcome(f"主人回答：{text}")
        return tools.Outcome(f"主人 {int(self.ask_wait_s // 60)} 分钟内没有回答（问题还在收件箱里）；先按你的判断继续，"
                             "或者告诉用户你在等什么", error=True)

    async def _approve(self, name: str, args: dict, call_id: str) -> str | None:
        """None when allowed, else the reason the model is told."""
        if self._runs is None or self._run is None:
            return "无法请求审批"
        answer = await self._runs.approval_request(self._run.token, {"tool_name": name, "input": args,
                                                                     "tool_use_id": call_id})
        if answer.get("behavior") == "allow":
            return None
        return str(answer.get("message") or "用户拒绝了这个操作")


__all__ = ["MAX_STEPS", "ApiAdapter"]
