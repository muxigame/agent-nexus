"""Observe-only services: shown by mcpm, never started, stopped or killed."""
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest
from starlette.testclient import TestClient

from agent_nexus import observers
from agent_nexus.observers import ABSENT, DEGRADED, RUNNING, BrowserProcess, Observation
from agent_nexus.registry import CONFIG_NAME, MANIFEST_NAME, discover, load_manager_config
from agent_nexus.supervisor import Supervisor, SupervisorError
from agent_nexus.web import build_app

OBSERVE = {"detector": "chromium-webmcp", "browser": "edge", "interval": 5}


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def watched(root: Path, name: str = "edge", **extra) -> Path:
    directory = root / name
    write(directory / MANIFEST_NAME, {"kind": "observe", "observe": OBSERVE, **extra})
    return directory


def fake_detector(monkeypatch, *observations: Observation) -> list[int]:
    """Replace the chromium detector; returns a list that counts calls."""
    calls: list[int] = []

    def detect(options):
        calls.append(1)
        return observations[min(len(calls), len(observations)) - 1]

    monkeypatch.setitem(observers.DETECTORS, "chromium-webmcp", detect)
    return calls


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# -- registry -----------------------------------------------------------------------

def test_observe_manifest_needs_no_command_and_drops_process_fields(tmp_path):
    watched(tmp_path, command=["msedge.exe"], port=9222, autostart=True, autorestart=True)
    write(tmp_path / CONFIG_NAME, {"services": {"edge": {"port": 9333}}})
    spec = discover(load_manager_config(tmp_path))["edge"]

    assert (spec.kind, spec.error) == ("observe", "")
    assert spec.command == [] and spec.port is None
    assert not spec.autostart and not spec.autorestart
    assert spec.health_interval == 5.0
    assert not spec.runnable  # so autostart and `start all` skip it


def test_observe_manifest_errors(tmp_path):
    write(tmp_path / "a" / MANIFEST_NAME, {"kind": "observe"})
    write(tmp_path / "b" / MANIFEST_NAME, {"kind": "observe", "observe": {"detector": "nope"}})
    write(tmp_path / "c" / MANIFEST_NAME, {"kind": "observe", "observe": {"detector": "chromium-webmcp", "browser": "lynx"}})
    write(tmp_path / "d" / MANIFEST_NAME, {"kind": "daemon", "command": ["x"]})
    specs = discover(load_manager_config(tmp_path))
    assert "needs an \"observe\" object" in specs["a"].error
    assert "unknown detector 'nope'" in specs["b"].error
    assert "unknown browser 'lynx'" in specs["c"].error
    assert "unknown kind 'daemon'" in specs["d"].error


# -- supervisor ---------------------------------------------------------------------

def test_lifecycle_refuses_observed_service(tmp_path, monkeypatch):
    watched(tmp_path)
    fake_detector(monkeypatch, Observation(RUNNING, pid=12345, port=9222))
    sup = Supervisor(tmp_path)
    sup.tick()
    for action in (sup.start, sup.stop, sup.restart):
        with pytest.raises(SupervisorError, match="observe-only"):
            action("edge")
    with pytest.raises(SupervisorError, match="observe-only"):
        sup.stop("edge", takeover=True)
    assert sup.autostart() == []


