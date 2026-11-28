"""``anx`` in the owner's own terminals on macOS (本机控制台 → 设置 → 终端里的 anx).

Windows gets ``<install dir>\\bin\\anx.exe`` on PATH from its installer (desktop/src-tauri/src/cli_path.rs), the deb
ships ``/usr/bin/anx``. macOS has no installer: the client writes ``<root>/bin/anx`` at every start (a small sh
script that reads ``runtime/current`` on each run, so an update never leaves it stale), and the console's
"在终端中使用 anx" links it into a folder of this user's own, like VS Code's "install 'code' command":

1. ``~/.local/bin`` or ``~/bin`` when the login shell's PATH already lists it (a GUI app only has launchd's PATH,
   so the login shell is asked);
2. else ``~/.local/bin``, added to the login shell's PATH with a marked block in ``~/.zprofile`` (zsh) or
   ``~/.bash_profile`` / ``~/.profile`` (bash, sh).

Never a system folder such as ``/usr/local/bin``: the script and the runtime it starts live in this user's data
root, which only this user can write, so a system-wide link would let this user run code as any other account
that types ``anx`` (``sudo anx`` included). Only a symlink that points at our script, and only our own marked
PATH block, are ever replaced or removed; another program's ``anx`` is never touched.

macOS has no uninstaller: dragging the app to the Trash leaves the link (the script then says so and how to remove
it); the console's 移除 takes it away, PATH block included.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .agents import anx_shim

#: Other places an ``anx`` may already be found first on PATH (reported as ``foreign``, never written).
SYSTEM_DIR = Path("/usr/local/bin")
USER_DIRS = (".local/bin", "bin")
NAME = "anx"
BLOCK_BEGIN = "# >>> agent-nexus: anx >>>"
BLOCK_END = "# <<< agent-nexus: anx <<<"
BLOCK = f'{BLOCK_BEGIN}\nexport PATH="$HOME/.local/bin:$PATH"\n{BLOCK_END}\n'


def supported(platform: str | None = None) -> bool:
    return (platform or sys.platform) == "darwin"


def script_path(root: Path) -> Path:
    return Path(root) / "bin" / NAME


def write_script(root: Path) -> Path | None:
    """``<root>/bin/anx`` (idempotent); None when it could not be written."""
    path = script_path(root)
    text = anx_shim.owner_script(Path(root))
    try:
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + ".tmp")
            temp.write_text(text, encoding="utf-8")
            os.chmod(temp, 0o755)
            os.replace(temp, path)
        return path
    except OSError:
        return None


def login_path(env: dict | None = None, runner: Callable[..., Any] | None = None) -> list[str]:
    """The login shell's PATH (a GUI app's own PATH is launchd's, without the user's additions)."""
    env = dict(os.environ if env is None else env)
    shell = env.get("SHELL") or "/bin/zsh"
    runner = runner or subprocess.run
    try:
        done = runner([shell, "-ilc", "printf %s \"$PATH\""], capture_output=True, text=True, timeout=10, env=env)
        value = (done.stdout or "").strip().splitlines()[-1] if (done.stdout or "").strip() else ""
    except (OSError, subprocess.SubprocessError, IndexError):
        value = ""
    return [part for part in (value or env.get("PATH", "")).split(":") if part]


def profile_for(home: Path, shell: str | None = None) -> Path | None:
    """The login shell's startup file a PATH line goes into (None: a shell we do not edit, e.g. fish)."""
    name = Path(shell or os.environ.get("SHELL") or "/bin/zsh").name
    if name == "zsh":
        return home / ".zprofile"
    if name in ("bash", "sh"):
        # bash reads only the first of these that exists: never create .bash_profile over an existing .profile
        for candidate in (".bash_profile", ".bash_login", ".profile"):
            if (home / candidate).exists():
                return home / candidate
        return home / (".bash_profile" if name == "bash" else ".profile")
    return None


def _ours(link: Path, script: Path) -> bool:
    try:
        return link.is_symlink() and Path(os.readlink(link)) == script
    except OSError:
        return False


def _on(folder: Path, path_dirs: list[str]) -> bool:
    return str(folder) in path_dirs or str(folder) + "/" in path_dirs


def candidates(home: Path, path_dirs: list[str]) -> list[Path]:
    return [home / name for name in USER_DIRS if _on(home / name, path_dirs)]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _has_block(text: str) -> bool:
    return BLOCK_BEGIN in text and BLOCK_END in text


