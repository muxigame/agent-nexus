"""``MailNoticeMiddleware``: the unread notice at the end of every tool result (design §8.6).

Every ``tools/call`` result, errors included, ends with a short text block
naming the caller's unread mail. Two failure shapes reach this middleware:

* a gateway-native tool raising (``RoutingError`` becomes FastMCP's
  ``ToolError``; unknown tools raise ``NotFoundError``) -- FastMCP would turn
  these into an ``isError`` result on the wire with ``str(exc)`` as the text,
  so this middleware builds exactly that result itself, with the notice added;
* a directly exposed device tool returning ``ToolFailure`` -- already an
  ``isError`` *result* (mcpkit/tools.py), kept as it is.

The same notice goes into ``_meta["agent-nexus/mail"]`` and, when the result
has a structured payload whose schema allows extra keys, into
``structuredContent["_mail"]`` -- some clients show the model mainly that.

Identity: for ``use_device``/``set_workspace`` the handle they just returned
(their ``workspace_id`` argument is the caller's previous handle); else the
call's ``workspace_id``; then the stateful session's selection; then
the one address this OAuth client used recently (``notice.fallback``). A call
with no identity bumps the ``notice_missed`` counter.

A session with an automatic name (design §8.2) also gets one short line,
once per handle and before the notice, saying it may pass ``session_name``.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from fastmcp.exceptions import DisabledError, FastMCPError, NotFoundError
from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import ToolResult
from mcp_types import TextContent

from .federation import RoutingError

if TYPE_CHECKING:
    from .gateway import Gateway

log = logging.getLogger(__name__)
META_KEY = "agent-nexus/mail"
STRUCTURED_KEY = "_mail"
BINDING_TOOLS = ("use_device", "set_workspace")


class MailNoticeMiddleware(Middleware):
    def __init__(self, gateway: "Gateway"):
        self.gateway = gateway
        self.mailbox = gateway.mailbox

    async def _caller(self, context, arguments: dict, result: ToolResult | None) -> tuple[str | None, str | None]:
        handle, address = None, None
        if result is not None and context.message.name in BINDING_TOOLS:
            # The handle just returned; a workspace_id argument there is the caller's *previous* handle.
            structured = result.structured_content if isinstance(result.structured_content, dict) else {}
            handle = structured.get("workspace_id")
            address = self.mailbox.address_for_handle(handle)
        if address is None:
            handle = arguments.get("workspace_id") if isinstance(arguments.get("workspace_id"), str) else None
            address = self.mailbox.address_for_handle(handle) if handle else None
        if address is None and context.fastmcp_context is not None:
            with contextlib.suppress(Exception):
                selection = await context.fastmcp_context.get_state("mcpm_workspace")
                if isinstance(selection, dict) and selection.get("scope"):
                    handle = selection["scope"]
                    address = self.mailbox.address_for_handle(handle)
        if address is None and handle and self.mailbox.displaced(handle) is not None:
            return None, None  # a displaced handle (§8.2) gets no notice, not even the client fallback's
        if address is None:
            from .gateway import _client_id
            address = self.mailbox.fallback_address(_client_id())
            handle = None
        return address, handle

    async def _structured_allowed(self, context, result: ToolResult) -> bool:
        mode = self.mailbox.config["notice"]["structured"]
        if mode == "never" or not isinstance(result.structured_content, dict):
            return False
        if mode == "always":
            return True
        schema = None
        with contextlib.suppress(Exception):
            server = context.fastmcp_context.fastmcp if context.fastmcp_context else None
            tool = await server.get_tool(context.message.name) if server is not None else None
            schema = getattr(tool, "output_schema", None)
        if not isinstance(schema, dict):
            return True
        # A strict schema would reject the extra key; a wrapped result would change shape.
        return schema.get("additionalProperties") is not False and not schema.get("x-fastmcp-wrap-result")

    async def on_call_tool(self, context, call_next):
        arguments = context.message.arguments or {}
        raised: Exception | None = None
        try:
            result = await call_next(context)
        except asyncio.CancelledError:
            raise
        except (DisabledError, NotFoundError):
            # What FastMCP answers for an unknown tool, plus the notice.
            raised = NotFoundError(context.message.name)
            result = ToolResult(content=[TextContent(type="text", text=f"Unknown tool: {context.message.name!r}")])
        except (FastMCPError, RoutingError) as exc:
            raised = exc
            result = ToolResult(content=[TextContent(type="text", text=str(exc))])
        if not isinstance(result, ToolResult):
            return result  # e.g. a task submission: nothing to append to
        try:
            await self._annotate(context, arguments, result, failed=raised is not None)
        except Exception as exc:  # the notice must never break a tool call
            log.warning("mail notice failed (%s)", type(exc).__name__)
        if raised is not None:
            return ToolResult(content=result.content, meta=result.meta, is_error=True)
        return result

    async def _annotate(self, context, arguments: dict, result: ToolResult, *, failed: bool) -> None:
        address, handle = await self._caller(context, arguments, None if failed else result)
        if address is None:
            self.mailbox.bump("notice_missed")
            return
        self.mailbox.touch(address)
        if self.mailbox.router is not None:
            self.mailbox.router.ack(address)  # the next call after a wake acknowledges it (§8.7)
        # An automatically named session is told once (per handle) that it may choose a name (§8.2 layer 3).
        nudge = None if failed else self.mailbox.nudge_for(address, handle)
        notice = self.mailbox.notice_for(address, handle)
        if notice is None and nudge is None:
            return
        # A proxied device result carries its exact wire form, which would win
        # over the edits below; drop it so they reach the client.
        with contextlib.suppress(AttributeError):
            result._raw_mcp_result = None
        if nudge is not None:
            result.content = [*(result.content or []), TextContent(type="text", text=nudge)]
        if notice is None:
            return
        # The unread notice stays the last text block.
        result.content = [*(result.content or []), TextContent(type="text", text=notice.text)]
        if self.mailbox.config["notice"]["meta"]:
            result.meta = {**(result.meta or {}), META_KEY: notice.meta}
        if not failed and await self._structured_allowed(context, result):
            result.structured_content = {**result.structured_content, STRUCTURED_KEY: notice.meta}
