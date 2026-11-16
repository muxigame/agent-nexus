"""Automatic session names (design §8.2): derivation, stability, collisions, rename and merge, titles, nudge.

Unit level on ``Mailbox`` with an injectable clock and a temporary database,
plus one end-to-end run over MCP (the nudge and a rename with mail following).
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from agent_nexus.federation import RoutingError
from agent_nexus.mailbox import (ME, Mailbox, auto_base, auto_label, clean_title, norm_workdir, slug,
                                 workdir_basename)
from agent_nexus.store import HANDLE_TTL_S, MIGRATIONS, GatewayStore
from agent_nexus.wake import WakeRouter

AGENT = "dev-a"
WORKDIR = "C:\\Work\\MyProj"


class Env:
    def __init__(self, tmp_path):
        self.now = [1_000_000.0]
        self.store = GatewayStore(tmp_path / "gateway.sqlite3", clock=lambda: self.now[0])
        self.mailbox = Mailbox(self.store)
        self.count = 0

    def tick(self, seconds: float) -> None:
        self.now[0] += seconds

    def bind(self, *, workdir=WORKDIR, client="client-1", kind_hint="claude", meta=None, previous=None,
             client_name=None, agent_id=AGENT):
        self.count += 1
        handle = f"h{self.count:02d}" + "0" * 30
        self.store.put_handle(handle, agent_id, "proj", client_id=client)
        bound = self.mailbox.bind(handle, agent_id, workdir=workdir, meta=meta, client_id=client,
                                  kind_hint=kind_hint, client_name=client_name, previous=previous)
        return handle, bound

    def use(self, handle):
        """A tool call on that handle (what the gateway does on every call)."""
        assert self.store.touch_handle(handle) is not None

    def rows(self, sql, *args):
        with self.store._lock:
            return [dict(row) for row in self.store._connect().execute(sql, args).fetchall()]


@pytest.fixture
def env(tmp_path):
    item = Env(tmp_path)
    yield item
    item.store.close()


# -- derivation ---------------------------------------------------------------------------

def test_slug_basename_and_base_names():
    assert workdir_basename("C:\\Users\\me\\My Project!\\") == "My Project!"
    assert workdir_basename("/home/u/repo") == "repo" and workdir_basename(None) == ""
    assert slug("My Project!") == "my-project" and slug("--.x__y..") == "x__y" and slug("项目") == ""
    assert norm_workdir("C:\\Work\\MyProj\\") == norm_workdir("c:/work/myproj") == "c:/work/myproj"
    assert auto_base("claude", "C:\\Users\\me\\My Project!") == "claude-my-project"
    assert auto_base("codex", "/home/u/项目") == "codex" and auto_base("gpt", None) == "gpt"
    long = auto_base("claude", "/x/" + "a" * 200)
    assert len(long) == 61 and len(long + "-99") == 64
    assert auto_label("claude", "whatever") == "claude" and auto_label("other", "Some Client v2") == "some-client-v2"
    assert auto_label(None, None) == "agent" and auto_label("other", "anon-x") == "agent"
    assert auto_label("other", "Me") == "agent" and auto_label("other", "device-ask") == "agent"
    assert len(auto_label("other", "x" * 50)) == 20


def test_bind_without_name_derives_kind_and_workdir(env):
    _, bound = env.bind()
    assert bound["address"] == f"claude-myproj@{AGENT}" and bound["name_source"] == "auto"
    row = env.mailbox.address_row(bound["address"])
    assert row["name_source"] == "auto" and row["wake_policy"] == "auto" and row["kind"] == "claude"
    _, plain = env.bind(workdir=None, client="other", kind_hint=None)
    assert plain["address"] == f"agent@{AGENT}"
    _, named = env.bind(client="c3", kind_hint=None, client_name="Cursor IDE")
    assert named["address"] == f"cursor-ide-myproj@{AGENT}"
    _, gpt = env.bind(client="c4", kind_hint=None, meta={"kind": "gpt"})
    assert gpt["address"] == f"gpt-myproj@{AGENT}"


def test_reserved_and_explicit_names_are_never_taken(env):
    # An explicit session already called claude-myproj, and a legacy row (no name_source) after it.
    env.bind(meta={"session_name": "claude-myproj", "kind": "claude"})
    with env.store._lock:
        env.store._connect().execute(
            "INSERT INTO addresses(address, name, agent_id, kind, workdir, wake_policy, oauth_client_id, created_at) "
            "VALUES (?, 'claude-myproj-2', ?, 'claude', ?, 'auto', 'client-1', 0)", (f"claude-myproj-2@{AGENT}", AGENT,
                                                                                  WORKDIR))
    _, bound = env.bind()
    assert bound["address"] == f"claude-myproj-3@{AGENT}"
    # "device-ask" (the device plugin's asker) is reserved: the derivation skips it.
    _, asker = env.bind(workdir="/x/ask", kind_hint=None, client="c9", client_name="device")
    assert asker["address"] == f"device-ask-2@{AGENT}"
    for name in ("me", "anon-123456", "device-ask"):
        with pytest.raises(RoutingError, match="reserved"):
            env.bind(meta={"session_name": name})


# -- stability and collisions -----------------------------------------------------------------

def test_reconnect_gets_the_same_address_and_its_mail(env):
    h1, first = env.bind()
    address = first["address"]
    env.mailbox.send(ME, address, "hello")
    env.tick(600)  # the first session went away
    h2, again = env.bind()
    assert again["address"] == address and again["unread"] == 1
    assert env.rows("SELECT superseded_by FROM handles WHERE handle = ?", h1)[0]["superseded_by"] == h2
    # The same conversation binding again (its previous handle known) keeps it even while active.
    h3, same = env.bind(previous=h2)
    assert same["address"] == address


def test_active_holder_pushes_a_second_session_to_suffix_then_idle_is_reused(env):
    h1, first = env.bind()
    env.tick(10)
    env.use(h1)
    h2, second = env.bind()
    assert second["address"] == f"claude-myproj-2@{AGENT}"  # h1 was used 10 s ago
    env.tick(10)
    env.use(h1)
    env.use(h2)
    _, third = env.bind()
    assert third["address"] == f"claude-myproj-3@{AGENT}"
    env.tick(HANDLE_TTL_S / 2)  # both idle now
    _, fourth = env.bind()
    assert fourth["address"] == first["address"]  # deterministic: the first free one


def test_hook_activity_does_not_count_as_a_holder(env):
    h1, first = env.bind()
    env.tick(300)
    # A hook reported a prompt in the folder (the address is "seen"); the handle itself is idle.
    env.mailbox.presence_report(AGENT, {"kind": "claude", "cwd": WORKDIR, "session_id": "s-2", "event": "SessionStart"})
    _, again = env.bind()
    assert again["address"] == first["address"]


def test_other_client_or_folder_never_shares_an_address(env):
    _, first = env.bind()
    env.tick(600)
    _, other_client = env.bind(client="client-2")
    _, other_folder = env.bind(workdir="D:\\Elsewhere\\MyProj")
    assert first["address"] == f"claude-myproj@{AGENT}"
    assert other_client["address"] == f"claude-myproj-2@{AGENT}"
    assert other_folder["address"] == f"claude-myproj-3@{AGENT}"
    _, other_device = env.bind(agent_id="dev-b")
    assert other_device["address"] == "claude-myproj@dev-b"


def test_auto_addresses_are_real_sessions(env):
    _, auto = env.bind()
    _, named = env.bind(client="c2", meta={"session_name": "alpha", "kind": "claude"})
    with env.store._lock:
        env.store._connect().execute(
            "INSERT INTO addresses(address, name, agent_id, kind, wake_policy, created_at, last_seen_at) "
            "VALUES (?, 'anon-abcdef', ?, 'claude', 'never', ?, ?)", (f"anon-abcdef@{AGENT}", AGENT, env.now[0],
                                                                       env.now[0]))
    for group in ("@all", f"@device:{AGENT}", "@kind:claude"):
        found, _ = env.mailbox.expand([group], None)
        assert auto["address"] in found and named["address"] in found and f"anon-abcdef@{AGENT}" not in found
    listed = {item["address"]: item for item in env.mailbox.addresses()}
    assert listed[auto["address"]]["name_source"] == "auto" and listed[named["address"]]["name_source"] == "explicit"
    assert listed[f"anon-abcdef@{AGENT}"]["name_source"] == "anon"
    assert listed[auto["address"]]["device_display"] == AGENT and "title" in listed[auto["address"]]


def test_auto_address_wakes_only_with_a_resume_id():
    assert WakeRouter.has_route({"name_source": "auto", "workdir": "/x"}, "claude") is False
    assert WakeRouter.has_route({"name_source": "auto", "workdir": "/x", "resume_id": "s-1"}, "codex") is True
    assert WakeRouter.has_route({"name_source": "explicit", "workdir": "/x"}, "claude") is True
    assert WakeRouter.has_route({"name_source": None, "workdir": "/x"}, "claude") is True  # legacy rows


# -- layer 3: rename and merge -----------------------------------------------------------------

def _populate(env, address):
    """Mail of every kind naming ``address``."""
    unread = env.mailbox.send(ME, address, "unread one")["message_id"]
    read = env.mailbox.send(ME, address, "read one")["message_id"]
    env.mailbox.mark_read(read, address)
    sent = env.mailbox.send(address, ME, "from the session")["message_id"]
    ask = env.mailbox.ask_create(address, "Which one?", options=["a", "b"])["ask_id"]
    env.mailbox.set_group("crew", [address, "someone@else"])
    with env.store._lock:
        env.store._connect().execute(
            "INSERT INTO wake_jobs(id, address, agent_id, adapter, state, message_ids, next_at, created_at, "
            "expires_at) VALUES ('w_old', ?, ?, 'claude', 'queued', ?, 0, 0, 9e12)", (address, AGENT,
                                                                                   json.dumps([unread])))
    return {"unread": unread, "read": read, "sent": sent, "ask": ask}


def test_rename_moves_every_table(env):
    h1, auto = env.bind()
    old = auto["address"]
    ids = _populate(env, old)
    h2, bound = env.bind(meta={"session_name": "worker"}, previous=h1)
    new = f"worker@{AGENT}"
    assert bound["address"] == new and bound["name_source"] == "explicit" and bound["renamed_from"] == old
    assert env.mailbox.address_row(old) is None
    row = env.mailbox.address_row(new)
    assert row["name_source"] == "explicit" and row["kind"] == "claude" and row["last_handle"] == h2
    assert bound["unread"] == 1
    assert {r["message_id"]: r["state"] for r in env.rows("SELECT * FROM deliveries WHERE to_addr = ?", new)} == \
        {ids["unread"]: "unread", ids["read"]: "read"}
    assert env.rows("SELECT * FROM deliveries WHERE to_addr = ?", old) == []
    assert env.rows("SELECT from_addr FROM messages WHERE id = ?", ids["sent"])[0]["from_addr"] == new
    assert json.loads(env.rows("SELECT to_expr FROM messages WHERE id = ?", ids["unread"])[0]["to_expr"]) == [new]
    assert env.rows("SELECT from_addr FROM asks WHERE id = ?", ids["ask"])[0]["from_addr"] == new
    assert env.rows("SELECT address, state FROM wake_jobs WHERE id = 'w_old'")[0] == {"address": new, "state": "queued"}
    assert json.loads(env.rows("SELECT members_json FROM groups WHERE name = 'crew'")[0]["members_json"]) == \
        [new, "someone@else"]
    assert env.rows("SELECT address, superseded_by FROM handles WHERE handle = ?", h1)[0] == \
        {"address": new, "superseded_by": h2}
    assert env.rows("SELECT * FROM events WHERE address = ? AND address = ?", old, old) == []
    renamed = env.rows("SELECT data_json FROM events WHERE type = 'address_renamed'")
    assert json.loads(renamed[0]["data_json"]) == {"from": old, "merged": False}
    # The new name now behaves as any explicit session (re-attach by name, no nudge).
    assert env.mailbox.nudge_for(new, h2) is None


def test_rename_merges_into_an_existing_session_of_the_same_kind(env):
    hw, worker = env.bind(client="c2", meta={"session_name": "worker", "kind": "claude"})
    target = worker["address"]
    with env.store._lock:
        env.store._connect().execute(
            "INSERT INTO wake_jobs(id, address, agent_id, adapter, state, message_ids, next_at, created_at, "
            "expires_at) VALUES ('w_target', ?, ?, 'claude', 'queued', '[]', 0, 0, 9e12)", (target, AGENT))
    h1, auto = env.bind()
    old = auto["address"]
    both = env.mailbox.send(ME, [old, target], "to both")["message_id"]
    ids = _populate(env, old)
    env.mailbox.presence_report(AGENT, {"kind": "claude", "cwd": "C:/elsewhere", "session_id": "x"})  # no-op
    with env.store._lock:
        env.store._connect().execute("UPDATE addresses SET resume_id = 'sess-auto', title = 'Fix login' "
                                     "WHERE address = ?", (old,))
    _, bound = env.bind(meta={"session_name": "worker"}, previous=h1)
    assert bound["address"] == target and env.mailbox.address_row(old) is None
    assert env.rows("SELECT COUNT(*) AS n FROM deliveries WHERE message_id = ?", both)[0]["n"] == 1
    states = {r["message_id"]: r["state"] for r in env.rows("SELECT * FROM deliveries WHERE to_addr = ?", target)}
    assert states[ids["unread"]] == "unread" and states[both] == "unread" and bound["unread"] == 2
    jobs = {r["id"]: r for r in env.rows("SELECT * FROM wake_jobs")}
    assert jobs["w_target"]["state"] == "queued"
    assert jobs["w_old"]["state"] == "skipped" and jobs["w_old"]["reason"] == "renamed"
    assert jobs["w_old"]["address"] == target
    row = env.mailbox.address_row(target)
    assert row["resume_id"] == "sess-auto" and row["title"] == "Fix login" and row["name_source"] == "explicit"
    assert json.loads(env.rows("SELECT data_json FROM events WHERE type = 'address_renamed'")[0]["data_json"])["merged"]


def test_rename_into_another_kind_fails_and_changes_nothing(env):
    env.bind(client="c2", kind_hint="codex", meta={"session_name": "worker", "kind": "codex"})
    h1, auto = env.bind()
    env.mailbox.send(ME, auto["address"], "keep me")
    with pytest.raises(RoutingError, match="name_taken.*mail stays at"):
        env.bind(meta={"session_name": "worker"}, previous=h1)
    assert env.mailbox.address_row(auto["address"])["name_source"] == "auto"
    assert env.mailbox.unread_count(auto["address"]) == 1


def test_no_link_to_the_previous_handle_means_no_rename(env):
    _, auto = env.bind()
    env.mailbox.send(ME, auto["address"], "stays")
    _, named = env.bind(meta={"session_name": "worker"})
    assert named["address"] == f"worker@{AGENT}" and "renamed_from" not in named
    assert env.mailbox.unread_count(auto["address"]) == 1


def test_previous_handle_on_another_device_is_not_renamed(env):
    h1, auto = env.bind()
    _, named = env.bind(agent_id="dev-b", meta={"session_name": "worker"}, previous=h1)
    assert named["address"] == "worker@dev-b" and env.mailbox.address_row(auto["address"]) is not None


def test_workdir_change_rederives(env):
    h1, first = env.bind()
    _, moved = env.bind(workdir="C:\\Work\\Other", previous=h1)
    assert moved["address"] == f"claude-other@{AGENT}" and moved["name_source"] == "auto"
    assert env.mailbox.address_row(first["address"]) is not None  # mail is not moved by a folder change


# -- layer 2: presence reports and titles ----------------------------------------------------------

def test_clean_title():
    assert clean_title("  Fix\x00 the\u202e login\n\tpage ") == "Fix the login page"
    assert clean_title("\u200b\x07") is None and clean_title(None) is None and clean_title(5) is None
    long = clean_title("x" * 300)
    assert len(long) == 80 and long.endswith("…")


def test_presence_report_matches_auto_addresses_and_keeps_titles(env):
    _, auto = env.bind()
    report = env.mailbox.presence_report
    result = report(AGENT, {"kind": "claude", "cwd": "c:/work/myproj/", "session_id": "s-1", "event": "SessionStart",
                            "title": "first prompt line", "title_source": "prompt"})
    assert result["matched"] == auto["address"] and result["title"] == "first prompt line"
    row = env.mailbox.address_row(auto["address"])
    assert row["resume_id"] == "s-1" and row["resume_source"] == "hook"
    # A later prompt does not replace a title; the client's own title does.
    report(AGENT, {"kind": "claude", "cwd": WORKDIR, "session_id": "s-1", "title": "second", "title_source": "prompt"})
    assert env.mailbox.address_row(auto["address"])["title"] == "first prompt line"
    report(AGENT, {"kind": "claude", "cwd": WORKDIR, "session_id": "s-1", "title": "Login\x1b[31m fix"})
    assert env.mailbox.address_row(auto["address"])["title"] == "Login [31m fix"
    # A new session in the folder starts without the old title.
    report(AGENT, {"kind": "claude", "cwd": WORKDIR, "session_id": "s-2", "title": "new one", "title_source": "prompt"})
    assert env.mailbox.address_row(auto["address"])["title"] == "new one"
    view = {item["address"]: item for item in env.mailbox.addresses()}[auto["address"]]
    assert view["title"] == "new one"


def test_presence_report_never_guesses_and_skips_legacy_anon(env):
    h1, first = env.bind()
    env.use(h1)
    _, second = env.bind()  # same folder, -2
    assert second["address"].endswith("-2@" + AGENT)
    assert env.mailbox.presence_report(AGENT, {"kind": "claude", "cwd": WORKDIR, "session_id": "zzz"})["matched"] is None
    with env.store._lock:
        env.store._connect().execute("UPDATE addresses SET resume_id = 'known' WHERE address = ?", (second["address"],))
    assert env.mailbox.presence_report(AGENT, {"kind": "claude", "cwd": WORKDIR,
                                               "session_id": "known"})["matched"] == second["address"]
    with env.store._lock:
        env.store._connect().execute(
            "INSERT INTO addresses(address, name, agent_id, kind, workdir, wake_policy, created_at) "
            "VALUES (?, 'anon-aaaaaa', ?, 'codex', ?, 'never', 0)", (f"anon-aaaaaa@{AGENT}", AGENT, "/only/anon"))
    assert env.mailbox.presence_report(AGENT, {"kind": "codex", "cwd": "/only/anon"})["matched"] is None


# -- nudge, legacy, retention, migration -------------------------------------------------------------

def test_nudge_once_per_handle(env):
    h1, auto = env.bind()
    text = env.mailbox.nudge_for(auto["address"], h1)
    assert "session_name" in text and h1 in text and "claude-myproj@" in text
    assert env.mailbox.nudge_for(auto["address"], h1) is None
    h2, again = env.bind()
    assert env.mailbox.nudge_for(again["address"], h2) is not None  # a new handle is told once too
    _, named = env.bind(client="c2", meta={"session_name": "alpha"})
    assert env.mailbox.nudge_for(named["address"], "hx") is None
    assert env.mailbox.nudge_for(auto["address"], None) is None


def test_legacy_handles_still_get_anon_addresses(env):
    env.store.put_handle("legacy" + "0" * 26, AGENT, "proj", client_id=None)
    address = env.mailbox.address_for_handle("legacy" + "0" * 26)
    assert address == f"anon-legacy@{AGENT}"
    row = env.mailbox.address_row(address)
    assert row["wake_policy"] == "never" and row["name_source"] is None


def test_retention_removes_stale_auto_rows_without_mail(env):
    h1, keep = env.bind()
    env.mailbox.send(ME, keep["address"], "unread")
    h2, drop = env.bind(workdir="/x/other")
    env.tick(31 * 86400)
    env.mailbox.retention()  # handles expired first, then the empty auto row
    assert env.mailbox.address_row(drop["address"]) is None
    assert env.mailbox.address_row(keep["address"]) is not None


def test_migration_7_on_an_existing_database(tmp_path):
    path = tmp_path / "gateway.sqlite3"
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
    for script in MIGRATIONS[:6]:
        for statement in script.split(";"):
            if statement.strip():
                db.execute(statement)
    db.execute("INSERT INTO schema_version(version) VALUES (6)")
    db.execute("INSERT INTO addresses(address, name, agent_id, kind, wake_policy, created_at) "
               "VALUES ('anon-abc123@dev-a', 'anon-abc123', 'dev-a', 'claude', 'never', 1)")
    db.execute("INSERT INTO addresses(address, name, agent_id, kind, workdir, wake_policy, created_at) "
               "VALUES ('alpha@dev-a', 'alpha', 'dev-a', 'claude', '/p', 'auto', 1)")
    db.close()
    store = GatewayStore(path)
    try:
        assert store.schema_version() == 8 == len(MIGRATIONS)
        mailbox = Mailbox(store)
        rows = {row["address"]: row for row in mailbox.addresses(include_ended=True)}
        assert rows["anon-abc123@dev-a"]["name_source"] == "anon" and rows["alpha@dev-a"]["name_source"] == "explicit"
        assert mailbox.address_row("alpha@dev-a")["name_source"] is None  # no backfill
        found, _ = mailbox.expand(["@all"], None)
        assert found == ["alpha@dev-a"]
        assert mailbox.presence_report("dev-a", {"kind": "claude", "cwd": "/p"})["matched"] == "alpha@dev-a"
    finally:
        store.close()


# -- end to end over MCP --------------------------------------------------------------------------------

async def test_nudge_and_rename_over_mcp(tmp_path, monkeypatch):
    from test_compat_matrix import AGENT_ID
    from test_gateway_m4 import mcp_client, running_gateway
    from test_mailbox_m6 import NOTICE, start
    async with running_gateway(tmp_path, monkeypatch) as (app, port, agents, _):
        await start(tmp_path, port, agents)
        async with mcp_client(port) as a, mcp_client(port) as b:
            result = await a.call_tool_mcp("use_device", {"device_id": AGENT_ID})
            selected = result.structured_content
            auto = selected["address"]
            assert auto == f"agent-{AGENT_ID}-project@{AGENT_ID}" and selected["name_source"] == "auto"
            texts = [block.text for block in result.content]
            assert sum("session_name" in text and NOTICE in text for text in texts) == 1
            ha = selected["workspace_id"]
            again = await a.call_tool_mcp("toolsearch", {"query": "execute", "workspace_id": ha})
            assert not any("session_name" in block.text for block in again.content)

            hb = (await b.call_tool("use_device", {"device_id": AGENT_ID, "session_name": "beta",
                                                   "kind": "claude"})).data["workspace_id"]
            await b.call_tool("mail_send", {"workspace_id": hb, "to": auto, "body": "for the auto session"})
            who = (await b.call_tool("mail_who", {"workspace_id": hb})).data
            listed = {item["address"]: item for item in who["addresses"]}
            assert listed[auto]["name_source"] == "auto" and listed[auto]["unread"] == 1

            renamed = (await a.call_tool("set_workspace", {"agent_id": AGENT_ID, "workspace": "project",
                                                           "session_name": "alpha", "workspace_id": ha})).data
            assert renamed["address"] == f"alpha@{AGENT_ID}" and renamed["name_source"] == "explicit"
            assert renamed["renamed_from"].startswith(f"agent-{AGENT_ID}-project@") and renamed["mail"]["unread"] == 1
            read = (await a.call_tool("mail_read", {"workspace_id": renamed["workspace_id"]})).data
            assert [m["body"] for m in read["messages"]] == ["for the auto session"]
            assert app.state.gateway.mailbox.address_row(auto) is None


# -- verifier regressions: two live conversations, quick restarts, takeovers, team members ---------------

def _takeover(env):
    """A binds, sits idle past idle_after_s; B (same client and folder) takes the address over and is active."""
    ha, a = env.bind()
    env.tick(180)
    hb, b = env.bind()
    assert a["address"] == b["address"]
    env.mailbox.send(ME, b["address"], "secret for B")
    env.use(hb)
    return ha, hb, b["address"]


def test_superseded_handle_has_no_identity_on_an_auto_address(env):
    ha, hb, address = _takeover(env)
    assert env.mailbox.address_for_handle(hb) == address
    assert env.mailbox.address_for_handle(ha) is None and env.mailbox.displaced(ha) == address
    assert env.mailbox.displaced(hb) is None and env.mailbox.unread_count(address) == 1
    # The gateway refuses the stale handle with a clear message instead of answering as B.
    from types import SimpleNamespace

    from agent_nexus.gateway import Gateway
    fake = SimpleNamespace(mailbox=env.mailbox)
    with pytest.raises(RoutingError, match="no longer holds .*set_workspace"):
        Gateway._address_of(fake, ha)
    assert Gateway._address_of(fake, hb) == address


def test_stale_handle_cannot_rename_the_address_away(env):
    ha, hb, address = _takeover(env)
    _, renamed = env.bind(meta={"session_name": "alice", "kind": "claude"}, previous=ha)
    assert renamed["address"] == f"alice@{AGENT}" and "renamed_from" not in renamed and renamed["unread"] == 0
    assert env.mailbox.address_for_handle(hb) == address and env.mailbox.unread_count(address) == 1
    assert env.rows("SELECT superseded_by FROM handles WHERE handle = ?", hb)[0]["superseded_by"] is None


def test_stale_handle_cannot_reclaim_an_active_address(env):
    ha, hb, address = _takeover(env)
    _, again = env.bind(previous=ha)
    assert again["address"] == f"claude-myproj-2@{AGENT}"  # B is active: A gets its own address
    assert env.mailbox.address_for_handle(hb) == address


def test_rename_leaves_superseded_handles_behind(env):
    ha, hb, address = _takeover(env)
    _, renamed = env.bind(meta={"session_name": "bob", "kind": "claude"}, previous=hb)
    assert renamed["renamed_from"] == address and renamed["unread"] == 1
    assert env.mailbox.address_for_handle(hb) == f"bob@{AGENT}"
    assert env.mailbox.address_for_handle(ha) is None  # A never follows B's mail to bob


def test_explicit_name_cannot_take_another_clients_auto_address(env):
    hb, b = env.bind()
    env.mailbox.send(ME, b["address"], "secret")
    env.use(hb)
    with pytest.raises(RoutingError, match="name_taken.*automatic"):
        env.bind(client="evil", workdir="/tmp/other", meta={"session_name": "claude-myproj", "kind": "claude"})
    row = env.mailbox.address_row(b["address"])
    assert row["oauth_client_id"] == "client-1" and row["name_source"] == "auto"
    # Not even the same client while that conversation is active, nor from another folder later.
    with pytest.raises(RoutingError, match="name_taken"):
        env.bind(meta={"session_name": "claude-myproj", "kind": "claude"})
    env.tick(600)
    with pytest.raises(RoutingError, match="name_taken"):
        env.bind(workdir="/tmp/other", meta={"session_name": "claude-myproj", "kind": "claude"})
    # What layer 1 would hand over anyway (same client and folder, holder idle) is allowed.
    _, claimed = env.bind(meta={"session_name": "claude-myproj", "kind": "claude"})
    assert claimed["address"] == b["address"] and claimed["name_source"] == "explicit" and claimed["unread"] == 1


def test_takeover_clears_the_previous_conversations_resume_id_and_title(env):
    ha, a = env.bind()
    env.mailbox.presence_report(AGENT, {"kind": "claude", "cwd": WORKDIR, "session_id": "sess-a",
                                        "event": "SessionStart", "title": "work of A"})
    env.tick(600)
    _, b = env.bind()
    row = env.mailbox.address_row(b["address"])
    assert b["address"] == a["address"] and row["resume_id"] is None and row["title"] is None
    assert WakeRouter.has_route(row, "claude") is False
    # The same conversation re-binding (its handle known) keeps its own.
    hc, c = env.bind(client="c2", workdir="/x/p")
    env.mailbox.presence_report(AGENT, {"kind": "claude", "cwd": "/x/p", "session_id": "sess-c"})
    _, again = env.bind(client="c2", workdir="/x/p", previous=hc)
    assert env.mailbox.address_row(again["address"])["resume_id"] == "sess-c"


def test_quick_restart_leaves_no_permanent_fork(env):
    ha, a = env.bind()
    env.mailbox.presence_report(AGENT, {"kind": "claude", "cwd": WORKDIR, "session_id": "sess-a",
                                        "event": "SessionStart"})
    env.tick(30)
    hb, b = env.bind()
    assert b["address"] == f"claude-myproj-2@{AGENT}"  # A was used 30 s ago
    env.tick(HANDLE_TTL_S + 3600)  # the restart's handle is gone too
    hc, c = env.bind()
    assert c["address"] == a["address"]
    assert env.mailbox.address_row(b["address"])["ended_at"] is not None  # nothing can use the -2 row
    assert env.mailbox.address_row(c["address"])["resume_id"] is None
    found, _ = env.mailbox.expand(["@all"], None)
    assert found == [c["address"]]
    hit = env.mailbox.presence_report(AGENT, {"kind": "claude", "cwd": WORKDIR, "session_id": "sess-c",
                                              "event": "UserPromptSubmit", "title": "C"})
    assert hit["matched"] == c["address"]
    # A later bind of the same key may take the ended row again, after the base.
    env.use(hc)
    _, d = env.bind()
    assert d["address"] == b["address"] and env.mailbox.address_row(b["address"])["ended_at"] is None


def test_hook_ignores_auto_rows_no_live_handle_holds(env):
    h1, a = env.bind()
    env.use(h1)
    _, b = env.bind()  # -2
    with env.store._lock:  # the -2 conversation's handle is gone (expired), nothing re-bound since
        env.store._connect().execute("DELETE FROM handles WHERE address = ?", (b["address"],))
    hit = env.mailbox.presence_report(AGENT, {"kind": "claude", "cwd": WORKDIR, "session_id": "s-x"})
    assert hit["matched"] == a["address"]


def test_rename_follows_team_members_in_any_form(env):
    env.mailbox.display_device = lambda agent: {AGENT: "pc-main"}.get(agent, agent)

    def resolve(value):
        table = {"pc-main": AGENT, AGENT: AGENT, "dev-b": "dev-b"}
        if value not in table:
            raise RoutingError("unknown device")
        return table[value]
    env.mailbox.resolve_device = resolve
    ha, a = env.bind()
    _, other = env.bind(agent_id="dev-b")  # the same name on another device
    assert other["address"] == "claude-myproj@dev-b"
    env.mailbox.set_group("crew", ["claude-myproj@pc-main", "claude-myproj@dev-b", "someone@else"])
    env.mailbox.set_group("solo", [a["address"]])
    env.mailbox.send(ME, ["claude-myproj@pc-main"], "typed with the display name")
    _, renamed = env.bind(meta={"session_name": "reviewer"}, previous=ha)
    groups = {r["name"]: json.loads(r["members_json"]) for r in env.rows("SELECT * FROM groups")}
    assert groups["crew"] == ["reviewer@pc-main", "claude-myproj@dev-b", "someone@else"]
    assert groups["solo"] == [f"reviewer@{AGENT}"]
    assert env.mailbox.expand(["@team:crew"], None) == ([f"reviewer@{AGENT}", "claude-myproj@dev-b"], ["someone@else"])
    exprs = [json.loads(r["to_expr"]) for r in env.rows("SELECT to_expr FROM messages")]
    assert ["reviewer@pc-main"] in exprs


def test_legacy_anon_with_read_mail_is_listed(env):
    with env.store._lock:
        env.store._connect().execute(
            "INSERT INTO addresses(address, name, agent_id, kind, wake_policy, created_at, last_seen_at) "
            "VALUES (?, 'anon-abcdef', ?, 'claude', 'never', 0, 0)", (f"anon-abcdef@{AGENT}", AGENT))
    assert f"anon-abcdef@{AGENT}" not in {item["address"] for item in env.mailbox.addresses()}
    message = env.mailbox.send(ME, f"anon-abcdef@{AGENT}", "old")["message_id"]
    env.mailbox.mark_read(message, f"anon-abcdef@{AGENT}")
    listed = {item["address"]: item for item in env.mailbox.addresses()}
    assert listed[f"anon-abcdef@{AGENT}"]["mail"] == 1 and listed[f"anon-abcdef@{AGENT}"]["unread"] == 0


# -- a conversation keeps its own earlier workspace_ids (its rebinds are not takeovers) -----------------

def _rebind(env):
    """One conversation: use_device (h1), then set_workspace in the same folder (h2 supersedes h1)."""
    h1, a = env.bind()
    env.tick(5)
    h2, b = env.bind(previous=h1)
    assert a["address"] == b["address"] == f"claude-myproj@{AGENT}"
    assert env.rows("SELECT superseded_by, displaced FROM handles WHERE handle = ?", h1)[0] == \
        {"superseded_by": h2, "displaced": 0}
    env.mailbox.send(ME, b["address"], "for this conversation")
    env.tick(5)
    return h1, h2, b["address"]


def test_own_superseded_handle_keeps_the_identity(env):
    h1, h2, address = _rebind(env)
    assert env.mailbox.displaced(h1) is None and env.mailbox.address_for_handle(h1) == address
    from types import SimpleNamespace

    from agent_nexus.gateway import Gateway
    assert Gateway._address_of(SimpleNamespace(mailbox=env.mailbox), h1) == address


def test_own_superseded_handle_rebinds_without_forking(env):
    h1, h2, address = _rebind(env)
    env.use(h2)
    _, again = env.bind(previous=h1)
    assert again["address"] == address and again["unread"] == 1  # no -2: h2 is this conversation's own


def test_own_superseded_handle_renames_with_the_mail(env):
    h1, h2, address = _rebind(env)
    env.use(h2)
    h3, renamed = env.bind(meta={"session_name": "mine"}, previous=h1)
    assert renamed["address"] == f"mine@{AGENT}" and renamed["renamed_from"] == address
    assert renamed["unread"] == 1 and env.mailbox.address_row(address) is None
    for handle in (h1, h2, h3):  # every workspace_id of the conversation follows
        assert env.mailbox.address_for_handle(handle) == f"mine@{AGENT}"


def test_takeover_after_rebinds_displaces_the_whole_conversation(env):
    h1, h2, address = _rebind(env)
    env.tick(180)  # the conversation is idle; another one in the folder takes the address
    hb, b = env.bind()
    assert b["address"] == address
    env.use(hb)
    for handle in (h1, h2):
        assert env.mailbox.displaced(handle) == address and env.mailbox.address_for_handle(handle) is None
    _, again = env.bind(previous=h1)
    assert again["address"] == f"claude-myproj-2@{AGENT}" and again["unread"] == 0
    _, alice = env.bind(meta={"session_name": "alice"}, previous=h1)
    assert "renamed_from" not in alice and env.mailbox.address_for_handle(hb) == address


def test_explicit_claim_of_an_idle_auto_address_displaces_its_handles(env):
    h1, a = env.bind()
    env.tick(180)
    hb, claimed = env.bind(meta={"session_name": "claude-myproj"})
    assert claimed["address"] == a["address"] and claimed["name_source"] == "explicit"
    assert env.mailbox.displaced(h1) == a["address"] and env.mailbox.address_for_handle(h1) is None
    assert env.mailbox.address_for_handle(hb) == a["address"]


def test_migration_8_adds_displaced_to_existing_handles(tmp_path):
    path = tmp_path / "gateway.sqlite3"
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
    for script in MIGRATIONS[:7]:
        for statement in script.split(";"):
            if statement.strip():
                db.execute(statement)
    db.execute("INSERT INTO schema_version(version) VALUES (7)")
    db.close()
    store = GatewayStore(path)
    try:
        assert store.schema_version() == 8
        store.put_handle("h" * 32, AGENT, "proj", client_id="c")
        with store._lock:
            row = store._connect().execute("SELECT displaced FROM handles").fetchone()
        assert row["displaced"] == 0
    finally:
        store.close()


async def test_first_workspace_id_keeps_mail_and_renames_over_mcp(tmp_path, monkeypatch):
    from test_compat_matrix import AGENT_ID
    from test_gateway_m4 import mcp_client, running_gateway
    from test_mailbox_m6 import NOTICE, start
    async with running_gateway(tmp_path, monkeypatch) as (app, port, agents, _):
        await start(tmp_path, port, agents)
        async with mcp_client(port) as a, mcp_client(port) as b:
            h1 = (await a.call_tool("use_device", {"device_id": AGENT_ID})).data["workspace_id"]
            second = (await a.call_tool("set_workspace", {"agent_id": AGENT_ID, "workspace": "project",
                                                          "workspace_id": h1})).data
            auto = second["address"]
            assert second["workspace_id"] != h1 and auto.startswith("agent-")
            hb = (await b.call_tool("use_device", {"device_id": AGENT_ID, "session_name": "beta",
                                                   "kind": "claude"})).data["workspace_id"]
            await b.call_tool("mail_send", {"workspace_id": hb, "to": auto, "body": "to the first id"})
            # The first workspace_id still reads the conversation's mail and gets its notices.
            status = await a.call_tool_mcp("toolsearch", {"query": "execute", "workspace_id": h1})
            assert any(NOTICE in block.text for block in status.content)
            read = (await a.call_tool("mail_read", {"workspace_id": h1, "mark_read": False})).data
            assert [m["body"] for m in read["messages"]] == ["to the first id"]
            renamed = (await a.call_tool("set_workspace", {"agent_id": AGENT_ID, "workspace": "project",
                                                           "session_name": "mine", "workspace_id": h1})).data
            assert renamed["address"] == f"mine@{AGENT_ID}" and renamed["renamed_from"].split("@")[0] == auto.split("@")[0]
            assert renamed["mail"]["unread"] == 1
            assert app.state.gateway.mailbox.address_for_handle(h1) == f"mine@{AGENT_ID}"
