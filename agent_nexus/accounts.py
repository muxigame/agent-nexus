"""执行账户: which account runs what, when the core runs as root (the Ubuntu system service).

The core (gateway link, updates, P2P, the remote hub) is root; what it runs for others is not:

* ``accounts.mcp``      -- the plugin processes (the device plugin's commands and file edits,
  every other MCP server) run as this account.
* ``accounts.terminal`` -- remote terminals open a login shell of this account.

The default is the account agent-nexus was installed for (``accounts.default``, written by the
system setup: the desktop user, or root on a server installed as root). ``root`` means no switch.
On Windows the system service (LocalSystem, "为所有用户安装") runs them in the session in front of
the screen, as its user (``active``, the default; agent_nexus/winsession.py), or as ``SYSTEM``.
There is no switching to a user who is not logged on (that would need their password); without
anyone logged on, ``active`` falls back to SYSTEM. A per-user manager never switches.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

PURPOSES = ("mcp", "terminal")
WINDOWS = sys.platform == "win32"
#: Windows: follow whoever is logged on at the screen
ACTIVE = "active"
#: the variables of the core a plugin still needs, over the user's own environment (Windows)
CORE_KEYS = ("ANX_", "MCPM_", "PYTHON", "UV_", "AGENT_NEXUS")


@dataclass
class Account:
    name: str
    uid: int
    gid: int
    home: str
    shell: str
    groups: list[int]
    #: Windows: the session whose user this is (its token starts the processes), and DOMAIN\\user
    session: int | None = None
    principal: str = ""
    profile_env: dict = field(default_factory=dict)

    def env(self, base: dict | None = None) -> dict:
        if self.session is not None:
            return self._windows_env(base)
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

    def _windows_env(self, base: dict | None) -> dict:
        """The user's environment, with what the core passes to its plugins (ANX_ROOT, tools on PATH)."""
        base = dict(base if base is not None else os.environ)
        env = dict(self.profile_env)
        upper = {key.upper() for key in env}
        for key, value in base.items():
            if key.upper().startswith(CORE_KEYS) or key.upper() not in upper:
                env[key] = value
        path_key = next((key for key in env if key.upper() == "PATH"), "Path")
        core_path = next((value for key, value in base.items() if key.upper() == "PATH"), "")
        user_path = next((value for key, value in self.profile_env.items() if key.upper() == "PATH"), "")
        tools = [part for part in core_path.split(";") if part and part.lower() not in user_path.lower()]
        env[path_key] = ";".join([*tools, user_path]) if user_path else core_path
        return env

    def popen_args(self) -> dict:
        if self.session is not None:
            return {"session": self.session}
        return {"user": self.uid, "group": self.gid, "extra_groups": self.groups}


def switching_supported() -> bool:
    if WINDOWS:
        from . import winsession
        return winsession.is_system()
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
    fallback = ACTIVE if WINDOWS else "root"
    default = accounts.get("default") if isinstance(accounts.get("default"), str) else fallback
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
    if WINDOWS:
        return _windows_account(name)
    if name == "root":
        return None
    return lookup(name)


def _windows_account(name: str) -> Account | None:
    """``active``: the user at the screen, in their session; ``SYSTEM`` (or nobody logged on): no switch."""
    from . import winsession
    if name.upper() in ("SYSTEM", "ROOT"):
        return None
    active = winsession.active_session()
    if active is None:
        return None
    if name != ACTIVE and name.lower() not in (active["user"].lower(), f"{active['domain']}\\{active['user']}".lower()):
        return None  # that user is not the one logged on: nothing to switch to
    try:
        profile = winsession.environment(active["uid"])
    except OSError:
        return None
    home = profile.get("USERPROFILE") or os.path.expanduser("~")
    return Account(active["user"], active["uid"], 0, home, os.environ.get("COMSPEC", "cmd.exe"), [],
                   session=active["uid"], principal=f"{active['domain']}\\{active['user']}", profile_env=profile)


def local_users() -> list[dict]:
    """Accounts a person would pick: root and regular users with a login shell (Windows: active / SYSTEM)."""
    if WINDOWS:
        from . import winsession
        active = winsession.active_session()
        now = f"（现在是 {active['user']}）" if active else "（现在没有人登录）"
        return [{"name": ACTIVE, "uid": -1, "label": "当前登录的用户" + now},
                {"name": "SYSTEM", "uid": 0, "label": "SYSTEM（不切换）"}]
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
    if WINDOWS:
        venv = Path(cwd) / ".venv" / "Scripts"
        command = venv / (("python" if rest[0] in ("python", "python3") else rest[0]) + ".exe")
    else:
        venv = Path(cwd) / ".venv" / "bin"
        command = venv / ("python" if rest[0] in ("python", "python3") else rest[0])
    return [str(command), *rest[1:]] if command.exists() else argv


def hand_over(account: Account, *directories: Path) -> None:
    """Give ``account`` the directories a plugin writes (its data, its logs), creating them."""
    if account.session is not None:
        import subprocess
        for directory in directories:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                subprocess.run(["icacls", str(directory), "/grant", f"{account.principal}:(OI)(CI)M", "/T", "/Q"],
                               capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError:
                pass
        return
    if not hasattr(os, "chown"):  # Windows without a session account: nothing to hand over
        for directory in directories:
            with contextlib.suppress(OSError):
                directory.mkdir(parents=True, exist_ok=True)
        return
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
    if WINDOWS:
        return  # Windows: everyone may traverse (SeChangeNotifyPrivilege); hand_over grants the subfolder
    try:
        if directory.is_dir() and (directory.stat().st_mode & 0o777) == 0o700:
            os.chmod(directory, 0o711)
    except OSError:
        pass
