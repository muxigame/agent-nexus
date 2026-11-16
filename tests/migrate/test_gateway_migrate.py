"""Milestone M3 (docs/design.md section 4.3): ``anx-gateway migrate``.

A synthetic ``/etc/mcpmanager`` + ``/var/lib/mcpmanager`` pair in a temporary
directory is copied to the new layout; the old files stay identical, the
copied ``gateway.json`` passes ``--check-config``, the OAuth database arrives
through an sqlite backup, and a second run changes nothing.
"""
from __future__ import annotations

import json
import sqlite3
import types
from pathlib import Path

from agent_nexus import cloud, migrate

from m3_fixtures import snapshot, write

TOKEN = "fixture-gateway-agent-token-000000000000000000"
SERVER = "fixture-gateway-server-token-00000000000000000"


def legacy_gateway(tmp_path):
    etc, state = tmp_path / "etc-mcpmanager", tmp_path / "var-lib-mcpmanager"
    write(etc / "cloud.json", {
        "host": "127.0.0.1", "port": 8440, "public_url": "https://hub.example.test",
        "token_env": "MCPM_SERVER_TOKEN", "agents": {"pc-main": "MCPM_PC_MAIN_TOKEN"},
        "media_cache_dir": "/var/lib/mcpmanager/media", "future_key": {"kept": True},
        "auth_note": {"store": "/var/lib/mcpmanager/oauth.sqlite3"},
    })
    write(etc / "server.env", f"MCPM_SERVER_TOKEN={SERVER}\nMCPM_PC_MAIN_TOKEN={TOKEN}\n")
    state.mkdir(parents=True)
    with sqlite3.connect(state / "oauth.sqlite3") as connection:
        connection.execute("create table clients (id text)")
        connection.execute("insert into clients values ('chatgpt')")
    write(state / "media" / "abc.bin", b"\x00\x01")
    return etc, state


def test_gateway_migration(tmp_path, capsys, monkeypatch):
    etc, state = legacy_gateway(tmp_path)
    before = snapshot(etc) | {f"state/{k}": v for k, v in snapshot(state).items()}
    to, state_to = tmp_path / "etc-agent-nexus", tmp_path / "var-lib-agent-nexus"
    argv = ["migrate", "--from", str(etc), "--state", str(state), "--to", str(to), "--state-to", str(state_to)]

    assert cloud.main(argv) == 0  # dry run
    assert not to.exists() and not state_to.exists()
    assert cloud.main([*argv, "--apply"]) == 0
    output = capsys.readouterr().out
    assert TOKEN not in output and SERVER not in output

    gateway = json.loads((to / "gateway.json").read_text(encoding="utf-8"))
    assert gateway["media_cache_dir"] == f"{state_to}/media"
    assert gateway["auth_note"]["store"] == f"{state_to}/oauth.sqlite3"
    assert gateway["token_env"] == "MCPM_SERVER_TOKEN"  # variable names are data
    assert gateway["agents"] == {"pc-main": "MCPM_PC_MAIN_TOKEN"}
    assert gateway["future_key"] == {"kept": True}
    assert gateway["mailbox"]["enabled"] is True
    assert (to / "gateway.env").read_bytes() == (etc / "server.env").read_bytes()
    with sqlite3.connect(state_to / "oauth.sqlite3") as connection:
        assert connection.execute("select id from clients").fetchall() == [("chatgpt",)]
    assert (state_to / "media/abc.bin").read_bytes() == b"\x00\x01"

    after = snapshot(etc) | {f"state/{k}": v for k, v in snapshot(state).items()}
    assert after == before

    # the copied configuration is accepted by the gateway's own check
    monkeypatch.setenv("MCPM_SERVER_TOKEN", SERVER)
    monkeypatch.setenv("MCPM_PC_MAIN_TOKEN", TOKEN)
    assert cloud.main(["--config", str(to / "gateway.json"), "--check-config"]) == 0

    # idempotent
    again = migrate.plan_gateway_migration(etc, state, to, state_to)
    assert not again.pending and not again.conflicts


def test_gateway_conflict_is_not_overwritten(tmp_path):
    etc, state = legacy_gateway(tmp_path)
    to = tmp_path / "etc-agent-nexus"
    write(to / "gateway.env", "SOMETHING=else\n")
    assert cloud.main(["migrate", "--from", str(etc), "--state", str(state), "--to", str(to),
                       "--state-to", str(tmp_path / "state-to"), "--apply"]) == 3
    assert (to / "gateway.env").read_text(encoding="utf-8") == "SOMETHING=else\n"
    assert (to / "gateway.json").is_file()


def test_standard_paths_are_rewritten():
    data = {"auth": {"store": "/var/lib/mcpmanager/oauth.sqlite3"}, "media_cache_dir": "/var/lib/mcpmanager/media",
            "other": "/var/lib/mcpmanagerx/keep"}
    rewritten = migrate._replace_prefix(data, migrate.OLD_STATE, migrate.NEW_STATE)
    assert rewritten == {"auth": {"store": "/var/lib/agent-nexus/oauth.sqlite3"},
                         "media_cache_dir": "/var/lib/agent-nexus/media", "other": "/var/lib/mcpmanagerx/keep"}


def test_gateway_secrets_are_copied_and_resolve(tmp_path, capsys, monkeypatch):
    """``auth.bearer_secrets`` / ``password_secret`` live in ``<config dir>/secrets``:
    the migrated config must find them there, or --check-config (and the gateway) fail."""
    etc, state = legacy_gateway(tmp_path)
    config = json.loads((etc / "cloud.json").read_text(encoding="utf-8"))
    config["auth"] = {"mode": "bearer", "bearer_secrets": ["workspace/bearer-token"],
                      "store": "/var/lib/mcpmanager/oauth.sqlite3"}
    write(etc / "cloud.json", config)
    bearer = "fixture-gateway-bearer-secret-000000000000000000"
    write(etc / "secrets" / "workspace" / "bearer-token", bearer + "\n")
    to, state_to = tmp_path / "etc-agent-nexus", tmp_path / "var-lib-agent-nexus"
    assert cloud.main(["migrate", "--from", str(etc), "--state", str(state), "--to", str(to),
                       "--state-to", str(state_to), "--apply"]) == 0
    assert bearer not in capsys.readouterr().out
    assert (to / "secrets" / "workspace" / "bearer-token").read_text(encoding="utf-8").strip() == bearer
    monkeypatch.setenv("MCPM_SERVER_TOKEN", SERVER)
    monkeypatch.setenv("MCPM_PC_MAIN_TOKEN", TOKEN)
    assert cloud.main(["--config", str(to / "gateway.json"), "--check-config"]) == 0
    assert not migrate.plan_gateway_migration(etc, state, to, state_to).pending


def test_chown_tree_includes_created_directories(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(migrate, "os", types.SimpleNamespace(name="posix"))
    monkeypatch.setattr(migrate.shutil, "chown", lambda path, user, group: calls.append(Path(path).resolve()))
    etc, var = tmp_path / "etc", tmp_path / "var"
    files = [etc / "gateway.json", etc / "secrets" / "workspace" / "bearer-token", var / "media" / "a.bin"]
    for path in files:
        write(path, "x")
    migrate.chown_tree(files, "agent-nexus", (etc, var))
    expected = {path.resolve() for path in files} | {(etc / "secrets").resolve(), (etc / "secrets" / "workspace").resolve(),
                                                     (var / "media").resolve(), var.resolve()}
    assert set(calls) == expected  # the config directory itself is left alone
