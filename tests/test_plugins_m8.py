"""M8: installing plugins from a folder, a git repository and an archive (design §6.4, §6.5).

The fixture plugin is a stdlib HTTP server (``server.py``) with a ``/healthz``;
its ``mode`` makes it healthy, crash on start, answer 503, or exit like mcpkit
does when its settings are wrong (code 2, ``mcpkit: ...``).
"""
from __future__ import annotations

import io
import json
import shutil
import socket
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent_nexus import sections
from agent_nexus.plugins import installer, manage
from agent_nexus.plugins.installer import InstallError, Installer, Source
from agent_nexus.registry import CONFIG_NAME
from agent_nexus.supervisor import Supervisor
from agent_nexus.web import build_app

SERVER = r'''
import json, sys, os
from http.server import BaseHTTPRequestHandler, HTTPServer
port, mode = int(sys.argv[1]), sys.argv[2]
if mode == "crash":
    sys.exit(3)
if mode == "config":
    print("mcpkit: service 'fixture': settings.team_id: Field required", flush=True)
    sys.exit(2)
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        code = 200 if mode == "ok" else 503
        self.send_response(code); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_plugin(directory: Path, name: str = "fixture", version: str = "1.0.0", mode: str = "ok",
                port: int | None = None, **manifest) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "server.py").write_text(SERVER, encoding="utf-8")
    data = {"name": name, "version": version, "description": "测试插件",
            "command": [sys.executable, "server.py", "{port}", mode],
            "port": port or free_port(), "health": {"path": "/healthz", "interval": 5},
            "autostart": True, "autorestart": False, **manifest}
    (directory / "mcp-service.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return directory


@pytest.fixture
def root(tmp_path):
    base = tmp_path / "root"
    base.mkdir()
    (base / CONFIG_NAME).write_text(json.dumps({"manager": {"port": free_port()}, "plugin_paths": ["external"],
                                                "plugins": {}}), encoding="utf-8")
    return base


def installer_for(root: Path, sup=None) -> Installer:
    return Installer(root, sup or Supervisor(root), health_timeout=15)


def config(root: Path) -> dict:
    return json.loads((root / CONFIG_NAME).read_text(encoding="utf-8"))


def stop_all(sup: Supervisor) -> None:
    sup.shutdown()


# -- sources ---------------------------------------------------------------------------

def test_install_from_folder_copies_without_venv_and_runs(root, tmp_path):
    src = make_plugin(tmp_path / "src" / "fixture-mcp")
    (src / ".venv").mkdir()
    (src / ".venv" / "junk").write_text("x")
    (src / "logs").mkdir()
    (src / "logs" / "server.log").write_text("old log")
    (src / "logs" / "oauth-store.json").write_text("{}")
    sup = Supervisor(root)
    try:
        result = installer_for(root, sup).install(Source("path", path=str(src)))
        assert (result.name, result.version, result.state) == ("fixture", "1.0.0", "running")
        target = root / "plugins" / "fixture" / "1.0.0"
        assert (root / "plugins" / "fixture" / "current").read_text().strip() == "1.0.0"
        assert (target / "server.py").is_file() and not (target / ".venv").exists()
        assert not (target / "logs" / "server.log").exists() and (target / "logs" / "oauth-store.json").is_file()
        lock = sections.read_lock(root)["fixture"]
        assert lock["mode"] == "managed" and lock["source"]["type"] == "path" and lock["source"]["path"] == str(src)
        assert config(root)["plugins"]["fixture"]["enabled"] is True
        view = manage.plugin_view(sup, "fixture")
        assert view["status"] == "running" and view["source"]["type"] == "path" and view["removable"]
    finally:
        stop_all(sup)


def test_link_mode_runs_in_place(root, tmp_path):
    src = make_plugin(tmp_path / "dev" / "fixture")
    sup = Supervisor(root)
    try:
        result = installer_for(root, sup).install(Source("path", path=str(src), link=True))
        assert result.state == "running"
        assert not (root / "plugins" / "fixture").exists()
        assert sections.read_lock(root)["fixture"] == {**sections.read_lock(root)["fixture"], "mode": "link",
                                                       "path": str(src.resolve())}
        assert Path(sup.spec("fixture").dir) == src.resolve()
    finally:
        stop_all(sup)


def _bare_repo(tmp_path: Path, tag: str = "v1.2.0") -> Path:
    work = make_plugin(tmp_path / "repo-work", version=tag.removeprefix("v"))
    git = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "init.defaultBranch=main"]
    subprocess.run([*git, "init", "-q"], cwd=work, check=True)
    subprocess.run([*git, "add", "."], cwd=work, check=True)
    subprocess.run([*git, "commit", "-qm", "fixture"], cwd=work, check=True)
    subprocess.run([*git, "tag", tag], cwd=work, check=True)
    bare = tmp_path / "fixture.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    return bare


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_install_from_local_bare_git_repo_picks_the_highest_tag(root, tmp_path):
    bare = _bare_repo(tmp_path)
    source = installer.parse_source(str(bare))
    assert source.type == "git"
    sup = Supervisor(root)
    try:
        result = installer_for(root, sup).install(source)
        assert (result.version, result.state) == ("1.2.0", "running")
        record = sections.read_lock(root)["fixture"]
        assert record["source"]["type"] == "git" and record["source"]["ref"] == "v1.2.0"
        assert len(record["source"]["commit"]) == 40
        assert not (root / "plugins" / "fixture" / "1.2.0" / ".git").exists()
    finally:
        stop_all(sup)


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buffer.getvalue()


def test_install_from_zip_upload_through_the_console_api(root, tmp_path):
    src = make_plugin(tmp_path / "pkg" / "fixture")
    files = {f"fixture-1.0.0/{p.name}": p.read_text(encoding="utf-8") for p in src.iterdir()}
    sup = Supervisor(root)
    client = TestClient(build_app(sup), base_url=sup.cfg.base_url)
    headers = {"X-ANX": "1"}
    try:
        up = client.post("/api/plugins/uploads?filename=fixture.zip", content=_zip(files), headers=headers)
        assert up.status_code == 200, up.text
        upload_id = up.json()["upload_id"]
        seen = client.post("/api/plugins/inspect", json={"source": "archive", "upload_id": upload_id}, headers=headers)
        assert seen.status_code == 200 and seen.json()["name"] == "fixture" and seen.json()["exists"] is False
        started = client.post("/api/plugins/install", json={"source": "archive", "upload_id": upload_id}, headers=headers)
        assert started.status_code == 202
        job = _wait_job(client, started.json()["job_id"])
        assert job["state"] == "done", job
        assert job["result"]["state"] == "running" and job["plugin"]["source"]["type"] == "archive"
        listed = client.get("/api/plugins").json()["plugins"]
        assert [p["status"] for p in listed if p["name"] == "fixture"] == ["running"]
        assert not (root / "plugins" / ".staging" / "uploads" / upload_id).exists()
    finally:
        stop_all(sup)


def _wait_job(client: TestClient, job_id: str, timeout: float = 60) -> dict:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/plugins/jobs/{job_id}").json()
        if job["state"] != "running":
            return job
        time.sleep(0.2)
    raise AssertionError("install job did not finish")


@pytest.mark.parametrize("bad", ["../evil.txt", "/abs.txt", "C:/win.txt", "a/../../evil.txt"])
def test_archive_with_path_traversal_is_rejected(root, tmp_path, bad):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(_zip({"fixture/mcp-service.json": "{}", bad: "x"}))
    with pytest.raises(InstallError) as info:
        installer_for(root).install(Source("archive", path=str(archive)))
    assert info.value.code == "unsafe_archive"
    assert not (tmp_path / "evil.txt").exists() and not (root / "plugins" / "fixture").exists()


def test_tar_with_a_symlink_is_rejected(root, tmp_path):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        info = tarfile.TarInfo("fixture/link")
        info.type, info.linkname = tarfile.SYMTYPE, "../../outside"
        tf.addfile(info)
    with pytest.raises(InstallError) as caught:
        installer_for(root).install(Source("archive", path=str(archive)))
    assert caught.value.code == "unsafe_archive"


def test_tar_gz_installs(root, tmp_path):
    src = make_plugin(tmp_path / "t" / "fixture")
    archive = tmp_path / "fixture.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(src, arcname="fixture")
    sup = Supervisor(root)
    try:
        result = installer_for(root, sup).install(installer.parse_source(str(archive)))
        assert result.state == "running"
        assert len(sections.read_lock(root)["fixture"]["source"]["sha256"]) == 64
    finally:
        stop_all(sup)


# -- failures roll back ----------------------------------------------------------------

def test_failed_health_check_rolls_a_fresh_install_back(root, tmp_path):
    src = make_plugin(tmp_path / "s" / "fixture", mode="crash")
    before = config(root)
    with pytest.raises(InstallError) as info:
        installer_for(root).install(Source("path", path=str(src)))
    assert info.value.code == "health_failed"
    assert not (root / "plugins" / "fixture").exists()
    assert "fixture" not in sections.read_lock(root)
    assert config(root) == before


def test_unhealthy_update_rolls_back_to_the_previous_version(root, tmp_path):
    good = make_plugin(tmp_path / "v1" / "fixture", version="1.0.0")
    port = json.loads((good / "mcp-service.json").read_text(encoding="utf-8"))["port"]
    bad = make_plugin(tmp_path / "v2" / "fixture", version="2.0.0", mode="unhealthy", port=port)
    sup = Supervisor(root)
    inst = Installer(root, sup, health_timeout=8)
    try:
        assert inst.install(Source("path", path=str(good))).state == "running"
        (root / "plugin-data" / "fixture").mkdir(parents=True)
        (root / "plugin-data" / "fixture" / "state.json").write_text("{}")
        with pytest.raises(InstallError) as info:
            inst.install(Source("path", path=str(bad)))
        assert info.value.code == "health_failed"
        home = root / "plugins" / "fixture"
        assert (home / "current").read_text().strip() == "1.0.0"
        assert not (home / "2.0.0").exists() and (home / "1.0.0").is_dir()
        assert sections.read_lock(root)["fixture"]["version"] == "1.0.0"
        assert (root / "plugin-data" / "fixture" / "state.json").is_file()
        sup.tick()
        assert sup.snapshot("fixture")["status"] in ("running", "starting")
    finally:
        stop_all(sup)


def test_failed_install_step_rolls_back(root, tmp_path):
    src = make_plugin(tmp_path / "s" / "fixture", schema_version=2,
                      install={"steps": [{"run": [sys.executable, "-c", "import sys; print('boom'); sys.exit(4)"]}]})
    with pytest.raises(InstallError) as info:
        installer_for(root).install(Source("path", path=str(src)))
    assert info.value.code == "step_failed" and "boom" in str(info.value)
    assert not (root / "plugins" / "fixture").exists()


def test_install_steps_run_in_the_version_directory(root, tmp_path):
    step = [sys.executable, "-c", "import os, pathlib; pathlib.Path('built.txt').write_text(os.environ['ANX_PLUGIN_ID'])"]
    src = make_plugin(tmp_path / "s" / "fixture", schema_version=2, install={"steps": [{"run": step}]})
    sup = Supervisor(root)
    try:
        installer_for(root, sup).install(Source("path", path=str(src)), enable=False)
        assert (root / "plugins" / "fixture" / "1.0.0" / "built.txt").read_text() == "fixture"
        assert sup.snapshot("fixture")["status"] == "disabled"
    finally:
        stop_all(sup)


def test_device_cannot_be_installed_or_uninstalled(root, tmp_path):
    src = make_plugin(tmp_path / "s" / "device", name="device")
    with pytest.raises(InstallError) as info:
        installer_for(root).install(Source("path", path=str(src)))
    assert info.value.code == "builtin"
    with pytest.raises(InstallError):
        installer_for(root).uninstall("device")


# -- configuration ---------------------------------------------------------------------

V2_CONFIG = {
    "schema_version": 2,
    "config": {"schema": {"type": "object", "required": ["team"], "properties": {
        "team": {"type": "string", "title": "团队", "minLength": 1},
        "level": {"type": "string", "enum": ["a", "b"], "default": "a"},
        "retries": {"type": "integer", "minimum": 0, "default": 1}}}},
    "secrets": [{"name": "token", "title": "Token", "required": True},
                {"name": "optional-key", "title": "可选", "required": False}],
}


def test_missing_required_config_means_needs_config_and_secrets_are_write_only(root, tmp_path):
    src = make_plugin(tmp_path / "s" / "fixture", **V2_CONFIG)
    sup = Supervisor(root)
    client = TestClient(build_app(sup), base_url=sup.cfg.base_url)
    headers = {"X-ANX": "1"}
    secret = "tok-" + "s3cr3t-value-never-shown"
    try:
        result = installer_for(root, sup).install(Source("path", path=str(src)))
        assert result.state == "needs_config"
        assert sup.snapshot("fixture")["status"] == "needs_config" and sup.rt["fixture"].pid is None
        got = client.get("/api/plugins/fixture/config").json()
        assert got["missing"] == ["team", "token"]
        assert [s["set"] for s in got["secrets"]] == [False, False]
        bad = client.put("/api/plugins/fixture/config", json={"values": {"retries": -1}}, headers=headers)
        assert bad.status_code == 422 and bad.json()["problems"][0]["path"] == "retries"
        saved = client.put("/api/plugins/fixture/config", headers=headers,
                           json={"values": {"team": "core", "level": "b"}, "secrets": {"token": secret}})
        assert saved.status_code == 200, saved.text
        assert saved.json()["plugin"]["status"] in ("starting", "running")
        assert (root / "secrets" / "fixture" / "token").read_text().strip() == secret
        again = client.get("/api/plugins/fixture/config")
        assert secret not in again.text and again.json()["secrets"][0]["set"] is True
        assert again.json()["values"] == {"team": "core", "level": "b", "retries": 1}
        assert secret not in (root / CONFIG_NAME).read_text(encoding="utf-8")
        assert config(root)["plugins"]["fixture"]["settings"] == {"team": "core", "level": "b"}
        # clearing a required secret stops the plugin and brings needs_config back
        cleared = client.put("/api/plugins/fixture/config", headers=headers, json={"secrets": {"token": None}})
        assert cleared.json()["plugin"]["status"] == "needs_config"
    finally:
        stop_all(sup)


def test_mcpkit_config_exit_shows_needs_config_not_a_crash(root, tmp_path):
    src = make_plugin(tmp_path / "s" / "fixture", mode="config")
    sup = Supervisor(root)
    try:
        result = installer_for(root, sup).install(Source("path", path=str(src)))
        assert result.state == "needs_config" and "team_id" in result.message
        assert (root / "plugins" / "fixture" / "current").is_file()  # kept: it only needs configuration
        sup.tick()
        snap = sup.snapshot("fixture")
        assert snap["status"] == "needs_config" and snap["crashes"] == 0
    finally:
        stop_all(sup)


def test_legacy_hints_give_v1_plugins_a_form(root):
    from agent_nexus.plugins import schema
    info = schema.effective("ones", {"name": "ones", "command": ["x"]})
    assert info["hinted"] and [s["name"] for s in info["secrets"]] == ["token"]
    assert schema.missing(root, "ones", {"name": "ones"}, {}) == ["team_id", "token"]
    (root / "secrets" / "ones").mkdir(parents=True)
    (root / "secrets" / "ones" / "token").write_text("x" * 20)
    assert schema.missing(root, "ones", {"name": "ones"}, {"settings": {"team_id": "T"}}) == []
    # a manifest that declares its own config wins over the hint
    own = schema.effective("ones", {"config": {"schema": {"type": "object", "properties": {}}}, "secrets": []})
    assert not own["hinted"] and own["secrets"] == []


def test_secret_with_setting_writes_a_reference_not_the_value(root, tmp_path):
    src = make_plugin(tmp_path / "s" / "fixture", schema_version=2, config={"schema": {"type": "object", "properties": {
        "username": {"type": "string"}}}}, secrets=[{"name": "password", "required": True, "setting": "password"}])
    sup = Supervisor(root)
    try:
        installer_for(root, sup).install(Source("path", path=str(src)))
        manage.put_config(sup, "fixture", {"values": {"username": "u"}, "secrets": {"password": "p@ss-word"}})
        entry = config(root)["plugins"]["fixture"]
        assert entry["settings"] == {"username": "u", "password": {"$secret": "fixture/password"}}
        assert "p@ss-word" not in json.dumps(manage.get_config(sup, "fixture"))
    finally:
        stop_all(sup)


# -- uninstall, template entries --------------------------------------------------------

def test_uninstall_keeps_data_unless_purge(root, tmp_path):
    src = make_plugin(tmp_path / "s" / "fixture", **V2_CONFIG)
    sup = Supervisor(root)
    try:
        inst = installer_for(root, sup)
        inst.install(Source("path", path=str(src)))
        manage.put_config(sup, "fixture", {"values": {"team": "t"}, "secrets": {"token": "keep-me-token"}})
        (root / "plugin-data" / "fixture").mkdir(parents=True)
        (root / "plugin-data" / "fixture" / "db.sqlite3").write_text("x")
        report = inst.uninstall("fixture")
        assert report["data_kept"] and not (root / "plugins" / "fixture").exists()
        assert "fixture" not in sections.read_lock(root)
        assert (root / "plugin-data" / "fixture" / "db.sqlite3").is_file()
        assert (root / "secrets" / "fixture" / "token").is_file()
        assert config(root)["plugins"]["fixture"] == {"settings": {"team": "t"}}
        sup.rescan()
        assert manage.plugin_view(sup, "fixture")["status"] == "not_installed"
        # reinstalling picks the kept settings and secret up again
        assert inst.install(Source("path", path=str(src))).state == "running"
        inst.uninstall("fixture", purge=True)
        assert not (root / "plugin-data" / "fixture").exists() and not (root / "secrets" / "fixture").exists()
        assert "fixture" not in config(root)["plugins"]
    finally:
        stop_all(sup)


def test_template_entries_without_code_show_not_installed(root, tmp_path):
    data = config(root)
    data.pop("plugins")
    data["services"] = {"ones": {"autostart": False, "enabled": False}, "unity": {"enabled": False}}
    (root / CONFIG_NAME).write_text(json.dumps(data), encoding="utf-8")
    sup = Supervisor(root)
    sup.tick()
    views = {v["name"]: v for v in manage.plugin_views(sup)}
    assert views["ones"]["status"] == "not_installed" and views["ones"]["error"] == ""
    assert views["ones"]["installed"] is False and views["ones"]["removable"] is True
    assert views["ones"]["title"] == "ONES"
    client = TestClient(build_app(sup), base_url=sup.cfg.base_url)
    refused = client.post("/api/plugins/ones/enable", headers={"X-ANX": "1"})
    assert refused.status_code == 409 and "尚未安装" in refused.json()["error"]
    # installing over a template entry turns it on and drops its stale switches
    src = make_plugin(tmp_path / "s" / "ones-mcp", name="ones", mode="config")
    result = installer_for(root, sup).install(Source("path", path=str(src)))
    assert result.state == "needs_config"
    assert config(root)["services"]["ones"] == {"autostart": True, "enabled": True}
    stop_all(sup)


def test_relative_mcpkit_dependency_is_provided_next_to_the_install(root, tmp_path):
    src = make_plugin(tmp_path / "s" / "fixture")
    (src / "pyproject.toml").write_text('[project]\nname = "fixture"\nversion = "1.0.0"\n'
                                        '[tool.uv.sources]\nmcpkit = { path = "../mcpkit", editable = true }\n',
                                        encoding="utf-8")
    target = root / "plugins" / "fixture" / "1.0.0"
    shutil.copytree(src, target)
    lines: list[str] = []
    provided = installer.provide_path_dependencies(root, "fixture", target, {"name": "fixture"}, src, lines.append)
    assert provided == ["mcpkit"]
    assert (root / "plugins" / "fixture" / "mcpkit" / "pyproject.toml").is_file()
    assert not (root / "plugins" / "fixture" / "mcpkit" / "tests").exists()
    steps = installer.install_steps(target, {"name": "fixture", "command": ["uv", "run", "--extra", "mcp", "x"]}, root)
    assert steps[-1][0] == ["uv", "sync", "--extra", "mcp"]


def test_cli_parses_sources(tmp_path):
    folder = make_plugin(tmp_path / "f")
    assert installer.parse_source(str(folder)).type == "path"
    assert installer.parse_source("https://example.invalid/x/anx-plugin-ones.git", ref="v1").ref == "v1"
    assert installer.parse_source("git@example.invalid:o/r.git").type == "git"
    assert installer.parse_source("https://example.invalid/p.zip").type == "archive"
    with pytest.raises(InstallError):
        installer.parse_source(str(tmp_path / "nowhere"))
