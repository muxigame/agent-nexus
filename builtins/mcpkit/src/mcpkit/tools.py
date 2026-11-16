"""Tools whose input schema is given, not derived from a Python signature.

FastMCP's ``@mcp.tool`` builds a schema from the function's parameters, which is
what you want for a tool you are writing. It is not what you want when the
schema *is* the contract: ones-mcp publishes the official ONES MCP schemas
verbatim so the same call works against either server. :class:`SchemaTool` takes the schema as given, validates
against it, and hands the raw arguments to a handler.

A handler returns any JSON-serialisable value, or raises :class:`ToolFailure`
to return an error *result* (a payload the model can read and act on) rather
than an exception.
"""
from __future__ import annotations

import inspect
import json
from typing import Annotated, Any, Callable

import anyio.to_thread
import mcp_types as types
from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import Tool, ToolResult
from jsonschema import Draft202012Validator
from pydantic import ConfigDict, Field, PrivateAttr


class ToolFailure(Exception):
    """Return an error result carrying ``payload`` instead of raising to the client."""

    def __init__(self, payload: Any):
        super().__init__(payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False))
        self.payload = payload


def json_result(payload: Any, *, is_error: bool = False) -> ToolResult:
    """One JSON text block - how these servers have always answered."""
    return ToolResult(content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
                      is_error=is_error)


class SchemaTool(Tool):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    handler: Annotated[Callable[[dict[str, Any]], Any], Field(exclude=True)]
    #: Treat an explicit ``null`` as "not given". Clients send nulls for optional
    #: parameters that a published schema types as a plain string.
    drop_nulls: bool = False
    _validator: Draft202012Validator = PrivateAttr()

    def model_post_init(self, _context: Any) -> None:
        self._validator = Draft202012Validator(self.parameters)

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        args = dict(arguments or {})
        if self.drop_nulls:
            args = {k: v for k, v in args.items() if v is not None}
        problems = sorted(self._validator.iter_errors(args), key=lambda e: [str(p) for p in e.path])
        if problems:
            return json_result({"error": "InvalidArguments", "message": "; ".join(_describe(e) for e in problems[:5])},
                               is_error=True)
        try:
            if inspect.iscoroutinefunction(self.handler):
                result = await self.handler(args)
            else:
                # These handlers do blocking HTTP; keep them off the event loop.
                result = await anyio.to_thread.run_sync(self.handler, args)
        except ToolFailure as failure:
            return json_result(failure.payload, is_error=True)
        return result if isinstance(result, ToolResult) else json_result(result)


def _describe(error: Any) -> str:
    where = ".".join(str(p) for p in error.path)
    return f"{where}: {error.message}" if where else error.message


class ToolTitles(Middleware):
    """Display labels for tools whose upstream server publishes none.

    A proxied stdio server's tools arrive without titles, so clients show the
    name title-cased. Unknown names keep whatever they had.
    """

    def __init__(self, titles: dict[str, str]):
        super().__init__()
        self.titles = dict(titles)

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        return [tool.model_copy(update={"title": self.titles[tool.name]}) if tool.name in self.titles else tool
                for tool in tools]
