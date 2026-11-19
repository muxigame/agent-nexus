"""Connector icons: found automatically, or uploaded by the owner (docs/ui-spec.md §3.4).

Where an icon comes from, first match wins:

1. ``upload`` -- the owner's own image (``PUT /api/connectors/<name>/icon``);
   automatic discovery never replaces it.
2. ``server`` -- ``serverInfo.icons`` from the MCP ``initialize`` answer (an
   ``https:`` URL or a ``data:`` URI).
3. ``site`` -- the connector's website: ``<link rel="icon">`` on the home page
   of the MCP host (then of its parent domain, then of the product site for API
   domains that have no website of their own, :data:`SITE_HOMES`), else
   ``/favicon.ico``.

Fetching is the gateway reaching out on a server's say-so, so a URL is only
followed when it is ``https:`` on a host that resolves to public addresses, or
anything on the connector's own host (the owner chose that one). Redirects are
followed by hand under the same rule. Bodies are capped and the image type is
sniffed from the bytes, never taken from a header. Icons are served with a
sandboxing CSP, so an SVG cannot run script even when opened directly.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import tempfile
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes, urljoin, urlsplit

import httpx2

log = logging.getLogger(__name__)

MAX_ICON_BYTES = 256 * 1024
MAX_PAGE_BYTES = 512 * 1024
FETCH_TIMEOUT_S = 8.0
MAX_REDIRECTS = 3
#: A failed automatic search is not repeated sooner than this.
RETRY_AFTER_S = 24 * 3600
USER_AGENT = "agent-nexus-gateway (connector icon)"
#: Bumped when discovery learns new places to look: a connector whose last search found
#: nothing is searched again at once instead of after RETRY_AFTER_S.
FINDER_VERSION = 2
#: API-only domains (no website, no favicon) and the product site whose icon they use.
SITE_HOMES = {"githubcopilot.com": "https://github.com", "aliyuncs.com": "https://www.aliyun.com"}
SOURCES = ("upload", "server", "site")


class IconError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def sniff(data: bytes) -> str | None:
    """The image type of ``data`` from its bytes; ``None`` when it is not an image we serve."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith((b"<svg", b"<?xml", b"<!--", b"<!doctype svg")) and b"<svg" in data[:4096].lower():
        return "image/svg+xml"
    return None


def check_image(data: Any) -> tuple[bytes, str]:
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise IconError("invalid_icon", "图标文件是空的")
    if len(data) > MAX_ICON_BYTES:
        raise IconError("icon_too_large", f"图标不能超过 {MAX_ICON_BYTES // 1024} KB", 413)
    kind = sniff(bytes(data))
    if kind is None:
        raise IconError("invalid_icon", "只支持 PNG、JPEG、GIF、WebP、ICO 和 SVG 图片", 415)
    return bytes(data), kind


# -- storage ---------------------------------------------------------------------------

