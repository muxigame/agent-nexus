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


def wanted(kind: str, url: str) -> dict:
    if kind == "claude":
        return {"type": "http", "url": url}
    if kind == "codex":
        return {"url": url, "tool_timeout_sec": TOOL_TIMEOUT_S}
    if kind == "gemini":
        return {"httpUrl": url, "timeout": TOOL_TIMEOUT_S * 1000}
    raise LinkError(f"unsupported CLI {kind!r}")


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

def plan_json(path: Path, kind: str, url: str, *, remove: bool, drop_empty: bool = False) -> integrate.Plan:
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
        entry = wanted(kind, url)
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


def plan_codex(path: Path, url: str, *, remove: bool) -> integrate.Plan:
    plan = integrate.Plan(path, bom=integrate._has_bom(path))
    original = integrate._read(path)
    text = original or ""
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LinkError(f"{path} 不是有效的 TOML（{exc}），未做任何修改") from None
    servers = parsed.get("mcp_servers") if isinstance(parsed.get("mcp_servers"), dict) else {}
    present = servers.get(NAME)
    entry = wanted("codex", url)
    if not remove and isinstance(present, dict) and present == entry:
        return plan
    if remove and present is None:
        return plan
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    if present is not None:
        span = _codex_span(lines)
        if span is None:
            raise LinkError(f"{path}：agent-nexus 不是一个独立的 [mcp_servers.agent-nexus] 表，请手动修改")
        lines = lines[:span[0]] + lines[span[1]:]
    if not remove:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += newline
        block = ([newline] if lines else []) + [
            f"{ENTRY_COMMENT}{newline}", f"[mcp_servers.{NAME}]{newline}",
            f"url = {json.dumps(url)}{newline}", f"tool_timeout_sec = {TOOL_TIMEOUT_S}{newline}"]
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


def plan(kind: str, path: Path, url: str, *, remove: bool, drop_empty: bool = False) -> integrate.Plan:
    if kind == "codex":
        return plan_codex(path, url, remove=remove)
    if kind in ("claude", "gemini"):
        return plan_json(path, kind, url, remove=remove, drop_empty=drop_empty)
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
          drop_empty: bool = False, use_cli: bool = True, now: float | None = None, path: Path | None = None) -> dict:
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
            planned = plan(kind, path, url, remove=remove, drop_empty=drop_empty)
            if planned.text is None:
                result.update(ok=True, entry=current(kind, path))
                return result
            try:
                _apply(planned, mtime, now)
            except InterruptedError:
                time.sleep(0.2)
                continue
            after = current(kind, path)
            if (after is not None) if remove else not integrate._same(after, wanted(kind, url)):
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


def write_as(account: Any, kind: str, url: str, *, remove: bool = False, drop_empty: bool = False,
             runner: Callable[..., tuple[int | None, str, str]] | None = None) -> dict:
    """:func:`write` for ``account`` (None: this process's own user, in process). Another account: a child
    ``python -I -m agent_nexus.cli mcp link`` started as that user, so the file keeps its owner and ACL."""
    if account is None:
        return write(kind, url, remove=remove, drop_empty=drop_empty)
    argv = [_python(), "-I", "-m", "agent_nexus.cli", "mcp", "link", "--kind", kind, "--url", url, "--json"]
    if remove:
        argv.append("--remove")
    if drop_empty:
        argv.append("--drop-empty")
    env = account.env()
    runner = runner or mcp_probe.run_user
    code, out, err = runner(argv, env, run_as=account.popen_args(), timeout=WRITE_TIMEOUT_S)
    try:
        result = json.loads(out.strip().splitlines()[-1]) if out.strip() else None
    except (ValueError, IndexError):
        result = None
    if not isinstance(result, dict):
        return {"ok": False, "changed": False, "path": None, "backup": None, "method": "edit",
                "error": (err or out or f"exit {code}").strip()[-300:]}
    return result


