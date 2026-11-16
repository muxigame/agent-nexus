import os
import sys
import textwrap
import time

import anyio
from fastmcp import Client

UPSTREAM = textwrap.dedent('''
    import os, sys
    from fastmcp import FastMCP

    with open(os.environ["STARTS_FILE"], "a", encoding="utf-8") as fh:
        fh.write(f"{os.getpid()}\\n")

    mcp = FastMCP("fake-upstream")

    @mcp.tool
    def echo(text: str) -> str:
        return "upstream:" + text

    @mcp.tool
    def die() -> str:
        os._exit(9)

    mcp.run("stdio", show_banner=False)
''')

APP = textwrap.dedent('''
    import os, sys
    from mcpkit import main
    from mcpkit.proxy import stdio_proxy

    def build(service):
        here = os.path.dirname(os.path.abspath(__file__))
        return stdio_proxy(service, command=sys.executable, args=[os.path.join(here, "upstream.py")],
                           env={"STARTS_FILE": os.path.join(here, "starts.txt")}, watchdog_interval=1.0)

    if __name__ == "__main__":
        sys.exit(main(__file__, build, version="0.0.1"))
''')


def test_all_clients_share_one_upstream_process_and_death_exits(workspace, run):
    directory = workspace.service("proxied", manifest={"port": 1}, app=APP)
    (directory / "upstream.py").write_text(UPSTREAM, encoding="utf-8")
    svc = run(directory)
    url = svc.url + "/mcp"

    async def session(text):
        async with Client(url) as client:
            names = sorted(t.name for t in await client.list_tools())
            first = (await client.call_tool("echo", {"text": text})).content[0].text
            second = (await client.call_tool("echo", {"text": text + "2"})).content[0].text
            return names, first, second

    for text in ("a", "b", "c"):
        names, first, second = anyio.run(session, text)
        assert names == ["die", "echo"] and first == f"upstream:{text}" and second == f"upstream:{text}2"

    starts = (directory / "starts.txt").read_text(encoding="utf-8").split()
    assert len(starts) == 1, f"expected one shared upstream process, saw {len(starts)}"

    async def kill():
        async with Client(url) as client:
            try:
                await client.call_tool("die", {})
            except Exception:
                pass
    anyio.run(kill)

    deadline = time.monotonic() + 20
    while svc.proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    assert svc.proc.returncode == 3, f"service should exit with 3 when the upstream dies, got {svc.proc.returncode}"
    assert "upstream MCP process lost" in svc.output()
