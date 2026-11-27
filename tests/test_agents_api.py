"""API providers as agents, client-direct (agent_nexus/agents/api, docs/agent-chat.md §16.4).

Every request goes to tests/fixtures/fake_api_server.py on 127.0.0.1 (all three SSE formats, tool-call round
trips, 401, 429 with Retry-After); no real provider is contacted and no agent CLI runs.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from fixtures.fake_api_server import FakeApiServer, call, error, reply

from agent_nexus import agent_cli
from agent_nexus.agents import registry, sessions
from agent_nexus.agents.api import config as api_config
from agent_nexus.agents.api import models, protocols, tools
from agent_nexus.agents.api.keys import KeyStore
from agent_nexus.agents.api.store import SessionStore, repair
from agent_nexus.agents.hub import OPS, AgentHub

PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
KEY = "sk-test-0123456789abcd"


@pytest.fixture(autouse=True)
def _direct(monkeypatch):
    """The fake server is on loopback: no proxy from the test machine in between (trust_env stays on)."""
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture
def server():
    fake = FakeApiServer().start()
    yield fake
    fake.stop()


def provider(server, protocol="openai", pid="fake", **extra) -> dict:
    return api_config.normalize_provider({
        "id": pid, "label": "Fake", "protocol": protocol, "base_url": server.url + ("/v1" if protocol == "openai" else ""),
        "models": [{"id": "fake-model", "label": "Fake Model", "context_window": 64000}], **extra})


# ============================================================================================== protocols

@pytest.mark.parametrize("protocol", ["openai", "anthropic", "gemini"])
async def test_stream_turn_text_thinking_tool_calls_and_usage(server, protocol):
    server.script(reply("你好，世界", thinking="想一想", tool_calls=[call("read_file", {"path": "a.txt"}, "c1")],
                        usage=(120, 30)))
    p = provider(server, protocol)
    deltas = []
    history = [{"role": "user", "text": "hi"}]
    async with protocols.client() as http:
        result = await protocols.stream_turn(http, p, p["models"][0], KEY, "SYSTEM", history,
                                             tools.specs("ask"), lambda kind, text: deltas.append((kind, text)))
    assert result.text == "你好，世界"
    assert result.thinking == "想一想"
    assert [(c.name, c.args) for c in result.tool_calls] == [("read_file", {"path": "a.txt"})]
    assert result.usage["input"] == 120 and result.usage["output"] == 30
    assert "".join(t for k, t in deltas if k == "text") == "你好，世界"
    assert len([d for d in deltas if d[0] == "text"]) >= 2  # streamed in pieces
    sent = server.posts()[-1]
    if protocol == "openai":
        assert sent["path"] == "/v1/chat/completions"
        assert sent["headers"]["authorization"] == f"Bearer {KEY}"
        assert sent["body"]["stream"] is True and sent["body"]["stream_options"] == {"include_usage": True}
        assert sent["body"]["messages"][0] == {"role": "system", "content": "SYSTEM"}
        assert sent["body"]["tools"][0]["type"] == "function"
    elif protocol == "anthropic":
        assert sent["path"] == "/v1/messages"
        assert sent["headers"]["x-api-key"] == KEY and sent["headers"]["anthropic-version"]
        assert sent["body"]["system"] == "SYSTEM" and sent["body"]["max_tokens"] > 0
        assert result.signature == "sig-1"
        assert result.usage["cached"] == 30
    else:
        assert sent["path"] == "/v1beta/models/fake-model:streamGenerateContent?alt=sse"
        assert sent["headers"]["x-goog-api-key"] == KEY
        assert sent["body"]["systemInstruction"] == {"parts": [{"text": "SYSTEM"}]}
        assert "additionalProperties" not in json.dumps(sent["body"]["tools"])
        assert result.tool_calls[0].signature == "ts-0"


@pytest.mark.parametrize("protocol", ["openai", "anthropic", "gemini"])
def test_tool_round_trip_bodies(protocol):
    """The neutral history becomes each protocol's tool-call / tool-result shape."""
    history = [{"role": "user", "text": "read it"},
               {"role": "assistant", "text": "", "thinking": "t", "signature": "s",
                "tool_calls": [{"id": "c1", "name": "read_file", "args": {"path": "a"}, "signature": "ts"}]},
               {"role": "tool", "results": [{"id": "c1", "name": "read_file", "output": "AAA", "error": False}]}]
    model = {"id": "m"}
    body = protocols.BODIES[protocol](model, "", history, [])
    text = json.dumps(body)
    if protocol == "openai":
        assert body["messages"][1]["tool_calls"][0]["function"] == {"name": "read_file", "arguments": '{"path": "a"}'}
        assert body["messages"][2] == {"role": "tool", "tool_call_id": "c1", "content": "AAA"}
    elif protocol == "anthropic":
        assert body["messages"][1]["content"][0] == {"type": "thinking", "thinking": "t", "signature": "s"}
        assert body["messages"][1]["content"][1]["type"] == "tool_use"
        assert body["messages"][2]["content"][0]["type"] == "tool_result"
    else:
        assert body["contents"][1]["parts"][0]["thoughtSignature"] == "ts"
        assert body["contents"][2]["parts"][0]["functionResponse"] == {"name": "read_file", "response": {"output": "AAA"}}
    assert "AAA" in text


async def test_http_errors_map_to_people_messages(server):
    p = provider(server)
    server.script(error(401, "invalid api key"), error(429, "slow down", {"Retry-After": "7"}))
    async with protocols.client() as http:
        with pytest.raises(protocols.ApiError) as auth:
            await protocols.stream_turn(http, p, p["models"][0], "bad", "", [{"role": "user", "text": "x"}], [])
        with pytest.raises(protocols.ApiError) as limited:
            await protocols.stream_turn(http, p, p["models"][0], "bad", "", [{"role": "user", "text": "x"}], [])
    assert auth.value.code == "auth" and "密钥无效或无权限" in str(auth.value) and "invalid api key" in str(auth.value)
    assert limited.value.code == "rate_limited" and limited.value.retry_after == 7.0


async def test_network_error_names_the_host():
    dead = api_config.normalize_provider({"id": "dead", "protocol": "openai", "base_url": "http://127.0.0.1:9/v1",
                                          "models": ["m"]})
    async with protocols.client() as http:
        with pytest.raises(protocols.ApiError) as failed:
            await protocols.stream_turn(http, dead, dead["models"][0], None, "", [{"role": "user", "text": "x"}], [])
    assert failed.value.code in ("network", "timeout") and "127.0.0.1:9" in str(failed.value)


def test_endpoints_do_not_double_versions():
    assert protocols.endpoint({"protocol": "anthropic", "base_url": "https://x/v1"}, "m") == "https://x/v1/messages"
    assert protocols.endpoint({"protocol": "gemini", "base_url": "https://g/v1beta"}, "a/b").endswith(
        "/v1beta/models/a%2Fb:streamGenerateContent?alt=sse")
    assert protocols.endpoint({"protocol": "openai", "base_url": "https://o/v1"}, "m") == "https://o/v1/chat/completions"


