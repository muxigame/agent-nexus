"""anx devices / exec / wait / kill end to end: a real gateway on a port, two real device links, each with the real
device plugin (builtins/device) in process, this computer's console (LocalOnly + LocalMcp on /mcp and /mcp/self)
on its own loopback port, and the CLI (agent_nexus.remote_cli) with nothing but ANX_CONSOLE_URL."""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.util
import io
import json
import os
import signal
import socket
import sys
import types
from pathlib import Path

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_nexus import remote_cli
from agent_nexus.agent import HubLink, run_agent
from agent_nexus.cloud import build_cloud_app
from agent_nexus.file_copy_device import CopyJobs, Links
from agent_nexus.file_share import FileShare
from agent_nexus.local_mcp import LocalMcp
from agent_nexus.web import LocalOnly

from test_remote_m19 import network_config

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell commands")
DEVICE_SRC = Path(__file__).resolve().parents[1] / "builtins" / "device" / "src"


def device_module(name: str):
    """builtins/device/src as package ``anx_device_src`` (its own tests import it as ``src``)."""
    if "anx_device_src" not in sys.modules:
        spec = importlib.util.spec_from_file_location("anx_device_src", DEVICE_SRC / "__init__.py",
                                                      submodule_search_locations=[str(DEVICE_SRC)])
        module = importlib.util.module_from_spec(spec)
        sys.modules["anx_device_src"] = module
        spec.loader.exec_module(module)
    return importlib.import_module(f"anx_device_src.{name}")


def device_plugin(home: Path):
    from mcpkit import Service
    from mcpkit.config import ServiceConfig
    server = device_module("server")
    settings = device_module("settings")
    service = Service(ServiceConfig(name="device", root=home, dir=home, port=8400), version="test")
    plugin = server.DeviceServer(service, settings.Config(project_root=str(home), allowed_roots=[str(home)],
                                                          watch_db=str(home / "watchers.sqlite3"),
                                                          companion_url="http://127.0.0.1:9"))
    plugin.manager_asks = None
    return plugin


async def serve(app, listener: socket.socket | None = None):
    if listener is None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False,
                                           lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(.05)
    assert server.started
    return types.SimpleNamespace(server=server, task=task, listener=listener, port=port)


async def stop(served) -> None:
    served.server.should_exit = True
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(served.task, 10)
    served.listener.close()


async def anx(env: dict, *argv: str, stdin: str = "") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = await asyncio.to_thread(remote_cli.main, list(argv), None, stdout=out, stderr=err,
                                   stdin=io.StringIO(stdin), env=env)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
async def network(tmp_path, monkeypatch):
    gateway = await serve(build_cloud_app(network_config(tmp_path, monkeypatch), tmp_path))
    agents, links = [], {}
    try:
        for name, token in (("pc-a", "TOKEN_A"), ("pc-b", "TOKEN_B")):
            root = tmp_path / name
            home = root / "home"
            home.mkdir(parents=True)
            cloud = {"enabled": True, "agent_id": name, "url": f"ws://127.0.0.1:{gateway.port}", "token_env": token,
                     "workspaces": {"default": {"path": str(home), "mcp": "device"}}}
            (root / "agent-nexus.json").write_text(json.dumps({"cloud": cloud}), encoding="utf-8")
            plugin = device_plugin(home)
            status: dict = {}
            hub = HubLink()
            # files/1-2 for anx cp; pc-a runs the copy jobs (straight to pc-b, through the gateway relay here)
            account = types.SimpleNamespace(home=str(home), session=None, uid=os.getuid(), gid=os.getgid())
            share = FileShare(account=lambda account=account: account)
            hub.handlers.update(share.handlers())
            hub.extra_features = ("files/1", "files/2")
            if name == "pc-a":
                async def no_tunnel(_device_id):
                    return None, "网关中转（测试里没有直连）"
                jobs = CopyJobs(root, share, links=Links(no_tunnel, lambda d: None), me=lambda: "pc-a")
                hub.handlers.update(jobs.handlers())
                hub.extra_features = (*hub.extra_features, "fxjob/1")
            ready = asyncio.Event()
            agents.append(asyncio.create_task(run_agent(root, cloud, status=status, transports={"device": plugin.mcp},
                                                        ready=ready, hub=hub)))
            await asyncio.wait_for(ready.wait(), 20)
            for _ in range(100):
                if hub.available:
                    break
                await asyncio.sleep(0.05)
            links[name] = types.SimpleNamespace(root=root, home=home, status=status, hub=hub, plugin=plugin)

        # this computer (pc-a): its device plugin also on HTTP for /mcp/self, its console on loopback
        a = links["pc-a"]
        plugin_http = await serve(a.plugin.mcp.http_app(path="/mcp", stateless_http=True))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        cfg = types.SimpleNamespace(host="127.0.0.1", port=port, root=a.root, mcp_local={})
        lock = types.SimpleNamespace(locked=False)
        local_mcp = LocalMcp(cfg, a.status, a.hub, lock,
                             device_plugin=lambda: (f"http://127.0.0.1:{plugin_http.port}/mcp", None))

        async def cloud(_request):
            return JSONResponse({"agent_id": "pc-a", "joined": True, "hostname": "pc-a"})

        console = await serve(LocalOnly(Starlette(routes=[Route("/mcp", local_mcp), Route("/mcp/self", local_mcp.self_app),
                                                          Route("/api/cloud", cloud)]), "127.0.0.1", port), listener)
        assert local_mcp.state() == "ready", local_mcp.status()
        yield types.SimpleNamespace(env={"ANX_CONSOLE_URL": f"http://127.0.0.1:{port}"}, links=links, lock=lock,
                                    local_mcp=local_mcp, cfg=cfg)
        await stop(console)
        await stop(plugin_http)
    finally:
        for task in agents:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        await stop(gateway)


