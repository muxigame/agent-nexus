"""Milestone M1 (docs/design.md §1, §13): the rename to agent-nexus and its shims.

Acceptance: ``python -m mcpm.cli status`` and ``anx status`` agree;
``python -m mcpm.cloud --check-config`` exits 0; a root with only
``mcp-manager.json`` loads and is never written; ``agent-nexus.json`` wins when
both exist; ``MCPM_ROOT`` still works and warns once; a legacy desktop
bootstrap (``python -m mcpm.cli serve`` with MCPM_* only) starts the new
manager and sees it through ``/api/ping``.

Every port here is ephemeral. Nothing touches a live service.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import sysconfig
import time
import urllib.request
from importlib import metadata
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent_nexus import env as anx_env
from agent_nexus import paths
from agent_nexus.registry import discover, load_manager_config, save_override
from agent_nexus.supervisor import Supervisor
from agent_nexus.web import build_app

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = Path(sysconfig.get_path("scripts"))
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def fingerprint(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def isolated_env(**extra: str) -> dict[str, str]:
    """The test process environment without any root/desktop overrides."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANX_", "MCPM_"))}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", **extra)
    return env


def legacy_root(tmp_path: Path, **config) -> Path:
    root = tmp_path / "root"
    write(root / "svc" / "mcp-service.json", {"name": "svc", "command": ["python", "-V"], "port": free_port()})
    write(root / paths.LEGACY_CONFIG_NAME, {"manager": {"port": free_port()}, "builtins": False, "scan": ["."],
                                            "cloud": {"enabled": False}, **config})
    return root


def run(argv: list[str], *, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=120)


# -- names, entry points, shim ------------------------------------------------------------

def test_console_scripts_new_names_and_deprecated_aliases():
    scripts = {ep.name: ep.value for ep in metadata.entry_points(group="console_scripts")
               if ep.dist is not None and ep.dist.name == "agent-nexus"}
    assert scripts == {
        "anx": "agent_nexus.cli:main", "anx-agent": "agent_nexus.agent:main", "anx-gateway": "agent_nexus.cloud:main",
        "mcpm": "agent_nexus.cli:deprecated_main", "mcpm-client": "agent_nexus.agent:deprecated_main",
        "mcpm-server": "agent_nexus.cloud:deprecated_main",
    }


def test_mcpm_shim_modules_are_the_agent_nexus_modules():
    import agent_nexus
    import agent_nexus.agent
    import agent_nexus.cli
    import agent_nexus.cloud
    import mcpm
    import mcpm.agent
    import mcpm.cli
    import mcpm.cloud
    from mcpm.registry import CONFIG_NAME

    assert mcpm.cli is agent_nexus.cli and mcpm.cloud is agent_nexus.cloud and mcpm.agent is agent_nexus.agent
    assert mcpm.__version__ == agent_nexus.__version__
    assert CONFIG_NAME == "agent-nexus.json"
    shims = {p.stem for p in (REPO / "mcpm").glob("*.py")} - {"__init__"}
    # Every module the legacy package had keeps a shim; modules new in agent-nexus
    # (env, paths, migrate, ...) never had an mcpm name and need none.
    legacy = {p.stem for p in (REPO / "tests/compat/legacy_mcpm").glob("*.py")} - {"__init__"}
    assert shims == legacy
    assert legacy <= {p.stem for p in (REPO / "agent_nexus").glob("*.py")}


def test_status_through_the_shim_matches_anx(tmp_path):
    root = legacy_root(tmp_path)
    env = isolated_env()
    anx = run([str(SCRIPTS / "anx"), "--root", str(root), "status"], env=env, cwd=tmp_path)
    shim = run([sys.executable, "-m", "mcpm.cli", "--root", str(root), "status"], env=env, cwd=tmp_path)
    legacy = run([str(SCRIPTS / "mcpm"), "--root", str(root), "status"], env=env, cwd=tmp_path)
    assert anx.returncode == shim.returncode == legacy.returncode == 0, (anx.stderr, shim.stderr, legacy.stderr)
    assert "svc" in anx.stdout and "manager: not running" in anx.stdout
    assert shim.stdout == anx.stdout == legacy.stdout
    assert "deprecated" in legacy.stderr and "deprecated" not in anx.stderr + shim.stderr
    as_json = run([sys.executable, "-m", "mcpm.cli", "--root", str(root), "status", "--json"], env=env, cwd=tmp_path)
    assert [s["name"] for s in json.loads(as_json.stdout)] == ["svc"]


