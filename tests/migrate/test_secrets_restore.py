"""Milestone M3 (docs/design.md section 6.9): ``anx secrets restore``.

Acceptance: the plan maps entry by entry onto the vault manifest (parsed from
the README, never a hard-coded count) and lists what no rule covers; a target
that exists with other content is never overwritten; no output carries a
secret value. A synthetic vault in a temporary directory stands in for the
real one.
"""
from __future__ import annotations

import pytest

from agent_nexus import cli, secrets_restore
from agent_nexus.secrets_restore import VaultError, parse_manifest, plan_restore

from m3_fixtures import VAULT_FILES, make_vault, snapshot, write

VAULT_VALUES = [v.strip() for k, v in VAULT_FILES.items() if "secrets" in k or "apikey" in k or "enrollment" in k]


def run_cli(capsys, *argv) -> tuple[int, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out + out.err


def assert_no_values(text: str) -> None:
    for value in VAULT_VALUES:
        assert value not in text


def no_plugins(_legacy_dir):
    return None


def test_manifest_list_is_parsed_and_count_mismatch_reported():
    text = "# v\n\n## 文件清单（38 个）\n\n- `root/a`\n- `root/b`\n- `repo/c`\n\n## next\n\n- `root/not-an-entry`\n"
    manifest = parse_manifest(text)
    assert manifest.entries == ["root/a", "root/b", "repo/c"]
    assert manifest.declared == 38
    assert any("says 38" in w and "lists 3" in w for w in manifest.warnings)
    assert parse_manifest("## 文件清单\n- `root/a`\n").warnings == []


@pytest.mark.parametrize("entry", ["../secrets/x", "/etc/passwd", "C:/x", "root/../x", "root\\x", "root//x"])
def test_unsafe_manifest_entries_are_refused(entry):
    with pytest.raises(VaultError):
        parse_manifest(f"## 文件清单\n- `{entry}`\n")


def test_manifest_without_list_is_an_error():
    with pytest.raises(VaultError):
        parse_manifest("# nothing here\n")


def test_plan_covers_every_manifest_entry(tmp_path):
    vault = make_vault(tmp_path, declared=38, extra_listed=["root/secrets/ones/gone"], unlisted=["root/stray.txt"])
    root = tmp_path / "root"
    plan = plan_restore(vault, root, plugin_dir=no_plugins)
    listed = set(VAULT_FILES) | {"root/secrets/ones/gone"}
    assert {item.ref for item in plan.items} == listed | {"root/stray.txt"}
    by_ref = {}
    for item in plan.items:
        by_ref.setdefault(item.ref, []).append(item)
    kind = {ref: sorted(i.kind if i.op == "skip" else i.op for i in items) for ref, items in by_ref.items()}
    assert kind["root/secrets/workspace/bearer-token"] == ["copy", "copy"]  # and again as device/*
    assert {i.dst.relative_to(root).as_posix() for i in by_ref["root/secrets/workspace/bearer-token"]} == \
           {"secrets/workspace/bearer-token", "secrets/device/bearer-token"}
    assert kind["root/secrets/cloud-agent-pc-main.env"] == ["copy"]
    assert by_ref["root/secrets/cloud-agent-pc-main.env"][0].dst == root / "secrets/cloud-agent-pc-main.env"
    assert kind["root/mcp-manager.json"] == ["migrate-input"]
    assert kind["root/mcp-manager.json.bak-20261108"] == ["migrate-input"]
    assert kind["root/.mcp-manager/mcp-manager.before-cloud.json"] == ["migrate-input"]
    assert kind["repo/secrets/build-enrollment.json"] == ["never"]
    assert kind["repo/builtins/payment-mcp/config.json"] == ["plugin-missing"]
    assert kind["repo/builtins/codex-mcp/logs/oauth-store.json.bak-domain"] == ["optional"]
    assert kind["root/云效apikey"] == ["manual"]
    assert kind["root/odd-file.txt"] == ["unmapped"]
    assert kind["root/secrets/ones/gone"] == ["missing"]
    assert kind["root/stray.txt"] == ["unlisted"]
    assert any("says 38" in w for w in plan.warnings)
    assert not root.exists()  # planning writes nothing


def test_dry_run_by_default_and_never_prints_values(tmp_path, capsys):
    vault = make_vault(tmp_path)
    root = tmp_path / "root"
    before = snapshot(vault)
    code, output = run_cli(capsys, "--root", str(root), "secrets", "restore", "--vault", str(vault))
    assert code == 0, output
    assert "dry run" in output and "unmapped" in output and "root/odd-file.txt" in output
    assert not root.exists()
    assert snapshot(vault) == before
    assert_no_values(output)


def test_apply_restores_and_is_idempotent(tmp_path, capsys):
    vault = make_vault(tmp_path)
    root = tmp_path / "root"
    vault_before = snapshot(vault)
    code, output = run_cli(capsys, "secrets", "restore", "--vault", str(vault), "--root", str(root), "--apply")
    assert code == 0, output
    assert_no_values(output)
    for rel in ("secrets/workspace/bearer-token", "secrets/device/bearer-token", "secrets/ones/token",
                "secrets/cloud/agent-pc-main", "secrets/cloud-agent-pc-main.env"):
        source = "root/" + rel.replace("secrets/device/", "secrets/workspace/")
        assert (root / rel).read_bytes() == (vault / source).read_bytes(), rel
    # never: the private build credential; not restored: config inputs, manual and optional items
    restored = set(snapshot(root))
    assert not any("enrollment" in name for name in restored)
    assert "mcp-manager.json" not in restored and not (root / "plugin-data").exists()
    assert snapshot(vault) == vault_before
    first = snapshot(root)
    plan = plan_restore(vault, root, plugin_dir=no_plugins)
    assert not plan.pending and not plan.conflicts
    code, _ = run_cli(capsys, "secrets", "restore", "--vault", str(vault), "--root", str(root), "--apply")
    assert code == 0 and snapshot(root) == first


def test_differing_target_is_a_conflict_and_kept(tmp_path, capsys):
    vault = make_vault(tmp_path)
    root = tmp_path / "root"
    write(root / "secrets/ones/token", "the-value-already-in-use\n")
    code, output = run_cli(capsys, "secrets", "restore", "--vault", str(vault), "--root", str(root), "--apply")
    assert code == 3
    assert "conflict" in output and "root/secrets/ones/token" in output
    assert (root / "secrets/ones/token").read_text(encoding="utf-8") == "the-value-already-in-use\n"
    assert (root / "secrets/workspace/bearer-token").is_file()  # the rest still restored
    assert_no_values(output)
    assert "the-value-already-in-use" not in output


def test_optional_items_and_installed_payment_plugin(tmp_path):
    vault = make_vault(tmp_path)
    root = tmp_path / "root"
    plugin = root / "plugins-src" / "payment"
    write(plugin / "mcp-service.json", {"name": "payment", "command": ["x"], "legacy": {"dirs": ["payment-mcp"]}})
    write(root / "agent-nexus.json", {"builtins": False, "plugin_paths": ["plugins-src"]})
    plan = secrets_restore.restore(vault, root, apply=True, include={"optional"})
    assert not plan.conflicts
    assert (plugin / "config.json").read_text(encoding="utf-8") == VAULT_FILES["repo/builtins/payment-mcp/config.json"]
    assert (plugin / "qrcodes/alipay.jpg").is_file()
    assert (root / "plugin-data/device/logs/oauth-store.json.bak-domain").is_file()


def test_payment_plugin_found_by_directory_name(tmp_path):
    root = tmp_path / "root"
    write(root / "builtins-x/payment-mcp/mcp-service.json", {"name": "payment", "command": ["x"]})
    write(root / "agent-nexus.json", {"builtins": False, "plugin_paths": ["builtins-x"]})
    find = secrets_restore.installed_plugin_dirs(root)
    assert find("payment-mcp") == (root / "builtins-x/payment-mcp").resolve()
    assert find("unity-mcp") is None


def test_root_inside_vault_is_refused(tmp_path):
    vault = make_vault(tmp_path)
    with pytest.raises(VaultError):
        plan_restore(vault, vault / "root", plugin_dir=no_plugins)


def test_secrets_set_from_file(tmp_path, capsys):
    root = tmp_path / "root"
    source = write(tmp_path / "key.txt", "fixture-connector-key-0000000000\n")
    code, output = run_cli(capsys, "secrets", "set", "connectors/yunxiao/key", "--from-file", str(source),
                           "--root", str(root))
    assert code == 0 and "fixture-connector-key" not in output
    assert (root / "secrets/connectors/yunxiao/key").read_text(encoding="utf-8").strip() == \
           "fixture-connector-key-0000000000"
    other = write(tmp_path / "other.txt", "another-value-000000000000000000\n")
    code, output = run_cli(capsys, "secrets", "set", "connectors/yunxiao/key", "--from-file", str(other),
                           "--root", str(root))
    assert code == 3 and "another-value" not in output
    assert (root / "secrets/connectors/yunxiao/key").read_text(encoding="utf-8").strip() == \
           "fixture-connector-key-0000000000"
