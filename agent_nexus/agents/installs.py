"""Installing an agent CLI from the console (docs/agent-chat.md §2): a background job per install that runs the
OS's install command as the run-as user, keeps its output line by line, estimates progress and re-probes at the end.

``Installs.start(kind, env, popen_args)`` returns a job id at once, or raises :class:`InstallError` (a Chinese
message and a ``code``) when the install cannot start at all -- most often: no npm. ``get(job_id, since)`` is
polled by the console (``agent_install_job``); ``current(kind)`` is the ``install`` field of ``agent_info``.

Method ``auto`` (the default) picks npm when npm and a new enough Node.js are there, otherwise the CLI's official
installer when it has one (Claude Code, Codex) -- a person who just wants the CLI should not have to install
Node.js first. ``npm`` / ``official`` force one.

Progress (0-100) moves through phases 检查环境 → 下载 → 安装 → 校验版本: npm runs with ``--loglevel=http`` so every
package fetch is a line to count, percentages in an installer's output are used as they are, and inside a phase the
bar keeps creeping slowly so a quiet installer never looks stuck.
"""
from __future__ import annotations

import math
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from typing import Any

from . import registry
from .registry import AgentDef

WINDOWS = sys.platform == "win32"
NODEJS_URL = "https://nodejs.org"
#: (id, label, start, end) -- the share of the bar each phase covers.
PHASES = (("prepare", "检查环境", 0, 8), ("download", "下载", 8, 60), ("install", "安装", 60, 90),
          ("verify", "校验版本", 90, 100))
_PHASE = {phase[0]: phase for phase in PHASES}
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
_PERCENT = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s?%")


class InstallError(Exception):
    def __init__(self, message: str, code: str, **extra: Any):
        super().__init__(message)
        self.message = message
        self.code = code
        self.extra = extra

    def view(self) -> dict:
        return {"error": self.message, "code": self.code, **self.extra}


def node_major(text: str | None) -> int | None:
    match = re.search(r"v?(\d+)\.\d+", text or "")
    return int(match.group(1)) if match else None


def _writable_by(path: str, run_as: dict | None) -> bool:
    """Whether the run-as user (or this process) can write ``path`` -- checked from the mode bits when the
    install runs as another user, since ``os.access`` answers for this process."""
    try:
        if not run_as or "user" not in run_as:
            return os.access(path, os.W_OK)
        info = os.stat(path)
        uid = int(run_as["user"])
        groups = {int(run_as.get("group", -1)), *(int(g) for g in run_as.get("extra_groups") or [])}
        mode = info.st_mode
        if info.st_uid == uid:
            return bool(mode & stat.S_IWUSR)
        if info.st_gid in groups:
            return bool(mode & stat.S_IWGRP)
        return bool(mode & stat.S_IWOTH)
    except (OSError, ValueError, TypeError):
        return False


