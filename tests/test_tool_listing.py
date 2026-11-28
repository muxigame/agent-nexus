"""0.8.35: a lean tools/list, toolsearch/tooluse in two steps, plain exec results, the wait cap,
progress while waiting, and the default device."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from agent_nexus import gateway as gateway_mod
from agent_nexus.federation import Hub, Peer, RoutingError
from agent_nexus.gateway import (DEFERRED_CAPABILITIES, HIDDEN_GATEWAY_TOOLS, LEGACY_TOOL_NAMES, Gateway,
                                 compact_schema, exec_text)

FIXTURE = Path(__file__).parent / "fixtures" / "device_tools_0834_windows.json"
#: tools/list of the 0.8.34 gateway with this device (60 tools, compact JSON): the size before the cut.
BASELINE_CHARS = 57_953


class FakePeer(Peer):
    """A connected device whose tool calls are answered by ``answer(tool, arguments)``."""

    def __init__(self, agent_id: str, tools: list[dict], answer=None, *, hostname: str | None = None,
                 delay: float = 0.0):
        catalog = {"workspaces": {"default": {"mcp": "device", "path": f"/home/{agent_id}"}},
                   "mcps": {"device": {"description": "device", "tools": tools}}}
        if hostname:
            capabilities = {(tool.get("_meta") or {}).get("anx/capability") for tool in tools} - {None}
            catalog["device"] = {"contract": "anx.device/1", "hostname": hostname, "platform": "linux",
                                 "capabilities": {name: {"v": 1} for name in capabilities}, "shells": ["bash", "sh"]}
        super().__init__(agent_id, None, catalog)
        self.answer = answer or (lambda tool, arguments: {"tool": tool, "machine": agent_id})
        self.calls: list[dict] = []
        self.delay = delay

    async def call(self, method: str, params: dict) -> Any:
        self.calls.append(params)
        if self.delay:
            await asyncio.sleep(self.delay)
        data = self.answer(params["tool"], params["arguments"])
        return {"content": [{"type": "text", "text": json.dumps(data)}], "structuredContent": data,
                "isError": False}


def device_tools() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["tools"]


def hub_with(*peers: Peer) -> Hub:
    hub = Hub()
    for peer in peers:
        hub.peers[peer.agent_id] = peer
    return hub


def compact(hub: Hub, **kwargs) -> Gateway:
    return Gateway(hub, tool_listing="compact", legacy_tools="hidden", **kwargs)


async def listed(gateway: Gateway) -> list[dict]:
    async with Client(gateway.build()) as client:
        tools = await client.list_tools()
    return [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]


def size(tools: list[dict]) -> int:
    return len(json.dumps({"tools": tools}, separators=(",", ":"), ensure_ascii=False))


# -- tools/list ------------------------------------------------------------------------------
async def test_tools_list_is_less_than_half_of_the_0834_size_and_full_mode_keeps_everything():
    full = await listed(Gateway(hub_with(FakePeer("pc", device_tools()))))
    # full: every tool listed (60, plus the exec_wait the gateway adds for this 0.8.34 device); only the
    # shortened descriptions (schemas/tool-metadata.en.json) make it smaller than the baseline.
    assert len(full) == 61 and size(full) > BASELINE_CHARS * 0.85
    lean = await listed(compact(hub_with(FakePeer("pc", device_tools()))))
    assert size(lean) <= BASELINE_CHARS * 0.5, size(lean)
    names = {tool["name"] for tool in lean}
    assert {"exec_command", "exec_wait", "write_stdin", "file_read", "edit_file", "toolsearch", "tooluse",
            "use_device", "mail_send", "file_copy"} <= names
    assert not names & (LEGACY_TOOL_NAMES | HIDDEN_GATEWAY_TOOLS)
    assert not names & {"screenshot", "mouse", "read_ui", "watch_create", "request_user_input", "notify",
                        "device_info", "sleep", "curr_time", "update_plan", "agent_sessions"}
    for tool in lean:
        assert (tool.get("_meta") or {}).get("fastmcp") is None, tool["name"]
        assert tool.get("title"), tool["name"]
    exec_command = next(tool for tool in lean if tool["name"] == "exec_command")
    assert exec_command["_meta"] == {"anx/capability": "exec"} and "outputSchema" not in exec_command
    assert "exec_wait" in exec_command["description"] and "max 50000" in exec_command["description"]
    assert exec_command["inputSchema"]["properties"]["yield_time_ms"] == {"type": "number"}
    assert len(gateway_mod.INSTRUCTIONS) < 700


def test_compact_schema_flattens_nullable_parameters_only():
    schema = {"type": "object", "properties": {
        "a": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
        "b": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
        "c": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": 3, "title": "C"}}}
    assert compact_schema(schema)["properties"] == {
        "a": {"type": "string"}, "b": schema["properties"]["b"], "c": {"type": "integer", "default": 3}}


async def test_deferred_tools_are_found_by_toolsearch_with_schema_and_run_in_two_steps():
    peer = FakePeer("pc", device_tools())
    gateway = compact(hub_with(peer))
    async with Client(gateway.build()) as client:
        found = (await client.call_tool("toolsearch", {"query": "screenshot"})).data
        hit = found["tools"][0]
        assert hit["name"] == "screenshot" and hit["mcp_id"] == "pc/device" and hit["inputSchema"]["type"] == "object"
        assert not {"_meta", "annotations", "outputSchema", "matched_terms"} & set(hit)
        assert len(found["tools"]) <= 8
        bare = await client.call_tool("tooluse", {"tool": "screenshot", "arguments": {}})
        assert bare.data["tool"] == "screenshot"
        dotted = await client.call_tool("tooluse", {"tool": "device.notify", "arguments": {"title": "hi"}})
        assert dotted.data["tool"] == "notify"
        old_style = await client.call_tool("tooluse", {"mcp_id": "pc/device", "tool": "mouse",
                                                       "arguments": {"action": "position"}})
        assert old_style.data["tool"] == "mouse"
        # Still callable directly by name, exactly as before.
        direct = await client.call_tool("device_info", {})
        assert direct.data["tool"] == "device_info"
        unknown = await client.call_tool("tooluse", {"tool": "no_such_tool"}, raise_on_error=False)
        assert unknown.is_error and "toolsearch" in unknown.content[0].text
    assert DEFERRED_CAPABILITIES >= {"uia", "screen", "input", "watch", "ask"}


async def test_hidden_gateway_tools_and_legacy_aliases_stay_callable():
    gateway = compact(hub_with(FakePeer("pc", device_tools())))
    async with Client(gateway.build()) as client:
        choices = await client.call_tool("workspace_list", {})
        assert choices.data["workspaces"][0]["agent_id"] == "pc"
        listed_jobs = await client.call_tool("file_copy_list", {})
        assert "jobs" in listed_jobs.structured_content or listed_jobs.structured_content is not None
        found = (await client.call_tool("toolsearch", {"query": "recall mail"})).data["tools"]
        assert any(item["name"] == "mail_recall" and item["mcp_id"] == "nexus" for item in found)
        via = await client.call_tool("tooluse", {"tool": "file_copy_list"})
        assert via.structured_content is not None
        detail = await client.call_tool("tooluse", {"tool": "nexus.mcpdetail", "arguments": {"mcp_id": "pc/device"}})
        assert detail.structured_content["mcp_id"] == "pc/device"


async def test_tooluse_bare_name_on_two_devices_needs_a_choice():
    gateway = compact(hub_with(FakePeer("pc-a", device_tools()), FakePeer("pc-b", device_tools())))
    async with Client(gateway.build()) as client:
        ambiguous = await client.call_tool("tooluse", {"tool": "screenshot"}, raise_on_error=False)
        assert ambiguous.is_error and "pc-a/device" in ambiguous.content[0].text
        found = (await client.call_tool("toolsearch", {"query": "screenshot"})).data["tools"]
        hits = [item for item in found if item["name"] == "screenshot"]
        assert len(hits) == 1 and hits[0]["also_on"]
        chosen = (await client.call_tool("use_device", {"device_id": "pc-b"})).data
        routed = await client.call_tool("tooluse", {"tool": "screenshot", "workspace_id": chosen["workspace_id"]})
        assert routed.data["machine"] == "pc-b"


# -- exec results, waits, progress ----------------------------------------------------------
def exec_answer(tool, arguments):
    if tool == "exec_command":
        return {"wall_time_seconds": 50.0, "output": "line 1\nline 2\n", "original_token_count": 4,
                "session_id": 1004, "pid": 9, "shell": "bash"}
    return {"wall_time_seconds": 61.2, "output": "done\n", "original_token_count": 1, "exit_code": 0}


async def test_exec_results_are_plain_text_and_old_devices_get_exec_wait_and_the_wait_cap():
    peer = FakePeer("pc", device_tools(), exec_answer, hostname="PC-A")
    gateway = compact(hub_with(peer))
    async with Client(gateway.build()) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert "exec_wait" in names  # 0.8.34 has no exec_wait: the gateway provides it
        started = await client.call_tool_mcp("exec_command", {"cmd": "make", "yield_time_ms": 150000})
        assert started.structuredContent is None
        text = started.content[0].text
        assert text.startswith("[running · session 1004 · 50.0s · PC-A · bash] exec_wait(session_id=1004)")
        assert text.endswith("line 1\nline 2\n")
        assert peer.calls[-1]["arguments"]["yield_time_ms"] == 50000  # capped for the old device
        waited = await client.call_tool_mcp("exec_wait", {"session_id": 1004, "timeout_ms": 120000})
        assert peer.calls[-1]["tool"] == "write_stdin"
        assert peer.calls[-1]["arguments"] == {"session_id": 1004, "chars": "", "yield_time_ms": 50000}
        assert waited.content[0].text == "[exit 0 · 61.2s · PC-A]\ndone\n"
        asked = await client.call_tool_mcp("request_user_input", {"question": "ok?"})
        assert peer.calls[-1]["arguments"]["timeout_s"] == 50  # the device default (240 s) is too long


def test_exec_text_formats():
    assert exec_text({"status": "error", "error": "shell 'zsh' is not available"}, "pc") == \
        ("[error · pc] shell 'zsh' is not available", True)
    assert exec_text({"output": "", "exit_code": 1, "wall_time_seconds": 0.04}, "pc") == ("[exit 1 · 0.0s · pc]\n(no output)", False)
    text, failed = exec_text({"output": "failed to start command: x", "exit_code": None, "error": "x",
                              "wall_time_seconds": 0.0}, "pc")
    assert failed and text.startswith("[error · pc]")


async def test_clamped_sleep_says_what_is_left_and_other_device_results_name_the_device():
    gateway = compact(hub_with(FakePeer("pc-a", device_tools()), FakePeer("pc-b", device_tools())),
                      tool_wait_max_s=10)
    async with Client(gateway.build()) as client:
        chosen = (await client.call_tool("use_device", {"device_id": "pc-a"})).data
        slept = await client.call_tool_mcp("tooluse", {"tool": "sleep", "arguments": {"duration_ms": 25000},
                                                       "workspace_id": chosen["workspace_id"]})
        texts = [block.text for block in slept.content]
        assert any("sleep(duration_ms=15000)" in text for text in texts) and "[device: pc-a]" in texts


async def test_waits_are_capped_at_tool_wait_max_s():
    gateway = Gateway(Hub(), tool_wait_max_s=50)
    assert gateway.wait_budget(170) == 50 and gateway.wait_budget(5) == 5
    assert gateway.file_copy.wait_max_ms == 50000


async def test_progress_notifications_only_with_a_token(monkeypatch):
    monkeypatch.setattr(gateway_mod, "PROGRESS_INTERVAL_S", 0.2)
    peer = FakePeer("pc", device_tools(), delay=0.9)
    gateway = compact(hub_with(peer))
    seen: list[tuple] = []

    async def on_progress(progress, total, message):
        seen.append((progress, message))

    async with Client(gateway.build()) as client:
        await client.call_tool("file_read", {"path": "x"}, progress_handler=on_progress)
        assert seen and "file_read" in seen[0][1]
        seen.clear()
        await client.call_tool("file_read", {"path": "x"})
        assert not seen


# -- the default device -----------------------------------------------------------------------
async def test_local_mcp_calls_default_to_the_calling_device(monkeypatch):
    a, b = FakePeer("pc-a", device_tools()), FakePeer("pc-b", device_tools())
    gateway = compact(hub_with(a, b))
    monkeypatch.setattr(gateway_mod, "caller_device", lambda: "pc-b")
    async with Client(gateway.build()) as client:
        result = await client.call_tool("file_read", {"path": "x"})
        assert result.data["machine"] == "pc-b"
        assert b.calls[-1]["scope"] == "device:pc-b"  # one stable upstream scope, not one per request
        again = await client.call_tool("file_read", {"path": "y"})
        assert again.data["machine"] == "pc-b" and b.calls[-1]["scope"] == "device:pc-b"
        # A use_device choice still wins over the default.
        chosen = (await client.call_tool("use_device", {"device_id": "pc-a"})).data
        assert "shells" not in chosen  # this fake device reports none
        other = await client.call_tool("file_read", {"path": "x", "workspace_id": chosen["workspace_id"]})
        assert other.data["machine"] == "pc-a"


async def test_without_a_device_choice_two_online_devices_give_a_clear_error():
    gateway = compact(hub_with(FakePeer("pc-a", device_tools(), hostname="Alpha"),
                               FakePeer("pc-b", device_tools(), hostname="Beta")))
    async with Client(gateway.build()) as client:
        failed = await client.call_tool("file_read", {"path": "x"}, raise_on_error=False)
        message = failed.content[0].text
        assert failed.is_error and "use_device" in message and "Alpha" in message and "Beta" in message
        devices = (await client.call_tool("device_list", {})).data["devices"]
        assert devices[0]["shells"] == ["bash", "sh"] and devices[0]["shell_default"] == "bash"
        chosen = (await client.call_tool("use_device", {"device_id": "Beta"})).data
        assert chosen["shells"] == ["bash", "sh"] and chosen["shell_default"] == "bash"
        assert "Shells: bash, sh (default bash)" in chosen["hint"]
        # No "current device" promise: the stateless default transport keeps no session.
        assert f"workspace_id={chosen['workspace_id']} on every call" in chosen["hint"]
        assert "Current device" not in chosen["hint"]


def test_one_device_with_several_workdirs_uses_its_default():
    peer = FakePeer("pc", [])
    peer.catalog["workspaces"]["other"] = {"mcp": "device", "path": "/elsewhere"}
    _, _, name = hub_with(peer).workspace(None)
    assert name == "default"
    del peer.catalog["workspaces"]["default"]
    peer.catalog["workspaces"]["third"] = {"mcp": "device", "path": "/third"}
    with pytest.raises(RoutingError, match="use_device"):
        hub_with(peer).workspace(None)


# -- review fixes -----------------------------------------------------------------------------
def _with_description(tools: list[dict], name: str, description: str) -> list[dict]:
    return [{**tool, "description": description} if tool["name"] == name else tool for tool in tools]


async def test_direct_call_of_a_deferred_tool_uses_the_selected_devices_schema(monkeypatch):
    """pc-a's screenshot gained a parameter; pc-b (first in the hub) has not: a call on pc-a is valid."""
    newer = [{**tool, "inputSchema": {**tool["inputSchema"], "properties": {
        **tool["inputSchema"].get("properties", {}), "scale": {"type": "number"}}}}
             if tool["name"] == "screenshot" else tool for tool in device_tools()]
    b, a = FakePeer("pc-b", device_tools()), FakePeer("pc-a", newer)
    gateway = compact(hub_with(b, a))
    monkeypatch.setattr(gateway_mod, "caller_device", lambda: "pc-a")
    async with Client(gateway.build()) as client:
        result = await client.call_tool("screenshot", {"scale": 0.5})
        assert result.data["machine"] == "pc-a"


