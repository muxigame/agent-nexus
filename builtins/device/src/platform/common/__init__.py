"""Capabilities every platform implements (contract ``anx.device/1``, design §5.2).

Each capability lists the standard tools that provide it. The server registers a
tool only when the running platform declares its capability, and tags it with
``_meta = {"anx/capability": <id>, "anx/contract": "anx.device/1"}`` so the
gateway can say *which* capability a device lacks instead of "unknown tool".

``tier`` is ``core`` (every device must implement it) or ``standard``
(optional, but these are what a full device offers). ``tools`` and ``tier`` are
plugin-internal: ``device_info`` publishes only ``v`` plus runtime facts.
"""
from __future__ import annotations

from typing import Any

CORE: tuple[str, ...] = ("info", "notify", "files", "time")

CAPABILITIES: dict[str, dict[str, Any]] = {
    # -- core ---------------------------------------------------------------
    "info": {"v": 1, "tier": "core", "tools": ["device_info"]},
    "notify": {"v": 1, "tier": "core", "tools": ["notify"], "actions": True},
    "files": {"v": 1, "tier": "core", "tools": ["file_list", "file_read", "file_write", "file_stat"]},
    "time": {"v": 1, "tier": "core", "tools": ["curr_time", "sleep"]},
    # -- standard -----------------------------------------------------------
    "exec": {"v": 1, "tier": "standard", "tools": ["exec_command", "write_stdin", "exec_wait", "exec_kill", "exec_list", "exec_tail"]},
    "edit": {"v": 1, "tier": "standard", "tools": ["edit_file", "patch_file"]},
    "search": {"v": 1, "tier": "standard", "tools": ["file_glob", "file_grep"]},
    "plan": {"v": 1, "tier": "standard", "tools": ["update_plan"]},
    "media": {"v": 1, "tier": "standard", "tools": ["view_image"]},
    "watch": {"v": 1, "tier": "standard", "tools": ["watch_create", "watch_training", "watch_list",
                                                    "watch_cancel", "watch_event", "watch_events"]},
    "ask": {"v": 1, "tier": "standard", "tools": ["request_user_input", "request_user_input_async",
                                                  "request_user_input_result"]},
    "agents": {"v": 1, "tier": "standard", "tools": ["agent_sessions"]},
    "companion": {"v": 1, "tier": "standard", "tools": ["companion_routes"]},
}

#: Platform extras that exist somewhere (Windows today). A platform without
#: them still declares them as unavailable, with the reason, so ``device_list``
#: and the gateway's errors can explain the gap.
SCREEN_CAPABILITIES: dict[str, dict[str, Any]] = {
    "screen": {"v": 1, "tier": "platform", "tools": ["list_screens", "screenshot", "vision"]},
    "uia": {"v": 1, "tier": "platform", "tools": ["start_control", "stop_control", "control_status",
                                                  "read_ui", "element"]},
    "input": {"v": 1, "tier": "platform", "tools": ["mouse", "keyboard"]},
}


def unavailable(capabilities: dict[str, dict[str, Any]], reason: str) -> dict[str, dict[str, Any]]:
    """Declare ``capabilities`` without tools, ``available: false`` with ``reason``."""
    return {name: {"v": spec["v"], "tier": spec["tier"], "tools": [], "available": False, "reason": reason}
            for name, spec in capabilities.items()}


def probe() -> dict[str, dict[str, Any]]:
    """Runtime availability overrides; the common capabilities are always available."""
    return {}
