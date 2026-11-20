"""Two real local managers joined to one gateway: A opens B's terminal over the P2P tunnel.

    ANX_MUXI_P2P=<path to muxi-p2p-agent> uv run python scripts/p2p_smoke.py

The gateway serves muxi-p2p signaling under /p2p and the reflector; B's manager starts its
tunnel agent (``remote-hub/1``); A's local console WebSocket must report a direct route and
run a command in B's shell. Exits non-zero when the route fell back to the gateway relay.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from console_screens import _device, _listener, _serve  # noqa: E402


async def main_async() -> int:
    import httpx
    from websockets.asyncio.client import connect

    from agent_nexus import agent as agent_module
    from agent_nexus import join as joining
    from agent_nexus.agent import run_agent
    from agent_nexus.cloud import build_cloud_app
    from agent_nexus.supervisor import Supervisor
    from agent_nexus.web import build_app

    if not os.environ.get("ANX_MUXI_P2P"):
        print("set ANX_MUXI_P2P to the muxi-p2p-agent binary")
        return 2
    work = Path(tempfile.mkdtemp(prefix="anx-p2p-"))
    gw_sock, gw_port = _listener()
    origin = f"http://127.0.0.1:{gw_port}"
    os.environ["ANX_P2P_SERVER_TOKEN"] = "p2p-smoke-server-token-0123456789abcdef"
    config = {"token_env": "ANX_P2P_SERVER_TOKEN", "agents": {}, "public_url": origin,
              "state_dir": str(work / "gateway/state"), "p2p": {"reflector_hosts": ["127.0.0.1"]}}
    app = build_cloud_app(config, work / "gateway")
    gw_server, gw_task = await _serve(app, gw_sock)
    enrollment = app.state.enrollment

    names = {}

    async def configured(root, status=None, hub=None):
        data = json.loads((root / "agent-nexus.json").read_text(encoding="utf-8"))
        cloud = data.get("cloud") or {}
        if not cloud.get("enabled"):
            status.update(state="disabled")
            return
        await run_agent(root, {**cloud, "hostname": Path(root).name, "workspaces": {"home": {"path": str(root), "mcp": "device"}}},
                        transports={"device": _device(Path(root).name)}, status=status, hub=hub)
    agent_module.run_configured_agent = configured

    managers = {}
    for name in ("host-alpha", "host-beta"):
        sock, port = _listener()
        root = work / name
        root.mkdir()
        (root / "agent-nexus.json").write_text(json.dumps({"manager": {"host": "127.0.0.1", "port": port}, "builtins": False,
                                                           "scan": [], "cloud": {"enabled": False}}), encoding="utf-8")
        sup = Supervisor(root)
        server, task = await _serve(build_app(sup), sock)
        managers[name] = {"url": f"http://127.0.0.1:{port}", "port": port, "server": server, "task": task}
        joining.local_hostname = lambda name=name: name
        async with httpx.AsyncClient(base_url=managers[name]["url"], headers={"X-ANX": "1"}, trust_env=False, timeout=30) as http:
            joined = await http.post("/api/cloud/join", json={"invite": enrollment.create_invite(label=name)["code"]})
            assert joined.status_code == 200, joined.text
            for _ in range(100):
                if (await http.get("/api/cloud")).json().get("state") == "connected":
                    break
                await asyncio.sleep(0.2)
    beta_id = next(d["device_id"] for d in app.state.gateway.devices.console_list() if d.get("hostname") == "host-beta")

    # B's tunnel agent registers with the signaling.
    async with httpx.AsyncClient(base_url=origin, trust_env=False, timeout=10) as http:
        for _ in range(60):
            endpoints = await http.get(f"/p2p/api/v1/tunnel/endpoints?server_id={beta_id}",
                                       headers={"Authorization": "Bearer " + os.environ["ANX_P2P_SERVER_TOKEN"]})
            if endpoints.status_code == 200 and endpoints.json().get("online"):
                break
            await asyncio.sleep(0.5)
        print("B registered with the signaling:", endpoints.status_code, endpoints.json().get("online"),
              [c.get("kind") for c in endpoints.json().get("candidates", [])])

    windows = sys.platform == "win32"
    command, answer = (b"echo anx-term-ok%PATHEXT:~0,0%\r", b"anx-term-ok\r") if windows else (b"echo anx-term-$((6*7))\r", b"anx-term-42")
    shell = "cmd" if windows else ""
    alpha = managers["host-alpha"]
    url = f"ws://127.0.0.1:{alpha['port']}/api/remote/devices/{beta_id}/terminal/ws?shell={shell}&cols=100&rows=30"
    route = None
    started = time.monotonic()
    out = bytearray()
    try:
        async with connect(url, proxy=None, open_timeout=60) as ws:
            while True:
                message = await asyncio.wait_for(ws.recv(), 60)
                if isinstance(message, bytes):
                    out.extend(message)
                    if b"\x1b[6n" in message:
                        await ws.send(b"\x1b[1;1R")
                    if answer in out:
                        break
                    continue
                payload = json.loads(message)
                print("message:", payload)
                if payload["type"] == "route":
                    route = payload
                elif payload["type"] == "ready":
                    print(f"terminal ready after {time.monotonic() - started:.1f}s")
                    await asyncio.sleep(1.5)
                    sent = time.monotonic()
                    await ws.send(command)
                elif payload["type"] in ("error", "exit"):
                    raise SystemExit(f"terminal ended: {payload}")
            print(f"command answered in {(time.monotonic() - sent) * 1000:.0f} ms")
            # keystroke echo round trips over the tunnel
            samples = []
            for ch in b"abcdefgh":
                before = len(out)
                t0 = time.monotonic()
                await ws.send(bytes([ch]))
                while len(out) == before:
                    message = await asyncio.wait_for(ws.recv(), 10)
                    if isinstance(message, bytes):
                        out.extend(message)
                samples.append((time.monotonic() - t0) * 1000)
            print("keystroke echo ms:", " ".join(f"{s:.0f}" for s in samples))
        if "--browser" in sys.argv:
            await browser_check(alpha["port"], beta_id, Path(sys.argv[sys.argv.index("--browser") + 1])
                                if len(sys.argv) > sys.argv.index("--browser") + 1 else None)
    finally:
        for manager in managers.values():
            manager["server"].should_exit = True
        gw_server.should_exit = True
        await asyncio.gather(*(m["task"] for m in managers.values()), gw_task, return_exceptions=True)
    if not route or not route.get("p2p"):
        print("FAIL: the terminal used the relay:", route)
        return 1
    print("ok: P2P terminal,", route["route"])
    return 0


async def browser_check(port: int, beta_id: str, out: Path | None) -> None:
    """A's local console in Edge: the device list, B's desktop over the tunnel, B's terminal."""
    from playwright.async_api import async_playwright

    errors: list[str] = []
    async with async_playwright() as playwright:
        # "new" headless is the full browser (WebCodecs H.264 included) without a window
        browser = await playwright.chromium.launch(channel="msedge" if sys.platform == "win32" else None, headless=False,
                                                   args=["--headless=new"])
        page = await browser.new_page(viewport={"width": 1280, "height": 800}, color_scheme="dark")
        page.on("pageerror", lambda exc: (print("pageerror:", str(exc)[:200]), errors.append(str(exc))))
        page.on("console", lambda msg: (print("console:", msg.type, msg.text[:200]), errors.append(msg.text) if msg.type == "error" else None))
        await page.goto(f"http://127.0.0.1:{port}/#/devices")
        await page.wait_for_selector(f'[data-device="{beta_id}"] a[href="#/desktop/{beta_id}"]', timeout=20000)
        if out:
            out.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(out / "p2p-devices.png"))
        print("H.264 decode here:", await page.evaluate("import('/local/desktop.js').then(m => m.canDecodeH264())"))
        await page.click(f'[data-device="{beta_id}"] a[href="#/desktop/{beta_id}"]')
        await page.wait_for_selector('.term-status .pill[data-state="online"]', timeout=30000)
        await page.wait_for_timeout(2500)
        size = await page.evaluate("() => { const c = document.querySelector('.desktop-screen canvas'); return c ? [c.width, c.height] : null; }")
        route = await page.inner_text(".term-route")
        codec = await page.inner_text(".term-toolbar .tag:not(.term-route)")
        lit = await page.evaluate("""() => { const c = document.querySelector('.desktop-screen canvas');
            const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; let n = 0;
            for (let i = 0; i < d.length; i += 4 * 997) if (d[i] + d[i + 1] + d[i + 2] > 30) n++; return n; }""")
        print("codec:", codec, "| lit samples:", lit)
        assert lit > 50, "the desktop picture is blank"
        print("desktop connected:", size, "|", route)
        assert size and size[0] > 100, size
        if out:
            await page.screenshot(path=str(out / "p2p-desktop.png"))
        await page.evaluate(f"location.hash = '#/terminal/{beta_id}'")
        await page.wait_for_selector('.term-status .pill[data-state="online"]', timeout=30000)
        await page.wait_for_selector(".term-route.is-p2p", timeout=10000)
        print("terminal route:", await page.inner_text(".term-route"))
        if out:
            await page.wait_for_timeout(1500)
            await page.screenshot(path=str(out / "p2p-terminal.png"))
        await browser.close()
    errors = [e for e in errors if "favicon" not in e]
    assert not errors, errors


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
