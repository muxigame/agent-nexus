"""Command line for mcpm.

Every command talks to a running manager over its local API when there is
one, so the web console sees the change immediately. Without a manager the
lifecycle commands act directly; the processes they start are recorded in
``.mcp-manager/run`` and adopted by the next ``mcpm serve``.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .desktop import open_console
from .registry import CONFIG_NAME, ManagerConfig, RegistryError, discover, load_manager_config, save_override
from .supervisor import Supervisor, SupervisorError

_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def default_root() -> Path:
    if os.environ.get("MCPM_ROOT"):
        return Path(os.environ["MCPM_ROOT"]).resolve()
    cwd = Path.cwd().resolve()
    for directory in (cwd, *cwd.parents):
        if (directory / CONFIG_NAME).is_file():
            return directory
    return Path(__file__).resolve().parents[2]  # <root>/mcp-manager/mcpm/cli.py


class Api:
    def __init__(self, cfg: ManagerConfig):
        self.cfg = cfg

    def call(self, method: str, path: str, body: Any = None, timeout: float = 60) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.cfg.base_url + path, data=data, method=method,
                                     headers={"X-MCPM": "1", "Content-Type": "application/json"})
        try:
            with _DIRECT.open(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except ValueError:
                return exc.code, {"error": str(exc.reason)}

    def alive(self) -> bool:
        """A manager for *this* root answers on the configured port."""
        try:
            code, info = self.call("GET", "/api/ping", timeout=2)
        except (OSError, ValueError):
            return False
        return code == 200 and Path(info.get("root", "")).resolve() == self.cfg.root


def _table(rows: list[tuple[str, ...]]) -> None:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)) + "  " + row[-1])


def _uptime(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def cmd_serve(args: argparse.Namespace, cfg: ManagerConfig) -> int:
    if Api(cfg).alive():
        print(f"mcpm is already running at {cfg.base_url}")
        return 0
    import uvicorn

    from .lifecycle import Lifecycle
    from .web import build_app

    life = Lifecycle(cfg.root)
    life.install()
    sup = Supervisor(cfg.root)
    if not args.no_autostart:
        for line in sup.autostart():
            print(f"  {line}", flush=True)
    sup.run_background()

    tray = None
    server: uvicorn.Server | None = None

    def request_exit(reason: str = "console request") -> None:
        life.request_exit(reason)
        if server is not None:
            server.should_exit = True
        if tray is not None:
            tray.stop()

    server = uvicorn.Server(uvicorn.Config(build_app(sup, request_exit=request_exit, lifecycle=life),
                                           host=cfg.host, port=cfg.port, log_level="warning"))
    if args.tray:
        try:
            from .tray import Tray

            tray = Tray(sup, cfg.base_url, lambda: request_exit("tray menu"))
        except Exception as exc:  # no desktop session, or no GUI backend for pystray
            print(f"tray unavailable, running without it: {exc}", file=sys.stderr, flush=True)

    print(f"mcpm console: {cfg.base_url}   (managed servers stop when the manager exits)", flush=True)
    if args.open:
        threading.Timer(1.5, open_console, [cfg.base_url]).start()
    try:
        if tray is None:
            server.run()
            life.note_exit("console server returned")
            return 0

        # The tray owns the main thread (macOS requires it); HTTP runs beside it.
        http = threading.Thread(target=server.run, name="mcpm-http", daemon=True)
        http.start()
        while not server.started and http.is_alive():
            time.sleep(0.1)
        if not server.started:  # uvicorn ends its thread when it cannot bind
            print(f"console could not listen on {cfg.base_url}", file=sys.stderr)
            return 1

        def stop_tray_with_console() -> None:
            http.join()
            life.note_exit("console HTTP server stopped")
            tray.stop()

        threading.Thread(target=stop_tray_with_console, name="mcpm-http-watch", daemon=True).start()
        tray.run()
        life.note_exit("tray message loop ended")
        server.should_exit = True
        http.join(timeout=10)
        return 0
    finally:
        sup.shutdown()
        life.finish()


def cmd_status(args: argparse.Namespace, cfg: ManagerConfig) -> int:
    api = Api(cfg)
    if api.alive():
        services = api.call("GET", "/api/services")[1]["services"]
        where = f"manager: {cfg.base_url}"
    else:
        sup = Supervisor(cfg.root)
        sup.tick()
        services = sup.snapshots()
        where = "manager: not running (local view)"
    if args.json:
        print(json.dumps(services, ensure_ascii=False, indent=2))
        return 0
    print(where)
    rows = [("NAME", "STATUS", "PORT", "PID", "HEALTH", "UPTIME", "NOTE")]
    for s in services:
        health = s.get("health") or {}
        if not health:
            health_text = "-"
        elif health.get("code") is not None:
            health_text = f"{health['code']} {health.get('ms')}ms"
        else:
            health_text = "down"
        pid = str(s["pid"]) if s.get("pid") else (f"({s['listener_pid']})" if s.get("listener_pid") else "-")
        if s.get("kind") == "observe":
            health_text = "observe"
            pid = f"~{s['observed_pid']}" if s.get("observed_pid") else "-"
        note = s.get("message") or ""
        if s.get("pending_restart"):
            note = f"restart to apply config; {note}".rstrip("; ")
        rows.append((s["name"], s["status"], str(s.get("port") or "-"), pid, health_text,
                     _uptime(s.get("uptime_s")), note))
    _table(rows)
    return 0


def cmd_lifecycle(args: argparse.Namespace, cfg: ManagerConfig) -> int:
    takeover = bool(getattr(args, "takeover", False))
    api, failed = Api(cfg), False
    if api.alive():
        names = [args.service]
        if args.service == "all":
            services = api.call("GET", "/api/services")[1]["services"]
            names = [s["name"] for s in services
                     if s["enabled"] and s["supported"] and not s["error"] and s.get("kind") != "observe"]
        for name in names:
            query = "?takeover=1" if takeover else ""
            code, data = api.call("POST", f"/api/services/{name}/{args.cmd}{query}")
            if code == 200:
                print(data["message"])
            else:
                print(f"{name}: {data.get('error')}", file=sys.stderr)
                failed = True
        return 1 if failed else 0

    sup = Supervisor(cfg.root)
    names = [n for n, s in sup.specs.items() if s.runnable] if args.service == "all" else [args.service]
    for name in names:
        try:
            action = getattr(sup, args.cmd)
            print(action(name) if args.cmd == "start" else action(name, takeover=takeover))
        except SupervisorError as exc:
            print(f"{name}: {exc}", file=sys.stderr)
            failed = True
    print("(manager not running: acted directly; `mcpm serve` adopts these processes)")
    return 1 if failed else 0


def cmd_port(args: argparse.Namespace, cfg: ManagerConfig) -> int:
    api = Api(cfg)
    if api.alive():
        code, data = api.call("PUT", f"/api/services/{args.service}/config", body={"port": args.port})
        if code != 200:
            print(f"mcpm: {data.get('error')}", file=sys.stderr)
            return 1
        print(f"{args.service}: port -> {args.port}" + ("; restart it to apply" if data.get("restart_needed") else ""))
        return 0
    specs = discover(cfg)
    if args.service not in specs:
        raise RegistryError(f"unknown service: {args.service}")
    if specs[args.service].observed:
        raise RegistryError(f"{args.service} is observe-only: it has no port of its own to change")
    save_override(cfg, args.service, {"port": args.port})
    print(f"{args.service}: port -> {args.port} (saved to {cfg.path}); restart it to apply")
    return 0


def cmd_logs(args: argparse.Namespace, cfg: ManagerConfig) -> int:
    from . import logs

    path = Supervisor(cfg.root).log_path(args.service, args.source)
    if not path.exists():
        print(f"(no log yet: {path})", file=sys.stderr)
    text, offset = logs.tail(path, args.lines)
    if text:
        print(text, flush=True)
    while args.follow:
        time.sleep(0.5)
        with contextlib.suppress(OSError):
            if path.stat().st_size < offset:
                offset = 0
        chunk, offset = logs.read_from(path, offset)
        if chunk:
            print(chunk, end="", flush=True)
    return 0


def cmd_open(args: argparse.Namespace, cfg: ManagerConfig) -> int:
    if not Api(cfg).alive():
        print(f"mcpm is not running; start it with `mcpm serve` (console will be {cfg.base_url})", file=sys.stderr)
        return 1
    print(f"opened the console in the {open_console(cfg.base_url)}")
    return 0


def cmd_shutdown(args: argparse.Namespace, cfg: ManagerConfig) -> int:
    api = Api(cfg)
    if not api.alive():
        stopped = Supervisor(cfg.root).shutdown()
        print("mcpm is not running; managed servers stopped" + (f" ({len(stopped)})" if stopped else ""))
        return 0
    code, data = api.call("POST", "/api/manager/shutdown")
    if code != 200:
        print(f"mcpm: {data.get('error')}", file=sys.stderr)
        return 1
    print(data["message"])
    return 0


COMMANDS = {
    "serve": cmd_serve, "status": cmd_status, "start": cmd_lifecycle, "stop": cmd_lifecycle,
    "restart": cmd_lifecycle, "port": cmd_port, "logs": cmd_logs, "open": cmd_open,
    "shutdown": cmd_shutdown,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcpm", description="Manage local MCP servers")
    parser.add_argument("--root", help=f"directory with {CONFIG_NAME} and the servers "
                                       "(default: $MCPM_ROOT, else nearest one upward from cwd)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the supervisor and the web console")
    p.add_argument("--no-autostart", action="store_true", help="do not start autostart services")
    p.add_argument("--open", action="store_true", help="open the console in a browser")
    p.add_argument("--tray", action="store_true",
                   help="show a system tray icon (left click: console; right click: services, quit)")

    p = sub.add_parser("status", help="list services")
    p.add_argument("--json", action="store_true")

    for name in ("start", "stop", "restart"):
        p = sub.add_parser(name, help=f"{name} a service ('all' = every enabled one)")
        p.add_argument("service")
        if name != "start":
            p.add_argument("--takeover", action="store_true",
                           help="also stop a process mcpm did not start that holds the port")

    p = sub.add_parser("port", help="change a service port (saved to the central config)")
    p.add_argument("service")
    p.add_argument("port", type=int)

    p = sub.add_parser("logs", help="print a service log")
    p.add_argument("service")
    p.add_argument("-n", "--lines", type=int, default=100)
    p.add_argument("-s", "--source", default="stdout", help="stdout, or the index of a declared extra log")
    p.add_argument("-f", "--follow", action="store_true")

    sub.add_parser("open", help="open the web console")
    sub.add_parser("shutdown", help="stop the manager and all its managed servers")
    return parser


def main(argv: list[str] | None = None) -> int:
    from mcpkit.runtime import install_windows_socket_cleanup
    install_windows_socket_cleanup()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace")  # Chinese text on a legacy console
    from .logs import configure_process_logging
    configure_process_logging()
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve() if args.root else default_root()
    from .logs import use_bundled_runtime
    use_bundled_runtime(root)
    try:
        if args.cmd == "serve":
            from .enrollment import enroll_if_needed
            enroll_if_needed(root)
        return COMMANDS[args.cmd](args, load_manager_config(root))
    except (RegistryError, SupervisorError) as exc:
        print(f"mcpm: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
