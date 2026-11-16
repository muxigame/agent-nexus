"""Synthetic legacy roots and vaults for the M3 tests. Every value here is fake."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

#: Values that must never appear in any output, plan or report.
FAKE_PASSWORD = "fixture-plain-password-not-real"
FAKE_TOKENS = {
    "secrets/workspace/bearer-token": "fixture-workspace-bearer-token-0000000000000000",
    "secrets/workspace/oauth-password": "fixture-workspace-oauth-password-000000000000",
    "secrets/ones/token": "fixture-ones-token-00000000000000000000000000000",
    "secrets/cloud/agent-pc-main": "fixture-cloud-agent-token-000000000000000000000",
    "secrets/cloud-agent-pc-main.env": "ANX_AGENT_TOKEN=fixture-env-token-000000000000000000000",
}
SECRET_VALUES = [FAKE_PASSWORD, *FAKE_TOKENS.values()]


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, (dict, list)):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elif isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    return path


def legacy_config(*, private: bool = False) -> dict:
    cloud = {
        "enabled": True, "agent_id": "pc-main", "url": "wss://hub.example.test",
        "token_secret": "cloud/agent-pc-main",
        "workspaces": {"default": {"path": "work", "mcp": "workspace"}},
        "mcps": {
            "local-http": {"url": "http://127.0.0.1:8500/mcp", "token_env": "EXTRA_MCP_TOKEN"},
            "local-stdio": {"command": ["node", "server.js"], "cwd": "external/my-mcp", "env": {}},
        },
    }
    if private:
        cloud = {"enabled": True, "agent_id": "portable-0123456789abcdef", "url": "wss://hub.example.test",
                 "token_secret": "cloud/enrolled-agent",
                 "workspaces": {"home": {"path": "work", "mcp": "device"}}}
    return {
        "manager": {"host": "127.0.0.1", "port": 8399},
        "builtins": True,
        "scan": ["external"],
        "services": {
            "workspace": {
                "public_url": "https://device.example.test",
                "auth": {"mode": "bearer", "bearer_secrets": ["workspace/bearer-token"]},
                "settings": {"default_workdir": "work", "allow_dangerous": True, "limits": {"max_sessions": 8}},
            },
            "jubian-go": {"settings": {"base_url": "https://jubian.example.test", "username": "someone",
                                       "password": FAKE_PASSWORD, "timeout_s": 30.0}},
            "ones": {"settings": {"team_id": "team", "manhour_mode": "auto"}},
            "payment": {"settings": {"config_file": "config.json"}},
        },
        "cloud": cloud,
    }


def make_legacy_root(base: Path, *, private: bool = False) -> Path:
    root = base / "old-root"
    write(root / "mcp-manager.json", legacy_config(private=private))
    (root / "work").mkdir(parents=True, exist_ok=True)
    (root / "external" / "my-mcp").mkdir(parents=True, exist_ok=True)
    for rel, value in FAKE_TOKENS.items():
        write(root / rel, value + "\n")
    write(root / "secrets/workspace/oauth.sqlite3", b"SQLite format 3\x00 fake bytes")
    if private:
        write(root / "secrets/cloud/enrolled-agent", "fixture-enrolled-agent-token-00000000000000000\n")
        write(root / ".private/enrollment.json", {"url": "wss://hub.example.test", "token": "x" * 40})
        write(root / ".private/endpoint-allow", "wss://hub.example.test\n")
    write(root / ".mcp-manager/state-console.json", {"tab": "services"})
    write(root / ".mcp-manager/run/device.json", {"pid": 1})
    write(root / ".mcp-manager/mcp-manager.before-cloud.json", {"old": True})
    device = root / "builtins" / "codex-mcp"
    write(device / "mcp-service.json", {"name": "workspace", "command": ["uv", "run", "server"], "port": 8400})
    database = device / "logs" / "watchers.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.execute("pragma journal_mode=wal")  # as the device plugin opens it
    connection.execute("create table watch (id integer primary key, name text)")
    connection.execute("insert into watch (name) values ('build')")
    connection.commit()
    connection.close()
    write(device / "logs" / "audit.jsonl", '{"event": "x"}\n')
    write(root / "builtins/jubian-go-mcp/mcp-service.json",
          {"name": "jubian-go", "command": ["uv", "run", "server"], "port": 8405, "version": "0.1.0"})
    write(root / "builtins/ones-mcp/mcp-service.json", {"name": "ones", "command": ["uv", "run", "server"], "port": 8402})
    (root / "builtins/mcpkit").mkdir(parents=True, exist_ok=True)
    write(root / "mcpm/cli.py", "# runtime\n")
    write(root / ".venv/pyvenv.cfg", "home = x\n")
    write(root / "tools/uv.exe", b"MZ")
    return root


def snapshot(root: Path) -> dict[str, tuple[str, int]]:
    """sha256 and mtime (ns) of every file under ``root``."""
    result = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        result[path.relative_to(root).as_posix()] = (hashlib.sha256(path.read_bytes()).hexdigest(),
                                                     os.stat(path).st_mtime_ns)
    return result


VAULT_FILES = {
    "root/secrets/workspace/bearer-token": FAKE_TOKENS["secrets/workspace/bearer-token"] + "\n",
    "root/secrets/ones/token": FAKE_TOKENS["secrets/ones/token"] + "\n",
    "root/secrets/cloud/agent-pc-main": FAKE_TOKENS["secrets/cloud/agent-pc-main"] + "\n",
    "root/secrets/cloud-agent-pc-main.env": FAKE_TOKENS["secrets/cloud-agent-pc-main.env"] + "\n",
    "root/mcp-manager.json": json.dumps(legacy_config()),
    "root/mcp-manager.json.bak-20261108": json.dumps(legacy_config()),
    "root/.mcp-manager/mcp-manager.before-cloud.json": "{}",
    "repo/builtins/payment-mcp/config.json": '{"account": "fixture"}',
    "repo/builtins/payment-mcp/qrcodes/alipay.jpg": "not really a jpeg",
    "repo/builtins/codex-mcp/logs/oauth-store.json.bak-domain": "{}",
    "repo/secrets/build-enrollment.json": '{"url": "wss://hub.example.test", "token": "fixture"}',
    "root/云效apikey": "fixture-yunxiao-key-000000000000000000\n",
    "root/odd-file.txt": "no rule covers this\n",
}


def make_vault(base: Path, *, declared: int | None = None, extra_listed=(), unlisted=()) -> Path:
    vault = base / "vault"
    for rel, value in VAULT_FILES.items():
        write(vault / rel, value)
    for rel in unlisted:
        write(vault / rel, "unlisted\n")
    listed = [*VAULT_FILES, *extra_listed]
    count = len(listed) if declared is None else declared
    readme = ["# 凭据保险库", "", "## 目录对应关系", "", "| a | b |", "| --- | --- |", "| `repo/...` | x |", "",
              f"## 文件清单（{count} 个）", "", *[f"- `{rel}`" for rel in listed], "", "## 以后", "", "- `not/an/entry`"]
    write(vault / "README.md", "\n".join(readme) + "\n")
    return vault
