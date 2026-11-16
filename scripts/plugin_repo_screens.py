"""Screenshots and a smoke run of the 插件库 (design §6.12, docs/ui-spec.md §3.3, §4.2) with real servers.

Starts, on ephemeral loopback ports and in a temporary directory:

* a gateway (OAuth owner password) whose plugin repository holds a demo
  package in two versions plus, with ``--packages DIR``, every ``*.zip`` there
  (e.g. the real packages in ``agent-nexus-dist/plugin-packages``);
* a local manager joined with an invite (announces ``plugin_repo/1``), an
  old device without that feature, and an offline device;

then drives Microsoft Edge (Playwright, ``channel="msedge"``) through the cloud
插件 page, "安装到…" with its live progress until the demo plugin runs on the
joined device, and the local 安装插件 dialog's 插件库 tab, at 1280 light,
1280 dark and 375 light. Every page is checked for console errors and
horizontal overflow.

    uv run --with playwright python scripts/plugin_repo_screens.py --out ../agent-nexus-dist/screens/plugin-repo \\
        --packages ../agent-nexus-dist/plugin-packages
    uv run --with playwright python scripts/plugin_repo_screens.py --smoke

Nothing here touches a real root, a live port, or the network beyond loopback.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for path in (str(ROOT), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from console_screens import AGENT_TOKEN, PASSWORD, VIEWPORTS, _device, _listener, _serve  # noqa: E402

DEMO_SERVER = r'''
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port = int(sys.argv[1])
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass
print(f"listening on 127.0.0.1:{port}", flush=True)
HTTPServer(("127.0.0.1", port), H).serve_forever()
'''
SLOW_STEP = ("import time\nfor step in ('解析依赖', '下载 3 个包', '创建虚拟环境', '安装完成'):\n"
             "    print(step, flush=True)\n    time.sleep(0.8)\n")


def demo_plugin(base: Path, version: str, port: int) -> Path:
    folder = base / f"notes-{version}" / "notes-mcp"
    folder.mkdir(parents=True)
    (folder / "server.py").write_text(DEMO_SERVER, encoding="utf-8")
    manifest = {"schema_version": 2, "name": "notes", "display_name": "笔记检索", "version": version,
                "description": "在本机笔记目录里检索和编辑 Markdown（演示插件）。",
                "command": [sys.executable, "server.py", "{port}"], "port": port,
                "install": {"steps": [{"run": [sys.executable, "-c", SLOW_STEP]}]},
                "health": {"path": "/healthz", "interval": 5}, "autostart": True, "autorestart": False}
    (folder / "mcp-service.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return folder


async def build_world(work: Path, packages: Path | None) -> dict:
    import os

    import httpx

    from agent_nexus import agent as agent_module
    from agent_nexus import join as joining
    from agent_nexus.agent import run_agent
    from agent_nexus.cloud import build_cloud_app
    from agent_nexus.plugins import pack
    from agent_nexus.supervisor import Supervisor
    from agent_nexus.web import build_app

    gw_sock, gw_port = _listener()
    origin = f"http://localhost:{gw_port}"
    os.environ["ANX_SHOT_AGENT_TOKEN"] = AGENT_TOKEN
    secrets = work / "gateway" / "secrets" / "workspace"
    secrets.mkdir(parents=True)
    (secrets / "oauth-password").write_text(PASSWORD, encoding="utf-8")
    state = work / "gateway" / "state"
    config = {"agents": {"pc-main": "ANX_SHOT_AGENT_TOKEN"}, "public_url": origin, "state_dir": str(state),
              "auth": {"mode": "oauth", "password_secret": "workspace/oauth-password", "scopes": ["tools"],
                       "required_scopes": ["tools"], "store": str(state / "oauth.sqlite3"), "title": "Agent Nexus"}}
    app = build_cloud_app(config, work / "gateway")
    gw_server, gw_task = await _serve(app, gw_sock)
    repo = app.state.plugin_repo

    def add(path: Path) -> None:
        incoming = repo.incoming()
        shutil.copyfile(path, incoming)
        repo.add(incoming, replace=True, by="owner")

    demo_port = _listener()
    demo_port[0].close()
    for version in ("1.0.0", "1.2.0"):
        result = pack.pack(demo_plugin(work / "src", version, demo_port[1]), work / "pkgs" / f"notes-{version}.zip")
        add(Path(result["path"]))
    real = sorted(packages.glob("*.zip")) if packages and packages.is_dir() else []
    for path in real:
        add(path)

    tasks: list[asyncio.Task] = []
    # An old device (no plugin_repo/1) on the env agents map, and an offline one joined earlier.
    legacy_root = work / "legacy-root"
    legacy_root.mkdir()
    ready = asyncio.Event()
    tasks.append(asyncio.create_task(run_agent(legacy_root, {
        "agent_id": "pc-main", "url": f"ws://127.0.0.1:{gw_port}", "token_env": "ANX_SHOT_AGENT_TOKEN", "hostname": "host-alpha",
        "workspaces": {"projects": {"path": str(legacy_root), "mcp": "device"}}, "refresh_interval_s": 60},
        transports={"device": _device("pc-main")}, ready=ready, status={})))
    await asyncio.wait_for(ready.wait(), 20)
    app.state.store.set_alias("pc-main", "旧工作站")
    enrollment = app.state.enrollment
    build_root = work / "build-root"
    build_root.mkdir()
    await asyncio.to_thread(joining.join, build_root, enrollment.create_invite(label="构建机")["code"], hostname="build-01")

    async def configured(root, status=None, hub=None):
        data = json.loads((root / "agent-nexus.json").read_text(encoding="utf-8"))
        cloud = data.get("cloud") or {}
        if not cloud.get("enabled"):
            status.update(state="disabled")
            return
        await run_agent(root, {**cloud, "hostname": "host-gamma", "workspaces": {"home": {"path": str(root), "mcp": "device"}}},
                        transports={"device": _device("gamma")}, status=status, hub=hub)
    agent_module.run_configured_agent = configured
    joining.local_hostname = lambda: "host-gamma"  # never the real machine name in a screenshot
    sock, port = _listener()
    root = work / "local-joined"
    root.mkdir()
    registered = {name: {"enabled": False} for name in ("ones", "unity")}
    (root / "agent-nexus.json").write_text(json.dumps({
        "manager": {"host": "127.0.0.1", "port": port}, "builtins": False, "scan": [], "plugins": registered,
        "cloud": {"enabled": False}}, ensure_ascii=False), encoding="utf-8")
    sup = Supervisor(root)
    sup.run_background()
    server, task = await _serve(build_app(sup), sock)
    local = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(base_url=local, headers={"X-ANX": "1"}, trust_env=False, timeout=30) as http:
        joined = await http.post("/api/cloud/join", json={"invite": enrollment.create_invite(label="办公室")["code"],
                                                          "agent_id": "host-gamma"})
        assert joined.status_code == 200, joined.text
    for _ in range(100):
        if any(p.agent_id == "host-gamma" for p in app.state.hub.peers.values()):
            break
        await asyncio.sleep(0.1)
    return {"origin": origin, "app": app, "local": local, "sup": sup, "real": [p.name for p in real],
            "servers": [(gw_server, gw_task), (server, task)], "tasks": tasks}


async def shoot(world: dict, out: Path | None, smoke: bool) -> list[str]:
    from playwright.async_api import async_playwright
    problems: list[str] = []
    origin, local = world["origin"], world["local"]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge", headless=True)
        try:
            for variant, (width, height, scheme) in VIEWPORTS.items():
                if smoke and variant == "dark":
                    continue
                context = await browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme,
                                                    device_scale_factor=1, locale="zh-CN", timezone_id="Asia/Shanghai")
                page = await context.new_page()
                errors: list[str] = []
                page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
                page.on("pageerror", lambda error: errors.append(str(error)))

                async def capture(name: str, *, full: bool = False):
                    await page.wait_for_timeout(300)
                    overflow = await page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                    if overflow > 1:
                        problems.append(f"{name}-{variant}: horizontal overflow {overflow}px")
                    for error in errors:
                        if "favicon" not in error:
                            problems.append(f"{name}-{variant}: console error: {error}")
                    errors.clear()
                    if out:
                        await page.screenshot(path=str(out / f"{name}-{variant}.png"), full_page=full)

                await page.goto(f"{origin}/", wait_until="networkidle")
                await page.wait_for_selector("#password", timeout=10000)
                await page.fill("#password", PASSWORD)
                await page.click("button[type=submit]")
                await page.wait_for_url(f"{origin}/**")
                # The login is a form POST and a redirect: wait for the console it lands on (as
                # console_screens.py does). wait_for_url alone matches the login page's own URL, and
                # a goto racing the redirect left only the 插件 page shell now and then.
                await page.wait_for_selector(".stat-value", timeout=20000)
                await page.goto(f"{origin}/#/plugins", wait_until="networkidle")
                try:
                    await page.wait_for_selector(".repo-row[data-package='notes']", timeout=15000)
                except Exception:
                    print(f"note: 插件库 row not shown after 15 s ({variant}); reloading once; "
                          f"console: {errors[-5:]}", flush=True)
                    await page.reload(wait_until="networkidle")  # once: a slow first catalog load
                    try:
                        await page.wait_for_selector(".repo-row[data-package='notes']", timeout=30000)
                    except Exception:
                        text = await page.inner_text("body")
                        raise RuntimeError(f"no 插件库 row ({variant}); console: {errors[-5:]}; "
                                           f"page: {text[:1500]}") from None
                await capture("cloud-plugins")
                if not smoke:
                    await capture("cloud-plugins-full", full=True)
                await page.click(".repo-row[data-package='notes'] >> text=安装到…")
                await page.wait_for_selector(".modal .device-choices")
                await capture("cloud-install-picker")
                await page.click(".modal-foot .btn-primary")
                await page.wait_for_selector(".modal .install-steps .step.current", timeout=30000)
                # A locator, not wait_for_function: the console's CSP forbids eval.
                await page.locator(".modal .install-log", has_text="解析依赖").wait_for(timeout=30000)
                await capture("cloud-install-progress")
                await page.wait_for_selector(".modal .banner.ok, .modal .banner.warn, .modal .banner.bad", timeout=120000)
                if await page.locator(".modal .banner.bad").count():
                    problems.append(f"cloud-install-done-{variant}: " + await page.locator(".modal .banner.bad").inner_text())
                await capture("cloud-install-done")
                await page.keyboard.press("Escape")

                await page.goto(f"{local}/#/plugins", wait_until="networkidle")
                await page.wait_for_selector(".plugin-row[data-name='notes']", timeout=20000)
                await page.wait_for_timeout(500)
                await capture("local-plugins")
                await page.click("button:has-text('安装插件') >> nth=0")
                await page.wait_for_selector(".modal .repo-choices .choice")
                await capture("local-install-repo")
                await page.keyboard.press("Escape")
                if await page.locator(".plugin-row[data-name='ones'] >> text=从插件库安装").count():
                    await page.click(".plugin-row[data-name='ones'] >> text=从插件库安装")
                    await page.wait_for_selector(".modal .repo-choices .choice")
                    await capture("local-install-repo-ones")
                    await page.keyboard.press("Escape")
                await context.close()
        finally:
            await browser.close()
    return problems


async def main_async(args) -> int:
    with tempfile.TemporaryDirectory(prefix="anx-repo-screens-", ignore_cleanup_errors=True) as temp:
        world = await build_world(Path(temp), Path(args.packages).resolve() if args.packages else None)
        out = Path(args.out).resolve() if args.out else None
        try:
            if out:
                out.mkdir(parents=True, exist_ok=True)
            problems = await shoot(world, out, args.smoke)
        finally:
            world["sup"].shutdown()
            for task in world["tasks"]:
                task.cancel()
            await asyncio.gather(*world["tasks"], return_exceptions=True)
            for server, _ in world["servers"]:
                server.should_exit = True
            await asyncio.gather(*(task for _, task in world["servers"]), return_exceptions=True)
    for problem in problems:
        print("PROBLEM", problem)
    print(f"real packages listed: {', '.join(world['real']) or 'none'}")
    print(f"{'smoke' if args.smoke else 'screenshots'}: {len(problems)} problem(s)" + (f"; saved to {out}" if out else ""))
    return 1 if problems else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", help="directory for the PNGs (outside the repository)")
    parser.add_argument("--packages", help="a directory of packages (*.zip) to list in the 插件库 as well")
    parser.add_argument("--smoke", action="store_true", help="light and phone only; fail on any problem")
    args = parser.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
