"""Review fixes of the 本机 MCP: the pass-through (encoding, disconnects, stateful sessions), the peer check, the
link's scheduling / accounts / dedup / version gate, safe config edits, run attachment and API runs."""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import stat
import sys
import types

import httpx
import pytest
from fastmcp import Client, FastMCP

from agent_nexus import integrate
from agent_nexus.agents import adapters, mcp_link, mcp_probe, registry
from agent_nexus.agents.api.loop import ApiAdapter
from agent_nexus.agents.hub import approved_tools, can_attach, connector_name, mcp_hint

from test_local_mcp import INIT, PORT, call, make
from test_mcp_link import URL, FakeLocal, env_for, link

SSE = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'


def run(coro):
    return asyncio.run(coro)


# -- the pass-through ------------------------------------------------------------------------------------------

def test_a_compressed_gateway_answer_reaches_the_cli_decoded(tmp_path):
    def gzipped(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream", "content-encoding": "gzip"},
                              stream=httpx.ByteStream(gzip.compress(SSE)))
    mcp, seen = make(tmp_path, handler=gzipped)
    got = call(mcp)
    assert got.content == SSE and "content-encoding" not in got.headers
    assert seen[-1].headers["accept-encoding"] == "identity"  # and a proxy is asked not to compress at all


def test_a_cli_that_goes_away_stops_the_gateway_stream(tmp_path):
    pulled = []

    async def slow():
        for n in range(5):
            pulled.append(n)
            yield SSE
            await asyncio.sleep(0.05)

    mcp, _ = make(tmp_path, handler=lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=slow()))
    scope = {"type": "http", "method": "POST", "path": "/mcp", "client": ("127.0.0.1", 50123),
             "server": ("127.0.0.1", PORT),
             "headers": [(b"host", f"127.0.0.1:{PORT}".encode()), (b"content-type", b"application/json"),
                         (b"accept", b"application/json, text/event-stream")]}
    body = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "mail_wait", "arguments": {}}}).encode()
    inbox = [{"type": "http.request", "body": body, "more_body": False}, {"type": "http.disconnect"}]
    sent = []

    async def receive():
        if inbox:
            return inbox.pop(0)
        await asyncio.sleep(3600)

    async def send(message):
        sent.append(message)

    run(mcp(scope, receive, send))
    assert len(pulled) < 5
    line = json.loads((tmp_path / ".anx" / "logs" / "local-mcp.jsonl").read_text().strip())
    assert line["outcome"] == "client_gone"


def test_an_expired_stateful_session_404_is_passed_on(tmp_path):
    mcp, _ = make(tmp_path, handler=lambda request: httpx.Response(
        404, json={"jsonrpc": "2.0", "id": "server-error", "error": {"code": -32600, "message": "Session not found"}}))
    listed = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    got = call(mcp, body=listed, headers={"Mcp-Session-Id": "gone"})
    assert got.status_code == 404  # the MCP client starts a new session
    assert "网关版本过旧" not in got.text
    # without a session id a 404 still means the gateway has no /mcp/device
    assert "网关版本过旧" in call(mcp).json()["result"]["instructions"]


# -- the peer check (mcp_local.peers = accounts) ---------------------------------------------------------------

def conn(lip, lport, rip, rport, pid, status="ESTABLISHED"):
    return types.SimpleNamespace(laddr=types.SimpleNamespace(ip=lip, port=lport),
                                 raddr=types.SimpleNamespace(ip=rip, port=rport), pid=pid, status=status)


