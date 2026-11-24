"""Command line for agent-nexus (``anx``; ``mcpm`` is a deprecated alias).

Every command talks to a running manager over its local API when there is
one, so the web console sees the change immediately. Without a manager the
lifecycle commands act directly; the processes they start are recorded in
``.anx/run`` and adopted by the next ``anx serve``.
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

from . import paths
from .desktop import open_console
from .hooks import hook_payload  # noqa: F401  (``anx hook``'s payload, design §8.3; tests import it from here)
from .registry import CONFIG_NAME, ManagerConfig, RegistryError, discover, load_manager_config, save_override
from .supervisor import Supervisor, SupervisorError

_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


PROG = "anx"
#: Request header the console API requires on state-changing calls.
#: ``X-MCPM`` is still accepted from older clients.
API_HEADERS = {"X-ANX": "1", "X-MCPM": "1", "Content-Type": "application/json"}


def default_root() -> Path:
    return paths.default_root()


class Api:
    def __init__(self, cfg: ManagerConfig):
        self.cfg = cfg

    def call(self, method: str, path: str, body: Any = None, timeout: float = 60) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.cfg.base_url + path, data=data, method=method,
                                     headers=API_HEADERS)
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


#: How long the tray may take to end after the console stopped (cmd_serve).
TRAY_STOP_GRACE_S = 15.0


def cmd_serve(args: argparse.Namespace, cfg: ManagerConfig) -> int:
    if Api(cfg).alive():
        print(f"agent-nexus is already running at {cfg.base_url}")
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

    print(f"agent-nexus console: {cfg.base_url}   (managed servers stop when the manager exits)", flush=True)
    if args.open:
        threading.Timer(1.5, open_console, [cfg.base_url]).start()
    try:
        if tray is None:
            server.run()
            life.note_exit("console server returned")
            return 0

        # The tray owns the main thread (macOS requires it); HTTP runs beside it.
        http = threading.Thread(target=server.run, name="anx-http", daemon=True)
        http.start()
        while not server.started and http.is_alive():
            time.sleep(0.1)
        if not server.started:  # uvicorn ends its thread when it cannot bind
            print(f"console could not listen on {cfg.base_url}", file=sys.stderr)
            return 1

        tray_done = threading.Event()

        def stop_tray_with_console() -> None:
            http.join()
            life.note_exit("console HTTP server stopped")
            tray.stop()
            # pystray's Windows message loop has been seen to keep running after stop(): the process
            # then lingers with its tray icon and no console, and the next manager shows a second
            # icon. Do not linger: stop the plugins, take the icon away and end the process.
            if not tray_done.wait(TRAY_STOP_GRACE_S):
                life.note_exit(f"tray did not stop within {TRAY_STOP_GRACE_S:.0f}s; ending the process")
                try:
                    sup.shutdown()
                    tray.hide()
                finally:
                    life.finish()
                    os._exit(0)

        threading.Thread(target=stop_tray_with_console, name="anx-http-watch", daemon=True).start()
        tray.run()
        tray_done.set()
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
    print(f"(manager not running: acted directly; `{PROG} serve` adopts these processes)")
    return 1 if failed else 0


def cmd_port(args: argparse.Namespace, cfg: ManagerConfig) -> int:
    api = Api(cfg)
    if api.alive():
        code, data = api.call("PUT", f"/api/services/{args.service}/config", body={"port": args.port})
        if code != 200:
            print(f"{PROG}: {data.get('error')}", file=sys.stderr)
            return 1
        print(f"{args.service}: port -> {args.port}" + ("; restart it to apply" if data.get("restart_needed") else ""))
        return 0
    specs = discover(cfg)
    if args.service not in specs:
        raise RegistryError(f"unknown service: {args.service}")
    if specs[args.service].observed:
        raise RegistryError(f"{args.service} is observe-only: it has no port of its own to change")
    save_override(cfg, args.service, {"port": args.port})
    print(f"{args.service}: port -> {args.port} (saved to {cfg.write_path}); restart it to apply")
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
        print(f"agent-nexus is not running; start it with `{PROG} serve` (console will be {cfg.base_url})", file=sys.stderr)
        return 1
    print(f"opened the console in the {open_console(cfg.base_url)}")
    return 0


def cmd_shutdown(args: argparse.Namespace, cfg: ManagerConfig) -> int:
    api = Api(cfg)
    if not api.alive():
        stopped = Supervisor(cfg.root).shutdown()
        print("agent-nexus is not running; managed servers stopped" + (f" ({len(stopped)})" if stopped else ""))
        return 0
    code, data = api.call("POST", "/api/manager/shutdown")
    if code != 200:
        print(f"{PROG}: {data.get('error')}", file=sys.stderr)
        return 1
    print(data["message"])
    return 0


def _source_body(source: Any) -> dict[str, Any]:
    if source.type == "path":
        return {"source": "path", "path": source.path, "link": source.link}
    if source.type == "git":
        return {"source": "git", "url": source.url, "ref": source.ref}
    return {"source": "archive", "path": source.path, "url": source.url, "sha256": source.sha256}


def _print_pack(result: dict[str, Any]) -> None:
    print(f"{result['name']} {result['version']}: {result['path']}")
    print(f"  {result['size']} bytes, {result['files']} files, sha256 {result['sha256']}")
    rules = (result.get("excluded") or {}).get("rules") or {}
    if rules:
        print("  excluded: " + ", ".join(f"{rule} ({count})" for rule, count in rules.items()))


def cmd_plugin_package(args: argparse.Namespace, root: Path) -> int:
    """``anx plugin pack <folder> [-o x.zip]`` and ``anx plugin publish <folder|zip>`` (design §6.12).

    Packing needs no manager and no gateway. Publishing uploads the package to
    the joined gateway's 插件库 over HTTPS with this device's own token.
    """
    import tempfile

    from .plugins import hub_source, installer, pack
    try:
        if args.plugin_cmd == "pack":
            result = pack.pack(Path(args.folder), Path(args.output) if args.output else None)
            _print_pack(result)
            return 0
        source = Path(args.source)
        with tempfile.TemporaryDirectory(prefix="anx-publish-") as work:
            if source.is_dir():
                result = pack.pack(source, Path(work) / "package.zip")
                _print_pack(result)
                archive = Path(result["path"])
            elif source.is_file():
                archive = source
            else:
                print(f"{PROG} plugin publish: {source} is neither a plugin folder nor a .zip", file=sys.stderr)
                return 1
            item = hub_source.publish(root, archive, replace=args.replace)
        state = "replaced" if item.get("replaced") else "published"
        print(f"{state} {item['name']} {item['version']} to the gateway's 插件库 (sha256 {item['sha256']})")
        return 0
    except pack.PackError as exc:
        print(f"{PROG} plugin {args.plugin_cmd}: {exc}", file=sys.stderr)
        return 1
    except hub_source.HubError as exc:
        print(f"{PROG} plugin publish: {exc} ({exc.code})", file=sys.stderr)
        for finding in exc.extra.get("findings") or []:
            print(f"  {finding['path']}: {finding['rule']}", file=sys.stderr)
        return 1
    except installer.InstallError as exc:
        print(f"{PROG} plugin {args.plugin_cmd}: {exc}", file=sys.stderr)
        return 1


def cmd_plugin(args: argparse.Namespace, cfg: ManagerConfig) -> int:
    """``anx plugin list|inspect|install|uninstall|enable|disable`` (design §6.5).

    With a manager running for this root the console API does the work, so its
    supervisor starts the plugin and the console shows it; otherwise the
    installer runs here, health-checks the plugin and stops it again.
    """
    from .plugins import installer, manage

    api = Api(cfg)
    online = api.alive()
    try:
        if args.plugin_cmd == "list":
            if online:
                plugins = api.call("GET", "/api/plugins")[1]["plugins"]
            else:
                sup = Supervisor(cfg.root)
                sup.tick()
                plugins = manage.plugin_views(sup)
            if args.json:
                print(json.dumps(plugins, ensure_ascii=False, indent=2))
                return 0
            rows = [("NAME", "VERSION", "STATUS", "SOURCE", "NOTE")]
            for item in plugins:
                rows.append((item["name"], item.get("version") or "-", item["status"],
                             (item.get("source") or {}).get("type", "-"), item.get("message") or ""))
            _table(rows)
            return 0
        if args.plugin_cmd in ("inspect", "install"):
            source = installer.parse_source(args.source, ref=getattr(args, "ref", "") or "",
                                            link=bool(getattr(args, "link", False)), sha256=getattr(args, "sha256", "") or "")
            if args.plugin_cmd == "inspect":
                info = installer.inspect(cfg.root, source)
                print(json.dumps(info, ensure_ascii=False, indent=2))
                return 0
            if not args.yes:
                info = installer.inspect(cfg.root, source)
                print(f"将安装 {info['name']} {info['version']}（{info['title']}）")
                for step in info["install_steps"]:
                    print(f"  安装步骤: {step}")
                for warning in info["warnings"]:
                    print(f"  注意: {warning}")
                if input("继续？[y/N] ").strip().lower() not in ("y", "yes"):
                    return 1
            enable = not args.no_enable
            if online:
                body = {**_source_body(source), "enable": enable, "port": args.port}
                code, data = api.call("POST", "/api/plugins/install", body=body)
                if code != 202:
                    print(f"{PROG} plugin: {data.get('error')}", file=sys.stderr)
                    return 1
                seen = 0
                while True:
                    job = api.call("GET", f"/api/plugins/jobs/{data['job_id']}?since={seen}")[1]
                    for line in job.get("log", []):
                        print(line, flush=True)
                    seen = job.get("log_total", seen)
                    if job.get("state") != "running":
                        break
                    time.sleep(0.5)
                if job["state"] != "done":
                    return 1
                result = job["result"]
            else:
                inst = installer.Installer(cfg.root, None, log=lambda line: print(line, flush=True))
                outcome = inst.install(source, enable=enable, port=args.port)
                result = {"name": outcome.name, "state": outcome.state, "message": outcome.message}
            if result["state"] == "needs_config":
                print(f"下一步：在控制台“插件 → {result['name']} → 配置”里填写必填项")
            return 0
        if args.plugin_cmd == "uninstall":
            if online:
                code, data = api.call("DELETE", f"/api/plugins/{args.name}" + ("?purge=1" if args.purge else ""))
                if code != 200:
                    print(f"{PROG} plugin: {data.get('error')}", file=sys.stderr)
                    return 1
            else:
                installer.Installer(cfg.root, None, log=print).uninstall(args.name, purge=args.purge)
                return 0
            print(f"已卸载 {args.name}" + ("" if args.purge else "；数据和凭据保留（--purge 才删除）"))
            return 0
        # enable / disable
        enabled = args.plugin_cmd == "enable"
        if online:
            code, data = api.call("POST", f"/api/plugins/{args.name}/{args.plugin_cmd}")
            if code != 200:
                print(f"{PROG} plugin: {data.get('error')}", file=sys.stderr)
                return 1
            print(data.get("message") or "ok")
            return 0
        installer.update_entry(cfg.root, args.name, enabled=enabled, autostart=True if enabled else None)
        print(f"{args.name}: {'已启用' if enabled else '已停用'}（manager 未运行；下次启动时生效）")
        return 0
    except (installer.InstallError, manage.PluginError) as exc:
        print(f"{PROG} plugin: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def cmd_migrate(args: argparse.Namespace, root: Path) -> int:
    from . import migrate

    try:
        if args.export_legacy:
            skipped = migrate.export_legacy(root, Path(args.export_legacy))
            print(f"wrote {Path(args.export_legacy).resolve()} in the legacy format (merge it by hand; "
                  "nothing else was changed)")
            for line in skipped:
                print(f"  not exported: {line}")
            return 0
        if args.in_place:
            source = Path(args.source).resolve() if args.source else root
            target = source
            if args.to and Path(args.to).resolve() != source:
                raise migrate.MigrationError("--in-place and --to name different roots")
        else:
            source = Path(args.source).resolve() if args.source else (
                Path(args.from_file).resolve().parent if args.from_file else paths.legacy_installed_root())
            target = Path(args.to).resolve() if args.to else paths.installed_root()
            if source is None or target is None:
                raise migrate.MigrationError("cannot tell the default roots here; pass --from and --to")
        plan = migrate.plan_migration(source, target, source_file=Path(args.from_file) if args.from_file else None,
                                      port_offset=args.port_offset, agent_id=args.agent_id, cloud_url=args.cloud_url)
        report = migrate.apply_migration(plan) if args.apply else None
        migrate.print_plan(plan, applied=bool(args.apply))
        if report:
            print(f"  report: {report}")
        return 3 if plan.conflicts else 0
    except (migrate.MigrationError, OSError) as exc:
        print(f"{PROG} migrate: {exc}", file=sys.stderr)
        return 2


def cmd_secrets(args: argparse.Namespace, root: Path) -> int:
    from mcpkit.config import ConfigError, ServiceConfig, check_secret_name, secret_path, write_secret

    from . import secrets_restore

    root = Path(args.secrets_root).resolve() if getattr(args, "secrets_root", None) else root
    try:
        if args.secrets_cmd == "restore":
            plan = secrets_restore.restore(Path(args.vault), root, apply=args.apply, include=set(args.include or ()))
            return 3 if plan.conflicts else 0
        # set
        check_secret_name(args.name)
        value = Path(args.from_file).read_text(encoding="utf-8-sig").strip()
        if not value:
            raise ConfigError(f"{args.from_file} is empty")
        config = ServiceConfig(name="manager", root=root, dir=root)
        target = secret_path(config, args.name)
        if target.is_file() and target.read_text(encoding="utf-8-sig").strip() != value and not args.replace:
            print(f"{PROG} secrets: {args.name} exists with other content; not replaced (use --replace)",
                  file=sys.stderr)
            return 3
        print(f"secret {args.name} -> {write_secret(config, args.name, value)}")
        return 0
    except (secrets_restore.VaultError, ConfigError, ValueError, OSError) as exc:
        # Messages name files and rules, never what a secret contains.
        print(f"{PROG} secrets: {exc}", file=sys.stderr)
        return 2


def cmd_device(args: argparse.Namespace, root: Path) -> int:
    from . import conformance

    if args.device_cmd == "conformance":
        return conformance.run(args)
    return 2


def _wake_config(root: Path) -> dict:
    """``wake`` of agent-nexus.json (``cloud.wake`` wins, as in the agent)."""
    try:
        config = paths.read_config(root) if paths.has_config(root) else {}
    except (OSError, ValueError):
        config = {}
    cloud = config.get("cloud") if isinstance(config.get("cloud"), dict) else {}
    section = cloud.get("wake") if isinstance(cloud.get("wake"), dict) else config.get("wake")
    return section if isinstance(section, dict) else {}


def cmd_wake(args: argparse.Namespace, root: Path) -> int:
    from .link.wake import doctor
    if args.wake_cmd == "doctor":
        kinds = args.kind or ["claude", "codex", "gpt"]
        return doctor.run(root, _wake_config(root), kinds=list(dict.fromkeys(kinds)), permission=args.permission,
                          timeout=args.timeout, endpoint=args.endpoint)
    return 2


#: ``anx hook``: the whole hook (stdin, title, report) ends within this many seconds.
HOOK_DEADLINE_S = 4.0
HOOK_STDIN_S = 1.5
HOOK_POST_TIMEOUT_S = 2.0
#: Threads of this hook still running at its end (a stdin read that never returns).
_HOOK_THREADS: list[threading.Thread] = []


def _read_stdin(timeout: float) -> str | None:
    """The hook's stdin, read in a thread: a client that never closes it must not hold the hook."""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        return None
    with contextlib.suppress(Exception):
        if sys.stdin.isatty():
            return None
    box: list[bytes] = []

    def read() -> None:
        with contextlib.suppress(Exception):
            box.append(stream.read(1024 * 1024))

    reader = threading.Thread(target=read, daemon=True)
    _HOOK_THREADS.append(reader)
    reader.start()
    reader.join(timeout)
    return box[0].decode("utf-8", errors="replace") if box else None