@pytest.mark.parametrize("argv", [
    [sys.executable, "-m", "mcpm.cloud"],
    [sys.executable, "-m", "agent_nexus.cloud"],
    [str(SCRIPTS / "anx-gateway")],
    [str(SCRIPTS / "mcpm-server")],
])
def test_gateway_check_config_through_every_name(tmp_path, argv):
    config = write(tmp_path / "gateway.json", {"agents": {"pc-main": "MCPM_AGENT1_TOKEN"}, "port": free_port()})
    env = isolated_env(MCPM_SERVER_TOKEN="s" * 40, MCPM_AGENT1_TOKEN="a" * 40)
    result = run([*argv, "--config", str(config), "--check-config"], env=env, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "configuration valid" in result.stdout
    # The default token variable is ANX_SERVER_TOKEN, falling back to MCPM_SERVER_TOKEN.
    env = isolated_env(ANX_SERVER_TOKEN="n" * 40, MCPM_AGENT1_TOKEN="a" * 40)
    assert run([*argv, "--config", str(config), "--check-config"], env=env, cwd=tmp_path).returncode == 0
    env = isolated_env(MCPM_AGENT1_TOKEN="a" * 40)
    missing = run([*argv, "--config", str(config), "--check-config"], env=env, cwd=tmp_path)
    assert missing.returncode == 2 and "ANX_SERVER_TOKEN" in missing.stderr


def test_agent_check_config_through_both_names(tmp_path):
    root = tmp_path / "root"
    (root / "work").mkdir(parents=True)
    write(root / paths.LEGACY_CONFIG_NAME, {"builtins": False, "scan": [], "cloud": {
        "enabled": True, "agent_id": "pc-main", "url": "ws://127.0.0.1:9", "token_env": "MCPM_AGENT_TOKEN",
        "workspaces": {"w": {"path": "work", "mcp": "extra"}},
        "mcps": {"extra": {"command": [sys.executable, "-c", "pass"]}}}})
    before = fingerprint(root / paths.LEGACY_CONFIG_NAME)
    env = isolated_env(MCPM_AGENT_TOKEN="a" * 40)
    for argv in ([str(SCRIPTS / "anx-agent")], [sys.executable, "-m", "mcpm.agent"], [str(SCRIPTS / "mcpm-client")]):
        result = run([*argv, "--root", str(root), "--check-config"], env=env, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "client configuration valid" in result.stdout
    assert fingerprint(root / paths.LEGACY_CONFIG_NAME) == before
    assert not (root / paths.CONFIG_NAME).exists()


# -- configuration file names ----------------------------------------------------------------

def test_legacy_only_root_loads_and_is_never_written(tmp_path):
    root = legacy_root(tmp_path, services={"svc": {"autostart": True}})
    legacy = root / paths.LEGACY_CONFIG_NAME
    before = fingerprint(legacy)
    cfg = load_manager_config(root)
    assert cfg.path == legacy and cfg.write_path == root / paths.CONFIG_NAME
    assert discover(cfg)["svc"].autostart is True
    sup = Supervisor(root)
    sup.tick()
    assert [s["name"] for s in sup.snapshots()] == ["svc"]
    assert fingerprint(legacy) == before and not (root / paths.CONFIG_NAME).exists()

    # The first write creates agent-nexus.json from the legacy content.
    save_override(cfg, "svc", {"port": 9555})
    assert fingerprint(legacy) == before
    created = json.loads((root / paths.CONFIG_NAME).read_text(encoding="utf-8"))
    assert created["services"]["svc"] == {"autostart": True, "port": 9555}
    assert created["manager"] == json.loads(legacy.read_text(encoding="utf-8"))["manager"]
    assert load_manager_config(root).path == root / paths.CONFIG_NAME
    assert discover(load_manager_config(root))["svc"].port == 9555


def test_agent_nexus_json_wins_when_both_exist(tmp_path):
    root = legacy_root(tmp_path)
    write(root / paths.CONFIG_NAME, {"manager": {"port": 9601}, "builtins": False, "scan": ["."]})
    write(root / paths.LEGACY_CONFIG_NAME, {"manager": {"port": 9602}, "builtins": False, "scan": ["."]})
    assert load_manager_config(root).port == 9601
    assert paths.read_config(root)["manager"]["port"] == 9601
    assert paths.find_root([root / "svc"]) == root


def test_root_resolution_order(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    write(a / paths.CONFIG_NAME, {})
    write(b / paths.LEGACY_CONFIG_NAME, {})
    nested = b / "deep" / "er"
    nested.mkdir(parents=True)
    assert paths.default_root({"ANX_ROOT": str(a), "MCPM_ROOT": str(b)}, cwd=nested) == a.resolve()
    assert paths.default_root({"MCPM_ROOT": str(a)}, cwd=nested) == a.resolve()
    assert paths.default_root({}, cwd=nested) == b.resolve()
    if sys.platform == "win32":
        installed = paths.installed_root({"LOCALAPPDATA": str(tmp_path / "local")})
        assert installed == tmp_path / "local" / "AgentNexus"
    else:
        installed = paths.installed_root({"XDG_DATA_HOME": str(tmp_path / "xdg")})
        assert installed == tmp_path / "xdg" / "agent-nexus"
    assert paths.installed_root({"ANX_DATA_HOME": str(tmp_path / "d")}).parent == tmp_path / "d"


# -- environment variables ---------------------------------------------------------------

def test_env_prefers_anx_and_warns_once_for_mcpm(capsys):
    anx_env._reset_warnings()
    assert anx_env.get("ROOT", environ={"ANX_ROOT": "new", "MCPM_ROOT": "old"}) == "new"
    assert capsys.readouterr().err == ""
    assert anx_env.get("ROOT", environ={"MCPM_ROOT": "old"}) == "old"
    assert anx_env.get("ROOT", environ={"MCPM_ROOT": "old"}) == "old"
    assert capsys.readouterr().err.count("MCPM_ROOT is deprecated") == 1
    assert anx_env.get("ROOT", "fallback", environ={"ANX_ROOT": ""}) == "fallback"
    assert anx_env.both("ROOT", "x") == {"ANX_ROOT": "x", "MCPM_ROOT": "x"}
    assert anx_env.resolve_token_env("CUSTOM", "SERVER_TOKEN") == "CUSTOM"
    assert anx_env.resolve_token_env(None, "SERVER_TOKEN", environ={"MCPM_SERVER_TOKEN": "t"}) == "MCPM_SERVER_TOKEN"
    assert anx_env.resolve_token_env(None, "SERVER_TOKEN", environ={"ANX_SERVER_TOKEN": "t", "MCPM_SERVER_TOKEN": "t"}) == "ANX_SERVER_TOKEN"
    assert anx_env.resolve_token_env(None, "SERVER_TOKEN", environ={}) == "ANX_SERVER_TOKEN"


def test_mcpm_root_still_selects_the_root_and_warns_once(tmp_path):
    root = legacy_root(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = run([str(SCRIPTS / "anx"), "status", "--json"], env=isolated_env(MCPM_ROOT=str(root)), cwd=elsewhere)
    assert result.returncode == 0, result.stderr
    assert [s["name"] for s in json.loads(result.stdout)] == ["svc"]
    assert result.stderr.count("MCPM_ROOT is deprecated") == 1
    quiet = run([str(SCRIPTS / "anx"), "status", "--json"], env=isolated_env(ANX_ROOT=str(root)), cwd=elsewhere)
    assert quiet.returncode == 0 and "deprecated" not in quiet.stderr


def test_supervisor_gives_plugins_both_root_variables(tmp_path):
    from agent_nexus import supervisor

    root = legacy_root(tmp_path)
    captured = {}

    class FakePopen:
        pid = 4242

        def __init__(self, argv, **kwargs):
            captured.update(kwargs["env"])
            raise OSError("not starting anything in a test")

    sup = Supervisor(root)
    original = supervisor.subprocess.Popen
    supervisor.subprocess.Popen = FakePopen
    try:
        with pytest.raises(Exception):
            sup.start("svc")
    finally:
        supervisor.subprocess.Popen = original
    assert captured["ANX_ROOT"] == captured["MCPM_ROOT"] == str(root.resolve())


def test_desktop_app_override_new_then_legacy(tmp_path, monkeypatch):
    from agent_nexus import desktop

    new, old = tmp_path / "agent-nexus.exe", tmp_path / "mcpm-desktop.exe"
    new.write_bytes(b"MZ")
    old.write_bytes(b"MZ")
    monkeypatch.delenv("ANX_DESKTOP_APP", raising=False)
    monkeypatch.setenv("MCPM_DESKTOP_APP", str(old))
    assert desktop.desktop_app() == old
    monkeypatch.setenv("ANX_DESKTOP_APP", str(new))
    assert desktop.desktop_app() == new


# -- console API ------------------------------------------------------------------------------

def test_ping_has_both_keys_and_either_header_is_accepted(tmp_path):
    root = legacy_root(tmp_path)
    sup = Supervisor(root)
    client = TestClient(build_app(sup), base_url=f"http://127.0.0.1:{sup.cfg.port}")
    info = client.get("/api/ping").json()
    assert info["anx"] == info["mcpm"] and info["root"] == str(root.resolve())
    assert info["config"] == str(root / paths.LEGACY_CONFIG_NAME) and info["config_exists"]
    assert client.post("/api/services/svc/stop").status_code == 403
    assert client.post("/api/services/svc/stop", headers={"X-ANX": "1"}).status_code == 200
    assert client.post("/api/services/svc/stop", headers={"X-MCPM": "1"}).status_code == 200


def test_legacy_bootstrap_starts_the_new_manager_and_sees_it_via_ping(tmp_path):
    """What the old Tauri shell does (bootstrap.rs before M1): MCPM_* only,
    ``python -m mcpm.cli --root R serve``, wait for ``mcpm`` in /api/ping,
    then quit through /api/manager/shutdown with ``X-MCPM``."""
    root = legacy_root(tmp_path)
    port = json.loads((root / paths.LEGACY_CONFIG_NAME).read_text())["manager"]["port"]
    before = fingerprint(root / paths.LEGACY_CONFIG_NAME)
    fake_shell = tmp_path / "mcpm-desktop.exe"
    fake_shell.write_bytes(b"MZ")
    env = isolated_env(MCPM_ROOT=str(root), MCPM_DESKTOP_APP=str(fake_shell), PYTHONUNBUFFERED="1")
    log = (tmp_path / "startup.log").open("wb")
    process = subprocess.Popen([sys.executable, "-m", "mcpm.cli", "--root", str(root), "serve", "--no-autostart"],
                               env=env, cwd=root, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    try:
        info = None
        for _ in range(150):
            try:
                request = urllib.request.Request(url + "/api/ping", headers={"X-MCPM": "1"})
                with _DIRECT.open(request, timeout=2) as response:
                    info = json.load(response)
                break
            except OSError:
                assert process.poll() is None, (tmp_path / "startup.log").read_text(errors="replace")
                time.sleep(0.2)
        assert info is not None and info.get("mcpm") and Path(info["root"]).resolve() == root.resolve()
        request = urllib.request.Request(url + "/api/manager/shutdown", data=b"", method="POST", headers={"X-MCPM": "1"})
        with _DIRECT.open(request, timeout=10) as response:
            assert response.status == 200
        assert process.wait(timeout=60) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
        log.close()
    # The old shell reads its own state path to tell a requested exit from a death.
    legacy_state = json.loads((root / ".mcp-manager/run/mcpm-manager.state").read_text(encoding="utf-8"))
    state = json.loads((root / ".anx/run/anx-manager.state").read_text(encoding="utf-8"))
    assert legacy_state["state"] == state["state"] == "exited"
    assert legacy_state["requested"] is state["requested"] is True
    assert fingerprint(root / paths.LEGACY_CONFIG_NAME) == before
    assert not (root / paths.CONFIG_NAME).exists()


def test_new_shell_gets_no_legacy_state_mirror(tmp_path, monkeypatch):
    from agent_nexus.lifecycle import Lifecycle, started_by_legacy_shell

    assert started_by_legacy_shell({"MCPM_DESKTOP_APP": "x"})
    assert not started_by_legacy_shell({"MCPM_DESKTOP_APP": "x", "ANX_DESKTOP_APP": "x"})
    assert not started_by_legacy_shell({})
    monkeypatch.setenv("ANX_DESKTOP_APP", "x")
    monkeypatch.setenv("MCPM_DESKTOP_APP", "x")
    life = Lifecycle(tmp_path)
    life._write("running")
    assert (tmp_path / ".anx/run/anx-manager.state").is_file()
    assert not (tmp_path / ".mcp-manager").exists()


# -- the device plugin (formerly workspace) -------------------------------------------------------

def test_device_plugin_manifest_and_legacy_service_entry(tmp_path):
    manifest = json.loads((REPO / "builtins/device/mcp-service.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "device" and manifest["aliases"] == ["workspace"]
    assert not (REPO / "builtins/codex-mcp").exists()

    root = tmp_path / "root"
    write(root / "plugins/device/mcp-service.json", {"name": "device", "aliases": ["workspace"],
                                                     "command": ["x"], "port": 9701})
    write(root / paths.LEGACY_CONFIG_NAME, {"builtins": False, "scan": ["plugins"],
                                            "services": {"workspace": {"autostart": True, "port": 9702}}})
    specs = discover(load_manager_config(root))
    assert set(specs) == {"device"}
    assert specs["device"].autostart is True and specs["device"].port == 9702
    # An entry under the new name wins key by key; the alias still fills the rest.
    write(root / paths.CONFIG_NAME, {"builtins": False, "scan": ["plugins"],
                                     "services": {"workspace": {"autostart": True, "port": 9702},
                                                  "device": {"port": 9703}}})
    specs = discover(load_manager_config(root))
    assert specs["device"].autostart is True and specs["device"].port == 9703


def test_hub_resolves_workspace_and_device_to_each_other():
    from agent_nexus.federation import Hub, RoutingError

    hub = Hub()
    old = hub.register("old-pc", object(), {"workspaces": {}, "mcps": {"workspace": {"tools": []}}})
    new = hub.register("new-pc", object(), {"workspaces": {}, "mcps": {"device": {"tools": []}, "extra": {"tools": []}}})
    assert hub.mcp("old-pc/device")[:2] == (old, "workspace")
    assert hub.mcp("old-pc/workspace")[:2] == (old, "workspace")
    assert hub.mcp("new-pc/workspace")[:2] == (new, "device")
    with pytest.raises(RoutingError):
        hub.mcp("new-pc/other")


def test_gateway_health_and_media_route_names(tmp_path, monkeypatch):
    from agent_nexus.cloud import build_cloud_app

    monkeypatch.setenv("T_SERVER", "s" * 40)
    monkeypatch.setenv("T_AGENT", "a" * 40)
    app = build_cloud_app({"token_env": "T_SERVER", "agents": {"pc": "T_AGENT"}}, tmp_path)
    asked = {}

    class Peer:
        def __init__(self, agent_id, mcps):
            self.agent_id, self.catalog = agent_id, {"workspaces": {}, "mcps": mcps}

        async def call(self, method, params):
            asked[self.agent_id] = params["mcp"]
            return {"status": 404}

    app.state.hub.peers.update({"old": Peer("old", {"workspace": {}}), "new": Peer("new", {"device": {}})})
    with TestClient(app) as client:
        health = client.get("/healthz").json()
        assert {key: health[key] for key in ("status", "service")} == {"status": "ok", "service": "agent-nexus-gateway"}
        # Design §10: version, the gateway's features and the database schema number.
        from agent_nexus import __version__
        from agent_nexus.features import GATEWAY_FEATURES
        assert health["version"] == __version__
        assert health["features"] == list(GATEWAY_FEATURES)
        assert isinstance(health["db_schema"], int) and health["db_schema"] >= 1
        assert client.get("/media/" + "m" * 43).status_code == 404
    assert asked == {"old": "workspace", "new": "device"}
