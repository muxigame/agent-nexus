"""The approval MCP server Claude Code asks before a tool runs (``--permission-prompt-tool mcp__anx__approve``).

A tiny stdio MCP server with one tool, ``approve``. Each call is POSTed to the loopback endpoint of the
Agent Nexus process that started the run (``--url``), with the run's one-time token; that process shows an
approval card in the chat and answers once the owner decides (or denies after 10 minutes). The answer goes
back to Claude Code as its permission-prompt result::

    {"behavior": "allow", "updatedInput": {...}}   |   {"behavior": "deny", "message": "..."}

Run as ``python -m agent_nexus.agents.approve_mcp --url http://127.0.0.1:<port>/approve [--token <t>]``, or
as a plain script (the runner copies this file into the run's temp folder, so the run-as account can read
it). Standard library only; the token is read from ``ANX_APPROVE_TOKEN`` when ``--token`` is not given.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request

PROTOCOL = "2025-06-18"
TIMEOUT_S = 11 * 60  # the endpoint itself denies after 10 minutes
TOOL = {
    "name": "approve",
    "description": "Ask the Agent Nexus owner to allow or deny a tool call.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string", "description": "The tool that wants to run"},
            "input": {"type": "object", "description": "The tool's input"},
            "tool_use_id": {"type": "string"},
        },
        "required": ["tool_name", "input"],
    },
}

_write_lock = threading.Lock()


def _send(obj: dict) -> None:
    data = json.dumps(obj, ensure_ascii=False) + "\n"
    with _write_lock:
        sys.stdout.write(data)
        sys.stdout.flush()


def decide(url: str, token: str, arguments: dict, *, timeout: float = TIMEOUT_S) -> dict:
    """POST one request to the endpoint; a deny decision when it cannot be reached or answers badly."""
    data = arguments.get("input")
    body = json.dumps({"token": token, "tool_name": str(arguments.get("tool_name") or ""),
                       "input": data if isinstance(data, dict) else {},
                       "tool_use_id": str(arguments.get("tool_use_id") or "")}).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    # Loopback only: never through a proxy from the user's environment.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            answer = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8") or "{}").get("error")
        except (ValueError, OSError):
            detail = None
        return {"behavior": "deny", "message": f"审批服务拒绝了请求：{detail or exc.code}"}
    except (OSError, ValueError) as exc:
        return {"behavior": "deny", "message": f"无法连接 Agent Nexus 审批服务：{exc}"}
    if isinstance(answer, dict) and answer.get("behavior") == "allow":
        updated = answer.get("updatedInput")
        return {"behavior": "allow", "updatedInput": updated if isinstance(updated, dict) else
                (data if isinstance(data, dict) else {})}
    message = answer.get("message") if isinstance(answer, dict) else None
    return {"behavior": "deny", "message": str(message or "用户拒绝了这个操作")}


def _call_tool(url: str, token: str, rpc_id, params: dict) -> None:
    if params.get("name") != "approve":
        _send({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32602, "message": "unknown tool"}})
        return
    arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    decision = decide(url, token, arguments)
    _send({"jsonrpc": "2.0", "id": rpc_id,
           "result": {"content": [{"type": "text", "text": json.dumps(decision, ensure_ascii=False)}]}})


def handle(message: dict, url: str, token: str) -> None:
    method, rpc_id = message.get("method"), message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if rpc_id is None:
        return  # notifications (initialized, cancelled) need no answer
    if method == "initialize":
        version = params.get("protocolVersion") if isinstance(params.get("protocolVersion"), str) else PROTOCOL
        _send({"jsonrpc": "2.0", "id": rpc_id, "result": {
            "protocolVersion": version, "capabilities": {"tools": {}},
            "serverInfo": {"name": "anx-approve", "version": "1.0"}}})
    elif method == "ping":
        _send({"jsonrpc": "2.0", "id": rpc_id, "result": {}})
    elif method == "tools/list":
        _send({"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": [TOOL]}})
    elif method == "tools/call":
        threading.Thread(target=_call_tool, args=(url, token, rpc_id, params), daemon=True).start()
    else:
        _send({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": f"method not found: {method}"}})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="approve_mcp", description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--token", default="")
    args = parser.parse_args(argv)
    token = args.token or os.environ.get("ANX_APPROVE_TOKEN", "")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        if isinstance(message, dict):
            handle(message, args.url, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
