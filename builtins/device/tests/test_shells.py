"""exec_command's shell choice: detection, clear errors, the exec_wait resume and the wait cap."""
import asyncio
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from src import shells as shells_mod
from src.exec_runtime import CMD_ENV, MAX_WAIT_MS, ShellUnavailable, UnifiedExecManager
from src.policy import Policy
from src.settings import Config

from conftest import device_server

POSIX = sys.platform != "win32"


def manager(root: Path, **limits) -> UnifiedExecManager:
    config = Config(project_root=str(root), allowed_roots=[str(root)])
    config = replace(config, limits=replace(config.limits, **limits))
    shells_mod.clear_cache()
    return UnifiedExecManager(config, Policy(config))


# -- detection on Windows, with stubbed probes -----------------------------------
WINDOWS_ENV = {"SystemRoot": r"C:\Windows", "ComSpec": r"C:\Windows\System32\cmd.exe",
               "ProgramFiles": r"C:\Program Files", "LOCALAPPDATA": r"C:\Users\me\AppData\Local",
               "USERNAME": "me", "USERPROFILE": r"C:\Users\me"}


def stub_windows(monkeypatch, files: set[str], which: dict[str, str], distros=(), with_bash=None):
    shells_mod.clear_cache()
    monkeypatch.setattr(shells_mod, "_is_file", lambda path: path in files)
    monkeypatch.setattr(shells_mod, "_which", lambda name: which.get(name))
    monkeypatch.setattr(shells_mod, "_wsl_distros", lambda: list(distros))
    has_bash = set(distros) if with_bash is None else set(with_bash)
    monkeypatch.setattr(shells_mod, "_wsl_has_bash", lambda distro: distro in has_bash)