def cli_main(args: Any, out=print) -> int:
    """``anx mcp link --kind K --url URL [--remove] [--json]`` (the child the service starts as the user)."""
    result = write(args.kind, args.url, remove=bool(args.remove), drop_empty=bool(getattr(args, "drop_empty", False)))
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
    """``<root>/.anx/mcp-link.json``: ``{account: {kind: {state, url, path, method, at, backup, created}}}``."""

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


def label(view: dict) -> str:
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
    return LABELS.get(state, state).format(use=use, also="、".join(view.get("also") or []),
                                           account_label=account_label, version=view.get("cli_version") or "",
                                           vendor="Claude" if view.get("kind") == "claude" else " ChatGPT ")


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
        resolved["label"] = label(resolved)
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
    def _write(self, account: Any, kind: str, *, remove: bool, pinned: bool = False) -> dict:
        name = self.account_name(account)
        owned = self.owned.get(name, kind) or {}
        url = self.local.url if not remove else (owned.get("url") or self.local.url)
        path = config_path(kind, self.home_of(account, self.env_of(account)), self.env_of(account))
        created = owned.get("created")
        if not remove and created is None:
            created = not path or not path.exists() or (kind != "codex" and self._no_servers(path))
        result = self.writer(account, kind, url, remove=remove, drop_empty=bool(remove and created))
        if result.get("ok"):
            if remove:
                self.owned.set(name, kind, None)
            else:
                self.owned.set(name, kind, {"state": "pinned" if pinned else "ours", "url": url,
                                            "path": result.get("path"), "method": result.get("method"),
                                            "at": time.time(), "created": bool(created),
                                            "backup": result.get("backup") or owned.get("backup")})
        self.last[f"{name}|{kind}"] = result
        return result

    @staticmethod
    def _no_servers(path: Path) -> bool:
        try:
            data = json.loads(integrate._read(path) or "{}")
        except ValueError:
            return False
        return isinstance(data, dict) and "mcpServers" not in data

    def _entry_is_ours(self, account: Any, kind: str, view: dict) -> bool:
        """The file's ``agent-nexus`` entry is the one we wrote (recorded, and its URL unchanged)."""
        owned = self.owned.get(self.account_name(account), kind)
        if not owned or owned.get("state") not in ("ours", "pinned"):
            return False
        path = config_path(kind, self.home_of(account, self.env_of(account)), self.env_of(account))
        try:
            entry = current(kind, path) if path else None
        except LinkError:
            return False
        return entry is not None and mcp_probe.entry_url(entry) == owned.get("url")

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
        if state in ("gateway_config", "gateway_account", "both"):
            # never two sets of Agent Nexus tools: ours goes unless the owner pinned it
            return "remove" if ours and owned.get("state") != "pinned" else None
        if state == "local":
            if ours and owned.get("url") != self.local.url:
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
                        self.owned.set(name, kind, {**owned, "state": "user_removed", "at": time.time()})
                        continue
                    stamp = self._probed.get(f"{name}|{kind}")
                    if view["state"] == "unknown" and kind in ("claude", "codex") and \
                            local_state not in ("off", "exposed") and \
                            (stamp is None or time.monotonic() - stamp > PROBE_RETRY_S):
                        self._probed[f"{name}|{kind}"] = time.monotonic()
                        await self.probe_account(kind, account, binary, env)
                        view = await asyncio.to_thread(self.view, kind, account, env)
                    action = self.decide(kind, account, view, local_state, auto)
                    if action is None:
                        continue
                    result = await asyncio.to_thread(self._write, account, kind, remove=action == "remove")
                    done.append({"account": name, "kind": kind, "action": action, **{
                        k: result.get(k) for k in ("ok", "changed", "path", "backup", "error")}})
                    if not result.get("ok"):
                        log.warning("mcp link: %s %s for %s failed: %s", action, kind, name, result.get("error"))
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
            if view["state"] in ("gateway_config", "gateway_account") and not force:
                return {"error": f"{view['label']}：再添加会出现两套 Agent Nexus 工具", "code": "duplicate",
                        "status": 409, "cli": view}
            result = await asyncio.to_thread(self._write, account, kind, remove=False,
                                             pinned=view["state"] in ("gateway_config", "gateway_account"))
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
                if mcp_probe.entry_url(entry) != self.local.url:
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
