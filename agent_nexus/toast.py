"""Device-side toasts for the owner's inbox (decision D11, design §8.12).

The gateway sends ``toast {tag, title, body, link, thread_id, ask_id?, actions[]}``
and ``toast_dismiss {tag}`` reverse RPCs to devices announcing
``toast_actions/1``. :class:`Toaster` is the interface the agent hands them to:

* :class:`DeviceNotifyToaster` (the default) shows them through the device
  plugin's ``notify`` tool -- a Windows toast there, ``notify-send`` on Linux --
  with every button turned into a link to this machine's console: "open" goes
  to ``/#/inbox/<thread>``, an answer button to ``/toast/answer`` with an HMAC
  signature, so clicking it answers the question (``hub_call ask_answer``,
  ``via: "toast"``) without a cross-site request being able to forge one;
* :class:`FakeToaster` records calls for tests (no real notification is ever
  shown by the test suite).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlencode

SECRET_NAME = "toast-link.key"
MAX_BUTTONS = 5  # the device plugin's notify accepts at most five actions


class Toaster(Protocol):
    async def show(self, toast: dict) -> dict: ...

    async def dismiss(self, tag: str) -> dict: ...


class FakeToaster:
    """Records toasts instead of showing them."""

    def __init__(self) -> None:
        self.shown: list[dict] = []
        self.dismissed: list[str] = []

    async def show(self, toast: dict) -> dict:
        self.shown.append(dict(toast))
        return {"shown": True, "backend": "fake"}

    async def dismiss(self, tag: str) -> dict:
        self.dismissed.append(tag)
        return {"dismissed": True}


def link_secret(state_dir: Path) -> bytes:
    """The per-root key that signs toast answer links (shared by the agent and the console)."""
    path = Path(state_dir) / SECRET_NAME
    try:
        value = path.read_bytes()
        if len(value) >= 32:
            return value
    except OSError:
        pass
    value = secrets.token_bytes(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
    except OSError:
        pass  # unsigned-in-practice: the console then refuses the link and shows the inbox
    return value


def sign(secret: bytes, ask_id: str, choice: str) -> str:
    return hmac.new(secret, f"{ask_id}\n{choice}".encode(), hashlib.sha256).hexdigest()[:32]


def verify(secret: bytes, ask_id: str, choice: str, signature: str) -> bool:
    return hmac.compare_digest(sign(secret, ask_id, choice), signature or "")


def answer_link(console_url: str, secret: bytes, ask_id: str, choice: str) -> str:
    query = urlencode({"ask": ask_id, "choice": choice, "sig": sign(secret, ask_id, choice)})
    return f"{console_url.rstrip('/')}/toast/answer?{query}"


def console_link(console_url: str, link: Any) -> str:
    fragment = link if isinstance(link, str) and link.startswith("#/inbox") else "#/inbox"
    return console_url.rstrip("/") + "/" + quote(fragment, safe="#/_-.")


def notify_arguments(toast: dict, console_url: str, secret: bytes) -> dict:
    """The device plugin ``notify`` call for a gateway toast."""
    actions = []
    for action in (toast.get("actions") or [])[:MAX_BUTTONS - 1]:
        if not isinstance(action, dict) or not isinstance(action.get("label"), str):
            continue
        if action.get("ask_id") and isinstance(action.get("choice"), str):
            url = answer_link(console_url, secret, str(action["ask_id"]), action["choice"])
        else:
            url = console_link(console_url, action.get("link") or toast.get("link"))
        actions.append({"id": str(action.get("id") or f"a{len(actions)}")[:64], "label": action["label"][:60],
                        "link": url})
    if not any(action["id"] == "open" for action in actions):
        actions.append({"id": "open", "label": "打开收件箱", "link": console_link(console_url, toast.get("link"))})
    level = toast.get("level") if toast.get("level") in ("info", "warn", "urgent") else "info"
    return {"title": str(toast.get("title") or "agent-nexus")[:256], "body": str(toast.get("body") or "")[:4000],
            "level": level, "actions": actions}


class DeviceNotifyToaster:
    """Shows toasts through the local device plugin's ``notify`` tool."""

    def __init__(self, gateway, console_url: str, state_dir: Path):
        self.gateway = gateway
        self.console_url = console_url
        self.state_dir = Path(state_dir)

    async def show(self, toast: dict) -> dict:
        name = self.gateway.resolve_mcp("device")
        if name not in self.gateway.targets:
            return {"shown": False, "reason": "no device plugin on this machine"}
        arguments = notify_arguments(toast, self.console_url, link_secret(self.state_dir))
        client = await self.gateway.client(name, "toast")
        result = await client.call_tool_mcp("notify", arguments)
        payload = result.structured_content if isinstance(result.structured_content, dict) else {}
        return {"shown": bool(payload.get("shown")) and not result.is_error, "backend": payload.get("backend")}

    async def dismiss(self, tag: str) -> dict:
        # The device plugin's notify has no dismissal yet; the toast simply expires.
        return {"dismissed": False, "reason": "unsupported by this device's notify backend"}
