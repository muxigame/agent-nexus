"""Run the real Ubuntu application lifecycle with deterministic GTK callbacks."""
import importlib.util
import os
import sys
import types
from pathlib import Path

import unittest
from unittest.mock import patch
from contextlib import ExitStack
from tempfile import TemporaryDirectory


def build_desktop(monkeypatch, tmp_path):
    queue, workers, timers, views, contexts, indicators = [], [], {}, [], [], []
    idle_id = [0]

    def idle_add(callback, *args):
        idle_id[0] += 1
        queue.append((callback, args))
        return idle_id[0]

    def drain():
        while queue:
            callback, args = queue.pop(0)
            assert not callback(*args), "one-shot idle callback must not repeat"

    def timeout_add_seconds(seconds, callback):
        idle_id[0] += 1
        timers[idle_id[0]] = callback
        return idle_id[0]

    class Thread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            workers.append(self.target)

    class Application:
        def __init__(self, **kwargs):
            self.holds = 0
            self.quits = 0

        def hold(self):
            self.holds += 1

        def quit(self):
            self.quits += 1

    class Window:
        def __init__(self, **kwargs):
            self.handlers = {}
            self.visible = False
            self.mapped = False
            self.destroyed = False
            self.size = (1100, 760)
            self.child = None
            self.maximize_calls = 0

        def connect(self, signal, callback):
            self.handlers[signal] = callback

        def emit(self, signal, *args):
            if signal in self.handlers:
                return self.handlers[signal](self, *args)

        def set_default_size(self, *size):
            self.size = size

        def get_size(self):
            assert not self.destroyed
            return self.size

        def add(self, child):
            self.child = child

        def get_visible(self):
            return self.visible

        def get_mapped(self):
            return self.mapped

        def show_all(self):
            assert not self.destroyed
            self.visible = self.mapped = True
            self.emit("map-event", None)

        def deiconify(self):
            self.emit("window-state-event", types.SimpleNamespace(new_window_state=0))

        def present(self):
            assert not self.destroyed

        def maximize(self):
            self.maximize_calls += 1
            self.emit("window-state-event", types.SimpleNamespace(new_window_state=2))

        def destroy(self):
            self.destroyed = True
            self.visible = self.mapped = False
            if self.child is not None:
                self.child.destroyed = True
            self.emit("destroy")

    class WebContext:
        def __init__(self, manager):
            self.manager = manager
            self.views = []
            contexts.append(self)

        def get_website_data_manager(self):
            return self.manager

        @classmethod
        def get_default(cls):
            return default_context

        @classmethod
        def new_with_website_data_manager(cls, manager):
            return cls(manager)

    default_context = WebContext(object())

    class WebView:
        @classmethod
        def new_with_context(cls, context):
            view = cls()
            view.context = context
            context.views.append(view)
            views.append(view)
            return view

        def __init__(self):
            self.handlers = {}
            self.destroyed = False
            self.terminated = 0
            self.stopped = 0
            self.scripts = []
            self.uris = []
            self.html = []

        def connect(self, signal, callback):
            self.handlers[signal] = callback

        def run_javascript(self, script, *_):
            assert not self.destroyed, "callback touched an already destroyed view"
            self.scripts.append(script)

        def load_html(self, *args):
            assert not self.destroyed
            self.html.append(args)

        def load_uri(self, uri):
            assert not self.destroyed
            self.uris.append(uri)

        def stop_loading(self):
            assert not self.destroyed
            self.stopped += 1

        def terminate_web_process(self):
            assert not self.destroyed
            for view in self.context.views:
                view.terminated += 1

    class Menu:
        def __init__(self):
            self.entries = []

        def append(self, entry):
            self.entries.append(entry)

        def show_all(self):
            pass

    class MenuItem:
        def __init__(self, label):
            self.label = label

        def connect(self, *args):
            pass

        def set_submenu(self, menu):
            pass

    class Indicator:
        @classmethod
        def new(cls, *args):
            result = cls()
            indicators.append(result)
            return result

        def set_status(self, status):
            pass

        def set_menu(self, menu):
            self.menu = menu

    gi = types.ModuleType("gi")
    gi.require_version = lambda *_: None
    repository = types.ModuleType("gi.repository")
    repository.Gtk = types.SimpleNamespace(Application=Application, ApplicationWindow=Window,
                                          Menu=Menu, MenuItem=MenuItem)
    repository.Gdk = types.SimpleNamespace(WindowState=types.SimpleNamespace(ICONIFIED=1, MAXIMIZED=2))
    repository.Gio = types.SimpleNamespace(ApplicationFlags=types.SimpleNamespace(FLAGS_NONE=0))
    repository.GLib = types.SimpleNamespace(idle_add=idle_add, timeout_add_seconds=timeout_add_seconds,
                                           source_remove=lambda source: timers.pop(source, None))
    repository.WebKit2 = types.SimpleNamespace(WebContext=WebContext, WebView=WebView,
                                              LoadEvent=types.SimpleNamespace(FINISHED=3))
    repository.AyatanaAppIndicator3 = types.SimpleNamespace(
        Indicator=Indicator, IndicatorCategory=types.SimpleNamespace(APPLICATION_STATUS=1),
        IndicatorStatus=types.SimpleNamespace(ACTIVE=1))
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    source = Path(os.environ.get("MCPM_UBUNTU_SOURCE", str(
        Path(__file__).resolve().parents[1] / "packaging/ubuntu/ubuntu_launcher.py")))
    spec = importlib.util.spec_from_file_location("ubuntu_lifecycle_under_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.threading, "Thread", Thread)
    monkeypatch.setattr(module, "HERE", tmp_path)
    (tmp_path / "startup.html").write_text("<html>startup</html>", encoding="utf-8")
    monkeypatch.setattr(module, "base_url", lambda: "http://127.0.0.1:8399")
    monkeypatch.setattr(module, "prepare", lambda: None)
    calls = []

    def api(path, method="GET"):
        calls.append((path, method))
        if path == "/api/ping":
            return {"mcpm": True, "root": str(module.ROOT)}
        if path == "/api/services":
            return {"services": []}
        return {}

    monkeypatch.setattr(module, "api", api)
    monkeypatch.setattr(module, "install_unit", lambda: (_ for _ in ()).throw(AssertionError("must not start an existing manager")))
    app = module.create_desktop_application()
    return types.SimpleNamespace(app=app, module=module, workers=workers, queue=queue, drain=drain,
                                 timers=timers, views=views, contexts=contexts, indicators=indicators,
                                 default_context=default_context, calls=calls, webview=WebView)


def finish_start(desktop):
    desktop.workers.pop(0)()
    desktop.drain()
    assert desktop.app.connected



class MonkeyPatch:
    def __init__(self):
        self.stack = ExitStack()

    def setitem(self, mapping, key, value):
        self.stack.enter_context(patch.dict(mapping, {key: value}))

    def setattr(self, target, name, value):
        self.stack.enter_context(patch.object(target, name, value))

    def undo(self):
        self.stack.close()


class UbuntuLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.monkeypatch = MonkeyPatch()
        self.addCleanup(self.monkeypatch.undo)
        self.temporary = TemporaryDirectory(prefix="mcpm-ubuntu-lifecycle-")
        self.addCleanup(self.temporary.cleanup)
        self.desktop = build_desktop(self.monkeypatch, Path(self.temporary.name))

    def test_close_and_reopen_releases_view_without_restarting_manager(self):
        desktop, monkeypatch = self.desktop, self.monkeypatch
        app = desktop.app
        app.do_activate()
        finish_start(desktop)
        for _ in range(20):
            window, view, context = app.window, app.view, app.web_context
            assert app.close_window(window) is True
            assert window.destroyed and view.destroyed and view.terminated == 1
            assert view.stopped == 1
            assert app.window is app.view is app.web_context is None
            assert app.quits == 0 and not app.closing
            app.reopen()
            assert app.web_context is not context
            assert app.view.uris == ["http://127.0.0.1:8399"]
            assert app.web_context.manager is desktop.default_context.manager
        assert app.holds == 1 and len(desktop.indicators) == 1
        assert len(desktop.timers) == 1 and not desktop.workers
        assert desktop.calls == [("/api/ping", "GET")]

    def test_startup_callbacks_after_close_do_not_touch_dead_view_or_reopen(self):
        desktop, monkeypatch = self.desktop, self.monkeypatch
        app = desktop.app
        app.do_activate()
        window, view = app.window, app.view
        app.progress("queued before close")
        app.close_window(window)
        finish_start(desktop)
        assert app.window is None and view.destroyed
        assert len(desktop.timers) == 1
        app.reopen()
        assert app.view is not view and app.view.uris
        assert not desktop.workers

    def test_queued_progress_uses_current_view_and_old_load_callback_is_ignored(self):
        desktop, monkeypatch = self.desktop, self.monkeypatch
        app = desktop.app
        app.do_activate()
        old = app.view
        app.progress("latest startup state")
        app.close_window(app.window)
        app.reopen()
        desktop.drain()
        assert any("latest startup state" in script for script in app.view.scripts)
        before = list(app.view.scripts)
        app.page_loaded(old, 3)
        assert app.view.scripts == before
        app.page_loaded(app.view, 3)
        assert "latest startup state" in app.view.scripts[-1]

    def test_repeated_activate_coalesces_startup_and_ready_timers(self):
        desktop, monkeypatch = self.desktop, self.monkeypatch
        app = desktop.app
        for _ in range(25):
            app.do_activate()
        assert len(desktop.workers) == 1
        assert len(desktop.views) == 1 and len(desktop.indicators) == 1
        finish_start(desktop)
        for _ in range(10):
            app.ready()
            app.reopen()
        assert len(desktop.timers) == 1 and not desktop.workers
        assert app.view.uris == ["http://127.0.0.1:8399"]

    def test_minimize_unmap_and_page_load_deliver_native_visibility(self):
        desktop, monkeypatch = self.desktop, self.monkeypatch
        app = desktop.app
        app.do_activate()
        view, window = app.view, app.window
        app.window_state(window, types.SimpleNamespace(new_window_state=1))
        assert "__mcpmNativeVisible=false" in view.scripts[-1]
        app.page_loaded(view, 3)
        assert "__mcpmNativeVisible=false" in view.scripts[-1]
        app.window_state(window, types.SimpleNamespace(new_window_state=0))
        assert "__mcpmNativeVisible=true" in view.scripts[-1]
        window.mapped = False
        app.visibility_changed(window)
        assert "__mcpmNativeVisible=false" in view.scripts[-1]
        window.mapped = True
        app.visibility_changed(window)
        assert "mcpmvisibilitychange" in view.scripts[-1]
        assert "__mcpmNativeVisible=true" in view.scripts[-1]

    def test_refresh_has_one_pending_worker_and_recovers_from_error(self):
        desktop, monkeypatch = self.desktop, self.monkeypatch
        app = desktop.app
        app.do_activate()
        finish_start(desktop)
        for _ in range(5):
            assert app.refresh() is True
        assert len(desktop.workers) == 1
        desktop.workers.pop(0)()
        desktop.drain()
        assert not app.refresh_pending
        monkeypatch.setattr(desktop.module, "api", lambda *_: (_ for _ in ()).throw(TimeoutError()))
        app.refresh()
        desktop.workers.pop(0)()
        desktop.drain()
        assert not app.refresh_pending
        assert app.refresh() is True and len(desktop.workers) == 1

    def test_stop_removes_timer_destroys_view_and_ignores_late_callbacks(self):
        desktop, monkeypatch = self.desktop, self.monkeypatch
        app = desktop.app
        app.do_activate()
        finish_start(desktop)
        subprocess_calls = []
        monkeypatch.setattr(desktop.module.subprocess, "run", lambda *args, **kwargs: subprocess_calls.append(args))
        app.progress("late")
        app.refresh()
        app.stop()
        app.stop()
        app.reopen()
        assert app.window is None and not desktop.timers
        assert len(desktop.workers) == 2  # one refresh and one explicit shutdown
        while desktop.workers:
            desktop.workers.pop(0)()
        desktop.drain()
        assert app.quits == 1 and len(subprocess_calls) == 1
        assert not app.refresh_pending and not desktop.timers
        assert app.ready() is False and app.window is None

    def test_saved_size_maximize_and_stale_window_events(self):
        desktop, monkeypatch = self.desktop, self.monkeypatch
        app = desktop.app
        app.do_activate()
        finish_start(desktop)
        old = app.window
        old.size = (900, 700)
        app.remember_size(old)
        app.window_state(old, types.SimpleNamespace(new_window_state=2))
        old.size = (1920, 1080)
        app.close_window(old)
        app.reopen()
        assert app.window.size == (900, 700)
        assert app.window.maximize_calls == 1
        new = app.window
        app.close_window(old)
        app.window_destroyed(old)
        app.window_state(old, types.SimpleNamespace(new_window_state=1))
        assert app.window is new and not new.destroyed and not app.minimized

    def test_failed_web_process_termination_still_destroys_window(self):
        desktop, monkeypatch = self.desktop, self.monkeypatch
        app = desktop.app
        app.do_activate()
        window, view = app.window, app.view
        def fail():
            raise RuntimeError("synthetic native failure")
        monkeypatch.setattr(view, "terminate_web_process", fail)
        with self.assertLogs(desktop.module.LOG, level="WARNING"):
            assert app.close_window(window) is True
        assert window.destroyed and app.window is app.view is app.web_context is None

    def test_native_cleanup_failures_do_not_block_explicit_stop(self):
        desktop, monkeypatch = self.desktop, self.monkeypatch
        app = desktop.app
        app.do_activate()
        finish_start(desktop)
        attempts, subprocess_calls = [], []

        def failure(operation):
            def run():
                attempts.append(operation)
                raise RuntimeError("synthetic native failure")
            return run

        monkeypatch.setattr(app.window, "get_size", failure("get_size"))
        monkeypatch.setattr(app.view, "stop_loading", failure("stop_loading"))
        monkeypatch.setattr(app.view, "terminate_web_process", failure("terminate_web_process"))
        monkeypatch.setattr(app.window, "destroy", failure("destroy"))
        monkeypatch.setattr(desktop.module.subprocess, "run", lambda *args, **kwargs: subprocess_calls.append(args))
        with self.assertLogs(desktop.module.LOG, level="WARNING") as captured:
            app.stop()
        app.stop()
        assert attempts == ["get_size", "stop_loading", "terminate_web_process", "destroy"]
        assert len(captured.records) == 4
        assert app.window is app.view is app.web_context is None
        assert not desktop.timers and len(desktop.workers) == 1
        desktop.workers.pop(0)()
        desktop.drain()
        assert app.quits == 1 and len(subprocess_calls) == 1
        assert ("/api/manager/shutdown", "POST") in desktop.calls

    def test_console_process_is_separate_from_default_context_views(self):
        desktop, monkeypatch = self.desktop, self.monkeypatch
        other = desktop.webview.new_with_context(desktop.default_context)
        app = desktop.app
        app.do_activate()
        own = app.view
        assert own.context is not other.context
        app.close_window(app.window)
        assert own.terminated == 1 and other.terminated == 0 and not other.destroyed

    def test_external_destroy_only_drops_references(self):
        desktop, monkeypatch = self.desktop, self.monkeypatch
        app = desktop.app
        app.do_activate()
        window, view = app.window, app.view
        window.destroy()
        assert app.window is app.view is app.web_context is None
        assert view.destroyed and view.terminated == 0


if __name__ == "__main__":
    unittest.main()
