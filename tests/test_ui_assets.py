"""The shared design system is served under /ui/ by the gateway and by the local console."""
import pytest
from starlette.testclient import TestClient

from agent_nexus.cloud import build_cloud_app
from agent_nexus.supervisor import Supervisor
from agent_nexus.ui_assets import UI_DIR, ui_file
from agent_nexus.web import build_app

CORE = {"tokens.css": "text/css", "ui.css": "text/css", "icons.js": "text/javascript", "ui.js": "text/javascript",
        "appearance.js": "text/javascript", "style-pure.css": "text/css", "style-mica.css": "text/css",
        "LICENSE-lucide.txt": "text/plain"}


def gateway_client(monkeypatch, tmp_path):
    for key, value in {"SERVER_TOKEN": "s" * 40, "A_TOKEN": "a" * 40}.items():
        monkeypatch.setenv(key, value)
    config = {"token_env": "SERVER_TOKEN", "agents": {"pc-a": "A_TOKEN"}}
    return TestClient(build_cloud_app(config, tmp_path))


def local_client(tmp_path):
    return TestClient(build_app(Supervisor(tmp_path)), base_url="http://127.0.0.1:8399")


def test_design_system_files_exist():
    for name in CORE:
        assert (UI_DIR / name).is_file(), name
    # ui.js imports icons.js relatively, so both mounts resolve it under /ui/.
    assert 'from "./icons.js"' in (UI_DIR / "ui.js").read_text(encoding="utf-8")


@pytest.mark.parametrize("which", ["gateway", "local"])
def test_ui_assets_are_served_with_types(which, monkeypatch, tmp_path):
    client = gateway_client(monkeypatch, tmp_path) if which == "gateway" else local_client(tmp_path)
    with client:
        for name, media_type in CORE.items():
            response = client.get(f"/ui/{name}")
            assert response.status_code == 200, name
            assert response.headers["content-type"].startswith(media_type)
            assert response.headers["x-content-type-options"] == "nosniff"
        assert client.get("/ui/missing.css").status_code == 404
        assert client.get("/ui/..%2Fweb.py").status_code == 404
        assert client.get("/ui/ui.css/x").status_code == 404


@pytest.mark.parametrize("name", ["../web.py", "..", ".hidden.css", "sub/ui.css", "ui.py", "", "a" * 80 + ".css"])
def test_ui_file_rejects_unsafe_names(name):
    assert ui_file(name) is None


def test_ui_has_no_external_urls_at_runtime():
    for path in UI_DIR.iterdir():
        if path.suffix in (".css", ".js"):
            text = path.read_text(encoding="utf-8")
            for marker in ("@import url(http", "src=\"http", "href=\"http", "fonts.googleapis", "cdn."):
                assert marker not in text, (path.name, marker)


def test_every_page_loads_the_appearance_styles():
    """Each page on the design system boots the style before paint and loads both scoped skins after ui.css."""
    static = UI_DIR.parent
    pages = [static / name for name in ("console.html", "index.html", "pwa.html", "downloads.html")]
    for page in pages:
        text = page.read_text(encoding="utf-8")
        boot, ui = text.index("/ui/appearance.js"), text.index("/ui/ui.css")
        assert boot < text.index("/ui/tokens.css") < ui, page.name
        assert ui < text.index("/ui/style-pure.css") and ui < text.index("/ui/style-mica.css"), page.name
        assert 'type="module" src="/ui/appearance.js"' not in text  # must block, not defer


def test_style_sheets_are_scoped():
    """Every rule in the skins applies only under <html data-style=...>, so the two never mix."""
    import re
    for name in ("pure", "mica"):
        css = re.sub(r"/\*.*?\*/", "", (UI_DIR / f"style-{name}.css").read_text(encoding="utf-8"), flags=re.S)
        for prelude in re.findall(r"([^{}]+)\{", css):
            prelude = prelude.strip()
            if prelude.startswith("@"):
                continue
            for selector in prelude.split(","):
                assert f'[data-style="{name}"]' in selector, (name, selector)


AGENT_CHAT = ("chat.js", "prompts.js", "agent-admin.js", "agent-wire.js")


@pytest.mark.parametrize("which", ["gateway", "local"])
def test_agent_chat_modules_are_served(which, monkeypatch, tmp_path):
    """The Overview chat and the Agent page (docs/agent-chat.md §11) load from /ui/ in both consoles."""
    client = gateway_client(monkeypatch, tmp_path) if which == "gateway" else local_client(tmp_path)
    with client:
        for name in AGENT_CHAT:
            response = client.get(f"/ui/{name}")
            assert response.status_code == 200, name
            assert response.headers["content-type"].startswith("text/javascript"), name


def test_agent_chat_imports_resolve_and_both_consoles_wire_it():
    import re
    static = UI_DIR.parent
    for name in AGENT_CHAT:
        text = (UI_DIR / name).read_text(encoding="utf-8")
        for target in re.findall(r'from "(\.\/[^"]+|/ui/[^"]+)"', text):
            assert (UI_DIR / target.rsplit("/", 1)[-1]).is_file(), (name, target)
    local_overview = (static / "local" / "overview.js").read_text(encoding="utf-8")
    console_pages = (static / "console" / "pages.js").read_text(encoding="utf-8")
    assert "mountChat(" in local_overview and "localAgentApi()" in local_overview
    assert "mountChat(" in console_pages and "cloudAgentApi()" in console_pages and "localDevice: false" in console_pages
    for app in (static / "local" / "app.js", static / "console" / "app.js"):
        text = app.read_text(encoding="utf-8")
        assert '"/agent"' in text and "agentPage(" in text, app
    wire = (UI_DIR / "agent-wire.js").read_text(encoding="utf-8")
    assert "/api/agents/local/" in wire and "/api/agents/remote/" in wire and "/agents/${enc(op)}" in wire
    assert "/api/inbox/conversation" in wire
