"""Publish a release to Aliyun OSS and then move the updater feed (design §11.2).

Layout under ``<prefix>`` (default ``agent-nexus``) in the public-read bucket::

    <prefix>/releases/<version>/<every release file, latest.json included>
    <prefix>/downloads.json     <- the public file list the gateway's /downloads page reads
    <prefix>/latest.json        <- what tauri.conf.json's updater endpoint polls

Order matters: every versioned file is uploaded first and then fetched back
through its public URL (size must match); only then is ``<prefix>/latest.json``
replaced, so a client never sees a feed whose installer is missing or partial.
``<prefix>/downloads.json`` (every installer of this version with its size,
SHA-256 and public URL) moves together with it, so the website's download page
lists a new release without anyone copying files to the gateway host.
A prerelease version (``1.2.3-rc.1``) is uploaded but never becomes latest.

Credentials come from the environment, as set by
``aliyun/configure-aliyun-credentials-action`` (short-lived STS from GitHub
OIDC): ``ALIBABA_CLOUD_ACCESS_KEY_ID``, ``ALIBABA_CLOUD_ACCESS_KEY_SECRET`` and
``ALIBABA_CLOUD_SECURITY_TOKEN``. Needs ``oss2`` (``uv run --with oss2``).

Usage::

    python scripts/publish_oss.py --dist release --version 0.2.1
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import mimetypes
import os
import sys
import urllib.request
from pathlib import Path
from urllib.parse import quote

DEFAULT_BUCKET = "muxigame-prod-static-cn"
DEFAULT_ENDPOINT = "https://oss-cn-hangzhou.aliyuncs.com"
DEFAULT_PREFIX = "agent-nexus"
DEFAULT_PUBLIC_BASE = f"https://{DEFAULT_BUCKET}.oss-cn-hangzhou.aliyuncs.com/{DEFAULT_PREFIX}"
FEED = "latest.json"
MANIFEST = "downloads.json"
NO_CACHE = "no-cache, max-age=0"
RESUMABLE_BYTES = 20 * 1024 * 1024


class PublishError(RuntimeError):
    pass


def prerelease(version: str) -> bool:
    return "-" in version


def plan(dist: Path, version: str, prefix: str) -> list[tuple[Path, str]]:
    """(local file, object key) for every release file, the feed last."""
    files = sorted(path for path in dist.iterdir() if path.is_file())
    if not any(path.name == FEED for path in files):
        raise PublishError(f"{dist} has no {FEED}")
    base = f"{prefix.strip('/')}/releases/{version}"
    ordered = [path for path in files if path.name != FEED] + [dist / FEED]
    return [(path, f"{base}/{path.name}") for path in ordered]


def check_feed(feed_path: Path, version: str, public_base: str) -> dict:
    """The feed must name this version and point into this version's folder."""
    feed = json.loads(feed_path.read_text(encoding="utf-8"))
    if feed.get("version") != version:
        raise PublishError(f"{FEED} is for version {feed.get('version')!r}, not {version!r}")
    expected = f"{public_base.rstrip('/')}/releases/{quote(version)}/"
    for platform, entry in (feed.get("platforms") or {}).items():
        url = entry.get("url", "")
        if not url.startswith(expected):
            raise PublishError(f"{FEED} {platform} url {url!r} is not under {expected}")
        if not entry.get("signature"):
            raise PublishError(f"{FEED} {platform} has no signature")
    if not feed.get("platforms"):
        raise PublishError(f"{FEED} lists no platform")
    return feed


def manifest(dist: Path, version: str, public_base: str, published_at: str) -> dict:
    """The download list for ``<prefix>/downloads.json``: installers only (no feed, signatures or sums)."""
    files = []
    for path in sorted(dist.iterdir()):
        if not path.is_file() or path.name in {FEED, MANIFEST, "SHA256SUMS"} or path.suffix == ".sig":
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        files.append({"filename": path.name, "size": path.stat().st_size, "sha256": digest.hexdigest(),
                      "url": f"{public_base.rstrip('/')}/releases/{quote(version)}/{quote(path.name)}"})
    if not files:
        raise PublishError(f"{dist} has no installer to list in {MANIFEST}")
    return {"version": version, "published_at": published_at, "files": files}


