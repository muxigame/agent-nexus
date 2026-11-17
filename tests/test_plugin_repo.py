"""The gateway's plugin repository (插件库, design §6.12; the owner's revision of decision D4).

* ``anx plugin pack``: what is left out, the secret scan (no override), pack.json;
* the repository: storage, the index, owner APIs, device downloads, the gateway CLI;
* end to end, with real processes on ephemeral ports and temporary roots: an
  ``anx-gateway`` subprocess and an ``anx serve`` manager subprocess joined to
  it with an invite. A fixture plugin is packed, uploaded through the owner API
  and installed with ``POST /api/devices/{id}/plugins/install`` until it runs;
  plus unauthenticated / revoked downloads, a tampered package, an oversize
  one, secrets on pack and on upload, and an old device without ``plugin_repo/1``.

Nothing here touches a live port, the user's installed root, or the network beyond loopback.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from agent_nexus.cloud import build_cloud_app
from agent_nexus.plugins import installer, pack
from agent_nexus.plugins.pack import PackError
from agent_nexus.store import MIGRATIONS, GatewayStore

from test_plugins_m8 import free_port, make_plugin

SERVER_TOKEN = "repo-server-token-" + "s" * 30
LEGACY_TOKEN = "repo-legacy-token-" + "l" * 30
FAKE_KEY = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEpAIBAAKCAQEA7\n-----END RSA PRIVATE KEY-----\n"
SCRIPTS = Path(sys.executable).parent


def zip_bytes(files: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def names_in(path: str | Path) -> set[str]:
    with zipfile.ZipFile(path) as zf:
        return {item.filename for item in zf.infolist()}


# -- anx plugin pack ------------------------------------------------------------------------

def plugin_with_clutter(base: Path) -> Path:
    folder = make_plugin(base / "fixture-mcp", data=["qrcodes/", "state.json"],
                         env={"FIXTURE_CACHE": "{dir}/runtime/cache"})
    for rel, text in {
        ".venv/pyvenv.cfg": "home = x", ".git/config": "[core]", "node_modules/x/index.js": "1",
        "pkg/__pycache__/m.cpython-312.pyc": "x", ".pytest_cache/v": "x", "logs/run.log": "x",
        "secrets/token": "x", "config.json": "{}", "config.example.json": "{}", "state.sqlite3": "x",
        "state.sqlite3-wal": "x", "debug.log": "x", ".env": "A=1", ".env.local": "A=1", "wxwork_keys.json": "{}",
        "oauth-store.json": "{}", "qrcodes/pay.jpg": "x", "state.json": "{}", "runtime/cache/a": "x",
        "tests/test_x.py": "token = 'sk-" + "A1b2C3d4" * 5 + "'", "ignored.tmp": "x", "build/out.txt": "x",
        "pkg/__init__.py": "", "pkg/server.py": "print('ok')", "README.md": "# fixture",
        ".gitignore": "*.tmp\nbuild/\n!keep.tmp\n", "keep.tmp": "kept by negation",
    }.items():
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return folder


def test_pack_leaves_out_environments_owner_data_and_ignored_files(tmp_path):
    folder = plugin_with_clutter(tmp_path / "src")
    before = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*"))
    result = pack.pack(folder, tmp_path / "out" / "fixture.zip")
    assert sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")) == before  # source untouched
    assert result["name"] == "fixture" and result["version"] == "1.0.0"
    assert names_in(result["path"]) == {"mcp-service.json", "server.py", "config.example.json", "pkg/__init__.py",
                                        "pkg/server.py", "README.md", ".gitignore", "keep.tmp", "pack.json"}
    rules = result["excluded"]["rules"]
    for rule in (".venv/", ".git/", "node_modules/", "__pycache__/", "logs/", "secrets/", "config.json", "*.sqlite3",
                 ".env", "*keys*.json", "oauth*.json", "数据 qrcodes/", "数据 state.json", "数据 runtime/",
                 "不需要 tests/", ".gitignore"):
        assert rule in rules, rule
    with zipfile.ZipFile(result["path"]) as zf:
        info = json.loads(zf.read("pack.json"))
    assert info["name"] == "fixture" and info["version"] == "1.0.0" and info["files"] == 8
    assert len(info["content_sha256"]) == 64 and info["created_at"].endswith("Z")
    # Same input, same content digest; the zip itself is checked by sha256 on install.
    again = pack.pack(folder, tmp_path / "out" / "again.zip")
    assert again["content_sha256"] == result["content_sha256"]
    assert result["sha256"] == installer.sha256_of(Path(result["path"]))


@pytest.mark.parametrize("rel, text, rule", [
    ("src/settings.py", FAKE_KEY, "私钥"),
    ("deploy/notes.txt", "aws AKIA" + "ABCDEFGHIJKLMNOP", "AWS 访问密钥"),
    ("scripts/push.ps1", "$t = 'ghp_" + "a1B2" * 9 + "'", "GitHub 令牌"),
    ("settings.toml", 'api_key = "Zq8' + 'xW2vL9pR4tY7uE1"', "疑似凭据赋值"),
    ("id_rsa", "anything", "凭据文件名"),
    ("certs/server.pem", "anything", "凭据文件名"),
])
def test_pack_refuses_secret_looking_content_and_names(tmp_path, rel, text, rule):
    folder = make_plugin(tmp_path / "src" / "fixture-mcp")
    (folder / rel).parent.mkdir(parents=True, exist_ok=True)
    (folder / rel).write_text(text, encoding="utf-8")
    with pytest.raises(PackError) as failure:
        pack.pack(folder, tmp_path / "x.zip")
    assert failure.value.code == "secrets_found"
    assert any(f["path"] == rel and f["rule"].startswith(rule) for f in failure.value.findings), failure.value.findings
    assert "MIIEpAIBAAKCAQEA7" not in str(failure.value) and "ABCDEFGHIJKLMNOP" not in str(failure.value)
    assert not (tmp_path / "x.zip").exists()


def test_pack_allows_placeholders_and_has_no_force(tmp_path):
    folder = make_plugin(tmp_path / "src" / "fixture-mcp")
    (folder / "config.example.json").write_text(json.dumps({"password": "your-password-here", "token": "<token>",
                                                           "token_env": "ANX_AGENT_TOKEN"}), encoding="utf-8")
    pack.pack(folder, tmp_path / "ok.zip")
    import inspect
    assert "force" not in inspect.signature(pack.pack).parameters
    from agent_nexus.cli import build_parser
    with pytest.raises(SystemExit):
        build_parser().parse_args(["plugin", "pack", str(folder), "--force"])


def test_pack_version_from_pyproject_and_v2_manifest(tmp_path):
    folder = make_plugin(tmp_path / "src" / "legacy-mcp", name="legacy", version="")
    manifest = json.loads((folder / "mcp-service.json").read_text(encoding="utf-8"))
    manifest.pop("version")
    (folder / "mcp-service.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "pyproject.toml").write_text('[project]\nname = "legacy"\nversion = "2.3.4"\n', encoding="utf-8")
    result = pack.pack(folder, tmp_path / "legacy.zip")
    assert result["version"] == "2.3.4"
    checked = pack.inspect_package(result["path"])
    assert (checked["name"], checked["version"]) == ("legacy", "2.3.4")


def test_inspect_package_rejects_bad_uploads(tmp_path):
    manifest = json.dumps({"name": "fixture", "version": "1.0.0", "command": ["python", "x.py"]})
    cases = {
        "secret": ({"mcp-service.json": manifest, "notes.txt": FAKE_KEY}, "secrets_found"),
        "config": ({"mcp-service.json": manifest, "config.json": "{}"}, "secrets_found"),
        "logs": ({"mcp-service.json": manifest, "logs/a.txt": "x"}, "secrets_found"),
        "traversal": ({"mcp-service.json": manifest, "../evil.txt": "x"}, "unsafe_archive"),
        "nomanifest": ({"x.txt": "x"}, "invalid_manifest"),
        "device": ({"mcp-service.json": json.dumps({"name": "device", "version": "1", "command": ["x"]})}, "builtin"),
        "noversion": ({"mcp-service.json": json.dumps({"name": "fixture", "command": ["x"]})}, "invalid_manifest"),
    }
    for label, (files, code) in cases.items():
        path = tmp_path / f"{label}.zip"
        path.write_bytes(zip_bytes(files))
        with pytest.raises(PackError) as failure:
            pack.inspect_package(path)
        assert failure.value.code == code, label
    big = tmp_path / "big.zip"
    big.write_bytes(zip_bytes({"mcp-service.json": manifest, "blob.bin": os.urandom(300_000)}))
    with pytest.raises(PackError) as failure:
        pack.inspect_package(big, max_bytes=100_000)
    assert failure.value.code == "too_large"
    (tmp_path / "plain.txt").write_text("not a zip")
    with pytest.raises(PackError):
        pack.inspect_package(tmp_path / "plain.txt")


def test_semver_order():
    versions = ["1.0.0", "1.10.0", "1.2.0", "1.2.0-rc1", "0.9"]
    assert sorted(versions, key=pack.semver_key) == ["0.9", "1.0.0", "1.2.0-rc1", "1.2.0", "1.10.0"]


def test_store_migration_adds_the_package_index(tmp_path):
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    assert store.schema_version() == len(MIGRATIONS)
    store.package_put({"name": "a", "version": "1.0.0", "sha256": "0" * 64, "size": 1, "files": 1, "info": {"x": 1}})
    assert store.package("a", "1.0.0")["info"] == {"x": 1}
    assert [row["version"] for row in store.packages("a")] == ["1.0.0"]
    assert store.package_delete("a", "1.0.0") and store.packages() == []
    store.close()


# -- the repository through the owner API (in process) ------------------------------------------

@pytest.fixture
def gateway_app(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_SERVER_TOKEN", SERVER_TOKEN)
    monkeypatch.setenv("REPO_LEGACY_TOKEN", LEGACY_TOKEN)
    config = {"token_env": "REPO_SERVER_TOKEN", "agents": {"legacy-pc": "REPO_LEGACY_TOKEN"},
              "public_url": "http://127.0.0.1:1", "state_dir": str(tmp_path / "state"),
              "plugin_repo": {"max_package_mb": 1}}
    app = build_cloud_app(config, tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:1") as client:
        client.headers["Authorization"] = f"Bearer {SERVER_TOKEN}"
        yield client, app, tmp_path


def test_owner_api_upload_list_download_delete(gateway_app):
    client, app, tmp_path = gateway_app
    folder = make_plugin(tmp_path / "src" / "fixture-mcp")
    package = pack.pack(folder, tmp_path / "fixture.zip")
    data = Path(package["path"]).read_bytes()
    created = client.post("/api/plugin-repo", content=data, headers={"Content-Type": "application/zip"})
    assert created.status_code == 201, created.text
    assert created.json()["package"]["sha256"] == package["sha256"]
    stored = tmp_path / "state" / "plugin-repo" / "fixture" / "1.0.0" / "fixture-1.0.0.zip"
    assert stored.read_bytes() == data
    assert client.post("/api/plugin-repo", content=data).status_code == 409  # duplicates need ?replace=1
    assert client.post("/api/plugin-repo?replace=1", content=data).status_code == 200
    # multipart works too
    second = pack.pack(make_plugin(tmp_path / "src2" / "fixture-mcp", version="1.1.0"), tmp_path / "f2.zip")
    multipart = client.post("/api/plugin-repo", files={"file": ("f2.zip", Path(second["path"]).read_bytes(), "application/zip")})
    assert multipart.status_code == 201, multipart.text
    listing = client.get("/api/plugin-repo").json()
    assert [(p["version"], p["latest"]) for p in listing["packages"]] == [("1.1.0", True), ("1.0.0", False)]
    assert listing["packages"][0]["title"] == "fixture" and listing["max_package_mb"] == 1
    download = client.get("/api/plugin-repo/fixture/1.0.0/download")
    assert download.status_code == 200 and download.content == data
    assert download.headers["x-anx-sha256"] == package["sha256"]
    # Private: the public downloads page and anonymous callers never see packages.
    anonymous = TestClient(app, base_url="http://127.0.0.1:1")
    assert anonymous.get("/api/plugin-repo").status_code == 401
    assert anonymous.get("/api/plugin-repo/fixture/1.0.0/download").status_code == 401
    assert anonymous.get("/api/plugin-repo/fixture/1.0.0/download", headers={"Authorization": "Bearer " + "x" * 40}).status_code == 401
    assert anonymous.post("/api/plugin-repo", content=data).status_code == 401
    assert "fixture" not in json.dumps(anonymous.get("/api/downloads").json())
    assert anonymous.get("/downloads/fixture-1.0.0.zip").status_code == 404
    # No traversal: names and versions are patterns, never paths.
    for path in ("/api/plugin-repo/..%2F..%2Fgateway/1/download", "/api/plugin-repo/fixture/..%2F..%2F..%2Fx/download",
                 "/api/plugin-repo/fixture/%2E%2E/download"):
        assert client.get(path).status_code in (400, 404, 405), path
    # The env-map device token may download; deleting needs the owner.
    legacy = {"Authorization": f"Bearer {LEGACY_TOKEN}"}
    assert anonymous.get("/api/plugin-repo/fixture/1.0.0/download", headers=legacy).status_code == 200
    assert anonymous.delete("/api/plugin-repo/fixture/1.0.0", headers=legacy).status_code == 401
    assert client.delete("/api/plugin-repo/fixture/1.0.0").json()["removed"]
    assert not stored.exists()
    assert client.delete("/api/plugin-repo/fixture/1.0.0").status_code == 404


def test_upload_rejects_secrets_oversize_and_bad_zips(gateway_app):
    client, _, tmp_path = gateway_app
    manifest = json.dumps({"name": "fixture", "version": "1.0.0", "command": ["python", "x.py"]})
    leaked = client.post("/api/plugin-repo", content=zip_bytes({"mcp-service.json": manifest, "deploy.txt": FAKE_KEY}))
    assert leaked.status_code == 422 and leaked.json()["code"] == "secrets_found"
    assert leaked.json()["findings"] == [{"path": "deploy.txt", "rule": "私钥"}]
    assert "MIIEpAIBAAKCAQEA7" not in leaked.text
    big = zip_bytes({"mcp-service.json": manifest, "blob.bin": os.urandom(1_200_000)})
    oversize = client.post("/api/plugin-repo", content=big)
    assert oversize.status_code == 413 and oversize.json()["code"] == "too_large"
    assert client.post("/api/plugin-repo", content=b"not a zip").json()["code"] == "invalid_archive"
    assert client.post("/api/plugin-repo", content=b"").status_code == 400
    assert client.get("/api/plugin-repo").json()["packages"] == []
    incoming = tmp_path / "state" / "plugin-repo" / ".incoming"
    assert not any(incoming.iterdir())  # refused uploads leave nothing behind


def test_install_on_an_unknown_or_offline_device_is_refused_in_chinese(gateway_app):
    client, _, _ = gateway_app
    missing = client.post("/api/devices/nobody/plugins/install", json={"name": "fixture"})
    assert missing.status_code == 404 and missing.json()["code"] == "unknown_device", missing.text
    assert "没有这台设备" in missing.json()["error"] and "不在线" not in missing.json()["error"]


def test_gateway_cli_add_list_remove(tmp_path, monkeypatch, capsys):
    from agent_nexus.cloud import main as gateway_main
    monkeypatch.setenv("REPO_SERVER_TOKEN", SERVER_TOKEN)
    config = tmp_path / "gateway.json"
    config.write_text(json.dumps({"token_env": "REPO_SERVER_TOKEN", "state_dir": str(tmp_path / "state")}), encoding="utf-8")
    package = pack.pack(make_plugin(tmp_path / "src" / "fixture-mcp"), tmp_path / "fixture.zip")
    assert gateway_main(["plugin-repo", "add", "--config", str(config), package["path"]]) == 0
    assert Path(package["path"]).exists()  # the owner's file is copied, not moved
    assert gateway_main(["plugin-repo", "add", "--config", str(config), package["path"]]) == 1
    assert gateway_main(["plugin-repo", "list", "--config", str(config), "--json"]) == 0
    out = capsys.readouterr().out
    assert '"name": "fixture"' in out and package["sha256"] in out
    assert gateway_main(["plugin-repo", "remove", "--config", str(config), "fixture", "1.0.0"]) == 0
    leaked = tmp_path / "leak.zip"
    leaked.write_bytes(zip_bytes({"mcp-service.json": json.dumps({"name": "x", "version": "1", "command": ["x"]}),
                                  "k.txt": FAKE_KEY}))
    assert gateway_main(["plugin-repo", "add", "--config", str(config), str(leaked)]) == 1
    assert "私钥" in capsys.readouterr().err


def test_cli_pack_prints_size_files_and_exclusions(tmp_path, capsys):
    from agent_nexus.cli import main
    folder = plugin_with_clutter(tmp_path / "src")
    out = tmp_path / "pkg.zip"
    assert main(["--root", str(tmp_path), "plugin", "pack", str(folder), "-o", str(out)]) == 0
    text = capsys.readouterr().out
    assert "fixture 1.0.0" in text and "files" in text and ".venv/" in text and out.is_file()
    (folder / "leak.txt").write_text(FAKE_KEY, encoding="utf-8")
    assert main(["--root", str(tmp_path), "plugin", "pack", str(folder), "-o", str(tmp_path / "bad.zip")]) == 1
    assert "leak.txt" in capsys.readouterr().err


# -- end to end: a real gateway process and a real joined manager --------------------------------

def _exe(name: str) -> list[str]:
    for candidate in (SCRIPTS / f"{name}.exe", SCRIPTS / name):
        if candidate.is_file():
            return [str(candidate)]
    module = {"anx-gateway": "agent_nexus.cloud", "anx": "agent_nexus.cli"}[name]
    return [sys.executable, "-m", module]


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANX_", "MCPM_"))}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", **extra)
    return env


def _wait_http(url: str, process: subprocess.Popen, log: Path, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert process.poll() is None, log.read_text(encoding="utf-8", errors="replace")[-4000:]
        try:
            if httpx.get(url, timeout=2, trust_env=False, headers={"X-ANX": "1"}).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"{url} did not come up\n" + log.read_text(encoding="utf-8", errors="replace")[-4000:])


@contextlib.contextmanager
def world(tmp_path: Path, *, max_mb: int = 2):
    """An anx-gateway process and an anx serve manager joined to it with an invite."""
    gw_port, mgr_port = free_port(), free_port()
    origin = f"http://127.0.0.1:{gw_port}"
    state = tmp_path / "gw-state"
    config = tmp_path / "gw" / "gateway.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "host": "127.0.0.1", "port": gw_port, "public_url": origin, "token_env": "REPO_SERVER_TOKEN",
        "agents": {"legacy-pc": "REPO_LEGACY_TOKEN"}, "state_dir": str(state), "rpc_timeout_s": 60,
        "plugin_repo": {"max_package_mb": max_mb}}), encoding="utf-8")
    gw_log = tmp_path / "gateway.log"
    root = tmp_path / "device-root"
    root.mkdir()
    (root / "agent-nexus.json").write_text(json.dumps({
        "manager": {"host": "127.0.0.1", "port": mgr_port}, "builtins": False, "scan": [], "plugins": {},
        "cloud": {"enabled": False, "workspaces": {}, "refresh_interval_s": 5}}), encoding="utf-8")
    mgr_log = tmp_path / "manager.log"
    fake_shell = tmp_path / "agent-nexus.exe"
    fake_shell.write_bytes(b"MZ")
    processes = []
    owner = httpx.Client(base_url=origin, trust_env=False, timeout=30, headers={"Authorization": f"Bearer {SERVER_TOKEN}"})
    manager = httpx.Client(base_url=f"http://127.0.0.1:{mgr_port}", trust_env=False, timeout=60, headers={"X-ANX": "1"})
    try:
        with open(gw_log, "wb") as gw_out, open(mgr_log, "wb") as mgr_out:
            gateway = subprocess.Popen([*_exe("anx-gateway"), "--config", str(config)], cwd=config.parent,
                                       env=_env(REPO_SERVER_TOKEN=SERVER_TOKEN, REPO_LEGACY_TOKEN=LEGACY_TOKEN),
                                       stdout=gw_out, stderr=subprocess.STDOUT)
            processes.append(gateway)
            _wait_http(origin + "/healthz", gateway, gw_log)
            device = subprocess.Popen([*_exe("anx"), "--root", str(root), "serve", "--no-autostart"], cwd=root,
                                      env=_env(ANX_DESKTOP_APP=str(fake_shell)), stdout=mgr_out, stderr=subprocess.STDOUT)
            processes.append(device)
            _wait_http(f"http://127.0.0.1:{mgr_port}/api/ping", device, mgr_log)
            invite = owner.post("/api/invites", json={"label": "repo e2e"}).json()
            joined = manager.post("/api/cloud/join", json={"invite": invite["code"], "agent_id": "repo-device"})
            assert joined.status_code == 200, joined.text
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and manager.get("/api/cloud").json().get("state") != "connected":
                time.sleep(0.2)
            assert manager.get("/api/cloud").json()["state"] == "connected", mgr_log.read_text(errors="replace")[-3000:]
            yield {"origin": origin, "owner": owner, "manager": manager, "root": root, "state": state,
                   "agent_id": "repo-device", "gw_log": gw_log, "mgr_log": mgr_log}
    finally:
        with contextlib.suppress(Exception):
            manager.post("/api/manager/shutdown", content=b"")
        for index, process in reversed(list(enumerate(processes))):
            if index == 1:  # the manager stops its plugins and exits on its own
                with contextlib.suppress(Exception):
                    process.wait(timeout=30)
            if process.poll() is None:
                process.terminate()
                with contextlib.suppress(Exception):
                    process.wait(timeout=10)
            if process.poll() is None:
                process.kill()
        owner.close()
        manager.close()


def wait_job(owner: httpx.Client, device: str, job_id: str, timeout: float = 120) -> dict:
    deadline, since, log = time.monotonic() + timeout, 0, []
    while time.monotonic() < deadline:
        response = owner.get(f"/api/devices/{device}/plugins/jobs/{job_id}", params={"since": since})
        assert response.status_code == 200, response.text
        job = response.json()
        log += job["log"]
        since = job["log_total"]
        if job["state"] != "running":
            return {**job, "log": log}
        time.sleep(0.4)
    raise AssertionError(f"job {job_id} still running: {log[-10:]}")


def test_end_to_end_pack_upload_and_install_from_the_gateway(tmp_path):
    folder = make_plugin(tmp_path / "src" / "fixture-mcp", description="插件库端到端测试")
    with world(tmp_path) as w:
        owner, manager, agent_id = w["owner"], w["manager"], w["agent_id"]
        devices = owner.get("/api/devices").json()["devices"]
        assert "plugin_repo/1" in next(d for d in devices if d["device_id"] == agent_id)["features"]
        assert "plugin_repo/1" in manager.get("/api/cloud").json()["features"]

        package = pack.pack(folder, tmp_path / "pkgs" / "fixture-1.0.0.zip")
        uploaded = owner.post("/api/plugin-repo", content=Path(package["path"]).read_bytes(),
                              headers={"Content-Type": "application/zip"})
        assert uploaded.status_code == 201, uploaded.text

        # The local console's 插件库 tab sees it, not installed yet.
        local = manager.get("/api/plugin-repo")
        assert local.status_code == 200, local.text
        [item] = local.json()["packages"]
        assert item["name"] == "fixture" and item["installed_version"] is None and not item["update_available"]

        started = owner.post(f"/api/devices/{agent_id}/plugins/install", json={"name": "fixture"})
        assert started.status_code == 202, started.text
        assert started.json()["version"] == "1.0.0"
        job = wait_job(owner, agent_id, started.json()["job_id"])
        assert job["state"] == "done", job
        assert job["result"]["state"] == "running", job
        assert any("从网关插件库下载" in line for line in job["log"]) and any("sha256" in line for line in job["log"])
        lock = json.loads((w["root"] / "plugins" / "plugins.lock.json").read_text(encoding="utf-8"))
        assert lock["fixture"]["source"] == {"type": "hub", "name": "fixture", "version": "1.0.0",
                                             "sha256": package["sha256"]}
        assert (w["root"] / "plugins" / "fixture" / "current").read_text().strip() == "1.0.0"
        view = next(p for p in manager.get("/api/plugins").json()["plugins"] if p["name"] == "fixture")
        assert view["status"] == "running" and view["health"]["code"] == 200
        # The gateway sees the plugin in the device's catalog once the link refreshes.

        # A newer version in the repo: the local console says 有新版本 and installs it itself.
        newer = pack.pack(make_plugin(tmp_path / "src2" / "fixture-mcp", version="1.1.0",
                                      port=json.loads((folder / "mcp-service.json").read_text(encoding="utf-8"))["port"]),
                          tmp_path / "pkgs" / "fixture-1.1.0.zip")
        assert owner.post("/api/plugin-repo", content=Path(newer["path"]).read_bytes()).status_code == 201
        items = {p["version"]: p for p in manager.get("/api/plugin-repo").json()["packages"]}
        assert items["1.1.0"]["update_available"] and items["1.1.0"]["from_hub"]
        assert items["1.1.0"]["installed_version"] == "1.0.0" and not items["1.0.0"]["update_available"]
        local_job = manager.post("/api/plugin-repo/install", json={"name": "fixture"})
        assert local_job.status_code == 202 and local_job.json()["version"] == "1.1.0"
        deadline = time.monotonic() + 120
        while (state := manager.get(f"/api/plugins/jobs/{local_job.json()['job_id']}").json())["state"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.4)
        assert state["state"] == "done" and state["result"]["state"] == "running", state

        # Tampered on the server after upload: the device's sha256 check refuses it, nothing changes.
        stored = w["state"] / "plugin-repo" / "fixture" / "1.0.0" / "fixture-1.0.0.zip"
        stored.write_bytes(stored.read_bytes()[:-10] + b"tampered!!")
        tampered = owner.post(f"/api/devices/{agent_id}/plugins/install", json={"name": "fixture", "version": "1.0.0"})
        job = wait_job(owner, agent_id, tampered.json()["job_id"])
        assert job["state"] == "failed" and job["code"] == "checksum_mismatch", job
        assert "篡改" in job["error"]
        lock = json.loads((w["root"] / "plugins" / "plugins.lock.json").read_text(encoding="utf-8"))
        assert lock["fixture"]["version"] == "1.1.0"

        # Oversize (max_package_mb = 2) and secrets are refused on upload.
        big = make_plugin(tmp_path / "src3" / "fixture-mcp", version="9.0.0")
        (big / "blob.bin").write_bytes(os.urandom(3 * 1024 * 1024))
        big_package = pack.pack(big, tmp_path / "pkgs" / "big.zip")
        oversize = owner.post("/api/plugin-repo", content=Path(big_package["path"]).read_bytes())
        assert oversize.status_code == 413
        leak = tmp_path / "leak.zip"
        leak.write_bytes(zip_bytes({"mcp-service.json": json.dumps({"name": "leak", "version": "1", "command": ["x"]}),
                                    "notes.md": FAKE_KEY}))
        refused = owner.post("/api/plugin-repo", content=leak.read_bytes())
        assert refused.status_code == 422 and refused.json()["code"] == "secrets_found"

        # anx plugin publish: pack + upload with the device token (the joined manager's root).
        published = subprocess.run([*_exe("anx"), "--root", str(w["root"]), "plugin", "publish",
                                    str(make_plugin(tmp_path / "src4" / "pub-mcp", name="pub", version="0.1.0"))],
                                   env=_env(), capture_output=True, text=True, encoding="utf-8", timeout=120)
        assert published.returncode == 0, published.stdout + published.stderr
        assert any(p["name"] == "pub" and p["uploaded_by"] == f"device:{agent_id}"
                   for p in owner.get("/api/plugin-repo").json()["packages"])

        # Downloads: anonymous 401; the device token works until it is revoked.
        url = "/api/plugin-repo/fixture/1.1.0/download"
        anonymous = httpx.Client(base_url=w["origin"], trust_env=False)
        assert anonymous.get(url).status_code == 401
        token = (w["root"] / "secrets" / "cloud" / "device-token").read_text(encoding="utf-8").strip()
        assert anonymous.get(url, headers={"Authorization": f"Bearer {token}"}).status_code == 200
        assert owner.post(f"/api/devices/{agent_id}/revoke").status_code == 200
        assert anonymous.get(url, headers={"Authorization": f"Bearer {token}"}).status_code == 401
        assert anonymous.get("/api/plugin-repo", headers={"Authorization": f"Bearer {token}"}).status_code == 401
        assert anonymous.post("/api/plugin-repo", content=leak.read_bytes(),
                              headers={"Authorization": f"Bearer {token}"}).status_code == 401
        deadline = time.monotonic() + 10
        while (offline := owner.post(f"/api/devices/{agent_id}/plugins/install", json={"name": "fixture"})).status_code != 409:
            assert time.monotonic() < deadline, offline.text
            time.sleep(0.2)
        assert offline.json()["code"] == "device_offline" and "不在线" in offline.json()["error"]
        anonymous.close()

        # An old device (no plugin_repo/1): a readable Chinese error, nothing sent to it.
        asyncio.run(_old_device_is_refused(w, tmp_path / "legacy-root"))


async def _old_device_is_refused(w, root: Path) -> None:
    from fastmcp import FastMCP
    from agent_nexus.agent import run_agent
    root.mkdir()
    (root / "agent-nexus.json").write_text(json.dumps({"manager": {"port": 1}, "builtins": False, "scan": []}),
                                           encoding="utf-8")
    server = FastMCP("device-legacy")

    @server.tool
    async def execute(value: str) -> dict:
        return {"value": value}

    os.environ["REPO_LEGACY_TOKEN"] = LEGACY_TOKEN
    ready = asyncio.Event()
    cloud = {"enabled": True, "agent_id": "legacy-pc", "url": w["origin"].replace("http://", "ws://"),
             "token_env": "REPO_LEGACY_TOKEN", "refresh_interval_s": 60}
    task = asyncio.create_task(run_agent(root, cloud, transports={"device": server}, ready=ready, status={}))
    try:
        await asyncio.wait_for(ready.wait(), 20)
        async with httpx.AsyncClient(base_url=w["origin"], trust_env=False, timeout=30,
                                     headers={"Authorization": f"Bearer {SERVER_TOKEN}"}) as owner:
            response = await owner.post("/api/devices/legacy-pc/plugins/install", json={"name": "fixture"})
        assert response.status_code == 409, response.text
        body = response.json()
        assert body["code"] == "unsupported_device" and "版本太旧" in body["error"] and "更新" in body["error"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        os.environ.pop("REPO_LEGACY_TOKEN", None)


def test_repo_dir_is_created_private_and_never_overlaps_downloads(tmp_path, monkeypatch):
    from mcpkit.config import ConfigError
    monkeypatch.setenv("REPO_SERVER_TOKEN", SERVER_TOKEN)
    base = {"token_env": "REPO_SERVER_TOKEN", "public_url": "http://127.0.0.1:1", "state_dir": str(tmp_path / "state")}
    build_cloud_app(dict(base), tmp_path)
    repo_dir = tmp_path / "state" / "plugin-repo"
    assert repo_dir.is_dir()
    if os.name != "nt":
        assert repo_dir.stat().st_mode & 0o077 == 0
    for directory in ("state/downloads", "state/downloads/repo"):
        with pytest.raises(ConfigError, match="overlap"):
            build_cloud_app({**base, "plugin_repo": {"dir": str(tmp_path / directory)}}, tmp_path)
    with pytest.raises(ConfigError, match="max_package_mb"):
        build_cloud_app({**base, "plugin_repo": {"max_package_mb": 0}}, tmp_path)


def test_gateway_migration_adds_the_plugin_repo_section(tmp_path):
    from agent_nexus import migrate
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "cloud.json").write_text(json.dumps({"token_env": "X"}), encoding="utf-8")
    plan = migrate.plan_gateway_migration(etc, None, tmp_path / "to", tmp_path / "state-to")
    assert plan.config["plugin_repo"] == {"dir": str(tmp_path / "state-to").rstrip("/") + "/plugin-repo", "max_package_mb": 300}
    assert migrate.gateway_main(["--from", str(etc), "--to", str(tmp_path / "to"), "--state-to", str(tmp_path / "state-to"),
                                 "--apply"]) == 0
    assert (tmp_path / "state-to" / "plugin-repo").is_dir()


UNCONFIGURED = r'''
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(503); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps({"status": "invalid_config"}).encode())
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''


def test_a_package_without_the_owners_config_installs_as_needs_config(tmp_path):
    """Like payment: its config.json is owner data, so the package leaves it out and health says invalid_config."""
    from agent_nexus.supervisor import Supervisor
    folder = make_plugin(tmp_path / "src" / "fixture-mcp")
    (folder / "server.py").write_text(UNCONFIGURED, encoding="utf-8")
    manifest = json.loads((folder / "mcp-service.json").read_text(encoding="utf-8"))
    manifest["command"] = [sys.executable, "server.py", "{port}"]
    (folder / "mcp-service.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "config.example.json").write_text("{}", encoding="utf-8")
    package = pack.pack(folder, tmp_path / "fixture.zip")
    root = tmp_path / "root"
    root.mkdir()
    (root / "agent-nexus.json").write_text(json.dumps({"manager": {"port": free_port()}, "builtins": False, "scan": [],
                                                       "plugins": {}}), encoding="utf-8")
    sup = Supervisor(root)
    try:
        result = installer.Installer(root, sup, health_timeout=20).install(
            installer.Source("archive", path=package["path"], sha256=package["sha256"]))
        assert result.state == "needs_config" and "config.json" in result.message, result
        assert installer.read_lock(root)["fixture"]["version"] == "1.0.0"  # kept, not rolled back
    finally:
        sup.shutdown()


# -- regressions from the independent verification ---------------------------------------------

SECRET_VALUE = "Xk9vQ2mPz7Lr4tWb"  # gitleaks:allow (synthetic)


def test_pack_excludes_owner_data_whatever_the_letter_case(tmp_path):
    """On Windows and macOS ``Config.json`` *is* ``config.json``: names are compared case-insensitively."""
    folder = make_plugin(tmp_path / "src" / "fixture-mcp", data=["QRCodes/"])
    for rel, text in {"Config.json": '{"a": 1}', ".ENV": f"PASSWORD={SECRET_VALUE}", ".Env.Local": "A=1",
                      "Secrets/device-token.txt": "abc", "SECRETS/x": "abc", "Logs/run.LOG": "x", "Debug.Log": "x",
                      "State.SQLite3": "x", "WxWork_Keys.JSON": "{}", "OAuth-store.json": "{}", ".Git/config": "x",
                      ".VENV/pyvenv.cfg": "x", "qrcodes/pay.jpg": "x", "Config.Example.json": "{}",
                      "pkg/Tests/code.py": "x = 1"}.items():
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    result = pack.pack(folder, tmp_path / "out.zip")
    names = {n.lower() for n in names_in(result["path"])}
    for rel in ("config.json", ".env", ".env.local", "secrets/device-token.txt", "secrets/x", "logs/run.log",
                "debug.log", "state.sqlite3", "wxwork_keys.json", "oauth-store.json", ".git/config",
                ".venv/pyvenv.cfg", "qrcodes/pay.jpg"):
        assert rel not in names, rel
    assert "config.example.json" in names        # examples stay, whatever their case
    assert "pkg/tests/code.py" in names          # only an exact ``tests/`` is pruned (unity's Editor/Resources/Tests is code)


@pytest.mark.parametrize("rel, text", [
    ("settings.yaml", f"db:\n  password: {SECRET_VALUE}\n"),
    ("settings.yml", f"password: {SECRET_VALUE}\r\n"),                       # CRLF
    ("app.ini", f"[db]\npassword = {SECRET_VALUE}  ; prod\n"),
    ("app.properties", f"api.token={SECRET_VALUE}\n"),
    ("run.sh", f"export API_TOKEN={SECRET_VALUE}\n"),
    ("conf.py", f'API_KEY = "{SECRET_VALUE}9"\n'),
    ("src/client.js", f"const accessKey = '{SECRET_VALUE}';\n"),
    ("App.INI", f"password = {SECRET_VALUE}\n"),
])
def test_pack_refuses_unquoted_and_source_code_credentials(tmp_path, rel, text):
    folder = make_plugin(tmp_path / "src" / "fixture-mcp")
    (folder / rel).parent.mkdir(parents=True, exist_ok=True)
    (folder / rel).write_bytes(text.encode("utf-8"))
    with pytest.raises(PackError) as failure:
        pack.pack(folder, tmp_path / "x.zip")
    assert failure.value.code == "secrets_found"
    assert [f["path"] for f in failure.value.findings] == [rel]
    assert failure.value.findings[0]["rule"].startswith("疑似凭据赋值")
    assert SECRET_VALUE not in str(failure.value)
    assert not (tmp_path / "x.zip").exists()


def test_pack_content_scan_keeps_ordinary_code_and_placeholders(tmp_path):
    folder = make_plugin(tmp_path / "src" / "fixture-mcp")
    files = {
        "Prefs.cs": 'internal const string LastInstanceToken = "MCPForUnity.LocalHttpServer.LastInstanceToken";\n',
        "auth.py": 'TOKEN_HEADER = "X-API-Key"\npassword = os.environ["PASSWORD"]\nAPI_KEY_ENV = "ANX_API_KEY"\n',
        "settings.yaml": "password: ${DB_PASSWORD}\ntoken: <your-token>\napi_key: changeme-later\n",
        "app.ini": "token = {{ token }}\npassword =\n",
        "run.sh": "export TOKEN=$ANX_TOKEN\n",
    }
    for rel, text in files.items():
        (folder / rel).write_text(text, encoding="utf-8")
    result = pack.pack(folder, tmp_path / "ok.zip")
    assert set(files) <= names_in(result["path"])


@pytest.mark.parametrize("rel, text", [
    ("Config.json", '{"a": 1}'),
    (".ENV", f"PASSWORD={SECRET_VALUE}"),
    ("SECRETS/device-token.txt", "abc"),
    ("Logs/run.LOG", "x"),
    ("State.SQLITE3", "x"),
    ("app.ini", f"password = {SECRET_VALUE}\n"),
    ("settings.yaml", f"password: {SECRET_VALUE}\n"),
    ("conf.py", f'API_KEY = "{SECRET_VALUE}9"\n'),
])
def test_upload_refuses_owner_data_whatever_the_case_and_unquoted_secrets(gateway_app, rel, text):
    client, _, _ = gateway_app
    manifest = json.dumps({"name": "fixture", "version": "1.0.0", "command": ["python", "x.py"]})
    refused = client.post("/api/plugin-repo", content=zip_bytes({"mcp-service.json": manifest, rel: text}))
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "secrets_found"
    assert [f["path"] for f in refused.json()["findings"]] == [rel]
    assert SECRET_VALUE not in refused.text
    assert client.get("/api/plugin-repo").json()["packages"] == []


def test_a_device_cannot_overwrite_or_out_version_someone_elses_package(gateway_app):
    client, app, tmp_path = gateway_app
    device = TestClient(app, base_url="http://127.0.0.1:1")
    device.headers["Authorization"] = f"Bearer {LEGACY_TOKEN}"

    def package(name: str, version: str, body: str = "1") -> bytes:
        manifest = json.dumps({"name": name, "version": version, "command": ["python", "x.py"]})
        return zip_bytes({"mcp-service.json": manifest, "x.py": f"print({body})"})

    assert client.post("/api/plugin-repo", content=package("mp", "0.0.1")).status_code == 201
    owners = client.get("/api/plugin-repo/mp/0.0.1/download").content
    for path, data in (("/api/plugin-repo?replace=1", package("mp", "0.0.1", "666")),
                       ("/api/plugin-repo", package("mp", "9.9.9", "666"))):
        refused = device.post(path, content=data)
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "not_your_package" and "所有者" in refused.json()["error"]
    assert client.get("/api/plugin-repo/mp/0.0.1/download").content == owners
    assert [p["version"] for p in client.get("/api/plugin-repo").json()["packages"]] == ["0.0.1"]
    # A device's own plugin: new versions and replacing its own versions are fine.
    assert device.post("/api/plugin-repo", content=package("devplug", "1.0.0")).status_code == 201
    assert device.post("/api/plugin-repo?replace=1", content=package("devplug", "1.0.0", "2")).status_code == 200
    assert device.post("/api/plugin-repo", content=package("devplug", "1.1.0")).status_code == 201
    # The owner may still replace anything, including a device's package; then the device is locked out of it.
    assert client.post("/api/plugin-repo?replace=1", content=package("devplug", "1.1.0", "3")).status_code == 200
    assert device.post("/api/plugin-repo", content=package("devplug", "1.2.0")).status_code == 403


def test_multipart_upload_without_a_length_is_refused_before_it_is_spooled(gateway_app):
    client, _, _ = gateway_app
    manifest = json.dumps({"name": "fixture", "version": "1.0.0", "command": ["python", "x.py"]})
    boundary = "anxboundary"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"f.zip\"\r\n"
            f"Content-Type: application/zip\r\n\r\n").encode() + zip_bytes({"mcp-service.json": manifest}) + \
        f"\r\n--{boundary}--\r\n".encode()

    def chunked():
        for start in range(0, len(body), 1000):
            yield body[start:start + 1000]
    refused = client.post("/api/plugin-repo", content=chunked(),
                          headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    assert refused.status_code == 411 and refused.json()["code"] == "length_required"
    accepted = client.post("/api/plugin-repo", content=body,
                           headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    assert accepted.status_code == 201, accepted.text
    raw = client.post("/api/plugin-repo?replace=1", content=chunked_raw(zip_bytes({"mcp-service.json": manifest})),
                      headers={"Content-Type": "application/zip"})
    assert raw.status_code == 200, raw.text  # a chunked raw body is counted as it streams


def chunked_raw(data: bytes):
    for start in range(0, len(data), 500):
        yield data[start:start + 500]


def test_device_download_cap_follows_the_gateways_index(tmp_path, monkeypatch):
    from agent_nexus.plugins import hub_source
    assert hub_source.download_cap(400 * 1024 * 1024) == 400 * 1024 * 1024  # above the old fixed 300 MB
    assert hub_source.download_cap(None) == hub_source.HARD_MAX_BYTES
    assert hub_source.download_cap(True) == hub_source.download_cap(-1) == hub_source.HARD_MAX_BYTES
    assert hub_source.HARD_MAX_BYTES == 4096 * 1024 * 1024  # = the gateway's upper bound for max_package_mb
    payload = os.urandom(5000)
    import hashlib
    digest = hashlib.sha256(payload).hexdigest()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
    monkeypatch.setattr(hub_source, "link", lambda root: ("http://hub.test", "t" * 40))
    monkeypatch.setattr(hub_source, "_client", lambda timeout: httpx.Client(transport=transport))
    ok = hub_source.download(tmp_path, "/api/plugin-repo/a/1/download", tmp_path / "a.zip", sha256=digest,
                             log=lambda line: None, size=len(payload))
    assert ok.read_bytes() == payload
    with pytest.raises(installer.InstallError) as failure:  # bigger than the index says: stop
        hub_source.download(tmp_path, "/api/plugin-repo/a/1/download", tmp_path / "b.zip", sha256=digest,
                            log=lambda line: None, size=len(payload) - 1)
    assert failure.value.code == "too_large"


# -- verifier round 3: Windows names, files outside the top folder, owner data on update -----------

MAN = json.dumps({"name": "evil", "version": "1.0.0", "command": ["python", "x.py"]})


@pytest.mark.parametrize("member", ["config.json.", "config.json ", "config.json::$DATA", ".env ", ".env.",
                                    "secrets./token", "logs /a.log", "CON.txt", "a<b.txt"])
def test_windows_rewritten_names_are_refused_on_upload_and_install(gateway_app, tmp_path, member):
    client, _, _ = gateway_app
    data = zip_bytes({"mcp-service.json": MAN, member: "{}"})
    refused = client.post("/api/plugin-repo", content=data)
    assert refused.status_code in (400, 422), refused.text
    assert refused.json()["code"] in ("unsafe_archive", "secrets_found")
    assert client.get("/api/plugin-repo").json()["packages"] == []
    archive = tmp_path / "evil.zip"
    archive.write_bytes(data)
    with pytest.raises(installer.InstallError) as info:
        installer.extract_archive(archive, tmp_path / "out")
    assert info.value.code == "unsafe_archive"


def test_windows_name_normalisation_in_name_rules():
    assert pack.windows_name("config.json.") == "config.json"
    assert pack.windows_name("config.json::$DATA") == "config.json"
    assert pack._matches("Config.Json. ", pack.SECRET_FILES)


@pytest.mark.parametrize("extra", [{".env": "A=1"}, {".git/config": "[x]"}, {"other/readme.txt": "hi"},
                                   {"__MACOSX/evil-mcp/._x": "x"}])
def test_files_outside_the_single_top_folder_are_refused(gateway_app, extra):
    client, _, _ = gateway_app
    refused = client.post("/api/plugin-repo", content=zip_bytes({"evil-mcp/mcp-service.json": MAN, **extra}))
    assert refused.status_code in (400, 422), refused.text
    assert refused.json()["code"] in ("secrets_found", "invalid_manifest")  # two top folders: no single plugin folder
    if refused.json()["code"] == "secrets_found":
        assert [f["path"] for f in refused.json()["findings"]] == list(extra)
    assert client.get("/api/plugin-repo").json()["packages"] == []
    # inside the top folder the same files are still refused by the name rules
    inner = client.post("/api/plugin-repo", content=zip_bytes({"evil-mcp/mcp-service.json": MAN, "evil-mcp/.env": "A=1"}))
    assert inner.status_code == 422 and inner.json()["findings"][0]["path"] == ".env"
    ok = client.post("/api/plugin-repo", content=zip_bytes({"evil-mcp/mcp-service.json": MAN, "evil-mcp/x.py": "print(1)"}))
    assert ok.status_code == 201, ok.text


def _owner_data(directory: Path) -> None:
    (directory / "config.json").write_text('{"merchant": "mine"}', encoding="utf-8")
    (directory / "qrcodes").mkdir(exist_ok=True)
    (directory / "qrcodes" / "alipay.png").write_bytes(b"png")


def _has_owner_data(directory: Path) -> bool:
    return ((directory / "config.json").is_file() and "mine" in (directory / "config.json").read_text(encoding="utf-8")
            and (directory / "qrcodes" / "alipay.png").read_bytes() == b"png")


def _zip_of(folder: Path, out: Path) -> installer.Source:
    result = pack.pack(folder, out)
    return installer.Source("archive", path=result["path"], sha256=result["sha256"])


def _bare_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    (root / "agent-nexus.json").write_text(json.dumps({"manager": {"port": free_port()}, "builtins": False,
                                                        "scan": [], "plugins": {}}), encoding="utf-8")
    return root


def test_update_and_reinstall_keep_legacy_owner_data_in_the_code_folder(tmp_path):
    """payment (v1, no data[]) keeps config.json and qrcodes/ in its code folder: LEGACY_DATA carries them."""
    root = _bare_root(tmp_path)
    home = root / "plugins" / "payment"
    inst = installer.Installer(root, None, log=lambda *_: None)
    v1 = _zip_of(make_plugin(tmp_path / "s1" / "payment-mcp", name="payment", version="0.1.0"), tmp_path / "p1.zip")
    v2 = _zip_of(make_plugin(tmp_path / "s2" / "payment-mcp", name="payment", version="0.2.0"), tmp_path / "p2.zip")
    v3 = _zip_of(make_plugin(tmp_path / "s3" / "payment-mcp", name="payment", version="0.3.0"), tmp_path / "p3.zip")
    inst.install(v1, enable=False)
    _owner_data(home / "0.1.0")
    inst.install(v2, enable=False)  # upgrade
    assert _has_owner_data(home / "0.2.0")
    inst.install(v2, enable=False)  # same version again: the .old backup is pruned, the data is not
    assert _has_owner_data(home / "0.2.0")
    assert not any(p.name.startswith((".old-", ".shipped-")) for p in home.iterdir())
    inst.install(v3, enable=False)  # the next upgrade prunes 0.1.0
    assert _has_owner_data(home / "0.3.0") and not (home / "0.1.0").exists()
    inst.install(v2, enable=False)  # downgrade
    assert _has_owner_data(home / "0.2.0")


def test_declared_data_wins_over_a_shipped_copy_and_moves_back_on_failure(tmp_path):
    root = _bare_root(tmp_path)
    home = root / "plugins" / "fixture"
    inst = installer.Installer(root, None, log=lambda *_: None)
    data = ["config.json", "qrcodes/"]
    inst.install(installer.Source("path", path=str(make_plugin(tmp_path / "a" / "fixture", data=data))), enable=False)
    _owner_data(home / "1.0.0")
    newer = make_plugin(tmp_path / "b" / "fixture", version="1.1.0", data=data)
    (newer / "qrcodes").mkdir()
    (newer / "qrcodes" / "alipay.png").write_bytes(b"shipped")
    inst.install(installer.Source("path", path=str(newer)), enable=False)
    assert _has_owner_data(home / "1.1.0")
    fail = {"steps": [{"run": [sys.executable, "-c", "import sys; sys.exit(4)"]}]}
    broken = make_plugin(tmp_path / "c" / "fixture", version="1.2.0", data=data, install=fail)
    with pytest.raises(installer.InstallError):
        inst.install(installer.Source("path", path=str(broken)), enable=False)
    assert _has_owner_data(home / "1.1.0") and not (home / "1.2.0").exists()
    assert (home / "current").read_text(encoding="utf-8").strip() == "1.1.0"
    same = make_plugin(tmp_path / "d" / "fixture", version="1.1.0", data=data, install=fail)
    with pytest.raises(installer.InstallError):
        inst.install(installer.Source("path", path=str(same)), enable=False)
    assert _has_owner_data(home / "1.1.0")


def test_pack_replaces_the_owners_account_ids_and_leaves_the_source_alone(tmp_path):
    folder = make_plugin(tmp_path / "src" / "fixture-mcp")
    account, member, uuid = "9" * 16, "4" * 17, "1a2b3c4d-1111-4222-8333-123456789abc"
    readme = (f"Run with --db-dir E:/WXWork/{account}/Data\n\nmentions=[\"{member}\"]\n\n"
              f'{{"public_id": "{uuid}"}}\n\nKeep 00000000-0000-0000-0000-000000000000 and 12345.\n')
    (folder / "README.md").write_text(readme, encoding="utf-8")
    web = f'"""python -m web --db-dir E:\\\\WXWork\\\\{account}\\\\Data"""\nID = "{uuid}"\n'
    (folder / "web.py").write_text(web, encoding="utf-8")
    result = pack.pack(folder, tmp_path / "out.zip")
    assert (folder / "README.md").read_text(encoding="utf-8") == readme  # the source is never changed
    with zipfile.ZipFile(result["path"]) as zf:
        blob = zf.read("README.md").decode() + zf.read("web.py").decode()
        info = json.loads(zf.read("pack.json"))
    assert account not in blob and member not in blob
    assert "WXWork/<account-id>/Data" in blob and '"<member-id>"' in blob
    assert blob.count(uuid) == 1  # only in code (.py), where a UUID is a real constant
    assert set(info["redacted"]) == {"README.md", "web.py"}
    assert result["content_sha256"] == info["content_sha256"]
    installer.extract_archive(Path(result["path"]), tmp_path / "x")
    entries = [(p.relative_to(tmp_path / "x").as_posix(), installer.sha256_of(p))
               for p in (tmp_path / "x").rglob("*") if p.is_file() and p.name != "pack.json"]
    assert pack.content_digest(entries) == info["content_sha256"]
