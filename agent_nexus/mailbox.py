"""The gateway's agent mailbox (design §8, milestone M6; asks: decision D11).

Everything durable lives in ``gateway.sqlite3`` (the tables of migration 2 in
``store.py``); this module is the logic on top of it:

* identity: an address ``<name>@<agent_id>`` bound to each context handle at
  ``use_device`` / ``set_workspace`` (§8.2); clients that pass no
  ``session_name`` get a stable automatic name ``<kind>-<workdir>`` (layer 1),
  which a later ``session_name`` renames together with its mail (layer 3);
* messages with one delivery row per recipient (groups expand at send time),
  inbox / read / status / recall (decision D9);
* the unread notice appended to every tool result (§8.6);
* ``ask_user`` questions to the owner ``me`` with answers delivered back as
  mail (§8.12);
* retention (§8.4) with an injectable clock.

Waking recipients (M7) is ``wake.WakeRouter``: when the gateway attaches one
as ``router``, every delivery to an agent asks it for the action (wake or
notice on the next call). The store is synchronous SQLite behind one lock; the
gateway is single-process and every statement here is small.
"""
from __future__ import annotations

import contextlib
import copy
import json
import logging
import re
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from mcpkit.config import ConfigError

from .federation import RoutingError
from .ids import RESUME_ID_RULE, valid_resume_id
from .store import HANDLE_TTL_S, GatewayStore

log = logging.getLogger(__name__)

ME = "me"
#: Pseudo senders: not addresses, they cannot receive mail.
ANON_SENDER = "anon"
SYSTEM_SENDER = "system"
KINDS = ("claude", "codex", "gpt", "dsh", "gemini", "human", "other")
MESSAGE_KINDS = ("message", "task", "result", "system", "question", "answer")
SEND_KINDS = ("message", "task", "result")
PRIORITIES = ("normal", "urgent")
WAKE_PERMISSIONS = ("bypass", "full-access", "inherit")
WAKE_POLICIES = ("auto", "never")
ANSWER_VIA = ("inbox", "toast", "pwa", "cli")
ASK_KINDS = ("single", "multi", "text")
NOTICE_PREFIX = "[agent-nexus 邮箱]"
#: The address the device plugin's request_user_input asks from (§8.12).
DEVICE_ASK_NAME = "device-ask"

MAX_OPTIONS = 20
MAX_OPTION_CHARS = 200
MAX_QUESTION_CHARS = 4000
MAX_SUBJECT_CHARS = 200
MAX_RECIPIENTS = 200
VACUUM_INTERVAL_S = 7 * 86400
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def _is_device_ask(address: str) -> bool:
    return isinstance(address, str) and address.rsplit("@", 1)[0] == DEVICE_ASK_NAME


# -- automatic names (design §8.2 layer 1) -------------------------------------
#: Client kinds that name an automatic address as they are.
AUTO_KIND_LABELS = ("claude", "codex", "gpt", "dsh", "gemini")
#: Room left for a "-NN" collision suffix inside the 64-character name rule.
AUTO_BASE_CHARS = 61
AUTO_LABEL_CHARS = 20
MAX_AUTO_SUFFIX = 99
MAX_TITLE_CHARS = 80
#: Wake job states that hold the ``wake_one_active`` index (store migration 3).
_ACTIVE_JOB_STATES = ("queued", "waiting_device", "probing", "dispatched", "running")


def reserved_name(name: str) -> bool:
    """Names no session may take: ``me``, legacy ``anon-…`` and the device-ask sender."""
    return name == ME or name.startswith("anon-") or name == DEVICE_ASK_NAME


def norm_workdir(path: Any) -> str | None:
    """A workdir compared across devices: both separators, no trailing slash, case-folded."""
    if not isinstance(path, str) or not path.strip():
        return None
    return path.strip().replace("\\", "/").rstrip("/").casefold()


def workdir_basename(path: Any) -> str:
    """The last folder of a device path; the gateway may run on another OS, so both separators count."""
    if not isinstance(path, str):
        return ""
    parts = [part for part in re.split(r"[\\/]+", path.strip()) if part]
    return parts[-1] if parts else ""


def slug(text: Any, limit: int = 64) -> str:
    """Lower-case ``[a-z0-9._-]``; other runs become one hyphen; starts with a letter or digit."""
    value = re.sub(r"[^a-z0-9._-]+", "-", str(text or "").lower())
    value = re.sub(r"-{2,}", "-", value)
    value = re.sub(r"^[^a-z0-9]+", "", value)[:limit]
    return value.rstrip("-._")


def auto_label(kind: str | None, client_name: str | None) -> str:
    """The first part of an automatic name: the client kind, else a slug of its DCR client_name, else agent."""
    if kind in AUTO_KIND_LABELS:
        return kind
    label = slug(client_name, AUTO_LABEL_CHARS)
    if not label or reserved_name(label) or label == "anon" or not _NAME.fullmatch(label):
        return "agent"
    return label


def auto_base(label: str, workdir: str | None) -> str:
    """``<label>-<workdir slug>`` (or just ``<label>``), short enough for a ``-NN`` suffix."""
    folder = slug(workdir_basename(workdir), AUTO_BASE_CHARS)
    if not folder:
        return label[:AUTO_BASE_CHARS]
    return f"{label}-{folder}"[:AUTO_BASE_CHARS].rstrip("-._")


def clean_title(value: Any) -> str | None:
    """A session title for display: control and format characters dropped, one line, at most 80 characters."""
    if not isinstance(value, str):
        return None
    text = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in value)
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= MAX_TITLE_CHARS else text[:MAX_TITLE_CHARS - 1] + "…"


DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "notice": {"repeat": "every", "max_items": 3, "max_chars": 600, "structured": "auto", "meta": True,
               "fallback": "client_id", "fallback_window_s": 1800},
    "presence": {"idle_after_s": 120, "end_after_days": 30},
    "retention": {"read_days": 90, "events_days": 30, "jobs_days": 14, "warn_db_mb": 1024,
                  "interval_s": 86400},
    "inbox": {"toast": True, "toast_max_buttons": 3, "pwa": True, "ask_expire_days": 30},
    "limits": {"max_body_bytes": 65536, "max_sends_per_minute": None, "max_wakes_per_hour": None,
               "max_thread_hops": None},
    "acl": {"mode": "open", "bind_to_oauth_client": False},
    # Waking recipients (M7, design §8.7, §8.10). ``scan_interval_s``,
    # ``rerun_debounce_s`` and ``probe_timeout_s`` name the fixed numbers of §8.7.
    "wake": {"enabled": True, "debounce_s": 5, "rerun_debounce_s": 10, "expire_s": 86400, "max_attempts": 20,
             "ack_timeout_s": 600, "run_timeout_s": 3600, "probe_live": True, "probe_timeout_s": 10,
             "fallback_exec": False, "permission_default": "bypass",
             "adapters": {"claude": True, "codex": True, "companion": True, "notify": True, "push": False},
             "cooldown_s": None, "scan_interval_s": 2},
    # Unread escalation (decision D9, design §8.11); null turns one level off.
    "escalation": {"enabled": True, "rewake_after_s": 3600, "notify_owner_after_s": 86400,
                   "expire_after_days": 30, "notify_sender_on_expire": True, "scan_interval_s": 60},
}
_CHOICES = {("notice", "repeat"): ("every", "on_change"), ("notice", "structured"): ("auto", "always", "never"),
            ("notice", "fallback"): ("client_id", "none", None),
            ("wake", "permission_default"): WAKE_PERMISSIONS}
#: Numbers that may also be ``null`` (that check or level is then off).
_NULLABLE = {("wake", "cooldown_s"), ("escalation", "rewake_after_s"), ("escalation", "notify_owner_after_s"),
             ("escalation", "expire_after_days")}


