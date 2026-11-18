"""The website's download list from the release manifest on OSS (downloads.manifest_url)."""
from __future__ import annotations

import io
import json
import urllib.request
from pathlib import Path

import pytest

from agent_nexus import downloads as dl
from mcpkit.config import ConfigError

SHA = "a" * 64
MANIFEST = "https://bucket.example/agent-nexus/downloads.json"


def listing(version="0.2.2"):
    base = f"https://bucket.example/agent-nexus/releases/{version}"
    return {"version": version, "published_at": "2026-11-18", "files": [
        {"filename": f"agent-nexus_{version}_x64-setup.exe", "size": 10, "sha256": SHA, "url": f"{base}/agent-nexus_{version}_x64-setup.exe"},
        {"filename": f"agent-nexus_{version}_amd64.deb", "size": 20, "sha256": SHA, "url": f"{base}/agent-nexus_{version}_amd64.deb"},
        {"filename": "../evil.exe", "size": 1, "sha256": SHA, "url": f"{base}/evil.exe"},
        {"filename": "notes.txt", "size": 1, "sha256": SHA, "url": f"{base}/notes.txt"},
        {"filename": "agent-nexus_9.9.9_x64-setup.exe", "size": 1, "sha256": SHA, "url": "http://insecure/x.exe"},
    ]}


@pytest.fixture
def served(monkeypatch):
    state = {"body": listing(), "calls": 0, "fail": False}

    def urlopen(request, timeout=None):
        state["calls"] += 1
        assert request.full_url == MANIFEST
        if state["fail"]:
            raise OSError("offline")
        return io.BytesIO(json.dumps(state["body"]).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return state


def make(tmp_path: Path, **section) -> dl.Downloads:
    folder = tmp_path / "downloads"
    folder.mkdir()
    return dl.Downloads({"manifest_url": MANIFEST, **section}, folder, tmp_path)


def test_manifest_installers_are_listed_with_their_public_urls(tmp_path, served):
    data = make(tmp_path).items()
    by_name = {item["filename"]: item for item in data["items"]}
    assert sorted(by_name) == ["agent-nexus_0.2.2_amd64.deb", "agent-nexus_0.2.2_x64-setup.exe"]
    deb = by_name["agent-nexus_0.2.2_amd64.deb"]
    assert deb["platform"] == "linux" and deb["kind"] == "desktop" and deb["latest"]
    assert deb["url"].startswith("https://bucket.example/") and deb["sha256"] == SHA and deb["size"] == 20
    assert data["latest"] == "0.2.2"


def test_local_files_stay_and_the_manifest_wins_a_name_clash(tmp_path, served):
    downloads = make(tmp_path)
    (downloads.dir / "agent-nexus_0.2.0_x64-setup.exe").write_bytes(b"old")
    (downloads.dir / "agent-nexus_0.2.2_x64-setup.exe").write_bytes(b"local copy")
    items = {item["filename"]: item for item in downloads.items()["items"]}
    assert items["agent-nexus_0.2.0_x64-setup.exe"]["url"] == "/downloads/agent-nexus_0.2.0_x64-setup.exe"
    assert not items["agent-nexus_0.2.0_x64-setup.exe"]["latest"]
    assert items["agent-nexus_0.2.2_x64-setup.exe"]["url"].startswith("https://")


def test_the_manifest_is_cached_and_survives_an_outage(tmp_path, served, monkeypatch):
    downloads = make(tmp_path)
    assert len(downloads.items()["items"]) == 2
    downloads.items()
    assert served["calls"] == 1
    clock = [10_000.0]
    monkeypatch.setattr(dl.time, "monotonic", lambda: clock[0])
    downloads._remote_cache = (clock[0] - dl.MANIFEST_TTL_S - 1, downloads._remote_cache[1])
    served["fail"] = True
    assert len(downloads.items()["items"]) == 2
    assert served["calls"] == 2


def test_manifest_url_must_be_https(tmp_path):
    with pytest.raises(ConfigError):
        dl.Downloads({"manifest_url": "http://x/downloads.json"}, tmp_path, tmp_path)
    assert dl.Downloads.warnings({"manifest_url": MANIFEST}) == []
