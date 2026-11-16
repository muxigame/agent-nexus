"""Formats of identifiers that end up on a command line (design §8.7).

A resume id becomes an argument of the woken ``claude`` / ``codex``. On
Windows the default executable is npm's ``claude.cmd`` shim, which runs
through cmd.exe, so the id is held to a strict pattern wherever it enters:
``use_device``/``set_workspace``, ``hub_call presence_report``, ``anx
address set`` and the device's ``session_wake``. Claude session ids are
UUIDs and Codex ids are UUIDs or ``rollout`` names; both fit.
"""
from __future__ import annotations

import re

RESUME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
RESUME_ID_RULE = "resume_id must be 1-200 letters, digits, dots, underscores, colons or hyphens"


def valid_resume_id(value: object) -> bool:
    return isinstance(value, str) and RESUME_ID.fullmatch(value) is not None
