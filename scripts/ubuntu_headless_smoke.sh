#!/usr/bin/env bash
# The Ubuntu package on a server without a desktop.
#
#   bash scripts/ubuntu_headless_smoke.sh path/to/agent-nexus_<version>_amd64.deb
#
# 1. A clean ubuntu:22.04 container installs it with --no-install-recommends: no GTK,
#    WebKit or python3-gi may come with it, `agent-nexus` prints the headless commands,
#    the bundled `anx` runs, and a bad invite fails with a message, not a traceback.
# 2. On the runner (real systemd): the package's postinst sets up the root system service
#    (/var/lib/agent-nexus, 执行账户 = the installing user); a gateway from this checkout makes
#    an invite, `sudo agent-nexus join` redeems it and restarts the service, and the gateway
#    lists the device online with os_family ubuntu.
# 3. The console's remote terminal reaches a real shell on that device (scripts/terminal_smoke.py).
#
# Needs docker, passwordless sudo and `uv` with this checkout synced (GitHub's runners).
set -euo pipefail

deb="$(readlink -f "$1")"
here="$(cd "$(dirname "$0")/.." && pwd)"

echo "--- 1. clean container, --no-install-recommends"
docker run --rm -v "$(dirname "$deb"):/pkg:ro" -e DEB="/pkg/$(basename "$deb")" ubuntu:22.04 bash -euo pipefail -c '
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$DEB" >/dev/null
    for desktop in python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1; do
        if dpkg -s "$desktop" >/dev/null 2>&1; then echo "error: $desktop was installed" >&2; exit 1; fi
    done
    useradd -m anx
    su - anx -c "agent-nexus" >/tmp/help 2>&1 || { cat /tmp/help; exit 1; }
    grep -q "agent-nexus service enable" /tmp/help || { cat /tmp/help; exit 1; }
    su - anx -c "agent-nexus anx --help" >/tmp/anx 2>&1 || { cat /tmp/anx; exit 1; }
    grep -q "join" /tmp/anx || { cat /tmp/anx; exit 1; }
    set +e
    su - anx -c "agent-nexus join not-an-invite" >/tmp/join 2>&1
    rc=$?
    set -e
    cat /tmp/join
    if [ "$rc" -eq 0 ] || grep -q Traceback /tmp/join; then echo "error: a bad invite must fail cleanly" >&2; exit 1; fi
    echo "ok: installs without desktop libraries; headless commands work"
'

echo "--- 2. the system service: set up by postinst, join a gateway"
sudo apt-get update -qq
# --reinstall: the offline check may have installed this version already (without the setup),
# and only a real configure runs postinst.
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --reinstall --no-install-recommends "$deb" >/dev/null
for _ in $(seq 1 300); do
    [ -f /var/lib/agent-nexus/.system ] && [ "$(systemctl is-active agent-nexus.service)" = "active" ] && break
    sleep 1
done
if [ ! -f /var/lib/agent-nexus/.system ]; then
    echo "error: postinst did not set up the system service" >&2
    sudo journalctl -u 'agent-nexus*' -n 80 --no-pager >&2 || true
    exit 1
fi
systemctl is-enabled agent-nexus.service
sudo python3 -c 'import json,sys; a=json.load(open("/var/lib/agent-nexus/agent-nexus.json")).get("accounts",{}); print("accounts:", a); sys.exit(0 if a.get("default")==sys.argv[1] else 1)' "$(id -un)"
work="$(mktemp -d)"
port=18440
SERVER_TOKEN="$(openssl rand -hex 24)"
export SERVER_TOKEN
cat >"$work/gateway.json" <<EOF
{"token_env": "SERVER_TOKEN", "agents": {}, "public_url": "http://127.0.0.1:$port", "port": $port,
 "state_dir": "$work/state"}
EOF
(cd "$here" && uv run --frozen python -m agent_nexus.cloud --config "$work/gateway.json") >"$work/gateway.log" 2>&1 &
gateway=$!
cleanup() {
    sudo agent-nexus service disable >/dev/null 2>&1 || true
    kill "$gateway" 2>/dev/null || true
}
trap cleanup EXIT
for _ in $(seq 1 60); do curl -fsS -m 2 "http://127.0.0.1:$port/healthz" >/dev/null 2>&1 && break; sleep 1; done
auth=(-H "Authorization: Bearer $SERVER_TOKEN")
code="$(curl -fsS "${auth[@]}" -H 'Content-Type: application/json' -d '{"label":"headless"}' \
    "http://127.0.0.1:$port/api/invites" | python3 -c 'import json,sys; print(json.load(sys.stdin)["code"])')"

# No user instance, no linger: the core is the system service; join re-runs itself with sudo.
agent-nexus join "$code"
agent-nexus service status

for _ in $(seq 1 90); do
    if curl -fsS "${auth[@]}" "http://127.0.0.1:$port/api/devices" | python3 -c '
import json, sys
devices = json.load(sys.stdin)["devices"]
online = [d for d in devices if d["online"]]
print("devices:", [(d["device_id"], d["online"], d.get("os_family")) for d in devices])
sys.exit(0 if online and online[0].get("os_family") == "ubuntu" else 1)'; then
        agent-nexus service status | tee "$work/status"
        grep -q "connected" "$work/status"
        echo "ok: joined, running as the system service, online as an Ubuntu device"
        echo "--- 3. a remote terminal through the gateway (real pty on the installed device)"
        (cd "$here" && uv run --frozen python scripts/terminal_smoke.py --gateway "http://127.0.0.1:$port" --token "$SERVER_TOKEN")
        exit 0
    fi
    sleep 2
done
echo "error: the device never came online" >&2
sudo journalctl -u agent-nexus.service -n 80 --no-pager >&2 || true
tail -40 "$work/gateway.log" >&2
exit 1
