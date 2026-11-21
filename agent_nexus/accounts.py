"""执行账户: which account runs what, when the core runs as root (the Ubuntu system service).

The core (gateway link, updates, P2P, the remote hub) is root; what it runs for others is not:

* ``accounts.mcp``      -- the plugin processes (the device plugin's commands and file edits,
  every other MCP server) run as this account.
* ``accounts.terminal`` -- remote terminals open a login shell of this account.

The default is the account agent-nexus was installed for (``accounts.default``, written by the
system setup: the desktop user, or root on a server installed as root). ``root`` means no switch.
Anywhere else (Windows, a per-user manager) nothing is switched.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from . import paths

PURPOSES = ("mcp", "terminal")


@dataclass
class Account:
    name: str
    uid: int
    gid: int
    home: str
    shell: str
    groups: list[int]

    def env(self, base: dict | None = None) -> dict:
        env = dict(base if base is not None else os.environ)
        for key in ("SUDO_USER", "SUDO_UID", "SUDO_GID", "SUDO_COMMAND", "MAIL", "XAUTHORITY"):
            env.pop(key, None)
        env.update(HOME=self.home, USER=self.name, LOGNAME=self.name, SHELL=self.shell)
        runtime = f"/run/user/{self.uid}"
        if os.path.isdir(runtime):
            env["XDG_RUNTIME_DIR"] = runtime
        else:
            env.pop("XDG_RUNTIME_DIR", None)
        return env

    def popen_args(self) -> dict:
        return {"user": self.uid, "group": self.gid, "extra_groups": self.groups}


def switching_supported() -> bool:
    return sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0


def _config(root: Path) -> dict:
    try:
        data = json.loads(paths.config_path(Path(root)).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def configured(root: Path) -> dict:
    accounts = _config(root).get("accounts")
    accounts = accounts if isinstance(accounts, dict) else {}
    default = accounts.get("default") if isinstance(accounts.get("default"), str) else "root"
    return {"default": default, **{p: (accounts.get(p) if isinstance(accounts.get(p), str) else default) for p in PURPOSES}}


def lookup(name: str) -> Account | None:
    if not sys.platform.startswith("linux"):
        return None
    import grp
    import pwd
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        return None
    groups = sorted({g.gr_gid for g in grp.getgrall() if name in g.gr_mem} | {entry.pw_gid})
    return Account(entry.pw_name, entry.pw_uid, entry.pw_gid, entry.pw_dir, entry.pw_shell or "/bin/sh", groups)


def effective(root: Path, purpose: str) -> Account | None:
    """The account to switch to for ``purpose``, or None (no switch: not root, root itself, unknown)."""
    if not switching_supported():
        return None
    name = configured(root).get(purpose) or "root"
    if name == "root":
        return None
    return lookup(name)


def local_users() -> list[dict]:
    """Accounts a person would pick: root and regular users with a login shell."""
    if not sys.platform.startswith("linux"):
        return []
    import pwd
    users = []
    for entry in pwd.getpwall():
        if entry.pw_uid != 0 and entry.pw_uid < 1000:
            continue
        if entry.pw_shell.endswith(("nologin", "false")) or entry.pw_name == "nobody":
            continue
        users.append({"name": entry.pw_name, "uid": entry.pw_uid, "home": entry.pw_dir, "shell": entry.pw_shell})
    return sorted(users, key=lambda u: (u["uid"] != 0, u["uid"]))


def save(root: Path, changes: dict) -> dict:
    known = {u["name"] for u in local_users()} or {"root"}
    data = _config(root)
    accounts = data.get("accounts") if isinstance(data.get("accounts"), dict) else {}
    for purpose in ("default", *PURPOSES):
        if purpose in changes:
            value = changes[purpose]
            if value is not None and value not in known:
                raise ValueError(f"这台电脑上没有账户 {value}")
            if value is None:
                accounts.pop(purpose, None)
            else:
                accounts[purpose] = value
    data["accounts"] = accounts
    target = paths.write_path(Path(root))
    temporary = target.with_suffix(".json.accounts.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return configured(root)


def direct_command(argv: list[str], cwd: Path) -> list[str]:
    """``uv run [flags] <cmd> ...`` -> ``<cwd>/.venv/bin/<cmd> ...`` for a switched account.

    uv locks its cache and the project environment, which the account cannot write when the
    core (root) prepared them; the environment is complete (synced at install), so the venv's
    own executable runs the same thing."""
    if len(argv) < 3 or Path(argv[0]).stem != "uv" or argv[1] != "run":
        return argv
    rest = list(argv[2:])
    with_value = {"--python", "--project", "--directory", "--with", "--package", "--extra", "--group", "--only-group",
                  "--no-group", "--env-file"}
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        if flag in with_value and rest:
            rest.pop(0)
    if not rest:
        return argv
    venv = Path(cwd) / ".venv" / "bin"
    command = venv / ("python" if rest[0] in ("python", "python3") else rest[0])
    return [str(command), *rest[1:]] if command.exists() else argv


def hand_over(account: Account, *directories: Path) -> None:
    """Give ``account`` the directories a plugin writes (its data, its logs), creating them."""
    for directory in directories:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            for current, dirs, files in os.walk(directory):
                os.chown(current, account.uid, account.gid)
                for name in files:
                    os.chown(os.path.join(current, name), account.uid, account.gid, follow_symlinks=False)
        except OSError:
            pass


def traversable(directory: Path) -> None:
    """Let other accounts pass through ``directory`` (to their own subdirectory) without listing it."""
    try:
        if directory.is_dir() and (directory.stat().st_mode & 0o777) == 0o700:
            os.chmod(directory, 0o711)
    except OSError:
        pass