async def test_a_nested_explicit_null_the_device_schema_allows_still_validates():
    peer = FakePeer("pc", device_tools())
    gateway = compact(hub_with(peer))
    async with Client(gateway.build()) as client:
        listed_tools = {tool.name: tool for tool in await client.list_tools()}
        assert "notify" not in listed_tools  # deferred, but callable by name
        args = {"title": "a", "body": "b", "actions": [{"id": "o", "label": "Open", "link": None}]}
        result = await client.call_tool("notify", args)
        assert result.data["tool"] == "notify" and peer.calls[-1]["arguments"]["actions"][0]["link"] is None
        # The listing is still compact.
        exec_command = listed_tools["exec_command"]
        assert exec_command.inputSchema["properties"]["yield_time_ms"] == {"type": "number"}


async def test_toolsearch_names_every_devices_mcp_id_and_tooluse_takes_a_device_prefix():
    gateway = compact(hub_with(FakePeer("pc-a", device_tools(), hostname="Alpha"),
                               FakePeer("pc-b", device_tools(), hostname="Beta")))
    async with Client(gateway.build()) as client:
        hit = next(item for item in (await client.call_tool("toolsearch", {"query": "screenshot"})).data["tools"]
                   if item["name"] == "screenshot")
        assert hit["also_on"] == [{"device": "Beta", "mcp_id": "pc-b/device"}]
        old_flow = await client.call_tool("tooluse", {"mcp_id": hit["also_on"][0]["mcp_id"], "tool": "screenshot"})
        assert old_flow.data["machine"] == "pc-b"
        by_name = await client.call_tool("tooluse", {"tool": "Beta.screenshot"})
        assert by_name.data["machine"] == "pc-b"
        by_id = await client.call_tool("tooluse", {"tool": "pc-a.screenshot"})
        assert by_id.data["machine"] == "pc-a"


