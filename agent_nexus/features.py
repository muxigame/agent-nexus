"""Link protocol feature exchange (design §0.1, §3.2).

The wire protocol stays ``protocol: 1``. Both ends add an ``anx`` object --
the device in ``hello``, the gateway in ``ready.params`` -- listing the
features they implement. A legacy end ignores the extra key, and announces
nothing, so neither side ever sends it a method it does not know: legacy
agents rewrite unknown reverse RPCs into a generic failure and legacy
gateways silently drop unknown device messages (§3.1), so *only* the
announced features can tell what the other end supports.

Only what this build actually implements is announced; later milestones add
their entries here (e.g. ``session_wake/1`` with M7, ``mail/1`` with M6).
"""
from __future__ import annotations

import re
from typing import Any

from . import __version__

#: Features a device (``anx-agent``) announces in ``hello.anx.features``.
#: ``device-contract/1``: the catalog carries the ``anx.device/1`` block (M5).
#: ``toast_actions/1``: answers the ``toast`` / ``toast_dismiss`` reverse RPCs (M6, §8.12);
#: ``ask/1``: the device plugin's request_user_input goes through ``hub_call ask_*``.
#: ``session_wake/1`` / ``session_probe/1``: the wake adapters and the liveness probe (M7, §8.7).
AGENT_FEATURES: tuple[str, ...] = ("device-contract/1", "mcp-alias/1", "hub_call/1", "ask/1", "toast_actions/1",
                                   "session_wake/1", "session_probe/1")
#: Features the gateway announces in ``ready.params.anx.features``.
#: ``mail/1``: the mailbox tools; ``ask/1``: ``hub_call ask_create/ask_get/ask_answer``;
#: ``inbox/1``: ``hub_call mail_local`` (the local console's inbox page).
#: ``wake/1``: ``hub_call wake_report`` and ``presence_report`` (M7, §8.3, §8.7).
#: ``invites/1``: ``hub_call device_admin`` invite_create/invite_list/invite_revoke/revoke (M16, §11.5).
#: ``plugin_repo/1``: the 插件库 (``/api/plugin-repo``, design §6.12). On a device it means the
#: manager answers ``plugin_repo_install`` / ``plugin_repo_job``; only a device running inside
#: the manager (``anx serve``, the desktop app) announces it, see :data:`AGENT_OPTIONAL_FEATURES`.
#: ``remote/1``: devices may open each other's terminals with their own credential
#: (``/api/devices/{id}/terminal[/ws]`` with ``X-ANX-Device``) and report ``hub_call lock_report``.
GATEWAY_FEATURES: tuple[str, ...] = ("hub_call/1", "mcp-alias/1", "mail/1", "ask/1", "inbox/1", "wake/1", "invites/1",
                                     "plugin_repo/1", "remote/1", "p2p/1")
REMOTE = "remote/1"
#: ``p2p/1``: muxi-p2p signaling under /p2p, the reflector, hub_call p2p_config / remote_ticket.
P2P = "p2p/1"
#: ``remote-hub/1`` (device): answers ``remote_ticket`` and runs a P2P tunnel agent to its remote hub.
REMOTE_HUB = "remote-hub/1"
PLUGIN_REPO = "plugin_repo/1"
#: ``term/1``: the device answers ``term_info/open/write/read/resize/close`` (remote terminal);
#: announced by the manager when a pseudo-terminal is available and ``terminal.enabled`` is not false.
TERM = "term/1"
#: Device features announced only when the process can serve them.
#: ``desktop/1``: the remote hub also serves the built-in VNC (a device with a desktop).
AGENT_OPTIONAL_FEATURES: tuple[str, ...] = (PLUGIN_REPO, TERM, "remote-hub/1", "desktop/1")
ASK = "ask/1"
INBOX = "inbox/1"
WAKE = "wake/1"
SESSION_WAKE = "session_wake/1"
SESSION_PROBE = "session_probe/1"

HUB_CALL = "hub_call/1"
_FEATURE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}/[0-9]{1,4}")
MAX_FEATURES = 64


def parse_features(anx: Any) -> frozenset[str]:
    """Well-formed feature names from a peer's ``anx`` object; anything else is ignored."""
    if not isinstance(anx, dict) or not isinstance(anx.get("features"), list):
        return frozenset()
    return frozenset(item for item in anx["features"][:MAX_FEATURES]
                     if isinstance(item, str) and _FEATURE.fullmatch(item))


def peer_version(anx: Any) -> str | None:
    if isinstance(anx, dict) and isinstance(anx.get("version"), str) and len(anx["version"]) <= 64:
        return anx["version"]
    return None


def agent_anx(extra: tuple[str, ...] | list[str] = ()) -> dict:
    return {"version": __version__,
            "features": list(AGENT_FEATURES) + [f for f in extra if f in AGENT_OPTIONAL_FEATURES]}


def gateway_anx(display_name: str) -> dict:
    return {"version": __version__, "features": list(GATEWAY_FEATURES), "display_name": display_name}
