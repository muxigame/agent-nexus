"""The shared design system is served under /ui/ by the gateway and by the local console."""
import pytest
from starlette.testclient import TestClient

from agent_nexus.cloud import build_cloud_app
from agent_nexus.supervisor import Supervisor
from agent_nexus.ui_assets import UI_DIR, ui_file
from agent_nexus.web import build_app

CORE = {"tokens.css": "text/css", "ui.css": "text/css", "icons.js": "text/javascript", "ui.js": "text/javascript",
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
