"""deploy/gateway/auto_update.py: when the gateway upgrades itself."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gateway_auto_update", ROOT / "deploy/gateway/auto_update.py")
auto = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auto)

ZIP = {"filename": "agent-nexus-gateway-0.6.0.zip", "url": "https://cdn.example.test/z.zip", "sha256": "a" * 64}


def test_newer_stable_release_is_installed():
    entry, why = auto.decide({"version": "0.6.0", "files": [ZIP]}, "0.5.2", {}, 1000.0)
    assert entry["filename"] == ZIP["filename"] and entry["version"] == "0.6.0" and "0.5.2 -> 0.6.0" in why


def test_same_older_or_prerelease_is_left_alone():
    assert auto.decide({"version": "0.5.2", "files": [ZIP]}, "0.5.2", {}, 0)[0] is None
    assert auto.decide({"version": "0.5.10", "files": [ZIP]}, "0.5.9", {}, 0)[0] is not None  # numeric, not text
    assert auto.decide({"version": "0.4.0", "files": [ZIP]}, "0.5.2", {}, 0)[0] is None
    assert auto.decide({"version": "0.6.0-rc.1", "files": [ZIP]}, "0.5.2", {}, 0)[0] is None
    assert auto.decide({"version": "0.6.0", "files": []}, "0.5.2", {}, 0)[0] is None


def test_a_failed_version_waits_six_hours():
    state = {"failed": {"0.6.0": 1000.0}}
    assert auto.decide({"version": "0.6.0", "files": [ZIP]}, "0.5.2", state, 1000.0 + 3600)[0] is None
    assert auto.decide({"version": "0.6.0", "files": [ZIP]}, "0.5.2", state, 1000.0 + 6 * 3600 + 1)[0] is not None
