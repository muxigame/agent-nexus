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
        "agent_prompt_preview", "agent_tmp_list", "agent_tmp_clean", "agent_models",
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
        # the context travels with the run and its session (the chat restores it on reopen)
        listed = next(r for r in (await handlers["agent_run_list"]({}))["runs"] if r["rid"] == opened["rid"])
        assert listed["context"] == {"mode": "gateway", "device": "pc-2", "workdir": "/srv/app"}
        session_id = listed["session_id"]
        assert hub.meta.get("codex", session_id)["context"] == {"device": "pc-2", "workdir": "/srv/app"}
        # the folder was the other device's: the runner's base folder is not a "recent" one
        assert all(item["path"] != str(box["home"] / "proj") for item in (await handlers["agent_dirs"]({}))["recent"])
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

def test_session_meta_keeps_a_gateway_context(box):
    from agent_nexus.agents.sessions import SessionMeta
    meta = SessionMeta(box["root"])
    meta.set("codex", "s-1", context={"device": "pc-2", "workdir": "/srv/app"}, managed=True)
    sessions = meta.apply([{"kind": "codex", "id": "s-1", "cwd": "/home/u"}, {"kind": "codex", "id": "s-2"}])
    assert sessions[0]["context"] == {"mode": "gateway", "device": "pc-2", "workdir": "/srv/app"}
    assert "context" not in sessions[1]
    meta.set("codex", "s-1", context=None)  # resumed directly: cleared
    assert "context" not in meta.get("codex", "s-1")
    meta.set("codex", "s-1", context={"device": "", "workdir": "/x"})  # incomplete: not stored
    assert "context" not in meta.get("codex", "s-1")


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


# ---------------------------------------------------------------------------------------------- 本机 MCP

class FakeLocal:
    url = "http://127.0.0.1:8399/mcp"

    def __init__(self, state="ready", auto=True):
        self._state = state
        self.settings = {"enabled": True, "auto_config": auto, "peers": "any"}

    def state(self):
        return self._state

    def status(self):
        return {"state": self._state, "reason": "网关未连接，稍后再试" if self._state != "ready" else "已就绪"}


def linked_hub(box, local=None):
    from agent_nexus.agents import mcp_link
    local = local or FakeLocal()
    link = mcp_link.MCPLink(box["root"], local_mcp=local, accounts=lambda: [None],
                            definitions=registry.definitions, gateway_host=lambda: "gw.example")
    hub = AgentHub(box["root"], gateway=FakeGateway(), workspaces=list, mcp_link=link,
                   device=lambda: {"id": "pc-1", "name": "笔记本", "gateway": "https://gw.example"})
    return hub, link


async def open_and_argv(handlers, log, params):
    opened = await handlers["agent_run_open"](params)
    assert "error" not in opened, opened
    events = await read_until(handlers, opened["rid"], turns_ended(1))
    argv = json.loads(log.read_text().splitlines()[-1])["argv"]
    await handlers["agent_run_close"]({"rid": opened["rid"]})
    return opened, events, argv


def codex_prompt(argv):
    return json.loads(next(a for a in argv if a.startswith("developer_instructions=")).split("=", 1)[1])