def test_the_peer_is_matched_on_the_full_connection_and_fails_closed(tmp_path, monkeypatch):
    mcp, _ = make(tmp_path)
    scope = {"client": ("127.0.0.1", 40625), "server": ("127.0.0.1", PORT)}
    table = [conn("127.0.0.1", 40625, "127.0.0.1", PORT, 111),       # the victim
             conn("127.0.0.2", 40625, "127.0.0.1", PORT, 222),       # the same source port on another 127.x
             conn("127.0.0.1", PORT, "127.0.0.1", 40625, os.getpid())]  # our own side
    monkeypatch.setattr(mcp, "_connections", lambda: table)
    assert mcp.peer(scope)["pid"] == 111
    # two candidates for the same 4-tuple (a stale entry): unknown, never a guess
    table.append(conn("127.0.0.1", 40625, "127.0.0.1", PORT, 333))
    assert mcp.peer(scope) == {}
    # a closed connection does not count; a new one is found at once (no cache)
    table[:] = [conn("127.0.0.1", 40625, "127.0.0.1", PORT, 111, status="TIME_WAIT")]
    assert mcp.peer(scope) == {}
    table.append(conn("127.0.0.1", 40625, "127.0.0.1", PORT, 444))
    assert mcp.peer(scope)["pid"] == 444


# -- MCPLink: scheduling, accounts, dedup ------------------------------------------------------------------------

def test_a_change_during_a_reconcile_is_not_lost(tmp_path):
    lk, _home, _writes = link(tmp_path)
    reasons = []

    async def reconcile(reason=""):
        reasons.append(reason)
        await asyncio.sleep(0.3)
        return []
    lk.reconcile = reconcile

    async def go():
        lk.soon("gateway connected")
        await asyncio.sleep(1.1)  # the first reconcile is running now
        lk.soon("settings")
        await asyncio.sleep(2.0)
    run(go())
    assert reasons == ["gateway connected", "settings"]


def test_the_windows_service_never_writes_or_probes_for_nobody(tmp_path):
    lk, home, writes = link(tmp_path, kinds=("gemini", "codex"))
    lk.system = lambda: True
    probed = []

    async def probe(*a, **k):
        probed.append(a)
        return {"state": "absent"}
    lk.probe_account = probe
    assert run(lk.reconcile("t")) == [] and writes == [] and probed == []
    assert run(lk.status()) == []
    refused = run(lk.action("gemini", None, "add"))
    assert refused["status"] == 409 and refused["code"] == "no_account"


def test_an_entry_of_another_claude_project_is_not_user_wide(tmp_path):
    lk, home, writes = link(tmp_path, kinds=("claude",))
    lk.probe.put("user", "claude", {"state": "absent", "connectors": [], "source": "probe"})
    (home / ".claude.json").write_text(json.dumps({"projects": {"/other/proj": {"mcpServers": {
        "gw": {"type": "http", "url": "https://gw.example/mcp"}}}}}))
    assert lk.view("claude", None)["state"] == "none"
    done = run(lk.reconcile("t"))
    assert [d["action"] for d in done] == ["add"]
    assert lk.for_run("claude", None, workdir="/somewhere/else")["use"] == "agent-nexus"
    # a run in that very project has the gateway entry: it is told to use it
    nx = lk.for_run("claude", None, workdir="/other/proj")
    assert nx["state"] == "both" and nx["use"] == "agent-nexus" and nx["also"] == ["gw"]
    # .mcp.json of the run's folder counts too
    proj = tmp_path / "p2"
    proj.mkdir()
    (proj / ".mcp.json").write_text(json.dumps({"mcpServers": {"anx": {"type": "http", "url": "https://gw.example/mcp"}}}))
    assert "anx" in lk.for_run("claude", None, workdir=str(proj))["also"]


def test_a_gateway_entry_named_agent_nexus_on_another_host_is_the_gateway(tmp_path):
    lk, home, writes = link(tmp_path, kinds=("gemini",))
    settings = home / ".gemini" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"mcpServers": {"agent-nexus": {
        "httpUrl": "https://203.0.113.7/mcp", "headers": {"Authorization": "Bearer x"}}}}))
    view = lk.view("gemini", None)
    assert view["state"] == "gateway_config" and view["use"] == "agent-nexus"
    assert run(lk.reconcile("t")) == []
    nx = lk.for_run("gemini", None)
    assert nx["attach"] is None and nx["use"] == "agent-nexus"


