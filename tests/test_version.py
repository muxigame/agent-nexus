"""M2 acceptance: single version source (docs/design.md section 10 and section 13 M2).

* bumping once updates every location;
* hand-made drift makes ``version.py check`` (the CI gate) fail;
* a release tag that differs from the source version fails.
"""
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "version.py"

spec = importlib.util.spec_from_file_location("anx_version_script", SCRIPT)
version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(version)

MANAGED = [version.INIT, version.PYPROJECT, version.UV_LOCK, version.CARGO_TOML, version.CARGO_LOCK,
           version.TAURI_CONF, version.UBUNTU]


@pytest.fixture
def tree(tmp_path):
    for rel in MANAGED:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, target)
    return tmp_path


def run(*args, env=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)


def read(root, rel):
    return (root / rel).read_bytes().decode("utf-8")


def test_repository_is_consistent():
    assert version.problems(ROOT) == []
    result = run("check")
    assert result.returncode == 0, result.stderr


def test_python_package_and_ubuntu_build_use_the_source_version():
    import agent_nexus
    import mcpm

    current = version.read_version(ROOT)
    assert agent_nexus.__version__ == mcpm.__version__ == current
    # pyproject has no literal; tauri.conf.json has no version at all.
    assert "version" not in json.loads(read(ROOT, version.TAURI_CONF))
    ubuntu = read(ROOT, version.UBUNTU)
    assert not re.search(r"^VERSION\s*=\s*[\"']", ubuntu, re.M)

    # build_ubuntu does ``from build_release import ...``; provide it only for this import
    # so no script module leaks into sys.modules for later tests.
    before = set(sys.modules)
    with patch.dict(sys.modules, {"build_release": _load("build_release")}):
        assert _load("build_ubuntu").VERSION == current
    assert ("build_release" in sys.modules) == ("build_release" in before)
    assert ("build_ubuntu" in sys.modules) == ("build_ubuntu" in before)


def test_verify_ubuntu_reads_the_source_version():
    text = read(ROOT, "scripts/verify_ubuntu.py")
    assert not re.search(r"\d+\.\d+\.\d+", text), "verify_ubuntu.py must not carry a version literal"
    assert _load("verify_ubuntu").VERSION == version.read_version(ROOT)


def test_readme_has_no_stale_package_versions():
    readme = read(ROOT, "README.md")
    current = version.read_version(ROOT)
    for found in re.findall(r"agent-nexus(?:-private)?_([0-9][^_\s`]*)_(?:amd64|linux-amd64)", readme):
        assert found == current, f"README names agent-nexus {found}; the source version is {current}"


def _load(name):
    module_spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def test_first_version_is_monotonic_over_legacy():
    parts = tuple(int(x) for x in version.read_version(ROOT).split("-")[0].split("."))
    assert parts >= (0, 2, 0) > (0, 1, 4)


def test_set_updates_every_location_once(tree):
    before = {rel: (tree / rel).read_bytes() for rel in MANAGED}
    result = run("--root", str(tree), "set", "1.4.7")
    assert result.returncode == 0, result.stderr
    assert version.read_version(tree) == "1.4.7"
    assert 'version = "1.4.7"' in read(tree, version.CARGO_TOML).split("[[bin]]")[0]
    assert re.search(r'name = "agent-nexus-desktop"\r?\nversion = "1.4.7"', read(tree, version.CARGO_LOCK))
    assert version.problems(tree) == []
    assert run("--root", str(tree), "check").returncode == 0
    # Only the version fields changed (line endings preserved, nothing else rewritten).
    old = version.read_version(ROOT)
    pkg = 'name = "agent-nexus-desktop"\nversion = "{}"'
    expected = {
        version.INIT: ('__version__ = "{}"'.format(old), '__version__ = "1.4.7"'),
        version.CARGO_TOML: (pkg.format(old), pkg.format("1.4.7")),
        version.CARGO_LOCK: (pkg.format(old), pkg.format("1.4.7")),
    }
    for rel in MANAGED:
        after = (tree / rel).read_bytes()
        if rel in expected:
            src, dst = expected[rel]
            if b"\r\n" in before[rel]:
                src, dst = src.replace("\n", "\r\n"), dst.replace("\n", "\r\n")
            assert before[rel].count(src.encode()) == 1, rel
            assert after == before[rel].replace(src.encode(), dst.encode()), rel
        else:
            assert after == before[rel], rel


