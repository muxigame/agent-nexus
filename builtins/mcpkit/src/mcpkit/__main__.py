"""``python -m mcpkit``: manage secrets and check service configuration.

    python -m mcpkit secret new workspace/bearer-token      # random 64-char token
    python -m mcpkit secret set ones/token < file            # value from stdin
    python -m mcpkit secret check workspace/oauth-password
    python -m mcpkit check ../ones-mcp

Values are never printed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config as _config
from .config import CENTRAL_NAME, ConfigError, ServiceConfig, has_central


def _root(value: str | None) -> Path:
    if value:
        return Path(value).resolve()
    here = Path.cwd().resolve()
    for directory in (here, *here.parents):
        if has_central(directory):
            return directory
    raise ConfigError(f"no {CENTRAL_NAME} found above {here}; pass --root")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcpkit")
    parser.add_argument("--root", help=f"directory holding {CENTRAL_NAME} (default: nearest upward)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    secret = sub.add_parser("secret").add_subparsers(dest="action", required=True)
    for action in ("new", "set", "check"):
        p = secret.add_parser(action)
        p.add_argument("name")
        if action == "check":
            p.add_argument("--min-length", type=int, default=32)
    check = sub.add_parser("check", help="validate a service's configuration and secrets")
    check.add_argument("service_dir")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "check":
            from .service import Service

            service = Service.load(args.service_dir)
            from fastmcp import FastMCP

            service.http_options(FastMCP(service.name))
            print(f"configuration ok: {service.describe('http')}")
            return 0

        root = _root(args.root)
        holder = ServiceConfig(name="mcpkit", root=root, dir=root)
        if args.action == "check":
            _config.read_secret(holder, args.name, min_length=args.min_length)
            print(f"secret {args.name}: ok")
        elif args.action == "new":
            path = _config.write_secret(holder, args.name)
            print(f"secret {args.name}: written to {path} (owner-only)")
        else:
            value = sys.stdin.read().strip()
            if not value:
                raise ConfigError("no value on stdin")
            path = _config.write_secret(holder, args.name, value)
            print(f"secret {args.name}: written to {path} (owner-only)")
        return 0
    except ConfigError as exc:
        print(f"mcpkit: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
