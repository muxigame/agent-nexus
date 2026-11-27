"""Write the Tauri updater feed ``latest.json`` (design §9.4, §11.2).

The desktop app polls ``<oss>/agent-nexus/latest.json`` (``tauri.conf.json``
``plugins.updater.endpoints``); ``scripts/publish_oss.py`` puts this file there
after the installer it names is uploaded. ``--base-url`` is the public folder
the installer is served from (``<oss>/agent-nexus/releases/<version>``);
without it the URL points at the GitHub release asset, as releases before the
move to OSS did.

Usage::

    python scripts/make_latest_json.py --setup artifacts/agent-nexus_0.2.1_x64-setup.exe --tag v0.2.1 \\
        --base-url https://<bucket>.oss-cn-hangzhou.aliyuncs.com/agent-nexus/releases/0.2.1 --out artifacts/latest.json

macOS builds add one ``--macos-bundle agent-nexus_<v>_<arch>.app.tar.gz`` each
(``aarch64`` -> ``darwin-aarch64``, ``x64`` -> ``darwin-x86_64``), its ``.sig`` next to it.

The signature is the content of ``<setup>.sig`` (minisign, written by
``cargo tauri build`` when ``TAURI_SIGNING_PRIVATE_KEY`` is set). Standard library only.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = "windows-x86_64"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def read_signature(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    try:
        decoded = base64.b64decode(text, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise ValueError(f"{path.name} is not a base64 minisign signature") from None
    if "untrusted comment:" not in decoded or "trusted comment:" not in decoded:
        raise ValueError(f"{path.name} is not a minisign signature")
    return text


def asset_url(repo: str, tag: str, name: str) -> str:
    if not REPO_RE.match(repo):
        raise ValueError("--repo must look like owner/name")
    return f"https://github.com/{repo}/releases/download/{quote(tag, safe='')}/{quote(name, safe='')}"


def base_url_asset(base_url: str, name: str) -> str:
    if not base_url.startswith("https://"):
        raise ValueError("--base-url must be an https:// URL")
    return f"{base_url.rstrip('/')}/{quote(name, safe='')}"


def macos_platform(name: str) -> str:
    """Tauri updater platform key of a macOS bundle name."""
    if "_aarch64." in name:
        return "darwin-aarch64"
    if "_x64." in name:
        return "darwin-x86_64"
    raise ValueError(f"{name}: cannot tell the architecture (expected _aarch64. or _x64.)")


def feed(version: str, signature: str, url: str, notes: str = "", pub_date: str | None = None,
         extra: dict | None = None) -> dict:
    return {
        "version": version,
        "notes": notes,
        "pub_date": pub_date or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "platforms": {PLATFORM: {"signature": signature, "url": url}, **(extra or {})},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--setup", type=Path, required=True, help="the NSIS installer (its .sig must sit next to it)")
    parser.add_argument("--signature", type=Path, help="default: <setup>.sig")
    parser.add_argument("--macos-bundle", type=Path, action="append", default=[],
                        help="a macOS updater bundle agent-nexus_<v>_<arch>.app.tar.gz (its .sig next to it); repeatable")
    parser.add_argument("--repo", default="muxigame/agent-nexus", help="GitHub owner/name hosting the release")
    parser.add_argument("--base-url", help="public folder serving the installer (OSS); overrides --repo")
    parser.add_argument("--tag", required=True, help="release tag, e.g. v0.2.0")
    parser.add_argument("--version", help="default: the tag without its leading v")
    parser.add_argument("--notes", default="", help="release notes shown by the updater")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    version = args.version or (args.tag[1:] if args.tag.startswith("v") else args.tag)
    if not args.setup.is_file():
        print(f"error: installer not found: {args.setup}", file=sys.stderr)
        return 2
    signature_path = args.signature or args.setup.with_name(args.setup.name + ".sig")
    def link(name):
        return base_url_asset(args.base_url, name) if args.base_url else asset_url(args.repo, args.tag, name)
    try:
        signature = read_signature(signature_path)
        url = link(args.setup.name)
        extra = {}
        for bundle in args.macos_bundle:
            if not bundle.is_file():
                raise ValueError(f"macOS bundle not found: {bundle}")
            extra[macos_platform(bundle.name)] = {"signature": read_signature(bundle.with_name(bundle.name + ".sig")),
                                                  "url": link(bundle.name)}
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(feed(version, signature, url, args.notes, extra=extra), indent=2) + "\n", encoding="utf-8")
    print(f"latest.json: {args.out} ({', '.join([PLATFORM, *extra])} -> {url})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
