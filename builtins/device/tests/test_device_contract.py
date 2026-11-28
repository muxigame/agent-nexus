"""The anx.device/1 contract on the device plugin (design §5.2, milestone M5).

* the earlier tool names, annotations and schemas are kept (snapshot recorded
  at f343399, minus apply_patch/codex_info, which edit_file/patch_file replaced);
  titles use English;
* every tool carries ``_meta`` anx/capability + anx/contract, and the plugin's
  capability table matches ``schemas/device-contract.v1.json``;
* ``device_info`` validates against the schema and reports the hostname;
* a POSIX platform declares screen/uia/input ``available: false`` with a reason;
* ``notify``, the ``file_*`` tools and ``agent_sessions`` behave;
* the same conformance checks ``anx device conformance`` runs pass in process.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError, ValidationError

REJECTED = (ToolError, ValidationError)
from jsonschema import Draft202012Validator

from conftest import device_server
from src.platform import CONTRACT, TOOL_CAPABILITY, Contract, current_platform
from src.platform import windows as windows_platform
from src.server import WINDOWS_TOOLS

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SCHEMA = json.loads((REPO / "schemas" / "device-contract.v1.json").read_text(encoding="utf-8"))
LEGACY = json.loads((HERE / "fixtures" / "legacy_device_tools.json").read_text(encoding="utf-8"))["tools"]


def payload(result):
    return result.structured_content


@pytest.fixture
def server(tmp_path: Path):
    return device_server(tmp_path, default_workdir=str(tmp_path))


@pytest.fixture
def linux_server(tmp_path: Path):
    return device_server(tmp_path, default_workdir=str(tmp_path), platform_name="linux")


# -- the legacy surface -------------------------------------------------------
async def test_legacy_tool_names_annotations_and_schemas_are_kept(server):
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    expected = {name: entry for name, entry in LEGACY.items()
                if server.contract.platform == "windows" or name not in WINDOWS_TOOLS}
    for name, entry in expected.items():
        assert name in tools, name
        tool = tools[name]
        assert tool.title and tool.title.isascii(), name
        assert tool.annotations.model_dump(mode="json", by_alias=True, exclude_none=True) == entry["annotations"], name
        schema = tool.parameters
        assert sorted(schema.get("required", [])) == entry["required"], name
        properties = schema.get("properties", {})
        for prop, kind in entry["properties"].items():
            assert prop in properties, f"{name}.{prop} removed"
            assert properties[prop].get("type", properties[prop].get("anyOf")) == kind, f"{name}.{prop} retyped"
    added = set(tools) - set(LEGACY)
    assert added == {"device_info", "notify", "file_list", "file_stat", "file_read", "file_write", "agent_sessions",
                     "edit_file", "exec_wait", "exec_tail", "patch_file", "file_glob", "file_grep"}


@pytest.mark.parametrize("platform_name", ["windows", "linux", "darwin"])
async def test_tool_metadata_uses_english_on_every_platform(tmp_path, platform_name):
    localized = device_server(tmp_path, platform_name=platform_name)

    def descriptions(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"title", "description"} and isinstance(item, str):
                    yield item
                yield from descriptions(item)
        elif isinstance(value, list):
            for item in value:
                yield from descriptions(item)

    for tool in await localized.mcp.list_tools():
        assert tool.title and tool.title.isascii(), tool.name
        for text in (tool.title, tool.description or "", *descriptions(tool.parameters)):
            assert not re.search(r"[\u3400-\u9fff]", text), (tool.name, text)


async def test_every_tool_is_tagged_with_its_capability(server):
    for tool in await server.mcp.list_tools():
        meta = tool.meta or {}
        assert meta.get("anx/contract") == CONTRACT, tool.name
        assert meta.get("anx/capability") == TOOL_CAPABILITY[tool.name], tool.name
        assert tool.annotations is not None, tool.name


def test_plugin_capability_table_matches_the_contract_schema():
    standard = {name: list(tools) for name, tools in SCHEMA["x-anx-standard-tools"].items()}
    for name, tools in SCHEMA["x-anx-optional-tools"].items():
        standard[name] = standard.get(name, []) + tools
    table: dict[str, list[str]] = {}
    for tool, capability in TOOL_CAPABILITY.items():
        table.setdefault(capability, []).append(tool)
    assert {name: sorted(tools) for name, tools in table.items()} == \
        {name: sorted(tools) for name, tools in standard.items()}
    for name in SCHEMA["x-anx-core-capabilities"]:
        for platform in ("windows", "linux", "darwin"):
            assert name in Contract(platform).declared


def test_platform_names():
    assert current_platform("win32") == "windows"
    assert current_platform("linux") == "linux"
    assert current_platform("darwin") == "darwin"
    assert current_platform("freebsd14") == "linux"


# -- device_info ----------------------------------------------------------------
async def test_device_info_matches_the_schema_and_reports_the_hostname(server):
    info = payload(await server.mcp.call_tool("device_info", {}))
    assert list(Draft202012Validator(SCHEMA).iter_errors(info)) == []
    assert info["contract"] == CONTRACT
    assert isinstance(info["hostname"], str) and info["hostname"]
    assert info["platform"] == current_platform()
    assert set(SCHEMA["x-anx-core-capabilities"]) <= set(info["capabilities"])
    assert info["capabilities"]["notify"].get("actions") is True
    # No tool lists or tiers leak into the published block.
    assert all(set(spec) <= {"v", "available", "reason", "actions"} for spec in info["capabilities"].values())
    assert set(info["agent_runtimes"]) == {"claude", "codex", "companion"}
    # The test companion URL refuses connections: reported unavailable, no exception.
    assert info["agent_runtimes"]["companion"] == {"available": False}
    if sys.platform == "win32":
        for name in ("screen", "uia", "input"):
            assert isinstance(info["capabilities"][name]["available"], bool)


async def test_posix_platform_declares_screen_unavailable_with_a_reason(linux_server):
    names = {tool.name for tool in await linux_server.mcp.list_tools()}
    assert not names & WINDOWS_TOOLS
    assert {"device_info", "notify", "file_read", "exec_command", "view_image"} <= names
    info = payload(await linux_server.mcp.call_tool("device_info", {}))
    assert list(Draft202012Validator(SCHEMA).iter_errors(info)) == []
    assert info["platform"] == "linux"
    for name in ("screen", "uia", "input"):
        assert info["capabilities"][name]["available"] is False
        assert "platform=linux" in info["capabilities"][name]["reason"]
    assert linux_server.control is None


def test_windows_probe_reports_availability_with_a_reason():
    state = windows_platform.probe()
    for name in ("screen", "uia", "input"):
        assert isinstance(state[name]["available"], bool)
        assert state[name]["available"] or state[name]["reason"]


def test_toast_xml_escapes_text_and_maps_actions():
    xml = windows_platform.toast_xml('a <b> & "c"', "body", "urgent",
                                     [{"id": "ok", "label": "OK"}, {"id": "go", "label": "Open", "link": "https://x/?a=1&b=2"}])
    assert 'scenario="urgent"' in xml and "a &lt;b&gt; &amp;" in xml
    assert 'arguments="anx-action:ok"' in xml and 'activationType="protocol"' in xml and "a=1&amp;b=2" in xml


# -- notify ---------------------------------------------------------------------
async def test_notify_dry_run_with_actions_validates_without_showing(server):
    calls = []
    server.notifier = lambda *args: calls.append(args) or {"backend": "fake"}
    result = payload(await server.mcp.call_tool("notify", {
        "title": "Build finished", "body": "all green", "level": "urgent", "dry_run": True,
        "actions": [{"id": "open", "label": "Open"}, {"id": "later", "label": "Later", "link": "agentnexus://inbox"}]}))
    assert result["status"] == "ok" and result["dry_run"] is True and result["shown"] is False
    assert result["notification"]["actions"][1] == {"id": "later", "label": "Later", "link": "agentnexus://inbox"}
    assert calls == []


async def test_notify_schema_rejects_bad_actions_and_levels(server):
    for arguments in ({"title": "x", "level": "loud"},
                      {"title": "x", "actions": [{"id": "only-id"}]},
                      {"title": "x", "actions": [{"id": "bad id!", "label": "x"}]}):
        with pytest.raises(REJECTED):
            await server.mcp.call_tool("notify", arguments)
    duplicate = payload(await server.mcp.call_tool("notify", {
        "title": "x", "dry_run": True, "actions": [{"id": "a", "label": "1"}, {"id": "a", "label": "2"}]}))
    assert duplicate["status"] == "error" and "unique" in duplicate["error"]


async def test_notify_uses_the_platform_backend_and_reports_failures(server):
    calls = []
    server.notifier = lambda title, body, level, actions: calls.append((title, body, level, actions)) or {"backend": "fake"}
    result = payload(await server.mcp.call_tool("notify", {"title": "t", "body": "b", "level": "warn"}))
    assert result == {"status": "ok", "dry_run": False, "shown": True, "backend": "fake"}
    assert calls == [("t", "b", "warn", [])]

    def broken(*_):
        raise RuntimeError("no notification service")
    server.notifier = broken
    failed = payload(await server.mcp.call_tool("notify", {"title": "t"}))
    assert failed["status"] == "error" and failed["shown"] is False and "no notification service" in failed["error"]


# -- files ----------------------------------------------------------------------
async def test_file_tools_round_trip_inside_allowed_roots(server, tmp_path):
    written = payload(await server.mcp.call_tool("file_write", {"path": "notes/a.txt", "content": "one\ntwo\n"}))
    assert written["status"] == "ok" and written["created"] is True
    appended = payload(await server.mcp.call_tool("file_write", {"path": "notes/a.txt", "content": "three\n",
                                                                 "mode": "append"}))
    assert appended["created"] is False
    again = payload(await server.mcp.call_tool("file_write", {"path": "notes/a.txt", "content": "x", "mode": "create"}))
    assert again["status"] == "error" and "already exists" in again["error"]

    read = payload(await server.mcp.call_tool("file_read", {"path": str(tmp_path / "notes" / "a.txt"),
                                                            "offset": 1, "limit": 1}))
    assert read["content"] == "two\n" and read["total_lines"] == 3 and read["next_offset"] == 2

    stat = payload(await server.mcp.call_tool("file_stat", {"path": "notes/a.txt"}))
    assert stat["exists"] is True and stat["type"] == "file" and stat["size"] == len("one\ntwo\nthree\n")
    assert payload(await server.mcp.call_tool("file_stat", {"path": "nope.txt"}))["exists"] is False

    listed = payload(await server.mcp.call_tool("file_list", {"path": "notes"}))
    assert [entry["name"] for entry in listed["entries"]] == ["a.txt"]
    assert payload(await server.mcp.call_tool("file_list", {"path": "notes", "pattern": "*.py"}))["entries"] == []


async def test_file_tools_obey_allowed_roots_and_refuse_binary(server, tmp_path):
    outside = tmp_path.parent / "outside-m5.txt"
    for tool, arguments in (("file_read", {"path": str(outside)}), ("file_stat", {"path": str(outside)}),
                            ("file_write", {"path": str(outside), "content": "x"}),
                            ("file_list", {"path": str(tmp_path.parent)})):
        result = payload(await server.mcp.call_tool(tool, arguments))
        assert result["status"] == "error" and "outside the allowed roots" in result["error"], tool
    assert not outside.exists()
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")
    result = payload(await server.mcp.call_tool("file_read", {"path": "blob.bin"}))
    assert result["status"] == "error" and "binary" in result["error"]


# -- agent_sessions ---------------------------------------------------------------
async def test_agent_sessions_finds_the_newest_claude_and_codex_sessions(server, tmp_path):
    home = tmp_path / "home"
    workdir = tmp_path
    server.agents_home = home
    import re
    project = home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workdir.resolve()))
    project.mkdir(parents=True)
    now = time.time()
    for name, age in (("old-session", 3600), ("new-session", 10)):
        path = project / f"{name}.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        os.utime(path, (now - age, now - age))
    claude = payload(await server.mcp.call_tool("agent_sessions", {"kind": "claude"}))
    assert claude["id"] == "new-session" and claude["confidence"] == "high"
    assert [item["id"] for item in claude["candidates"]] == ["new-session", "old-session"]

    day = home / ".codex" / "sessions" / "2026" / "11" / "10"
    day.mkdir(parents=True)
    for name, cwd, age in (("rollout-a.jsonl", str(workdir.resolve()), 50), ("rollout-b.jsonl", str(home), 5)):
        record = {"type": "session_meta", "payload": {"id": f"thread-{name[8]}", "cwd": cwd}}
        path = day / name
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        os.utime(path, (now - age, now - age))
    codex = payload(await server.mcp.call_tool("agent_sessions", {"kind": "codex"}))
    assert codex["id"] == "thread-a" and codex["confidence"] == "high"

    other = payload(await server.mcp.call_tool("agent_sessions", {"kind": "codex", "workdir": str(home)}))
    assert other["id"] == "thread-b"
    none = payload(await server.mcp.call_tool("agent_sessions", {"kind": "claude", "workdir": str(home)}))
    assert none["id"] is None and none["confidence"] == "none" and none["candidates"] == []
    with pytest.raises(REJECTED):
        await server.mcp.call_tool("agent_sessions", {"kind": "gpt"})


# -- conformance (the same checks as `anx device conformance`) ---------------------
def _conformance():
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from agent_nexus import conformance
    return conformance


@pytest.mark.parametrize("platform_name", [None, "linux"], ids=["native", "linux"])
async def test_conformance_passes_in_process(tmp_path, platform_name):
    server = device_server(tmp_path, default_workdir=str(tmp_path), platform_name=platform_name)
    server.notifier = lambda *_: pytest.fail("conformance must not show a notification")
    async with Client(server.mcp) as client:
        report = await _conformance().check_client(client)
    assert report.ok, report.failures()
    assert report.device["platform"] == (platform_name or current_platform())