def cmd_hook(args: argparse.Namespace, root: Path) -> int:
    """``anx hook claude|codex``: report the session to the local manager.

    Never fails or delays the client: it prints nothing (Claude adds a hook's
    stdout to the conversation), always exits 0, and gives up after
    ``HOOK_DEADLINE_S``.
    """
    def work() -> None:
        raw = args.payload if args.payload is not None else _read_stdin(HOOK_STDIN_S)
        payload = hook_payload(args.kind, raw, args.event)
        if payload is not None:
            Api(load_manager_config(root)).call("POST", "/api/agents/presence", payload,
                                                timeout=HOOK_POST_TIMEOUT_S)

    def guarded() -> None:
        with contextlib.suppress(BaseException):
            work()

    worker = threading.Thread(target=guarded, daemon=True)
    worker.start()
    worker.join(HOOK_DEADLINE_S)
    if worker.is_alive() or any(thread.is_alive() for thread in _HOOK_THREADS):
        # A thread stuck in a read makes interpreter shutdown abort (a non-zero exit): leave at once.
        with contextlib.suppress(Exception):
            sys.stdout.flush()
        os._exit(0)
    return 0


def cmd_integrate(args: argparse.Namespace, root: Path) -> int:
    """``anx integrate claude|codex``: install (or ``--remove``) the session hooks (design §8.3)."""
    from . import integrate
    # The hook names this root: from a project folder, the nearest root upward may be another one.
    return integrate.run(args.kind, dry_run=args.dry_run, remove=args.remove, hooks=args.hooks, root=root)