async def test_tooluse_with_mcp_id_nexus_runs_the_hidden_gateway_tool():
    gateway = compact(hub_with(FakePeer("pc", device_tools())))
    async with Client(gateway.build()) as client:
        found = (await client.call_tool("toolsearch", {"query": "copy jobs list"})).data["tools"]
        hit = next(item for item in found if item["name"] == "file_copy_list")
        assert hit["mcp_id"] == "nexus"
        ran = await client.call_tool("tooluse", {"mcp_id": hit["mcp_id"], "tool": hit["name"]})
        assert ran.structured_content is not None
        unknown = await client.call_tool("tooluse", {"mcp_id": "nexus", "tool": "exec_command"}, raise_on_error=False)
        assert unknown.is_error and "unknown tool" in unknown.content[0].text
        detail = (await client.call_tool("mcpdetail", {"mcp_id": "nexus"})).structured_content
        assert any(tool["name"] == "mail_recall" for tool in detail["tools"])


NEW_EXEC = ("Run a command in a shell and return its output and exit code. shell: bash|sh|zsh|pwsh|powershell|cmd "
            "(default here: powershell; device_list shows each device's shells). Waits up to yield_time_ms.")


async def test_exec_description_names_no_single_devices_default_at_the_gateway(monkeypatch):
    a = FakePeer("pc-a", _with_description(device_tools(), "exec_command", NEW_EXEC))
    b = FakePeer("pc-b", _with_description(device_tools(), "exec_command", NEW_EXEC.replace("powershell;", "bash;", 1)))
    gateway = compact(hub_with(a, b))
    async with Client(gateway.build()) as client:
        text = next(tool for tool in await client.list_tools() if tool.name == "exec_command").description
        assert "default here" not in text and "default: the device's own" in text
    monkeypatch.setattr(gateway_mod, "caller_device", lambda: "pc-a")
    async with Client(gateway.build()) as client:  # the local MCP: "here" is one device
        text = next(tool for tool in await client.list_tools() if tool.name == "exec_command").description
        assert "(default here: powershell;" in text


