"""The console's CLI config editor (agent_nexus/agents/cli_config.py and /api/cli-config*)."""
from __future__ import annotations

import json
import sys
import types

import pytest

from agent_nexus import integrate
from agent_nexus.agents import cli_config, mcp_link
from agent_nexus.local_mcp import LocalMcp

from test_agents_web import WRITE, local  # noqa: F401 - the fixture


def env_for(home):
    return {"HOME": str(home), "USERPROFILE": str(home), "PATH": ""}


def test_save_keeps_crlf_and_bom_and_backs_up(tmp_path):
    env = env_for(tmp_path)
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_bytes(b"\xef\xbb\xbfmodel = \"a\"\r\n[x]\r\ny = 1\r\n")
    info = cli_config.read("codex", env)
    assert info["text"] == 'model = "a"\n[x]\ny = 1\n' and info["newline"] == "\r\n" and info["bom"]
    result = cli_config.save("codex", info["text"].replace('"a"', '"b"'), info["sha256"], env, now=1_700_000_000)
    assert result["ok"] and result["changed"]
    assert path.read_bytes() == b"\xef\xbb\xbfmodel = \"b\"\r\n[x]\r\ny = 1\r\n"
    assert ".anx-edit-" in result["backup"]
    listed = cli_config.backups("codex", env)["backups"]
    assert [row["source"] for row in listed] == ["edit"]
    # unchanged text: no write, no backup
    again = cli_config.read("codex", env)
    assert cli_config.save("codex", again["text"], again["sha256"], env)["changed"] is False


def test_stale_invalid_and_restore(tmp_path):
    env = env_for(tmp_path)
    path = tmp_path / ".gemini" / "settings.json"
    path.parent.mkdir()
    path.write_text('{"a": 1}\n')
    info = cli_config.read("gemini", env)
    with pytest.raises(cli_config.ConfigError) as stale:
        cli_config.save("gemini", '{"a": 2}\n', "0" * 64, env)
    assert stale.value.code == "stale"
    with pytest.raises(cli_config.ConfigError) as invalid:
        cli_config.save("gemini", '{"a": }', info["sha256"], env)
    assert invalid.value.code == "invalid" and invalid.value.extra["line"] == 1
    assert cli_config.validate("gemini", '{"a": 1 // why\n}')["warning"]  # Gemini reads comments: only a warning
    assert not cli_config.validate("claude_settings", '{"a": 1 // why\n}')["ok"]
    assert not cli_config.validate("claude_settings", "[1]")["ok"]
    saved = cli_config.save("gemini", '{"a": 2}\n', info["sha256"], env, now=1_700_000_000)
    backup = saved["backup"].rsplit("/", 1)[-1]
    restored = cli_config.restore("gemini", backup, saved["sha256"], env)
    assert restored["ok"] and path.read_text() == '{"a": 1}\n'
    for bad in ("../settings.json", "settings.json", "settings.json.anx-edit-x", "other.anx-edit-20240101-000000"):
        with pytest.raises(cli_config.ConfigError):
            cli_config.restore("gemini", bad, restored["sha256"], env)


def test_link_writes_never_prune_editor_backups(tmp_path):
    env = env_for(tmp_path)
    path = tmp_path / ".gemini" / "settings.json"
    path.parent.mkdir()
    path.write_text("{}\n")
    info = cli_config.read("gemini", env)
    cli_config.save("gemini", '{"theme": "x"}\n', info["sha256"], env, now=1_600_000_000)
    for n in range(4):
        mcp_link.write("gemini", f"http://127.0.0.1:{8000 + n}/mcp", home=tmp_path, env=env, now=1_700_000_000 + n)
    names = [row["name"] for row in cli_config.backups("gemini", env)["backups"]]
    assert sum(".anx-edit-" in name for name in names) == 1 and sum(".anx-backup-" in name for name in names) == 2
    assert integrate._backup_path(path, 0, integrate.EDIT_TAG).name.startswith("settings.json.anx-edit-")


def test_the_child_gets_the_text_on_stdin_only(tmp_path):
    seen = {}

    def runner(argv, env, *, run_as, timeout, stdin=None):
        seen.update(argv=argv, stdin=stdin)
        return 0, json.dumps({"ok": True, "changed": True}) + "\n", ""
    account = types.SimpleNamespace(env=lambda: {}, popen_args=lambda: {"user": 1000})
    secret = 'token = "SECRET"\n'
    cli_config.run_as(account, "save", "codex", text=secret, base_sha256="ab", runner=runner)
    assert seen["stdin"] == secret.encode() and not any("SECRET" in part for part in seen["argv"])
    assert seen["argv"][4:] == ["mcp", "config", "save", "--file", "codex", "--json", "--base-sha256", "ab"]


@pytest.mark.skipif(sys.platform == "win32", reason="the fake CLIs are /bin/sh shims")
def test_the_api_needs_a_trusted_account_and_round_trips(local, monkeypatch):
    client, _root, home = local
    refused = client.get("/api/cli-config")
    assert refused.status_code == 403 and refused.json()["code"] == "forbidden_account"
    monkeypatch.setattr(LocalMcp, "peer_allowed", lambda self, scope: True)
    files = client.get("/api/cli-config").json()["files"]
    assert {row["id"] for row in files} == set(cli_config.FILES)
    assert next(row for row in files if row["id"] == "claude_json")["readonly_default"] is True
    (home / ".claude").mkdir(exist_ok=True)
    loaded = client.get("/api/cli-config/file", params={"id": "claude_settings"}).json()
    assert loaded["exists"] is False and loaded["sha256"] is None
    preview = client.post("/api/cli-config/preview", json={"id": "claude_settings", "text": '{"x": 1}\n',
                                                           "base_sha256": None}, headers=WRITE).json()
    assert preview["valid"] and preview["changed"] and '+{"x": 1}' in preview["diff"] and not preview["stale"]
    bad = client.put("/api/cli-config/file", json={"id": "claude_settings", "text": "{", "base_sha256": None},
                     headers=WRITE)
    assert bad.status_code == 400 and bad.json()["code"] == "invalid"
    saved = client.put("/api/cli-config/file", json={"id": "claude_settings", "text": '{"x": 1}\n',
                                                     "base_sha256": None}, headers=WRITE).json()
    assert saved["ok"] and (home / ".claude" / "settings.json").read_text() == '{"x": 1}\n'
    stale = client.put("/api/cli-config/file", json={"id": "claude_settings", "text": "{}", "base_sha256": None},
                       headers=WRITE)
    assert stale.status_code == 409
    second = client.put("/api/cli-config/file", json={"id": "claude_settings", "text": '{"x": 2}\n',
                                                      "base_sha256": saved["sha256"]}, headers=WRITE).json()
    backups = client.get("/api/cli-config/backups", params={"id": "claude_settings"}).json()["backups"]
    assert len(backups) == 1
    preview = client.post("/api/cli-config/preview", json={"id": "claude_settings", "backup": backups[0]["name"],
                                                           "base_sha256": second["sha256"]}, headers=WRITE).json()
    assert '-{"x": 2}' in preview["diff"]
    restored = client.post("/api/cli-config/restore", json={"id": "claude_settings", "backup": backups[0]["name"],
                                                            "base_sha256": second["sha256"]}, headers=WRITE)
    assert restored.status_code == 200 and (home / ".claude" / "settings.json").read_text() == '{"x": 1}\n'
    assert client.get("/api/cli-config/file", params={"id": "../etc"}).status_code == 404