def _is_hook_call(argv: list[str]) -> bool:
    """``anx [--root R] hook ...``, found before argparse: it exits 2 on bad arguments, and 2 blocks a Claude prompt."""
    index = 0
    while index < len(argv) and argv[index].startswith("-"):
        index += 2 if argv[index] == "--root" else 1
    return index < len(argv) and argv[index] == "hook"


def _main_hook(argv: list[str]) -> int:
    import io
    try:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            args = build_parser().parse_args(argv)  # a usage error stays silent too
        root = Path(args.root).resolve() if args.root else default_root()
        if args.root and not root.is_dir():
            # A root that does not exist has no manager: never report to the default port
            # instead (that is whichever manager runs there, maybe another installation's).
            return 0
        return cmd_hook(args, root)
    except BaseException:  # a hook must never fail its client
        return 0


def cmd_mail(args: argparse.Namespace, cfg: ManagerConfig) -> int:
    """``anx mail address set <address> --wake-permission <mode>`` through the local manager."""
    from urllib.parse import quote
    fields: dict[str, Any] = {}
    if args.wake_permission is not None:
        fields["wake_permission"] = None if args.wake_permission == "default" else args.wake_permission
    if args.wake_policy is not None:
        fields["wake_policy"] = args.wake_policy
    if args.resume_id is not None:
        fields["resume_id"] = args.resume_id or None
    if not fields:
        print(f"{PROG}: nothing to set (--wake-permission, --wake-policy or --resume-id)", file=sys.stderr)
        return 2
    try:
        code, data = Api(cfg).call("PUT", "/api/inbox/addresses/" + quote(args.address, safe=""), fields, timeout=15)
    except OSError as exc:
        print(f"{PROG}: the manager is not running ({exc})", file=sys.stderr)
        return 1
    if code != 200:
        print(f"{PROG}: {data.get('error', code)}", file=sys.stderr)
        return 1
    address = data.get("address") or {}
    print(f"{address.get('address_display', args.address)}: wake_permission="
          f"{address.get('wake_permission') or 'default'}, wake_policy={address.get('wake_policy')}")
    return 0


