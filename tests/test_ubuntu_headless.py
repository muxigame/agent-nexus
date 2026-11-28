"""The Ubuntu launcher on a server without a desktop (packaging/ubuntu/ubuntu_launcher.py)."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "packaging" / "ubuntu" / "ubuntu_launcher.py"
BUILD = Path(__file__).resolve().parents[1] / "scripts" / "build_ubuntu.py"


@pytest.fixture()
def launcher(tmp_path, monkeypatch):
    monkeypatch.setenv("ANX_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("USER", "anx")
    spec = importlib.util.spec_from_file_location("ubuntu_launcher_headless", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def run(command, **kwargs):
        calls.append(list(command))
        stdout = ""
        if command[:3] == ["systemctl", "--user", "is-active"]:
            stdout = module._active
        if command[:3] == ["systemctl", "--user", "is-enabled"]:
            stdout = "enabled"
        if command[:2] == ["loginctl", "show-user"]:
            stdout = "no"
        return subprocess.CompletedProcess(command, 0, stdout + "\n", "")

    module._active = "inactive"
    module.calls = calls
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.subprocess, "call", lambda command, **kwargs: calls.append(list(command)) or 0)
    monkeypatch.setattr(module, "prepare", lambda: calls.append(["prepare"]))
    monkeypatch.setattr(module, "desktop", lambda: calls.append(["desktop"]) or 0)
    return module


def test_no_display_prints_the_headless_commands(launcher, monkeypatch, capsys):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert launcher.main([]) == 0
    out = capsys.readouterr().out
    assert "agent-nexus join" in out and "agent-nexus service enable" in out and "ssh -L 8399" in out
    assert ["desktop"] not in launcher.calls


def test_display_without_python3_gi_is_headless_too(launcher, monkeypatch, capsys):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setitem(sys.modules, "gi", None)  # import gi -> ImportError
    assert launcher.main([]) == 0
    assert "agent-nexus service enable" in capsys.readouterr().out
    assert ["desktop"] not in launcher.calls


def test_display_with_gi_opens_the_desktop(launcher, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setitem(sys.modules, "gi", object())
    assert launcher.main([]) == 0
    assert ["desktop"] in launcher.calls


def test_anx_and_join_run_the_bundled_cli(launcher, capsys):
    assert launcher.main(["anx", "status", "--json"]) == 0
    command = launcher.calls[-1]
    assert Path(command[0]).parts[-2:] == ("tools", "uv") and command[1] == "run" and "--offline" not in command[:2]
    assert command[-4:] == ["--root", str(launcher.ROOT), "status", "--json"]
    assert launcher.main(["join", "anx1.CODE", "--agent-id", "box"]) == 0
    assert launcher.calls[-2][-4:] == ["join", "anx1.CODE", "--agent-id", "box"]
    assert "agent-nexus service enable" in capsys.readouterr().out  # not running yet: next step
    assert launcher.main(["join"]) == 1
    assert "用法" in capsys.readouterr().err


def test_service_enable_writes_enables_and_asks_for_linger(launcher, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "wait_ready", lambda timeout=300: 0)
    assert launcher.main(["service", "enable"]) == 0
    unit = launcher.unit_path().read_text()
    assert "Restart=on-failure" in unit and "WantedBy=default.target" in unit and "--serve" in unit
    assert ["systemctl", "--user", "enable", "agent-nexus.service"] in launcher.calls
    assert ["loginctl", "enable-linger", "anx"] in launcher.calls
    assert ["prepare"] in launcher.calls


def test_service_status_logs_disable_and_unknown(launcher, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "api", lambda path, method="GET": {"state": "connected", "hub": "https://hub.example.test"})
    monkeypatch.setattr(launcher, "base_url", lambda: "http://127.0.0.1:8399")
    launcher._active = "active"
    assert launcher.main(["service", "status"]) == 0
    out = capsys.readouterr().out
    assert "active" in out and "connected" in out and "hub.example.test" in out
    assert launcher.main(["service", "logs", "-f"]) == 0
    assert launcher.calls[-1][:2] == ["journalctl", "--user"] and launcher.calls[-1][-1] == "-f"
    assert launcher.main(["service", "disable"]) == 0
    assert ["systemctl", "--user", "disable", "--now", "agent-nexus.service"] in launcher.calls
    assert launcher.main(["service", "explode"]) == 1
    assert launcher.main(["frobnicate"]) == 1
    assert "未知命令" in capsys.readouterr().err


def test_missing_user_bus_is_explained(launcher, monkeypatch, capsys):
    def no_bus(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "Failed to connect to bus: No medium found")
    monkeypatch.setattr(launcher.subprocess, "run", no_bus)
    monkeypatch.setattr(launcher, "install_unit", lambda: None)
    assert launcher.main(["service", "enable"]) == 1
    err = capsys.readouterr().err
    assert "systemd 用户实例" in err and "enable-linger anx" in err


def test_package_recommends_rather_than_depends_on_the_desktop():
    text = BUILD.read_text(encoding="utf-8")
    depends = next(line for line in text.splitlines() if line.startswith("Depends:"))
    recommends = next(line for line in text.splitlines() if line.startswith("Recommends:"))
    for desktop in ("python3-gi", "gir1.2-gtk-3.0", "gir1.2-webkit2-4.1", "xdg-utils"):
        assert desktop not in depends and desktop in recommends


def test_usr_bin_anx_is_agent_nexus_anx(launcher, monkeypatch):
    """/usr/bin/anx runs `agent-nexus anx "$@"`: device commands as the caller, without sudo."""
    text = BUILD.read_text(encoding="utf-8")
    assert '''entries["usr/bin/anx"] = (f'#!/bin/sh\\nexec /{LIB}{LAUNCHER} anx "$@"\\n'.encode(), 0o755)''' in text
    seen = []
    monkeypatch.setattr(launcher, "device_cli", lambda rest: seen.append(rest) or 0)
    assert launcher.main(["anx", "exec", "nas", "--", "uname", "-a"]) == 0
    assert seen == [["exec", "nas", "--", "uname", "-a"]]
