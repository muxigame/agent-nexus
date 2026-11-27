"""The dedicated ``agent-nexus`` entry in the CLIs' own MCP config (agent_nexus/agents/mcp_link.py)."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tomllib
import types

import pytest

from agent_nexus.agents import mcp_link, mcp_probe

URL = "http://127.0.0.1:8399/mcp"
URL2 = "http://127.0.0.1:8500/mcp"
GATEWAY = "https://gw.example/mcp"

CODEX_TOML = ("﻿# my codex config\r\n"
              "model = \"gpt-5\"  # inline comment\r\n"
              "\r\n"
              "[mcp_servers.unityMCP]\r\n"
              "url = \"http://127.0.0.1:8401/mcp\"\r\n"
              "\r\n"
              "[mcp_servers.node_repl]\r\n"
              "command = \"node\"\r\n"
              "args = [\"repl.js\"]\r\n")


def env_for(home):
    return {"HOME": str(home), "USERPROFILE": str(home), "PATH": ""}


def test_codex_append_and_remove_are_byte_exact_and_keep_everything_else(tmp_path):
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_bytes(CODEX_TOML.encode("utf-8"))
    original = path.read_bytes()
    result = mcp_link.write("codex", URL, home=tmp_path, env=env_for(tmp_path), now=1_700_000_000)
    assert result["ok"] and result["changed"] and result["backup"]
    assert open(result["backup"], "rb").read() == original
    data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    assert data["mcp_servers"]["agent-nexus"] == {"url": URL, "tool_timeout_sec": 900}
    assert data["mcp_servers"]["unityMCP"] == {"url": "http://127.0.0.1:8401/mcp"}
    assert data["model"] == "gpt-5" and path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert b"\r\n[mcp_servers.agent-nexus]\r\n" in path.read_bytes()
    # idempotent: no write, no backup
    again = mcp_link.write("codex", URL, home=tmp_path, env=env_for(tmp_path))
    assert again["ok"] and not again["changed"] and again["backup"] is None
    # a port change rewrites the URL
    moved = mcp_link.write("codex", URL2, home=tmp_path, env=env_for(tmp_path), now=1_700_000_100)
    assert moved["changed"] and tomllib.loads(path.read_text(encoding="utf-8-sig"))["mcp_servers"]["agent-nexus"]["url"] == URL2
    removed = mcp_link.write("codex", URL2, remove=True, home=tmp_path, env=env_for(tmp_path), now=1_700_000_200)
    assert removed["changed"] and path.read_bytes() == original


def test_codex_without_a_config_file(tmp_path):
    result = mcp_link.write("codex", URL, home=tmp_path, env=env_for(tmp_path))
    path = tmp_path / ".codex" / "config.toml"
    assert result["ok"] and result["backup"] is None
    assert tomllib.loads(path.read_text())["mcp_servers"]["agent-nexus"]["url"] == URL


def test_gemini_and_claude_json_edits_keep_the_rest(tmp_path):
    settings = tmp_path / ".gemini" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{\n    "theme": "dark",\n    "mcpServers": {\n        "x": {"command": "x"}\n    }\n}\n')
    before = settings.read_text()
    assert mcp_link.write("gemini", URL, home=tmp_path, env=env_for(tmp_path))["changed"]
    data = json.loads(settings.read_text())
    assert data["mcpServers"]["agent-nexus"] == {"httpUrl": URL, "timeout": 900000} and data["theme"] == "dark"
    assert mcp_link.write("gemini", URL, remove=True, home=tmp_path, env=env_for(tmp_path))["changed"]
    assert settings.read_text() == before

    claude = tmp_path / ".claude.json"
    claude.write_text('{"numStartups": 3, "projects": {}}')
    os.chmod(claude, 0o600)
    result = mcp_link.write("claude", URL, home=tmp_path, env=env_for(tmp_path), use_cli=False)
    assert result["ok"] and result["method"] == "edit"
    data = json.loads(claude.read_text())
    assert data["mcpServers"]["agent-nexus"] == {"type": "http", "url": URL} and data["numStartups"] == 3
    if sys.platform != "win32":
        assert stat.S_IMODE(claude.stat().st_mode) == 0o600
    mcp_link.write("claude", URL, remove=True, home=tmp_path, env=env_for(tmp_path), use_cli=False, drop_empty=True)
    assert json.loads(claude.read_text()) == {"numStartups": 3, "projects": {}}


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX shell script stands in for claude")
def test_claude_goes_through_its_own_command_when_it_can(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "argv.json"
    fake = bindir / "claude"
    fake.write_text(f"""#!{sys.executable}
