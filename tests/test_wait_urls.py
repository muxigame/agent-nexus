"""Wait URLs and per-client wait caps (?wait_max=): a real gateway, two real devices with the real device plugin,
this computer's console relaying /wait/<token> (the harness of test_remote_cli_e2e), plus unit tests of the tokens,
the stream writer and the log redaction."""
from __future__ import annotations

import asyncio
import logging
import sys
import time
import types

import httpx
import pytest

from agent_nexus import remote_cli, wait_urls
from agent_nexus.wait_urls import RedactAccessLog, Streamer, WaitTokens

from test_remote_cli_e2e import network  # noqa: F401 - the fixture

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell commands")


def call(base: str, path: str, tool: str, arguments: dict) -> dict:
    mcp = remote_cli.Mcp(base, path)
    try:
        return mcp.call("tooluse", {"tool": tool, "mcp_id": "pc-b/device", "arguments": arguments})
    finally:
        mcp.close()


async def read_all(url: str, headers: dict | None = None, *, limit_s: float = 30.0) -> tuple[int, str]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(limit_s)) as client:
        async with client.stream("GET", url, headers=headers or {}) as response:
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
            return response.status_code, body.decode()


# -- end to end ---------------------------------------------------------------------------------------------------
async def test_wait_max_lifts_the_cap_with_segmented_device_waits(network):
    gateway = network.gateway
    gateway.tool_wait_max_s = 1.0
    base = network.env["ANX_CONSOLE_URL"]
    command = {"cmd": "echo one; sleep 2.5; echo two; exit 3", "yield_time_ms": 20000}
    # without wait_max: one device call of at most tool_wait_max_s, a running session with a wait URL
    raw = await asyncio.to_thread(call, base, "/mcp", "exec_command", dict(command))
    fields = raw["_meta"]["anx/exec"]
    assert fields.get("exit_code") is None and fields["session_id"]
    assert fields["wait_url"].startswith(base + "/wait/")
    assert "wait: curl -sN " + fields["wait_url"] in raw["content"][0]["text"]
    # the same job again: the same URL (idempotent per job and minter)
    again = await asyncio.to_thread(call, base, "/mcp", "exec_wait", {"session_id": fields["session_id"],
                                                                     "timeout_ms": 10})
    assert again["_meta"]["anx/exec"].get("wait_url") in (None, fields["wait_url"])
    # the wait URL streams the rest and ends with the exit code (through this console's loopback relay)
    status, body = await read_all(fields["wait_url"])
    assert status == 200 and "two\n" in body and body.endswith("[anx] exit 3\n")

    # with wait_max (the entry 延长 MCP 等待 writes): one MCP call waits through several device calls
    started = time.monotonic()
    raw = await asyncio.to_thread(call, base, "/mcp?wait_max=30", "exec_command", dict(command))
    fields = raw["_meta"]["anx/exec"]
    assert fields["exit_code"] == 3, raw
    assert raw["content"][0]["text"].endswith("\none\ntwo\n")
    assert time.monotonic() - started >= 2.0
    # tools/list carries the same query: the descriptions name this client's cap
    mcp = remote_cli.Mcp(base, "/mcp?wait_max=30")
    try:
        tools = await asyncio.to_thread(mcp.request, "tools/list", {}, 30.0)
    finally:
        mcp.close()
    described = {tool["name"]: tool.get("description", "") for tool in (tools.get("result") or tools)["tools"]}
    assert "max 30000" in described["exec_command"]


