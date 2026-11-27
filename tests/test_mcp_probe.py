"""Finding a CLI's existing Agent Nexus connector (agent_nexus/agents/mcp_probe.py), account connectors included."""
from __future__ import annotations

import json
import sys
import time

import pytest

from agent_nexus.agents import adapters, mcp_probe
from agent_nexus.agents.registry import builtin_definitions

URL = "http://127.0.0.1:8399/mcp"
CLAUDE_LIST = """Checking MCP server health…

unityMCP: http://127.0.0.1:8401/mcp (HTTP) - ✓ Connected
claude.ai Agent Nexus: https://gw.example/mcp - ✓ Connected
claude.ai Gmail: https://gmail.mcp.claude.com/mcp - ! Needs authentication
"""


def env_for(home):
    return {"HOME": str(home), "USERPROFILE": str(home), "PATH": ""}


def test_claude_mcp_list_finds_the_claude_ai_connector():
    found = mcp_probe.parse_claude_list(CLAUDE_LIST, "gw.example")
    assert found == [{"name": "claude.ai Agent Nexus", "url": "https://gw.example/mcp",
                      "prefix": "mcp__claude_ai_Agent_Nexus__"}]
    # by host alone, under any name
    other = mcp_probe.parse_claude_list("claude.ai My Tools: https://gw.example/mcp - ✓ Connected", "gw.example")
    assert other[0]["prefix"] == "mcp__claude_ai_My_Tools__"
    assert mcp_probe.parse_claude_list(CLAUDE_LIST, "") [0]["name"] == "claude.ai Agent Nexus"
    assert mcp_probe.parse_claude_list("unityMCP: http://127.0.0.1:8401/mcp (HTTP)", "gw.example") == []


def test_claude_init_event_reports_account_connectors():
    seen = []
    d = builtin_definitions()["claude"]
    opts = adapters.AdapterOptions(binary="claude", workdir="/tmp",
                                   extra={"on_mcp": lambda found, version=None: seen.append((found, version))})
    adapter = adapters.ClaudeStream(d, opts)
    adapter.feed(json.dumps({"type": "system", "subtype": "init", "session_id": "s", "claude_code_version": "2.1.332",
                             "mcp_servers": [{"name": "claude.ai Agent Nexus", "status": "connected"},
                                             {"name": "unityMCP", "status": "connected"}],
                             "tools": ["Bash", "mcp__claude_ai_Agent_Nexus__device_list",
                                       "mcp__claude_ai_Agent_Nexus__use_device", "mcp__unityMCP__run",
                                       "mcp__agent-nexus__device_list", "mcp__agent-nexus__use_device"]}))
    assert seen == [([{"name": "claude.ai Agent Nexus", "prefix": "mcp__claude_ai_Agent_Nexus__"}], "2.1.332")]


FAKE_CODEX = r'''
import json, sys
mode = sys.argv[1]
if len(sys.argv) > 2 and sys.argv[2] != "app-server":
    sys.exit(2)
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg["method"] == "initialize":
        out = {"id": msg["id"], "result": {"userAgent": "codex"}}
    elif mode == "sleep":
        continue
    elif msg["method"] == "app/list" and mode == "apps":
        out = {"id": msg["id"], "result": {"data": [
            {"id": "connector_1", "name": "Gmail", "isAccessible": True},
            {"id": "connector_2", "name": "Agent Nexus", "isAccessible": True}], "nextCursor": None}}
    elif msg["method"] == "mcpServerStatus/list" and mode == "apps":
        out = {"id": msg["id"], "result": {"data": [{"name": "unityMCP", "tools": {"run": {}}}]}}
    else:
        out = {"id": msg["id"], "error": {"code": -32601, "message": "method not found"}}
    print(json.dumps(out), flush=True)
'''


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX script stands in for codex")
@pytest.mark.parametrize("mode,state", [("apps", "present"), ("old", "unknown"), ("sleep", "unknown")])
def test_codex_app_server_probe(tmp_path, mode, state):
    script = tmp_path / "codex"
    script.write_text(f"#!{sys.executable}\nimport sys\nsys.argv.insert(1, {mode!r})\n" + FAKE_CODEX)
    script.chmod(0o755)
    started = time.monotonic()
    answer = mcp_probe.probe_codex(str(script), env_for(tmp_path), None, "gw.example", timeout=2)
    assert answer["state"] == state, answer
    if state == "present":
        assert answer["connectors"][0]["name"] == "Agent Nexus"
    assert time.monotonic() - started < 5


def test_codex_scan_by_tool_signature_and_host():
    # codex-cli 0.164.0 lists account apps under codex_apps as "<app>.<tool>" (seen on a real account)
    assert mcp_probe._codex_scan({"data": [{"name": "codex_apps", "tools": {
        "my gw.device_list": {}, "my gw.use_device": {}, "my gw.toolsearch": {}, "github.search": {}}}]}, "") == ["my gw"]
    assert mcp_probe._codex_scan({"data": [{"name": "Mine", "url": "https://gw.example/mcp"}]}, "gw.example") == ["Mine"]
    assert mcp_probe._codex_scan({"data": [{"name": "Agent Nexus", "isAccessible": False}]}, "") == []


def test_cache_validity_and_manual_answers(tmp_path, monkeypatch):
    probe = mcp_probe.Probe(tmp_path)
    probe.put("user", "codex", {"state": "present", "connectors": [{"name": "Agent Nexus"}], "source": "probe",
                                "cli_version": "0.164.0"})
    assert probe.account_answer("user", "codex", version="0.164.0")["state"] == "present"
    assert probe.account_answer("user", "codex", version="0.170.0") is None  # a new CLI: ask again
    monkeypatch.setattr(mcp_probe.time, "time", lambda: 10**10)
    assert probe.account_answer("user", "codex", version="0.164.0") is None  # older than 6 h
    probe.manual("user", "codex", True)
    assert probe.account_answer("user", "codex", version="9")["source"] == "user"  # never expires
    probe.observe("user", "codex", [])  # a run's observation does not override the owner
    assert probe.get("user", "codex")["state"] == "present"


def test_resolve_states(tmp_path):
    probe = mcp_probe.Probe(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    common = dict(account="user", home=home, env=env_for(home), gateway_host="gw.example", local_url=URL, probe=probe)
    assert mcp_probe.resolve("codex", **common)["state"] == "unknown"
    assert mcp_probe.resolve("gemini", **common)["state"] == "none"
    assert mcp_probe.resolve("dsh", **common)["state"] == "unsupported"
    probe.put("user", "claude", {"state": "present", "source": "probe",
                                 "connectors": [{"name": "claude.ai Agent Nexus", "prefix": "mcp__claude_ai_Agent_Nexus__"}]})
    r = mcp_probe.resolve("claude", **common)
    assert r["state"] == "gateway_account" and r["use"] == "claude.ai Agent Nexus"
    assert r["tool_prefix"] == "mcp__claude_ai_Agent_Nexus__" and r["source"] == "account:claude.ai"
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"agent-nexus": {"type": "http", "url": URL}}}))
    both = mcp_probe.resolve("claude", **common)
    assert both["state"] == "both" and both["use"] == "agent-nexus" and both["also"] == ["claude.ai Agent Nexus"]
