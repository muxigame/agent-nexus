import base64
import sys
from pathlib import Path

import pytest

from conftest import device_server
from src.server import DeviceServer, MEDIA_TTL_SECONDS


@pytest.fixture
def server(tmp_path: Path) -> DeviceServer:
    return device_server(tmp_path, default_workdir=str(tmp_path))


@pytest.mark.asyncio
async def test_expected_tool_surface(server: DeviceServer) -> None:
    tools = await server.mcp.list_tools()
    expected = [
        "exec_command",
        "write_stdin",
        "exec_wait",  # 0.8.35: waits for a running command (additive)
        "exec_kill",
        "exec_list",
        "edit_file",
        "patch_file",
        "view_image",
        "update_plan",
        "curr_time",
        "sleep",
        "companion_routes",
        "request_user_input",
        "request_user_input_async",
        "request_user_input_result",
        "watch_create",
        "watch_training",
        "watch_list",
        "watch_cancel",
        "watch_event",
        "watch_events",
        "start_control",
        "stop_control",
        "control_status",
        "list_screens",
        "screenshot",
        "vision",
        "read_ui",
        "element",
        "mouse",
        "keyboard",
        # anx.device/1 (M5): only added after the legacy surface.
        "device_info",
        "notify",
        "file_list",
        "file_stat",
        "file_read",
        "file_glob",
        "file_grep",
        "file_write",
        "agent_sessions",
    ]
    if sys.platform != "win32":  # desktop control (uia) is a Windows capability
        uia = {"start_control", "stop_control", "control_status", "list_screens", "screenshot",
               "vision", "read_ui", "element", "mouse", "keyboard"}
        expected = [name for name in expected if name not in uia]
    assert [tool.name for tool in tools] == expected



@pytest.mark.asyncio
async def test_view_image_returns_image_and_three_day_user_url(server: DeviceServer, tmp_path: Path) -> None:
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    image_path = tmp_path / "pixel.png"
    image_path.write_bytes(png)

    before = __import__("time").time()
    result = await server.mcp.call_tool("view_image", {"path": str(image_path)})
    encoded = base64.b64encode(png).decode("ascii")

    assert result.is_error is False
    assert result.content[0].type == "image"
    assert result.content[0].mime_type == "image/png"
    assert result.content[0].data == encoded
    assert result.content[1].type == "text"
    assert "If this image is important for the user to see" in result.content[1].text
    assert "This link expires in 3 days." in result.content[1].text

    url_line = next(line for line in result.content[1].text.splitlines() if "/media/" in line)
    assert url_line.startswith("http://127.0.0.1:")
    token = url_line.rsplit("/", 1)[-1]
    target, mime, expires_at = server._media_tokens[token]
    assert target == image_path.resolve()
    assert mime == "image/png"
    remaining = expires_at - before
    assert MEDIA_TTL_SECONDS - 2 <= remaining <= MEDIA_TTL_SECONDS + 2


@pytest.mark.asyncio
async def test_exec_command_short(server: DeviceServer, tmp_path: Path) -> None:
    result = await server.mcp.call_tool(
        "exec_command", {"cmd": "printf hello", "workdir": str(tmp_path)}
    )
    assert result.is_error is False
    assert result.structured_content["output"] == "hello"
    assert result.structured_content["exit_code"] == 0
    assert "session_id" not in result.structured_content


@pytest.mark.asyncio
async def test_exec_command_uses_default_workdir(tmp_path: Path) -> None:
    default_dir = tmp_path / "default"
    default_dir.mkdir()
    custom = device_server(tmp_path, default_workdir=str(default_dir), allowed_roots=[],
                          watch_db=str(tmp_path / "watchers-default.sqlite3"))
    result = await custom.mcp.call_tool("exec_command", {"cmd": "pwd"})
    output = result.structured_content["output"].replace("\\", "/")
    assert output.rstrip().endswith("/default")


@pytest.mark.asyncio
async def test_update_plan_validation(server: DeviceServer) -> None:
    good = await server.mcp.call_tool(
        "update_plan",
        {
            "plan": [
                {"step": "one", "status": "in_progress"},
                {"step": "two", "status": "pending"},
            ]
        },
    )
    assert good.structured_content["status"] == "ok"

    bad = await server.mcp.call_tool(
        "update_plan",
        {
            "plan": [
                {"step": "one", "status": "in_progress"},
                {"step": "two", "status": "in_progress"},
            ]
        },
    )
    assert bad.structured_content["status"] == "error"


@pytest.mark.asyncio
async def test_no_codex_compatibility_surface_remains(server: DeviceServer) -> None:
    tools = await server.mcp.list_tools()
    names = {tool.name for tool in tools}
    assert not names & {"apply_patch", "codex_info"}
    # Codex is still a real agent runtime that these two tools look for; nothing else names it.
    for tool in tools:
        if tool.name not in {"agent_sessions", "device_info"}:
            assert "codex" not in (tool.description or "").lower(), tool.name