async def test_wait_url_rules_owner_and_device_tokens(network):
    gateway = network.gateway
    gateway.tool_wait_max_s = 1.0
    origin = f"http://127.0.0.1:{network.gateway_port}"
    base = network.env["ANX_CONSOLE_URL"]
    raw = await asyncio.to_thread(call, base, "/mcp", "exec_command", {"cmd": "echo hi; sleep 30", "yield_time_ms": 300})
    fields = raw["_meta"]["anx/exec"]
    token = fields["wait_url"].rsplit("/", 1)[1]
    # minted through pc-a's 本机 MCP: on the gateway it needs pc-a's credential
    status, _ = await read_all(f"{origin}/wait/{token}")
    assert status == 404
    status, _ = await read_all(f"{origin}/wait/{'x' * 32}")
    assert status == 404
    # pc-a locks: its tokens die, also on the local relay
    peer = gateway.hub.peers["pc-a"]
    peer.locked = True
    gateway.wait_urls.revoke_device("pc-a")
    status, _ = await read_all(fields["wait_url"])
    assert status == 404
    peer.locked = False

    # an owner token (pure capability) streams straight from the gateway; quiet=1 has no keepalives
    owner = gateway.wait_urls.mint("exec", minter="owner", agent_id="pc-b", mcp="device",
                                   session_id=fields["session_id"], job=fields["job"], since=0)
    async with httpx.AsyncClient(timeout=10) as client:
        async with client.stream("GET", f"{origin}/wait/{owner}") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/plain")
            assert response.headers["cache-control"] == "no-store"
            first = await anext(response.aiter_bytes())
            assert b"hi" in first
        # the client left: the gateway's stream and its device call end
        for _ in range(100):
            entry = gateway.wait_urls.lookup(owner)
            if entry is not None and entry.streams == 0 and not gateway.hub.peers["pc-b"].pending:
                break
            await asyncio.sleep(0.05)
        assert entry.streams == 0 and not gateway.hub.peers["pc-b"].pending
    # killed: the stream's last line says so
    await asyncio.to_thread(call, base, "/mcp", "exec_kill", {"session_id": fields["session_id"]})
    status, body = await read_all(f"{origin}/wait/{owner}?quiet=1")
    assert status == 200 and body.endswith("[anx] killed (signal 15)\n"), body


async def test_the_follow_does_not_consume_and_the_mcp_client_still_gets_the_output(network):
    network.gateway.tool_wait_max_s = 1.0
    base = network.env["ANX_CONSOLE_URL"]
    raw = await asyncio.to_thread(call, base, "/mcp", "exec_command",
                                  {"cmd": "sleep 0.5; echo late; exit 0", "yield_time_ms": 250})
    fields = raw["_meta"]["anx/exec"]
    status, body = await read_all(fields["wait_url"])
    assert body == "late\n[anx] exit 0\n"
    waited = await asyncio.to_thread(call, base, "/mcp", "exec_wait", {"session_id": fields["session_id"],
                                                                      "timeout_ms": 2000})
    assert waited["_meta"]["anx/exec"]["exit_code"] == 0 and waited["content"][0]["text"].endswith("\nlate\n")


# -- units ----------------------------------------------------------------------------------------------------------
def test_tokens_are_idempotent_hashed_and_expire_after_the_end(monkeypatch):
    tokens = WaitTokens()
    first = tokens.mint("exec", minter="c1", agent_id="pc", mcp="device", session_id=1000, job="ab")
    assert first == tokens.mint("exec", minter="c1", agent_id="pc", mcp="device", session_id=1000, job="ab")
    assert first != tokens.mint("exec", minter="c2", agent_id="pc", mcp="device", session_id=1000, job="ab")
    assert len(first) >= 32 and first not in repr(tokens._entries)  # only the digest is kept
    entry = tokens.lookup(first)
    assert entry is not None and tokens.lookup(first[:-1] + ("A" if first[-1] != "A" else "B")) is None
    tokens.ended("exec", "pc", session_id=1000)
    now = time.time()
    monkeypatch.setattr(wait_urls.time, "time", lambda: now + wait_urls.TTL_AFTER_END_S + 1)
    assert tokens.lookup(first) is None
    tokens.prune()
    assert tokens.lookup(first) is None


def test_device_tokens_die_with_the_device():
    tokens = WaitTokens()
    mine = tokens.mint("exec", minter="device:pc-a", agent_id="pc-b", mcp="device", session_id=1, job="j")
    owner = tokens.mint("exec", minter="owner", agent_id="pc-b", mcp="device", session_id=1, job="j")
    assert tokens.lookup(mine).device == "pc-a"
    assert tokens.revoke_device("pc-a") == 1
    assert tokens.lookup(mine) is None and tokens.lookup(owner) is not None


