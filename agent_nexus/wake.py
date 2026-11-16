"""Waking recipients: routing, the wake job queue and the dispatcher (design §8.7, milestone M7).

``WakeRouter.route`` decides, for every delivery to an agent address, whether
the recipient is woken or only sees the unread notice on its next call. A
wake becomes a row in ``wake_jobs``; the partial unique index keeps **one
active job per address**, so mail arriving while a job is pending is merged
into it and two processes never resume the same session.

The dispatcher (``run``: every ``scan_interval_s``, and at once on a send or
when a device connects) sends due jobs to the device hosting the address over
the ``session_wake`` reverse RPC -- only to devices that announced
``session_wake/1`` (§3.2). Legacy devices get ``unsupported`` (or, with
``wake.fallback_exec``, the command through the device plugin's
``exec_command``). Devices that announced ``session_probe/1`` are asked first
whether the session is in use (``skipped_active``).

Job states: ``queued`` -> (``probing``) -> ``dispatched`` / ``running`` ->
``acked`` (the recipient's next tool call) -> ``done`` / ``failed`` (the
device's ``wake_report``). ``waiting_device`` while the device is offline,
``unsupported``, ``skipped_active``, ``skipped`` (recalled / expired mail),
``expired``. Everything uses the store's clock, so tests can move time.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shlex
import subprocess
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .federation import RoutingError
from .ids import RESUME_ID_RULE, valid_resume_id
from .link.wake.adapters import DEFAULT_PERMISSION_FLAGS

if TYPE_CHECKING:
    from .gateway import Gateway

log = logging.getLogger(__name__)

SESSION_WAKE = "session_wake/1"
SESSION_PROBE = "session_probe/1"
#: States holding the address's single active slot (the unique index).
ACTIVE_STATES = ("queued", "waiting_device", "probing", "dispatched", "running")
#: Jobs that hand the session a run: further mail merges into them until their report.
RUNNING_STATES = ("dispatched", "running")
#: Adapters that start a process and report its exit; the others are done once acknowledged.
PROCESS_ADAPTERS = ("claude", "codex")
ADAPTER_BY_KIND = {"claude": "claude", "codex": "codex", "gpt": "companion"}
#: Retry delays after a refused or unacknowledged wake (§8.7): 10 s ... capped at 1 h.
BACKOFF_S = (10, 30, 120, 600, 1800, 3600)
WAKE_RPC_TIMEOUT_S = 30.0
FINAL_STATES = ("acked", "done", "failed", "unsupported", "skipped_active", "skipped", "expired")

PROMPT = ("[agent-nexus] 你（{address}）有 {n} 封新邮件，最新一封来自 {sender}：「{subject}」。\n"
          "你的 workspace_id 是 {handle}（已续期）。请调用 mail_inbox(workspace_id=\"{handle}\") 阅读并处理，\n"
          "需要回复时用 mail_send(reply_to=…)。处理完可以结束本次运行。")

#: Commands for ``wake.fallback_exec`` on legacy devices (no ``session_wake/1``):
#: the default templates of §8.7 with the plain executable names.
FALLBACK_COMMANDS = {
    "claude": {"resume": ["claude", "-p", "--resume", "{resume_id}", "--output-format", "json"],
               "last": ["claude", "-p", "--continue", "--output-format", "json"]},
    "codex": {"resume": ["codex", "exec", "resume", "{resume_id}", "--json", "--skip-git-repo-check"],
              "last": ["codex", "exec", "resume", "--last", "--json", "--skip-git-repo-check"]},
}
FALLBACK_FLAGS = DEFAULT_PERMISSION_FLAGS  # decision D2, the same table as the device side


def _ids(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


class WakeRouter:
    def __init__(self, gateway: "Gateway"):
        self.gateway = gateway
        self.mailbox = gateway.mailbox
        self.store = gateway.store
        self.hub = gateway.hub
        self._kick: asyncio.Event | None = None
        self._nudged: set[str] = set()

    # -- plumbing -----------------------------------------------------------------
    @property
    def cfg(self) -> dict:
        return self.mailbox.config["wake"]

    def clock(self) -> float:
        return self.store.clock()

    def _db(self):
        return self.store._connect()

    def kick(self) -> None:
        """Run the dispatcher now (a send, a device came online)."""
        if self._kick is not None:
            self._kick.set()

    def job(self, job_id: str) -> dict | None:
        with self.store._lock:
            row = self._db().execute("SELECT * FROM wake_jobs WHERE id = ?", (job_id,)).fetchone()
        return self._view(row) if row else None

    def jobs(self, address: str | None = None, *, state: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM wake_jobs WHERE 1 = 1", []
        if address:
            sql += " AND address = ?"
            args.append(address)
        if state:
            sql += " AND state = ?"
            args.append(state)
        with self.store._lock:
            rows = self._db().execute(sql + " ORDER BY created_at, id", args).fetchall()
        return [self._view(row) for row in rows]

    @staticmethod
    def _view(row) -> dict:
        item = dict(row)
        item["message_ids"] = _ids(item["message_ids"])
        return item

    def _set(self, job_id: str, **fields: Any) -> None:
        if "message_ids" in fields:
            fields["message_ids"] = json.dumps(fields["message_ids"])
        with self.store._lock:
            self._db().execute(f"UPDATE wake_jobs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                               (*fields.values(), job_id))

    # -- routing (§8.7 "入队判定") ------------------------------------------------------
    def adapter_for(self, row: dict) -> str | None:
        adapter = ADAPTER_BY_KIND.get(row.get("kind"))
        if adapter is None or not self.cfg["adapters"].get(adapter, False):
            return None
        return adapter

    @staticmethod
    def has_route(row: dict, adapter: str) -> bool:
        if adapter in PROCESS_ADAPTERS:
            if row.get("name_source") == "auto":
                # An automatic name (§8.2 layer 1) is a folder, not a session: "the last session in
                # this folder" may be another conversation, so only a resume id is a route.
                return bool(row.get("resume_id"))
            return bool(row.get("resume_id") or row.get("workdir"))
        if adapter == "companion":
            return bool(row.get("companion_route"))
        return True

    def wakeable(self, row: dict) -> bool:
        if not self.cfg["enabled"] or row.get("kind") == "human" or row.get("wake_policy") == "never" \
                or row.get("ended_at"):
            return False
        adapter = self.adapter_for(row)
        if adapter is None or not self.has_route(row, adapter):
            return False
        peer = self.hub.peers.get(row["agent_id"])
        if peer is None:
            device = self.store.device(row["agent_id"]) or {}
            features = set(device.get("features") or [])
        else:
            features = set(peer.features)
        return SESSION_WAKE in features or (bool(self.cfg["fallback_exec"]) and adapter in PROCESS_ADAPTERS)

    def permission(self, row: dict) -> str:
        """Decision D2: the address's wake_permission, else ``wake.permission_default``."""
        return row.get("wake_permission") or self.cfg["permission_default"] or "bypass"

    def _active(self, address: str) -> dict | None:
        """The job new mail merges into: an active one, or a process run not yet reported."""
        with self.store._lock:
            row = self._db().execute(
                "SELECT * FROM wake_jobs WHERE address = ? AND (state IN (%s) OR (state = 'acked' AND adapter IN (%s) "
                "AND finished_at IS NULL)) ORDER BY created_at DESC LIMIT 1"
                % (",".join("?" * len(ACTIVE_STATES)), ",".join("?" * len(PROCESS_ADAPTERS))),
                (address, *ACTIVE_STATES, *PROCESS_ADAPTERS)).fetchone()
        return self._view(row) if row else None

    def busy(self, address: str) -> bool:
        """``busy`` presence (§8.5): a woken process is running for this address."""
        with self.store._lock:
            return self._db().execute(
                "SELECT 1 FROM wake_jobs WHERE address = ? AND finished_at IS NULL AND adapter IN (%s) "
                "AND state IN ('dispatched', 'running', 'acked') LIMIT 1" % ",".join("?" * len(PROCESS_ADAPTERS)),
                (address, *PROCESS_ADAPTERS)).fetchone() is not None

    def sender_hop(self, address: str) -> int:
        """Mail sent by a woken session: its job's hop + 1 (§8.8); 0 otherwise."""
        with self.store._lock:
            row = self._db().execute(
                "SELECT hop FROM wake_jobs WHERE address = ? AND finished_at IS NULL "
                "AND state IN ('dispatched', 'running', 'acked') ORDER BY created_at DESC LIMIT 1",
                (address,)).fetchone()
        return row["hop"] + 1 if row else 0

    def _wakes_last_hour(self, address: str) -> int:
        with self.store._lock:
            return self._db().execute("SELECT COUNT(*) FROM events WHERE type = 'wake_dispatched' AND address = ? "
                                      "AND at >= ?", (address, self.clock() - 3600)).fetchone()[0]

    def route(self, message_id: str, row: dict, *, wake: str = "auto", hop: int = 0, force: bool = False) -> str:
        """The delivery action for one recipient (§8.7); queues or merges a wake job."""
        if not self.cfg["enabled"] or wake == "never" or row.get("wake_policy") == "never" or row.get("ended_at"):
            return "noticed_on_next_call"
        limit = self.mailbox.config["limits"]["max_thread_hops"]
        if limit is not None and hop >= int(limit):
            self.mailbox.event("wake_hop_limited", row["address"], row["agent_id"], message_id=message_id, hop=hop)
            return "noticed_on_next_call"
        adapter = self.adapter_for(row)
        if adapter is None or not self.has_route(row, adapter):
            return "noticed_on_next_call"
        active = self._active(row["address"])
        if active is not None:
            if message_id not in active["message_ids"]:
                self._set(active["id"], message_ids=[*active["message_ids"], message_id], hop=max(active["hop"], hop))
            return "wake_waiting_device" if active["state"] == "waiting_device" else "wake_queued"
        if not force and wake != "always" and self.mailbox.presence(row) == "active":
            return "noticed_on_next_call"
        per_hour = self.mailbox.config["limits"]["max_wakes_per_hour"]
        if per_hour is not None and self._wakes_last_hour(row["address"]) >= int(per_hour):
            return "noticed_on_next_call"
        return self.create(row, [message_id], adapter=adapter, hop=hop)

    def create(self, row: dict, message_ids: list[str], *, adapter: str, hop: int = 0,
               debounce: float | None = None) -> str:
        now = self.clock()
        peer = self.hub.peers.get(row["agent_id"])
        if peer is not None and SESSION_WAKE not in peer.features and not (
                self.cfg["fallback_exec"] and adapter in PROCESS_ADAPTERS):
            state, action = "unsupported", "wake_unsupported"
        elif peer is None:
            state, action = "waiting_device", "wake_waiting_device"
        else:
            state, action = "queued", "wake_queued"
        delay = float(self.cfg["debounce_s"] if debounce is None else debounce)
        cooldown = self.cfg["cooldown_s"]
        if cooldown:
            last = self.last_wake(row["address"])
            if last and last.get("at") and now - last["at"] < float(cooldown):
                delay = max(delay, float(cooldown) - (now - last["at"]))
        job_id = "w_" + uuid4().hex[:20]
        final = state == "unsupported"
        try:
            with self.store._lock:
                self._db().execute(
                    "INSERT INTO wake_jobs(id, address, agent_id, adapter, state, message_ids, next_at, created_at, "
                    "expires_at, hop, permission, finished_at, last_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (job_id, row["address"], row["agent_id"], adapter, state, json.dumps(message_ids), now + delay,
                     now, now + float(self.cfg["expire_s"]), hop, self.permission(row), now if final else None,
                     "the device does not support session_wake (legacy agent)" if final else None))
        except Exception as exc:  # the unique index: another job became active in between
            if "UNIQUE" not in str(exc):
                raise
            active = self._active(row["address"])
            if active is not None:
                self._set(active["id"], message_ids=list(dict.fromkeys([*active["message_ids"], *message_ids])))
            return "wake_queued"
        self.mailbox.event("wake_" + state, row["address"], row["agent_id"], job_id=job_id, adapter=adapter)
        if not final:
            self.kick()
        return action

    def drop_message(self, address: str, message_id: str, *, reason: str) -> None:
        """Recalled or expired mail leaves the pending jobs; an emptied job is skipped."""
        with self.store._lock:
            rows = self._db().execute("SELECT * FROM wake_jobs WHERE address = ? AND state IN ('queued', 'waiting_device')",
                                      (address,)).fetchall()
        for row in rows:
            ids = _ids(row["message_ids"])
            if message_id not in ids:
                continue
            ids.remove(message_id)
            if ids:
                self._set(row["id"], message_ids=ids)
            else:
                self._set(row["id"], message_ids=ids, state="skipped", reason=reason, finished_at=self.clock())

    # -- status views ------------------------------------------------------------------
    def last_wake(self, address: str) -> dict | None:
        with self.store._lock:
            row = self._db().execute("SELECT * FROM wake_jobs WHERE address = ? ORDER BY created_at DESC, id DESC "
                                     "LIMIT 1", (address,)).fetchone()
        if row is None:
            return None
        return {"job_id": row["id"], "state": row["state"], "adapter": row["adapter"],
                "at": row["dispatched_at"] or row["created_at"], "attempts": row["attempts"],
                "error": row["last_error"]}

    def wake_for(self, address: str, message_id: str) -> dict | None:
        """The latest wake job of ``address`` that carried ``message_id`` (``mail_status``)."""
        with self.store._lock:
            rows = self._db().execute("SELECT * FROM wake_jobs WHERE address = ? AND message_ids LIKE ? "
                                      "ORDER BY created_at DESC", (address, f'%"{message_id}"%')).fetchall()
        for row in rows:
            if message_id in _ids(row["message_ids"]):
                return {"job_id": row["id"], "state": row["state"], "at": row["dispatched_at"] or row["created_at"]}
        return None

    # -- acknowledgement and reports --------------------------------------------------------
    def ack(self, address: str) -> None:
        """The recipient's next tool call after a dispatch acknowledges the wake (§8.7)."""
        now = self.clock()
        with self.store._lock:
            db = self._db()
            rows = db.execute("SELECT id, adapter FROM wake_jobs WHERE address = ? AND state IN ('dispatched', 'running') "
                              "AND dispatched_at IS NOT NULL AND dispatched_at < ?", (address, now)).fetchall()
            for row in rows:
                if row["adapter"] in PROCESS_ADAPTERS:
                    db.execute("UPDATE wake_jobs SET state = 'acked', acked_at = ? WHERE id = ?", (now, row["id"]))
                else:  # nothing will report: acknowledged is done
                    db.execute("UPDATE wake_jobs SET state = 'acked', acked_at = ?, finished_at = ? WHERE id = ?",
                               (now, now, row["id"]))
        for row in rows:
            self.mailbox.event("wake_acked", address, None, job_id=row["id"])

    def report(self, agent_id: str, args: dict) -> dict:
        """``hub_call wake_report {job_id, state, exit_code}`` from the device that ran the job."""
        job = self.job(str(args.get("job_id") or ""))
        if job is None or job["agent_id"] != agent_id:
            raise RoutingError("unknown wake job")
        state = args.get("state")
        exit_code = args.get("exit_code")
        if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
            exit_code = None
        now = self.clock()
        if state == "running":
            if job["state"] == "dispatched":
                self._set(job["id"], state="running", pid=args.get("pid") if isinstance(args.get("pid"), int) else None)
            return {"job_id": job["id"], "state": self.job(job["id"])["state"]}
        if state not in ("done", "failed"):
            raise RoutingError("wake_report state must be running, done or failed")
        if job["finished_at"] is not None and job["state"] in ("done", "failed"):
            return {"job_id": job["id"], "state": job["state"]}  # a repeated report
        final = "done" if state == "done" and exit_code in (0, None) else "failed"
        error = args.get("error") if isinstance(args.get("error"), str) else None
        self._set(job["id"], state=final, exit_code=exit_code, finished_at=now,
                  last_error=(error or (f"exit code {exit_code}" if final == "failed" else None)))
        self.mailbox.event("wake_" + final, job["address"], agent_id, job_id=job["id"], exit_code=exit_code)
        self._rerun(job)
        return {"job_id": job["id"], "state": final}

    def _rerun(self, job: dict) -> None:
        """Mail that arrived after the run started gets a second wake (§8.7)."""
        row = self.mailbox.address_row(job["address"])
        if row is None or row.get("ended_at") or row.get("wake_policy") == "never":
            return
        since = job["started_at"] or job["dispatched_at"] or job["created_at"]
        with self.store._lock:
            rows = self._db().execute(
                "SELECT m.id FROM deliveries d JOIN messages m ON m.id = d.message_id WHERE d.to_addr = ? "
                "AND d.state = 'unread' AND m.created_at > ? ORDER BY m.id", (job["address"], since)).fetchall()
        if rows and self._active(job["address"]) is None:
            adapter = self.adapter_for(row)
            if adapter is not None and self.has_route(row, adapter):
                self.create(row, [r["id"] for r in rows], adapter=adapter, hop=job["hop"],
                            debounce=float(self.cfg["rerun_debounce_s"]))

    # -- the dispatcher ------------------------------------------------------------------------
    def device_online(self, agent_id: str) -> None:
        """``on_device_online``: the device's waiting jobs are due now (§8.7 offline queue)."""
        now = self.clock()
        with self.store._lock:
            changed = self._db().execute("UPDATE wake_jobs SET state = 'queued', next_at = MIN(next_at, ?) "
                                         "WHERE agent_id = ? AND state = 'waiting_device'", (now, agent_id)).rowcount
        if changed:
            self.kick()

    def recover(self) -> None:
        """At gateway start: an interrupted probe is queued again (§8.7 "网关重启")."""
        with self.store._lock:
            self._db().execute("UPDATE wake_jobs SET state = 'queued' WHERE state = 'probing'")

    async def run(self) -> None:
        self._kick = asyncio.Event()
        self.recover()
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("wake dispatcher failed (%s: %s)", type(exc).__name__, exc)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._kick.wait(), float(self.cfg["scan_interval_s"]) or 2.0)
            self._kick.clear()

    async def tick(self) -> None:
        now = self.clock()
        await self._timeouts(now)
        with self.store._lock:
            due = [self._view(row) for row in self._db().execute(
                "SELECT * FROM wake_jobs WHERE state = 'queued' AND next_at <= ? ORDER BY next_at", (now,)).fetchall()]
            waiting = self._db().execute("SELECT id FROM wake_jobs WHERE state = 'waiting_device' AND expires_at < ?",
                                         (now,)).fetchall()
        for row in waiting:
            self._set(row["id"], state="expired", finished_at=now, reason="the device stayed offline")
        for job in due:
            await self._dispatch(job)

    async def _timeouts(self, now: float) -> None:
        ack_timeout = float(self.cfg["ack_timeout_s"])
        run_timeout = float(self.cfg["run_timeout_s"])
        with self.store._lock:
            unacked = [self._view(r) for r in self._db().execute(
                "SELECT * FROM wake_jobs WHERE state IN ('dispatched', 'running') AND dispatched_at < ?",
                (now - ack_timeout,)).fetchall()]
            unreported = [self._view(r) for r in self._db().execute(
                "SELECT * FROM wake_jobs WHERE state = 'acked' AND finished_at IS NULL AND acked_at < ?",
                (now - run_timeout,)).fetchall()]
        for job in unacked:
            self.mailbox.event("wake_ack_timeout", job["address"], job["agent_id"], job_id=job["id"])
            self._retry(job, f"not acknowledged within {int(ack_timeout)} s")
        for job in unreported:
            peer = self.hub.peers.get(job["agent_id"])
            if peer is not None and SESSION_PROBE in peer.features:
                probe = await self._probe(peer, job)
                if probe.get("live"):
                    self._set(job["id"], acked_at=now)  # still running: check again later
                    continue
            self._set(job["id"], state="done", finished_at=now, reason="no wake_report within run_timeout_s")

    def _retry(self, job: dict, error: str) -> None:
        attempts = job["attempts"] + 1
        now = self.clock()
        if attempts >= int(self.cfg["max_attempts"]):
            self._set(job["id"], state="failed", attempts=attempts, last_error=error[:500], finished_at=now)
            self.mailbox.event("wake_failed", job["address"], job["agent_id"], job_id=job["id"], error=error[:200])
            self._failure_mail(job, error)
            return
        delay = BACKOFF_S[min(attempts - 1, len(BACKOFF_S) - 1)]
        self._set(job["id"], state="queued", attempts=attempts, last_error=error[:500], next_at=now + delay,
                  dispatched_at=None)

    def _failure_mail(self, job: dict, error: str) -> None:
        """After max_attempts the senders get a system mail (§8.7)."""
        from .mailbox import ANON_SENDER, SYSTEM_SENDER
        with self.store._lock:
            rows = self._db().execute("SELECT id, from_addr, subject, thread_id FROM messages WHERE id IN (%s)"
                                      % ",".join("?" * len(job["message_ids"])), job["message_ids"]).fetchall() \
                if job["message_ids"] else []
        senders: dict[str, list] = {}
        for row in rows:
            if row["from_addr"] not in (ANON_SENDER, SYSTEM_SENDER):
                senders.setdefault(row["from_addr"], []).append(row)
        target = self.mailbox.display(job["address"])
        for sender, items in senders.items():
            subjects = "、".join(f"「{item['subject'] or item['id']}」" for item in items[:5])
            with contextlib.suppress(RoutingError):
                sent = self.mailbox.send(SYSTEM_SENDER, sender,
                                         f"发给 {target} 的邮件 {subjects} 唤醒失败（已尝试 {job['attempts'] + 1} 次）："
                                         f"{error}。邮件仍在对方邮箱里，对方下次调用工具时会看到提醒。",
                                         subject=f"唤醒 {target} 失败", reply_to=items[-1]["id"], kind="system",
                                         meta={"wake_job": job["id"], "state": "failed"}, wake="never")
                self.gateway.mail_sent(sent)

    async def _probe(self, peer, job: dict) -> dict:
        row = self.mailbox.address_row(job["address"]) or {}
        try:
            result = await asyncio.wait_for(peer.call("session_probe", {
                "job_id": job["id"], "kind": row.get("kind"), "resume_id": row.get("resume_id"),
                "workdir": row.get("workdir")}), float(self.cfg["probe_timeout_s"]))
        except Exception as exc:
            log.info("session_probe on %s failed (%s)", job["agent_id"], type(exc).__name__)
            return {}
        return result if isinstance(result, dict) else {}

    def _handle(self, row: dict, peer) -> str:
        """The address's handle, renewed; a new one when it expired (§8.7 "句柄在此时续期")."""
        handle = row.get("last_handle")
        if handle and self.store.touch_handle(handle) is not None:
            return handle
        workspaces = peer.catalog.get("workspaces", {}) if peer is not None else {}
        workspace = next((name for name, item in workspaces.items() if item.get("path") == row.get("workdir")),
                         next(iter(workspaces), "default"))
        handle = uuid4().hex
        self.store.put_handle(handle, row["agent_id"], workspace, client_id=row.get("oauth_client_id"),
                              meta={"session_name": row["name"], "kind": row["kind"], "woken": True})
        with self.store._lock:
            self._db().execute("UPDATE handles SET address = ? WHERE handle = ?", (row["address"], handle))
            self._db().execute("UPDATE addresses SET last_handle = ? WHERE address = ?", (handle, row["address"]))
        return handle

    def prompt(self, row: dict, handle: str, message_ids: list[str]) -> str:
        with self.store._lock:
            latest = self._db().execute(
                "SELECT m.from_addr, m.subject, m.body FROM deliveries d JOIN messages m ON m.id = d.message_id "
                "WHERE d.to_addr = ? AND d.state = 'unread' ORDER BY m.id DESC LIMIT 1", (row["address"],)).fetchone()
        unread = self.mailbox.unread_count(row["address"])
        subject = (latest["subject"] or " ".join(latest["body"].split())[:40]) if latest else ""
        return PROMPT.format(address=self.mailbox.display(row["address"]), n=max(unread, 1),
                             sender=self.mailbox.display(latest["from_addr"]) if latest else "?",
                             subject=subject, handle=handle)

    async def _dispatch(self, job: dict) -> None:
        now = self.clock()
        if now > job["expires_at"]:
            self._set(job["id"], state="expired", finished_at=now, reason="expire_s passed")
            return
        row = self.mailbox.address_row(job["address"])
        if row is None or row.get("ended_at") or row.get("wake_policy") == "never":
            self._set(job["id"], state="skipped", finished_at=now, reason="the address ended or does not wake")
            return
        if not self.has_route(row, job["adapter"]):
            # Checked when the job was made, but the row may have changed since (a takeover of an
            # automatic address clears its resume id: "--continue" would resume whichever session
            # ran last in the folder, with the taker's handle in the prompt).
            self._set(job["id"], state="skipped", finished_at=now, reason="no route")
            self.mailbox.event("wake_no_route", job["address"], job["agent_id"], job_id=job["id"])
            return
        if not job["message_ids"] or self.mailbox.unread_count(job["address"]) == 0:
            self._set(job["id"], state="skipped", finished_at=now, reason="nothing unread any more")
            return
        peer = self.hub.peers.get(job["agent_id"])
        if peer is None:
            self._set(job["id"], state="waiting_device")
            return
        if SESSION_WAKE not in peer.features:
            if self.cfg["fallback_exec"] and job["adapter"] in PROCESS_ADAPTERS:
                await self._fallback_exec(job, row, peer)
            else:
                self._set(job["id"], state="unsupported", finished_at=now,
                          last_error="the device does not support session_wake (legacy agent)")
                self.mailbox.event("wake_unsupported", job["address"], job["agent_id"], job_id=job["id"])
            return
        if job["adapter"] in PROCESS_ADAPTERS and SESSION_PROBE in peer.features and self.cfg["probe_live"]:
            self._set(job["id"], state="probing")
            probe = await self._probe(peer, job)
            if probe.get("live"):
                # Never fork a session in interactive use; it sees the notice on its next call.
                self._set(job["id"], state="skipped_active", finished_at=self.clock(),
                          reason=str(probe.get("reason") or "the session is in use")[:200])
                self.mailbox.event("wake_skipped_active", job["address"], job["agent_id"], job_id=job["id"])
                return
        job = self.job(job["id"]) or job  # mail may have merged in meanwhile
        row = self.mailbox.address_row(job["address"])  # and the address may have changed hands
        if row is None or row.get("ended_at") or not self.has_route(row, job["adapter"]):
            self._set(job["id"], state="skipped", finished_at=self.clock(), reason="no route")
            return
        handle = self._handle(row, peer)
        params = {"job_id": job["id"], "adapter": job["adapter"], "kind": row["kind"], "address": row["address"],
                  "address_display": self.mailbox.display(row["address"]), "resume_id": row.get("resume_id"),
                  "workdir": row.get("workdir"), "companion_route": row.get("companion_route"),
                  "permission": job["permission"] or self.permission(row), "handle": handle,
                  "prompt": self.prompt(row, handle, job["message_ids"]), "message_ids": job["message_ids"]}
        try:
            result = await asyncio.wait_for(peer.call("session_wake", params), WAKE_RPC_TIMEOUT_S)
        except Exception as exc:
            result = {"accepted": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        if not isinstance(result, dict):
            result = {"accepted": False, "error": "invalid session_wake reply"}
        if result.get("accepted"):
            self._dispatched(job, result)
            return
        error = str(result.get("error") or "the device refused the wake")
        self.mailbox.event("wake_refused", job["address"], job["agent_id"], job_id=job["id"], error=error[:200])
        if job["adapter"] == "companion" and job["id"] not in self._nudged:
            # chatx is not running (§8.7): one notice on that device per job.
            self._nudged.add(job["id"])
            self.gateway.notify_device(peer, {
                "tag": f"anx-wake-{job['id']}", "title": "有发给 GPT 的邮件",
                "body": f"{self.mailbox.display(row['address'])} 有新邮件，请打开 GPT Companion。",
                "link": "#/inbox", "level": "info", "actions": []})
        self._retry(self.job(job["id"]) or job, error)

    def _dispatched(self, job: dict, result: dict) -> None:
        now = self.clock()
        pid = result.get("pid") if isinstance(result.get("pid"), int) and not isinstance(result.get("pid"), bool) else None
        self._set(job["id"], state="running" if pid else "dispatched", dispatched_at=now, started_at=now, pid=pid,
                  last_error=None)
        with self.store._lock:
            self._db().executemany("UPDATE deliveries SET woken_at = ? WHERE message_id = ? AND to_addr = ? "
                                   "AND woken_at IS NULL", [(now, mid, job["address"]) for mid in job["message_ids"]])
        self.mailbox.event("wake_dispatched", job["address"], job["agent_id"], job_id=job["id"], adapter=job["adapter"])

    async def _fallback_exec(self, job: dict, row: dict, peer) -> None:
        """``wake.fallback_exec``: the command through the legacy device plugin's exec_command (its policy applies)."""
        name = next((mcp for mcp in ("device", "workspace") if mcp in peer.catalog.get("mcps", {})), None)
        tools = {tool.get("name") for tool in peer.catalog.get("mcps", {}).get(name, {}).get("tools", [])} if name else set()
        if "exec_command" not in tools:
            self._set(job["id"], state="unsupported", finished_at=self.clock(),
                      last_error="legacy device without session_wake or exec_command")
            return
        if row.get("resume_id") and not valid_resume_id(row["resume_id"]):
            self._set(job["id"], state="failed", finished_at=self.clock(), last_error=RESUME_ID_RULE)
            return
        handle = self._handle(row, peer)
        prompt = " ".join(self.prompt(row, handle, job["message_ids"]).split())
        template = FALLBACK_COMMANDS[job["adapter"]]["resume" if row.get("resume_id") else "last"]
        argv = [part.replace("{resume_id}", row.get("resume_id") or "") for part in template]
        argv += FALLBACK_FLAGS[job["adapter"]].get(job["permission"] or self.permission(row), []) + [prompt]
        platform = ((peer.catalog.get("device") or {}).get("platform") if isinstance(peer.catalog.get("device"), dict)
                    else None) or "windows"
        if platform == "windows":
            # The device shell (cmd.exe or PowerShell) expands these even inside double quotes,
            # and list2cmdline's \" does not close cmd's quoting: the subject is the sender's text.
            argv[-1] = re.sub(r'["%!$`]', "", argv[-1])
            command = subprocess.list2cmdline(argv)
        else:
            command = shlex.join(argv)
        try:
            result = await asyncio.wait_for(peer.call("tool_call", {
                "mcp": name, "tool": "exec_command", "scope": f"wake-{job['id']}",
                "arguments": {"cmd": command, "workdir": row.get("workdir"), "yield_time_ms": 1000}}),
                WAKE_RPC_TIMEOUT_S)
        except Exception as exc:
            self._retry(job, f"fallback exec failed: {type(exc).__name__}")
            return
        if isinstance(result, dict) and result.get("isError"):
            self._retry(job, "fallback exec returned an error")
            return
        # Nothing reports a fallback run: acknowledged means done.
        self._dispatched({**job, "adapter": "fallback"}, {})
        self._set(job["id"], adapter="fallback")