async def test_an_old_windows_device_without_git_bash_refuses_shell_bash():
    old = ("Run a command and return its output and exit_code. cmd is a PowerShell (Git Bash is not installed on "
           "this machine) command line; shell='powershell' or shell='cmd' runs another shell instead. The call waits "
           "up to yield_time_ms (default 10000, max 150000) and returns as soon as the command exits; a command "
           "still running then returns a session_id: poll it or type into it with write_stdin, stop it with "
           "exec_kill. Pass a long yield_time_ms for builds and tests, a short one to start a server. Output beyond "
           "max_output_tokens (about 4 characters each, default 10000) keeps its head and tail. workdir defaults to "
           "the session's workdir. For reading, searching and editing files prefer file_read, file_grep, file_glob "
           "and edit_file: they need no quoting.")
    peer = FakePeer("pc", _with_description(device_tools(), "exec_command", old), exec_answer)
    gateway = compact(hub_with(peer))
    async with Client(gateway.build()) as client:
        text = next(tool for tool in await client.list_tools() if tool.name == "exec_command").description
        assert "bash|" not in text and "powershell|cmd" in text
        refused = await client.call_tool_mcp("exec_command", {"cmd": "ls", "shell": "bash"})
        assert refused.isError and "available: powershell, cmd" in refused.content[0].text
        assert not peer.calls
        await client.call_tool_mcp("exec_command", {"cmd": "dir", "shell": "cmd"})
        assert peer.calls[-1]["arguments"]["shell"] == "cmd"
