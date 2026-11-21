"""System mode: the core as a root system service, the window and session helper per user.

packaging/ubuntu/ubuntu_launcher.py (--setup-system, system scope), agent_nexus/accounts.py
(执行账户) and agent_nexus/session_helper.py (remote desktop from the logged-in session).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from agent_nexus import accounts, session_helper

LAUNCHER = Path(__file__).resolve().parents[1] / "packaging" / "ubuntu" / "ubuntu_launcher.py"
PACKAGING = LAUNCHER.parent


def load_launcher(monkeypatch, tmp_path, *, system: bool, explicit_root: Path | None = None):
    system_root = tmp_path / "system"
    unit = tmp_path / "agent-nexus.service"
    unit.write_text("[Unit]\n")
    system_root.mkdir(exist_ok=True)
    if system:
        (system_root / ".system").write_text("1\n")
    monkeypatch.setenv("ANX_SYSTEM_ROOT", str(system_root))
    monkeypatch.setenv("ANX_SYSTEM_UNIT_FILE", str(unit))
    monkeypatch.delenv("MCPM_ROOT", raising=False)
    if explicit_root is None:
        monkeypatch.delenv("ANX_ROOT", raising=False)
    else:
        monkeypatch.setenv("ANX_ROOT", str(explicit_root))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    spec = importlib.util.spec_from_file_location("ubuntu_launcher_system", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- the launcher ---------------------------------------------------------------------------

def test_root_is_the_system_root_once_set_up(monkeypatch, tmp_path):
    before = load_launcher(monkeypatch, tmp_path, system=False)
    assert not before.system_mode()
    assert before.ROOT == (tmp_path / "data" / "agent-nexus").resolve()
    after = load_launcher(monkeypatch, tmp_path, system=True)
    assert after.system_mode()
    assert after.ROOT == tmp_path / "system"


@pytest.mark.parametrize("marker", [True, False])
def test_old_user_unit_steps_aside(monkeypatch, tmp_path, marker):
    # Even before --setup-system wrote its marker (the old version's updater restarts its unit).
    legacy = tmp_path / "home/me/.local/share/agent-nexus"
    (tmp_path / "systemd").mkdir()
    monkeypatch.setenv("ANX_SYSTEMD_DIR", str(tmp_path / "systemd"))
    module = load_launcher(monkeypatch, tmp_path, system=marker, explicit_root=legacy)
    calls = []
    monkeypatch.setattr(module.subprocess, "run", lambda command, **kw: calls.append(command))
    monkeypatch.setattr(module, "prepare", lambda: pytest.fail("the old unit must not start the manager"))
    assert module.serve() == 0
    assert calls == [["systemctl", "--user", "disable", "agent-nexus.service"]]


def test_the_system_core_retires_user_units_that_came_back(monkeypatch, tmp_path):
    module = load_launcher(monkeypatch, tmp_path, system=True)
    monkeypatch.setattr(module, "_is_root", lambda: True)
    retired = []
    monkeypatch.setattr(module, "_legacy_roots", lambda: [(tmp_path / "old", "me")])
    monkeypatch.setattr(module, "_retire_legacy", lambda root, owner: retired.append(owner))
    monkeypatch.setattr(module, "prepare", lambda: (_ for _ in ()).throw(SystemExit(7)))
    with pytest.raises(SystemExit):
        module.serve()
    assert retired == ["me"]


def test_system_scope_for_service_commands(monkeypatch, tmp_path):
    module = load_launcher(monkeypatch, tmp_path, system=True)
    calls = []

    def run(command, **kw):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "active\n", "")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module, "wait_ready", lambda: 0)
    assert module.service("restart") == 0
    assert ["systemctl", "enable", "agent-nexus.service"] in calls
    assert ["systemctl", "restart", "agent-nexus.service"] in calls
    assert not any("--user" in command for command in calls)


def test_commands_rerun_with_sudo_in_system_mode(monkeypatch, tmp_path):
    module = load_launcher(monkeypatch, tmp_path, system=True)
    monkeypatch.setattr(module, "_is_root", lambda: False)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/sudo")
    seen = []
    monkeypatch.setattr(module.os, "execvp", lambda file, argv: seen.append(argv) or (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        module.main(["join", "CODE"])
    assert seen[0][:2] == ["sudo", "/usr/bin/python3"]
    assert seen[0][-2:] == ["join", "CODE"]


def test_pick_legacy_prefers_the_installing_user_then_a_joined_root(monkeypatch, tmp_path):
    module = load_launcher(monkeypatch, tmp_path, system=False)
    a, b, c = (tmp_path / name for name in "abc")
    for root in (a, b, c):
        root.mkdir()
        (root / "agent-nexus.json").write_text("{}")
    (b / "secrets/cloud").mkdir(parents=True)
    (b / "secrets/cloud/device-token").write_text("x")
    roots = [(a, "alice"), (b, "bob"), (c, "carol")]
    assert module._pick_legacy(roots, "carol") == (c, "carol")
    assert module._pick_legacy(roots, None) == (b, "bob")
    assert module._pick_legacy([], None) is None


def test_migrate_copies_state_not_logs_and_sets_the_default_account(monkeypatch, tmp_path):
    module = load_launcher(monkeypatch, tmp_path, system=True)
    monkeypatch.setattr(module.os, "chown", lambda *a, **kw: None, raising=False)
    source = tmp_path / "legacy"
    (source / "secrets/cloud").mkdir(parents=True)
    (source / "secrets/cloud/device-token").write_text("token")
    (source / "plugin-data/device").mkdir(parents=True)
    (source / "plugin-data/device/state.json").write_text("{}")
    (source / ".anx/logs").mkdir(parents=True)
    (source / ".anx/logs/manager.log").write_text("old log")
    (source / ".anx/lock.json").write_text("{}")
    (source / "agent-nexus.json").write_text(json.dumps({"cloud": {"enabled": True}}))
    module._migrate(source)
    system = tmp_path / "system"
    assert (system / "secrets/cloud/device-token").read_text() == "token"
    assert (system / "plugin-data/device/state.json").is_file()
    assert (system / ".anx/lock.json").is_file()
    assert not (system / ".anx/logs").exists()
    assert (system / "migrated-from").read_text().strip() == str(source)
    module._default_account("me")
    module._default_account("someone-else")  # an existing choice is kept
    data = json.loads((system / "agent-nexus.json").read_text())
    assert data["accounts"] == {"default": "me"}
    assert data["cloud"] == {"enabled": True}


def test_package_ships_the_system_unit_autostart_and_scripts():
    unit = (PACKAGING / "agent-nexus.service").read_text()
    assert "ANX_ROOT=/var/lib/agent-nexus" in unit and "WantedBy=multi-user.target" in unit
    assert "Restart=always" in unit
    autostart = (PACKAGING / "agent-nexus-session.desktop").read_text()
    assert "Exec=/usr/bin/agent-nexus session" in autostart
    postinst = (PACKAGING / "postinst").read_bytes()
    assert b"\r\n" not in postinst
    assert b"--setup-system" in postinst and b"--on-active=5" in postinst and b"SUDO_USER" in postinst
    build = (PACKAGING.parents[1] / "scripts/build_ubuntu.py").read_text(encoding="utf-8")
    for name in ("postinst", "prerm", "usr/lib/systemd/system/agent-nexus.service",
                 "etc/xdg/autostart/agent-nexus-session.desktop"):
        assert name in build


# -- 执行账户 --------------------------------------------------------------------------------

def test_accounts_default_to_the_configured_default(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "WINDOWS", False)
    (tmp_path / "agent-nexus.json").write_text(json.dumps({"accounts": {"default": "me", "terminal": "ops"}}))
    assert accounts.configured(tmp_path) == {"default": "me", "mcp": "me", "terminal": "ops"}
    (tmp_path / "agent-nexus.json").write_text("{}")
    assert accounts.configured(tmp_path) == {"default": "root", "mcp": "root", "terminal": "root"}


def test_no_switch_unless_root_on_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "WINDOWS", False)
    (tmp_path / "agent-nexus.json").write_text(json.dumps({"accounts": {"default": "me"}}))
    monkeypatch.setattr(accounts, "switching_supported", lambda: False)
    assert accounts.effective(tmp_path, "mcp") is None
    monkeypatch.setattr(accounts, "switching_supported", lambda: True)
    monkeypatch.setattr(accounts, "lookup", lambda name: accounts.Account(name, 1000, 1000, "/home/me", "/bin/bash", [1000]))
    assert accounts.effective(tmp_path, "mcp").name == "me"
    (tmp_path / "agent-nexus.json").write_text(json.dumps({"accounts": {"default": "root"}}))
    assert accounts.effective(tmp_path, "terminal") is None


def test_direct_command_skips_uv_for_a_switched_account(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "WINDOWS", False)
    argv = ["uv", "run", "--offline", "--python", "3.12", "python", "-m", "src.server", "--port", "1"]
    assert accounts.direct_command(argv, tmp_path) == argv  # no environment yet: uv builds it
    (tmp_path / ".venv/bin").mkdir(parents=True)
    (tmp_path / ".venv/bin/python").write_text("")
    direct = accounts.direct_command(argv, tmp_path)
    assert direct == [str(tmp_path / ".venv/bin/python"), "-m", "src.server", "--port", "1"]
    assert accounts.direct_command(["node", "server.js"], tmp_path) == ["node", "server.js"]


def test_account_environment_is_its_own():
    account = accounts.Account("me", 1000, 1000, "/home/me", "/bin/zsh", [1000, 27])
    env = account.env({"HOME": "/root", "USER": "root", "SUDO_USER": "x", "PATH": "/usr/bin"})
    assert env["HOME"] == "/home/me" and env["USER"] == "me" and env["SHELL"] == "/bin/zsh"
    assert "SUDO_USER" not in env and env["PATH"] == "/usr/bin"
    assert account.popen_args() == {"user": 1000, "group": 1000, "extra_groups": [1000, 27]}


def test_windows_accounts_follow_the_user_at_the_screen(tmp_path, monkeypatch):
    from agent_nexus import winsession
    monkeypatch.setattr(accounts, "WINDOWS", True)
    (tmp_path / "agent-nexus.json").write_text("{}")
    assert accounts.configured(tmp_path) == {"default": "active", "mcp": "active", "terminal": "active"}
    monkeypatch.setattr(accounts, "switching_supported", lambda: True)
    monkeypatch.setattr(winsession, "active_session",
                        lambda: {"id": "2", "uid": 2, "user": "me", "domain": "PC", "type": "console"})
    monkeypatch.setattr(winsession, "environment",
                        lambda session: {"USERPROFILE": r"C:\Users\me", "Path": r"C:\Users\me\bin", "TEMP": r"C:\Users\me\Temp"})
    account = accounts.effective(tmp_path, "mcp")
    assert account.session == 2 and account.principal == "PC\\me" and account.home == r"C:\Users\me"
    assert account.popen_args() == {"session": 2}
    env = account.env({"ANX_ROOT": r"C:\ProgramData\AgentNexus", "PATH": r"C:\tools;C:\Windows", "TEMP": r"C:\Windows\Temp"})
    assert env["ANX_ROOT"] == r"C:\ProgramData\AgentNexus" and env["TEMP"] == r"C:\Users\me\Temp"
    assert env["Path"].startswith(r"C:\tools;C:\Windows;") and env["Path"].endswith(r"C:\Users\me\bin")
    (tmp_path / "agent-nexus.json").write_text(json.dumps({"accounts": {"mcp": "SYSTEM"}}))
    assert accounts.effective(tmp_path, "mcp") is None
    assert accounts.effective(tmp_path, "terminal").name == "me"
    monkeypatch.setattr(winsession, "active_session", lambda: None)  # nobody logged on: SYSTEM
    assert accounts.effective(tmp_path, "terminal") is None


def test_windows_direct_command_uses_the_scripts_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "WINDOWS", True)
    (tmp_path / ".venv/Scripts").mkdir(parents=True)
    (tmp_path / ".venv/Scripts/python.exe").write_text("")
    argv = ["uv", "run", "python", "-m", "src.server"]
    assert accounts.direct_command(argv, tmp_path) == [str(tmp_path / ".venv/Scripts/python.exe"), "-m", "src.server"]


def test_save_rejects_unknown_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "WINDOWS", False)
    (tmp_path / "agent-nexus.json").write_text("{}")
    monkeypatch.setattr(accounts, "local_users", lambda: [{"name": "root", "uid": 0}, {"name": "me", "uid": 1000}])
    assert accounts.save(tmp_path, {"mcp": "me"})["mcp"] == "me"
    with pytest.raises(ValueError):
        accounts.save(tmp_path, {"terminal": "nobody-here"})


# -- the session helper ---------------------------------------------------------------------

unix_only = pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="needs Linux unix sockets")
supported_platform = pytest.mark.skipif(not (hasattr(socket, "SO_PEERCRED") or sys.platform == "win32"),
                                        reason="Linux unix sockets or Windows sessions")


def _my_identity():
    """The uid (Linux) or the session (Windows) the registry knows this process by."""
    if sys.platform == "win32":
        from agent_nexus import winsession
        return winsession.process_session(os.getpid())
    return os.getuid()


@supported_platform
def test_registry_pipes_the_desktop_stream_to_the_active_sessions_helper(tmp_path, monkeypatch):
    sock = tmp_path / "s" / "session.sock"
    monkeypatch.setattr(session_helper, "socket_path", lambda: sock)
    monkeypatch.setenv("ANX_ROOT", str(tmp_path))
    me = {"id": "7", "uid": _my_identity(), "user": "me", "type": "x11"}

    class FakeDesktop:
        def __init__(self, name):
            pass

        async def serve(self, request, reader, writer):
            writer.write(b"READY " + json.dumps(request).encode() + b"\n")
            await writer.drain()
            writer.write(b"echo:" + await reader.readline())
            await writer.drain()

    import agent_nexus.vnc as vnc
    monkeypatch.setattr(vnc, "DesktopService", FakeDesktop)
    monkeypatch.setenv("XDG_SESSION_ID", "7")

    async def scenario():
        registry = session_helper.SessionRegistry(sock, root=tmp_path, active=lambda: me,
                                                  launch=lambda root, active: False)
        await registry.start()
        helper = asyncio.create_task(session_helper.run_helper())
        for _ in range(100):
            if registry.helpers:
                break
            await asyncio.sleep(0.02)
        assert registry.status()["helper"]

        # the remote hub's side of one viewer: a socket pair standing in for the P2P stream
        ours, theirs = socket.socketpair()
        hub_reader, hub_writer = await asyncio.open_connection(sock=ours)
        view_reader, view_writer = await asyncio.open_connection(sock=theirs)
        served = asyncio.create_task(registry.serve({"service": "desktop", "w": 1}, hub_reader, hub_writer))
        assert json.loads((await view_reader.readline())[len(b"READY "):]) == {"service": "desktop", "w": 1}
        view_writer.write(b"hello\n")
        await view_writer.drain()
        assert await view_reader.readline() == b"echo:hello\n"
        view_writer.close()
        await asyncio.wait_for(served, 5)
        helper.cancel()
        await registry.stop()

    asyncio.run(scenario())


@supported_platform
def test_registry_explains_when_nobody_is_logged_in(tmp_path):
    async def scenario():
        registry = session_helper.SessionRegistry(tmp_path / "x.sock", active=lambda: None)
        ours, theirs = socket.socketpair()
        reader, writer = await asyncio.open_connection(sock=ours)
        view_reader, _ = await asyncio.open_connection(sock=theirs)
        await registry.serve({}, reader, writer)
        writer.close()
        return await view_reader.read()

    data = asyncio.run(scenario())
    assert "没有人登录桌面".encode() in data


def test_active_session_is_the_local_graphical_one(monkeypatch):
    listing = " 102 1000 me seat0 tty2\n7861 1000 me\n"
    shown = {"102": "Name=me\nRemote=no\nType=x11\nClass=user\nActive=yes\nState=active\nUser=1000\n",
             "7861": "Name=me\nRemote=yes\nType=tty\nClass=user\nActive=yes\nState=active\nUser=1000\n"}

    def run(command, **kwargs):
        out = listing if command[1] == "list-sessions" else shown[command[2]]
        return subprocess.CompletedProcess(command, 0, out, "")

    monkeypatch.setattr(session_helper.subprocess, "run", run)
    monkeypatch.setattr(session_helper, "WINDOWS", False)
    assert session_helper.active_session() == {"id": "102", "uid": 1000, "user": "me", "type": "x11"}
    # older systemd without User=: the uid comes from list-sessions
    shown["102"] = shown["102"].replace("User=1000\n", "")
    assert session_helper.active_session()["uid"] == 1000
