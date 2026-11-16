"""Fail when any public release artifact carries a private-edition payload (design §11.2, §11.4).

Opens every artifact in the given directory (recursing into nested archives) and
fails on:

* a member path with a ``.private`` component, or named ``*enrollment*.json`` or
  ``endpoint-allow``;
* a JSON member that has an ``agent_prefix`` key (only private enrollment profiles do);
* inside a raw ``.exe``: any embedded ZIP member under ``.private/`` or named like an
  enrollment profile (the desktop exe embeds its runtime zip uncompressed);
* an artifact whose own file name says ``private``.

The NSIS ``setup.exe`` is LZMA-compressed and is checked by name only; it installs
the raw ``agent-nexus.exe``, which is checked byte for byte. Findings print the
artifact and member names only. Standard library only.

Usage: ``python scripts/test_no_private_payload.py <artifacts-dir-or-file>...``
"""
from __future__ import annotations

import io
import json
import lzma
import re
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

ARCHIVE_SUFFIXES = (".zip", ".whl", ".deb", ".tar.xz", ".tar.gz", ".tgz", ".tar")
EMBEDDED_PRIVATE = re.compile(rb"(PK\x03\x04.{26}|PK\x01\x02.{42})(?:[^\x00]{0,200}/)?"
                              rb"(\.private/|[^/\x00]{0,80}enrollment[^/\x00]{0,40}\.json|endpoint-allow)", re.S)
MAX_NESTED_BYTES = 512 * 1024 * 1024


def bad_name(name: str) -> str | None:
    parts = PurePosixPath(name.replace("\\", "/")).parts
    base = parts[-1].lower() if parts else ""
    if ".private" in parts:
        return "member under .private/"
    if base == "endpoint-allow":
        return "endpoint allow-list"
    if "enrollment" in base and base.endswith(".json"):
        return "enrollment profile"
    return None


def json_has_prefix(data: bytes) -> bool:
    if b"agent_prefix" not in data:
        return False
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return False
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if "agent_prefix" in item:
                return True
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def members(name: str, data: bytes):
    """Yield (member name, bytes) of an archive, or nothing for a non-archive."""
    lower = name.lower()
    if lower.endswith((".zip", ".whl")) or (data[:4] == b"PK\x03\x04" and not lower.endswith(".exe")):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    yield info.filename, archive.read(info) if info.file_size <= MAX_NESTED_BYTES else b""
    elif lower.endswith(".deb") or data[:8] == b"!<arch>\n":
        offset = 8
        while offset + 60 <= len(data):
            header = data[offset:offset + 60]
            member = header[:16].decode("ascii", "replace").strip().rstrip("/")
            size = int(header[48:58].decode("ascii").strip() or 0)
            body = data[offset + 60:offset + 60 + size]
            offset += 60 + size + (size % 2)
            yield member, body
    elif lower.endswith((".tar.xz", ".tar.gz", ".tgz", ".tar")) or lower.endswith((".tar.zst",)):
        try:
            archive = tarfile.open(fileobj=io.BytesIO(data))
        except (tarfile.TarError, lzma.LZMAError):
            return
        with archive:
            for info in archive.getmembers():
                if info.isfile():
                    handle = archive.extractfile(info)
                    yield info.name, handle.read() if handle and info.size <= MAX_NESTED_BYTES else b""


def scan(name: str, data: bytes, findings: list, depth: int = 0) -> None:
    lower = name.lower()
    if lower.endswith(".exe"):
        if EMBEDDED_PRIVATE.search(data):
            findings.append(f"{name}: embedded private runtime member")
        return
    if lower.endswith(".json") and json_has_prefix(data):
        findings.append(f"{name}: enrollment profile (agent_prefix)")
    if depth > 4:
        return
    try:
        for member, body in members(name, data):
            label = f"{name}!{member}"
            reason = bad_name(member)
            if reason:
                findings.append(f"{label}: {reason}")
            scan(label, body, findings, depth + 1)
    except (zipfile.BadZipFile, tarfile.TarError, lzma.LZMAError, ValueError, EOFError) as error:
        if lower.endswith(ARCHIVE_SUFFIXES) and depth == 0:
            findings.append(f"{name}: unreadable archive ({type(error).__name__})")


def check_paths(paths) -> list[str]:
    findings: list[str] = []
    files = []
    for path in map(Path, paths):
        files += sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
    for path in files:
        if "private" in path.name.lower():
            findings.append(f"{path.name}: private artifact in a public release")
            continue
        scan(path.name, path.read_bytes(), findings)
    return findings


def main(argv=None) -> int:
    paths = list(argv if argv is not None else sys.argv[1:])
    if not paths:
        print("usage: test_no_private_payload.py <artifacts-dir-or-file>...", file=sys.stderr)
        return 2
    findings = check_paths(paths)
    for finding in findings:
        print("private payload:", finding, file=sys.stderr)
    if findings:
        return 1
    print(f"no private payload in {', '.join(map(str, paths))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
