import asyncio
import time
from pathlib import Path

import pytest

import src.exec_runtime as exec_runtime
from conftest import device_server
from src.server import DeviceServer


def make_server(tmp_path: Path) -> DeviceServer:
    return device_server(tmp_path, watch_poll_interval_s=0.25, watch_notify_timeout_s=0.1)


def wait_for_event(server: DeviceServer, *, watch_id: str, timeout: float = 4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        events = server.watch_manager.events(watch_id=watch_id)
        if events:
            return events[0]
        time.sleep(0.05)
    raise AssertionError("watch event did not arrive")


def test_log_watch_detects_new_training_error(tmp_path: Path) -> None:
    server = make_server(tmp_path)
    try:
        log = tmp_path / "train.log"
        log.write_text("epoch 1 ok\n", encoding="utf-8")
        watch = server.watch_manager.create_watch(
            kind="log",
            target=str(log),
            workdir=str(tmp_path),
            patterns=[r"CUDA out of memory"],
            companion_project_id="project-test",
            poll_interval_s=0.25,
        )
        with log.open("a", encoding="utf-8") as handle:
            handle.write("RuntimeError: CUDA out of memory\n")
        event = wait_for_event(server, watch_id=watch["id"])
        assert event["event_type"] == "log_match"
        assert "CUDA out of memory" in event["summary"]
        assert event["delivered"] is False
    finally:
        server.watch_manager.close()


@pytest.mark.asyncio
async def test_session_watch_sees_exit_without_write_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = make_server(tmp_path)
    monkeypatch.setattr(exec_runtime.sys, "platform", "linux")
    try:
        result = await server.exec_manager.exec_command(
            "sleep 0.8",
            workdir=str(tmp_path),
            yield_time_ms=250,
        )
        session_id = result["session_id"]
        watch = server.watch_manager.create_watch(
            kind="session",
            session_id=session_id,
            companion_project_id="project-test",
            poll_interval_s=0.25,
        )
        event = await asyncio.to_thread(wait_for_event, server, watch_id=watch["id"])
        assert event["event_type"] == "process_completed"
        assert "code 0" in event["summary"]
        status = server.exec_manager.session_status(session_id)
        assert status is not None
        assert status["completed"] is True
        assert status["exit_code"] == 0
    finally:
        server.watch_manager.close()
        await server.exec_manager.close()
