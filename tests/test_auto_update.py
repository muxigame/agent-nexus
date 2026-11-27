"""desktop.update.mode = auto: the switch, and one unattended round on each platform."""
from __future__ import annotations

import asyncio
import json

from agent_nexus import auto_update, desktop, linux_update


def test_switch_is_saved_in_the_config(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_update, "headless", lambda: False)
    (tmp_path / "agent-nexus.json").write_text(json.dumps({"manager": {"port": 1}}), encoding="utf-8")
    assert auto_update.mode(tmp_path) == "auto"  # the default on a desktop too
    assert auto_update.set_mode(tmp_path, "notify") == "notify"
    assert auto_update.mode(tmp_path) == "notify"
    assert auto_update.set_mode(tmp_path, "auto") == "auto"
    data = json.loads((tmp_path / "agent-nexus.json").read_text(encoding="utf-8"))
    assert data["desktop"]["update"]["mode"] == "auto" and data["manager"] == {"port": 1}
    assert auto_update.mode(tmp_path) == "auto"
    try:
        auto_update.set_mode(tmp_path, "sometimes")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown mode accepted")


def test_ubuntu_round_installs_without_a_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "desktop_app", lambda: None)
    monkeypatch.setattr(linux_update, "supported", lambda: True)
    monkeypatch.setattr(linux_update, "check", lambda root: {"available": True, "version": "9.9.9"})
    seen = {}

    def apply(root, *, unattended=False):
        seen["unattended"] = unattended
        return {"installed": True, "version": "9.9.9", "restart": "service"}

    monkeypatch.setattr(linux_update, "apply", apply)
    outcome = asyncio.run(auto_update.run_once(tmp_path))
    assert outcome["result"] == "installed" and seen == {"unattended": True}


def test_unattended_apply_never_uses_pkexec(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(linux_update.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(linux_update.shutil, "which", lambda name: "/usr/bin/" + name)

    class Done:
        returncode = 1

    monkeypatch.setattr(linux_update.subprocess, "run", lambda *a, **k: Done())  # sudo -n refuses
    assert linux_update._privileged(unattended=True) is None
    assert linux_update._privileged(unattended=False) == ["pkexec"]
    monkeypatch.setattr(linux_update.os, "geteuid", lambda: 0, raising=False)
    assert linux_update._privileged(unattended=True) == []


def test_windows_round_uses_the_desktop_shell(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "desktop_app", lambda: tmp_path / "agent-nexus.exe")
    verbs = []
    state = {"checked_at": None, "available": False}

    def run_verb(*args):
        verbs.append(args[0])
        if args[0] == "--check-update":
            state.update(checked_at="now", available=True, version="9.9.9")
        return True

    monkeypatch.setattr(desktop, "run_verb", run_verb)
    monkeypatch.setattr(desktop, "read_update", lambda root: dict(state))
    monkeypatch.setattr(auto_update.asyncio, "sleep", _no_sleep)
    outcome = asyncio.run(auto_update.run_once(tmp_path))
    assert verbs == ["--check-update", "--apply-update"] and outcome["result"] == "installing"


async def _no_sleep(_seconds):
    return None


def test_a_running_copy_job_postpones_the_install(tmp_path, monkeypatch):
    """Installing restarts the manager: not while a file_copy job runs here (at most six hours)."""
    import asyncio
    rounds, sleeps = [], []
    monkeypatch.setattr(auto_update, "mode", lambda root: "auto")

    async def run_once(root):
        rounds.append(root)
        return {"result": "current"}
    monkeypatch.setattr(auto_update, "run_once", run_once)
    busy = {"value": True}

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            busy["value"] = False
        if len(sleeps) > 4:
            raise asyncio.CancelledError
    monkeypatch.setattr(auto_update.asyncio, "sleep", sleep)
    try:
        asyncio.run(auto_update.loop(tmp_path, busy=lambda: busy["value"]))
    except asyncio.CancelledError:
        pass
    # first check + two busy rounds: no install; then idle rounds install
    assert len(rounds) == 2
