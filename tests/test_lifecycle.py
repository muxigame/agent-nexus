"""A manager that disappears must leave a record of how, and of where it was stuck."""
import asyncio
import json
import logging
import time

import pytest

from agent_nexus import agent, lifecycle
from agent_nexus.lifecycle import Lifecycle, state_path
from agent_nexus.supervisor import describe_exit


def write_state(root, **data):
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_previous_manager_that_died_is_reported(tmp_path, caplog):
    write_state(tmp_path, pid=999999, create_time=1.0, started=time.time() - 60,
                heartbeat=time.time() - 30, state="running")
    with caplog.at_level(logging.WARNING, logger="agent_nexus.lifecycle"):
        Lifecycle(tmp_path)._report_previous()
    assert "ended without a clean exit" in caplog.text


def test_a_previous_manager_that_never_finished_exiting_is_ended(tmp_path, caplog):
    """A manager whose tray loop never ended lingers with a tray icon: the next one ends it."""
    import subprocess
    import sys

    import psutil
    lingering = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        write_state(tmp_path, pid=lingering.pid, create_time=psutil.Process(lingering.pid).create_time(),
                    started=time.time() - 600, state="exited", requested=True, reason="console API")
        with caplog.at_level(logging.WARNING, logger="agent_nexus.lifecycle"):
            Lifecycle(tmp_path)._report_previous()
        assert lingering.wait(10) is not None
        assert "still running without its console" in caplog.text
    finally:
        if lingering.poll() is None:
            lingering.kill()


def test_requested_exit_is_recorded_for_the_desktop_app(tmp_path):
    life = Lifecycle(tmp_path)
    life.request_exit("tray menu")
    life.note_exit("tray message loop ended")  # the consequence must not overwrite the cause
    life.finish()
    state = json.loads(state_path(tmp_path).read_text(encoding="utf-8"))
    assert state["state"] == "exited" and state["requested"] is True and state["reason"] == "tray menu"


def test_unrequested_exit_is_not_marked_as_requested(tmp_path):
    life = Lifecycle(tmp_path)
    life.note_exit("console HTTP server stopped")
    life.finish()
    state = json.loads(state_path(tmp_path).read_text(encoding="utf-8"))
    assert state["requested"] is False and state["reason"] == "console HTTP server stopped"


def test_stalled_event_loop_dumps_every_thread_once(tmp_path, caplog):
    life = Lifecycle(tmp_path)
    life._log_file = open(tmp_path / "faults.log", "w", encoding="utf-8")
    try:
        life.loop_beat = time.monotonic() - lifecycle.STALL_S - 5
        with caplog.at_level(logging.WARNING, logger="agent_nexus.lifecycle"):
            life.check_loop()
            life.check_loop()
            life.loop_beat = time.monotonic()
            life.check_loop()
    finally:
        life._log_file.close()
    dump = (tmp_path / "faults.log").read_text(encoding="utf-8")
    assert dump.count("event loop stalled") == 1 and "most recent call first" in dump
    assert "has not run for" in caplog.text and "running again" in caplog.text


@pytest.mark.parametrize("code, expected", [
    (3221225477, "0xC0000005, access violation"),
    (-1073741819, "0xC0000005, access violation"),
    (0, "exit code 0"),
    (None, "unknown"),
])
def test_exit_codes_are_decoded(code, expected):
    assert expected in describe_exit(code)


async def test_reconnect_does_not_wait_for_a_hung_local_service(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "CONNECT_CATALOG_TIMEOUT_S", 0.05)
    connected = asyncio.Event()
    hello = {}

    class Gateway:
        rpc_timeout = 1.0
        catalog = {"workspaces": {}, "mcps": {"workspace": {"tools": []}}}

        def __init__(self, *args, **kwargs):
            pass

        async def refresh(self):
            await asyncio.Future()  # a local MCP that never answers

        async def close(self):
            pass

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, message):
            hello.update(json.loads(message))

        async def recv(self):
            connected.set()
            return '{"method":"ready"}'

        async def close(self, **kwargs):
            pass

        def __aiter__(self):
            async def messages():
                await asyncio.Future()
                yield ""
            return messages()

    monkeypatch.setattr(agent, "LocalGateway", Gateway)
    monkeypatch.setattr(agent, "connect", lambda *args, **kwargs: Socket())
    monkeypatch.setenv("LIFECYCLE_TEST_TOKEN", "x" * 40)
    task = asyncio.create_task(agent.run_agent(tmp_path, {"agent_id": "test", "url": "ws://127.0.0.1",
                                                          "token_env": "LIFECYCLE_TEST_TOKEN"}))
    try:
        await asyncio.wait_for(connected.wait(), 2)
        assert hello["catalog"] == Gateway.catalog
    finally:
        task.cancel()
        await asyncio.wait([task], timeout=2)