async def test_devices_lists_both_with_shells_and_workdirs(network):
    code, out, err = await anx(network.env, "devices")
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0].split() == ["NAME", "ID", "ONLINE", "OS", "SHELLS", "WORKDIRS"]
    by_id = {line.split()[1]: line for line in lines[1:]}
    assert "（本机）" in by_id["pc-a"] and "在线" in by_id["pc-a"] and "*" in by_id["pc-a"]
    assert "（本机）" not in by_id["pc-b"]
    assert f"default={network.links['pc-b'].home}" in by_id["pc-b"]
    code, out, _ = await anx(network.env, "devices", "--json")
    rows = json.loads(out)["devices"]
    assert {row["device_id"]: row["self"] for row in rows} == {"pc-a": True, "pc-b": False}


async def test_exec_streams_output_and_returns_the_remote_exit_code(network):
    b = network.links["pc-b"]
    code, out, err = await anx(network.env, "exec", "pc-b", "--", "echo", "hello;", "pwd;", "exit", "7")
    assert code == 7, err
    assert out == f"hello\n{b.home}\n"
    # longer than one wait: the loop goes on until the end, the output in order
    code, out, err = await anx(network.env, "exec", "pc-b", "--", "echo one; sleep 2.5; echo two")
    assert (code, out) == (0, "one\ntwo\n"), err
    # no output at all
    code, out, _ = await anx(network.env, "exec", "pc-b", "--", "true")
    assert (code, out) == (0, "")
    # what the CLI relies on: tooluse(mcp_id=<id>/device) keeps the gateway's exec fields in _meta
    mcp = remote_cli.Mcp(network.env["ANX_CONSOLE_URL"], "/mcp")
    raw = await asyncio.to_thread(mcp.call, "tooluse", {"tool": "exec_command", "mcp_id": "pc-b/device",
                                                        "arguments": {"cmd": "echo x", "stdin": ""}})
    mcp.close()
    assert raw["_meta"]["anx/exec"]["exit_code"] == 0 and raw["content"][0]["text"].endswith("\nx\n")
    # stdin is closed: a command reading it does not hang
    code, out, _ = await anx(network.env, "exec", "pc-b", "--timeout", "20", "--", "cat; echo end")
    assert (code, out) == (0, "end\n")


async def test_stdin_passes_the_command_untouched(network):
    command = "x='a b'; echo \"$x\" '$HOME' \"$(printf %s ok)\" `echo bq`\n"
    code, out, err = await anx(network.env, "exec", "pc-b", "--stdin", stdin=command)
    assert (code, out) == (0, "a b $HOME ok bq\n"), err
    code, _, err = await anx(network.env, "exec", "pc-b", "--stdin", "--", "ls")
    assert code == 2 and "--stdin" in err