def test_refresh_maps_states_and_logs_only_changes(tmp_path, monkeypatch):
    watched(tmp_path)
    facts = [("浏览器", "Microsoft Edge 1.2.3"), ("主进程", "pid 42 · 共 5 个进程")]
    busier = [("浏览器", "Microsoft Edge 1.2.3"), ("主进程", "pid 42 · 共 9 个进程")]
    calls = fake_detector(monkeypatch,
                          Observation(RUNNING, pid=42, port=9222, started_at=time.time() - 60, facts=facts),
                          Observation(RUNNING, pid=42, port=9222, started_at=time.time() - 60, facts=busier),
                          Observation(DEGRADED, "WebMCP 未开启", pid=42, facts=busier),
                          Observation(ABSENT, "Microsoft Edge 未运行"))
    sup = Supervisor(tmp_path)
    rt = sup.rt["edge"]

    sup.tick()
    snap = sup.snapshot("edge")
    assert (snap["status"], snap["kind"], snap["observed_pid"], snap["port"]) == ("running", "observe", 42, 9222)
    assert snap["pid"] is None and snap["listener_pid"] is None  # nothing a kill path could use
    assert snap["facts"][0] == ["浏览器", "Microsoft Edge 1.2.3"]
    assert 55 <= snap["uptime_s"] <= 70

    sup.tick()
    assert len(calls) == 1, "second tick inside the interval must not run the detector again"

    for expected in ("running", "unhealthy", "stopped"):
        rt.next_probe = 0
        sup.tick()
        assert sup.snapshot("edge")["status"] == expected
    assert sup.snapshot("edge")["uptime_s"] is None

    lines = sup.stdout_log("edge").read_text(encoding="utf-8").splitlines()
    # 4 observations, but the process count alone changing is not a new line
    assert [line.split("  ")[1].split("]")[0] + "]" for line in lines] == ["[running]", "[degraded]", "[absent]"]
    assert "WebMCP 未开启" in lines[1]


def test_detector_exception_becomes_unhealthy(tmp_path, monkeypatch):
    watched(tmp_path)

    def broken(options):
        raise RuntimeError("boom")

    monkeypatch.setitem(observers.DETECTORS, "chromium-webmcp", broken)
    sup = Supervisor(tmp_path)
    sup.tick()
    snap = sup.snapshot("edge")
    assert snap["status"] == "unhealthy" and "detector failed: boom" in snap["message"]


def test_disabled_observed_service_does_not_run_detector(tmp_path, monkeypatch):
    watched(tmp_path, enabled=False)
    calls = fake_detector(monkeypatch, Observation(RUNNING, pid=1))
    sup = Supervisor(tmp_path)
    sup.tick()
    assert sup.snapshot("edge")["status"] == "disabled" and calls == []


def test_takeover_refuses_to_kill_a_watched_process(tmp_path, monkeypatch):
    """The real risk: a managed service configured on the browser's debugging
    port shows as ``external``, and takeover would kill the whole browser."""
    port = free_port()
    browser = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.2)
        write(tmp_path / "svc" / MANIFEST_NAME, {"command": [sys.executable, "-c", "pass"], "port": port})
        watched(tmp_path)
        fake_detector(monkeypatch, Observation(RUNNING, pid=browser.pid, port=port))
        sup = Supervisor(tmp_path)
        sup.tick()
        assert sup.snapshot("svc")["status"] == "external"

        with pytest.raises(SupervisorError, match="observe-only service edge is watching"):
            sup.stop("svc", takeover=True)
        with pytest.raises(SupervisorError, match="refusing to kill"):
            sup.restart("svc", takeover=True)
        assert psutil.pid_exists(browser.pid) and browser.poll() is None
    finally:
        browser.kill()
        browser.wait(timeout=10)


# -- web ------------------------------------------------------------------------------

def test_api_refuses_actions_and_config_except_enabled(tmp_path, monkeypatch):
    watched(tmp_path)
    fake_detector(monkeypatch, Observation(RUNNING, pid=7, port=9222))
    sup = Supervisor(tmp_path)
    client = TestClient(build_app(sup), base_url="http://127.0.0.1:8399")
    headers = {"X-MCPM": "1"}

    for action in ("start", "stop", "restart"):
        resp = client.post(f"/api/services/edge/{action}?takeover=1", headers=headers)
        assert resp.status_code == 409 and "observe-only" in resp.json()["error"]
    for body in ({"port": 9333}, {"autostart": True}, {"enabled": False, "port": 1}):
        assert client.put("/api/services/edge/config", json=body, headers=headers).status_code == 409

    resp = client.put("/api/services/edge/config", json={"enabled": False}, headers=headers)
    assert resp.status_code == 200 and resp.json()["service"]["status"] == "disabled"


# -- the chromium detector ---------------------------------------------------------

def browser_dir(tmp_path: Path, flags=("enable-webmcp-testing@1",), devtools_port: int | None = 9222) -> Path:
    user_data = tmp_path / "User Data"
    write(user_data / "Local State", {"browser": {"enabled_labs_experiments": ["other-flag@2", *flags]}})
    (user_data / "Last Version").write_text("153.0.4234.32\n", encoding="utf-8")
    if devtools_port is not None:
        (user_data / "DevToolsActivePort").write_text(f"{devtools_port}\n/devtools/browser/abc\n", encoding="utf-8")
    return user_data


