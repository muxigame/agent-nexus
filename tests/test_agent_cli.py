"""Finding Claude Code and Codex where the user has them, not only in this process's PATH."""
from __future__ import annotations

import os
from pathlib import Path

from agent_nexus import agent_cli


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    path.chmod(0o755)
    return path


def test_windows_service_finds_the_npm_install_and_the_apps_codex(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_cli, "WINDOWS", True)
    home = tmp_path / "Users" / "someone"
    apps = tmp_path / "Program Files"
    monkeypatch.setenv("ProgramW6432", str(apps))
    service = {"PATH": str(tmp_path / "Windows" / "system32")}  # SYSTEM: no user folders

    assert agent_cli.find("codex", service) == {"available": False, "path_known": False}
    older = touch(apps / "WindowsApps" / "OpenAI.Codex_26.900.1.0_x64__abc" / "app" / "resources" / "codex.exe")
    newer = touch(apps / "WindowsApps" / "OpenAI.Codex_26.1117.3838.0_x64__abc" / "app" / "resources" / "codex.exe")
    found = agent_cli.find("codex", service)
    assert found["path"] == str(newer) and found["source"] == "app" and not found["on_path"]
    assert older.exists()

    user = {"PATH": service["PATH"], "USERPROFILE": str(home), "APPDATA": str(home / "AppData" / "Roaming")}
    npm = touch(home / "AppData" / "Roaming" / "npm" / "codex.cmd")
    found = agent_cli.find("codex", user)  # the user's npm install wins over the app's copy
    assert found["path"] == str(npm) and found["source"] == "npm"
    claude = touch(home / ".local" / "bin" / "claude.exe")  # the native installer's place
    assert agent_cli.find("claude", user)["path"] == str(claude)


def test_on_the_users_path_it_is_typed_by_name(tmp_path, monkeypatch):
    folder = tmp_path / "bin"
    touch(folder / ("codex.exe" if os.name == "nt" else "codex"))
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".EXE;.CMD")
    found = agent_cli.find("codex", {"PATH": str(folder)})
    assert found["on_path"] and found["source"] == "path"


def test_macos_app_and_nvm(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_cli, "WINDOWS", False)
    monkeypatch.setattr(agent_cli.sys, "platform", "darwin")
    home = tmp_path / "home"
    env = {"PATH": "/nonexistent", "HOME": str(home)}
    touch(home / ".nvm" / "versions" / "node" / "v18.20.0" / "bin" / "codex")
    newest = touch(home / ".nvm" / "versions" / "node" / "v22.11.0" / "bin" / "codex")
    found = agent_cli.find("codex", env)
    assert Path(found["path"]) == newest and found["source"] == "npm"
    app = touch(home / "Applications" / "Codex.app" / "Contents" / "Resources" / "codex")
    (newest).unlink()
    (home / ".nvm" / "versions" / "node" / "v18.20.0" / "bin" / "codex").unlink()
    assert Path(agent_cli.find("codex", env)["path"]) == app


def test_runtimes_are_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(agent_cli, "_cache", None)
    monkeypatch.setattr(agent_cli, "user_env", lambda: calls.append(1) or {"PATH": ""})
    monkeypatch.setattr(agent_cli, "candidates", lambda name, env: [])
    agent_cli.runtimes()
    agent_cli.runtimes()
    assert calls == [1]
    agent_cli.runtimes(fresh=True)
    assert calls == [1, 1]
