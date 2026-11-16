"""Exercise the installed app on the user's actual GTK desktop session."""
import importlib.util
import json
import socket
import time
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
gi.require_foreign("cairo")
from gi.repository import GLib, WebKit2

spec = importlib.util.spec_from_file_location("installed_shell", "/usr/lib/agent-nexus/ubuntu_launcher.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
app = module.create_desktop_application()
start = time.monotonic()
evidence = {}
errors = []
directory = Path.home() / "anx-desktop-smoke"
directory.mkdir(exist_ok=True)


def finished(webview, result, _):
    try:
        surface = webview.get_snapshot_finish(result)
        surface.write_to_png(str(directory / "console.png"))
        evidence["snapshot"] = True
    except Exception as exc:
        errors.append("snapshot: " + str(exc))
    app.window.close()  # Actual delete-event handler should hide, not stop the manager.
    GLib.timeout_add_seconds(2, hidden_check)


def hidden_check():
    try:
        evidence["hidden"] = not app.window.get_visible()
        before = module.api("/api/ping")
        evidence["manager_survived_close"] = bool(before.get("anx") or before.get("mcpm"))
        evidence["cloud"] = module.api("/api/cloud")["state"]
        evidence["services"] = {entry["name"]: entry["status"] for entry in module.api("/api/services")["services"]}
        assert evidence["hidden"] and evidence["manager_survived_close"]
        assert evidence["cloud"] == "connected" and evidence["services"]["workspace"] == "running"
        app.do_activate()
        evidence["reopened"] = app.window.get_visible()
        evidence["same_manager"] = module.api("/api/ping")["pid"] == before["pid"]
        assert evidence["same_manager"] and evidence["reopened"]
        menu = app.indicator.get_menu()
        exit_item = next(item for item in menu.get_children() if item.get_label() == "退出管理器并停止所有服务")
        evidence["tray_exit_clicked"] = True
        exit_item.activate()
    except Exception as exc:
        errors.append(str(exc))
        app.stop()
    return False


def check():
    if time.monotonic() - start > 240:
        errors.append("desktop startup timed out")
        app.stop()
        return False
    if app.window is None or not app.connected or not (app.view.get_uri() or "").startswith("http://127.0.0.1") or app.view.is_loading():
        return True
    try:
        if module.api("/api/cloud")["state"] != "connected":
            return True
        services = module.api("/api/services")["services"]
        if next(service["status"] for service in services if service["name"] == "workspace") != "running":
            return True
    except Exception:
        return True
    app.view.get_snapshot(WebKit2.SnapshotRegion.VISIBLE, WebKit2.SnapshotOptions.NONE, None, finished, None)
    return False


GLib.timeout_add_seconds(1, check)
app.run(["anx-desktop-smoke"])
time.sleep(1)
evidence["ports_after_exit"] = []
for port in (8399, 8400):
    with socket.socket() as connection:
        connection.settimeout(1)
        if connection.connect_ex(("127.0.0.1", port)) == 0:
            evidence["ports_after_exit"].append(port)
if evidence["ports_after_exit"]:
    errors.append("owned ports remained after tray quit")
evidence["errors"] = errors
(directory / "result.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
print(json.dumps(evidence, ensure_ascii=False))
raise SystemExit(1 if errors else 0)
