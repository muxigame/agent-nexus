"""Milestone M3 (docs/design.md section 4.5): plugins on an old mcpkit read the
configuration the console wrote, through the shadow root ``.anx/compat-root``.

``legacy_mcpkit.config`` is the baseline mcpkit module (commit f343399) frozen
byte for byte (``LEGACY_MCPKIT_PROVENANCE.json``). It only knows
``$MCPM_ROOT/mcp-manager.json`` and ``services.<name>``.
"""
from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path

import pytest

from agent_nexus import supervisor
from agent_nexus.plugins import compat_root
from agent_nexus.registry import load_manager_config, save_override
from agent_nexus.supervisor import Supervisor

COMPAT = Path(__file__).resolve().parent
PASSWORD = "fixture-legacy-plugin-password"
BEARER = "fixture-legacy-plugin-bearer-token-000000000000000"
OTHER_SECRET = "fixture-not-for-the-shadow-root-0000000000000000"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n" if not isinstance(data, str) else data, encoding="utf-8")
    return path


def test_frozen_legacy_mcpkit_matches_provenance():
    provenance = json.loads((COMPAT / "LEGACY_MCPKIT_PROVENANCE.json").read_text(encoding="utf-8"))
    for name, digest in provenance["files"].items():
        data = (COMPAT / "legacy_mcpkit" / name).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(data).hexdigest() == digest, name


def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    write(root / "plugins/legacyplug/mcp-service.json",
          {"name": "legacyplug", "aliases": ["oldname"], "command": ["python", "-c", "pass"], "port": free_port()})
    # a v1 project on the baseline mcpkit 0.1.0 (path source, as the old builtins had it)
    write(root / "plugins/legacyplug/pyproject.toml",
          '[project]\nname = "legacyplug"\nversion = "1.0.0"\ndependencies = ["mcpkit"]\n\n'
          '[tool.uv.sources]\nmcpkit = { path = "../oldkit", editable = true }\n')
    write(root / "plugins/oldkit/pyproject.toml", '[project]\nname = "mcpkit"\nversion = "0.1.0"\n')
    write(root / "plugins/modernplug/mcp-service.json",
          {"name": "modernplug", "schema_version": 2, "requires": {"mcpkit": ">=0.2"},
           "command": ["python", "-c", "pass"], "port": free_port()})
    # v1 manifests that do not need the shadow root: no mcpkit at all, and a current mcpkit
    write(root / "plugins/plainplug/mcp-service.json",
          {"name": "plainplug", "command": ["python", "-c", "pass"], "port": free_port()})
    write(root / "plugins/plainplug/package.json", '{"name": "plainplug"}\n')
    write(root / "plugins/v1modern/mcp-service.json",
          {"name": "v1modern", "command": ["python", "-c", "pass"], "port": free_port()})
    write(root / "plugins/v1modern/pyproject.toml", '[project]\nname = "v1modern"\ndependencies = ["mcpkit>=0.1"]\n')
    write(root / "plugins/v1modern/uv.lock",
          'version = 1\n\n[[package]]\nname = "mcpkit"\nversion = "0.2.0"\n'
          'source = { registry = "https://pypi.org/simple" }\n')
    write(root / "agent-nexus.json", {
        "builtins": False, "plugin_paths": ["plugins"],
        "plugins": {"legacyplug": {
            "auth": {"mode": "bearer", "bearer_secrets": ["legacyplug/bearer"]},
            "settings": {"greeting": "hi from the console", "password": {"$secret": "legacyplug/password"}},
            "install": {"state": "linked", "path": "elsewhere"}},
            "plainplug": {"settings": {"token": {"$secret": "plainplug/token"}}},
            "v1modern": {"settings": {"token": {"$secret": "v1modern/token"}}}},
    })
    write(root / "secrets/plainplug/token", OTHER_SECRET + "\n")
    write(root / "secrets/v1modern/token", OTHER_SECRET + "\n")
    write(root / "secrets/legacyplug/password", PASSWORD + "\n")
    write(root / "secrets/legacyplug/bearer", BEARER + "\n")
    # the rollback target: must stay byte-for-byte as it is
    write(root / "mcp-manager.json", {"builtins": False, "scan": ["plugins"], "services": {"legacyplug": {"settings": {"greeting": "old"}}}})
    return root


def start_env(root: Path, name: str) -> dict:
    captured = {}

    def fake_spawn(argv, cwd, env, log):
        captured.update(env)
        raise OSError("not starting anything in a test")

    sup = Supervisor(root)
    original = supervisor._spawn
    supervisor._spawn = fake_spawn  # protect() still runs icacls through subprocess
    try:
        with pytest.raises(OSError):
            sup.start(name)
    finally:
        supervisor._spawn = original
    return captured


