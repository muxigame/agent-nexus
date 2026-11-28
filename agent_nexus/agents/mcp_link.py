"""The dedicated ``agent-nexus`` entry in each installed CLI's own MCP config (docs/design.md "本机 MCP").

Like Unity MCP, the client writes one entry named ``agent-nexus`` pointing at the 本机 MCP
(``http://127.0.0.1:<port>/mcp``; no credential, so nothing secret lands on disk) into the user-level MCP config of
every installed CLI, ahead of time:

* claude -- ``mcpServers`` of ``~/.claude.json`` (``$CLAUDE_CONFIG_DIR/.claude.json``): ``claude mcp add-json
  --scope user`` when the CLI can be run directly, else a careful in-place JSON edit (Claude rewrites that file all
  the time: the edit re-checks the file's mtime before replacing it and tries again);
* codex -- a ``[mcp_servers.agent-nexus]`` table appended to ``$CODEX_HOME/config.toml`` (``~/.codex``);
* gemini -- ``mcpServers["agent-nexus"]`` of ``~/.gemini/settings.json``.

Every write keeps every other entry and the file's formatting (integrate.py's span editing), takes a timestamped
``.anx-backup`` copy first, does nothing when the entry is already right, and re-parses the result. It runs as the
account that runs the CLIs: a system service (Windows SYSTEM, Linux root) starts ``anx mcp link`` as that user, so
the file keeps its owner and ACL and nothing is written into the service's own profile.

What is ours is recorded in ``<root>/.anx/mcp-link.json`` (:class:`Ownership`); only an entry recorded as ours whose
URL is still what we wrote is ever changed or removed. An entry of ours the user deleted becomes ``user_removed``
and is never added again by itself. :meth:`MCPLink.reconcile` adds or removes entries following
:mod:`.mcp_probe`'s answer: never next to another Agent Nexus connector (a gateway entry or an account connector).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import tomllib
from pathlib import Path
from typing import Any, Callable

from .. import integrate
from . import mcp_probe, registry

log = logging.getLogger(__name__)

NAME = mcp_probe.NAME
KINDS = mcp_probe.KINDS
TOOL_TIMEOUT_S = 900
#: 延长 MCP 等待 (the 本机 MCP setting ``mcp_local.extended_wait``, default on): our Codex / Gemini CLI entries get a
#: 24 h tool timeout and ``?wait_max=`` on the URL, a little below it, so the gateway lets one call wait that long
#: (split into device calls). Claude Code is never touched: it waits with background shell tasks.
LONG_TIMEOUT_S = 86400
WAIT_MAX_S = 86100
WAIT_PARAM = "wait_max"
#: kind -> (the entry's timeout key, its unit in seconds^-1)
WAIT_KEY = {"codex": ("tool_timeout_sec", 1), "gemini": ("timeout", 1000)}


class _Standard:
    """``timeout`` not given: the standard value (900 s)."""

    def __repr__(self) -> str:
        return "STANDARD"


STANDARD: Any = _Standard()
#: a key that is not in the file (``current`` value of a missing timeout)
ABSENT = None


def entry_url_for(base: str, wait_on: bool) -> str:
    return f"{base}?{WAIT_PARAM}={WAIT_MAX_S}" if wait_on else base


def strip_wait(url: Any) -> Any:
    """``url`` without our ``?wait_max=`` (for comparisons)."""
    if isinstance(url, str) and "?" in url:
        base, _, query = url.partition("?")
        if re.fullmatch(rf"{WAIT_PARAM}=\d+", query):
            return base
    return url


def wait_written(kind: str) -> int:
    return LONG_TIMEOUT_S * WAIT_KEY[kind][1]
RECONCILE_EVERY_S = 6 * 3600.0
WRITE_TIMEOUT_S = 60.0
#: an account probe that gave no answer is not tried again sooner than this
PROBE_RETRY_S = 600.0
#: Codex reads ``url`` (streamable HTTP) servers without extra flags from this version on; older releases needed
#: ``command`` (or an experimental switch) and may refuse the whole config.toml with a ``url``-only entry. Kept
#: conservative: below it (or a version that cannot be parsed as newer) nothing is written or attached for Codex.
CODEX_URL_MIN = "0.50.0"
#: local states in which no new entry is written and nothing is attached by flags: the 本机 MCP would only offer
#: nexus_status (the gateway-attach reconcile adds the entries once the device can use them)
NOT_SERVING = ("not_joined", "revoked", "gateway_old", "device_access_off")
ENTRY_COMMENT = "# agent-nexus: the 本机 MCP of the Agent Nexus client (no credential; anx mcp unlink removes it)"


class LinkError(Exception):
    pass


# -- where and what ----------------------------------------------------------------------------------------------

def _home(env: dict | None) -> Path:
    env = env if env is not None else dict(os.environ)
    value = env.get("USERPROFILE") if os.name == "nt" else None
    return Path(value or env.get("HOME") or os.path.expanduser("~"))


def config_path(kind: str, home: Path | None = None, env: dict | None = None) -> Path | None:
    env = env if env is not None else dict(os.environ)
    home = Path(home) if home is not None else _home(env)
    if kind == "claude":
        return Path(env["CLAUDE_CONFIG_DIR"]) / ".claude.json" if env.get("CLAUDE_CONFIG_DIR") else home / ".claude.json"
    if kind == "codex":
        return (Path(env["CODEX_HOME"]) if env.get("CODEX_HOME") else home / ".codex") / "config.toml"
    if kind == "gemini":
        return home / ".gemini" / "settings.json"
    return None


def wanted(kind: str, url: str, timeout: Any = STANDARD) -> dict:
    """The entry we write. ``timeout``: the value of the timeout key in the CLI's own unit (STANDARD: 900 s;
    None: no such key, an "absent" original put back)."""
    if kind == "claude":
        return {"type": "http", "url": url}
    # No approval prompts for Agent Nexus' own tools (the owner's choice): codex exec refuses every call that
    # needs one, and the device's lock and the gateway still decide what a caller may do.
    if kind in WAIT_KEY:
        key, unit = WAIT_KEY[kind]
        value = TOOL_TIMEOUT_S * unit if timeout is STANDARD else timeout
        if kind == "codex":
            entry: dict = {"url": url}
            if value is not None:
                entry[key] = value
            entry["default_tools_approval_mode"] = "approve"
            return entry
        entry = {"httpUrl": url}
        if value is not None:
            entry[key] = value
        entry["trust"] = True
        return entry
    raise LinkError(f"unsupported CLI {kind!r}")


#: kind -> every key :func:`wanted` may write; any other key in our entry is the user's and is kept
OUR_KEYS = {"claude": ("type", "url"), "codex": ("url", "tool_timeout_sec", "default_tools_approval_mode"),
            "gemini": ("httpUrl", "timeout", "trust")}


def target_entry(kind: str, present: Any, url: str, timeout: Any = STANDARD) -> dict:
    """The entry to write over ``present``: our keys as :func:`wanted` has them (a key it leaves out, such as an
    absent original timeout, is removed), every other key the user put there (``enabled = false``,
    ``includeTools``, an ``env`` table, ...) kept as it is, in place."""
    want = wanted(kind, url, timeout)
    if not isinstance(present, dict):
        return want
    entry = {key: value for key, value in present.items() if key in want or key not in OUR_KEYS[kind]}
    for key, value in want.items():
        entry[key] = value
    return entry


def ours_match(kind: str, entry: Any, want: dict) -> bool:
    """``entry`` holds our keys exactly as ``want`` (other keys are not looked at)."""
    if not isinstance(entry, dict):
        return False
    missing = object()
    return all(integrate._same(entry.get(key, missing), want.get(key, missing)) if key in want or key in entry
               else True for key in OUR_KEYS[kind])


def current(kind: str, path: Path) -> dict | None:
    """The ``agent-nexus`` entry in ``path`` (None when absent)."""
    text = integrate._read(path)
    if not text or not text.strip():
        return None
    try:
        data = tomllib.loads(text) if kind == "codex" else json.loads(text)
    except (ValueError, tomllib.TOMLDecodeError):
        raise LinkError(f"{path} 不是有效的配置文件，未做任何修改") from None
    block = data.get("mcp_servers" if kind == "codex" else "mcpServers") if isinstance(data, dict) else None
    entry = block.get(NAME) if isinstance(block, dict) else None
    return entry if isinstance(entry, dict) else None


# -- planning (pure: text in, text out) -------------------------------------------------------------------------

def plan_json(path: Path, kind: str, url: str, *, remove: bool, drop_empty: bool = False,
              timeout: Any = STANDARD) -> integrate.Plan:
    plan = integrate.Plan(path, bom=integrate._has_bom(path))
    original = integrate._read(path)
    try:
        data = json.loads(original) if original and original.strip() else {}
    except ValueError as exc:
        raise LinkError(f"{path} 不是有效的 JSON（{exc}），未做任何修改") from None
    if not isinstance(data, dict):
        raise LinkError(f"{path} 不是 JSON 对象，未做任何修改")
    servers = data.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise LinkError(f"{path}：mcpServers 不是对象，未做任何修改")
    new = dict(data)
    block = dict(servers or {})
    if remove:
        if NAME not in block:
            return plan
        block.pop(NAME)
        plan.changes.append(f"remove mcpServers.{NAME}")
    else:
        entry = target_entry(kind, block.get(NAME), url, timeout)
        if integrate._same(block.get(NAME), entry):
            return plan
        plan.changes.append(f"{'update' if NAME in block else 'add'} mcpServers.{NAME} = {url}")
        block[NAME] = entry
    if block or not drop_empty:
        new["mcpServers"] = block
    else:
        new.pop("mcpServers", None)
    plan.text = integrate.edit_json(original or "", data, new)
    return plan


_TABLE = re.compile(r'^\s*\[\s*mcp_servers\s*\.\s*(?:agent-nexus|"agent-nexus"|\'agent-nexus\')\s*(?:\]|\.)')
_HEADER = re.compile(r"^\s*\[")


def _codex_span(lines: list[str]) -> tuple[int, int] | None:
    """``[start, end)`` line range of the ``[mcp_servers.agent-nexus]`` table (with its sub-tables and our comment)."""
    start = next((n for n, line in enumerate(lines) if _TABLE.match(line)), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and (not _HEADER.match(lines[end]) or _TABLE.match(lines[end])):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1  # trailing blank lines belong to what follows
    if start > 0 and lines[start - 1].strip() == ENTRY_COMMENT:
        start -= 1
    if start > 0 and not lines[start - 1].strip() and end == len(lines):
        start -= 1  # the blank line we put before an appended table
    return start, end


_MAIN_TABLE = re.compile(r'^\s*\[\s*mcp_servers\s*\.\s*(?:agent-nexus|"agent-nexus"|\'agent-nexus\')\s*\]')


def _codex_in_place(lines: list[str], span: tuple[int, int], present: dict, entry: dict,
                    newline: str) -> list[str] | None:
    """Set only our keys inside the existing ``[mcp_servers.agent-nexus]`` table (its other keys, comments and
    sub-tables untouched); None when the table is not laid out so that this is possible."""
    start, end = span
    header = next((n for n in range(start, end) if _MAIN_TABLE.match(lines[n])), None)
    if header is None:
        return None
    body_end = header + 1
    while body_end < end and not _HEADER.match(lines[body_end]):
        body_end += 1
    out = list(lines)
    missing = object()
    inserts: list[str] = []
    removed: list[int] = []
    for key in OUR_KEYS["codex"]:
        want = entry.get(key, missing)
        if key in present and present.get(key) == want:
            continue
        pattern = re.compile(rf'^\s*(?:{re.escape(key)}|"{re.escape(key)}")\s*=')
        found = [n for n in range(header + 1, body_end) if pattern.match(lines[n])]
        if len(found) > 1:
            return None
        if found:
            if want is missing:
                removed.append(found[0])
            else:
                out[found[0]] = f"{key} = {_toml_value(want)}{newline}"
        elif want is not missing:
            inserts.append(f"{key} = {_toml_value(want)}{newline}")
    at = body_end
    while at > header + 1 and not out[at - 1].strip():
        at -= 1
    if at > 0 and not out[at - 1].endswith(("\n", "\r")):
        out[at - 1] += newline
    out[at:at] = inserts
    for n in sorted(removed, reverse=True):
        del out[n]
    return out


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return json.dumps(value)
    raise LinkError(f"cannot write {type(value).__name__} into config.toml")


def plan_codex(path: Path, url: str, *, remove: bool, timeout: Any = STANDARD) -> integrate.Plan:
    plan = integrate.Plan(path, bom=integrate._has_bom(path))
    original = integrate._read(path)
    text = original or ""
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LinkError(f"{path} 不是有效的 TOML（{exc}），未做任何修改") from None
    servers = parsed.get("mcp_servers") if isinstance(parsed.get("mcp_servers"), dict) else {}
    present = servers.get(NAME)
    entry = target_entry("codex", present, url, timeout)
    if not remove and isinstance(present, dict) and present == entry:
        return plan
    if remove and present is None:
        return plan
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    edited = None
    if present is not None:
        span = _codex_span(lines)
        if span is None:
            raise LinkError(f"{path}：agent-nexus 不是一个独立的 [mcp_servers.agent-nexus] 表，请手动修改")
        if not remove and isinstance(present, dict):
            edited = _codex_in_place(lines, span, present, entry, newline)
            if edited is not None and (tomllib.loads("".join(edited)).get("mcp_servers") or {}).get(NAME) != entry:
                edited = None
            if edited is None and set(present) - set(OUR_KEYS["codex"]):
                raise LinkError(f"{path}：没法只改 [mcp_servers.agent-nexus] 里的 url 和超时，未做任何修改（请手动修改）")
        if edited is not None:
            lines = edited
        else:
            lines = lines[:span[0]] + lines[span[1]:]
    if not remove and edited is None:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += newline
        block = ([newline] if lines else []) + [
            f"{ENTRY_COMMENT}{newline}", f"[mcp_servers.{NAME}]{newline}",
            *(f"{key} = {_toml_value(value)}{newline}" for key, value in entry.items())]
        lines += block
    new_text = "".join(lines)
    reparsed = tomllib.loads(new_text)  # never write a file Codex could not read

    def others(data: dict) -> dict:
        copy = dict(data)
        block = dict(copy.get("mcp_servers") or {}) if isinstance(copy.get("mcp_servers"), dict) else None
        if block is not None:
            block.pop(NAME, None)
            if block:
                copy["mcp_servers"] = block
            else:
                copy.pop("mcp_servers", None)
        return copy

    if others(parsed) != others(reparsed) or \
            (reparsed.get("mcp_servers") or {}).get(NAME) != (None if remove else entry):
        raise LinkError(f"{path}：没法在不改动其他设置的前提下写入 agent-nexus，未做任何修改（请手动添加）")
    plan.changes.append(f"{'remove' if remove else ('update' if present is not None else 'add')} "
                        f"[mcp_servers.{NAME}]" + ("" if remove else f" url = {url}"))
    plan.text = new_text
    return plan


def plan(kind: str, path: Path, url: str, *, remove: bool, drop_empty: bool = False,
         timeout: Any = STANDARD) -> integrate.Plan:
    if kind == "codex":
        return plan_codex(path, url, remove=remove, timeout=timeout)
    if kind in ("claude", "gemini"):
        return plan_json(path, kind, url, remove=remove, drop_empty=drop_empty, timeout=timeout)
    raise LinkError(f"unsupported CLI {kind!r}")


#: ``.anx-backup`` copies kept per config file (every edit makes one; older ones are pruned)
KEEP_BACKUPS = 2


def _apply(plan_: integrate.Plan, mtime: float | None, now: float | None = None) -> None:
    """integrate.apply, refusing (InterruptedError) when the file changed since it was read -- checked again right
    before the rename, so a write Claude Code makes meanwhile is never lost -- and keeping its mode and owner."""
    path = plan_.path
    try:
        stat = path.stat()
    except FileNotFoundError:
        stat = None
    if (stat.st_mtime if stat else None) != mtime:
        raise InterruptedError("changed while editing")
    integrate.apply(plan_, now=now, expect_mtime=mtime, keep_backups=KEEP_BACKUPS)


def _claude_cli(url: str, remove: bool, env: dict) -> bool:
    """``claude mcp add-json|remove --scope user``; False when the CLI cannot be run directly."""
    binary = shutil.which("claude", path=env.get("PATH"))
    if not binary or binary.lower().endswith((".cmd", ".bat", ".ps1")):
        return False  # an npm shim on Windows: cmd.exe would mangle the JSON argument
    argv = [binary, "mcp", "remove", "--scope", "user", NAME] if remove else \
        [binary, "mcp", "add-json", "--scope", "user", NAME, json.dumps(wanted("claude", url))]
    code, _out, _err = mcp_probe.run_user(argv, env, run_as=None, timeout=WRITE_TIMEOUT_S)
    return code == 0


def write(kind: str, url: str, *, remove: bool = False, home: Path | None = None, env: dict | None = None,
          drop_empty: bool = False, use_cli: bool = True, now: float | None = None, path: Path | None = None,
          timeout: Any = STANDARD) -> dict:
    """Add (or ``remove``) the entry for the user this process runs as (``path``: that file instead of the one
    ``kind`` uses for this user). ``{ok, changed, path, backup, method, entry, error}``; never raises."""
    env = env if env is not None else dict(os.environ)
    if path is not None:
        path, use_cli = Path(path), False  # "claude mcp" edits its own default file, not this one
    else:
        path = config_path(kind, home, env)
    result: dict[str, Any] = {"ok": False, "changed": False, "path": str(path) if path else None, "backup": None,
                              "method": "edit", "error": None}
    if path is None:
        result["error"] = f"不支持写入 {kind} 的配置"
        return result
    try:
        before = current(kind, path)
        if kind == "claude" and use_cli and (remove and before is not None or
                                             not remove and not integrate._same(before, wanted(kind, url))):
            backup = None
            if path.exists():
                backup = integrate._backup_path(path, time.time() if now is None else now)
                integrate.safe_backup(path, backup)
            if _claude_cli(url, remove, env):
                after = current(kind, path)
                if (after is None) if remove else integrate._same(after, wanted(kind, url)):
                    integrate.prune_backups(path, KEEP_BACKUPS)
                    result.update(ok=True, changed=True, backup=str(backup) if backup else None, method="cli",
                                  entry=after)
                    return result
            if backup is not None:
                with contextlib.suppress(OSError):
                    backup.unlink()
        for _attempt in range(3):
            try:
                mtime = path.stat().st_mtime
            except FileNotFoundError:
                mtime = None
            planned = plan(kind, path, url, remove=remove, drop_empty=drop_empty, timeout=timeout)
            if planned.text is None:
                result.update(ok=True, entry=current(kind, path))
                return result
            try:
                _apply(planned, mtime, now)
            except InterruptedError:
                time.sleep(0.2)
                continue
            after = current(kind, path)
            if (after is not None) if remove else not ours_match(kind, after, wanted(kind, url, timeout)):
                raise LinkError(f"{path}：写入后校验失败")
            result.update(ok=True, changed=True, backup=str(planned.backup) if planned.backup else None, entry=after)
            return result
        result["error"] = f"{path} 一直在被改写，稍后再试"
    except (LinkError, integrate.IntegrateError, OSError) as exc:
        result["error"] = str(exc)
    return result


# -- running as the CLI's user ---------------------------------------------------------------------------------

def _python() -> str:
    python = sys.executable
    if os.name == "nt" and python.lower().endswith("pythonw.exe"):
        console = python[:-len("pythonw.exe")] + "python.exe"
        python = console if os.path.isfile(console) else python
    return python


def run_child(account: Any, args: list[str], *, stdin: bytes | None = None,
              runner: Callable[..., tuple[int | None, str, str]] | None = None) -> dict:
    """``python -I -m agent_nexus.cli <args>`` as ``account``, answering one JSON line (the last of stdout).

    Shared by the link writes and the CLI config editor (cli_config): text only ever goes over stdin."""
    argv = [_python(), "-I", "-m", "agent_nexus.cli", *args]
    env = account.env()
    runner = runner or mcp_probe.run_user
    kwargs = {"stdin": stdin} if stdin is not None else {}
    code, out, err = runner(argv, env, run_as=account.popen_args(), timeout=WRITE_TIMEOUT_S, **kwargs)
    try:
        result = json.loads(out.strip().splitlines()[-1]) if out.strip() else None
    except (ValueError, IndexError):
        result = None
    if not isinstance(result, dict):
        return {"ok": False, "changed": False, "path": None, "backup": None, "method": "edit",
                "error": (err or out or f"exit {code}").strip()[-300:]}
    return result


def write_as(account: Any, kind: str, url: str, *, remove: bool = False, drop_empty: bool = False,
             timeout: Any = STANDARD, runner: Callable[..., tuple[int | None, str, str]] | None = None) -> dict:
    """:func:`write` for ``account`` (None: this process's own user, in process). Another account: a child
    ``python -I -m agent_nexus.cli mcp link`` started as that user, so the file keeps its owner and ACL."""
    if account is None:
        return write(kind, url, remove=remove, drop_empty=drop_empty, timeout=timeout)
    args = ["mcp", "link", "--kind", kind, "--url", url, "--json"]
    if remove:
        args.append("--remove")
    if drop_empty:
        args.append("--drop-empty")
    if timeout is None:
        args.append("--no-timeout")
    elif timeout is not STANDARD:
        args += ["--timeout", json.dumps(timeout)]
    return run_child(account, args, runner=runner)


def cli_main(args: Any, out=print) -> int:
    """``anx mcp link --kind K --url URL [--remove] [--json]`` (the child the service starts as the user)."""
    timeout: Any = STANDARD
    if getattr(args, "no_timeout", False):
        timeout = None
    elif getattr(args, "timeout", None) is not None:
        try:
            timeout = json.loads(args.timeout)
        except ValueError:
            timeout = STANDARD
        if not isinstance(timeout, (int, float, str)) or isinstance(timeout, bool):
            timeout = STANDARD
    result = write(args.kind, args.url, remove=bool(args.remove), drop_empty=bool(getattr(args, "drop_empty", False)),
                   timeout=timeout)
    if getattr(args, "json", False):
        out(json.dumps({k: v for k, v in result.items() if k != "entry"} | {"entry": result.get("entry")},
                       ensure_ascii=False, default=str))
    else:
        verb = "removed" if args.remove else "linked"
        if result["error"]:
            out(f"anx mcp: {args.kind}: {result['error']}")
        elif result["changed"]:
            out(f"{result['path']}: {verb} {NAME}" + (f" (backup {result['backup']})" if result["backup"] else ""))
        else:
            out(f"{result['path']}: already {'clean' if args.remove else 'set up'}")
    return 0 if result["ok"] else 1


def _same_user(recorded: str, me: str) -> bool:
    return str(recorded or "").rsplit("\\", 1)[-1].lower() == str(me or "").rsplit("\\", 1)[-1].lower()


def unlink_offline(root: Path, kinds: list[str] | tuple[str, ...] = KINDS, me: str | None = None) -> list[dict]:
    """``anx mcp unlink`` while the client is not running: remove the entries recorded as ours, in the file recorded
    for them, but only those of the account this process runs as (``me``) -- another account's file is never
    edited from here (it would be the wrong profile, or the wrong owner)."""
    if me is None:
        import getpass
        try:
            me = getpass.getuser()
        except Exception:  # noqa: BLE001
            me = ""
    out: list[dict] = []
    owned = Ownership(root)
    for account, block in owned.load().items():
        for kind, entry in (block or {}).items():
            if kind not in kinds or not isinstance(entry, dict) or entry.get("state") not in ("ours", "pinned"):
                continue
            if not _same_user(account, me):
                out.append({"kind": kind, "account": account, "ok": True, "changed": False,
                            "error": None, "skipped": f"account {account}"})
                continue
            path = Path(entry["path"]) if entry.get("path") else config_path(kind)
            try:
                present = current(kind, path) if path else None
            except LinkError:
                present = None
            if present is None or mcp_probe.entry_url(present) != entry.get("url"):
                out.append({"kind": kind, "account": account, "ok": True, "changed": False, "error": None})
                continue
            result = write(kind, str(entry["url"]), remove=True, drop_empty=bool(entry.get("created")), path=path)
            if result.get("ok"):
                owned.set(account, kind, None)
            out.append({"kind": kind, "account": account, **result})
    return out


# -- ownership -------------------------------------------------------------------------------------------------

class Ownership:
    """``<root>/.anx/mcp-link.json``: ``{account: {kind: {state, url, path, method, at, backup, created, wait}}}``.

    ``wait`` (Codex, Gemini CLI; 延长 MCP 等待): ``{state: on|modified|unrestored, key, original, absent, wrote, at}``
    -- what the timeout key held before we wrote ours, so turning the setting off can put it back."""

    def __init__(self, root: Path):
        self.path = Path(root) / ".anx" / "mcp-link.json"
        self._lock = threading.Lock()

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, account: str, kind: str) -> dict | None:
        entry = (self.load().get(account) or {}).get(kind)
        return entry if isinstance(entry, dict) else None

    def set(self, account: str, kind: str, entry: dict | None) -> None:
        with self._lock:
            data = self.load()
            block = dict(data.get(account) or {})
            if entry is None:
                block.pop(kind, None)
            else:
                block[kind] = entry
            if block:
                data[account] = block
            else:
                data.pop(account, None)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.path.with_name(self.path.name + ".tmp")
                temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(temp, self.path)
            except OSError as exc:
                log.warning("mcp link: cannot save %s: %s", self.path, exc)


LABELS = {
    "local": "已接入（{use}）",
    "gateway_config": "已通过云端网关接入（配置项 {use}），不重复添加",
    "gateway_account": "已通过云端网关接入（{account_label}），不重复添加",
    "gateway_account_stale": "已通过云端网关接入（{account_label}），不重复添加；但它的工具列表过时（缺少 device_list、"
                             "file_copy 等新工具），请在{vendor}的连接器设置里刷新它",
    "both": "已接入（{use}）；另有 {also}，对话里只用 {use}",
    "none": "未接入",
    "unknown": "无法确定",
    "name_taken": "名称被占用（agent-nexus 指向别处），不修改",
    "unsupported": "不支持写入，对话中临时接入",
    "too_old": "Codex 版本过旧（{version}），不支持 URL 方式的 MCP；更新 Codex 后自动接入",
    "user_removed": "已被手动移除",
}


def label(view: dict, local_ok: bool | None = None) -> str:
    """The Agent page's text for a connector view. ``local_ok``: whether the 本机 MCP can serve a run now (only
    Codex' account connector depends on it: its runs attach the 本机 MCP per run, see :meth:`MCPLink.for_run`)."""
    state = "user_removed" if view.get("entry") == "user_removed" and view["state"] == "none" else view["state"]
    if view.get("too_old"):
        state = "too_old"
    elif state == "gateway_account" and view.get("stale"):
        state = "gateway_account_stale"
    use = view.get("use") or ""
    account_label = (f"Claude 账户连接器 {use.removeprefix('claude.ai ')}" if view.get("kind") == "claude"
                     else f"ChatGPT 账户连接器 {use}" if view.get("kind") == "codex" else use)
    if view.get("source") == "user":
        account_label = "你确认已连接"
    if view.get("kind") == "codex" and view["state"] == "gateway_account" and not view.get("too_old"):
        # codex exec (approval "never") cannot approve an account app's tools: chat runs do not use it
        text = f"{account_label}（codex exec 无法审批它的工具）"
        text += CODEX_ACCOUNT_NOTE if local_ok is not False else CODEX_ACCOUNT_DOWN
        if view.get("stale"):
            text += "；它的工具列表也过时了，在交互式 Codex 里用之前，请到 ChatGPT 的连接器设置里刷新"
        return text
    return LABELS.get(state, state).format(use=use, also="、".join(view.get("also") or []),
                                           account_label=account_label, version=view.get("cli_version") or "",
                                           vendor="Claude" if view.get("kind") == "claude" else " ChatGPT ")


#: Codex with an account connector: what chat runs do instead (see for_run), when the 本机 MCP can / cannot serve.
CODEX_ACCOUNT_NOTE = "；对话中临时接入本机 MCP（不写入配置）"
CODEX_ACCOUNT_DOWN = "；本机 MCP 不可用，对话里调用不了 Agent Nexus 工具，请先开启本机 MCP（本机控制台 → 设置 → 本机 MCP）"


def version_ok(kind: str, version: str | None) -> bool:
    """False only for a known Codex version older than :data:`CODEX_URL_MIN` (unknown: assume new enough)."""
    if kind != "codex" or not version:
        return True
    return registry._at_least(version, CODEX_URL_MIN)


def takes_url(d: Any) -> bool | None:
    """Whether a run of ``d`` can be given the 本机 MCP by flags: claude / codex / gemini (their own flags), an
    ACP agent (decided after ``initialize``: None), a text CLI only when its command has ``{mcp_url}``."""
    adapter = getattr(d, "adapter", None)
    if adapter in ("claude-stream", "codex-exec", "codex", "gemini", "api") or getattr(d, "kind", None) in KINDS:
        return True
    if adapter == "acp":
        return None
    templates = list(getattr(d, "command", None) or []) + list(getattr(d, "resume_command", None) or [])
    return any("{mcp_url}" in str(part) for part in templates)


class MCPLink:
    """Keeps the ``agent-nexus`` entries of this computer's CLIs in line with the 本机 MCP.

    ``local_mcp``: the :class:`~agent_nexus.local_mcp.LocalMcp`; ``accounts``: () -> the accounts whose CLIs to link
    (``[Account | None]``, None = this process's user); ``definitions``: () -> the CLI definitions;
    ``gateway_host``: () -> the gateway's host name."""

    def __init__(self, root: Path, *, local_mcp: Any, accounts: Callable[[], list[Any]],
                 definitions: Callable[[], dict], gateway_host: Callable[[], str],
                 writer: Callable[..., dict] | None = None, probe: mcp_probe.Probe | None = None,
                 locate: Callable[[Any, dict], str | None] | None = None,
                 system: Callable[[], bool] | None = None, version_of: Callable[..., str | None] | None = None):
        self.root = Path(root)
        self.local = local_mcp
        self.accounts = accounts
        self.definitions = definitions
        self.gateway_host = gateway_host
        self.writer = writer or write_as
        self.probe = probe or mcp_probe.Probe(self.root)
        self.owned = Ownership(self.root)
        self.locate = locate or registry.locate
        #: () -> this process is the Windows system service (LocalSystem): it never writes or probes for "nobody"
        self.system = system or self._is_system
        self.version_of = version_of or self._cli_version
        self._again: str | None = None
        self.suspended = False  # unlink_all(suspend=True): the client is being uninstalled
        self._task: asyncio.Task | None = None
        self._busy: asyncio.Lock | None = None
        self._versions: dict[str, str | None] = {}
        self._version_at: dict[str, float] = {}
        self._probed: dict[str, float] = {}
        self.last: dict[str, Any] = {}
        self.probe.on_change.append(self.soon)

    # -- who and where ---------------------------------------------------------------------------------------
    @staticmethod
    def account_name(account: Any) -> str:
        if account is not None and getattr(account, "name", None):
            return str(account.name)
        with contextlib.suppress(Exception):
            import getpass
            return getpass.getuser()
        return "user"

    @staticmethod
    def env_of(account: Any) -> dict:
        if account is not None:
            return account.env()
        from .. import agent_cli
        return agent_cli.user_env()

    @staticmethod
    def home_of(account: Any, env: dict) -> Path:
        if account is not None and getattr(account, "home", None):
            return Path(account.home)
        return _home(env)

    @staticmethod
    def _is_system() -> bool:
        if os.name != "nt":
            return False
        with contextlib.suppress(Exception):
            from .. import winsession
            return bool(winsession.is_system())
        return False

    def _accounts(self) -> list[Any]:
        """The accounts to link. As the Windows system service, "this process's user" (None) is SYSTEM while the
        environment would be the screen user's: never write, probe or run anything for it (only real accounts,
        each through :func:`write_as`, as that user)."""
        try:
            listed = list(self.accounts() or [None])
        except Exception:  # noqa: BLE001
            listed = [None]
        listed = listed or [None]
        if self.system():
            listed = [account for account in listed if account is not None]
        return listed

    @staticmethod
    def _cli_version(binary: str, env: dict, run_as: dict | None) -> str | None:
        code, text = registry.run_capture(registry.runnable(binary, ["--version"]), env, timeout=10.0, run_as=run_as)
        return registry.parse_version(text) if code is not None else None

    def note_version(self, account: Any, kind: str, version: str | None) -> None:
        """A version the CLI probe already found (agent_info): no need to run ``--version`` again."""
        if kind == "codex" and version:
            key = f"{self.account_name(account)}|{kind}"
            self._versions[key] = version
            self._version_at[key] = time.monotonic()

    async def _learn_version(self, kind: str, account: Any, binary: str, env: dict) -> None:
        if kind != "codex":
            return
        key = f"{self.account_name(account)}|{kind}"
        stamp = self._version_at.get(key)
        if key in self._versions and stamp is not None and time.monotonic() - stamp < RECONCILE_EVERY_S:
            return
        self._version_at[key] = time.monotonic()
        run_as = account.popen_args() if account is not None else None
        try:
            self._versions[key] = await asyncio.to_thread(self.version_of, binary, env, run_as)
        except Exception:  # noqa: BLE001
            self._versions[key] = None

    # -- views -----------------------------------------------------------------------------------------------
    def view(self, kind: str, account: Any, env: dict | None = None, workdir: str | None = None) -> dict:
        env = env if env is not None else self.env_of(account)
        name = self.account_name(account)
        version = self._versions.get(f"{name}|{kind}")
        resolved = mcp_probe.resolve(kind, account=name, home=self.home_of(account, env), env=env,
                                     gateway_host=self.gateway_host() or "", local_url=self.local.url,
                                     probe=self.probe, version=version, workdir=workdir)
        if not version_ok(kind, version) and resolved["state"] in ("none", "unknown", "local", "name_taken"):
            resolved.update(state="unsupported", too_old=True, use=None, tool_prefix="")
        owned = self.owned.get(name, kind) if kind in KINDS else None
        resolved["entry"] = (owned or {}).get("state")
        if resolved["state"] == "name_taken" and self._entry_is_ours(account, kind, resolved):
            # ours, written for another port: still ours (reconcile rewrites the URL)
            resolved.update(state="local", use=NAME, tool_prefix=mcp_probe.tool_prefix(kind, NAME))
        if owned and owned.get("state") in ("ours", "pinned") and resolved["state"] in ("none", "unknown"):
            # recorded as ours but gone from the file: the user took it out
            resolved["entry"] = "user_removed" if owned.get("state") == "ours" else owned.get("state")
        if owned and owned.get("backup"):
            resolved["backup"] = owned["backup"]
        record = (owned or {}).get("wait")
        if isinstance(record, dict) and kind in WAIT_KEY:
            value = None
            with contextlib.suppress(Exception):
                entry = current(kind, config_path(kind, self.home_of(account, env), env))
                value = (entry or {}).get(WAIT_KEY[kind][0])
            resolved["wait"] = {"state": record.get("state"), "value": value,
                                "seconds": value / WAIT_KEY[kind][1] if isinstance(value, (int, float)) else None}
        local_state = self.local.state()
        resolved["label"] = label(resolved, local_ok=local_state not in ("off", "exposed", *NOT_SERVING))
        return resolved

    def for_run(self, kind: str, account: Any, env: dict | None = None, *, api: bool = False,
                workdir: str | None = None, d: Any = None) -> dict:
        """The connector a chat run uses: the :func:`view` (for ``workdir``: Claude's folder scopes count) plus
        ``attach`` (None: the CLI's own config, "flags": per-run flags, "inprocess": an API model) and ``ready``
        (the 本机 MCP serves the gateway's tools). ``d``: the CLI's definition (which adapters can take flags)."""
        local_state = self.local.state()
        usable = local_state not in ("off", "exposed")
        serving = usable and local_state not in NOT_SERVING
        if api:
            view = {"kind": kind, "account": self.account_name(account), "state": "local" if usable else "none",
                    "use": NAME if usable else None, "tool_prefix": "", "source": "inprocess", "also": [],
                    "entry": None}
        else:
            try:
                view = self.view(kind, account, env, workdir)
            except Exception as exc:  # noqa: BLE001 - never block a chat over this
                view = {"kind": kind, "account": None, "state": "unknown", "use": None, "tool_prefix": "",
                        "source": None, "also": [], "entry": None, "error": str(exc)[:200]}
        attach = None
        flags = takes_url(d) if d is not None else (True if kind in KINDS else None)
        if api:
            attach = "inprocess" if usable else None
        elif view["state"] == "name_taken":
            # the user's own "agent-nexus": never a second connector next to it; the context names it
            view = {**view, "use": NAME, "tool_prefix": mcp_probe.tool_prefix(kind, NAME)}
        elif view["state"] in ("none", "unsupported") and serving and not view.get("too_old") and flags is not False:
            attach = "flags"
            view = {**view, "use": NAME, "tool_prefix": mcp_probe.tool_prefix(kind, NAME)}
        elif view["state"] == "unsupported" and not attach:
            view = {**view, "use": None}
        elif kind == "codex" and view["state"] == "gateway_account" and serving and flags is not False \
                and not view.get("too_old"):
            # A ChatGPT account connector (an app under codex_apps): `codex exec` runs with approval "never" and an
            # app's tools cannot be approved per run without its connector id, so every call would be refused
            # ("MCP tool call requires approval"). The run attaches the 本机 MCP with per-run flags instead (no
            # file is written; decide() still keeps config.toml free of an entry while the account connector exists).
            old = view.get("use")
            view = {**view, "use": NAME, "tool_prefix": mcp_probe.tool_prefix(kind, NAME), "shadowed": old,
                    "also": [n for n in [old, *(view.get("also") or [])] if n and n != NAME], "run_only": True}
            attach = "flags"
        if view["state"] == "both" and local_state != "ready" and view.get("also"):
            # the local entry cannot serve now: the run uses the other Agent Nexus connector, which may well work
            other, rest = view["also"][0], view["also"][1:]
            view = {**view, "use": other, "also": [NAME] + rest, "tool_prefix": mcp_probe.tool_prefix(kind, other),
                    "swapped": True}
        return {**view, "attach": attach, "ready": local_state == "ready", "local_state": local_state,
                "local_reason": self.local.status()["reason"], "url": self.local.url,
                "acp_pending": flags is None and attach == "flags"}

    async def status(self) -> list[dict]:
        out = []
        defs = self.definitions()
        for account in self._accounts():
            env = await asyncio.to_thread(self.env_of, account)
            for kind in KINDS:
                d = defs.get(kind)
                if d is None:
                    continue
                binary = await asyncio.to_thread(self._locate, d, env)
                if not binary:
                    continue
                await self._learn_version(kind, account, binary, env)
                view = await asyncio.to_thread(self.view, kind, account, env)
                view["installed"] = True
                out.append(view)
        return out

    def _locate(self, d: Any, env: dict) -> str | None:
        try:
            return self.locate(d, env)
        except Exception:  # noqa: BLE001
            return None

    # -- writing ---------------------------------------------------------------------------------------------
    def wait_wanted(self, kind: str) -> bool:
        """延长 MCP 等待 applies to this CLI (the setting is on; Codex and Gemini CLI only)."""
        return kind in WAIT_KEY and self.local.settings.get("extended_wait", True) is not False

    def _file_entry(self, account: Any, kind: str) -> dict | None:
        env = self.env_of(account)
        path = config_path(kind, self.home_of(account, env), env)
        try:
            return current(kind, path) if path else None
        except LinkError:
            return None

    def _call_writer(self, account: Any, kind: str, url: str, *, remove: bool, drop_empty: bool,
                     timeout: Any = STANDARD) -> dict:
        if timeout is STANDARD:  # writers written before the timeout existed keep working
            return self.writer(account, kind, url, remove=remove, drop_empty=drop_empty)
        return self.writer(account, kind, url, remove=remove, drop_empty=drop_empty, timeout=timeout)

    def _write(self, account: Any, kind: str, *, remove: bool, pinned: bool = False) -> dict:
        """Add, update or remove our entry. An update never resets the timeout key (it keeps what the file holds:
        the user's value or ours, which the wait step owns); a new entry while 延长 MCP 等待 is on gets the long
        timeout at once (one write, one backup), recorded with the standard value as its original."""
        name = self.account_name(account)
        owned = self.owned.get(name, kind) or {}
        record = owned.get("wait") if isinstance(owned.get("wait"), dict) else None
        timeout: Any = STANDARD
        new_record = record
        if remove:
            url = owned.get("url") or self.local.url
        else:
            existing = self._file_entry(account, kind) if kind in WAIT_KEY else None
            if existing is not None:  # an update (another port, an outdated key): keep its timeout
                key = WAIT_KEY[kind][0]
                timeout = existing.get(key, ABSENT)
                url = entry_url_for(self.local.url, bool(record and record.get("state") in ("on", "modified")))
            elif self.wait_wanted(kind):
                timeout = wait_written(kind)
                url = entry_url_for(self.local.url, True)
                new_record = {"state": "on", "key": WAIT_KEY[kind][0], "original": TOOL_TIMEOUT_S * WAIT_KEY[kind][1],
                              "absent": False, "wrote": timeout, "at": time.time()}
            else:
                url = self.local.url
                new_record = None
        path = config_path(kind, self.home_of(account, self.env_of(account)), self.env_of(account))
        created = owned.get("created")
        if not remove and created is None:
            created = not path or not path.exists() or (kind != "codex" and self._no_servers(path))
        result = self._call_writer(account, kind, url, remove=remove, drop_empty=bool(remove and created),
                                   timeout=timeout)
        if result.get("ok"):
            if remove:
                self.owned.set(name, kind, None)
            else:
                entry = {"state": "pinned" if pinned else "ours", "url": url,
                         "path": result.get("path"), "method": result.get("method"),
                         "at": time.time(), "created": bool(created),
                         "backup": result.get("backup") or owned.get("backup")}
                if new_record:
                    entry["wait"] = new_record
                self.owned.set(name, kind, entry)
        self.last[f"{name}|{kind}"] = result
        return result

    def _reconcile_wait(self, account: Any, kind: str) -> dict | None:
        """延长 MCP 等待 for one of our entries: apply it, keep it, or put the original value back.

        On: record what the timeout key holds now (or that it is absent) and write ours plus ``?wait_max=``. Off:
        restore the original only if the file still holds the value we wrote; a value the user changed since is left
        as it is (``unrestored``: 已被手动修改，未还原). The URL query always follows the setting. Returns what was
        written (None: nothing)."""
        if kind not in WAIT_KEY:
            return None
        name = self.account_name(account)
        owned = self.owned.get(name, kind)
        if not owned or owned.get("state") not in ("ours", "pinned"):
            return None
        entry = self._file_entry(account, kind)
        if entry is None or not mcp_probe.is_local_url(mcp_probe.entry_url(entry) or "", self.local.url) \
                or strip_wait(mcp_probe.entry_url(entry)) != strip_wait(owned.get("url")):
            return None
        key = WAIT_KEY[kind][0]
        wrote = wait_written(kind)
        cur = entry.get(key, ABSENT)
        record = owned.get("wait") if isinstance(owned.get("wait"), dict) else None
        state = (record or {}).get("state")
        want_on = self.wait_wanted(kind)
        now = time.time()
        timeout: Any = cur
        new_record = record
        if want_on:
            if state in ("on", "modified"):
                new_record = {**record, "state": "on" if cur == record.get("wrote", wrote) else "modified"}
            else:  # turning on: whatever is there now is the original, silently
                new_record = {"state": "on", "key": key, "original": cur, "absent": key not in entry, "wrote": wrote,
                              "at": now}
                timeout = wrote
            url = entry_url_for(self.local.url, True)
        else:
            if state in ("on", "modified"):
                if cur == record.get("wrote", wrote):
                    timeout = None if record.get("absent") else record.get("original")
                    new_record = None
                else:
                    new_record = {**record, "state": "unrestored", "at": now}
            url = entry_url_for(self.local.url, False)
        changed_file = timeout != cur or (key in entry) != (timeout is not None) or mcp_probe.entry_url(entry) != url
        if changed_file:
            result = self._call_writer(account, kind, url, remove=False, drop_empty=False, timeout=timeout)
            self.last[f"{name}|{kind}"] = result
            if not result.get("ok"):
                return {"ok": False, **{k: result.get(k) for k in ("error", "path")}}
            owned = {**owned, "url": url, "at": now, "backup": result.get("backup") or owned.get("backup")}
        if new_record != record or changed_file:
            owned = dict(owned)
            if new_record:
                owned["wait"] = new_record
            else:
                owned.pop("wait", None)
            self.owned.set(name, kind, owned)
        if changed_file:
            return {"ok": True, "changed": True, "path": result.get("path"), "backup": result.get("backup"),
                    "error": None, "wait": (new_record or {}).get("state", "off")}
        return None

    @staticmethod
    def _no_servers(path: Path) -> bool:
        try:
            data = json.loads(integrate._read(path) or "{}")
        except ValueError:
            return False
        return isinstance(data, dict) and "mcpServers" not in data

    def _entry_is_ours(self, account: Any, kind: str, view: dict) -> bool:
        """The file's ``agent-nexus`` entry is the one we wrote (recorded, and its URL unchanged; our own
        ``?wait_max=`` does not count)."""
        owned = self.owned.get(self.account_name(account), kind)
        if not owned or owned.get("state") not in ("ours", "pinned"):
            return False
        path = config_path(kind, self.home_of(account, self.env_of(account)), self.env_of(account))
        try:
            entry = current(kind, path) if path else None
        except LinkError:
            return False
        return entry is not None and strip_wait(mcp_probe.entry_url(entry)) == strip_wait(owned.get("url"))

    def _entry_outdated(self, account: Any, kind: str) -> bool:
        """Our entry lacks something the current version writes (e.g. the approval setting added in 0.8.34)."""
        env = self.env_of(account)
        path = config_path(kind, self.home_of(account, env), env)
        try:
            entry = current(kind, path) if path else None
        except LinkError:
            return False
        if entry is None or kind == "claude":
            return False
        want = wanted(kind, mcp_probe.entry_url(entry) or self.local.url)
        skip = WAIT_KEY[kind][0]  # the timeout belongs to the user or to the wait step, never "outdated"
        return any(entry.get(key) != value for key, value in want.items() if key != skip)

    def decide(self, kind: str, account: Any, view: dict, local_state: str, auto: bool) -> str | None:
        """"add" / "remove" / "update" / None for one CLI (the decision table of docs/design.md)."""
        name = self.account_name(account)
        owned = self.owned.get(name, kind) or {}
        ours = self._entry_is_ours(account, kind, view)
        if local_state in ("off", "exposed"):
            return "remove" if ours else None
        if view.get("too_old"):
            return "remove" if ours else None  # this Codex could not even read its config with it
        state = view["state"]
        if kind == "codex" and auto and not self.suspended and (
                state == "gateway_account" or (state == "both" and not view.get("others_config"))):
            # A ChatGPT account app is Codex's only other way to the gateway, and codex exec cannot approve its
            # tools (and its tool list is a snapshot): the owner wants ours there for good, approved.
            if ours:
                return "update" if self._entry_outdated(account, kind) else None
            if owned.get("state") in ("user_removed",) or local_state in NOT_SERVING:
                return None
            return "add"
        if state in ("gateway_config", "gateway_account", "both"):
            # never two sets of Agent Nexus tools: ours goes unless the owner pinned it
            return "remove" if ours and owned.get("state") != "pinned" else None
        if state == "local":
            if ours and (strip_wait(owned.get("url")) != self.local.url or self._entry_outdated(account, kind)):
                return "update"
            return None
        if local_state in NOT_SERVING:
            return None  # keep what is there (it starts working later), add nothing new
        if state == "none" and auto and not self.suspended and \
                owned.get("state") not in ("ours", "user_removed", "pinned"):
            return "add"
        return None

    async def reconcile(self, reason: str = "") -> list[dict]:
        """Bring every installed CLI's entry in line; returns what was done."""
        if self._busy is None:
            self._busy = asyncio.Lock()
        async with self._busy:
            done = []
            local_state = self.local.state()
            auto = bool(self.local.settings.get("auto_config", True))
            defs = self.definitions()
            for account in self._accounts():
                env = await asyncio.to_thread(self.env_of, account)
                name = self.account_name(account)
                for kind in KINDS:
                    d = defs.get(kind)
                    if d is None:
                        continue
                    binary = await asyncio.to_thread(self._locate, d, env)
                    if not binary:
                        continue
                    await self._learn_version(kind, account, binary, env)
                    view = await asyncio.to_thread(self.view, kind, account, env)
                    owned = self.owned.get(name, kind) or {}
                    if view["entry"] == "user_removed" and owned.get("state") == "ours":
                        record = {**owned, "state": "user_removed", "at": time.time()}
                        record.pop("wait", None)
                        self.owned.set(name, kind, record)
                        continue
                    stamp = self._probed.get(f"{name}|{kind}")
                    if view["state"] == "unknown" and kind in ("claude", "codex") and \
                            local_state not in ("off", "exposed") and \
                            (stamp is None or time.monotonic() - stamp > PROBE_RETRY_S):
                        self._probed[f"{name}|{kind}"] = time.monotonic()
                        await self.probe_account(kind, account, binary, env)
                        view = await asyncio.to_thread(self.view, kind, account, env)
                    action = self.decide(kind, account, view, local_state, auto)
                    if action is not None:
                        result = await asyncio.to_thread(self._write, account, kind, remove=action == "remove")
                        done.append({"account": name, "kind": kind, "action": action, **{
                            k: result.get(k) for k in ("ok", "changed", "path", "backup", "error")}})
                        if not result.get("ok"):
                            log.warning("mcp link: %s %s for %s failed: %s", action, kind, name, result.get("error"))
                    if action != "remove" and local_state not in ("off", "exposed"):
                        waited = await asyncio.to_thread(self._reconcile_wait, account, kind)
                        if waited is not None:
                            done.append({"account": name, "kind": kind, "action": "wait", **waited})
                            if not waited.get("ok"):
                                log.warning("mcp link: wait setting for %s %s failed: %s", kind, name,
                                            waited.get("error"))
            if done:
                log.info("mcp link (%s): %s", reason or "reconcile", done)
            return done

    async def probe_account(self, kind: str, account: Any, binary: str | None = None, env: dict | None = None) -> dict:
        env = env if env is not None else await asyncio.to_thread(self.env_of, account)
        name = self.account_name(account)
        if binary is None:
            d = self.definitions().get(kind)
            binary = await asyncio.to_thread(self._locate, d, env) if d else None
        if not binary:
            return {"state": "unknown", "error": "没有找到这个 CLI"}
        run_as = account.popen_args() if account is not None else None
        host = self.gateway_host() or ""
        if kind == "claude":
            answer = await asyncio.to_thread(mcp_probe.probe_claude, binary, env, run_as, host)
        elif kind == "codex":
            from .. import agent_cli
            signed = await asyncio.to_thread(agent_cli.codex_signed_in, env)
            if signed is False:
                answer = {"state": "absent", "connectors": [], "note": "没有登录 ChatGPT"}
            else:
                answer = await asyncio.to_thread(mcp_probe.probe_codex, binary, env, run_as, host)
        else:
            return {"state": "absent"}
        if answer.get("state") != "unknown":
            current_ = self.probe.get(name, kind) or {}
            if current_.get("source") != "user":
                self.probe.put(name, kind, {**answer, "source": "probe",
                                            "cli_version": self._versions.get(f"{name}|{kind}")})
        return answer

    async def action(self, kind: str, account_name: str | None, action: str, *, force: bool = False) -> dict:
        """The console's buttons: add / remove / probe / mark_connected / mark_missing."""
        listed = self._accounts()
        if not listed:
            return {"error": "没有可以接入的账户：服务以 SYSTEM 运行，现在没有用户登录（或终端账户设为 SYSTEM）",
                    "code": "no_account", "status": 409}
        account = next((a for a in listed if account_name in (None, "", self.account_name(a))), None)
        if account_name and self.account_name(account) != account_name:
            return {"error": f"不认识的账户：{account_name}", "code": "unknown_account", "status": 404}
        if kind not in KINDS:
            return {"error": f"不支持写入 {kind} 的配置：对话中会临时接入", "code": "unsupported", "status": 400}
        name = self.account_name(account)
        env = await asyncio.to_thread(self.env_of, account)
        if action == "probe":
            answer = await self.probe_account(kind, account, env=env)
            return {"probe": answer, "cli": await asyncio.to_thread(self.view, kind, account, env)}
        if action in ("mark_connected", "mark_missing"):
            self.probe.manual(name, kind, action == "mark_connected")
            await self.reconcile("manual answer")
            return {"cli": await asyncio.to_thread(self.view, kind, account, env)}
        if action == "add":
            if self.local.state() in ("off", "exposed"):
                return {"error": self.local.status()["reason"], "code": "local_mcp_off", "status": 409}
            view = await asyncio.to_thread(self.view, kind, account, env)
            if view["state"] == "name_taken":
                return {"error": "agent-nexus 这个名称已被另一个配置项占用，未修改", "code": "name_taken", "status": 409}
            codex_account = kind == "codex" and view["state"] == "gateway_account"  # see decide()
            if view["state"] in ("gateway_config", "gateway_account") and not force and not codex_account:
                return {"error": f"{view['label']}：再添加会出现两套 Agent Nexus 工具", "code": "duplicate",
                        "status": 409, "cli": view}
            result = await asyncio.to_thread(self._write, account, kind, remove=False,
                                             pinned=view["state"] == "gateway_config" or
                                             (view["state"] == "gateway_account" and not codex_account))
        elif action == "remove":
            if not self._entry_is_ours(account, kind, {}):
                path = config_path(kind, self.home_of(account, env), env)
                try:
                    entry = current(kind, path) if path else None
                except LinkError as exc:
                    return {"error": str(exc), "code": "bad_config", "status": 409}
                if entry is None:
                    owned = self.owned.get(name, kind)
                    if owned:
                        self.owned.set(name, kind, {**owned, "state": "user_removed", "at": time.time()})
                    return {"cli": await asyncio.to_thread(self.view, kind, account, env), "result": {"ok": True}}
                if strip_wait(mcp_probe.entry_url(entry)) != self.local.url:
                    return {"error": "这个 agent-nexus 配置项不是本机 MCP 写入的，未修改", "code": "not_ours",
                            "status": 409}
            result = await asyncio.to_thread(self._write, account, kind, remove=True)
            if result.get("ok"):  # removed on purpose: never added back by itself
                self.owned.set(name, kind, {"state": "user_removed", "url": self.local.url, "at": time.time(),
                                            "backup": result.get("backup"), "path": result.get("path")})
        else:
            return {"error": f"不支持的操作：{action}", "code": "bad_action", "status": 400}
        if not result.get("ok"):
            return {"error": result.get("error") or "写入失败", "code": "write_failed", "status": 500, "result": result}
        return {"result": result, "cli": await asyncio.to_thread(self.view, kind, account, env)}

    async def unlink_all(self, *, suspend: bool = False) -> list[dict]:
        """Remove every entry of ours. ``suspend`` (the uninstaller): add none again while this process runs."""
        if suspend:
            self.suspended = True
        done = []
        for account in self._accounts():
            for kind in KINDS:
                if self._entry_is_ours(account, kind, {}):
                    result = await asyncio.to_thread(self._write, account, kind, remove=True)
                    done.append({"kind": kind, **result})
        return done

    # -- scheduling ------------------------------------------------------------------------------------------
    def soon(self, reason: str = "change") -> None:
        """Reconcile shortly (from any thread or callback); coalesced."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = getattr(self, "_loop", None)
            if loop is None or loop.is_closed():
                return
            loop.call_soon_threadsafe(self._schedule, reason)
            return
        self._schedule(reason)

    def _schedule(self, reason: str) -> None:
        if getattr(self, "_pending", None) is not None and not self._pending.done():
            # one is waiting or running: it runs (again) once more afterwards, so a change made meanwhile
            # (the feature turned off, a probe's answer) is never lost
            self._again = reason
            return

        async def later():
            current_reason = reason
            while True:
                self._again = None
                await asyncio.sleep(1.0)
                try:
                    await self.reconcile(current_reason)
                except Exception:  # noqa: BLE001
                    log.exception("mcp link: reconcile failed")
                if self._again is None:
                    return
                current_reason = self._again
        self._pending = asyncio.ensure_future(later())

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._loop_forever())

    async def _loop_forever(self) -> None:
        await asyncio.sleep(5.0)  # after the first cloud state
        while True:
            try:
                await self.reconcile("periodic")
            except Exception:  # noqa: BLE001
                log.exception("mcp link: reconcile failed")
            await asyncio.sleep(RECONCILE_EVERY_S)

    def close(self) -> None:
        for task in (self._task, getattr(self, "_pending", None)):
            if task is not None:
                task.cancel()
