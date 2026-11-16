"""Servers end with their manager (Windows job object) and orphans are restarted, not adopted."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest

from agent_nexus import supervisor as supervisor_mod, winjob
from agent_nexus.supervisor import Supervisor

REPO = Path(__file__).resolve().parents[1]
windows_only = pytest.mark.skipif(os.name != "nt", reason="job objects are Windows-only")


def wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


@windows_only
def test_a_killed_manager_takes_its_servers_and_their_children_along(tmp_path: Path) -> None:
    # A stand-in manager: starts one server (which starts a grandchild) the way
    # the supervisor does, reports the pids, then waits to be killed.
    script = tmp_path / "manager.py"
    script.write_text(textwrap.dedent(f"""
        import json, subprocess, sys, time
        sys.path.insert(0, {str(REPO)!r})
        from agent_nexus.supervisor import _spawn
        from agent_nexus import winjob
        from pathlib import Path
        server = _spawn([sys.executable, "-c",
                         "import subprocess, sys, time; "
                         "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
                         "print(c.pid, flush=True); time.sleep(120)"],
                        Path({str(tmp_path)!r}), None, subprocess.PIPE)
        grandchild = int(server.stdout.readline())
        print(json.dumps({{"server": server.pid, "grandchild": grandchild,
                          "in_job": winjob.contains(int(server._handle))}}), flush=True)
        time.sleep(120)
    """), encoding="utf-8")
    manager = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    try:
        info = json.loads(manager.stdout.readline())
        assert info["in_job"] is True
        server, grandchild = psutil.Process(info["server"]), psutil.Process(info["grandchild"])
        manager.kill()  # no clean shutdown: the job alone must end them
        manager.wait(10)
        assert wait_for(lambda: not server.is_running() and not grandchild.is_running())
    finally:
        if manager.poll() is None:
            manager.kill()


@windows_only
def test_servers_started_by_the_supervisor_are_in_its_job(tmp_path: Path) -> None:
    service = tmp_path / "sleeper"
    service.mkdir()
    (service / "mcp-service.json").write_text(json.dumps(
        {"command": [sys.executable, "-c", "import time; time.sleep(60)"]}), encoding="utf-8")
    sup = Supervisor(tmp_path)
    try:
        sup.start("sleeper")
        assert winjob.contains(int(sup.rt["sleeper"].proc._handle))
    finally:
        sup.shutdown()


def test_an_orphaned_server_is_restarted_under_this_manager(tmp_path: Path, monkeypatch) -> None:
    service = tmp_path / "sleeper"
    service.mkdir()
    (service / "mcp-service.json").write_text(json.dumps(
        {"command": [sys.executable, "-c", "import time; time.sleep(60)"], "autostart": True}), encoding="utf-8")
    first = Supervisor(tmp_path)
    first.start("sleeper")
    old_pid = first.rt["sleeper"].pid
    # A second manager adopts the running server through its pid record; make
    # that server look like the orphan an abnormally ended manager leaves.
    monkeypatch.setattr(supervisor_mod, "_orphaned", lambda pid: pid == old_pid)
    second = Supervisor(tmp_path)
    try:
        assert second.rt["sleeper"].pid == old_pid and second.rt["sleeper"].proc is None
        report = second.autostart()
        assert any("restarted under this manager" in line for line in report), report
        assert second.rt["sleeper"].pid != old_pid and second.rt["sleeper"].proc is not None
        assert wait_for(lambda: not psutil.pid_exists(old_pid) or
                        psutil.Process(old_pid).status() == psutil.STATUS_ZOMBIE)
    finally:
        second.shutdown()
        first.shutdown()


def test_a_server_adopted_from_this_process_is_not_an_orphan(tmp_path: Path) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert supervisor_mod._orphaned(child.pid) is False
    finally:
        child.kill()
        child.wait()
