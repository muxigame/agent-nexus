#!/usr/bin/env bash
# First-time install of the agent-nexus gateway on a hub host (run as root on that host).
# Usage: deploy_cloud_host.sh <server-zip-sha256> <uv-tarball-sha256> [first-agent-id]
# Inputs are expected under $BASE/bootstrap/ (agent-nexus-server.zip, uv.tar.gz).
# Nothing about a specific deployment is built in: hashes and the first agent id are arguments.
set -euo pipefail

if [ "$#" -lt 2 ]; then
    echo "usage: $0 <server-zip-sha256> <uv-tarball-sha256> [first-agent-id]" >&2
    exit 2
fi
base=${BASE:-/opt/agent-nexus}
expected_package=$1
expected_uv=$2
first_agent=${3:-pc-main}
first_agent_env=${FIRST_AGENT_TOKEN_ENV:-ANX_AGENT1_TOKEN}
for value in "$expected_package" "$expected_uv"; do
    if ! [[ "$value" =~ ^[0-9a-f]{64}$ ]]; then
        echo "expected hashes must be 64 lowercase hex characters" >&2
        exit 2
    fi
done
if ! [[ "$first_agent" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || ! [[ "$first_agent_env" =~ ^[A-Z_][A-Z0-9_]*$ ]]; then
    echo "invalid agent id or token variable name" >&2
    exit 2
fi
export FIRST_AGENT="$first_agent" FIRST_AGENT_ENV="$first_agent_env"
release="$base/releases/${expected_package:0:16}"

printf '%s  %s\n' "$expected_package" "$base/bootstrap/agent-nexus-server.zip" | sha256sum --check -
printf '%s  %s\n' "$expected_uv" "$base/bootstrap/uv.tar.gz" | sha256sum --check -
tar -xzf "$base/bootstrap/uv.tar.gz" -C "$base/bootstrap"
install -m 0755 "$base/bootstrap/uv-x86_64-unknown-linux-gnu/uv" /usr/local/bin/uv
install -m 0755 "$base/bootstrap/uv-x86_64-unknown-linux-gnu/uvx" /usr/local/bin/uvx

if ! getent passwd agent-nexus >/dev/null; then
    useradd --system --home-dir /var/lib/agent-nexus --shell /usr/sbin/nologin agent-nexus
fi
install -d -m 0755 "$release" /etc/agent-nexus
install -d -m 0750 -o agent-nexus -g agent-nexus /var/lib/agent-nexus
python3 - "$base/bootstrap/agent-nexus-server.zip" "$release" <<'PY'
import hashlib,json,sys,zipfile
from pathlib import Path
with zipfile.ZipFile(sys.argv[1]) as archive:
    manifest=json.loads(archive.read('SHA256.json'))
    assert set(archive.namelist()) == set(manifest) | {'SHA256.json'}
    for name,digest in manifest.items():
        assert not Path(name).is_absolute() and '..' not in Path(name).parts
        assert hashlib.sha256(archive.read(name)).hexdigest() == digest, name
    archive.extractall(sys.argv[2])
print('Uploaded package manifest verified')
PY

export UV_PYTHON_INSTALL_DIR="$base/python"
export UV_CACHE_DIR="$base/cache"
uv python install 3.12.11
uv sync --project "$release" --frozen --no-dev --python 3.12.11
chmod -R a+rX "$base/python" "$release"

python3 - <<'PY'
import json,os,secrets
from pathlib import Path
config_dir=Path('/etc/agent-nexus')
environment=config_dir/'gateway.env'
if not environment.exists():
    server_token=secrets.token_urlsafe(48)
    agent_token=secrets.token_urlsafe(48)
    environment.write_text(f'ANX_SERVER_TOKEN={server_token}\n{os.environ["FIRST_AGENT_ENV"]}={agent_token}\n')
    os.chmod(environment,0o600)
    client=config_dir/'client.env'
    client.write_text(f'ANX_AGENT_TOKEN={agent_token}\n')
    os.chmod(client,0o600)
config=config_dir/'gateway.json'
if not config.exists():
    config.write_text(json.dumps({'host':'127.0.0.1','port':8440,'token_env':'ANX_SERVER_TOKEN',
        'agents':{os.environ['FIRST_AGENT']:os.environ['FIRST_AGENT_ENV']},'rpc_timeout_s':180},indent=2)+'\n')
    os.chmod(config,0o644)
PY

set -a
source /etc/agent-nexus/gateway.env
set +a
"$release/.venv/bin/python" -m agent_nexus.cloud --config /etc/agent-nexus/gateway.json --check-config

if [ -L "$base/current" ]; then
    readlink "$base/current" > "$base/previous-release"
fi
ln -sfn "$release" "$base/current"
if [ -f /etc/systemd/system/agent-nexus-gateway.service ]; then
    cp -a /etc/systemd/system/agent-nexus-gateway.service "$base/bootstrap/agent-nexus-gateway.service.previous"
fi
cat > /etc/systemd/system/agent-nexus-gateway.service <<'UNIT'
[Unit]
Description=Agent Nexus gateway (cloud MCP endpoint and device ingress)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=agent-nexus
Group=agent-nexus
WorkingDirectory=/opt/agent-nexus/current
EnvironmentFile=/etc/agent-nexus/gateway.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/agent-nexus/current/.venv/bin/python -m agent_nexus.cloud --config /etc/agent-nexus/gateway.json
Restart=on-failure
RestartSec=3
TimeoutStopSec=20
StateDirectory=agent-nexus
StateDirectoryMode=0750
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/agent-nexus

[Install]
WantedBy=multi-user.target
UNIT
systemd-analyze verify /etc/systemd/system/agent-nexus-gateway.service
systemctl daemon-reload
systemctl enable --now agent-nexus-gateway.service
for attempt in $(seq 1 30); do
    if curl --fail --silent http://127.0.0.1:8440/healthz; then
        printf '\n'
        systemctl is-active agent-nexus-gateway.service
        exit 0
    fi
    sleep 1
done
journalctl -u agent-nexus-gateway.service -n 30 --no-pager
exit 1
