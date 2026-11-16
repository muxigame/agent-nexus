"""M7 hardening: resume ids and the arguments of a ``.cmd`` shim (design §8.7), wake job retention (§8.10).

npm installs ``claude.cmd`` / ``codex.cmd``; Windows runs a batch file through
cmd.exe, which ``subprocess.list2cmdline`` does not quote for. A resume id
comes from MCP clients (``use_device``) and device hooks, so it is held to a
strict pattern at every entry point, and every argument of a batch executable
is either refused or quoted so cmd.exe takes it literally.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_nexus.federation import RoutingError
from agent_nexus.gateway import _session_meta
from agent_nexus.ids import valid_resume_id
from agent_nexus.link.wake import adapters
from agent_nexus.link.wake.runner import WakeRunner
from agent_nexus.mailbox import Mailbox
from agent_nexus.store import GatewayStore

AGENT = "dev-a"
BAD_IDS = ["abc&echo.INJECTED>marker.txt", 'abc"&calc&"', "abc|whoami", "a b", "abc%PATH%", "-flag", "", "x" * 201,
           "abc\nnext", "abc^", "abc!x!"]
GOOD_IDS = ["0f6b2a6e-7c1d-4b8e-9a3f-2d5c8e1b4a70", "rollout-2026-11-10T08-00-00", "thread_1.2:3"]
windows = pytest.mark.skipif(os.name != "nt", reason="batch executables run through cmd.exe on Windows only")

ECHO_ARGS = "import json, sys\nopen(sys.argv[1], 'w', encoding='utf-8').write(json.dumps(sys.argv[2:]))\n"


def fake_cmd(tmp_path: Path, name: str = "claude") -> tuple[Path, Path]:
    """A ``<name>.cmd`` that hands its arguments to Python, which records them (no real CLI)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "echo_args.py"
    script.write_text(ECHO_ARGS, encoding="utf-8")
    seen = tmp_path / "seen.json"
    shim = bin_dir / f"{name}.cmd"
    shim.write_text(f'@"{sys.executable}" "{script}" "{seen}" %*\r\n', encoding="utf-8")
    return shim, seen


def test_resume_id_pattern():
    assert all(valid_resume_id(item) for item in GOOD_IDS)
    assert not any(valid_resume_id(item) for item in BAD_IDS)
    assert not valid_resume_id(None) and not valid_resume_id(12)


@pytest.mark.parametrize("bad", BAD_IDS[:-1])
def test_build_argv_refuses_unsafe_resume_ids(bad):
    config = {"executables": {"claude": [sys.executable, "-c", "pass"]}}
    if not bad:
        assert "--continue" in adapters.build_argv("claude", {"resume_id": bad, "prompt": "hi"}, config)
        return
    with pytest.raises(adapters.WakeError):
        adapters.build_argv("claude", {"resume_id": bad, "prompt": "hi"}, config)


@pytest.mark.parametrize("bad", ["abc&echo.INJECTED>marker.txt", 'abc"&calc&"', "a b"])
def test_use_device_refuses_unsafe_resume_ids(bad):
    with pytest.raises(RoutingError, match="resume_id"):
        _session_meta("worker", "claude", bad, None, "auto")
    assert _session_meta("worker", "claude", GOOD_IDS[0], None, "auto")["resume_id"] == GOOD_IDS[0]


def test_presence_report_and_address_set_refuse_unsafe_ids(tmp_path):
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    mailbox = Mailbox(store)
    workdir = str(tmp_path / "proj")
    bound = mailbox.bind("h_" + "1" * 30, AGENT, workdir=workdir,
                         meta={"session_name": "worker", "kind": "claude"}, client_id=None)
    address = bound["address"]
    with pytest.raises(RoutingError, match="session_id"):
        mailbox.presence_report(AGENT, {"kind": "claude", "cwd": workdir, "session_id": BAD_IDS[0]})
    with pytest.raises(RoutingError, match="resume_id"):
        mailbox.set_address(address, {"resume_id": BAD_IDS[1]})
    assert mailbox.address_row(address).get("resume_id") is None
    assert mailbox.presence_report(AGENT, {"kind": "claude", "cwd": workdir, "session_id": GOOD_IDS[0]})["matched"]
    assert mailbox.address_row(address)["resume_id"] == GOOD_IDS[0]


