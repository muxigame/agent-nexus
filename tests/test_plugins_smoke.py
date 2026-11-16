"""Browser smoke of the local console's 插件 page (docs/ui-spec.md §4.2): install from a folder,
configure a required secret, see it run; phone width without horizontal scroll.

Needs Playwright and Microsoft Edge; skipped otherwise:
``uv run --with playwright pytest tests/test_plugins_smoke.py``.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

pytest.importorskip("playwright")
import uvicorn  # noqa: E402
from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright  # noqa: E402

from agent_nexus.registry import CONFIG_NAME  # noqa: E402
from agent_nexus.supervisor import Supervisor  # noqa: E402
from agent_nexus.web import build_app  # noqa: E402

from test_plugins_m8 import V2_CONFIG, free_port, make_plugin  # noqa: E402


@pytest.fixture
def console(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    port = free_port()
    (root / CONFIG_NAME).write_text(json.dumps({"manager": {"port": port}, "plugin_paths": ["external"],
                                                "plugins": {"unity": {"enabled": False}}}), encoding="utf-8")
    sup = Supervisor(root)
    sup.run_background()  # what `anx serve` does: ticks refresh statuses
    server = uvicorn.Server(uvicorn.Config(build_app(sup), host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.1)
    yield f"http://127.0.0.1:{port}", root, sup
    server.should_exit = True
    thread.join(timeout=10)
    sup.shutdown()


def test_install_configure_and_run_from_the_plugins_page(console, tmp_path):
    base, root, sup = console
    folder = make_plugin(tmp_path / "src" / "fixture-mcp", **V2_CONFIG)
    secret = "smoke-secret-" + "x" * 12
    errors: list[str] = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="msedge")
        except PlaywrightError as exc:
            pytest.skip(f"Microsoft Edge is not available: {exc}")
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script("try{localStorage.setItem('anx-local-seen','1')}catch(e){}")
        page.goto(base + "/#/plugins")
        unity = page.locator('.plugin-row[data-name="unity"]')
        expect(unity).to_contain_text("未安装")

        page.get_by_role("button", name="安装插件").first.click()
        page.locator("#p-path").fill(str(folder))
        expect(page.locator(".modal")).to_contain_text("找到插件 fixture", timeout=15000)
        page.locator(".modal-foot button", has_text="安装").click()
        # needs a secret: the drawer opens on its configuration tab
        drawer = page.locator(".drawer")
        expect(drawer).to_contain_text("还缺少必填项", timeout=60000)
        drawer.get_by_label("团队").fill("core")
        drawer.get_by_label("Token").fill(secret)
        drawer.locator("button[type=submit]").click()
        row = page.locator('.plugin-row[data-name="fixture"]')
        expect(row).to_contain_text("运行中", timeout=30000)
        assert secret not in page.content()
        assert (root / "secrets" / "fixture" / "token").read_text().strip() == secret

        page.set_viewport_size({"width": 375, "height": 812})
        page.keyboard.press("Escape")
        page.goto(base + "/#/plugins")
        expect(page.locator('.plugin-row[data-name="fixture"]')).to_be_visible()
        assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
        # The 安装插件 dialog's source tabs: with 插件库 there are four, and none may be clipped on a phone.
        page.get_by_role("button", name="安装插件").first.click()
        expect(page.locator(".modal .source-tabs")).to_be_visible()
        clipped = page.evaluate("""() => {
            const bar = document.querySelector('.modal .source-tabs');
            while (bar.querySelectorAll('button').length < 4) bar.append(bar.querySelector('button').cloneNode(true));
            const box = bar.getBoundingClientRect();
            return [...bar.querySelectorAll('button')].filter(b => { const r = b.getBoundingClientRect();
                return r.left < box.left - 1 || r.right > box.right + 1 || b.scrollWidth > b.clientWidth + 1; }).length;
        }""")
        assert clipped == 0
        assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
        browser.close()
    assert errors == []
