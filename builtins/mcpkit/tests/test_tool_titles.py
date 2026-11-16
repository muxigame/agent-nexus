"""Proxied tools arrive without titles; ToolTitles supplies the display labels."""
import anyio
from fastmcp import Client, FastMCP

from mcpkit.tools import ToolTitles


def test_known_tools_get_labels_and_others_keep_theirs():
    server = FastMCP("t")

    @server.tool
    def take_screenshot() -> str:
        return "ok"

    @server.tool(title="Already labelled")
    def other() -> str:
        return "ok"

    server.add_middleware(ToolTitles({"take_screenshot": "页面截图"}))

    async def check():
        async with Client(server) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert tools["take_screenshot"].title == "页面截图"
            assert tools["other"].title == "Already labelled"
            assert (await client.call_tool("take_screenshot")).data == "ok"

    anyio.run(check)
