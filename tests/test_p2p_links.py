"""The client's P2P tunnels (p2p.Connector): an idle one is closed, one in use is not."""
from __future__ import annotations

from agent_nexus import p2p


class _Process:
    def __init__(self):
        self.returncode = None
        self.stdin = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0


def _connector_with(target: str, used_ago: float, monkeypatch) -> tuple[p2p.Connector, p2p._Link]:
    connector = p2p.Connector("muxi-p2p-agent")
    link = p2p._Link(_Process(), 50000, "P2P")
    connector.links[target] = link
    now = 10_000.0
    monkeypatch.setattr(p2p.time, "monotonic", lambda: now)
    link.used = now - used_ago
    return connector, link


def test_an_idle_tunnel_is_closed(monkeypatch):
    connector, link = _connector_with("pc", p2p.IDLE_S + 1, monkeypatch)
    connector.reap()
    assert "pc" not in connector.links and link.process.terminated


def test_a_tunnel_carrying_a_terminal_is_kept_however_long_it_runs(monkeypatch):
    """A terminal open longer than IDLE_S was cut when any other tunnel was asked for (reap)."""
    connector, link = _connector_with("pc", p2p.IDLE_S + 1, monkeypatch)
    with connector.hold("pc"):
        connector.reap()
        assert connector.links["pc"] is link and not link.process.terminated
    assert link.streams == 0
    connector.reap()  # used just now: the idle time starts again when the stream ends
    assert connector.links["pc"] is link


def test_holding_a_target_without_a_tunnel_is_harmless(monkeypatch):
    connector = p2p.Connector("muxi-p2p-agent")
    with connector.hold("relayed-only"):
        pass
    assert connector.links == {}