def public_url(public_base: str, key: str, prefix: str) -> str:
    relative = key[len(prefix.strip("/")) + 1:]
    return f"{public_base.rstrip('/')}/{quote(relative)}"


def remote_size(url: str) -> int | None:
    request = urllib.request.Request(url, method="HEAD", headers={"Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return int(response.headers.get("Content-Length", "-1"))
    except OSError:
        return None


def _bucket(bucket_name: str, endpoint: str):
    import oss2  # imported here so --dry-run and the tests need no SDK

    key_id = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID")
    secret = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    token = os.environ.get("ALIBABA_CLOUD_SECURITY_TOKEN")
    if not key_id or not secret:
        raise PublishError("no Aliyun credentials in the environment (ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET)")
    auth = oss2.StsAuth(key_id, secret, token) if token else oss2.Auth(key_id, secret)
    return oss2, oss2.Bucket(auth, endpoint, bucket_name)


def upload(oss2, bucket, path: Path, key: str, cache_control: str | None = None) -> None:
    headers = {"Content-Type": mimetypes.guess_type(path.name)[0] or "application/octet-stream"}
    if path.suffix in {".json", ".sig"} or path.name == "SHA256SUMS":
        headers["Content-Type"] = "application/json" if path.suffix == ".json" else "text/plain; charset=utf-8"
    if cache_control:
        headers["Cache-Control"] = cache_control
    if path.stat().st_size > RESUMABLE_BYTES:
        oss2.resumable_upload(bucket, key, str(path), headers=headers, multipart_threshold=RESUMABLE_BYTES)
    else:
        bucket.put_object_from_file(key, str(path), headers=headers)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, required=True, help="folder with the release files and latest.json")
    parser.add_argument("--version", required=True, help="e.g. 0.2.1 (a tag without its leading v)")
    parser.add_argument("--bucket", default=os.environ.get("OSS_BUCKET") or DEFAULT_BUCKET)
    parser.add_argument("--endpoint", default=os.environ.get("OSS_ENDPOINT") or DEFAULT_ENDPOINT)
    parser.add_argument("--prefix", default=os.environ.get("OSS_PREFIX") or DEFAULT_PREFIX)
    parser.add_argument("--public-base", default=os.environ.get("OSS_PUBLIC_BASE") or DEFAULT_PUBLIC_BASE,
                        help="public URL of --prefix, as the updater sees it")
    parser.add_argument("--dry-run", action="store_true", help="check and print the plan, upload nothing")
    args = parser.parse_args(argv)
    version = args.version.removeprefix("v")
    try:
        steps = plan(args.dist, version, args.prefix)
        check_feed(args.dist / FEED, version, args.public_base)
        downloads = manifest(args.dist, version, args.public_base, _dt.date.today().isoformat())
        latest_key = f"{args.prefix.strip('/')}/{FEED}"
        manifest_key = f"{args.prefix.strip('/')}/{MANIFEST}"
        for path, key in steps:
            print(f"{'would upload' if args.dry_run else 'upload'} {path.name} -> oss://{args.bucket}/{key}")
        if prerelease(version):
            print(f"{version} is a prerelease: {latest_key} and {manifest_key} stay as they are")
        else:
            print(f"then {manifest_key} <- {len(downloads['files'])} file(s) of {version}")
            print(f"then {latest_key} <- {FEED} of {version}")
        if args.dry_run:
            return 0
        oss2, bucket = _bucket(args.bucket, args.endpoint)
        for path, key in steps:
            upload(oss2, bucket, path, key, NO_CACHE if path.name == FEED else None)
        for path, key in steps:
            url = public_url(args.public_base, key, args.prefix)
            size = remote_size(url)
            if size != path.stat().st_size:
                raise PublishError(f"{url} answers with {size} bytes, expected {path.stat().st_size}; latest.json not moved")
            print(f"verified {url}")
        if not prerelease(version):
            listing = args.dist.parent / f".{MANIFEST}"
            listing.write_text(json.dumps(downloads, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            upload(oss2, bucket, listing, manifest_key, NO_CACHE)
            print(f"published {public_url(args.public_base, manifest_key, args.prefix)} -> {version}")
            upload(oss2, bucket, args.dist / FEED, latest_key, NO_CACHE)
            print(f"published {public_url(args.public_base, latest_key, args.prefix)} -> {version}")
    except (PublishError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
