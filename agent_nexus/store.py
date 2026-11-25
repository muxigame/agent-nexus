"""The gateway's durable state: ``gateway.sqlite3`` (design §8.4).

M4 creates the ``devices`` and ``handles`` tables: context handles
(``workspace_id``) survive gateway restarts, and the owner's device aliases
(decision D6) live here. Later milestones add their tables through
``MIGRATIONS``; ``schema_version`` is a single integer.

The database is opened lazily, so ``anx-gateway --check-config`` never creates
or touches it. When the configured location cannot be opened the gateway keeps
running on an in-memory database and logs why (handles then last only until
the next restart, as they did before agent-nexus).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

#: Sliding lifetime of a context handle (the legacy in-memory semantics).
HANDLE_TTL_S = 86400.0
#: Only rewrite ``expires_at`` when a handle has aged this much since its
#: last extension; every tool call would otherwise be a database write.
HANDLE_TOUCH_SLACK_S = 60.0
MAX_ALIAS_CHARS = 64

MIGRATIONS: list[str] = [
    # 1: devices and persisted handles (M4). Columns used by later milestones
    # (enrolled_via, address, superseded_by) exist already so their
    # migrations only add tables.
    """
    CREATE TABLE devices(
      agent_id TEXT PRIMARY KEY, features TEXT, device_json TEXT,
      hostname TEXT, alias TEXT,
      enrolled_via TEXT,
      online INTEGER NOT NULL DEFAULT 0, last_connected_at REAL, last_seen_at REAL);
    CREATE TABLE handles(
      handle TEXT PRIMARY KEY, address TEXT,
      agent_id TEXT NOT NULL, workspace TEXT NOT NULL, client_id TEXT,
      created_at REAL NOT NULL, expires_at REAL NOT NULL, superseded_by TEXT,
      meta TEXT);
    CREATE INDEX handles_expiry ON handles(expires_at);
    """,
    # 2: the mailbox (M6, design §8.4): addresses, messages, per-recipient
    # deliveries, asks (decision D11), events and counters. ``me`` (the
    # owner, kind human) always exists. Wake jobs arrive with M7.
    """
    CREATE TABLE addresses(
      address TEXT PRIMARY KEY, name TEXT NOT NULL, agent_id TEXT NOT NULL, kind TEXT NOT NULL,
      workdir TEXT, resume_id TEXT, resume_source TEXT, resume_confidence REAL,
      companion_route TEXT, wake_policy TEXT NOT NULL DEFAULT 'auto', wake_permission TEXT,
      oauth_client_id TEXT, last_seen_at REAL, last_handle TEXT,
      created_at REAL NOT NULL, ended_at REAL, meta TEXT);
    CREATE INDEX addresses_name ON addresses(name);
    CREATE INDEX addresses_client ON addresses(oauth_client_id, last_seen_at);
    CREATE INDEX handles_address ON handles(address);
    CREATE TABLE groups(name TEXT PRIMARY KEY, members_json TEXT NOT NULL);
    CREATE TABLE messages(
      id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, reply_to TEXT, from_addr TEXT NOT NULL,
      to_expr TEXT NOT NULL, subject TEXT, body TEXT NOT NULL,
      kind TEXT NOT NULL DEFAULT 'message', priority TEXT NOT NULL DEFAULT 'normal',
      hop INTEGER NOT NULL DEFAULT 0, attachments_json TEXT, meta_json TEXT, created_at REAL NOT NULL);
    CREATE INDEX messages_thread ON messages(thread_id, created_at);
    CREATE INDEX messages_from ON messages(from_addr, created_at);
    CREATE TABLE deliveries(
      message_id TEXT NOT NULL REFERENCES messages(id), to_addr TEXT NOT NULL,
      state TEXT NOT NULL DEFAULT 'unread',
      noticed_count INTEGER NOT NULL DEFAULT 0, first_noticed_at REAL, read_at REAL,
      woken_at REAL, escalation_level INTEGER NOT NULL DEFAULT 0,
      expired_at REAL, recalled_at REAL,
      PRIMARY KEY(message_id, to_addr));
    CREATE INDEX deliveries_unread ON deliveries(to_addr, state);
    CREATE TABLE asks(
      id TEXT PRIMARY KEY, from_addr TEXT NOT NULL, message_id TEXT REFERENCES messages(id),
      thread_id TEXT, title TEXT, question TEXT NOT NULL, options_json TEXT NOT NULL,
      recommended TEXT, context TEXT, kind TEXT NOT NULL DEFAULT 'single',
      allow_other INTEGER NOT NULL DEFAULT 1, blocking INTEGER NOT NULL DEFAULT 0,
      origin TEXT NOT NULL DEFAULT 'ask_user', state TEXT NOT NULL DEFAULT 'open',
      answer_json TEXT, answered_via TEXT, answered_at REAL, created_at REAL NOT NULL, expires_at REAL);
    CREATE INDEX asks_state ON asks(state, created_at);
    CREATE TABLE events(id INTEGER PRIMARY KEY, at REAL, type TEXT, address TEXT, agent_id TEXT, data_json TEXT);
    CREATE INDEX events_at ON events(at);
    CREATE TABLE counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
    INSERT INTO addresses(address, name, agent_id, kind, wake_policy, created_at)
      VALUES ('me', 'me', '', 'human', 'never', 0)
    """,
    # 3: wake jobs (M7, design §8.7). At most one active job per address (the
    # partial unique index): two processes must never resume one session.
    # Beyond §8.4: ``hop`` (§8.8), ``permission`` (decision D2), ``acked_at``,
    # ``reason`` (why a job ended) and ``notified`` (one nudge per job).
    """
    CREATE TABLE wake_jobs(
      id TEXT PRIMARY KEY, address TEXT NOT NULL, agent_id TEXT NOT NULL, adapter TEXT NOT NULL,
      state TEXT NOT NULL, message_ids TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL,
      dispatched_at REAL, started_at REAL, finished_at REAL, exit_code INTEGER, pid INTEGER,
      last_error TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL,
      hop INTEGER NOT NULL DEFAULT 0, permission TEXT, acked_at REAL, reason TEXT,
      notified INTEGER NOT NULL DEFAULT 0);
    CREATE UNIQUE INDEX wake_one_active ON wake_jobs(address)
      WHERE state IN ('queued','waiting_device','probing','dispatched','running');
    CREATE INDEX wake_jobs_state ON wake_jobs(state, next_at);
    CREATE INDEX wake_jobs_address ON wake_jobs(address, created_at)
    """,
    # 4: invites and per-device credentials (M16, decision D3, design §11.5).
    # Only sha256 digests of invite secrets and device tokens are stored.
    # ``devices`` learns the agent version and when it was revoked.
    """
    CREATE TABLE invites(
      id TEXT PRIMARY KEY, secret_sha256 TEXT NOT NULL, label TEXT, agent_id TEXT,
      created_at REAL NOT NULL, expires_at REAL NOT NULL,
      used_at REAL, used_by TEXT, revoked_at REAL);
    CREATE INDEX invites_created ON invites(created_at);
    CREATE TABLE device_tokens(
      id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, token_sha256 TEXT NOT NULL UNIQUE,
      invite_id TEXT, created_at REAL NOT NULL, revoked_at REAL, last_used_at REAL);
    CREATE INDEX device_tokens_agent ON device_tokens(agent_id, revoked_at);
    ALTER TABLE devices ADD COLUMN version TEXT;
    ALTER TABLE devices ADD COLUMN revoked_at REAL
    """,
    # 5: the connector registry (M10, decision D10, design §7.1). Definitions
    # only: secret values and OAuth tokens live in <state_dir>/connectors/.
    # ``cache_json`` keeps the last tool list so hub/<name> is searchable
    # right after a restart, before the upstream session is open again.
    """
    CREATE TABLE connectors(
      name TEXT PRIMARY KEY, config_json TEXT NOT NULL, cache_json TEXT,
      created_at REAL NOT NULL, updated_at REAL NOT NULL)
    """,
    # 6: the plugin repository (插件库, design §6.12; revision of decision D4).
    # The index only: the zips live in <plugin_repo.dir>/<name>/<version>/.
    """
    CREATE TABLE plugin_packages(
      name TEXT NOT NULL, version TEXT NOT NULL, title TEXT, description TEXT,
      sha256 TEXT NOT NULL, size INTEGER NOT NULL, files INTEGER NOT NULL,
      uploaded_at REAL NOT NULL, uploaded_by TEXT, info_json TEXT,
      PRIMARY KEY(name, version))
    """,
    # 7: automatic session names (design §8.2). ``name_source`` says how the
    # name was obtained: 'explicit' (session_name) or 'auto' (derived from the
    # client and the workdir); rows from before stay NULL (anon-… rows keep
    # their legacy behaviour, any other NULL row counts as explicit).
    # ``title`` is the human-readable session title a hook reports (§8.3).
    """
    ALTER TABLE addresses ADD COLUMN name_source TEXT;
    ALTER TABLE addresses ADD COLUMN title TEXT;
    CREATE INDEX addresses_auto ON addresses(agent_id, name_source, oauth_client_id)
    """,
    # 8: a handle another conversation's bind took an automatic address away from
    # (design §8.2 "takeover"). ``superseded_by`` alone cannot tell that from the
    # same conversation binding again (use_device, then set_workspace): only
    # ``displaced`` handles lose their identity; the rest still belong to the
    # address's current holder. Before this migration (schema 7) a takeover was
    # recorded only as ``superseded_by``: every superseded handle on an automatic
    # address other than its current holder is marked displaced, so a takeover's
    # victim does not get the taker's mail back with the upgrade (a schema-7
    # conversation's own older handles lose their identity too: it binds again).
    """
    ALTER TABLE handles ADD COLUMN displaced INTEGER NOT NULL DEFAULT 0;
    UPDATE handles SET displaced = 1 WHERE superseded_by IS NOT NULL
      AND address IN (SELECT address FROM addresses WHERE name_source = 'auto')
      AND handle IS NOT (SELECT last_handle FROM addresses a WHERE a.address = handles.address)
    """,
    # 9: a device's position on the 设备 page's map, set by hand (geoip.py guesses it from the
    # device's egress address otherwise). JSON {"lat", "lon", "label"}; NULL = automatic.
    """
    ALTER TABLE devices ADD COLUMN location_json TEXT
    """,
]


def clean_alias(value: Any) -> str | None:
    """A display alias, or ``None`` to clear it. Raises ``ValueError``."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("alias must be a string or null")
    value = " ".join(value.split())
    if not value:
        return None
    if len(value) > MAX_ALIAS_CHARS or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"alias must be at most {MAX_ALIAS_CHARS} printable characters")
    return value


