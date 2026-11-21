"""M12 (desktop): tray badges, update menu, update state and the shell verbs' API.

Design sections 9.2-9.6 and decision D7. No real tray is shown: the icon is
composed with Pillow and the menu is built from a pystray Icon that never runs.
"""
import json
import sys
import time

import pytest
from PIL import Image
from starlette.testclient import TestClient

from agent_nexus import __version__, desktop
from agent_nexus.supervisor import Supervisor
from agent_nexus.web import build_app

pystray = pytest.importorskip("pystray")
from agent_nexus import tray as tray_module  # noqa: E402
from agent_nexus.tray import (ICON_PATH, OFFLINE_GREY, TONES, UPDATE_ORANGE, Tray, arrow_box, compose,  # noqa: E402
                              dot_box, inbox_label, tray_icon, update_label)

SIZE = 64


@pytest.fixture
def base():
    return Image.open(ICON_PATH).convert("RGBA").resize((SIZE, SIZE), Image.Resampling.LANCZOS)


def write_update(root, **data):
    path = root / desktop.UPDATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def centre(box):
    return ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)


# -- icon composition ----------------------------------------------------------------

@pytest.mark.parametrize("connected", [True, False])
@pytest.mark.parametrize("update_available", [True, False])
def test_four_badge_combinations(base, connected, update_available):
    image = compose(base, connected, update_available)
    assert image.size == base.size and image is not base
    dot = image.getpixel(centre(dot_box(SIZE)))[:3]
    assert dot == (TONES["ok"] if connected else OFFLINE_GREY)
    corner = arrow_box(SIZE)
    top_left = image.crop(corner)
    if update_available:
        # Orange disc with a white arrow: the shaft at the centre, orange beside it.
        cx, cy = centre(corner)
        assert image.getpixel((cx, cy + 4))[:3] == (255, 255, 255)
        assert image.getpixel((corner[0] + 3, cy))[:3] == UPDATE_ORANGE
        assert top_left.tobytes() != base.crop(corner).tobytes()
    else:
        assert top_left.tobytes() == base.crop(corner).tobytes()


def test_badges_never_overlap(base):
    dot, arrow = dot_box(SIZE), arrow_box(SIZE)
    assert arrow[2] < dot[0] and arrow[3] < dot[1]
    for size in (16, 32, 64, 256):
        d, a = dot_box(size), arrow_box(size)
        assert a[2] < d[0] and a[3] < d[1], size
    # The update badge does not touch the dot's corner and vice versa.
    both = compose(base, True, True)
    assert both.crop(dot).tobytes() == compose(base, True, False).crop(dot).tobytes()
    assert both.crop(arrow).tobytes() == compose(base, False, True).crop(arrow).tobytes()


def test_broken_services_still_colour_the_dot(base):
    assert compose(base, True, False, "bad").getpixel(centre(dot_box(SIZE)))[:3] == TONES["bad"]
    assert compose(base, False, False, "warn").getpixel(centre(dot_box(SIZE)))[:3] == TONES["warn"]
    assert compose(base, False, False, "ok").getpixel(centre(dot_box(SIZE)))[:3] == OFFLINE_GREY


def test_composition_is_cached_per_combination(base):
    first = tray_icon(base, True, True)
    assert tray_icon(base, True, True) is first
    assert tray_icon(base, True, False) is not first
    assert tray_icon(base, True, True, "ok") is first  # a healthy tone does not change the image


# -- menu text and update state ------------------------------------------------------

def test_menu_labels():
    assert update_label({"available": True, "version": "0.2.1"}) == "更新到 v0.2.1"
    assert update_label({"available": True, "version": "v0.3.0"}) == "更新到 v0.3.0"
    assert update_label({"available": False, "version": None}) == "检查更新"
    assert update_label({}) == "检查更新"
    assert inbox_label(3) == "收件箱（3）"
    assert inbox_label(0) == inbox_label(None) == "收件箱"


def test_update_state_file(tmp_path):
    assert desktop.read_update(tmp_path)["available"] is False
    path = write_update(tmp_path, available=True, version="9.9.9", notes="n", current=__version__, checked_at=5)
    found = desktop.read_update(tmp_path)
    assert found["available"] is True and found["version"] == "9.9.9" and found["notes"] == "n"
    # A record naming the running version is stale (written before this update).
    write_update(tmp_path, available=True, version=__version__, checked_at=6)
    import os
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert desktop.read_update(tmp_path)["available"] is False


