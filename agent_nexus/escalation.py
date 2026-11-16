"""Unread escalation (decision D9, design §8.11).

Every ``escalation.scan_interval_s`` the gateway looks at unread deliveries and
ages them by the message's ``created_at`` (the store's clock, so tests move time):

1. ``rewake_after_s`` (1 h): wake the recipient once more, even when it is
   active (``wake=always``); skipped for ``wake_policy=never`` and humans.
2. ``notify_owner_after_s`` (24 h): one system mail to ``me`` per recipient
   and scan, listing the messages (not for mail to ``me`` itself).
3. ``expire_after_days`` (30 d): the delivery becomes ``expired``, pending
   wake jobs forget it, and the sender gets a system mail.

``escalation_level`` records the last level done, so each level runs once and
a restarted gateway continues where it stopped. A threshold of ``null`` turns
that level off. Reading or recalling the mail ends its escalation (only
``unread`` deliveries are scanned).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from .federation import RoutingError
from .mailbox import ANON_SENDER, DEVICE_ASK_NAME, ME, SYSTEM_SENDER

if TYPE_CHECKING:
    from .gateway import Gateway

log = logging.getLogger(__name__)


class Escalation:
    def __init__(self, gateway: "Gateway"):
        self.gateway = gateway
        self.mailbox = gateway.mailbox
        self.store = gateway.store

    @property
    def cfg(self) -> dict:
        return self.mailbox.config["escalation"]

    def scan(self, now: float | None = None) -> dict[str, int]:
        cfg = self.cfg
        done = {"rewake": 0, "notify_owner": 0, "expired": 0}
        if not cfg["enabled"]:
            return done
        now = self.store.clock() if now is None else now
        rewake, owner, expire_days = cfg["rewake_after_s"], cfg["notify_owner_after_s"], cfg["expire_after_days"]
        thresholds = [value for value in (rewake, owner) if value is not None]
        if expire_days is not None:
            thresholds.append(float(expire_days) * 86400)
        if not thresholds:
            return done
        with self.store._lock:
            rows = [dict(row) for row in self.store._connect().execute(
                "SELECT d.message_id, d.to_addr, d.escalation_level, m.created_at, m.from_addr, m.subject, m.body, "
                "m.thread_id FROM deliveries d JOIN messages m ON m.id = d.message_id "
                "WHERE d.state = 'unread' AND d.escalation_level < 3 AND m.created_at <= ? ORDER BY m.id",
                (now - min(thresholds),)).fetchall()]
        owner_digest: dict[str, list[dict]] = {}
        for row in rows:
            to_addr = row["to_addr"]
            if to_addr.rsplit("@", 1)[0] == DEVICE_ASK_NAME:
                continue  # nobody reads device-ask mail (the device polls ask_get)
            age = now - row["created_at"]
            level = row["escalation_level"]
            new_level = level
            if expire_days is not None and age >= float(expire_days) * 86400:
                self._expire(row, now)
                done["expired"] += 1
                continue
            if rewake is not None and age >= float(rewake) and level < 1 and to_addr != ME:
                self._rewake(row)
                done["rewake"] += 1
                new_level = 1
            if owner is not None and age >= float(owner) and level < 2 and to_addr != ME:
                owner_digest.setdefault(to_addr, []).append(row)
                new_level = 2
            if new_level != level:
                self._level(row, new_level)
        for to_addr, items in owner_digest.items():
            self._tell_owner(to_addr, items)
            done["notify_owner"] += 1
        return done

    def _level(self, row: dict, level: int, **extra: Any) -> None:
        sets = ", ".join(["escalation_level = ?", *(f"{key} = ?" for key in extra)])
        with self.store._lock:
            self.store._connect().execute(
                f"UPDATE deliveries SET {sets} WHERE message_id = ? AND to_addr = ? AND state = 'unread'",
                (level, *extra.values(), row["message_id"], row["to_addr"]))

    def _rewake(self, row: dict) -> None:
        address = self.mailbox.address_row(row["to_addr"])
        if address is None or address["kind"] == "human" or address["wake_policy"] == "never":
            return
        action = self.mailbox.router.route(row["message_id"], address, wake="always", force=True) \
            if self.mailbox.router is not None else "noticed_on_next_call"
        self.mailbox.event("escalation_rewake", row["to_addr"], address["agent_id"],
                           message_id=row["message_id"], action=action)

    def _tell_owner(self, to_addr: str, items: list[dict]) -> None:
        target = self.mailbox.display(to_addr)
        hours = max(1, int((self.cfg["notify_owner_after_s"] or 0) // 3600))
        lines = [f"发给 {target} 的 {len(items)} 封邮件已超过 {hours} 小时未读：" if len(items) > 1
                 else f"发给 {target} 的邮件已超过 {hours} 小时未读："]
        for item in items[:20]:
            subject = item["subject"] or " ".join(item["body"].split())[:40]
            lines.append(f"- 「{subject}」来自 {self.mailbox.display(item['from_addr'])}（{item['message_id']}，"
                         f"线程 #/inbox/{item['thread_id']}）")
        if len(items) > 20:
            lines.append(f"- 另有 {len(items) - 20} 封")
        with contextlib.suppress(RoutingError):
            sent = self.mailbox.send(SYSTEM_SENDER, ME, "\n".join(lines), subject=f"{target} 有邮件长时间未读",
                                     kind="system", meta={"escalation": "notify_owner", "to": to_addr,
                                                          "message_ids": [item["message_id"] for item in items]})
            self.gateway.mail_sent(sent)
        self.mailbox.event("escalation_notify_owner", to_addr, None, messages=len(items))

    def _expire(self, row: dict, now: float) -> None:
        with self.store._lock:
            changed = self.store._connect().execute(
                "UPDATE deliveries SET state = 'expired', expired_at = ?, escalation_level = 3 "
                "WHERE message_id = ? AND to_addr = ? AND state = 'unread'",
                (now, row["message_id"], row["to_addr"])).rowcount
        if not changed:
            return
        if self.mailbox.router is not None:
            self.mailbox.router.drop_message(row["to_addr"], row["message_id"], reason="expired")
        self.mailbox.event("escalation_expired", row["to_addr"], None, message_id=row["message_id"])
        sender = row["from_addr"]
        if not self.cfg["notify_sender_on_expire"] or sender in (ANON_SENDER, SYSTEM_SENDER) \
                or sender == row["to_addr"] or sender.rsplit("@", 1)[0] == DEVICE_ASK_NAME:
            return
        subject = row["subject"] or " ".join(row["body"].split())[:40]
        with contextlib.suppress(RoutingError):
            sent = self.mailbox.send(SYSTEM_SENDER, sender,
                                     f"你发给 {self.mailbox.display(row['to_addr'])} 的「{subject}」已过期未读"
                                     f"（{row['message_id']}），对方邮箱里不再提醒。",
                                     subject="邮件已过期未读", reply_to=row["message_id"], kind="system",
                                     meta={"escalation": "expired", "message_id": row["message_id"],
                                           "to": row["to_addr"]})
            self.gateway.mail_sent(sent)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(float(self.cfg["scan_interval_s"]) or 60.0)
            try:
                # On the loop, not in a thread: it queues wake jobs and sends toasts.
                done = self.scan()
                if any(done.values()):
                    log.info("mail escalation: %s", done)
            except Exception as exc:
                log.warning("mail escalation failed (%s)", type(exc).__name__)
