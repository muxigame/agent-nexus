"""publish_oss.py and make_latest_json.py --base-url: the OSS release feed."""
from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://bucket.oss-cn-hangzhou.aliyuncs.com/agent-nexus"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publish = load("publish_oss")
latest = load("make_latest_json")
SIGNATURE = base64.b64encode(b"untrusted comment: x\nRWQ\ntrusted comment: y\nsig").decode()


def release(tmp_path: Path, version="0.2.1", url=None) -> Path:
    dist = tmp_path / "release"
    dist.mkdir(parents=True)
    setup = dist / f"agent-nexus_{version}_x64-setup.exe"
    setup.write_bytes(b"MZ-installer")
    (dist / (setup.name + ".sig")).write_text(SIGNATURE, encoding="utf-8")
    (dist / "SHA256SUMS").write_text("x  y\n", encoding="utf-8")
    assert latest.main(["--setup", str(setup), "--tag", f"v{version}", "--base-url", f"{BASE}/releases/{version}",
                        "--out", str(dist / "latest.json")]) == 0
    if url:
        feed = json.loads((dist / "latest.json").read_text(encoding="utf-8"))
        feed["platforms"]["windows-x86_64"]["url"] = url
        (dist / "latest.json").write_text(json.dumps(feed), encoding="utf-8")
    return dist


def test_feed_points_at_the_versioned_oss_folder(tmp_path: Path) -> None:
    dist = release(tmp_path)
    feed = json.loads((dist / "latest.json").read_text(encoding="utf-8"))
    assert feed["version"] == "0.2.1"
    assert feed["platforms"]["windows-x86_64"]["url"] == f"{BASE}/releases/0.2.1/agent-nexus_0.2.1_x64-setup.exe"
    assert feed["platforms"]["windows-x86_64"]["signature"] == SIGNATURE
    with pytest.raises(ValueError):
        latest.base_url_asset("http://insecure.example", "x.exe")


def test_plan_uploads_the_feed_last_into_the_version_folder(tmp_path: Path) -> None:
    dist = release(tmp_path)
    steps = publish.plan(dist, "0.2.1", "agent-nexus")
    assert [key for _, key in steps][-1] == "agent-nexus/releases/0.2.1/latest.json"
    assert all(key.startswith("agent-nexus/releases/0.2.1/") for _, key in steps)
    assert len(steps) == 4
    assert publish.public_url(BASE, steps[0][1], "agent-nexus").startswith(f"{BASE}/releases/0.2.1/")


def test_a_feed_for_another_version_or_place_is_refused(tmp_path: Path) -> None:
    dist = release(tmp_path)
    with pytest.raises(publish.PublishError, match="not '0.2.2'"):
        publish.check_feed(dist / "latest.json", "0.2.2", BASE)
    other = release(tmp_path / "x", url="https://github.com/o/r/releases/download/v0.2.1/a.exe")
    with pytest.raises(publish.PublishError, match="is not under"):
        publish.check_feed(other / "latest.json", "0.2.1", BASE)


def test_dry_run_needs_no_credentials_and_keeps_latest_for_prereleases(tmp_path: Path, capsys) -> None:
    dist = release(tmp_path, version="0.3.0-rc.1")
    assert publish.main(["--dist", str(dist), "--version", "v0.3.0-rc.1", "--public-base", BASE, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "prerelease" in out and "would upload" in out
    stable = release(tmp_path / "s")
    assert publish.main(["--dist", str(stable), "--version", "0.2.1", "--public-base", BASE, "--dry-run"]) == 0
    assert "agent-nexus/latest.json <- latest.json of 0.2.1" in capsys.readouterr().out


def test_the_desktop_updater_polls_the_oss_feed() -> None:
    conf = json.loads((ROOT / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    assert conf["plugins"]["updater"]["endpoints"] == [f"{publish.DEFAULT_PUBLIC_BASE}/latest.json"]
