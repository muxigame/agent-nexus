"""AgentHub (agent_nexus/agents/hub.py): the agents/1 handlers end to end with fake CLIs (docs/agent-chat.md §13).

No real claude / codex / dsh / gemini runs: PATH holds only /bin/sh shims that start
tests/fixtures/fake_agents/hub_cli.py, and agent_cli's extra install folders are hidden.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from agent_nexus import agent_cli
from agent_nexus.agents import hub as hub_mod
from agent_nexus.agents import registry, sessions
from agent_nexus.agents.hub import OPS, AgentHub

FAKES = Path(__file__).parent / "fixtures" / "fake_agents"
SESSIONS_HOME = Path(__file__).parent / "fixtures" / "agent_sessions" / "home"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake CLIs are /bin/sh shims")


def shim(folder: Path, name: str, kind: str) -> Path:
    path = folder / name
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKES / "hub_cli.py"}" {kind} "$@"\n', encoding="utf-8")
    path.chmod(0o755)
    return path


class FakeGateway:
    """``hub_call`` to a gateway: hands out a handle and address, or (``away``) is not there."""

    def __init__(self, away: bool = False):
        self.away = away
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, op: str, args: dict):
        self.calls.append((op, dict(args)))
        if self.away:
            return None
        name = args.get("name") or f"{args['kind']}-{os.path.basename(args['workdir'])}"
        return {"workspace_id": f"h-{len(self.calls)}", "agent_id": args["device_id"], "workspace": "default",
                "path": args["workdir"], "address": f"{name}@pc-1", "address_display": f"{name}@笔记本",
                "display_name": "笔记本", "device_display": "笔记本"}


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    shutil.copytree(SESSIONS_HOME, home)
    (home / "proj").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim(bin_dir, "codex", "codex")
    shim(bin_dir, "claude", "claude")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(bin_dir))
    for key in ("CODEX_HOME", "DSH_HOME", "CLAUDE_CONFIG_DIR", "GEMINI_CLI_HOME", "ANTHROPIC_API_KEY",
                "GEMINI_API_KEY", "GOOGLE_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "CODEX_API_KEY",
                "NPM_CONFIG_PREFIX"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(agent_cli, "candidates", lambda name, env: [])
    registry.invalidate()
    sessions.clear_cache()
    root = tmp_path / "root"
    root.mkdir()
    return {"home": home, "root": root, "bin": bin_dir, "tmp": tmp_path}


def make_hub(box, gateway=None) -> AgentHub:
    return AgentHub(box["root"], gateway=gateway, workspaces=list,
                    device=lambda: {"id": "pc-1", "name": "笔记本", "gateway": "https://gw.example"})


@pytest.fixture
def hub(box):
    instance = make_hub(box, FakeGateway())
    yield instance
    instance.close()


async def read_until(handlers, rid: str, pred, timeout: float = 15.0) -> list[dict]:
    events, cursor = [], 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred(events):
            return events
        result = await handlers["agent_run_read"]({"rid": rid, "cursor": cursor, "wait": 1})
        assert "error" not in result, result
        events += result["events"]
        cursor = result["cursor"]
    raise AssertionError(f"timed out: {json.dumps(events, ensure_ascii=False)[:3000]}")


def turns_ended(count: int):
    return lambda events: sum(1 for e in events if e["type"] == "status" and e["state"] in ("done", "failed", "stopped")) >= count


# ---------------------------------------------------------------------------------------------- the table

async def test_handlers_are_exactly_the_spec_ops_and_never_raise(hub):
    handlers = hub.handlers()
    assert tuple(handlers) == OPS
    assert set(OPS) == {
        "agent_info", "agent_install", "agent_install_job", "agent_sessions", "agent_session_read",
        "agent_session_meta", "agent_run_open", "agent_run_send", "agent_run_read", "agent_run_answer",
        "agent_run_stop", "agent_run_close", "agent_run_list", "agent_dirs", "agent_prompts", "agent_prompt_save",
        "agent_prompt_preview", "agent_tmp_list", "agent_tmp_clean",
        # §16.4: API providers
        "agent_api_providers", "agent_api_provider_save", "agent_api_provider_delete", "agent_api_key_set",
        "agent_api_test", "agent_api_models"}
    assert hub_mod.FEATURE == "agents/1"
    for op, handler in handlers.items():
        for params in (None, {}, {"rid": "nope", "kind": 5, "id": [], "path": 7, "job_id": None}):
            result = await handler(params)
            assert isinstance(result, dict), op
            if "error" in result:
                assert isinstance(result["error"], str) and result.get("code"), (op, result)
    assert (await handlers["agent_run_read"]({"rid": "nope"}))["code"] == "not_found"
    assert (await handlers["agent_session_read"]({}))["code"] == "bad_request"
    assert (await handlers["agent_install_job"]({"job_id": "ij_x"}))["code"] == "not_found"


def test_call_timeouts_follow_the_long_poll():
    assert hub_mod.call_timeout("agent_run_read", {"wait": 20}) == 30
    assert hub_mod.call_timeout("agent_run_read", {"wait": 999}) == 30
    assert hub_mod.call_timeout("agent_run_read", {"wait": "x"}) == 10
    assert hub_mod.call_timeout("agent_run_open", {}) >= 30


# ---------------------------------------------------------------------------------------------- CLIs

async def test_info_reports_installed_clis_and_install_problems(hub, box):
    handlers = hub.handlers()
    info = await handlers["agent_info"]({"fresh": True})
    agents = {a["kind"]: a for a in info["agents"]}
    assert set(agents) >= {"claude", "codex", "dsh", "gemini"}
    codex = agents["codex"]
    assert codex["installed"] and codex["version"] == "2.3.4" and codex["login"] == "ok"
    assert codex["path"] == str(box["bin"] / "codex")
    assert agents["claude"]["installed"] and agents["claude"]["login"] == "ok"
    assert not agents["gemini"]["installed"] and agents["gemini"]["install"] is None
    assert info["device"]["name"] == "笔记本" and info["home"] == str(box["home"])
    only = await handlers["agent_info"]({"kind": "codex"})
    assert [a["kind"] for a in only["agents"]] == ["codex"]
    assert (await handlers["agent_info"]({"kind": "nope"}))["code"] == "unknown_kind"
    # no npm on PATH: gemini has no official installer, so a clear error instead of a failing job
    failed = await handlers["agent_install"]({"kind": "gemini"})
    assert failed["code"] in ("npm_missing", "node_missing", "no_installer") and "Node.js" in failed["error"]
    assert (await handlers["agent_install"]({"kind": "nope"}))["code"] == "unknown_kind"


# ---------------------------------------------------------------------------------------------- sessions

async def test_sessions_grouped_by_cli_read_and_managed(hub, box):
    handlers = hub.handlers()
    listed = await handlers["agent_sessions"]({})
    kinds = {s["kind"] for s in listed["sessions"]}
    assert {"claude", "codex"} <= kinds
    assert [g["kind"] for g in listed["groups"]][:2] == ["claude", "codex"]
    assert all("files" not in s for s in listed["sessions"]), "internal file lists stay on the device"
    claude = next(s for s in listed["sessions"] if s["id"] == "7f3c1e2a-5b4d-4c6e-8f90-1a2b3c4d5e6f")
    # the fixtures were recorded in /home/u/...: not on this machine, so never offered as a default workdir
    assert claude["cwd"] == "/home/u/proj" and claude["cwd_missing"] is True

    history = await handlers["agent_session_read"]({"kind": "claude", "id": claude["id"]})
    assert history["events"] and history["events"][-1]["type"] == "status"
    assert (await handlers["agent_session_read"]({"kind": "claude", "id": "missing"}))["code"] == "not_found"

    pinned = await handlers["agent_session_meta"]({"kind": "claude", "id": claude["id"], "pinned": True, "alias": "登录修复"})
    assert pinned["ok"] and pinned["pinned"]
    again = await handlers["agent_sessions"]({"kind": "claude"})
    assert again["sessions"][0]["id"] == claude["id"] and again["sessions"][0]["alias"] == "登录修复"
    assert {s["kind"] for s in again["sessions"]} == {"claude"}


# ---------------------------------------------------------------------------------------------- runs

async def test_codex_run_open_send_with_prompt_tmpdir_address_and_close(hub, box):
    handlers = hub.handlers()
    log = box["tmp"] / "argv.jsonl"
    os.environ["FAKE_ARGV_LOG"] = str(log)
    try:
        work = str(box["home"] / "proj")
        opened = await handlers["agent_run_open"]({"kind": "codex", "workdir": work, "prompt": "hello",
                                                   "permission": "ask", "binary": "/bin/false",
                                                   "system_prompt": "ignored text"})
        assert "error" not in opened, opened
        rid = opened["rid"]
        assert opened["address"] == "codex-proj@笔记本" and opened["gateway"] and opened["mode"] == "direct"
        assert opened["tmpdir"] == str(box["root"] / ".anx" / "agents" / "tmp" / rid)
        assert Path(opened["tmpdir"]).is_dir()
        events = await read_until(handlers, rid, turns_ended(1))
        assert events[-1]["state"] == "done"
        session_id = next(e["session_id"] for e in events if e.get("session_id"))

        first = json.loads(log.read_text().splitlines()[0])
        argv = first["argv"]
        assert first["prompt"] == "hello"
        assert argv[argv.index("-C") + 1] == work
        assert 'approval_policy="never"' in argv and "--ask-for-approval" not in argv
        dev = next(a for a in argv if a.startswith("developer_instructions="))
        text = json.loads(dev.split("=", 1)[1])
        assert "笔记本" in text and opened["tmpdir"] in text and work in text and "codex-proj@笔记本" in text
        assert "$" not in text.replace("$$", "")

        op, args = hub.gateway.calls[0]
        assert op == "agent_handle" and args == {"device_id": "pc-1", "workdir": work, "kind": "codex"}

        sent = await handlers["agent_run_send"]({"rid": rid, "prompt": "again"})
        assert sent["ok"]
        await read_until(handlers, rid, turns_ended(2))
        resumed = json.loads(log.read_text().splitlines()[1])["argv"]
        assert resumed[resumed.index("resume") + 1] == session_id

        listed = (await handlers["agent_run_list"]({}))["runs"]
        assert listed[0]["rid"] == rid and listed[0]["mode"] == "direct" and listed[0]["address"] == "codex-proj@笔记本"
        sessions_now = (await handlers["agent_sessions"]({"kind": "codex"}))["sessions"]
        assert not any(s["id"] == session_id for s in sessions_now) or \
            next(s for s in sessions_now if s["id"] == session_id)["running"]
        assert hub.meta.get("codex", session_id)["address"] == "codex-proj@笔记本"
        assert hub.meta.get("codex", session_id)["managed"] is True

        recent = (await handlers["agent_dirs"]({}))["recent"]
        assert recent[0]["path"] == work
        tmp = await handlers["agent_tmp_list"]({})
        entry = next(i for i in tmp["items"] if i["rid"] == rid)
        assert entry["active"] and entry["session"] == session_id and entry["kind"] == "codex"
        assert (await handlers["agent_tmp_clean"]({"rid": rid}))["code"] == "active"

        assert (await handlers["agent_run_close"]({"rid": rid}))["ok"]
        assert (await handlers["agent_run_read"]({"rid": rid}))["code"] == "not_found"
        closed = next(i for i in (await handlers["agent_tmp_list"]({}))["items"] if i["rid"] == rid)
        assert not closed["active"] and closed["closed_at"]
        assert "rid" not in hub.meta.get("codex", session_id)
        cleaned = await handlers["agent_tmp_clean"]({"rid": rid})
        assert cleaned["removed"] == 1 and not Path(opened["tmpdir"]).exists()
    finally:
        os.environ.pop("FAKE_ARGV_LOG", None)


async def test_resume_keeps_the_address_and_stop_interrupts(hub, box):
    handlers = hub.handlers()
    work = str(box["home"] / "proj")
    hub.meta.set("codex", "019a6f2e-3c4d-7e8f-9a0b-1c2d3e4f5a6b", address="fixer@笔记本")
    opened = await handlers["agent_run_open"]({"kind": "codex", "workdir": work, "prompt": "slow",
                                               "resume_id": "019a6f2e-3c4d-7e8f-9a0b-1c2d3e4f5a6b"})
    assert opened["address"] == "fixer@笔记本"
    assert hub.gateway.calls[-1][1]["name"] == "fixer"
    rid = opened["rid"]
    await read_until(handlers, rid, lambda ev: any(e["type"] == "status" and e["state"] == "running" for e in ev))
    assert (await handlers["agent_run_stop"]({"rid": rid}))["ok"]
    events = await read_until(handlers, rid, turns_ended(1))
    assert events[-1]["state"] == "stopped"
    await handlers["agent_run_close"]({"rid": rid})


async def test_without_a_gateway_the_run_still_works_and_says_so(box):
    gateway = FakeGateway(away=True)
    hub = make_hub(box, gateway)
    try:
        handlers = hub.handlers()
        opened = await handlers["agent_run_open"]({"kind": "codex", "workdir": str(box["home"] / "proj"),
                                                   "prompt": "hi"})
        assert opened["address"] == "" and opened["gateway"] is False and "ask_user" in opened["note"]
        events = await read_until(handlers, opened["rid"], turns_ended(1))
        notes = [e for e in events if e["type"] == "status" and e.get("note")]
        assert notes and "无网关" in notes[0]["note"] and notes[0]["gateway"] is False
        assert events[-1]["state"] == "done"
        # gateway mode needs the gateway: refused clearly, nothing left behind
        refused = await handlers["agent_run_open"]({"kind": "codex", "workdir": str(box["home"] / "proj"),
                                                    "prompt": "hi", "context": {"mode": "gateway", "device": "pc-2",
                                                                                "workdir": "/srv/app"}})
        assert refused["code"] == "no_gateway"
        assert len(list((box["root"] / ".anx" / "agents" / "tmp").iterdir())) == 1
    finally:
        hub.close()


async def test_gateway_mode_binds_the_context_device_and_renders_its_fragment(hub, box):
    handlers = hub.handlers()
    log = box["tmp"] / "argv-gw.jsonl"
    os.environ["FAKE_ARGV_LOG"] = str(log)
    try:
        opened = await handlers["agent_run_open"]({"kind": "codex", "workdir": str(box["home"] / "proj"), "prompt": "hi",
                                                   "context": {"mode": "gateway", "device": "pc-2", "workdir": "/srv/app"}})
        assert opened["mode"] == "gateway" and opened["handle"]
        assert hub.gateway.calls[-1][1] == {"device_id": "pc-2", "workdir": "/srv/app", "kind": "codex"}
        await read_until(handlers, opened["rid"], turns_ended(1))
        argv = json.loads(log.read_text().splitlines()[0])["argv"]
        text = json.loads(next(a for a in argv if a.startswith("developer_instructions=")).split("=", 1)[1])
        assert f'workspace_id="{opened["handle"]}"' in text and "/srv/app" in text
        await handlers["agent_run_close"]({"rid": opened["rid"]})
    finally:
        os.environ.pop("FAKE_ARGV_LOG", None)


async def test_gateway_mode_connector_warning_and_codex_auto_approve(hub, box):
    handlers = hub.handlers()
    log = box["tmp"] / "argv-gw2.jsonl"
    os.environ["FAKE_ARGV_LOG"] = str(log)
    context = {"mode": "gateway", "device": "pc-2", "workdir": "/srv/app"}
    try:
        # no Agent Nexus connector in codex's config: a clear warning, no approval overrides
        opened = await handlers["agent_run_open"]({"kind": "codex", "workdir": str(box["home"] / "proj"), "prompt": "hi",
                                                   "context": context})
        events = await read_until(handlers, opened["rid"], turns_ended(1))
        assert any(e["type"] == "error" and "MCP 连接器" in e.get("text", "") for e in events)
        await handlers["agent_run_close"]({"rid": opened["rid"]})
        # with one: codex exec gets it auto-approved (it can never ask)
        (box["home"] / ".codex").mkdir(exist_ok=True)
        (box["home"] / ".codex" / "config.toml").write_text('[mcp_servers.agent-nexus]\nurl = "https://gw/mcp"\n')
        opened = await handlers["agent_run_open"]({"kind": "codex", "workdir": str(box["home"] / "proj"), "prompt": "hi",
                                                   "context": context})
        await read_until(handlers, opened["rid"], turns_ended(1))
        argv = json.loads(log.read_text().splitlines()[-1])["argv"]
        assert 'mcp_servers.agent-nexus.default_tools_approval_mode="approve"' in argv
        await handlers["agent_run_close"]({"rid": opened["rid"]})
        # read-only runs never get it
        opened = await handlers["agent_run_open"]({"kind": "codex", "workdir": str(box["home"] / "proj"), "prompt": "hi",
                                                   "permission": "readonly", "context": context})
        await read_until(handlers, opened["rid"], turns_ended(1))
        argv = json.loads(log.read_text().splitlines()[-1])["argv"]
        assert not any("default_tools_approval_mode" in a for a in argv)
        await handlers["agent_run_close"]({"rid": opened["rid"]})
    finally:
        os.environ.pop("FAKE_ARGV_LOG", None)

async def test_open_refuses_bad_input_without_leaving_temp_folders(hub, box):
    handlers = hub.handlers()
    assert (await handlers["agent_run_open"]({"kind": "nope"}))["code"] == "unknown_kind"
    assert (await handlers["agent_run_open"]({"kind": "codex", "workdir": str(box["tmp"] / "missing")}))["code"] == "bad_workdir"
    outside = await handlers["agent_run_open"]({"kind": "codex", "workdir": str(box["tmp"]), "prompt": "x"})
    assert outside["code"] == "workdir_not_allowed"
    gemini = await handlers["agent_run_open"]({"kind": "gemini", "workdir": str(box["home"]), "prompt": "x"})
    assert gemini["code"] == "not_installed"
    tmp = box["root"] / ".anx" / "agents" / "tmp"
    assert not tmp.exists() or not list(tmp.iterdir())


# ---------------------------------------------------------------------------------------------- prompts, dirs

async def test_prompts_and_dirs(hub, box):
    handlers = hub.handlers()
    doc = await handlers["agent_prompts"]({})
    assert {f["id"] for f in doc["fragments"]} >= {"identity", "workdir", "gateway"}
    preview = await handlers["agent_prompt_preview"]({"preset": "default", "context": {"kind": "codex", "mode": "direct"}})
    assert "笔记本" in preview["text"] and preview["segments"]
    saved = await handlers["agent_prompt_save"]({"fragment": {"id": "mine", "title": "我的", "text": "设备 $device_id"}})
    assert "error" not in saved
    preview = await handlers["agent_prompt_preview"]({"context": {"kind": "codex"}})
    assert "设备 pc-1" in preview["text"]

    root_view = await handlers["agent_dirs"]({})
    assert root_view["home"] == str(box["home"]) and root_view["path"] is None
    assert any(d["path"] == str(box["home"]) for d in root_view["dirs"])
    inside = await handlers["agent_dirs"]({"path": str(box["home"])})
    assert any(d["name"] == "proj" for d in inside["dirs"])
    assert (await handlers["agent_dirs"]({"path": str(box["tmp"])}))["code"] == "outside"


async def test_session_delete_is_refused_while_a_run_uses_it(hub, box):
    handlers = hub.handlers()
    session_id = "019a6f2e-3c4d-7e8f-9a0b-1c2d3e4f5a6b"
    opened = await handlers["agent_run_open"]({"kind": "codex", "workdir": str(box["home"] / "proj"), "prompt": "hi",
                                               "resume_id": session_id})
    await read_until(handlers, opened["rid"], turns_ended(1))
    refused = await handlers["agent_session_meta"]({"kind": "codex", "id": session_id, "delete_file": True})
    assert refused["code"] == "busy"
    await handlers["agent_run_close"]({"rid": opened["rid"]})
    deleted = await handlers["agent_session_meta"]({"kind": "codex", "id": session_id, "delete_file": True})
    assert deleted["ok"] and deleted["deleted"] >= 1


async def test_sweeper_starts_and_stops(hub):
    hub.start()
    await asyncio.sleep(0.2)
    assert hub._last_sweep > 0
    hub.close()
    assert hub._sweeper is None


# ---------------------------------------------------------------------------------------------- review fixes

async def test_refused_opens_never_ask_the_gateway_and_closed_runs_release_their_handle(hub, box):
    handlers = hub.handlers()
    await handlers["agent_run_open"]({"kind": "codex", "workdir": str(box["tmp"]), "prompt": "x"})
    await handlers["agent_run_open"]({"kind": "gemini", "workdir": str(box["home"]), "prompt": "x"})
    await handlers["agent_run_open"]({"kind": "codex", "workdir": str(box["home"]), "resume_id": "--yolo"})
    assert hub.gateway.calls == [], "no handle / address is handed out for a run that cannot start"
    opened = await handlers["agent_run_open"]({"kind": "codex", "workdir": str(box["home"] / "proj"), "prompt": "hi"})
    await read_until(handlers, opened["rid"], turns_ended(1))
    await handlers["agent_run_close"]({"rid": opened["rid"]})
    for _ in range(50):
        if hub.gateway.calls[-1][0] == "agent_release":
            break
        await asyncio.sleep(0.02)
    assert hub.gateway.calls[-1] == ("agent_release", {"workspace_id": opened["handle"]})


async def test_opening_a_session_that_already_runs_attaches_to_that_run(hub, box):
    handlers = hub.handlers()
    work = str(box["home"] / "proj")
    first = await handlers["agent_run_open"]({"kind": "codex", "workdir": work, "prompt": "hi"})
    events = await read_until(handlers, first["rid"], turns_ended(1))
    session_id = next(e["session_id"] for e in events if e.get("session_id"))
    again = await handlers["agent_run_open"]({"kind": "codex", "workdir": work, "resume_id": session_id,
                                              "prompt": "from another tab"})
    assert again["rid"] == first["rid"] and again["attached"] and again.get("sent")
    assert len(hub.runs.runs) == 1
    events = await read_until(handlers, first["rid"], turns_ended(2))
    assert any(e["type"] == "user" and e["text"] == "from another tab" for e in events)
    await handlers["agent_run_close"]({"rid": first["rid"]})


async def test_remote_callers_cannot_use_full_unless_allowed(hub, box):
    remote = hub_mod.remote_handlers(hub.handlers(), allow_full=False)
    refused = await remote["agent_run_open"]({"kind": "codex", "workdir": str(box["home"] / "proj"),
                                              "permission": "full", "prompt": "x"})
    assert refused["code"] == "permission_disabled"
    info = await remote["agent_info"]({})
    assert info["policy"]["full"] is False
    assert hub_mod.remote_handlers(hub.handlers(), allow_full=True).keys() == set(OPS)