class FakePeer:
    def __init__(self, tools, answers):
        self.catalog = {"mcps": {"device": {"tools": [{"name": name} for name in tools]}}}
        self.answers = list(answers)
        self.calls = []
        self.locked = False

    async def call(self, method, params):
        self.calls.append(params)
        answer = self.answers.pop(0)
        if isinstance(answer, float):
            await asyncio.sleep(answer)
            answer = self.answers.pop(0)
        return {"structuredContent": answer, "content": []}


def fake_gateway(peer, minter_peer=None):
    hub = types.SimpleNamespace(peers={"pc-b": peer, **({"pc-a": minter_peer} if minter_peer else {})})
    return types.SimpleNamespace(hub=hub, tool_wait_max_s=1.0, wait_urls=WaitTokens())


async def run_stream(gateway, entry, **kwargs) -> str:
    out: list[str] = []

    async def write(text):
        out.append(text)
    await Streamer(gateway, entry, write, **kwargs).run()
    return "".join(out)


async def test_old_devices_fall_back_to_exec_wait_and_keepalives_are_written():
    peer = FakePeer(["exec_command", "exec_wait"], [
        {"output": "a", "session_id": 7}, 0.05, {"output": "", "session_id": 7}, {"output": "b\n", "exit_code": 0}])
    entry = wait_urls.WaitEntry(kind="exec", agent_id="pc-b", minter="owner", mcp="device", session_id=7)
    text = await run_stream(fake_gateway(peer), entry, keepalive_s=0.01)
    assert [call["tool"] for call in peer.calls] == ["exec_wait"] * 3
    assert text.startswith("a\n[anx] still running (0m)\n") and text.endswith("b\n[anx] exit 0\n")
    assert entry.ended_at is not None


async def test_exec_tail_stream_and_a_locked_minter():
    peer = FakePeer(["exec_command", "exec_tail"], [
        {"output": "x", "seq": 2, "running": True}, {"output": "", "seq": 2, "running": False, "exit_code": 5}])
    entry = wait_urls.WaitEntry(kind="exec", agent_id="pc-b", minter="owner", mcp="device", session_id=7, job="j")
    assert await run_stream(fake_gateway(peer), entry, quiet=True) == "x\n[anx] exit 5\n"
    assert [call["arguments"]["since"] for call in peer.calls] == [0, 2]
    minter = types.SimpleNamespace(locked=True)
    entry = wait_urls.WaitEntry(kind="exec", agent_id="pc-b", minter="device:pc-a", mcp="device", session_id=7, job="j")
    assert await run_stream(fake_gateway(peer, minter), entry) == "[anx] revoked\n"
    gone = wait_urls.WaitEntry(kind="exec", agent_id="pc-b", minter="device:pc-c", mcp="device", session_id=7, job="j")
    assert "device offline" in await run_stream(fake_gateway(peer), gone)


async def test_copy_stream_reports_progress_and_the_outcome():
    snaps = [{"state": "running", "files": {"done": 1, "total": 3}, "bytes": {"done": 10, "total": 30}},
             {"state": "completed", "files": {"done": 3, "total": 3}, "bytes": {"done": 3 * 1024 * 1024, "total": 30}}]

    class Copies:
        async def status(self, job_id, wait_ms, cap_ms=None):
            assert job_id == "pc-a:x" and cap_ms == 1000
            return snaps.pop(0)
    gateway = fake_gateway(FakePeer([], []))
    gateway.file_copy = Copies()
    entry = wait_urls.WaitEntry(kind="copy", agent_id="pc-a", minter="owner", job_id="pc-a:x")
    text = await run_stream(gateway, entry)
    assert text == "[anx] copying 1/3 files, 10 B/30 B\n[anx] copy done: 3 files, 3.0 MB\n"
    assert not text.splitlines()[0].startswith(wait_urls.FINAL_PREFIXES)  # a cut stream never looks finished


