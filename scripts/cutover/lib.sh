# Shared helpers for gateway-cutover.sh and gateway-rollback.sh (sourced, not run).
# Expects: dry_run, python (release interpreter), new_config, bearer_file.
# Tokens are never printed: env files are sourced in a subshell, the bearer is read
# from its file by the probe itself.

t_start=$(date +%s.%N)
t_down="" t_up=""

say() { printf '[%s +%6.1fs] %s\n' "$(date +%H:%M:%S)" "$(elapsed)" "$*"; }
step() { say "== step $*"; }
die() { say "ERROR: $*" >&2; exit 1; }
elapsed() { awk -v a="$t_start" -v b="$(date +%s.%N)" 'BEGIN { printf "%.1f", b - a }'; }
mark_down() { t_down=$(date +%s.%N); }
mark_up() { t_up=$(date +%s.%N); }
window() {
    [ -n "$t_down" ] || { echo 0; return; }
    awk -v a="$t_down" -v b="${t_up:-$(date +%s.%N)}" 'BEGIN { printf "%.1f", b - a }'
}

run() {
    say "\$ $*"
    [ "$dry_run" = 1 ] && return 0
    "$@"
}

json_get() {  # json_get FILE KEY -> top-level value
    python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$1" "$2"
}

check_config() {  # the new gateway's own --check-config, with gateway.env loaded
    say "\$ (gateway.env loaded) $python -m agent_nexus.cloud --config $new_config/gateway.json --check-config"
    [ "$dry_run" = 1 ] && return 0
    (set -a; . "$new_config/gateway.env"; set +a
     "$python" -m agent_nexus.cloud --config "$new_config/gateway.json" --check-config) || die "check-config failed"
}

compare_sqlite() {  # compare_sqlite SOURCE COPY: same tables, same row counts (source is stopped)
    say "compare $1 -> $2"
    [ "$dry_run" = 1 ] && return 0
    python3 - "$1" "$2" <<'PY' || die "OAuth store copy does not match its source"
import sqlite3, sys
def counts(path, immutable):
    uri = f"file:{path}?mode=ro" + ("&immutable=1" if immutable else "")
    with sqlite3.connect(uri, uri=True) as db:
        assert db.execute("pragma integrity_check").fetchone()[0] == "ok", path
        tables = [r[0] for r in db.execute("select name from sqlite_master where type='table' order by name")]
        return {t: db.execute(f'select count(*) from "{t}"').fetchone()[0] for t in tables}
source, copy = counts(sys.argv[1], True), counts(sys.argv[2], False)
print("  rows:", ", ".join(f"{t}={n}" for t, n in source.items()) or "(no tables)")
sys.exit(0 if source == copy else 1)
PY
}

wait_health() {  # wait_health PORT SERVICE|"" TIMEOUT [VERSION]
    local port=$1 service=$2 timeout=$3 version=${4:-}
    say "waiting for http://127.0.0.1:$port/healthz (service ${service:-any}, version ${version:-any}, ${timeout}s)"
    [ "$dry_run" = 1 ] && return 0
    python3 - "$port" "$service" "$timeout" "$version" <<'PY'
import json, sys, time, urllib.request
port, service, timeout, version = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
deadline, last = time.monotonic() + timeout, None
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
            last = json.loads(response.read())
        if last.get("status") == "ok" and (not service or last.get("service") == service) \
                and (not version or last.get("version") == version):
            print("  healthz:", json.dumps(last))
            sys.exit(0)
    except Exception:
        pass
    time.sleep(0.5)
print("  healthz: no healthy answer; last:", json.dumps(last))
sys.exit(1)
PY
}

wait_agents() {  # wait_agents PORT "a,b" TIMEOUT: every agent shows up in workspace_list
    local port=$1 agents=$2 timeout=$3
    say "waiting for agents $agents on 127.0.0.1:$port (${timeout}s, bearer from $bearer_file)"
    [ "$dry_run" = 1 ] && return 0
    [ -r "$bearer_file" ] || die "bearer file $bearer_file not readable"
    "$python" - "$port" "$agents" "$timeout" "$bearer_file" <<'PY'
import asyncio, sys, time
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
port, wanted, timeout, bearer = sys.argv[1], set(sys.argv[2].split(",")), float(sys.argv[3]), open(sys.argv[4]).read().strip()
async def seen():
    async with Client(StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp", auth=bearer), timeout=20) as client:
        result = await client.call_tool("workspace_list", {})
        items = (result.structured_content or result.data or {}).get("workspaces", [])
        return {item.get("agent_id") for item in items}
deadline, have = time.monotonic() + timeout, set()
while time.monotonic() < deadline:
    try:
        have = asyncio.run(seen())
        if wanted <= have:
            print("  agents connected:", ", ".join(sorted(wanted)))
            sys.exit(0)
    except Exception as exc:
        have = {f"<{type(exc).__name__}>"}
    time.sleep(2)
print("  agents missing:", ", ".join(sorted(wanted - have)), "| seen:", ", ".join(sorted(map(str, have))))
sys.exit(1)
PY
}
