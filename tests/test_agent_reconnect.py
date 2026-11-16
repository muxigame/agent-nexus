"""Reconnection must not await a stale request's cancellation tail forever."""
import asyncio
import json

import pytest

from agent_nexus import agent


@pytest.mark.parametrize("suppress_cancel", [False, True])
async def test_reconnect_does_not_wait_for_stalled_old_request_cleanup(monkeypatch, tmp_path, suppress_cancel):
    started = asyncio.Event()
    cancelling = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    reconnected = asyncio.Event()
    sockets = []
    executions = []

    class Gateway:
        rpc_timeout = .02

        def __init__(self, *args, **kwargs):
            pass

        async def refresh(self):
            return {"mcps": {}, "workspaces": {}}

        async def dispatch(self, method, params):
            executions.append(method)
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                if not suppress_cancel:
                    raise
            finally:
                cancelling.set()
                await release.wait()
                finished.set()
            return {"late": "old response"}

        async def close(self):
            pass

    class Socket:
        def __init__(self):
            self.attempts = []
            self.closed = False

        async def __aenter__(self):
            sockets.append(self)
            if len(sockets) == 2:
                reconnected.set()
            return self

        async def __aexit__(self, *args):
            self.closed = True

        async def send(self, message):
            payload = json.loads(message)
            self.attempts.append(payload)
            assert payload.get("method") != "tool_call"
            if self.closed:
                raise ConnectionError("old connection is closed")

        async def recv(self):
            return '{"method":"ready"}'

        async def close(self, **kwargs):
            self.closed = True

        def __aiter__(self):
            async def messages():
                if len(sockets) == 1:
                    yield json.dumps({"id": "old-request", "method": "tool_call", "params": {}})
                    await started.wait()
                    raise OSError("simulated connection loss")
                await asyncio.Future()
                if False:
                    yield ""
            return messages()

    monkeypatch.setattr(agent, "LocalGateway", Gateway)
    monkeypatch.setattr(agent, "connect", lambda *args, **kwargs: Socket())
    monkeypatch.setenv("RECONNECT_TEST_TOKEN", "x" * 40)
    task = asyncio.create_task(agent.run_agent(tmp_path, {"agent_id": "test", "url": "ws://127.0.0.1", "token_env": "RECONNECT_TEST_TOKEN"}))
    try:
        await asyncio.wait_for(cancelling.wait(), 1)
        await asyncio.wait_for(reconnected.wait(), 1.5)
        assert not finished.is_set()
        assert executions == ["tool_call"]  # Reconnection never replays an old request.
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.sleep(.05)
        if len(sockets) >= 2:
            assert not any(message.get("id") == "old-request" for message in sockets[1].attempts)
        task.cancel()
        done, _ = await asyncio.wait([task], timeout=2)
        if not done:
            task.print_stack()
        assert done, "agent cancellation stalled after reconnect"


async def test_local_sessions_survive_cloud_reconnects(monkeypatch, tmp_path):
    """Per-session state in local MCPs (e.g. Unity's active instance) must not reset on a cloud drop."""
    gateways = []
    closed = []
    second = asyncio.Event()
    sockets = []

    class Gateway:
        rpc_timeout = .02

        def __init__(self, *args, **kwargs):
            gateways.append(self)

        async def refresh(self):
            return {"mcps": {}, "workspaces": {}}

        async def close(self):
            closed.append(self)

    class Socket:
        async def __aenter__(self):
            sockets.append(self)
            if len(sockets) == 2:
                second.set()
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, message):
            pass

        async def recv(self):
            return '{"method":"ready"}'

        async def close(self, **kwargs):
            pass

        def __aiter__(self):
            async def messages():
                if len(sockets) == 1:
                    raise OSError("simulated connection loss")
                await asyncio.Future()
                yield ""
            return messages()

    monkeypatch.setattr(agent, "LocalGateway", Gateway)
    monkeypatch.setattr(agent, "connect", lambda *args, **kwargs: Socket())
    monkeypatch.setenv("RECONNECT_TEST_TOKEN", "x" * 40)
    task = asyncio.create_task(agent.run_agent(tmp_path, {"agent_id": "test", "url": "ws://127.0.0.1", "token_env": "RECONNECT_TEST_TOKEN"}))
    try:
        await asyncio.wait_for(second.wait(), 3)
        assert len(gateways) == 1 and not closed
    finally:
        task.cancel()
        await asyncio.wait([task], timeout=2)
    assert closed == gateways
