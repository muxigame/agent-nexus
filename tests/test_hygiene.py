"""M0 repository hygiene: scrub rules, ignore rules and parametrized ops scripts."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location("hygiene_" + name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


scrub = load("scrub_check")
GIT = shutil.which("git") is not None and (ROOT / ".git").exists()


def rules(text, private=()):
    import re
    return {finding.rule for finding in scrub.scan_text("f.md", text, [re.compile(p) for p in private])}


@pytest.mark.parametrize("text,expected", [
    ("connect to 8.8.4.4 now", {"ipv4"}),
    ("listen on 127.0.0.1:8440 or 0.0.0.0", set()),
    ("docs use 192.0.2.10 and 203.0.113.5", set()),
    ("lan 192.168.1.20 and 10.1.2.3", set()),
    ("ssh root@hub.example.test", {"root-login"}),
    ("the `root@` rule", set()),
    (r"path C:\Users\alice\work", {"user-home"}),
    ("path C:/Users/alice/work", {"user-home"}),
    (r'env = {"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}', set()),
    ("cd /home/bob/project", {"user-home"}),
    ("-----BEGIN OPENSSH PRIVATE KEY-----", {"private-key"}),
    ("token ghp_" + "a" * 36, {"github-token"}),
    ("key AKIA" + "A" * 16, {"aws-key"}),
])
def test_generic_scrub_rules(text, expected):
    assert rules(text) == expected


def test_private_patterns_come_from_environment_and_never_echo(monkeypatch, tmp_path, capsys):
    (tmp_path / "note.md").write_text("deploy to secret-host.internal today\n", encoding="utf-8")
    monkeypatch.setenv("SCRUB_PATTERNS", "# comment\nsecret-host\\.internal\n")
    assert scrub.main(["--root", str(tmp_path), "--allow-file", str(tmp_path / "none"), "note.md"]) == 1
    captured = capsys.readouterr()
    assert "note.md:1: private-pattern-1" in captured.err
    assert "secret-host" not in captured.out + captured.err
    monkeypatch.delenv("SCRUB_PATTERNS")
    assert scrub.main(["--root", str(tmp_path), "--allow-file", str(tmp_path / "none"), "note.md"]) == 0


def test_allow_file_requires_reason_and_never_covers_private_patterns(tmp_path, monkeypatch):
    allow = tmp_path / ".scrub-allow"
    allow.write_text("*.lock ipv4\n", encoding="utf-8")
    assert scrub.load_allow(allow)[1], "an exception without a reason is an error"
    allow.write_text("*.md * # broad on purpose\n", encoding="utf-8")
    allows, errors = scrub.load_allow(allow)
    assert not errors
    (tmp_path / "x.md").write_text("ip 8.8.4.4 host hidden.internal\n", encoding="utf-8")
    monkeypatch.setenv("SCRUB_PATTERNS", "hidden\\.internal")
    findings = scrub.scan(tmp_path, ["x.md"], scrub.load_private_patterns(None))
    kept, _ = scrub.apply_allow(findings, allows)
    assert [finding.rule for finding in kept] == ["private-pattern-1"]


@pytest.mark.parametrize("name", ["secrets/workspace/bearer-token", "builtins/x/.private/enrollment.json",
                                  "build-enrollment.json", "deploy/server.env", "certs/site.key",
                                  "mcp-manager.json", "data/oauth.sqlite3"])
def test_forbidden_paths(name):
    assert scrub.scan_path(name)


def test_large_files_are_rejected(tmp_path):
    (tmp_path / "big.txt").write_bytes(b"a" * (scrub.MAX_BYTES + 1))
    assert [f.rule for f in scrub.scan(tmp_path, ["big.txt"], [])] == ["file-too-large"]


@pytest.mark.skipif(not GIT, reason="needs a git checkout")
def test_repository_passes_generic_scrub(capsys):
    assert scrub.main(["--root", str(ROOT)]) == 0, capsys.readouterr().err


@pytest.mark.skipif(not GIT, reason="needs a git checkout")
@pytest.mark.parametrize("path", [
    "secrets/cloud/agent-token", "secrets/build-enrollment.json", ".private/enrollment.json",
    "mcp-manager.json", "mcp-manager.json.bak-1", ".mcp-manager/state.json", "agent-nexus.json", ".anx/x.json",
    "artifacts/mcpm-desktop.exe", "builtins/wechat-mcp/x.py", "builtins/payment-mcp/config.json",
    "builtins/codex-mcp/logs/oauth-store.json.bak", ".venv/x", "desktop/src-tauri/target/x",
    "x/oauth.sqlite3", "server.env", "some-apikey", ".extracted/agent-nexus-ops/scripts/x.py",
])
def test_sensitive_paths_are_ignored(path):
    result = subprocess.run(["git", "check-ignore", "--no-index", "-q", path], cwd=ROOT)
    assert result.returncode == 0, path


@pytest.mark.skipif(not GIT, reason="needs a git checkout")
def test_tracked_files_are_not_ignored_and_external_plugins_absent():
    ignored = subprocess.run(["git", "ls-files", "-ci", "--exclude-standard"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split()
    assert ignored == []
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
    for plugin in ("chrome-devtools", "jubian-go", "ones", "payment", "unity", "wechat"):
        assert not any(name.startswith(f"builtins/{plugin}-mcp/") for name in tracked), plugin


def test_license_is_apache_2():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in text and "Version 2.0, January 2004" in text
    assert (ROOT / "THIRD_PARTY_NOTICES.md").is_file()
    assert 'license = "Apache-2.0"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_bundle_tolerates_missing_business_plugins(tmp_path, monkeypatch):
    bundle = importlib.util.module_from_spec(spec := importlib.util.spec_from_file_location("hygiene_bundle", ROOT / "bundle_files.py"))
    spec.loader.exec_module(bundle)
    for name in bundle.REQUIRED_BUILTINS:
        (tmp_path / "builtins" / name / "src").mkdir(parents=True)
        (tmp_path / "builtins" / name / "src" / "m.py").write_text("x = 1\n")
    monkeypatch.delenv("ANX_REQUIRE_ALL_BUILTINS", raising=False)
    monkeypatch.delenv("MCPM_REQUIRE_ALL_BUILTINS", raising=False)
    assert bundle.present_builtins(tmp_path) == bundle.REQUIRED_BUILTINS
    assert {relative.parts[0] for _, relative in bundle.builtin_files(tmp_path)} == set(bundle.REQUIRED_BUILTINS)
    monkeypatch.setenv("MCPM_REQUIRE_ALL_BUILTINS", "1")
    with pytest.raises(FileNotFoundError):
        bundle.present_builtins(tmp_path)


def test_hub_probes_require_an_explicit_url(monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import hub_args
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    monkeypatch.delenv("ANX_HUB_URL", raising=False)
    monkeypatch.delenv("MCPM_HUB_URL", raising=False)
    with pytest.raises(SystemExit):
        hub_args.parse(hub_args.hub_parser("x"), [])
    assert hub_args.parse(hub_args.hub_parser("x"), ["--url", "https://hub.example.test/mcp/"]).url == "https://hub.example.test"
    monkeypatch.setenv("ANX_HUB_URL", "https://env.example.test")
    assert hub_args.parse(hub_args.hub_parser("x"), []).url == "https://env.example.test"


def _package(entries):
    buffer = io.BytesIO()
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in entries.items()}
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        archive.writestr("SHA256.json", json.dumps(manifest))
    return buffer.getvalue()


def test_upgrade_script_checks_survive_optimisation_and_reject_bad_packages(tmp_path):
    # The upgrade lives in deploy/gateway/upgrade.py (it ships in every gateway zip);
    # scripts/upgrade_cloud_release.py only forwards the legacy command line to it.
    spec = importlib.util.spec_from_file_location("hygiene_gateway_upgrade", ROOT / "deploy/gateway/upgrade.py")
    upgrade = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(upgrade)
    version = b'__version__ = "0.2.0"\n'
    good = _package({"agent_nexus/__init__.py": version, "agent_nexus/cloud.py": b"# gateway\n"})
    package = tmp_path / "server.zip"
    package.write_bytes(good)
    digest = hashlib.sha256(good).hexdigest()
    assert upgrade.verify_package(package, digest) == "0.2.0"
    upgrade.extract(package, tmp_path / "release")
    assert (tmp_path / "release/agent_nexus/cloud.py").read_bytes() == b"# gateway\n"
    with pytest.raises(upgrade.UpgradeError, match="sha256 mismatch"):
        upgrade.verify_package(package, "0" * 64)
    bad = _package({"agent_nexus/__init__.py": version, ".private/enrollment.json": b"{}"})
    package.write_bytes(bad)
    with pytest.raises(upgrade.UpgradeError, match="excluded data"):
        upgrade.verify_package(package, hashlib.sha256(bad).hexdigest())
    for path in ("deploy/gateway/upgrade.py", "scripts/upgrade_cloud_release.py"):
        source = (ROOT / path).read_text(encoding="utf-8")
        assert "assert " not in source and "e636183" not in source, path
