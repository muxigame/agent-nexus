"""Detectors for services mcpm watches but never runs.

Some things are worth seeing in the console without being ours to start or
stop - a web browser most of all, where "restart" would close every tab the
user has open. A service with ``"kind": "observe"`` names a detector here
instead of a command. The supervisor calls it on a timer and shows what it
reports, and never records a pid that ``kill_tree`` could reach.

Detectors must be passive: read files and the process table, never connect to
the thing they watch. Chromium's runtime-enabled remote debugging
(``edge://inspect/#remote-debugging``) can ask the user to approve a new
DevTools connection, so even a harmless-looking probe of that port every few
seconds could keep popping a dialog in the user's browser.
"""
from __future__ import annotations

import ipaddress
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import psutil

RUNNING, DEGRADED, ABSENT = "running", "degraded", "absent"
DEFAULT_INTERVAL_S = 10.0


@dataclass
class Observation:
    state: str
    message: str = ""
    pid: int | None = None
    port: int | None = None
    started_at: float | None = None
    facts: list[tuple[str, str]] = field(default_factory=list)

    def signature(self) -> tuple:
        """What deserves a log line. Facts such as the process count change
        whenever a tab opens, so they are shown but do not count as a change."""
        return self.state, self.message, self.pid, self.port


# -- Chromium browsers with WebMCP -------------------------------------------------

WEBMCP_FEATURE = "WebMCP"
WEBMCP_FLAG = "enable-webmcp-testing"

BROWSERS: dict[str, dict[str, Any]] = {
    "edge": {
        "label": "Microsoft Edge",
        "scheme": "edge",
        # Lower-cased process names of the browser's main process per platform.
        "processes": {"msedge.exe", "msedge", "microsoft edge", "microsoft-edge", "microsoft-edge-stable"},
        # (environment variable or "~", path parts) of the default user data directory.
        "user_data": {
            "win32": ("LOCALAPPDATA", "Microsoft", "Edge", "User Data"),
            "darwin": ("~", "Library", "Application Support", "Microsoft Edge"),
            "linux": ("~", ".config", "microsoft-edge"),
        },
    },
    "chrome": {
        "label": "Google Chrome",
        "scheme": "chrome",
        "processes": {"chrome.exe", "chrome", "google chrome", "google-chrome", "google-chrome-stable"},
        "user_data": {
            "win32": ("LOCALAPPDATA", "Google", "Chrome", "User Data"),
            "darwin": ("~", "Library", "Application Support", "Google", "Chrome"),
            "linux": ("~", ".config", "google-chrome"),
        },
    },
}


@dataclass
class BrowserProcess:
    """The browser's main process and what its process tree says about it."""
    pid: int
    started_at: float
    cmdline: list[str]
    child_cmdlines: list[list[str]]
    #: (ip, port) of TCP sockets the main process listens on; None when the
    #: OS would not tell us.
    listening: list[tuple[str, int]] | None


def default_user_data_dir(browser: str, platform: str = sys.platform,
                          environ: dict[str, str] | None = None) -> Path | None:
    environ = os.environ if environ is None else environ
    table = BROWSERS[browser]["user_data"]
    parts = table.get("linux" if platform.startswith("linux") else platform)
    if parts is None:
        return None
    root, *rest = parts
    base = str(Path.home()) if root == "~" else environ.get(root)
    return Path(base, *rest) if base else None


def switch_value(cmdline: list[str], name: str) -> str | None:
    prefix = f"--{name}="
    for arg in cmdline:
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return None


def features_in(cmdline: list[str], switch: str) -> set[str]:
    """Feature names from every ``--enable-features=`` (or ``--disable-features=``).

    Entries may carry a field trial and parameters (``Name<Trial:key/value``);
    only the name matters here.
    """
    prefix = f"--{switch}="
    names: set[str] = set()
    for arg in cmdline:
        if not arg.startswith(prefix):
            continue
        for item in arg[len(prefix):].split(","):
            name = item.split("<", 1)[0].split(":", 1)[0].strip().lstrip("*")
            if name:
                names.add(name)
    return names