class IconStore:
    """``<state_dir>/connector-icons/<name>.{img,json}``; in memory without a state dir."""

    def __init__(self, base: Path | None):
        self.base = base
        self._memory: dict[str, tuple[bytes, dict]] = {}
        self._lock = threading.RLock()

    def meta(self, name: str) -> dict:
        with self._lock:
            if self.base is None:
                return dict(self._memory.get(name, (b"", {}))[1])
            try:
                data = json.loads((self.base / f"{name}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
            return data if isinstance(data, dict) else {}

    def read(self, name: str) -> tuple[bytes, dict] | None:
        meta = self.meta(name)
        if not meta.get("type"):
            return None
        with self._lock:
            if self.base is None:
                return self._memory[name][0], meta
            try:
                return (self.base / f"{name}.img").read_bytes(), meta
            except OSError:
                return None

    def _write(self, path: Path, data: bytes) -> None:
        handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
            os.replace(temp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise

    def save(self, name: str, data: bytes | None, meta: dict) -> dict:
        """Store an icon (``data``) or only a note (``data=None``: a search that found nothing)."""
        meta = {**meta, "saved_at": time.time()}
        if data is not None:
            meta["sha256"] = hashlib.sha256(data).hexdigest()
            meta["size"] = len(data)
        with self._lock:
            if self.base is None:
                self._memory[name] = (data or b"", meta)
                return meta
            self.base.mkdir(parents=True, exist_ok=True)
            if data is not None:
                self._write(self.base / f"{name}.img", data)
            else:
                with contextlib.suppress(FileNotFoundError):
                    (self.base / f"{name}.img").unlink()
            self._write(self.base / f"{name}.json", json.dumps(meta).encode())
        return meta

    def remove(self, name: str) -> None:
        with self._lock:
            if self.base is None:
                self._memory.pop(name, None)
                return
            for suffix in (".img", ".json"):
                with contextlib.suppress(FileNotFoundError):
                    (self.base / f"{name}{suffix}").unlink()


# -- discovery -------------------------------------------------------------------------

class _IconLinks(HTMLParser):
    """``<link rel="…icon…" href sizes>`` in a page's head."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[int, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        values = {key.lower(): (value or "") for key, value in attrs}
        rel = values.get("rel", "").lower().split()
        href = values.get("href", "").strip()
        if not href or not any(word in ("icon", "apple-touch-icon", "apple-touch-icon-precomposed") for word in rel):
            return
        score = 0
        if "image/svg" in values.get("type", "") or href.lower().split("?")[0].endswith(".svg"):
            score = 1000
        else:
            sizes = re.findall(r"(\d+)x\d+", values.get("sizes", ""))
            score = max((int(size) for size in sizes), default=180 if "apple-touch-icon" in rel else 32)
        self.found.append((score, href))


def page_icons(html: str, base_url: str) -> list[str]:
    parser = _IconLinks()
    with contextlib.suppress(Exception):
        parser.feed(html[:MAX_PAGE_BYTES])
    best = sorted(parser.found, key=lambda item: -min(item[0], 1000))
    return [urljoin(base_url, href) for _, href in best]


def server_icons(server: Any) -> list[str]:
    """``serverInfo.icons`` (MCP Implementation.icons): ``src`` values, SVG and largest first."""
    icons = server.get("icons") if isinstance(server, dict) else None
    if not isinstance(icons, list):
        return []
    ranked = []
    for icon in icons[:20]:
        if not isinstance(icon, dict) or not isinstance(icon.get("src"), str):
            continue
        src = icon["src"].strip()
        mime = str(icon.get("mimeType") or "")
        sizes = icon.get("sizes") if isinstance(icon.get("sizes"), list) else [icon.get("sizes")]
        numbers = [int(n) for size in sizes if isinstance(size, str) for n in re.findall(r"(\d+)x\d+", size)]
        score = 1000 if "svg" in mime or any(size == "any" for size in sizes) else max(numbers, default=64)
        if icon.get("theme") == "dark":
            score -= 1  # the console's avatars sit on light and dark surfaces; prefer the default
        ranked.append((score, src))
    return [src for _, src in sorted(ranked, key=lambda item: -item[0])]


def decode_data_uri(uri: str) -> bytes | None:
    head, sep, payload = uri[5:].partition(",")
    if not sep or len(payload) > MAX_ICON_BYTES * 2:
        return None
    try:
        return base64.b64decode(payload, validate=False) if head.endswith(";base64") else unquote_to_bytes(payload)
    except (ValueError, TypeError):
        return None


def _parent_origin(url: str) -> str | None:
    parts = urlsplit(url)
    host = parts.hostname or ""
    labels = host.split(".")
    with contextlib.suppress(ValueError):
        ipaddress.ip_address(host)
        return None
    if len(labels) < 3:
        return None
    return f"https://{'.'.join(labels[1:])}"


class IconFinder:
    """Finds a connector's icon on the network under the rules in the module docstring."""

    def __init__(self, *, resolve=None, transport=None):
        self._resolve = resolve or self._getaddrinfo
        self._transport = transport  # tests: an httpx2.MockTransport

    @staticmethod
    async def _getaddrinfo(host: str) -> list[str]:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        return [info[4][0] for info in infos]

    async def allowed(self, url: str, connector_url: str) -> bool:
        parts = urlsplit(url)
        own = urlsplit(connector_url).hostname
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            return False
        if parts.hostname == own:
            return True  # the owner chose this host
        if parts.scheme != "https":
            return False
        try:
            addresses = await asyncio.wait_for(self._resolve(parts.hostname), FETCH_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError):
            return False
        try:
            return bool(addresses) and all(ipaddress.ip_address(address.split("%")[0]).is_global for address in addresses)
        except ValueError:
            return False

    async def get(self, client, url: str, connector_url: str, limit: int) -> tuple[bytes, str] | None:
        """``(body, final url)`` of a GET under the URL rule, following redirects by hand."""
        for _ in range(MAX_REDIRECTS + 1):
            if not await self.allowed(url, connector_url):
                return None
            try:
                async with client.stream("GET", url) as response:
                    if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                        url = urljoin(url, response.headers["location"])
                        continue
                    if response.status_code != 200:
                        return None
                    if int(response.headers.get("content-length") or 0) > limit:
                        return None
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > limit:
                            return None
                    return bytes(body), url
            except (httpx2.HTTPError, OSError, asyncio.TimeoutError):
                return None
        return None

    async def image(self, client, src: str, connector_url: str) -> tuple[bytes, str] | None:
        if src.startswith("data:"):
            data = decode_data_uri(src)
        else:
            got = await self.get(client, src, connector_url, MAX_ICON_BYTES)
            data = got[0] if got else None
        if not data or len(data) > MAX_ICON_BYTES:
            return None
        kind = sniff(data)
        return (data, kind) if kind else None

    async def find(self, connector_url: str, server: Any = None) -> dict | None:
        """``{data, type, source, url}`` of the best icon found, or ``None``."""
        timeout = httpx2.Timeout(FETCH_TIMEOUT_S)
        async with httpx2.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False, transport=self._transport,
                                      headers={"User-Agent": USER_AGENT, "Accept": "image/*,text/html;q=0.8,*/*;q=0.5"}) as client:
            for src in server_icons(server):
                found = await self.image(client, src, connector_url)
                if found:
                    return {"data": found[0], "type": found[1], "source": "server",
                            "url": None if src.startswith("data:") else src[:2048]}
            parts = urlsplit(connector_url)
            origins = [f"{parts.scheme}://{parts.netloc}"]
            if (parent := _parent_origin(connector_url)) and parent not in origins:
                origins.append(parent)
            host = (parts.hostname or "").lower()
            for suffix, home in SITE_HOMES.items():
                if (host == suffix or host.endswith("." + suffix)) and home not in origins:
                    origins.append(home)
            for origin in origins:
                page = await self.get(client, origin + "/", connector_url, MAX_PAGE_BYTES)
                candidates = page_icons(page[0].decode("utf-8", errors="replace"), page[1]) if page else []
                candidates.append(origin + "/favicon.ico")
                for src in candidates[:6]:
                    found = await self.image(client, src, connector_url)
                    if found:
                        return {"data": found[0], "type": found[1], "source": "site", "url": src[:2048]}
        return None