def status(root: Path, *, home: Path | None = None, path_dirs: list[str] | None = None,
           system_dir: Path | None = None, shell: str | None = None) -> dict:
    home = Path(home or Path.home())
    script = script_path(root)
    dirs = [home / name for name in USER_DIRS]
    linked = next((str(folder / NAME) for folder in dirs if _ours(folder / NAME, script)), None)
    foreign = [str(folder / NAME) for folder in [system_dir or SYSTEM_DIR, *dirs]
               if (folder / NAME).exists() or (folder / NAME).is_symlink()]
    foreign = [item for item in foreign if item != linked]
    profile = profile_for(home, shell)
    return {"supported": True, "script": str(script), "script_ok": script.is_file(), "linked": linked,
            "foreign": foreign, "profile": str(profile) if profile and _has_block(_read(profile)) else None,
            "on_path": bool(linked) and (path_dirs is None or str(Path(linked).parent) in path_dirs)}


def _symlink(link: Path, script: Path) -> None:
    temp = link.with_name(f".{NAME}.anx-tmp")
    if temp.is_symlink() or temp.exists():
        temp.unlink()
    os.symlink(script, temp)
    os.replace(temp, link)


def _write_text(path: Path, text: str) -> None:
    temp = path.with_name(path.name + ".anx-tmp")
    temp.write_text(text, encoding="utf-8")
    try:
        os.chmod(temp, path.stat().st_mode & 0o7777)
    except OSError:
        pass
    os.replace(temp, path)


def add_to_profile(home: Path, shell: str | None = None) -> Path:
    """Our marked PATH block (``~/.local/bin`` first) in the login shell's startup file; raises OSError."""
    profile = profile_for(home, shell)
    if profile is None:
        raise OSError("这个登录 shell 不支持自动修改 PATH")
    text = _read(profile)
    if not _has_block(text):
        _write_text(profile, text + ("" if not text or text.endswith("\n") else "\n") + BLOCK)
    return profile


def remove_from_profile(home: Path, shell: str | None = None) -> Path | None:
    """Remove our block (only it) from the startup file; the file when one was removed."""
    profile = profile_for(home, shell)
    text = _read(profile) if profile else ""
    if not profile or not _has_block(text):
        return None
    start = text.index(BLOCK_BEGIN)
    end = text.index(BLOCK_END, start) + len(BLOCK_END)
    if end < len(text) and text[end] == "\n":
        end += 1
    _write_text(profile, text[:start] + text[end:])
    return profile


def link(root: Path, *, home: Path | None = None, path_dirs: list[str] | None = None,
         system_dir: Path | None = None, shell: str | None = None) -> dict:
    """Link ``anx`` into a folder of this user's on the login shell's PATH (see the module doc for the order)."""
    home = Path(home or Path.home())
    script = write_script(root)
    if script is None:
        return {"ok": False, "error": "无法写入 anx 脚本"}
    path_dirs = login_path() if path_dirs is None else path_dirs
    user_dirs = candidates(home, path_dirs)
    for folder in [home / name for name in USER_DIRS]:
        target = folder / NAME
        if _ours(target, script):
            return {"ok": True, "linked": str(target), "changed": False}
    for folder in user_dirs:
        target = folder / NAME
        if target.exists() or target.is_symlink():
            continue  # another program's anx: never replaced
        try:
            _symlink(target, script)
            return {"ok": True, "linked": str(target), "changed": True}
        except OSError:
            continue
    # No folder of ours on PATH: ~/.local/bin, put on the login shell's PATH.
    folder = home / USER_DIRS[0]
    target = folder / NAME
    if target.exists() or target.is_symlink():
        return {"ok": False, "error": f"{target} 已被其他程序占用，未修改"}
    try:
        folder.mkdir(parents=True, exist_ok=True)
        profile = add_to_profile(home, shell)
        _symlink(target, script)
    except OSError as exc:
        return {"ok": False, "error": f"无法添加：{exc}（也可以把 {script} 链接到 PATH 上你自己的文件夹）"}
    return {"ok": True, "linked": str(target), "changed": True, "profile": str(profile)}


def unlink(root: Path, *, home: Path | None = None, system_dir: Path | None = None,
           shell: str | None = None) -> dict:
    home = Path(home or Path.home())
    script = script_path(root)
    removed = []
    for folder in [home / name for name in USER_DIRS]:
        target = folder / NAME
        if not _ours(target, script):
            continue
        try:
            target.unlink()
        except OSError as exc:
            return {"ok": False, "error": f"无法移除 {target}：{exc}"}
        removed.append(str(target))
    try:
        profile = remove_from_profile(home, shell)
    except OSError as exc:
        return {"ok": False, "error": f"无法修改 PATH 设置：{exc}", "removed": removed}
    return {"ok": True, "removed": removed, "profile": str(profile) if profile else None}


def on_start(root: Path) -> None:
    """At every client start on macOS: keep ``<root>/bin/anx`` current (a link to it then always works)."""
    if supported():
        write_script(Path(root))
