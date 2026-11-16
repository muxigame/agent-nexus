"""``anx-gateway invite …`` and ``anx-gateway device …`` (M16, design §11.5).

They work on the gateway's state database directly, so they run on the
gateway host next to (or instead of) the running service::

    anx-gateway invite create --config /etc/agent-nexus/gateway.json [--ttl 24h] [--label 家里的台式机] [--agent-id x]
    anx-gateway invite list   --config … [--all]
    anx-gateway invite revoke --config … <invite_id>
    anx-gateway device list   --config …
    anx-gateway device revoke --config … <agent_id>
    anx-gateway plugin-repo add    --config … <package.zip> [--replace]
    anx-gateway plugin-repo list   --config … [--json]
    anx-gateway plugin-repo remove --config … <name> <version>

A running gateway closes the link of a device revoked here within a few
seconds (its revocation sweep). Invite secrets and device tokens are never
printed except the one-time invite code from ``invite create``.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

from mcpkit.config import ConfigError

from .invites import EnrollError, Enrollment
from .registry import read_json
from .store import GatewayStore


def _when(seconds) -> str:
    if not seconds:
        return "-"
    return _dt.datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")


def _enrollment(config_path: Path) -> tuple[Enrollment, GatewayStore]:
    from .cloud import invite_settings, state_db_path
    path = config_path.resolve()
    config = read_json(path)
    store = GatewayStore(state_db_path(config, path.parent))
    groups = config.get("agent_groups") if isinstance(config.get("agent_groups"), dict) else {}
    agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
    return Enrollment(store, None, public_url=config.get("public_url"), agents=agents.keys(), groups=groups.keys(),
                      settings=invite_settings(config)), store


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="anx-gateway", description="agent-nexus gateway: invites and devices")
    groups = parser.add_subparsers(dest="group", required=True)
    invite = groups.add_parser("invite", help="one-time invite codes for new devices").add_subparsers(dest="action", required=True)
    create = invite.add_parser("create", help="create an invite and print its code once")
    create.add_argument("--ttl", help="lifetime: seconds or 90m / 24h / 7d (default: invites.default_ttl_s)")
    create.add_argument("--label", help="a note shown in the console")
    create.add_argument("--agent-id", help="the agent_id the device will get (default: from its hostname)")
    create.add_argument("--hub", help="gateway origin written into the code (default: public_url)")
    create.add_argument("--json", action="store_true", help="print {invite, code, link} as JSON")
    listing = invite.add_parser("list", help="list invites (unused by default)")
    listing.add_argument("--all", action="store_true")
    revoke_invite = invite.add_parser("revoke", help="void an unused invite")
    revoke_invite.add_argument("invite_id")
    device = groups.add_parser("device", help="devices joined through invites").add_subparsers(dest="action", required=True)
    device.add_parser("list", help="list known devices")
    revoke_device = device.add_parser("revoke", help="revoke a device's credential")
    revoke_device.add_argument("agent_id")
    repo = groups.add_parser("plugin-repo", help="the plugin repository (design 6.12)").add_subparsers(dest="action", required=True)
    repo_add = repo.add_parser("add", help="add a package made by `anx plugin pack` (checked like an upload)")
    repo_add.add_argument("package", type=Path)
    repo_add.add_argument("--replace", action="store_true", help="replace the same name and version")
    repo_list = repo.add_parser("list", help="list the packages")
    repo_list.add_argument("--json", action="store_true")
    repo_remove = repo.add_parser("remove", help="delete one package")
    repo_remove.add_argument("name")
    repo_remove.add_argument("version")
    for sub in (create, listing, revoke_invite, *device.choices.values(), *repo.choices.values()):
        sub.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.group == "plugin-repo":
        return repo_main(args)
    try:
        enrollment, store = _enrollment(args.config)
        try:
            if args.group == "invite" and args.action == "create":
                created = enrollment.create_invite(label=args.label, ttl_s=args.ttl, agent_id=args.agent_id,
                                                   hub_url=args.hub)
                if args.json:
                    print(json.dumps(created, ensure_ascii=False, indent=2))
                else:
                    item = created["invite"]
                    print(f"invite {item['id']} (expires {_when(item['expires_at'])}; shown only once)")
                    print(created["code"])
                    print(created["link"])
            elif args.group == "invite" and args.action == "list":
                for item in enrollment.list_invites("all" if args.all else "unused"):
                    print(f"{item['id']}\t{item['state']}\t{_when(item['created_at'])}\t{_when(item['expires_at'])}"
                          f"\t{item.get('used_by') or '-'}\t{item.get('label') or ''}")
            elif args.group == "invite":
                item = enrollment.revoke_invite(args.invite_id)
                print(f"invite {item['id']}: {item['state']}")
            elif args.action == "list":
                for row in store.devices():
                    state = "revoked" if row.get("revoked_at") else ("online" if row["online"] else "offline")
                    print(f"{row['agent_id']}\t{state}\t{row.get('enrolled_via') or '-'}\t{row.get('hostname') or '-'}"
                          f"\t{row.get('alias') or ''}")
            else:
                result = enrollment.revoke_credentials(args.agent_id)
                print(f"device {result['agent_id']} revoked; a running gateway disconnects it within a few seconds")
        finally:
            store.close()
        return 0
    except EnrollError as exc:
        print(f"anx-gateway: {exc.message} ({exc.code})", file=sys.stderr)
        return 1
    except (ConfigError, OSError, ValueError) as exc:
        print(f"anx-gateway: {exc}", file=sys.stderr)
        return 2


def repo_main(args) -> int:
    """``anx-gateway plugin-repo add|list|remove`` on the gateway host (design §6.12)."""
    import shutil

    from .cloud import state_db_path
    from .plugin_repo import PluginRepo, RepoError, repo_settings
    try:
        path = args.config.resolve()
        config = read_json(path)
        settings = repo_settings(config, state_db_path(config, path.parent).parent, path.parent)
        store = GatewayStore(state_db_path(config, path.parent))
        repo = PluginRepo(store, settings["dir"], max_package_mb=settings["max_package_mb"])
        try:
            if args.action == "add":
                # add() moves (and so consumes) its input: give it a copy of the owner's file.
                incoming = repo.incoming()
                shutil.copyfile(args.package, incoming)
                item = repo.add(incoming, replace=args.replace, by="cli")
                state = "replaced" if item["replaced"] else "added"
                print(f"{item['name']} {item['version']}: {state} "
                      f"({item['size']} bytes, {item['files']} files, sha256 {item['sha256']})")
            elif args.action == "list":
                items = repo.items()
                if args.json:
                    print(json.dumps(items, ensure_ascii=False, indent=2))
                else:
                    for item in items:
                        latest = " (latest)" if item["latest"] else ""
                        print(f"{item['name']}\t{item['version']}{latest}\t{item['size']}\t{_when(item['uploaded_at'])}"
                              f"\t{item.get('uploaded_by') or '-'}\t{item['sha256'][:16]}")
            else:
                repo.remove(args.name, args.version)
                print(f"removed {args.name} {args.version}")
        finally:
            store.close()
        return 0
    except RepoError as exc:
        print(f"anx-gateway: {exc.message} ({exc.code})", file=sys.stderr)
        for finding in exc.extra.get("findings") or []:
            print(f"  {finding['path']}: {finding['rule']}", file=sys.stderr)
        return 1
    except (ConfigError, OSError, ValueError) as exc:
        print(f"anx-gateway: {exc}", file=sys.stderr)
        return 2