def cmd_join(args: argparse.Namespace, cfg: ManagerConfig) -> int:
    """``anx join <invite>``: redeem an invite code and connect to that gateway (design §11.5)."""
    from . import join as joining
    api = Api(cfg)
    if api.alive():
        code, data = api.call("POST", "/api/cloud/join", {"invite": args.invite, "agent_id": args.agent_id}, timeout=60)
    else:
        try:
            data = joining.join(cfg.root, args.invite, agent_id=args.agent_id, replace=args.force)
            code = 200
        except joining.JoinError as exc:
            code, data = exc.status, exc.body()
    if code != 200:
        print(f"{PROG}: {data.get('error', code)} ({data.get('code', code)})", file=sys.stderr)
        return 1
    print(f"joined {data.get('hub')} as {data.get('agent_id')} (shown as {data.get('display_name') or data.get('agent_id')})")
    if not api.alive():
        print("the manager is not running: `anx serve` (or the desktop app) connects this device")
    elif data.get("state") not in ("connected", None):
        print(f"cloud link: {data.get('state')}")
    return 0


def cmd_leave(args: argparse.Namespace, cfg: ManagerConfig) -> int:
    """``anx leave``: forget the gateway (removes the device token; the device stays in the console until revoked)."""
    from . import join as joining
    api = Api(cfg)
    if api.alive():
        code, data = api.call("POST", "/api/cloud/leave", {})
    else:
        try:
            data, code = joining.leave(cfg.root), 200
        except joining.JoinError as exc:
            code, data = exc.status, exc.body()
    if code != 200:
        print(f"{PROG}: {data.get('error', code)}", file=sys.stderr)
        return 1
    print("left the gateway; this device now runs locally only")
    return 0


