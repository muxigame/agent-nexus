"""The client's 连接器: local MCP servers offered to the gateway, credentials kept as secrets."""
from __future__ import annotations

import asyncio
import json
import socket

import pytest
import uvicorn
from fastmcp import FastMCP

from agent_nexus import local_connectors
from agent_nexus.sections import mcp_from_connector


def root_for(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "agent-nexus.json").write_text(json.dumps({"manager": {"port": 1}, "builtins": False, "scan": []}), encoding="utf-8")
    return root


def test_save_keeps_credentials_out_of_the_config(tmp_path):
    root = root_for(tmp_path)
    saved = local_connectors.save(root, "ide", {"title": "IDE", "url": "http://127.0.0.1:9/mcp",
                                                 "auth": {"type": "bearer", "token": "super-secret-token"}})
    assert saved["auth"] == {"type": "bearer", "headers": None} and saved["url"] == "http://127.0.0.1:9/mcp"
    config = (root / "agent-nexus.json").read_text(encoding="utf-8")
    assert "super-secret-token" not in config
    assert (root / "secrets" / "connectors" / "ide").read_text(encoding="utf-8").strip() == "super-secret-token"
    entry = mcp_from_connector(json.loads(config)["connectors"]["ide"])
    assert entry == {"url": "http://127.0.0.1:9/mcp", "transport": "http", "token_secret": "connectors/ide", "enabled": True,
                     "description": ""}
    # editing without a new token keeps the old one
    local_connectors.save(root, "ide", {"url": "http://127.0.0.1:9/mcp2", "auth": {"type": "bearer"}})
    assert (root / "secrets" / "connectors" / "ide").read_text(encoding="utf-8").strip() == "super-secret-token"
    headers = local_connectors.save(root, "api", {"url": "https://api.example.test/mcp",
                                                  "auth": {"type": "headers", "headers": {"X-Api-Key": "k-123", "X-Region": "r1"}}})
    assert headers["auth"]["headers"] == ["X-Api-Key", "X-Region"]
    assert json.loads((root / "secrets" / "connectors" / "api").read_text(encoding="utf-8")) == {"X-Api-Key": "k-123", "X-Region": "r1"}
    local_connectors.delete(root, "ide")
    assert not (root / "secrets" / "connectors" / "ide").exists()
    assert [c["name"] for c in local_connectors.list_connectors(root)] == ["api"]


def test_invalid_connectors_are_refused(tmp_path):
    root = root_for(tmp_path)
    for name, body in (("bad name!", {"url": "http://127.0.0.1/mcp"}), ("x", {"url": "ftp://host/mcp"}),
                       ("x", {"transport": "stdio"}), ("x", {"url": "http://h/mcp", "auth": {"type": "bearer"}}),
                       ("x", {"transport": "carrier-pigeon"})):
        with pytest.raises(local_connectors.ConnectorError):
            local_connectors.save(root, name, body)
    stdio = local_connectors.save(root, "fs", {"transport": "stdio", "command": "npx -y some-server /data"})
    assert stdio["command"] == ["npx", "-y", "some-server", "/data"]


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_a_running_mcp_server_is_tested_and_offered_to_the_gateway(tmp_path):
    from agent_nexus.agent import LocalGateway

    server = FastMCP("local-ide")

    @server.tool
    def open_file(path: str) -> str:
        """Open a file in the editor."""
        return path

    async def scenario():
        port = _free_port()
        app = server.http_app(path="/mcp")
        uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on"))
        task = asyncio.create_task(uv.serve())
        for _ in range(100):
            if uv.started:
                break
            await asyncio.sleep(0.05)
        try:
            root = root_for(tmp_path)
            local_connectors.save(root, "ide", {"title": "IDE", "description": "the editor", "url": f"http://127.0.0.1:{port}/mcp"})
            result = await local_connectors.test(root, "ide")
            assert result == {"ok": True, "tools": ["open_file"]}
            gateway = LocalGateway(root, {"agent_id": "pc", "url": "wss://hub.example.test"})
            targets = gateway.configured_targets()
            assert "ide" in targets and gateway.descriptions["ide"] == "the editor"
            unreachable = local_connectors.save(root, "gone", {"url": "http://127.0.0.1:9/mcp"})
            assert unreachable["name"] == "gone"
            assert (await local_connectors.test(root, "gone"))["ok"] is False
        finally:
            uv.should_exit = True
            await task

    asyncio.run(scenario())