@pytest.mark.parametrize("state", ["not_joined", "revoked", "gateway_old", "device_access_off"])
def test_nothing_is_added_or_attached_while_the_device_cannot_use_the_gateway(tmp_path, state):
    lk, home, writes = link(tmp_path, local=FakeLocal(state), kinds=("gemini",))
    assert run(lk.reconcile("t")) == [] and writes == []
    assert lk.for_run("gemini", None)["attach"] is None


def test_an_old_codex_gets_no_entry_and_no_flags(tmp_path):
    lk, home, writes = link(tmp_path, kinds=("codex",))
    lk.version_of = lambda binary, env, run_as: "0.30.0"
    lk.probe.put("user", "codex", {"state": "absent", "connectors": [], "source": "probe"})
    assert run(lk.reconcile("t")) == [] and writes == []
    view = lk.view("codex", None)
    assert view["state"] == "unsupported" and "版本过旧" in view["label"]
    assert lk.for_run("codex", None)["attach"] is None
    lk.note_version(None, "codex", "0.164.0")
    assert [d["action"] for d in run(lk.reconcile("t"))] == ["add"]


def test_both_with_a_local_mcp_that_cannot_serve_uses_the_other_connector(tmp_path):
    lk, home, writes = link(tmp_path, kinds=("codex",))
    lk.probe.manual("user", "codex", True)
    run(lk.action("codex", None, "add", force=True))  # pinned next to the account connector
    lk.local._state = "device_access_off"
    nx = lk.for_run("codex", None)
    assert nx["state"] == "both" and nx["use"] == "Agent Nexus" and "agent-nexus" in nx["also"]
    hint = mcp_hint(nx, {"handle": "h", "target_device": "pc", "target_workdir": "/w", "address": "a@pc"})
    assert "连接器 Agent Nexus" in hint and "现在不可用" not in hint


def test_unlink_for_the_uninstaller_removes_ours_and_adds_none_again(tmp_path):
    lk, home, writes = link(tmp_path, kinds=("gemini",))
    run(lk.reconcile("t"))
    assert lk.view("gemini", None)["state"] == "local"
    done = run(lk.unlink_all(suspend=True))
    assert done and done[0]["ok"] and lk.view("gemini", None)["state"] == "none"
    assert run(lk.reconcile("t")) == []


def test_offline_unlink_uses_the_recorded_file_and_only_this_users_entries(tmp_path):
    other_home = tmp_path / "other"
    path = other_home / ".gemini" / "settings.json"
    assert mcp_link.write("gemini", URL, home=other_home, env=env_for(other_home))["ok"]
    owned = mcp_link.Ownership(tmp_path)
    owned.set("me", "gemini", {"state": "ours", "url": URL, "path": str(path), "created": True})
    owned.set("someone", "codex", {"state": "ours", "url": URL, "path": str(tmp_path / "x.toml")})
    results = mcp_link.unlink_offline(tmp_path, me="DOMAIN\\Me")
    by_kind = {r["kind"]: r for r in results}
    assert by_kind["gemini"]["changed"] and mcp_link.current("gemini", path) is None
    assert by_kind["codex"].get("skipped") and not by_kind["codex"]["changed"]
    assert owned.get("me", "gemini") is None and owned.get("someone", "codex") is not None


# -- safe config edits ----------------------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_a_private_config_is_never_readable_by_others_while_it_is_edited(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[mcp_servers.gw]\nurl = "https://gw.example/mcp"\nhttp_headers = { Authorization = "Bearer SECRET" }\n')
    os.chmod(config, 0o600)
    seen = []
    real = os.replace

    def watch(src, dst):
        seen.append((stat.S_IMODE(os.stat(src).st_mode), src))
        return real(src, dst)
    monkeypatch.setattr(os, "replace", watch)
    old = os.umask(0o022)
    try:
        result = mcp_link.write("codex", URL, home=home, env=env_for(home))
    finally:
        os.umask(old)
    assert result["ok"] and result["changed"]
    assert seen and all(mode == 0o600 for mode, _ in seen)
    assert stat.S_IMODE(os.stat(config).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(result["backup"]).st_mode) == 0o600


