"""Verify the production console lifecycle in an isolated WebView profile.

Runs the console_lifecycle Rust example against a loopback-only fixture. It
never imports bootstrap, starts a manager, or stops an existing service.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import psutil

ROOT = Path(__file__).resolve().parents[1]


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def wait_until(check, description, timeout=20):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(.1)
    raise AssertionError(f"Timed out: {description}; last={last!r}")


def memory(pid):
    rows = []
    try:
        parent = psutil.Process(pid)
        processes = [parent, *parent.children(recursive=True)]
    except psutil.NoSuchProcess:
        processes = []
    for process in processes:
        try:
            info = process.memory_info()
            rows.append({"pid": process.pid, "name": process.name(),
                         "rss_bytes": info.rss, "private_bytes": getattr(info, "private", None)})
        except psutil.Error:
            pass
    webviews = [row for row in rows if row["name"].lower() == "msedgewebview2.exe"]
    return {"at": utc(), "processes": rows, "webview_count": len(webviews),
            "webview_rss_mib": round(sum(p["rss_bytes"] for p in webviews) / 1024**2, 2),
            "webview_private_mib": round(sum(p["private_bytes"] or 0 for p in webviews) / 1024**2, 2),
            "total_rss_mib": round(sum(p["rss_bytes"] for p in rows) / 1024**2, 2)}


def existing_services(known_pids=None):
    result = {}
    for process in psutil.process_iter(["pid", "name", "exe", "create_time"]):
        if known_pids is not None and process.pid not in known_pids:
            continue
        try:
            name = (process.info["name"] or "").lower()
            if name in {"agent-nexus.exe", "agent-nexus-private.exe", "mcpm-desktop-cloud.exe", "mcpm-desktop.exe", "python.exe", "uv.exe", "node.exe"}:
                result[str(process.pid)] = {"name": name, "started": process.create_time(),
                                            "exe": process.exe()}
        except psutil.Error:
            pass
    return result


class Fixture:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.polls = 0
        self.sse_opened = 0
        self.sse_active = 0
        self.reports = {}
        self.errors = []
        self.html = (ROOT / "agent_nexus/static/index.html").read_bytes()
        self.log_text = "\n".join(f"synthetic {i:05d} " + "x" * 160 for i in range(10000)) + "\n"

    def counts(self):
        with self.lock:
            return {"polls": self.polls, "sse_opened": self.sse_opened, "sse_active": self.sse_active}

    def report(self, tag):
        with self.lock:
            return self.reports.get(tag)

    def handler(self):
        fixture = self
        service = dict(name="fixture", description="Isolated lifecycle fixture", kind="managed",
                       status="running", enabled=True, autostart=False, restart_on_crash=False,
                       port=12345, pid=0, listener_pid=None, health_path="/healthz",
                       health={"at": 1, "code": 200, "ms": 1}, health_interval=5, uptime_s=1,
                       crashes=0, exit_code=None, pending_restart=False, dir="fixture",
                       cwd="fixture", command=["fixture"], env_keys=[], source="manifest",
                       platforms=["windows"], logs=[{"id": "stdout", "label": "fixture"}])

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def respond(self, status, body, kind="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/":
                    self.respond(200, fixture.html, "text/html; charset=utf-8")
                elif path == "/api/services":
                    with fixture.lock:
                        fixture.polls += 1
                    self.respond(200, json.dumps({"services": [service], "manager": {
                        "root": "isolated fixture", "platform": "windows", "pid": 0,
                        "anx": "test", "mcpm": "test", "cloud": {"state": "disabled"}}}).encode())
                elif path == "/api/services/fixture/logs/stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    with fixture.lock:
                        fixture.sse_opened += 1
                        fixture.sse_active += 1
                    try:
                        payload = json.dumps({"text": fixture.log_text, "reset": True,
                                              "path": "synthetic fixture; no real logs"})
                        self.wfile.write(("data: " + payload + "\n\n").encode())
                        self.wfile.flush()
                        while not fixture.stop.wait(.2):
                            self.wfile.write(b": heartbeat\n\n")
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                        pass
                    finally:
                        with fixture.lock:
                            fixture.sse_active -= 1
                else:
                    self.respond(404, b"{}")

            def do_POST(self):
                if self.path != "/__snapshot":
                    self.respond(405, b"{}")
                    return
                size = min(int(self.headers.get("Content-Length", "0")), 16384)
                data = json.loads(self.rfile.read(size))
                with fixture.lock:
                    fixture.reports[data["tag"]] = data
                self.respond(200, b"{}")
        return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--exe", type=Path, default=ROOT / "desktop/src-tauri/target/release/examples/console_lifecycle.exe")
    args = parser.parse_args()
    args.out = args.out.resolve()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    folder = args.out.parent / ("native-" + uuid.uuid4().hex[:10])
    folder.mkdir()
    control, status = folder / "control.json", folder / "status.json"
    profile = folder / "profile"
    fixture = Fixture()
    server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.handler())
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/#tab=fixture"
    known_pids = ({int(row["pid"]) for row in json.loads(args.baseline.read_text("utf-8"))["processes"]}
                  if args.baseline else None)
    before = existing_services(known_pids)
    result = {"started": utc(), "exe": str(args.exe.resolve()), "profile": str(profile),
              "fixture": url, "checks": [], "memory": {}, "snapshots": {}}
    proc = None
    command_id = 0

    def read_status():
        if proc is not None and proc.poll() is not None:
            raise AssertionError(f"native fixture exited early: {proc.returncode}; see {folder / 'native.log'}")
        try:
            data = json.loads(status.read_text("utf-8"))
            if data.get("error"):
                raise AssertionError(data["error"])
            return data
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def command(action, **extra):
        nonlocal command_id
        command_id += 1
        temporary = control.with_suffix(".tmp")
        temporary.write_text(json.dumps({"id": command_id, "action": action, **extra}), "utf-8")
        temporary.replace(control)
        if action != "exit":
            wait_until(lambda: read_status().get("last_command", 0) >= command_id,
                       f"native command {action}", 12)

    def snapshot(tag, script=""):
        js = script + "\nfetch('/__snapshot', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({" + \
             "tag:" + json.dumps(tag) + ", hidden:document.hidden, nativeVisible:window.__mcpmNativeVisible, active:state.active, " + \
             "follow:view?.follow, filter:view?.filter, lines:view?.lines.length, characters:view?.characters, " + \
             "domLines:view?.pre.childNodes.length, draft:document.querySelector('[data-editing=\"port\"]')?.value, " + \
             "details:document.querySelector('details.more')?.open, activeSse:!!view?.es," + \
             "first:view?.lines[0], last:view?.lines.at(-1)})});"
        command("eval", script=js)
        data = wait_until(lambda: fixture.report(tag), f"page snapshot {tag}", 12)
        result["snapshots"][tag] = data
        return data

    def visible():
        data = read_status()
        return data if data.get("windows") == 1 and (data.get("window") or {}).get("visible") else None

    def loaded(tag):
        for attempt in range(80):
            data = snapshot(f"{tag}-{attempt}")
            if data.get("lines") == 5000:
                return data
            time.sleep(.1)
        raise AssertionError("Fixture logs did not load")

    def check(name, condition=True):
        if not condition:
            raise AssertionError(name)
        result["checks"].append(name)
        print(json.dumps({"check": name, "at": utc()}, ensure_ascii=False), flush=True)

    try:
        with (folder / "native.log").open("wb") as output:
            proc = subprocess.Popen([str(args.exe.resolve()), url, str(profile), str(control), str(status)],
                                    cwd=str(ROOT), stdout=output, stderr=subprocess.STDOUT,
                                    env={**os.environ, "RUST_BACKTRACE": "1"})
        result["pid"] = proc.pid
        wait_until(visible, "first visible console", 25)
        first = loaded("initial")
        check("native page keeps only the newest 5000 of 10000 log lines",
              first["domLines"] == 5000 and "05000" in first["first"] and "09999" in first["last"])
        edited = snapshot("edited", """const draft=document.querySelector('[data-editing="port"]');
