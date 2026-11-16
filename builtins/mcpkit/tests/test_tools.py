import json
import threading

import anyio
import pytest
from fastmcp import Client, FastMCP
from mcp_types import ToolAnnotations

from mcpkit.tools import SchemaTool, ToolFailure, json_result

SCHEMA = {
    "type": "object",
    "properties": {"issueID": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}},
    "required": ["issueID"],
    "additionalProperties": False,
}


def server(handler, **kwargs) -> FastMCP:
    mcp = FastMCP("t")
    mcp.add_tool(SchemaTool(name="get_issue", description="Get an issue.", parameters=SCHEMA,
                            annotations=ToolAnnotations(read_only_hint=True), handler=handler, **kwargs))
    return mcp


def call(mcp: FastMCP, arguments: dict):
    async def go():
        async with Client(mcp) as client:
            return await client.call_tool("get_issue", arguments, raise_on_error=False)
    return anyio.run(go)


def test_published_schema_is_served_as_given_and_enforced():
    result = call(server(lambda args: {"seen": args}), {"issueID": "X1", "limit": 2})
    assert json.loads(result.content[0].text) == {"seen": {"issueID": "X1", "limit": 2}}

    async def listed():
        async with Client(server(lambda args: args)) as client:
            return (await client.list_tools())[0]
    tool = anyio.run(listed)
    assert tool.input_schema == SCHEMA and tool.annotations.read_only_hint is True

    invalid = call(server(lambda args: args), {"limit": 0})
    payload = json.loads(invalid.content[0].text)
    assert invalid.is_error and payload["error"] == "InvalidArguments"
    assert "issueID" in payload["message"] and "limit" in payload["message"]


def test_explicit_nulls_can_be_treated_as_absent():
    strict = call(server(lambda args: args), {"issueID": "X1", "limit": None})
    assert strict.is_error  # null is not an integer

    lenient = call(server(lambda args: args, drop_nulls=True), {"issueID": "X1", "limit": None})
    assert json.loads(lenient.content[0].text) == {"issueID": "X1"}


def test_handler_failure_becomes_a_readable_error_result():
    def handler(args):
        raise ToolFailure({"error": "PermissionDenied", "message": "无权限"})

    result = call(server(handler), {"issueID": "X1"})
    assert result.is_error and json.loads(result.content[0].text) == {"error": "PermissionDenied", "message": "无权限"}


def test_blocking_handlers_run_off_the_event_loop():
    def handler(args):
        return {"thread": threading.current_thread().name}

    main = threading.current_thread().name
    assert json.loads(call(server(handler), {"issueID": "X1"}).content[0].text)["thread"] != main


def test_async_handlers_and_prebuilt_results():
    async def handler(args):
        return json_result({"async": True, "id": args["issueID"]})

    assert json.loads(call(server(handler), {"issueID": "X9"}).content[0].text) == {"async": True, "id": "X9"}