async def test_runs_use_the_dedicated_entry_with_the_ready_workspace_and_no_warning(box):
    hub, link = linked_hub(box)
    link.probe.put("user", "codex", {"state": "absent", "connectors": [], "source": "probe"})
    link.account_name = lambda account: "user"
    await link.reconcile("test")
    assert "agent-nexus" in (box["home"] / ".codex" / "config.toml").read_text()
    handlers = hub.handlers()
    log = box["tmp"] / "argv-nx.jsonl"
    os.environ["FAKE_ARGV_LOG"] = str(log)
    try:
        for mode, context in (("gateway", {"mode": "gateway", "device": "pc-2", "workdir": "/srv/app"}), ("direct", {})):
            opened, events, argv = await open_and_argv(handlers, log, {
                "kind": "codex", "workdir": str(box["home"] / "proj"), "prompt": "hi", "context": context})
            assert not any(e["type"] == "error" and "MCP" in e.get("text", "") for e in events)
            assert 'mcp_servers.agent-nexus.default_tools_approval_mode="approve"' in argv
            # permission "ask": only the read tools + mail_send / ask_user exist in the run; never exec_command
            enabled = next(a for a in argv if a.startswith("mcp_servers.agent-nexus.enabled_tools="))
            tools = json.loads(enabled.split("=", 1)[1])
            assert "file_read" in tools and "mail_send" in tools and "ask_user" in tools
            assert not {"exec_command", "write_stdin", "file_write", "keyboard", "tooluse", "use_device"} & set(tools)
            assert not any(a.startswith("mcp_servers.agent-nexus.url") for a in argv)  # the entry is in config
            text = codex_prompt(argv)
            folder = "/srv/app" if mode == "gateway" else str(box["home"] / "proj")
            assert f'workspace_id="{opened["handle"]}"' in text and "连接器 agent-nexus" in text
            assert folder in text and "不需要先调用 device_list / use_device" in text and "@笔记本" in text
        info = await handlers["agent_info"]({})
        codex = next(a for a in info["agents"] if a["kind"] == "codex")
        assert codex["nexus_mcp"]["state"] == "local" and codex["gateway_mcp"] is True
    finally:
        os.environ.pop("FAKE_ARGV_LOG", None)
        hub.close()


async def test_without_an_entry_the_run_attaches_the_local_mcp_by_flags(box):
    hub, link = linked_hub(box, FakeLocal(auto=False))
    link.probe.put(link.account_name(None), "codex", {"state": "absent", "connectors": [], "source": "probe"})
    handlers = hub.handlers()
    log = box["tmp"] / "argv-flags.jsonl"
    os.environ["FAKE_ARGV_LOG"] = str(log)
    try:
        _opened, _events, argv = await open_and_argv(handlers, log, {
            "kind": "codex", "workdir": str(box["home"] / "proj"), "prompt": "hi",
            "context": {"mode": "gateway", "device": "pc-2", "workdir": "/srv/app"}})
        assert 'mcp_servers.agent-nexus.url="http://127.0.0.1:8399/mcp"' in argv
        assert argv.index('mcp_servers.agent-nexus.url="http://127.0.0.1:8399/mcp"') < argv.index("--json")
        assert not (box["home"] / ".codex" / "config.toml").exists()  # the user's files are untouched
    finally:
        os.environ.pop("FAKE_ARGV_LOG", None)
        hub.close()


async def test_an_unknown_account_answer_gives_the_generic_context(box):
    hub, link = linked_hub(box)
    handlers = hub.handlers()
    log = box["tmp"] / "argv-unknown.jsonl"
    os.environ["FAKE_ARGV_LOG"] = str(log)
    try:
        opened, events, argv = await open_and_argv(handlers, log, {
            "kind": "codex", "workdir": str(box["home"] / "proj"), "prompt": "hi",
            "context": {"mode": "gateway", "device": "pc-2", "workdir": "/srv/app"}})
        text = codex_prompt(argv)
        assert "只用其中一个连接器" in text and f'workspace_id="{opened["handle"]}"' in text
        assert not any(e["type"] == "error" and "MCP" in e.get("text", "") for e in events)
        assert not any("default_tools_approval_mode" in a or "mcp_servers.agent-nexus.url" in a for a in argv)
    finally:
        os.environ.pop("FAKE_ARGV_LOG", None)
        hub.close()


async def test_a_local_mcp_that_cannot_work_warns_in_gateway_mode(box):
    hub, link = linked_hub(box, FakeLocal(state="gateway_old"))
    link.probe.put(link.account_name(None), "codex", {"state": "absent", "connectors": [], "source": "probe"})
    handlers = hub.handlers()
    log = box["tmp"] / "argv-old.jsonl"
    os.environ["FAKE_ARGV_LOG"] = str(log)
    try:
        _opened, events, _argv = await open_and_argv(handlers, log, {
            "kind": "codex", "workdir": str(box["home"] / "proj"), "prompt": "hi",
            "context": {"mode": "gateway", "device": "pc-2", "workdir": "/srv/app"}})
        assert any(e["type"] == "error" and "无法接入 Agent Nexus MCP" in e.get("text", "") for e in events)
    finally:
        os.environ.pop("FAKE_ARGV_LOG", None)
        hub.close()


