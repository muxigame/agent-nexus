"""paths.default_root inside an installed versioned runtime (<root>/runtime/<tag>/) resolves to <root>."""
from __future__ import annotations

from agent_nexus import paths


def test_a_versioned_runtime_resolves_to_the_data_root(tmp_path):
    root = tmp_path / "AgentNexus"
    runtime = root / "runtime" / "0.9.0-abcd1234"
    package = runtime / ".venv" / "Lib" / "site-packages" / "agent_nexus"
    package.mkdir(parents=True)
    (root / "agent-nexus.json").write_text("{}")
    (runtime / "agent-nexus.json").write_text("{}")  # the bundled default config
    (runtime / paths.RUNTIME_MARKER).write_text("")
    elsewhere = tmp_path / "work"
    elsewhere.mkdir()
    assert paths.default_root({}, cwd=elsewhere, package=package) == root
    # a working directory inside the runtime: still the root
    assert paths.default_root({}, cwd=runtime, package=package) == root
    # an unfinished runtime (no marker) is not trusted as one
    (runtime / paths.RUNTIME_MARKER).unlink()
    assert paths.default_root({}, cwd=elsewhere, package=package) == runtime


def test_a_development_tree_is_unchanged(tmp_path):
    tree = tmp_path / "agent-nexus"
    package = tree / "agent_nexus"
    package.mkdir(parents=True)
    (tree / "agent-nexus.json").write_text("{}")
    elsewhere = tmp_path / "work"
    elsewhere.mkdir()
    assert paths.default_root({}, cwd=elsewhere, package=package) == tree
    assert paths.default_root({"ANX_ROOT": str(elsewhere)}, cwd=elsewhere, package=package) == elsewhere