def flag_entries(local_state: dict[str, Any], name: str = WEBMCP_FLAG) -> list[str]:
    """``chrome://flags`` choices for ``name``, as stored in ``Local State``."""
    labs = (local_state.get("browser") or {}).get("enabled_labs_experiments") or []
    return [e for e in labs if isinstance(e, str) and e.split("@", 1)[0] == name]


def flag_enabled(entry: str) -> bool:
    """Whether a stored flag choice is plainly "Enabled".

    Choice flags are stored as ``name@index``. Index 1 is always "Enabled";
    what the higher indexes mean depends on how many variations the flag has
    (the last one is "Disabled"), so they are not guessed at - the running
    processes' ``--enable-features`` is what settles whether it is active.
    """
    _, sep, index = entry.partition("@")
    return not sep or index == "1"


def read_devtools_port(user_data: Path) -> int | None:
    """Port from ``DevToolsActivePort``. The file outlives the browser, so
    this is only a candidate until something is seen listening on it."""
    try:
        first = (user_data / "DevToolsActivePort").read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return None
    first = first.strip()
    return int(first) if first.isdigit() and 0 < int(first) < 65536 else None


def _is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip.split("%", 1)[0]).is_loopback
    except ValueError:
        return ip == "localhost"


_local_state_cache: dict[Path, tuple[float, dict[str, Any]]] = {}


def read_local_state(user_data: Path) -> dict[str, Any]:
    """``Local State`` (tens of KB of JSON), re-parsed only when it changes."""
    path = user_data / "Local State"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _local_state_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    _local_state_cache[path] = (mtime, data)
    return data


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def find_browser(names: set[str], user_data: Path) -> tuple[BrowserProcess | None, int]:
    """The main process using ``user_data``, and how many main processes of
    the same browser use some other profile directory (test runners, etc.)."""
    target = os.path.normcase(os.path.abspath(user_data))
    mains: list[tuple[float, psutil.Process, list[str]]] = []
    others = 0
    for proc in psutil.process_iter(["name"]):
        if (proc.info.get("name") or "").lower() not in names:
            continue
        try:
            cmdline = proc.cmdline()
            started = proc.create_time()
        except psutil.Error:
            continue
        if not cmdline:
            # Being created or exiting: with no arguments to read, a renderer
            # would look exactly like a main process.
            continue
        if any(arg.startswith("--type=") for arg in cmdline):
            continue  # renderer, GPU, utility... children of a main process
        custom = switch_value(cmdline, "user-data-dir")
        if custom and os.path.normcase(os.path.abspath(custom.strip('"'))) != target:
            others += 1
            continue
        mains.append((started, proc, cmdline))
    if not mains:
        return None, others
    # Chromium runs one browser process per profile directory. Any other
    # candidate on the same directory is a launcher handing its arguments to
    # that process and exiting, so the oldest wins and the rest are not
    # "other instances".
    started, proc, cmdline = min(mains, key=lambda m: m[0])

    child_cmdlines: list[list[str]] = []
    try:
        children = proc.children(recursive=True)
    except psutil.Error:
        children = []
    for child in children:
        try:
            child_cmdlines.append(child.cmdline())
        except psutil.Error:
            continue

    listening: list[tuple[str, int]] | None
    try:
        getter = getattr(proc, "net_connections", None) or proc.connections
        listening = [(c.laddr.ip, c.laddr.port) for c in getter(kind="tcp")
                     if c.status == psutil.CONN_LISTEN and c.laddr]
    except psutil.Error:
        listening = None
    return BrowserProcess(proc.pid, started, cmdline, child_cmdlines, listening), others


