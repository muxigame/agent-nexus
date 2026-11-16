"""Ubuntu desktop shell uses distro GTK; managed Python lives in its own uv environment."""
import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "runtime"
CONFIG_NAMES = ("agent-nexus.json", "mcp-manager.json")
UNIT = "agent-nexus.service"
APPLICATION_ID = "io.github.muxigame.agentnexus.ubuntu"
INDICATOR_ID = "agent-nexus"


def _resolve_root():
    """ANX_ROOT, else the legacy MCPM_ROOT, else $XDG_DATA_HOME/agent-nexus.

    The legacy ``roc-mcp-manager/client`` root is never written: it stays the
    rollback target, and copying it over is the job of ``anx migrate``.
    """
    explicit = os.environ.get("ANX_ROOT") or os.environ.get("MCPM_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    return (data / "agent-nexus").expanduser().resolve()


def _has_config(directory):
    return any((directory / name).is_file() for name in CONFIG_NAMES)


def config_path():
    for name in CONFIG_NAMES:
        if (ROOT / name).is_file():
            return ROOT / name
    return ROOT / CONFIG_NAMES[0]


ROOT = _resolve_root()
STARTUP_LOG = ROOT / ".anx/logs/startup.log"
LOG = logging.getLogger(__name__)


def prepare():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    version = hashlib.sha256((SOURCE / "SHA256.json").read_bytes()).hexdigest()
    if (ROOT / ".installed-version").exists() and (ROOT / ".installed-version").read_text() == version and (ROOT / "tools/uv").is_file() and (ROOT / "agent_nexus/cli.py").is_file():
        return
    for source in SOURCE.rglob("*"):
        relative = source.relative_to(SOURCE)
        target = ROOT / relative
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif relative.as_posix() not in CONFIG_NAMES or not _has_config(ROOT):
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file() and target.read_bytes() == source.read_bytes():
                continue  # uv may already be executing.
            temporary = target.with_name(target.name + ".bundle.tmp")
            shutil.copyfile(source, temporary)
            temporary.replace(target)
            if relative.parts[0] == ".private":
                target.parent.chmod(0o700)
                target.chmod(0o600)
    (ROOT / "tools/uv").chmod(0o755)
    (ROOT / ".installed-version").write_text(version)


def base_url():
    config = json.loads(config_path().read_text(encoding="utf-8-sig"))
    port = config.get("manager", {}).get("port", 8399)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("管理器端口必须在 1–65535 之间")
    return "http://127.0.0.1:%s" % port


def api(path, method="GET"):
    request = urllib.request.Request(base_url() + path, data=b"" if method == "POST" else None,
                                     headers={"X-ANX": "1", "X-MCPM": "1"}, method=method)
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=2) as response:
        return json.load(response)


def serve():
    prepare()
    os.environ["ANX_ROOT"] = str(ROOT)
    os.environ.pop("MCPM_ROOT", None)
    os.environ["PATH"] = str(ROOT / "tools") + os.pathsep + os.environ.get("PATH", "")
    os.environ["PYTHONUTF8"] = "1"
    os.chdir(ROOT)
    if "1" in (os.environ.get("ANX_LOG_STDIO"), os.environ.get("MCPM_LOG_STDIO")):
        log = STARTUP_LOG
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab", buffering=0) as output:
            os.dup2(output.fileno(), 1)
            os.dup2(output.fileno(), 2)
    uv = str(ROOT / "tools/uv")
    os.execv(uv, [uv, "run", "--no-dev", "--python", "3.12", "--project", str(ROOT),
                     "anx", "--root", str(ROOT), "serve"])


def install_unit():
    directory = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd/user"
    directory.mkdir(parents=True, exist_ok=True)
    log = STARTUP_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    unit = """[Unit]
Description=Agent Nexus manager and owned MCP plugins
After=network-online.target
[Service]
Type=simple
ExecStart=/usr/bin/python3 %s --serve
Environment=%s
Environment=ANX_LOG_STDIO=1
StandardOutput=journal
StandardError=journal
KillMode=mixed
TimeoutStopSec=30
Restart=no
[Install]
WantedBy=default.target
""" % (json.dumps(str(HERE / "ubuntu_launcher.py")), json.dumps("ANX_ROOT=" + str(ROOT)))
    (directory / UNIT).write_text(unit)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True)
    subprocess.run(["systemctl", "--user", "start", UNIT], check=True, capture_output=True)


