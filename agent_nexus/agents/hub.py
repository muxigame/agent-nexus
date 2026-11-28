"""AgentHub: the agent chat of one device (docs/agent-chat.md §9, §13).

It composes the CLI registry and installs, session discovery, runs, system-prompt fragments, the work-folder
picker and the per-run temp folders, and answers the ``agent_*`` reverse RPCs (feature ``agents/1``). The local
console calls the same handlers in process (``/api/agents/local/{op}``); the gateway forwards the cloud console's
calls (``/api/devices/{id}/agents/{op}``) and another device's relayed ones.

Everything runs as the 执行账户 (``account()``: an ``accounts.Account`` or None = this process's user): probes,
installs, runs, and session discovery under that account's HOME. Every handler takes a dict and returns a dict;
problems come back as ``{"error": "中文说明", "code": "..."}``, never as an exception.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..android import device_name
from . import prompts as prompts_mod
from . import registry, sessions, workdirs
from .api import config as api_config
from .api import service as api_service
from .installs import InstallError, Installs
from .mcp_probe import NAME as MCP_NAME
from .mcp_probe import codex_models, codex_models_cache
from .prompts import Prompts
from .runner import RunError, Runs
from .tmpdirs import TmpDirs
from .workdirs import Recent

log = logging.getLogger(__name__)

FEATURE = "agents/1"
#: The reverse RPCs of ``agents/1`` (§13), the only ops the consoles and the gateway forward.
OPS: tuple[str, ...] = (
    "agent_info", "agent_install", "agent_install_job",
    "agent_sessions", "agent_session_read", "agent_session_meta",
    "agent_run_open", "agent_run_send", "agent_run_read", "agent_run_answer", "agent_run_stop", "agent_run_close",
    "agent_run_list",
    "agent_dirs", "agent_prompts", "agent_prompt_save", "agent_prompt_preview",
    "agent_tmp_list", "agent_tmp_clean", "agent_models",
    *api_service.OPS,
)
READ_WAIT_S = 20.0
#: How long a forwarded call may take before the caller gives up waiting (it is never cancelled).
CALL_TIMEOUT_S = {"agent_info": 45.0, "agent_install": 45.0, "agent_sessions": 45.0, "agent_session_read": 45.0,
                  "agent_run_open": 60.0, "agent_api_test": 45.0, "agent_api_models": 45.0,
                  "agent_models": 45.0}
DEFAULT_TIMEOUT_S = 30.0
SWEEP_EVERY_S = 6 * 3600.0
NO_GATEWAY_NOTE = "无网关：ask_user / 邮件暂不可用，对话本身不受影响"


def call_timeout(op: str, params: dict | None = None) -> float:
    """How long a relay waits for ``op`` (a long poll's own wait plus 10 s)."""
    if op == "agent_run_read":
        try:
            wait = float((params or {}).get("wait") or 0)
        except (TypeError, ValueError):
            wait = 0.0
        return max(0.0, min(wait, READ_WAIT_S)) + 10.0
    return CALL_TIMEOUT_S.get(op, DEFAULT_TIMEOUT_S)


def configured_workspaces(root: Path) -> list[dict]:
    """The work folders this device registered with its gateway (``cloud.workspaces``; the device plugin's
    ``default_workdir`` when none are listed): ``[{name, path}]``. Never raises."""
    try:
        from .. import paths, sections
        data = paths.read_config(Path(root))
    except Exception:  # noqa: BLE001
        return []
    cloud = data.get("cloud") if isinstance(data.get("cloud"), dict) else {}
    entries = cloud.get("workspaces")
    if not isinstance(entries, dict):
        try:
            services = sections.plugin_entries(data)
            settings = {**services.get("workspace", {}).get("settings", {}),
                        **services.get("device", {}).get("settings", {})}
        except Exception:  # noqa: BLE001
            settings = {}
        default = settings.get("default_workdir")
        entries = {"default": default} if isinstance(default, str) and default else {}
    out = []
    for name, value in entries.items():
        path = value.get("path") if isinstance(value, dict) else value
        if isinstance(path, str) and path:
            resolved = (Path(root) / os.path.expanduser(path)).resolve()
            if resolved.is_dir():
                out.append({"name": str(name), "path": str(resolved)})
    return out


#: Agent Nexus tools that only read (claude readonly / ask: allowed without a prompt). exec_wait only
#: waits for and reads a command that was already approved. Tools the gateway keeps behind toolsearch
#: (curr_time, device_info, ...; design.md §20.1) still pass when called by name; through tooluse
#: they ask, like any tooluse call.
READ_TOOLS = ("device_list", "curr_time", "device_info", "exec_wait", "file_read", "file_list", "file_stat", "file_glob",
              "file_grep", "view_image", "mail_inbox", "mail_read", "mail_who", "mail_status", "mcplist", "mcpdetail",
              "toolsearch", "file_copy_status", "file_copy_list")
#: Agent Nexus tools that edit files on the workspace's device (gateway mode, ``edits``: without a prompt).
EDIT_TOOLS = ("file_write", "edit_file", "patch_file")
ATTACHED = ("local", "gateway_config", "gateway_account", "both")
#: the 本机 MCP cannot serve the gateway's tools in these states: a run that depends on it is warned
LOCAL_DOWN = ("off", "exposed", "not_joined", "revoked", "locked", "gateway_old", "device_access_off", "rejected")


def can_attach(nx: dict | None) -> bool | None:
    """Whether a run of this CLI gets an Agent Nexus connector (None: cannot tell)."""
    if not nx:
        return None
    if nx.get("state") in ("unknown", "name_taken"):
        return None  # name_taken: the user's own "agent-nexus" (whatever it is) -- the context stays generic
    if nx.get("state") in ("gateway_config", "gateway_account"):
        return True
    if nx.get("local_state") in LOCAL_DOWN:
        return nx.get("state") == "both"
    return nx.get("state") in ATTACHED or nx.get("attach") in ("flags", "inprocess")


def connector_name(nx: dict | None) -> str:
    """The one connector a run is told to use ("" when unknown or there is none)."""
    if not nx or nx.get("state") == "unknown" or not can_attach(nx):
        return ""
    return str(nx.get("use") or "")


def approved_tools(permission: str, mode: str = "gateway") -> list[str] | None:
    """The Agent Nexus tools a run may call without a prompt in ``permission`` mode (None: all of them, ``full``).

    Never exec_command / write_stdin / keyboard / mouse / tooluse / use_device / file_copy outside ``full``: they
    run on the device's builtin, outside any CLI sandbox. ``edits`` adds the file edit tools only in gateway mode
    (in direct mode the CLI's own, sandboxed tools edit this computer's files)."""
    if permission == "full":
        return None
    tools = list(READ_TOOLS)
    if permission in ("ask", "edits"):
        tools += ["mail_send", "ask_user"]
    if permission == "edits" and mode == "gateway":
        tools += list(EDIT_TOOLS)
    return tools


def claude_allowed(prefix: str, permission: str, mode: str = "gateway") -> list[str]:
    """``--allowedTools`` for the connector's tools in ``permission`` mode (full: everything is allowed anyway);
    always an explicit list, never the whole server."""
    tools = approved_tools(permission, mode)
    if not prefix or tools is None:
        return []
    return [prefix + tool for tool in tools]


def mcp_hint(nx: dict | None, v: dict) -> str:
    """The ``nexus_mcp`` fragment's text: the connector to use and the ready workspace (needs a handle)."""
    tail = (f"工作区已经为你选好：workspace_id=\"{v.get('handle')}\"，指向 {v.get('target_device')} 上的 "
            f"{v.get('target_workdir')}；调用它的设备工具、mail_*、ask_user、file_copy 时每次都带上这个 workspace_id，"
            f"不需要先调用 device_list / use_device。传 workdir 或路径时用 {v.get('target_workdir')} 下的绝对路径。"
            f"你的邮箱地址是 {v.get('address')}。")
    name = connector_name(nx)
    if not name:
        if nx and nx.get("state") not in ("unknown", None) and not can_attach(nx):
            return ""
        return ("如果你有 Agent Nexus 的 MCP 工具（含 device_list / use_device / mail_send），只用其中一个连接器，"
                "不要重复调用；" + tail)
    prefix = nx.get("tool_prefix") or ""
    if nx.get("acp_pending"):
        return (f"本次对话尝试接入 Agent Nexus MCP（连接器 {name}）；如果你的工具里没有它，说明这个 Agent 接不了，"
                "就不要调用这些工具。" + tail)
    lead = "本次对话临时接入了本机 Agent Nexus MCP" if nx.get("shadowed") else "本次对话已接入 Agent Nexus MCP"
    head = f"{lead}（连接器 {name}" + (f"，工具名前缀 {prefix}" if prefix else "") + "）。"
    if nx.get("shadowed"):
        shadowed = nx["shadowed"]
        head += (f"ChatGPT 账户连接器 {shadowed}（codex_apps 下的 {shadowed}.* 工具）指向同一个网关，但它的调用需要审批，"
                 f"在这里会被拒绝（它的工具列表也可能过时）：只用 {name} 的工具，不要调用 {shadowed}.*。")
    elif nx.get("also"):
        head += f"另有 {'、'.join(nx['also'])} 指向同一个网关，只用 {name}，不要重复调用。"
    ours = bool(nx.get("attach")) or nx.get("state") == "local" or \
        (nx.get("state") == "both" and not nx.get("swapped"))
    if ours and nx.get("local_state") not in (None, "ready"):
        head += f"注意：本机 MCP 现在不可用（{nx.get('local_reason')}），恢复前这些工具只会返回状态。"
    return head + tail


def shadow_hint(nx: dict | None) -> str:
    """Without a handle (no gateway): only the warning that a Codex account connector's tools are refused here."""
    if not nx or not nx.get("shadowed"):
        return ""
    shadowed = nx["shadowed"]
    return (f"ChatGPT 账户连接器 {shadowed}（codex_apps 下的 {shadowed}.* 工具）的调用需要审批，在这里会被拒绝，"
            f"不要调用 {shadowed}.*；需要 Agent Nexus 工具时用连接器 {connector_name(nx) or MCP_NAME}。")


MODELS_TTL_S = 600.0
#: a failed or empty ``model/list`` is not asked again for this long; ``fresh`` re-asks at most this often
MODELS_RETRY_S = 60.0


def _cli_default_model(kind: str, home: Path, env: dict) -> str | None:
    """The model a CLI uses by its own settings (codex ``config.toml`` ``model``, Claude ``settings.json``
    ``model``): non-credential files, read for the menu's "默认模型（X）"."""
    try:
        if kind == "codex":
            import tomllib
            base = Path(env.get("CODEX_HOME") or Path(home) / ".codex")
            value = tomllib.loads((base / "config.toml").read_text(encoding="utf-8-sig")).get("model")
        elif kind == "claude":
            import json
            value = json.loads((Path(home) / ".claude" / "settings.json").read_text(encoding="utf-8-sig")).get("model")
        else:
            return None
    except (OSError, ValueError, AttributeError):
        return None
    return value if registry.model_ok(value) else None


def _error(message: str, code: str, **extra: Any) -> dict:
    return {"error": message, "code": code, **extra}

def _host_name() -> str:
    """This machine's name; on Android the device's own name (``gethostname`` is ``localhost`` there)."""
    return device_name() or socket.gethostname()


class AgentHub:
    """``root``: the anx root (``.anx/agents/*`` lives there). ``account``: () -> the run-as account or None.
    ``gateway``: async (op, args) -> dict, a ``hub_call`` to this device's gateway, or None; it returns None
    (or raises) when the gateway is unreachable or too old. ``device``: () -> ``{id, name, gateway}`` of this
    device. ``workspaces``: () -> ``[{name, path}]`` (default: :func:`configured_workspaces`)."""

    def __init__(self, root: Path, *, account: Callable[[], Any] | None = None,
                 gateway: Callable[[str, dict], Awaitable[dict | None]] | None = None,
                 device: Callable[[], dict] | None = None, workspaces: Callable[[], list[dict]] | None = None,
                 config: Callable[[], dict] | None = None, runs: Runs | None = None, mcp_link: Any = None):
        self.root = Path(root)
        self.account = account or (lambda: None)
        self.gateway = gateway
        self.device = device or (lambda: {"id": "", "name": _host_name(), "gateway": ""})
        self.workspaces = workspaces or (lambda: configured_workspaces(self.root))
        #: the 本机 MCP's link (agents/mcp_link.MCPLink): which Agent Nexus connector each run uses; None: none
        self.mcp_link = mcp_link
        self._config = config or (lambda: registry.load_config(self.root))
        self.meta = sessions.SessionMeta(self.root)
        self.recent = Recent(self.root)
        self.prompts = Prompts(self.root, context_defaults=self._prompt_defaults)
        self.installs = Installs(self.definitions, on_done=lambda kind, _job: registry.invalidate(kind))
        self.runs = runs or Runs(self.definitions, account=self.account, roots=self._roots,
                                 tmp_root=self.root / ".anx" / "agents" / "tmp")
        self.runs.on_session = self._on_session
        self.runs.on_close = self._on_close
        self.tmp = TmpDirs(self.root, active=lambda: set(self.runs.runs))
        #: API providers as agents (§16.4): kinds ``api:<provider>/<model>``, in-process runs
        self.api = api_service.ApiAgents(self.root, config=self._config, gateway=self._gateway_call,
                                         env=lambda: self._env(self._account()))
        self._sweeper: asyncio.Task | None = None
        self._last_sweep = 0.0
        self._releases: set[asyncio.Task] = set()
        #: (account, kind) -> (asked at, payload, ok): ok=False is a short negative entry (MODELS_RETRY_S)
        self._models_cache: dict[tuple[str, str], tuple[float, dict, bool]] = {}
        self._models_locks: dict[tuple[str, str], asyncio.Lock] = {}

    # -- context -------------------------------------------------------------------------------------------
    def definitions(self) -> dict[str, registry.AgentDef]:
        """The CLIs and the enabled API models (``api:<provider>/<model>``)."""
        return {**self.cli_definitions(), **self.api.defs()}

    def cli_definitions(self) -> dict[str, registry.AgentDef]:
        try:
            return registry.definitions(self._config())
        except Exception:
            log.exception("agents: reading agents.custom failed")
            return registry.definitions()

    def _account(self) -> Any:
        try:
            return self.account()
        except Exception:
            log.exception("agents: resolving the run-as account failed")
            return None

    def _env(self, account: Any = None) -> dict:
        if account is not None:
            return account.env()
        from .. import agent_cli
        return agent_cli.user_env()

    @staticmethod
    def _popen(account: Any) -> dict | None:
        return account.popen_args() if account is not None else None

    def _home(self, account: Any = None, env: dict | None = None) -> Path:
        if account is not None and getattr(account, "home", None):
            return Path(account.home)
        env = env if env is not None else self._env(None)
        home = env.get("USERPROFILE") if os.name == "nt" else None
        return Path(home or env.get("HOME") or Path.home())

    def _workspaces(self) -> list[dict]:
        try:
            return [w for w in (self.workspaces() or []) if isinstance(w, dict) and w.get("path")]
        except Exception:  # noqa: BLE001
            return []

    def _root_entries(self, *, drives: bool | None = None) -> list[dict]:
        """Where a work folder may be picked and run: the run-as account's HOME, the registered workspaces and, on
        Windows, every drive root (what the device's file share exposes anyway)."""
        account = self._account()
        return workdirs.default_roots(str(self._home(account)), self._workspaces(), drives=drives)

    def _roots(self) -> list[str]:
        return [root["path"] for root in self._root_entries()]

    def _state_dirs(self) -> list[str]:
        """Agent Nexus' own state (keys, device credentials): never a work folder."""
        return [str(self.root / ".anx"), str(self.root / ".mcp-manager")]

    async def _workdir_ok(self, workdir: str, kind: str, resume_id: str = "") -> dict | None:
        """None when ``workdir`` may be used, else the error. Allowed: inside the roots, or exactly a folder this
        device already ran a CLI in (a recent folder, the recorded folder of the session being resumed or of another
        discovered session of this CLI) -- the CLI ran there already, so it adds no reach. Never Agent Nexus' own
        state folders, nor what :func:`workdirs.cwd_refused` refuses (a drive or file system root, a system folder,
        a folder that contains Agent Nexus' root other than HOME): those also defeat the exceptions, whose folders
        come from transcripts anything writing HOME can forge. The roots (drive enumeration, realpath) are worked
        out in a thread: a stale network drive must not stall the event loop."""
        for denied in self._state_dirs():
            if workdirs.allowed(workdir, [denied]):
                return _error(f"不能把 Agent Nexus 自己的数据目录当作工作目录：{workdir}", "workdir_not_allowed")
        account = self._account()
        env = self._env(account)
        home = self._home(account, env)
        why = await asyncio.to_thread(workdirs.cwd_refused, workdir, [self.root, home], [home])
        if why:
            return _error(f"不能把{why}当作工作目录：{workdir}（请选它下面的具体项目目录）", "workdir_not_allowed")
        roots = await asyncio.to_thread(self._roots)
        if await asyncio.to_thread(workdirs.allowed, workdir, roots):
            return None
        try:
            recent = [item["path"] for item in await asyncio.to_thread(self.recent.list, limit=50, existing=True)]
        except (OSError, ValueError):
            recent = []
        if workdirs.allowed(workdir, (), recent):
            return None
        defs = {k: d for k, d in self.cli_definitions().items() if k == kind}
        if defs:
            try:
                if resume_id:
                    found = await asyncio.to_thread(sessions.find, defs, home, kind, resume_id, env=env)
                    if found and found.get("cwd") and workdirs.allowed(workdir, (), [found["cwd"]]):
                        return None
                listed = await asyncio.to_thread(sessions.discover, defs, home, kind=kind, limit=200, env=env)
                if workdirs.allowed(workdir, (), [s.get("cwd") for s in listed if s.get("cwd")]):
                    return None
            except Exception:  # noqa: BLE001 - an unreadable transcript only means no exception
                log.debug("agents: listing sessions for the workdir check failed", exc_info=True)
        where = "执行账户的主目录、已登记的工作区" + ("、各磁盘里的目录" if workdirs.WINDOWS else "")
        return _error(f"工作目录不在允许的范围内：{workdir}（可选：{where}，以及本机 CLI 用过的目录；"
                      "其他位置请先登记为工作区）", "workdir_not_allowed")

    def _device(self) -> dict:
        try:
            info = dict(self.device() or {})
        except Exception:  # noqa: BLE001
            info = {}
        info.setdefault("name", _host_name())
        info.setdefault("id", "")
        info.setdefault("gateway", "")
        return info

    def _prompt_defaults(self) -> dict:
        info = self._device()
        return {"device": info.get("name") or "", "device_id": info.get("id") or "", "gateway": info.get("gateway") or "",
                "owner": info.get("owner") or ""}

    async def _gateway_call(self, op: str, args: dict) -> dict | None:
        """``hub_call`` to the gateway; None when there is none (not joined, offline, too old) or it failed."""
        if self.gateway is None:
            return None
        try:
            result = await self.gateway(op, args)
        except Exception as exc:  # noqa: BLE001 - no gateway is a normal state for 本机
            log.info("agents: gateway %s unavailable (%s)", op, exc)
            return None
        return result if isinstance(result, dict) else None

    # -- hooks from the runs -----------------------------------------------------------------------------------
    def _on_session(self, run) -> None:
        session_id = run.session_id
        if not session_id:
            return
        try:
            fresh = not getattr(run.opts, "resume_id", None)  # a resumed session keeps the CLI's own title
            self.meta.set(run.kind, session_id, managed=True, rid=run.rid, address=run.address or None,
                          context=self._meta_context(run.context), model=getattr(run.opts, "model", "") or None,
                          **({"first_prompt": run.last_prompt} if fresh and run.last_prompt else {}))
        except (OSError, ValueError):
            log.warning("agents: could not record session %s", session_id)
        try:
            self.tmp.update(run.rid, session=session_id)
        except (OSError, ValueError):
            pass

    @staticmethod
    def _meta_context(context: dict | None) -> dict | None:
        """What the session metadata keeps of a run's context: the gateway target, or None (direct: cleared)."""
        context = context or {}
        if context.get("mode") != "gateway":
            return None
        return {"device": context.get("context_device_id") or "", "workdir": context.get("context_workdir_raw") or ""}

    def _on_close(self, run) -> None:
        try:
            if not run.own_tmpdir:
                self.tmp.close(run.rid)
        except (OSError, ValueError):
            pass
        handle = (run.context or {}).get("handle")
        if handle:
            self._release_soon(handle)
        if run.session_id:
            try:  # the model the run ended on: switches made during the run (agent_run_send) included
                self.meta.set(run.kind, run.session_id, rid=None, model=getattr(run.opts, "model", "") or None)
            except (OSError, ValueError):
                pass

    def _busy(self, kind: str, session_id: str) -> bool:
        return self._live_run(kind, session_id) is not None

    def _live_run(self, kind: str, session_id: str):
        return next((run for run in self.runs.runs.values()
                     if not run.closed and run.kind == kind and session_id and run.session_id == session_id), None)

    async def _release(self, handle: str) -> None:
        """Tell the gateway the run's handle is done (its address is free again); best effort."""
        await self._gateway_call("agent_release", {"workspace_id": handle})

    def _release_soon(self, handle: str) -> None:
        try:
            task = asyncio.get_running_loop().create_task(self._release(handle))
        except RuntimeError:  # no loop (shutdown from a thread): the handle simply expires
            return
        self._releases.add(task)
        task.add_done_callback(self._releases.discard)

    # -- housekeeping ----------------------------------------------------------------------------------------
    def start(self) -> None:
        """Sweep old temp folders now and every few hours (call from a running event loop)."""
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.ensure_future(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.tmp.sweep)
                self._last_sweep = time.time()
            except Exception:
                log.exception("agents: temp folder sweep failed")
            await asyncio.sleep(SWEEP_EVERY_S)

    def close(self) -> None:
        self.runs.close_all()
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None

    def _connector(self, kind: str, account: Any, env: dict, api: bool = False, workdir: str | None = None,
                   d: Any = None) -> dict | None:
        """The Agent Nexus connector ``kind`` uses (mcp_link.for_run), or None without a link."""
        if self.mcp_link is None:
            return None
        try:
            return self.mcp_link.for_run(kind, account, env, api=api, workdir=workdir, d=d)
        except Exception:  # noqa: BLE001 - never break the chat over this
            log.exception("agents: resolving the MCP connector of %s failed", kind)
            return None

    def _observer(self, kind: str, account: Any):
        """A run's report of the Agent Nexus servers its CLI really loaded (passive detection, mcp_probe)."""
        link = self.mcp_link
        if link is None:
            return None
        name = link.account_name(account)

        def observe(connectors: list[dict], version: str | None = None) -> None:
            try:
                link.probe.observe(name, kind, connectors, version=version)
            except Exception:  # noqa: BLE001
                log.debug("agents: recording the MCP observation failed", exc_info=True)
        return observe

    # -- CLIs ------------------------------------------------------------------------------------------------
    async def agent_info(self, params: dict) -> dict:
        await self.api.pull(force=bool(params.get("fresh")))
        defs = self.cli_definitions()
        api_defs = self.api.defs()
        account = self._account()
        env = self._env(account)
        kind = params.get("kind")
        if isinstance(kind, str) and kind:
            if kind not in defs and kind not in api_defs:
                return _error(f"不认识的 Agent：{kind}", "unknown_kind")
            defs = {kind: defs[kind]} if kind in defs else {}
            api_defs = {kind: api_defs[kind]} if kind in api_defs else {}
        fresh = bool(params.get("fresh"))
        probed = await asyncio.to_thread(registry.probe_all, defs, env, fresh=fresh, run_as=self._popen(account))
        agents = [registry.info(d, probed[k], self.installs.current(k)) for k, d in defs.items()]
        api_agents = await asyncio.to_thread(self.api.info, set(api_defs))
        device = self._device()
        home = self._home(account, env)
        undecided = False
        for item in agents:
            if self.mcp_link is not None and item.get("version") and hasattr(self.mcp_link, "note_version"):
                self.mcp_link.note_version(account, item["kind"], item["version"])
            nx = await asyncio.to_thread(self._connector, item["kind"], account, env, False, None,
                                         defs.get(item["kind"])) if item.get("installed") else None
            if nx is None:
                try:
                    item["gateway_mcp"] = registry.gateway_mcp(item["kind"], home, env, str(device.get("gateway") or ""))
                except Exception:  # noqa: BLE001 - a broken config file must not break agent_info
                    item["gateway_mcp"] = None
                continue
            item["nexus_mcp"] = nx
            # kept for one release (an older chat.js served by a gateway): false only when nothing can attach
            item["gateway_mcp"] = can_attach(nx)
            undecided = undecided or (nx["state"] in ("none", "unknown") and not nx.get("entry"))
        if undecided and self.mcp_link is not None:
            self.mcp_link.soon("a CLI without a connector")
        for item in api_agents:
            if self.mcp_link is not None:
                nx = await asyncio.to_thread(self._connector, item["kind"], account, env, True)
                if nx is not None:
                    item["nexus_mcp"] = nx
        agents += api_agents
        return {"agents": agents, "device": {"id": device.get("id"), "name": device.get("name"),
                                             "os": prompts_mod.os_name()},
                "account": getattr(account, "name", None), "home": str(self._home(account, env))}

    async def agent_install(self, params: dict) -> dict:
        kind = str(params.get("kind") or "")
        method = str(params.get("method") or "auto")
        account = self._account()
        env = self._env(account)
        try:
            job_id = await asyncio.to_thread(self.installs.start, kind, env, self._popen(account), method=method)
        except InstallError as exc:
            return exc.view()
        job = self.installs.get(job_id, 0) or {}
        return {"job_id": job_id, "kind": kind, "state": job.get("state", "queued"), "command": job.get("command"),
                "method": job.get("method")}

    async def agent_install_job(self, params: dict) -> dict:
        job_id = str(params.get("job_id") or "")
        if params.get("cancel") and not self.installs.cancel(job_id):
            return _error("这个安装任务已经结束或不存在", "not_found")
        try:
            since = max(0, int(params.get("since") or 0))
        except (TypeError, ValueError):
            since = 0
        job = self.installs.get(job_id, since)
        if job is None:
            return _error("找不到这个安装任务（可能已经过期），请重新检测", "not_found")
        job["lines"] = job.get("log", [])
        job["stage"] = job.get("phase_label", "")
        return job

    # -- sessions --------------------------------------------------------------------------------------------
    async def agent_sessions(self, params: dict) -> dict:
        defs = self.definitions()
        account = self._account()
        env = self._env(account)
        home = self._home(account, env)
        kind = params.get("kind") if isinstance(params.get("kind"), str) and params.get("kind") else None
        try:
            limit = int(params.get("limit") or 200)
        except (TypeError, ValueError):
            limit = 200
        known = [w["path"] for w in self._workspaces()] + [r["path"] for r in self.recent.list(limit=50)]
        query = params.get("query") if isinstance(params.get("query"), str) else None
        cli_defs = {k: d for k, d in defs.items() if not api_config.is_api_kind(k)}
        found = [] if kind and (kind == "api" or api_config.is_api_kind(kind)) else await asyncio.to_thread(
            sessions.discover, cli_defs, home, kind=kind, limit=limit, env=env, meta=self.meta,
            include_hidden=bool(params.get("include_hidden")), include_blank=bool(params.get("include_blank")),
            query=query, known_dirs=known)
        found += await asyncio.to_thread(
            self.api.sessions, kind, meta=self.meta, include_hidden=bool(params.get("include_hidden")),
            include_blank=bool(params.get("include_blank")), query=query, limit=limit)
        if not kind:
            found.sort(key=lambda s: (not s["pinned"], -(s["updated_at"] or 0)))
        running = {(run.kind, run.session_id): run for run in self.runs.runs.values() if run.session_id}
        cwd_ok: dict[str, bool] = {}
        items = []
        for session in found:
            item = sessions.public(session)
            cwd = item.get("cwd")
            if isinstance(cwd, str) and cwd:
                if cwd not in cwd_ok:
                    cwd_ok[cwd] = await asyncio.to_thread(os.path.isdir, cwd)
                if not cwd_ok[cwd]:
                    item["cwd_missing"] = True  # a deleted project (or another machine's path): never a default workdir
            run = running.get((session["kind"], session["id"]))
            if run is not None:
                item.update(rid=run.rid, running=True, run_state=run.state)
            items.append(item)
        groups = [{k: v for k, v in group.items() if k != "sessions"} for group in sessions.grouped(items, defs)]
        return {"sessions": items, "groups": groups, "home": str(home)}

    async def agent_session_read(self, params: dict) -> dict:
        kind, session_id = params.get("kind"), params.get("id") or params.get("session_id")
        if not isinstance(kind, str) or not kind or not isinstance(session_id, str) or not session_id:
            return _error("缺少会话的 kind 或 id", "bad_request")
        defs = self.definitions()
        account = self._account()
        env = self._env(account)
        try:
            max_events = max(10, min(int(params.get("max_events") or 2000), 5000))
        except (TypeError, ValueError):
            max_events = 2000
        try:
            if api_config.is_api_kind(kind):
                events = await asyncio.to_thread(self.api.store.read_events, session_id, max_events=max_events)
            else:
                events = await asyncio.to_thread(sessions.read, defs, self._home(account, env), kind, session_id,
                                                 max_events=max_events, env=env)
        except LookupError:
            return _error("找不到这个会话，它可能已经被删除", "not_found")
        entry = self.meta.get(kind, session_id)
        result: dict[str, Any] = {"kind": kind, "session_id": session_id, "events": events}
        if entry.get("address"):
            result["address"] = entry["address"]
        if entry.get("model"):
            result["model"] = entry["model"]  # the model the console last asked for (§16.7)
        run = next((r for r in self.runs.runs.values() if r.kind == kind and r.session_id == session_id), None)
        if run is not None:
            result["rid"] = run.rid
        return result

    async def agent_session_meta(self, params: dict) -> dict:
        if api_config.is_api_kind(params.get("kind")) and params.get("delete_file"):
            session_id = str(params.get("id") or "")
            if self._busy(str(params["kind"]), session_id):
                return _error("这个会话正在运行，先停止再删除", "busy")
            try:
                deleted = await asyncio.to_thread(self.api.store.delete, session_id)
            except LookupError:
                deleted = False
            if not deleted:
                return _error("找不到这个会话的文件，可能已经被删除", "not_found")
            self.meta.forget(str(params["kind"]), session_id)
            return {"ok": True, "deleted": 1}
        account = self._account()
        env = self._env(account)
        return await asyncio.to_thread(sessions.meta_op, self.definitions(), self._home(account, env), self.meta,
                                       params, env=env, busy=self._busy)

    # -- runs ------------------------------------------------------------------------------------------------
    async def _bind(self, kind: str, workdir: str, mode: str, context: dict, name: str | None) -> dict | None:
        """The run's workspace handle and mailbox address from the gateway (§1, §10), or None without one."""
        device = self._device()
        if mode == "gateway":
            target, target_dir = context.get("device"), context.get("workdir")
        else:
            target, target_dir = device.get("id"), workdir
        if not target:
            return None
        args = {"device_id": target, "workdir": target_dir or "", "kind": kind}
        if name:
            args["name"] = name
        bound = await self._gateway_call("agent_handle", args)
        if bound is None or "error" in bound or not bound.get("workspace_id"):
            if name and bound is not None:  # the name may be taken by a live session: an automatic one instead
                args.pop("name", None)
                bound = await self._gateway_call("agent_handle", args)
            if bound is None or "error" in bound or not bound.get("workspace_id"):
                return bound if isinstance(bound, dict) and "error" in bound else None
        return bound

    async def agent_run_open(self, params: dict) -> dict:
        defs = self.definitions()
        kind = str(params.get("kind") or "")
        d = defs.get(kind)
        if d is None:
            return _error(f"不认识的 Agent：{kind or '（未选择）'}", "unknown_kind")
        context = params.get("context") if isinstance(params.get("context"), dict) else {}
        mode = "gateway" if context.get("mode") == "gateway" else "direct"
        if mode == "gateway" and not (context.get("device") and context.get("workdir")):
            return _error("经网关操作其他设备时，要选好目标设备和目录", "bad_context")
        account = self._account()
        workdir_raw = params.get("workdir")
        workdir = os.path.abspath(os.path.expanduser(workdir_raw.strip())) if isinstance(workdir_raw, str) and \
            workdir_raw.strip() else str(self._home(account))
        if not os.path.isdir(workdir):
            return _error(f"工作目录不存在：{workdir}", "bad_workdir")
        resume_id = str(params.get("resume_id") or "").strip()

        # A session that already runs here (another tab, the cloud and the local console at once): never a
        # second CLI process on one transcript -- hand that run over, with this message when it can take one.
        live = self._live_run(kind, resume_id) if resume_id else None
        if live is not None:
            return await self._attach(live, params)

        # The checks runs.open makes anyway, before the gateway hands out a handle and an address for nothing.
        if resume_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", resume_id):
            return _error("会话 ID 格式不对", "bad_resume_id")
        if resume_id and not d.resume:
            return _error(f"{d.label} 不支持续接已有会话，发消息会开一个新对话", "resume_unsupported")
        model = params.get("model")
        if model is not None and not (isinstance(model, str) and (not model.strip() or registry.model_ok(model.strip()))):
            return _error(registry.MODEL_ERROR, "bad_model")
        refused = await self._workdir_ok(workdir, kind, resume_id)
        if refused is not None:
            return refused
        run_env = self.runs._env(account, None)
        api_spec: dict | None = None
        if d.adapter == "api":  # an API model (§16.4): no binary; its provider, model and key instead
            try:
                api_spec = self.api.run_spec(kind, run_env)
            except LookupError as exc:
                return _error(str(exc), "unknown_kind")
            except PermissionError as exc:
                return _error(str(exc), getattr(exc, "code", "no_key"))
            if resume_id and not self.api.store.exists(resume_id) and self.meta.get(kind, resume_id) == {}:
                return _error("找不到这个会话，它可能已经被删除", "not_found")
            binary = "api"
        else:
            binary = await asyncio.to_thread(registry.locate, d, run_env)
        if not binary:
            return _error(f"这台设备上没有找到 {d.label}，可以先在 Agent 菜单里安装", "not_installed")
        rid = uuid.uuid4().hex[:12]
        try:
            tmpdir = await asyncio.to_thread(self.tmp.create, rid, account, kind=kind, session=resume_id or None)
        except (OSError, ValueError) as exc:
            return _error(f"无法创建临时目录：{exc}", "tmpdir_failed")

        # The mailbox address: a resumed session keeps the one it had (§10).
        previous = self.meta.get(kind, resume_id).get("address") if resume_id else None
        name = params.get("name") if isinstance(params.get("name"), str) and params.get("name").strip() else None
        if name is None and previous and "@" in previous:
            name = previous.rsplit("@", 1)[0]
        bound = await self._bind(kind, workdir, mode, context, name)
        events: list[dict] = []
        handle = address = ""
        if bound is not None and "error" not in bound:
            handle = bound.get("workspace_id") or ""
            address = bound.get("address_display") or bound.get("address") or ""
        elif mode == "gateway":
            await asyncio.to_thread(self.tmp.clean, rid)
            reason = (bound or {}).get("error") or "网关不可达"
            return _error(f"经网关操作其他设备需要网关签发工作区：{reason}", "no_gateway")
        else:
            reason = (bound or {}).get("error")
            events.append({"type": "status", "state": "starting", "note": NO_GATEWAY_NOTE if not reason
                           else f"网关没有为这个对话分配邮箱（{reason}）：ask_user / 邮件暂不可用", "gateway": False})

        device = self._device()
        nx = await asyncio.to_thread(self._connector, kind, account, run_env, api_spec is not None, workdir, d)
        session_name = address.split("@", 1)[0] if address else (params.get("title") or f"{kind}-{rid[:6]}")
        variables = {
            "kind": kind, "mode": mode, "device": device.get("name") or "", "device_id": device.get("id") or "",
            "workdir": workdir, "tmpdir": str(tmpdir), "session": session_name, "address": address,
            "gateway": device.get("gateway") or "", "handle": handle, "owner": device.get("owner") or "",
        }
        if mode == "gateway":
            variables["context_device"] = (bound or {}).get("device_display") or (bound or {}).get("display_name") \
                or str(context.get("device"))
            variables["context_workdir"] = (bound or {}).get("path") or str(context.get("workdir"))
        variables["target_device"] = variables.get("context_device") or variables["device"]
        variables["target_workdir"] = variables.get("context_workdir") or workdir
        connector = connector_name(nx)
        variables.update(mcp=connector or "", mcp_prefix=(nx or {}).get("tool_prefix") or "" if connector else "",
                         mcp_state=(nx or {}).get("state") or "")
        variables["mcp_hint"] = mcp_hint(nx, variables) if handle else shadow_hint(nx)
        draft = params.get("draft") if isinstance(params.get("draft"), dict) else None
        if draft is None and isinstance(params.get("fragments"), list):
            draft = {"fragments": params["fragments"], "presets": self.prompts.load()["presets"]}
        preset = params.get("preset") if isinstance(params.get("preset"), str) else None
        try:
            rendered = self.prompts.render(preset, variables, draft=draft)
            system_prompt = rendered["text"]
        except Exception as exc:  # noqa: BLE001 - a bad fragment never blocks the chat
            log.warning("agents: rendering the system prompt failed: %s", exc)
            system_prompt = ""
            events.append({"type": "error", "level": "warn", "text": f"系统提示词没能生成（{exc}），这次不带提示词"})
        if params.get("system_prompt") is False:
            system_prompt = ""

        console = {key: params[key] for key in ("kind", "workdir", "prompt", "resume_id", "permission", "title",
                                                "model") if key in params}
        console["workdir"] = workdir
        if api_spec is not None and handle:
            # ask_user / mail_send of an API model (§16.4): through the gateway, as this run's mailbox address
            api_spec.update(handle=handle, gateway=self._gateway_call, mode=mode)
            if nx is not None and nx.get("ready"):
                api_spec.update(mcp_url=nx["url"])  # the 本机 MCP's tools, the handle filled in
        extra: dict = {"api": api_spec} if api_spec is not None else {}
        permission = str(params.get("permission") or "ask")
        if nx is not None and api_spec is None:
            use = nx.get("use")
            if nx.get("local_state") not in ("off", "exposed"):
                extra["mcp_url"] = nx["url"]  # {mcp_url} of custom CLIs
            if nx.get("attach") == "flags" and use:
                extra["mcp_attach"] = {"name": use, "url": nx["url"], "gateway": mode == "gateway"}
            run_only = nx.get("attach") == "flags" and use == MCP_NAME
            if use and kind == "codex" and (run_only or (connector_name(nx) and nx.get("source") != "account:chatgpt"
                                                         and nx["state"] != "gateway_account")):
                # codex exec never asks: the tools this permission mode allows are approved, every other tool of
                # the connector is left out of the run (enabled_tools), never approved
                extra["mcp_approve"] = [use]
                tools_ok = approved_tools(permission, mode)
                if tools_ok is not None:
                    extra["mcp_enabled_tools"] = tools_ok
            if use and kind == "claude" and connector_name(nx):
                allowed = claude_allowed(nx.get("tool_prefix") or "", permission, mode)
                if allowed:
                    extra["mcp_allow"] = allowed
            if kind == "codex" and nx.get("state") == "gateway_account" and nx.get("attach") != "flags":
                # an account app's tools need an approval codex exec never gives, and the 本机 MCP cannot stand in
                events.append({"type": "error", "level": "warn",
                               "text": f"Codex 的 ChatGPT 账户连接器 {use or ''} 的工具需要审批，codex exec 无法审批；"
                                       f"本机 MCP 当前不可用（{nx.get('local_reason') or nx.get('local_state') or '未知'}），"
                                       "本次对话调用不了 Agent Nexus 工具"})
            observe = self._observer(kind, account)
            if observe is not None:
                extra["on_mcp"] = observe
            if mode == "gateway" and can_attach(nx) is False:
                events.append({"type": "error", "level": "warn",
                               "text": f"{d.label} 无法接入 Agent Nexus MCP（{nx.get('local_reason') or '本机 MCP 不可用'}），"
                                       "经网关模式下它调用不了 use_device / 文件工具"})
        elif nx is None and mode == "gateway" and api_spec is None:
            try:
                names = await asyncio.to_thread(registry.gateway_mcp_names, kind, self._home(account, run_env),
                                                run_env, str(device.get("gateway") or ""))
            except Exception:  # noqa: BLE001 - a broken CLI config must not block the chat
                names = None
            if names == []:
                events.append({"type": "error", "level": "warn",
                               "text": f"{d.label} 没有配置 Agent Nexus MCP 连接器，经网关模式下它调用不了 use_device / "
                                       "文件工具；先在 CLI 里加上网关的 MCP 地址"})
            elif names and kind == "codex" and permission != "readonly":
                extra["mcp_approve"] = names
        try:
            result = await self.runs.open(console, binary=binary, system_prompt=system_prompt, tmpdir=str(tmpdir),
                                          address=address, rid=rid, events=events, extra=extra, workdir_ok=True)
        except RunError as exc:
            await asyncio.to_thread(self.tmp.clean, rid)
            if handle:
                await self._release(handle)
            return _error(str(exc), exc.code)
        run = self.runs.runs.get(rid)
        if run is None and handle:  # closed already (it failed at once): its on_close had no handle to release
            self._release_soon(handle)
        if run is not None:
            run.context = {"mode": mode, "handle": handle, "preset": preset or "default",
                           "context_device": variables.get("context_device"),
                           "context_workdir": variables.get("context_workdir"),
                           # as the console sent them (a device id, the folder typed / picked): stored with the
                           # session and listed with the run, so reopening it restores the same context
                           "context_device_id": str(context.get("device") or "") if mode == "gateway" else "",
                           "context_workdir_raw": str(context.get("workdir") or "") if mode == "gateway" else "",
                           "connector": {"state": (nx or {}).get("state"), "use": connector_name(nx) or None,
                                         "attach": (nx or {}).get("attach")}}
        try:
            if mode == "direct":  # gateway mode: the folder is the other device's; the runner's cwd is only a base
                await asyncio.to_thread(self.recent.add, workdir, kind=kind)
        except (OSError, ValueError):
            pass
        if resume_id and address:
            try:
                self.meta.set(kind, resume_id, managed=True, rid=rid, address=address)
            except (OSError, ValueError):
                pass
        if resume_id and run is not None:  # the model this open asked for; None: the CLI's default (§16.7)
            try:
                self.meta.set(kind, resume_id, model=result.get("model") or None)
            except (OSError, ValueError, TypeError):
                pass
        # the context the session now runs in (a gateway target, or cleared for direct): a resumed one at once, a new
        # one whose id arrived before run.context was set here too (else _on_session records it)
        session_id = resume_id or (getattr(run, "session_id", None) if run is not None else None)
        if session_id and run is not None:
            try:
                self.meta.set(kind, session_id, context=self._meta_context(run.context))
            except (OSError, ValueError):
                pass
        result.update(mode=mode, handle=handle or None, gateway=bool(handle),
                      system_prompt_chars=len(system_prompt))
        if not handle:
            result["note"] = NO_GATEWAY_NOTE
        return result

    async def _attach(self, run, params: dict) -> dict:
        """``agent_run_open`` for a session that already has a run: that run, and the message sent to it."""
        prompt = params.get("prompt") if isinstance(params.get("prompt"), str) else ""
        view = {"rid": run.rid, "attached": True, "session_id": run.session_id, "state": run.state,
                "address": run.address, "adapter": run.adapter.name, "tmpdir": run.tmpdir,
                "permission": run.permission, "mode": (run.context or {}).get("mode", "direct"),
                "handle": (run.context or {}).get("handle") or None,
                "note": "这个会话已经在另一个窗口运行，已切换到那个对话"}
        if prompt.strip():
            try:
                sent = {"rid": run.rid, "prompt": prompt}
                if params.get("model") is not None:
                    sent["model"] = params["model"]
                await self.runs.send(sent)
            except RunError as exc:
                if exc.code == "busy":
                    return _error("这个会话已经在另一个窗口运行，上一轮还没结束；已切换到那个对话，等它结束再发",
                                  "busy", rid=run.rid, attached=True)
                return _error(str(exc), exc.code, rid=run.rid)
            view["state"] = run.state
            view["sent"] = True
        return view

    def _run_handlers(self) -> dict:
        base = self.runs.handlers()

        async def read(params: dict) -> dict:
            params = dict(params) if isinstance(params, dict) else {}
            try:
                params["wait"] = max(0.0, min(float(params.get("wait") or 0), READ_WAIT_S))
            except (TypeError, ValueError):
                params["wait"] = 0.0
            return await base["agent_run_read"](params)

        async def listing(params: dict) -> dict:
            result = await base["agent_run_list"](params)
            for view in result.get("runs", []) if isinstance(result, dict) else []:
                run = self.runs.runs.get(view.get("rid"))
                if run is not None and run.context:
                    view["mode"] = run.context.get("mode")
                    view["context_device"] = run.context.get("context_device")
                    view["context_workdir"] = run.context.get("context_workdir")
                    if run.context.get("mode") == "gateway":
                        view["context"] = {"mode": "gateway", "device": run.context.get("context_device_id") or "",
                                           "workdir": run.context.get("context_workdir_raw") or ""}
            return result

        return {**base, "agent_run_read": read, "agent_run_list": listing}

    # -- models (§16.7) --------------------------------------------------------------------------------------
    async def agent_models(self, params: dict) -> dict:
        """``{kind, models: [{id, label, desc?, default?}], default, source: cli|cache|session|hints}``: what the
        model menu offers. Codex asks its app-server (``model/list``; cached), else reads its models cache; ACP
        agents tell at session start (a running run's list); the rest are the definition's hints."""
        kind = str(params.get("kind") or "")
        d = self.definitions().get(kind)
        if d is None:
            return _error(f"不认识的 Agent：{kind or '（未选择）'}", "unknown_kind")
        account = self._account()
        env = self._env(account)
        home = self._home(account, env)
        out: dict[str, Any] = {"kind": kind, "models": [dict(h) for h in d.model_hints], "source": "hints",
                               "default": await asyncio.to_thread(_cli_default_model, kind, home, env),
                               "flag": bool(d.model_args), "protocol": d.adapter == "acp"}
        if d.adapter == "api":
            return {**out, "models": [], "source": "api", "flag": False}
        if kind == "codex":
            key = (getattr(account, "name", None) or "", kind)
            # one app-server at a time per account (it also starts the user's stdio MCP servers): callers wait
            # for the one asking, and a failure is not retried for MODELS_RETRY_S
            lock = self._models_locks.setdefault(key, asyncio.Lock())
            async with lock:
                found = await self._codex_models(key, d, env, account, fresh=bool(params.get("fresh")))
            if found.get("models"):
                return {**out, "models": found["models"], "source": found.get("source") or "cli"}
            listed = await asyncio.to_thread(codex_models_cache, home, env)
            if listed:
                return {**out, "models": listed, "source": "cache"}
            if found.get("error"):
                out["note"] = found["error"]
            return out
        if d.adapter == "acp":
            for run in self.runs.runs.values():
                choices = getattr(run.adapter, "model_choices", None)
                if run.kind == kind and choices is not None:
                    listed = [{k: c[k] for k in ("id", "label", "desc") if c.get(k)} for c in choices()]
                    if listed:
                        return {**out, "models": listed, "source": "session"}
        return out

    async def _codex_models(self, key: tuple[str, str], d: registry.AgentDef, env: dict, account: Any, *,
                            fresh: bool) -> dict:
        """``model/list`` through the cache: a good answer for MODELS_TTL_S, a failed / empty one for MODELS_RETRY_S;
        ``fresh`` skips the cache only when the entry is older than MODELS_RETRY_S."""
        now = time.monotonic()
        cached = self._models_cache.get(key)
        if cached:
            age, payload, ok = now - cached[0], cached[1], cached[2]
            if age < MODELS_RETRY_S or (ok and age < MODELS_TTL_S and not fresh):
                return {**payload, "source": "cache" if not ok else payload.get("source", "cli")}
        binary = await asyncio.to_thread(registry.locate, d, env)
        found: dict = {}
        if binary:
            found = await asyncio.to_thread(codex_models, binary, env, self._popen(account))
        if found.get("models"):
            payload = {"models": found["models"], "source": "cli"}
            self._models_cache[key] = (time.monotonic(), payload, True)
            return payload
        payload = {"models": [], **({"error": found["error"]} if found.get("error") else {})}
        self._models_cache[key] = (time.monotonic(), payload, False)
        return payload

    # -- folders ---------------------------------------------------------------------------------------------
    async def agent_dirs(self, params: dict) -> dict:
        home = str(self._home(self._account()))
        # drives: on by default on Windows (an older console never asks for them); ``drives: false`` leaves them out
        drives = False if params.get("drives") is False else None
        path = params.get("path") if isinstance(params.get("path"), str) and params.get("path").strip() else None

        def listing() -> tuple[list[dict], dict]:  # drive enumeration and realpath: off the event loop
            entries = self._root_entries(drives=drives)
            return entries, workdirs.list_dirs(path, entries, hidden=bool(params.get("hidden")))

        roots, listed = await asyncio.to_thread(listing)
        if "error" in listed:
            return listed
        kind = params.get("kind") if isinstance(params.get("kind"), str) else None
        listed.update(home=home, workspaces=[r for r in roots if r["kind"] == "workspace"],
                      recent=await asyncio.to_thread(self.recent.list, kind=kind))
        return listed

    # -- the handler table -----------------------------------------------------------------------------------
    def handlers(self) -> dict[str, Callable[[dict], Awaitable[dict]]]:
        """The ``agents/1`` reverse RPCs (§13): exactly :data:`OPS`. None of them raises."""
        def guard(name: str, fn: Callable[[dict], Any]):
            async def handler(params: Any = None) -> dict:
                params = params if isinstance(params, dict) else {}
                try:
                    result = fn(params)
                    if asyncio.iscoroutine(result):
                        result = await result
                except RunError as exc:
                    return _error(str(exc), exc.code)
                except Exception as exc:
                    log.exception("agents: %s failed", name)
                    return _error(f"出错了：{exc}", "internal")
                return result if isinstance(result, dict) else {"result": result}
            handler.__name__ = name
            return handler

        def threaded(fn: Callable[[dict], dict]) -> Callable[[dict], Awaitable[dict]]:
            async def call(params: dict) -> dict:
                return await asyncio.to_thread(fn, params)
            return call

        prompt_ops = self.prompts.handlers()
        table: dict[str, Callable[[dict], Any]] = {
            "agent_info": self.agent_info, "agent_install": self.agent_install,
            "agent_install_job": self.agent_install_job,
            "agent_sessions": self.agent_sessions, "agent_session_read": self.agent_session_read,
            "agent_session_meta": self.agent_session_meta,
            **self._run_handlers(),
            "agent_run_open": self.agent_run_open,
            "agent_dirs": self.agent_dirs,
            "agent_prompts": threaded(prompt_ops["agent_prompts"]),
            "agent_prompt_save": threaded(prompt_ops["agent_prompt_save"]),
            "agent_prompt_preview": threaded(prompt_ops["agent_prompt_preview"]),
            "agent_tmp_list": threaded(self.tmp.op_list), "agent_tmp_clean": threaded(self.tmp.op_clean),
            "agent_models": self.agent_models,
            **self.api.handlers(),
        }
        assert set(table) == set(OPS), set(table) ^ set(OPS)
        return {name: guard(name, table[name]) for name in OPS}


FULL_REFUSED = "这台设备不允许远程以“完全信任”运行 Agent（agent-nexus.json 里 agents.allow_full_remote），请改用其他权限模式"


def remote_handlers(handlers: dict, *, allow_full: bool) -> dict:
    """The handlers other devices and the cloud console reach (feature ``agents/1``): the same table, except
    that ``permission: full`` is refused unless the device allows it (``agents.allow_full_remote``), and
    ``agent_info`` says so (``policy.full``) so the console can grey the mode out instead of failing later."""
    if allow_full:
        return dict(handlers)
    base_open, base_info = handlers["agent_run_open"], handlers["agent_info"]

    async def run_open(params: Any = None) -> dict:
        params = params if isinstance(params, dict) else {}
        if str(params.get("permission") or "") == "full":
            return _error(FULL_REFUSED, "permission_disabled")
        return await base_open(params)

    async def info(params: Any = None) -> dict:
        result = await base_info(params)
        if isinstance(result, dict) and "error" not in result:
            result = {**result, "policy": {**(result.get("policy") or {}), "full": False,
                                           "full_reason": "这台设备不允许远程使用完全信任"}}
        return result

    return {**handlers, "agent_run_open": run_open, "agent_info": info}


__all__ = [
    "CALL_TIMEOUT_S",
    "FEATURE",
    "FULL_REFUSED",
    "NO_GATEWAY_NOTE",
    "OPS",
    "READ_WAIT_S",
    "AgentHub",
    "call_timeout",
    "configured_workspaces",
    "remote_handlers",
]
