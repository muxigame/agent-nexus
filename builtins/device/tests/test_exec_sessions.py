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
