#!/usr/bin/env bash
# Gateway cutover, legacy -> agent-nexus (design §12.5, stage 3). Run as root on <hub-host>.
#
# Window: the legacy unit is down from step 3 until the new unit answers /healthz
# (typically 10-20 s). Any failure after the legacy unit was stopped rolls back on its
# own: the new unit is stopped and the legacy unit started again. The legacy config and
# state are only read (sqlite online backup), so the automatic rollback needs no copy.
#
#   gateway-cutover.sh [options]
#
# Options (defaults are the production layout of design §4.3 / §12.5):
#   --legacy-unit U     legacy gateway unit                  (mcpmanager-cloud.service)
#   --legacy-config D   legacy config dir (cloud.json, server.env, secrets/)  (/etc/mcpmanager)
#   --legacy-state D    legacy state dir (oauth.sqlite3, media/)              (/var/lib/mcpmanager)
#   --new-unit U        agent-nexus gateway unit             (agent-nexus-gateway.service)
#   --new-config D      agent-nexus config dir               (/etc/agent-nexus)
#   --new-state D       agent-nexus state dir                (/var/lib/agent-nexus)
#   --release D         installed agent-nexus release        (/opt/agent-nexus/current)
#   --port N            port both units listen on (the reverse proxy's upstream)  (8440)
#   --owner USER        system user of the new unit          (agent-nexus)
#   --stop-unit U       also stop and disable this unit after a successful switch
#                       (repeatable; e.g. agent-nexus-gateway-trial.service)
#   --expect-version V  /healthz version the new unit must report (default: any)
#   --expect-agents L   comma-separated agent ids that must reconnect (workspace_list)
#   --bearer-file F     bearer for the agent check (default <new-config>/secrets/workspace/bearer-token)
#   --health-timeout S  seconds to wait for /healthz           (60)
#   --agent-timeout S   seconds to wait for --expect-agents    (120)
#   --backup-dir D      where the pre-cutover copies go        (/opt/agent-nexus/backups)
#   --dry-run           print the plan and the commands, change nothing
#
# Example (rehearsal on spare ports, see docs/design.md §12.1 G4):
#   gateway-cutover.sh --legacy-unit <legacy-copy>.service --legacy-config /etc/<legacy-copy> \
#       --legacy-state /var/lib/<legacy-copy> --new-unit agent-nexus-gateway-trial.service \
#       --new-config /etc/agent-nexus-trial --new-state /var/lib/agent-nexus-trial --port 8441
set -euo pipefail

legacy_unit=mcpmanager-cloud.service legacy_config=/etc/mcpmanager legacy_state=/var/lib/mcpmanager
new_unit=agent-nexus-gateway.service new_config=/etc/agent-nexus new_state=/var/lib/agent-nexus
release=/opt/agent-nexus/current port=8440 owner=agent-nexus expect_version="" expect_agents=""
bearer_file="" health_timeout=60 agent_timeout=120 backup_root=/opt/agent-nexus/backups dry_run=0
stop_units=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --legacy-unit) legacy_unit=$2; shift 2 ;;
        --legacy-config) legacy_config=$2; shift 2 ;;
        --legacy-state) legacy_state=$2; shift 2 ;;
        --new-unit) new_unit=$2; shift 2 ;;
        --new-config) new_config=$2; shift 2 ;;
        --new-state) new_state=$2; shift 2 ;;
        --release) release=$2; shift 2 ;;
        --port) port=$2; shift 2 ;;
        --owner) owner=$2; shift 2 ;;
        --stop-unit) stop_units+=("$2"); shift 2 ;;
        --expect-version) expect_version=$2; shift 2 ;;
        --expect-agents) expect_agents=$2; shift 2 ;;
        --bearer-file) bearer_file=$2; shift 2 ;;
        --health-timeout) health_timeout=$2; shift 2 ;;
        --agent-timeout) agent_timeout=$2; shift 2 ;;
        --backup-dir) backup_root=$2; shift 2 ;;
        --dry-run) dry_run=1; shift ;;
        -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
bearer_file=${bearer_file:-$new_config/secrets/workspace/bearer-token}
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib.sh
. "$here/lib.sh"

[ "$(id -u)" = 0 ] || [ "$dry_run" = 1 ] || die "run as root"
python=$release/.venv/bin/python
[ -x "$python" ] || die "no release environment at $release (install the release first: anx-upgrade / install.sh)"
[ -f "$legacy_config/cloud.json" ] || die "no $legacy_config/cloud.json"
[ -f "$legacy_state/oauth.sqlite3" ] || die "no $legacy_state/oauth.sqlite3"
getent passwd "$owner" >/dev/null || die "system user $owner does not exist (install.sh creates it)"
stamp=$(date +%Y%m%d-%H%M%S)
backup_dir=$backup_root/cutover-$stamp
legacy_was_active=0 legacy_was_enabled=0
systemctl is-active --quiet "$legacy_unit" && legacy_was_active=1
systemctl is-enabled --quiet "$legacy_unit" 2>/dev/null && legacy_was_enabled=1

