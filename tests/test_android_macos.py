"""The Android app and the macOS client in the core: plugins without uv, the device's own name,
in-app updates of the APK, the download list, and the release payload check over their files."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import zipfile
from pathlib import Path

import pytest

from agent_nexus import android, auto_update, desktop, downloads, join, linux_update

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import test_no_private_payload as payload  # noqa: E402


def test_plugins_run_with_the_apps_interpreter_and_their_own_packages(tmp_path):
    argv, env = android.plugin_command(["uv", "run", "--frozen", "--project", ".", "python", "-m", "src.server"],
                                       tmp_path, {"PYTHONPATH": "/manager/site", "X": "1"}, executable="/lib/libpyexe.so")
    assert argv == ["/lib/libpyexe.so", "-m", "src.server"]
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(tmp_path / ".site") and "/manager/site" not in env["PYTHONPATH"]
    assert env["X"] == "1"
    untouched = ["node", "server.js"]
    assert android.plugin_command(untouched, tmp_path, {})[0] == untouched


def test_android_reports_the_devices_own_name(monkeypatch):
    monkeypatch.setenv("ANX_ANDROID_DEVICE_NAME", "  Pixel 8 Pro  ")
    monkeypatch.setattr(android, "ANDROID", False)
    assert android.device_name() is None
    monkeypatch.setattr(join.platform, "node", lambda: "localhost")
    assert join.local_hostname() == "localhost"
    monkeypatch.setattr(android, "ANDROID", True)
    assert android.device_name() == "Pixel 8 Pro"
    assert join.local_hostname() == "Pixel 8 Pro"
    monkeypatch.setenv("ANX_ANDROID_DEVICE_NAME", "")
    assert join.local_hostname() == "localhost"


class _Response:
    def __init__(self, items):
        self.items = items

    def raise_for_status(self):
        pass

    def json(self):
        return {"items": self.items}


def _android(monkeypatch, tmp_path):
    monkeypatch.setattr(linux_update, "ANDROID", True)
    monkeypatch.setattr(linux_update, "WINDOWS", False)
    monkeypatch.setenv("ANX_ANDROID_APP", "io.github.muxigame.agentnexus")
    monkeypatch.setenv("ANX_ROOT", str(tmp_path))
    monkeypatch.setattr(linux_update, "_gateway", lambda root: "https://hub.example.test")


def test_android_update_check_finds_the_newest_apk(tmp_path, monkeypatch):
    import httpx
    _android(monkeypatch, tmp_path)
    assert linux_update.supported()
    items = [{"id": "windows-x64", "filename": "agent-nexus_99.0.0_x64-setup.exe", "version": "99.0.0", "url": "u"},
             {"id": "android-arm64", "filename": "agent-nexus_99.0.0_arm64.apk", "version": "99.0.0",
              "url": "https://oss.example.test/agent-nexus_99.0.0_arm64.apk", "sha256": "0" * 64, "size": 3}]
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Response(items))
    found = linux_update.check(tmp_path)
    assert found["available"] and found["version"] == "99.0.0"
    assert found["deb"]["filename"].endswith("_arm64.apk")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Response(items[:1]))
    assert "Android" in linux_update.check(tmp_path)["error"]


def test_android_apply_downloads_once_and_hands_the_apk_to_the_app(tmp_path, monkeypatch):
    _android(monkeypatch, tmp_path)
    body = b"PK\x03\x04 not really an apk"
    record = {"available": True, "version": "99.0.0", "deb": {
        "url": "https://oss.example.test/agent-nexus_99.0.0_arm64.apk", "filename": "agent-nexus_99.0.0_arm64.apk",
        "sha256": hashlib.sha256(body).hexdigest()}}
    (tmp_path / desktop.UPDATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / desktop.UPDATE_FILE).write_text(json.dumps(record), encoding="utf-8")
    fetched = []

    class Stream:
        def __enter__(self):
            fetched.append(1)
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self, size):
            yield body

    import httpx
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: Stream())
    result = linux_update.apply(tmp_path, unattended=True)
    assert result["prompted"] and not result["installed"] and result["version"] == "99.0.0"
    apk = tmp_path / linux_update.ANDROID_UPDATES / "agent-nexus_99.0.0_arm64.apk"
    assert apk.read_bytes() == body
    request = json.loads((tmp_path / linux_update.ANDROID_REQUEST).read_text(encoding="utf-8"))
    assert request["path"] == str(apk.resolve()) and request["version"] == "99.0.0"
    # Still "available" (the user has not confirmed), and the next round does not download again.
    assert json.loads((tmp_path / desktop.UPDATE_FILE).read_text(encoding="utf-8"))["available"]
    linux_update.apply(tmp_path, unattended=True)
    assert fetched == [1]


def _android_record(tmp_path, body):
    record = {"available": True, "version": "99.0.0", "deb": {
        "url": "https://oss.example.test/agent-nexus_99.0.0_arm64.apk", "filename": "agent-nexus_99.0.0_arm64.apk",
        "sha256": hashlib.sha256(body).hexdigest()}}
    (tmp_path / desktop.UPDATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / desktop.UPDATE_FILE).write_text(json.dumps(record), encoding="utf-8")


def _serve_apk(monkeypatch, body):
    fetched = []

    class Stream:
        def __enter__(self):
            fetched.append(1)
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self, size):
            yield body

    import httpx
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: Stream())
    return fetched


def test_android_waits_for_an_unmetered_network_before_downloading_unattended(tmp_path, monkeypatch):
    _android(monkeypatch, tmp_path)
    body = b"PK\x03\x04 apk"
    _android_record(tmp_path, body)
    fetched = _serve_apk(monkeypatch, body)
    network = tmp_path / linux_update.ANDROID_NETWORK
    network.parent.mkdir(parents=True, exist_ok=True)
    network.write_text('{"metered": true}', encoding="utf-8")
    assert linux_update.android_metered(tmp_path)
    result = linux_update.apply(tmp_path, unattended=True)
    assert result["deferred"] == "metered" and not result.get("prompted") and fetched == []
    assert not (tmp_path / linux_update.ANDROID_REQUEST).exists()
    # "立即更新" in the settings page downloads anyway; once downloaded, an automatic round reuses it.
    assert linux_update.apply(tmp_path)["prompted"] and fetched == [1]
    assert linux_update.apply(tmp_path, unattended=True)["prompted"] and fetched == [1]
    network.write_text('{"metered": false}', encoding="utf-8")
    assert not linux_update.android_metered(tmp_path)
    network.write_text("not json", encoding="utf-8")
    assert not linux_update.android_metered(tmp_path)


def test_an_automatic_round_does_not_renew_the_request_but_update_now_does(tmp_path, monkeypatch):
    _android(monkeypatch, tmp_path)
    body = b"PK\x03\x04 apk"
    _android_record(tmp_path, body)
    _serve_apk(monkeypatch, body)
    request = tmp_path / linux_update.ANDROID_REQUEST
    linux_update.apply(tmp_path, unattended=True)
    first = request.read_text(encoding="utf-8")
    assert "asked_at" not in json.loads(first)
    linux_update.apply(tmp_path, unattended=True)
    assert request.read_text(encoding="utf-8") == first  # the app asks once and does not nag
    linux_update.apply(tmp_path)
    assert json.loads(request.read_text(encoding="utf-8"))["asked_at"]  # the app asks again, even after "稍后"


def test_the_automatic_round_reports_a_deferred_android_download(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "desktop_app", lambda: None)
    monkeypatch.setattr(linux_update, "supported", lambda: True)
    monkeypatch.setattr(linux_update, "check", lambda root: {"available": True, "version": "9.9.9"})
    monkeypatch.setattr(linux_update, "apply", lambda root, unattended: {"installed": False, "deferred": "metered",
                                                                         "version": "9.9.9"})
    assert asyncio.run(auto_update.run_once(tmp_path))["result"] == "deferred"


def test_the_app_keeps_the_update_notice_and_network_report_going_while_the_core_runs():
    java = ROOT / "android/app/src/main/java/io/github/muxigame/agentnexus"
    service = (java / "CoreService.java").read_text(encoding="utf-8")
    # A single blocking waitFor() would never reach the notification while the core runs.
    assert "process.waitFor()" not in service and "process.waitFor(3, TimeUnit.SECONDS)" in service
    loop = service[service.index("process.waitFor(3, TimeUnit.SECONDS)"):]
    assert "notifyUpdate();" in loop[:300]
    helper = service[service.index("private void notifyUpdate()"):]
    assert "Updates.reportNetwork(this)" in helper[:400] and "Updates.notifyIfPending(this)" in helper[:400]
    updates = (java / "Updates.java").read_text(encoding="utf-8")
    assert f'"{linux_update.ANDROID_NETWORK.as_posix()}"' in updates and "isActiveNetworkMetered" in updates
    assert 'optString("asked_at"' in updates
    activity = (java / "MainActivity.java").read_text(encoding="utf-8")
    resume = activity[activity.index("protected void onResume()"):]
    resume = resume[:resume.index("\n    }\n")]
    assert "if (awayToInstall)" in resume  # not a reset on every return to the app
    assert "declined = code" in activity


def test_the_automatic_round_reports_an_android_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "desktop_app", lambda: None)
    monkeypatch.setattr(linux_update, "supported", lambda: True)
    monkeypatch.setattr(linux_update, "check", lambda root: {"available": True, "version": "9.9.9"})
    monkeypatch.setattr(linux_update, "apply", lambda root, unattended: {"installed": False, "prompted": True,
                                                                         "version": "9.9.9"})
    assert asyncio.run(auto_update.run_once(tmp_path))["result"] == "prompted"
    monkeypatch.setattr(linux_update, "ANDROID", True)
    assert desktop.update_status(tmp_path)["updater"] == "apk"


def test_the_app_serves_exactly_the_folder_the_core_downloads_into():
    paths = (ROOT / "android/app/src/main/res/xml/file_paths.xml").read_text(encoding="utf-8")
    folder = re.search(r'<files-path name="updates" path="([^"]+)"', paths).group(1)
    assert folder == f"AgentNexus/{linux_update.ANDROID_UPDATES.as_posix()}/"
    manifest = (ROOT / "android/app/src/main/AndroidManifest.xml").read_text(encoding="utf-8")
    assert "android.permission.REQUEST_INSTALL_PACKAGES" in manifest
    assert 'android:authorities="${applicationId}.files"' in manifest and "androidx.core.content.FileProvider" in manifest
    updates = (ROOT / "android/app/src/main/java/io/github/muxigame/agentnexus/Updates.java").read_text(encoding="utf-8")
    assert f'"{linux_update.ANDROID_REQUEST.as_posix()}"' in updates and '".files"' in updates
    assert "androidx.core:core" in (ROOT / "android/app/build.gradle").read_text(encoding="utf-8")
    assert "android.useAndroidX=true" in (ROOT / "android/gradle.properties").read_text(encoding="utf-8")
    runtime = (ROOT / "android/app/src/main/java/io/github/muxigame/agentnexus/Runtime.java").read_text(encoding="utf-8")
    assert '"AgentNexus"' in runtime and '"ANX_ANDROID_APP"' in runtime and '"ANX_ANDROID_DEVICE_NAME"' in runtime


def test_the_download_list_names_each_mac_chip_and_hides_updater_bundles(tmp_path):
    arm, intel = downloads.classify("agent-nexus_1.0.0_aarch64.dmg"), downloads.classify("agent-nexus_1.0.0_x64.dmg")
    assert (arm["id"], intel["id"]) == ("macos-arm64", "macos-x64") and arm["label"] != intel["label"]
    assert arm["label"] == "Agent Nexus for macOS（Apple 芯片 / M 系列）"
    assert intel["label"] == "Agent Nexus for macOS（Intel 芯片）"
    assert all(item["label"].count("（") == 1 for item in (arm, intel))  # no nested brackets
    assert downloads.classify("agent-nexus_1.0.0_arm64.apk")["platform"] == "android"
    folder = tmp_path / "downloads"
    folder.mkdir()
    for name in ("agent-nexus_1.0.0_aarch64.dmg", "agent-nexus_1.0.0_aarch64.app.tar.gz", "agent-nexus_1.0.0_arm64.apk"):
        (folder / name).write_bytes(b"x")
    listed = downloads.Downloads({}, folder, tmp_path).items()["items"]
    assert sorted(item["filename"] for item in listed) == ["agent-nexus_1.0.0_aarch64.dmg", "agent-nexus_1.0.0_arm64.apk"]
    assert all(item["latest"] and item["version"] == "1.0.0" for item in listed)


def test_publish_names_the_apk_and_dmg_types():
    import publish_oss
    assert publish_oss.CONTENT_TYPES[".apk"] == "application/vnd.android.package-archive"
    assert publish_oss.CONTENT_TYPES[".dmg"] == "application/x-apple-diskimage"


def _runtime_zip(private: bool) -> bytes:
    import io
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(".private/enrollment.json" if private else "agent_nexus/cli.py", b"{}")
    return buffer.getvalue()


@pytest.mark.parametrize("magic", payload.MACHO_MAGIC)
def test_the_macos_executable_is_checked_for_an_embedded_private_runtime(tmp_path, magic):
    clean, dirty = tmp_path / "clean", tmp_path / "dirty"
    clean.mkdir()
    dirty.mkdir()
    (clean / "agent-nexus").write_bytes(magic + b"\0" * 60 + _runtime_zip(False))
    (dirty / "agent-nexus").write_bytes(magic + b"\0" * 60 + _runtime_zip(True))
    assert payload.check_paths([clean]) == []
    assert any("embedded private" in finding for finding in payload.check_paths([dirty]))


def test_an_apk_is_opened_like_a_zip(tmp_path):
    import io
    apk = io.BytesIO()
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("assets/runtime.zip", _runtime_zip(True))
    (tmp_path / "agent-nexus_1.0.0_arm64.apk").write_bytes(apk.getvalue())
    assert any(".private" in finding for finding in payload.check_paths([tmp_path]))