def chromium_webmcp(options: dict[str, Any]) -> Observation:
    browser = str(options.get("browser") or "edge")
    info = BROWSERS[browser]
    label, scheme = info["label"], info["scheme"]
    flag_url = f"{scheme}://flags/#{WEBMCP_FLAG}"

    if options.get("user_data_dir"):
        user_data: Path | None = Path(str(options["user_data_dir"])).expanduser()
    else:
        user_data = default_user_data_dir(browser)
    if user_data is None:
        return Observation(DEGRADED, f"不知道 {label} 在 {sys.platform} 上的配置目录，请在 observe.user_data_dir 指定")

    version = _read_text(user_data / "Last Version")
    title = f"{label} {version}".strip()
    entries = flag_entries(read_local_state(user_data))
    flag_text = ", ".join(entries) if entries else "未设置"
    configured = any(flag_enabled(e) for e in entries)

    main, others = find_browser(info["processes"], user_data)
    if main is None:
        message = f"{label} 未运行"
        if others:
            message += f"（另有 {others} 个实例使用其他配置目录）"
        facts = [("浏览器", title), (f"flags · {WEBMCP_FLAG}", flag_text), ("配置目录", str(user_data))]
        return Observation(ABSENT, message, facts=facts)

    trees = [main.cmdline, *main.child_cmdlines]
    disabled = any(WEBMCP_FEATURE in features_in(c, "disable-features") for c in trees)
    enabled = any(WEBMCP_FEATURE in features_in(c, "enable-features") for c in trees)

    problems: list[str] = []
    if disabled:
        webmcp = "被 --disable-features 关闭"
        problems.append("WebMCP 被命令行 --disable-features 关闭")
    elif enabled:
        webmcp = "已生效" if configured else "已生效（命令行 --enable-features，非 flags 页）"
    elif not main.child_cmdlines:
        webmcp = "等待子进程启动后确认"
    elif configured:
        webmcp = "flags 已开启，未生效"
        problems.append(f"flags 已开启 WebMCP，但当前进程没有加载：重启 {label} 后生效")
    else:
        webmcp = "未开启"
        problems.append(f"WebMCP 未开启：在 {flag_url} 启用后重启 {label}")

    port: int | None = None
    candidate = read_devtools_port(user_data)
    cli_port = switch_value(main.cmdline, "remote-debugging-port")
    if main.listening is None:
        debugging = "无法读取浏览器的监听端口"
    else:
        wanted = {candidate, int(cli_port) if cli_port and cli_port.isdigit() else None} - {None}
        bound = [(ip, p) for ip, p in main.listening if p in wanted]
        if bound:
            ip, port = bound[0]
            how = "命令行 --remote-debugging-port" if cli_port else f"运行时开启（{scheme}://inspect/#remote-debugging）"
            debugging = f"{ip}:{port} · {how}"
            exposed = [f"{ip}:{p}" for ip, p in bound if not _is_loopback(ip)]
            if exposed:
                problems.append(f"远程调试监听在 {', '.join(exposed)}，同一网络里的任何人都能直接控制这个浏览器")
        elif "--remote-debugging-pipe" in main.cmdline:
            debugging = "管道模式（--remote-debugging-pipe），没有端口"
        else:
            debugging = "关闭"

    facts = [
        ("浏览器", title),
        ("主进程", f"pid {main.pid} · 共 {1 + len(main.child_cmdlines)} 个进程"),
        ("WebMCP", webmcp),
        (f"flags · {WEBMCP_FLAG}", flag_text),
        ("远程调试", debugging),
        ("配置目录", str(user_data)),
    ]
    if others:
        facts.append(("其他实例", f"{others} 个（使用其他配置目录，未计入）"))
    return Observation(DEGRADED if problems else RUNNING, "；".join(problems),
                       pid=main.pid, port=port, started_at=main.started_at, facts=facts)


# -- registry ------------------------------------------------------------------------

DETECTORS: dict[str, Callable[[dict[str, Any]], Observation]] = {
    "chromium-webmcp": chromium_webmcp,
}


def check(options: Any) -> str:
    """Why ``options`` cannot be observed, or an empty string."""
    if not isinstance(options, dict):
        return 'kind "observe" needs an "observe" object naming a detector'
    name = options.get("detector")
    if name not in DETECTORS:
        return f"unknown detector {name!r} (known: {', '.join(sorted(DETECTORS))})"
    if name == "chromium-webmcp" and str(options.get("browser") or "edge") not in BROWSERS:
        return f"unknown browser {options.get('browser')!r} (known: {', '.join(sorted(BROWSERS))})"
    interval = options.get("interval")
    if interval is not None:
        try:
            float(interval)
        except (TypeError, ValueError):
            return f"invalid observe.interval: {interval!r}"
    return ""


def observe(options: dict[str, Any]) -> Observation:
    return DETECTORS[str(options["detector"])](options)