def test_access_log_never_shows_a_token():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:5", "GET", "/wait/SECRETTOKEN123456789012?quiet=1", "1.1", 200), None)
    RedactAccessLog().filter(record)
    assert "SECRET" not in record.getMessage() and "/wait/***" in record.getMessage()
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "GET /media/abc", None, None)
    RedactAccessLog().filter(record)
    assert record.getMessage() == "GET /media/***"


def test_clamp_keeps_request_user_input_at_the_gateway_cap():
    from agent_nexus.federation import Hub
    from agent_nexus.gateway import Gateway
    gateway = Gateway(Hub(), tool_wait_max_s=50)
    arguments = {"timeout_s": 600}
    gateway.clamp_waits("request_user_input", arguments, 86100)
    assert arguments["timeout_s"] == 50
    arguments = {"yield_time_ms": 3_600_000}
    assert gateway.clamp_waits("exec_command", arguments, 86100) == {}
    assert arguments["yield_time_ms"] == 3_600_000
    arguments = {"yield_time_ms": 3_600_000}
    gateway.clamp_waits("exec_command", arguments)
    assert arguments["yield_time_ms"] == 50_000
    assert gateway.client_wait_max_s() == 50  # outside a request: the default


def test_local_mcp_forwards_only_a_numeric_wait_max():
    from agent_nexus.local_mcp import wait_max_of
    assert wait_max_of({"query_string": b"wait_max=86100&x=1"}) == 86100
    assert wait_max_of({"query_string": b"wait_max=86401"}) is None
    assert wait_max_of({"query_string": b"wait_max=1e9"}) is None
    assert wait_max_of({"query_string": b"other=5"}) is None
    assert wait_max_of({}) is None


def test_a_relock_makes_a_new_token_and_the_old_one_stays_dead():
    tokens = WaitTokens()
    key = dict(minter="device:pc-a", agent_id="pc-b", mcp="device", session_id=1, job="j")
    old = tokens.mint("exec", **key)
    tokens.revoke_device("pc-a")
    new = tokens.mint("exec", **key)  # unlocked again, the same job still running
    assert new != old and tokens.lookup(old) is None and tokens.lookup(new) is not None
    assert tokens.mint("exec", **key) == new  # still idempotent within a generation


def test_a_token_whose_end_nobody_saw_dies_a_day_after_its_last_use(monkeypatch):
    tokens = WaitTokens()
    token = tokens.mint("copy", minter="owner", agent_id="pc", job_id="pc:x")
    now = time.time()
    monkeypatch.setattr(wait_urls.time, "time", lambda: now + 3600)
    assert tokens.lookup(token) is not None  # used: the idle clock starts again
    monkeypatch.setattr(wait_urls.time, "time", lambda: now + 3600 + wait_urls.IDLE_MAX_S + 1)
    assert tokens.lookup(token) is None


def test_copy_results_in_a_terminal_state_start_the_expiry():
    from agent_nexus.file_copy_tools import with_wait_url
    tokens = WaitTokens()
    gateway = types.SimpleNamespace(wait_urls=tokens, caller_windows=lambda: True,
                                    wait_url=lambda kind, **key: "http://127.0.0.1:1/wait/" + tokens.mint(kind, minter="owner", **key))
    running = with_wait_url(gateway, {"job_id": "pc:x", "state": "running"})
    assert running["wait_hint"].startswith("curl.exe -sN http://127.0.0.1:1/wait/")
    entry = tokens.lookup(running["wait_url"].rsplit("/", 1)[-1])
    assert entry.ended_at is None
    assert "wait_url" not in with_wait_url(gateway, {"job_id": "pc:x", "state": "completed"})
    assert entry.ended_at is not None


def test_wait_hint_says_curl_exe_for_windows():
    assert wait_urls.wait_hint("http://h/wait/t") == "curl -sN http://h/wait/t"
    assert wait_urls.wait_hint("http://h/wait/t", windows=True) == "curl.exe -sN http://h/wait/t"
    from agent_nexus.gateway import INSTRUCTIONS, exec_text
    assert "curl.exe" in INSTRUCTIONS and len(INSTRUCTIONS) < 700
    text, _ = exec_text({"session_id": 3, "output": "", "wait_url": "http://h/wait/t"}, "pc", windows=True)
    assert "wait: curl.exe -sN http://h/wait/t" in text