import json, os, sys
open({str(log)!r}, "a").write(json.dumps(sys.argv[1:]) + "\\n")
path = os.path.join(os.environ["HOME"], ".claude.json")
data = json.load(open(path)) if os.path.exists(path) else {{}}
servers = data.setdefault("mcpServers", {{}})
if sys.argv[2] == "add-json":
    servers[sys.argv[5]] = json.loads(sys.argv[6])
else:
    servers.pop(sys.argv[5], None)
json.dump(data, open(path, "w"))
""")
    fake.chmod(0o755)
    env = {**env_for(tmp_path), "PATH": str(bindir)}
    (tmp_path / ".claude.json").write_text("{}")
    result = mcp_link.write("claude", URL, home=tmp_path, env=env)
    assert result["ok"] and result["method"] == "cli" and result["backup"]
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[0] == ["mcp", "add-json", "--scope", "user", "agent-nexus", json.dumps({"type": "http", "url": URL})]
    assert mcp_link.write("claude", URL, remove=True, home=tmp_path, env=env)["method"] == "cli"
    assert json.loads(log.read_text().splitlines()[-1]) == ["mcp", "remove", "--scope", "user", "agent-nexus"]


def test_the_writer_runs_as_the_cli_user(tmp_path):
    seen = {}

    def runner(argv, env, *, run_as, timeout):
        seen.update(argv=argv, env=env, run_as=run_as)
        return 0, json.dumps({"ok": True, "changed": True, "path": "/home/user/.codex/config.toml",
                              "backup": None, "method": "edit", "error": None}) + "\n", ""
    account = types.SimpleNamespace(name="user", env=lambda: {"HOME": "/home/user"},
                                    popen_args=lambda: {"user": 1000, "group": 1000})
    result = mcp_link.write_as(account, "codex", URL, runner=runner)
    assert result["ok"] and seen["run_as"] == {"user": 1000, "group": 1000} and seen["env"]["HOME"] == "/home/user"
    assert seen["argv"][1:] == ["-I", "-m", "agent_nexus.cli", "mcp", "link", "--kind", "codex", "--url", URL, "--json"]


# -- the decision table ------------------------------------------------------------------------------------------

class FakeLocal:
    def __init__(self, state="ready", auto=True, url=URL):
        self._state, self.url = state, url
        self.settings = {"enabled": True, "auto_config": auto, "peers": "any"}

    def state(self):
        return self._state

    def status(self):
        return {"state": self._state, "reason": "r"}


def link(tmp_path, local=None, kinds=("codex", "gemini", "claude")):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = env_for(home)
    defs = {kind: types.SimpleNamespace(kind=kind) for kind in kinds}
    writes = []

    def writer(account, kind, url, *, remove=False, drop_empty=False):
        writes.append((kind, "remove" if remove else "add", url))
        return mcp_link.write(kind, url, remove=remove, home=home, env=env, drop_empty=drop_empty, use_cli=False)
    lk = mcp_link.MCPLink(tmp_path, local_mcp=local or FakeLocal(), accounts=lambda: [None],
                          definitions=lambda: defs, gateway_host=lambda: "gw.example", writer=writer,
                          locate=lambda d, e: "/usr/bin/" + d.kind)
    lk.env_of = lambda account: env
    lk.home_of = lambda account, e: home
    lk.account_name = lambda account: "user"
    return lk, home, writes


def run(coro):
    return asyncio.run(coro)


def test_reconcile_adds_where_nothing_reaches_the_gateway_and_never_duplicates(tmp_path):
    lk, home, writes = link(tmp_path, kinds=("codex", "gemini"))
    # codex: account answer known absent; gemini has no account level
    lk.probe.put("user", "codex", {"state": "absent", "connectors": [], "source": "probe"})
    done = run(lk.reconcile("test"))
    assert {(d["kind"], d["action"]) for d in done} == {("codex", "add"), ("gemini", "add")}
    assert lk.view("codex", None)["state"] == "local" and lk.view("codex", None)["use"] == "agent-nexus"
    assert run(lk.reconcile("again")) == []  # idempotent
    # the user removes ours by hand: never added back
    mcp_link.write("gemini", URL, remove=True, home=home, env=env_for(home))
    run(lk.reconcile("after"))
    assert lk.owned.get("user", "gemini")["state"] == "user_removed"
    assert lk.view("gemini", None)["label"] == "已被手动移除"
    assert run(lk.reconcile("again")) == []


def test_reconcile_removes_ours_when_an_account_connector_appears_and_skips_unknown(tmp_path):
    lk, home, writes = link(tmp_path, kinds=("codex",))
    lk.probe.put("user", "codex", {"state": "absent", "connectors": [], "source": "probe"})
    run(lk.reconcile("t"))
    assert lk.view("codex", None)["state"] == "local"
    lk.probe.observe("user", "codex", [{"name": "Agent Nexus", "prefix": ""}])
    done = run(lk.reconcile("t"))
    assert done and done[0]["action"] == "remove"
    view = lk.view("codex", None)
    assert view["state"] == "gateway_account" and "ChatGPT 账户连接器" in view["label"]
    assert "agent-nexus" not in (home / ".codex" / "config.toml").read_text()


def test_unknown_account_state_adds_nothing(tmp_path, monkeypatch):
    lk, home, writes = link(tmp_path, kinds=("codex",))

    async def nothing(*a, **k):
        return {"state": "unknown"}
    monkeypatch.setattr(lk, "probe_account", nothing)
    assert run(lk.reconcile("t")) == [] and lk.view("codex", None)["state"] == "unknown"


def test_a_gateway_entry_in_the_config_is_left_alone_and_nothing_is_added(tmp_path):
    lk, home, writes = link(tmp_path, kinds=("gemini",))
    settings = home / ".gemini" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"mcpServers": {"agent-nexus": {"httpUrl": GATEWAY}}}))
    assert run(lk.reconcile("t")) == []
    view = lk.view("gemini", None)
    assert view["state"] == "gateway_config" and view["use"] == "agent-nexus"
    # 添加 needs force, then the entry is pinned... but the name is the user's: never overwritten
    refused = run(lk.action("gemini", None, "add"))
    assert refused["status"] == 409 and refused["code"] == "duplicate"


def test_a_foreign_agent_nexus_entry_is_name_taken(tmp_path):
    lk, home, writes = link(tmp_path, kinds=("gemini",))
    settings = home / ".gemini" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"mcpServers": {"agent-nexus": {"command": "something-else"}}}))
    assert run(lk.reconcile("t")) == []
    assert lk.view("gemini", None)["state"] == "name_taken"
    nx = lk.for_run("gemini", None)
    assert nx["attach"] is None and nx["use"] == "agent-nexus"  # never a second connector next to the user's


def test_off_removes_ours_and_gateway_old_keeps_it(tmp_path):
    local = FakeLocal()
    lk, home, writes = link(tmp_path, local=local, kinds=("gemini",))
    run(lk.reconcile("t"))
    assert lk.view("gemini", None)["state"] == "local"
    local._state = "gateway_old"
    assert run(lk.reconcile("t")) == []
    local._state = "off"
    assert run(lk.reconcile("t"))[0]["action"] == "remove"
    assert lk.view("gemini", None)["state"] == "none"


def test_a_port_change_rewrites_our_url(tmp_path):
    local = FakeLocal()
    lk, home, writes = link(tmp_path, local=local, kinds=("gemini",))
    run(lk.reconcile("t"))
    local.url = URL2
    done = run(lk.reconcile("t"))
    assert done[0]["action"] == "update"
    assert json.loads((home / ".gemini" / "settings.json").read_text())["mcpServers"]["agent-nexus"]["httpUrl"] == URL2


def test_pinned_stays_next_to_a_gateway_connector(tmp_path):
    lk, home, writes = link(tmp_path, kinds=("codex",))
    lk.probe.manual("user", "codex", True)
    assert lk.view("codex", None)["state"] == "gateway_account"
    added = run(lk.action("codex", None, "add", force=True))
    assert added["cli"]["state"] == "both" and added["cli"]["use"] == "agent-nexus"
    assert lk.owned.get("user", "codex")["state"] == "pinned"
    assert run(lk.reconcile("t")) == []  # pinned: not removed
    removed = run(lk.action("codex", None, "remove"))
    assert removed["result"]["ok"] and lk.owned.get("user", "codex")["state"] == "user_removed"


def test_for_run_attach_modes(tmp_path):
    lk, home, writes = link(tmp_path, kinds=("gemini", "codex"))
    assert lk.for_run("gemini", None)["attach"] == "flags"  # none yet: per-run flags
    run(lk.reconcile("t"))
    nx = lk.for_run("gemini", None)
    assert nx["attach"] is None and nx["state"] == "local" and nx["ready"]
    assert lk.for_run("dsh", None)["attach"] == "flags"
    assert lk.for_run("api:x/y", None, api=True)["attach"] == "inprocess"
    lk.local._state = "off"
    assert lk.for_run("dsh", None)["attach"] is None


def test_nexus_entries_classify(tmp_path):
    home = tmp_path
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.unityMCP]\nurl = "http://127.0.0.1:8401/mcp"\n'
        '[mcp_servers.mine]\nurl = "http://localhost:8399/mcp"\n'
        '[mcp_servers.gw]\nurl = "https://gw.example/mcp"\n')
    found = mcp_probe.nexus_entries("codex", home, env_for(home), "gw.example", URL)
    assert found == {"local": ["mine"], "config": ["gw"], "taken": False}


# -- the owner's work PC (codex-cli 0.164.0, ChatGPT sign-in with the gateway as an account connector) -----------

WORK_PC_CODEX = r'''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg["method"] == "initialize":
        out = {"id": msg["id"], "result": {"userAgent": "codex_cli_rs/0.164.0"}}
    elif msg["method"] == "app/list":
        out = {"id": msg["id"], "result": {"data": [
            {"id": "connector_68", "name": "My Gateway", "isAccessible": True}], "nextCursor": None}}
    elif msg["method"] == "mcpServerStatus/list":
        out = {"id": msg["id"], "result": {"data": [
            # as codex-cli 0.164.0 answers on the work PC: account apps under codex_apps as "<app>.<tool>"; the
            # gateway app has whatever name its owner gave it, and an older registration has the legacy names
            {"name": "codex_app", "tools": {}},
            {"name": "codex_apps", "tools": {"github.search": {}, "gmail.read_email_thread": {},
                                             "My Gateway.workspace_list": {}, "My Gateway.set_workspace": {},
                                             "My Gateway.mcplist": {}, "My Gateway.exec_command": {},
                                             "files.device_list": {}, "files.exec_command": {}}},
            {"name": "cua_repl", "tools": {"run": {}}},
            {"name": "node_repl", "tools": {"eval": {}}},
            {"name": "unityMCP", "tools": {"manage_scene": {}}}]}}
    else:
        out = {"id": msg["id"], "error": {"code": -32601, "message": "method not found"}}
    print(json.dumps(out), flush=True)
'''


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX script stands in for codex")
def test_the_work_pc_codex_account_connector_is_found_and_nothing_is_added(tmp_path):
    lk, home, writes = link(tmp_path, kinds=("codex",))
    config = home / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_bytes(CODEX_TOML.encode("utf-8"))  # only unityMCP and node_repl, as on the work PC
    script = tmp_path / "codex"
    script.write_text(f"#!{sys.executable}\n" + WORK_PC_CODEX)
    script.chmod(0o755)
    answer = mcp_probe.probe_codex(str(script), env_for(tmp_path), None, "gw.example", timeout=5)
    assert answer["state"] == "present", answer
    lk.probe.put("user", "codex", {**answer, "source": "probe"})
    assert run(lk.reconcile("work pc")) == [] and writes == []
    assert config.read_bytes() == CODEX_TOML.encode("utf-8")  # untouched, no duplicate tools
    view = lk.view("codex", None)
    assert view["state"] == "gateway_account" and view["use"] and view["tool_prefix"] == "My Gateway." and view["use"] == "My Gateway"


def test_gateway_connectors_are_known_by_their_tools_not_their_name_or_address():
    names = ["workspace.workspace_list", "workspace.set_workspace", "workspace.toolsearch",   # legacy registration
             "anything.device_list", "anything.use_device", "anything.mail_send",            # any name, new tools
             "mcp__gw__device_list", "mcp__gw__use_device", "mcp__gw__tooluse",               # claude style
             "device.exec_command", "device.device_list",                                     # a device plugin: no
             "lookalike.device_list", "lookalike.use_device",                                 # no federation tool: no
             "github.search"]
    assert sorted(mcp_probe.nexus_groups(names)) == ["anything", "gw", "workspace"]
