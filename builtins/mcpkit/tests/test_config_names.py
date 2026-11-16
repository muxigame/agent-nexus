"""mcpkit 0.2: both central file names, ANX_ROOT, and service aliases (agent-nexus M1)."""
import json
from pathlib import Path

from mcpkit.config import (CENTRAL_NAME, LEGACY_CENTRAL_NAME, ServiceConfig, central_path, find_root, load,
                           update_settings)


def write(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def service(root: Path, name: str, **manifest) -> Path:
    directory = root / "plugins" / name
    write(directory / "mcp-service.json", {"name": name, **manifest})
    return directory


def test_names():
    assert CENTRAL_NAME == "agent-nexus.json" and LEGACY_CENTRAL_NAME == "mcp-manager.json"


def test_a_root_with_only_the_legacy_file_is_found_and_read(tmp_path):
    write(tmp_path / LEGACY_CENTRAL_NAME, {"services": {"svc": {"settings": {"a": 1}}}})
    directory = service(tmp_path, "svc")
    assert find_root(directory, {}) == tmp_path
    assert central_path(tmp_path) == tmp_path / LEGACY_CENTRAL_NAME
    assert load(directory, environ={}).settings == {"a": 1}


def test_the_new_file_wins_when_both_exist(tmp_path):
    write(tmp_path / LEGACY_CENTRAL_NAME, {"services": {"svc": {"settings": {"from": "legacy"}}}})
    write(tmp_path / CENTRAL_NAME, {"services": {"svc": {"settings": {"from": "new"}}}})
    directory = service(tmp_path, "svc")
    assert central_path(tmp_path) == tmp_path / CENTRAL_NAME
    assert load(directory, environ={}).settings == {"from": "new"}


def test_anx_root_then_legacy_mcpm_root(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(a / CENTRAL_NAME, {})
    write(b / LEGACY_CENTRAL_NAME, {})
    directory = service(tmp_path / "elsewhere", "svc")
    assert find_root(directory, {"ANX_ROOT": str(a), "MCPM_ROOT": str(b)}) == a.resolve()
    assert find_root(directory, {"MCPM_ROOT": str(b)}) == b.resolve()
    # A variable pointing at a directory without either file is ignored.
    assert find_root(directory, {"ANX_ROOT": str(tmp_path / "empty"), "MCPM_ROOT": str(b)}) == b.resolve()


def test_a_renamed_service_reads_its_former_entry(tmp_path):
    write(tmp_path / LEGACY_CENTRAL_NAME, {"services": {"workspace": {"settings": {"default_workdir": "x"}}}})
    directory = service(tmp_path, "device", aliases=["workspace"])
    cfg = load(directory, environ={})
    assert cfg.name == "device" and cfg.aliases == ["workspace"]
    assert cfg.settings == {"default_workdir": "x"}
    # An entry under the current name takes precedence over the alias.
    write(tmp_path / CENTRAL_NAME, {"services": {"workspace": {"settings": {"default_workdir": "old"}},
                                                 "device": {"settings": {"default_workdir": "new"}}}})
    assert load(directory, environ={}).settings == {"default_workdir": "new"}


def test_update_settings_never_writes_the_legacy_file(tmp_path):
    legacy = write(tmp_path / LEGACY_CENTRAL_NAME, {"manager": {"port": 8399},
                                                    "services": {"workspace": {"settings": {"keep": 1}}}})
    before = legacy.read_bytes()
    directory = service(tmp_path, "device", aliases=["workspace"])
    update_settings(load(directory, environ={}), learned=2)
    assert legacy.read_bytes() == before
    data = json.loads((tmp_path / CENTRAL_NAME).read_text(encoding="utf-8"))
    assert data["manager"] == {"port": 8399}
    assert data["services"]["device"]["settings"] == {"keep": 1, "learned": 2}
    assert load(directory, environ={}).settings == {"keep": 1, "learned": 2}


def test_oauth_store_under_a_former_name_is_kept(tmp_path):
    cfg = ServiceConfig(name="device", root=tmp_path, dir=tmp_path, aliases=["workspace"])
    assert cfg.oauth_store == tmp_path / "secrets/device/oauth.sqlite3"
    old = tmp_path / "secrets/workspace/oauth.sqlite3"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"")
    assert cfg.oauth_store == old
