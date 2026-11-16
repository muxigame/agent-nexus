"""Servers start under the desktop shell and survive a manager restart (Windows)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from agent_nexus import supervisor as supervisor_mod, winspawn
from agent_nexus.supervisor import Supervisor

pytestmark = pytest.mark.skipif(os.name != "nt", reason="parent-process spawning is Windows-only")


def wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def shell():
    """A stand-in for agent-nexus.exe: a long-lived process to hold servers."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    yield process
    process.kill()
    process.wait()


def test_spawn_under_makes_the_shell_the_parent_and_logs_output(tmp_path: Path, shell) -> None:
    log_path = tmp_path / "server.log"
    with open(log_path, "ab") as log:
        child = winspawn.spawn_under(shell.pid, [sys.executable, "-c", "import os; print('hello', os.getcwd()); raise SystemExit(7)"],
                                     tmp_path, dict(os.environ, ANX_PROBE="x"), log, subprocess.CREATE_NO_WINDOW)
    assert child.wait(20) == 7 and child.poll() == 7
    assert f"hello {tmp_path}" in log_path.read_text(encoding="utf-8")


def test_a_running_server_reports_the_shell_as_its_parent(tmp_path: Path, shell) -> None:
    with open(tmp_path / "s.log", "ab") as log:
        child = winspawn.spawn_under(shell.pid, [sys.executable, "-c", "import time; time.sleep(60)"],
                                     tmp_path, dict(os.environ), log, subprocess.CREATE_NO_WINDOW)
    try:
        assert psutil.Process(child.pid).ppid() == shell.pid
        assert child.poll() is None
    finally:
        child.kill()
        assert child.wait(10) is not None


def test_holder_is_the_ancestor_named_by_the_environment() -> None:
    parent = psutil.Process().parent()
    assert winspawn.holder_pid({"ANX_DESKTOP_APP": parent.exe()}) == parent.pid
    assert winspawn.holder_pid({"ANX_DESKTOP_APP": r"C:\nowhere\agent-nexus.exe"}) is None
    assert winspawn.holder_pid({}) is None


def test_servers_survive_a_manager_restart_under_the_shell(tmp_path: Path, shell, monkeypatch) -> None:
    service = tmp_path / "sleeper"
    service.mkdir()
    (service / "mcp-service.json").write_text(json.dumps(
        {"command": [sys.executable, "-c", "import time; time.sleep(60)"], "autostart": True}), encoding="utf-8")
    monkeypatch.setattr(winspawn, "holder_pid", lambda environ=None: shell.pid)
    first = Supervisor(tmp_path)
    first.start("sleeper")
    pid = first.rt["sleeper"].pid
    assert psutil.Process(pid).ppid() == shell.pid
    # The next manager adopts it where it is: same process, nothing restarted.
    second = Supervisor(tmp_path)
    try:
        report = second.autostart()
        assert any("adopted" in line for line in report), report
        assert second.rt["sleeper"].pid == pid
        assert supervisor_mod._orphaned(pid) is False
    finally:
        second.shutdown()
        first.shutdown()
    assert wait_for(lambda: not psutil.pid_exists(pid))
