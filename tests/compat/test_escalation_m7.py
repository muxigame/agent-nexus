"""Milestone M7, no network: unread escalation (decision D9, §8.11), the device-side adapters,
the runner's restart adoption and ``anx wake doctor`` against fake CLIs.

The clock is injected (``GatewayStore(clock=...)``) and the thresholds stay at
their defaults (1 h, 24 h, 30 d): time is moved, not waited for. The device is
a fake peer that accepts every ``session_wake``.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_nexus.federation import Hub
from agent_nexus.gateway import Gateway
from agent_nexus.link.wake import adapters, doctor
from agent_nexus.link.wake.runner import WakeRunner
from agent_nexus.mailbox import ME, mailbox_config
from agent_nexus.store import GatewayStore

from test_wake_m7 import FakeCli

DEVICE = "pc-main"
HOUR, DAY = 3600.0, 86400.0


class FakePeer:
    def __init__(self, agent_id: str = DEVICE):
        self.agent_id = agent_id
        self.features = frozenset({"session_wake/1"})
        self.catalog = {"workspaces": {"project": {"path": "/work", "mcp": "device"}}, "mcps": {"device": {"tools": []}}}
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method: str, params: dict):
        self.calls.append((method, params))
        return {"accepted": True}


class World:
    def __init__(self, path: Path, mailbox: dict | None = None, now: float = 1_000_000.0):
        self.now = [now]
        self.store = GatewayStore(path, clock=lambda: self.now[0])
        self.hub = Hub()
        self.gateway = Gateway(self.hub, self.store, mailbox=mailbox)
        self.mailbox = self.gateway.mailbox
        self.wake = self.gateway.wake

    def bind(self, name: str, kind: str = "claude", resume_id: str | None = "sess") -> str:
        handle = f"h-{name}-{int(self.now[0])}"
        self.store.put_handle(handle, DEVICE, "project")
        meta = {"session_name": name, "kind": kind, **({"resume_id": resume_id} if resume_id else {})}
        return self.mailbox.bind(handle, DEVICE, workdir="/work", meta=meta, client_id=None)["address"]

    def online(self) -> FakePeer:
        self.hub.peers[DEVICE] = peer = FakePeer()
        return peer

    def advance(self, seconds: float) -> None:
        self.now[0] += seconds

    def jobs(self, address: str | None = None) -> list[dict]:
        return self.wake.jobs(address)

    def inbox(self, address: str) -> list[dict]:
        return self.mailbox.inbox(address, unread_only=False)["messages"]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path / "gateway.sqlite3")


# -- level 1: one more wake ---------------------------------------------------------------

async def test_rewake_once_after_an_hour_even_when_active(world):
    peer = world.online()
    alpha, beta = world.bind("alpha"), world.bind("beta")
    sent = world.mailbox.send(alpha, beta, "still there?", subject="进度")
    assert sent["deliveries"][0]["action"] == "noticed_on_next_call"  # beta is active
    assert world.jobs() == []
    world.advance(HOUR - 1)
    assert world.gateway.escalation.scan()["rewake"] == 0
    world.advance(2)
    world.mailbox.touch(beta)  # active again: level 1 wakes all the same (wake=always)
    assert world.gateway.escalation.scan()["rewake"] == 1
    assert len(world.jobs(beta)) == 1 and world.jobs(beta)[0]["state"] == "queued"
    world.advance(10)  # the debounce
    await world.wake.tick()
    assert [method for method, _ in peer.calls] == ["session_wake"] and world.jobs(beta)[0]["state"] == "dispatched"
    assert world.mailbox.status(sent["message_id"])["recipients"][0]["status"] == "woken"
    # Repeated scans do not wake again.
    world.advance(HOUR)
    assert world.gateway.escalation.scan()["rewake"] == 0
    assert len(world.jobs(beta)) == 1


def test_level_two_tells_the_owner_once_per_recipient_and_scan(world):
    alpha, beta, gamma = world.bind("alpha"), world.bind("beta"), world.bind("gamma", kind="codex")
    world.mailbox.send(alpha, beta, "one", subject="第一封")
    world.mailbox.send(alpha, beta, "two", subject="第二封")
    world.mailbox.send(alpha, gamma, "three", subject="第三封")
    world.mailbox.send(alpha, ME, "for the owner", subject="给我")
    world.advance(DAY + 1)
    done = world.gateway.escalation.scan()
    assert done["notify_owner"] == 2
    system = [m for m in world.inbox(ME) if m["kind"] == "system"]
    assert len(system) == 2  # two recipients, two mails; two messages to beta merged into one
    beta_notice = next(m for m in system if "beta@" in m["subject"])
    body = world.mailbox.read(ME, message_id=beta_notice["id"], mark_read=False)["messages"][0]["body"]
    assert "2 封邮件" in body and "第一封" in body and "第二封" in body
    # Mail to "me" is never forwarded to "me"; nothing repeats on the next scan.
    assert world.gateway.escalation.scan()["notify_owner"] == 0
    assert len([m for m in world.inbox(ME) if m["kind"] == "system"]) == 2


async def test_level_three_expires_cancels_the_job_and_tells_the_sender(world):
    alpha, beta = world.bind("alpha"), world.bind("beta")
    world.hub.peers[DEVICE] = FakePeer()
    world.advance(200)  # beta idle
    first = world.mailbox.send(alpha, beta, "woken one", subject="已唤醒")
    world.advance(10)
    await world.wake.tick()
    assert world.mailbox.status(first["message_id"])["recipients"][0]["status"] == "woken"
    world.mailbox.read(beta, message_id=first["message_id"], mark_read=False)
    world.wake.report(DEVICE, {"job_id": world.jobs(beta)[0]["id"], "state": "done", "exit_code": 0})
    # A later message sits in a queued job when the 30 days run out.
    world.advance(29 * DAY)
    pending = world.mailbox.send(alpha, beta, "pending one", subject="排队中", wake="always")
    assert world.jobs(beta)[-1]["state"] == "queued"
    world.advance(DAY + 1)
    done = world.gateway.escalation.scan()
    assert done["expired"] == 1
    status = world.mailbox.status(first["message_id"])["recipients"][0]
    assert status["status"] == "expired" and status["woken_at"] is not None and status["escalation_level"] == 3
    assert world.mailbox.unread_count(beta) == 1  # the newer one is not 30 days old
    to_alpha = [m for m in world.inbox(alpha) if m["kind"] == "system"]
    assert len(to_alpha) == 1 and "已过期未读" in to_alpha[0]["snippet"]
    world.advance(30 * DAY)
    world.gateway.escalation.scan()
    assert world.jobs(beta)[-1]["state"] == "skipped" and world.jobs(beta)[-1]["reason"] == "expired"
    assert world.mailbox.status(pending["message_id"])["recipients"][0]["status"] == "expired"


def test_reading_stops_escalation_and_a_restart_does_not_repeat_levels(tmp_path):
    first = World(tmp_path / "gateway.sqlite3")
    alpha, beta, gamma = first.bind("alpha"), first.bind("beta"), first.bind("gamma")
    read_me = first.mailbox.send(alpha, beta, "read it", subject="会被读")
    first.mailbox.send(alpha, gamma, "ignored", subject="没人读")
    first.advance(HOUR / 2)
    first.mailbox.read(beta, message_id=read_me["message_id"])
    first.advance(HOUR)
    assert first.gateway.escalation.scan() == {"rewake": 1, "notify_owner": 0, "expired": 0}  # gamma only
    first.store.close()
    # Same database, new gateway: level 1 is not done twice.
    second = World(tmp_path / "gateway.sqlite3", now=first.now[0] + 60)
    assert second.gateway.escalation.scan() == {"rewake": 0, "notify_owner": 0, "expired": 0}
    second.advance(DAY)
    assert second.gateway.escalation.scan() == {"rewake": 0, "notify_owner": 1, "expired": 0}
    third = World(tmp_path / "gateway.sqlite3", now=second.now[0] + 60)
    assert third.gateway.escalation.scan() == {"rewake": 0, "notify_owner": 0, "expired": 0}
    assert len(third.jobs(gamma)) == 1


def test_null_thresholds_turn_levels_off(tmp_path):
    config = {"escalation": {"rewake_after_s": None, "notify_owner_after_s": None, "expire_after_days": None}}
    world = World(tmp_path / "gateway.sqlite3", mailbox=config)
    world.online()
    alpha, beta = world.bind("alpha"), world.bind("beta")
    world.mailbox.send(alpha, beta, "forever")
    world.advance(400 * DAY)
    assert world.gateway.escalation.scan() == {"rewake": 0, "notify_owner": 0, "expired": 0}
    assert world.jobs() == [] and world.mailbox.unread_count(beta) == 1
    # Only one level off: the others still run.
    partial = World(tmp_path / "other.sqlite3", mailbox={"escalation": {"rewake_after_s": None}})
    partial.online()
    a, b = partial.bind("alpha"), partial.bind("beta")
    partial.mailbox.send(a, b, "x")
    partial.advance(DAY + 1)
    assert partial.gateway.escalation.scan() == {"rewake": 0, "notify_owner": 1, "expired": 0}
    assert partial.jobs() == []


def test_wake_and_escalation_config_is_checked():
    config, warnings = mailbox_config({"wake": {"permission_default": "inherit", "adapters": {"companion": False},
                                                "cooldown_s": None},
                                       "escalation": {"rewake_after_s": None},
                                       "retention": {"unread_days": 45}})
    assert config["wake"]["permission_default"] == "inherit" and config["wake"]["adapters"]["companion"] is False
    assert config["wake"]["adapters"]["claude"] is True and config["escalation"]["rewake_after_s"] is None
    assert config["escalation"]["expire_after_days"] == 45 and any("unread_days" in item for item in warnings)
    from mcpkit.config import ConfigError
    for bad in ({"wake": {"permission_default": "yolo"}}, {"wake": {"debounce_s": None}},
                {"wake": {"adapters": {"claude": "yes"}}}, {"escalation": {"rewake_after_s": "1h"}}):
        with pytest.raises(ConfigError):
            mailbox_config(bad)
    assert "mailbox.wake.adapters.fax" in mailbox_config({"wake": {"adapters": {"fax": True}}})[1][0]


def test_unwakeable_addresses_are_noticed(world):
    alpha = world.bind("alpha")
    other = world.bind("tool", kind="other")
    gpt = world.bind("gpt-main", kind="gpt")  # no companion_route
    claude = world.bind("free", kind="claude", resume_id=None)  # workdir is enough
    world.advance(200)
    actions = {item["to"]: item["action"] for item in world.mailbox.send(alpha, [other, gpt, claude], "hi")["deliveries"]}
    assert actions == {other: "noticed_on_next_call", gpt: "noticed_on_next_call", claude: "wake_waiting_device"}
    disabled = World(world.store.path.with_name("disabled.sqlite3"), mailbox={"wake": {"enabled": False}})
    a, b = disabled.bind("alpha"), disabled.bind("beta")
    disabled.advance(200)
    assert disabled.mailbox.send(a, b, "x")["deliveries"][0]["action"] == "noticed_on_next_call"


# -- device side: adapters, runner, doctor -------------------------------------------------------

def test_adapter_templates_and_lookup(tmp_path):
    exe = {"executables": {"claude": ["claude-bin"], "codex": "codex-bin"}}
    assert adapters.build_argv("claude", {"prompt": "p", "permission": "bypass"}, exe) == \
        ["claude-bin", "-p", "--continue", "--output-format", "json", "--dangerously-skip-permissions", "p"]
    assert adapters.build_argv("codex", {"prompt": "p", "permission": "inherit"}, exe) == \
        ["codex-bin", "exec", "resume", "--last", "--json", "--skip-git-repo-check", "p"]
    custom = {**exe, "claude": ["{claude}", "--resume", "{resume_id}", "--cwd", "{workdir}", "{prompt}"]}
    assert adapters.build_argv("claude", {"resume_id": "r1", "prompt": "p", "workdir": "/w"}, custom) == \
        ["claude-bin", "--resume", "r1", "--cwd", "/w", "p"]
    with pytest.raises(adapters.WakeError):
        adapters.build_argv("claude", {"prompt": "p", "permission": "yolo"}, exe)
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off\n", encoding="utf-8")
    argv = adapters.build_argv("claude", {"resume_id": "r", "prompt": 'line 1\n"50%" done'},
                               {"executables": {"claude": str(shim)}})
    assert argv[-1] == "line 1 '50' done"  # cmd.exe: one line, no % or "
    env = adapters.wake_env()
    assert str(Path.home() / ".local" / "bin") in env["PATH"]


async def test_runner_survives_a_manager_restart_and_reports_later(tmp_path):
    fake = FakeCli(tmp_path / "fake")
    fake.control(claude={"sleep": 1.0})
    workdir = tmp_path / "work"
    workdir.mkdir()
    config = {"executables": fake.executables()}
    first = WakeRunner(tmp_path / "root", config)
    params = {"job_id": "w_restart", "adapter": "claude", "resume_id": "r", "prompt": "p", "workdir": str(workdir),
              "permission": "bypass"}
    started = await first.wake(params)
    assert started["accepted"] and started["pid"]
    # The same job again while it runs (a gateway retry): no second process.
    again = await first.wake(params)
    assert again == {"accepted": True, "pid": started["pid"], "already_running": True}
    first.procs.clear()  # the manager restarts: the child is no longer its own
    reports: list[dict] = []

    async def report(args):
        reports.append(args)

    second = WakeRunner(tmp_path / "root", config, report=report)
    second.start()
    try:
        for _ in range(200):
            if reports:
                break
            await asyncio.sleep(0.05)
    finally:
        await second.close()
    assert reports == [{"job_id": "w_restart", "state": "done", "exit_code": None}]
    assert not (tmp_path / "root" / ".anx" / "wake" / "w_restart.json").exists()
    assert len(fake.calls("claude")) == 1
    missing = await second.wake({**params, "job_id": "w_nowhere", "workdir": str(tmp_path / "gone")})
    assert not missing["accepted"] and "does not exist" in missing["error"]


def test_doctor_with_fake_clis_and_a_fake_companion(tmp_path):
    fake = FakeCli(tmp_path / "fake")
    fake.control(claude={"session_id": "doc-claude"}, codex={"session_id": "doc-codex"})
    posted = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            posted.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            data = json.dumps({"status": "ok", "delivered": True, "event_id": "evt-doc"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    plugin = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=plugin.serve_forever, daemon=True).start()
    lines: list[str] = []
    try:
        code = doctor.run(tmp_path / "root", {"executables": fake.executables()}, kinds=["claude", "codex", "gpt"],
                          timeout=30, endpoint=f"http://127.0.0.1:{plugin.server_port}", out=lines.append)
    finally:
        plugin.shutdown()
    assert code == 0, lines
    results = doctor.load_results(tmp_path / "root")
    assert results["claude"]["ok"] and results["codex"]["ok"] and results["gpt"]["ok"]
    assert results["claude"]["version"] == "claude 9.9.9 (fake)" and results["claude"]["session_id"] == "doc-claude"
    resumed = [call["argv"] for call in fake.calls("codex") if "resume" in call["argv"]]
    assert resumed and resumed[0][:3] == ["exec", "resume", "doc-codex"]
    assert "--dangerously-bypass-approvals-and-sandbox" in resumed[0]
    assert posted and posted[0]["event_type"] == "wake_doctor"
    # inherit: a warning; a run that never exits (an interactive prompt): a failure, not a hang.
    fake.control(claude={"sleep": 5})
    lines.clear()
    code = doctor.run(tmp_path / "root", {"executables": fake.executables()}, kinds=["claude"],
                      permission="inherit", timeout=1.5, out=lines.append)
    assert code == 1
    text = "\n".join(lines)
    assert "warning: inherit" in text and "interactive prompt" in text
    assert doctor.load_results(tmp_path / "root")["codex"]["ok"]  # earlier results are kept
