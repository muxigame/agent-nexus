"""``anx wake doctor [--kind claude|codex|gpt]``: does unattended waking work on this device? (design §8.7)

For claude and codex: resolve the executable and print its version, create a
scratch session in a temporary folder, then resume it with exactly the wake
command (the template and the permission flags of decision D2) and the prompt
"回复 OK 后结束". It passes when that exits 0 within the timeout; stdin is the
null device, so an interactive prompt can only end in a failure or a timeout,
never a hang. For gpt: one test event through the device plugin's
``/anx/companion-event`` to GPT Companion (chatx).

The results go to ``.anx/wake-doctor.json``; the manager adds them to the
device's ``agent_runtimes`` (``mail_who`` shows them). This talks to the real
CLIs and the real chatx: it is a command for the owner, never run by tests
against anything but fakes.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .adapters import (PERMISSIONS, WakeError, build_argv, permission_flags, popen_args, resolve_executable,
                       safe_prompt, wake_env)

PROMPT = "回复 OK 后结束"
RESULTS_NAME = "wake-doctor.json"
DEFAULT_TIMEOUT_S = 120.0


def results_path(root: Path) -> Path:
    return Path(root) / ".anx" / RESULTS_NAME


def load_results(root: Path) -> dict:
    try:
        data = json.loads(results_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _run(argv: list[str], cwd: str, timeout: float) -> tuple[int | None, str, float, str | None]:
    started = time.monotonic()
    try:
        done = subprocess.run(popen_args(argv), cwd=cwd, env=wake_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        output = (exc.output or b"").decode("utf-8", "replace") if isinstance(exc.output, bytes) else str(exc.output or "")
        return None, output, time.monotonic() - started, (
            f"no exit within {int(timeout)} s: it is probably waiting for an interactive prompt")
    except (OSError, WakeError) as exc:
        return None, "", time.monotonic() - started, f"could not start: {exc}"
    return done.returncode, done.stdout.decode("utf-8", "replace"), time.monotonic() - started, None


def _session_id(kind: str, output: str) -> str | None:
    """The new session's id: claude's JSON result ``session_id``; codex's ``thread.started`` event."""
    for line in [output, *output.splitlines()]:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        for key in ("session_id", "thread_id", "conversation_id", "id"):
            value = item.get(key)
            if kind == "codex" and key == "id" and item.get("type") not in ("thread.started", "session.created"):
                continue
            if isinstance(value, str) and value:
                return value
    return None


def _scratch_command(kind: str, executable: list[str], flags: list[str]) -> list[str]:
    prompt = safe_prompt(PROMPT, executable)
    if kind == "claude":
        return [*executable, "-p", "--output-format", "json", *flags, prompt]
    return [*executable, "exec", "--json", "--skip-git-repo-check", *flags, prompt]


def check_cli(kind: str, config: dict, *, permission: str, timeout: float) -> dict[str, Any]:
    result: dict[str, Any] = {"kind": kind, "ok": False, "permission": permission, "at": time.time()}
    if permission == "inherit":
        result["warning"] = ("inherit adds no permission flags: a session whose own mode asks for confirmation "
                             "stops at that prompt when woken")
    executable = resolve_executable(kind, config)
    if executable is None:
        result["error"] = f"{kind} executable not found (PATH, %APPDATA%\\npm, ~/.local/bin or wake.executables.{kind})"
        return result
    result["executable"] = executable
    scratch = tempfile.mkdtemp(prefix=f"anx-wake-doctor-{kind}-")
    try:
        code, output, _, error = _run([*executable, "--version"], scratch, 30)
        result["version"] = output.strip().splitlines()[0][:200] if output.strip() else None
        if code != 0:
            result["error"] = error or f"--version exited {code}"
            return result
        try:
            flags = permission_flags(kind, permission, config)
        except WakeError as exc:
            result["error"] = str(exc)
            return result
        code, output, _, error = _run(_scratch_command(kind, executable, flags), scratch, timeout)
        session = _session_id(kind, output)
        if code != 0 or not session:
            result["error"] = error or (f"the scratch session exited {code}" if code != 0
                                        else "the scratch session printed no session id")
            result["output"] = output[-800:]
            return result
        result["session_id"] = session
        try:
            argv = build_argv(kind, {"resume_id": session, "prompt": PROMPT, "permission": permission,
                                     "workdir": scratch}, config)
        except WakeError as exc:
            result["error"] = str(exc)
            return result
        result["argv"] = argv
        code, output, seconds, error = _run(argv, scratch, timeout)
        result.update(exit_code=code, seconds=round(seconds, 1))
        if error or code != 0:
            result["error"] = error or f"the resumed run exited {code}"
            result["output"] = output[-800:]
            return result
        result["ok"] = True
        return result
    except WakeError as exc:
        result["error"] = str(exc)
        return result
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def check_companion(config: dict, *, endpoint: str | None, timeout: float) -> dict[str, Any]:
    result: dict[str, Any] = {"kind": "gpt", "ok": False, "at": time.time()}
    endpoint = endpoint or config.get("companion_endpoint")
    if not endpoint:
        result["error"] = "pass --endpoint (the device plugin's URL) or set wake.companion_endpoint"
        return result
    payload = {"project_id": config.get("doctor_companion_route") or "auto", "event_type": "wake_doctor",
               "summary": "[agent-nexus] wake doctor 测试事件：无需处理，回复 OK 即可。", "workdir": os.getcwd()}
    request = urllib.request.Request(f"{str(endpoint).rstrip('/')}/anx/companion-event",
                                     data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-ANX": "1"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except (OSError, ValueError, urllib.error.URLError) as exc:
        result["error"] = f"device plugin unreachable at {endpoint}: {exc}"
        return result
    result.update(event_id=body.get("event_id"), project_id=body.get("project_id"))
    if not body.get("delivered"):
        result["error"] = f"GPT Companion did not take the event: {body.get('error') or body}"
        return result
    result["ok"] = True
    return result


def run(root: Path, config: dict, *, kinds: list[str], permission: str = "bypass", timeout: float = DEFAULT_TIMEOUT_S,
        endpoint: str | None = None, out: Callable[[str], None] = print) -> int:
    results = load_results(root)
    failed = False
    for kind in kinds:
        out(f"== {kind}")
        item = (check_companion(config, endpoint=endpoint, timeout=min(timeout, 30)) if kind == "gpt"
                else check_cli(kind, config, permission=permission, timeout=timeout))
        results[kind] = {key: value for key, value in item.items() if key != "output"}
        for key in ("executable", "version", "permission", "session_id", "exit_code", "seconds", "event_id"):
            if item.get(key) is not None:
                out(f"   {key}: {item[key]}")
        if item.get("warning"):
            out(f"   warning: {item['warning']}")
        if item["ok"]:
            out("   OK: unattended run finished without an interactive prompt" if kind != "gpt"
                else "   OK: GPT Companion took the test event")
        else:
            failed = True
            out(f"   FAILED: {item.get('error')}")
            if item.get("output"):
                out("   output (end): " + " ".join(str(item["output"]).split())[-400:])
    path = results_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if failed else 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--kind", action="append", choices=["claude", "codex", "gpt"],
                        help="check only this runtime (repeatable; default: all three)")
    parser.add_argument("--permission", choices=PERMISSIONS, default="bypass",
                        help="the wake permission mode to test (decision D2; default bypass)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="seconds per run (default 120)")
    parser.add_argument("--endpoint", help="the device plugin's URL for the gpt check (default: wake.companion_endpoint)")