def clean_hostname(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 255 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    return value


class GatewayStore:
    def __init__(self, path: Path | str | None, *, clock: Callable[[], float] = time.time):
        self.path = None if path in (None, ":memory:") else Path(path)
        self.clock = clock
        self._db: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self.persistent = self.path is not None

    # -- connection ---------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        if self._db is not None:
            return self._db
        db = None
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA busy_timeout=5000")
            except (OSError, sqlite3.Error) as exc:
                log.warning("gateway state database %s unavailable (%s); handles will not survive a restart",
                            self.path, type(exc).__name__)
                db = None
                self.persistent = False
        if db is None:
            db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        db.row_factory = sqlite3.Row
        self._migrate(db)
        self._db = db
        return db

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL)")
        row = db.execute("SELECT version FROM schema_version").fetchone()
        version = row[0] if row else 0
        if version > len(MIGRATIONS):
            raise RuntimeError(f"gateway.sqlite3 schema {version} is newer than this gateway ({len(MIGRATIONS)})")
        for number in range(version, len(MIGRATIONS)):
            db.execute("BEGIN IMMEDIATE")
            try:
                for statement in MIGRATIONS[number].split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("DELETE FROM schema_version")
                db.execute("INSERT INTO schema_version(version) VALUES (?)", (number + 1,))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def schema_version(self) -> int:
        with self._lock:
            return self._connect().execute("SELECT version FROM schema_version").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    # -- handles ------------------------------------------------------------
    def put_handle(self, handle: str, agent_id: str, workspace: str, *, client_id: str | None = None,
                   meta: dict | None = None, ttl: float = HANDLE_TTL_S) -> None:
        now = self.clock()
        with self._lock:
            self._connect().execute(
                "INSERT INTO handles(handle, agent_id, workspace, client_id, created_at, expires_at, meta) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (handle, agent_id, workspace, client_id, now, now + ttl,
                 json.dumps(meta, sort_keys=True) if meta else None))

    def touch_handle(self, handle: str, *, ttl: float = HANDLE_TTL_S) -> dict | None:
        """The live handle, with its sliding expiry extended; ``None`` if unknown or expired."""
        now = self.clock()
        with self._lock:
            db = self._connect()
            row = db.execute("SELECT * FROM handles WHERE handle = ?", (handle,)).fetchone()
            if row is None:
                return None
            if row["expires_at"] < now:
                db.execute("DELETE FROM handles WHERE handle = ?", (handle,))
                return None
            if row["expires_at"] < now + ttl - HANDLE_TOUCH_SLACK_S:
                db.execute("UPDATE handles SET expires_at = ? WHERE handle = ?", (now + ttl, handle))
            return {"handle": row["handle"], "agent_id": row["agent_id"], "workspace": row["workspace"],
                    "client_id": row["client_id"], "meta": json.loads(row["meta"]) if row["meta"] else {}}

    def drop_handle(self, handle: str) -> None:
        with self._lock:
            self._connect().execute("DELETE FROM handles WHERE handle = ?", (handle,))

    def live_handle_count(self) -> int:
        now = self.clock()
        with self._lock:
            db = self._connect()
            db.execute("DELETE FROM handles WHERE expires_at < ?", (now,))
            return db.execute("SELECT COUNT(*) FROM handles").fetchone()[0]

    # -- devices ------------------------------------------------------------
    def device_connected(self, agent_id: str, *, hostname: str | None, features: list[str],
                         device: dict | None, enrolled_via: str = "legacy_env", version: str | None = None) -> None:
        now = self.clock()
        with self._lock:
            self._connect().execute(
                "INSERT INTO devices(agent_id, features, device_json, hostname, enrolled_via, online, "
                "last_connected_at, last_seen_at, version) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?) "
                "ON CONFLICT(agent_id) DO UPDATE SET features = excluded.features, "
                "device_json = excluded.device_json, hostname = COALESCE(excluded.hostname, devices.hostname), "
                "enrolled_via = COALESCE(devices.enrolled_via, excluded.enrolled_via), online = 1, "
                "last_connected_at = excluded.last_connected_at, last_seen_at = excluded.last_seen_at, "
                "version = COALESCE(excluded.version, devices.version)",
                (agent_id, json.dumps(sorted(features)), json.dumps(device, sort_keys=True) if device else None,
                 hostname, enrolled_via, now, now, version))

    def device_catalog(self, agent_id: str, *, hostname: str | None, device: dict | None) -> None:
        with self._lock:
            self._connect().execute(
                "UPDATE devices SET device_json = ?, hostname = COALESCE(?, hostname), last_seen_at = ? "
                "WHERE agent_id = ?",
                (json.dumps(device, sort_keys=True) if device else None, hostname, self.clock(), agent_id))

    def device_disconnected(self, agent_id: str) -> None:
        with self._lock:
            self._connect().execute("UPDATE devices SET online = 0, last_seen_at = ? WHERE agent_id = ?",
                                    (self.clock(), agent_id))

    def set_alias(self, agent_id: str, alias: Any) -> str | None:
        alias = clean_alias(alias)
        with self._lock:
            updated = self._connect().execute("UPDATE devices SET alias = ? WHERE agent_id = ?",
                                              (alias, agent_id)).rowcount
        if not updated:
            raise KeyError(agent_id)
        return alias

    def set_location(self, agent_id: str, location: dict | None) -> dict | None:
        """A manual map position, or None to go back to the automatic one."""
        if location is not None:
            try:
                lat, lon = float(location["lat"]), float(location["lon"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("location needs numeric lat and lon") from None
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("lat/lon out of range")
            label = clean_alias(location.get("label")) if location.get("label") else None
            location = {"lat": round(lat, 4), "lon": round(lon, 4), "label": label}
        with self._lock:
            updated = self._connect().execute(
                "UPDATE devices SET location_json = ? WHERE agent_id = ?",
                (json.dumps(location, ensure_ascii=False) if location else None, agent_id)).rowcount
        if not updated:
            raise KeyError(agent_id)
        return location

    def device(self, agent_id: str) -> dict | None:
        with self._lock:
            row = self._connect().execute("SELECT * FROM devices WHERE agent_id = ?", (agent_id,)).fetchone()
        return self._device_row(row) if row else None

    def devices(self) -> list[dict]:
        with self._lock:
            rows = self._connect().execute("SELECT * FROM devices ORDER BY agent_id").fetchall()
        return [self._device_row(row) for row in rows]

    def aliases(self) -> dict[str, str]:
        with self._lock:
            rows = self._connect().execute("SELECT agent_id, alias FROM devices WHERE alias IS NOT NULL").fetchall()
        return {row["agent_id"]: row["alias"] for row in rows}

    @staticmethod
    def _device_row(row: sqlite3.Row) -> dict:
        return {"agent_id": row["agent_id"], "hostname": row["hostname"], "alias": row["alias"],
                "features": json.loads(row["features"]) if row["features"] else [],
                "device": json.loads(row["device_json"]) if row["device_json"] else None,
                "enrolled_via": row["enrolled_via"], "online": bool(row["online"]),
                "last_connected_at": row["last_connected_at"], "last_seen_at": row["last_seen_at"],
                "version": row["version"], "revoked_at": row["revoked_at"],
                "location": json.loads(row["location_json"]) if row["location_json"] else None}

    # -- invites and device tokens (M16, design §11.5) ------------------------
    def invite_insert(self, invite_id: str, secret_sha256: str, *, label: str | None, agent_id: str | None,
                      expires_at: float) -> dict:
        with self._lock:
            self._connect().execute(
                "INSERT INTO invites(id, secret_sha256, label, agent_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (invite_id, secret_sha256, label, agent_id, self.clock(), expires_at))
        return self.invite(invite_id)

    def invite(self, invite_id: str) -> dict | None:
        with self._lock:
            row = self._connect().execute("SELECT * FROM invites WHERE id = ?", (invite_id,)).fetchone()
        return self._invite_row(row) if row else None

    def invite_secret(self, invite_id: str) -> str | None:
        with self._lock:
            row = self._connect().execute("SELECT secret_sha256 FROM invites WHERE id = ?", (invite_id,)).fetchone()
        return row[0] if row else None

    def invites(self, state: str | None = None) -> list[dict]:
        with self._lock:
            rows = self._connect().execute("SELECT * FROM invites ORDER BY created_at DESC").fetchall()
        items = [self._invite_row(row) for row in rows]
        return [item for item in items if state in (None, "all") or item["state"] == state]

    def invite_revoke(self, invite_id: str) -> bool:
        with self._lock:
            return self._connect().execute(
                "UPDATE invites SET revoked_at = ? WHERE id = ? AND used_at IS NULL AND revoked_at IS NULL",
                (self.clock(), invite_id)).rowcount == 1

    def _invite_row(self, row: sqlite3.Row) -> dict:
        if row["revoked_at"]:
            state = "revoked"
        elif row["used_at"]:
            state = "used"
        elif row["expires_at"] <= self.clock():
            state = "expired"
        else:
            state = "unused"
        return {"id": row["id"], "label": row["label"], "agent_id": row["agent_id"], "created_at": row["created_at"],
                "expires_at": row["expires_at"], "state": state, "used_at": row["used_at"], "used_by": row["used_by"],
                "revoked_at": row["revoked_at"]}

    def enroll_device(self, *, invite_id: str, agent_id: str, token_id: str, token_sha256: str,
                      hostname: str | None, device: dict | None) -> bool:
        """Use the invite and issue the device credential in one transaction (design §11.5 steps 2-4).

        ``False`` when the invite was used, revoked or expired meanwhile: exactly
        one concurrent caller wins.
        """
        now = self.clock()
        with self._lock:
            db = self._connect()
            db.execute("BEGIN IMMEDIATE")
            try:
                used = db.execute(
                    "UPDATE invites SET used_at = ?, used_by = ? WHERE id = ? AND used_at IS NULL "
                    "AND revoked_at IS NULL AND expires_at > ?", (now, agent_id, invite_id, now)).rowcount == 1
                if not used:
                    db.execute("ROLLBACK")
                    return False
                db.execute("INSERT INTO device_tokens(id, agent_id, token_sha256, invite_id, created_at) "
                           "VALUES (?, ?, ?, ?, ?)", (token_id, agent_id, token_sha256, invite_id, now))
                db.execute(
                    "INSERT INTO devices(agent_id, features, device_json, hostname, enrolled_via, online, last_seen_at) "
                    "VALUES (?, '[]', ?, ?, 'invite', 0, ?) ON CONFLICT(agent_id) DO UPDATE SET "
                    "hostname = COALESCE(excluded.hostname, devices.hostname), enrolled_via = 'invite', "
                    "revoked_at = NULL, device_json = COALESCE(excluded.device_json, devices.device_json)",
                    (agent_id, json.dumps(device, sort_keys=True) if device else None, hostname, now))
                db.execute("COMMIT")
                return True
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def device_token_hashes(self, agent_id: str) -> tuple[list[str], bool]:
        """(active token digests, whether a revoked token exists) for ``agent_id``."""
        with self._lock:
            rows = self._connect().execute("SELECT token_sha256, revoked_at FROM device_tokens WHERE agent_id = ?",
                                           (agent_id,)).fetchall()
        return [row[0] for row in rows if row[1] is None], any(row[1] is not None for row in rows)

    def device_token_match(self, agent_id: str, token_sha256: str) -> str | None:
        """``"ok"`` for an active credential of ``agent_id``, ``"revoked"`` for a revoked one, else ``None``."""
        with self._lock:
            row = self._connect().execute(
                "SELECT revoked_at FROM device_tokens WHERE agent_id = ? AND token_sha256 = ?",
                (agent_id, token_sha256)).fetchone()
        if row is None:
            return None
        return "ok" if row[0] is None else "revoked"

    def device_token_agents(self, *, active: bool = False) -> set[str]:
        """Every agent_id that holds (``active``) or ever held a per-device credential."""
        query = "SELECT DISTINCT agent_id FROM device_tokens" + (" WHERE revoked_at IS NULL" if active else "")
        with self._lock:
            rows = self._connect().execute(query).fetchall()
        return {row[0] for row in rows}

    def device_token_owner(self, token_sha256: str) -> tuple[str, bool] | None:
        """``(agent_id, revoked)`` of a per-device credential, or ``None`` (tokens are unique)."""
        with self._lock:
            row = self._connect().execute("SELECT agent_id, revoked_at FROM device_tokens WHERE token_sha256 = ?",
                                          (token_sha256,)).fetchone()
        return None if row is None else (row[0], row[1] is not None)

    # -- the plugin repository (design §6.12) ---------------------------------
    def package_put(self, item: dict) -> None:
        with self._lock:
            self._connect().execute(
                "INSERT OR REPLACE INTO plugin_packages(name, version, title, description, sha256, size, files,"
                " uploaded_at, uploaded_by, info_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (item["name"], item["version"], item.get("title"), item.get("description"), item["sha256"],
                 int(item["size"]), int(item["files"]), item.get("uploaded_at") or self.clock(), item.get("uploaded_by"),
                 json.dumps(item.get("info") or {}, ensure_ascii=False)))

    def package(self, name: str, version: str) -> dict | None:
        with self._lock:
            row = self._connect().execute("SELECT * FROM plugin_packages WHERE name = ? AND version = ?",
                                          (name, version)).fetchone()
        return self._package_row(row) if row else None

    def packages(self, name: str | None = None) -> list[dict]:
        query, args = "SELECT * FROM plugin_packages", ()
        if name is not None:
            query, args = query + " WHERE name = ?", (name,)
        with self._lock:
            rows = self._connect().execute(query + " ORDER BY name, uploaded_at", args).fetchall()
        return [self._package_row(row) for row in rows]

    def package_delete(self, name: str, version: str) -> bool:
        with self._lock:
            return self._connect().execute("DELETE FROM plugin_packages WHERE name = ? AND version = ?",
                                           (name, version)).rowcount > 0

    @staticmethod
    def _package_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        try:
            item["info"] = json.loads(item.pop("info_json") or "{}")
        except ValueError:
            item["info"] = {}
        return item

    def device_token_used(self, token_sha256: str) -> None:
        with self._lock:
            self._connect().execute("UPDATE device_tokens SET last_used_at = ? WHERE token_sha256 = ?",
                                    (self.clock(), token_sha256))

    def revoke_device(self, agent_id: str) -> int:
        """Revoke every credential of ``agent_id``; returns how many tokens were revoked."""
        now = self.clock()
        with self._lock:
            db = self._connect()
            count = db.execute("UPDATE device_tokens SET revoked_at = ? WHERE agent_id = ? AND revoked_at IS NULL",
                               (now, agent_id)).rowcount
            db.execute("UPDATE devices SET revoked_at = ?, online = 0 WHERE agent_id = ?", (now, agent_id))
            return count

    def delete_device(self, agent_id: str) -> bool:
        """Forget an offline device's row (the console's 删除). Its token digests stay,
        so a revoked client that comes back is still told it was revoked."""
        now = self.clock()
        with self._lock:
            db = self._connect()
            removed = db.execute("DELETE FROM devices WHERE agent_id = ?", (agent_id,)).rowcount > 0
            db.execute("UPDATE addresses SET ended_at = ? WHERE agent_id = ? AND ended_at IS NULL AND kind != 'human'",
                       (now, agent_id))
            return removed

    def end_device_addresses(self, agent_id: str) -> int:
        """Mark the revoked device's mailbox addresses ended (design §11.5)."""
        with self._lock:
            return self._connect().execute(
                "UPDATE addresses SET ended_at = ? WHERE agent_id = ? AND ended_at IS NULL AND kind != 'human'",
                (self.clock(), agent_id)).rowcount
