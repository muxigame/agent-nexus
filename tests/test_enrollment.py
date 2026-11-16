import json

import pytest

from agent_nexus.enrollment import enroll_if_needed
from agent_nexus.registry import RegistryError

HUB = "wss://hub.example.test"


def profile(root, token="x" * 48, url=HUB, allow=HUB + "\n"):
    root.mkdir()
    (root / "mcp-manager.json").write_text(json.dumps({"cloud": {"enabled": False}, "services": {"workspace": {"port": 8490}}}))
    private = root / ".private"
    private.mkdir()
    (private / "enrollment.json").write_text(json.dumps({"url": url, "token": token}))
    if allow is not None:
        # Written by the private build from the local profile; never part of source.
        (private / "endpoint-allow").write_text(allow)


def test_enrollment_uses_unique_persistent_identity_and_preserves_existing_config(tmp_path):
    identities = set()
    for name in ("one", "two"):
        root = tmp_path / name
        profile(root)
        legacy = (root / "mcp-manager.json").read_bytes()
        assert enroll_if_needed(root)
        # The legacy file is only read; enrollment writes agent-nexus.json.
        assert (root / "mcp-manager.json").read_bytes() == legacy
        data = json.loads((root / "agent-nexus.json").read_text())
        identities.add(data["cloud"]["agent_id"])
        assert data["cloud"]["agent_id"].startswith("portable-")
        assert data["cloud"]["url"] == HUB
        assert data["services"]["workspace"]["port"] == 8490
        assert data["cloud"]["workspaces"]["home"]["mcp"] == "device"
        assert "token" not in data["cloud"]
        assert (root / "secrets/cloud/enrolled-agent").read_text().strip() == "x" * 48
        assert not enroll_if_needed(root)
        assert json.loads((root / "agent-nexus.json").read_text()) == data
    assert len(identities) == 2


def test_invalid_enrollment_never_leaks_token_or_changes_config(tmp_path):
    root = tmp_path / "bad"
    profile(root, "do-not-print-this-token")
    before = (root / "mcp-manager.json").read_bytes()
    with pytest.raises(RegistryError) as error:
        enroll_if_needed(root)
    assert "do-not-print" not in str(error.value)
    assert (root / "mcp-manager.json").read_bytes() == before


@pytest.mark.parametrize("url,allow", [
    (HUB, None),                                  # no build-time allow list
    ("wss://other.example.test", HUB + "\n"),     # profile edited after build
    ("ws://hub.example.test", HUB + "\n"),        # plaintext transport
    ("wss://user@hub.example.test", HUB + "\n"),  # credentials in authority
])
def test_enrollment_endpoint_must_match_build_time_allow_list(tmp_path, url, allow):
    root = tmp_path / "pinned"
    profile(root, url=url, allow=allow)
    before = (root / "mcp-manager.json").read_bytes()
    with pytest.raises(RegistryError):
        enroll_if_needed(root)
    assert (root / "mcp-manager.json").read_bytes() == before
    assert not (root / "agent-nexus.json").exists()
    assert not (root / "secrets").exists()


def test_enrollment_accepts_https_alias_and_stores_wss(tmp_path):
    root = tmp_path / "alias"
    profile(root, url="https://hub.example.test/", allow="# pinned at build time\nhttps://hub.example.test\n")
    assert enroll_if_needed(root)
    assert json.loads((root / "agent-nexus.json").read_text())["cloud"]["url"] == HUB


def test_build_and_runtime_agree_on_endpoint_canonical_form():
    import importlib.util
    from pathlib import Path

    from agent_nexus.enrollment import canonical_endpoint

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    modules = {}
    for name in ("build_release", "verify_client_artifacts"):
        spec = importlib.util.spec_from_file_location("parity_" + name, scripts / (name + ".py"))
        modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modules[name])
    cases = ["wss://hub.example.test", "https://hub.example.test/", "wss://hub.example.test:8443/base",
             "ws://hub.example.test", "wss://", "wss://a@b", "wss://hub.example.test/?q", " wss://x", 7]
    for url in cases:
        assert modules["build_release"].canonical_endpoint(url) == canonical_endpoint(url), url
    profile = json.dumps({"url": "https://hub.example.test/", "token": "t" * 40}).encode()
    assert modules["build_release"].endpoint_allow(profile) == b"wss://hub.example.test\n"
    assert modules["verify_client_artifacts"].expected_endpoint_allow(profile) == b"wss://hub.example.test\n"