async def test_this_computer_runs_through_mcp_self_even_unjoined(network):
    a = network.links["pc-a"]
    for device in (".", "pc-a"):
        code, out, err = await anx(network.env, "exec", device, "--cwd", str(a.home), "--", "pwd; exit 3")
        assert (code, out) == (3, f"{a.home}\n"), err
    # not joined any more: other devices are unavailable (251), this computer still works
    network.links["pc-a"].status["state"] = "disabled"
    assert network.local_mcp.state() == "not_joined"
    code, out, _ = await anx(network.env, "exec", "--", "echo local")
    assert (code, out) == (0, "local\n")
    code, _, err = await anx(network.env, "exec", "pc-b", "--", "echo hi")
    assert code == remote_cli.EXIT_UNAVAILABLE and "还没加入网关" in err
    code, out, err = await anx(network.env, "devices")
    assert code == 0 and "未加入网关" in err and "pc-a" in out
    # locked: neither route
    network.lock.locked = True
    code, _, err = await anx(network.env, "exec", ".", "--", "echo hi")
    assert code == remote_cli.EXIT_UNAVAILABLE and "锁定" in err


async def test_detach_wait_kill_and_timeout(network):
    code, out, err = await anx(network.env, "exec", "pc-b", "--detach", "--", "sleep 1.5; echo late; exit 4")
    assert code == 0, err
    token = out.strip()
    assert token.startswith("pc-b:") and token.split(":")[1].isdigit()
    code, out, err = await anx(network.env, "wait", token)
    assert (code, out) == (4, "late\n"), err
    # the session is gone once collected
    code, _, err = await anx(network.env, "wait", token)
    assert code in (4, remote_cli.EXIT_TOOL)

    code, out, err = await anx(network.env, "exec", "pc-b", "--timeout", "1.5", "--", "echo start; sleep 30")
    assert code == remote_cli.EXIT_TIMEOUT and out == "start\n"
    token = err.split("anx wait ", 1)[1].split()[0]
    code, _, err = await anx(network.env, "kill", token)
    assert code == 0 and "已停止" in err
    code, _, err = await anx(network.env, "kill", token)
    assert code == remote_cli.EXIT_TOOL and "会话不存在" in err
    code, _, err = await anx(network.env, "wait", "pc-b:999999")
    assert code == remote_cli.EXIT_TOOL and "会话不存在" in err


async def test_device_and_shell_errors(network):
    code, _, err = await anx(network.env, "exec", "nas", "--", "true")
    assert code == remote_cli.EXIT_DEVICE and "nas" in err
    code, _, err = await anx(network.env, "exec", "pc-b", "--shell", "cmd", "--", "true")
    assert code == remote_cli.EXIT_SHELL and "可用" in err
    # the device itself refuses an unknown shell on this computer
    code, _, err = await anx(network.env, "exec", ".", "--shell", "cmd", "--", "true")
    assert code == remote_cli.EXIT_SHELL
    # both devices are called after this host: a display name that two devices share is refused
    name = (await asyncio.to_thread(lambda: remote_cli.Ctx(network.env["ANX_CONSOLE_URL"], None, None).devices()))[1]
    code, _, err = await anx(network.env, "exec", name["display_name"].upper(), "--", "true")
    assert code == remote_cli.EXIT_DEVICE and "pc-a" in err and "pc-b" in err


async def test_ctrl_c_kills_the_remote_command(network):
    b = network.links["pc-b"]
    marker = b.home / "survived"
    out, err = io.StringIO(), io.StringIO()
    ctx = remote_cli.Ctx(network.env["ANX_CONSOLE_URL"], remote_cli.Out(out), remote_cli.Out(err))
    target = await asyncio.to_thread(ctx.resolve, "pc-b")
    run = remote_cli.ExecRun(target, remote_cli.Out(out), remote_cli.Out(err))
    job = remote_cli.Job()

    def work() -> int:
        return run.start(job, {"cmd": f"echo started; sleep 4; touch {marker}", "workdir": str(b.home)})

    worker = asyncio.ensure_future(asyncio.to_thread(work))
    for _ in range(100):
        if run.session is not None and "started" in out.getvalue():
            break
        await asyncio.sleep(0.05)
    job.cancel.set()
    run.request_kill()
    assert await asyncio.wait_for(worker, 30) == remote_cli.EXIT_INTERRUPTED
    assert "已停止远程命令" in err.getvalue()
    await asyncio.sleep(4.5)
    assert not marker.exists()
    ctx.close()


