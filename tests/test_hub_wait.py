"""HubLink.wait_available: P2P requests ride out a short gateway reconnect."""
import asyncio

from agent_nexus import features
from agent_nexus.agent import HubLink



async def _noop_send(message):
    return None


def test_wait_available_returns_at_once_when_attached():
    async def run():
        hub = HubLink()
        hub.attach(_noop_send, features.gateway_anx("gw"))
        return await hub.wait_available(0.01)
    assert asyncio.run(run()) is True


def test_wait_available_rides_out_a_reconnect():
    async def run():
        hub = HubLink()
        hub.attach(_noop_send, features.gateway_anx("gw"))
        hub.detach()
        assert not hub.available

        async def reconnect():
            await asyncio.sleep(0.05)
            hub.attach(_noop_send, features.gateway_anx("gw"))
        task = asyncio.create_task(reconnect())
        ok = await hub.wait_available(2)
        await task
        return ok
    assert asyncio.run(run()) is True


def test_wait_available_gives_up_after_timeout():
    async def run():
        hub = HubLink()
        return await hub.wait_available(0.05)
    assert asyncio.run(run()) is False