def test_a_write_made_meanwhile_is_never_overwritten_and_backups_are_pruned(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"a": 1}')
    planned = mcp_link.plan("gemini", path, URL, remove=False)
    mtime = path.stat().st_mtime
    os.utime(path, (mtime + 5, mtime + 5))  # Claude Code wrote it after we read it
    with pytest.raises(InterruptedError):
        integrate.apply(planned, expect_mtime=mtime)
    assert json.loads(path.read_text()) == {"a": 1}
    assert not list(tmp_path.glob("*.anx-tmp*")) and not list(tmp_path.glob("*.anx-backup*"))
    home = tmp_path / "h"
    for n in range(5):
        url = f"http://127.0.0.1:{8000 + n}/mcp"
        assert mcp_link.write("gemini", url, home=home, env=env_for(home), now=1_700_000_000 + n)["changed"]
    backups = list((home / ".gemini").glob("settings.json.anx-backup-*"))
    assert len(backups) == mcp_link.KEEP_BACKUPS


# -- what a run is told / given ---------------------------------------------------------------------------------

def test_codex_and_claude_approvals_follow_the_permission_mode():
    assert approved_tools("full") is None
    for mode in ("readonly", "ask", "edits"):
        tools = approved_tools(mode)
        assert not {"exec_command", "write_stdin", "keyboard", "mouse", "tooluse", "use_device", "file_copy"} & set(tools)
    assert "file_write" in approved_tools("edits", "gateway") and "file_write" not in approved_tools("edits", "direct")
    argv = adapters._codex_mcp_approve(["codex", "exec", "x"], ["agent-nexus"], approved_tools("ask"))
    enabled = json.loads(next(a for a in argv if a.startswith("mcp_servers.agent-nexus.enabled_tools=")).split("=", 1)[1])
    assert "exec_command" not in enabled and "mail_send" in enabled
    full = adapters._codex_mcp_approve(["codex", "exec", "x"], ["agent-nexus"], None)
    assert not any("enabled_tools" in a for a in full)


