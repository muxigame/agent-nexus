"""muxi-p2p-agent ends with the manager, also when the manager is killed (no orphan keeps its log locked)."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest

from agent_nexus import lifeline, p2p

REPO = Path(__file__).resolve().parents[1]
posix_only = pytest.mark.skipif(os.name == "nt", reason="the fake agent is a script; Windows is covered by test_winjob")


def alive(pid: int) -> bool:
    """Running; a zombie (exited, not yet reaped by its new parent) counts as gone."""
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def wait_for(predicate, timeout=15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def fake_agent(tmp_path: Path) -> Path:
    exe = tmp_path / "muxi-p2p-agent"
    exe.write_text(f"#!{sys.executable}\nimport time\nprint('registered', flush=True)\ntime.sleep(300)\n")
    exe.chmod(0o755)
    return exe


def start_manager(tmp_path: Path, method: str) -> tuple[subprocess.Popen, int]:
    """A stand-in manager running the real sidecar; returns it and the agent's pid."""
    script = tmp_path / "manager.py"
    script.write_text(textwrap.dedent(f"""
        import asyncio, json, sys
        sys.path.insert(0, {str(REPO)!r})
        from agent_nexus import lifeline, p2p
        lifeline.METHOD = {method!r}

        async def main():
            sidecar = p2p.AgentSidecar({str(fake_agent(tmp_path))!r})
            sidecar.apply({{"control_plane": "http://127.0.0.1:9/p2p", "server_id": "dev", "token": "t"}}, 1)
            for _ in range(300):
                if sidecar.status.get("last") == "registered":
                    break
                await asyncio.sleep(0.05)
            else:
                sys.exit(f"the agent did not start: {{sidecar.status}}")
            print(json.dumps({{"agent": sidecar.status["pid"]}}), flush=True)
            await asyncio.sleep(300)

        asyncio.run(main())
    """), encoding="utf-8")
    manager = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    line = manager.stdout.readline()
    assert line, "the stand-in manager did not start the agent"
    return manager, json.loads(line)["agent"]


@posix_only
@pytest.mark.parametrize("method", [
    pytest.param("pdeathsig", marks=pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")),
    "watchdog",
])
def test_a_killed_manager_takes_the_p2p_agent_along(tmp_path: Path, method: str) -> None:
    manager, agent = start_manager(tmp_path, method)
    try:
        assert alive(agent)
        manager.send_signal(signal.SIGKILL)  # no clean shutdown: only the lifeline can end the agent
        manager.wait(10)
        assert wait_for(lambda: not alive(agent)), "muxi-p2p-agent outlived the killed manager"
    finally:
        manager.kill()
        if alive(agent):
            os.kill(agent, signal.SIGKILL)


@posix_only
@pytest.mark.parametrize("method", ["pdeathsig", "watchdog", None])
def test_the_normal_stop_still_ends_the_agent_and_its_watchdog(tmp_path: Path, method: str | None) -> None:
    async def run() -> tuple[int, lifeline.Lifeline]:
        original = lifeline.METHOD
        lifeline.METHOD = method
        try:
            sidecar = p2p.AgentSidecar(str(fake_agent(tmp_path)))
            sidecar.apply({"control_plane": "http://127.0.0.1:9/p2p", "server_id": "dev", "token": "t"}, 1)
            for _ in range(300):
                if sidecar.status.get("last") == "registered":
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail(f"the agent did not start: {sidecar.status}")
            pid, tie = sidecar.status["pid"], sidecar.lifeline
            watchdog = tie.watchdog
            assert (watchdog is not None) == (method == "watchdog")
            await sidecar.stop()
            assert sidecar.status == {"state": "stopped"}
            return pid, watchdog
        finally:
            lifeline.METHOD = original

    pid, watchdog = asyncio.run(run())
    assert wait_for(lambda: not alive(pid))
    if watchdog is not None:
        assert watchdog.poll() is not None
