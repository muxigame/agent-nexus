"""Screenshots and a smoke run of the web consoles with real servers (docs/ui-spec.md §5).

Starts, on ephemeral loopback ports and in temporary directories:

* a gateway with OAuth (owner password), sample devices (one joined with an
  invite, one from the env agents map), mail and an open question for ``me``,
  an unused invite and an installer downloads directory;
* two local managers: one not yet joined (the "加入" page with a pasted code)
  and one joined through ``POST /api/cloud/join``;

then drives Microsoft Edge (Playwright, ``channel="msedge"``; no browser
download) through the login page, every console page, the invite dialog, the
PWA, the public downloads page and the local "连接云端" page. Every page is
checked for console errors and horizontal overflow.

    uv run --with playwright python scripts/console_screens.py --out ../agent-nexus-dist/screens/gateway
    uv run --with playwright python scripts/console_screens.py --smoke       # checks only, a few shots

Nothing here touches a real root, the network beyond loopback, or a live port.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASSWORD = "owner-password-for-screenshots-only-0123456789"
AGENT_TOKEN = "legacy-device-token-for-screenshots-only-0123456789"
VIEWPORTS = {"light": (1280, 800, "light"), "dark": (1280, 800, "dark"), "phone": (375, 812, "light")}


def _listener() -> tuple[socket.socket, int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))  # ephemeral: never a live service port
    sock.listen()
    return sock, sock.getsockname()[1]


async def _serve(app, sock):
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    if not server.started:
        raise RuntimeError("server did not start")
    return server, task


def _device(name: str):
    from fastmcp import FastMCP
    server = FastMCP(f"device-{name}")

    @server.tool
    async def execute(command: str) -> dict:
        """Run a command on this device."""
        return {"device": name, "command": command}

    @server.tool
    async def file_read(path: str) -> dict:
        """Read a file on this device."""
        return {"device": name, "path": path}

    return server


async def build_world(work: Path, downloads_dir: Path | None):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from agent_nexus import join as joining
    from agent_nexus.agent import run_agent
    from agent_nexus.cloud import build_cloud_app
    from agent_nexus.inbox_web import oauth_provider

    gw_sock, gw_port = _listener()
    origin = f"http://localhost:{gw_port}"
    os.environ["ANX_SHOT_AGENT_TOKEN"] = AGENT_TOKEN
    secrets = work / "gateway" / "secrets" / "workspace"
    secrets.mkdir(parents=True)
    (secrets / "oauth-password").write_text(PASSWORD, encoding="utf-8")
    state = work / "gateway" / "state"
    downloads = state / "downloads"
    downloads.mkdir(parents=True)
    if downloads_dir and downloads_dir.is_dir():
        for path in downloads_dir.iterdir():
            if path.is_file() and path.name in ("agent-nexus_0.2.0_x64-setup.exe", "agent-nexus-gateway-0.2.0.zip", "agent-nexus.exe", "SHA256SUMS"):
                with contextlib.suppress(OSError):
                    os.link(path, downloads / path.name)
                if not (downloads / path.name).exists():
                    shutil.copy2(path, downloads / path.name)
    else:
        (downloads / "agent-nexus_0.2.0_x64-setup.exe").write_bytes(b"MZ" + os.urandom(4096))
        (downloads / "agent-nexus-gateway-0.2.0.zip").write_bytes(b"PK" + os.urandom(2048))
        (downloads / "agent-nexus_0.2.0_amd64.deb").write_bytes(b"!<arch>" + os.urandom(4096))
    config = {"agents": {"pc-main": "ANX_SHOT_AGENT_TOKEN"}, "public_url": origin, "state_dir": str(state),
              "auth": {"mode": "oauth", "password_secret": "workspace/oauth-password", "scopes": ["tools"],
                       "required_scopes": ["tools"], "store": str(state / "oauth.sqlite3"), "title": "Agent Nexus"},
              "downloads": {"links": [{"label": "GitHub Releases", "url": "https://github.com/", "description": "历史版本与发布说明"}]}}
    app = build_cloud_app(config, work / "gateway")
    gw_server, gw_task = await _serve(app, gw_sock)
    tasks: list[asyncio.Task] = []
    http_base = f"http://127.0.0.1:{gw_port}"

    # A device from the env agents map (the pre-0.2 way) and one that joins with an invite.
    legacy_root = work / "legacy-root"
    legacy_root.mkdir()
    ready = asyncio.Event()
    tasks.append(asyncio.create_task(run_agent(legacy_root, {
        "agent_id": "pc-main", "url": f"ws://127.0.0.1:{gw_port}", "token_env": "ANX_SHOT_AGENT_TOKEN", "hostname": "host-alpha",
        "workspaces": {"projects": {"path": str(legacy_root), "mcp": "device"}}, "refresh_interval_s": 60},
        transports={"device": _device("pc-main")}, ready=ready, status={})))
    await asyncio.wait_for(ready.wait(), 20)
    enrollment = app.state.enrollment
    laptop_root = work / "laptop-root"
    laptop_root.mkdir()
    created = enrollment.create_invite(label="笔记本")
    await asyncio.to_thread(joining.join, laptop_root, created["code"], hostname="host-beta")
    cloud = json.loads((laptop_root / "agent-nexus.json").read_text(encoding="utf-8"))["cloud"]
    ready = asyncio.Event()
    tasks.append(asyncio.create_task(run_agent(laptop_root, {**cloud, "workspaces": {"home": {"path": str(laptop_root), "mcp": "device"}}},
                                               transports={"device": _device("laptop")}, ready=ready, status={})))
    await asyncio.wait_for(ready.wait(), 20)
    # Offline devices of other systems, so every device icon shows (facts as each client would report them).
    real_facts = joining.device_facts
    for host, facts, revoke in (
            ("build-01", {"platform": "linux", "os": "6.8.0-48-generic", "arch": "x64", "form": "pc", "distro": "ubuntu"}, False),
            ("mac-studio", {"platform": "macos", "os": "24.6.0", "arch": "arm64", "form": "pc"}, False),
            ("pixel-8", {"platform": "android", "os": "Android 15", "arch": "arm64", "form": "phone"}, False),
            ("old-debian", {"platform": "linux", "os": "6.1.0-25-amd64", "arch": "x64", "form": "pc", "distro": "debian"}, True)):
        joining.device_facts = lambda facts=facts: dict(facts)
        root = work / f"{host}-root"
        root.mkdir()
        await asyncio.to_thread(joining.join, root, enrollment.create_invite(label=host)["code"], hostname=host)
        if revoke:
            await enrollment.revoke_device(host)
    joining.device_facts = real_facts
    app.state.store.set_alias("pc-main", "工作站")
    enrollment.create_invite(label="家里的台式机")
    enrollment.create_invite(label="测试虚拟机", ttl_s=5 * 86400)

    # Mail and a question for me, sent by sessions through the MCP endpoint.
    provider = oauth_provider(app.state.mcp.auth)
    token = provider.issue("anx-inbox", ["tools"]).access_token
    async with Client(StreamableHttpTransport(f"{http_base}/mcp", auth=token)) as client:
        codex = (await client.call_tool("use_device", {"device_id": "pc-main", "session_name": "codex", "kind": "codex"})).data
        await client.call_tool("mail_send", {"to": ["me"], "subject": "夜间构建完成", "workspace_id": codex["workspace_id"],
                                             "body": "全部 412 个测试通过，安装包已经复制到网关的下载目录。"})
        await client.call_tool("ask_user", {"question": "现在合并 release/0.2.1 到 main 并打 tag 吗？", "options": ["只合并", "合并并打 tag", "先别合并"],
                                            "recommended": "合并并打 tag", "context": "测试全部通过，变更 14 个文件。",
                                            "blocking": False, "workspace_id": codex["workspace_id"]})
        claude = (await client.call_tool("use_device", {"device_id": "host-beta", "session_name": "claude", "kind": "claude"})).data
        await client.call_tool("mail_send", {"to": ["me"], "subject": "周报草稿", "workspace_id": claude["workspace_id"],
                                             "body": "已按上周的格式整理好三段内容，请过目。"})
        # A session without session_name: an automatic name, and the title its hook reported (design §8.2/§8.3).
        unnamed = (await client.call_tool("use_device", {"device_id": "pc-main", "kind": "claude"})).data
        app.state.gateway.mailbox.presence_report("pc-main", {"kind": "claude", "cwd": unnamed["path"], "session_id": "s-shot",
                                                              "event": "SessionStart", "title": "整理周报素材"})
        await client.call_tool("mail_send", {"to": [unnamed["address"]], "subject": "素材在共享盘", "workspace_id": codex["workspace_id"],
                                             "body": "上周的 ONES 工时已导出。"})

    # Local managers: one fresh (join page), one joined through the console API.
    from agent_nexus.supervisor import Supervisor
    from agent_nexus.web import build_app
    from agent_nexus import agent as agent_module

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
    locals_ = {}
    for name in ("fresh", "joined"):
        sock, port = _listener()
        root = work / f"local-{name}"
        root.mkdir()
        data = {"manager": {"host": "127.0.0.1", "port": port}, "builtins": False, "scan": [], "cloud": {"enabled": False}}
        if name == "joined":
            # Plugins in different states (design §6): one running, one waiting for its
            # settings, one that keeps crashing, and two the installed client lists
            # without code yet (未安装).
            data.update(plugin_paths=["external"], plugins={"unity": {"enabled": False}, "chrome-devtools": {"enabled": False}})
            _demo_plugins(root / "external")
        (root / "agent-nexus.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        sup = Supervisor(root)
        if name == "joined":
            sup.run_background()
            await asyncio.to_thread(sup.autostart)
        server, task = await _serve(build_app(sup), sock)
        locals_[name] = {"url": f"http://127.0.0.1:{port}", "server": server, "task": task, "root": root, "sup": sup}
    import httpx
    async with httpx.AsyncClient(base_url=locals_["joined"]["url"], headers={"X-ANX": "1"}, trust_env=False, timeout=30) as local:
        joined = await local.post("/api/cloud/join", json={"invite": enrollment.create_invite(label="办公室")["code"]})
        assert joined.status_code == 200, joined.text
    invite_for_page = enrollment.create_invite(label="粘贴演示")["code"]

    # Connectors (design §7): a header-auth upstream that works, one that needs an
    # OAuth login, and one that cannot be reached. All upstreams are loopback stubs.
    upstreams = await _connector_upstreams()
    host = app.state.connectors
    await host.create({"name": "yunxiao", "title": "云效 DevOps", "url": f"{upstreams['http']}/mcp",
                       "auth": {"type": "headers", "headers": {"X-Api-Key": "screenshot-key-0123456789", "X-Region": "cn-hangzhou"}}})
    await host.create({"name": "cloudflare", "title": "Cloudflare API", "url": f"{upstreams['oauth']}/mcp",
                       "auth": {"type": "oauth"}})
    await host.create({"name": "wiki", "title": "内网知识库", "url": "http://127.0.0.1:9/sse", "transport": "sse",
                       "auth": {"type": "bearer", "token": "screenshot-token-0123456789"}})
    for name in ("yunxiao", "wiki"):
        await host.test(name)
    # An uploaded icon (a plain placeholder mark, not a vendor logo); the others fall back to initials.
    host.upload_icon("yunxiao", b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" '
                                b'fill="#1F6FEB"/><path d="M8 20l5-9 4 6 3-4 4 7z" fill="#fff"/></svg>')
    return {"origin": origin, "app": app,
            "servers": [(gw_server, gw_task)] + [(v["server"], v["task"]) for v in locals_.values()] + upstreams["servers"],
            "tasks": tasks, "local": locals_, "invite": invite_for_page, "upstreams": upstreams}


PLUGIN_SERVER = r'''
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port, mode = int(sys.argv[1]), sys.argv[2]
if mode == "crash":
    print("Traceback (most recent call last):", flush=True)
    print("ConnectionError: 无法连接到 127.0.0.1:6400", flush=True)
    sys.exit(3)
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass
print(f"listening on 127.0.0.1:{port}", flush=True)
HTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def _demo_plugins(base: Path) -> None:
    """Stdlib stand-ins for plugins (never real services): healthy, needs settings, crashes."""
    plugins = [
        ("notes", "ok", {"display_name": "笔记", "description": "在本机笔记目录里检索和编辑 Markdown。", "version": "1.3.0"}),
        ("ones", "ok", {"display_name": "ONES 工时", "description": "读取 ONES 任务与工时，生成周报素材。", "version": "0.4.2",
                        "schema_version": 2,
                        "config": {"schema": {"type": "object", "required": ["team"], "properties": {
                            "team": {"type": "string", "title": "团队", "minLength": 1},
                            "region": {"type": "string", "title": "区域", "enum": ["cn", "global"], "default": "cn"}}}},
                        "secrets": [{"name": "token", "title": "API Token", "required": True}]}),
        ("blender", "crash", {"display_name": "Blender 桥接", "description": "通过本机 Blender 插件执行场景操作。", "version": "0.9.0"}),
    ]
    for name, mode, manifest in plugins:
        directory = base / name
        directory.mkdir(parents=True)
        (directory / "server.py").write_text(PLUGIN_SERVER, encoding="utf-8")
        sock, port = _listener()
        sock.close()
        data = {"name": name, "command": [sys.executable, "server.py", "{port}", mode], "port": port,
                "health": {"path": "/healthz", "interval": 5}, "autostart": True, "autorestart": False, **manifest}
        (directory / "mcp-service.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


async def _connector_upstreams() -> dict:
    """Loopback stand-ins for third-party MCP servers (never the real services)."""
    from fastmcp import FastMCP
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    devops = FastMCP("yunxiao-stub", version="1.4.0")
    for tool_name, text in (("list_projects", "列出项目"), ("get_pipeline", "查看流水线"), ("run_pipeline", "运行流水线"),
                            ("list_work_items", "列出工作项"), ("create_work_item", "创建工作项")):
        async def handler(project: str = "") -> dict:
            return {"ok": True}
        devops.tool(handler, name=tool_name, description=f"{text}（演示用的上游服务）")
    inner = devops.http_app(path="/mcp")

    async def gated(scope, receive, send):
        if scope["type"] == "http" and dict(scope["headers"]).get(b"x-api-key") != b"screenshot-key-0123456789":
            await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        await inner(scope, receive, send)

    sock, port = _listener()
    servers = [await _serve(gated, sock)]
    http_origin = f"http://127.0.0.1:{port}"

    sock, port = _listener()
    oauth_origin = f"http://127.0.0.1:{port}"

    async def protected(request):
        return Response(status_code=401, headers={
            "WWW-Authenticate": f'Bearer resource_metadata="{oauth_origin}/.well-known/oauth-protected-resource/mcp"'})

    async def prm(request):
        return JSONResponse({"resource": f"{oauth_origin}/mcp", "authorization_servers": [oauth_origin]})
    servers.append(await _serve(Starlette(routes=[Route("/mcp", protected, methods=["GET", "POST"]),
                                                  Route("/.well-known/oauth-protected-resource/mcp", prm)]), sock))
    return {"http": http_origin, "oauth": oauth_origin, "servers": servers}


async def shoot(world, out: Path | None, smoke: bool) -> list[str]:
    from playwright.async_api import async_playwright
    problems: list[str] = []
    origin = world["origin"]
    pages = [
        ("cloud-overview", "/#/"), ("cloud-devices", "/#/devices"), ("cloud-inbox", "/#/inbox"),
        ("cloud-downloads", "/#/downloads"), ("cloud-settings", "/#/settings"), ("cloud-connectors", "/#/connectors"),
        ("cloud-plugins", "/#/plugins"), ("cloud-agent", "/#/agent"),
    ]
    if smoke:
        pages = pages[:3] + [("cloud-connectors", "/#/connectors")]
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

                async def focus_check(name: str, presses: int = 14):
                    """Tab through the page: every focused control must show a ring (outline or box-shadow)."""
                    if variant != "light":
                        return
                    await page.mouse.click(1, 1)
                    for _ in range(presses):
                        await page.keyboard.press("Tab")
                        seen = await page.evaluate("""() => {
                            const node = document.activeElement;
                            if (!node || node === document.body) return null;
                            const style = getComputedStyle(node);
                            const ring = (style.outlineStyle !== "none" && parseFloat(style.outlineWidth) > 0) || (style.boxShadow && style.boxShadow !== "none");
                            return ring ? "" : (node.tagName.toLowerCase() + "." + node.className + " " + (node.textContent || "").trim().slice(0, 20));
                        }""")
                        if seen:
                            problems.append(f"{name}: no focus ring on {seen}")

                async def capture(name: str, *, full: bool = False):
                    await page.wait_for_timeout(350)
                    overflow = await page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                    if overflow > 1:
                        problems.append(f"{name}-{variant}: horizontal overflow {overflow}px")
                    for error in errors:
                        # the failed sign-in page is served with 401 on purpose
                        if "favicon" not in error and not (name in ("cloud-login-error", "oauth-consent-error") and "401" in error):
                            problems.append(f"{name}-{variant}: console error: {error}")
                    errors.clear()
                    if out:
                        await page.screenshot(path=str(out / f"{name}-{variant}.png"), full_page=full)
                        if variant == "phone" and full is False and name.startswith(("cloud-overview", "cloud-devices", "cloud-downloads", "cloud-settings", "local-join")):
                            await page.screenshot(path=str(out / f"{name}-phone-full.png"), full_page=True)

                await page.goto(f"{origin}/", wait_until="networkidle")
                if out and variant == "light":
                    print("login page at", page.url, flush=True)
                await page.wait_for_selector("#password", timeout=10000)
                await capture("cloud-login")
                await page.fill("#password", "wrong-password")
                await page.click("button[type=submit]")
                await page.wait_for_load_state("networkidle")
                await capture("cloud-login-error")
                await page.fill("#password", PASSWORD)
                await page.click("button[type=submit]")
                await page.wait_for_url(f"{origin}/**")
                await page.wait_for_selector(".stat-value")
                for name, path in pages:
                    await page.goto(f"{origin}{path}", wait_until="networkidle")
                    await page.wait_for_timeout(300)
                    await capture(name)
                    if name in ("cloud-overview", "cloud-devices", "cloud-connectors", "cloud-plugins"):
                        await focus_check(name)
                    if name == "cloud-inbox":
                        # The viewer dropdown: grouped by device, the hook's title shown, no legacy anon rows.
                        await page.wait_for_selector("select[aria-label='查看邮箱'] optgroup", state="attached")
                        labels = await page.eval_on_selector_all("select[aria-label='查看邮箱'] option", "nodes => nodes.map(n => n.textContent)")
                        if not any(label.startswith("整理周报素材 — claude-") and "1 未读" in label for label in labels) \
                                or any(label.startswith("anon-") for label in labels):
                            problems.append(f"cloud-inbox-{variant}: viewer options {labels}")
                        viewer = "select[aria-label='查看邮箱']"
                        value = await page.eval_on_selector(viewer, "s => ([...s.options].find(o => o.textContent.startsWith('整理周报素材')) || {}).value")
                        if value:
                            await page.select_option(viewer, value)
                            await page.wait_for_selector(".thread-item")
                            await capture("cloud-inbox-viewer")
                            await page.select_option(viewer, "me")
                            await page.wait_for_timeout(300)
                        await page.click(".thread-item >> nth=0")
                        await page.wait_for_selector(".ask-card")
                        await capture("cloud-inbox-thread")
                    if name == "cloud-connectors":
                        await page.wait_for_selector("tr[data-connector='yunxiao']")
                        await page.click("tr[data-connector='yunxiao'] >> text=测试")
                        await page.wait_for_selector(".modal .list-item")
                        await capture("cloud-connectors-test")
                        await page.keyboard.press("Escape")
                        await page.click("text=添加连接器 >> nth=0")
                        await page.wait_for_selector("#c-url")
                        await page.fill("#c-url", f"{world['upstreams']['oauth']}/mcp")
                        await page.click(".drawer .input-group .btn")
                        await page.wait_for_selector(".drawer .banner.ok, .drawer .banner.warn", timeout=20000)
                        await capture("cloud-connectors-add-oauth")
                        await page.fill("#c-url", f"{world['upstreams']['http']}/mcp")
                        await page.fill("#c-name", "devops")
                        await page.click(".drawer button[data-value='headers']")
                        await capture("cloud-connectors-add-headers")
                        await page.keyboard.press("Escape")
                    if name == "cloud-devices":
                        await page.click("text=邀请新设备 >> nth=0")
                        await page.wait_for_selector("#invite-label")
                        await page.fill("#invite-label", "家里的台式机")
                        await capture("cloud-devices-invite-form")
                        await page.click(".modal-foot .btn-primary")
                        await page.wait_for_selector(".qr-box svg")
                        await capture("cloud-devices-invite-result")
                        await page.keyboard.press("Escape")
                        await page.click("button[aria-label='更多操作'] >> nth=0")
                        await capture("cloud-devices-menu")
                        await page.keyboard.press("Escape")
                        await page.mouse.click(5, 5)
                if not smoke:
                    await page.goto(f"{origin}/inbox/", wait_until="networkidle")
                    await page.wait_for_selector(".thread-item")
                    await capture("pwa-inbox")
                    await page.goto(f"{origin}/downloads", wait_until="networkidle")
                    await page.wait_for_selector(".card.accent")
                    await capture("public-downloads")
                if not smoke:
                    # The OAuth consent page ChatGPT opens (design §5.3), on a fresh flow.
                    await page.goto(await _consent_url(origin), wait_until="networkidle")
                    await page.wait_for_selector("input[name=password]")
                    await capture("oauth-consent")
                    await page.fill("input[name=password]", "wrong-password")
                    await page.click("button[value=approve]")
                    await page.wait_for_load_state("networkidle")
                    await capture("oauth-consent-error")
                local = world["local"]
                await page.goto(f"{local['fresh']['url']}/cloud?invite={world['invite']}", wait_until="networkidle")
                await page.wait_for_selector("#invite")
                await capture("local-join")
                await page.goto(f"{local['joined']['url']}/cloud", wait_until="networkidle")
                await page.wait_for_selector(".kv")
                await capture("local-joined")
                if not smoke:
                    joined = local["joined"]["url"]
                    await page.goto(f"{joined}/#/", wait_until="networkidle")
                    await page.wait_for_timeout(600)
                    await capture("local-overview")
                    await page.goto(f"{joined}/#/agent", wait_until="networkidle")
                    await page.wait_for_timeout(800)
                    await capture("local-agent")
                    await page.goto(f"{joined}/#/plugins", wait_until="networkidle")
                    await page.wait_for_selector(".plugin-row[data-name='blender']")
                    await page.wait_for_timeout(600)
                    await capture("local-plugins")
                    await focus_check("local-plugins", 18)
                    await page.click(".plugin-row[data-name='ones']")
                    await page.wait_for_selector(".drawer")
                    await page.wait_for_timeout(500)
                    await capture("local-plugin-ones")
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(300)
                    await page.click(".plugin-row[data-name='blender']")
                    await page.wait_for_selector(".drawer")
                    with contextlib.suppress(Exception):
                        await page.click(".drawer [role=tab]:has-text('日志')", timeout=2000)
                    await page.wait_for_timeout(600)
                    await capture("local-plugin-logs")
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(300)
                    await page.click("button:has-text('安装插件') >> nth=0")
                    await page.wait_for_selector(".modal")
                    await capture("local-plugin-install")
                    await page.keyboard.press("Escape")
                    await page.goto(f"{joined}/#/inbox", wait_until="networkidle")
                    await page.wait_for_timeout(800)
                    await capture("local-inbox")
                    await page.goto(f"{joined}/#/settings", wait_until="networkidle")
                    await page.wait_for_timeout(600)
                    await capture("local-settings")
                    await page.goto(f"{local['fresh']['url']}/#/inbox", wait_until="networkidle")
                    await page.wait_for_timeout(500)
                    await capture("local-inbox-offline")
                await context.close()
        finally:
            await browser.close()
    return problems


async def _consent_url(origin: str) -> str:
    """Register a ChatGPT-shaped client and start an authorization; return the consent page URL."""
    import base64
    import hashlib
    import secrets as pysecrets
    from urllib.parse import urlencode
    import httpx
    redirect = "https://chatgpt.com/connector_platform_oauth_redirect"
    async with httpx.AsyncClient(base_url=origin, trust_env=False, timeout=30, follow_redirects=False) as http:
        client = (await http.post("/register", json={"redirect_uris": [redirect], "client_name": "ChatGPT",
                                                     "token_endpoint_auth_method": "none",
                                                     "grant_types": ["authorization_code", "refresh_token"],
                                                     "response_types": ["code"]})).json()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(pysecrets.token_urlsafe(48).encode()).digest()).rstrip(b"=").decode()
        answer = await http.get("/authorize?" + urlencode({
            "response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect, "state": "screens",
            "code_challenge": challenge, "code_challenge_method": "S256", "scope": "tools"}))
        location = answer.headers.get("location", "")
        if "/login?" not in location:
            raise RuntimeError(f"authorize did not reach the consent page: {answer.status_code} {answer.text[:200]}")
        return location if location.startswith("http") else origin + location


async def main_async(args) -> int:
    with tempfile.TemporaryDirectory(prefix="anx-screens-", ignore_cleanup_errors=True) as temp:
        world = await build_world(Path(temp), Path(args.downloads) if args.downloads else None)
        try:
            out = Path(args.out).resolve() if args.out else None
            if out:
                out.mkdir(parents=True, exist_ok=True)
            problems = await shoot(world, out, args.smoke)
        finally:
            for item in world["local"].values():
                item["sup"].shutdown()
            for task in world["tasks"]:
                task.cancel()
            await asyncio.gather(*world["tasks"], return_exceptions=True)
            for server, task in world["servers"]:
                server.should_exit = True
            await asyncio.gather(*(task for _, task in world["servers"]), return_exceptions=True)
    for problem in problems:
        print("PROBLEM", problem)
    print(f"{'smoke' if args.smoke else 'screenshots'}: {len(problems)} problem(s)" + (f"; saved to {out}" if args.out else ""))
    return 1 if problems else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", help="directory for the PNGs (outside the repository)")
    parser.add_argument("--smoke", action="store_true", help="fewer pages and viewports; fail on any problem")
    parser.add_argument("--downloads", help="a directory with release artifacts to list on the downloads page")
    args = parser.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
