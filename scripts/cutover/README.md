# Gateway cutover and rollback (design §12.5)

Run as root on `<hub-host>` after the release is installed under `/opt/agent-nexus`
(`deploy/gateway/install.sh` or `anx-upgrade`). Copy this directory to the host; the two
scripts source `lib.sh` from their own directory.

| Script | Does |
|---|---|
| `gateway-cutover.sh` | preflight (`--check-config` while the legacy gateway still serves) → stop the legacy unit → sqlite online backup of `oauth.sqlite3` and the media cache into the new state directory (`anx-gateway migrate`; an older copy is moved to the backup directory first, existing config files are kept) → row-count comparison → start the new unit on the same port → `/healthz` (and optionally the agents) → disable the legacy unit. Any failure after the stop restarts the legacy unit automatically. |
| `gateway-rollback.sh` | stop the new unit → copy its `oauth.sqlite3` back over the legacy one (`.restore` into the existing file; the replaced store is saved first) → start and enable the legacy unit → `/healthz` (and optionally the agents) → disable the new unit. Mailbox data stays in the new state directory. |

Both take the same options (`--help`); the defaults are the production layout
(`mcpmanager-cloud.service`, `/etc/mcpmanager`, `/var/lib/mcpmanager` →
`agent-nexus-gateway.service`, `/etc/agent-nexus`, `/var/lib/agent-nexus`, port 8440).
`--dry-run` prints every command without changing anything. Tokens are never printed.

Production cutover (after the stage-2 trial on 8441):

```sh
./gateway-cutover.sh --stop-unit agent-nexus-gateway-trial.service --expect-version <version> \
    --expect-agents <agent-id>[,<agent-id>...]
# rollback, any time later:
./gateway-rollback.sh --expect-agents <agent-id>[,<agent-id>...]
```

Rehearsal (gate G4) on spare ports: a copy of the legacy release's config and state
(`<legacy-copy>` unit, port 8441) and the trial unit play legacy and new; pass
`--legacy-unit/--legacy-config/--legacy-state/--new-unit/--new-config/--new-state/--port`.
`--expect-version <wrong>` exercises the automatic rollback.

Note: `/etc/agent-nexus` created by the cutover is a fresh `anx-gateway migrate` of
`/etc/mcpmanager` (port, `public_url`, agents and tokens unchanged); devices added only to
the trial config (e.g. `pc-next`) are not carried over.