def run_detector(monkeypatch, user_data: Path, process: BrowserProcess | None, others: int = 0) -> Observation:
    monkeypatch.setattr(observers, "find_browser", lambda names, path: (process, others))
    return observers.chromium_webmcp({"browser": "edge", "user_data_dir": str(user_data)})


def edge(cmdline=("msedge.exe", "--restart"), children=None, listening=(("127.0.0.1", 9222),)) -> BrowserProcess:
    if children is None:
        children = [["msedge.exe", "--type=renderer", "--enable-features=Foo,WebMCP<Trial:x/1,Bar"]]
    return BrowserProcess(pid=4242, started_at=1000.0, cmdline=list(cmdline), child_cmdlines=children,
                          listening=None if listening is None else list(listening))


def fact(obs: Observation, key: str) -> str:
    return dict(obs.facts)[key]


def test_detector_running_with_webmcp_and_runtime_debugging(tmp_path, monkeypatch):
    obs = run_detector(monkeypatch, browser_dir(tmp_path), edge())
    assert (obs.state, obs.message, obs.pid, obs.port) == (RUNNING, "", 4242, 9222)
    assert fact(obs, "浏览器") == "Microsoft Edge 153.0.4234.32"
    assert fact(obs, "WebMCP") == "已生效"
    assert fact(obs, "flags · enable-webmcp-testing") == "enable-webmcp-testing@1"
    assert fact(obs, "远程调试") == "127.0.0.1:9222 · 运行时开启（edge://inspect/#remote-debugging）"


def test_detector_not_running(tmp_path, monkeypatch):
    obs = run_detector(monkeypatch, browser_dir(tmp_path), None, others=2)
    assert obs.state == ABSENT and obs.pid is None and obs.port is None
    assert "未运行" in obs.message and "另有 2 个实例" in obs.message


def test_detector_flag_on_but_not_loaded_needs_restart(tmp_path, monkeypatch):
    obs = run_detector(monkeypatch, browser_dir(tmp_path), edge(children=[["msedge.exe", "--type=gpu-process"]]))
    assert obs.state == DEGRADED and "重启 Microsoft Edge 后生效" in obs.message


def test_detector_flag_off(tmp_path, monkeypatch):
    obs = run_detector(monkeypatch, browser_dir(tmp_path, flags=()), edge(children=[["msedge.exe", "--type=renderer"]]))
    assert obs.state == DEGRADED and "edge://flags/#enable-webmcp-testing" in obs.message
    assert fact(obs, "flags · enable-webmcp-testing") == "未设置"


def test_detector_disable_features_wins(tmp_path, monkeypatch):
    children = [["msedge.exe", "--type=renderer", "--enable-features=WebMCP", "--disable-features=WebMCP"]]
    obs = run_detector(monkeypatch, browser_dir(tmp_path), edge(children=children))
    assert obs.state == DEGRADED and "--disable-features" in obs.message


def test_detector_enabled_from_command_line_only(tmp_path, monkeypatch):
    obs = run_detector(monkeypatch, browser_dir(tmp_path, flags=()), edge())
    assert obs.state == RUNNING and "命令行" in fact(obs, "WebMCP")


def test_detector_stale_devtools_file_means_debugging_off(tmp_path, monkeypatch):
    obs = run_detector(monkeypatch, browser_dir(tmp_path), edge(listening=()))
    assert obs.state == RUNNING and obs.port is None and fact(obs, "远程调试") == "关闭"


def test_detector_flags_debugging_exposed_beyond_loopback(tmp_path, monkeypatch):
    obs = run_detector(monkeypatch, browser_dir(tmp_path), edge(listening=(("0.0.0.0", 9222),)))
    assert obs.state == DEGRADED and "0.0.0.0:9222" in obs.message


def test_detector_command_line_debugging_port(tmp_path, monkeypatch):
    process = edge(cmdline=("msedge.exe", "--remote-debugging-port=9333"), listening=(("127.0.0.1", 9333),))
    obs = run_detector(monkeypatch, browser_dir(tmp_path, devtools_port=None), process)
    assert obs.port == 9333 and "命令行 --remote-debugging-port" in fact(obs, "远程调试")