def test_set_also_updates_device_manifest_when_present(tree):
    manifest = tree / version.DEVICE_MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{\n  "name": "device",\n  "version": "0.0.1"\n}\n', encoding="utf-8")
    assert any("mcp-service.json" in issue for issue in version.problems(tree))
    assert run("--root", str(tree), "set", "0.3.0").returncode == 0
    assert json.loads(manifest.read_text(encoding="utf-8"))["version"] == "0.3.0"
    assert version.problems(tree) == []


def test_set_rejects_non_semver(tree):
    result = run("--root", str(tree), "set", "v1.2")
    assert result.returncode == 2
    assert version.problems(tree) == []


@pytest.mark.parametrize("rel,old,new,needle", [
    (version.CARGO_TOML, 'version = "{v}"', 'version = "9.9.9"', "Cargo.toml"),
    (version.CARGO_LOCK, 'name = "agent-nexus-desktop"\nversion = "{v}"',
     'name = "agent-nexus-desktop"\nversion = "9.9.9"', "Cargo.lock"),
    (version.TAURI_CONF, '"productName"', '"version": "{v}",\n  "productName"', "tauri.conf.json"),
    (version.PYPROJECT, 'dynamic = ["version"]', 'version = "{v}"', "pyproject.toml"),
    (version.UBUNTU, "VERSION = source_version()", 'VERSION = "{v}"', "build_ubuntu.py"),
    (version.UV_LOCK, 'name = "agent-nexus"\nsource', 'name = "agent-nexus"\nversion = "{v}"\nsource', "uv.lock"),
])
def test_manual_drift_fails_check(tree, rel, old, new, needle):
    current = version.read_version(ROOT)
    text = read(tree, rel)
    old, new = old.format(v=current), new.format(v=current)
    if "\r\n" in text:
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
    assert old in text, (rel, old)
    (tree / rel).write_bytes(text.replace(old, new, 1).encode("utf-8"))
    result = run("--root", str(tree), "check")
    assert result.returncode == 1
    assert needle in result.stderr


def test_tag_mismatch_fails_release_gate(tree):
    current = version.read_version(tree)
    assert run("--root", str(tree), "check", "--tag", f"v{current}").returncode == 0
    bad = run("--root", str(tree), "check", "--tag", "v0.0.1")
    assert bad.returncode == 1 and "tag v0.0.1" in bad.stderr
    # In GitHub Actions the tag comes from GITHUB_REF automatically.
    import os
    env = dict(os.environ, GITHUB_REF="refs/tags/v0.0.1")
    assert run("--root", str(tree), "check", env=env).returncode == 1
    env["GITHUB_REF"] = f"refs/tags/v{current}"
    assert run("--root", str(tree), "check", env=env).returncode == 0
    env["GITHUB_REF"] = "refs/heads/main"
    assert run("--root", str(tree), "check", env=env).returncode == 0


def test_ci_and_release_run_the_gate():
    ci = read(ROOT, ".github/workflows/ci.yml")
    assert "scripts/version.py check" in ci
    release = read(ROOT, ".github/workflows/release.yml")
    assert 'tags: ["v*"]' in release
    assert 'scripts/version.py check --tag "${GITHUB_REF_NAME}"' in release


def test_installed_distribution_metadata_matches():
    from importlib.metadata import version as dist_version

    assert dist_version("agent-nexus") == version.read_version(ROOT)
