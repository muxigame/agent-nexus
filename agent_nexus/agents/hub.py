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

from . import prompts as prompts_mod
from . import registry, sessions, workdirs
from .installs import InstallError, Installs
from .prompts import Prompts
from .runner import RunError, Runs, _within
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
    "agent_tmp_list", "agent_tmp_clean",
)
READ_WAIT_S = 20.0
#: How long a forwarded call may take before the caller gives up waiting (it is never cancelled).
CALL_TIMEOUT_S = {"agent_info": 45.0, "agent_install": 45.0, "agent_sessions": 45.0, "agent_session_read": 45.0,
                  "agent_run_open": 60.0}
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


def _error(message: str, code: str, **extra: Any) -> dict:
    return {"error": message, "code": code, **extra}


class AgentHub:
    """``root``: the anx root (``.anx/agents/*`` lives there). ``account``: () -> the run-as account or None.
    ``gateway``: async (op, args) -> dict, a ``hub_call`` to this device's gateway, or None; it returns None
    (or raises) when the gateway is unreachable or too old. ``device``: () -> ``{id, name, gateway}`` of this
    device. ``workspaces``: () -> ``[{name, path}]`` (default: :func:`configured_workspaces`)."""

    def __init__(self, root: Path, *, account: Callable[[], Any] | None = None,
                 gateway: Callable[[str, dict], Awaitable[dict | None]] | None = None,
                 device: Callable[[], dict] | None = None, workspaces: Callable[[], list[dict]] | None = None,
                 config: Callable[[], dict] | None = None, runs: Runs | None = None):
        self.root = Path(root)
        self.account = account or (lambda: None)
        self.gateway = gateway
        self.device = device or (lambda: {"id": "", "name": socket.gethostname(), "gateway": ""})
        self.workspaces = workspaces or (lambda: configured_workspaces(self.root))
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
        self._sweeper: asyncio.Task | None = None
        self._last_sweep = 0.0
        self._releases: set[asyncio.Task] = set()

    # -- context -------------------------------------------------------------------------------------------
    def definitions(self) -> dict[str, registry.AgentDef]:
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

    def _roots(self) -> list[str]:
        account = self._account()
        roots = workdirs.default_roots(str(self._home(account)), self._workspaces())
        return [root["path"] for root in roots]

    def _device(self) -> dict:
        try:
            info = dict(self.device() or {})
        except Exception:  # noqa: BLE001
            info = {}
        info.setdefault("name", socket.gethostname())
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
                          **({"first_prompt": run.last_prompt} if fresh and run.last_prompt else {}))
        except (OSError, ValueError):
            log.warning("agents: could not record session %s", session_id)
        try:
            self.tmp.update(run.rid, session=session_id)
        except (OSError, ValueError):
            pass

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
            try:
                self.meta.set(run.kind, run.session_id, rid=None)
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

    # -- CLIs ------------------------------------------------------------------------------------------------
    async def agent_info(self, params: dict) -> dict:
        defs = self.definitions()
        account = self._account()
        env = self._env(account)
        kind = params.get("kind")
        if isinstance(kind, str) and kind:
            if kind not in defs:
                return _error(f"不认识的 Agent：{kind}", "unknown_kind")
            defs = {kind: defs[kind]}
        fresh = bool(params.get("fresh"))
        probed = await asyncio.to_thread(registry.probe_all, defs, env, fresh=fresh, run_as=self._popen(account))
        agents = [registry.info(d, probed[k], self.installs.current(k)) for k, d in defs.items()]
        device = self._device()
        home = self._home(account, env)
        for item in agents:
            try:
                item["gateway_mcp"] = registry.gateway_mcp(item["kind"], home, env, str(device.get("gateway") or ""))
            except Exception:  # noqa: BLE001 - a broken config file must not break agent_info
                item["gateway_mcp"] = None
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
        found = await asyncio.to_thread(
            sessions.discover, defs, home, kind=kind, limit=limit, env=env, meta=self.meta,
            include_hidden=bool(params.get("include_hidden")), include_blank=bool(params.get("include_blank")),
            query=params.get("query") if isinstance(params.get("query"), str) else None, known_dirs=known)
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
            events = await asyncio.to_thread(sessions.read, defs, self._home(account, env), kind, session_id,
                                             max_events=max_events, env=env)
        except LookupError:
            return _error("找不到这个会话，它可能已经被删除", "not_found")
        entry = self.meta.get(kind, session_id)
        result: dict[str, Any] = {"kind": kind, "session_id": session_id, "events": events}
        if entry.get("address"):
            result["address"] = entry["address"]
        run = next((r for r in self.runs.runs.values() if r.kind == kind and r.session_id == session_id), None)
        if run is not None:
            result["rid"] = run.rid
        return result

    async def agent_session_meta(self, params: dict) -> dict:
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
        if not _within(workdir, self._roots()):
            return _error(f"工作目录不在允许的范围内：{workdir}", "workdir_not_allowed")
        run_env = self.runs._env(account, None)
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

        console = {key: params[key] for key in ("kind", "workdir", "prompt", "resume_id", "permission", "title")
                   if key in params}
        console["workdir"] = workdir
        try:
            result = await self.runs.open(console, binary=binary, system_prompt=system_prompt, tmpdir=str(tmpdir),
                                          address=address, rid=rid, events=events)
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
                           "context_workdir": variables.get("context_workdir")}
        try:
            await asyncio.to_thread(self.recent.add, workdir, kind=kind)
        except (OSError, ValueError):
            pass
        if resume_id and address:
            try:
                self.meta.set(kind, resume_id, managed=True, rid=rid, address=address)
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
                await self.runs.send({"rid": run.rid, "prompt": prompt})
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
            return result

        return {**base, "agent_run_read": read, "agent_run_list": listing}

    # -- folders ---------------------------------------------------------------------------------------------
    async def agent_dirs(self, params: dict) -> dict:
        account = self._account()
        home = str(self._home(account))
        workspaces = self._workspaces()
        roots = workdirs.default_roots(home, workspaces, drives=bool(params.get("drives")))
        path = params.get("path") if isinstance(params.get("path"), str) and params.get("path").strip() else None
        listed = await asyncio.to_thread(workdirs.list_dirs, path, roots, hidden=bool(params.get("hidden")))
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
