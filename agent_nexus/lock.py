"""The client's 锁定 (private-network model).

Every device that joined the gateway may operate on the others: list, revoke, delete, open a
remote terminal or desktop. Locking this client stops it from operating on other devices;
it can still be operated on. Unlocking restores it.

A lock password is optional and never asked for: without one, the lock button just toggles.
With one, unlocking needs it. It is kept as a salted PBKDF2 hash in ``<root>/.anx/lock.json``
(owner-only), so it guards the client's UI and CLI, not against someone who can rewrite this
account's files.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

from . import paths

ITERATIONS = 200_000
MIN_PASSWORD = 4
MAX_PASSWORD = 128
#: wrong unlock passwords allowed per minute
MAX_ATTEMPTS = 5


class LockError(ValueError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def body(self) -> dict:
        return {"error": self.message, "code": self.code}


def _hash(password: str, salt: bytes, iterations: int = ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations).hex()


class Lock:
    def __init__(self, root: Path):
        self.path = Path(root) / paths.STATE_DIR_NAME / "lock.json"
        self._guard = threading.Lock()
        self._failures: list[float] = []

    # -- storage
    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"locked": False, "password": None}
        return data if isinstance(data, dict) else {"locked": False, "password": None}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        temporary.replace(self.path)

    # -- state
    @property
    def locked(self) -> bool:
        return bool(self._read().get("locked"))

    def state(self) -> dict:
        data = self._read()
        return {"locked": bool(data.get("locked")), "has_password": isinstance(data.get("password"), dict)}

    def _check(self, data: dict, password: str | None) -> None:
        stored = data.get("password")
        if not isinstance(stored, dict):
            return
        now = time.monotonic()
        self._failures = [t for t in self._failures if now - t < 60]
        if len(self._failures) >= MAX_ATTEMPTS:
            raise LockError(429, "too_many_attempts", "密码错误次数过多，请 1 分钟后再试")
        try:
            salt = bytes.fromhex(stored["salt"])
            expected = stored["hash"]
            iterations = int(stored.get("iterations", ITERATIONS))
        except (KeyError, TypeError, ValueError):
            raise LockError(500, "corrupt", "锁定密码记录已损坏；删除 .anx/lock.json 可清除") from None
        if not password or not hmac.compare_digest(_hash(password, salt, iterations), expected):
            self._failures.append(now)
            raise LockError(403, "wrong_password", "锁定密码不正确")

    def lock(self) -> dict:
        with self._guard:
            data = self._read()
            data["locked"] = True
            self._write(data)
        return self.state()

    def unlock(self, password: str | None = None) -> dict:
        with self._guard:
            data = self._read()
            self._check(data, password)
            data["locked"] = False
            self._write(data)
        return self.state()

    def set_password(self, new: str | None, current: str | None = None) -> dict:
        """Set, change (``current`` needed) or remove (``new`` None) the lock password."""
        with self._guard:
            data = self._read()
            self._check(data, current)
            if new is None or new == "":
                data["password"] = None
            else:
                if not MIN_PASSWORD <= len(new) <= MAX_PASSWORD:
                    raise LockError(400, "invalid_password", f"锁定密码需要 {MIN_PASSWORD}-{MAX_PASSWORD} 个字符")
                salt = secrets.token_bytes(16)
                data["password"] = {"salt": salt.hex(), "hash": _hash(new, salt), "iterations": ITERATIONS}
            self._write(data)
        return self.state()

    def require_unlocked(self) -> None:
        if self.locked:
            raise LockError(423, "locked", "这台设备已锁定，解锁后才能操作其他设备")