def test_windows_bash_is_git_bash_never_the_system32_launcher(monkeypatch):
    git_bash = r"C:\Program Files\Git\bin\bash.exe"
    files = {r"C:\Windows\System32\cmd.exe", git_bash, r"C:\Windows\System32\bash.exe",
             r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", r"C:\Program Files\Git\bin\sh.exe"}
    stub_windows(monkeypatch, files, {"bash": r"C:\Windows\System32\bash.exe", "git": r"C:\Program Files\Git\cmd\git.exe",
                                      "wsl.exe": r"C:\Windows\System32\wsl.exe"}, distros=["Ubuntu"])
    found = shells_mod.detect(r"D:\nowhere\bash.exe", platform="win32", env=WINDOWS_ENV)
    assert found["bash"].path == git_bash and found["bash"].kind == "git-bash"
    assert found["sh"].path == r"C:\Program Files\Git\bin\sh.exe"
    assert found["cmd"].path == r"C:\Windows\System32\cmd.exe"
    assert found["powershell"].path.endswith("powershell.exe")
    assert "pwsh" not in found and "zsh" not in found
    assert shells_mod.ordered(found, "bash", platform="win32") == ["bash", "powershell", "cmd", "sh"]


def test_windows_without_git_bash_uses_msys2_then_wsl_but_not_under_system(monkeypatch):
    msys = r"C:\msys64\usr\bin\bash.exe"
    stub_windows(monkeypatch, {msys, r"C:\Windows\System32\cmd.exe"}, {})
    assert shells_mod.detect("", platform="win32", env=WINDOWS_ENV)["bash"].kind == "msys2"

    stub_windows(monkeypatch, {r"C:\Windows\System32\cmd.exe"}, {"wsl.exe": r"C:\Windows\System32\wsl.exe"},
                 distros=["Ubuntu-22.04"])
    found = shells_mod.detect("", platform="win32", env=WINDOWS_ENV)
    assert found["bash"].kind == "wsl:Ubuntu-22.04" and found["bash"].wsl_distro == "Ubuntu-22.04"

    monkeypatch.setenv("USERNAME", "SYSTEM")
    stub_windows(monkeypatch, {r"C:\Windows\System32\cmd.exe"}, {"wsl.exe": r"C:\Windows\System32\wsl.exe"},
                 distros=["Ubuntu"])
    assert "bash" not in shells_mod.detect("", platform="win32", env={**WINDOWS_ENV, "USERNAME": "SYSTEM"})


def test_windows_launch_needs_no_second_quoting_layer(tmp_path):
    exec_manager = manager(tmp_path)
    git = shells_mod.ShellInfo("bash", r"C:\Program Files\Git\bin\bash.exe", "git-bash")
    command = """echo "a b" 'x' *.py $HOME"""
    launch = exec_manager._shell_launch(git, command, True, platform="win32")
    # The command never reaches the MSYS/Cygwin command-line parser or its globbing.
    assert launch["argv"][:2] == [git.path, "-lc"] and command not in " ".join(launch["argv"])
    assert launch["env"] == {CMD_ENV: command}
    wsl = shells_mod.ShellInfo("bash", r"C:\Windows\System32\wsl.exe", "wsl:Ubuntu")
    launch = exec_manager._shell_launch(wsl, command, False, platform="win32")
    assert launch["argv"][:6] == [wsl.path, "-d", "Ubuntu", "-e", "bash", "-c"]
    assert launch["env"]["WSLENV"] == f"{CMD_ENV}/u"
    cmd = shells_mod.ShellInfo("cmd", r"C:\Windows\System32\cmd.exe")
    # /d as in 0.8.34 (no AutoRun hooks such as conda init or clink), and the command as typed.
    assert exec_manager._shell_launch(cmd, 'dir "C:\\Program Files"', True, platform="win32") == \
        {"cmdline": '"C:\\Windows\\System32\\cmd.exe" /d /s /c "dir "C:\\Program Files""'}
    ps = shells_mod.ShellInfo("powershell", "powershell.exe")
    assert exec_manager._shell_launch(ps, "Get-Date", False, platform="win32")["argv"] == \
        ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", "Get-Date"]


def test_windows_bash_wrapper_leaves_no_shell_variable_behind(tmp_path):
    """The wrapper evals the variable itself: the command sees no `c` (or any other) helper variable."""
    from src.exec_runtime import _BASH_EVAL
    assert _BASH_EVAL == f'eval "unset {CMD_ENV}; ${CMD_ENV}"'
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash")
    import subprocess
    command = 'echo "[${c-unset}] [${ANX_EXEC_CMD-gone}]"; printenv ANX_EXEC_CMD || echo no-env'
    done = subprocess.run([bash, "-c", _BASH_EVAL], env={"PATH": "/usr/bin:/bin", CMD_ENV: command},
                          capture_output=True, text=True, timeout=10)
    assert done.stdout.split() == ["[unset]", "[gone]", "no-env"]


def test_wsl_launcher_paths_get_the_command_through_wslenv(tmp_path):
    """A bash_path / override / explicit shell pointing at the WSL launcher must not eval ''."""
    exec_manager = manager(tmp_path)
    for path, head in ((r"C:\Windows\System32\bash.exe", [r"C:\Windows\System32\bash.exe", "-lc"]),
                       (r"C:\Windows\System32\wsl.exe", [r"C:\Windows\System32\wsl.exe", "-e", "bash", "-lc"]),
                       (r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\bash.exe", None)):
        launch = exec_manager._shell_launch(shells_mod.ShellInfo("bash", path), "ls", True, platform="win32")
        assert launch["env"]["WSLENV"] == f"{CMD_ENV}/u" and launch["env"][CMD_ENV] == "ls"
        if head:
            assert launch["argv"][:len(head)] == head
    stub_windows(monkeypatch := pytest.MonkeyPatch(), {r"C:\Windows\System32\cmd.exe", r"C:\Windows\System32\bash.exe"}, {})
    try:
        found = shells_mod.detect("", {"bash": r"C:\Windows\System32\bash.exe"}, platform="win32", env=WINDOWS_ENV)
        assert found["bash"].kind == "wsl"
    finally:
        monkeypatch.undo()


def test_wsl_bash_needs_a_distro_that_has_bash(monkeypatch):
    """Docker Desktop's distros have no bash: not advertised, and the default distro is probed."""
    files = {r"C:\Windows\System32\cmd.exe"}
    which = {"wsl.exe": r"C:\Windows\System32\wsl.exe"}
    stub_windows(monkeypatch, files, which, distros=["Ubuntu"], with_bash=[])
    assert "bash" not in shells_mod.detect("", platform="win32", env=WINDOWS_ENV)
    stub_windows(monkeypatch, files, which, distros=["Alpine", "Ubuntu"], with_bash=["Ubuntu"])
    assert shells_mod.detect("", platform="win32", env=WINDOWS_ENV)["bash"].kind == "wsl:Ubuntu"
    # The listing itself drops docker-desktop / docker-desktop-data.
    monkeypatch.undo()

    class Done:
        stdout = "docker-desktop\r\ndocker-desktop-data\r\nUbuntu\r\n".encode("utf-16-le")
    monkeypatch.setattr(shells_mod.subprocess, "run", lambda *a, **k: Done())
    assert shells_mod._wsl_distros() == ["Ubuntu"]
    shells_mod.clear_cache()


@pytest.mark.asyncio
async def test_shell_detection_does_not_block_the_event_loop(tmp_path, monkeypatch):
    exec_manager = manager(tmp_path)
    import threading
    loop_thread = threading.get_ident()
    seen = []

    def slow_detect(*args, **kwargs):
        seen.append(threading.get_ident())
        return {}
    monkeypatch.setattr(shells_mod, "detect", slow_detect)
    await exec_manager.shells_async()
    assert seen and seen[0] != loop_thread


@pytest.mark.asyncio
async def test_command_line_spawn_runs_a_program(tmp_path):
    """The private-transport path that keeps cmd's /d (smoke test of the plumbing on POSIX)."""
    from src.exec_runtime import _spawn_command_line
    true = shutil.which("true")
    if true is None or not POSIX:
        pytest.skip("POSIX smoke test")
    proc = await _spawn_command_line(true, executable=true, cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"})
    assert await asyncio.wait_for(proc.wait(), 10) == 0


def test_posix_launch_is_unchanged(tmp_path):
    exec_manager = manager(tmp_path)
    info = shells_mod.ShellInfo("bash", "/bin/bash")
    assert exec_manager._shell_launch(info, "echo hi", True, platform="linux") == {"argv": ["/bin/bash", "-lc", "echo hi"]}


# -- running ------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.skipif(not POSIX, reason="POSIX shells")
async def test_named_shells_run_and_report_themselves(tmp_path):
    exec_manager = manager(tmp_path)
    try:
        available = exec_manager.available_shells()
        assert "bash" in available and "sh" in available
        for name in ("bash", "sh", "BASH", "bash.exe"):
            result = await exec_manager.exec_command("""printf '%s|' "a b" 'x' 中文""", shell=name, login=False)
            assert result["output"] == "a b|x|中文|" and result["exit_code"] == 0
            assert result["shell"] in ("bash", "sh")
        default = await exec_manager.exec_command("echo $0", login=False)
        assert default["shell"] == exec_manager.default_shell_name()
        # An existing path keeps working (back compatible), named by its file.
        legacy = await exec_manager.exec_command("print('py')", shell=sys.executable, yield_time_ms=5000)
        assert legacy["output"].strip() == "py"
    finally:
        await exec_manager.close()


@pytest.mark.asyncio
async def test_missing_shell_is_a_clear_error_listing_the_available_ones(tmp_path):
    exec_manager = manager(tmp_path)
    missing = "cmd" if POSIX else "zsh"
    if missing in exec_manager.available_shells():
        pytest.skip(f"{missing} is installed here")
    with pytest.raises(ShellUnavailable) as raised:
        await exec_manager.exec_command("echo hi", shell=missing)
    message = str(raised.value)
    assert f"shell '{missing}' is not available" in message and "available:" in message
    assert exec_manager.default_shell_name() in message
    with pytest.raises(ShellUnavailable):
        await exec_manager.exec_command("echo hi", shell="/no/such/shell")


@pytest.mark.asyncio
async def test_exec_command_tool_reports_the_missing_shell_and_device_info_lists_shells(tmp_path):
    server = device_server(tmp_path, default_workdir=str(tmp_path))
    try:
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        assert "exec_wait" in tools and "stdin" in tools["exec_command"].parameters["properties"]
        missing = "cmd" if POSIX else "zsh"
        if missing not in server.exec_manager.available_shells():
            result = (await server.mcp.call_tool("exec_command", {"cmd": "echo hi", "shell": missing})).structured_content
            assert result["status"] == "error" and "available:" in result["error"]
        info = await server.device_info()
        assert info["shells"][0] == info["shell_default"] == server.exec_manager.default_shell_name()
    finally:
        await server.exec_manager.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not POSIX, reason="POSIX shells")
async def test_stdin_is_fed_then_closed(tmp_path):
    exec_manager = manager(tmp_path)
    try:
        result = await exec_manager.exec_command("sort", stdin="b\na\n", shell="sh", login=False, yield_time_ms=5000)
        assert result["output"] == "a\nb\n" and result["exit_code"] == 0
    finally:
        await exec_manager.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not POSIX, reason="POSIX shells")
async def test_large_stdin_to_a_command_that_ignores_it_does_not_hold_the_call(tmp_path):
    exec_manager = manager(tmp_path)
    try:
        loop = asyncio.get_running_loop()
        began = loop.time()
        result = await exec_manager.exec_command("sleep 8", stdin="x" * 400_000, shell="sh", login=False,
                                                 yield_time_ms=1000)
        assert loop.time() - began < 3, "the yield window starts at once"
        assert result.get("session_id") and result.get("exit_code") is None
        killed = await exec_manager.kill(result["session_id"])
        assert killed["exit_code"] is not None
    finally:
        await exec_manager.close()


@pytest.mark.asyncio
async def test_waits_are_capped_and_exec_wait_resumes(tmp_path):
    exec_manager = manager(tmp_path, call_wait_max_s=1.0)
    assert MAX_WAIT_MS == 50_000 and Config().limits.call_wait_max_s == 50.0
    try:
        code = "import time; print('start', flush=True); time.sleep(2.5); print('end', flush=True)"
        loop = asyncio.get_running_loop()
        began = loop.time()
        first = await exec_manager.exec_command(code, shell=sys.executable, yield_time_ms=150_000)
        assert loop.time() - began < 1.8, "yield_time_ms is capped at call_wait_max_s"
        sid = first["session_id"]
        assert first.get("exit_code") is None
        outputs = [first["output"]]
        for _ in range(10):
            waited = await exec_manager.exec_wait(sid, timeout_ms=150_000)
            outputs.append(waited["output"])
            if waited.get("exit_code") is not None:
                break
            assert waited["session_id"] == sid
        assert waited["exit_code"] == 0
        assert "".join(outputs).split() == ["start", "end"]
    finally:
        await exec_manager.close()


@pytest.mark.asyncio
async def test_a_cancelled_exec_call_leaves_a_visible_session(tmp_path):
    """The client gave up mid-wait: the command keeps running and stays reachable."""
    exec_manager = manager(tmp_path)
    try:
        task = asyncio.create_task(exec_manager.exec_command("import time; time.sleep(30)", shell=sys.executable,
                                                             yield_time_ms=20_000))
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        sessions = exec_manager.list_sessions()
        assert len(sessions) == 1 and sessions[0]["running"]
        killed = await exec_manager.kill(sessions[0]["session_id"])
        assert killed["exit_code"] is not None
    finally:
        await exec_manager.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not POSIX or shutil.which("sleep") is None, reason="POSIX process groups")
async def test_exec_kill_reaches_grandchildren(tmp_path):
    exec_manager = manager(tmp_path)
    marker = tmp_path / "pid"
    try:
        started = await exec_manager.exec_command(f"sleep 30 & echo $! > {marker}; wait", shell="sh", login=False,
                                                  yield_time_ms=500)
        for _ in range(50):
            if marker.exists() and marker.read_text().strip():
                break
            await asyncio.sleep(0.05)
        grandchild = int(marker.read_text())
        await exec_manager.kill(started["session_id"])
        await asyncio.sleep(0.3)
        import os
        try:
            os.kill(grandchild, 0)
            alive = Path(f"/proc/{grandchild}/stat").exists() and \
                Path(f"/proc/{grandchild}/stat").read_text().split()[2] != "Z"
        except ProcessLookupError:
            alive = False
        assert not alive
    finally:
        await exec_manager.close()


@pytest.mark.asyncio
async def test_sleep_and_ask_waits_return_before_the_cap(tmp_path):
    server = device_server(tmp_path, default_workdir=str(tmp_path))
    server.config = replace(server.config, limits=replace(server.config.limits, call_wait_max_s=1.0))
    server.exec_manager.config = server.config
    result = (await server.mcp.call_tool("sleep", {"duration_ms": 3000})).structured_content
    assert result["slept_ms"] == 1000 and result["remaining_ms"] == 2000