class Installs:
    """Install jobs. ``defs`` is the definitions dict, or a callable returning it (so config edits apply)."""

    KEEP_S = 3600.0
    TIMEOUT_S = 15 * 60.0
    MAX_LOG = 2000
    MAX_LINE = 600
    CREEP_S = 45.0  # time constant of the in-phase creep

    def __init__(self, defs: dict[str, AgentDef] | Callable[[], dict[str, AgentDef]] | None = None, *,
                 on_done: Callable[[str, dict], None] | None = None, timeout_s: float | None = None,
                 platform: str | None = None):
        self._defs = defs
        self.on_done = on_done
        self.timeout_s = timeout_s or self.TIMEOUT_S
        self.platform = platform
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.run_lock = threading.Lock()  # one npm -g at a time: two would fight over the same prefix

    # -- planning -------------------------------------------------------------------------------------------

    def defs(self) -> dict[str, AgentDef]:
        defs = self._defs() if callable(self._defs) else self._defs
        return defs if defs is not None else registry.definitions()

    def plan(self, kind: str, env: dict, *, method: str = "auto") -> dict[str, Any]:
        """What :meth:`start` would run: ``{kind, method, argv, display, note}``; :class:`InstallError` when nothing
        can run. Only looks for files; the Node.js version is checked inside the job."""
        d = self.defs().get(kind)
        if d is None:
            raise InstallError(f"不认识的 CLI：{kind}", "unknown_kind")
        if method not in ("auto", "npm", "official"):
            raise InstallError(f"不支持的安装方式：{method}", "bad_method")
        osk = registry.os_key(self.platform)
        main, alt = d.install.get(osk), d.install_alt.get(osk)
        is_npm = bool(main) and main[0] == "npm"
        os_label = {"windows": "Windows", "darwin": "macOS", "linux": "Linux"}[osk]
        if not main and not alt:
            raise InstallError(f"{d.label} 在 {os_label} 上没有可用的安装方式", "no_installer")

        problem: InstallError | None = None
        if method in ("auto", "npm") and main:
            try:
                return self._plan_main(d, main, is_npm, env)
            except InstallError as exc:
                problem = exc
            if method == "npm" or not alt:
                raise problem
        if method == "npm":
            raise InstallError(f"{d.label} 在 {os_label} 上不能用 npm 安装", "no_installer")
        if not alt:
            raise InstallError(f"{d.label} 没有官方安装脚本", "no_installer")
        plan = self._plan_official(d, alt, env)
        if problem is not None:
            plan["note"] = f"{problem.message.split('。')[0]}，改用官方安装脚本"
        return plan

    def _plan_main(self, d: AgentDef, argv: list[str], is_npm: bool, env: dict) -> dict[str, Any]:
        if is_npm:
            npm = registry.find_tool("npm", env)
            node = registry.find_tool("node", env)
            need = f" {d.node_min} 或更高版本" if d.node_min else ""
            alt = f"；{d.install_hint}" if d.install_hint and d.install_alt else ""
            if not npm:
                raise InstallError(f"没有找到 npm。npm 随 Node.js 一起安装，请先安装 Node.js{need}（{NODEJS_URL}）"
                                   f"后重试{alt}", "npm_missing", hint=d.install_hint, url=NODEJS_URL)
            if not node:
                raise InstallError(f"找到了 npm，但没有找到 node。请重新安装 Node.js{need}（{NODEJS_URL}）{alt}",
                                   "node_missing", hint=d.install_hint, url=NODEJS_URL)
            full = registry.runnable(npm, [*argv[1:], "--no-fund", "--no-audit", "--loglevel=http"])
            return {"kind": d.kind, "method": "npm", "argv": full, "display": " ".join(argv), "node": node,
                    "npm": npm, "note": ""}
        tool = registry.find_tool(argv[0], env)
        if not tool:
            raise InstallError(f"没有找到安装 {d.label} 需要的命令：{argv[0]}", "tool_missing")
        return {"kind": d.kind, "method": "command", "argv": registry.runnable(tool, argv[1:]),
                "display": " ".join(argv), "note": ""}

    def _plan_official(self, d: AgentDef, argv: list[str], env: dict) -> dict[str, Any]:
        shell = registry.find_tool(argv[0], env)
        if not shell:
            raise InstallError(f"没有找到 {argv[0]}，无法运行 {d.label} 的官方安装脚本", "tool_missing")
        script = argv[-1]
        if not WINDOWS and argv[0] != "powershell" and "curl " in script and not registry.find_tool("curl", env):
            raise InstallError(f"没有找到 curl，无法下载 {d.label} 的官方安装脚本。请先安装 curl 后重试",
                               "tool_missing")
        return {"kind": d.kind, "method": "official", "argv": [shell, *argv[1:]], "display": script, "note": ""}

    # -- jobs -----------------------------------------------------------------------------------------------

    def start(self, kind: str, env: dict, popen_args: dict | None = None, *, method: str = "auto") -> str:
        """Start installing ``kind`` (or return the install already running). ``env`` / ``popen_args``: the
        run-as account's (``Account.env()`` / ``Account.popen_args()``; None = this process's user)."""
        with self.lock:
            for job in self.jobs.values():
                if job["kind"] == kind and job["state"] in ("queued", "running"):
                    return job["job_id"]
        plan = self.plan(kind, env, method=method)
        d = self.defs()[kind]
        job_id = f"ij_{secrets.token_hex(6)}"
        now = time.time()
        job: dict[str, Any] = {
            "job_id": job_id, "kind": kind, "label": d.label, "method": plan["method"], "command": plan["display"],
            "state": "queued", "phase": "prepare", "progress": 0.0, "phase_at": now, "log": [], "log_base": 0,
            "error": None, "code": None, "hint": None, "result": None, "started_at": now, "finished_at": None,
            "proc": None, "cancel": False, "fetches": 0,
        }
        with self.lock:
            self.jobs = {k: v for k, v in self.jobs.items()
                         if v["state"] in ("queued", "running") or now - v["started_at"] < self.KEEP_S}
            self.jobs[job_id] = job
        threading.Thread(target=self._run, args=(job, d, plan, dict(env), popen_args), daemon=True,
                         name=f"agent-install-{kind}").start()
        return job_id

    def get(self, job_id: str, since: int = 0) -> dict | None:
        """The job with log lines from absolute index ``since`` on (``next`` is the index to ask from next)."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            return self._view(job, since)

    def current(self, kind: str) -> dict | None:
        """The running (or last, for ten minutes) install of ``kind``: ``{job_id,state,progress,log_tail,...}``."""
        with self.lock:
            jobs = [job for job in self.jobs.values() if job["kind"] == kind]
            if not jobs:
                return None
            job = max(jobs, key=lambda j: j["started_at"])
            if job["state"] not in ("queued", "running") and time.time() - (job["finished_at"] or 0) > 600:
                return None
            view = self._view(job, None)
        return {key: view[key] for key in ("job_id", "state", "progress", "phase", "phase_label", "log_tail",
                                           "error", "code", "method")}

    def list(self) -> list[dict]:
        with self.lock:
            return [self._view(job, None) for job in sorted(self.jobs.values(), key=lambda j: -j["started_at"])]

    def cancel(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job["state"] not in ("queued", "running"):
                return False
            job["cancel"] = True
            proc = job["proc"]
        if proc is not None:
            self._kill(proc)
        return True

    def _view(self, job: dict, since: int | None) -> dict:
        lines: list[str] = job["log"]
        base = job["log_base"]
        view = {key: job[key] for key in ("job_id", "kind", "label", "method", "command", "state", "phase", "error",
                                          "code", "hint", "result", "started_at", "finished_at")}
        view["phase_label"] = _PHASE[job["phase"]][1] if job["state"] in ("queued", "running") else {
            "done": "已完成", "failed": "失败", "cancelled": "已取消"}.get(job["state"], "")
        view["progress"] = round(self._progress(job))
        view["log_tail"] = lines[-8:]
        if since is not None:
            start = max(0, int(since) - base) if isinstance(since, int) and since >= 0 else 0
            view["log"] = lines[start:]
            view["next"] = base + len(lines)
            view["dropped"] = base > max(0, int(since) if isinstance(since, int) else 0)
        return view

    def _progress(self, job: dict) -> float:
        if job["state"] == "done":
            return 100.0
        _, _, low, high = _PHASE[job["phase"]]
        value = max(job["progress"], float(low))
        if job["state"] == "running":  # creep toward the end of the phase so a quiet installer still moves
            elapsed = max(0.0, time.time() - job["phase_at"])
            creep = low + (high - low) * 0.85 * (1 - math.exp(-elapsed / self.CREEP_S))
            value = max(value, creep)
        return min(value, 99.0 if job["state"] == "running" else value)

    # -- running --------------------------------------------------------------------------------------------

    def _phase(self, job: dict, phase: str, at_least: float | None = None) -> None:
        order = [p[0] for p in PHASES]
        if order.index(phase) > order.index(job["phase"]):
            job["progress"] = max(job["progress"], float(_PHASE[phase][2]))
            job["phase"], job["phase_at"] = phase, time.time()
        if at_least is not None:
            job["progress"] = max(job["progress"], min(at_least, float(_PHASE[job["phase"]][3])))

    def _log(self, job: dict, line: str) -> None:
        line = _ANSI.sub("", line).rstrip()
        if not line.strip():
            return
        with self.lock:
            job["log"].append(line[: self.MAX_LINE])
            extra = len(job["log"]) - self.MAX_LOG
            if extra > 0:
                del job["log"][:extra]
                job["log_base"] += extra
        self._estimate(job, line)

    def _estimate(self, job: dict, line: str) -> None:
        low = line.lower()
        if "http fetch" in low or low.startswith(("downloading", "fetching")) or "download" in low:
            self._phase(job, "download")
            if "http fetch" in low:
                job["fetches"] += 1
                job["progress"] = max(job["progress"], 10 + 48 * (1 - math.exp(-job["fetches"] / 25)))
        match = _PERCENT.search(line)
        if match and job["phase"] in ("prepare", "download"):
            pct = min(100.0, float(match.group(1)))
            self._phase(job, "download", 8 + 0.52 * pct)
        if re.search(r"\breify\b|postinstall|extracting|linking", low) or (
                # an official script opens with "==> Installing Codex CLI" before it downloads anything (seen on
                # Windows): the generic words only count once the download has started
                job["phase"] != "prepare" and re.search(r"\binstalling\b|setting up|\bbuild\b", low)):
            self._phase(job, "install", 64)
        if re.search(r"\badded \d+ packages?|\bchanged \d+ packages?|up to date|successfully installed|"
                     r"installation complete|installed successfully", low):
            self._phase(job, "install", 88)

    def _finish(self, job: dict, state: str, error: str | None = None, code: str | None = None,
                hint: str | None = None) -> None:
        with self.lock:
            job.update(state=state, error=error, code=code, hint=hint, finished_at=time.time(), proc=None)
            if state == "done":
                job["progress"], job["phase"] = 100.0, "verify"
        if error:
            self._log(job, error)
        if self.on_done is not None:
            try:
                self.on_done(job["kind"], self.get(job["job_id"]) or {})
            except Exception:  # noqa: BLE001, S110
                pass

    def _run(self, job: dict, d: AgentDef, plan: dict, env: dict, popen_args: dict | None) -> None:
        try:
            if not self.run_lock.acquire(blocking=False):
                self._log(job, "等待其他 CLI 安装完成…")
                self.run_lock.acquire()
            try:
                job["state"] = "running"
                job["phase_at"] = time.time()
                self._run_locked(job, d, plan, env, popen_args)
            finally:
                self.run_lock.release()
        except Exception as exc:  # noqa: BLE001 -- never leave a job "running"
            if job["state"] in ("queued", "running"):
                self._finish(job, "failed", f"安装出错：{exc}", "internal")

    def _run_locked(self, job: dict, d: AgentDef, plan: dict, env: dict, popen_args: dict | None) -> None:
        if job["cancel"]:
            return self._finish(job, "cancelled", None, "cancelled")
        self._log(job, f"安装 {d.label}：{plan['display']}")
        if plan.get("note"):
            self._log(job, plan["note"])
        argv = list(plan["argv"])
        if plan["method"] == "npm":
            argv, env, switched = self._prepare_npm(job, d, plan, env, popen_args)
            if argv is None:
                return None
            if switched:
                job["method"] = "official"
        output = self._exec(job, argv, env, popen_args)
        if output is None:
            return None
        code, text = output
        if job["cancel"]:
            return self._finish(job, "cancelled", "已取消安装", "cancelled")
        if code != 0:
            return self._finish(job, "failed", *self._explain_failure(d, code, text, job["method"]))
        self._phase(job, "verify", 92)
        self._log(job, "安装命令已完成，正在检查版本…")
        registry.invalidate(d.kind)
        probed = registry.probe(d, env, fresh=True, run_as=popen_args)
        job["result"] = probed
        if not probed.get("installed"):
            where = "npm 的全局目录" if job["method"] == "npm" else "安装目录"
            return self._finish(job, "failed", f"安装命令执行成功，但没有找到 {'/'.join(d.binaries)}。"
                                f"可能需要把{where}加入 PATH，然后在 CLI 管理里点“重新检测”", "not_found_after_install")
        version = f" {probed['version']}" if probed.get("version") else ""
        self._log(job, f"已安装 {d.label}{version}：{probed['path']}")
        return self._finish(job, "done")

    def _prepare_npm(self, job: dict, d: AgentDef, plan: dict, env: dict, popen_args: dict | None):
        """Node.js version and a writable global prefix. Returns (argv, env, switched_to_official) or (None,..)."""
        argv = list(plan["argv"])
        code, out = registry.run_capture(registry.runnable(plan["node"], ["--version"]), env, timeout=10,
                                         run_as=popen_args)
        major = node_major(out) if code == 0 else None
        if major is not None:
            self._log(job, f"Node.js {out.strip().splitlines()[0]}")
        if d.node_min and major is not None and major < d.node_min:
            message = (f"{d.label} 需要 Node.js {d.node_min} 或更高版本，当前是 {out.strip().splitlines()[0]}。"
                       f"请先升级 Node.js（{NODEJS_URL}）")
            alt = d.install_alt.get(registry.os_key(self.platform))
            if alt:
                try:
                    official = self._plan_official(d, alt, env)
                except InstallError:
                    official = None
                if official is not None:
                    self._log(job, f"Node.js 版本太低，改用官方安装脚本：{official['display']}")
                    return official["argv"], env, True
            self._finish(job, "failed", message, "node_too_old", d.install_hint or None)
            return None, env, False
        self._phase(job, "prepare", 5)
        if not WINDOWS:
            prefix = registry.npm_prefix(env)
            if prefix:
                target = os.path.join(prefix, "lib", "node_modules")
                target = target if os.path.isdir(target) else prefix
                if not _writable_by(target, popen_args):
                    home = registry._home(env)
                    if home:
                        user_prefix = os.path.join(home, ".npm-global")
                        try:
                            os.makedirs(os.path.join(user_prefix, "bin"), exist_ok=True)
                            if popen_args and "user" in popen_args:
                                for folder in (user_prefix, os.path.join(user_prefix, "bin")):
                                    os.chown(folder, int(popen_args["user"]), int(popen_args.get("group", -1)))
                        except OSError:
                            pass
                        env = {**env, "NPM_CONFIG_PREFIX": user_prefix}
                        self._log(job, f"npm 全局目录 {prefix} 没有写权限，改为安装到 {user_prefix}（不需要管理员权限）。"
                                       f"在终端里直接使用需要把 {user_prefix}/bin 加入 PATH")
        return argv, env, False

    def _explain_failure(self, d: AgentDef, code: int | None, text: str, method: str) -> tuple[str, str, str | None]:
        low = text.lower()
        alt = d.install_hint if d.install_alt and method == "npm" else None
        if "eacces" in low or "permission denied" in low or "eperm" in low:
            return ("安装失败：没有写入权限。可以改用官方安装脚本，或执行 npm config set prefix ~/.npm-global 后重试",
                    "permission_denied", alt)
        if re.search(r"enotfound|etimedout|econnreset|econnrefused|eai_again|could not resolve host|"
                     r"network|unable to access", low):
            return "安装失败：网络不通，无法下载。请检查网络或代理设置后重试", "network", alt
        if "e404" in low or "404 not found" in low:
            return "安装失败：npm 上找不到这个包，可能包名已经变了", "not_found", alt
        if code is None:
            return "安装失败：安装命令没能运行", "spawn_failed", alt
        return f"安装失败（退出码 {code}），详见上面的输出", "exit_code", alt

    def _exec(self, job: dict, argv: list[str], env: dict, popen_args: dict | None) -> tuple[int | None, str] | None:
        """Run ``argv``, feeding the log; (exit code, last output) -- None when the job already finished."""
        home = registry._home(env)
        cwd = home if home and os.path.isdir(home) else None
        tail: list[str] = []

        def feed(line: str) -> None:
            self._log(job, line)
            tail.append(line)
            del tail[:-60]

        if WINDOWS and popen_args and popen_args.get("session") is not None:
            return self._exec_session(job, argv, env, cwd, int(popen_args["session"]), feed, tail)
        extra: dict[str, Any] = {}
        if WINDOWS:
            extra["creationflags"] = registry.NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            extra["start_new_session"] = True
            if popen_args and "user" in popen_args:
                extra.update({key: popen_args[key] for key in ("user", "group", "extra_groups") if key in popen_args})
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=env, cwd=cwd, **extra)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self._finish(job, "failed", f"无法启动安装命令：{exc}", "spawn_failed")
            return None
        job["proc"] = proc
        if job["cancel"]:
            self._kill(proc)
        timer = threading.Timer(self.timeout_s, self._timeout, args=(job, proc))
        timer.daemon = True
        timer.start()
        try:
            buffer = b""
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
                if not chunk:
                    break
                buffer += chunk
                parts = re.split(rb"\r\n|\r|\n", buffer)
                buffer = parts.pop()
                for part in parts:
                    feed(part.decode("utf-8", "replace"))
            if buffer:
                feed(buffer.decode("utf-8", "replace"))
            code = proc.wait()
        finally:
            timer.cancel()
        if job.get("timed_out"):
            self._finish(job, "failed", f"安装超过 {int(self.timeout_s // 60)} 分钟没有完成，已停止", "timeout")
            return None
        return code, "\n".join(tail)

    def _exec_session(self, job, argv, env, cwd, session, feed, tail):  # pragma: no cover - Windows service only
        """As the Windows service: start in the user's session with output to a file, and follow the file."""
        from .. import winsession
        folder = tempfile.mkdtemp(prefix="anx-install-")
        path = os.path.join(folder, "install.log")
        try:
            with open(path, "wb") as log:
                try:
                    proc = winsession.spawn(session, argv, cwd, env, log, flags=registry.NO_WINDOW)
                except OSError as exc:
                    self._finish(job, "failed", f"无法启动安装命令：{exc}", "spawn_failed")
                    return None
            job["proc"] = proc
            deadline = time.monotonic() + self.timeout_s
            position, buffer = 0, b""
            while True:
                done = proc.poll() is not None
                with open(path, "rb") as handle:
                    handle.seek(position)
                    chunk = handle.read()
                position += len(chunk)
                buffer += chunk
                parts = re.split(rb"\r\n|\r|\n", buffer)
                buffer = parts.pop()
                for part in parts:
                    feed(part.decode("utf-8", "replace"))
                if done:
                    break
                if time.monotonic() > deadline:
                    proc.kill()
                    self._finish(job, "failed", f"安装超过 {int(self.timeout_s // 60)} 分钟没有完成，已停止",
                                 "timeout")
                    return None
                time.sleep(0.3)
            if buffer:
                feed(buffer.decode("utf-8", "replace"))
            return proc.poll(), "\n".join(tail)
        finally:
            try:
                os.remove(path)
                os.rmdir(folder)
            except OSError:
                pass

    def _timeout(self, job: dict, proc) -> None:
        job["timed_out"] = True
        self._kill(proc)

    @staticmethod
    def _kill(proc) -> None:
        try:
            if not WINDOWS and isinstance(proc, subprocess.Popen):
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(3)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
            elif WINDOWS and isinstance(proc, subprocess.Popen):
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], check=False, capture_output=True,
                               creationflags=registry.NO_WINDOW, timeout=10)
            else:
                proc.kill()
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass
