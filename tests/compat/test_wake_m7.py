"""Milestone M7: waking recipients and the offline queue (design §8.7, §8.8, §8.11, §13 M7; decisions D2, D9).

End to end over real WebSockets and the MCP endpoint on ephemeral ports, with
temporary state. The "claude" and "codex" the device starts are a fake Python
script (``fake_cli.py``, written per test) that records its argv, cwd and job
id; GPT Companion is a fake HTTP endpoint. No real claude, codex or chatx is
ever started or contacted.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastmcp import FastMCP

from test_compat_matrix import AGENT_ID, device_server, extra_server, package
from test_gateway_m4 import HOST_A, SECOND_ID, mcp_client, running_gateway, start_agent

FAKE_CLI = r'''
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
try:
    with open(os.path.join(here, "control.json"), encoding="utf-8") as handle:
        control = json.load(handle)
except Exception:
    control = {}
kind, args = sys.argv[1], sys.argv[2:]
with open(os.path.join(here, "calls.jsonl"), "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"kind": kind, "argv": args, "cwd": os.getcwd(),
                             "job": os.environ.get("ANX_WAKE_JOB"), "at": time.time()}) + "\n")
if "--version" in args:
    print(kind + " 9.9.9 (fake)")
    sys.exit(0)
settings = control.get(kind, {})
time.sleep(float(settings.get("sleep", 0)))
if kind == "claude":
    print(json.dumps({"type": "result", "session_id": settings.get("session_id", "scratch-claude"), "result": "OK"}))
else:
    print(json.dumps({"type": "thread.started", "thread_id": settings.get("session_id", "scratch-codex")}))
sys.exit(int(settings.get("exit", 0)))
'''

#: Short timings so a test runs in seconds; the logic is the same as with the defaults.
MAILBOX = {"presence": {"idle_after_s": 0.5},
           "wake": {"debounce_s": 0.05, "rerun_debounce_s": 0.05, "scan_interval_s": 0.1}}


class FakeCli:
    def __init__(self, directory: Path):
        self.dir = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.script = directory / "fake_cli.py"
        self.script.write_text(FAKE_CLI, encoding="utf-8")
        self.calls_path = directory / "calls.jsonl"

    def control(self, **settings) -> None:
        (self.dir / "control.json").write_text(json.dumps(settings), encoding="utf-8")

    def executables(self) -> dict:
        return {kind: [sys.executable, str(self.script), kind] for kind in ("claude", "codex")}

    def calls(self, kind: str | None = None) -> list[dict]:
        if not self.calls_path.exists():
            return []
        items = [json.loads(line) for line in self.calls_path.read_text(encoding="utf-8").splitlines() if line]
        return [item for item in items if kind is None or item["kind"] == kind]


async def until(predicate, timeout=5.0, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(step)
    return predicate()


async def start_device(tmp_path, port, agents, fake: FakeCli, *, wake_extra=None, transport=None,
                       agent_id=AGENT_ID, token_env="AGENT_TOKEN"):
    agent_module = package("current", "agent")
    workdir = (tmp_path / f"{agent_id}-project").resolve()
    workdir.mkdir(exist_ok=True)
    wake = {"executables": fake.executables(), "claude_home": str(tmp_path / "claude-home"),
            "codex_home": str(tmp_path / "codex-home"), "probe_processes": False, **(wake_extra or {})}
    cloud = {"agent_id": agent_id, "url": f"ws://127.0.0.1:{port}", "token_env": token_env, "hostname": HOST_A,
             "workspaces": {"project": {"path": str(workdir)}}, "refresh_interval_s": 60,
             "console_url": "http://127.0.0.1:1", "wake": wake}
    fake.wake_config = wake  # the runner reads it at each wake
    ready = asyncio.Event()
    agents.append(asyncio.create_task(agent_module.run_agent(
        tmp_path / f"root-{agent_id}", cloud, transports={"workspace": transport or device_server(agent_id),
                                                          "extra": extra_server()},
        ready=ready, status={})))
    await asyncio.wait_for(ready.wait(), 20)
    return workdir


async def use(client, name, kind="claude", resume_id=None, device=AGENT_ID, **extra):
    arguments = {"device_id": device, "session_name": name, "kind": kind, **extra}
    if resume_id:
        arguments["resume_id"] = resume_id
    selected = (await client.call_tool("use_device", arguments)).data
    return selected["workspace_id"]


async def send(client, handle, to, body, **extra):
    return (await client.call_tool("mail_send", {"workspace_id": handle, "to": to, "body": body, **extra})).data


def jobs(app, address=None):
    return app.state.gateway.wake.jobs(address)


@contextlib.asynccontextmanager
async def gateway(tmp_path, monkeypatch, mailbox=None):
    merged = json.loads(json.dumps(MAILBOX))
    for key, value in (mailbox or {}).items():
        merged.setdefault(key, {}).update(value)
    async with running_gateway(tmp_path, monkeypatch, config_extra={"mailbox": merged}) as (app, port, agents, _):
        yield app, port, agents


def root_of(tmp_path, agent_id=AGENT_ID) -> Path:
    return tmp_path / f"root-{agent_id}"


# -- the main path: idle recipient woken, template, ack, report ---------------------------------

async def test_idle_recipient_is_woken_with_the_template_then_acked_and_reported(tmp_path, monkeypatch):
    fake = FakeCli(tmp_path / "fake")
    fake.control(claude={"sleep": 1.0})
    async with gateway(tmp_path, monkeypatch) as (app, port, agents):
        workdir = await start_device(tmp_path, port, agents, fake)
        peer = app.state.hub.peers[AGENT_ID]
        assert {"session_wake/1", "session_probe/1"} <= peer.features
        async with mcp_client(port) as a, mcp_client(port) as b:
            ha = await use(a, "alpha")
            hb = await use(b, "beta", resume_id="sess-beta")
            await asyncio.sleep(0.6)  # beta goes idle (idle_after_s = 0.5)
            started = time.monotonic()
            sent = await send(a, ha, "beta", "please build", subject="构建任务", kind="task")
            assert sent["deliveries"][0]["action"] == "wake_queued"
            calls = await until(lambda: fake.calls("claude"), timeout=3)
            assert calls and time.monotonic() - started < 1.0, "woken within 1 s"
            call = calls[0]
            # The default template (§8.7) with the default permission mode bypass (decision D2).
            prompt = call["argv"][-1]
            assert call["argv"] == ["-p", "--resume", "sess-beta", "--output-format", "json",
                                    "--dangerously-skip-permissions", prompt]
            assert Path(call["cwd"]).resolve() == workdir
            assert prompt.startswith("[agent-nexus] 你（beta@" + HOST_A + "）有 1 封新邮件")
            assert "构建任务" in prompt and f'mail_inbox(workspace_id="{hb}")' in prompt
            job = jobs(app, f"beta@{AGENT_ID}")[0]
            assert call["job"] == job["id"] and job["state"] == "running" and job["pid"]

            who = (await a.call_tool("mail_who", {"workspace_id": ha})).data
            beta = next(item for item in who["addresses"] if item["name"] == "beta")
            assert beta["wakeable"] and beta["wake_adapter"] == "claude" and beta["presence"] == "busy"
            assert beta["last_wake"]["state"] == "running"
            status = (await a.call_tool("mail_status", {"workspace_id": ha, "message_id": sent["message_id"]})).data
            assert status["recipients"][0]["status"] == "woken" and status["recipients"][0]["wake"]["state"] == "running"

            # The recipient's next tool call acknowledges the wake ...
            await b.call_tool("mail_inbox", {"workspace_id": hb})
            assert jobs(app, f"beta@{AGENT_ID}")[0]["state"] == "acked"
            # ... and the device reports the exit (wake_report).
            done = await until(lambda: jobs(app, f"beta@{AGENT_ID}")[0]["state"] == "done", timeout=6)
            assert done and jobs(app, f"beta@{AGENT_ID}")[0]["exit_code"] == 0
            log = root_of(tmp_path) / ".anx" / "wake" / f"{job['id']}.log"
            assert "session_id" in log.read_text(encoding="utf-8")
            job_file = root_of(tmp_path) / ".anx" / "wake" / f"{job['id']}.json"
            assert await until(lambda: not job_file.exists())  # removed once the report was taken


async def test_active_recipient_is_not_woken_and_a_burst_makes_one_job(tmp_path, monkeypatch):
    fake = FakeCli(tmp_path / "fake")
    async with gateway(tmp_path, monkeypatch, {"wake": {"debounce_s": 0.4}}) as (app, port, agents):
        await start_device(tmp_path, port, agents, fake)
        async with mcp_client(port) as a, mcp_client(port) as b:
            ha = await use(a, "alpha")
            hb = await use(b, "beta", resume_id="sess-beta")
            # Active (it just called): the notice on its next call is enough.
            sent = await send(a, ha, "beta", "hello")
            assert sent["deliveries"][0]["action"] == "noticed_on_next_call"
            assert jobs(app) == []
            await b.call_tool("mail_read", {"workspace_id": hb})
            await asyncio.sleep(0.6)
            # Five mails in a row: one job (the debounce merges them), one process.
            actions = [(await send(a, ha, "beta", f"mail {n}"))["deliveries"][0]["action"] for n in range(5)]
            assert actions == ["wake_queued"] * 5
            assert len(jobs(app)) == 1 and len(jobs(app)[0]["message_ids"]) == 5
            assert await until(lambda: len(fake.calls("claude")) == 1, timeout=3)
            assert "5 封新邮件" in fake.calls("claude")[0]["argv"][-1]
            await asyncio.sleep(0.5)
            assert len(fake.calls("claude")) == 1 and len(jobs(app)) == 1
            # wake=always wakes even an active recipient; wake=never never does.
            await b.call_tool("mail_read", {"workspace_id": hb, "thread_id": sent["thread_id"]})
            assert (await send(a, ha, "beta", "quiet", wake="never"))["deliveries"][0]["action"] == "noticed_on_next_call"


async def test_mail_during_a_run_triggers_a_second_wake_after_the_report(tmp_path, monkeypatch):
    fake = FakeCli(tmp_path / "fake")
    fake.control(claude={"sleep": 1.0})
    async with gateway(tmp_path, monkeypatch) as (app, port, agents):
        await start_device(tmp_path, port, agents, fake)
        async with mcp_client(port) as a, mcp_client(port) as b:
            ha = await use(a, "alpha")
            await use(b, "beta", resume_id="sess-beta")
            await asyncio.sleep(0.6)
            await send(a, ha, "beta", "first")
            assert await until(lambda: fake.calls("claude"), timeout=3)
            second = await send(a, ha, "beta", "second, while running")
            assert second["deliveries"][0]["action"] == "wake_queued"
            assert len(fake.calls("claude")) == 1  # never two processes for one session
            assert await until(lambda: len(fake.calls("claude")) == 2, timeout=8)
            first_job, second_job = jobs(app, f"beta@{AGENT_ID}")
            assert first_job["state"] == "done" and second_job["message_ids"] == [second["message_id"]]
            assert fake.calls("claude")[1]["job"] == second_job["id"]


async def test_a_live_session_is_skipped(tmp_path, monkeypatch):
    fake = FakeCli(tmp_path / "fake")
    async with gateway(tmp_path, monkeypatch) as (app, port, agents):
        await start_device(tmp_path, port, agents, fake)
        session_file = tmp_path / "claude-home" / "projects" / "some-project" / "sess-beta.jsonl"
        session_file.parent.mkdir(parents=True)
        session_file.write_text("{}\n", encoding="utf-8")  # written just now: in use
        async with mcp_client(port) as a, mcp_client(port) as b:
            ha = await use(a, "alpha")
            await use(b, "beta", resume_id="sess-beta")
            await asyncio.sleep(0.6)
            await send(a, ha, "beta", "are you there")
            assert await until(lambda: jobs(app) and jobs(app)[0]["state"] == "skipped_active", timeout=3)
            await asyncio.sleep(0.3)
            assert fake.calls() == []


async def test_offline_device_waits_and_a_restarted_gateway_dispatches_on_reconnect(tmp_path, monkeypatch):
    fake = FakeCli(tmp_path / "fake")
    async with gateway(tmp_path, monkeypatch) as (app, port, agents):
        await start_device(tmp_path, port, agents, fake)
        async with mcp_client(port) as a, mcp_client(port) as b:
            ha = await use(a, "alpha")
            await use(b, "beta", resume_id="sess-beta")
        for agent in agents:
            agent.cancel()
        await asyncio.gather(*agents, return_exceptions=True)
        agents.clear()
        assert await until(lambda: AGENT_ID not in app.state.hub.peers)
        async with mcp_client(port) as a:
            sent = await send(a, ha, "beta", "for later")
        assert sent["deliveries"][0]["action"] == "wake_waiting_device"
        assert jobs(app)[0]["state"] == "waiting_device"
    # The gateway restarts (same state directory); the job is still there and goes out on reconnect.
    async with gateway(tmp_path, monkeypatch) as (app, port, agents):
        assert jobs(app)[0]["state"] == "waiting_device"
        await start_device(tmp_path, port, agents, fake)
        connected = time.monotonic()
        assert await until(lambda: fake.calls("claude"), timeout=3)
        assert time.monotonic() - connected < 2.0, "dispatched within 2 s of the reconnect"
        assert fake.calls("claude")[0]["argv"][2] == "sess-beta"


async def test_legacy_agent_wakes_are_unsupported(tmp_path, monkeypatch):
    async with gateway(tmp_path, monkeypatch) as (app, port, agents):
        await start_agent(tmp_path, port, agents, kind="legacy")
        async with mcp_client(port) as a, mcp_client(port) as b:
            ha = await use(a, "alpha")
            await use(b, "beta", resume_id="sess-beta")
            await asyncio.sleep(0.6)
            sent = await send(a, ha, "beta", "hello old device")
            assert sent["deliveries"][0]["action"] == "wake_unsupported"
            job = jobs(app)[0]
            assert job["state"] == "unsupported" and "legacy" in job["last_error"]
            status = (await a.call_tool("mail_status", {"message_id": sent["message_id"]})).data
            assert status["recipients"][0]["wake"]["state"] == "unsupported"
            assert status["recipients"][0]["status"] == "delivered"
            who = (await a.call_tool("mail_who", {})).data
            assert not next(item for item in who["addresses"] if item["name"] == "beta")["wakeable"]


async def test_fallback_exec_runs_the_command_through_a_legacy_device_plugin(tmp_path, monkeypatch):
    commands = []
    server = device_server(AGENT_ID)

    @server.tool
    async def exec_command(cmd: str, workdir: str | None = None, yield_time_ms: float | None = None) -> dict:
        commands.append({"cmd": cmd, "workdir": workdir})
        return {"status": "ok", "exit_code": None, "session_id": 1}

    legacy_agent = package("legacy", "agent")
    async with gateway(tmp_path, monkeypatch, {"wake": {"fallback_exec": True}}) as (app, port, agents):
        workdir = (tmp_path / "legacy-project").resolve()
        workdir.mkdir()
        ready = asyncio.Event()
        cloud = {"agent_id": AGENT_ID, "url": f"ws://127.0.0.1:{port}", "token_env": "AGENT_TOKEN",
                 "workspaces": {"project": {"path": str(workdir)}}, "refresh_interval_s": 60}
        agents.append(asyncio.create_task(legacy_agent.run_agent(
            tmp_path, cloud, transports={"workspace": server, "extra": extra_server()}, ready=ready)))
        await asyncio.wait_for(ready.wait(), 20)
        async with mcp_client(port) as a, mcp_client(port) as b:
            ha = await use(a, "alpha")
            hb = await use(b, "beta", resume_id="sess-beta")
            await asyncio.sleep(0.6)
            await send(a, ha, "beta", "old device, new trick")
            assert await until(lambda: commands, timeout=3)
            assert commands[0]["cmd"].startswith("claude -p --resume sess-beta --output-format json "
                                                  "--dangerously-skip-permissions ")
            assert commands[0]["workdir"] == str(workdir)
            assert await until(lambda: jobs(app)[0]["state"] == "dispatched")
            await b.call_tool("mail_inbox", {"workspace_id": hb})
            job = jobs(app)[0]
            assert job["state"] == "acked" and job["finished_at"] is not None  # nothing reports a fallback run


async def test_ack_timeout_retries_then_fails_with_a_system_mail(tmp_path, monkeypatch):
    fake = FakeCli(tmp_path / "fake")
    fake.control(claude={"sleep": 4})
    async with gateway(tmp_path, monkeypatch, {"wake": {"ack_timeout_s": 0.3, "max_attempts": 2}}) \
            as (app, port, agents):
        offset = [0.0]
        app.state.store.clock = lambda: time.time() + offset[0]
        await start_device(tmp_path, port, agents, fake)
        async with mcp_client(port) as a, mcp_client(port) as b:
            ha = await use(a, "alpha")
            await use(b, "beta", resume_id="sess-beta")
            offset[0] += 1
            sent = await send(a, ha, "beta", "nobody answers", subject="无人应答")
            assert await until(lambda: fake.calls("claude"), timeout=3)
            # No acknowledgement within ack_timeout_s: queued again with backoff (10 s for the first retry).
            requeued = await until(lambda: jobs(app)[0]["state"] == "queued" and jobs(app)[0]["attempts"] == 1, timeout=3)
            assert requeued and "not acknowledged" in jobs(app)[0]["last_error"]
            offset[0] += 11
            app.state.gateway.wake.kick()
            # The retry reaches the same job id: the device does not start a second process.
            assert await until(lambda: jobs(app)[0]["state"] == "failed", timeout=4)
            assert len(fake.calls("claude")) == 1
            inbox = (await a.call_tool("mail_inbox", {"workspace_id": ha})).data
            system = [m for m in inbox["messages"] if m["kind"] == "system"]
            assert system and "唤醒失败" in system[0]["snippet"] and system[0]["thread_id"] == sent["thread_id"]


async def test_companion_wake_posts_the_prompt_and_retries_with_a_notice(tmp_path, monkeypatch):
    received, answers = [], [{"delivered": False, "error": "chatx is not running"}]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append({"path": self.path, "body": body, "x_anx": self.headers.get("X-ANX")})
            reply = answers.pop(0) if answers else {"status": "ok", "delivered": True, "event_id": "evt-1"}
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    plugin = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=plugin.serve_forever, daemon=True).start()
    fake = FakeCli(tmp_path / "fake")
    try:
        async with gateway(tmp_path, monkeypatch) as (app, port, agents):
            offset = [0.0]
            app.state.store.clock = lambda: time.time() + offset[0]
            await start_device(tmp_path, port, agents, fake,
                               wake_extra={"companion_endpoint": f"http://127.0.0.1:{plugin.server_port}"})
            async with mcp_client(port) as a, mcp_client(port) as g:
                ha = await use(a, "alpha")
                hg = await use(g, "gpt-main", kind="gpt", companion_route="bm-1")
                offset[0] += 1
                await send(a, ha, "gpt-main", "plan the release", subject="发版计划")
                # chatx is down: the job stays queued with backoff and the device gets one notice.
                assert await until(lambda: jobs(app)[0]["attempts"] == 1, timeout=3)
                assert jobs(app)[0]["state"] == "queued" and "companion_unavailable" in jobs(app)[0]["last_error"]
                nudges = lambda: [item for item in app.state.gateway.toast_log if "GPT" in item[2].get("title", "")]
                assert await until(nudges, timeout=3) and nudges()[0][:2] == (AGENT_ID, "toast")
                offset[0] += 11
                app.state.gateway.wake.kick()
                assert await until(lambda: jobs(app)[0]["state"] == "dispatched", timeout=3)
                assert len(nudges()) == 1  # one notice per job, not per retry
                post = received[-1]
                assert post["path"] == "/anx/companion-event" and post["x_anx"] == "1"
                assert post["body"]["project_id"] == "bm-1" and post["body"]["event_type"] == "mail_received"
                assert post["body"]["summary"].startswith("[agent-nexus] 你（gpt-main@")
                assert f'mail_inbox(workspace_id="{hg}")' in post["body"]["summary"]
                # Nothing reports a companion wake: GPT's next call makes it done.
                await g.call_tool("mail_inbox", {"workspace_id": hg})
                job = jobs(app)[0]
                assert job["state"] == "acked" and job["finished_at"] is not None
                assert fake.calls() == []
    finally:
        plugin.shutdown()


async def test_permission_modes_per_address_default_and_device_table(tmp_path, monkeypatch):
    fake = FakeCli(tmp_path / "fake")
    async with gateway(tmp_path, monkeypatch) as (app, port, agents):
        await start_device(tmp_path, port, agents, fake)
        mailbox = app.state.gateway.mailbox
        async with mcp_client(port) as a, mcp_client(port) as b, mcp_client(port) as c:
            ha = await use(a, "alpha")
            hb = await use(b, "builder", kind="codex", resume_id="thread-1")
            hc = await use(c, "writer", kind="claude", resume_id="sess-c")
            await asyncio.sleep(0.6)

            async def wake_once(to, handle, client, kind):
                count = len(fake.calls(kind))
                await send(a, ha, to, "go")
                assert await until(lambda: len(fake.calls(kind)) == count + 1, timeout=3)
                address = mailbox.expand([to], None)[0][0]
                assert await until(lambda: jobs(app, address)[-1]["state"] == "done", timeout=5)
                await client.call_tool("mail_read", {"workspace_id": handle})  # nothing left unread
                await asyncio.sleep(0.6)
                return fake.calls(kind)[-1]["argv"]

            argv = await wake_once("builder", hb, b, "codex")  # unset: bypass
            assert argv[:5] == ["exec", "resume", "thread-1", "--json", "--skip-git-repo-check"]
            assert "--dangerously-bypass-approvals-and-sandbox" in argv
            mailbox.set_address(f"builder@{AGENT_ID}", {"wake_permission": "full-access"})
            argv = await wake_once("builder", hb, b, "codex")
            assert 'sandbox_mode="danger-full-access"' in argv and 'approval_policy="never"' in argv
            assert "--dangerously-bypass-approvals-and-sandbox" not in argv
            mailbox.set_address(f"builder@{AGENT_ID}", {"wake_permission": "inherit"})
            argv = await wake_once("builder", hb, b, "codex")
            assert argv == ["exec", "resume", "thread-1", "--json", "--skip-git-repo-check", argv[-1]]
            mailbox.set_address(f"writer@{AGENT_ID}", {"wake_permission": "inherit"})
            argv = await wake_once("writer", hc, c, "claude")
            assert argv == ["-p", "--resume", "sess-c", "--output-format", "json", argv[-1]]
            # permission_default applies to addresses without their own mode.
            mailbox.set_address(f"writer@{AGENT_ID}", {"wake_permission": None})
            app.state.gateway.mailbox_config["wake"]["permission_default"] = "full-access"
            argv = await wake_once("writer", hc, c, "claude")
            assert "--permission-mode" in argv and "bypassPermissions" in argv
            # The device's translation table (agent-nexus.json wake.permission_flags) overrides the built-in one.
            app.state.gateway.mailbox_config["wake"]["permission_default"] = "bypass"
            fake.wake_config["permission_flags"] = {"claude": {"bypass": ["--custom-bypass"]}}
            argv = await wake_once("writer", hc, c, "claude")
            assert argv == ["-p", "--resume", "sess-c", "--output-format", "json", "--custom-bypass", argv[-1]]


async def test_max_thread_hops_delivers_the_third_hop_without_waking(tmp_path, monkeypatch):
    fake = FakeCli(tmp_path / "fake")
    async with gateway(tmp_path, monkeypatch, {"limits": {"max_thread_hops": 2},
                                               "wake": {"debounce_s": 30}}) as (app, port, agents):
        await start_device(tmp_path, port, agents, fake)
        async with mcp_client(port) as a, mcp_client(port) as b:
            ha = await use(a, "alpha", resume_id="sess-a")
            hb = await use(b, "beta", resume_id="sess-b")
            await asyncio.sleep(0.6)
            first = await send(a, ha, "beta", "hop 0")
            assert first["deliveries"][0]["action"] == "wake_queued"
            await asyncio.sleep(0.6)
            second = await send(b, hb, "alpha", "hop 1", reply_to=first["message_id"])
            assert second["deliveries"][0]["action"] == "wake_queued"
            await asyncio.sleep(0.6)
            third = await send(a, ha, "beta", "hop 2", reply_to=second["message_id"])
            assert third["deliveries"][0]["action"] == "noticed_on_next_call"
            read = (await b.call_tool("mail_read", {"workspace_id": hb, "message_id": third["message_id"]})).data
            assert read["messages"][0]["body"] == "hop 2"  # delivered all the same


async def test_recall_drops_the_message_from_a_pending_job(tmp_path, monkeypatch):
    fake = FakeCli(tmp_path / "fake")
    async with gateway(tmp_path, monkeypatch, {"wake": {"debounce_s": 30}}) as (app, port, agents):
        await start_device(tmp_path, port, agents, fake)
        async with mcp_client(port) as a, mcp_client(port) as b:
            ha = await use(a, "alpha")
            await use(b, "beta", resume_id="sess-b")
            await asyncio.sleep(0.6)
            one = await send(a, ha, "beta", "one")
            two = await send(a, ha, "beta", "two")
            assert jobs(app)[0]["message_ids"] == [one["message_id"], two["message_id"]]
            await a.call_tool("mail_recall", {"workspace_id": ha, "message_id": one["message_id"]})
            assert jobs(app)[0]["message_ids"] == [two["message_id"]] and jobs(app)[0]["state"] == "queued"
            await a.call_tool("mail_recall", {"workspace_id": ha, "message_id": two["message_id"]})
            assert jobs(app)[0]["state"] == "skipped" and jobs(app)[0]["reason"] == "recalled"


async def test_hook_presence_report_sets_the_resume_id(tmp_path, monkeypatch):
    from agent_nexus.agent import HubLink
    from agent_nexus.cli import hook_payload
    hub = HubLink()
    async with gateway(tmp_path, monkeypatch) as (app, port, agents):
        workdir = await start_agent(tmp_path, port, agents, hostname=HOST_A, hub=hub)
        assert "wake/1" in hub.features
        async with mcp_client(port) as b:
            await use(b, "beta")
            payload = hook_payload("claude", json.dumps({"session_id": "sess-hook", "cwd": str(workdir),
                                                         "hook_event_name": "UserPromptSubmit"}))
            assert payload == {"kind": "claude", "cwd": str(workdir), "event": "UserPromptSubmit",
                               "session_id": "sess-hook"}
            result = await hub.call("presence_report", payload)
            assert result["matched"] == f"beta@{AGENT_ID}"
            row = app.state.gateway.mailbox.address_row(f"beta@{AGENT_ID}")
            assert row["resume_id"] == "sess-hook" and row["resume_source"] == "hook"
            codex = hook_payload("codex", json.dumps({"type": "agent-turn-complete", "thread-id": "t-1",
                                                      "cwd": str(workdir)}))
            assert codex["session_id"] == "t-1" and codex["event"] == "turn"
            assert (await hub.call("presence_report", codex))["matched"] is None  # no codex session there
