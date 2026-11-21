# Operating the agent-nexus gateway

The gateway is the public MCP endpoint (`https://<hub-domain>/mcp`) that ChatGPT, Claude
and other MCP clients connect to, and the rendezvous point for your devices
(`wss://<hub-domain>/ingress/<agent_id>`). Everything below uses placeholders:
`<hub-domain>` is your public name (for example `hub.example.test`), `<hub-host>` the
SSH name of the server.

## What ships in the gateway zip

`agent-nexus-gateway-<version>.zip` (one asset of every GitHub release) contains the
gateway source, `uv.lock`, a sample `gateway.json`, `SHA256.json` (sha256 of every other
member) and this directory:

| File | Purpose |
|---|---|
| `install.sh` | first install of an instance: system user, directories, config and env, unit, first release |
| `upgrade.py` | verify → `uv sync` → `--check-config` → backup → switch → restart → `/healthz`, rollback on failure; installed as `/opt/agent-nexus/bin/anx-upgrade` |
| `agent-nexus-gateway.service` | production unit (`127.0.0.1:8440` by default, set in `gateway.json`) |
| `agent-nexus-gateway@.service` | extra instances, e.g. `agent-nexus-gateway@trial.service` on `127.0.0.1:8441` |
| `nginx.example.conf` | generic TLS reverse proxy (`/mcp`, `/ingress/` WebSockets, everything else) |
| `anx-deploy.sudoers.example` | lets the CI deploy user run `anx-upgrade` and nothing else |
| `auto_update.py` | the gateway's self-update, installed as `/opt/agent-nexus/bin/anx-auto-upgrade`: follows `downloads.manifest_url` and hands newer stable releases to `anx-upgrade` |
| `agent-nexus-gateway-update.service` / `.timer` | runs it every minute (it only fetches the small `downloads.json`); always on (every upgrade enables it again) |

## Updates

The gateway updates itself. CI publishes releases (GitHub and the download mirror) and
deploys nothing; within about a minute the timer on the host sees the new version in
`downloads.json`, downloads `agent-nexus-gateway-<version>.zip`, checks its SHA-256 and runs
`anx-upgrade` (verify, `uv sync`, `--check-config`, backup, switch, restart, `/healthz`, rollback
on failure). A version whose upgrade failed is retried after 6 hours; prereleases are never
installed by it. `journalctl -u agent-nexus-gateway-update.service` shows what it did;
`anx-auto-upgrade --dry-run` says what it would do. `deploy-gateway.yml` remains for a person:
a trial instance, or rolling back to an older tag.

## Layout

| | production | instance `<name>` (e.g. `trial`) |
|---|---|---|
| releases, `current` symlink, backups | `/opt/agent-nexus` | `/opt/agent-nexus-<name>` |
| `gateway.json`, `gateway.env` | `/etc/agent-nexus` | `/etc/agent-nexus-<name>` |
| state (`oauth.sqlite3`, `gateway.sqlite3`) | `/var/lib/agent-nexus` | `/var/lib/agent-nexus-<name>` |
| unit | `agent-nexus-gateway.service` | `agent-nexus-gateway@<name>.service` |

Releases live in `releases/<version>-<sha8>`; `current` is switched atomically. The
gateway runs as the unprivileged `agent-nexus` user. Re-deploying the package that is
already live (for example re-running `deploy-gateway.yml` for the same tag after a probe
failure) extracts to a fresh `releases/<version>-<sha8>-<n>` and switches to that; the
running release directory and its `.venv` are never replaced in place.

## First install

