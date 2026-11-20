"""The console's remote terminal against a real device link, in Edge (Playwright).

A gateway with an owner password and one device on this machine whose manager-side terminal
handlers are the real ones (term/1). Edge signs in, opens #/terminal/pc-main, runs a command in
the device's default shell and checks the answer shows up in xterm.js; screenshots in light,
dark and phone sizes.

    uv run --with playwright python scripts/terminal_screens.py [--out DIR]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from console_screens import PASSWORD, _device, _listener, _serve  # noqa: E402


async def main_async(out: Path | None) -> int:
    from agent_nexus import terminal
    from agent_nexus.agent import HubLink, run_agent
    from agent_nexus.cloud import build_cloud_app
    from playwright.async_api import async_playwright

    work = Path(tempfile.mkdtemp(prefix="anx-term-"))
    sock, port = _listener()
    origin = f"http://localhost:{port}"
    os.environ["ANX_TERM_AGENT_TOKEN"] = "terminal-device-token-for-screens-0123456789"
    secrets = work / "gateway/secrets/workspace"
    secrets.mkdir(parents=True)
    (secrets / "oauth-password").write_text(PASSWORD, encoding="utf-8")
    state = work / "gateway/state"
    config = {"agents": {"pc-main": "ANX_TERM_AGENT_TOKEN"}, "public_url": origin, "state_dir": str(state),
              "auth": {"mode": "oauth", "password_secret": "workspace/oauth-password", "scopes": ["tools"],
                       "required_scopes": ["tools"], "store": str(state / "oauth.sqlite3")}}
    app = build_cloud_app(config, work / "gateway")
    server, task = await _serve(app, sock)
    hub = HubLink()
    terms = terminal.Terminals()
    hub.handlers.update(terms.handlers())
    hub.extra_features = ("term/1",)
    ready = asyncio.Event()
    device_root = work / "device"
    device_root.mkdir()
    agent = asyncio.create_task(run_agent(device_root, {
        "agent_id": "pc-main", "url": f"ws://127.0.0.1:{port}", "token_env": "ANX_TERM_AGENT_TOKEN", "hostname": "host-alpha",
        "workspaces": {"home": {"path": str(device_root), "mcp": "device"}}, "refresh_interval_s": 60},
        transports={"device": _device("pc-main")}, ready=ready, status={}, hub=hub))
    await asyncio.wait_for(ready.wait(), 20)
    windows = sys.platform == "win32"
    command, answer = ("echo ('anx-term-' + 6*7)", "anx-term-42") if windows else ("echo anx-term-$((6*7))", "anx-term-42")
    errors: list[str] = []
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="msedge" if windows else None, headless=True)
            for name, (width, height, scheme) in {"light": (1280, 800, "light"), "dark": (1280, 800, "dark"),
                                                  "phone": (390, 844, "light")}.items():
                context = await browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme,
                                                    base_url=origin)
                page = await context.new_page()
                page.on("console", lambda msg: errors.append(f"{name}: {msg.text}") if msg.type == "error" else None)
                page.on("pageerror", lambda exc: errors.append(f"{name}: {exc}"))
                await page.goto("/auth/login")
                await page.fill('input[name="password"]', PASSWORD)
                await page.click('button[type="submit"]')
                await page.wait_for_url(f"{origin}/")
                await page.wait_for_selector(".page-head", timeout=15000)
                await page.evaluate("location.hash = '#/devices'")
                await page.wait_for_selector('a[href="#/terminal/pc-main"]', timeout=15000)
                await page.click('a[href="#/terminal/pc-main"]')
                await page.wait_for_selector(".xterm-rows", timeout=15000)
                await page.wait_for_selector('.term-status .pill[data-state="online"]', timeout=20000)
                await page.wait_for_timeout(2500)  # the prompt
                await page.click(".term-frame")
                await page.keyboard.type(command)
                await page.keyboard.press("Enter")
                rows = []
                for _ in range(80):  # the console's CSP forbids wait_for_function's eval
                    rows = await page.evaluate("() => [...document.querySelectorAll('.xterm-rows > div')].map(r => r.textContent.trim())")
                    if answer in rows:
                        break
                    await page.wait_for_timeout(250)
                else:
                    raise AssertionError(f"{name}: no {answer!r} in the terminal: {rows[-8:]}")
                chips = await page.locator(".chip").count()
                print(f"{name}: command answered in the browser terminal; {chips} CLI chips")
                if out:
                    out.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(out / f"terminal-{name}.png"))
                overflow = await page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                if overflow > 1:
                    errors.append(f"{name}: horizontal overflow {overflow}px")
                await context.close()
            await browser.close()
    finally:
        terms.close_all()
        agent.cancel()
        server.should_exit = True
        await asyncio.gather(agent, task, return_exceptions=True)
    if errors:
        print("errors:", *errors, sep="\n  ")
        return 1
    print("ok: console terminal")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path)
    return asyncio.run(main_async(parser.parse_args().out))


if __name__ == "__main__":
    sys.exit(main())
