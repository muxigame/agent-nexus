"""request_user_input through agent-nexus (decision D11, design §8.12).

When the device plugin runs under an agent-nexus manager (``ANX_MANAGER_URL``,
set by the supervisor) whose gateway announced ``ask/1``, questions become
gateway asks: they appear in the owner's inbox (console, desktop, phone PWA)
and as toasts, and are answered from any of them. Without a manager, or when
the gateway is unreachable, the tools fall back to the local chatx window as
before. Tool names, parameters and result shapes do not change.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote, urlsplit

TIMEOUT_S = 10.0


class ManagerAsks:
    def __init__(self, url: str):
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("ANX_MANAGER_URL must be the local manager's http://127.0.0.1:<port>")
        self.url = url.rstrip("/")

    @classmethod
    def from_env(cls) -> "ManagerAsks | None":
        url = os.environ.get("ANX_MANAGER_URL", "").strip()
        if not url:
            return None
        try:
            return cls(url)
        except ValueError:
            return None

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"X-ANX": "1", "Content-Type": "application/json"})
        # The manager is on loopback: never go through a proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=TIMEOUT_S) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read() or b"{}").get("error")
            except ValueError:
                message = None
            raise RuntimeError(message or f"manager answered HTTP {exc.code}") from None

    def available(self) -> bool:
        try:
            return bool(self._request("GET", "/api/asks/status").get("available"))
        except (OSError, RuntimeError, ValueError):
            return False

    def create(self, question: str, *, title: str | None, kind: str, options: list[str] | None,
               allow_custom: bool) -> dict:
        return self._request("POST", "/api/asks", {"question": question, "title": title, "kind": kind,
                                                   "options": options or [], "allow_custom": allow_custom})

    def state(self, ask_id: str) -> dict:
        return self._request("GET", "/api/asks/" + quote(ask_id, safe=""))


def as_question(ask: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """A gateway ask in the chatx question shape the tools already return, and its status."""
    answer = ask.get("answer") or {}
    selected: list[str] = []
    if isinstance(answer.get("choice"), str):
        selected = [answer["choice"]]
    elif isinstance(answer.get("choices"), list):
        selected = [item for item in answer["choices"] if isinstance(item, str)]
    status = {"open": "pending", "answered": "answered"}.get(ask.get("state"), "cancelled")
    return {"id": ask.get("ask_id"), "answer": {"selected": selected, "text": answer.get("text")}}, status