Requirements: a Linux host with systemd, `python3`, [`uv`](https://docs.astral.sh/uv/) and
a TLS reverse proxy (see `nginx.example.conf`).

```sh
# on <hub-host>, as root
sha=$(sha256sum agent-nexus-gateway-<version>.zip | cut -d' ' -f1)   # compare with SHA256SUMS of the release
unzip -q agent-nexus-gateway-<version>.zip 'deploy/gateway/*' -d /tmp/anx
bash /tmp/anx/deploy/gateway/install.sh --package agent-nexus-gateway-<version>.zip --sha256 "$sha" \
    --public-url https://<hub-domain> --first-agent pc-main
```

`install.sh` never overwrites an existing `gateway.json` or `gateway.env`. On first run it
generates `ANX_SERVER_TOKEN` and one device token into `gateway.env` (mode 0600) and writes
the matching device token to `client.env` next to it, to hand to that device. Edit
`gateway.json` for OAuth (`auth`), more `agents`, and the mailbox settings, then
`systemctl restart agent-nexus-gateway`. Check the result:

```sh
curl -s http://127.0.0.1:8440/healthz
# {"status": "ok", "service": "agent-nexus-gateway", "version": "<version>", "features": [...], "db_schema": N}
```

New devices are best added with an invite from the console (one-time code, no file
copying); see the main README.

### Trial instance (parallel run on 127.0.0.1:8441)

```sh
bash /tmp/anx/deploy/gateway/install.sh --instance trial --package ... --sha256 "$sha" --port 8441
```

It has its own config, state and unit and never shares files with production. Point a
test device at it through an SSH tunnel or a separate proxy location.

## Upgrades

```sh
sudo /opt/agent-nexus/bin/anx-upgrade --package /opt/agent-nexus/incoming/agent-nexus-gateway-<version>.zip \
    --sha256 <sha256>
# trial: add --base /opt/agent-nexus-trial --config-dir /etc/agent-nexus-trial \
#        --state-dir /var/lib/agent-nexus-trial --service agent-nexus-gateway@trial.service --port 8441
# dry run (verify, sync, --check-config; nothing switched): add --check-config
```

What it does, in order: checks the zip's sha256 and every member against `SHA256.json`
(no `..`, no `.private`/`secrets`/`.venv`/`logs`), extracts to `releases/<version>-<sha8>`,
runs `uv sync --frozen --no-dev` and `python -m agent_nexus.cloud --check-config` with
`gateway.env` loaded, backs up `gateway.json`, `gateway.env` and every `*.sqlite3` of the
state directory to `backups/deployment-<timestamp>`, switches `current`, restarts the unit
and polls `/healthz` until it reports the new version (60 s by default). If anything after
the switch fails it points `current` back, restores the databases from the backup,
restarts the previous release and exits non-zero. A failure before the switch (bad hash,
failed `uv sync`, failed `--check-config`) leaves the running gateway untouched, also when
the package is the one already running. Old releases are pruned only after a successful
switch (`--keep-releases`, default 5; the previous release is always kept).

Note: the sudoers rule lets `anx-deploy` run `anx-upgrade` as root with any arguments, and
`--check-config` runs the package's own code as root, so the deploy key is effectively a
root key for this host. Guard it accordingly (the `production` environment's reviewer gate
is the control).

Hosts still on the legacy layout pass their paths explicitly (`--base /opt/mcpmanager
--config-dir /etc/mcpmanager --config-name cloud.json --env-name server.env --state-dir
/var/lib/mcpmanager --service mcpmanager-cloud.service`).

## Web console, invites and downloads

The site root (`https://<hub-domain>/`) is the owner's web console: sign in with the OAuth
owner password (the one the ChatGPT / Claude consent page asks for), or with the gateway
token on a bearer-only gateway. One sign-in also covers the `/inbox/` PWA.

- **Invites** (design §11.5): *设备 → 邀请新设备* in the console, or on the host
  `anx-gateway invite create --config /etc/agent-nexus/gateway.json [--ttl 24h] [--label …]`.
  A device redeems the code with `POST /enroll` and gets its own token (only its sha256 is
  stored, in `gateway.sqlite3`). `anx-gateway device revoke --config … <agent_id>` (or the
  console's *吊销*) closes its link with WebSocket code 4401 within a few seconds. Devices
  in the `agents` map of `gateway.json`/`gateway.env` keep working; the map may now be empty.
  `invites: {"default_ttl_s": 86400, "max_ttl_s": 604800}` in `gateway.json` changes the
  lifetimes. Invite codes carry `public_url`, so it must be set.
- **Downloads**: `GET /downloads` (a public page), `GET /api/downloads` and
  `GET /downloads/<file>` serve the release artifacts found in `downloads.dir`
  (default `<state_dir>/downloads`, created by `install.sh`; `anx-gateway migrate` writes the
  key), plus every installer of the latest release when `downloads.manifest_url` is set:
  `"downloads": {"manifest_url": "https://muxigame-prod-static-cn.oss-cn-hangzhou.aliyuncs.com/agent-nexus/downloads.json"}`.
  Release CI (`scripts/publish_oss.py`) rewrites that file with each release, so nothing has to be
  copied to the host; the gateway re-reads it every 5 minutes. Without it, copy the installers by hand, e.g.
  `install -m 0644 -o agent-nexus agent-nexus_<version>_x64-setup.exe SHA256SUMS /var/lib/agent-nexus/downloads/`.
  Only `.exe .msi .deb .rpm .AppImage .dmg .zip .tar.gz` files directly in that directory
  are served; SHA-256 comes from `SHA256SUMS` or is computed once. Optional:
  `"downloads": {"release_url": "https://github.com/<owner>/<repo>/releases/download/v<version>",
  "links": [{"label": "GitHub Releases", "url": "https://github.com/<owner>/<repo>/releases"}]}`
  (files missing locally are redirected to `release_url`; links are listed on the page).
- nginx: the console, `/auth/*`, `/api/*`, `/downloads*` and `/enroll` go to the gateway
  like every other path (`nginx.example.conf` already proxies `/`).

## Deploying from GitHub Actions

`.github/workflows/deploy-gateway.yml` (manual, environment `production`) downloads the
release's gateway zip, checks it against `SHA256SUMS`, copies it to
`/opt/agent-nexus/incoming/` and runs `sudo anx-upgrade` over SSH, then probes `/healthz`.

One-time host setup for it:

```sh
bash /tmp/anx/deploy/gateway/install.sh ... --deploy-key /path/to/anx-deploy.pub
```

creates the user `anx-deploy` (key marked `restrict`), the upload directory
`/opt/agent-nexus/incoming` and `/etc/sudoers.d/anx-deploy` from
`anx-deploy.sudoers.example`. Then add to the repository's `production` environment:

| Name | Kind | Value |
|---|---|---|
| `DEPLOY_HOST` | secret | `<hub-host>` |
| `DEPLOY_USER` | secret | `anx-deploy` |
| `DEPLOY_SSH_KEY` | secret | the private half of `anx-deploy.pub` |
| `DEPLOY_KNOWN_HOSTS` | secret | output of `ssh-keyscan <hub-host>` (verify it) |
| `GATEWAY_PROBE_TOKEN` | secret, optional | bearer token for `/healthz?detail=1` |
| `GATEWAY_PUBLIC_URL` | variable | `https://<hub-domain>` |

Give the environment required reviewers so every deploy is approved by a person.