def test_session_wake_refuses_unsafe_resume_id(tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    marker = workdir / "marker.txt"
    runner = WakeRunner(tmp_path, {"executables": {"claude": [sys.executable, "-c", "pass"]}})
    result = asyncio.run(runner.wake({"job_id": "w_1", "adapter": "claude", "workdir": str(workdir),
                                      "resume_id": f"abc&echo.INJECTED>{marker}", "prompt": "hi"}))
    assert result["accepted"] is False and "resume_id" in result["error"]
    assert not marker.exists() and not runner.procs


@windows
def test_cmd_shim_takes_metacharacters_literally(tmp_path, monkeypatch):
    """The reproduction of the finding: nothing after ``&`` runs, the CLI gets each argument whole."""
    shim, seen = fake_cmd(tmp_path)
    monkeypatch.chdir(tmp_path)
    marker = tmp_path / "marker.txt"
    prompt = 'mail from x: "hi"&echo.INJECTED>marker.txt|(boom)^ 100% done!'
    config = {"executables": {"claude": str(shim)},
              "claude": ["{claude}", "-p", "--resume", "{resume_id}", "--add-dir", "{workdir}", "{prompt}",
                         "tag&echo.INJECTED>marker.txt"]}
    workdir = str(tmp_path / "dir with space&(x)") + "\\"
    argv = adapters.build_argv("claude", {"resume_id": GOOD_IDS[0], "prompt": prompt, "workdir": workdir}, config)
    done = subprocess.run(adapters.popen_args(argv), cwd=tmp_path, capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert not marker.exists()
    got = json.loads(seen.read_text(encoding="utf-8"))
    assert got == ["-p", "--resume", GOOD_IDS[0], "--add-dir", workdir, adapters.safe_prompt(prompt, [str(shim)]),
                   "tag&echo.INJECTED>marker.txt"]
    # list2cmdline (what subprocess does with a list) would have run the injected command.
    assert "&echo" in subprocess.list2cmdline(["x.cmd", "a&echo.x"])


@windows
def test_cmd_shim_keeps_quoted_permission_flags(tmp_path, monkeypatch):
    shim, seen = fake_cmd(tmp_path, "codex")
    monkeypatch.chdir(tmp_path)
    argv = adapters.build_argv("codex", {"resume_id": GOOD_IDS[1], "prompt": "check mail", "permission": "full-access"},
                               {"executables": {"codex": str(shim)}})
    done = subprocess.run(adapters.popen_args(argv), cwd=tmp_path, capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr
    got = json.loads(seen.read_text(encoding="utf-8"))
    assert got[-5:] == ["-c", 'sandbox_mode="danger-full-access"', "-c", 'approval_policy="never"', "check mail"]


@windows
@pytest.mark.parametrize("arg", ["50%", "a\r\nb", 'x"&calc&"y'])
def test_cmd_shim_refuses_what_cannot_be_quoted(tmp_path, arg):
    shim, _ = fake_cmd(tmp_path)
    config = {"executables": {"claude": str(shim)}, "claude": ["{claude}", "--resume", "{resume_id}", arg]}
    with pytest.raises(adapters.WakeError):
        adapters.build_argv("claude", {"resume_id": GOOD_IDS[0], "prompt": "hi"}, config)


@windows
def test_cmd_shim_carries_quote_state_across_arguments(tmp_path, monkeypatch):
    """An odd number of ``"`` in one template part must not expose the next argument (the prompt) to cmd.exe."""
    shim, seen = fake_cmd(tmp_path)
    monkeypatch.chdir(tmp_path)
    marker = tmp_path / "marker.txt"
    config = {"executables": {"claude": str(shim)},
              "claude": ["{claude}", "-p", "--resume", "{resume_id}", "--append-system-prompt", 'reply "briefly',
                         "{prompt}"]}
    params = {"resume_id": GOOD_IDS[0], "prompt": "hi&echo INJECTED>marker.txt"}
    with pytest.raises(adapters.WakeError):
        adapters.build_argv("claude", params, config)
    # popen_args refuses the line on its own too, whoever built the argv.
    argv = [str(shim), "--append-system-prompt", 'reply "briefly', "hi&echo INJECTED>marker.txt"]
    with pytest.raises(adapters.WakeError):
        adapters.popen_args(argv)
    assert not marker.exists() and not seen.exists()
    # An even number of quotes still passes, literally.
    config["claude"][5] = 'reply "briefly"'
    argv = adapters.build_argv("claude", params, config)
    done = subprocess.run(adapters.popen_args(argv), cwd=tmp_path, capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert not marker.exists()
    assert json.loads(seen.read_text(encoding="utf-8"))[-2:] == ['reply "briefly"', "hi&echo INJECTED>marker.txt"]


@pytest.mark.parametrize("arg", ['reply "briefly', 'a"b"c"d', '"'])
def test_batch_arg_refuses_unbalanced_quote_state(arg):
    with pytest.raises(adapters.WakeError):
        adapters._batch_arg(arg)
    assert adapters._batch_unsafe(" ".join(['x.cmd', '"a \\"b"', '"c&d"']))


def test_retention_purges_finished_wake_jobs(tmp_path):
    now = [1_000_000_000.0]
    store = GatewayStore(tmp_path / "gateway.sqlite3", clock=lambda: now[0])
    mailbox = Mailbox(store)
    day = 86400.0
    rows = [("old_done", "a1@dev-a", "succeeded", now[0] - 20 * day, now[0] - 20 * day),
            ("old_failed", "a2@dev-a", "failed", now[0] - 30 * day, None),
            ("recent_done", "a3@dev-a", "succeeded", now[0] - 2 * day, now[0] - 2 * day),
            ("old_active", "a4@dev-a", "waiting_device", now[0] - 40 * day, None)]
    with store._lock:
        mailbox._db().executemany(
            "INSERT INTO wake_jobs(id, address, agent_id, adapter, state, message_ids, next_at, created_at, expires_at, "
            "finished_at) VALUES (?, ?, 'dev-a', 'claude', ?, '[]', ?, ?, ?, ?)",
            [(job, addr, state, created, created, created + day, fin) for job, addr, state, created, fin in rows])
    removed = mailbox.retention()
    assert removed["wake_jobs"] == 2
    with store._lock:
        left = sorted(row[0] for row in mailbox._db().execute("SELECT id FROM wake_jobs").fetchall())
    assert left == ["old_active", "recent_done"]