def test_text_clis_without_the_placeholder_and_acp_agents_are_not_promised_a_connector(tmp_path):
    lk, home, writes = link(tmp_path, kinds=("gemini",))
    plain = registry.AgentDef(kind="mine", label="Mine", binaries=("mine",), adapter="text", command=["{bin}", "{prompt}"])
    nx = lk.for_run("mine", None, d=plain)
    assert nx["attach"] is None and can_attach(nx) is False and connector_name(nx) == ""
    with_url = registry.AgentDef(kind="mine", label="Mine", binaries=("mine",), adapter="text",
                                 command=["{bin}", "--mcp", "{mcp_url}", "{prompt}"])
    assert lk.for_run("mine", None, d=with_url)["attach"] == "flags"
    dsh = registry.builtin_definitions()["dsh"]
    nx = lk.for_run("dsh", None, d=dsh)
    assert nx["attach"] == "flags" and nx["acp_pending"]
    hint = mcp_hint(nx, {"handle": "h", "target_device": "pc", "target_workdir": "/w", "address": "a@pc"})
    assert "尝试接入" in hint and "本次对话已接入" not in hint
    # an ACP agent without HTTP MCP, gateway mode: the warning says the gateway tools are out of reach
    adapter = adapters.Acp(dsh, adapters.AdapterOptions(binary="dsh", workdir="/w", extra={
        "mcp_attach": {"name": "agent-nexus", "url": URL, "gateway": True}}))
    adapter.start()
    adapter.outbox.clear()
    events = adapter.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"agentCapabilities": {}}}))
    events += adapter.feed(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s"}}))
    assert any("无法接入 Agent Nexus MCP" in e.get("text", "") for e in events)


@pytest.mark.parametrize("local_state", ["device_access_off", "rejected"])
def test_a_local_mcp_that_cannot_serve_is_warned_about(local_state):
    nx = {"state": "local", "use": "agent-nexus", "attach": None, "local_state": local_state}
    assert can_attach(nx) is False


def test_the_claude_init_observer_does_not_record_absent_for_a_failed_connector():
    seen = []
    observe = lambda found, version=None: seen.append(found)  # noqa: E731
    adapters._observe_claude_init({"mcp_servers": [{"name": "claude.ai Agent Nexus", "status": "failed"}],
                                   "tools": ["Bash"]}, observe, None)
    assert seen == [[{"name": "claude.ai Agent Nexus", "prefix": "mcp__claude_ai_Agent_Nexus__"}]]
    seen.clear()
    adapters._observe_claude_init({"mcp_servers": [{"name": "claude.ai Gmail", "status": "pending"}],
                                   "tools": ["Bash"]}, observe, None)
    assert seen == []  # it may be the Agent Nexus one: the cached answer stays
    adapters._observe_claude_init({"mcp_servers": [{"name": "claude.ai Gmail", "status": "connected"}],
                                   "tools": ["Bash"]}, observe, None)
    assert seen == [[]]


# -- API models --------------------------------------------------------------------------------------------------

def gateway_like(full: bool) -> FastMCP:
    server = FastMCP("gw")

    @server.tool(annotations={"readOnlyHint": True})
    def nexus_status() -> str:
        return "state: offline"

    if full:
        @server.tool(annotations={"readOnlyHint": True})
        def file_read(path: str, workspace_id: str) -> str:
            return f"content of {path}"
    return server


def api_run(factory, mode="gateway"):
    d = registry.AgentDef(kind="api:p/m", label="M", binaries=(), adapter="api")
    spec = {"provider": {"id": "p"}, "model": {"id": "m"}, "handle": "h-42", "gateway": object(), "mode": mode,
            "mcp_url": URL, "mcp_client": factory}
    adapter = ApiAdapter(d, adapters.AdapterOptions(binary="api", workdir="/w", permission="full", extra={"api": spec}))
    adapter._runs = types.SimpleNamespace(_push_all=lambda run, events: None)
    adapter._run = types.SimpleNamespace(closed=False, token="t")
    return adapter


def test_an_api_run_does_not_keep_a_degraded_tool_list():
    servers = {"now": gateway_like(False)}

    async def go():
        adapter = api_run(lambda url: Client(servers["now"]))
        await adapter._load_mcp()
        assert adapter._mcp_specs is None  # not cached: the hand-rolled tools stay
        assert {"ask_user", "mail_send"} & {s["name"] for s in adapter._offered()}
        servers["now"] = gateway_like(True)
        await adapter._load_mcp()
        assert adapter._mcp_specs is None  # not before the retry time
        adapter._mcp_retry_at = 0
        await adapter._mcp_drop()  # a new client for the new (fake) server
        await adapter._load_mcp()
        assert {s["name"] for s in adapter._offered()} == {"file_read"}
        await adapter._mcp_drop()
    run(go())


def test_an_api_run_keeps_one_mcp_session():
    made = []
    server = gateway_like(True)

    def factory(url):
        made.append(url)
        return Client(server)

    async def go():
        adapter = api_run(factory)
        await adapter._load_mcp()
        for n in range(3):
            result = await adapter._call_tool({"id": f"c{n}", "name": "file_read", "args": {"path": f"/a{n}"}})
            assert result["output"] == f"content of /a{n}"
        await adapter._mcp_drop()
    run(go())
    assert len(made) == 1