def test_detector_socket_table_unavailable(tmp_path, monkeypatch):
    obs = run_detector(monkeypatch, browser_dir(tmp_path), edge(listening=None))
    assert obs.state == RUNNING and obs.port is None and "无法读取" in fact(obs, "远程调试")


# -- parsing helpers -----------------------------------------------------------------

def test_features_flags_and_devtools_file(tmp_path):
    cmd = ["x", "--enable-features=A,WebMCP<Trial:p/1", "--enable-features=*B:k/v", "--disable-features=C"]
    assert observers.features_in(cmd, "enable-features") == {"A", "WebMCP", "B"}
    assert observers.features_in(cmd, "disable-features") == {"C"}

    state = {"browser": {"enabled_labs_experiments": ["enable-webmcp-testing@1", "enable-webmcp-testing-x@1", 7]}}
    assert observers.flag_entries(state) == ["enable-webmcp-testing@1"]
    assert observers.flag_entries({}) == []
    assert [observers.flag_enabled(e) for e in ("f@1", "f", "f@2", "f@0", "f@3")] == [True, True, False, False, False]

    (tmp_path / "DevToolsActivePort").write_text("9222\n/devtools/browser/x", encoding="utf-8")
    assert observers.read_devtools_port(tmp_path) == 9222
    (tmp_path / "DevToolsActivePort").write_text("junk", encoding="utf-8")
    assert observers.read_devtools_port(tmp_path) is None
    assert observers.read_devtools_port(tmp_path / "missing") is None


def test_default_user_data_dirs():
    env = {"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
    assert observers.default_user_data_dir("edge", "win32", env) == Path(env["LOCALAPPDATA"], "Microsoft", "Edge", "User Data")
    assert observers.default_user_data_dir("edge", "win32", {}) is None
    assert observers.default_user_data_dir("chrome", "linux", {}) == Path.home() / ".config" / "google-chrome"
    assert observers.default_user_data_dir("edge", "darwin", {}).parts[-1] == "Microsoft Edge"
    assert observers.default_user_data_dir("edge", "sunos5", {}) is None


class FakeProc:
    def __init__(self, pid, name, cmdline, started, children=(), listening=()):
        self.pid, self.info = pid, {"name": name}
        self._cmdline, self._started, self._children, self._listening = cmdline, started, list(children), listening

    def cmdline(self):
        return self._cmdline

    def create_time(self):
        return self._started

    def children(self, recursive=False):
        return self._children

    def net_connections(self, kind="tcp"):
        return [type("C", (), {"status": psutil.CONN_LISTEN, "laddr": type("A", (), {"ip": ip, "port": p})()})()
                for ip, p in self._listening]


def test_find_browser_ignores_transient_processes_and_counts_only_other_profiles(tmp_path, monkeypatch):
    default = tmp_path / "User Data"
    renderer = FakeProc(11, "msedge.exe", ["msedge.exe", "--type=renderer", "--enable-features=WebMCP"], 50.0)
    procs = [
        FakeProc(10, "msedge.exe", ["msedge.exe", "--restart"], 10.0, children=[renderer], listening=[("127.0.0.1", 9222)]),
        renderer,
        FakeProc(12, "msedge.exe", [], 60.0),                                   # exiting: no arguments readable
        FakeProc(13, "msedge.exe", ["msedge.exe", "https://example.com"], 70.0),  # launcher handing off
        FakeProc(14, "msedge.exe", ["msedge.exe", f'--user-data-dir="{default}"'], 80.0),  # same dir, quoted
        FakeProc(20, "msedge.exe", ["msedge.exe", f"--user-data-dir={tmp_path / 'playwright'}"], 5.0),
        FakeProc(30, "chrome.exe", ["chrome.exe"], 1.0),
    ]
    monkeypatch.setattr(observers.psutil, "process_iter", lambda attrs=None: iter(procs))

    main, others = observers.find_browser(observers.BROWSERS["edge"]["processes"], default)
    assert (main.pid, others) == (10, 1)
    assert main.child_cmdlines == [renderer.cmdline()]
    assert main.listening == [("127.0.0.1", 9222)]
