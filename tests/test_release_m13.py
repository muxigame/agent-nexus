"""M13: release pipeline pieces, the gateway zip and the gateway upgrade script (design §10, §11.2)."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


upgrade = load("anx_gateway_upgrade", ROOT / "deploy" / "gateway" / "upgrade.py")
latest = load("make_latest_json", ROOT / "scripts" / "make_latest_json.py")
payload = load("test_no_private_payload_script", ROOT / "scripts" / "test_no_private_payload.py")
shim = load("upgrade_cloud_release", ROOT / "scripts" / "upgrade_cloud_release.py")
sys.path.insert(0, str(ROOT / "scripts"))
import build_release  # noqa: E402

VERSION = build_release.source_version()


def make_zip(path: Path, files: dict, manifest: dict | None = None) -> str:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        if manifest is None:
            manifest = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
        archive.writestr("SHA256.json", json.dumps(manifest))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def package_files(version="9.9.9"):
    return {"agent_nexus/__init__.py": f'__version__ = "{version}"\n'.encode(),
            "deploy/gateway/upgrade.py": b"# upgrade\n", "pyproject.toml": b"[project]\n"}


def symlinks_supported(tmp_path) -> bool:
    try:
        (tmp_path / "probe-target").mkdir()
        (tmp_path / "probe-link").symlink_to(tmp_path / "probe-target", target_is_directory=True)
        (tmp_path / "probe-link").unlink()
        return True
    except (OSError, NotImplementedError):
        return False


# ---- workflows -----------------------------------------------------------------------------------

def test_public_workflows_never_build_the_private_edition():
    files = sorted(WORKFLOWS.glob("*.yml"))
    assert {path.name for path in files} >= {"ci.yml", "release.yml", "deploy-gateway.yml"}
    for path in files:
        text = path.read_text(encoding="utf-8")
        for needle in ("build_private_desktop", "--enrollment-file", "private-desktop", "ENROLLMENT_FILE ",
                       "ubuntu-cloud", "secrets/build-enrollment"):
            assert needle not in text, f"{path.name} mentions {needle}"


def test_ci_has_every_section_11_2_job():
    yaml = pytest.importorskip("yaml")
    jobs = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))["jobs"]
    assert set(jobs) >= {"hygiene", "python", "sdk", "device", "console", "desktop-check"}
    hygiene = json.dumps(jobs["hygiene"])
    for step in ("version.py check", "scrub_check.py", "SCRUB_PATTERNS", "gitleaks", "Private guard"):
        assert step in hygiene
    assert jobs["python"]["strategy"]["matrix"]["os"] == ["windows-latest", "ubuntu-latest"]
    assert "xvfb-run -a uv run pytest" in json.dumps(jobs["python"])
    assert "ANX_RUNTIME_ARCHIVE" in json.dumps(jobs["desktop-check"])


def test_release_creates_a_draft_with_every_asset():
    yaml = pytest.importorskip("yaml")
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    jobs = yaml.safe_load(text)["jobs"]
    assert {"version-gate", "windows", "linux", "draft-release"} <= set(jobs)
    for job in ("windows", "linux"):
        assert jobs[job]["needs"] == "version-gate"
    for needle in ("version.py check --tag", "TAURI_SIGNING_PRIVATE_KEY", "TAURI_SIGNING_PRIVATE_KEY_PASSWORD",
                   "make_latest_json.py", "build_release.py gateway", "build_ubuntu.py", "test_no_private_payload.py",
                   "SHA256SUMS", "--draft", "x64-setup.exe.sig", "_amd64.deb"):
        assert needle in text, needle


def test_release_builds_both_macos_architectures_and_one_feed_for_all():
    yaml = pytest.importorskip("yaml")
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    jobs = yaml.safe_load(text)["jobs"]
    macos = jobs["macos"]
    assert macos["needs"] == "version-gate"
    assert {(entry["rid"], entry["arch"]) for entry in macos["strategy"]["matrix"]["include"]} == {
        ("osx-arm64", "aarch64"), ("osx-x64", "x64")}
    assert "TAURI_SIGNING_PRIVATE_KEY" in json.dumps(macos)
    feed = json.dumps(jobs["feed"])
    assert set(jobs["feed"]["needs"]) == {"windows", "macos"}
    for needle in ("x64-setup.exe", "for arch in aarch64 x64", ".app.tar.gz", "--macos-bundle", "make_latest_json.py"):
        assert needle in feed, needle
    assert "make_latest_json.py" not in json.dumps(jobs["windows"])  # one latest.json, from the feed job
    for job in ("publish-oss", "draft-release"):
        assert {"windows", "macos", "linux", "feed"} <= set(jobs[job]["needs"])


def test_a_failed_macos_build_does_not_hold_back_the_other_platforms():
    """macOS is best effort: Windows, Linux, Android and the feed still publish without it."""
    yaml = pytest.importorskip("yaml")
    jobs = yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))["jobs"]
    assert jobs["macos"]["strategy"]["fail-fast"] is False
    assert "ANX_RELEASE_MACOS" in jobs["macos"]["if"]
    assert "!cancelled()" in jobs["feed"]["if"] and "needs.windows.result == 'success'" in jobs["feed"]["if"]
    assert "needs.macos" not in jobs["feed"]["if"]
    for job in ("publish-oss", "draft-release"):
        condition = jobs[job]["if"]
        assert "!cancelled()" in condition and "needs.macos" not in condition
        for need in ("windows", "linux", "android", "feed"):
            assert f"needs.{need}.result == 'success'" in condition, (job, need)
    publish = jobs["publish-release"]["if"]
    assert "needs.publish-oss.result == 'success'" in publish and "needs.draft-release.result == 'success'" in publish
    assert "::warning::no macOS" in json.dumps(jobs["feed"])


def test_release_builds_the_signed_android_app():
    yaml = pytest.importorskip("yaml")
    jobs = yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))["jobs"]
    android = json.dumps(jobs["android"])
    assert jobs["android"]["needs"] == "version-gate"
    for needle in ("ANDROID_KEYSTORE_BASE64", "build_android.py wheels", "build_android.py apk",
                   "--rid linux-bionic-arm64", "ANX_ANDROID_P2P_TOOLS", "test_no_private_payload.py"):
        assert needle in android, needle
    for job in ("publish-oss", "draft-release"):
        assert "android" in jobs[job]["needs"]


def test_without_the_keystore_the_android_job_skips_instead_of_failing_the_release():
    yaml = pytest.importorskip("yaml")
    steps = yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))["jobs"]["android"]["steps"]
    names = [step.get("id") or step.get("name") or step.get("uses") for step in steps]
    gate = names.index("keystore")
    assert "exit 0" in steps[gate]["run"] and "present=false" in steps[gate]["run"]
    for step in steps[gate + 1:]:
        assert step.get("if") == "steps.keystore.outputs.present == 'true'", step


def test_a_keystore_without_its_password_stops_the_android_job_early(tmp_path):
    """Half-configured signing fails at once with an ::error:: (not after the long build, in Gradle)."""
    yaml = pytest.importorskip("yaml")
    import shutil
    if not shutil.which("bash"):
        pytest.skip("bash")
    steps = yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))["jobs"]["android"]["steps"]
    gate = next(step for step in steps if step.get("id") == "keystore")
    assert "ANDROID_KEYSTORE_PASSWORD" in gate["env"]

    def run(base64, password):
        output = tmp_path / "output"
        output.write_text("", encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", ""), "GITHUB_OUTPUT": str(output), "RUNNER_TEMP": str(tmp_path),
               "ANDROID_KEYSTORE_BASE64": base64, "ANDROID_KEYSTORE_PASSWORD": password}
        done = subprocess.run(["bash", "-c", gate["run"]], env=env, capture_output=True, text=True)
        return done.returncode, done.stdout, output.read_text(encoding="utf-8")

    code, out, result = run("", "")
    assert code == 0 and "present=false" in result and "::warning::" in out
    code, out, result = run("a2V5", "")
    assert code == 1 and "::error::" in out and "present=true" not in result
    code, out, result = run("a2V5", "secret")
    assert code == 0 and "present=true" in result and (tmp_path / "release.keystore").read_bytes() == b"key"


def test_macos_checks_its_executable_and_runtime_for_a_private_payload():
    yaml = pytest.importorskip("yaml")
    steps = yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))["jobs"]["macos"]["steps"]
    collect = next(step["run"] for step in steps if step.get("name") == "Collect")
    assert "test_no_private_payload.py dist artifacts/agent-nexus-runtime.zip artifacts/agent-nexus" in collect


def test_the_download_page_lists_the_android_app():
    from agent_nexus import downloads
    item = downloads.classify("agent-nexus_0.9.0_arm64.apk")
    assert (item["platform"], item["arch"], item["kind"]) == ("android", "arm64", "desktop")
    assert ".apk" in downloads.ARTIFACT_SUFFIXES


def test_release_tags_publish_and_the_gateway_updates_itself():
    """CI publishes; nothing in it deploys. The gateway follows the releases (auto_update.py)."""
    yaml = pytest.importorskip("yaml")
    jobs = yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))["jobs"]
    assert "deploy-gateway" not in jobs
    publish = jobs["publish-release"]
    assert set(publish["needs"]) == {"publish-oss", "draft-release"}
    assert "--draft=false --latest" in json.dumps(publish)
    deploy = ROOT / "deploy" / "gateway"
    for name in ("auto_update.py", "agent-nexus-gateway-update.service", "agent-nexus-gateway-update.timer"):
        assert (deploy / name).is_file(), name
    assert "install_auto_update" in (deploy / "upgrade.py").read_text(encoding="utf-8")
    assert "agent-nexus-gateway-update.timer" in (deploy / "install.sh").read_text(encoding="utf-8")


def test_deploy_workflow_is_manual_only_in_the_production_environment():
    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load((WORKFLOWS / "deploy-gateway.yml").read_text(encoding="utf-8"))
    triggers = document.get("on", document.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    job = document["jobs"]["deploy"]
    assert job["environment"] == "production"
    text = json.dumps(job)
    for needle in ("DEPLOY_HOST", "DEPLOY_USER", "DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS", "GATEWAY_PROBE_TOKEN",
                   "/opt/agent-nexus/incoming/", "sudo /opt/agent-nexus/bin/anx-upgrade", "--sha256", "/healthz",
                   "agent-nexus-gateway@trial.service", "8441"):
        assert needle in text, needle
    assert "verify_cloud_host" not in text


# ---- latest.json ---------------------------------------------------------------------------------

def minisign_signature() -> str:
    body = ("untrusted comment: signature from tauri secret key\nRUT" + "A" * 98 + "\n"
            "trusted comment: timestamp:1\tfile:agent-nexus_0.2.0_x64-setup.exe\n" + "B" * 88 + "\n")
    return base64.b64encode(body.encode()).decode()


def test_latest_json_points_at_the_release_asset(tmp_path):
    setup = tmp_path / "agent-nexus_0.2.0_x64-setup.exe"
    setup.write_bytes(b"MZ installer")
    signature = minisign_signature()
    (tmp_path / (setup.name + ".sig")).write_text(signature + "\n", encoding="utf-8")
    out = tmp_path / "latest.json"
    assert latest.main(["--setup", str(setup), "--repo", "owner/agent-nexus", "--tag", "v0.2.0", "--out", str(out)]) == 0
    feed = json.loads(out.read_text(encoding="utf-8"))
    assert feed["version"] == "0.2.0"
    assert set(feed["platforms"]) == {"windows-x86_64"}
    entry = feed["platforms"]["windows-x86_64"]
    assert entry["signature"] == signature
    assert entry["url"] == "https://github.com/owner/agent-nexus/releases/download/v0.2.0/agent-nexus_0.2.0_x64-setup.exe"
    assert feed["pub_date"].endswith("Z")


def test_latest_json_adds_the_macos_updater_bundles(tmp_path):
    setup = tmp_path / "agent-nexus_0.2.0_x64-setup.exe"
    setup.write_bytes(b"MZ installer")
    signature = minisign_signature()
    bundles = [tmp_path / "agent-nexus_0.2.0_aarch64.app.tar.gz", tmp_path / "agent-nexus_0.2.0_x64.app.tar.gz"]
    for path in [setup, *bundles]:
        path.write_bytes(b"x")
        (tmp_path / (path.name + ".sig")).write_text(signature + "\n", encoding="utf-8")
    out = tmp_path / "latest.json"
    base = "https://example.com/agent-nexus/releases/0.2.0"
    assert latest.main(["--setup", str(setup), "--macos-bundle", str(bundles[0]), "--macos-bundle", str(bundles[1]),
                        "--tag", "v0.2.0", "--base-url", base, "--out", str(out)]) == 0
    platforms = json.loads(out.read_text(encoding="utf-8"))["platforms"]
    assert set(platforms) == {"windows-x86_64", "darwin-aarch64", "darwin-x86_64"}
    assert platforms["darwin-aarch64"]["url"] == base + "/agent-nexus_0.2.0_aarch64.app.tar.gz"
    assert platforms["darwin-x86_64"]["signature"] == signature
    (tmp_path / (bundles[1].name + ".sig")).unlink()
    assert latest.main(["--setup", str(setup), "--macos-bundle", str(bundles[1]), "--tag", "v0.2.0",
                        "--out", str(tmp_path / "other.json")]) == 2


def test_latest_json_rejects_a_missing_or_bogus_signature(tmp_path):
    setup = tmp_path / "agent-nexus_0.2.0_x64-setup.exe"
    setup.write_bytes(b"MZ")
    out = tmp_path / "latest.json"
    assert latest.main(["--setup", str(setup), "--tag", "v0.2.0", "--out", str(out)]) == 2
    (tmp_path / (setup.name + ".sig")).write_text("not a signature", encoding="utf-8")
    assert latest.main(["--setup", str(setup), "--tag", "v0.2.0", "--out", str(out)]) == 2
    assert not out.exists()


# ---- no private payload --------------------------------------------------------------------------

def test_clean_artifacts_pass(tmp_path):
    make_zip(tmp_path / "agent-nexus-gateway-0.2.0.zip", {"agent_nexus/enrollment.py": b"# reads .private/enrollment.json\n",
                                                          "config/server.example.json": b'{"port": 8440}'})
    (tmp_path / "agent-nexus.exe").write_bytes(b"MZ" + b"\0" * 64 + b"no payload")
    assert payload.check_paths([tmp_path]) == []


@pytest.mark.parametrize("member", [".private/enrollment.json", "runtime/.private/endpoint-allow",
                                    "secrets/build-enrollment.json", "endpoint-allow"])
def test_private_members_in_a_zip_fail(tmp_path, member):
    make_zip(tmp_path / "agent-nexus-gateway-0.2.0.zip", {member: b"{}"})
    findings = payload.check_paths([tmp_path])
    assert findings and all("agent-nexus-gateway-0.2.0.zip!" in item for item in findings)


def test_agent_prefix_profile_fails_and_value_is_not_printed(tmp_path):
    make_zip(tmp_path / "a.zip", {"config/client.json": json.dumps({"agent_prefix": "portable-", "token": "s3cr3t"}).encode()})
    findings = payload.check_paths([tmp_path])
    assert findings == ["a.zip!config/client.json: enrollment profile (agent_prefix)"]
    assert "s3cr3t" not in " ".join(findings)


def test_private_payload_inside_a_deb_tarball_fails(tmp_path):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:xz") as archive:
        member = b'{"url": "wss://hub.example.test"}'
        info = tarfile.TarInfo("usr/lib/agent-nexus/runtime/.private/enrollment.json")
        info.size = len(member)
        archive.addfile(info, io.BytesIO(member))
    body = data.getvalue()
    header = f"{'data.tar.xz/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(body):<10}`\n".encode()
    (tmp_path / "agent-nexus_0.2.0_amd64.deb").write_bytes(b"!<arch>\n" + header + body + (b"\n" if len(body) % 2 else b""))
    findings = payload.check_paths([tmp_path])
    assert any(".private" in item for item in findings), findings


def test_embedded_private_runtime_in_an_exe_fails(tmp_path):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(".private/enrollment.json", b"{}")
    (tmp_path / "agent-nexus.exe").write_bytes(b"MZ" + b"\0" * 128 + inner.getvalue() + b"\0" * 16)
    assert payload.check_paths([tmp_path]) == ["agent-nexus.exe: embedded private runtime member"]


def test_private_artifact_names_fail(tmp_path):
    (tmp_path / "agent-nexus-private.exe").write_bytes(b"MZ")
    assert payload.check_paths([tmp_path]) == ["agent-nexus-private.exe: private artifact in a public release"]


# ---- gateway zip ---------------------------------------------------------------------------------

def test_gateway_zip_is_versioned_and_carries_the_upgrade_script(tmp_path):
    build_release.build("gateway", tmp_path)
    archive_path = tmp_path / f"agent-nexus-gateway-{VERSION}.zip"
    assert archive_path.is_file()
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("SHA256.json"))
        assert names == set(manifest) | {"SHA256.json"}
        for name, digest in manifest.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
        for required in ("deploy/gateway/upgrade.py", "deploy/gateway/agent-nexus-gateway.service",
                         "deploy/gateway/agent-nexus-gateway@.service", "deploy/gateway/README.md",
                         "deploy/gateway/install.sh", "agent_nexus/cloud.py", "uv.lock", "gateway.json",
                         "builtins/mcpkit/pyproject.toml"):
            assert required in names, required
        assert not any(set(Path(name).parts) & {".private", "secrets", ".venv", "logs", "__pycache__"} for name in names)
        assert not any(name.startswith("builtins/device/") for name in names)
        # The server package never runs the client wheel hook.
        assert b"[tool.hatch.build.targets.wheel.hooks.custom]" not in archive.read("pyproject.toml")
    # The upgrade script accepts what the build produced.
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    assert upgrade.verify_package(archive_path, digest) == VERSION
    assert payload.check_paths([archive_path]) == []


# ---- upgrade.py ----------------------------------------------------------------------------------

def test_verify_package_rejects_tampering(tmp_path):
    good = tmp_path / "good.zip"
    digest = make_zip(good, package_files())
    assert upgrade.verify_package(good, digest) == "9.9.9"
    with pytest.raises(upgrade.UpgradeError, match="sha256 mismatch"):
        upgrade.verify_package(good, "0" * 64)
    with pytest.raises(upgrade.UpgradeError, match="64 lowercase hex"):
        upgrade.verify_package(good, "XYZ")
    files = package_files()
    bad = tmp_path / "member.zip"
    digest = make_zip(bad, files, {**{n: hashlib.sha256(d).hexdigest() for n, d in files.items()},
                                   "pyproject.toml": "0" * 64})
    with pytest.raises(upgrade.UpgradeError, match="member hash mismatch"):
        upgrade.verify_package(bad, digest)
    for member, message in (("../escape.py", "unsafe member path"), (".venv/bin/python", "excluded data"),
                            ("secrets/token", "excluded data"), ("logs/a.log", "excluded data")):
        path = tmp_path / "case.zip"
        digest = make_zip(path, {**package_files(), member: b"x"})
        with pytest.raises(upgrade.UpgradeError, match=message):
            upgrade.verify_package(path, digest)
    extra = tmp_path / "extra.zip"
    digest = make_zip(extra, package_files(), {"pyproject.toml": hashlib.sha256(b"[project]\n").hexdigest()})
    with pytest.raises(upgrade.UpgradeError, match="exact file set"):
        upgrade.verify_package(extra, digest)


def test_env_file_parsing(tmp_path):
    env = tmp_path / "gateway.env"
    env.write_text("# comment\n\nANX_SERVER_TOKEN=abc=def\nexport B='quoted'\nC=\"x y\"\n", encoding="utf-8")
    assert upgrade.read_env_file(env) == {"ANX_SERVER_TOKEN": "abc=def", "B": "quoted", "C": "x y"}
    env.write_text("not a pair\n", encoding="utf-8")
    with pytest.raises(upgrade.UpgradeError, match="malformed"):
        upgrade.read_env_file(env)


class FakeHost(upgrade.Host):
    def __init__(self, *, fail_check=False, healthy_versions=(), state_dir=None, migrate=False):
        self.commands, self.restarts, self.stops = [], [], []
        self.fail_check, self.healthy_versions = fail_check, set(healthy_versions)
        self.state_dir, self.migrate = state_dir, migrate
        self.running = None

    def run(self, command, env=None):
        self.commands.append([str(part) for part in command])
        if "--check-config" in command:
            assert env["ANX_SERVER_TOKEN"] == "t" * 40  # gateway.env is loaded for the check
            if self.fail_check:
                raise upgrade.UpgradeError("command failed with exit code 1: python")

    def restart(self, service):
        self.restarts.append(service)
        current = Path(self.base / "current").resolve()
        self.running = (current / "agent_nexus" / "__init__.py").read_text().split('"')[1]
        if self.migrate and self.running == "9.9.9":
            with sqlite3.connect(str(self.state_dir / "gateway.sqlite3")) as db:
                db.execute("UPDATE schema_version SET version = 99")

    def stop(self, service):
        self.stops.append(service)
        self.running = None

    def health(self, url):
        if self.running is None:
            return None
        status = "ok" if self.running in self.healthy_versions else "starting"
        return {"status": status, "service": "agent-nexus-gateway", "version": self.running}

    def sleep(self, seconds):
        pass


@pytest.fixture
def host_tree(tmp_path):
    if not symlinks_supported(tmp_path):
        pytest.skip("directory symlinks are not available to this user")
    base, config_dir, state_dir = tmp_path / "opt", tmp_path / "etc", tmp_path / "var"
    for path in (base / "releases", base / "bin", config_dir, state_dir):
        path.mkdir(parents=True)
    (config_dir / "gateway.json").write_text(json.dumps({"host": "127.0.0.1", "port": 18440}), encoding="utf-8")
    (config_dir / "gateway.env").write_text("ANX_SERVER_TOKEN=" + "t" * 40 + "\n", encoding="utf-8")
    with sqlite3.connect(str(state_dir / "gateway.sqlite3")) as db:
        db.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
        db.execute("INSERT INTO schema_version VALUES (3)")
    previous = base / "releases" / "0.1.0-aaaaaaaa"
    (previous / "agent_nexus").mkdir(parents=True)
    (previous / "agent_nexus" / "__init__.py").write_text('__version__ = "0.1.0"\n')
    (base / "current").symlink_to(previous, target_is_directory=True)
    package = tmp_path / "incoming" / "agent-nexus-gateway-9.9.9.zip"
    package.parent.mkdir()
    digest = make_zip(package, {**package_files(), "deploy/gateway/upgrade.py": b"# new upgrade script\n"})
    args = ["--package", str(package), "--sha256", digest, "--base", str(base), "--config-dir", str(config_dir),
            "--state-dir", str(state_dir), "--health-timeout", "0", "--uv", "uv-fake"]
    return {"base": base, "state": state_dir, "previous": previous, "digest": digest, "args": args}


def run_upgrade(tree, host, *extra):
    host.base = tree["base"]
    return upgrade.upgrade(upgrade.parse_args(tree["args"] + list(extra)), host)


def schema(tree):
    with sqlite3.connect(str(tree["state"] / "gateway.sqlite3")) as db:
        return db.execute("SELECT version FROM schema_version").fetchone()[0]


def test_upgrade_switches_restarts_and_installs_itself(host_tree):
    host = FakeHost(healthy_versions={"9.9.9"})
    (host_tree["base"] / "bin" / "anx-upgrade").write_text("# old\n")
    assert run_upgrade(host_tree, host) == 0
    release = host_tree["base"] / "releases" / ("9.9.9-" + host_tree["digest"][:8])
    assert (host_tree["base"] / "current").resolve() == release.resolve()
    assert host.restarts == ["agent-nexus-gateway.service"]
    sync, check = host.commands
    assert sync[:2] == ["uv-fake", "sync"] and "--frozen" in sync and "--no-dev" in sync
    assert check[1:3] == ["-m", "agent_nexus.cloud"] and check[-1] == "--check-config"
    assert (host_tree["base"] / "bin" / "anx-upgrade").read_text() == "# new upgrade script\n"
    backups = list((host_tree["base"] / "backups").iterdir())
    assert len(backups) == 1 and (backups[0] / "state" / "gateway.sqlite3").is_file()
    assert (backups[0] / "gateway.json").is_file() and (backups[0] / "gateway.env").is_file()


def test_failed_check_config_leaves_the_running_release_alone(host_tree):
    host = FakeHost(fail_check=True, healthy_versions={"9.9.9"})
    with pytest.raises(upgrade.UpgradeError):
        run_upgrade(host_tree, host)
    assert (host_tree["base"] / "current").resolve() == host_tree["previous"].resolve()
    assert host.restarts == [] and host.stops == []


def test_check_config_mode_switches_nothing(host_tree):
    host = FakeHost(healthy_versions={"9.9.9"})
    assert run_upgrade(host_tree, host, "--check-config") == 0
    assert (host_tree["base"] / "current").resolve() == host_tree["previous"].resolve()
    assert host.restarts == [] and not (host_tree["base"] / "backups").exists()


def test_unhealthy_release_rolls_back_and_restores_the_database(host_tree):
    host = FakeHost(healthy_versions={"0.1.0"}, state_dir=host_tree["state"], migrate=True)
    with pytest.raises(upgrade.UpgradeError, match="health check failed"):
        run_upgrade(host_tree, host)
    assert (host_tree["base"] / "current").resolve() == host_tree["previous"].resolve()
    assert host.restarts == ["agent-nexus-gateway.service"] * 2 and host.stops == ["agent-nexus-gateway.service"]
    assert host.running == "0.1.0"
    assert schema(host_tree) == 3  # the new release's migration was undone from the backup


def test_instance_options_reach_the_service_and_probe(host_tree):
    host = FakeHost(healthy_versions={"9.9.9"})
    urls = []
    original = host.health
    host.health = lambda url: urls.append(url) or original(url)
    assert run_upgrade(host_tree, host, "--service", "agent-nexus-gateway@trial.service", "--port", "8441") == 0
    assert host.restarts == ["agent-nexus-gateway@trial.service"]
    assert urls[-1] == "http://127.0.0.1:8441/healthz"


def test_redeploying_the_live_package_never_touches_the_running_release(host_tree):
    # Re-running deploy-gateway.yml for the tag that is already live (say after a probe
    # failure) must not delete the running release before anything is switched.
    assert run_upgrade(host_tree, FakeHost(healthy_versions={"9.9.9"})) == 0
    live = host_tree["base"] / "releases" / ("9.9.9-" + host_tree["digest"][:8])
    assert (host_tree["base"] / "current").resolve() == live.resolve()
    marker = live / ".venv" / "marker"
    marker.parent.mkdir()
    marker.write_text("running venv")

    for extra in ([], ["--check-config"]):
        host = FakeHost(fail_check=True, healthy_versions={"9.9.9"})
        with pytest.raises(upgrade.UpgradeError):
            run_upgrade(host_tree, host, *extra)
        assert marker.read_text() == "running venv"
        assert (host_tree["base"] / "current").resolve() == live.resolve()
        assert host.restarts == [] and host.stops == []
        sync = host.commands[0]
        assert Path(sync[sync.index("--project") + 1]).name == live.name + "-2"

    assert run_upgrade(host_tree, FakeHost(healthy_versions={"9.9.9"}), "--check-config") == 0
    assert marker.read_text() == "running venv"


def test_redeploy_switches_to_a_fresh_copy_and_keeps_the_old_one_until_pruned(host_tree):
    assert run_upgrade(host_tree, FakeHost(healthy_versions={"9.9.9"})) == 0
    first = host_tree["base"] / "releases" / ("9.9.9-" + host_tree["digest"][:8])
    second = first.with_name(first.name + "-2")
    assert run_upgrade(host_tree, FakeHost(healthy_versions={"9.9.9"})) == 0
    assert (host_tree["base"] / "current").resolve() == second.resolve()
    assert first.is_dir()  # the previous release stays for a manual rollback
    # A third deploy reuses the base name: that copy is no longer live, so replacing it is safe.
    (second / "keep").write_text("live")
    assert run_upgrade(host_tree, FakeHost(healthy_versions={"9.9.9"})) == 0
    assert (host_tree["base"] / "current").resolve() == first.resolve()
    assert (second / "keep").read_text() == "live"


def test_release_dir_and_extract_refuse_the_live_directory(tmp_path):
    live = tmp_path / "releases" / "1.0.0-abcdef12"
    live.mkdir(parents=True)
    assert upgrade.release_dir(tmp_path, "1.0.0-abcdef12", None) == live
    assert upgrade.release_dir(tmp_path, "1.0.0-abcdef12", live.resolve()).name == "1.0.0-abcdef12-2"
    (tmp_path / "releases" / "1.0.0-abcdef12-2").mkdir()
    assert upgrade.release_dir(tmp_path, "1.0.0-abcdef12-2", live.resolve()).name == "1.0.0-abcdef12-2"
    with pytest.raises(upgrade.UpgradeError, match="live release"):
        upgrade.extract(tmp_path / "unused.zip", live, live.resolve())
    assert live.is_dir()


def test_upgrade_script_has_no_asserts_or_legacy_special_cases():
    source = (ROOT / "deploy" / "gateway" / "upgrade.py").read_text(encoding="utf-8")
    assert "\nassert " not in source and "    assert " not in source
    assert "e636183" not in source


def test_legacy_wrapper_translates_the_positional_hash():
    argv = shim.translate(["a" * 64, "--base", "/opt/mcpmanager", "--service", "mcpmanager-cloud.service"])
    assert argv[:2] == ["--sha256", "a" * 64]
    assert argv[-2:] == ["--package", str(Path("/opt/mcpmanager") / "bootstrap" / "agent-nexus-server-next.zip")]
    assert shim.translate(["--sha256", "b" * 64, "--package", "x.zip"]) == ["--sha256", "b" * 64, "--package", "x.zip"]


# ---- public branch -------------------------------------------------------------------------------

publisher = load("make_public_branch", ROOT / "scripts" / "publish" / "make_public_branch.py")


def test_public_tree_filter_refuses_private_paths():
    allowed = ["agent_nexus/cloud.py", ".github/workflows/ci.yml", "config/server.example.json",
               "deploy/gateway/upgrade.py", "builtins/device/README.md", "builtins/mcpkit/src/mcpkit/auth.py",
               "agent_nexus/secrets_restore.py", "examples/gateway.env.example"]
    refused = ["builtins/payment-mcp/config.json", "builtins/wechat-mcp/README.md", "builtins/unity-mcp/x.py",
               "builtins/ones-mcp/a", "builtins/jubian-go-mcp/a", "builtins/chrome-devtools-mcp/a",
               "secrets/build-enrollment.json", "root/secrets/ones/token", ".private/endpoint-allow",
               "mcp-manager.json", "builtins/codex-mcp/logs/oauth-store.json.bak-domain", "gateway.env",
               "state/oauth.sqlite3", ".extracted/agent-nexus-ops/x.py"]
    assert publisher.forbidden_paths(allowed) == []
    assert publisher.forbidden_paths(refused) == refused


def test_pre_push_hook_only_lets_the_public_branch_through():
    hook = publisher.HOOK.format(marker=publisher.HOOK_MARKER, branch="public")
    assert hook.startswith("#!/bin/sh\n") and "\r" not in hook
    assert "refs/heads/public) ;;" in hook
    assert "refs/tags/v*)" in hook and "merge-base --is-ancestor" in hook
    assert publisher.PUSH_REFSPEC == "refs/heads/public:refs/heads/main"
    assert publisher.AUTHOR_EMAIL == "76636667+muxigame@users.noreply.github.com"


def _scratch_repo(path, monkeypatch):
    def run(*args, cwd=path):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
    path.mkdir()
    run("init", "-q", "-b", "main")
    run("config", "user.name", "t")
    run("config", "user.email", "t@example.invalid")
    run("config", "commit.gpgsign", "false")
    monkeypatch.setattr(publisher, "ROOT", path)
    return run


def _commit_file(run, path, text):
    (path / "README.md").write_text(text, encoding="utf-8")
    run("add", "README.md")
    run("commit", "-q", "-m", "private history " + text)
    return run("rev-parse", "HEAD^{tree}")


def test_public_branch_is_append_only_so_refreshes_and_old_tags_push_without_force(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    run = _scratch_repo(repo, monkeypatch)
    first, old = publisher.update_public_ref("public", _commit_file(run, repo, "one"), "Agent Nexus 1")
    assert old == "" and run("rev-list", "--count", first) == "1"  # orphan: no private history
    assert publisher.update_public_ref("public", run("rev-parse", "HEAD^{tree}"), "again") == (first, first)

    second, old = publisher.update_public_ref("public", _commit_file(run, repo, "two"), "Agent Nexus 2")
    assert old == first and run("rev-parse", second + "^") == first
    assert run("rev-list", "--count", second) == "2"
    assert run("log", "--format=%ae", second) == f"{publisher.AUTHOR_EMAIL}\n{publisher.AUTHOR_EMAIL}"

    # Push through the guard: refreshed public and an older v-tag go through without --force.
    remote = tmp_path / "remote.git"
    run("init", "-q", "--bare", str(remote), cwd=tmp_path)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    with open(hooks / "pre-push", "w", encoding="utf-8", newline="\n") as handle:
        handle.write(publisher.HOOK.format(marker=publisher.HOOK_MARKER, branch="public"))
    os.chmod(hooks / "pre-push", 0o755)
    run("config", "core.hooksPath", hooks.as_posix())
    run("remote", "add", "origin", str(remote))
    run("tag", "v0.1.0", first)
    run("update-ref", "refs/heads/public", first)  # as published before the refresh
    run("push", "-q", "origin", "refs/heads/public:refs/heads/main")
    run("update-ref", "refs/heads/public", second)
    run("push", "-q", "origin", "refs/heads/public:refs/heads/main", "refs/tags/v0.1.0")
    assert run("rev-parse", "main", cwd=remote) == second
    refused = subprocess.run(["git", "push", "origin", "main:refs/heads/leak"], cwd=repo, capture_output=True)
    assert refused.returncode != 0


def test_cutover_scripts_parse(tmp_path):
    """scripts/cutover (design §12.5): valid bash with the automatic rollback trap."""
    import shutil as _shutil
    import subprocess as _subprocess

    bash = _shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    cutover = Path(__file__).resolve().parents[1] / "scripts" / "cutover"
    for name in ("lib.sh", "gateway-cutover.sh", "gateway-rollback.sh"):
        assert _subprocess.run([bash, "-n", str(cutover / name)]).returncode == 0, name
    text = (cutover / "gateway-cutover.sh").read_text(encoding="utf-8") + (cutover / "gateway-rollback.sh").read_text(encoding="utf-8")
    assert "trap rollback_on_failure EXIT" in text and "--expect-agents" in text
