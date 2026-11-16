"""Service log files: rotation, tail, and incremental reads for live follow."""
from __future__ import annotations

import logging
import os
from pathlib import Path

ROTATE_BYTES = 5 * 1024 * 1024
KEEP = 3


def configure_process_logging() -> None:
    """Timestamp mcpm's own warnings; without a handler Python prints bare messages."""
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s",
                                               "%Y-%m-%d %H:%M:%S"))
        root.addHandler(handler)
        root.setLevel(logging.WARNING)


def rotate(path: Path) -> None:
    """Rotate ``path`` before a new process starts writing to it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not path.exists() or path.stat().st_size < ROTATE_BYTES:
            return
        for i in range(KEEP - 1, 0, -1):
            older = path.with_name(f"{path.name}.{i}")
            if older.exists():
                os.replace(older, path.with_name(f"{path.name}.{i + 1}"))
        os.replace(path, path.with_name(f"{path.name}.1"))
    except OSError:
        # Windows refuses to rename a file another process still holds open.
        # Appending to the big file is better than failing the start.
        pass


def tail(path: Path, lines: int = 400, max_bytes: int = 2 * 1024 * 1024) -> tuple[str, int]:
    """Last ``lines`` lines of ``path`` and the file size to follow from."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = min(size, max(64_000, lines * 512), max_bytes)
            fh.seek(size - block)
            data = fh.read(block)
    except OSError:
        return "", 0
    parts = data.decode("utf-8", errors="replace").splitlines()
    if block < size and parts:
        parts = parts[1:]  # the first line of a mid-file block is partial
    return "\n".join(parts[-lines:]), size


def read_from(path: Path, offset: int, max_bytes: int = 512_000) -> tuple[str, int]:
    """Complete lines appended since ``offset``, and the new offset."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size < offset:
                offset = 0  # truncated or rotated underneath us
            if size == offset:
                return "", offset
            fh.seek(offset)
            data = fh.read(min(size - offset, max_bytes))
    except OSError:
        return "", offset
    cut = data.rfind(b"\n")
    if cut == -1:
        if len(data) < max_bytes:
            return "", offset  # wait for the line to finish
        cut = len(data) - 1
    chunk = data[: cut + 1]
    return chunk.decode("utf-8", errors="replace"), offset + len(chunk)


def use_bundled_runtime(root: Path) -> None:
    """Point uv at the Python and package cache shipped with an offline package.

    Every managed service starts through ``uv run`` and inherits this
    environment, so a machine with a slow or no connection never downloads.
    An explicit setting from the user always wins.
    """
    python = root / "tools" / "python"
    cache = root / "tools" / "uv-cache"
    if python.is_dir():
        os.environ.setdefault("UV_PYTHON_INSTALL_DIR", str(python))
        os.environ.setdefault("UV_PYTHON_PREFERENCE", "managed")
    if cache.is_dir():
        os.environ.setdefault("UV_CACHE_DIR", str(cache))
