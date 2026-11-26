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


@pytest.fixture(autouse=True)
def _no_geoip_downloads(monkeypatch):
    """A gateway under test locates devices too: it must not fetch ip2region or DB-IP (11 / 60 MB)."""
    from agent_nexus import geoip

    def offline(*_args, **_kwargs):
        raise RuntimeError("no IP databases are downloaded in tests")

    monkeypatch.setattr(geoip, "_download", offline)
    monkeypatch.setattr(geoip, "_download_dbip", offline)
