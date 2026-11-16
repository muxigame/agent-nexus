#!/usr/bin/env bash
# First-time install of an agent-nexus gateway instance (run as root on the hub host).
#
#   install.sh --package agent-nexus-gateway-<ver>.zip --sha256 <hex> [options]
#
# Options:
#   --instance NAME        extra instance (e.g. trial): uses /opt/agent-nexus-NAME,
#                          /etc/agent-nexus-NAME, /var/lib/agent-nexus-NAME and
#                          agent-nexus-gateway@NAME.service. Default: production layout.
#   --port N               port written into a new gateway.json (default 8440; trial: 8441)
#   --public-url URL       public https URL written into a new gateway.json
#   --first-agent ID       first device id allowed to connect (default pc-main)
#   --deploy-key FILE      also create the CI deploy user anx-deploy with this public key
#                          (authorized_keys "restrict"; sudo only for anx-upgrade)
#
# Needs python3, uv (https://docs.astral.sh/uv/) and systemd. Existing config and env
# files are never overwritten; generated tokens are written only to the env files.
set -euo pipefail

package="" sha="" instance="" port="" public_url="" first_agent="pc-main" deploy_key=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --package) package=$2; shift 2 ;;
        --sha256) sha=$2; shift 2 ;;
        --instance) instance=$2; shift 2 ;;
        --port) port=$2; shift 2 ;;
        --public-url) public_url=$2; shift 2 ;;
        --first-agent) first_agent=$2; shift 2 ;;
        --deploy-key) deploy_key=$2; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
if [ -z "$package" ] || [ -z "$sha" ]; then
    echo "usage: $0 --package FILE --sha256 HEX [--instance NAME] [--port N] [--public-url URL] [--first-agent ID] [--deploy-key FILE]" >&2
    exit 2
fi
if ! [[ "$sha" =~ ^[0-9a-f]{64}$ ]]; then echo "--sha256 must be 64 lowercase hex characters" >&2; exit 2; fi
if [ -n "$instance" ] && ! [[ "$instance" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]]; then echo "invalid instance name" >&2; exit 2; fi
if ! [[ "$first_agent" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; then echo "invalid agent id" >&2; exit 2; fi
command -v uv >/dev/null || { echo "uv is required (install it first)" >&2; exit 2; }

suffix=${instance:+-$instance}
base=/opt/agent-nexus$suffix
config_dir=/etc/agent-nexus$suffix
state_dir=/var/lib/agent-nexus$suffix
if [ -n "$instance" ]; then unit="agent-nexus-gateway@$instance.service"; else unit="agent-nexus-gateway.service"; fi
port=${port:-$([ "$instance" = "trial" ] && echo 8441 || echo 8440)}
here=$(cd "$(dirname "$0")" && pwd)

if ! getent passwd agent-nexus >/dev/null; then
    useradd --system --home-dir /var/lib/agent-nexus --shell /usr/sbin/nologin agent-nexus
fi
install -d -m 0755 "$base" "$base/releases" "$base/bin" "$config_dir"
install -d -m 0700 "$base/backups"
install -d -m 0750 -o agent-nexus -g agent-nexus "$state_dir"
# Installers served at /downloads (copy agent-nexus_<version>_x64-setup.exe and SHA256SUMS here).
install -d -m 0755 -o agent-nexus -g agent-nexus "$state_dir/downloads"
# The plugin repository (design 6.12): private packages, owner-only, never under downloads/.
install -d -m 0700 -o agent-nexus -g agent-nexus "$state_dir/plugin-repo"
# One upgrade script serves every instance (the sudoers rule names only this path).
install -d -m 0755 /opt/agent-nexus/bin
install -m 0755 "$here/upgrade.py" /opt/agent-nexus/bin/anx-upgrade

# Config and env: created once, never overwritten.
ANX_PORT=$port ANX_PUBLIC_URL=$public_url ANX_FIRST_AGENT=$first_agent ANX_CONFIG_DIR=$config_dir \
ANX_STATE_DIR=$state_dir python3 - <<'PY'
import json, os, secrets
from pathlib import Path
config_dir = Path(os.environ["ANX_CONFIG_DIR"])
env_file = config_dir / "gateway.env"
agent = os.environ["ANX_FIRST_AGENT"]
agent_env = "ANX_AGENT_" + agent.upper().replace("-", "_") + "_TOKEN"
if not env_file.exists():
    agent_token = secrets.token_urlsafe(48)
    env_file.write_text(f"ANX_SERVER_TOKEN={secrets.token_urlsafe(48)}\n{agent_env}={agent_token}\n")
    os.chmod(env_file, 0o600)
    client = config_dir / "client.env"
    client.write_text(f"ANX_AGENT_TOKEN={agent_token}\n")
    os.chmod(client, 0o600)
    print("wrote", env_file, "and", client, "(device token for", agent + ")")
config = config_dir / "gateway.json"
if not config.exists():
    body = {"host": "127.0.0.1", "port": int(os.environ["ANX_PORT"]), "token_env": "ANX_SERVER_TOKEN",
            "agents": {agent: agent_env}, "rpc_timeout_s": 180, "state_dir": os.environ["ANX_STATE_DIR"]}
    if os.environ.get("ANX_PUBLIC_URL"):
        body["public_url"] = os.environ["ANX_PUBLIC_URL"]
    config.write_text(json.dumps(body, indent=2) + "\n")
    os.chmod(config, 0o644)
    print("wrote", config)
PY

# Unit file (template for instances).
if [ -n "$instance" ]; then
    install -m 0644 "$here/agent-nexus-gateway@.service" /etc/systemd/system/agent-nexus-gateway@.service
else
    install -m 0644 "$here/agent-nexus-gateway.service" /etc/systemd/system/agent-nexus-gateway.service
fi
systemctl daemon-reload

# Verify, extract, sync, check-config, then switch and start (the upgrade path does it all).
umask 022
systemctl enable "$unit"
/opt/agent-nexus/bin/anx-upgrade --package "$package" --sha256 "$sha" --base "$base" \
    --config-dir "$config_dir" --state-dir "$state_dir" --service "$unit" --port "$port"

if [ -n "$deploy_key" ]; then
    if ! getent passwd anx-deploy >/dev/null; then
        useradd --create-home --shell /bin/sh anx-deploy
    fi
    deploy_home=$(getent passwd anx-deploy | cut -d: -f6)
    install -d -m 0700 -o anx-deploy -g anx-deploy "$deploy_home/.ssh"
    printf 'restrict %s\n' "$(cat "$deploy_key")" > "$deploy_home/.ssh/authorized_keys"
    chown anx-deploy:anx-deploy "$deploy_home/.ssh/authorized_keys"
    chmod 0600 "$deploy_home/.ssh/authorized_keys"
    install -d -m 0770 -o root -g anx-deploy /opt/agent-nexus/incoming
    install -m 0440 "$here/anx-deploy.sudoers.example" /etc/sudoers.d/anx-deploy
    visudo -cf /etc/sudoers.d/anx-deploy
    echo "deploy user anx-deploy ready (uploads go to /opt/agent-nexus/incoming)"
fi
systemctl --no-pager --lines=0 status "$unit" || true