#: Commands that work on roots and files themselves, not on a loaded configuration.
def cmd_term(args: argparse.Namespace, root: Path) -> int:
    from .term_cli import cmd_term as run
    return run(args, root)


ROOT_COMMANDS = {"migrate": cmd_migrate, "secrets": cmd_secrets, "device": cmd_device, "wake": cmd_wake,
                 "hook": cmd_hook, "integrate": cmd_integrate, "term": cmd_term}

COMMANDS = {
    "serve": cmd_serve, "status": cmd_status, "start": cmd_lifecycle, "stop": cmd_lifecycle,
    "restart": cmd_lifecycle, "port": cmd_port, "logs": cmd_logs, "open": cmd_open,
    "shutdown": cmd_shutdown, "mail": cmd_mail, "join": cmd_join, "leave": cmd_leave,
    "plugin": cmd_plugin,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="agent-nexus: manage local MCP servers")
    parser.add_argument("--root", help=f"directory with {CONFIG_NAME} (or legacy {paths.LEGACY_CONFIG_NAME}) "
                                       "and the servers (default: $ANX_ROOT, else nearest one upward from cwd)")
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
                           help="also stop a process agent-nexus did not start that holds the port")

    p = sub.add_parser("port", help="change a service port (saved to the central config)")
    p.add_argument("service")
    p.add_argument("port", type=int)

    p = sub.add_parser("logs", help="print a service log")
    p.add_argument("service")
    p.add_argument("-n", "--lines", type=int, default=100)
    p.add_argument("-s", "--source", default="stdout", help="stdout, or the index of a declared extra log")
    p.add_argument("-f", "--follow", action="store_true")

    p = sub.add_parser("migrate", help="copy a legacy root's configuration into agent-nexus.json "
                                       "(dry run unless --apply; the old files are never changed)")
    p.add_argument("--from", dest="source", help="old root (default: the legacy installed root, "
                                                  "or --root with --in-place)")
    p.add_argument("--from-file", help="a mcp-manager.json to migrate from (e.g. the vault copy)")
    p.add_argument("--to", help="new root (default: the installed root)")
    p.add_argument("--in-place", action="store_true", help="new root = old root; mcp-manager.json stays as it is")
    p.add_argument("--port-offset", type=int, default=0, help="add N to the manager and plugin ports (trial run)")
    p.add_argument("--agent-id", help="cloud.agent_id for the new root")
    p.add_argument("--cloud-url", help="cloud.url for the new root")
    p.add_argument("--apply", action="store_true", help="write; without it only the plan is printed")
    p.add_argument("--export-legacy", metavar="FILE",
                   help="instead: save the root's configuration in the legacy format to a new FILE")

    p = sub.add_parser("secrets", help="credentials: restore from the vault, set one from a file")
    secrets_sub = p.add_subparsers(dest="secrets_cmd", required=True)
    q = secrets_sub.add_parser("restore", help="restore the credentials vault (dry run unless --apply)")
    q.add_argument("--vault", required=True, help="the credentials-vault directory (with its README.md manifest)")
    q.add_argument("--root", dest="secrets_root", help="root to restore into (default: the global --root)")
    q.add_argument("--include", action="append", choices=["optional"], help="also restore optional items")
    q.add_argument("--apply", action="store_true", help="write; without it only the plan is printed")
    q = secrets_sub.add_parser("set", help="store a secret from a file (the value is never printed)")
    q.add_argument("name", help="secret name, e.g. connectors/yunxiao/key")
    q.add_argument("--from-file", required=True)
    q.add_argument("--root", dest="secrets_root", help="root to write into (default: the global --root)")
    q.add_argument("--replace", action="store_true", help="replace an existing secret with other content")

    p = sub.add_parser("device", help="device plugin tools (anx.device/1 contract)")
    device_sub = p.add_subparsers(dest="device_cmd", required=True)
    q = device_sub.add_parser("conformance", help="check a device MCP endpoint against anx.device/1")
    from .conformance import add_arguments as conformance_arguments
    conformance_arguments(q)

    p = sub.add_parser("wake", help="waking agent sessions (design §8.7)")
    wake_sub = p.add_subparsers(dest="wake_cmd", required=True)
    q = wake_sub.add_parser("doctor", help="check unattended claude/codex/gpt wakes on this device "
                                           "(runs the real CLIs once each)")
    from .link.wake.doctor import add_arguments as doctor_arguments
    doctor_arguments(q)

    p = sub.add_parser("hook", help="session hook for Claude Code / Codex: reports the session to the manager")
    p.add_argument("kind", choices=["claude", "codex"])
    p.add_argument("--event", help="the hook event (default: from the payload)")
    p.add_argument("payload", nargs="?", help="JSON payload (Codex notify); default: stdin")

    p = sub.add_parser("integrate", help="install the session hooks of Claude Code / Codex (anx hook)")
    p.add_argument("kind", choices=["claude", "codex"])
    p.add_argument("--dry-run", action="store_true", help="only print what would change")
    p.add_argument("--remove", action="store_true", help="remove agent-nexus's hook entries")
    p.add_argument("--hooks", action="store_true",
                   help="codex: use Codex's hooks.json instead of the notify setting")

    p = sub.add_parser("mail", help="mailbox settings")
    mail_sub = p.add_subparsers(dest="mail_cmd", required=True)
    address_parser = mail_sub.add_parser("address", help="mailbox addresses")
    address_sub = address_parser.add_subparsers(dest="address_cmd", required=True)
    q = address_sub.add_parser("set", help="change an address's wake settings")
    q.add_argument("address")
    q.add_argument("--wake-permission", choices=["bypass", "full-access", "inherit", "default"],
                   help="decision D2; default = the gateway's mailbox.wake.permission_default")
    q.add_argument("--wake-policy", choices=["auto", "never"])
    q.add_argument("--resume-id", help="correct the resume id ('' clears it)")

    sub.add_parser("open", help="open the web console")
    sub.add_parser("shutdown", help="stop the manager and all its managed servers")
    p = sub.add_parser("join", help="join a gateway with an invite code (anx1.… or agentnexus://enroll?invite=…)")
    p.add_argument("invite")
    p.add_argument("--agent-id", help="request this device id (default: from the hostname)")
    p.add_argument("--force", action="store_true", help="replace an existing gateway configuration")
    sub.add_parser("leave", help="leave the gateway (remove this device's token)")
    from .term_cli import add_parser as add_term_parser
    add_term_parser(sub)

    p = sub.add_parser("plugin", help="install, remove, enable and list plugins (design §6.5)")
    plugin_sub = p.add_subparsers(dest="plugin_cmd", required=True)
    q = plugin_sub.add_parser("list", help="installed plugins and their status")
    q.add_argument("--json", action="store_true")
    for verb in ("inspect", "install"):
        q = plugin_sub.add_parser(verb, help="show what a source contains" if verb == "inspect" else
                                  "install from a folder, a git URL or a .zip/.tar.gz (file or URL)")
        q.add_argument("source", help="folder path, git URL, or archive path / URL")
        q.add_argument("--ref", help="git branch or tag (default: the highest vX.Y.Z tag)")
        q.add_argument("--link", action="store_true", help="folder: run it in place instead of copying it")
        q.add_argument("--sha256", help="archive: expected SHA-256")
        if verb == "install":
            q.add_argument("--port", type=int, help="use this port instead of the manifest's")
            q.add_argument("--no-enable", action="store_true", help="install but leave it disabled")
            q.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    q = plugin_sub.add_parser("uninstall", help="remove a plugin; its data and secrets stay unless --purge")
    q.add_argument("name")
    q.add_argument("--purge", action="store_true", help="also delete plugin-data/<name> and secrets/<name>")
    for verb in ("enable", "disable"):
        q = plugin_sub.add_parser(verb, help=f"{verb} a plugin")
        q.add_argument("name")
    q = plugin_sub.add_parser("pack", help="pack a plugin folder into a .zip for the gateway's 插件库 "
                                           "(no .venv, node_modules, logs, secrets, data; refuses secrets)")
    q.add_argument("folder")
    q.add_argument("-o", "--output", help="the .zip to write (default: <name>-<version>.zip here)")
    q = plugin_sub.add_parser("publish", help="pack (if a folder) and upload to the joined gateway's 插件库 "
                                              "with this device's token")
    q.add_argument("source", help="plugin folder or a .zip made by `anx plugin pack`")
    q.add_argument("--replace", action="store_true", help="replace the same name and version on the gateway")
    return parser


