"""Deterministic regression tests for scoped client ownership and deadlines."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from agent_nexus import agent
from agent_nexus.federation import RoutingError

pytestmark = pytest.mark.asyncio


async def test_close_does_not_interrupt_initializer_already_cleaning_up(pool, monkeypatch):
    gateway = pool()
    backend = Backend()
    started, cleanup_started, cleanup_release, cleanup_finished = (event() for _ in range(4))
    original_enter = FakeClient.__aenter__

    async def entering(client):
        if client.backend is not backend:
            return await original_enter(client)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await cleanup_release.wait()
            cleanup_finished.set()

    monkeypatch.setattr(FakeClient, "__aenter__", entering)
    gateway.targets = {"workspace": backend}
    gateway.target_signatures = {"workspace": "old-target"}
    pending = asyncio.create_task(gateway.client("workspace", "branch"))
    closing = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        # Reconfiguration cancels the old initializer and starts SDK cleanup.
        await gateway.refresh()
        await asyncio.wait_for(cleanup_started.wait(), 1)
        closing = asyncio.create_task(gateway.close())
        done, _ = await asyncio.wait((closing,), timeout=.05)
        assert not done and not pending.done()
        cleanup_release.set()
        await asyncio.wait_for(closing, 1)
        with pytest.raises(RoutingError):
            await pending
        assert cleanup_finished.is_set()
    finally:
        cleanup_release.set()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await gateway.close()
        await asyncio.gather(pending, return_exceptions=True)


def event(*, ready=False):
    result = asyncio.Event()
    if ready:
        result.set()
    return result


class Backend:
    def __init__(self):
        self.init_started = event()
        self.init_release = event(ready=True)
        self.close_started = event()
        self.close_release = event(ready=True)
        self.call_started = event()
        self.call_release = event(ready=True)
        self.init_error = None
        self.ignore_init_cancel = False
        self.discovery_delay = 0
        self.call_delay = 0
        self.calls = 0


class FakeClient:
    instances = []

    def __init__(self, backend, **kwargs):
        self.backend = backend
        self.options = kwargs
        self.exits = 0
        self.enter_cancelled = False
        self.closed = False
        self.instances.append(self)

    async def __aenter__(self):
        self.backend.init_started.set()
        try:
            await self.backend.init_release.wait()
        except asyncio.CancelledError:
            self.enter_cancelled = True
            if not self.backend.ignore_init_cancel:
                raise
        if self.backend.init_error:
            raise self.backend.init_error
        return self

    async def __aexit__(self, *_args):
        self.exits += 1
        self.backend.close_started.set()
        try:
            await self.backend.close_release.wait()
        finally:
            self.closed = True

    async def list_tools(self):
        assert not self.closed
        if self.backend.discovery_delay:
            await asyncio.sleep(self.backend.discovery_delay)
        return [SimpleNamespace(name="read", input_schema={},
                                model_dump=lambda **kwargs: {"name": "read", "inputSchema": {}})]

    async def call_tool_mcp(self, tool, arguments):
        assert not self.closed
        self.backend.calls += 1
        self.backend.call_started.set()
        await self.backend.call_release.wait()
        if self.backend.call_delay:
            await asyncio.sleep(self.backend.call_delay)
        return SimpleNamespace(model_dump=lambda **kwargs: {"result": "ok"})


@pytest.fixture
def pool(tmp_path, monkeypatch):
    FakeClient.instances = []
    monkeypatch.setattr(agent, "Client", FakeClient)

    def make(**settings):
        return agent.LocalGateway(tmp_path, {"workspaces": {}, **settings}, transports={})
    return make


async def test_slow_initialization_does_not_block_another_cached_scope(pool):
    gateway = pool()
    slow, fast = Backend(), Backend()
    slow.init_release.clear()
    gateway.targets = {"slow": slow, "workspace": fast}
    cached = await gateway.client("workspace", "main")
    pending = asyncio.create_task(gateway.client("slow", "branch"))
    try:
        await slow.init_started.wait()
        assert await asyncio.wait_for(gateway.client("workspace", "main"), 1) is cached
        assert not pending.done()
        slow.init_release.set()
        await pending
    finally:
        await gateway.close()


async def test_one_shared_initializer_survives_one_waiter_cancellation(pool):
    gateway = pool()
    backend = Backend()
    backend.init_release.clear()
    gateway.targets = {"workspace": backend}
    first = asyncio.create_task(gateway.client("workspace", "same"))
    second = asyncio.create_task(gateway.client("workspace", "same"))
    try:
        await backend.init_started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert len(FakeClient.instances) == 1
        assert not FakeClient.instances[0].enter_cancelled
        backend.init_release.set()
        assert await asyncio.wait_for(second, 1) is FakeClient.instances[0]
        assert not gateway.pending
    finally:
        await gateway.close()
    assert FakeClient.instances[0].exits == 1


async def test_pending_initializers_count_toward_capacity(pool):
    gateway = pool(max_sessions=1)
    backend = Backend()
    backend.init_release.clear()
    gateway.targets = {"workspace": backend}
    first = asyncio.create_task(gateway.client("workspace", "one"))
    try:
        await backend.init_started.wait()
        with pytest.raises(RoutingError, match="session limit"):
            await gateway.client("workspace", "two")
        assert len(FakeClient.instances) == 1
        backend.init_release.set()
        await first
    finally:
        await gateway.close()


async def test_failed_initialization_is_not_cached_and_next_explicit_call_can_connect(pool):
    gateway = pool()
    backend = Backend()
    backend.init_error = RuntimeError("private transport detail")
    gateway.targets = {"workspace": backend}
    try:
        with pytest.raises(RuntimeError):
            await gateway.client("workspace", "same")
        assert not gateway.sessions and not gateway.pending
        assert len(FakeClient.instances) == 1
        backend.init_error = None
        assert await gateway.client("workspace", "same") is FakeClient.instances[1]
    finally:
        await gateway.close()
    assert [client.exits for client in FakeClient.instances] == [0, 1]


async def test_reap_detaches_before_close_and_does_not_delete_new_call_counter(pool):
    gateway = pool(session_idle_ttl_s=1)
    old, new = Backend(), Backend()
    gateway.targets = {"workspace": old}
    old_client = await gateway.client("workspace", "same")
    gateway.sessions[("workspace", "same")] = (old_client, 0)
    old.close_release.clear()
    reaper = asyncio.create_task(gateway.reap())
    try:
        await old.close_started.wait()
        gateway.targets["workspace"] = new
        result = await asyncio.wait_for(gateway.dispatch("tool_call", {
            "mcp": "workspace", "tool": "read", "scope": "same"}), 1)
        assert result == {"result": "ok"}
        assert not gateway.active
        assert gateway.sessions[("workspace", "same")][0] is not old_client
        old.close_release.set()
        await reaper
        assert not gateway.active
    finally:
        old.close_release.set()
        await asyncio.gather(reaper, return_exceptions=True)
        await gateway.close()
    assert old_client.exits == 1


async def test_cancelled_reaper_does_not_return_closing_client_to_pool(pool):
    gateway = pool(session_idle_ttl_s=1)
    old, new = Backend(), Backend()
    gateway.targets = {"workspace": old}
    old_client = await gateway.client("workspace", "same")
    gateway.sessions[("workspace", "same")] = (old_client, 0)
    old.close_release.clear()
    reaper = asyncio.create_task(gateway.reap())
    try:
        await old.close_started.wait()
        reaper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reaper
        gateway.targets["workspace"] = new
        assert await gateway.client("workspace", "same") is not old_client
        old.close_release.set()
    finally:
        old.close_release.set()
        await gateway.close()
    assert old_client.exits == 1


async def test_refresh_revokes_pending_old_target_and_returns_routing_error(pool):
    gateway = pool()
    old, new = Backend(), Backend()
    gateway.transports = {"workspace": old}
    await gateway.refresh()
    old.init_started.clear()
    old.init_release.clear()
    pending = asyncio.create_task(gateway.client("workspace", "branch"))
    try:
        await old.init_started.wait()
        gateway.transports = {"workspace": new}
        await asyncio.wait_for(gateway.refresh(), 1)
        with pytest.raises(RoutingError, match="closed during initialization"):
            await pending
        replacement = await gateway.client("workspace", "branch")
        assert replacement.backend is new
        assert all(client.backend is new for client, _ in gateway.sessions.values())
    finally:
        await gateway.close()


async def test_close_prevents_late_initializer_from_republishing_connection(pool):
    gateway = pool()
    backend = Backend()
    backend.init_release.clear()
    backend.ignore_init_cancel = True
    gateway.targets = {"workspace": backend}
    pending = asyncio.create_task(gateway.client("workspace", "branch"))
    await backend.init_started.wait()
    await gateway.close()
    with pytest.raises(RoutingError):
        await pending
    assert not gateway.sessions and not gateway.pending
    assert FakeClient.instances[0].exits == 1
    with pytest.raises(RoutingError, match="gateway is closed"):
        await gateway.client("workspace", "branch")


async def test_dispatch_has_one_budget_across_discovery_and_tool_without_retry(pool):
    gateway = pool(rpc_timeout_s=.15)
    backend = Backend()
    backend.discovery_delay = .10
    backend.call_delay = .10
    gateway.targets = {"workspace": backend}
    try:
        with pytest.raises(asyncio.TimeoutError):
            await gateway.dispatch("tool_call", {"mcp": "workspace", "tool": "read", "scope": "same"})
        assert backend.calls == 1
        assert not gateway.active
    finally:
        await gateway.close()


async def test_completed_call_refreshes_idle_timestamp(pool):
    gateway = pool()
    backend = Backend()
    backend.call_release.clear()
    gateway.targets = {"workspace": backend}
    call = asyncio.create_task(gateway.dispatch("tool_call", {
        "mcp": "workspace", "tool": "read", "scope": "same"}))
    try:
        await backend.call_started.wait()
        finished_after = time.monotonic()
        backend.call_release.set()
        await call
        assert gateway.sessions[("workspace", "same")][1] >= finished_after
    finally:
        await gateway.close()


async def test_catalog_failure_log_does_not_expose_transport_details(pool, caplog):
    gateway = pool()
    backend = Backend()
    backend.init_error = RuntimeError("https://user:secret@example.invalid Bearer private-token")
    gateway.transports = {"extra": backend}
    try:
        catalog = await gateway.refresh()
        assert catalog["mcps"]["extra"]["error"]
        assert "RuntimeError" in caplog.text
        assert "private-token" not in caplog.text and "example.invalid" not in caplog.text
    finally:
        await gateway.close()


async def test_retired_connection_keeps_capacity_until_close_finishes(pool):
    gateway = pool(max_sessions=1, session_idle_ttl_s=1)
    old, new = Backend(), Backend()
    gateway.targets = {"workspace": old}
    old_client = await gateway.client("workspace", "same")
    gateway.sessions[("workspace", "same")] = (old_client, 0)
    old.close_release.clear()
    reaper = asyncio.create_task(gateway.reap())
    try:
        await old.close_started.wait()
        gateway.targets["workspace"] = new
        for _ in range(3):
            with pytest.raises(RoutingError, match="session limit"):
                await gateway.client("workspace", "same")
        assert len(FakeClient.instances) == 1
        assert len(gateway.owners) == 1
        old.close_release.set()
        await reaper
        assert not gateway.owners
        assert (await gateway.client("workspace", "same")).backend is new
    finally:
        old.close_release.set()
        await asyncio.gather(reaper, return_exceptions=True)
        await gateway.close()
    assert not gateway.owners


async def test_publishing_initializer_does_not_double_count_capacity(pool):
    gateway = pool(max_sessions=2)
    first_backend, second_backend = Backend(), Backend()
    first_backend.init_release.clear()
    gateway.targets = {"first": first_backend, "second": second_backend}
    first = asyncio.create_task(gateway.client("first", "scope"))
    try:
        await first_backend.init_started.wait()
        await gateway.lock.acquire()
        first_backend.init_release.set()
        # Queue the publishing initializer ahead of the second acquisition.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        second = asyncio.create_task(gateway.client("second", "scope"))
        await asyncio.sleep(0)
        gateway.lock.release()
        results = await asyncio.wait_for(asyncio.gather(first, second), 1)
        assert {client.backend for client in results} == {first_backend, second_backend}
        assert len(gateway.owners) == 2
    finally:
        if gateway.lock.locked():
            gateway.lock.release()
        first_backend.init_release.set()
        await gateway.close()
    assert not gateway.owners


class Socket:
    def __init__(self):
        self.sent = []
        self.closed = []
        self.block = False
        self.started = event()

    async def send(self, data):
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        self.sent.append(json.loads(data))

    async def close(self, code):
        self.closed.append(code)


async def test_agent_send_deadline_includes_waiting_for_send_lock():
    socket, lock = Socket(), asyncio.Lock()
    await lock.acquire()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await agent._send_agent_message(socket, lock, {"id": "waiting"}, .02)
        assert socket.closed == [1011]
        assert not socket.sent and not socket.started.is_set()
    finally:
        lock.release()


async def test_agent_send_stall_closes_socket_and_releases_lock():
    socket, lock = Socket(), asyncio.Lock()
    socket.block = True
    with pytest.raises(asyncio.TimeoutError):
        await agent._send_agent_message(socket, lock, {"id": "stalled"}, .02)
    assert socket.started.is_set() and socket.closed == [1011]
    assert not lock.locked()


async def test_agent_send_and_socket_close_cleanup_cannot_extend_deadline():
    class SlowCleanupSocket(Socket):
        def __init__(self):
            super().__init__()
            self.send_cleanup_started = event()
            self.close_started = event()
            self.close_cleanup_started = event()
            self.release_cleanup = event()

        async def send(self, data):
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.send_cleanup_started.set()
                await self.release_cleanup.wait()

        async def close(self, code):
            self.closed.append(code)
            self.close_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.close_cleanup_started.set()
                await self.release_cleanup.wait()

    socket, lock = SlowCleanupSocket(), asyncio.Lock()
    initial = set(agent._agent_io_tasks)
    call = asyncio.create_task(agent._send_agent_message(socket, lock, {"id": "tail"}, .02))
    try:
        done, _ = await asyncio.wait((call,), timeout=.5)
        assert call in done, "send deadline waited for a cancelled I/O cleanup tail"
        with pytest.raises(asyncio.TimeoutError):
            await call
        await asyncio.wait_for(socket.send_cleanup_started.wait(), .5)
        await asyncio.wait_for(socket.close_cleanup_started.wait(), .5)
        assert socket.closed == [1011]
        assert not socket.release_cleanup.is_set()
        assert len(set(agent._agent_io_tasks) - initial) == 2
    finally:
        socket.release_cleanup.set()
        await asyncio.gather(call, *list(set(agent._agent_io_tasks) - initial), return_exceptions=True)
    assert not lock.locked()
    assert not (set(agent._agent_io_tasks) - initial)


async def test_cancellation_while_queued_does_not_close_healthy_socket():
    socket, lock = Socket(), asyncio.Lock()
    await lock.acquire()
    call = asyncio.create_task(agent._send_agent_message(socket, lock, {"id": "cancelled"}, 1))
    try:
        await asyncio.sleep(0)
        call.cancel()
        done, _ = await asyncio.wait((call,), timeout=.5)
        assert call in done
        with pytest.raises(asyncio.CancelledError):
            await call
        assert not socket.closed and not socket.sent
    finally:
        lock.release()
