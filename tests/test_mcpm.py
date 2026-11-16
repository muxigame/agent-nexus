import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent_nexus import logs
from agent_nexus.registry import CONFIG_NAME, MANIFEST_NAME, discover, load_manager_config, save_override
from agent_nexus.supervisor import Supervisor, SupervisorError, listener_of, resolve_executable
from agent_nexus.web import build_app


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write(path: Path, data: dict, bom: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + json.dumps(data, ensure_ascii=False).encode("utf-8"))


def http_service(root: Path, name: str, port: int, **extra) -> Path:
    directory = root / name
    write(directory / MANIFEST_NAME, {
        "command": [sys.executable, "-m", "http.server", "{port}", "--bind", "127.0.0.1"],
        "port": port, "health": {"path": "/", "interval": 5}, **extra,
    })
    return directory


def wait_for(predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def status_is(sup: Supervisor, name: str, status: str):
    def check() -> bool:
        sup.tick()
        return sup.snapshot(name)["status"] == status
    return check


# -- registry -------------------------------------------------------------------

def test_manifest_with_bom_merges_config_override(tmp_path):
    write(tmp_path / "svc" / MANIFEST_NAME,
          {"name": "alpha", "command": ["run", "--port", "{port}"], "port": 9001, "env": {"A": "{dir}", "B": "1"}}, bom=True)
    write(tmp_path / CONFIG_NAME, {"manager": {"port": 9000}, "services": {
        "alpha": {"port": 9002, "env": {"B": "2"}},
        "beta": {"dir": "elsewhere", "command": ["x"]},
    }}, bom=True)
    specs = discover(load_manager_config(tmp_path))

    alpha = specs["alpha"]
    assert (alpha.source, alpha.port, alpha.error) == ("manifest+config", 9002, "")
    assert alpha.resolved_command() == ["run", "--port", "9002"]
    assert alpha.resolved_env() == {"A": str((tmp_path / "svc").resolve()), "B": "2"}
    assert specs["beta"].source == "config"
    assert "directory not found" in specs["beta"].error


def test_port_clashes_are_flagged(tmp_path):
    for name in ("a", "b"):
        write(tmp_path / name / MANIFEST_NAME, {"command": ["x"], "port": 9100})
    write(tmp_path / "c" / MANIFEST_NAME, {"command": ["x"], "port": 8399})
    specs = discover(load_manager_config(tmp_path))
    assert "also used by b" in specs["a"].error
    assert "also used by a" in specs["b"].error
    assert "agent-nexus" in specs["c"].error


def test_portable_client_prefers_bundled_uv(tmp_path):
    bundled = tmp_path / "tools" / ("uv.exe" if sys.platform == "win32" else "uv")
    bundled.parent.mkdir()
    bundled.write_bytes(b"portable uv")
    assert resolve_executable("uv", {"PATH": ""}, tmp_path) == str(bundled)


def test_save_override_validates_and_persists(tmp_path):
    write(tmp_path / "a" / MANIFEST_NAME, {"command": ["x"], "port": 9100})
    cfg = load_manager_config(tmp_path)
    save_override(cfg, "a", {"port": "9200"})
    assert json.loads((tmp_path / CONFIG_NAME).read_text(encoding="utf-8"))["services"]["a"]["port"] == 9200
    with pytest.raises(ValueError):
        save_override(cfg, "a", {"port": 70000})
    with pytest.raises(ValueError):
        save_override(cfg, "a", {"bogus": 1})


# -- logs -------------------------------------------------------------------------

def test_log_tail_and_incremental_read(tmp_path):
    path = tmp_path / "x.log"
    path.write_bytes(b"one\ntwo\nthr")
    assert logs.tail(path, 10) == ("one\ntwo\nthr", 11)
    assert logs.read_from(path, 0) == ("one\ntwo\n", 8)  # the unfinished line waits
    with open(path, "ab") as fh:
        fh.write(b"ee\n")
    assert logs.read_from(path, 8) == ("three\n", 14)


# -- web ----------------------------------------------------------------------------

def test_web_rejects_foreign_requests_and_edits_config(tmp_path):
    write(tmp_path / "svc" / MANIFEST_NAME, {"name": "svc", "command": ["x"], "port": 9300})
    sup = Supervisor(tmp_path)
    client = TestClient(build_app(sup), base_url="http://127.0.0.1:8399")
    guarded = {"X-MCPM": "1"}

    assert client.get("/api/services").status_code == 200
    assert TestClient(build_app(sup), base_url="http://evil.example:8399").get("/api/services").status_code == 403
    assert client.post("/api/services/svc/start").status_code == 403
    assert client.post("/api/services/svc/stop", headers={**guarded, "Origin": "http://evil.example"}).status_code == 403

    resp = client.put("/api/services/svc/config", headers=guarded, json={"port": 9301})
    assert resp.status_code == 200
    assert resp.json()["service"]["port"] == 9301 and resp.json()["restart_needed"] is False
    assert client.put("/api/services/svc/config", headers=guarded, json={"command": ["y"]}).status_code == 400
    assert client.get("/api/services/nope").status_code == 404


# -- supervisor ------------------------------------------------------------------------

def test_start_health_adopt_pending_restart_stop(tmp_path):
    port = free_port()
    http_service(tmp_path, "web", port)
    sup = Supervisor(tmp_path)
    assert "started" in sup.start("web")
    assert wait_for(status_is(sup, "web", "running")), sup.snapshot("web")
    snap = sup.snapshot("web")
    assert snap["health"]["code"] == 200
    assert snap["pending_restart"] is False

    restarted_manager = Supervisor(tmp_path)
    assert wait_for(status_is(restarted_manager, "web", "running"))
    assert restarted_manager.snapshot("web")["pid"] == snap["pid"]

    save_override(restarted_manager.cfg, "web", {"port": free_port()})
    restarted_manager.rescan()
    assert restarted_manager.snapshot("web")["pending_restart"] is True

    restarted_manager.stop("web")
    assert wait_for(lambda: not listener_of("127.0.0.1", port)[0])
    assert wait_for(status_is(restarted_manager, "web", "stopped"))


def test_crash_is_reported_and_auto_restarted(tmp_path):
    write(tmp_path / "boom" / MANIFEST_NAME,
          {"command": [sys.executable, "-c", "import sys; sys.exit(3)"], "autorestart": True})
    sup = Supervisor(tmp_path)
    sup.start("boom")
    assert wait_for(status_is(sup, "boom", "crashed"))
    message = sup.snapshot("boom")["message"]
    assert "code 3" in message and "restarting in" in message

    sup.rt["boom"].restart_at = 0  # skip the back-off
    sup.tick()
    assert sup.rt["boom"].pid is not None
    assert wait_for(lambda: (sup.tick(), sup.snapshot("boom")["crashes"] == 2)[1])
    sup.stop("boom")
    assert sup.rt["boom"].restart_at is None


def test_external_process_is_left_alone_until_takeover(tmp_path):
    port = free_port()
    directory = http_service(tmp_path, "ext", port)
    outsider = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                                cwd=directory, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_for(lambda: listener_of("127.0.0.1", port)[0])
        sup = Supervisor(tmp_path)
        assert wait_for(status_is(sup, "ext", "external"))
        with pytest.raises(SupervisorError):
            sup.start("ext")

        assert "started" in sup.restart("ext", takeover=True)
        assert outsider.wait(timeout=10) is not None
        assert wait_for(status_is(sup, "ext", "running"))
        sup.stop("ext")
    finally:
        if outsider.poll() is None:
            outsider.kill()