class FlakyPeer(FakePeer):
    """Raises like a link that dropped (the peer is closed and replaced by a new one)."""

    def __init__(self, tools, answers, hub, replacement):
        super().__init__(tools, answers)
        self.hub, self.replacement, self._closed = hub, replacement, False

    async def call(self, method, params):
        self.calls.append(params)
        self._closed = True
        self.hub.peers.pop("pc-b")
        asyncio.get_running_loop().call_later(0.05, self.hub.peers.__setitem__, "pc-b", self.replacement)
        raise RuntimeError("manager pc-b disconnected")


async def test_a_short_disconnect_is_waited_out_with_the_same_since():
    fresh = FakePeer(["exec_command", "exec_tail"], [{"output": "y\n", "seq": 9, "running": False, "exit_code": 0}])
    gateway = fake_gateway(None)
    flaky = FlakyPeer(["exec_command", "exec_tail"], [], gateway.hub, fresh)
    gateway.hub.peers["pc-b"] = flaky
    entry = wait_urls.WaitEntry(kind="exec", agent_id="pc-b", minter="owner", mcp="device", session_id=7, job="j",
                                since=4)
    text = await run_stream(gateway, entry, grace_s=5, poll_s=0.01)
    assert text == "y\n[anx] exit 0\n"
    assert flaky.calls[0]["arguments"]["since"] == 4 and fresh.calls[0]["arguments"]["since"] == 4
    assert entry.since == 9  # the same URL fetched again continues from here
    # gone for longer than the grace period: the device-offline line
    gateway.hub.peers.pop("pc-b")
    entry = wait_urls.WaitEntry(kind="exec", agent_id="pc-b", minter="owner", mcp="device", session_id=7, job="j")
    assert "device offline" in await run_stream(gateway, entry, grace_s=0.05, poll_s=0.01)


async def test_a_copy_that_is_briefly_unknown_keeps_streaming():
    snaps = [{"state": "unknown", "message": "offline"}, {"state": "running", "files": {"done": 1, "total": 2}},
             {"state": "completed", "files": {"done": 2, "total": 2}, "bytes": {"done": 5}}]

    class Copies:
        async def status(self, job_id, wait_ms, cap_ms=None):
            return snaps.pop(0)
    gateway = fake_gateway(FakePeer([], []))
    gateway.file_copy = Copies()
    entry = wait_urls.WaitEntry(kind="copy", agent_id="pc-a", minter="owner", job_id="pc-a:x")
    text = await run_stream(gateway, entry, grace_s=5, poll_s=0.01)
    assert text.endswith("[anx] copy done: 2 files, 5 B\n") and "unknown" not in text


async def test_a_failed_later_segment_still_returns_the_output_already_taken():
    from agent_nexus.federation import Hub, RoutingError
    from agent_nexus.gateway import Gateway
    gateway = Gateway(Hub(), tool_wait_max_s=1)

    class Peer:
        agent_id = "pc-b"

        def __init__(self, answers):
            self.answers = answers

        async def call(self, method, params):
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

    def ok(text):
        data = {"output": text, "session_id": 5, "exit_code": None}
        return {"structuredContent": data, "content": [], "isError": False}
    mcp = {"tools": [{"name": "exec_wait"}]}
    for failure in (RoutingError("manager pc-b disconnected; execution outcome may be unknown"),
                    {"structuredContent": {"status": "error", "error": "unknown exec session 5"}, "isError": True}):
        peer = Peer([ok("one\n"), ok("two\n"), failure])
        result = await gateway._segmented(peer, "device", mcp, "exec_command",
                                          {"cmd": "x", "yield_time_ms": 60_000}, None, "default")
        data = result["structuredContent"]
        assert result["isError"] is False and data["output"] == "one\ntwo\n" and data["session_id"] == 5
        assert data.get("exit_code") is None and data["note"].startswith("wait interrupted: ")
        assert "call exec_wait to continue" in data["note"]
