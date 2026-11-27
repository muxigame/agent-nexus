"""Every module agent_nexus imports, including imports inside functions, resolves in this interpreter.

Run it with the installed runtime's Python (``uv sync --no-dev``): a package that is
only a dev dependency passes the tests but breaks the shipped app the first time the
code path runs (0.2.3: ``join`` imported httpx, which only the dev group installed).
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

# Imported only on the platform that has them, behind a platform check.
PLATFORM_ONLY = {"winreg", "msvcrt", "_winapi", "winsound", "pywintypes", "win32api", "win32con",
                 "win32gui", "win32process", "win32job", "pythoncom", "fcntl", "termios", "pwd", "grp",
                 "resource", "gi", "AppKit", "Foundation", "Quartz", "objc"}
# Standard-library modules of newer Pythons, imported only as a fallback inside try/except ImportError
# (``compression.zstd`` is 3.14's; agent_nexus/agents/sessions.py prefers the ``zstandard`` package).
PLATFORM_ONLY |= {"compression"}
# Third-party packages installed only on Windows (sys_platform markers in pyproject.toml);
# required there, skipped elsewhere.
WINDOWS_PACKAGES = {"winpty"}
LINUX_PACKAGES = {"Xlib"}
if sys.platform != "win32":
    PLATFORM_ONLY = PLATFORM_ONLY | WINDOWS_PACKAGES
if not sys.platform.startswith("linux"):
    PLATFORM_ONLY = PLATFORM_ONLY | LINUX_PACKAGES


def imported(package: Path) -> dict[str, str]:
    """Top-level module name -> first file that imports it (absolute imports only)."""
    found: dict[str, str] = {}
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                found.setdefault(name.split(".")[0], str(path.relative_to(package.parent)))
    return found


def main() -> int:
    package = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "agent_nexus"
    missing = [(name, where) for name, where in sorted(imported(package).items())
               if name not in PLATFORM_ONLY and name not in sys.stdlib_module_names
               and importlib.util.find_spec(name) is None]
    for name, where in missing:
        print(f"missing runtime module {name!r} (imported by {where})", file=sys.stderr)
    if not missing:
        print("ok: every module agent_nexus imports is installed")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