def create_desktop_application():
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import Gtk, Gdk, Gio, GLib, WebKit2, AyatanaAppIndicator3

    class Application(Gtk.Application):
        def __init__(self):
            super().__init__(application_id=APPLICATION_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
            self.window = None
            self.view = None
            self.web_context = None
            self.indicator = None
            self.services = []
            self.connected = False
            self.closing = False
            self.starting = False
            self.startup_payload = None
            self.refresh_source = None
            self.refresh_pending = False
            self.window_size = (1100, 760)
            self.minimized = False
            self.maximized = False
            self.start_lock = threading.Lock()

        def do_activate(self):
            if self.closing:
                return
            if self.indicator is None:
                # One hold and one tray per application, independent of windows.
                self.hold()
                self.indicator = AyatanaAppIndicator3.Indicator.new(INDICATOR_ID, INDICATOR_ID, AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS)
                self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
                self.update_menu()
            self.open_window()
            self.start_bootstrap()

        def open_window(self):
            if self.window is not None:
                self.window.show_all()
                self.window.deiconify()
                self.window.present()
                return
            restore_maximized = self.maximized
            self.window = Gtk.ApplicationWindow(application=self, title="MCP 管理器")
            self.window.set_default_size(*self.window_size)
            self.window.connect("delete-event", self.close_window)
            self.window.connect("destroy", self.window_destroyed)
            self.window.connect("window-state-event", self.window_state)
            self.window.connect("configure-event", self.remember_size)
            self.window.connect("map-event", self.visibility_changed)
            self.window.connect("unmap-event", self.visibility_changed)
            # This context has exactly one view: terminating its web process
            # cannot affect another window. Preserve the existing website data.
            manager = WebKit2.WebContext.get_default().get_website_data_manager()
            self.web_context = WebKit2.WebContext.new_with_website_data_manager(manager)
            self.view = WebKit2.WebView.new_with_context(self.web_context)
            self.view.connect("load-changed", self.page_loaded)
            self.window.add(self.view)
            if self.connected:
                self.view.load_uri(base_url())
            else:
                self.view.load_html((HERE / "startup.html").read_text(encoding="utf-8"), "file:///usr/lib/agent-nexus/")
            self.minimized = False
            self.window.show_all()
            if restore_maximized:
                self.window.maximize()
            self.window.present()

        def remember_size(self, window, *_):
            if window is self.window and not self.minimized and not self.maximized:
                size = window.get_size()
                if size[0] > 0 and size[1] > 0:
                    self.window_size = tuple(size)
            return False

        def window_state(self, window, event):
            if window is self.window:
                self.minimized = bool(event.new_window_state & Gdk.WindowState.ICONIFIED)
                self.maximized = bool(event.new_window_state & Gdk.WindowState.MAXIMIZED)
                self.sync_visibility()
            return False

        def visibility_changed(self, window, *_):
            if window is self.window:
                self.sync_visibility()
            return False

        def sync_visibility(self):
            if self.view is not None and self.window is not None:
                visible = self.window.get_visible() and self.window.get_mapped() and not self.minimized
                self.view.run_javascript(
                    "window.__mcpmNativeVisible=%s;window.dispatchEvent(new Event('mcpmvisibilitychange'));"
                    % ("true" if visible else "false"), None, None, None)

        def release_window(self, window):
            if window is not self.window:
                return
            try:
                self.remember_size(window)
            except Exception as exc:
                LOG.warning("Could not remember console window size: %s", type(exc).__name__)
            view = self.view
            # Clear references before callbacks from terminating/destroying it.
            self.window = self.view = self.web_context = None
            if view is not None:
                for operation in ("stop_loading", "terminate_web_process"):
                    try:
                        getattr(view, operation)()
                    except Exception as exc:
                        # A failed native cleanup must not prevent the remaining
                        # cleanup, especially the user's explicit app shutdown.
                        LOG.warning("Could not %s for the console: %s", operation, type(exc).__name__)

        def close_window(self, window, *_):
            if window is not self.window:
                return True
            try:
                self.release_window(window)
            finally:
                try:
                    window.destroy()
                except Exception as exc:
                    LOG.warning("Could not destroy console window: %s", type(exc).__name__)
            return True

        def window_destroyed(self, window):
            # External destruction already disposed the child widget; do not
            # call methods on it from the destroy notification.
            if window is self.window:
                self.window = self.view = self.web_context = None

        def page_loaded(self, view, event):
            if view is not self.view or self.closing or event != WebKit2.LoadEvent.FINISHED:
                return
            self.sync_visibility()
            if not self.connected and self.startup_payload is not None:
                self.apply_progress(self.startup_payload)

        def start_bootstrap(self):
            if self.closing or self.connected or self.starting:
                return
            self.starting = True
            def run():
                try:
                    self.bootstrap()
                finally:
                    GLib.idle_add(self.bootstrap_finished)
            threading.Thread(target=run, daemon=True).start()

        def bootstrap_finished(self):
            self.starting = False
            return False

        def progress(self, message, stage=0, logs="", error=False, elapsed=0):
            payload = json.dumps({"message": message, "stage": stage, "logs": logs, "error": error,
                                  "elapsed": elapsed, "root": str(ROOT)})
            # Queue application state, never a bound method of a disposable view.
            GLib.idle_add(self.apply_progress, payload)

        def apply_progress(self, payload):
            if not self.closing and not self.connected:
                self.startup_payload = payload
                if self.view is not None:
                    self.view.run_javascript("window.updateStartup && window.updateStartup(%s);" % payload, None, None, None)
            return False

        def bootstrap(self):
            if not self.start_lock.acquire(blocking=False):
                return
            started = time.monotonic()
            try:
                self.progress("正在准备本机程序…", 1)
                prepare()
                info = None
                try:
                    info = api("/api/ping")
                except Exception:
                    pass
                if info and info.get("mcpm"):
                    if Path(info["root"]).resolve() != ROOT:
                        raise RuntimeError("管理器端口被另一个实例占用，请修改本实例的端口。")
                    GLib.idle_add(self.ready)
                    return
                if self.closing:
                    return
                install_unit()
                while not self.closing:
                    try:
                        info = api("/api/ping")
                        if (info.get("anx") or info.get("mcpm")) and Path(info["root"]).resolve() == ROOT:
                            GLib.idle_add(self.ready)
                            return
                    except Exception:
                        pass
                    log = STARTUP_LOG
                    logs = "\n".join(log.read_text(errors="replace").splitlines()[-45:]) if log.exists() else ""
                    state = subprocess.run(["systemctl", "--user", "is-active", UNIT], capture_output=True, text=True).stdout.strip()
                    if state in ("failed", "inactive"):
                        raise RuntimeError("启动进程已退出。\n" + logs)
                    self.progress("正在准备 Python、安装依赖和启动服务…", 3, logs, elapsed=int(time.monotonic() - started))
                    time.sleep(1)
            except Exception as exc:
                self.progress("启动失败", logs=str(exc), error=True, elapsed=int(time.monotonic() - started))
            finally:
                self.start_lock.release()

        def ready(self):
            if self.closing or self.connected:
                return False
            self.connected = True
            self.startup_payload = None
            if self.view is not None:
                self.view.load_uri(base_url())
            if self.refresh_source is None:
                self.refresh_source = GLib.timeout_add_seconds(3, self.refresh)
            return False

        def refresh(self):
            if self.closing:
                return False
            if self.refresh_pending:
                return True
            self.refresh_pending = True
            def fetch():
                services = None
                try:
                    services = api("/api/services")["services"]
                except Exception:
                    pass
                finally:
                    GLib.idle_add(self.refresh_finished, services)
            threading.Thread(target=fetch, daemon=True).start()
            return True

        def refresh_finished(self, services):
            self.refresh_pending = False
            if not self.closing and services is not None:
                self.services = services
                self.update_menu()
            return False

        def update_menu(self):
            if self.closing or self.indicator is None:
                return False
            menu = Gtk.Menu()
            def item(label, callback):
                entry = Gtk.MenuItem(label=label)
                entry.connect("activate", lambda *_: callback())
                menu.append(entry)
                return entry
            item("打开控制台 / 重试启动", self.reopen)
            for service in self.services:
                entry = item(service["name"] + " · " + service["status"], self.reopen)
                submenu = Gtk.Menu()
                if service["kind"] != "observe" and service["supported"]:
                    for action, label in (("start", "启动"), ("restart", "重启"), ("stop", "停止")):
                        action_item = Gtk.MenuItem(label=label)
                        action_item.connect("activate", lambda _, name=service["name"], action=action: self.service_action(name, action))
                        submenu.append(action_item)
                    entry.set_submenu(submenu)
            item("退出管理器并停止所有服务", self.stop)
            menu.show_all()
            self.indicator.set_menu(menu)
            return False

        def reopen(self):
            self.do_activate()

        def service_action(self, name, action):
            def run():
                try:
                    api("/api/services/" + name + "/" + action, "POST")
                except Exception as exc:
                    GLib.idle_add(self.show_error, str(exc))
            threading.Thread(target=run, daemon=True).start()

        def show_error(self, message):
            if self.closing:
                return False
            dialog = Gtk.MessageDialog(transient_for=self.window, message_type=Gtk.MessageType.ERROR,
                                       buttons=Gtk.ButtonsType.CLOSE, text=message)
            dialog.run()
            dialog.destroy()
            return False

        def stop(self):
            if self.closing:
                return
            self.closing = True
            if self.refresh_source is not None:
                GLib.source_remove(self.refresh_source)
                self.refresh_source = None
            if self.window is not None:
                self.close_window(self.window)
            def run():
                with self.start_lock:
                    try:
                        info = api("/api/ping")
                        if Path(info["root"]).resolve() == ROOT.resolve():
                            api("/api/manager/shutdown", "POST")
                    except Exception:
                        pass
                    subprocess.run(["systemctl", "--user", "stop", UNIT], capture_output=True)
                GLib.idle_add(self.quit)
            threading.Thread(target=run, daemon=True).start()

    return Application()


def desktop():
    return create_desktop_application().run([sys.argv[0]])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    if args.serve:
        serve()
    elif args.prepare:
        prepare()
    else:
        sys.exit(desktop())