def main(argv: list[str] | None = None) -> int:
    if _is_hook_call(sys.argv[1:] if argv is None else list(argv)):
        return _main_hook(sys.argv[1:] if argv is None else list(argv))
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
    if args.cmd in ROOT_COMMANDS:
        return ROOT_COMMANDS[args.cmd](args, root)
    if args.cmd == "plugin" and args.plugin_cmd in ("pack", "publish"):
        return cmd_plugin_package(args, root)
    try:
        if args.cmd == "serve" and not args.root and not paths.has_config(root) \
                and root == (paths.installed_root() or Path()).resolve():
            # First start of an installed root next to a legacy one (design 4.1 step 4).
            from .migrate import MigrationError, migrate_legacy_root
            try:
                migrate_legacy_root()
            except (MigrationError, OSError) as exc:
                print(f"{PROG}: could not copy the legacy root: {exc}", file=sys.stderr)
        if args.cmd == "serve":
            from .enrollment import enroll_if_needed
            enroll_if_needed(root)
        return COMMANDS[args.cmd](args, load_manager_config(root))
    except (RegistryError, SupervisorError) as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def deprecated_main() -> int:
    """The ``mcpm`` console script: same command, one deprecation line."""
    print("mcpm: deprecated command name; use `anx` (same arguments)", file=sys.stderr, flush=True)
    return main()


if __name__ == "__main__":
    sys.exit(main())
