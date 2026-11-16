"""Milestone M6: the cloud mailbox and the unread notice (design §8, §13 M6; decisions D9, D11).

End to end over real WebSockets and the MCP endpoint on ephemeral ports, with
temporary state directories. Devices are in-process FastMCP servers; toasts go
to :class:`agent_nexus.toast.FakeToaster`, so nothing is ever shown on screen,
and no real claude / codex / chatx is involved.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastmcp import Client, FastMCP
from pydantic import BaseModel, ConfigDict

from test_compat_matrix import AGENT_ID, SERVER_TOKEN, device_server, extra_server, package
from test_gateway_m4 import HOST_A, HOST_B, SECOND_ID, mcp_client, running_gateway

NOTICE = "[agent-nexus 邮箱]"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


def strict_device(machine: str, *, notify_calls: list | None = None) -> FastMCP:
    server = device_server(machine)

    @server.tool
    async def strict() -> Strict:
        """A tool whose outputSchema forbids extra keys."""
        return Strict(value=machine)

    if notify_calls is not None:
        @server.tool
        async def notify(title: str, body: str = "", level: str = "info") -> dict:
            notify_calls.append({"title": title, "body": body, "level": level})
            return {"status": "ok", "shown": True}

    return server


async def start(tmp_path, port, agents, *, agent_id=AGENT_ID, token_env="AGENT_TOKEN", hostname=HOST_A,
                toaster=None, hub=None, transport=None):
    agent_module = package("current", "agent")
    workdir = (tmp_path / f"{agent_id}-project").resolve()
    workdir.mkdir(exist_ok=True)
    cloud = {"agent_id": agent_id, "url": f"ws://127.0.0.1:{port}", "token_env": token_env, "hostname": hostname,
             "workspaces": {"project": {"path": str(workdir)}}, "refresh_interval_s": 60,
             "console_url": "http://127.0.0.1:1"}
    ready = asyncio.Event()
    agents.append(asyncio.create_task(agent_module.run_agent(
        tmp_path, cloud, transports={"workspace": transport or strict_device(agent_id), "extra": extra_server()},
        ready=ready, status={}, hub=hub, toaster=toaster)))
    await asyncio.wait_for(ready.wait(), 20)


async def use(client, name, kind="claude", device=AGENT_ID):
    selected = (await client.call_tool("use_device", {"device_id": device, "session_name": name, "kind": kind})).data
    return selected["workspace_id"], selected


def last_text(result) -> str:
    return result.content[-1].text


async def eventually(predicate, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def api(port):
    return httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                             headers={"Authorization": f"Bearer {SERVER_TOKEN}"})


# -- identity, notice, both failure shapes, strict schemas ---------------------------

async def test_notice_on_every_result_including_both_failure_shapes(tmp_path, monkeypatch):
    async with running_gateway(tmp_path, monkeypatch) as (app, port, agents, _):
        await start(tmp_path, port, agents)
        async with mcp_client(port) as a, mcp_client(port) as b:
            ha, selected_a = await use(a, "alpha")
            hb, selected_b = await use(b, "beta", kind="codex")
            assert selected_a["address"] == f"alpha@{AGENT_ID}"
            assert selected_a["address_display"] == f"alpha@{HOST_A}" and selected_a["mail"] == {"unread": 0}
            # Old clients keep every field they relied on (superset).
            assert {"workspace_id", "agent_id", "workspace", "mcp"} <= set(selected_b)

            sent = (await a.call_tool("mail_send", {"workspace_id": ha, "to": f"beta@{HOST_A}",
                                                    "subject": "构建失败日志", "body": "see attached"})).data
            assert [d["to"] for d in sent["deliveries"]] == [f"beta@{AGENT_ID}"]
            assert sent["deliveries"][0]["action"] == "noticed_on_next_call" and sent["unknown_recipients"] == []

            # An unrelated tool: the notice is the last text block, in _meta and structuredContent.
            result = await b.call_tool_mcp("toolsearch", {"query": "execute", "workspace_id": hb})
            assert not result.is_error and last_text(result).startswith(NOTICE)
            assert f"beta@{HOST_A}" in last_text(result) and sent["message_id"] in last_text(result)
            assert "构建失败日志" in last_text(result) and len(last_text(result)) <= 600
            assert result.meta["agent-nexus/mail"]["unread"] == 1
            assert result.structured_content["_mail"]["unread"] == 1
            assert result.structured_content["tools"]  # the tool's own payload is intact

            # A native tool raising RoutingError: still an error, still the notice.
            raised = await b.call_tool_mcp("mcpdetail", {"mcp_id": "nope", "workspace_id": hb})
            assert raised.is_error and "mcp_id must be instance/service" in raised.content[0].text
            assert last_text(raised).startswith(NOTICE) and raised.meta["agent-nexus/mail"]["unread"] == 1
            # A direct device tool returning an error result (ToolFailure / isError).
            failed = await b.call_tool_mcp("failure", {"workspace_id": hb})
            assert failed.is_error and failed.content[0].text == "expected failure"
            assert last_text(failed).startswith(NOTICE)
            # Unknown tools still answer "Unknown tool", with the notice.
            unknown = await b.call_tool_mcp("no_such_tool", {"workspace_id": hb})
            assert unknown.is_error and "Unknown tool" in unknown.content[0].text and last_text(unknown).startswith(NOTICE)

            # A strict outputSchema: the client still validates; only text and _meta carry the notice.
            strict = await b.call_tool("strict", {"workspace_id": hb})
            assert strict.data.value == AGENT_ID and "_mail" not in (strict.structured_content or {})
            assert last_text(strict).startswith(NOTICE) and strict.meta["agent-nexus/mail"]["unread"] == 1

            # The sender sees nothing; reading clears the notice.
            assert not last_text(await a.call_tool_mcp("toolsearch", {"query": "execute", "workspace_id": ha})).startswith(NOTICE)
            read = (await b.call_tool("mail_read", {"workspace_id": hb})).data
            assert read["messages"][0]["body"] == "see attached" and read["unread_remaining"] == 0
            after = await b.call_tool_mcp("toolsearch", {"query": "execute", "workspace_id": hb})
            assert not last_text(after).startswith(NOTICE) and "agent-nexus/mail" not in (after.meta or {})
            assert "_mail" not in after.structured_content

            # Re-binding the same name and kind keeps the address and its mail; another kind is refused.
            await a.call_tool("mail_send", {"workspace_id": ha, "to": "beta", "body": "second"})
            hb2, again = await use(b, "beta", kind="codex")
            assert again["address"] == f"beta@{AGENT_ID}" and again["mail"] == {"unread": 1}
            taken = await b.call_tool_mcp("use_device", {"device_id": AGENT_ID, "session_name": "beta", "kind": "gpt"})
            assert taken.is_error and "name_taken" in taken.content[0].text and "beta-gpt" in taken.content[0].text
            # Clients without session_name get an automatic name: <kind>-<workdir> (design §8.2 layer 1);
            # no OAuth client here, so the kind is unknown and the label is "agent".
            auto = (await b.call_tool("set_workspace", {"agent_id": AGENT_ID, "workspace": "project"})).data
            assert auto["address"] == f"agent-{AGENT_ID}-project@{AGENT_ID}" and auto["name_source"] == "auto"


async def test_groups_who_wait_bye_and_missed_notices(tmp_path, monkeypatch):
    async with running_gateway(tmp_path, monkeypatch) as (app, port, agents, _):
        await start(tmp_path, port, agents)
        await start(tmp_path, port, agents, agent_id=SECOND_ID, token_env="SECOND_TOKEN", hostname=HOST_B)
        async with mcp_client(port) as client:
            ha, _ = await use(client, "alpha")
            hb, _ = await use(client, "beta", kind="codex")
            hc, _ = await use(client, "gamma", kind="codex", device=SECOND_ID)
            # @device: expands to that device's sessions (the sender excluded), by display name too.
            sent = (await client.call_tool("mail_send", {"workspace_id": ha, "to": f"@device:{HOST_A}", "body": "x"})).data
            assert [d["to"] for d in sent["deliveries"]] == [f"beta@{AGENT_ID}"]
            kinds = (await client.call_tool("mail_send", {"workspace_id": ha, "to": "@kind:codex", "body": "y"})).data
            assert sorted(d["to"] for d in kinds["deliveries"]) == [f"beta@{AGENT_ID}", f"gamma@{SECOND_ID}"]
            everyone = (await client.call_tool("mail_send", {"workspace_id": ha, "to": ["@all", "nobody@nowhere"],
                                                             "body": "z"})).data
            assert len(everyone["deliveries"]) == 2 and everyone["unknown_recipients"] == ["nobody@nowhere"]
            missing = await client.call_tool_mcp("mail_send", {"workspace_id": ha, "to": "nobody", "body": "z"})
            assert missing.is_error and "no known recipients" in missing.content[0].text

            who = (await client.call_tool("mail_who", {"workspace_id": hb})).data
            assert who["me"] == f"beta@{AGENT_ID}"
            rows = {row["address"]: row for row in who["addresses"]}
            assert rows[f"gamma@{SECOND_ID}"]["address_display"] == f"gamma@{HOST_B}"
            assert rows[f"beta@{AGENT_ID}"]["presence"] == "active" and rows[f"beta@{AGENT_ID}"]["unread"] == 3
            assert rows["me"]["kind"] == "human"
            assert {d["agent_id"] for d in who["devices"]} == {AGENT_ID, SECOND_ID}

            # mail_wait returns as soon as mail arrives.
            await client.call_tool("mail_read", {"workspace_id": hc, "thread_id": kinds["thread_id"]})
            await client.call_tool("mail_read", {"workspace_id": hc, "thread_id": everyone["thread_id"]})
            waiter = asyncio.create_task(client.call_tool("mail_wait", {"workspace_id": hc, "timeout_s": 20}))
            await asyncio.sleep(0.3)
            assert not waiter.done()
            await client.call_tool("mail_send", {"workspace_id": ha, "to": "gamma", "body": "ping", "priority": "urgent"})
            waited = (await asyncio.wait_for(waiter, 10)).data
            assert [m["snippet"] for m in waited["messages"]][0] == "ping" and not waited["timed_out"]

            # A call without any identity is counted as a missed notice.
            before = (await client.call_tool("mail_who", {})).data["counters"]["notice_missed"]
            await client.call_tool("device_list", {})
            after = (await client.call_tool("mail_who", {})).data["counters"]["notice_missed"]
            assert after == before + 2  # the device_list and the first mail_who themselves
            async with httpx.AsyncClient(trust_env=False) as http:
                health = (await http.get(f"http://127.0.0.1:{port}/healthz?detail=1")).json()
            assert health["detail"]["mailbox"]["counters"]["notice_missed"] >= after

            bye = (await client.call_tool("mail_bye", {"workspace_id": hc})).data
            assert bye["state"] == "ended"
            who = (await client.call_tool("mail_who", {"include_ended": True})).data
            assert {row["address"]: row["presence"] for row in who["addresses"]}[f"gamma@{SECOND_ID}"] == "ended"


async def test_mail_addresses_and_handles_survive_a_gateway_restart(tmp_path, monkeypatch):
    async with running_gateway(tmp_path, monkeypatch) as (app, port, agents, _):
        await start(tmp_path, port, agents)
        async with mcp_client(port) as client:
            ha, _ = await use(client, "alpha")
            hb, _ = await use(client, "beta", kind="codex")
            sent = (await client.call_tool("mail_send", {"workspace_id": ha, "to": "beta", "body": "kept"})).data
    async with running_gateway(tmp_path, monkeypatch) as (app, port, agents, _):
        await start(tmp_path, port, agents)
        async with mcp_client(port) as client:
            inbox = (await client.call_tool("mail_inbox", {"workspace_id": hb})).data
            assert inbox["me"] == f"beta@{AGENT_ID}" and [m["id"] for m in inbox["messages"]] == [sent["message_id"]]
            notice = await client.call_tool_mcp("device_list", {"workspace_id": hb})
            assert last_text(notice).startswith(NOTICE)
            assert (await client.call_tool("mail_status", {"message_id": sent["message_id"]})).data["recipients"][0]["status"] == "delivered"


# -- sender status and recall (decision D9) -----------------------------------------------

async def test_status_and_recall(tmp_path, monkeypatch):
    async with running_gateway(tmp_path, monkeypatch) as (app, port, agents, _):
        await start(tmp_path, port, agents)
        async with mcp_client(port) as client:
            ha, _ = await use(client, "alpha")
            hb, _ = await use(client, "beta", kind="codex")
            hc, _ = await use(client, "gamma", kind="codex")

            async def status(message_id):
                data = (await client.call_tool("mail_status", {"workspace_id": ha, "message_id": message_id})).data
                return {r["to"]: r["status"] for r in data["recipients"]}

            first = (await client.call_tool("mail_send", {"workspace_id": ha, "to": "beta", "body": "one"})).data
            assert await status(first["message_id"]) == {f"beta@{AGENT_ID}": "delivered"}
            await client.call_tool("mail_read", {"workspace_id": hb, "message_id": first["message_id"]})
            assert await status(first["message_id"]) == {f"beta@{AGENT_ID}": "read"}

            second = (await client.call_tool("mail_send", {"workspace_id": ha, "to": "beta", "body": "oops",
                                                           "subject": "secret"})).data
            recalled = (await client.call_tool("mail_recall", {"workspace_id": ha, "message_id": second["message_id"]})).data
            assert recalled["recalled"] == [f"beta@{AGENT_ID}"] and recalled["not_recalled"] == []
            assert await status(second["message_id"]) == {f"beta@{AGENT_ID}": "recalled"}
            assert (await client.call_tool("mail_inbox", {"workspace_id": hb})).data["messages"] == []
            assert not last_text(await client.call_tool_mcp("device_list", {"workspace_id": hb})).startswith(NOTICE)
            gone = (await client.call_tool("mail_read", {"workspace_id": hb, "message_id": second["message_id"]})).data
            assert gone["state"] == "recalled" and gone["messages"] == [] and "oops" not in json.dumps(gone)

            late = (await client.call_tool("mail_recall", {"workspace_id": ha, "message_id": first["message_id"]})).data
            assert late["recalled"] == [] and late["not_recalled"] == [{"to": f"beta@{AGENT_ID}", "reason": "already_read"}]
            third = (await client.call_tool("mail_send", {"workspace_id": ha, "to": "beta", "body": "three"})).data
            stranger = (await client.call_tool("mail_recall", {"workspace_id": hb, "message_id": third["message_id"]})).data
            assert stranger["not_recalled"] == [{"to": f"beta@{AGENT_ID}", "reason": "not_sender"}]

            group = (await client.call_tool("mail_send", {"workspace_id": ha, "to": ["beta", "gamma"], "body": "g"})).data
            await client.call_tool("mail_read", {"workspace_id": hc, "message_id": group["message_id"]})
            partial = (await client.call_tool("mail_recall", {"workspace_id": ha, "message_id": group["message_id"]})).data
            assert partial["recalled"] == [f"beta@{AGENT_ID}"]
            assert partial["not_recalled"] == [{"to": f"gamma@{AGENT_ID}", "reason": "already_read"}]


# -- retention (injectable clock) ------------------------------------------------------------

def test_retention_removes_read_mail_after_90_days_only(tmp_path):
    from agent_nexus.mailbox import Mailbox
    from agent_nexus.store import GatewayStore
    now = [1_000_000.0]
    store = GatewayStore(tmp_path / "gateway.sqlite3", clock=lambda: now[0])
    mailbox = Mailbox(store)
    store.put_handle("h1", AGENT_ID, "project")
    store.put_handle("h2", AGENT_ID, "project")
    sender = mailbox.bind("h1", AGENT_ID, workdir=None, meta={"session_name": "alpha"}, client_id=None)["address"]
    reader = mailbox.bind("h2", AGENT_ID, workdir=None, meta={"session_name": "beta"}, client_id=None)["address"]
    old = mailbox.send(sender, reader, "old")["message_id"]
    unread = mailbox.send(sender, reader, "never read")["message_id"]
    mailbox.read(reader, message_id=old)
    now[0] += 89 * 86400
    recent = mailbox.send(sender, reader, "recent")["message_id"]
    mailbox.read(reader, message_id=recent)
    assert mailbox.retention()["messages"] == 0  # 89 days: everything stays
    now[0] += 2 * 86400
    removed = mailbox.retention()
    assert removed["messages"] == 1 and removed["deliveries"] == 1
    with pytest.raises(Exception, match="unknown message"):
        mailbox.read(reader, message_id=old)
    assert mailbox.read(reader, message_id=recent, mark_read=False)["messages"][0]["body"] == "recent"
    # Unread mail is not retention's business (M7 escalation expires it at 30 days).
    assert mailbox.read(reader, message_id=unread, mark_read=False)["messages"][0]["state"] == "unread"
    store.close()


def test_device_ask_answers_and_expiries_never_stay_unread(tmp_path):
    from agent_nexus.mailbox import DEVICE_ASK_NAME, Mailbox
    from agent_nexus.store import GatewayStore
    now = [1_000_000.0]
    store = GatewayStore(tmp_path / "gateway.sqlite3", clock=lambda: now[0])
    mailbox = Mailbox(store)
    asker = mailbox.ensure_address(DEVICE_ASK_NAME, AGENT_ID)
    answered = mailbox.ask_create(asker, "部署到哪里？", options=["a", "b"], origin="request_user_input")
    reply = mailbox.ask_answer(answered["ask_id"], choice="b")
    assert mailbox.ask_get(answered["ask_id"])["answer"]["choice"] == "b"
    # The device polls ask_get; its answer mail is filed as read, not left for nobody.
    assert mailbox.unread_count(asker) == 0
    assert mailbox.read(asker, message_id=reply["message_id"], mark_read=False)["messages"][0]["state"] == "read"
    mailbox.ask_create(asker, "过期的问题", origin="request_user_input")
    now[0] += 31 * 86400
    assert mailbox.retention()["asks_expired"] == 1
    assert mailbox.unread_count(asker) == 0  # the expiry notice too
    # Rows left unread by an older build are filed as read and age out.
    store._connect().execute("UPDATE deliveries SET state = 'unread', read_at = NULL WHERE to_addr = ?", (asker,))
    mailbox.retention()
    assert mailbox.unread_count(asker) == 0
    now[0] += 200 * 86400
    mailbox.retention()
    count = store._connect().execute("SELECT COUNT(*) FROM deliveries WHERE to_addr = ?", (asker,)).fetchone()[0]
    assert count == 0
    store.close()


def test_retention_vacuums_at_most_once_a_week(tmp_path):
    from agent_nexus.mailbox import Mailbox
    from agent_nexus.store import GatewayStore
    now = [1_000_000.0]
    store = GatewayStore(tmp_path / "gateway.sqlite3", clock=lambda: now[0])
    mailbox = Mailbox(store)
    assert mailbox.retention()["vacuumed"] == 1
    now[0] += 6 * 86400
    assert mailbox.retention()["vacuumed"] == 0
    store.close()
    reopened = GatewayStore(tmp_path / "gateway.sqlite3", clock=lambda: now[0])
    mailbox = Mailbox(reopened)
    assert mailbox.retention()["vacuumed"] == 0  # the last run survives a restart
    now[0] += 86400
    assert mailbox.retention()["vacuumed"] == 1
    reopened.close()


async def test_a_session_can_mail_itself(tmp_path, monkeypatch):
    """§8.6: the real clients are measured by a session mailing itself first."""
    async with running_gateway(tmp_path, monkeypatch) as (app, port, agents, _):
        await start(tmp_path, port, agents)
        async with mcp_client(port) as client:
            h, _ = await use(client, "probe")
            other, _ = await use(client, "other")
            for to in ("probe", f"probe@{AGENT_ID}", f"probe@{HOST_A}"):
                sent = (await client.call_tool("mail_send", {"workspace_id": h, "to": to, "body": f"to {to}"})).data
                assert [d["to"] for d in sent["deliveries"]] == [f"probe@{AGENT_ID}"]
            result = await client.call_tool_mcp("toolsearch", {"query": "execute", "workspace_id": h})
            assert not result.is_error and last_text(result).startswith(NOTICE)
            assert result.meta["agent-nexus/mail"]["unread"] == 3
            # Groups still leave the sender out.
            group = (await client.call_tool("mail_send", {"workspace_id": h, "to": f"@device:{HOST_A}",
                                                          "body": "g"})).data
            assert [d["to"] for d in group["deliveries"]] == [f"other@{AGENT_ID}"]
            assert other


async def test_notice_falls_back_to_the_oauth_client_id(tmp_path, monkeypatch):
    """§8.6 identity fallback: no workspace_id, one recent address for this client -> its notice."""
    from agent_nexus import gateway as gateway_module
    monkeypatch.setattr(gateway_module, "_client_id", lambda: "dcr-client-1")
    async with running_gateway(tmp_path, monkeypatch) as (app, port, agents, _):
        await start(tmp_path, port, agents)
        async with mcp_client(port) as client:
            h, _ = await use(client, "fallback")
            await client.call_tool("mail_send", {"workspace_id": h, "to": "fallback", "body": "hi"})
            before = (await client.call_tool("mail_who", {"workspace_id": h})).data["counters"]["notice_fallback_used"]
        async with mcp_client(port) as fresh:  # a new MCP session: no session state, no workspace_id
            result = await fresh.call_tool_mcp("toolsearch", {"query": "execute"})
            assert not result.is_error and last_text(result).startswith(NOTICE)
            assert f"fallback@{HOST_A}" in last_text(result)
            after = (await fresh.call_tool("mail_who", {})).data["counters"]["notice_fallback_used"]
            assert after >= before + 1


# -- ask_user, toasts and the inbox (decision D11) ---------------------------------------------

async def test_ask_user_cards_toasts_answers_and_blocking(tmp_path, monkeypatch):
    from agent_nexus.agent import HubLink
    from agent_nexus.toast import FakeToaster
    toaster, hub = FakeToaster(), HubLink()
    async with running_gateway(tmp_path, monkeypatch) as (app, port, agents, _):
        await start(tmp_path, port, agents, toaster=toaster, hub=hub)
        assert "toast_actions/1" in app.state.hub.peers[AGENT_ID].features and "ask/1" in hub.features
        async with mcp_client(port) as client, api(port) as http:
            ha, _ = await use(client, "claude-unity")
            asked = (await client.call_tool("ask_user", {"workspace_id": ha, "question": "用哪个分支发版？",
                                                         "options": ["main", "release/0.2"],
                                                         "recommended": "release/0.2"})).data
            assert asked["state"] == "open" and asked["ask_id"].startswith("q_")
            card = (await http.get(f"/api/inbox/asks/{asked['ask_id']}")).json()
            assert card["options"] == ["release/0.2", "main"] and card["allow_other"] is True
            assert await eventually(lambda: toaster.shown)
            shown = toaster.shown[0]
            assert shown["link"] == f"#/inbox/{asked['thread_id']}" and shown["tag"] == f"anx-ask-{asked['ask_id']}"
            assert [a["label"] for a in shown["actions"]] == ["release/0.2（推荐）", "main"]
            threads = (await http.get("/api/inbox/threads")).json()
            assert threads["threads"][0]["open_asks"] == 1 and threads["open_asks"][0]["ask_id"] == asked["ask_id"]

            # Answering from the toast (hub_call ask_answer): kind=answer mail back to the asker.
            answered = await hub.call("ask_answer", {"ask_id": asked["ask_id"], "choice": "release/0.2", "via": "toast"})
            assert answered["state"] == "answered" and answered["answer"] == {"choice": "release/0.2", "via": "toast"}
            mail = (await client.call_tool("mail_read", {"workspace_id": ha})).data["messages"][0]
            assert mail["kind"] == "answer" and mail["from_address"] == "me" and mail["body"] == "已选择：release/0.2"
            assert mail["meta"]["answer"]["choice"] == "release/0.2" and mail["reply_to"] == asked["message_id"]
            assert await eventually(lambda: f"anx-ask-{asked['ask_id']}" in toaster.dismissed)
            again = (await http.post(f"/api/inbox/asks/{asked['ask_id']}/answer", json={"choice": "main"})).json()
            assert again["already"] is True and "toast" in again["message"]

            # More options than toast buttons: the toast only opens the inbox.
            many = (await client.call_tool("ask_user", {"workspace_id": ha, "question": "选一个",
                                                        "options": ["a", "b", "c", "d"]})).data
            assert await eventually(lambda: len(toaster.shown) >= 2)
            assert [a["id"] for a in toaster.shown[1]["actions"]] == ["open"]
            assert (await http.post(f"/api/inbox/asks/{many['ask_id']}/answer", json={"text": "都不要"})).json()["answer"]["text"] == "都不要"

            # blocking=true: answered within the timeout -> returned directly, and the answer mail is not unread.
            blocking = asyncio.create_task(client.call_tool("ask_user", {"workspace_id": ha, "question": "继续吗？",
                                                                         "options": ["yes", "no"], "blocking": True,
                                                                         "timeout_s": 20}))
            open_asks = []
            for _ in range(200):
                open_asks = (await http.get("/api/inbox/asks")).json()["asks"]
                if open_asks:
                    break
                await asyncio.sleep(0.05)
            await http.post(f"/api/inbox/asks/{open_asks[0]['ask_id']}/answer", json={"choice": "yes"})
            result = (await asyncio.wait_for(blocking, 10)).data
            assert result["state"] == "answered" and result["answer"]["choice"] == "yes"
            inbox = (await client.call_tool("mail_inbox", {"workspace_id": ha})).data
            # Only the non-blocking "选一个" answer is unread; the blocking one was handed over directly.
            assert [m["kind"] for m in inbox["messages"]].count("answer") == 1
            timed_out = (await client.call_tool("ask_user", {"workspace_id": ha, "question": "在吗？", "blocking": True,
                                                             "timeout_s": 1})).data
            assert timed_out["state"] == "open"
            anonymous = await client.call_tool_mcp("ask_user", {"question": "who am I?"})
            assert anonymous.is_error and "workspace_id" in anonymous.content[0].text


async def test_mail_to_me_inbox_reply_and_toast_fallback(tmp_path, monkeypatch):
    from agent_nexus.agent import HubLink
    from agent_nexus.toast import FakeToaster
    features = package("current", "features")
    toaster, hub, notified = FakeToaster(), HubLink(), []
    async with running_gateway(tmp_path, monkeypatch) as (app, port, agents, _):
        with monkeypatch.context() as patch:
            # An older device: no toast_actions/1, but a device plugin with notify.
            patch.setattr(features, "AGENT_FEATURES", ("device-contract/1", "mcp-alias/1", "hub_call/1"))
            await start(tmp_path, port, agents, agent_id=SECOND_ID, token_env="SECOND_TOKEN", hostname=HOST_B,
                        transport=strict_device(SECOND_ID, notify_calls=notified))
        await start(tmp_path, port, agents, toaster=toaster, hub=hub)
        async with mcp_client(port) as client, api(port) as http:
            ha, _ = await use(client, "alpha")
            sent = (await client.call_tool("mail_send", {"workspace_id": ha, "to": "me", "subject": "请 review PR",
                                                         "body": "PR #12"})).data
            assert sent["deliveries"] == [{"to": "me", "to_display": "me", "presence": "owner", "action": "inbox"}]
            assert await eventually(lambda: toaster.shown and notified)
            assert toaster.shown[0]["link"] == f"#/inbox/{sent['thread_id']}" and "请 review PR" in toaster.shown[0]["title"]
            assert notified[0]["title"] == toaster.shown[0]["title"]  # plain notify, no buttons

            threads = (await http.get("/api/inbox/threads")).json()
            assert threads["unread"] == 1 and threads["threads"][0]["thread_id"] == sent["thread_id"]
            thread = (await http.get(f"/api/inbox/threads/{sent['thread_id']}")).json()
            assert thread["messages"][0]["body"] == "PR #12" and thread["unread_remaining"] == 0
            # Reply as me: recipients default to the other side of the thread.
            reply = (await http.post("/api/inbox/send", json={"reply_to": sent["message_id"], "body": "LGTM"})).json()
            assert [d["to"] for d in reply["deliveries"]] == [f"alpha@{AGENT_ID}"]
            got = await client.call_tool_mcp("device_list", {"workspace_id": ha})
            assert last_text(got).startswith(NOTICE) and "来自 me" in last_text(got)
            # Viewing another mailbox is read-only: nothing is marked read for it.
            peek = (await http.get(f"/api/inbox/threads/{sent['thread_id']}", params={"viewer": f"alpha@{AGENT_ID}"})).json()
            assert peek["viewer"] == f"alpha@{AGENT_ID}"
            assert (await client.call_tool("mail_inbox", {"workspace_id": ha})).data["unread"] == 1
            # Address settings (design §8.7): wake_permission is validated.
            changed = (await http.put(f"/api/addresses/alpha@{AGENT_ID}", json={"wake_permission": "inherit"})).json()
            assert changed["address"]["wake_permission"] == "inherit"
            assert (await http.put(f"/api/addresses/alpha@{AGENT_ID}", json={"wake_permission": "root"})).status_code == 400
            # The local console's inbox goes through hub_call mail_local.
            local = await hub.call("mail_local", {"op": "threads"})
            assert local["threads"][0]["thread_id"] == sent["thread_id"]
            assert (await http.get("/api/inbox/threads", headers={"Authorization": "Bearer wrong"})).status_code == 401


async def test_request_user_input_asks_reach_the_inbox_through_hub_call(tmp_path, monkeypatch):
    from agent_nexus.agent import HubLink
    hub = HubLink()
    async with running_gateway(tmp_path, monkeypatch) as (app, port, agents, _):
        await start(tmp_path, port, agents, hub=hub, toaster=__import__("agent_nexus.toast").toast.FakeToaster())
        created = await hub.call("ask_create", {"origin": "request_user_input", "question": "部署到哪里？",
                                                "kind": "single", "options": ["a", "b"], "allow_custom": False})
        ask = await hub.call("ask_get", {"ask_id": created["ask_id"]})
        assert ask["origin"] == "request_user_input" and ask["from_address"] == f"device-ask@{AGENT_ID}"
        assert ask["allow_other"] is False and ask["state"] == "open"
        async with api(port) as http:
            assert (await http.post(f"/api/inbox/asks/{created['ask_id']}/answer", json={"text": "c"})).status_code == 400
            await http.post(f"/api/inbox/asks/{created['ask_id']}/answer", json={"choice": "b"})
        assert (await hub.call("ask_get", {"ask_id": created["ask_id"]}))["answer"]["choice"] == "b"


# -- the PWA behind the gateway's OAuth login ----------------------------------------------------

def test_pwa_login_cookie_manifest_and_service_worker(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from agent_nexus.cloud import build_cloud_app
    origin = "https://hub.example.test"
    monkeypatch.setenv("SERVER_TOKEN", SERVER_TOKEN)
    monkeypatch.setenv("AGENT_TOKEN", "a" * 40)
    secrets_dir = tmp_path / "secrets" / "workspace"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "oauth-password").write_text("p" * 40)
    config = {"agents": {AGENT_ID: "AGENT_TOKEN"}, "public_url": origin,
              "auth": {"mode": "oauth", "password_secret": "workspace/oauth-password", "scopes": ["tools"],
                       "required_scopes": ["tools"], "store": str(tmp_path / "oauth.sqlite3")}}
    with TestClient(build_cloud_app(config, tmp_path), base_url=origin) as client:
        first = client.get("/inbox/", follow_redirects=False)
        assert first.status_code == 302 and first.headers["location"] == "/inbox/login"
        login = client.get("/inbox/login", follow_redirects=False)
        authorize = urlsplit(login.headers["location"])
        query = parse_qs(authorize.query)
        assert authorize.path == "/authorize" and query["redirect_uri"] == [f"{origin}/inbox/callback"]
        assert query["code_challenge_method"] == ["S256"]
        to_login = client.get(login.headers["location"], follow_redirects=False)
        assert to_login.status_code == 302 and "/login?" in to_login.headers["location"]
        page = client.get(to_login.headers["location"])
        flow = re.search(r'name="flow" value="([^"]+)"', page.text).group(1)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        approved = client.post("/login", data={"flow": flow, "csrf": csrf, "password": "p" * 40, "decision": "approve"},
                               headers={"Origin": origin}, follow_redirects=False)
        assert approved.status_code == 303 and approved.headers["location"].startswith(f"{origin}/inbox/callback?")
        callback = client.get(approved.headers["location"], follow_redirects=False)
        assert callback.status_code == 303 and callback.headers["location"] == "/inbox/"
        cookie = callback.headers["set-cookie"]
        assert cookie.startswith("__Host-anx_inbox=")
        lowered = cookie.lower()
        assert "httponly" in lowered and "secure" in lowered and "samesite=strict" in lowered and "path=/" in lowered
        shell = client.get("/inbox/")
        assert shell.status_code == 200 and 'rel="manifest"' in shell.text and "/inbox/sw.js" in shell.text
        assert client.get("/api/inbox/threads").json()["threads"] == []
        # Cookie-authenticated writes need X-ANX as well.
        assert client.post("/api/inbox/send", json={"to": "me", "body": "x"}).status_code == 401
        assert client.post("/api/inbox/send", json={"to": "nobody", "body": "x"}, headers={"X-ANX": "1"}).status_code == 400
        # A replayed or forged callback is refused.
        assert client.get(approved.headers["location"], follow_redirects=False).status_code == 400

        manifest = client.get("/inbox/manifest.webmanifest")
        data = json.loads(manifest.text)
        assert manifest.headers["content-type"].startswith("application/manifest+json")
        assert data["start_url"] == "/inbox/" and data["scope"] == "/inbox/" and data["display"] == "standalone"
        assert data["icons"] and client.get(data["icons"][0]["src"]).status_code == 200
        worker = client.get("/inbox/sw.js")
        assert worker.headers["service-worker-allowed"] == "/inbox/" and "/api/" in worker.text
    with TestClient(build_cloud_app(config, tmp_path), base_url=origin) as fresh:
        assert fresh.get("/api/inbox/threads").status_code == 401  # no cookie, no bearer


def test_mailbox_config_is_type_checked_and_unknown_keys_warn(tmp_path, monkeypatch):
    from mcpkit.config import ConfigError
    from agent_nexus.cloud import build_cloud_app, config_warnings
    monkeypatch.setenv("SERVER_TOKEN", SERVER_TOKEN)
    monkeypatch.setenv("AGENT_TOKEN", "a" * 40)
    base = {"token_env": "SERVER_TOKEN", "agents": {AGENT_ID: "AGENT_TOKEN"}}
    config = {**base, "mailbox": {"notice": {"structured": "never", "later": 1}, "wake": {"enabled": True}}}
    build_cloud_app(config, tmp_path)
    assert "unknown configuration key 'mailbox.notice.later' (ignored)" in config_warnings(config)
    with pytest.raises(ConfigError, match="mailbox.notice.structured"):
        build_cloud_app({**base, "mailbox": {"notice": {"structured": "sometimes"}}}, tmp_path)
    with pytest.raises(ConfigError, match="mailbox.retention.read_days"):
        build_cloud_app({**base, "mailbox": {"retention": {"read_days": "ninety"}}}, tmp_path)


def test_disabled_mailbox_keeps_the_routing_tools_only():
    from agent_nexus.federation import Hub
    from agent_nexus.gateway import Gateway

    async def names():
        async with Client(Gateway(Hub(), mailbox={"enabled": False}).build()) as client:
            return {tool.name for tool in await client.list_tools()}
    listed = asyncio.run(names())
    assert "mail_send" not in listed and {"use_device", "set_workspace", "toolsearch"} <= listed


# -- device toasts (agent side) -------------------------------------------------------------------

def test_toast_notify_arguments_and_signed_answer_links(tmp_path):
    from agent_nexus import toast
    secret = toast.link_secret(tmp_path)
    assert toast.link_secret(tmp_path) == secret and len(secret) == 32
    arguments = toast.notify_arguments({"title": "claude 提问", "body": "用哪个？", "link": "#/inbox/t_1", "level": "urgent",
                                        "actions": [{"id": "choice-0", "label": "b（推荐）", "ask_id": "q_1", "choice": "b"},
                                                    {"id": "choice-1", "label": "a", "ask_id": "q_1", "choice": "a"}]},
                                       "http://127.0.0.1:8399", secret)
    assert [a["id"] for a in arguments["actions"]] == ["choice-0", "choice-1", "open"]
    assert arguments["actions"][2]["link"] == "http://127.0.0.1:8399/#/inbox/t_1"
    link = urlsplit(arguments["actions"][0]["link"])
    query = {key: value[0] for key, value in parse_qs(link.query).items()}
    assert link.path == "/toast/answer" and query["ask"] == "q_1" and query["choice"] == "b"
    assert toast.verify(secret, "q_1", "b", query["sig"]) and not toast.verify(secret, "q_1", "a", query["sig"])


def test_console_toast_answer_link_and_inbox_proxy(tmp_path):
    from starlette.testclient import TestClient
    from agent_nexus import toast
    from agent_nexus.supervisor import Supervisor
    from agent_nexus.web import build_app
    sup = Supervisor(tmp_path)
    app = build_app(sup)
    calls = []

    async def fake_call(op, args=None, **_):
        calls.append((op, args))
        if op == "ask_answer":
            return {"ask_id": args["ask_id"], "state": "answered", "answer": {"choice": args["choice"]}}
        return {"threads": [], "open_asks": [], "unread": 0}

    with TestClient(app, base_url="http://127.0.0.1:8399") as client:
        hub = app.app.state.hub
        assert client.get("/api/inbox/threads").status_code == 409  # no gateway yet
        hub.attach(lambda message: None, {"features": ["hub_call/1", "ask/1", "inbox/1"]})
        hub.call = fake_call
        assert client.get("/api/inbox/threads").json()["threads"] == []
        assert calls[-1] == ("mail_local", {"op": "threads"})
        assert client.post("/api/inbox/send", json={"to": "x", "body": "y"}).status_code == 403  # X-ANX required
        secret = toast.link_secret(sup.cfg.state_dir)
        good = client.get("/toast/answer", params={"ask": "q_1", "choice": "b", "sig": toast.sign(secret, "q_1", "b")})
        assert "已回答：b" in good.text and calls[-1] == ("ask_answer", {"ask_id": "q_1", "choice": "b", "via": "toast"})
        forged = client.get("/toast/answer", params={"ask": "q_1", "choice": "a", "sig": toast.sign(secret, "q_1", "b")})
        assert "无效" in forged.text and len([c for c in calls if c[0] == "ask_answer"]) == 1
        assert client.get("/api/asks/status").json() == {"available": True}
        assert client.get("/inbox.js").status_code == 200


def test_mail_tool_schemas_match_the_published_file():
    """schemas/mail-tools.v1.json is the contract: only optional properties may be added (design §3.3)."""
    from agent_nexus.federation import Hub
    from agent_nexus.gateway import Gateway
    published = json.loads((Path(__file__).resolve().parents[2] / "schemas" / "mail-tools.v1.json")
                           .read_text(encoding="utf-8"))["tools"]

    async def live():
        async with Client(Gateway(Hub()).build()) as client:
            return {tool.name: tool for tool in await client.list_tools()}
    tools = asyncio.run(live())
    for name, entry in published.items():
        schema = tools[name].input_schema
        assert set(schema.get("required") or []) == set(entry["inputSchema"].get("required") or []), name
        assert set(entry["inputSchema"]["properties"]) <= set(schema["properties"]), name
        for prop, spec in entry["inputSchema"]["properties"].items():
            assert schema["properties"][prop] == spec, (name, prop)
        assert tools[name].title == entry["title"]