async def test_cp_copies_a_folder_between_devices(network):
    a, b = network.links["pc-a"], network.links["pc-b"]
    folder = b.home / "data"
    (folder / "sub").mkdir(parents=True)
    (folder / "one.txt").write_text("1", encoding="utf-8")
    (folder / "sub" / "two.bin").write_bytes(os.urandom(300_000))
    code, out, err = await anx(network.env, "cp", f"pc-b:{folder}", f".:{a.home}/")
    assert code == 0, err
    assert out.startswith("完成") and "2 个文件" in out
    assert (a.home / "data" / "sub" / "two.bin").read_bytes() == (folder / "sub" / "two.bin").read_bytes()
    # again: nothing to copy, still a success (resume / sync)
    code, out, err = await anx(network.env, "cp", f"pc-b:{folder}", f".:{a.home}/")
    assert code == 0, err
    # a missing source fails with the job's own outcome
    code, out, err = await anx(network.env, "cp", f"pc-b:{b.home}/missing", f".:{a.home}/x")
    assert code == 4 and out.startswith("失败") and "路径不存在" in err, (out, err)
    code, _, err = await anx(network.env, "cp", "C:\\x", f".:{a.home}/x")
    assert code == 2 and ".:C:" in err


async def test_a_real_ctrl_c_stops_the_command(network):
    """The process as a shell runs it: SIGINT -> exec_kill -> exit 130."""
    b = network.links["pc-b"]
    marker = b.home / "after-sigint"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "agent_nexus.cli", "exec", "pc-b", "--", f"echo started; sleep 5; touch {marker}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={**os.environ, **network.env})
    line = await asyncio.wait_for(proc.stdout.readline(), 30)
    assert line == b"started\n"
    proc.send_signal(signal.SIGINT)
    _, err = await asyncio.wait_for(proc.communicate(), 30)
    assert proc.returncode == remote_cli.EXIT_INTERRUPTED, err.decode()
    assert "已停止远程命令" in err.decode()
    await asyncio.sleep(5.5)
    assert not marker.exists()


async def test_ctrl_c_through_the_deb_launcher_stops_the_command(network, tmp_path):
    """``agent-nexus anx exec`` (the only anx in the owner's terminals on Linux): the launcher replaces itself, so
    the terminal's SIGINT reaches anx itself and is not answered by a SIGKILL 0.25 s later."""
    b = network.links["pc-b"]
    marker = b.home / "after-launcher-sigint"
    root = tmp_path / "deb-root"
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f"#!/bin/sh\nexec '{sys.executable}' \"$@\"\n", encoding="utf-8")
    python.chmod(0o755)
    launcher = Path(__file__).resolve().parents[1] / "packaging" / "ubuntu" / "ubuntu_launcher.py"
    env = {**os.environ, **network.env, "ANX_ROOT": str(root), "ANX_SYSTEM_ROOT": str(tmp_path / "no-system")}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(launcher), "anx", "exec", "pc-b", "--", f"echo started; sleep 5; touch {marker}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    line = await asyncio.wait_for(proc.stdout.readline(), 30)
    assert line == b"started\n"
    proc.send_signal(signal.SIGINT)
    _, err = await asyncio.wait_for(proc.communicate(), 30)
    assert proc.returncode == remote_cli.EXIT_INTERRUPTED, err.decode()
    assert "已停止远程命令" in err.decode() and "Traceback" not in err.decode()
    await asyncio.sleep(5.5)
    assert not marker.exists()


async def test_a_closed_pipe_stops_the_remote_command(network, tmp_path):
    """``anx exec pc-b -- <long producer> | head -1``: anx notices the closed pipe, stops the remote command and
    exits 141 without the interpreter's "Exception ignored" noise."""
    b = network.links["pc-b"]
    marker = b.home / "after-head"
    script = f"i=0; while [ $i -lt 60 ]; do echo line$i; i=$((i+1)); sleep 0.1; done; touch {marker}"
    shell = (f"'{sys.executable}' -m agent_nexus.cli exec pc-b -- '{script}' | head -1; "
             "echo \"status=${PIPESTATUS[0]}\" >&2")
    proc = await asyncio.create_subprocess_exec("bash", "-c", shell, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, env={**os.environ, **network.env})
    out, err = await asyncio.wait_for(proc.communicate(), 60)
    text = err.decode()
    assert out == b"line0\n" and "status=141" in text, text
    assert "Exception ignored" not in text and "Traceback" not in text
    await asyncio.sleep(6.5)
    assert not marker.exists()