def test_old_mcpkit_plugin_reads_console_config_through_shadow_root(tmp_path):
    from legacy_mcpkit import config as legacy

    root = make_root(tmp_path)
    original = (root / "mcp-manager.json").read_bytes()
    env = start_env(root, "legacyplug")
    shadow = compat_root.compat_dir(root)
    assert env["MCPM_ROOT"] == str(shadow)
    assert env["ANX_ROOT"] == str(root.resolve())

    service = legacy.load(root / "plugins/legacyplug", environ=env)
    assert service.root == shadow.resolve()
    assert service.settings == {"greeting": "hi from the console", "password": PASSWORD}
    assert legacy.read_secret(service, "legacyplug/bearer") == BEARER  # secrets junction
    generated = json.loads((shadow / "mcp-manager.json").read_text(encoding="utf-8"))
    assert generated["_generated_by"] == "agent-nexus"
    assert set(generated) == {"_generated_by", "services"}
    assert "install" not in generated["services"]["legacyplug"]
    assert generated["services"]["oldname"] == generated["services"]["legacyplug"]  # former name too
    # only the legacy plugin's entry: no other plugin's secret is resolved into the shadow file
    assert set(generated["services"]) == {"legacyplug", "oldname"}
    assert OTHER_SECRET not in (shadow / "mcp-manager.json").read_text(encoding="utf-8")

    # a console edit reaches the old plugin at its next start
    save_override(load_manager_config(root), "legacyplug", {"port": 18777})
    env = start_env(root, "legacyplug")
    assert legacy.load(root / "plugins/legacyplug", environ=env).port == 18777

    assert (root / "mcp-manager.json").read_bytes() == original


def test_current_mcpkit_plugin_is_unaffected(tmp_path):
    from mcpkit.config import load

    root = make_root(tmp_path)
    env = start_env(root, "modernplug")
    assert env["MCPM_ROOT"] == env["ANX_ROOT"] == str(root.resolve())
    # a current mcpkit prefers ANX_ROOT even when MCPM_ROOT is the shadow root
    env = start_env(root, "legacyplug")
    config = load(root / "plugins/legacyplug", environ=env)
    assert config.root == root.resolve()
    assert config.settings["password"] == PASSWORD


def test_no_shadow_root_for_a_legacy_only_root(tmp_path):
    root = make_root(tmp_path)
    (root / "agent-nexus.json").unlink()
    env = start_env(root, "legacyplug")
    assert env["MCPM_ROOT"] == str(root.resolve())
    assert not compat_root.compat_dir(root).exists()


@pytest.mark.parametrize("schema, requires, legacy", [
    (1, "", True), (2, "", False), (2, ">=0.1", True), (1, ">=0.2", False), (2, "^0.2.1", False),
])
def test_legacy_detection(schema, requires, legacy):
    class Spec:
        schema_version = schema
        requires_mcpkit = requires
    assert compat_root.is_legacy_plugin(Spec()) is legacy


@pytest.mark.parametrize("name", ["plainplug", "v1modern", "modernplug"])
def test_v1_manifest_without_legacy_mcpkit_gets_the_real_root(tmp_path, name):
    root = make_root(tmp_path)
    env = start_env(root, name)
    assert env["MCPM_ROOT"] == env["ANX_ROOT"] == str(root.resolve())
    assert not compat_root.compat_dir(root).exists()


def test_bundled_device_plugin_is_not_legacy():
    from agent_nexus.registry import ServiceSpec

    device = Path(__file__).resolve().parents[2] / "builtins" / "device"
    assert compat_root.mcpkit_version(device)[:2] >= (0, 2)
    assert not compat_root.is_legacy_plugin(ServiceSpec(name="device", dir=device))


def _project(path: Path, files: dict) -> Path:
    for rel, text in files.items():
        write(path / rel, text)
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_mcpkit_version_sources(tmp_path):
    assert compat_root.mcpkit_version(_project(tmp_path / "none", {"README.md": "x"})) == compat_root.NOT_USED
    unknown = _project(tmp_path / "unknown", {"requirements.txt": "fastmcp\nMCPKit==0.1\n"})
    assert compat_root.mcpkit_version(unknown) is None
    vendored = _project(tmp_path / "vendored", {"src/mcpkit/__init__.py": ""})
    assert compat_root.mcpkit_version(vendored) is None
    win = _project(tmp_path / "win", {".venv/Lib/site-packages/mcpkit-0.1.0.dist-info/METADATA": "x"})
    assert compat_root.mcpkit_version(win) == (0, 1, 0)
    posix = _project(tmp_path / "posix", {".venv/lib/python3.12/site-packages/mcpkit-0.2.1.dist-info/METADATA": "x"})
    assert compat_root.mcpkit_version(posix) == (0, 2, 1)
    # an editable source in uv.lock runs the code on disk, not the locked version
    _project(tmp_path / "kit", {"pyproject.toml": '[project]\nname = "mcpkit"\nversion = "0.2.3"\n'})
    locked = _project(tmp_path / "locked", {"uv.lock": '[[package]]\nname = "mcpkit"\nversion = "0.1.0"\n'
                                                       'source = { editable = "../kit" }\n'})
    assert compat_root.mcpkit_version(locked) == (0, 2, 3)
