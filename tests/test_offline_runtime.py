"""The offline cache must hold exactly what each service's `uv run` installs."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("offline_runtime", ROOT / "scripts" / "offline_runtime.py")
offline_runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offline_runtime)


def test_sync_flags_follow_the_service_command():
    flags = offline_runtime.sync_flags
    assert flags(["uv", "run", "python", "-m", "x"]) == []
    assert flags(["uv", "run", "--frozen", "--no-dev", "python", "-m", "x"]) == ["--no-dev"]
    assert flags(["uv", "run", "--extra", "mcp", "python", "-m", "x"]) == ["--extra", "mcp"]
    assert flags(["C:/tools/uv.exe", "run", "--with", "rich", "--extra", "a", "x"]) == ["--extra", "a"]
    assert flags(["node", "server.js"]) is None


def test_every_packaged_python_builtin_is_cached(tmp_path):
    for name, cwd, command in (("plain", ".", ["uv", "run", "python", "-m", "a"]),
                               ("nested", "Server", ["uv", "run", "--extra", "mcp", "python", "x.py"]),
                               ("node", ".", ["node", "x.js"])):
        project = tmp_path / "builtins" / name / cwd
        project.mkdir(parents=True)
        (project / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        (project / "uv.lock").write_text("", encoding="utf-8")
        (tmp_path / "builtins" / name / "mcp-service.json").write_text(
            json.dumps({"command": command, "cwd": cwd}), encoding="utf-8")
    found = {label: (path.relative_to(tmp_path.resolve()).as_posix(), flags)
             for label, path, flags in offline_runtime.projects(tmp_path.resolve())}
    assert found == {"agent-nexus": (".", []), "plain": ("builtins/plain", []),
                     "nested": ("builtins/nested/Server", ["--extra", "mcp"])}