draft.value='9234'; draft.dispatchEvent(new Event('input',{bubbles:true}));
view.filterInput.value='synthetic'; view.filterInput.dispatchEvent(new Event('input',{bubbles:true}));
view.pause.click(); document.querySelector('details.more').open=true;""")
        time.sleep(.3)
        check("fixture preference and port draft edits applied", edited["draft"] == "9234" and not edited["follow"])
        result["memory"]["visible"] = memory(proc.pid)
        command("hide")
        wait_until(lambda: (read_status().get("window") or {}).get("visible") is False, "hidden console")
        time.sleep(1)
        hidden = snapshot("hidden")
        check("hidden native WebView pauses through the native visibility signal",
              hidden.get("nativeVisible") is False and not hidden["activeSse"])
        wait_until(lambda: fixture.counts()["sse_active"] == 0, "hidden stream disconnect", 5)
        idle_counts = fixture.counts()
        time.sleep(2.5)
        check("hidden console pauses service polling and log streaming", fixture.counts() == idle_counts)
        result["memory"]["hidden_with_paused_ui"] = memory(proc.pid)
        command("open", repeat=25)
        wait_until(visible, "show existing console")
        command("resize")
        wait_until(lambda: abs((read_status().get("window") or {}).get("width", 0)-920) < 2, "resized console")
        command("maximize")
        wait_until(lambda: (read_status().get("window") or {}).get("maximized"), "maximized console")
        command("close")
        wait_until(lambda: read_status().get("windows") == 0, "destroyed console")
        wait_until(lambda: memory(proc.pid)["webview_count"] == 0, "WebView2 subprocess shutdown", 20)
        check("closing the console releases every owned WebView2 process", proc.poll() is None)
        result["memory"]["closed_to_tray"] = memory(proc.pid)
        command("navigate-ready", url=url)
        time.sleep(.3)
        check("backend-ready update while closed does not reopen the console", read_status()["windows"] == 0)
        command("open", repeat=25)
        wait_until(visible, "recreated console", 25)
        restored = loaded("restored")
        check("repeated open requests create one console", read_status()["windows"] == 1)
        check("recreated console restores tab, draft, filter and pause",
              restored["active"] == "fixture" and restored["draft"] == "9234"
              and restored["filter"] == "synthetic" and restored["follow"] is False and restored["details"])
        check("recreated console restores maximized state", read_status()["window"]["maximized"])
        command("unmaximize")
        normal = wait_until(lambda: read_status() if not (read_status().get("window") or {}).get("maximized") else None,
                            "restored normal geometry")
        check("normal window size is retained", abs(normal["window"]["width"]-920) < 2 and abs(normal["window"]["height"]-640) < 2)
        command("minimize")
        wait_until(lambda: (read_status().get("window") or {}).get("minimized"), "minimized console")
        time.sleep(.5)
        minimized = snapshot("minimized")
        check("minimized console pauses updates", minimized.get("nativeVisible") is False and not minimized["activeSse"])
        command("open", repeat=25)
        wait_until(lambda: visible() if not (read_status().get("window") or {}).get("minimized") else None,
                   "restore from minimized")
        resumed = snapshot("resumed")
        check("opening from minimized resumes the page",
              resumed.get("nativeVisible") is True and not resumed["hidden"] and resumed["activeSse"])
        for cycle in range(2):
            command("close")
            wait_until(lambda: read_status().get("windows") == 0, "cycle destroy")
            wait_until(lambda: memory(proc.pid)["webview_count"] == 0, "cycle WebView2 release", 20)
            check(f"reopen cycle {cycle + 1} releases all WebView2 processes")
            command("open", repeat=25)
            wait_until(visible, "cycle reopen", 25)
            loaded(f"cycle-{cycle}")
        command("close")
        wait_until(lambda: read_status().get("windows") == 0, "final destroy")
        wait_until(lambda: memory(proc.pid)["webview_count"] == 0, "final browser release", 20)
        result["memory"]["final_closed"] = memory(proc.pid)
        result["native_final"] = read_status()
        check("no manager or service process is spawned by window recreation",
              all(p["name"].lower() in {"console_lifecycle.exe", "msedgewebview2.exe"}
                  for p in memory(proc.pid)["processes"]))
        command("exit")
        proc.wait(timeout=10)
        check("explicit application exit still works", proc.returncode == 0)
        result["passed"] = True
    except BaseException as error:
        result["passed"] = False
        result["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if proc and proc.poll() is None:
            try:
                command("exit")
                proc.wait(timeout=8)
            except Exception:
                try:
                    owned = psutil.Process(proc.pid)
                    for child in reversed(owned.children(recursive=True)):
                        try:
                            child.kill()
                        except psutil.Error:
                            pass
                    owned.kill()
                except psutil.Error:
                    pass
        fixture.stop.set()
        server.shutdown()
        server.server_close()
        after = existing_services(known_pids)
        changed = [pid for pid, item in before.items() if after.get(pid) != item]
        result["existing_processes_changed"] = changed
        result["existing_processes_checked"] = len(before)
        if changed:
            result["passed"] = False
        result["counts"] = fixture.counts()
        result["process_exit_code"] = proc.poll() if proc else None
        result["finished"] = utc()
        args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False), "utf-8")
        print(json.dumps({"report": str(args.out), "passed": result.get("passed"),
                          "existing_processes_changed": changed}, ensure_ascii=False), flush=True)
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

