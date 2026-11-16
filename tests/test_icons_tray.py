import json
import sys

import pytest
from PIL import Image
from starlette.testclient import TestClient

from agent_nexus.registry import ICON_MAX_BYTES, MANIFEST_NAME, discover, load_manager_config
from agent_nexus.supervisor import Supervisor
from agent_nexus.web import build_app

pystray = pytest.importorskip("pystray")
from agent_nexus.tray import ICON_PATH, Tray, badge, overall_tone, summary  # noqa: E402


def service(root, name, **manifest):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / MANIFEST_NAME).write_text(json.dumps({"command": ["x"], **manifest}), encoding="utf-8")
    return directory


def png(path, size=8):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (size, size), (255, 0, 0, 255)).save(path)
    return path


def snap(name, status, **extra):
    return {"name": name, "status": status, "enabled": True, "supported": True, "message": "", "pid": None, **extra}


# -- icons ------------------------------------------------------------------------

def test_icon_lookup_rules(tmp_path):
    png(service(tmp_path, "auto") / "favicon.png")
    png(service(tmp_path, "explicit", icon="assets/logo.png") / "assets" / "logo.png")
    service(tmp_path, "missing", icon="nope.png")
    (service(tmp_path, "big") / "favicon.png").write_bytes(b"\x89PNG" + b"0" * ICON_MAX_BYTES)
    png(tmp_path / "outside.png")
    service(tmp_path, "escape", icon="../outside.png")
    (service(tmp_path, "badtype", icon="favicon.gif") / "favicon.gif").write_bytes(b"GIF89a")
    service(tmp_path, "none")

    icons = {name: spec.icon_file() for name, spec in discover(load_manager_config(tmp_path)).items()}
    assert icons["auto"][0].name == "favicon.png"
    assert icons["explicit"][0].name == "logo.png"
    assert icons["none"] == (None, "")
    assert "not found" in icons["missing"][1]
    assert "limit is 10KB" in icons["big"][1]
    assert "inside the service directory" in icons["escape"][1]
    assert "unsupported icon type" in icons["badtype"][1]


def test_icon_endpoint_favicon_and_snapshot(tmp_path):
    png(service(tmp_path, "svc", port=9500) / "favicon.png")
    service(tmp_path, "bare", port=9501)
    client = TestClient(build_app(Supervisor(tmp_path)), base_url="http://127.0.0.1:8399")

    url = client.get("/api/services/svc").json()["icon_url"]
    assert url.startswith("/api/services/svc/icon?v=")
    resp = client.get(url)
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
    assert "sandbox" in resp.headers["content-security-policy"]

    assert client.get("/api/services/bare").json()["icon_url"] is None
    assert client.get("/api/services/bare/icon").status_code == 404
    assert client.get("/favicon.png").headers["content-type"] == "image/png"


def test_bundled_icons_stay_small():
    assert ICON_PATH.stat().st_size <= ICON_MAX_BYTES


# -- shutdown -----------------------------------------------------------------------

def test_shutdown_endpoint_needs_header_and_calls_hook(tmp_path):
    calls = []
    client = TestClient(build_app(Supervisor(tmp_path), request_exit=calls.append),
                        base_url="http://127.0.0.1:8399")
    assert client.post("/api/manager/shutdown").status_code == 403
    assert client.post("/api/manager/shutdown", headers={"X-MCPM": "1"}).status_code == 200
    # The reason is recorded so the desktop app knows not to restart it.
    assert calls == ["console API /api/manager/shutdown"]


# -- tray -----------------------------------------------------------------------------

def test_tray_rollup_tooltip_and_badge():
    assert overall_tone([]) is None
    assert overall_tone([snap("a", "running"), snap("b", "stopped")]) == "ok"
    assert overall_tone([snap("a", "running"), snap("b", "starting")]) == "warn"
    assert overall_tone([snap("a", "starting"), snap("b", "crashed")]) == "bad"

    text = summary([snap("a", "running"), snap("b", "crashed"), snap("c", "running", supported=False)])
    assert text.startswith("Agent Nexus · 1/2 运行中") and "异常: b" in text
    assert len(summary([snap("x" * 40, "crashed"), snap("y" * 40, "crashed")])) <= 63

    base = Image.open(ICON_PATH).convert("RGBA").resize((64, 64))
    assert badge(base, "bad").getpixel((49, 49))[:3] == (239, 68, 68)
    assert badge(base, None).tobytes() == base.tobytes()


