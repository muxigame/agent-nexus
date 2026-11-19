#!/usr/bin/env bash
# Install an Agent Nexus .deb and start it with the network cut off: the manager and the
# device plugin must come up from the bundled CPython and package cache alone.
#
#   bash scripts/ubuntu_offline_smoke.sh path/to/agent-nexus_<version>_amd64.deb
#
# Needs passwordless sudo (GitHub's Ubuntu runners have it). The system packages the .deb
# depends on come from apt (with the network); only the Agent Nexus start runs offline.
set -euo pipefail

if [ "${1:-}" != "--offline-run" ]; then
    deb="$(readlink -f "$1")"
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$deb"
    work="$(mktemp -d)"
    chmod 755 "$work"
    # A fresh network namespace has only a loopback interface: no DNS, no PyPI, no GitHub.
    sudo unshare --net -- sh -c 'ip link set lo up && exec sudo -u "$1" -H env PATH="/usr/bin:/bin" bash "$2" --offline-run "$3"' \
        _ "$(id -un)" "$(readlink -f "$0")" "$work"
    exit $?
fi

work="$2"
export ANX_ROOT="$work/agent-nexus"
log="$work/serve.log"
if curl -fsS -m 5 -o /dev/null https://pypi.org/simple/ 2>/dev/null; then
    echo "error: the network is reachable; this check must run without it" >&2
    exit 2
fi
/usr/bin/agent-nexus --serve >"$log" 2>&1 &
pid=$!
cleanup() { pkill -TERM -P "$pid" 2>/dev/null || true; kill "$pid" 2>/dev/null || true; }
trap cleanup EXIT

for _ in $(seq 1 180); do
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "error: agent-nexus --serve exited before the manager came up" >&2
        cat "$log" >&2
        exit 1
    fi
    body="$(curl -fsS -m 2 -H 'X-ANX: 1' http://127.0.0.1:8399/api/services 2>/dev/null || true)"
    if [ -n "$body" ] && printf '%s' "$body" | python3 -c '
import json, sys
services = {s["name"]: s["status"] for s in json.load(sys.stdin)["services"]}
print("services:", services)
sys.exit(0 if services.get("device") == "running" else 1)'; then
        echo "ok: manager and device plugin are running with no network"
        exit 0
    fi
    sleep 2
done
echo "error: timed out waiting for the device plugin" >&2
cat "$log" >&2
exit 1
