import subprocess
import sys

import anyio
import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from conftest import free_port

TOKEN = "t" * 40


def call_echo(url: str, text: str = "你好", auth: str | None = None) -> str:
    async def go():
        async with Client(url, auth=auth) as client:
            result = await client.call_tool("echo", {"text": text})
            return result.content[0].text
    return anyio.run(go)


def mcp_post(url: str, headers: dict | None = None) -> httpx.Response:
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    base = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with httpx.Client(trust_env=False, timeout=15) as http:
        return http.post(url, json=body, headers={**base, **(headers or {})})


def test_unauthenticated_loopback_service(workspace, run):
    directory = workspace.service("svc", manifest={"port": 1})
    svc = run(directory)
    assert call_echo(svc.url + "/mcp") == "echo:你好"

    with httpx.Client(trust_env=False) as http:
        health = http.get(svc.url + "/healthz")
    assert health.json() == {"status": "ok", "service": "svc", "version": "1.2.3"}
    assert mcp_post(svc.url + "/mcp").status_code == 200
    assert mcp_post(svc.url + "/mcp", {"Host": "evil.example"}).status_code == 421
    assert mcp_post(svc.url + "/mcp", {"Origin": "https://evil.example"}).status_code == 403


def test_refuses_public_bind_without_auth(workspace):
    directory = workspace.service("svc", manifest={"port": 1})
    result = subprocess.run([sys.executable, "app.py", "--transport", "http", "--host", "0.0.0.0", "--port", str(free_port())],
                            cwd=directory, capture_output=True, text=True, timeout=60)
    assert result.returncode == 2
    assert "refusing to listen on 0.0.0.0 without authentication" in result.stderr


def test_bearer_mode(workspace, run):
    workspace.secret("svc/token", TOKEN)
    directory = workspace.service("svc", manifest={"port": 1},
                                  entry={"auth": {"mode": "bearer", "bearer_secrets": ["svc/token"]}})
    svc = run(directory)
    assert mcp_post(svc.url + "/mcp").status_code == 401
    assert mcp_post(svc.url + "/mcp", {"Authorization": "Bearer " + "x" * 40}).status_code == 401
    assert call_echo(svc.url + "/mcp", auth=TOKEN) == "echo:你好"
    with httpx.Client(trust_env=False) as http:
        assert http.get(svc.url + "/healthz").status_code == 200  # health stays open for mcpm

    svc.stop()
    banner = svc.output()
    assert "auth=bearer" in banner and TOKEN not in banner


def test_stdio_needs_no_auth_or_port(workspace):
    workspace.secret("svc/token", TOKEN)
    directory = workspace.service("svc", entry={"auth": {"mode": "bearer", "bearer_secrets": ["svc/token"]}})

    async def go():
        transport = StdioTransport(command=sys.executable, args=["app.py", "--transport", "stdio"], cwd=str(directory))
        async with Client(transport) as client:
            return (await client.call_tool("echo", {"text": "stdio"})).content[0].text
    assert anyio.run(go) == "echo:stdio"


def test_check_config_reports_missing_secret_without_starting(workspace):
    directory = workspace.service("svc", manifest={"port": 1},
                                  entry={"auth": {"mode": "bearer", "bearer_secrets": ["svc/token"]}})
    missing = subprocess.run([sys.executable, "app.py", "--transport", "http", "--check-config"], cwd=directory,
                             capture_output=True, text=True, timeout=60)
    assert missing.returncode == 2 and "secret 'svc/token'" in missing.stderr and "does not exist" in missing.stderr

    workspace.secret("svc/token", TOKEN)
    ok = subprocess.run([sys.executable, "app.py", "--transport", "http", "--check-config"], cwd=directory,
                        capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0 and "configuration ok" in ok.stdout and TOKEN not in ok.stdout + ok.stderr


@pytest.mark.parametrize("args", [["secret", "new", "svc/generated"]])
def test_secret_cli_never_prints_values(workspace, args):
    result = subprocess.run([sys.executable, "-m", "mcpkit", "--root", str(workspace.root), *args],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0
    value = (workspace.root / "secrets" / "svc" / "generated").read_text(encoding="utf-8").strip()
    assert len(value) == 64 and value not in result.stdout
    check = subprocess.run([sys.executable, "-m", "mcpkit", "--root", str(workspace.root), "secret", "check", "svc/generated"],
                           capture_output=True, text=True, timeout=60)
    assert check.returncode == 0 and value not in check.stdout


def test_public_url_host_is_accepted_and_others_are_not(workspace, run):
    """Changing the tunnel domain must stay a one-line edit: the guard follows
    public_url, so no second list can silently disagree with it."""
    workspace.secret("svc/token", TOKEN)
    directory = workspace.service("svc", manifest={"port": 1}, entry={
        "public_url": "https://svc.example",
        "auth": {"mode": "bearer", "bearer_secrets": ["svc/token"]}})
    svc = run(directory)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert mcp_post(svc.url + "/mcp", {**headers, "Host": "svc.example"}).status_code == 200
    assert mcp_post(svc.url + "/mcp", {**headers, "Host": "old.example"}).status_code == 421
