"""Explicit release allowlist. Runtime data and credentials are never bundled.

Only ``REQUIRED_BUILTINS`` live in this repository. The business plugins in
``OPTIONAL_BUILTINS`` are maintained in separate private repositories and are
bundled only when a local checkout happens to be present under ``builtins/``.
Set ``ANX_REQUIRE_ALL_BUILTINS=1`` (or the legacy ``MCPM_REQUIRE_ALL_BUILTINS=1``)
to restore the old behaviour of failing when any of them is missing.
"""
import os
from pathlib import Path

REQUIRED_BUILTINS = ("mcpkit", "device")
OPTIONAL_BUILTINS = ("ones-mcp", "payment-mcp", "wechat-mcp", "chrome-devtools-mcp", "unity-mcp", "jubian-go-mcp")
BUILTINS = REQUIRED_BUILTINS + OPTIONAL_BUILTINS
EXCLUDED_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "tests", "logs", "secrets", "runtime", "target"}
TOP_FILES = {"pyproject.toml", "uv.lock", "mcp-service.json", "README.md", "LICENSE", "LICENSE.md",
             "favicon.png", "favicon.svg", "favicon.ico", "config.example.json", "package.json", "package-lock.json",
             "THIRD_PARTY_NOTICES.md"}
SOURCE_DIRS = {"src", "wecom_reader", "THIRD_PARTY_LICENSES"}


def _require_all() -> bool:
    for name in ("ANX_REQUIRE_ALL_BUILTINS", "MCPM_REQUIRE_ALL_BUILTINS"):
        value = os.environ.get(name)
        if value is not None:
            return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def present_builtins(root: Path, *, require_all: bool | None = None):
    """Return the builtin directory names to bundle, in a stable order."""
    strict = _require_all() if require_all is None else require_all
    found = []
    for name in BUILTINS:
        if (root / "builtins" / name).is_dir():
            found.append(name)
        elif name in REQUIRED_BUILTINS or strict:
            raise FileNotFoundError(f"missing builtin: {name}")
    return tuple(found)


def builtin_files(root: Path, *, require_all: bool | None = None):
    for name in present_builtins(root, require_all=require_all):
        base = root / "builtins" / name
        if name == "unity-mcp":
            roots = [base / "Server" / "src"]
            files = [base / "mcp-service.json", base / "favicon.png", base / "README.md",
                     base / "Server" / "pyproject.toml", base / "Server" / "uv.lock"]
        else:
            roots = [base / source for source in SOURCE_DIRS if (base / source).is_dir()]
            files = [base / name for name in TOP_FILES]
        if name == "chrome-devtools-mcp":
            # Upstream ships a bundled JS runtime, so no npm install is needed
            # to start the packaged DevTools bridge (Node remains a runtime prerequisite).
            roots += [base / "node_modules" / "chrome-devtools-mcp" / "build"]
            files += [base / "node_modules" / "chrome-devtools-mcp" / "package.json",
                      base / "node_modules" / "chrome-devtools-mcp" / "LICENSE"]
        for path in files:
            if path.is_file():
                yield path, Path(name) / path.relative_to(base)
        for source in roots:
            if not source.is_dir():
                continue
            for path in sorted(source.rglob("*")):
                relative = path.relative_to(source)
                if path.is_file() and not any(part in EXCLUDED_DIRS for part in relative.parts) and path.suffix not in {".pyc", ".pyo"}:
                    yield path, Path(name) / path.relative_to(base)