def test_context_window_table():
    assert models.context_window("gpt-4o-mini") == 128_000
    assert models.context_window("anthropic/claude-sonnet-4.5") == 200_000
    assert models.context_window("deepseek-chat") == 128_000
    assert models.context_window("gemini-2.5-flash") == 1_048_576
    assert models.context_window("llama3.1:8b") == 131_072
    assert models.context_window("totally-unknown") is None


# ============================================================================================== keys and config

def test_key_file_is_owner_only_and_never_returned(tmp_path, monkeypatch):
    keys = KeyStore(tmp_path)
    status = keys.set("fake", KEY)
    assert status["set"] and status["last4"] == "abcd" and KEY not in json.dumps(status)
    if os.name != "nt":
        assert stat.S_IMODE(keys.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(keys.path.parent.stat().st_mode) == 0o700
    assert keys.resolve("fake") == KEY
    monkeypatch.setenv("FAKE_KEY_VAR", "from-env")
    assert keys.set("other", "env:FAKE_KEY_VAR") == {"set": True, "env": "FAKE_KEY_VAR",
                                                     "updated": keys.status("other")["updated"]}
    assert keys.resolve("other") == "from-env"
    assert keys.status("other", {})["set"] is False
    keys.set("fake", None)
    assert keys.status("fake") == {"set": False} and keys.resolve("fake") is None
    with pytest.raises(ValueError):
        keys.set("Bad Id", "x")
    with pytest.raises(ValueError):
        keys.set("fake", "env:not a name")


def test_config_refuses_key_fields_and_merges_local_first(tmp_path):
    for bad in ({"api_key": "x"}, {"headers": {"Authorization": "Bearer x"}}, {"key": "x"},
                {"models": [{"id": "m", "token": "x"}]}, {"headers": {"X-Api-Key": "x"}}):
        with pytest.raises(api_config.ConfigError):
            api_config.normalize_provider({"id": "p", "protocol": "openai", "base_url": "https://x", **bad})
    assert api_config.find_secret({"providers": [{"models": [{"max_output": 1, "context_window": 2}]}]}) is None
    cfg = api_config.ApiConfig(tmp_path)
    cfg.save({"id": "one", "protocol": "openai", "base_url": "https://a/v1/", "models": ["m1", {"id": "m2", "enabled": False}]})
    cfg.set_gateway([{"id": "one", "label": "G1", "protocol": "openai", "base_url": "https://gw", "models": ["x"]},
                     {"id": "two", "protocol": "gemini", "base_url": "https://g", "models": ["gm"]},
                     {"id": "bad", "protocol": "openai", "base_url": "https://b", "api_key": "leak"}])
    merged = cfg.providers()
    assert [(p["id"], p["source"]) for p in merged] == [("one", "local"), ("two", "gateway")]
    assert merged[0]["base_url"] == "https://a/v1"
    assert set(cfg.defs()) == {"api:one/m1", "api:two/gm"}
    data = json.loads((tmp_path / "agent-nexus.json").read_text())
    assert data["agents"]["api"]["providers"][0]["id"] == "one"
    assert cfg.delete("one") and not cfg.delete("two")
    assert api_config.parse_kind("api:two/a/b:c") == ("two", "a/b:c")


def test_store_repair_answers_dangling_tool_calls(tmp_path):
    history = [{"role": "user", "text": "a"},
               {"role": "assistant", "text": "", "tool_calls": [{"id": "c1", "name": "run_command", "args": {}}]},
               {"role": "user", "text": "b"}]
    fixed = repair(history)
    assert [m["role"] for m in fixed] == ["user", "assistant", "tool", "user"]
    assert fixed[2]["results"][0]["id"] == "c1" and fixed[2]["results"][0]["error"]
    store = SessionStore(tmp_path)
    store.append("s1", [{"type": "msg", "msg": history[0]}], meta={"kind": "api:p/m"})
    with open(store.path("s1"), "a", encoding="utf-8") as handle:
        handle.write('{"type": "msg", "msg": {"role"')  # torn last line
    assert store.load("s1")[1] == [history[0]]
    with pytest.raises(LookupError):
        store.path("../evil")


# ============================================================================================== tools

@pytest.fixture
def box(tmp_path):
    work = tmp_path / "work"
    tmp = tmp_path / "tmp"
    outside = tmp_path / "outside"
    for folder in (work, tmp, outside):
        folder.mkdir()
    (outside / "secret.txt").write_text("SECRET", encoding="utf-8")
    (work / "a.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    (work / "src").mkdir()
    (work / "src" / "m.py").write_text("def f():\n    return 42\n", encoding="utf-8")
    return {"work": work, "tmp": tmp, "outside": outside, "toolbox": tools.Toolbox(str(work), str(tmp))}


async def test_paths_are_confined_after_resolving_symlinks(box):
    toolbox = box["toolbox"]
    if os.name != "nt":
        os.symlink(box["outside"], box["work"] / "link")
        os.symlink(box["outside"] / "secret.txt", box["work"] / "s.txt")
    attempts = [("read_file", {"path": "../outside/secret.txt"}), ("read_file", {"path": str(box["outside"] / "secret.txt")}),
                ("write_file", {"path": "../outside/new.txt", "content": "x"}), ("list_dir", {"path": ".."}),
                ("grep", {"pattern": "SECRET", "path": str(box["outside"])}), ("glob", {"pattern": "../*"})]
    if os.name != "nt":
        attempts += [("read_file", {"path": "link/secret.txt"}), ("read_file", {"path": "s.txt"}),
                     ("write_file", {"path": "link/new.txt", "content": "x"}), ("edit_file", {"path": "s.txt",
                     "old_string": "SECRET", "new_string": "x"})]
    for name, args in attempts:
        outcome = await toolbox.run(name, args)
        assert outcome.error, (name, args, outcome.output)
        assert "SECRET" not in outcome.output
    assert not (box["outside"] / "new.txt").exists()
    assert (box["outside"] / "secret.txt").read_text() == "SECRET"
    grep_all = await toolbox.run("grep", {"pattern": "SECRET"})
    assert "SECRET" not in grep_all.output  # the symlinked file is skipped inside a search too
    ok = await toolbox.run("write_file", {"path": str(box["tmp"] / "scratch.txt"), "content": "tmp is fine"})
    assert not ok.error


async def test_file_tools(box):
    toolbox = box["toolbox"]
    read = await toolbox.run("read_file", {"path": "a.txt", "offset": 2, "limit": 1})
    assert "beta" in read.output and "alpha" not in read.output and "offset=3" in read.output
    listed = await toolbox.run("list_dir", {})
    assert listed.output.splitlines()[0] == "src/"
    assert "src/m.py" in (await toolbox.run("glob", {"pattern": "**/*.py"})).output
    assert "src/m.py:2:" in (await toolbox.run("grep", {"pattern": "return \\d+", "glob": "*.py"})).output
    wrote = await toolbox.run("write_file", {"path": "new/b.txt", "content": "one\n"})
    assert not wrote.error and wrote.events[0]["kind"] == "add" and (box["work"] / "new" / "b.txt").exists()
    edited = await toolbox.run("edit_file", {"path": "a.txt", "old_string": "beta", "new_string": "BETA"})
    assert not edited.error and edited.events[0]["kind"] == "edit" and "+BETA" in edited.events[0]["diff"]
    twice = await toolbox.run("edit_file", {"path": "a.txt", "old_string": "a", "new_string": "x"})
    assert twice.error and "replace_all" in twice.output
    missing = await toolbox.run("edit_file", {"path": "a.txt", "old_string": "zzz", "new_string": "x"})
    assert missing.error


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
async def test_run_command_output_exit_code_and_timeout(box):
    toolbox = tools.Toolbox(str(box["work"]), str(box["tmp"]), timeout_s=1)
    done = await toolbox.run("run_command", {"command": "pwd; echo out; echo err 1>&2; exit 3"})
    assert done.error and "[退出码 3]" in done.output and "out" in done.output and "err" in done.output
    assert os.path.realpath(str(box["work"])) in done.output
    slow = await toolbox.run("run_command", {"command": "sleep 30"})
    assert slow.error and "已结束" in slow.output
    many = await toolbox.run("run_command", {"command": "seq 1 20000"})
    assert "省略" in many.output and len(many.output) < tools.OUTPUT_MAX + 200


def test_permission_table():
    assert {s["name"] for s in tools.specs("readonly")} == set(tools.READ_TOOLS)
    assert {s["name"] for s in tools.specs("ask")} == set(tools.ALL_TOOLS)
    assert [tools.needs_approval(n, "ask") for n in ("read_file", "write_file", "run_command")] == [False, True, True]
    assert [tools.needs_approval(n, "edits") for n in ("read_file", "edit_file", "run_command")] == [False, False, True]
    assert [tools.needs_approval(n, "full") for n in ("write_file", "run_command")] == [False, False]
    assert not tools.allowed("write_file", "readonly")


# ============================================================================================== the hub

@pytest.fixture
def hub_box(tmp_path, monkeypatch, server):
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    (home / "proj" / "notes.txt").write_text("hello notes\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(tmp_path / "nobin"))
    monkeypatch.setattr(agent_cli, "candidates", lambda name, env: [])
    registry.invalidate()
    sessions.clear_cache()
    root = tmp_path / "root"
    root.mkdir()
    hub = AgentHub(root, workspaces=list, device=lambda: {"id": "pc-1", "name": "笔记本", "gateway": ""})
    handlers = hub.handlers()
    yield {"hub": hub, "h": handlers, "home": home, "proj": home / "proj", "root": root, "server": server}
    hub.close()


async def add_provider(box, protocol="openai", pid="fake", models_=None, key=KEY):
    server = box["server"]
    body = {"id": pid, "label": "Fake " + protocol, "protocol": protocol,
            "base_url": server.url + ("/v1" if protocol == "openai" else ""),
            "models": models_ or [{"id": "fake-model", "label": "Fake Model", "context_window": 64000}],
            "key_optional": False}  # loopback would need no key otherwise
    saved = await box["h"]["agent_api_provider_save"]({"provider": body})
    assert saved.get("ok"), saved
    if key:
        assert (await box["h"]["agent_api_key_set"]({"provider_id": pid, "key": key}))["ok"]
    return f"api:{pid}/{(models_ or [{'id': 'fake-model'}])[0]['id']}"


async def read_until(h, rid, *, done=("done", "failed", "stopped"), pending_approval=False, timeout=15.0, cursor=0):
    events = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        result = await h["agent_run_read"]({"rid": rid, "cursor": cursor, "wait": 2})
        assert "error" not in result, result
        events += result["events"]
        cursor = result["cursor"]
        if pending_approval and any(e["type"] == "approval" and e.get("state") == "pending" for e in result["events"]):
            return events, cursor
        if any(e["type"] == "status" and e.get("state") in done for e in result["events"]):
            return events, cursor
    raise AssertionError(f"timed out: {events}")


def test_ops_are_registered():
    for op in ("agent_api_providers", "agent_api_provider_save", "agent_api_provider_delete", "agent_api_key_set",
               "agent_api_test", "agent_api_models"):
        assert op in OPS


async def test_providers_rpc_never_returns_keys(hub_box):
    kind = await add_provider(hub_box)
    listed = await hub_box["h"]["agent_api_providers"]({})
    text = json.dumps(listed)
    assert KEY not in text
    entry = listed["providers"][0]
    assert entry["key"] == {"set": True, "last4": "abcd"} and entry["source"] == "local"
    info = await hub_box["h"]["agent_info"]({})
    api = [a for a in info["agents"] if a["kind"] == kind]
    assert api and api[0]["adapter"] == "api" and api[0]["login"] == "ok"
    assert api[0]["supports"] == {"resume": True, "approvals": True, "stream": True, "tools": True}
    assert api[0]["provider_label"] == "Fake openai" and api[0]["context_window"] == 64000
    assert KEY not in json.dumps(info)
    refused = await hub_box["h"]["agent_api_provider_save"]({"provider": {
        "id": "x", "protocol": "openai", "base_url": "https://x", "api_key": KEY}})
    assert refused["code"] == "bad_request"
    assert (await hub_box["h"]["agent_api_key_set"]({"provider_id": "nope", "key": "x"}))["code"] == "not_found"
    await hub_box["h"]["agent_api_key_set"]({"provider_id": "fake", "key": None})
    info = await hub_box["h"]["agent_info"]({"kind": kind})
    assert info["agents"][0]["login"] == "missing"
    opened = await hub_box["h"]["agent_run_open"]({"kind": kind, "workdir": str(hub_box["proj"]), "prompt": "hi"})
    assert opened["code"] == "no_key"
    assert (await hub_box["h"]["agent_api_provider_delete"]({"provider_id": "fake"}))["ok"]
    assert (await hub_box["h"]["agent_api_providers"]({}))["providers"] == []


async def test_test_and_models_rpcs(hub_box):
    await add_provider(hub_box)
    server = hub_box["server"]
    server.script(reply("pong"))
    tested = await hub_box["h"]["agent_api_test"]({"provider_id": "fake", "model": "fake-model"})
    assert tested["ok"] and tested["reply"] == "pong" and tested["latency_ms"] >= 0
    assert server.posts()[-1]["body"]["max_tokens"] == 16
    server.script(error(401))
    failed = await hub_box["h"]["agent_api_test"]({"provider_id": "fake"})
    assert failed["code"] == "auth" and failed["ok"] is False
    listed = await hub_box["h"]["agent_api_models"]({"provider_id": "fake"})
    assert [m["id"] for m in listed["models"]] == ["fake-small", "fake-large"]
    assert listed["models"][0]["context_window"] == 32000
    assert server.requests[-1]["path"] == "/v1/models"


@pytest.mark.parametrize("protocol", ["openai", "anthropic", "gemini"])
async def test_run_with_a_tool_round_trip_then_resume(hub_box, protocol):
    kind = await add_provider(hub_box, protocol)
    server = hub_box["server"]
    server.script(reply("我先读一下", tool_calls=[call("read_file", {"path": "notes.txt"}, "c1")], usage=(100, 10)),
                  reply("文件里写着 hello notes", usage=(150, 12)))
    h = hub_box["h"]
    opened = await h["agent_run_open"]({"kind": kind, "workdir": str(hub_box["proj"]), "prompt": "读 notes.txt",
                                        "permission": "readonly"})
    assert "error" not in opened, opened
    assert opened["adapter"] == "api" and opened["session_id"]
    events, _ = await read_until(h, opened["rid"])
    types = [e["type"] for e in events]
    assert "tool" in types and "usage" in types
    tool_done = [e for e in events if e["type"] == "tool" and e.get("status") == "ok"]
    assert tool_done and "hello notes" in tool_done[0]["output"]
    text = "".join(e["text"] for e in events if e["type"] == "message")
    assert "hello notes" in text
    usage = [e for e in events if e["type"] == "usage"][-1]
    assert usage["context"] == {"used": 162, "limit": 64000, "model": usage["context"]["model"]}
    assert usage["turn"]["input"] == 250 and usage["turn"]["output"] == 22
    final = events[-1]
    assert final["type"] == "status" and final["state"] == "done" and final["usage"]["input"] == 250
    assert types.index("usage") < len(types) - 1
    # the second request carried the tool result
    second = json.dumps(server.posts()[-1]["body"], ensure_ascii=False)
    assert "hello notes" in second
    # the session is listed and readable, then resumed in a new run with its history
    session_id = opened["session_id"]
    listed = await h["agent_sessions"]({})
    mine = [s for s in listed["sessions"] if s["id"] == session_id]
    assert mine and mine[0]["kind"] == kind and mine[0]["title"] == "读 notes.txt"
    assert mine[0]["cwd"] == str(hub_box["proj"])
    assert any(g["kind"] == kind for g in listed["groups"])
    only = await h["agent_sessions"]({"kind": "api"})
    assert [s["id"] for s in only["sessions"]] == [session_id]
    history = await h["agent_session_read"]({"kind": kind, "id": session_id})
    htypes = [e["type"] for e in history["events"]]
    assert htypes[0] == "user" and "tool" in htypes and htypes[-2:] == ["usage", "status"]
    await h["agent_run_close"]({"rid": opened["rid"]})
    server.script(reply("还记得"))
    resumed = await h["agent_run_open"]({"kind": kind, "workdir": str(hub_box["proj"]), "prompt": "还记得吗",
                                         "resume_id": session_id, "permission": "readonly"})
    assert resumed["session_id"] == session_id
    await read_until(h, resumed["rid"])
    body = json.dumps(server.posts()[-1]["body"], ensure_ascii=False)
    assert "读 notes.txt" in body and "hello notes" in body and "还记得吗" in body
    # deleting the session file
    await h["agent_run_close"]({"rid": resumed["rid"]})
    gone = await h["agent_session_meta"]({"kind": kind, "id": session_id, "delete_file": True})
    assert gone == {"ok": True, "deleted": 1}
    assert (await h["agent_session_read"]({"kind": kind, "id": session_id}))["code"] == "not_found"


async def test_system_prompt_goes_into_the_system_field(hub_box):
    kind = await add_provider(hub_box)
    h = hub_box["h"]
    opened = await h["agent_run_open"]({"kind": kind, "workdir": str(hub_box["proj"]), "prompt": "hi"})
    await read_until(h, opened["rid"])
    body = hub_box["server"].posts()[-1]["body"]
    assert body["messages"][0]["role"] == "system" and body["messages"][0]["content"]
    assert body["messages"][1] == {"role": "user", "content": "hi"}  # not prefixed to the first message


async def test_ask_mode_write_needs_approval_and_deny_is_told(hub_box):
    kind = await add_provider(hub_box)
    server, h, proj = hub_box["server"], hub_box["h"], hub_box["proj"]
    server.script(reply("", tool_calls=[call("write_file", {"path": "out.txt", "content": "A"}, "w1")]),
                  reply("", tool_calls=[call("write_file", {"path": "out2.txt", "content": "B"}, "w2")]),
                  reply("好了"))
    opened = await h["agent_run_open"]({"kind": kind, "workdir": str(proj), "prompt": "写文件", "permission": "ask"})
    events, cursor = await read_until(h, opened["rid"], pending_approval=True)
    card = next(e for e in events if e["type"] == "approval" and e["state"] == "pending")
    assert card["tool"] == "write_file" and card["summary"] == "out.txt"
    assert not (proj / "out.txt").exists()
    assert (await h["agent_run_answer"]({"rid": opened["rid"], "approval_id": card["id"], "allow": True}))["ok"]
    more = await h["agent_run_read"]({"rid": opened["rid"], "cursor": cursor, "wait": 5})
    events = more["events"]
    while not any(e["type"] == "approval" and e.get("state") == "pending" for e in events):
        nxt = await h["agent_run_read"]({"rid": opened["rid"], "cursor": more["cursor"], "wait": 5})
        more, events = nxt, events + nxt["events"]
    second = next(e for e in events if e["type"] == "approval" and e.get("state") == "pending")
    await h["agent_run_answer"]({"rid": opened["rid"], "approval_id": second["id"], "allow": False})
    rest, _ = await read_until(h, opened["rid"])
    assert (proj / "out.txt").read_text() == "A" and not (proj / "out2.txt").exists()
    assert any(e["type"] == "file_change" and e["path"] == "out.txt" for e in events + rest)
    told = [r for r in server.posts()[-1]["body"]["messages"] if r["role"] == "tool"]
    assert "拒绝" in told[-1]["content"]


async def test_edits_mode_writes_freely_but_asks_for_commands(hub_box):
    kind = await add_provider(hub_box)
    server, h, proj = hub_box["server"], hub_box["h"], hub_box["proj"]
    server.script(reply("", tool_calls=[call("write_file", {"path": "e.txt", "content": "E"}, "w1"),
                                        call("run_command", {"command": "echo hi"}, "r1")]), reply("ok"))
    opened = await h["agent_run_open"]({"kind": kind, "workdir": str(proj), "prompt": "x", "permission": "edits"})
    events, _ = await read_until(h, opened["rid"], pending_approval=True)
    pending = [e for e in events if e["type"] == "approval" and e.get("state") == "pending"]
    assert [p["tool"] for p in pending] == ["run_command"] and pending[0]["remember_scope"] == "command"
    assert (proj / "e.txt").read_text() == "E"  # written before the command's card, without one
    await h["agent_run_answer"]({"rid": opened["rid"], "approval_id": pending[0]["id"], "allow": True})
    await read_until(h, opened["rid"])


async def test_full_mode_runs_commands_without_cards_and_readonly_refuses_writes(hub_box):
    kind = await add_provider(hub_box)
    server, h, proj = hub_box["server"], hub_box["h"], hub_box["proj"]
    server.script(reply("", tool_calls=[call("run_command", {"command": "echo full > f.txt"}, "r1")]), reply("ok"))
    opened = await h["agent_run_open"]({"kind": kind, "workdir": str(proj), "prompt": "x", "permission": "full"})
    events, _ = await read_until(h, opened["rid"])
    assert not [e for e in events if e["type"] == "approval"]
    if sys.platform != "win32":
        assert (proj / "f.txt").read_text().strip() == "full"
    assert {t["function"]["name"] for t in server.posts()[0]["body"]["tools"]} == set(tools.ALL_TOOLS)
    server.script(reply("", tool_calls=[call("write_file", {"path": "ro.txt", "content": "x"}, "w1")]), reply("ok"))
    ro = await h["agent_run_open"]({"kind": kind, "workdir": str(proj), "prompt": "x", "permission": "readonly"})
    events, _ = await read_until(h, ro["rid"])
    assert not (proj / "ro.txt").exists()
    assert [e for e in events if e["type"] == "tool" and e.get("status") == "error"]
    offered = {t["function"]["name"] for t in server.posts()[-2]["body"]["tools"]}
    assert offered == set(tools.READ_TOOLS)


async def test_429_is_retried_once_after_retry_after(hub_box):
    kind = await add_provider(hub_box)
    server, h = hub_box["server"], hub_box["h"]
    server.script(error(429, "busy", {"Retry-After": "0"}), reply("after retry"))
    opened = await h["agent_run_open"]({"kind": kind, "workdir": str(hub_box["proj"]), "prompt": "x"})
    events, _ = await read_until(h, opened["rid"])
    assert any(e["type"] == "error" and e.get("level") == "warn" and "429" in e["text"] for e in events)
    assert events[-1]["state"] == "done" and "after retry" in "".join(e["text"] for e in events if e["type"] == "message")
    server.script(error(429, "busy", {"Retry-After": "0"}), error(429, "still busy", {"Retry-After": "0"}))
    await h["agent_run_send"]({"rid": opened["rid"], "prompt": "again"})
    events, _ = await read_until(h, opened["rid"], done=("failed",))
    assert [e for e in events if e["type"] == "error" and e.get("code") == "rate_limited"]


async def test_401_fails_the_turn_with_the_key_message(hub_box):
    kind = await add_provider(hub_box)
    hub_box["server"].script(error(401, "bad key"))
    h = hub_box["h"]
    opened = await h["agent_run_open"]({"kind": kind, "workdir": str(hub_box["proj"]), "prompt": "x"})
    events, _ = await read_until(h, opened["rid"])
    assert events[-1]["state"] == "failed"
    assert any(e["type"] == "error" and "密钥无效或无权限" in e["text"] for e in events)


async def test_stop_cancels_a_streaming_turn(hub_box):
    kind = await add_provider(hub_box)
    server, h = hub_box["server"], hub_box["h"]
    server.script({**reply("半句话"), "hang": 30})
    opened = await h["agent_run_open"]({"kind": kind, "workdir": str(hub_box["proj"]), "prompt": "x"})
    cursor = 0
    for _ in range(50):
        result = await h["agent_run_read"]({"rid": opened["rid"], "cursor": cursor, "wait": 0.5})
        cursor = result["cursor"]
        if any(e["type"] == "message" for e in result["events"]):
            break
    else:
        raise AssertionError("no streamed text")
    assert (await h["agent_run_stop"]({"rid": opened["rid"]}))["ok"]
    events, cursor = await read_until(h, opened["rid"], done=("stopped",))
    assert events[-1]["state"] == "stopped"
    server.release.set()
    server.script(reply("继续"))
    await h["agent_run_send"]({"rid": opened["rid"], "prompt": "go on"})
    await read_until(h, opened["rid"], cursor=cursor, done=("done",))
    sent = server.posts()[-1]["body"]["messages"]
    assert [m["role"] for m in sent[1:]] == ["user", "assistant", "user"]
    assert sent[2]["content"].startswith("半")  # what had streamed before the stop


async def test_gateway_copy_is_pulled_and_merged_under_local(hub_box):
    calls = []

    async def gateway(op, args):
        calls.append(op)
        if op == "api_config_get":
            return {"providers": [{"id": "shared", "label": "Shared", "protocol": "openai",
                                   "base_url": hub_box["server"].url + "/v1", "models": ["m"]}]}
        return None

    hub = AgentHub(hub_box["root"], workspaces=list, gateway=gateway,
                   device=lambda: {"id": "pc-1", "name": "x", "gateway": "https://gw"})
    listed = await hub.handlers()["agent_api_providers"]({})
    assert [(p["id"], p["source"]) for p in listed["providers"]] == [("shared", "gateway")]
    assert "api:shared/m" in hub.definitions()
    assert (await hub.handlers()["agent_api_provider_delete"]({"provider_id": "shared"}))["code"] == "gateway_provider"
    assert calls.count("api_config_get") == 1
    await hub.handlers()["agent_api_providers"]({})
    assert calls.count("api_config_get") == 1  # pulled at most once a minute
    hub.close()


# ============================================================================================== the gateway

def test_gateway_api_config_ops(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from test_agents_gateway import connect
    from test_console_m16 import ORIGIN, SERVER_TOKEN
    from test_remote_m19 import TOKEN_A, hub_call, network_config

    from agent_nexus.cloud import build_cloud_app

    owner = {"Authorization": f"Bearer {SERVER_TOKEN}", "X-ANX": "1"}
    with TestClient(build_cloud_app(network_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        assert client.get("/api/agents/api_config_get").status_code == 401
        good = {"providers": [{"id": "ds", "label": "DeepSeek", "protocol": "openai",
                               "base_url": "https://api.deepseek.com/v1", "models": [{"id": "deepseek-chat"}]}]}
        put = client.post("/api/agents/api_config_put", json=good, headers=owner)
        assert put.status_code == 200 and put.json()["providers"][0]["id"] == "ds"
        for leak in ({"providers": [{**good["providers"][0], "api_key": "sk-x"}]},
                     {"providers": [{**good["providers"][0], "headers": {"Authorization": "Bearer x"}}]},
                     {"providers": [], "token": "x"}):
            refused = client.post("/api/agents/api_config_put", json=leak, headers=owner)
            assert refused.status_code == 400 and "密钥" in refused.json()["error"]
        got = client.get("/api/agents/api_config_get", headers=owner).json()
        assert got["providers"][0]["id"] == "ds" and "sk-x" not in json.dumps(got)
        ws = connect(client, "pc-a", TOKEN_A, ["agents/1"])
        try:
            pulled = hub_call(ws, "api_config_get", {})
            assert pulled["result"]["providers"][0]["base_url"] == "https://api.deepseek.com/v1"
            bad = hub_call(ws, "api_config_put", {"providers": [{"id": "x", "protocol": "openai",
                                                                 "base_url": "https://x", "secret": "s"}]})
            assert "error" in bad
            ok = hub_call(ws, "api_config_put", {"providers": []})
            assert ok["result"]["ok"]
        finally:
            ws.__exit__(None, None, None)


# ============================================================================================== review fixes

async def test_stop_mid_batch_keeps_the_results_of_the_calls_that_ran(hub_box):
    """read_file A ran, run_command B waits for its card, stop: the next request answers both A (its real
    result) and B ("stopped") -- a tool_call without its result is a 400 at every provider."""
    kind = await add_provider(hub_box)
    server, h = hub_box["server"], hub_box["h"]
    server.script(reply("", tool_calls=[call("read_file", {"path": "notes.txt"}, "A"),
                                        call("run_command", {"command": "echo hi"}, "B")]))
    opened = await h["agent_run_open"]({"kind": kind, "workdir": str(hub_box["proj"]), "prompt": "x", "permission": "ask"})
    _events, cursor = await read_until(h, opened["rid"], pending_approval=True)
    assert (await h["agent_run_stop"]({"rid": opened["rid"]}))["ok"]
    _events, cursor = await read_until(h, opened["rid"], done=("stopped",), cursor=cursor)
    server.script(reply("好"))
    await h["agent_run_send"]({"rid": opened["rid"], "prompt": "go on"})
    await read_until(h, opened["rid"], cursor=cursor, done=("done",))
    sent = server.posts()[-1]["body"]["messages"]
    results = {m["tool_call_id"]: m["content"] for m in sent if m["role"] == "tool"}
    assert set(results) == {"A", "B"}
    assert "hello notes" in results["A"] and "停止" in results["B"]
    # the saved history says the same (A really ran), for a resume
    _meta, history = SessionStore(hub_box["root"]).load(opened["session_id"])
    saved = [r for m in history if m["role"] == "tool" for r in m["results"]]
    assert {r["id"]: r["error"] for r in saved} == {"A": False, "B": True}


async def test_tool_call_ids_are_unique_across_steps_and_the_whole_text_is_emitted(hub_box):
    kind = await add_provider(hub_box, "gemini")
    server, h = hub_box["server"], hub_box["h"]
    server.script(reply("", tool_calls=[call("read_file", {"path": "notes.txt"})]),
                  reply("", tool_calls=[call("list_dir", {})]), reply("全部看完了"))
    opened = await h["agent_run_open"]({"kind": kind, "workdir": str(hub_box["proj"]), "prompt": "x",
                                        "permission": "readonly"})
    events, _ = await read_until(h, opened["rid"])
    ids = [e["id"] for e in events if e["type"] == "tool" and e.get("status") == "ok"]
    assert len(ids) == 2 and len(set(ids)) == 2, ids  # fc_1 twice before: the second card replaced the first
    deltas = [e for e in events if e["type"] == "message" and e.get("delta")]
    whole = [e for e in events if e["type"] == "message" and not e.get("delta")]
    assert deltas and whole and whole[-1]["text"] == "全部看完了" and whole[-1]["id"] == deltas[-1]["id"]


def test_empty_assistant_turns_are_never_sent():
    history = [{"role": "user", "text": "a"}, {"role": "assistant", "text": "", "thinking": "hmm"},
               {"role": "user", "text": "b"}]
    model = {"id": "m"}
    anthropic = protocols.anthropic_body(model, "", history, [])["messages"]
    assert [m["role"] for m in anthropic] == ["user"]
    assert [c["text"] for c in anthropic[0]["content"]] == ["a", "b"]
    gemini = protocols.gemini_body(model, "", history, [])["contents"]
    assert [c["role"] for c in gemini] == ["user"] and len(gemini[0]["parts"]) == 2
    openai = protocols.openai_body(model, "", history, [])["messages"]
    assert [m["role"] for m in openai] == ["user", "user"]
    assert '"text": ""' not in json.dumps([anthropic, gemini])


async def test_stopping_after_thinking_only_stores_no_empty_assistant_turn(hub_box):
    kind = await add_provider(hub_box, "anthropic")
    server, h = hub_box["server"], hub_box["h"]
    server.script({**reply("", thinking="想一想"), "hang": 30})
    opened = await h["agent_run_open"]({"kind": kind, "workdir": str(hub_box["proj"]), "prompt": "x"})
    cursor = 0
    for _ in range(50):
        result = await h["agent_run_read"]({"rid": opened["rid"], "cursor": cursor, "wait": 0.5})
        cursor = result["cursor"]
        if any(e["type"] == "thinking" for e in result["events"]):
            break
    await h["agent_run_stop"]({"rid": opened["rid"]})
    _events, cursor = await read_until(h, opened["rid"], done=("stopped",), cursor=cursor)
    server.release.set()
    server.script(reply("好"))
    await h["agent_run_send"]({"rid": opened["rid"], "prompt": "go on"})
    await read_until(h, opened["rid"], cursor=cursor, done=("done",))
    sent = server.posts()[-1]["body"]["messages"]
    assert all(c.get("text") != "" for m in sent for c in m["content"] if c.get("type") == "text")
    _meta, history = SessionStore(hub_box["root"]).load(opened["session_id"])
    assert not [m for m in history if m["role"] == "assistant" and not m.get("text") and not m.get("tool_calls")
                and m is not history[-1]]


async def test_edit_and_write_keep_crlf_line_endings(box):
    toolbox = box["toolbox"]
    path = box["work"] / "win.txt"
    path.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    edited = await toolbox.run("edit_file", {"path": "win.txt", "old_string": "beta\ngamma", "new_string": "B\nG"})
    assert not edited.error, edited.output
    assert path.read_bytes() == b"alpha\r\nB\r\nG\r\n"
    wrote = await toolbox.run("write_file", {"path": "win.txt", "content": "x\ny\n"})
    assert not wrote.error and path.read_bytes() == b"x\r\ny\r\n"
    (box["work"] / "unix.txt").write_bytes(b"a\nb\n")
    await toolbox.run("write_file", {"path": "unix.txt", "content": "c\nd\n"})
    assert (box["work"] / "unix.txt").read_bytes() == b"c\nd\n"


async def test_file_tools_through_the_run_as_helper(box):
    """The helper path (as the run-as account when the service runs as another one), here as ourselves."""
    toolbox = tools.Toolbox(str(box["work"]), str(box["tmp"]), delegate=True)
    read = await toolbox.run("read_file", {"path": "a.txt"})
    assert not read.error and "beta" in read.output
    wrote = await toolbox.run("write_file", {"path": "sub/c.txt", "content": "中文\n"})
    assert not wrote.error and wrote.events[0]["kind"] == "add"
    assert (box["work"] / "sub" / "c.txt").read_text(encoding="utf-8") == "中文\n"
    assert (box["tmp"] / "anx_fileops.py").is_file()
    refused = await toolbox.run("read_file", {"path": "../outside/secret.txt"})
    assert refused.error and "SECRET" not in refused.output
    assert "src/m.py:2:" in (await toolbox.run("grep", {"pattern": "return"})).output
    assert tools.other_account(None) is False
    if hasattr(os, "geteuid"):
        assert tools.other_account({"user": os.geteuid()}) is False
        assert tools.other_account({"user": os.geteuid() + 1}) is True


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root (a service with run_as)")
async def test_a_root_service_runs_file_tools_as_the_run_as_account(tmp_path):
    import subprocess
    import tempfile
    nobody = 65534
    if subprocess.run([sys.executable, "-I", "-c", "pass"], user=nobody, group=nobody, check=False,  # noqa: ASYNC221
                      capture_output=True).returncode != 0:
        pytest.skip("the run-as account cannot run this Python")
    base = tempfile.mkdtemp(prefix="anx-runas-")
    try:
        os.chmod(base, 0o755)
        work, tmp = os.path.join(base, "work"), os.path.join(base, "tmp")
        os.mkdir(work)
        os.mkdir(tmp)
        os.chown(work, nobody, nobody)
        os.chown(tmp, nobody, nobody)
        secret = os.path.join(work, "root-only.txt")
        Path(secret).write_text("ROOT SECRET", encoding="utf-8")
        os.chmod(secret, 0o600)
        toolbox = tools.Toolbox(work, tmp, run_as={"user": nobody, "group": nobody, "extra_groups": []})
        assert toolbox.delegate
        read = await toolbox.run("read_file", {"path": "root-only.txt"})
        assert read.error and "ROOT SECRET" not in read.output  # root could read it; the user cannot
        wrote = await toolbox.run("write_file", {"path": "made.txt", "content": "x"})
        assert not wrote.error, wrote.output
        assert os.stat(os.path.join(work, "made.txt")).st_uid == nobody
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.skipif(os.name == "nt", reason="symlinks / hard links")
async def test_a_link_swapped_in_after_the_check_is_not_written_through(box, monkeypatch):
    from agent_nexus.agents.api import fileops
    work, outside = box["work"], box["outside"]
    (work / "a").mkdir()
    original = fileops.Files.resolve

    def racing(self, value, **kwargs):
        path = original(self, value, **kwargs)
        if value == "a/authorized_keys":  # the user swaps a folder for a link between the check and the write
            os.rename(work / "a", work / "a.old")
            os.symlink(outside, work / "a")
        return path

    monkeypatch.setattr(fileops.Files, "resolve", racing)
    outcome = await box["toolbox"].run("write_file", {"path": "a/authorized_keys", "content": "ssh-rsa evil"})
    assert outcome.error and not (outside / "authorized_keys").exists()
    monkeypatch.setattr(fileops.Files, "resolve", original)
    os.link(outside / "secret.txt", work / "hard.txt")
    for name, args in (("write_file", {"path": "hard.txt", "content": "x"}),
                       ("edit_file", {"path": "hard.txt", "old_string": "SECRET", "new_string": "x"})):
        outcome = await box["toolbox"].run(name, args)
        assert outcome.error and "硬链接" in outcome.output
    assert (outside / "secret.txt").read_text() == "SECRET"


async def test_anx_state_is_never_readable_by_the_tools(tmp_path):
    home = tmp_path / "home"
    root = home / ".local" / "share" / "agent-nexus"
    (root / ".anx" / "agents").mkdir(parents=True)
    (root / ".anx" / "agents" / "api-keys.json").write_text(json.dumps({"keys": {"p": {"key": KEY}}}), encoding="utf-8")
    (root / ".anx" / "device.json").write_text('{"token": "device-secret"}', encoding="utf-8")
    (home / "notes.txt").write_text("fine\n", encoding="utf-8")
    toolbox = tools.Toolbox(str(home), deny=[str(root / ".anx"), str(root / ".mcp-manager")])
    for name, args in (("read_file", {"path": ".local/share/agent-nexus/.anx/agents/api-keys.json"}),
                       ("list_dir", {"path": str(root / ".anx")}),
                       ("grep", {"pattern": "sk-|device-secret"}),
                       ("grep", {"pattern": "sk-", "path": str(root / ".anx" / "agents" / "api-keys.json")}),
                       ("glob", {"pattern": "**/*.json"})):
        outcome = await toolbox.run(name, args)
        assert KEY not in outcome.output and "device-secret" not in outcome.output, (name, outcome.output)
        if name == "glob":
            assert "api-keys.json" not in outcome.output and "device.json" not in outcome.output
        elif "path" in args or name == "read_file":
            assert outcome.error and "Agent Nexus 自己的数据" in outcome.output
    assert "fine" in (await toolbox.run("grep", {"pattern": "fine"})).output
    api = __import__("agent_nexus.agents.api.service", fromlist=["ApiAgents"]).ApiAgents(root)
    api.config.save({"id": "p", "protocol": "openai", "base_url": "https://api.example.com/v1", "models": ["m"]})
    api.keys.set("p", KEY, origin="https://api.example.com:443")
    spec = api.run_spec("api:p/m", {})
    assert str(root / ".anx") in spec["deny"]


async def test_a_key_is_never_sent_to_a_changed_address(hub_box):
    kind = await add_provider(hub_box)
    server, h = hub_box["server"], hub_box["h"]
    port = server.url.rsplit(":", 1)[1]
    moved = {"id": "fake", "label": "Fake", "protocol": "openai", "base_url": f"http://localhost:{port}/v1",
             "models": [{"id": "fake-model"}], "key_optional": False}
    assert (await h["agent_api_provider_save"]({"provider": moved}))["ok"]
    listed = (await h["agent_api_providers"]({}))["providers"][0]
    assert listed["key"] == {"set": False, "origin_changed": True} and listed["login"] == "missing"
    info = (await h["agent_info"]({"kind": kind}))["agents"][0]
    assert info["login"] == "missing" and "地址变了" in info["login_detail"]
    opened = await h["agent_run_open"]({"kind": kind, "workdir": str(hub_box["proj"]), "prompt": "x"})
    assert opened["code"] == "key_origin_changed"
    before = len(server.requests)
    assert (await h["agent_api_test"]({"provider_id": "fake"}))["code"] == "key_origin_changed"
    assert (await h["agent_api_models"]({"provider_id": "fake"}))["code"] == "key_origin_changed"
    assert len(server.requests) == before  # nothing went out with the old key
    assert (await h["agent_api_key_set"]({"provider_id": "fake", "key": KEY}))["key"]["set"]
    server.script(reply("pong"))
    assert (await h["agent_api_test"]({"provider_id": "fake"}))["ok"]
    assert server.requests[-1]["headers"]["authorization"] == f"Bearer {KEY}"
    assert server.requests[-1]["headers"]["host"].startswith("localhost")


def test_base_url_userinfo_and_auth_headers_are_refused():
    base = {"id": "x", "protocol": "openai", "models": []}
    with pytest.raises(api_config.ConfigError, match="账号密码"):
        api_config.normalize_provider({**base, "base_url": "https://user:sk-secret@api.example.com/v1"})
    for name in ("X-Auth", "X-Custom-Auth", "X-Session-Token", "Proxy-Credential"):
        with pytest.raises(api_config.ConfigError, match="密钥"):
            api_config.normalize_provider({**base, "base_url": "https://api.example.com", "headers": {name: "s"}})
    ok = api_config.normalize_provider({**base, "base_url": "https://openrouter.ai/api/v1",
                                        "headers": {"HTTP-Referer": "https://anx", "X-Title": "anx",
                                                    "anthropic-beta": "x", "OpenAI-Organization": "org"}})
    assert len(ok["headers"]) == 4
    assert protocols.host_of("https://user:sk-secret@api.example.com:8443/v1") == "api.example.com:8443"
    assert protocols.host_of("https://api.example.com/v1") == "api.example.com"
    error = protocols.http_error(401, b"{}", protocols.host_of("https://u:sk-secret@h.example/v1"))
    assert "sk-secret" not in str(error)


async def test_ask_user_and_mail_send_go_through_the_gateway_as_the_run(hub_box, monkeypatch):
    from agent_nexus.agents.api import loop
    monkeypatch.setattr(loop, "ASK_POLL_S", 0.05)
    calls: list[tuple[str, dict]] = []
    polls = {"n": 0}

    async def gateway(op, args):
        calls.append((op, dict(args)))
        if op == "agent_handle":
            return {"workspace_id": "h-1", "address": "api-proj@pc-1", "address_display": "api-proj@笔记本"}
        if op == "agent_ask":
            return {"ask_id": "q-1", "message_id": "m-1", "state": "open"}
        if op == "ask_get":
            polls["n"] += 1
            return {"ask_id": "q-1", "state": "open"} if polls["n"] < 2 else \
                {"ask_id": "q-1", "state": "answered", "answer": {"choice": "推送"}}
        if op == "agent_mail_send":
            return {"message_id": "m-2", "deliveries": [{"to": "me", "to_display": "me"}], "unknown_recipients": []}
        return None

    hub = AgentHub(hub_box["root"], workspaces=list, gateway=gateway,
                   device=lambda: {"id": "pc-1", "name": "笔记本", "gateway": "https://gw"})
    h = hub.handlers()
    box = {**hub_box, "h": h}
    kind = await add_provider(box)
    server = hub_box["server"]
    server.script(reply("", tool_calls=[call("ask_user", {"question": "要推送吗？", "options": ["推送", "先别"]}, "q")]),
                  reply("", tool_calls=[call("mail_send", {"to": "me", "body": "推送好了"}, "m")]), reply("完成"))
    opened = await h["agent_run_open"]({"kind": kind, "workdir": str(hub_box["proj"]), "prompt": "x", "permission": "edits"})
    assert opened["handle"] == "h-1"
    await read_until(h, opened["rid"])
    offered = {t["function"]["name"] for t in server.posts()[0]["body"]["tools"]}
    assert {"ask_user", "mail_send"} <= offered
    asked = next(args for op, args in calls if op == "agent_ask")
    assert asked["workspace_id"] == "h-1" and asked["question"] == "要推送吗？" and asked["options"] == ["推送", "先别"]
    mailed = next(args for op, args in calls if op == "agent_mail_send")
    assert mailed["workspace_id"] == "h-1" and mailed["to"] == "me"
    results = [m["content"] for m in server.posts()[-1]["body"]["messages"] if m["role"] == "tool"]
    assert any("主人回答：推送" in r for r in results) and any("已发送给 me" in r for r in results)
    # readonly: ask_user only (sending mail writes)
    ro = tools.specs("readonly", gateway=True)
    assert {s["name"] for s in ro} == set(tools.READ_TOOLS) | {"ask_user"}
    assert tools.needs_approval("mail_send", "ask") and not tools.needs_approval("ask_user", "ask")
    hub.close()


def test_gateway_agent_ask_and_mail_send_ops(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from test_agents_gateway import CATALOG, connect
    from test_console_m16 import ORIGIN
    from test_remote_m19 import TOKEN_A, TOKEN_B, hub_call, network_config

    from agent_nexus.cloud import build_cloud_app

    with TestClient(build_cloud_app(network_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        a = connect(client, "pc-a", TOKEN_A, ["agents/1"], CATALOG)
        b = connect(client, "pc-b", TOKEN_B, ["agents/1"], CATALOG)
        try:
            gateway = client.app.state.gateway
            bound = hub_call(a, "agent_handle", {"device_id": "pc-a", "workdir": "/srv/work/app", "kind": "other"})["result"]
            handle, address = bound["workspace_id"], bound["address"]
            asked = hub_call(a, "agent_ask", {"workspace_id": handle, "question": "要推送吗？", "options": ["推送", "先别"],
                                              "recommended": "推送"})["result"]
            view = gateway.mailbox.ask_get(asked["ask_id"])
            assert view["from_address"] == address and view["state"] == "open" and view["recommended"] == "推送"
            assert hub_call(a, "ask_get", {"ask_id": asked["ask_id"]})["result"]["state"] == "open"
            sent = hub_call(a, "agent_mail_send", {"workspace_id": handle, "to": "me", "body": "好了"})["result"]
            assert sent["message_id"] and sent["deliveries"][0]["to"] == "me"
            assert "error" in hub_call(b, "agent_ask", {"workspace_id": handle, "question": "q"}), "not b's run"
            assert "error" in hub_call(a, "agent_mail_send", {"workspace_id": "unknown", "to": "me", "body": "x"})
            hub_call(a, "agent_release", {"workspace_id": handle})
            assert "error" in hub_call(a, "agent_ask", {"workspace_id": handle, "question": "q"}), "a closed run"
        finally:
            a.__exit__(None, None, None)
            b.__exit__(None, None, None)
