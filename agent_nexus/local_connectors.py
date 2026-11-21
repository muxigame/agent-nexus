"""The client's 连接器: local MCP servers that agent-nexus does not run, offered to the gateway.

They live in ``connectors`` of ``agent-nexus.json`` (sections.mcp_from_connector) and the agent
publishes them next to the plugins on its next catalog refresh. Credentials never go into the
config: a bearer token or a set of headers is written to ``secrets/connectors/<name>``
(owner-only) and only the secret's name is kept in the connector.

    {"title": "...", "description": "...", "enabled": true,
     "transport": "http" | "sse" | "stdio",
     "url": "http://127.0.0.1:3000/mcp",                    # http / sse
     "command": ["node", "server.js"], "cwd": "...", "env": {...},   # stdio
     "auth": {"type": "none" | "bearer" | "headers", "token_secret" | "headers_secret": "connectors/<name>"}}
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcpkit.config import ConfigError, ServiceConfig, read_secret, secret_path, write_secret

from . import paths
from .federation import valid_id

TRANSPORTS = ("http", "sse", "stdio")


class ConnectorError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _config(root: Path) -> ServiceConfig:
    return ServiceConfig(name="manager", root=root, dir=root)


def _read(root: Path) -> dict:
    data = paths.read_config(root)
    return data if isinstance(data, dict) else {}


def _write(root: Path, data: dict) -> None:
    target = paths.write_path(root)
    temporary = target.with_suffix(".json.connectors.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def view(name: str, connector: dict) -> dict:
    auth = connector.get("auth") or {"type": "none"}
    return {"name": name, "title": connector.get("title") or name, "description": connector.get("description", ""),
            "enabled": connector.get("enabled", True) is not False, "transport": connector.get("transport", "http"),
            "url": connector.get("url"), "command": connector.get("command"), "cwd": connector.get("cwd"),
            "env_keys": sorted((connector.get("env") or {}).keys()),
            "auth": {"type": auth.get("type", "none"),
                     "headers": sorted(auth.get("header_names") or []) if auth.get("type") == "headers" else None}}


def list_connectors(root: Path) -> list[dict]:
    connectors = _read(root).get("connectors") or {}
    return [view(name, value) for name, value in sorted(connectors.items()) if isinstance(value, dict)]


def _plugin_names(root: Path) -> set[str]:
    try:
        from .registry import discover, load_manager_config
        return set(discover(load_manager_config(root)))
    except Exception:
        return set()


def save(root: Path, name: str, body: dict) -> dict:
    root = Path(root)
    try:
        valid_id(name)
    except Exception:
        raise ConnectorError(400, "名称只能用字母、数字、点、下划线和连字符（1-100 个）") from None
    data = _read(root)
    connectors = data.get("connectors") if isinstance(data.get("connectors"), dict) else {}
    existing = connectors.get(name) if isinstance(connectors.get(name), dict) else None
    if existing is None and name in _plugin_names(root):
        raise ConnectorError(409, f"已经有一个叫 {name} 的插件")
    transport = str(body.get("transport") or "http")
    if transport not in TRANSPORTS:
        raise ConnectorError(400, "transport 只能是 http、sse 或 stdio")
    entry: dict[str, Any] = {"title": str(body.get("title") or name)[:100], "description": str(body.get("description") or "")[:500],
                             "enabled": body.get("enabled", True) is not False, "transport": transport}
    if transport == "stdio":
        command = body.get("command")
        if isinstance(command, str):
            command = command.split()
        if not isinstance(command, list) or not command or not all(isinstance(c, str) and c for c in command):
            raise ConnectorError(400, "stdio 连接器需要启动命令")
        entry["command"] = command
        if body.get("cwd"):
            entry["cwd"] = str(body["cwd"])
        env = body.get("env") if isinstance(body.get("env"), dict) else (existing or {}).get("env") or {}
        if env:
            entry["env"] = {str(k): str(v) for k, v in env.items()}
    else:
        url = str(body.get("url") or "").strip()
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ConnectorError(400, "需要一个 http(s) 地址，例如 http://127.0.0.1:3000/mcp")
        entry["url"] = url
        auth = body.get("auth") if isinstance(body.get("auth"), dict) else {"type": "none"}
        kind = auth.get("type", "none")
        old = (existing or {}).get("auth") or {}
        secret = f"connectors/{name}"
        if kind == "bearer":
            token = str(auth.get("token") or "").strip()
            if token:
                write_secret(_config(root), secret, token)
            elif old.get("type") != "bearer":
                raise ConnectorError(400, "需要令牌")
            entry["auth"] = {"type": "bearer", "token_secret": secret}
        elif kind == "headers":
            headers = auth.get("headers")
            if isinstance(headers, dict) and headers:
                clean = {str(k).strip(): str(v) for k, v in headers.items() if str(k).strip()}
                write_secret(_config(root), secret, json.dumps(clean))
                names = sorted(clean)
            elif old.get("type") == "headers":
                names = old.get("header_names") or []
            else:
                raise ConnectorError(400, "需要至少一个请求头")
            entry["auth"] = {"type": "headers", "headers_secret": secret, "header_names": names}
        elif kind != "none":
            raise ConnectorError(400, "认证方式只能是 none、bearer 或 headers")
        if kind == "none":
            _forget_secret(root, name)
    connectors[name] = entry
    data["connectors"] = connectors
    _write(root, data)
    return view(name, entry)


def _forget_secret(root: Path, name: str) -> None:
    try:
        secret_path(_config(root), f"connectors/{name}").unlink(missing_ok=True)
    except (OSError, ConfigError):
        pass


def delete(root: Path, name: str) -> None:
    data = _read(root)
    connectors = data.get("connectors") if isinstance(data.get("connectors"), dict) else {}
    if name not in connectors:
        raise ConnectorError(404, f"没有连接器 {name}")
    connectors.pop(name)
    data["connectors"] = connectors
    _write(root, data)
    _forget_secret(root, name)


def transport_for(root: Path, entry: dict):
    """A fastmcp transport for a connector in the agent's legacy shape (sections.mcp_from_connector)."""
    import os
    from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport
    if "url" not in entry:
        command = entry["command"]
        return StdioTransport(command=command[0], args=command[1:], cwd=str((Path(root) / entry.get("cwd", ".")).resolve()),
                              env={**os.environ, **entry.get("env", {})})
    token = None
    headers = None
    if entry.get("token_env"):
        token = os.environ.get(entry["token_env"])
    if entry.get("token_secret"):
        token = read_secret(_config(root), entry["token_secret"], min_length=1)
    if entry.get("headers_secret"):
        headers = json.loads(secret_path(_config(root), entry["headers_secret"]).read_text(encoding="utf-8"))
    if entry.get("transport") == "sse":
        return SSETransport(entry["url"], auth=token, headers=headers)
    return StreamableHttpTransport(entry["url"], auth=token, headers=headers)


async def test(root: Path, name: str) -> dict:
    from fastmcp import Client
    from .sections import mcp_from_connector
    connector = (_read(root).get("connectors") or {}).get(name)
    if not isinstance(connector, dict):
        raise ConnectorError(404, f"没有连接器 {name}")
    entry = mcp_from_connector(connector)
    if entry is None:
        raise ConnectorError(400, "这个连接器的配置不完整")
    try:
        async with asyncio.timeout(20):
            async with Client(transport_for(root, entry)) as client:
                tools = await client.list_tools()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return {"ok": True, "tools": [tool.name for tool in tools][:200]}
