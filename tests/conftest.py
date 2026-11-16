"""Shared test setup."""
from __future__ import annotations

import pytest

#: Set when the tests themselves run under an installed Agent Nexus (for example
#: through its device plugin). Servers started by a test must never be parented
#: to that real desktop shell (agent_nexus/winspawn.py), so tests run without it
#: unless they set it on purpose.
DESKTOP_SHELL_VARS = ("ANX_DESKTOP_APP", "MCPM_DESKTOP_APP")


@pytest.fixture(autouse=True)
def _no_real_desktop_shell(monkeypatch):
    for name in DESKTOP_SHELL_VARS:
        monkeypatch.delenv(name, raising=False)
