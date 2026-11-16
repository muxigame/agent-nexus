"""Milestone M3 (docs/design.md sections 4.2-4.4, 13): ``anx migrate``.

Acceptance: after migrating a legacy root fixture ``plugins.device``,
``connectors`` and ``{"$secret"}`` are right; **every file of the old root
keeps its sha256 and mtime**; a second run changes nothing; a target that
exists with other content is never overwritten; no output contains a secret
value; a private-edition root keeps its identity (section 12.7 item 6).

Everything runs on synthetic fixtures in temporary directories.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from agent_nexus import cli, migrate, paths, sections
from agent_nexus.registry import discover, load_manager_config, save_override

from m3_fixtures import FAKE_PASSWORD, FAKE_TOKENS, SECRET_VALUES, legacy_config, make_legacy_root, snapshot, write


def run_cli(capsys, *argv) -> tuple[int, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out + out.err


def assert_no_secrets(text: str) -> None:
    for value in SECRET_VALUES:
        assert value not in text


@pytest.fixture
def legacy(tmp_path):
    return make_legacy_root(tmp_path)


def migrated(tmp_path, legacy, **options):
    target = tmp_path / "new-root"
    plan = migrate.plan_migration(legacy, target, **options)
    migrate.apply_migration(plan)
    return target, plan


def test_dry_run_writes_nothing(tmp_path, legacy, capsys):
    before = snapshot(legacy)
    target = tmp_path / "new-root"
    code, output = run_cli(capsys, "migrate", "--from", str(legacy), "--to", str(target))
    assert code == 0
    assert "dry run" in output and "agent-nexus.json" in output
    assert not target.exists()
    assert snapshot(legacy) == before
    assert_no_secrets(output)


def test_migrated_configuration(tmp_path, legacy):
    target, plan = migrated(tmp_path, legacy)
    data = json.loads((target / "agent-nexus.json").read_text(encoding="utf-8"))
    assert "services" not in data and "scan" not in data
    plugins = data["plugins"]
    assert "workspace" not in plugins
    assert plugins["device"]["settings"] == legacy_config()["services"]["workspace"]["settings"]
    assert plugins["device"]["auth"] == {"mode": "bearer", "bearer_secrets": ["workspace/bearer-token"]}
    assert "install" not in plugins["device"]
    # plain password -> secret reference + secret file
    assert plugins["jubian-go"]["settings"]["password"] == {"$secret": "jubian-go/password"}
    assert plugins["jubian-go"]["settings"]["username"] == "someone"
    assert (target / "secrets/jubian-go/password").read_text(encoding="utf-8").strip() == FAKE_PASSWORD
    # old plugin code stays where it is: link plugins, with the repository to install later
    assert plugins["jubian-go"]["install"] == {
        "state": "linked", "path": str((legacy / "builtins/jubian-go-mcp").resolve()),
        "source": "https://github.com/muxigame/anx-plugin-jubian-go.git"}
    assert plugins["payment"]["install"] == {"state": "pending_install",
                                             "source": "https://github.com/muxigame/anx-plugin-payment.git"}
    lock = json.loads((target / "plugins/plugins.lock.json").read_text(encoding="utf-8"))
    assert set(lock) == {"jubian-go", "ones"}
    assert lock["jubian-go"] == {"mode": "link", "path": str((legacy / "builtins/jubian-go-mcp").resolve()),
                                 "version": "0.1.0"}
    # cloud: identity kept, workspace -> device, mcps -> connectors, paths made absolute
    cloud = data["cloud"]
    assert (cloud["agent_id"], cloud["url"], cloud["token_secret"]) == ("pc-main", "wss://hub.example.test",
                                                                         "cloud/agent-pc-main")
    assert cloud["workspaces"] == {"default": {"path": str((legacy / "work").resolve()), "mcp": "device"}}
    assert "mcps" not in cloud
    assert data["connectors"] == {
        "local-http": {"transport": "http", "url": "http://127.0.0.1:8500/mcp",
                       "auth": {"type": "bearer", "token_env": "EXTRA_MCP_TOKEN"}},
        "local-stdio": {"transport": "stdio", "command": ["node", "server.js"],
                        "cwd": str((legacy / "external/my-mcp").resolve()), "env": {}},
    }
    assert data["plugin_paths"] == [str((legacy / "external").resolve())]
    assert data["manager"] == {"host": "127.0.0.1", "port": 8399} and data["builtins"] is True
    raw = (legacy / "mcp-manager.json").read_bytes()
    assert data["migrated_from"]["path"] == str((legacy / "mcp-manager.json").resolve())
    assert data["migrated_from"]["sha256"] == hashlib.sha256(raw).hexdigest()


def test_files_copied_and_left_behind(tmp_path, legacy):
    target, _ = migrated(tmp_path, legacy)
    for rel, value in FAKE_TOKENS.items():
        assert (target / rel).read_text(encoding="utf-8").strip() == value
    # workspace/* is also device/*
    assert (target / "secrets/device/bearer-token").read_bytes() == (legacy / "secrets/workspace/bearer-token").read_bytes()
    assert (target / "secrets/device/oauth.sqlite3").is_file()
    assert (target / ".anx/state-console.json").is_file()
    assert not (target / ".anx/run").exists()
    assert not (target / ".anx/mcp-manager.before-cloud.json").exists()
    database = target / "plugin-data/device/watchers.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute("select name from watch").fetchall() == [("build",)]
    assert not (target / "plugin-data/device/audit.jsonl").exists()
    for rel in ("builtins", "mcpm", ".venv", "tools", ".private", "mcp-manager.json"):
        assert not (target / rel).exists(), rel
    report = (target / ".anx/migration-report.json").read_text(encoding="utf-8")
    assert_no_secrets(report)
    items = json.loads(report)["items"]
    # digests only for non-secret files
    assert all("sha256" not in item for item in items if "secret" in item["kind"])
    assert any(item["kind"] == "config" and item.get("sha256") for item in items)


def test_watchers_database_is_copied_with_the_online_backup_api(tmp_path, legacy):
    """A running device plugin keeps recent rows in -wal; a file copy would lose them."""
    database = legacy / "builtins/codex-mcp/logs/watchers.sqlite3"
    live = sqlite3.connect(database)
    try:
        live.execute("pragma wal_autocheckpoint=0")
        live.execute("insert into watch (name) values ('only-in-wal')")
        live.commit()
        assert database.with_name(database.name + "-wal").stat().st_size > 0
        target, plan = migrated(tmp_path, legacy)
    finally:
        live.close()
    [item] = [item for item in plan.items if item.kind == "device data"]
    assert item.note == "sqlite backup" and item.copier is migrate._sqlite_backup
    copied = target / "plugin-data/device"
    assert sorted(p.name for p in copied.iterdir()) == ["watchers.sqlite3"]  # no -wal / -shm carried over
    with sqlite3.connect(copied / "watchers.sqlite3") as connection:
        assert connection.execute("select name from watch order by id").fetchall() == [("build",), ("only-in-wal",)]
    # a second plan sees the same content as "same", although the bytes differ
    again = migrate.plan_migration(legacy, target)
    assert [i.op for i in again.items if i.kind == "device data"] == ["same"]


def test_closed_wal_database_leaves_no_side_files(tmp_path, legacy):
    logs = legacy / "builtins/codex-mcp/logs"
    before = sorted(p.name for p in logs.iterdir())
    migrated(tmp_path, legacy)
    assert sorted(p.name for p in logs.iterdir()) == before


def test_old_root_is_byte_and_mtime_identical(tmp_path, legacy, capsys):
    before = snapshot(legacy)
    target = tmp_path / "new-root"
    code, output = run_cli(capsys, "migrate", "--from", str(legacy), "--to", str(target), "--apply")
    assert code == 0, output
    assert snapshot(legacy) == before
    assert_no_secrets(output)


def test_second_run_changes_nothing(tmp_path, legacy, capsys):
    target, _ = migrated(tmp_path, legacy)
    report = target / ".anx/migration-report.json"
    first = {k: v for k, v in snapshot(target).items() if k != report.relative_to(target).as_posix()}
    again = migrate.plan_migration(legacy, target)
    assert not again.pending and not again.conflicts
    assert {item.op for item in again.items} <= {"same", "skip"}
    code, output = run_cli(capsys, "migrate", "--from", str(legacy), "--to", str(target), "--apply")
    assert code == 0, output
    after = {k: v for k, v in snapshot(target).items() if k != report.relative_to(target).as_posix()}
    assert after == first


def test_existing_different_target_is_never_overwritten(tmp_path, legacy, capsys):
    target = tmp_path / "new-root"
    write(target / "secrets/ones/token", "someone-else-put-this-here\n")
    write(target / "secrets/jubian-go/password", "a-different-password\n")
    code, output = run_cli(capsys, "migrate", "--from", str(legacy), "--to", str(target), "--apply")
    assert code == 3
    assert "conflict" in output
    assert (target / "secrets/ones/token").read_text(encoding="utf-8") == "someone-else-put-this-here\n"
    assert (target / "secrets/jubian-go/password").read_text(encoding="utf-8") == "a-different-password\n"
    # the value it could not move stays where it was rather than pointing at the wrong secret
    data = json.loads((target / "agent-nexus.json").read_text(encoding="utf-8"))
    assert data["plugins"]["jubian-go"]["settings"]["password"] == FAKE_PASSWORD
    assert_no_secrets(output)
    assert "a-different-password" not in output and "someone-else" not in output


def test_edited_target_config_is_a_conflict(tmp_path, legacy):
    target, _ = migrated(tmp_path, legacy)
    config = target / "agent-nexus.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    data["manager"]["port"] = 9999
    config.write_text(json.dumps(data), encoding="utf-8")
    plan = migrate.plan_migration(legacy, target)
    assert [item.kind for item in plan.conflicts] == ["config"]
    migrate.apply_migration(plan)
    assert json.loads(config.read_text(encoding="utf-8"))["manager"]["port"] == 9999


def test_in_place_keeps_legacy_file_and_new_file_wins(tmp_path, legacy, capsys):
    before = snapshot(legacy)
    code, output = run_cli(capsys, "--root", str(legacy), "migrate", "--in-place", "--apply")
    assert code == 0, output
    after = snapshot(legacy)
    assert {k: after[k] for k in before} == before  # every old file untouched
    new_files = set(after) - set(before)
    assert "agent-nexus.json" in new_files and "secrets/device/bearer-token" in new_files
    assert "secrets/jubian-go/password" in new_files
    assert not any(name.startswith("secrets/ones") for name in new_files)  # nothing copied onto itself
    assert paths.config_path(legacy) == legacy / "agent-nexus.json"
    data = json.loads((legacy / "agent-nexus.json").read_text(encoding="utf-8"))
    assert data["plugin_paths"] == ["external"]  # relative paths stay relative in place
    assert data["cloud"]["workspaces"]["default"]["path"] == "work"
    cfg = load_manager_config(legacy)
    assert cfg.overrides["device"]["settings"]["default_workdir"] == "work"
    # the second in-place run is a no-op too
    code, _ = run_cli(capsys, "--root", str(legacy), "migrate", "--in-place", "--apply")
    assert code == 0
    assert {k: v for k, v in snapshot(legacy).items() if k != ".anx/migration-report.json"} == \
           {k: v for k, v in after.items() if k != ".anx/migration-report.json"}


def test_port_offset_agent_id_cloud_url(tmp_path, legacy):
    target, _ = migrated(tmp_path, legacy, port_offset=10, agent_id="pc-next", cloud_url="wss://trial.example.test")
    data = json.loads((target / "agent-nexus.json").read_text(encoding="utf-8"))
    assert data["manager"]["port"] == 8409
    assert data["plugins"]["device"]["port"] == 8410  # manifest default 8400
    assert data["plugins"]["jubian-go"]["port"] == 8415
    assert data["plugins"]["ones"]["port"] == 8412
    assert data["cloud"]["agent_id"] == "pc-next" and data["cloud"]["url"] == "wss://trial.example.test"
    assert data["cloud"]["token_secret"] == "cloud/agent-pc-main"


def test_private_edition_identity_survives(tmp_path):
    from agent_nexus.enrollment import enroll_if_needed

    legacy = make_legacy_root(tmp_path, private=True)
    before = snapshot(legacy)
    target, plan = migrated(tmp_path, legacy)
    data = json.loads((target / "agent-nexus.json").read_text(encoding="utf-8"))
    old = legacy_config(private=True)["cloud"]
    assert data["cloud"]["agent_id"] == old["agent_id"]
    assert data["cloud"]["token_secret"] == "cloud/enrolled-agent"
    assert data["cloud"]["enabled"] is True
    assert (target / "secrets/cloud/enrolled-agent").read_bytes() == (legacy / "secrets/cloud/enrolled-agent").read_bytes()
    assert not (target / ".private").exists()
    assert any(item.kind == "enrollment profile" and item.op == "skip" for item in plan.items)
    assert enroll_if_needed(target) is False  # nothing to enroll with, and nothing changes
    assert json.loads((target / "agent-nexus.json").read_text(encoding="utf-8")) == data
    assert snapshot(legacy) == before


def test_registry_and_mcpkit_read_the_migrated_root(tmp_path, legacy):
    from mcpkit.config import load

    target, _ = migrated(tmp_path, legacy)
    specs = discover(load_manager_config(target))
    # link plugins from the lock file are discovered where they are
    assert specs["jubian-go"].dir == (legacy / "builtins/jubian-go-mcp").resolve()
    assert specs["ones"].source == "manifest+config"
    # mcpkit 0.2: plugins.<name> and {"$secret"} references
    config = load(legacy / "builtins/jubian-go-mcp", root=target)
    assert config.settings["password"] == FAKE_PASSWORD
    assert config.settings["username"] == "someone"
    # the console keeps writing into the plugins section
    save_override(load_manager_config(target), "ones", {"autostart": True})
    data = json.loads((target / "agent-nexus.json").read_text(encoding="utf-8"))
    assert data["plugins"]["ones"]["autostart"] is True and "services" not in data


def test_agent_reads_connectors_and_plugins(tmp_path, legacy):
    from agent_nexus.agent import LocalGateway

    target, _ = migrated(tmp_path, legacy)
    data = paths.read_config(target)
    assert sections.extra_mcps(data)["local-http"] == {"url": "http://127.0.0.1:8500/mcp", "transport": "http",
                                                       "token_env": "EXTRA_MCP_TOKEN"}
    gateway = LocalGateway(target, data["cloud"])
    workspaces = gateway.configured_workspaces()
    assert workspaces["default"]["mcp"] == "device"


def test_export_legacy_round_trip(tmp_path, legacy, capsys):
    target, _ = migrated(tmp_path, legacy)
    out = tmp_path / "exported" / "mcp-manager.json"
    code, output = run_cli(capsys, "--root", str(target), "migrate", "--export-legacy", str(out))
    assert code == 0, output
    exported = json.loads(out.read_text(encoding="utf-8"))
    assert "workspace" in exported["services"] and "device" not in exported["services"]
    assert exported["services"]["jubian-go"]["settings"]["password"] == FAKE_PASSWORD
    assert exported["cloud"]["workspaces"]["default"]["mcp"] == "workspace"
    assert set(exported["cloud"]["mcps"]) == {"local-http", "local-stdio"}
    assert_no_secrets(output)
    code, output = run_cli(capsys, "--root", str(target), "migrate", "--export-legacy", str(out))
    assert code == 2 and "exists" in output


def test_from_file_uses_a_vault_copy(tmp_path):
    source = write(tmp_path / "vault/root/mcp-manager.json", legacy_config())
    target = tmp_path / "fresh"
    plan = migrate.plan_migration(source.parent, target, source_file=source)
    migrate.apply_migration(plan)
    data = json.loads((target / "agent-nexus.json").read_text(encoding="utf-8"))
    assert data["migrated_from"]["path"] == str(source.resolve())
    assert "device" in data["plugins"]


def test_secret_key_rule():
    assert migrate.is_secret_setting("password")
    assert migrate.is_secret_setting("api_key") and migrate.is_secret_setting("client_secret")
    assert migrate.is_secret_setting("token")
    for key in ("password_secret", "token_secret", "bearer_secrets", "token_env", "access_token_ttl_s",
                "username", "base_url", "config_file"):
        assert not migrate.is_secret_setting(key), key
    assert migrate.is_secret_setting("pin", {"pin"})


def test_legacy_installed_root_is_copied_on_first_start(tmp_path, monkeypatch):
    import sys

    base = tmp_path / "data"
    legacy_dir = base / (paths.LEGACY_WINDOWS_DATA_DIR if sys.platform == "win32" else paths.LEGACY_LINUX_DATA_DIR)
    write(legacy_dir / "mcp-manager.json", {"manager": {"port": 8399}, "services": {"workspace": {"autostart": True}}})
    environ = {"ANX_DATA_HOME": str(base)}
    before = snapshot(legacy_dir)
    new = migrate.migrate_legacy_root(environ)
    assert new is not None and (new / "agent-nexus.json").is_file()
    assert json.loads((new / "agent-nexus.json").read_text(encoding="utf-8"))["plugins"]["device"]["autostart"] is True
    assert snapshot(legacy_dir) == before
    assert migrate.migrate_legacy_root(environ) is None  # once only
