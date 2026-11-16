"""mcpkit 0.2 reads a migrated agent-nexus.json (docs/design.md section 4.2):
``plugins.<name>`` entries and ``{"$secret": ...}`` settings references."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcpkit.config import ConfigError, load, update_settings


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")


def make(tmp_path: Path, central: dict) -> Path:
    write(tmp_path / "agent-nexus.json", central)
    write(tmp_path / "svc" / "mcp-service.json", {"name": "svc", "aliases": ["old"], "port": 9100,
                                                  "settings": {"a": 1, "b": 2}})
    return tmp_path / "svc"


def test_plugins_section_overlays_services(tmp_path):
    service = make(tmp_path, {"services": {"svc": {"settings": {"a": 10}, "port": 9101}},
                              "plugins": {"svc": {"settings": {"b": 20}, "install": {"state": "linked"}}}})
    config = load(service, root=tmp_path)
    assert config.settings == {"a": 10, "b": 20} and config.port == 9101


def test_plugins_entry_found_by_alias(tmp_path):
    service = make(tmp_path, {"plugins": {"old": {"settings": {"a": 5}}}})
    assert load(service, root=tmp_path).settings["a"] == 5


def test_secret_reference_is_resolved(tmp_path):
    service = make(tmp_path, {"plugins": {"svc": {"settings": {"password": {"$secret": "svc/password"},
                                                               "nested": [{"token": {"$secret": "svc/t"}}]}}}})
    write(tmp_path / "secrets/svc/password", "pw\n")
    write(tmp_path / "secrets/svc/t", "tok\n")
    config = load(service, root=tmp_path)
    assert config.settings["password"] == "pw"
    assert config.settings["nested"] == [{"token": "tok"}]


def test_missing_secret_reference_names_the_secret_only(tmp_path):
    service = make(tmp_path, {"plugins": {"svc": {"settings": {"password": {"$secret": "svc/missing"}}}}})
    with pytest.raises(ConfigError, match="svc/missing"):
        load(service, root=tmp_path)


def test_update_settings_writes_into_plugins(tmp_path):
    service = make(tmp_path, {"plugins": {"svc": {"settings": {"a": 1}}}})
    update_settings(load(service, root=tmp_path), learned="yes")
    data = json.loads((tmp_path / "agent-nexus.json").read_text(encoding="utf-8"))
    assert data["plugins"]["svc"]["settings"] == {"a": 1, "learned": "yes"} and "services" not in data
