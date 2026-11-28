"""Exec session capacity counts live processes, and uncollected results expire."""
import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from src.exec_runtime import UnifiedExecManager
from src.policy import Policy
from src.settings import Config


def manager(root: Path, **limits) -> UnifiedExecManager:
    config = Config(project_root=str(root), allowed_roots=[str(root)])
    config = replace(config, limits=replace(config.limits, **limits))
    return UnifiedExecManager(config, Policy(config))


async def start(exec_manager: UnifiedExecManager, code: str) -> dict:
    # The shell argv for an unrecognised shell is [shell, "-c", cmd]: run Python directly.
    return await exec_manager.exec_command(code, shell=sys.executable, yield_time_ms=250)


@pytest.mark.asyncio
async def test_finished_uncollected_sessions_do_not_block_new_commands(tmp_path: Path) -> None:
    exec_manager = manager(tmp_path, max_sessions=1)
    try:
        first = await start(exec_manager, "import time; time.sleep(11)")
        assert first.get("session_id")
        with pytest.raises(RuntimeError, match="limit reached"):
            await start(exec_manager, "pass")
        await exec_manager.kill(first["session_id"])

        # A disconnected caller never collects this one; it must not hold the slot.
        lingering = await start(exec_manager, "import time; time.sleep(11)")
        exec_manager._kill_tree(exec_manager._handles[lingering["session_id"]].proc)
        await asyncio.wait_for(exec_manager._handles[lingering["session_id"]].proc.wait(), 10)
        await asyncio.sleep(0.05)
        assert lingering["session_id"] in exec_manager._handles
        later = await start(exec_manager, "print('ok')")
        assert later["exit_code"] == 0
    finally:
        await exec_manager.close()


@pytest.mark.asyncio
async def test_uncollected_results_expire_but_a_late_poll_still_learns_the_exit_code(tmp_path: Path) -> None:
    exec_manager = manager(tmp_path, finished_session_ttl_s=0.0)
    try:
        started = await start(exec_manager, "import time; time.sleep(11)")
        sid = started["session_id"]
        exec_manager._kill_tree(exec_manager._handles[sid].proc)
        await asyncio.wait_for(exec_manager._handles[sid].proc.wait(), 10)
        await asyncio.sleep(0.05)
        await exec_manager._reap_idle()
        assert sid not in exec_manager._handles
        late = await exec_manager.write_stdin(sid)
        assert late["session_id"] == sid and "released" in late["note"]
        assert late["exit_code"] is not None
    finally:
        await exec_manager.close()


def test_only_side_effect_free_tools_skip_confirmation() -> None:
    from src.server import TOOL_ANNOTATIONS
    assert TOOL_ANNOTATIONS["update_plan"].read_only_hint is True
    for name in ("exec_command", "write_stdin", "edit_file", "patch_file", "view_image", "exec_kill"):
        assert TOOL_ANNOTATIONS[name].read_only_hint is False, name


@pytest.mark.asyncio
async def test_exec_tail_reads_without_consuming_and_keeps_the_job_alive(tmp_path: Path) -> None:
    exec_manager = manager(tmp_path)
    try:
        started = await exec_manager.exec_command(
            "import sys, time; print('one', flush=True); time.sleep(0.6); print('two', flush=True)",
            shell=sys.executable, yield_time_ms=250)
        sid, job = started["session_id"], started["job"]
        assert isinstance(job, str) and len(job) == 16
        exec_manager._handles[sid].last_used = 0.0
        seen, since, final = "", 0, None
        for _ in range(20):
            tail = await exec_manager.exec_tail(sid, job, since, timeout_ms=2000)
            seen += tail["output"]
            since = tail["seq"]
            if not tail["running"]:
                final = tail
                break
        assert final is not None and final["exit_code"] == 0
        assert "one" in seen and "two" in seen
        # exec_wait still gets everything that was not delivered to it.
        waited = await exec_manager.exec_wait(sid, 2000)
        assert "two" in (started["output"] + waited["output"])
        assert waited["exit_code"] == 0 and waited["job"] == job
        # After the exit was collected the tail still answers from the kept output.
        again = await exec_manager.exec_tail(sid, job, 0, timeout_ms=100)
        assert "two" in again["output"] and again["exit_code"] == 0 and not again["running"]
    finally:
        await exec_manager.close()


@pytest.mark.asyncio
async def test_exec_tail_refreshes_last_used_and_checks_the_job(tmp_path: Path) -> None:
    exec_manager = manager(tmp_path, idle_session_ttl_s=0.5)
    try:
        started = await start(exec_manager, "import time; time.sleep(30)")
        sid = started["session_id"]
        with pytest.raises(ValueError, match="different job"):
            await exec_manager.exec_tail(sid, "0" * 16, 0, timeout_ms=10)
        exec_manager._handles[sid].last_used = 0.0
        await exec_manager.exec_tail(sid, started["job"], 0, timeout_ms=10)
        await exec_manager._reap_idle()
        assert sid in exec_manager._handles  # a followed job is not reaped as idle
        with pytest.raises(KeyError):
            await exec_manager.exec_tail(9999, "x", 0, timeout_ms=10)
    finally:
        await exec_manager.close()


@pytest.mark.asyncio
async def test_output_buffer_is_bounded(tmp_path: Path) -> None:
    exec_manager = manager(tmp_path, max_buffer_chars=20_000)
    try:
        started = await exec_manager.exec_command(
            "import sys, time\nfor i in range(400): sys.stdout.write('x' * 999 + '\\n'); sys.stdout.flush()\n"
            "time.sleep(30)", shell=sys.executable, yield_time_ms=250)
        sid = started["session_id"]
        handle = exec_manager._handles[sid]
        for _ in range(100):
            if handle.seq and handle.base_seq:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        assert handle.buffered <= 20_000 + 9_000
        tail = await exec_manager.exec_tail(sid, started["job"], 0, timeout_ms=10)
        assert tail.get("skipped") is True
        polled = await exec_manager.exec_wait(sid, 10)
        assert "dropped" in started.get("note", "") + polled.get("note", "")
    finally:
        await exec_manager.close()
