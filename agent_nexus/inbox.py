"""The owner's inbox (decision D11, design §8.12): one set of operations for every surface.

The local console and desktop reach it through ``hub_call mail_local``; the
gateway's own web inbox and PWA through ``/api/inbox/*``. Both call
:meth:`InboxService.call`, which acts as ``me`` (the owner). Viewing another
address's mailbox is read-only: nothing is marked read on its behalf.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .federation import RoutingError
from .mailbox import ANSWER_VIA, ME

if TYPE_CHECKING:
    from .gateway import Gateway

OPS = ("summary", "threads", "thread", "send", "answer", "asks", "ask", "addresses", "set_address", "conversation")


class InboxService:
    def __init__(self, gateway: "Gateway"):
        self.gateway = gateway
        self.mailbox = gateway.mailbox

    def _viewer(self, args: dict) -> str:
        viewer = args.get("viewer") or ME
        if not isinstance(viewer, str) or (viewer != ME and self.mailbox.address_row(viewer) is None):
            raise RoutingError(f"unknown address {viewer!r}")
        return viewer

    async def call(self, op: Any, args: Any, *, via: str = "inbox") -> dict:
        if op not in OPS:
            raise RoutingError(f"unsupported inbox op {op!r}")
        args = args if isinstance(args, dict) else {}
        mailbox = self.mailbox
        if op == "summary":
            return {"unread": mailbox.unread_count(ME), "open_asks": len(mailbox.asks(state="open"))}
        if op == "threads":
            viewer = self._viewer(args)
            return {"viewer": viewer, "viewer_display": mailbox.display(viewer),
                    "threads": mailbox.threads(viewer, limit=int(args.get("limit") or 50)),
                    "open_asks": mailbox.asks(state="open"), "unread": mailbox.unread_count(viewer)}
        if op == "conversation":
            # me <-> one address, mail and asks in time order: the agent chat's timeline (docs/agent-chat.md §10)
            try:
                limit = int(args.get("limit") or 200)
            except (TypeError, ValueError):
                limit = 200
            return mailbox.conversation(args.get("address"), limit=limit,
                                        mark_read=args.get("mark_read") in (True, "1", "true"))
        if op == "thread":
            viewer = self._viewer(args)
            thread_id = args.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id:
                raise RoutingError("thread_id is required")
            return mailbox.thread(viewer, thread_id, mark_read=viewer == ME and args.get("mark_read", True) is not False)
        if op == "send":
            return self.send(args)
        if op == "answer":
            if via not in ANSWER_VIA:
                via = "inbox"
            ask_id = args.get("ask_id")
            if not isinstance(ask_id, str):
                raise RoutingError("ask_id is required")
            return await self.gateway.answer_ask(ask_id, choice=args.get("choice"), text=args.get("text"),
                                                 choices=args.get("choices"), via=args.get("via") or via)
        if op == "asks":
            state = args.get("state", "open")
            return {"asks": mailbox.asks(state=state if state in ("open", "answered", "expired", "cancelled") else None)}
        if op == "ask":
            return mailbox.ask_get(str(args.get("ask_id") or ""))
        if op == "addresses":
            return {"addresses": mailbox.addresses(include_ended=bool(args.get("include_ended")))}
        address = args.get("address")
        fields = args.get("fields")
        if not isinstance(address, str) or not isinstance(fields, dict):
            raise RoutingError("set_address needs address and fields")
        return {"address": mailbox.set_address(address, fields)}

    def send(self, args: dict) -> dict:
        """Mail as ``me``: a reply (reply_to, recipients default to the other side) or a new message."""
        body = args.get("body")
        if not isinstance(body, str) or not body.strip():
            raise RoutingError("body is required")
        to = args.get("to")
        reply_to = args.get("reply_to") or None
        if not to and reply_to:
            with self.mailbox.store._lock:
                parent = self.mailbox._db().execute("SELECT from_addr, to_expr FROM messages WHERE id = ?",
                                                    (reply_to,)).fetchone()
                if parent is None:
                    raise RoutingError(f"unknown reply_to message {reply_to!r}")
                if parent["from_addr"] == ME:
                    to = [row["to_addr"] for row in self.mailbox._db().execute(
                        "SELECT to_addr FROM deliveries WHERE message_id = ?", (reply_to,))]
                else:
                    to = parent["from_addr"]
        if not to:
            raise RoutingError("to is required for a new message")
        result = self.mailbox.send(ME, to, body, subject=str(args.get("subject") or ""),
                                   thread_id=args.get("thread_id") or None, reply_to=reply_to,
                                   kind=args.get("kind") if args.get("kind") in ("message", "task") else "message",
                                   priority="urgent" if args.get("priority") == "urgent" else "normal")
        self.gateway.mail_sent(result)
        return result

