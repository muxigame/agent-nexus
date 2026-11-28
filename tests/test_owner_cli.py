"""anx in the owner's own terminals: the macOS link (agent_nexus/owner_cli.py) and the shim texts."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from agent_nexus import owner_cli
from agent_nexus.agents import anx_shim

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks and sh")


def fake_runtime(root, tag="0.9.0-abcd1234"):
    runtime = root / "runtime" / tag
    python = runtime / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\necho "py $*"\n')
    python.chmod(0o755)
    (root / "runtime" / "current").write_text(tag)
    return runtime


def test_the_script_follows_runtime_current_and_explains_a_missing_runtime(tmp_path):
    root = tmp_path / "Application Support" / "AgentNexus"
    script = owner_cli.write_script(root)
    assert script == root / "bin" / "anx" and os.access(script, os.X_OK)
    missing = subprocess.run([str(script), "devices"], capture_output=True, text=True)
    assert missing.returncode == 127 and "请先打开 Agent Nexus" in missing.stderr
    fake_runtime(root)
    done = subprocess.run([str(script), "exec", "nas", "--", "ls"], capture_output=True, text=True)
    assert done.stdout.strip() == f"py -P -m agent_nexus.cli --root {root} exec nas -- ls"
    (root / "runtime" / "current").write_text("../../etc")
    assert subprocess.run([str(script)], capture_output=True).returncode == 127
    assert owner_cli.write_script(root) == script  # idempotent


def test_link_uses_a_user_dir_on_path_and_never_a_system_dir(tmp_path):
    root, home = tmp_path / "root", tmp_path / "home"
    system = tmp_path / "usr-local-bin"
    system.mkdir()  # writable, on PATH: still never used (other accounts would run this user's script)
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    result = owner_cli.link(root, home=home, path_dirs=[str(system), str(local_bin)], system_dir=system, shell="/bin/zsh")
    assert result == {"ok": True, "linked": str(local_bin / "anx"), "changed": True}
    assert os.readlink(local_bin / "anx") == str(root / "bin" / "anx") and not (system / "anx").exists()
    assert not (home / ".zprofile").exists()  # already on PATH: no profile edit
    assert owner_cli.link(root, home=home, path_dirs=[], system_dir=system)["changed"] is False
    assert owner_cli.status(root, home=home, system_dir=system)["linked"] == str(local_bin / "anx")
    assert owner_cli.unlink(root, home=home, system_dir=system, shell="/bin/zsh")["removed"] == [str(local_bin / "anx")]
    assert not (local_bin / "anx").is_symlink()

    # someone else's anx in ~/.local/bin: never replaced; ~/bin is on the login PATH
    (local_bin / "anx").write_text("other")
    (system / "anx").write_text("brew")
    user_bin = home / "bin"
    user_bin.mkdir()
    result = owner_cli.link(root, home=home, path_dirs=[str(local_bin), str(user_bin)], system_dir=system)
    assert result["linked"] == str(user_bin / "anx") and (local_bin / "anx").read_text() == "other"
    view = owner_cli.status(root, home=home, system_dir=system)
    assert sorted(view["foreign"]) == sorted([str(system / "anx"), str(local_bin / "anx")])
    owner_cli.unlink(root, home=home, system_dir=system)
    assert (local_bin / "anx").read_text() == "other" and (system / "anx").read_text() == "brew"


def test_link_puts_local_bin_on_the_login_path_when_no_user_dir_is_on_it(tmp_path):
    root, home = tmp_path / "root", tmp_path / "home"
    home.mkdir()
    (home / ".zprofile").write_text("export FOO=1")
    result = owner_cli.link(root, home=home, path_dirs=["/usr/bin", "/bin"], shell="/bin/zsh")
    assert result["ok"] and result["linked"] == str(home / ".local" / "bin" / "anx")
    assert result["profile"] == str(home / ".zprofile")
    text = (home / ".zprofile").read_text()
    assert text.startswith("export FOO=1\n") and owner_cli.BLOCK in text
    owner_cli.link(root, home=home, path_dirs=[], shell="/bin/zsh")
    assert (home / ".zprofile").read_text().count(owner_cli.BLOCK_BEGIN) == 1
    assert owner_cli.status(root, home=home, shell="/bin/zsh")["profile"] == str(home / ".zprofile")
    done = owner_cli.unlink(root, home=home, shell="/bin/zsh")
    assert done["profile"] == str(home / ".zprofile") and (home / ".zprofile").read_text() == "export FOO=1\n"
    assert not (home / ".local" / "bin" / "anx").is_symlink()
    # bash: the first startup file bash reads (never a new .bash_profile over an existing .profile)
    (home / ".profile").write_text("")
    assert owner_cli.profile_for(home, "/bin/bash") == home / ".profile"
    assert owner_cli.profile_for(home, "/usr/local/bin/fish") is None
    assert not owner_cli.link(root, home=home, path_dirs=[], shell="/usr/local/bin/fish")["ok"]


def test_a_dangling_link_explains_how_to_remove_it(tmp_path):
    root, home = tmp_path / "root", tmp_path / "home"
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    owner_cli.link(root, home=home, path_dirs=[str(local_bin)])
    missing = subprocess.run([str(local_bin / "anx")], capture_output=True, text=True)
    assert missing.returncode == 127 and f'rm "{local_bin / "anx"}"' in missing.stderr


def test_the_git_bash_shim_text_matches_the_desktop_shell():
    rust = (owner_cli.Path(__file__).resolve().parents[1] / "desktop" / "src-tauri" / "src" / "cli_path.rs").read_text()
    expected = anx_shim.GIT_BASH_SHIM.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    assert f'const GIT_BASH_SHIM: &str = "{expected}";' in rust
