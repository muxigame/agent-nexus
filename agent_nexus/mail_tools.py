"""The gateway's native mailbox tools (design §8.5; schemas in ``schemas/mail-tools.v1.json``).

``mail_inbox``/``mail_read``/``mail_wait``/``mail_who``/``mail_status`` change
nothing and carry the routing tools' read-only annotations; ``mail_send``,
``mail_bye`` and ``ask_user`` write but destroy nothing; ``mail_recall`` is
destructive (a recalled message cannot be restored).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from fastmcp import Context
from mcp_types import ToolAnnotations

from .federation import RoutingError
from .mailbox import ANON_SENDER

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from .gateway import Gateway

MAIL_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
MAIL_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
MAIL_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)
MAIL_TOOL_NAMES = ("mail_send", "mail_inbox", "mail_read", "mail_wait", "mail_who", "mail_bye",
                   "mail_status", "mail_recall", "ask_user")


def register(server: "FastMCP", gateway: "Gateway") -> None:
    mailbox = gateway.mailbox

    async def me(workspace_id: str | None, *, required: bool = True) -> str | None:
        address = await gateway.caller_address(workspace_id)
        if address is None and required:
            raise RoutingError("pass the workspace_id returned by use_device (or set_workspace); "
                               "it identifies your mailbox address")
        return address

    @server.tool(title="Send mail", annotations=MAIL_WRITE)
    async def mail_send(to: str | list[str], body: str, workspace_id: str | None = None, subject: str = "",
                        thread_id: str | None = None, reply_to: str | None = None,
                        kind: Literal["message", "task", "result"] = "message",
                        priority: Literal["normal", "urgent"] = "normal",
                        wake: Literal["auto", "always", "never"] = "auto",
                        attachments: list[dict[str, Any]] | None = None) -> dict:
        """Send mail to other agent sessions or to the owner ("me").
        to: an address name@device (device id or display name), a bare name, "me", or a group:
        @all, @device:<device>, @kind:<claude|codex|gpt|other>, @team:<name>; or a list of these.
        Pass workspace_id so the mail comes from your address; reply_to answers a message in its thread.
        wake: auto wakes idle recipients on their device (not active ones), always also wakes active ones,
        never only leaves the unread notice for their next call."""
        sender = await me(workspace_id, required=False) or ANON_SENDER
        if kind not in ("message", "task", "result"):
            raise RoutingError("kind must be message, task or result")
        if wake not in ("auto", "always", "never"):
            raise RoutingError("wake must be auto, always or never")
        result = mailbox.send(sender, to, body, subject=subject, thread_id=thread_id, reply_to=reply_to,
                              kind=kind, priority=priority, attachments=attachments, wake=wake)
        gateway.mail_sent(result)
        return result

    @server.tool(title="Check your inbox", annotations=MAIL_READ_ONLY)
    async def mail_inbox(workspace_id: str, unread_only: bool = True, thread_id: str | None = None,
                         limit: int = 20, before: str | None = None) -> dict:
        """List your mail (newest first): id, thread, sender, subject, snippet, priority, kind and state.
        Use mail_read to read a message in full."""
        return mailbox.inbox(await me(workspace_id), unread_only=unread_only, thread_id=thread_id,
                             limit=limit, before=before)

    @server.tool(title="Read mail", annotations=MAIL_READ_ONLY)
    async def mail_read(workspace_id: str, message_id: str | None = None, thread_id: str | None = None,
                        mark_read: bool = True) -> dict:
        """Read a message in full (or a whole thread), with attachments; marks it read.
        Without message_id and thread_id, reads your oldest unread message."""
        return mailbox.read(await me(workspace_id), message_id=message_id, thread_id=thread_id, mark_read=mark_read)

    @server.tool(title="Wait for new mail", annotations=MAIL_READ_ONLY)
    async def mail_wait(workspace_id: str, timeout_s: int = 50, thread_id: str | None = None) -> dict:
        """Wait for unread mail (optionally in one thread), up to timeout_s seconds; returns like mail_inbox."""
        address = await me(workspace_id)
        return await gateway.wait_for_mail(address, timeout_s, thread_id,
                                           handle=await gateway.caller_handle(workspace_id))

    @server.tool(title="List mailbox addresses", annotations=MAIL_READ_ONLY)
    async def mail_who(workspace_id: str | None = None, agent_id: str | None = None, kind: str | None = None,
                       include_ended: bool = False) -> dict:
        """List devices and the mailbox addresses on them: kind, presence (active/idle/offline/ended),
        unread counts. Address names are shown as name@<device display name>."""
        address = await me(workspace_id, required=False)
        resolved = gateway.devices.resolve(agent_id) if agent_id else None
        devices = [{"agent_id": item["device_id"], "display_name": item["display_name"], "online": item["online"],
                    "form": item["form"], "capabilities": item["capabilities"],
                    "agent_runtimes": item["agent_runtimes"], "features": item["features"]}
                   for item in gateway.devices.list(include_offline=True)
                   if resolved is None or item["device_id"] == resolved]
        return {"me": address, "me_display": mailbox.display(address) if address else None, "devices": devices,
                "addresses": mailbox.addresses(agent_id=resolved, kind=kind, include_ended=include_ended),
                "counters": mailbox.counters()}

    @server.tool(title="End this session", annotations=MAIL_WRITE)
    async def mail_bye(workspace_id: str) -> dict:
        """End this session's mailbox presence: it will not be woken again; mail is kept."""
        return mailbox.bye(await me(workspace_id))

    @server.tool(title="Check delivery status", annotations=MAIL_READ_ONLY)
    async def mail_status(message_id: str, workspace_id: str | None = None) -> dict:
        """Per-recipient status of a message you sent: delivered, woken, read, expired or recalled."""
        await me(workspace_id, required=False)
        return mailbox.status(message_id)

    @server.tool(title="Recall mail", annotations=MAIL_DESTRUCTIVE)
    async def mail_recall(workspace_id: str, message_id: str, to: str | list[str] | None = None) -> dict:
        """Recall a message you sent from recipients who have not read it yet (all, or only those in to).
        Already-read deliveries are reported in not_recalled."""
        return mailbox.recall(await me(workspace_id), message_id, to)

    @server.tool(title="Ask the owner", annotations=MAIL_WRITE)
    async def ask_user(question: str, workspace_id: str | None = None, options: list[str] | None = None,
                       recommended: str | None = None, context: str = "", blocking: bool = False,
                       timeout_s: int = 50) -> dict:
        """Ask the owner a question. It appears as a question card in the agent-nexus inbox (recommended
        option first, then the others, then a free "other" answer) and as a toast on online devices.
        blocking=false returns at once; the answer arrives later as mail (kind=answer).
        blocking=true waits up to timeout_s for the answer and returns it directly."""
        sender = await me(workspace_id, required=False) or ANON_SENDER
        if sender == ANON_SENDER:
            raise RoutingError("ask_user needs your workspace_id: the answer is mailed back to your address")
        created = mailbox.ask_create(sender, question, options=options, recommended=recommended,
                                     context=context, blocking=blocking)
        gateway.ask_created(created["ask_id"])
        if not blocking:
            return created
        # While it blocks, the asker is in use: nobody takes its address (and the answer mail) over.
        with mailbox.in_use(await gateway.caller_handle(workspace_id)):
            answered = await gateway.wait_for_answer(created["ask_id"], timeout_s)
        if answered is None:
            return {**created, "state": "open",
                    "message": "No answer yet; it will arrive as mail (kind=answer) in your inbox."}
        return {"ask_id": created["ask_id"], "message_id": created["message_id"], "state": "answered",
                "answer": answered}