say "cutover $legacy_unit -> $new_unit on 127.0.0.1:$port (release $(readlink -f "$release"))"
say "legacy: $legacy_config + $legacy_state; new: $new_config + $new_state; backups: $backup_dir"

# 1. Unit file for the new gateway (the production unit ships in the release).
step "1 unit file"
if [ ! -f "/etc/systemd/system/$new_unit" ] && [ ! -f "/lib/systemd/system/$new_unit" ]; then
    shipped=$release/deploy/gateway/$new_unit
    [ -f "$shipped" ] || die "no unit file $new_unit installed and none shipped at $shipped"
    run install -m 0644 "$shipped" "/etc/systemd/system/$new_unit"
    run systemctl daemon-reload
fi

# 2. Pre-flight while the legacy gateway still serves: a config migrated now must pass
#    --check-config (a fresh /etc/agent-nexus only; an existing one is kept as it is).
step "2 preflight"
run install -d -m 0700 "$backup_dir"
if [ ! -f "$new_config/gateway.json" ]; then
    # Rewrites the state paths in the copied config; the state copied here is refreshed in step 4.
    run "$python" -m agent_nexus.cloud migrate --from "$legacy_config" --state "$legacy_state" \
        --to "$new_config" --state-to "$new_state" --owner "$owner" --apply
fi
check_config
config_port=$(json_get "$new_config/gateway.json" port)
[ "$dry_run" = 1 ] || [ "$config_port" = "$port" ] || die "$new_config/gateway.json listens on port $config_port, expected $port"

# From here on a failure restores the legacy gateway.
rollback_on_failure() {
    local status=$?
    trap - EXIT
    if [ "$status" -ne 0 ] && [ "$dry_run" = 0 ]; then
        say "FAILED (exit $status) after $(elapsed)s: rolling back to $legacy_unit"
        systemctl stop "$new_unit" || true
        systemctl disable "$new_unit" 2>/dev/null || true
        [ "$legacy_was_enabled" = 0 ] || systemctl enable "$legacy_unit" 2>/dev/null || true
        systemctl start "$legacy_unit" || true
        if wait_health "$port" "" "$health_timeout"; then
            say "rolled back: $legacy_unit healthy on $port after $(elapsed)s"
            if [ -n "$expect_agents" ]; then
                legacy_bearer=$legacy_config/secrets/workspace/bearer-token
                bearer_file=$legacy_bearer wait_agents "$port" "$expect_agents" "$agent_timeout" \
                    || say "WARNING: agents not back yet: $expect_agents"
            fi
        else
            say "WARNING: $legacy_unit did not answer /healthz; check: journalctl -u $legacy_unit"
        fi
    fi
    exit "$status"
}
trap rollback_on_failure EXIT

# 3. Stop the legacy gateway: from now on nothing writes its OAuth store.
step "3 stop $legacy_unit"
run systemctl stop "$legacy_unit"
mark_down

# 4. Copy the latest state: the OAuth store through an sqlite online backup (clients
#    registered and tokens issued since the trial started come along), media cache too.
#    An earlier copy (trial, previous cutover) is moved aside first, never merged.
step "4 copy state"
run install -d -m 0750 -o "$owner" -g "$owner" "$new_state"
for name in oauth.sqlite3 oauth.sqlite3-wal oauth.sqlite3-shm; do
    if [ -e "$new_state/$name" ]; then
        run mv "$new_state/$name" "$backup_dir/new-state-before-$name"
    fi
done
run "$python" -m agent_nexus.cloud migrate --from "$legacy_config" --state "$legacy_state" \
    --to "$new_config" --state-to "$new_state" --owner "$owner" --apply || {
    code=$?
    # 3 = conflicts: files that already exist in the new layout are kept as they are
    # (an edited gateway.json, the gateway.env with extra tokens, changed media).
    [ "$code" = 3 ] || exit "$code"
    say "existing files in $new_config / $new_state kept (listed as conflict above)"
}
compare_sqlite "$legacy_state/oauth.sqlite3" "$new_state/oauth.sqlite3"
check_config

# 5. Start the new gateway on the same port; the reverse proxy needs no change.
step "5 start $new_unit"
run systemctl enable "$new_unit"
run systemctl restart "$new_unit"
wait_health "$port" agent-nexus-gateway "$health_timeout" "$expect_version" || die "$new_unit did not become healthy"
mark_up
if [ -n "$expect_agents" ]; then
    wait_agents "$port" "$expect_agents" "$agent_timeout" || die "agents did not reconnect: $expect_agents"
fi

# 6. Make it permanent.
step "6 disable $legacy_unit"
run systemctl disable "$legacy_unit"
for unit in "${stop_units[@]}"; do
    run systemctl disable --now "$unit" || true
done
trap - EXIT
say "CUTOVER OK in $(elapsed)s (legacy down for $(window)s). Rollback: gateway-rollback.sh with the same options."
[ "$legacy_was_active" = 1 ] || say "note: $legacy_unit was not running before the cutover"
