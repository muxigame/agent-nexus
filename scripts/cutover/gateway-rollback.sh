#!/usr/bin/env bash
# Gateway rollback, agent-nexus -> legacy (design §12.5 step 5). Run as root on <hub-host>.
#
# Stops the new gateway, copies its OAuth store back over the legacy one with an sqlite
# online backup (tokens and client registrations issued after the cutover keep working;
# both sides use the same mcpkit schema), starts the legacy unit on the same port and
# waits for /healthz. Devices reconnect on their own (same URL, same tokens). Mailbox
# data stays in the new state directory for the next cutover; the legacy gateway has no
# mailbox. The legacy OAuth store being replaced is saved to the backup directory first.
#
#   gateway-rollback.sh [options]      (same options as gateway-cutover.sh, plus:)
#   --keep-legacy-db    do not copy the OAuth store back (tokens issued since the
#                       cutover stop working; only for a broken new store)
#   --disable-new / --no-disable-new   disable the new unit (default: disable)
set -euo pipefail

legacy_unit=mcpmanager-cloud.service legacy_config=/etc/mcpmanager legacy_state=/var/lib/mcpmanager
new_unit=agent-nexus-gateway.service new_config=/etc/agent-nexus new_state=/var/lib/agent-nexus
release=/opt/agent-nexus/current port=8440 owner=agent-nexus expect_agents="" bearer_file=""
health_timeout=60 agent_timeout=120 backup_root=/opt/agent-nexus/backups dry_run=0 keep_db=0 disable_new=1
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
        --stop-unit|--expect-version) shift 2 ;;  # accepted so both scripts take one option set
        --expect-agents) expect_agents=$2; shift 2 ;;
        --bearer-file) bearer_file=$2; shift 2 ;;
        --health-timeout) health_timeout=$2; shift 2 ;;
        --agent-timeout) agent_timeout=$2; shift 2 ;;
        --backup-dir) backup_root=$2; shift 2 ;;
        --keep-legacy-db) keep_db=1; shift ;;
        --disable-new) disable_new=1; shift ;;
        --no-disable-new) disable_new=0; shift ;;
        --dry-run) dry_run=1; shift ;;
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
# The legacy gateway reads its bearer from its own config directory.
bearer_file=${bearer_file:-$legacy_config/secrets/workspace/bearer-token}
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib.sh
. "$here/lib.sh"

[ "$(id -u)" = 0 ] || [ "$dry_run" = 1 ] || die "run as root"
python=$release/.venv/bin/python
[ -f "$legacy_config/cloud.json" ] || die "no $legacy_config/cloud.json"
legacy_db=$legacy_state/oauth.sqlite3 new_db=$new_state/oauth.sqlite3
[ "$keep_db" = 1 ] || [ -f "$new_db" ] || die "no $new_db (use --keep-legacy-db to start the legacy store as it is)"
backup_dir=$backup_root/rollback-$(date +%Y%m%d-%H%M%S)
say "rollback $new_unit -> $legacy_unit on 127.0.0.1:$port; backups: $backup_dir"

step "1 stop $new_unit"
run systemctl stop "$new_unit"
mark_down

if [ "$keep_db" = 0 ]; then
    step "2 OAuth store back to $legacy_db"
    run install -d -m 0700 "$backup_dir"
    if [ -f "$legacy_db" ]; then
        run sqlite3 "$legacy_db" ".backup '$backup_dir/legacy-oauth.sqlite3'"
    fi
    owner_group=$(stat -c %U:%G "$legacy_state")
    run sqlite3 "$new_db" ".backup '$backup_dir/new-oauth.sqlite3'"
    # .restore writes into the existing file: inode, owner and mode stay the legacy unit's.
    run rm -f "$legacy_db-wal" "$legacy_db-shm"
    run sqlite3 "$legacy_db" ".restore '$backup_dir/new-oauth.sqlite3'"
    run chown "$owner_group" "$legacy_db"
    run chmod 0600 "$legacy_db"
    compare_sqlite "$backup_dir/new-oauth.sqlite3" "$legacy_db"
else
    step "2 OAuth store: kept as it is (--keep-legacy-db)"
fi

step "3 start $legacy_unit"
run systemctl enable "$legacy_unit"
run systemctl restart "$legacy_unit"
wait_health "$port" "" "$health_timeout" || die "$legacy_unit did not become healthy; journalctl -u $legacy_unit"
mark_up
if [ "$disable_new" = 1 ]; then
    run systemctl disable "$new_unit" || true
fi
if [ -n "$expect_agents" ]; then
    wait_agents "$port" "$expect_agents" "$agent_timeout" || die "agents did not reconnect: $expect_agents"
fi
say "ROLLBACK OK in $(elapsed)s (gateway down for $(window)s). New state kept in $new_state."
