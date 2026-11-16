"""Command templates, executable lookup and permission flags (design §8.7, decision D2).

Configuration: the ``wake`` object of ``agent-nexus.json`` (or ``cloud.wake``)::

    "wake": {
      "executables": {"claude": "C:/tools/claude.exe", "codex": ["node", "C:/codex/bin/codex.js"]},
      "claude": [...], "claude_last": [...], "codex": [...], "codex_last": [...],
      "permission_flags": {"codex": {"full-access": ["--sandbox", "danger-full-access"]}},
      "companion_endpoint": "http://127.0.0.1:8400", "claude_home": "...", "codex_home": "..."
    }

A template is a list of arguments. ``{claude}`` / ``{codex}`` expand to the
executable (one or more arguments), ``{permission_flags...}`` to the flags of
the address's permission mode; ``{resume_id}``, ``{prompt}``, ``{workdir}``,
``{handle}`` and ``{address}`` are substituted inside arguments. The ``_last``
templates are used when the address has no resume id.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

from ...ids import RESUME_ID_RULE, valid_resume_id

PROCESS_KINDS = ("claude", "codex")
PERMISSIONS = ("bypass", "full-access", "inherit")

DEFAULT_TEMPLATES: dict[str, list[str]] = {
    "claude": ["{claude}", "-p", "--resume", "{resume_id}", "--output-format", "json", "{permission_flags...}",
               "{prompt}"],
    "claude_last": ["{claude}", "-p", "--continue", "--output-format", "json", "{permission_flags...}", "{prompt}"],
    "codex": ["{codex}", "exec", "resume", "{resume_id}", "--json", "--skip-git-repo-check",
              "{permission_flags...}", "{prompt}"],
    "codex_last": ["{codex}", "exec", "resume", "--last", "--json", "--skip-git-repo-check",
                   "{permission_flags...}", "{prompt}"],
}

#: Decision D2: the gateway sends the mode name; this table makes it arguments.
DEFAULT_PERMISSION_FLAGS: dict[str, dict[str, list[str]]] = {
    "claude": {"bypass": ["--dangerously-skip-permissions"],
               "full-access": ["--permission-mode", "bypassPermissions"],
               "inherit": []},
    "codex": {"bypass": ["--dangerously-bypass-approvals-and-sandbox"],
              "full-access": ["-c", 'sandbox_mode="danger-full-access"', "-c", 'approval_policy="never"'],
              "inherit": []},
}


class WakeError(RuntimeError):
    """A wake that cannot start (missing executable, bad template, unknown mode)."""


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def extra_path_dirs() -> list[str]:
    """``%APPDATA%\\npm`` and ``~/.local/bin``: autostarted managers may lack them on PATH (§8.7)."""
    dirs = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        dirs.append(str(Path(appdata) / "npm"))
    dirs.append(str(_home() / ".local" / "bin"))
    return dirs


def wake_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    path = env.get("PATH", "")
    prefix = [item for item in extra_path_dirs() if item not in path.split(os.pathsep)]
    env["PATH"] = os.pathsep.join([*prefix, path]) if path else os.pathsep.join(prefix)
    env.update(extra or {})
    return env


def resolve_executable(kind: str, config: dict | None = None) -> list[str] | None:
    """The configured path, then PATH, then ``%APPDATA%\\npm\\<kind>.cmd``, then ``~/.local/bin/<kind>``."""
    configured = ((config or {}).get("executables") or {}).get(kind)
    if isinstance(configured, list) and configured and all(isinstance(item, str) and item for item in configured):
        return list(configured)
    if isinstance(configured, str) and configured.strip():
        return [configured.strip()]
    found = shutil.which(kind, path=wake_env()["PATH"])
    if found:
        return [found]
    candidates = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "npm" / f"{kind}.cmd")
    local = _home() / ".local" / "bin"
    candidates += [local / f"{kind}.exe", local / kind]
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate)]
    return None


def permission_flags(kind: str, mode: str | None, config: dict | None = None) -> list[str]:
    mode = mode or "bypass"
    if mode not in PERMISSIONS:
        raise WakeError(f"unknown wake permission {mode!r}")
    override = (((config or {}).get("permission_flags") or {}).get(kind) or {}).get(mode)
    if override is not None:
        if not isinstance(override, list) or not all(isinstance(item, str) for item in override):
            raise WakeError(f"wake.permission_flags.{kind}.{mode} must be a list of strings")
        return list(override)
    return list(DEFAULT_PERMISSION_FLAGS.get(kind, {}).get(mode, []))


def _batch(executable: list[str]) -> bool:
    return Path(executable[0]).suffix.lower() in (".cmd", ".bat")


#: Characters cmd.exe expands even inside double quotes, or that end a command line.
_BATCH_NEVER = frozenset('%!\r\n\x00')
#: Characters cmd.exe acts on outside double quotes.
_BATCH_SPECIAL = frozenset('&|<>^()')
#: Arguments made only of these pass unquoted.
_BATCH_PLAIN = re.compile(r"[A-Za-z0-9_.:/\@+=,-]+")


def safe_prompt(prompt: str, executable: list[str]) -> str:
    """A ``.cmd`` shim (npm's ``claude.cmd``) goes through cmd.exe: one line, no ``%``, ``!`` or ``"``."""
    if not _batch(executable):
        return prompt
    return " ".join(prompt.split()).replace("%", "").replace("!", ".").replace('"', "'")


def _crt_quote(arg: str) -> str:
    """``arg`` double-quoted for the C runtime's parser (the program behind the shim)."""
    out, slashes = ['"'], 0
    for char in arg:
        if char == "\\":
            slashes += 1
            continue
        out.append("\\" * (slashes * 2 + 1) + '"' if char == '"' else "\\" * slashes + char)
        slashes = 0
    out.append("\\" * (slashes * 2) + '"')
    return "".join(out)


def _batch_refuse(arg: str) -> WakeError:
    return WakeError(f"argument {arg[:40]!r} cannot be passed safely through a .cmd/.bat executable")


def _batch_arg(arg: str) -> str:
    """One argument of a command line cmd.exe will parse, or :class:`WakeError` when none is safe.

    The result is plain, or double-quoted with an even number of ``"`` so cmd.exe
    is back outside quotes at its end: cmd carries its quote state across the
    whole line, so an argument leaving it "inside" would flip the next
    argument's quoting and expose that argument's metacharacters.
    """
    if any(char in _BATCH_NEVER for char in arg):
        raise _batch_refuse(arg)
    if arg and _BATCH_PLAIN.fullmatch(arg):
        return arg
    quoted = _crt_quote(arg)
    if _batch_unsafe(quoted):
        raise _batch_refuse(arg)
    return quoted


def _batch_unsafe(text: str) -> bool:
    """Whether ``text`` read by cmd.exe from "outside quotes" exposes a metacharacter or ends inside quotes."""
    inside = False
    for char in text:  # cmd.exe toggles on every '"' (it ignores the C runtime's \")
        if char == '"':
            inside = not inside
        elif not inside and char in _BATCH_SPECIAL:
            return True
    return inside


def _batch_line(argv: list[str]) -> str:
    """The cmd.exe command line for ``argv``, checked as a whole with the quote state carried across arguments."""
    line = " ".join(_batch_arg(arg) for arg in argv)
    if _batch_unsafe(line) or any(char in _BATCH_NEVER for char in line):
        raise WakeError("command line cannot be passed safely through a .cmd/.bat executable")
    return line


def popen_args(argv: list[str]) -> list[str] | str:
    """What to hand to ``subprocess``: ``argv`` itself, or for a ``.cmd``/``.bat`` the quoted command line.

    ``subprocess.list2cmdline`` quotes for the C runtime only -- it leaves
    ``&`` unquoted in an argument without spaces and escapes ``"`` as ``\\"``,
    which cmd.exe does not honour -- so a batch executable gets a command line
    built here, every argument either plain or fully double-quoted with cmd's
    quote state balanced, and the whole line re-checked (§8.7).
    """
    if os.name != "nt" or not argv or not _batch(argv):
        return list(argv)
    return _batch_line(argv)


def build_argv(kind: str, params: dict, config: dict | None = None) -> list[str]:
    """The wake command for ``params`` (the ``session_wake`` arguments)."""
    if kind not in PROCESS_KINDS:
        raise WakeError(f"no command adapter for {kind!r}")
    config = config or {}
    executable = resolve_executable(kind, config)
    if executable is None:
        raise WakeError(f"{kind} executable not found (set wake.executables.{kind} in agent-nexus.json)")
    resume_id = params.get("resume_id") or ""
    if resume_id and not valid_resume_id(resume_id):
        raise WakeError(RESUME_ID_RULE)
    key = kind if resume_id else f"{kind}_last"
    template = config.get(key) or DEFAULT_TEMPLATES[key]
    if not isinstance(template, list) or not all(isinstance(item, str) for item in template):
        raise WakeError(f"wake.{key} must be a list of strings")
    flags = permission_flags(kind, params.get("permission"), config)
    values: dict[str, Any] = {"resume_id": resume_id, "prompt": safe_prompt(str(params.get("prompt") or ""), executable),
                              "workdir": params.get("workdir") or "", "handle": params.get("handle") or "",
                              "address": params.get("address") or ""}
    argv: list[str] = []
    for part in template:
        if part == "{" + kind + "}":
            argv += executable
        elif part == "{permission_flags...}":
            argv += flags
        else:
            for name, value in values.items():
                part = part.replace("{" + name + "}", str(value))
            argv.append(part)
    if _batch(executable):
        _batch_line(argv)  # refuse a command line cmd.exe would interpret, before anything starts
    return argv
