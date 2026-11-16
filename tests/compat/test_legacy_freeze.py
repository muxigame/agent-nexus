"""The frozen legacy package must stay exactly what the compat matrix was built on."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

COMPAT = Path(__file__).resolve().parent
LEGACY = COMPAT / "legacy_mcpm"
PROVENANCE = json.loads((COMPAT / "LEGACY_PROVENANCE.json").read_text(encoding="utf-8"))
URL = re.compile(rb"(wss|https)://[A-Za-z0-9.-]+")


def frozen_files():
    return {path.relative_to(LEGACY).as_posix(): path for path in sorted(LEGACY.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts}


def test_frozen_package_matches_its_manifest():
    files = frozen_files()
    assert set(files) == set(PROVENANCE["files"])
    for name, path in files.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == PROVENANCE["files"][name], name


def test_frozen_package_is_importable_and_distinct_from_current():
    import legacy_mcpm.cloud
    import legacy_mcpm.federation
    import agent_nexus.federation

    assert legacy_mcpm.federation is not agent_nexus.federation
    assert Path(legacy_mcpm.cloud.__file__).resolve().parent == LEGACY


def _source(name: str) -> bytes | None:
    commit = PROVENANCE["source_commit"]
    try:
        result = subprocess.run(["git", "show", f"{commit}:{PROVENANCE['source_path']}/{name}"],
                                cwd=COMPAT, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def test_frozen_package_matches_source_commit_when_history_is_available():
    # The public repository has an orphan history without the baseline commit;
    # the manifest test above still guards the bytes there.
    if _source("__init__.py") is None:
        pytest.skip("baseline commit is not in this clone")
    for name, path in frozen_files().items():
        original, frozen = _source(name), path.read_bytes()
        assert original is not None, name
        if name in PROVENANCE["scrubbed"]:
            # Only URLs may differ, and the scrubbed copy must name no real host.
            assert URL.sub(b"<url>", original) == URL.sub(b"<url>", frozen), name
            assert all(host.endswith(b".invalid") for host in re.findall(rb"(?:wss|https)://([A-Za-z0-9.-]+)", frozen))
        else:
            assert original == frozen, name
