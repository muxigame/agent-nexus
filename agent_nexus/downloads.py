"""Installer downloads served by the gateway (docs/ui-spec.md §3.7).

``gateway.json``::

    "downloads": {"dir": "<state_dir>/downloads",            # default
                  "manifest_url": "https://<bucket>/agent-nexus/downloads.json",  # optional
                  "release_url": "https://github.com/<owner>/<repo>/releases",  # optional
                  "links": [{"label": "GitHub Releases", "url": "https://…"}]}  # optional

``manifest_url`` is the file list release CI publishes with every release
(scripts/publish_oss.py): its installers are listed with their public URLs, so
a new release appears here without copying anything to the gateway host. It is
fetched at most every few minutes; on failure the last good copy is kept.

Release artifacts copied into ``dir`` (``agent-nexus_<v>_x64-setup.exe``, the
``.deb``, ``agent-nexus-gateway-<v>.zip``, and optionally ``SHA256SUMS``) are
listed by ``GET /api/downloads`` and streamed by ``GET /downloads/<file>``.
Both are public: a new device needs the installer before it can sign in.
Only regular files directly in ``dir`` with a known installer suffix are ever
served. SHA-256 comes from ``SHA256SUMS`` when it lists the file, else it is
computed once and cached by (size, mtime).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from mcpkit.config import ConfigError

ARTIFACT_SUFFIXES = (".exe", ".msi", ".deb", ".rpm", ".appimage", ".dmg", ".zip", ".tar.gz")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,199}")
_VERSION = re.compile(r"(?<![0-9])(\d+\.\d+\.\d+(?:-(?:rc|beta|alpha|dev)[0-9.]*)?)", re.IGNORECASE)
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")
KNOWN_KEYS = frozenset({"dir", "manifest_url", "release_url", "links"})
MANIFEST_TTL_S = 300
MANIFEST_MAX_BYTES = 1024 * 1024

log = logging.getLogger(__name__)


def version_key(version: str | None) -> tuple:
    if not version:
        return (-1,)
    main, _, pre = version.partition("-")
    numbers = tuple(int(part) for part in main.split("."))
    # A pre-release sorts before its release.
    return numbers + ((0, pre) if pre else (1, ""))


def classify(name: str) -> dict:
    lower = name.lower()
    arch = "arm64" if ("arm64" in lower or "aarch64" in lower) else "x64"
    if lower.endswith(".exe") and "setup" not in lower:
        return {"id": f"windows-portable-{arch}", "platform": "windows", "arch": arch, "kind": "portable",
                "label": "Windows 便携版（免安装）", "requirements": f"Windows 10 / 11 {arch}"}
    if lower.endswith((".exe", ".msi")):
        return {"id": f"windows-{arch}", "platform": "windows", "arch": arch, "kind": "desktop",
                "label": "Agent Nexus for Windows", "requirements": f"Windows 10 / 11 {arch}"}
    if lower.endswith((".deb", ".rpm", ".appimage")):
        package = "deb" if lower.endswith(".deb") else "rpm" if lower.endswith(".rpm") else "AppImage"
        return {"id": f"linux-{package.lower()}", "platform": "linux", "arch": arch, "kind": "desktop",
                "label": f"Linux（.{package}）", "requirements": "Ubuntu 22.04+" if package == "deb" else "Linux"}
    if lower.endswith(".dmg"):
        return {"id": f"macos-{arch}", "platform": "macos", "arch": arch, "kind": "desktop",
                "label": "Agent Nexus for macOS", "requirements": "macOS"}
    if "gateway" in lower:
        return {"id": "gateway-zip", "platform": "linux", "arch": "any", "kind": "gateway",
                "label": "网关（自托管）", "requirements": "Linux + systemd + uv"}
    return {"id": "other-" + lower, "platform": "any", "arch": "any", "kind": "other", "label": name,
            "requirements": ""}


class Downloads:
    def __init__(self, section: Any, default_dir: Path, root: Path):
        if section is None:
            section = {}
        if not isinstance(section, dict):
            raise ConfigError("downloads must be an object")
        directory = section.get("dir")
        if directory is not None and not isinstance(directory, str):
            raise ConfigError("downloads.dir must be a path string")
        self.dir = (root / directory).resolve() if directory else default_dir
        release = section.get("release_url")
        if release is not None and (not isinstance(release, str) or not release.startswith(("https://", "http://"))):
            raise ConfigError("downloads.release_url must be an http(s) URL")
        self.release_url = release.rstrip("/") if release else None
        manifest = section.get("manifest_url")
        if manifest is not None and (not isinstance(manifest, str) or not manifest.startswith("https://")):
            raise ConfigError("downloads.manifest_url must be an https URL")
        self.manifest_url = manifest or None
        self._remote_cache: tuple[float, list[dict]] | None = None
        links = section.get("links") or []
        if not isinstance(links, list) or not all(
                isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"].startswith(("https://", "http://"))
                for item in links):
            raise ConfigError("downloads.links must be a list of {label, url}")
        self.links = [{"label": str(item.get("label") or item["url"])[:100], "url": item["url"],
                       "description": str(item.get("description") or "")[:200]} for item in links]
        if self.release_url and not any(link["url"] == self.release_url for link in self.links):
            self.links.append({"label": "全部版本（发布页）", "url": self.release_url, "description": "历史版本与发布说明"})
        self._cache: dict[str, tuple[int, int, str]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def warnings(section: Any) -> list[str]:
        if not isinstance(section, dict):
            return []
        return [f"unknown configuration key 'downloads.{key}' (ignored)" for key in sorted(set(section) - KNOWN_KEYS)]

    # -- files ---------------------------------------------------------------------
    def file(self, name: str) -> Path | None:
        """The servable file called ``name``: directly in ``dir``, a known suffix, a regular file."""
        if not isinstance(name, str) or not _NAME.fullmatch(name) or ".." in name:
            return None
        if not name.lower().endswith(ARTIFACT_SUFFIXES):
            return None
        path = self.dir / name
        try:
            if not path.is_file() or path.is_symlink() or path.resolve().parent != self.dir.resolve():
                return None
        except OSError:
            return None
        return path

    def _sums(self) -> dict[str, str]:
        sums = {}
        path = self.dir / "SHA256SUMS"
        try:
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
        except (OSError, UnicodeDecodeError):
            return sums
        for line in text.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and _HEX64.fullmatch(parts[0]):
                sums[parts[1].lstrip("*").strip()] = parts[0].lower()
        return sums

    def sha256(self, path: Path, sums: dict[str, str] | None = None) -> str:
        stat = path.stat()
        listed = (sums if sums is not None else self._sums()).get(path.name)
        if listed:
            return listed
        with self._lock:
            cached = self._cache.get(path.name)
            if cached and cached[:2] == (stat.st_size, stat.st_mtime_ns):
                return cached[2]
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        value = digest.hexdigest()
        with self._lock:
            self._cache[path.name] = (stat.st_size, stat.st_mtime_ns, value)
        return value

    def _remote(self) -> list[dict]:
        """Installers listed by ``manifest_url`` (cached for MANIFEST_TTL_S; the last good list on failure)."""
        if not self.manifest_url:
            return []
        now = time.monotonic()
        with self._lock:
            cached = self._remote_cache
        if cached and now - cached[0] < MANIFEST_TTL_S:
            return cached[1]
        try:
            request = urllib.request.Request(self.manifest_url, headers={
                "Cache-Control": "no-cache", "User-Agent": "agent-nexus-gateway"})
            with urllib.request.urlopen(request, timeout=5) as response:
                data = json.loads(response.read(MANIFEST_MAX_BYTES))
            published = str(data.get("published_at") or "")[:10] or None
            entries = []
            for entry in data.get("files") or []:
                name, url, sha, size = (entry.get(key) for key in ("filename", "url", "sha256", "size"))
                if (isinstance(name, str) and _NAME.fullmatch(name) and ".." not in name
                        and name.lower().endswith(ARTIFACT_SUFFIXES)
                        and isinstance(url, str) and url.startswith("https://")
                        and isinstance(sha, str) and _HEX64.fullmatch(sha)
                        and isinstance(size, int) and not isinstance(size, bool) and size >= 0):
                    entries.append({"filename": name, "url": url, "sha256": sha.lower(), "size": size,
                                    "published_at": published})
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            log.warning("downloads.manifest_url %s: %s", self.manifest_url, exc)
            entries = cached[1] if cached else []
        with self._lock:
            self._remote_cache = (now, entries)
        return entries

    def items(self) -> dict:
        """``GET /api/downloads`` (blocking: hashes new files; call from a thread)."""
        found = []
        sums = self._sums()
        try:
            entries = sorted(self.dir.iterdir()) if self.dir.is_dir() else []
        except OSError:
            entries = []
        for path in entries:
            if self.file(path.name) is None:
                continue
            try:
                stat = path.stat()
                sha = self.sha256(path, sums)
            except OSError:
                continue
            match = _VERSION.search(path.name)
            item = classify(path.name)
            item.update(filename=path.name, version=match.group(1) if match else None, size=stat.st_size,
                        sha256=sha, url="/downloads/" + path.name,
                        published_at=_dt.datetime.fromtimestamp(stat.st_mtime).date().isoformat())
            found.append(item)
        remote = self._remote()
        listed = {entry["filename"] for entry in remote}
        found = [item for item in found if item["filename"] not in listed]
        for entry in remote:
            match = _VERSION.search(entry["filename"])
            item = classify(entry["filename"])
            item.update(entry, version=match.group(1) if match else None)
            found.append(item)
        found.sort(key=lambda item: (version_key(item["version"]), item["filename"]), reverse=True)
        newest: dict[str, str | None] = {}
        for item in found:
            newest.setdefault(item["id"], item["version"])
            item["latest"] = item["version"] == newest[item["id"]]
        desktops = [item for item in found if item["kind"] == "desktop" and item["platform"] == "windows"]
        latest = (desktops or found or [{}])[0].get("version")
        return {"latest": latest, "items": found, "links": self.links, "release_url": self.release_url}