def test_update_mode_auto_notify_off(monkeypatch):
    from agent_nexus import auto_update
    monkeypatch.setattr(auto_update, "headless", lambda: False)
    assert desktop.update_mode({}) == ("notify", None)
    assert desktop.update_mode({"desktop": {"update": {"mode": "off"}}}) == ("off", None)
    assert desktop.update_mode({"desktop": {"update": {"mode": "auto"}}}) == ("auto", None)
    mode, warning = desktop.update_mode({"desktop": {"update": {"mode": "weird"}}})
    assert mode == "notify" and "weird" in warning
    monkeypatch.setattr(auto_update, "headless", lambda: True)  # no desktop: nobody to click
    assert desktop.update_mode({}) == ("auto", None)


# -- the tray, built but not run ---------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="building a pystray icon needs a desktop backend")
def test_tray_menu_follows_the_update_file(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tray_module, "run_verb", lambda *args: calls.append(args) or True)
    monkeypatch.setattr(tray_module, "autostart_enabled", lambda: True)
    sup = Supervisor(tmp_path)
    tray = Tray(sup, "http://127.0.0.1:1", lambda: None)

    def texts():
        return [str(item.text) for item in tray.icon.menu.items]

    assert "检查更新" in texts() and "开机自启" in texts()
    assert texts()[1] == "收件箱"
    autostart = next(item for item in tray.icon.menu.items if str(item.text) == "开机自启")
    assert autostart.checked is True

    # The shell found 9.9.9: the next refresh shows it, with the orange arrow.
    write_update(tmp_path, available=True, version="9.9.9", current=__version__, checked_at=int(time.time()))
    tray.refresh()
    assert "更新到 v9.9.9" in texts() and "检查更新" not in texts()
    corner = arrow_box(SIZE)
    assert tray.icon.icon.getpixel((corner[0] + 3, centre(corner)[1]))[:3] == UPDATE_ORANGE

    # Clicking installs only through the shell's --apply-update.
    next(item for item in tray.icon.menu.items if str(item.text) == "更新到 v9.9.9")(tray.icon)
    assert calls == [("--apply-update",)]

    # Gone again (installed, or the file replaced): back to "检查更新", which asks for a check.
    (tmp_path / desktop.UPDATE_FILE).unlink()
    tray.refresh()
    assert "检查更新" in texts()
    next(item for item in tray.icon.menu.items if str(item.text) == "检查更新")(tray.icon)
    assert calls[-1] == ("--check-update",)

    next(item for item in tray.icon.menu.items if str(item.text) == "开机自启")(tray.icon)
    assert calls[-1] == ("--set-autostart=off",)


@pytest.mark.skipif(sys.platform != "win32", reason="building a pystray icon needs a desktop backend")
def test_tray_dot_follows_the_gateway_link(tmp_path):
    sup = Supervisor(tmp_path)
    tray = Tray(sup, "http://127.0.0.1:1", lambda: None)
    dot = centre(dot_box(SIZE))
    assert tray.icon.icon.getpixel(dot)[:3] == OFFLINE_GREY
    sup.cloud_status = {"state": "connected"}
    tray._inbox_at = time.monotonic()  # no inbox request in this test
    tray.refresh()
    assert tray.icon.icon.getpixel(dot)[:3] == TONES["ok"]


# -- manager API -----------------------------------------------------------------------------

def test_app_update_api(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, "run_verb", lambda *args: calls.append(args) or True)
    (tmp_path / "agent-nexus.json").write_text(json.dumps({"desktop": {"update": {"mode": "auto"}}}), encoding="utf-8")
    client = TestClient(build_app(Supervisor(tmp_path)), base_url="http://127.0.0.1:8399")
    headers = {"X-ANX": "1"}

    status = client.get("/api/app/update", headers=headers).json()
    assert status["available"] is False and status["mode"] == "auto" and status["warning"] is None
    assert status["current"] == __version__
    # Installing needs a known update; checking is always possible.
    assert client.post("/api/app/update/apply", headers=headers).status_code == 409
    assert client.post("/api/app/update/check", headers=headers).json() == {"started": "check-update"}

    write_update(tmp_path, available=True, version="9.9.9", notes="fixes", checked_at=1)
    status = client.get("/api/app/update", headers=headers).json()
    assert status["available"] is True and status["version"] == "9.9.9"
    assert client.get("/api/services", headers=headers).json()["manager"]["app_update"]["version"] == "9.9.9"
    assert client.post("/api/app/update/apply", headers=headers).json() == {"started": "apply-update"}
    assert calls == [("--check-update",), ("--apply-update",)]

    assert client.put("/api/app/autostart", headers=headers, json={"enabled": "yes"}).status_code == 400


def test_app_update_api_without_the_shell(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "desktop_app", lambda: None)
    client = TestClient(build_app(Supervisor(tmp_path)), base_url="http://127.0.0.1:8399")
    response = client.post("/api/app/update/check", headers={"X-ANX": "1"})
    assert response.status_code == 409 and "desktop app" in response.json()["error"]
