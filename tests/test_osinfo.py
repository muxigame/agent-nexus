"""os_family for the console's device icons (agent_nexus/osinfo.py)."""
from __future__ import annotations

import pytest

from agent_nexus import osinfo
from agent_nexus.devices import Devices


@pytest.mark.parametrize(("device", "family"), [
    ({"platform": "windows", "os": "10.0.26100"}, "windows"),
    ({"os": "Windows 11"}, "windows"),
    ({"platform": "darwin"}, "macos"),
    ({"platform": "macos", "form": "laptop"}, "macos"),
    ({"platform": "linux", "distro": "ubuntu", "os": "6.8.0-48-generic"}, "ubuntu"),
    ({"platform": "linux", "distro": "linuxmint"}, "ubuntu"),
    ({"platform": "linux", "distro": "debian"}, "debian"),
    ({"platform": "linux", "distro": "rocky"}, "fedora"),
    ({"platform": "linux", "distro": "manjaro"}, "arch"),
    ({"platform": "linux", "os": "6.8.0-48-generic"}, "ubuntu"),
    ({"platform": "linux", "os": "6.1.0-25-amd64"}, "linux"),
    ({"platform": "linux", "distro": "nixos", "os": "6.8.0-48-generic"}, "linux"),
    ({"platform": "linux", "distro": "nixos"}, "linux"),
    ({"platform": "android", "form": "phone"}, "android"),
    ({"platform": "ios", "form": "phone"}, "ios"),
    ({}, None),
    (None, None),
])
def test_family(device, family):
    assert osinfo.family(device) == family


def test_os_release_parsing(tmp_path, monkeypatch):
    release = tmp_path / "os-release"
    release.write_text('NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\nVERSION_ID="22.04"\n', encoding="utf-8")
    assert osinfo.os_release(release)["ID"] == "ubuntu"
    monkeypatch.setattr(osinfo.sys, "platform", "linux")
    monkeypatch.setattr(osinfo, "os_release", lambda path=None: {"ID": "ubuntu"})
    assert osinfo.distro() == "ubuntu"
    monkeypatch.setattr(osinfo, "os_release", lambda path=None: {"ID": "../etc"})
    assert osinfo.distro() is None
    monkeypatch.setattr(osinfo.sys, "platform", "win32")
    assert osinfo.distro() is None


def test_join_reports_the_distro(monkeypatch):
    from agent_nexus import join
    monkeypatch.setattr(osinfo, "distro", lambda: "ubuntu")
    assert join.device_facts()["distro"] == "ubuntu"


class _Store:
    def __init__(self, rows):
        self.rows = rows

    def devices(self):
        return list(self.rows.values())

    def device(self, agent_id):
        return self.rows.get(agent_id)


class _Hub:
    peers = {}


def test_device_list_carries_os_family_for_enrolled_devices():
    rows = {"u": {"agent_id": "u", "hostname": "box", "alias": None,
                  "device": {"device": {"platform": "linux", "os": "6.8.0", "distro": "ubuntu"}}},
            "w": {"agent_id": "w", "hostname": "pc", "alias": None, "device": {"platform": "windows"}}}
    listed = {d["device_id"]: d for d in Devices(_Hub(), _Store(rows)).list(include_offline=True)}
    assert listed["u"]["os_family"] == "ubuntu" and listed["u"]["distro"] == "ubuntu"
    assert listed["u"]["platform"] == "linux"
    assert listed["w"]["os_family"] == "windows"
