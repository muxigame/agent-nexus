"""Fail when files that would be published carry infrastructure details or secrets.

Only generic rules live in this repository. Deployment-specific patterns (real
host names, addresses, account names) are supplied at run time and never
committed:

* ``SCRUB_PATTERNS`` environment variable: newline-separated regular
  expressions (in CI this is a repository secret), and/or
* ``--patterns-file FILE``: the same format, one regex per line, ``#`` comments.

Exceptions live in ``.scrub-allow``; every line is
``<path-glob> <rule-id> # <reason>`` and a missing reason is itself an error.
Findings print the file, line and rule id only, never the matched text, so the
check is safe to run in public CI logs.

Usage: ``uv run python scripts/scrub_check.py [--root DIR] [--patterns-file F] [paths...]``
"""
from __future__ import annotations

import argparse
import fnmatch
import ipaddress
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 5 * 1024 * 1024

# Documentation ranges (RFC 5737), loopback/unspecified and RFC 1918 private space.
ALLOWED_NETWORKS = [ipaddress.ip_network(net) for net in (
    "127.0.0.0/8", "0.0.0.0/32", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "255.255.255.255/32")]
PLACEHOLDER_USERS = {"u", "user", "username", "me", "example", "<user>", "%username%", "$env:username",
                     "public", "default", "all users", "runneradmin", "you", "name"}
IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.])")
WINDOWS_HOME = re.compile(r"[A-Za-z]:(?:\\\\|\\|/)+Users(?:\\\\|\\|/)+([^\\/\s\"'`<>|*?:]+)", re.IGNORECASE)
POSIX_HOME = re.compile(r"(?<![\w.~])/home/([A-Za-z0-9._-]+)")
GENERIC_RULES = {
    "root-login": re.compile(r"\broot@[A-Za-z0-9]"),
    "private-key": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "github-token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{40,}"),
    "openai-key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}"),
    "aws-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "slack-token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
}
FORBIDDEN_PATHS = {
    "private-path": ("secrets/*", "*/secrets/*", ".private/*", "*/.private/*", "credentials-vault/*"),
    "enrollment-file": ("*enrollment.json", "*build-enrollment*", "*endpoint-allow"),
    "key-file": ("*.pem", "*.key", "*.pfx", "*.p12", "*.token", "*.env", "*apikey*", "*api-key*", "id_rsa*", "id_ed25519*"),
    "runtime-data": ("*.sqlite3", "*.db", "mcp-manager.json", "agent-nexus.json", "*/.mcp-manager/*", "*/.anx/*"),
}
BINARY_SUFFIXES = {".png", ".ico", ".icns", ".jpg", ".jpeg", ".gif", ".webp", ".exe", ".dll", ".zip", ".whl", ".woff", ".woff2"}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}" if self.line else f"{self.path}: {self.rule}"


@dataclass(frozen=True)
class Allow:
    glob: str
    rule: str
    reason: str


def load_allow(path: Path) -> tuple[list[Allow], list[str]]:
    allows, errors = [], []
    if not path.is_file():
        return allows, errors
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        body, _, reason = line.partition("#")
        fields = body.split()
        if len(fields) != 2 or not reason.strip():
            errors.append(f"{path.name}:{number}: expected '<path-glob> <rule-id> # <reason>'")
            continue
        allows.append(Allow(fields[0], fields[1], reason.strip()))
    return allows, errors


def load_private_patterns(patterns_file: Path | None) -> list[re.Pattern]:
    sources = []
    if os.environ.get("SCRUB_PATTERNS"):
        sources.append(os.environ["SCRUB_PATTERNS"])
    if patterns_file is not None:
        sources.append(patterns_file.read_text(encoding="utf-8"))
    patterns = []
    for source in sources:
        for line in source.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(re.compile(line, re.IGNORECASE))
    return patterns


def candidate_files(root: Path) -> list[str]:
    """Tracked files plus untracked files that are not ignored (what a commit could add)."""
    result = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                            cwd=root, capture_output=True, check=True)
    names = [name for name in result.stdout.decode("utf-8").split("\0") if name]
    return sorted(name for name in set(names) if (root / name).is_file())


def _ip_flagged(text: str) -> bool:
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return not any(address in network for network in ALLOWED_NETWORKS)


def scan_text(name: str, text: str, private: list[re.Pattern]) -> list[Finding]:
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        if any(_ip_flagged(match.group(1)) for match in IPV4.finditer(line)):
            findings.append(Finding(name, number, "ipv4"))
        for match in WINDOWS_HOME.finditer(line):
            if match.group(1).lower() not in PLACEHOLDER_USERS:
                findings.append(Finding(name, number, "user-home"))
                break
        for match in POSIX_HOME.finditer(line):
            if match.group(1).lower() not in PLACEHOLDER_USERS:
                findings.append(Finding(name, number, "user-home"))
                break
        for rule, pattern in GENERIC_RULES.items():
            if pattern.search(line):
                findings.append(Finding(name, number, rule))
        for index, pattern in enumerate(private, 1):
            if pattern.search(line):
                findings.append(Finding(name, number, f"private-pattern-{index}"))
    return findings


def scan_path(name: str) -> list[Finding]:
    findings = []
    posix = PurePosixPath(name).as_posix()
    for rule, globs in FORBIDDEN_PATHS.items():
        if any(fnmatch.fnmatch(posix, glob) for glob in globs):
            findings.append(Finding(name, 0, rule))
    return findings


def scan(root: Path, names: list[str], private: list[re.Pattern]) -> list[Finding]:
    findings = []
    for name in names:
        path = root / name
        findings.extend(scan_path(name))
        size = path.stat().st_size
        if size > MAX_BYTES:
            findings.append(Finding(name, 0, "file-too-large"))
            continue
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue
        findings.extend(scan_text(name, data.decode("utf-8", errors="replace"), private))
    return findings


def apply_allow(findings: list[Finding], allows: list[Allow]) -> tuple[list[Finding], set[Allow]]:
    kept, used = [], set()
    for finding in findings:
        match = next((allow for allow in allows
                      if fnmatch.fnmatch(finding.path, allow.glob) and allow.rule in (finding.rule, "*")
                      and not finding.rule.startswith("private-pattern")), None)
        if match is None:
            kept.append(finding)
        else:
            used.add(match)
    return kept, used


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", help="files to scan (default: every committable file)")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--patterns-file", type=Path, help="private patterns, one regex per line")
    parser.add_argument("--allow-file", type=Path, help="default: <root>/.scrub-allow")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    allows, errors = load_allow(args.allow_file or root / ".scrub-allow")
    names = args.paths or candidate_files(root)
    findings, used = apply_allow(scan(root, names, load_private_patterns(args.patterns_file)), allows)
    for error in errors:
        print(error, file=sys.stderr)
    for finding in findings:
        print(finding, file=sys.stderr)
    if not args.paths:
        for allow in allows:
            if allow not in used:
                print(f".scrub-allow: unused exception {allow.glob} {allow.rule}", file=sys.stderr)
    status = 1 if findings or errors else 0
    print(f"scrub_check: {len(names)} files, {len(findings)} findings, {len(errors)} allow-file errors")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