def mailbox_config(raw: Any) -> tuple[dict, list[str]]:
    """``gateway.json``'s ``mailbox`` merged over the defaults; type errors raise, unknown keys warn."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("mailbox must be an object")
    config = copy.deepcopy(DEFAULT_CONFIG)
    warnings: list[str] = []
    for key, value in raw.items():
        if key not in config:
            # db, relay, invites: other milestones read them.
            continue
        default = config[key]
        if isinstance(default, dict):
            if not isinstance(value, dict):
                raise ConfigError(f"mailbox.{key} must be an object")
            for sub, item in value.items():
                if key == "retention" and sub == "unread_days":
                    # Deprecated by decision D9: unread mail now expires through escalation level 3.
                    if item is not None:
                        if isinstance(item, bool) or not isinstance(item, (int, float)) or item < 0:
                            raise ConfigError("mailbox.retention.unread_days must be a nonnegative number or null")
                        if "expire_after_days" not in (raw.get("escalation") or {}):
                            config["escalation"]["expire_after_days"] = item
                        warnings.append("mailbox.retention.unread_days is deprecated; "
                                        "use mailbox.escalation.expire_after_days")
                    continue
                if sub not in default:
                    warnings.append(f"unknown configuration key 'mailbox.{key}.{sub}' (ignored)")
                    continue
                expected = default[sub]
                choices = _CHOICES.get((key, sub))
                if isinstance(expected, dict):
                    if not isinstance(item, dict):
                        raise ConfigError(f"mailbox.{key}.{sub} must be an object")
                    for name, flag in item.items():
                        if name not in expected:
                            warnings.append(f"unknown configuration key 'mailbox.{key}.{sub}.{name}' (ignored)")
                        elif not isinstance(flag, bool):
                            raise ConfigError(f"mailbox.{key}.{sub}.{name} must be true or false")
                        else:
                            expected[name] = flag
                    continue
                if (key, sub) in _NULLABLE and item is None:
                    default[sub] = None
                    continue
                if choices is not None:
                    if item not in choices:
                        raise ConfigError(f"mailbox.{key}.{sub} must be one of {', '.join(str(c) for c in choices)}")
                elif isinstance(expected, bool):
                    if not isinstance(item, bool):
                        raise ConfigError(f"mailbox.{key}.{sub} must be true or false")
                elif isinstance(expected, (int, float)) or expected is None:
                    if item is not None and (isinstance(item, bool) or not isinstance(item, (int, float)) or item < 0):
                        raise ConfigError(f"mailbox.{key}.{sub} must be a nonnegative number or null")
                    if item is None and expected is not None:
                        raise ConfigError(f"mailbox.{key}.{sub} must be a number")
                elif isinstance(expected, str) and not isinstance(item, str):
                    raise ConfigError(f"mailbox.{key}.{sub} must be a string")
                default[sub] = item
        else:
            if not isinstance(value, type(default)):
                raise ConfigError(f"mailbox.{key} must be {type(default).__name__}")
            config[key] = value
    return config, warnings


# -- ids ---------------------------------------------------------------------
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ulid_lock = threading.Lock()
_ulid_last = [0, 0]


def ulid(now: float) -> str:
    """A monotonic ULID: 48-bit milliseconds + 80 random bits, Crockford base32."""
    with _ulid_lock:
        ms = int(now * 1000)
        if ms <= _ulid_last[0]:
            ms, rand = _ulid_last[0], _ulid_last[1] + 1
        else:
            rand = secrets.randbits(80)
        _ulid_last[:] = [ms, rand]
    value = (ms << 80) | (rand & ((1 << 80) - 1))
    return "".join(_CROCKFORD[(value >> (5 * index)) & 31] for index in range(25, -1, -1))


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


def _clip(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def ordered_options(options: list[str], recommended: str | None) -> list[str]:
    """Question-card order (decision D11): the recommended option first, then the rest in order."""
    if recommended and recommended in options:
        return [recommended, *[item for item in options if item != recommended]]
    return list(options)


@dataclass
class Notice:
    text: str
    meta: dict


class Mailbox:
    def __init__(self, store: GatewayStore, config: dict | None = None, *,
                 display_device: Callable[[str], str] | None = None,
                 resolve_device: Callable[[str], str] | None = None,
                 online: Callable[[str], bool] | None = None):
        self.store = store
        self.config = config or copy.deepcopy(DEFAULT_CONFIG)
        self.display_device = display_device or (lambda agent_id: agent_id)
        self.resolve_device = resolve_device or (lambda value: value)
        self.online = online or (lambda agent_id: False)
        self._last_notice: dict[str, frozenset] = {}
        #: Handles already told they may choose a session_name (layer 3 nudge, once each).
        self._nudged: set[str] = set()
        #: Handles with a blocking call in flight (mail_wait, blocking ask_user) -> how many.
        self._inflight: dict[str, int] = {}
        #: The wake router (M7, ``wake.WakeRouter``); ``None``: every delivery is noticed on the next call.
        self.router = None

    # -- plumbing -------------------------------------------------------------
    @property
    def clock(self) -> Callable[[], float]:
        return self.store.clock

    def _db(self):
        return self.store._connect()

    def _cfg(self, section: str, key: str) -> Any:
        return self.config[section][key]

    def event(self, kind: str, address: str | None = None, agent_id: str | None = None, **data: Any) -> None:
        with self.store._lock:
            self._db().execute("INSERT INTO events(at, type, address, agent_id, data_json) VALUES (?, ?, ?, ?, ?)",
                               (self.clock(), kind, address, agent_id, json.dumps(data) if data else None))

    def bump(self, counter: str, amount: int = 1) -> None:
        with self.store._lock:
            self._db().execute("INSERT INTO counters(name, value) VALUES (?, ?) "
                               "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value", (counter, amount))

    def counters(self) -> dict[str, int]:
        with self.store._lock:
            rows = self._db().execute("SELECT name, value FROM counters").fetchall()
        result = {"notice_missed": 0, "notice_fallback_used": 0}
        result.update({row["name"]: row["value"] for row in rows})
        return result

    # -- addresses -------------------------------------------------------------
    def address_row(self, address: str) -> dict | None:
        with self.store._lock:
            row = self._db().execute("SELECT * FROM addresses WHERE address = ?", (address,)).fetchone()
        return dict(row) if row else None

    def display(self, address: str) -> str:
        """``<name>@<display_name>`` (decision D6); pseudo senders and ``me`` as they are."""
        if not isinstance(address, str) or "@" not in address:
            return address
        name, agent_id = address.rsplit("@", 1)
        return f"{name}@{self.display_device(agent_id)}"

    def bind(self, handle: str, agent_id: str, *, workdir: str | None, meta: dict | None,
             client_id: str | None, kind_hint: str | None = None, client_name: str | None = None,
             previous: str | None = None, legacy_anon: bool = False, exclusive: bool = False) -> dict:
        """Bind a new context handle to its address (design §8.2).

        ``exclusive``: a new conversation that must never take over an automatic address another live
        handle holds, however idle (console agent runs, docs/agent-chat.md §10: two open chats in one
        folder would otherwise share one mailbox).

        Without ``session_name`` the address gets an automatic, stable name
        (layer 1); ``legacy_anon`` keeps the old ``anon-<handle>`` address
        (handles made before the mailbox). ``previous`` is the caller's earlier
        handle, when the gateway knows it: an automatic address it held is
        reused first, and renamed (with its mail) when ``session_name`` is
        given now (layer 3).
        """
        meta = meta or {}
        now = self.clock()
        name = meta.get("session_name")
        explicit_kind = meta.get("kind")
        renamed_from = None
        with self.store._lock:
            db = self._db()
            if name is None and not legacy_anon:
                named = self._previous_named(db, previous, agent_id, workdir, explicit_kind)
                if named is not None:
                    # The conversation chose its name (or had one from before migration 7): binding
                    # again with its workspace_id but without session_name keeps that address.
                    name = named["name"]
            if name is None and legacy_anon:
                address = f"anon-{handle[:6]}@{agent_id}"
                db.execute(
                    "INSERT INTO addresses(address, name, agent_id, kind, workdir, wake_policy, oauth_client_id, "
                    "last_seen_at, last_handle, created_at) VALUES (?, ?, ?, ?, ?, 'never', ?, ?, ?, ?) "
                    "ON CONFLICT(address) DO UPDATE SET last_seen_at = excluded.last_seen_at, "
                    "last_handle = excluded.last_handle, ended_at = NULL",
                    (address, address.split("@")[0], agent_id, explicit_kind or kind_hint or "other", workdir,
                     client_id, now, handle, now))
            elif name is None:
                legacy = self._previous_auto(db, previous, agent_id, legacy=True)
                with self._transaction(db):
                    address = self._bind_auto(db, handle, agent_id, workdir=workdir, meta=meta,
                                              client_id=client_id, kind_hint=kind_hint, client_name=client_name,
                                              previous=previous, now=now, exclusive=exclusive)
                    if legacy is not None and self._legacy_anon(legacy) and legacy["address"] != address:
                        # A conversation from before migration 7 passes its anon-... handle: its mail
                        # follows into the automatic address instead of staying behind.
                        target = db.execute("SELECT kind FROM addresses WHERE address = ?", (address,)).fetchone()
                        self._rename(db, legacy, address, address.rsplit("@", 1)[0], target["kind"], now)
                        renamed_from = legacy["address"]
            else:
                address, renamed_from = self._bind_named(db, handle, agent_id, name, workdir=workdir, meta=meta,
                                                         client_id=client_id, kind_hint=kind_hint,
                                                         previous=previous, now=now)
            db.execute("UPDATE handles SET address = ? WHERE handle = ?", (address, handle))
            source = db.execute("SELECT name_source FROM addresses WHERE address = ?", (address,)).fetchone()
        result = {"address": address, "address_display": self.display(address), "unread": self.unread_count(address),
                  "name_source": (source["name_source"] if source else None) or ("anon" if legacy_anon else "explicit")}
        if renamed_from:
            self._last_notice.pop(renamed_from, None)
            result["renamed_from"] = renamed_from
            result["renamed_from_display"] = self.display(renamed_from)
        return result

    def _bind_named(self, db, handle: str, agent_id: str, name: str, *, workdir: str | None, meta: dict,
                    client_id: str | None, kind_hint: str | None, previous: str | None,
                    now: float) -> tuple[str, str | None]:
        """``session_name`` given (or kept): create, re-attach, claim, or rename the caller's automatic address.

        Every check runs before the first write, and the writes are one transaction: a refused
        bind (``name_taken``) changes nothing, in particular it displaces nobody.
        """
        if reserved_name(name):
            raise RoutingError(f"name_taken: session_name {name!r} is reserved; choose another one")
        explicit_kind = meta.get("kind")
        address = f"{name}@{agent_id}"
        held = self._previous_auto(db, previous, agent_id, legacy=True)
        existing = db.execute("SELECT * FROM addresses WHERE address = ?", (address,)).fetchone()
        own = held is not None and held["address"] == address
        claim = existing is not None and existing["name_source"] == "auto" and not own
        if claim and (self._lost_address(db, previous) == address
                      or not self._may_claim_auto(db, existing, handle, client_id, workdir, now)):
            # An automatic name is predictable: another client's (or a live conversation's)
            # address is never taken over by typing it as session_name, and a conversation that
            # lost it to a takeover does not take it back (the mail there is the taker's now).
            suggestion = self._free_name(db, name, existing["kind"], agent_id)
            raise RoutingError(f"name_taken: {self.display(address)} is another session's automatic "
                               f"address; use another session_name such as {suggestion!r}")
        if held is not None and not own:
            self._rename_conflict(db, held, address, name, explicit_kind)
        elif existing is not None and explicit_kind and existing["kind"] != explicit_kind:
            suggestion = self._free_name(db, name, explicit_kind, agent_id)
            raise RoutingError(f"name_taken: {self.display(address)} is a {existing['kind']} session; "
                               f"use another session_name such as {suggestion!r}")
        renamed_from = None
        with self._transaction(db):
            if claim:
                # Claimed from another conversation (same client and folder, holder idle): a
                # takeover like layer 1's. Its resume id and title are not this one's (a wake
                # would resume the wrong conversation), and its handles lose the identity.
                db.execute("UPDATE addresses SET resume_id = NULL, resume_source = NULL, resume_confidence = NULL, "
                           "title = NULL WHERE address = ?", (address,))
                self._displace(db, address, keep=handle)
            if held is not None and not own:
                self._rename(db, held, address, name, explicit_kind, now)
                renamed_from = held["address"]
            row = db.execute("SELECT * FROM addresses WHERE address = ?", (address,)).fetchone()
            kind = explicit_kind or (row["kind"] if row else None) or kind_hint or "other"
            if row is not None and explicit_kind and row["kind"] != explicit_kind:
                raise RoutingError(f"name_taken: {self.display(address)} is a {row['kind']} session")
            wake_policy = meta.get("wake", "auto")
            if row is None:
                db.execute(
                    "INSERT INTO addresses(address, name, agent_id, kind, workdir, resume_id, resume_source, "
                    "companion_route, wake_policy, oauth_client_id, last_seen_at, last_handle, created_at, "
                    "name_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'explicit')",
                    (address, name, agent_id, kind, workdir, meta.get("resume_id"),
                     "explicit" if meta.get("resume_id") else None, meta.get("companion_route"),
                     wake_policy, client_id, now, handle, now))
            else:
                db.execute(
                    "UPDATE addresses SET kind = ?, workdir = COALESCE(?, workdir), "
                    "resume_id = COALESCE(?, resume_id), "
                    "resume_source = CASE WHEN ? IS NOT NULL THEN 'explicit' ELSE resume_source END, "
                    "companion_route = COALESCE(?, companion_route), wake_policy = ?, "
                    "oauth_client_id = COALESCE(?, oauth_client_id), last_seen_at = ?, last_handle = ?, "
                    "ended_at = NULL, name_source = 'explicit' WHERE address = ?",
                    (kind, workdir, meta.get("resume_id"), meta.get("resume_id"), meta.get("companion_route"),
                     wake_policy, client_id, now, handle, address))
                # Re-attach (§8.2): the older handles now point at this one; mail is kept.
                db.execute("UPDATE handles SET superseded_by = ? WHERE address = ? AND handle != ? "
                           "AND superseded_by IS NULL", (handle, address, handle))
        return address, renamed_from

    @staticmethod
    @contextlib.contextmanager
    def _transaction(db):
        """One atomic unit: a transaction, or a savepoint inside the one already open."""
        if db.in_transaction:
            point = f"anx_{secrets.token_hex(4)}"
            db.execute(f"SAVEPOINT {point}")
            try:
                yield
            except BaseException:
                db.execute(f"ROLLBACK TO {point}")
                db.execute(f"RELEASE {point}")
                raise
            db.execute(f"RELEASE {point}")
            return
        db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

    @contextlib.contextmanager
    def in_use(self, handle: str | None):
        """A blocking call on ``handle`` (mail_wait, a blocking ask_user): its address counts as in use.

        Nothing touches the handle while the call waits, so without this the holder would look
        idle after ``idle_after_s`` and another conversation could take the address (and its
        mail) over while the wait is still pending.
        """
        if not handle:
            yield
            return
        with self.store._lock:
            self._inflight[handle] = self._inflight.get(handle, 0) + 1
        try:
            yield
        finally:
            with self.store._lock:
                left = self._inflight.get(handle, 0) - 1
                if left > 0:
                    self._inflight[handle] = left
                else:
                    self._inflight.pop(handle, None)

    def _waiting_on(self, db, address: str, *, exclude: tuple = ()) -> bool:
        """Whether a non-displaced handle of ``address`` has a blocking call in flight."""
        if not self._inflight:
            return False
        rows = db.execute("SELECT handle FROM handles WHERE address = ? AND displaced = 0", (address,)).fetchall()
        return any(row["handle"] in self._inflight and row["handle"] not in exclude for row in rows)

    # -- layer 1: automatic names ----------------------------------------------------
    def _holder_active(self, db, row, handle: str, previous: str | None, now: float) -> bool:
        """Whether another session still uses this automatic address (its handle seen within idle_after_s).

        Activity is the holder *handle's* last use (its sliding expiry), not the
        address's ``last_seen_at``: a hook reporting a new prompt in the folder
        bumps the address, and must not push the reconnecting session to ``-2``.
        """
        if row["ended_at"]:
            return False
        holder = row["last_handle"]
        if not holder or holder in (handle, previous):
            return False
        if self.router is not None and self.router.busy(row["address"]):
            return True
        if self._waiting_on(db, row["address"], exclude=(handle,)):
            return True  # blocked in mail_wait / ask_user: in use, however long ago the call began
        record = db.execute("SELECT expires_at, superseded_by FROM handles WHERE handle = ?", (holder,)).fetchone()
        if record is None or record["superseded_by"] or record["expires_at"] < now:
            return False
        last_used = record["expires_at"] - HANDLE_TTL_S
        return now - last_used < float(self._cfg("presence", "idle_after_s"))

    def _may_claim_auto(self, db, row, handle: str, client_id: str | None, workdir: str | None,
                        now: float) -> bool:
        """An explicit ``session_name`` equal to an automatic address that is not the caller's own.

        Allowed only where layer 1 itself would hand the address over: the same OAuth client and
        folder, and no other session still using it.
        """
        return (row["oauth_client_id"] == client_id and norm_workdir(row["workdir"]) == norm_workdir(workdir)
                and not self._holder_active(db, row, handle, None, now))

    @staticmethod
    def _has_live_handle(db, address: str, now: float) -> bool:
        return db.execute("SELECT 1 FROM handles WHERE address = ? AND superseded_by IS NULL AND expires_at >= ? "
                          "LIMIT 1", (address, now)).fetchone() is not None

    @staticmethod
    def _same_auto_key(row, kind: str, client_id: str | None, workdir: str | None) -> bool:
        return (row["name_source"] == "auto" and row["kind"] == kind and row["oauth_client_id"] == client_id
                and norm_workdir(row["workdir"]) == norm_workdir(workdir))

    def _bind_auto(self, db, handle: str, agent_id: str, *, workdir: str | None, meta: dict, client_id: str | None,
                   kind_hint: str | None, client_name: str | None, previous: str | None, now: float,
                   exclusive: bool = False) -> str:
        """Layer 1: ``<kind>-<workdir>`` for (device, OAuth client, workdir); ``-2``… while another holds it."""
        kind = meta.get("kind") or kind_hint or "other"
        base = auto_base(auto_label(kind, client_name), workdir)
        chosen = None
        held = self._previous_auto(db, previous, agent_id)
        # An address the caller's handle was displaced from (another conversation took it over)
        # is never handed back to it, however idle the taker is now: the mail there is the taker's.
        lost = self._lost_address(db, previous)
        # A caller that names an earlier handle of its own is a running conversation, not a
        # restarted one: it may reuse an idle address only once no live handle holds it (the
        # holder expired, ended or rebound elsewhere). Taking an idle holder's address (and its
        # unread mail) over is for a fresh bind only, the restart that layer 1 exists for.
        running = (isinstance(previous, str) and bool(previous)) or exclusive
        previous = held["last_handle"] if held is not None else None  # only the current holder counts
        if held is not None and self._same_auto_key(held, kind, client_id, workdir):
            chosen = held  # the same conversation binding again keeps its address
        if chosen is None:
            candidates = [base, *(f"{base}-{n}" for n in range(2, MAX_AUTO_SUFFIX + 1))]
            for candidate in candidates:
                if reserved_name(candidate) or not _NAME.fullmatch(candidate):
                    continue
                row = db.execute("SELECT * FROM addresses WHERE address = ?", (f"{candidate}@{agent_id}",)).fetchone()
                if row is None:
                    chosen = candidate
                    break
                if row["address"] == lost:
                    continue
                if running and not row["ended_at"] and self._has_live_handle(db, row["address"], now):
                    continue
                # Never an explicit session's address, a legacy row or another client's / folder's name.
                if self._same_auto_key(row, kind, client_id, workdir) \
                        and not self._holder_active(db, row, handle, previous, now):
                    chosen = row
                    break
            if chosen is None:
                chosen = f"{base[:AUTO_BASE_CHARS - 5]}-{secrets.token_hex(2)}"
        wake_policy = meta.get("wake", "auto")
        self._end_dead_siblings(db, agent_id, kind, client_id, workdir,
                                keep=f"{chosen}@{agent_id}" if isinstance(chosen, str) else chosen["address"],
                                now=now)
        if isinstance(chosen, str):
            address = f"{chosen}@{agent_id}"
            self._displace(db, address, keep=handle)  # handles left from a deleted row of that name
            db.execute(
                "INSERT INTO addresses(address, name, agent_id, kind, workdir, resume_id, resume_source, "
                "companion_route, wake_policy, oauth_client_id, last_seen_at, last_handle, created_at, name_source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'auto')",
                (address, chosen, agent_id, kind, workdir, meta.get("resume_id"),
                 "explicit" if meta.get("resume_id") else None, meta.get("companion_route"), wake_policy,
                 client_id, now, handle, now))
            return address
        address = chosen["address"]
        if chosen is not held:
            # Another conversation takes an idle address over: the previous one's resume id and
            # title are not this one's (a wake would resume the wrong conversation). The hook
            # reports this conversation's own on its next prompt.
            db.execute("UPDATE addresses SET resume_id = NULL, resume_source = NULL, resume_confidence = NULL, "
                       "title = NULL WHERE address = ?", (address,))
            # Its handles lose the identity (``displaced``). The same conversation binding again
            # (``chosen is held``) only supersedes its own older handles, which keep it.
            self._displace(db, address, keep=handle)
        db.execute(
            "UPDATE addresses SET workdir = COALESCE(?, workdir), resume_id = COALESCE(?, resume_id), "
            "resume_source = CASE WHEN ? IS NOT NULL THEN 'explicit' ELSE resume_source END, "
            "companion_route = COALESCE(?, companion_route), wake_policy = ?, last_seen_at = ?, last_handle = ?, "
            "ended_at = NULL WHERE address = ?",
            (workdir, meta.get("resume_id"), meta.get("resume_id"), meta.get("companion_route"), wake_policy,
             now, handle, address))
        # A reconnect: the older handles point at the new one; the mail is still there.
        db.execute("UPDATE handles SET superseded_by = ? WHERE address = ? AND handle != ? "
                   "AND superseded_by IS NULL", (handle, address, handle))
        return address

    @staticmethod
    def _displace(db, address: str, *, keep: str) -> None:
        """Another conversation takes ``address``: every handle on it but ``keep`` loses its identity."""
        db.execute("UPDATE handles SET displaced = 1 WHERE address = ? AND handle != ? AND displaced = 0",
                   (address, keep))

    def _end_dead_siblings(self, db, agent_id: str, kind: str, client_id: str | None, workdir: str | None, *,
                           keep: str, now: float) -> None:
        """End the other automatic rows of this (device, client, folder) that no live handle holds any more.

        A quick restart leaves a ``-2`` row behind; once nothing can use it, it must not stay in
        ``@all`` or make hook matching in the folder ambiguous. Its mail is kept (the owner's
        inbox lists it) and a later bind of the same key may take it again.
        """
        rows = db.execute("SELECT * FROM addresses WHERE agent_id = ? AND name_source = 'auto' AND kind = ? "
                          "AND oauth_client_id IS ? AND ended_at IS NULL AND address != ?",
                          (agent_id, kind, client_id, keep)).fetchall()
        for row in rows:
            if norm_workdir(row["workdir"]) != norm_workdir(workdir) or self._has_live_handle(db, row["address"], now):
                continue
            if self.router is not None and self.router.busy(row["address"]):
                continue
            db.execute("UPDATE addresses SET ended_at = ? WHERE address = ?", (now, row["address"]))

    @staticmethod
    def _legacy_anon(row) -> bool:
        """An ``anon-<handle>`` row from before migration 7."""
        return row["name_source"] is None and str(row["name"]).startswith("anon-")

    @staticmethod
    def _held_row(db, previous: str | None, agent_id: str):
        """The address row the caller's earlier handle still belongs to (not displaced), on this device."""
        if not isinstance(previous, str) or not previous:
            return None
        row = db.execute("SELECT a.* FROM handles h JOIN addresses a ON a.address = h.address "
                         "WHERE h.handle = ? AND h.displaced = 0", (previous,)).fetchone()
        if row is None or row["agent_id"] != agent_id:
            return None
        return row

    @classmethod
    def _previous_auto(cls, db, previous: str | None, agent_id: str, *, legacy: bool = False):
        """The automatic address the caller's earlier handle still belongs to, on this device; else ``None``.

        Any handle of the conversation that holds the address counts, also one its own later
        bind superseded (use_device, then set_workspace: the first workspace_id stays valid).
        A ``displaced`` handle (another conversation took the idle address over) does not: it
        may neither reclaim nor rename that address. ``legacy`` also accepts a legacy
        ``anon-…`` row, which is renamed or merged (with its mail) like an automatic one.
        """
        row = cls._held_row(db, previous, agent_id)
        if row is None:
            return None
        if row["name_source"] == "auto" or (legacy and cls._legacy_anon(row)):
            return row
        return None

    @classmethod
    def _previous_named(cls, db, previous: str | None, agent_id: str, workdir: str | None, kind: str | None):
        """The explicit (or legacy explicit) address the caller's earlier handle belongs to, when it fits.

        Binding again without ``session_name`` keeps it (§8.2 "pass the workspace_id you already
        hold to keep your address"): same device, same folder (or none known) and kind.
        """
        row = cls._held_row(db, previous, agent_id)
        if row is None or row["name_source"] == "auto" or cls._legacy_anon(row) or reserved_name(row["name"]):
            return None
        if kind and row["kind"] != kind:
            return None
        if workdir and row["workdir"] and norm_workdir(row["workdir"]) != norm_workdir(workdir):
            return None
        return row

    @staticmethod
    def _lost_address(db, previous: str | None) -> str | None:
        """The address the caller's earlier handle was displaced from (a takeover), if any."""
        if not isinstance(previous, str) or not previous:
            return None
        row = db.execute("SELECT address FROM handles WHERE handle = ? AND displaced = 1", (previous,)).fetchone()
        return row["address"] if row else None

    def displaced(self, handle: Any) -> str | None:
        """The address a handle was bound to when another conversation has taken it over since.

        Such a handle has no mailbox identity any more: reading or sending as that address would
        reach the other conversation's mail. Also a superseded handle whose address row is gone
        (left behind by an older rename). ``None`` for every other handle, including the older
        handles of the conversation that still holds the address (its own rebinds).
        """
        if not isinstance(handle, str) or not handle or len(handle) > 200:
            return None
        with self.store._lock:
            row = self._db().execute(
                "SELECT h.address, h.superseded_by, h.displaced, a.address AS present FROM handles h "
                "LEFT JOIN addresses a ON a.address = h.address WHERE h.handle = ?", (handle,)).fetchone()
        if row is None or not row["address"]:
            return None
        if row["displaced"] or (row["present"] is None and row["superseded_by"]):
            return row["address"]
        return None

    # -- layer 3: renaming an automatic address ---------------------------------------
    def _rename(self, db, held, address: str, name: str, explicit_kind: str | None, now: float) -> None:
        """Move an automatic address and everything that names it to ``address`` (one transaction).

        The target does not exist: the row is renamed. It exists with the same
        kind: the automatic address is merged into it (all deliveries, sent
        mail, asks, handles, wake jobs and history move; the target keeps its
        own settings and only gains a resume id / title it lacked) and the
        automatic row is deleted. Another kind: ``name_taken``, nothing changes.
        """
        old = held["address"]
        kind = explicit_kind or held["kind"]
        self._rename_conflict(db, held, address, name, explicit_kind)
        target = db.execute("SELECT * FROM addresses WHERE address = ?", (address,)).fetchone()
        active = ",".join("?" * len(_ACTIVE_JOB_STATES))
        # Team members and what senders typed name the address in any accepted form (the display
        # form above all): resolve each now, before the rows change, and rewrite what resolved
        # to the old address.
        rewrites = self._references(db, old, name, address)
        with self._transaction(db):
            if target is None:
                db.execute("UPDATE addresses SET address = ?, name = ?, kind = ?, name_source = 'explicit' "
                           "WHERE address = ?", (address, name, kind, old))
            else:
                db.execute(
                    "UPDATE addresses SET resume_id = COALESCE(resume_id, ?), "
                    "resume_source = CASE WHEN resume_id IS NULL THEN ? ELSE resume_source END, "
                    "title = COALESCE(title, ?), workdir = COALESCE(workdir, ?) WHERE address = ?",
                    (held["resume_id"], held["resume_source"], held["title"], held["workdir"], address))
                db.execute("DELETE FROM addresses WHERE address = ?", (old,))
            # One message delivered to both: the target's row stays.
            db.execute("UPDATE OR IGNORE deliveries SET to_addr = ? WHERE to_addr = ?", (address, old))
            db.execute("DELETE FROM deliveries WHERE to_addr = ?", (old,))
            db.execute("UPDATE messages SET from_addr = ? WHERE from_addr = ?", (address, old))
            db.execute("UPDATE asks SET from_addr = ? WHERE from_addr = ?", (address, old))
            # The holder's handles follow, also the ones its own rebinds superseded; a displaced
            # one (a conversation that held the address before) stays behind without an identity.
            db.execute("UPDATE handles SET address = ? WHERE address = ? AND displaced = 0", (address, old))
            db.execute("UPDATE events SET address = ? WHERE address = ?", (address, old))
            # At most one active wake job per address: the target's wins, the moved one ends.
            db.execute(f"UPDATE wake_jobs SET state = 'skipped', reason = 'renamed', finished_at = ? "
                       f"WHERE address = ? AND state IN ({active}) AND EXISTS (SELECT 1 FROM wake_jobs w "
                       f"WHERE w.address = ? AND w.state IN ({active}))",
                       (now, old, *_ACTIVE_JOB_STATES, address, *_ACTIVE_JOB_STATES))
            db.execute("UPDATE wake_jobs SET address = ? WHERE address = ?", (address, old))
            # Team members and what senders typed (informational).
            for sql, value, key in rewrites:
                db.execute(sql, (value, key))
            db.execute("INSERT INTO events(at, type, address, agent_id, data_json) VALUES (?, ?, ?, ?, ?)",
                       (now, "address_renamed", address, held["agent_id"],
                        json.dumps({"from": old, "merged": target is not None})))

    def _rename_conflict(self, db, held, address: str, name: str, explicit_kind: str | None) -> None:
        """``name_taken`` when ``address`` exists as another kind (checked before anything is written)."""
        kind = explicit_kind or held["kind"]
        target = db.execute("SELECT kind FROM addresses WHERE address = ?", (address,)).fetchone()
        if target is not None and target["kind"] != kind:
            suggestion = self._free_name(db, name, kind, held["agent_id"])
            raise RoutingError(f"name_taken: {self.display(address)} is a {target['kind']} session; use another "
                               f"session_name such as {suggestion!r} (your mail stays at {self.display(held['address'])})")

    def _references(self, db, old: str, name: str, address: str) -> list[tuple[str, str, Any]]:
        """UPDATEs for team members and ``messages.to_expr`` items that resolve to ``old``.

        An item with a device part keeps the device spelling it was written with
        (``claude-x@pc-main`` -> ``reviewer@pc-main``); a bare name becomes the full new address,
        so it cannot come to mean a same-named session on another device. Items that resolve
        elsewhere (the same name on another device) are left alone.
        """
        old_name = old.rsplit("@", 1)[0]

        def rewrite(items: Any) -> list | None:
            if not isinstance(items, list):
                return None
            changed, out = False, []
            for item in items:
                if isinstance(item, str) and item.rsplit("@", 1)[0] == old_name \
                        and self._expand_plain(db, [item])[0] == [old]:
                    out.append(f"{name}@{item.rsplit('@', 1)[1]}" if "@" in item else address)
                    changed = True
                else:
                    out.append(item)
            return out if changed else None

        updates = []
        for row in db.execute("SELECT name, members_json FROM groups").fetchall():
            with contextlib.suppress(ValueError, TypeError):
                members = rewrite(json.loads(row["members_json"]))
                if members is not None:
                    updates.append(("UPDATE groups SET members_json = ? WHERE name = ?",
                                    json.dumps(members, ensure_ascii=False), row["name"]))
        for row in db.execute("SELECT id, to_expr FROM messages WHERE instr(to_expr, ?) > 0", (old_name,)).fetchall():
            with contextlib.suppress(ValueError, TypeError):
                items = rewrite(json.loads(row["to_expr"]))
                if items is not None:
                    updates.append(("UPDATE messages SET to_expr = ? WHERE id = ?",
                                    json.dumps(items, ensure_ascii=False), row["id"]))
        return updates

    def nudge_for(self, address: str, handle: str | None) -> str | None:
        """Layer 3: one line, once per handle, telling an automatically named session it may choose a name."""
        if not handle or handle in self._nudged:
            return None
        row = self.address_row(address)
        if row is None or row.get("name_source") != "auto":
            return None
        if len(self._nudged) > 10000:
            self._nudged.clear()
        self._nudged.add(handle)
        return (f"{NOTICE_PREFIX} 你的邮箱地址是自动起的 {self.display(address)}。"
                f"想换个好记的名字，调用 set_workspace 或 use_device 时传 session_name"
                f'（并带上 workspace_id="{handle}"），邮件会一起跟过去。')

    @staticmethod
    def _free_name(db, name: str, kind: str, agent_id: str) -> str:
        for candidate in [f"{name}-{kind}", *(f"{name}-{n}" for n in range(2, 100))]:
            if db.execute("SELECT 1 FROM addresses WHERE address = ?", (f"{candidate}@{agent_id}",)).fetchone() is None:
                return candidate
        return f"{name}-{secrets.token_hex(2)}"

    def address_for_handle(self, handle: Any) -> str | None:
        """The address behind a live handle; ``None`` (never an error) for anything else."""
        if not isinstance(handle, str) or not handle or len(handle) > 200:
            return None
        record = self.store.touch_handle(handle)
        if record is None:
            return None
        if self.displaced(handle) is not None:
            return None  # another conversation holds that automatic address now
        with self.store._lock:
            row = self._db().execute("SELECT address FROM handles WHERE handle = ?", (handle,)).fetchone()
        if row is not None and row["address"]:
            return row["address"]
        # A handle created before the mailbox existed: give it an anonymous address now (legacy behaviour).
        return self.bind(handle, record["agent_id"], workdir=None, meta=None,
                         client_id=record.get("client_id"), legacy_anon=True)["address"]

    def fallback_address(self, client_id: str | None) -> str | None:
        """Identity fallback (§8.6): the one address this OAuth client used recently, if exactly one."""
        if not client_id or self._cfg("notice", "fallback") != "client_id":
            return None
        since = self.clock() - float(self._cfg("notice", "fallback_window_s"))
        with self.store._lock:
            rows = self._db().execute(
                "SELECT address FROM addresses WHERE oauth_client_id = ? AND last_seen_at >= ? AND ended_at IS NULL "
                "LIMIT 2", (client_id, since)).fetchall()
        if len(rows) != 1:
            return None
        self.bump("notice_fallback_used")
        return rows[0]["address"]

    def touch(self, address: str) -> None:
        with self.store._lock:
            self._db().execute("UPDATE addresses SET last_seen_at = ? WHERE address = ?", (self.clock(), address))

    def ensure_address(self, name: str, agent_id: str, kind: str = "other", wake_policy: str = "never") -> str:
        address = f"{name}@{agent_id}"
        now = self.clock()
        with self.store._lock:
            self._db().execute(
                "INSERT INTO addresses(address, name, agent_id, kind, wake_policy, last_seen_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(address) DO UPDATE SET last_seen_at = excluded.last_seen_at",
                (address, name, agent_id, kind, wake_policy, now, now))
        return address

    def presence(self, row: dict) -> str:
        if row["kind"] == "human":
            return "owner"
        if row.get("ended_at"):
            return "ended"
        if not self.online(row["agent_id"]):
            return "offline"
        if self.router is not None and self.router.busy(row["address"]):
            return "busy"
        if self._inflight:
            with self.store._lock:
                if self._waiting_on(self._db(), row["address"]):
                    return "active"  # blocked in mail_wait / ask_user: the mail reaches it there, no wake
        seen = row.get("last_seen_at")
        if seen is not None and self.clock() - seen < float(self._cfg("presence", "idle_after_s")):
            return "active"
        return "idle"

    def presence_report(self, agent_id: str, args: dict) -> dict:
        """A Claude/Codex hook on a device (design §8.3 source 1): resume id and presence by (device, kind, cwd)."""
        kind, cwd, session_id = args.get("kind"), args.get("cwd"), args.get("session_id")
        if kind not in ("claude", "codex") or not isinstance(cwd, str) or not cwd.strip():
            raise RoutingError("presence_report needs kind (claude or codex) and cwd")
        if session_id is not None and not valid_resume_id(session_id):
            # It becomes a command-line argument of a later wake (§8.7).
            raise RoutingError(RESUME_ID_RULE.replace("resume_id", "session_id"))

        # Explicit and automatic (layer 1) sessions; legacy anon-… rows never.
        with self.store._lock:
            rows = [dict(row) for row in self._db().execute(
                "SELECT * FROM addresses WHERE agent_id = ? AND kind = ? AND ended_at IS NULL "
                "AND name NOT LIKE 'anon-%'", (agent_id, kind)).fetchall()]
        matched = [row for row in rows if row.get("workdir") and norm_workdir(row["workdir"]) == norm_workdir(cwd)]
        if len(matched) > 1:
            # Automatic rows that no live handle holds (a restart left them behind) are not
            # sessions that could be reporting.
            now = self.clock()
            with self.store._lock:
                db = self._db()
                matched = [row for row in matched if row.get("name_source") != "auto"
                           or self._has_live_handle(db, row["address"], now)] or matched
        if len(matched) > 1 and session_id:
            # Several sessions in one folder: the one already holding this id, else none (never guess).
            matched = [row for row in matched if row.get("resume_id") == session_id]
        if len(matched) != 1:
            return {"matched": None, "candidates": [row["address"] for row in matched]}
        row = matched[0]
        now = self.clock()
        active = args.get("event") in ("prompt", "UserPromptSubmit", "SessionStart", "turn")
        # The title (layer 2): a client's own session title replaces what is there; a first-prompt
        # fallback (title_source "prompt") only fills an empty one. Another session in the folder
        # (a new session id) starts without the old session's title.
        title = row.get("title")
        if session_id and row.get("resume_id") and session_id != row["resume_id"]:
            title = None
        reported = clean_title(args.get("title"))
        if reported and not (args.get("title_source") == "prompt" and title):
            title = reported
        with self.store._lock:
            self._db().execute(
                "UPDATE addresses SET resume_id = COALESCE(?, resume_id), "
                "resume_source = CASE WHEN ? IS NOT NULL THEN 'hook' ELSE resume_source END, "
                "last_seen_at = CASE WHEN ? THEN ? ELSE last_seen_at END, title = ? WHERE address = ?",
                (session_id, session_id, int(active), now, title, row["address"]))
        return {"matched": row["address"], "resume_source": "hook" if session_id else row.get("resume_source"),
                "title": title}

    def has_live_handle(self, address: str) -> bool:
        with self.store._lock:
            return self._has_live_handle(self._db(), address, self.clock())

    def bye(self, address: str) -> dict:
        with self.store._lock:
            self._db().execute("UPDATE addresses SET ended_at = ? WHERE address = ?", (self.clock(), address))
        return {"address": address, "address_display": self.display(address), "state": "ended",
                "unread": self.unread_count(address),
                "message": "Session ended: it will not be woken again. Mail is kept; use_device with the same "
                           "session_name (or, for an automatic name, from the same client and workdir) "
                           "re-attaches."}

    def set_address(self, address: str, fields: dict) -> dict:
        """Owner settings of an address (§8.7/§8.12): wake_permission, wake_policy, resume_id."""
        row = self.address_row(address)
        if row is None:
            raise RoutingError(f"unknown address {address!r}")
        updates: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "wake_permission":
                if value is not None and value not in WAKE_PERMISSIONS:
                    raise RoutingError(f"wake_permission must be one of {', '.join(WAKE_PERMISSIONS)} or null")
            elif key == "wake_policy":
                if value not in WAKE_POLICIES:
                    raise RoutingError(f"wake_policy must be one of {', '.join(WAKE_POLICIES)}")
            elif key == "resume_id":
                if value is not None and not valid_resume_id(value):
                    raise RoutingError(RESUME_ID_RULE + " (or null)")
                updates["resume_source"] = "explicit" if value else None
            else:
                raise RoutingError(f"not editable: {key}")
            updates[key] = value
        if updates:
            with self.store._lock:
                self._db().execute(f"UPDATE addresses SET {', '.join(f'{k} = ?' for k in updates)} WHERE address = ?",
                                   (*updates.values(), address))
        return self.address_view(self.address_row(address))

    @staticmethod
    def name_source(row: dict) -> str:
        """How the name was obtained: explicit, auto, or anon (legacy rows from before migration 7)."""
        if row.get("name_source"):
            return row["name_source"]
        return "anon" if str(row.get("name") or "").startswith("anon-") else "explicit"

    def address_view(self, row: dict) -> dict:
        now = self.clock()
        seen = row.get("last_seen_at")
        return {"address": row["address"], "address_display": self.display(row["address"]), "name": row["name"],
                "kind": row["kind"], "agent_id": row["agent_id"] or None, "presence": self.presence(row),
                "last_seen_s_ago": int(now - seen) if seen else None, "workdir": row.get("workdir"),
                "wakeable": self.router.wakeable(row) if self.router else False,
                "wake_adapter": self.router.adapter_for(row) if self.router else None,
                "wake_policy": row["wake_policy"],
                "wake_permission": row.get("wake_permission"), "resume_source": row.get("resume_source"),
                "name_source": self.name_source(row), "title": row.get("title"),
                "device_display": self.display_device(row["agent_id"]) if row["agent_id"] else None,
                "unread": self.unread_count(row["address"]), "mail": self.mail_count(row["address"]),
                "last_wake": self.router.last_wake(row["address"]) if self.router else None}

    def addresses(self, *, agent_id: str | None = None, kind: str | None = None,
                  include_ended: bool = False) -> list[dict]:
        sql, args = "SELECT * FROM addresses WHERE 1 = 1", []
        if agent_id:
            sql += " AND agent_id = ?"
            args.append(agent_id)
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if not include_ended:
            sql += " AND ended_at IS NULL"
        with self.store._lock:
            rows = [dict(row) for row in self._db().execute(sql + " ORDER BY address", args).fetchall()]
        recent = self.clock() - 86400
        # Anonymous addresses (one per handle of a client without session_name)
        # are listed only while recently used or holding mail.
        rows = [row for row in rows if not row["name"].startswith("anon-") or include_ended
                or (row.get("last_seen_at") or 0) >= recent or self.mail_count(row["address"])]
        return [self.address_view(row) for row in rows]

    # -- recipients ---------------------------------------------------------------
    def expand(self, items: Iterable[str], sender: str | None) -> tuple[list[str], list[str]]:
        """Recipient expressions -> (addresses, unknown). Groups expand now (§8.4).

        Groups (``@all``, ``@device:``, ``@kind:``, ``@team:``) leave the sender
        out; an explicit address that names the sender keeps it, so a session
        can mail itself (§8.6 measures the real clients that way).
        """
        found: list[str] = []
        unknown: list[str] = []

        def add(address: str, *, explicit: bool = False) -> None:
            if (explicit or address != sender) and address not in found:
                found.append(address)

        with self.store._lock:
            db = self._db()
            live = "ended_at IS NULL AND kind != 'human'"
            for raw in items:
                if not isinstance(raw, str) or not raw.strip():
                    continue
                item = raw.strip()
                if item in (ME, "@me"):
                    add(ME, explicit=True)
                elif item == "@all":
                    for row in db.execute(f"SELECT address FROM addresses WHERE {live} AND name NOT LIKE 'anon-%' "
                                          f"AND name != ?", (DEVICE_ASK_NAME,)):
                        add(row["address"])
                elif item.startswith("@device:"):
                    try:
                        agent_id = self.resolve_device(item[len("@device:"):])
                    except RoutingError:
                        unknown.append(item)
                        continue
                    rows = db.execute(f"SELECT address FROM addresses WHERE {live} AND agent_id = ? "
                                      f"AND name NOT LIKE 'anon-%' AND name != ?", (agent_id, DEVICE_ASK_NAME)).fetchall()
                    if not rows:
                        unknown.append(item)
                    for row in rows:
                        add(row["address"])
                elif item.startswith("@kind:"):
                    rows = db.execute(f"SELECT address FROM addresses WHERE {live} AND kind = ? "
                                      f"AND name NOT LIKE 'anon-%'", (item[len("@kind:"):],)).fetchall()
                    if not rows:
                        unknown.append(item)
                    for row in rows:
                        add(row["address"])
                elif item.startswith("@team:"):
                    row = db.execute("SELECT members_json FROM groups WHERE name = ?", (item[len("@team:"):],)).fetchone()
                    if row is None:
                        unknown.append(item)
                        continue
                    members, missing = self._expand_plain(db, json.loads(row["members_json"]))
                    unknown.extend(missing)
                    for address in members:
                        add(address)
                else:
                    members, missing = self._expand_plain(db, [item])
                    unknown.extend(missing)
                    for address in members:
                        add(address, explicit=True)
        return found[:MAX_RECIPIENTS], unknown

    def _expand_plain(self, db, items: list[str]) -> tuple[list[str], list[str]]:
        found, unknown = [], []
        for item in items:
            if not isinstance(item, str):
                continue
            if item == ME:
                found.append(ME)
            elif "@" in item:
                name, device = item.rsplit("@", 1)
                try:
                    agent_id = self.resolve_device(device)
                except RoutingError:
                    unknown.append(item)
                    continue
                address = f"{name}@{agent_id}"
                if db.execute("SELECT 1 FROM addresses WHERE address = ?", (address,)).fetchone():
                    found.append(address)
                else:
                    unknown.append(item)
            else:
                rows = db.execute("SELECT address FROM addresses WHERE name = ? AND ended_at IS NULL",
                                  (item,)).fetchall()
                if len(rows) == 1:
                    found.append(rows[0]["address"])
                else:
                    unknown.append(item)  # unknown, or a bare name used on several devices
        return found, unknown

    def set_group(self, name: str, members: list[str]) -> None:
        if not _NAME.fullmatch(name or "") or not isinstance(members, list):
            raise RoutingError("a team needs a simple name and a list of members")
        with self.store._lock:
            self._db().execute("INSERT INTO groups(name, members_json) VALUES (?, ?) "
                               "ON CONFLICT(name) DO UPDATE SET members_json = excluded.members_json",
                               (name, json.dumps(members)))

    # -- sending ------------------------------------------------------------------
    def send(self, from_addr: str, to: Any, body: str, *, subject: str = "", thread_id: str | None = None,
             reply_to: str | None = None, kind: str = "message", priority: str = "normal",
             attachments: list | None = None, meta: dict | None = None, wake: str = "auto") -> dict:
        if kind not in MESSAGE_KINDS:
            raise RoutingError(f"kind must be one of {', '.join(SEND_KINDS)}")
        if priority not in PRIORITIES:
            raise RoutingError(f"priority must be one of {', '.join(PRIORITIES)}")
        if not isinstance(body, str):
            raise RoutingError("body must be a string")
        subject = _clip(subject if isinstance(subject, str) else "", MAX_SUBJECT_CHARS)
        if attachments is not None:
            if not isinstance(attachments, list) or not all(isinstance(item, dict) for item in attachments):
                raise RoutingError("attachments must be a list of objects (text, media_url or a tool reference)")
        attachments_json = json.dumps(attachments, ensure_ascii=False) if attachments else None
        size = len(body.encode()) + len((attachments_json or "").encode())
        if size > int(self._cfg("limits", "max_body_bytes")):
            raise RoutingError(f"message too large: body and attachments are limited to "
                               f"{self._cfg('limits', 'max_body_bytes')} bytes")
        targets = [to] if isinstance(to, str) else list(to or [])
        if not targets or not all(isinstance(item, str) for item in targets):
            raise RoutingError("to must be an address, a group (@all, @device:<id>, @kind:<k>, @team:<name>) or a list")
        recipients, unknown = self.expand(targets, from_addr)
        if not recipients:
            raise RoutingError("no known recipients" + (f": {', '.join(unknown)}" if unknown else "")
                               + "; mail_who lists the addresses")
        now = self.clock()
        hop = 0
        with self.store._lock:
            db = self._db()
            if reply_to:
                parent = db.execute("SELECT thread_id, hop FROM messages WHERE id = ?", (reply_to,)).fetchone()
                if parent is None:
                    raise RoutingError(f"unknown reply_to message {reply_to!r}")
                thread_id, hop = parent["thread_id"], parent["hop"] + 1
            elif thread_id:
                if db.execute("SELECT 1 FROM messages WHERE thread_id = ? LIMIT 1", (thread_id,)).fetchone() is None:
                    raise RoutingError(f"unknown thread_id {thread_id!r}")
            else:
                thread_id = "t_" + ulid(now)
            if self.router is not None:
                # A session woken by a job continues that job's hop count (§8.8).
                hop = max(hop, self.router.sender_hop(from_addr))
            message_id = "m_" + ulid(now)
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT INTO messages(id, thread_id, reply_to, from_addr, to_expr, subject, body, kind, priority, "
                    "hop, attachments_json, meta_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (message_id, thread_id, reply_to, from_addr, json.dumps(targets, ensure_ascii=False), subject,
                     body, kind, priority, hop, attachments_json,
                     json.dumps(meta, ensure_ascii=False) if meta else None, now))
                db.executemany("INSERT INTO deliveries(message_id, to_addr) VALUES (?, ?)",
                               [(message_id, address) for address in recipients])
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        deliveries = []
        for address in recipients:
            row = self.address_row(address) or {"kind": "other", "agent_id": "", "address": address}
            presence = self.presence(row)
            if row["kind"] == "human":
                action = "inbox"  # the owner's inbox and toasts (§8.12)
            elif self.router is not None and "wake_policy" in row:
                action = self.router.route(message_id, row, wake=wake, hop=hop)
            else:
                action = "noticed_on_next_call"
            deliveries.append({"to": address, "to_display": self.display(address), "presence": presence,
                               "action": action})
        self.event("mail_sent", from_addr, None, message_id=message_id, recipients=len(recipients))
        return {"message_id": message_id, "thread_id": thread_id, "deliveries": deliveries,
                "unknown_recipients": unknown}

    # -- reading -------------------------------------------------------------------
    def mail_count(self, address: str) -> int:
        """Mail delivered to the address in any state (the inbox lists a legacy address while it has any)."""
        with self.store._lock:
            return self._db().execute("SELECT COUNT(*) FROM deliveries WHERE to_addr = ?", (address,)).fetchone()[0]

    def unread_count(self, address: str) -> int:
        with self.store._lock:
            return self._db().execute("SELECT COUNT(*) FROM deliveries WHERE to_addr = ? AND state = 'unread'",
                                      (address,)).fetchone()[0]

    def _summary(self, row) -> dict:
        sender = row["from_addr"]
        sender_row = self.address_row(sender) if "@" in sender or sender == ME else None
        return {"id": row["id"], "thread_id": row["thread_id"], "from": self.display(sender), "from_address": sender,
                "from_kind": sender_row["kind"] if sender_row else sender, "subject": row["subject"] or "",
                "snippet": _clip(row["body"], 140), "priority": row["priority"], "kind": row["kind"],
                "created_at": row["created_at"], "state": row["state"] if "state" in row.keys() else None}

    def _full(self, row, *, viewer: str | None) -> dict:
        item = self._summary(row)
        item.update(body=row["body"], reply_to=row["reply_to"], to=json.loads(row["to_expr"]),
                    attachments=json.loads(row["attachments_json"]) if row["attachments_json"] else [],
                    meta=json.loads(row["meta_json"]) if row["meta_json"] else {},
                    mine=viewer is not None and row["from_addr"] == viewer)
        item.pop("snippet", None)
        return item

    def inbox(self, address: str, *, unread_only: bool = True, thread_id: str | None = None, limit: int = 20,
              before: str | None = None) -> dict:
        if not 1 <= int(limit) <= 100:
            raise RoutingError("limit must be 1-100")
        sql = ("SELECT m.*, d.state AS state FROM deliveries d JOIN messages m ON m.id = d.message_id "
               "WHERE d.to_addr = ? AND d.state NOT IN ('recalled')")
        args: list[Any] = [address]
        if unread_only:
            sql += " AND d.state = 'unread'"
        if thread_id:
            sql += " AND m.thread_id = ?"
            args.append(thread_id)
        if before:
            sql += " AND m.id < ?"
            args.append(before)
        sql += " ORDER BY m.id DESC LIMIT ?"
        args.append(int(limit) + 1)
        with self.store._lock:
            rows = self._db().execute(sql, args).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        return {"me": address, "me_display": self.display(address), "unread": self.unread_count(address),
                "messages": [self._summary(row) for row in rows],
                "next_before": rows[-1]["id"] if more and rows else None}

    def read(self, address: str, *, message_id: str | None = None, thread_id: str | None = None,
             mark_read: bool = True) -> dict:
        now = self.clock()
        with self.store._lock:
            db = self._db()
            if message_id:
                row = db.execute("SELECT m.*, d.state AS state FROM messages m LEFT JOIN deliveries d "
                                 "ON d.message_id = m.id AND d.to_addr = ? WHERE m.id = ?",
                                 (address, message_id)).fetchone()
                if row is None or (row["state"] is None and row["from_addr"] != address):
                    raise RoutingError(f"unknown message {message_id!r} (not addressed to {self.display(address)})")
                if row["state"] == "recalled":
                    return {"messages": [], "message_id": message_id, "state": "recalled",
                            "thread_id": row["thread_id"],
                            "message": "The sender recalled this message before you read it.",
                            "unread_remaining": self.unread_count(address)}
                rows = [row]
                thread = row["thread_id"]
            elif thread_id:
                rows = db.execute(
                    "SELECT m.*, d.state AS state FROM messages m LEFT JOIN deliveries d "
                    "ON d.message_id = m.id AND d.to_addr = ? WHERE m.thread_id = ? "
                    "AND ((d.state IS NOT NULL AND d.state != 'recalled') OR m.from_addr = ?) ORDER BY m.id",
                    (address, thread_id, address)).fetchall()
                if not rows:
                    raise RoutingError(f"unknown thread {thread_id!r} (no messages to or from {self.display(address)})")
                thread = thread_id
            else:
                row = db.execute("SELECT m.*, d.state AS state FROM deliveries d JOIN messages m "
                                 "ON m.id = d.message_id WHERE d.to_addr = ? AND d.state = 'unread' "
                                 "ORDER BY m.id LIMIT 1", (address,)).fetchone()
                if row is None:
                    return {"messages": [], "thread_id": None, "participants": [], "unread_remaining": 0}
                rows = [row]
                thread = row["thread_id"]
            ids = [row["id"] for row in rows if row["state"] == "unread"]
            if mark_read and ids:
                db.executemany("UPDATE deliveries SET state = 'read', read_at = ? WHERE message_id = ? "
                               "AND to_addr = ? AND state = 'unread'", [(now, mid, address) for mid in ids])
            participants = self._participants(db, thread)
        messages = []
        for row in rows:
            item = self._full(row, viewer=address)
            if mark_read and row["id"] in ids:
                item["state"] = "read"
            messages.append(item)
        return {"messages": messages, "thread_id": thread, "participants": participants,
                "unread_remaining": self.unread_count(address)}

    def _participants(self, db, thread_id: str) -> list[str]:
        found: list[str] = []
        for row in db.execute("SELECT id, from_addr FROM messages WHERE thread_id = ? ORDER BY id", (thread_id,)):
            for address in [row["from_addr"], *(r["to_addr"] for r in db.execute(
                    "SELECT to_addr FROM deliveries WHERE message_id = ?", (row["id"],)))]:
                if address not in found:
                    found.append(address)
        return [self.display(address) for address in found]

    # -- sender status and recall (decision D9) --------------------------------------
    @staticmethod
    def _status(row) -> str:
        state = row["state"]
        if state == "recalled":
            return "recalled"
        if state == "expired":
            return "expired"
        if state in ("read", "archived"):
            return "read"
        if row["woken_at"]:
            return "woken"
        return "delivered"

    def status(self, message_id: str) -> dict:
        with self.store._lock:
            db = self._db()
            message = db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
            if message is None:
                raise RoutingError(f"unknown message {message_id!r}")
            rows = db.execute("SELECT * FROM deliveries WHERE message_id = ? ORDER BY to_addr", (message_id,)).fetchall()
        return {"message_id": message_id, "thread_id": message["thread_id"], "from": self.display(message["from_addr"]),
                "recipients": [{"to": row["to_addr"], "to_display": self.display(row["to_addr"]),
                                "status": self._status(row), "read_at": row["read_at"], "woken_at": row["woken_at"],
                                "escalation_level": row["escalation_level"],
                                "wake": self.router.wake_for(row["to_addr"], message_id) if self.router else None}
                               for row in rows]}

    def recall(self, address: str, message_id: str, to: Any = None) -> dict:
        with self.store._lock:
            message = self._db().execute("SELECT from_addr FROM messages WHERE id = ?", (message_id,)).fetchone()
        if message is None:
            raise RoutingError(f"unknown message {message_id!r}")
        with self.store._lock:
            rows = self._db().execute("SELECT to_addr, state FROM deliveries WHERE message_id = ?",
                                      (message_id,)).fetchall()
        if to is not None:
            wanted, _ = self.expand([to] if isinstance(to, str) else list(to), None)
            rows = [row for row in rows if row["to_addr"] in wanted]
        if message["from_addr"] != address:
            return {"message_id": message_id, "recalled": [],
                    "not_recalled": [{"to": row["to_addr"], "reason": "not_sender"} for row in rows]}
        recalled, not_recalled = [], []
        now = self.clock()
        with self.store._lock:
            db = self._db()
            for row in rows:
                if row["state"] == "unread":
                    changed = db.execute("UPDATE deliveries SET state = 'recalled', recalled_at = ? "
                                         "WHERE message_id = ? AND to_addr = ? AND state = 'unread'",
                                         (now, message_id, row["to_addr"])).rowcount
                    if changed:
                        recalled.append(row["to_addr"])
                        continue
                    row = db.execute("SELECT to_addr, state FROM deliveries WHERE message_id = ? AND to_addr = ?",
                                     (message_id, row["to_addr"])).fetchone()
                reason = {"read": "already_read", "archived": "already_read", "expired": "expired",
                          "recalled": "already_recalled"}.get(row["state"], row["state"])
                not_recalled.append({"to": row["to_addr"], "reason": reason})
        if recalled and self.router is not None:
            # Pending wake jobs forget the message; a job left empty is skipped (§8.5).
            for to_addr in recalled:
                self.router.drop_message(to_addr, message_id, reason="recalled")
        if recalled:
            self.event("mail_recalled", address, None, message_id=message_id, recipients=len(recalled))
        return {"message_id": message_id, "recalled": recalled, "not_recalled": not_recalled}

    # -- the unread notice (§8.6) -------------------------------------------------------
    def notice_for(self, address: str, handle: str | None = None) -> Notice | None:
        with self.store._lock:
            db = self._db()
            rows = db.execute(
                "SELECT m.id, m.from_addr, m.subject, m.body, m.priority, m.created_at FROM deliveries d "
                "JOIN messages m ON m.id = d.message_id WHERE d.to_addr = ? AND d.state = 'unread' "
                "ORDER BY (m.priority = 'urgent') DESC, m.id DESC", (address,)).fetchall()
        if not rows:
            self._last_notice.pop(address, None)
            return None
        ids = frozenset(row["id"] for row in rows)
        if self._cfg("notice", "repeat") == "on_change" and self._last_notice.get(address) == ids:
            return None
        self._last_notice[address] = ids
        now = self.clock()
        limit = max(1, int(self._cfg("notice", "max_items")))
        shown = rows[:limit]
        lines = [f"{NOTICE_PREFIX} {self.display(address)} 有 {len(rows)} 封未读："]
        for row in shown:
            label = _clip(row["subject"] or row["body"], 40)
            urgent = " (urgent)" if row["priority"] == "urgent" else ""
            lines.append(f"- {row['id']} 来自 {self.display(row['from_addr'])}「{label}」{urgent} {_ago(now - row['created_at'])}")
        if len(rows) > len(shown):
            lines.append(f"- 另有 {len(rows) - len(shown)} 封")
        handle_text = handle or (self.address_row(address) or {}).get("last_handle") or "…"
        lines.append(f'用 mail_read(workspace_id="{handle_text}", message_id="…") 阅读，mail_inbox 查看全部。')
        text = "\n".join(lines)
        max_chars = int(self._cfg("notice", "max_chars"))
        if len(text) > max_chars:
            text = text[:max_chars - 1] + "…"
        meta = {"unread": len(rows), "me": address, "me_display": self.display(address),
                "latest": [{"id": row["id"], "from": self.display(row["from_addr"]), "subject": row["subject"] or "",
                            "priority": row["priority"], "at": row["created_at"]} for row in shown]}
        with self.store._lock:
            self._db().executemany(
                "UPDATE deliveries SET noticed_count = noticed_count + 1, first_noticed_at = COALESCE(first_noticed_at, ?) "
                "WHERE message_id = ? AND to_addr = ?", [(now, row["id"], address) for row in shown])
        return Notice(text=text, meta=meta)

    # -- asks (decision D11, §8.12) --------------------------------------------------------
    def ask_create(self, from_addr: str, question: str, *, options: list[str] | None = None,
                   recommended: str | None = None, context: str = "", blocking: bool = False,
                   origin: str = "ask_user", kind: str | None = None, title: str | None = None,
                   allow_other: bool = True) -> dict:
        if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
            raise RoutingError(f"question must be 1-{MAX_QUESTION_CHARS} characters")
        options = list(options or [])
        if len(options) > MAX_OPTIONS or not all(isinstance(item, str) and 0 < len(item.strip()) <= MAX_OPTION_CHARS
                                                for item in options):
            raise RoutingError(f"options must be at most {MAX_OPTIONS} non-empty strings of up to {MAX_OPTION_CHARS} characters")
        if len(set(options)) != len(options):
            raise RoutingError("options must be unique")
        if recommended is not None and recommended not in options:
            raise RoutingError("recommended must be one of the options")
        kind = kind or ("single" if options else "text")
        if kind not in ASK_KINDS:
            raise RoutingError(f"kind must be one of {', '.join(ASK_KINDS)}")
        if kind != "text" and not options:
            raise RoutingError(f"kind={kind!r} needs options")
        if not isinstance(context, str) or len(context) > 8000:
            raise RoutingError("context must be a string of at most 8000 characters")
        now = self.clock()
        ask_id = "q_" + ulid(now)
        ordered = ordered_options(options, recommended)
        with self.store._lock:
            previous = self._db().execute("SELECT thread_id FROM asks WHERE from_addr = ? AND thread_id IS NOT NULL "
                                          "ORDER BY id DESC LIMIT 1", (from_addr,)).fetchone()
        lines = [question.strip()]
        if context.strip():
            lines += ["", context.strip()]
        if ordered:
            lines += ["", "选项：" + "；".join(f"{index}) {item}{'（推荐）' if item == recommended else ''}"
                                              for index, item in enumerate(ordered, 1))
                      + ("；也可以自由填写" if allow_other else "")]
        sent = self.send(from_addr, ME, "\n".join(lines), subject=title or _clip(question, 80),
                         thread_id=previous["thread_id"] if previous else None, kind="question",
                         priority="urgent" if blocking else "normal", meta={"ask_id": ask_id})
        expire_days = self._cfg("inbox", "ask_expire_days")
        with self.store._lock:
            self._db().execute(
                "INSERT INTO asks(id, from_addr, message_id, thread_id, title, question, options_json, recommended, "
                "context, kind, allow_other, blocking, origin, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ask_id, from_addr, sent["message_id"], sent["thread_id"], title, question.strip(),
                 json.dumps(options, ensure_ascii=False), recommended, context, kind, int(bool(allow_other)),
                 int(bool(blocking)), origin, now, now + float(expire_days) * 86400 if expire_days else None))
        self.event("ask_created", from_addr, None, ask_id=ask_id, origin=origin)
        return {"ask_id": ask_id, "message_id": sent["message_id"], "thread_id": sent["thread_id"], "state": "open"}

    def ask_view(self, row) -> dict:
        row = dict(row)
        options = json.loads(row["options_json"])
        return {"ask_id": row["id"], "from": self.display(row["from_addr"]), "from_address": row["from_addr"],
                "question": row["question"], "title": row["title"], "context": row["context"] or "",
                "options": ordered_options(options, row["recommended"]), "recommended": row["recommended"],
                "kind": row["kind"], "allow_other": bool(row["allow_other"]), "origin": row["origin"],
                "state": row["state"], "answer": json.loads(row["answer_json"]) if row["answer_json"] else None,
                "answered_via": row["answered_via"], "answered_at": row["answered_at"],
                "created_at": row["created_at"], "message_id": row["message_id"], "thread_id": row["thread_id"],
                "blocking": bool(row["blocking"])}

    def ask_get(self, ask_id: str) -> dict:
        with self.store._lock:
            row = self._db().execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        if row is None:
            raise RoutingError(f"unknown ask {ask_id!r}")
        return self.ask_view(row)

    def asks(self, *, state: str | None = "open", thread_id: str | None = None, limit: int = 50) -> list[dict]:
        sql, args = "SELECT * FROM asks WHERE 1 = 1", []
        if state:
            sql += " AND state = ?"
            args.append(state)
        if thread_id:
            sql += " AND thread_id = ?"
            args.append(thread_id)
        with self.store._lock:
            rows = self._db().execute(sql + " ORDER BY id DESC LIMIT ?", (*args, int(limit))).fetchall()
        return [self.ask_view(row) for row in rows]

    def ask_answer(self, ask_id: str, *, choice: str | None = None, text: str | None = None,
                   choices: list[str] | None = None, via: str = "inbox") -> dict:
        """First answer wins; the asker gets it back as a ``kind=answer`` mail from ``me``."""
        if via not in ANSWER_VIA:
            raise RoutingError(f"via must be one of {', '.join(ANSWER_VIA)}")
        current = self.ask_get(ask_id)
        if current["state"] != "open":
            return {"ask_id": ask_id, "state": current["state"], "already": True,
                    "answered_via": current["answered_via"], "answer": current["answer"],
                    "message": f"已由 {current['answered_via'] or '其他途径'} 回答" if current["state"] == "answered"
                    else f"this question is {current['state']}"}
        options = current["options"]
        if choices is not None:
            if current["kind"] != "multi" or not isinstance(choices, list) or not choices \
                    or not all(item in options for item in choices):
                raise RoutingError("choices must be a non-empty list of this question's options (multi questions only)")
            answer: dict[str, Any] = {"choices": [item for item in options if item in choices]}
            body = "已选择：" + "、".join(answer["choices"])
        elif choice is not None:
            if choice not in options:
                raise RoutingError("choice must be one of the options; use text for a free answer")
            answer = {"choice": choice}
            body = f"已选择：{choice}"
        elif isinstance(text, str) and text.strip():
            if options and not current["allow_other"]:
                raise RoutingError("this question only accepts one of its options")
            answer = {"text": text.strip()[:MAX_QUESTION_CHARS]}
            body = answer["text"]
        else:
            raise RoutingError("an answer needs choice, choices or text")
        answer["via"] = via
        now = self.clock()
        with self.store._lock:
            changed = self._db().execute(
                "UPDATE asks SET state = 'answered', answer_json = ?, answered_via = ?, answered_at = ? "
                "WHERE id = ? AND state = 'open'", (json.dumps(answer, ensure_ascii=False), via, now, ask_id)).rowcount
            if changed and current["message_id"]:
                self._db().execute("UPDATE deliveries SET state = 'read', read_at = ? WHERE message_id = ? "
                                   "AND to_addr = ? AND state = 'unread'", (now, current["message_id"], ME))
        if not changed:  # someone else answered in between
            return self.ask_answer(ask_id, choice=choice, text=text, choices=choices, via=via)
        sent = self.send(ME, current["from_address"], body, subject=f"回答：{_clip(current['question'], 60)}",
                         reply_to=current["message_id"], kind="answer",
                         meta={"ask_id": ask_id, "answer": answer})
        if _is_device_ask(current["from_address"]):
            # The device polls ask_get; nobody reads device-ask mail, so it is filed as read.
            self.mark_read(sent["message_id"], current["from_address"])
        self.event("ask_answered", current["from_address"], None, ask_id=ask_id, via=via)
        return {"ask_id": ask_id, "state": "answered", "answer": answer, "message_id": sent["message_id"],
                "thread_id": sent["thread_id"], "asker": current["from_address"]}

    def mark_read(self, message_id: str, address: str) -> None:
        with self.store._lock:
            self._db().execute("UPDATE deliveries SET state = 'read', read_at = ? WHERE message_id = ? AND to_addr = ? "
                               "AND state = 'unread'", (self.clock(), message_id, address))

    # -- the inbox page (§8.12) ------------------------------------------------------------
    def threads(self, viewer: str = ME, *, limit: int = 50) -> list[dict]:
        with self.store._lock:
            db = self._db()
            rows = db.execute(
                "SELECT m.thread_id AS thread_id, MAX(m.id) AS last_id, COUNT(*) AS n FROM messages m "
                "WHERE m.from_addr = ? OR EXISTS (SELECT 1 FROM deliveries d WHERE d.message_id = m.id "
                "AND d.to_addr = ? AND d.state != 'recalled') GROUP BY m.thread_id ORDER BY last_id DESC LIMIT ?",
                (viewer, viewer, int(limit))).fetchall()
            result = []
            for row in rows:
                first = db.execute("SELECT subject, body FROM messages WHERE thread_id = ? ORDER BY id LIMIT 1",
                                   (row["thread_id"],)).fetchone()
                last = db.execute("SELECT * FROM messages WHERE id = ?", (row["last_id"],)).fetchone()
                unread = db.execute("SELECT COUNT(*) FROM deliveries d JOIN messages m ON m.id = d.message_id "
                                    "WHERE m.thread_id = ? AND d.to_addr = ? AND d.state = 'unread'",
                                    (row["thread_id"], viewer)).fetchone()[0]
                open_asks = db.execute("SELECT COUNT(*) FROM asks WHERE thread_id = ? AND state = 'open'",
                                       (row["thread_id"],)).fetchone()[0]
                result.append({"thread_id": row["thread_id"], "subject": first["subject"] or _clip(first["body"], 60),
                               "participants": self._participants(db, row["thread_id"]), "messages": row["n"],
                               "unread": unread, "open_asks": open_asks, "last_at": last["created_at"],
                               "last_from": self.display(last["from_addr"]), "last_snippet": _clip(last["body"], 100)})
        return result

    def resolve_address(self, address: Any) -> str | None:
        """An address as stored (``name@<agent id>``) from that or its display form (``name@<device name>``)."""
        if not isinstance(address, str) or not address or len(address) > 300:
            return None
        if address == ME or self.address_row(address) is not None:
            return address
        with self.store._lock:
            found, _unknown = self._expand_plain(self._db(), [address])
        return found[0] if found else None

    def conversation(self, address: str, *, limit: int = 200, mark_read: bool = False) -> dict:
        """``me`` and one address (docs/agent-chat.md §10): the mail between them both ways and that address's
        asks, oldest first. ``mark_read``: file the address's mail to ``me`` as read (the chat shows it)."""
        resolved = self.resolve_address(address)
        if resolved is None or resolved == ME:
            raise RoutingError(f"unknown address {address!r}")
        limit = max(1, min(int(limit or 200), 500))
        with self.store._lock:
            db = self._db()
            rows = db.execute(
                "SELECT m.*, d.state AS state, d.to_addr AS delivered_to FROM messages m JOIN deliveries d "
                "ON d.message_id = m.id WHERE d.state != 'recalled' AND ((m.from_addr = ? AND d.to_addr = ?) "
                "OR (m.from_addr = ? AND d.to_addr = ?)) ORDER BY m.created_at DESC, m.id DESC LIMIT ?",
                (resolved, ME, ME, resolved, limit)).fetchall()
            ask_rows = db.execute("SELECT * FROM asks WHERE from_addr = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                                  (resolved, limit)).fetchall()
            unread = [row["id"] for row in rows if row["from_addr"] == resolved and row["state"] == "unread"]
            if mark_read and unread:
                now = self.clock()
                db.executemany("UPDATE deliveries SET state = 'read', read_at = ? WHERE message_id = ? "
                               "AND to_addr = ? AND state = 'unread'", [(now, mid, ME) for mid in unread])
        messages = []
        for row in reversed(rows):
            item = self._full(row, viewer=ME)
            item["direction"] = "out" if row["from_addr"] == ME else "in"
            if mark_read and row["id"] in unread:
                item["state"] = "read"
            ask_id = item["meta"].get("ask_id") if isinstance(item.get("meta"), dict) else None
            if ask_id and row["kind"] == "question":
                item["ask_id"] = ask_id
            messages.append(item)
        asks = [self.ask_view(row) for row in reversed(ask_rows)]
        row = self.address_row(resolved) or {}
        return {"address": resolved, "address_display": self.display(resolved), "kind": row.get("kind"),
                "presence": self.presence(row) if row else None, "messages": messages, "asks": asks,
                "unread": sum(1 for item in messages if item["direction"] == "in" and item.get("state") == "unread")}

    def thread(self, viewer: str, thread_id: str, *, mark_read: bool = True) -> dict:
        view = self.read(viewer, thread_id=thread_id, mark_read=mark_read)
        for message in view["messages"]:
            if message["mine"]:
                message["recipients"] = self.status(message["id"])["recipients"]
        view["asks"] = self.asks(state=None, thread_id=thread_id)
        view["viewer"] = viewer
        view["viewer_display"] = self.display(viewer)
        return view

    # -- retention (§8.4, decision D9) --------------------------------------------------------
    def retention(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        read_cut = now - float(self._cfg("retention", "read_days")) * 86400
        events_cut = now - float(self._cfg("retention", "events_days")) * 86400
        jobs_cut = now - float(self._cfg("retention", "jobs_days")) * 86400
        end_cut = now - float(self._cfg("presence", "end_after_days")) * 86400
        removed: dict[str, int] = {}
        expired_asks = self._expire_asks(now)
        with self.store._lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                # device-ask mail is never read by anyone (the device polls ask_get):
                # file any left unread (older databases) as read so it ages out below.
                db.execute("UPDATE deliveries SET state = 'read', read_at = ? WHERE state = 'unread' AND to_addr IN "
                           "(SELECT address FROM addresses WHERE name = ?)", (now, DEVICE_ASK_NAME))
                removed["deliveries"] = db.execute(
                    "DELETE FROM deliveries WHERE (state IN ('read', 'archived') AND read_at < ?) "
                    "OR (state = 'expired' AND expired_at < ?) OR (state = 'recalled' AND recalled_at < ?)",
                    (read_cut, read_cut, read_cut)).rowcount
                removed["messages"] = db.execute(
                    "DELETE FROM messages WHERE created_at < ? AND NOT EXISTS "
                    "(SELECT 1 FROM deliveries d WHERE d.message_id = messages.id) AND NOT EXISTS "
                    "(SELECT 1 FROM asks a WHERE a.message_id = messages.id AND a.state = 'open')",
                    (read_cut,)).rowcount
                removed["asks"] = db.execute(
                    "DELETE FROM asks WHERE state != 'open' AND NOT EXISTS "
                    "(SELECT 1 FROM messages m WHERE m.id = asks.message_id)").rowcount
                removed["events"] = db.execute("DELETE FROM events WHERE at < ?", (events_cut,)).rowcount
                removed["handles"] = db.execute("DELETE FROM handles WHERE expires_at < ?", (now,)).rowcount
                # Finished wake jobs (§8.10 jobs_days); active ones are never purged.
                removed["wake_jobs"] = db.execute(
                    "DELETE FROM wake_jobs WHERE state NOT IN "
                    "('queued','waiting_device','probing','dispatched','running') "
                    "AND COALESCE(finished_at, created_at) < ?", (jobs_cut,)).rowcount
                removed["addresses"] = db.execute(
                    "DELETE FROM addresses WHERE (name LIKE 'anon-%' OR name_source = 'auto') "
                    "AND COALESCE(last_seen_at, created_at) < ? "
                    "AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.to_addr = addresses.address) "
                    "AND NOT EXISTS (SELECT 1 FROM handles h WHERE h.address = addresses.address)",
                    (end_cut,)).rowcount
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        removed["asks_expired"] = expired_asks
        removed["vacuumed"] = int(self._weekly_vacuum(now))
        return removed

    def _weekly_vacuum(self, now: float) -> bool:
        """``VACUUM`` after a WAL checkpoint, at most once a week (§8.4).

        The last run is an ``events`` row (kept 30 days), so it survives restarts.
        """
        with self.store._lock:
            db = self._db()
            last = db.execute("SELECT MAX(at) FROM events WHERE type = 'vacuum'").fetchone()[0]
            if last is not None and now - last < VACUUM_INTERVAL_S:
                return False
            try:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db.execute("VACUUM")
            except Exception as exc:  # a busy database is retried at the next retention run
                log.info("mailbox VACUUM skipped (%s)", type(exc).__name__)
                return False
            db.execute("INSERT INTO events(at, type) VALUES (?, 'vacuum')", (now,))
        return True

    def _expire_asks(self, now: float) -> int:
        with self.store._lock:
            rows = self._db().execute("SELECT * FROM asks WHERE state = 'open' AND expires_at IS NOT NULL "
                                      "AND expires_at < ?", (now,)).fetchall()
        count = 0
        for row in rows:
            with self.store._lock:
                changed = self._db().execute("UPDATE asks SET state = 'expired' WHERE id = ? AND state = 'open'",
                                             (row["id"],)).rowcount
            if not changed:
                continue
            count += 1
            if row["message_id"]:
                self.mark_read(row["message_id"], ME)
            try:
                sent = self.send(SYSTEM_SENDER, row["from_addr"],
                                 f"你的提问「{_clip(row['question'], 60)}」已过期，未得到回答。",
                                 subject="提问已过期", reply_to=row["message_id"], kind="system",
                                 meta={"ask_id": row["id"], "state": "expired"})
            except RoutingError:
                continue  # the asker's address is gone
            if _is_device_ask(row["from_addr"]):
                self.mark_read(sent["message_id"], row["from_addr"])
        return count

    def db_size_mb(self) -> float | None:
        path = self.store.path
        if path is None or not self.store.persistent:
            return None
        try:
            return round(sum(p.stat().st_size for p in (path, path.with_name(path.name + "-wal")) if p.exists())
                         / 1048576, 2)
        except OSError:
            return None

