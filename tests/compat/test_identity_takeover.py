"""Mailbox identity after takeovers (design §8.2): the second verifier round on automatic names.

A conversation that lost its automatic address to another one never gets it (or its mail) back;
a pending mail_wait / blocking ask_user keeps the address in use; claiming an idle automatic
address by name is a full takeover and a refused claim changes nothing; a queued wake re-checks
its route; migration 8 marks schema-7 takeover victims; a kept explicit name and a legacy anon
handle re-attach instead of forking. Unit level on ``Mailbox`` (injectable clock) plus end-to-end
runs over MCP with the M7 wake harness (``idle_after_s`` = 0.5 s).
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastmcp.exceptions import ToolError

from agent_nexus.federation import RoutingError
from agent_nexus.mailbox import ME
from agent_nexus.store import MIGRATIONS, GatewayStore
from agent_nexus.wake import WakeRouter

from test_autoname import AGENT, WORKDIR, env  # noqa: F401  (the fixture)


def _takeover(env):
    """A binds and goes idle; B (same client and folder) takes the address over; mail for B arrives."""
    ha, a = env.bind(meta={"resume_id": "sess-a"})
    env.tick(600)
    hb, b = env.bind()
    assert b["address"] == a["address"] and env.mailbox.displaced(ha) == a["address"]
    env.mailbox.send(ME, b["address"], "secret for B")
    return ha, hb, b["address"]


# -- 1. the victim never gets the address back ---------------------------------------------------------

def test_takeover_victim_never_reclaims_the_address_while_the_taker_is_idle(env):
    ha, hb, address = _takeover(env)
    env.tick(600)  # B is busy elsewhere (a long build): idle by the presence rule
    _, back = env.bind(previous=ha)
    assert back["address"] == f"claude-myproj-2@{AGENT}" and back["unread"] == 0
    assert env.mailbox.address_for_handle(hb) == address and env.mailbox.unread_count(address) == 1
    # Nor by typing the name.
    with pytest.raises(RoutingError, match="name_taken"):
        env.bind(meta={"session_name": "claude-myproj", "kind": "claude"}, previous=ha)
    assert env.mailbox.address_for_handle(hb) == address and env.mailbox.displaced(hb) is None


def test_a_running_conversation_takes_no_idle_holders_address(env):
    ha, a = env.bind()
    hb, b = env.bind()  # A is active: -2
    env.tick(600)  # both idle now, both handles live
    hc, elsewhere = env.bind(workdir="/x/other")
    _, moved = env.bind(previous=hc)  # a running conversation moves into the folder
    assert moved["address"] == f"claude-myproj-3@{AGENT}" and moved["unread"] == 0
    assert env.mailbox.displaced(ha) is None and env.mailbox.displaced(hb) is None
    _, fresh = env.bind()  # a restart (no earlier handle) still gets the stable name back
    assert fresh["address"] == a["address"]


# -- 2. a blocking call keeps the address in use ------------------------------------------------------

def test_a_pending_blocking_call_keeps_the_address_in_use(env):
    ha, a = env.bind()
    with env.mailbox.in_use(ha):
        env.tick(600)
        _, b = env.bind()
        assert b["address"] == f"claude-myproj-2@{AGENT}"  # A is blocked in mail_wait, not idle
        with pytest.raises(RoutingError, match="name_taken"):
            env.bind(meta={"session_name": "claude-myproj", "kind": "claude"})
        assert env.mailbox.displaced(ha) is None
    assert env.mailbox._inflight == {}
    _, c = env.bind()  # the wait ended long ago: the usual idle rule again
    assert c["address"] == a["address"]


def test_presence_is_active_while_blocked(env):
    env.mailbox.online = lambda agent: True
    ha, a = env.bind()
    env.tick(600)
    assert env.mailbox.presence(env.mailbox.address_row(a["address"])) == "idle"
    with env.mailbox.in_use(ha), env.mailbox.in_use(ha):
        assert env.mailbox.presence(env.mailbox.address_row(a["address"])) == "active"
    assert env.mailbox.presence(env.mailbox.address_row(a["address"])) == "idle"


# -- 3. an explicit claim is a full takeover --------------------------------------------------------------

def test_explicit_claim_clears_the_previous_conversations_resume_id_and_title(env):
    ha, a = env.bind(meta={"resume_id": "sess-a"})
    env.mailbox.presence_report(AGENT, {"kind": "claude", "cwd": WORKDIR, "session_id": "sess-a",
                                        "event": "SessionStart", "title": "work of A"})
    env.tick(600)
    _, claimed = env.bind(meta={"session_name": "claude-myproj"})
    row = env.mailbox.address_row(claimed["address"])
    assert claimed["address"] == a["address"] and row["name_source"] == "explicit"
    assert row["resume_id"] is None and row["resume_source"] is None and row["title"] is None
    assert WakeRouter.has_route(row, "claude")  # an explicit name with a workdir routes, but not to sess-a
    _, mine = env.bind(client="c2", workdir="/x/other", meta={"session_name": "solo", "resume_id": "sess-own"})
    assert env.mailbox.address_row(mine["address"])["resume_id"] == "sess-own"


def test_claim_with_a_merge_keeps_the_claimers_resume_id(env):
    ha, a = env.bind(meta={"resume_id": "sess-a"})
    hb, b = env.bind(meta={"resume_id": "sess-b"})  # A is active: B gets -2
    assert b["address"] == f"claude-myproj-2@{AGENT}"
    env.mailbox.send(ME, b["address"], "for B")
    env.tick(600)
    _, merged = env.bind(meta={"session_name": "claude-myproj"}, previous=hb)
    row = env.mailbox.address_row(a["address"])
    assert merged["address"] == a["address"] and merged["renamed_from"] == b["address"]
    assert row["resume_id"] == "sess-b" and merged["unread"] == 1
    assert env.mailbox.displaced(ha) == a["address"] and env.mailbox.address_for_handle(hb) == a["address"]


# -- 4. a refused claim changes nothing ----------------------------------------------------------------------

def test_a_refused_claim_displaces_nobody(env):
    ha, a = env.bind(kind_hint="codex", meta={"kind": "codex", "resume_id": "sess-codex-a"})
    assert a["address"] == f"codex-myproj@{AGENT}"
    env.tick(600)
    with pytest.raises(RoutingError, match="is a codex session"):
        env.bind(meta={"session_name": "codex-myproj", "kind": "claude"})
    row = env.mailbox.address_row(a["address"])
    assert env.mailbox.displaced(ha) is None and env.mailbox.address_for_handle(ha) == a["address"]
    assert row["resume_id"] == "sess-codex-a" and row["name_source"] == "auto"
    # The rename path: B holds a claude automatic address and asks for the codex one's name.
    hb, b = env.bind(workdir=WORKDIR)
    env.tick(600)  # the checks above used A's handle: idle again
    with pytest.raises(RoutingError, match="is a codex session"):
        env.bind(meta={"session_name": "codex-myproj"}, previous=hb)
    assert env.mailbox.displaced(ha) is None and env.mailbox.address_for_handle(hb) == b["address"]
    assert env.mailbox.address_row(a["address"])["resume_id"] == "sess-codex-a"
    # A's own next bind is its own rebind, not a takeover: the resume id stays.
    _, again = env.bind(kind_hint="codex", meta={"kind": "codex"}, previous=ha)
    assert again["address"] == a["address"] and env.mailbox.address_row(a["address"])["resume_id"] == "sess-codex-a"


# -- 6. migration 8 marks schema-7 takeover victims ---------------------------------------------------------

def test_migration_8_marks_the_victims_of_schema_7_takeovers(tmp_path):
    path = tmp_path / "gateway.sqlite3"
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
    for script in MIGRATIONS[:7]:
        for statement in script.split(";"):
            if statement.strip():
                db.execute(statement)
    db.execute("INSERT INTO schema_version(version) VALUES (7)")
    far = 10 ** 10
    for handle, address, superseded in [("ha" * 16, "claude-p@dev-a", "hb" * 16), ("hb" * 16, "claude-p@dev-a", None),
                                        ("e1" * 16, "alpha@dev-a", "e2" * 16), ("e2" * 16, "alpha@dev-a", None)]:
        db.execute("INSERT INTO handles(handle, agent_id, workspace, created_at, expires_at, address, superseded_by) "
                   "VALUES (?, 'dev-a', 'p', 1, ?, ?, ?)", (handle, far, address, superseded))
    db.execute("INSERT INTO addresses(address, name, agent_id, kind, wake_policy, created_at, last_handle, name_source) "
               "VALUES ('claude-p@dev-a', 'claude-p', 'dev-a', 'claude', 'auto', 1, ?, 'auto')", ("hb" * 16,))
    db.execute("INSERT INTO addresses(address, name, agent_id, kind, wake_policy, created_at, last_handle, name_source) "
               "VALUES ('alpha@dev-a', 'alpha', 'dev-a', 'claude', 'auto', 1, ?, 'explicit')", ("e2" * 16,))
    db.close()
    store = GatewayStore(path)
    try:
        from agent_nexus.mailbox import Mailbox
        mailbox = Mailbox(store)
        assert store.schema_version() == len(MIGRATIONS)
        mailbox.send(ME, "claude-p@dev-a", "secret for B")
        assert mailbox.displaced("ha" * 16) == "claude-p@dev-a" and mailbox.address_for_handle("ha" * 16) is None
        assert mailbox.address_for_handle("hb" * 16) == "claude-p@dev-a"
        # Explicit addresses: a superseded handle is the same session, never displaced.
        assert mailbox.address_for_handle("e1" * 16) == "alpha@dev-a" == mailbox.address_for_handle("e2" * 16)
    finally:
        store.close()


# -- 7. a kept explicit name re-attaches ------------------------------------------------------------------------

def test_rebinding_an_explicit_address_without_session_name_keeps_it(env):
    h1, named = env.bind(meta={"session_name": "mine"})
    env.mailbox.send(ME, named["address"], "for mine")
    h2, again = env.bind(previous=h1)
    assert again["address"] == f"mine@{AGENT}" and again["name_source"] == "explicit" and again["unread"] == 1
    assert env.mailbox.address_for_handle(h1) == env.mailbox.address_for_handle(h2) == f"mine@{AGENT}"
    # Another folder or kind is another session: the automatic name of that one.
    _, elsewhere = env.bind(previous=h2, workdir="/x/other")
    assert elsewhere["address"] == f"claude-other@{AGENT}"
    _, codex = env.bind(previous=h2, kind_hint="codex", meta={"kind": "codex"})
    assert codex["address"] == f"codex-myproj@{AGENT}"


def test_a_legacy_explicit_row_re_attaches(env):
    with env.store._lock:
        db = env.store._connect()
        db.execute("INSERT INTO addresses(address, name, agent_id, kind, workdir, wake_policy, created_at, "
                   "last_handle) VALUES (?, 'alpha', ?, 'claude', ?, 'auto', 1, ?)",
                   (f"alpha@{AGENT}", AGENT, WORKDIR, "e1" * 16))
    env.store.put_handle("e1" * 16, AGENT, "proj", client_id="client-1")
    with env.store._lock:
        env.store._connect().execute("UPDATE handles SET address = ? WHERE handle = ?", (f"alpha@{AGENT}", "e1" * 16))
    _, again = env.bind(previous="e1" * 16)
    assert again["address"] == f"alpha@{AGENT}"


# -- 8. a legacy anon handle brings its mail along ----------------------------------------------------------

def test_a_legacy_anon_handle_moves_its_mail_into_the_automatic_address(env):
    legacy = "n1" + "0" * 30
    env.store.put_handle(legacy, AGENT, "proj", client_id="client-1")
    anon = env.mailbox.address_for_handle(legacy)
    assert anon.startswith("anon-")
    env.mailbox.send(ME, anon, "to anon")
    _, bound = env.bind(previous=legacy)
    assert bound["address"] == f"claude-myproj@{AGENT}" and bound["unread"] == 1 and bound["renamed_from"] == anon
    assert env.mailbox.address_row(anon) is None and env.mailbox.address_for_handle(legacy) == bound["address"]


def test_a_legacy_anon_handle_renames_with_its_mail(env):
    legacy = "n2" + "0" * 30
    env.store.put_handle(legacy, AGENT, "proj", client_id="client-1")
    anon = env.mailbox.address_for_handle(legacy)
    env.mailbox.send(ME, anon, "to anon")
    _, named = env.bind(meta={"session_name": "beta"}, previous=legacy)
    assert named["address"] == f"beta@{AGENT}" and named["unread"] == 1 and named["renamed_from"] == anon


# -- end to end over MCP -------------------------------------------------------------------------------------------

def _harness():
    from test_compat_matrix import AGENT_ID
    from test_gateway_m4 import mcp_client
    from test_mailbox_m6 import start
    from test_wake_m7 import FakeCli, gateway, jobs, start_device, until
    return AGENT_ID, mcp_client, start, FakeCli, gateway, jobs, start_device, until


async def _use(client, agent_id, **args):
    return (await client.call_tool("use_device", {"device_id": agent_id, **args})).data


async def test_takeover_victim_and_pending_mail_wait_over_mcp(tmp_path, monkeypatch):
    agent_id, mcp_client, start, _, gateway, _, _, _ = _harness()
    async with gateway(tmp_path, monkeypatch) as (app, port, agents):
        await start(tmp_path, port, agents)
        async with mcp_client(port) as a, mcp_client(port) as b, mcp_client(port) as c, mcp_client(port) as d:
            hc = (await _use(c, agent_id, session_name="sender", kind="codex"))["workspace_id"]
            # A pending mail_wait keeps A's address: B gets its own.
            sa = await _use(a, agent_id, kind="claude")
            ha, auto = sa["workspace_id"], sa["address"]
            wait = asyncio.create_task(a.call_tool("mail_wait", {"workspace_id": ha, "timeout_s": 4}))
            await asyncio.sleep(0.8)
            sb = await _use(b, agent_id, kind="claude")
            assert sb["address"] != auto
            await c.call_tool("mail_send", {"workspace_id": hc, "to": sb["address"], "body": "secret for B"})
            await c.call_tool("mail_send", {"workspace_id": hc, "to": auto, "body": "for A"})
            got = (await wait).data
            assert [m["snippet"] for m in got["messages"]] == ["for A"]
            await a.call_tool("mail_read", {"workspace_id": ha})
            # A goes idle; a new conversation takes the address over; A comes back while it is idle.
            await asyncio.sleep(0.7)
            sd = await _use(d, agent_id, kind="claude")
            assert sd["address"] == auto
            await c.call_tool("mail_send", {"workspace_id": hc, "to": auto, "body": "secret for D"})
            with pytest.raises(ToolError, match="no longer holds"):
                await a.call_tool("mail_inbox", {"workspace_id": ha})
            await asyncio.sleep(0.7)
            back = await _use(a, agent_id, kind="claude", workspace_id=ha)
            assert back["address"] != auto and back["mail"]["unread"] == 0
            inbox = (await a.call_tool("mail_inbox", {"workspace_id": back["workspace_id"]})).data
            assert inbox["messages"] == []


async def test_mail_wait_taken_over_midway_returns_nothing_of_the_takers(tmp_path, monkeypatch):
    agent_id, mcp_client, start, _, gateway, _, _, _ = _harness()
    async with gateway(tmp_path, monkeypatch) as (app, port, agents):
        await start(tmp_path, port, agents)
        mailbox = app.state.gateway.mailbox
        async with mcp_client(port) as a, mcp_client(port) as c:
            hc = (await _use(c, agent_id, session_name="sender", kind="codex"))["workspace_id"]
            sa = await _use(a, agent_id, kind="claude")
            ha, auto = sa["workspace_id"], sa["address"]
            wait = asyncio.create_task(a.call_tool("mail_wait", {"workspace_id": ha, "timeout_s": 4}))
            await asyncio.sleep(0.3)
            with mailbox.store._lock:  # as if a takeover had happened anyway (the check before returning)
                mailbox._displace(mailbox._db(), auto, keep="someone-else")
            await c.call_tool("mail_send", {"workspace_id": hc, "to": auto, "body": "secret for B"})
            with pytest.raises(ToolError, match="no longer holds"):
                await wait


async def test_explicit_claim_and_queued_wake_never_resume_the_previous_conversation(tmp_path, monkeypatch):
    agent_id, mcp_client, _, FakeCli, gateway, jobs, start_device, until = _harness()
    fake = FakeCli(tmp_path / "fake")
    async with gateway(tmp_path, monkeypatch, {"wake": {"debounce_s": 0.6}}) as (app, port, agents):
        await start_device(tmp_path, port, agents, fake)
        async with mcp_client(port) as a, mcp_client(port) as b, mcp_client(port) as c, mcp_client(port) as d:
            hc = (await _use(c, agent_id, session_name="sender", kind="codex"))["workspace_id"]
            # A queued wake, then a takeover inside the debounce window: no --continue run.
            sa = await _use(a, agent_id, kind="claude", resume_id="sess-of-A")
            auto = sa["address"]
            await asyncio.sleep(0.7)
            sent = (await c.call_tool("mail_send", {"workspace_id": hc, "to": auto, "body": "for A"})).data
            assert sent["deliveries"][0]["action"] == "wake_queued"
            sb = await _use(b, agent_id, kind="claude")
            assert sb["address"] == auto
            await until(lambda: any(j["state"] == "skipped" for j in jobs(app, auto)), timeout=3)
            assert [j.get("reason") for j in jobs(app, auto)] == ["no route"] and fake.calls("claude") == []
            # An explicit claim of the idle address: B's mail never resumes A's conversation.
            await asyncio.sleep(0.7)
            sd = await _use(d, agent_id, kind="claude", session_name=auto.split("@")[0], resume_id="sess-of-D")
            assert sd["address"] == auto
            assert app.state.gateway.mailbox.address_row(auto)["resume_id"] == "sess-of-D"
            await asyncio.sleep(0.7)
            await c.call_tool("mail_send", {"workspace_id": hc, "to": auto, "body": "task for D"})
            calls = await until(lambda: fake.calls("claude"), timeout=3)
            assert "sess-of-D" in calls[0]["argv"] and "sess-of-A" not in calls[0]["argv"]