@pytest.mark.skipif(sys.platform != "win32", reason="building a pystray icon needs a desktop backend")
def test_tray_menu_lists_services(tmp_path):
    png(service(tmp_path, "svc", port=9600) / "favicon.png")
    sup = Supervisor(tmp_path)
    sup.tick()
    tray = Tray(sup, "http://127.0.0.1:8399", lambda: None)

    items = list(tray.icon.menu.items)
    texts = [str(item.text) for item in items]
    assert texts[0] == "打开控制台"
    assert texts[-1] == "退出管理器并停止所有服务"
    entry = next(item for item in items if str(item.text).startswith("svc"))
    assert "已停止" in str(entry.text)
    assert [str(item.text) for item in entry.submenu.items] == ["查看状态和日志", "启动"]


def test_tray_tone_treats_observed_services_as_advisory():
    edge = lambda status: snap("edge", status, kind="observe")  # noqa: E731
    assert overall_tone([snap("a", "running"), edge("stopped")]) == "ok"  # browser closed is normal
    assert overall_tone([snap("a", "running"), edge("unhealthy")]) == "warn"  # never red
    assert overall_tone([snap("a", "crashed"), edge("running")]) == "bad"
    assert overall_tone([edge("running")]) == "ok"
    text = summary([snap("a", "running"), edge("stopped"), edge("unhealthy")])
    assert text.startswith("Agent Nexus · 1/1 运行中")


@pytest.mark.skipif(sys.platform != "win32", reason="building a pystray icon needs a desktop backend")
def test_tray_menu_offers_no_actions_for_observed_service(tmp_path, monkeypatch):
    from agent_nexus import observers

    directory = tmp_path / "edge"
    directory.mkdir()
    (directory / MANIFEST_NAME).write_text(json.dumps(
        {"kind": "observe", "observe": {"detector": "chromium-webmcp"}}), encoding="utf-8")
    monkeypatch.setitem(observers.DETECTORS, "chromium-webmcp",
                        lambda options: observers.Observation(observers.ABSENT, "Microsoft Edge 未运行"))
    sup = Supervisor(tmp_path)
    sup.tick()
    tray = Tray(sup, "http://127.0.0.1:8399", lambda: None)

    entry = next(item for item in tray.icon.menu.items if str(item.text).startswith("edge"))
    assert "未运行" in str(entry.text)
    assert [str(item.text) for item in entry.submenu.items] == [
        "查看状态和日志", "Microsoft Edge 未运行", "仅观察：管理器不会启动或停止它"]


# -- opening the console ----------------------------------------------------------------

def test_console_opens_the_window_when_built_and_a_browser_otherwise(tmp_path, monkeypatch):
    from agent_nexus import desktop

    opened: list = []
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: opened.append(("browser", url)))
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda argv, **kw: opened.append(("app", argv[0])))

    monkeypatch.setenv(desktop.ENV_OVERRIDE, str(tmp_path / "missing.exe"))
    assert desktop.desktop_app() is None
    assert desktop.open_console("http://127.0.0.1:8399") == "browser"

    exe = tmp_path / "mcpm-desktop.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setenv(desktop.ENV_OVERRIDE, str(exe))
    assert desktop.desktop_app() == exe
    assert desktop.open_console("http://127.0.0.1:8399") == "window"
    assert opened == [("browser", "http://127.0.0.1:8399"), ("app", str(exe))]


def test_the_window_is_told_which_page_to_show(tmp_path, monkeypatch):
    """The tray's per-service link must reach the right tab even when the
    window is already open."""
    from agent_nexus import desktop

    exe = tmp_path / "mcpm-desktop.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setenv(desktop.ENV_OVERRIDE, str(exe))
    launched: list = []
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda argv, **kw: launched.append(argv))
    desktop.open_console("http://127.0.0.1:8399/#tab=ones")
    assert launched == [[str(exe), "http://127.0.0.1:8399/#tab=ones"]]


def test_a_window_that_cannot_start_falls_back_to_the_browser(tmp_path, monkeypatch, capsys):
    from agent_nexus import desktop

    exe = tmp_path / "mcpm-desktop.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setenv(desktop.ENV_OVERRIDE, str(exe))
    opened: list = []
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: opened.append(url))

    def refuse(argv, **kw):
        raise OSError("no desktop session")

    monkeypatch.setattr(desktop.subprocess, "Popen", refuse)
    assert desktop.open_console("http://127.0.0.1:8399") == "browser"
    assert opened == ["http://127.0.0.1:8399"] and "could not start" in capsys.readouterr().err