@pytest.mark.parametrize("permission,expect", [
    ("edits", "mcp__agent-nexus__mail_send"),
    ("readonly", "mcp__agent-nexus__device_list"),
    ("ask", "mcp__agent-nexus__ask_user"),
    ("full", None),
])
async def test_claude_gets_the_connector_allowed_and_the_flags_file_in_every_mode(box, permission, expect):
    hub, link = linked_hub(box, FakeLocal(auto=False))
    link.probe.put(link.account_name(None), "claude", {"state": "absent", "connectors": [], "source": "probe"})
    handlers = hub.handlers()
    log = box["tmp"] / f"argv-claude-{permission}.jsonl"
    os.environ["FAKE_ARGV_LOG"] = str(log)
    try:
        opened = await handlers["agent_run_open"]({"kind": "claude", "workdir": str(box["home"] / "proj"),
                                                   "prompt": "hi", "permission": permission})
        assert "error" not in opened, opened
        await read_until(handlers, opened["rid"], turns_ended(1))
        argv = json.loads(log.read_text().splitlines()[-1])
        config = json.loads(open(argv[argv.index("--mcp-config") + 1], encoding="utf-8").read())["mcpServers"]
        assert config["agent-nexus"] == {"type": "http", "url": "http://127.0.0.1:8399/mcp"}
        assert ("anx" in config) == (permission == "ask")
        if expect is None:
            assert "--allowedTools" not in argv
        else:
            allowed = argv[argv.index("--allowedTools") + 1].split(",")
            assert expect in allowed
            # never the whole server, never a tool that runs outside the CLI's own sandbox
            assert "mcp__agent-nexus" not in allowed
            assert not {"mcp__agent-nexus__exec_command", "mcp__agent-nexus__write_stdin",
                        "mcp__agent-nexus__tooluse", "mcp__agent-nexus__use_device"} & set(allowed)
            if permission == "edits":  # direct mode: the CLI's own tools edit this computer's files
                assert "mcp__agent-nexus__file_write" not in allowed
            if permission == "readonly":
                assert "mcp__agent-nexus__mail_send" not in allowed
        await handlers["agent_run_close"]({"rid": opened["rid"]})
    finally:
        os.environ.pop("FAKE_ARGV_LOG", None)
        hub.close()


# ---------------------------------------------------------------------------------------------- codex account apps

async def test_codex_with_an_account_connector_gets_the_local_mcp_and_is_told_not_to_use_the_app(box):
    """The owner's report: Codex reached the gateway only through a ChatGPT account connector "workspace" (an app
    under codex_apps), whose calls codex exec refuses ("requires approval, but approval policy is never")."""
    hub, link = linked_hub(box)
    link.account_name = lambda account: "user"
    link.probe.put("user", "codex", {"state": "present", "source": "probe",
                                     "connectors": [{"name": "workspace", "prefix": "", "stale": True}]})
    handlers = hub.handlers()
    log = box["tmp"] / "argv-account.jsonl"
    os.environ["FAKE_ARGV_LOG"] = str(log)
    try:
        for permission in ("ask", "full"):
            opened, events, argv = await open_and_argv(handlers, log, {
                "kind": "codex", "workdir": str(box["home"] / "proj"), "prompt": "hi", "permission": permission})
            assert 'mcp_servers.agent-nexus.url="http://127.0.0.1:8399/mcp"' in argv
            assert 'mcp_servers.agent-nexus.default_tools_approval_mode="approve"' in argv
            assert argv.index('mcp_servers.agent-nexus.url="http://127.0.0.1:8399/mcp"') < argv.index("--json")
            enabled = [a for a in argv if a.startswith("mcp_servers.agent-nexus.enabled_tools=")]
            if permission == "ask":
                assert enabled and "exec_command" not in json.loads(enabled[0].split("=", 1)[1])
            else:
                assert not enabled  # full: every tool approved
            text = codex_prompt(argv)
            assert "临时接入了本机 Agent Nexus MCP（连接器 agent-nexus" in text
            assert "不要调用 workspace.*" in text
            assert not any(e["type"] == "error" and "无法审批" in e.get("text", "") for e in events)
        assert not (box["home"] / ".codex" / "config.toml").exists()  # run-only: nothing written
        # the 本机 MCP is down: the run is warned that it cannot use the Agent Nexus tools
        link.local._state = "revoked"
        opened, events, argv = await open_and_argv(handlers, log, {
            "kind": "codex", "workdir": str(box["home"] / "proj"), "prompt": "hi"})
        assert any(e["type"] == "error" and "codex exec 无法审批" in e.get("text", "") for e in events)
        assert not any(a.startswith("mcp_servers.agent-nexus.url") for a in argv)
    finally:
        os.environ.pop("FAKE_ARGV_LOG", None)
        hub.close()


