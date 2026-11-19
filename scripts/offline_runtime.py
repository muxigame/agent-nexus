"""Bundle Python and every locked dependency so a client (Windows or Ubuntu) starts offline.

A fresh machine used to download a CPython build (from GitHub) and roughly
two hundred packages (from PyPI) on first start, which takes very long on a
poor connection. The package now carries ``tools/python`` (a uv-managed
CPython) and ``tools/uv-cache`` (uv's cache, filled by syncing each project
exactly the way its service starts). agent-nexus and the desktop app point
``UV_PYTHON_INSTALL_DIR`` and ``UV_CACHE_DIR`` there, so ``uv run`` installs
from disk. Optional extras that a service does not start with (the device plugin's
multi-gigabyte ``vision``) are deliberately left out.

On Unix uv also links each cached wheel to its unpacked archive with an
absolute symlink into the build machine's cache, which would dangle on a
client. uv finds a cached wheel through the archive id recorded in its
``.http`` entry, so those links are dropped, and the offline proof runs against
exactly that layout.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

PYTHON = "3.12"
# Flags of `uv run` that also select what `uv sync` installs.
VALUE_FLAGS = {"--extra", "--group", "--only-group", "--no-group"}
SWITCH_FLAGS = {"--no-dev", "--all-extras", "--all-groups", "--no-default-groups"}


def sync_flags(command: list[str]) -> list[str] | None:
    """The install selection of a `uv run ...` service command, or None if it is not one."""
    if len(command) < 2 or Path(command[0]).stem != "uv" or command[1] != "run":
        return None
    flags, rest = [], command[2:]
    while rest and rest[0].startswith("--"):
        flag = rest.pop(0)
        if flag in VALUE_FLAGS and rest:
            flags += [flag, rest.pop(0)]
        elif flag in SWITCH_FLAGS:
            flags.append(flag)
        elif flag in {"--python", "--project", "--directory", "--with", "--package"} and rest:
            rest.pop(0)
    return flags


def projects(stage: Path) -> list[tuple[str, Path, list[str]]]:
    """(label, project dir, sync flags) for the manager and each Python builtin."""
    found = [("agent-nexus", stage, [])]
    for spec in sorted((stage / "builtins").glob("*/mcp-service.json")):
        service = json.loads(spec.read_text(encoding="utf-8-sig"))
        flags = sync_flags(list(service.get("command", [])))
        project = (spec.parent / service.get("cwd", ".")).resolve()
        if flags is not None and (project / "pyproject.toml").is_file() and (project / "uv.lock").is_file():
            found.append((spec.parent.name, project, flags))
    return found


#: The uv-managed CPython build of the platform the package is built on (and for).
PLATFORM = "windows-x86_64-none" if os.name == "nt" else "linux-x86_64-gnu"


def managed_python(uv: str) -> Path:
    base = Path(subprocess.run([uv, "python", "dir"], check=True, capture_output=True, text=True).stdout.strip())
    candidates = sorted(base.glob(f"cpython-{PYTHON}.*-{PLATFORM}"),
                        key=lambda p: tuple(int(x) for x in p.name.split("-")[1].split(".")))
    if not candidates:
        subprocess.run([uv, "python", "install", PYTHON], check=True)
        return managed_python(uv)
    return candidates[-1]


def _sync(uv: str, stage: Path, project: Path, flags: list[str], venv: Path, offline: bool) -> None:
    env = {**os.environ, "UV_CACHE_DIR": str(stage / "tools" / "uv-cache"),
           "UV_PYTHON_INSTALL_DIR": str(stage / "tools" / "python"), "UV_PYTHON_PREFERENCE": "only-managed",
           "UV_PROJECT_ENVIRONMENT": str(venv), "UV_LINK_MODE": "copy", "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("VIRTUAL_ENV", None)
    if offline:
        env["UV_OFFLINE"] = "1"
    subprocess.run([uv, "sync", "--frozen", "--python", PYTHON, "--project", str(project), *flags],
                   check=True, env=env, stdout=subprocess.DEVNULL)


def prepare(stage: Path, uv: str) -> dict:
    """Fill stage/tools/python and stage/tools/uv-cache, then prove they suffice offline."""
    source = managed_python(uv)
    target = stage / "tools" / "python" / source.name
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    report = {"python": source.name, "projects": {}}
    with tempfile.TemporaryDirectory(prefix="anx-offline-venvs-") as scratch:
        for label, project, flags in projects(stage):
            _sync(uv, stage, project, flags, Path(scratch) / f"online-{label}", offline=False)
            report["projects"][label] = flags
        _drop_machine_specific(uv, stage / "tools" / "uv-cache")
        _drop_links(stage / "tools" / "uv-cache")
        # The proof: a cold environment for every project, with the network off.
        for label, project, flags in projects(stage):
            _sync(uv, stage, project, flags, Path(scratch) / f"offline-{label}", offline=True)
    # The proof rebuilt the packaged projects into the cache; drop them again.
    _drop_machine_specific(uv, stage / "tools" / "uv-cache")
    _drop_links(stage / "tools" / "uv-cache")
    return report


def _drop_links(cache: Path) -> None:
    """Remove the cache's symlinks (Unix only: absolute links into this build machine's cache)."""
    for link in [path for path in cache.rglob("*") if path.is_symlink()]:
        link.unlink()


def _drop_machine_specific(uv: str, cache: Path) -> None:
    """Remove cache entries that can never hit on another machine.

    Builds of the packaged projects themselves are keyed by their source path,
    so a client always rebuilds them from source with the cached build
    backends; interpreter probes are per-machine too. Pruning then drops the
    unpacked archives that only those entries referenced.
    """
    for stale in [*cache.glob("sdists-v*/editable"), *cache.glob("sdists-v*/path"), *cache.glob("interpreter-v*")]:
        shutil.rmtree(stale)
    # uv's marker that keeps git out of its source cache; uv recreates it, and
    # release verification rejects any `.git` member in a client package.
    for marker in cache.glob("*/.git"):
        marker.unlink() if marker.is_file() else shutil.rmtree(marker)
    subprocess.run([uv, "cache", "prune"], check=True, env={**os.environ, "UV_CACHE_DIR": str(cache)},
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def bundle(files: list[tuple[Path, str]], uv: str, scratch: Path) -> list[tuple[Path, str]]:
    """Release files plus the offline runtime, staged under ``scratch``."""
    stage = scratch / "stage"
    for path, name in files:
        destination = stage / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    report = prepare(stage, uv)
    links = [path for folder in (stage / "tools" / "python", stage / "tools" / "uv-cache")
             for path in folder.rglob("*") if path.is_symlink()]
    if links:
        raise RuntimeError(f"offline runtime still has symlinks, e.g. {links[0]}")
    extra = [(path, path.relative_to(stage).as_posix())
             for folder in (stage / "tools" / "python", stage / "tools" / "uv-cache")
             for path in sorted(folder.rglob("*"))
             # Running the staged interpreter (uv sync, build backends) leaves bytecode
             # behind; clients compile their own and release verification rejects it.
             if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"]
    longest = max((len(name) for _, name in extra), default=0)
    print(f"offline runtime: {report['python']}, {len(report['projects'])} projects, {len(extra)} files, "
          f"longest path {longest} chars")
    return extra
