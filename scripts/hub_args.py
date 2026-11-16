"""Shared command-line options for probes that talk to a deployed hub.

No hub address is built in: pass ``--url`` or set ``ANX_HUB_URL`` (legacy
``MCPM_HUB_URL``). The bearer token is read from a local file and never printed.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

DEFAULT_TOKEN_FILE = Path(__file__).resolve().parents[2] / "secrets/workspace/bearer-token"


def hub_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--url", default=os.environ.get("ANX_HUB_URL") or os.environ.get("MCPM_HUB_URL"),
                        help="hub origin, e.g. https://hub.example.com (default: $ANX_HUB_URL)")
    parser.add_argument("--token-file", type=Path,
                        default=Path(os.environ["ANX_HUB_TOKEN_FILE"]) if os.environ.get("ANX_HUB_TOKEN_FILE") else DEFAULT_TOKEN_FILE,
                        help="file holding the hub bearer token (default: <root>/secrets/workspace/bearer-token)")
    return parser


def parse(parser: argparse.ArgumentParser, argv=None):
    args = parser.parse_args(argv)
    if not args.url:
        parser.error("--url (or ANX_HUB_URL) is required; no hub address is built in")
    if not args.url.startswith(("https://", "http://")):
        parser.error("--url must be an http(s) origin")
    args.url = args.url.rstrip("/")
    if args.url.endswith("/mcp"):
        args.url = args.url[: -len("/mcp")]
    return args


def read_token(args) -> str:
    return Path(args.token_file).read_text(encoding="utf-8").strip()
