"""The ``search`` capability: find files by name (``file_glob``) and by content (``file_grep``).

Both work without a shell, so a caller does not have to guess whether the
device's shell is bash, PowerShell or cmd, or how it quotes a regex.
``file_grep`` uses ripgrep when it is on PATH (fast, honours ``.gitignore``)
and falls back to a plain Python walk otherwise; the result has the same shape
either way and says which engine ran.

Paths in results are relative to the searched directory, with ``/``
separators, so they can be passed straight back to ``file_read`` together with
``path``.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator

from .policy import Policy

#: Directories nobody means to search: VCS data, dependency trees, caches.
#: A pattern that names one of them explicitly still reaches it.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".idea", ".gradle", ".next",
})
MAX_RESULTS = 2000
MAX_SCAN_ENTRIES = 300_000
MAX_GREP_FILE_BYTES = 20 * 1024 * 1024
MAX_LINE_CHARS = 400
SEARCH_TIMEOUT_S = 60.0
GREP_MODES = ("content", "files", "count")
CASE_INSENSITIVE_FS = sys.platform in ("win32", "darwin")


class SearchError(ValueError):
    pass


# -- glob ------------------------------------------------------------------
def glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile a glob into a regex over ``/``-separated relative paths.

    ``*`` and ``?`` stay inside one path segment, ``**`` spans segments,
    ``[abc]``/``[!abc]`` are character classes and ``{a,b}`` alternatives.
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    depth = 0
    while i < n:
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = pattern.find("]", i + 2 if pattern[i + 1:i + 2] in ("!", "]") else i + 1)
            if end == -1:
                out.append(re.escape(char))
            else:
                body = pattern[i + 1:end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = end
        elif char == "{":
            depth += 1
            out.append("(?:")
        elif char == "}" and depth:
            depth -= 1
            out.append(")")
        elif char == "," and depth:
            out.append("|")
        else:
            out.append(re.escape(char))
        i += 1
    if depth:
        raise SearchError(f"unbalanced '{{' in pattern {pattern!r}")
    flags = re.IGNORECASE if CASE_INSENSITIVE_FS else 0
    return re.compile("(?:" + "".join(out) + r")\Z", flags)


class _PathMatcher:
    """A glob without ``/`` matches file names at any depth; with ``/`` it matches the relative path."""

    def __init__(self, pattern: str) -> None:
        pattern = pattern.replace("\\", "/").lstrip("/")
        if pattern.startswith("./"):
            pattern = pattern[2:]
        if not pattern:
            raise SearchError("pattern must not be empty")
        self.by_name = "/" not in pattern
        self.regex = glob_regex(pattern)
        lowered = pattern.lower()
        self.named_dirs = {name for name in SKIP_DIRS if name in lowered}

    def matches(self, relative: str) -> bool:
        return bool(self.regex.match(relative.rsplit("/", 1)[-1] if self.by_name else relative))

    def prune(self, name: str) -> bool:
        return name in SKIP_DIRS and name not in self.named_dirs


def _walk(base: Path, prune, deadline: float) -> Iterator[tuple[str, os.DirEntry]]:
    """(relative path, entry) for every file under ``base``; stops at the scan budget or deadline."""
    stack = [(base, "")]
    scanned = 0
    while stack:
        directory, prefix = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            scanned += 1
            if scanned > MAX_SCAN_ENTRIES or time.monotonic() > deadline:
                raise _Budget()
            relative = prefix + entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if not prune(entry.name):
                    stack.append((Path(entry.path), relative + "/"))
            else:
                yield relative, entry


class _Budget(Exception):
    pass


def _base_dir(policy: Policy, path: str | None) -> Path:
    base = policy.resolve_path(path or ".")
    if not base.is_dir():
        raise SearchError(f"not a directory: {base}")
    return base


def file_glob(policy: Policy, pattern: str, path: str | None = None, limit: int = 200) -> dict[str, Any]:
    """Files under ``path`` matching ``pattern``, newest first."""
    base = _base_dir(policy, path)
    matcher = _PathMatcher(pattern)
    limit = max(1, min(int(limit), MAX_RESULTS))
    found: list[tuple[float, str]] = []
    incomplete = False
    try:
        for relative, entry in _walk(base, matcher.prune, time.monotonic() + SEARCH_TIMEOUT_S):
            if matcher.matches(relative):
                try:
                    found.append((entry.stat(follow_symlinks=False).st_mtime, relative))
                except OSError:
                    found.append((0.0, relative))
    except _Budget:
        incomplete = True
    found.sort(key=lambda item: (-item[0], item[1]))
    result: dict[str, Any] = {"status": "ok", "path": str(base), "files": [name for _, name in found[:limit]],
                              "total": len(found), "truncated": len(found) > limit}
    if incomplete:
        result["incomplete"] = "the search stopped early (too many entries or too slow); narrow path or pattern"
    return result


# -- grep ------------------------------------------------------------------
def _clip(line: str) -> str:
    line = line.rstrip("\r\n")
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + " [...]"


def _ripgrep() -> str | None:
    return shutil.which("rg")


def file_grep(policy: Policy, pattern: str, path: str | None = None, glob: str | None = None,
              ignore_case: bool = False, fixed_strings: bool = False, context: int = 0,
              output: str = "content", limit: int = 200, max_chars: int = 60_000,
              engine: str | None = None) -> dict[str, Any]:
    if not pattern:
        raise SearchError("pattern must not be empty")
    if output not in GREP_MODES:
        raise SearchError(f"output must be one of {', '.join(GREP_MODES)}")
    context = max(0, min(int(context), 20))
    limit = max(1, min(int(limit), MAX_RESULTS))
    target = policy.resolve_path(path or ".")
    if not target.exists():
        raise SearchError(f"no such file or directory: {target}")
    use_rg = engine != "python" and _ripgrep() is not None
    if engine == "rg" and not use_rg:
        raise SearchError("ripgrep (rg) is not on PATH")
    if use_rg:
        items, incomplete = _grep_rg(target, pattern, glob, ignore_case, fixed_strings, context, output)
    else:
        items, incomplete = _grep_python(target, pattern, glob, ignore_case, fixed_strings, context, output)
    result: dict[str, Any] = {"status": "ok", "path": str(target), "engine": "rg" if use_rg else "python",
                              "output": output}
    total = len(items)
    kept: list[Any] = []
    chars = 0
    for item in items[:limit]:
        size = len(item) if isinstance(item, str) else len(item[0]) + 8
        if kept and chars + size > max_chars:
            break
        kept.append(item)
        chars += size
    if output == "count":
        result["counts"] = {name: count for name, count in kept}
        result["matches"] = sum(count for _, count in items)
    elif output == "files":
        result["files"] = kept
    else:
        result["lines"] = kept
    result["total"] = total
    result["truncated"] = len(kept) < total
    if incomplete:
        result["incomplete"] = incomplete
    return result


def _grep_rg(target: Path, pattern: str, glob: str | None, ignore_case: bool, fixed: bool,
             context: int, output: str) -> tuple[list[Any], str | None]:
    argv = [_ripgrep() or "rg", "--no-config", "--color", "never", "--no-heading", "--with-filename",
            "--null", "--path-separator", "/", "--max-columns", str(MAX_LINE_CHARS), "--max-columns-preview",
            "--max-filesize", str(MAX_GREP_FILE_BYTES)]
    for name in sorted(SKIP_DIRS):
        if not glob or name not in glob:
            argv += ["--glob", f"!{name}"]
    if ignore_case:
        argv.append("--ignore-case")
    if fixed:
        argv.append("--fixed-strings")
    if glob:
        argv += ["--glob", glob]
    if output == "files":
        argv.append("--files-with-matches")
    elif output == "count":
        argv.append("--count")
    else:
        argv.append("--line-number")
        if context:
            argv += ["--context", str(context)]
    if target.is_dir():
        cwd, where = target, "."
    else:
        cwd, where = target.parent, target.name
    argv += ["--regexp", pattern, "--", where]
    try:
        proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, timeout=SEARCH_TIMEOUT_S,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        stdout, note = exc.stdout or b"", f"ripgrep timed out after {SEARCH_TIMEOUT_S:.0f}s; narrow path or glob"
    else:
        stdout, note = proc.stdout, None
        if proc.returncode == 2 and not stdout:
            raise SearchError(proc.stderr.decode("utf-8", "replace").strip() or "ripgrep failed")
    text = stdout.decode("utf-8", "replace")
    if output == "files":
        # With --files-with-matches, --null ends each path with NUL instead of a newline.
        names = [name.strip("\r\n") for name in text.split("\0")]
        return [name[2:] if name.startswith("./") else name for name in names if name], note
    items: list[Any] = []
    for raw in text.splitlines():
        if raw == "--":
            items.append("--")
            continue
        name, _, rest = raw.partition("\0")
        if name.startswith("./"):
            name = name[2:]
        if output == "files":
            items.append(name)
        elif output == "count":
            items.append((name, int(rest or 0)))
        else:
            match = re.match(r"(\d+)([:-])(.*)", rest, re.DOTALL)
            if match:
                items.append(f"{name}{match.group(2)}{match.group(1)}{match.group(2)}{_clip(match.group(3))}")
    return items, note


def _grep_python(target: Path, pattern: str, glob: str | None, ignore_case: bool, fixed: bool,
                 context: int, output: str) -> tuple[list[Any], str | None]:
    try:
        regex = re.compile(re.escape(pattern) if fixed else pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise SearchError(f"invalid regex: {exc}") from exc
    matcher = _PathMatcher(glob) if glob else None

    def prune(name: str) -> bool:
        return matcher.prune(name) if matcher else name in SKIP_DIRS

    if target.is_file():
        files: Iterator[tuple[str, Any]] = iter([(target.name, target)])
    else:
        files = ((relative, entry.path) for relative, entry in _walk(target, prune, time.monotonic() + SEARCH_TIMEOUT_S)
                 if matcher is None or matcher.matches(relative))
    items: list[Any] = []
    note = None
    try:
        for relative, full in files:
            try:
                if os.path.getsize(full) > MAX_GREP_FILE_BYTES:
                    continue
                with open(full, "rb") as handle:
                    data = handle.read()
            except OSError:
                continue
            if b"\0" in data[:8192]:
                continue
            lines = data.decode("utf-8", "replace").splitlines()
            hits = [number for number, line in enumerate(lines) if regex.search(line)]
            if not hits:
                continue
            if output == "files":
                items.append(relative)
            elif output == "count":
                items.append((relative, len(hits)))
            else:
                shown = sorted({n for hit in hits for n in range(max(0, hit - context), min(len(lines), hit + context + 1))})
                hit_set = set(hits)
                previous = None
                for number in shown:
                    if previous is not None and number != previous + 1:
                        items.append("--")
                    mark = ":" if number in hit_set else "-"
                    items.append(f"{relative}{mark}{number + 1}{mark}{_clip(lines[number])}")
                    previous = number
                if context:
                    items.append("--")
            if len(items) > MAX_RESULTS * 2:
                note = "stopped after too many matches; narrow the pattern"
                break
    except _Budget:
        note = "the search stopped early (too many files or too slow); narrow path or glob"
    if items and items[-1] == "--":
        items.pop()
    return items, note