# ---------------------------------------------------------------------------------------------- work folders

def codex_session_in(home: Path, folder: Path, session_id: str) -> None:
    day = home / ".codex" / "sessions" / "2026" / "11" / "27"
    day.mkdir(parents=True, exist_ok=True)
    lines = [{"timestamp": "2026-11-27T10:00:00.000Z", "type": "session_meta",
              "payload": {"id": session_id, "timestamp": "2026-11-27T10:00:00.000Z", "cwd": str(folder),
                          "originator": "codex_cli_rs", "cli_version": "0.164.0"}},
             {"timestamp": "2026-11-27T10:00:01.000Z", "type": "response_item",
              "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "在外面的项目"}]}}]
    (day / f"rollout-2026-11-27T10-00-00-{session_id}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


async def test_a_folder_the_cli_already_ran_in_may_be_used_but_not_below_it(hub, box):
    """The owner's D:\\CPN\\BMC5-workspace: a session Codex recorded outside the roots can be resumed (and that
    exact folder used again); a folder below it, or anywhere else, is still refused."""
    outside = box["tmp"] / "srv-app"
    (outside / "sub").mkdir(parents=True)
    session_id = "019b0000-0000-7000-8000-000000000001"
    codex_session_in(box["home"], outside, session_id)
    sessions.clear_cache()
    handlers = hub.handlers()
    assert await hub._workdir_ok(str(outside), "codex", session_id) is None
    assert await hub._workdir_ok(str(outside), "codex") is None  # a discovered session's folder
    refused = await hub._workdir_ok(str(outside / "sub"), "codex", session_id)
    assert refused["code"] == "workdir_not_allowed" and "先登记为工作区" in refused["error"]
    assert (await hub._workdir_ok(str(outside), "claude"))["code"] == "workdir_not_allowed"  # another CLI's: no
    opened = await handlers["agent_run_open"]({"kind": "codex", "workdir": str(outside), "resume_id": session_id})
    assert "error" not in opened, opened
    await handlers["agent_run_close"]({"rid": opened["rid"]})
    # a recent folder (it only gets there after a run opened in it) counts too
    other = box["tmp"] / "recent-one"
    other.mkdir()
    assert (await hub._workdir_ok(str(other), "claude"))["code"] == "workdir_not_allowed"
    hub.recent.add(str(other), kind="claude")
    assert await hub._workdir_ok(str(other), "claude") is None
    # Agent Nexus' own state is never a work folder, even inside an allowed root
    (box["root"] / ".anx" / "keys").mkdir(parents=True, exist_ok=True)
    hub.recent.add(str(box["root"] / ".anx"), kind="codex")
    assert (await hub._workdir_ok(str(box["root"] / ".anx" / "keys"), "codex"))["code"] == "workdir_not_allowed"
    assert (await hub._workdir_ok(str(box["root"] / ".anx"), "codex"))["code"] == "workdir_not_allowed"


async def test_on_windows_every_drive_root_is_a_root(hub, box, monkeypatch):
    from agent_nexus.agents import workdirs
    drive = box["tmp"] / "D"
    (drive / "CPN" / "BMC5-workspace").mkdir(parents=True)
    monkeypatch.setattr(workdirs, "WINDOWS", True)
    monkeypatch.setattr(workdirs, "drive_roots", lambda: [str(drive)])
    roots = workdirs.default_roots(str(box["home"]), [])
    assert [r["kind"] for r in roots] == ["home", "drive"]
    assert workdirs.default_roots(str(box["home"]), [], drives=False)[-1]["kind"] == "home"
    assert str(drive) in hub._roots()
    assert await hub._workdir_ok(str(drive / "CPN" / "BMC5-workspace"), "codex") is None
    listed = await hub.handlers()["agent_dirs"]({})
    assert any(r["kind"] == "drive" for r in listed["roots"])  # the picker shows the drives without asking
    assert not any(r["kind"] == "drive" for r in (await hub.handlers()["agent_dirs"]({"drives": False}))["roots"])
    opened = await hub.handlers()["agent_run_open"]({"kind": "codex", "workdir": str(drive / "CPN" / "BMC5-workspace")})
    assert "error" not in opened, opened
    await hub.handlers()["agent_run_close"]({"rid": opened["rid"]})


async def test_roots_state_ancestors_and_forged_transcripts_never_make_a_work_folder(hub, box, monkeypatch):
    from agent_nexus.agents import workdirs
    drive = box["tmp"] / "D"
    (drive / "proj").mkdir(parents=True)
    # Agent Nexus' root and the folder that contains it: a non-full run there could edit agent-nexus.json
    hub.recent.add(str(box["root"]), kind="codex")
    hub.recent.add(str(box["tmp"]), kind="codex")
    assert (await hub._workdir_ok(str(box["root"]), "codex"))["code"] == "workdir_not_allowed"
    refused = await hub._workdir_ok(str(box["tmp"]), "codex")
    assert refused["code"] == "workdir_not_allowed" and "Agent Nexus" in refused["error"]
    # a file system root or a system folder, even when a (forged) transcript or the recent list names it
    hub.recent.add("/", kind="codex")
    hub.recent.add("/etc", kind="codex")
    assert (await hub._workdir_ok("/", "codex"))["code"] == "workdir_not_allowed"
    assert (await hub._workdir_ok("/etc", "codex"))["code"] == "workdir_not_allowed"
    # HOME itself stays the default work folder
    assert await hub._workdir_ok(str(box["home"]), "codex") is None
    # Windows: a drive root is a browse root, never a work folder; its sub-folders are
    monkeypatch.setattr(workdirs, "WINDOWS", True)
    monkeypatch.setattr(workdirs, "drive_roots", lambda: [str(drive)])
    assert (await hub._workdir_ok(str(drive), "codex"))["code"] == "workdir_not_allowed"
    assert await hub._workdir_ok(str(drive / "proj"), "codex") is None
    assert any(r["kind"] == "drive" for r in (await hub.handlers()["agent_dirs"]({}))["roots"])


def test_drive_roots_skip_network_and_cache(monkeypatch):
    from agent_nexus.agents import workdirs
    calls = []
    monkeypatch.setattr(workdirs, "WINDOWS", True)
    monkeypatch.setattr(workdirs, "_drives_cache", None)
    monkeypatch.setattr(workdirs, "_list_drives", lambda: calls.append(1) or ["C:\\"])
    assert workdirs.drive_roots() == ["C:\\"] and workdirs.drive_roots() == ["C:\\"]
    assert calls == [1]  # cached: the picker and every open do not enumerate the drives again
    assert workdirs._DRIVE_SKIP == {1, 4}  # DRIVE_NO_ROOT_DIR, DRIVE_REMOTE (a stale SMB mapping blocks isdir)


# ---------------------------------------------------------------------------------------------- models (§16.7)

async def test_the_model_reaches_the_cli_and_a_bad_one_is_refused_before_the_gateway(box):
    gateway = FakeGateway()
    hub = make_hub(box, gateway)
    handlers = hub.handlers()
    try:
        for bad in ("-m", "a b", 'x"y', "%PATH%", 5, "x" * 101):
            result = await handlers["agent_run_open"]({"kind": "codex", "workdir": str(box["home"] / "proj"),
                                                       "prompt": "hi", "model": bad})
            assert result["code"] == "bad_model", bad
        assert gateway.calls == []  # refused before a handle was handed out
        log = box["tmp"] / "argv-model.jsonl"
        os.environ["FAKE_ARGV_LOG"] = str(log)
        opened, _events, argv = await open_and_argv(handlers, log, {
            "kind": "codex", "workdir": str(box["home"] / "proj"), "prompt": "hi", "model": "gpt-6.1-sol"})
        assert opened["model"] == "gpt-6.1-sol"
        assert argv[argv.index("-m") + 1] == "gpt-6.1-sol"
        opened, _events, argv = await open_and_argv(handlers, log, {
            "kind": "codex", "workdir": str(box["home"] / "proj"), "prompt": "hi"})
        assert "-m" not in argv and opened["model"] is None  # no model: the CLI's own default
        # a resumed session records the model of every open, the default (None) included
        sid = "019a6f2e-3c4d-7e8f-9a0b-1c2d3e4f5a6b"
        await open_and_argv(handlers, log, {"kind": "codex", "workdir": str(box["home"] / "proj"), "prompt": "hi",
                                            "resume_id": sid, "model": "gpt-6.1-sol"})
        assert hub.meta.get("codex", sid).get("model") == "gpt-6.1-sol"
        await open_and_argv(handlers, log, {"kind": "codex", "workdir": str(box["home"] / "proj"), "prompt": "hi",
                                            "resume_id": sid})
        assert "model" not in hub.meta.get("codex", sid)
        models = await handlers["agent_models"]({"kind": "claude"})
        assert models["flag"] and any(m["id"] == "opus" for m in models["models"]) and models["source"] == "hints"
        assert (await handlers["agent_models"]({"kind": "nope"}))["code"] == "unknown_kind"
        info = await handlers["agent_info"]({})
        claude = next(a for a in info["agents"] if a["kind"] == "claude")
        assert claude["models"]["flag"] is True and claude["models"]["hints"]
    finally:
        os.environ.pop("FAKE_ARGV_LOG", None)
        hub.close()


async def test_codex_model_list_is_asked_once_at_a_time_and_a_failure_is_not_retried_at_once(hub, monkeypatch):
    from agent_nexus.agents import hub as hub_module
    calls = []

    def failing(binary, env, popen):
        calls.append(binary)
        time.sleep(0.05)
        return {"error": "app-server 没有回答"}

    monkeypatch.setattr(hub_module, "codex_models", failing)
    monkeypatch.setattr(hub_module, "codex_models_cache", lambda home, env: [])
    handlers = hub.handlers()
    results = await asyncio.gather(*(handlers["agent_models"]({"kind": "codex"}) for _ in range(4)))
    assert len(calls) == 1 and all(r["source"] == "hints" for r in results)
    assert results[0]["note"] == "app-server 没有回答"
    await handlers["agent_models"]({"kind": "codex", "fresh": True})
    assert len(calls) == 1  # fresh: not within MODELS_RETRY_S of the last ask
    good = {"models": [{"id": "gpt-6", "label": "GPT-6"}]}
    monkeypatch.setattr(hub_module, "codex_models", lambda binary, env, popen: calls.append(binary) or good)
    key = next(iter(hub._models_cache))
    stamp, payload, ok = hub._models_cache[key]
    hub._models_cache[key] = (stamp - hub_module.MODELS_RETRY_S - 1, payload, ok)
    listed = await handlers["agent_models"]({"kind": "codex"})
    assert listed["source"] == "cli" and listed["models"][0]["id"] == "gpt-6" and len(calls) == 2
    assert (await handlers["agent_models"]({"kind": "codex", "fresh": True}))["models"][0]["id"] == "gpt-6"
    assert len(calls) == 2


async def test_runs_get_a_private_anx_and_the_devices_cli_hint_only_where_it_works(box):
    import subprocess
    hub, link = linked_hub(box)
    link.probe.put("user", "codex", {"state": "absent", "connectors": [], "source": "probe"})
    link.account_name = lambda account: "user"
    await link.reconcile("test")
    handlers = hub.handlers()
    log = box["tmp"] / "argv-cli.jsonl"
    os.environ["FAKE_ARGV_LOG"] = str(log)
    try:
        opened, _, argv = await open_and_argv(handlers, log, {
            "kind": "codex", "workdir": str(box["home"] / "proj"), "prompt": "hi", "permission": "full"})
        record = json.loads(log.read_text().splitlines()[-1])
        assert record["console"] == "http://127.0.0.1:8399"
        first = record["path"].split(os.pathsep)[0]
        assert first == os.path.join(opened["tmpdir"], ".bin") and str(box["bin"]) in record["path"].split(os.pathsep)
        shim = Path(first) / "anx"
        assert os.access(shim, os.X_OK)
        done = subprocess.run([str(shim), "exec", "--help"], capture_output=True, text=True, timeout=60,
                              env={**os.environ, "PATH": record["path"]})
        assert done.returncode == 0 and "用法：anx exec" in done.stderr
        text = codex_prompt(argv)
        assert "anx devices" in text and "--stdin" in text and "anx exec <设备>" in text
        # Codex outside full mode: its sandbox blocks loopback, so no hint (the PATH is still there)
        _, _, argv = await open_and_argv(handlers, log, {
            "kind": "codex", "workdir": str(box["home"] / "proj"), "prompt": "hi", "permission": "ask"})
        assert "anx devices" not in codex_prompt(argv)
        assert json.loads(log.read_text().splitlines()[-1])["path"].split(os.pathsep)[0].endswith(".bin")
    finally:
        os.environ.pop("FAKE_ARGV_LOG", None)
        hub.close()


@pytest.mark.parametrize("kwargs,shown", [
    ({}, True),
    ({"kind": "codex", "adapter": "codex-exec", "permission": "full"}, True),
    ({"kind": "codex", "adapter": "codex-exec", "permission": "ask"}, False),
    ({"adapter": "api"}, False),
    ({"runnable": False}, False),
    ({"permission": "readonly"}, False),
    ({"local_state": "not_joined"}, False),
    ({"local_state": "off"}, False),
    ({"local_state": "revoked"}, False),
    ({"local_state": "rejected"}, False),
    ({"local_state": "device_access_off"}, False),
    ({"local_state": "gateway_old"}, False),
    ({"local_state": "locked"}, True),
    ({"local_state": "offline"}, True),
])
def test_devices_cli_hint_conditions(kwargs, shown):
    args = {"kind": "claude", "adapter": "claude-stream", "permission": "ask", "runnable": True,
            "local_state": "ready", **kwargs}
    text = hub_mod.devices_cli_hint(**args)
    assert bool(text) is shown
    if text:
        assert "%" not in text and "!" not in text and "\n" not in text  # a batch-launched CLI refuses those
        assert len(text) < 400


def test_devices_cli_hint_example_for_powershell():
    # every CLI but Claude Code (Git Bash) runs commands in PowerShell on Windows: a multi-line here-string, with
    # $OutputEncoding first (Windows PowerShell 5.1 pipes ASCII to a native program: Chinese would become '?')
    for kind, adapter in (("codex", "codex-exec"), ("gemini", "acp"), ("dsh", "acp")):
        text = hub_mod.devices_cli_hint(kind=kind, adapter=adapter, permission="full", runnable=True,
                                        local_state="ready", windows=True)
        assert "$OutputEncoding=[Text.UTF8Encoding]::new(); @'<换行>命令<换行>'@ | anx exec nas --stdin" in text
        assert "<<'EOF'" not in text
        assert "%" not in text and "!" not in text and "\n" not in text and len(text) < 460
    text = hub_mod.devices_cli_hint(kind="claude", adapter="claude-stream", permission="ask", runnable=True,
                                    local_state="ready", windows=True)
    assert "anx exec nas --stdin <<'EOF'" in text and "OutputEncoding" not in text
