"""Client to client: the local console of device A opens a terminal on device B (Edge, Playwright).

A gateway; device B with the real terminal handlers (term/1); and the local manager of
client A, joined with an invite and serving its console. Edge opens A's 设备 page, opens B's
终端 there, runs a command and checks the answer; then locks A and checks the terminal is
refused. Screenshots of the device list, the terminal and the locked state.

    uv run --with playwright python scripts/client_terminal_screens.py [--out DIR]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from console_screens import PASSWORD, _device, _listener, _serve  # noqa: E402


async def main_async(out: Path | None) -> int:
    import httpx
    from playwright.async_api import async_playwright

    from agent_nexus import agent as agent_module
    from agent_nexus import join as joining
    from agent_nexus import terminal
    from agent_nexus.agent import HubLink, run_agent
    from agent_nexus.cloud import build_cloud_app
    from agent_nexus.supervisor import Supervisor
    from agent_nexus.web import build_app

    work = Path(tempfile.mkdtemp(prefix="anx-c2c-"))
    gw_sock, gw_port = _listener()
    origin = f"http://localhost:{gw_port}"
    secrets = work / "gateway/secrets/workspace"
    secrets.mkdir(parents=True)
    (secrets / "oauth-password").write_text(PASSWORD, encoding="utf-8")
    state = work / "gateway/state"
    config = {"agents": {}, "public_url": origin, "state_dir": str(state),
              "auth": {"mode": "oauth", "password_secret": "workspace/oauth-password", "scopes": ["tools"],
                       "required_scopes": ["tools"], "store": str(state / "oauth.sqlite3")}}
    app = build_cloud_app(config, work / "gateway")
    gw_server, gw_task = await _serve(app, gw_sock)
    enrollment = app.state.enrollment
    tasks = []

    # Device B: joined with an invite, answering term_* for real.
    b_root = work / "device-b"
    b_root.mkdir()
    await asyncio.to_thread(joining.join, b_root, enrollment.create_invite(label="B")["code"], hostname="host-beta")
    b_cloud = json.loads((b_root / "agent-nexus.json").read_text(encoding="utf-8"))["cloud"]
    b_hub = HubLink()
    terms = terminal.Terminals()
    b_hub.handlers.update(terms.handlers())
    b_hub.extra_features = ("term/1",)
    ready = asyncio.Event()
    tasks.append(asyncio.create_task(run_agent(b_root, {**b_cloud, "workspaces": {"home": {"path": str(b_root), "mcp": "device"}}},
                                               transports={"device": _device("beta")}, ready=ready, status={}, hub=b_hub)))
    await asyncio.wait_for(ready.wait(), 20)

    # Client A: a local manager joined through its own console API.
    async def configured(root, status=None, hub=None):
        data = json.loads((root / "agent-nexus.json").read_text(encoding="utf-8"))
        cloud = data.get("cloud") or {}
        if not cloud.get("enabled"):
            status.update(state="disabled")
            return
        await run_agent(root, {**cloud, "hostname": "host-alpha", "workspaces": {"home": {"path": str(root), "mcp": "device"}}},
                        transports={"device": _device("alpha")}, status=status, hub=hub)
    agent_module.run_configured_agent = configured
    joining.local_hostname = lambda: "host-alpha"
    a_sock, a_port = _listener()
    a_root = work / "client-a"
    a_root.mkdir()
    (a_root / "agent-nexus.json").write_text(json.dumps({"manager": {"host": "127.0.0.1", "port": a_port}, "builtins": False,
                                                         "scan": [], "cloud": {"enabled": False}}), encoding="utf-8")
    sup = Supervisor(a_root)
    a_server, a_task = await _serve(build_app(sup), a_sock)
    local = f"http://127.0.0.1:{a_port}"
    async with httpx.AsyncClient(base_url=local, headers={"X-ANX": "1"}, trust_env=False, timeout=30) as http:
        joined = await http.post("/api/cloud/join", json={"invite": enrollment.create_invite(label="A")["code"]})
        assert joined.status_code == 200, joined.text
        for _ in range(100):
            if (await http.get("/api/cloud")).json().get("state") == "connected":
                break
            await asyncio.sleep(0.2)

    windows = sys.platform == "win32"
    command, answer = ("echo ('anx-term-' + 6*7)", "anx-term-42") if windows else ("echo anx-term-$((6*7))", "anx-term-42")
    errors: list[str] = []
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="msedge" if windows else None, headless=True)
            context = await browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark", base_url=local)
            page = await context.new_page()
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            await page.goto("/#/devices")
            await page.wait_for_selector('[data-device="host-beta"] a[href="#/terminal/host-beta"]', timeout=20000)
            if out:
                out.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(out / "client-devices.png"))
            await page.click('[data-device="host-beta"] a[href="#/terminal/host-beta"]')
            await page.wait_for_selector('.term-status .pill[data-state="online"]', timeout=20000)
            await page.wait_for_timeout(2500)
            await page.click(".term-frame")
            await page.keyboard.type(command)
            await page.keyboard.press("Enter")
            rows = []
            for _ in range(80):
                rows = await page.evaluate("() => [...document.querySelectorAll('.xterm-rows > div')].map(r => r.textContent.trim())")
                if answer in rows:
                    break
                await page.wait_for_timeout(250)
            else:
                raise AssertionError(f"no {answer!r} in the client terminal: {rows[-8:]}")
            print("client A ran a command in device B's terminal")
            if out:
                await page.screenshot(path=str(out / "client-terminal.png"))
            # 锁定: the header lock, then the terminal is refused
            await page.click('header button[aria-label^="未锁定"]') if await page.locator('header button[aria-label^="未锁定"]').count() \
                else await page.click('button[aria-label^="未锁定"]')
            await page.wait_for_timeout(500)
            await page.evaluate("location.hash = '#/devices'")
            await page.wait_for_selector(".banner.warn", timeout=10000)
            disabled = await page.get_attribute('[data-device="host-beta"] a.btn', "aria-disabled")
            assert disabled == "true", disabled
            async with httpx.AsyncClient(base_url=local, trust_env=False, timeout=30) as http:
                refused = await http.get("/api/remote/devices/host-beta/terminal")
                assert refused.status_code == 423, refused.text
            print("locked: the terminal is refused")
            if out:
                await page.screenshot(path=str(out / "client-locked.png"))
            await browser.close()
    finally:
        terms.close_all()
        for task in tasks:
            task.cancel()
        for server in (a_server, gw_server):
            server.should_exit = True
        await asyncio.gather(*tasks, a_task, gw_task, return_exceptions=True)
        sup.shutdown() if hasattr(sup, "shutdown") else None
    errors = [e for e in errors if "favicon" not in e]
    if errors:
        print("errors:", *errors, sep="\n  ")
        return 1
    print("ok: client to client terminal")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path)
    return asyncio.run(main_async(parser.parse_args().out))


if __name__ == "__main__":
    sys.exit(main())
