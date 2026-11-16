"""Bounded reverse-RPC waits, cancellation and response correlation."""
import asyncio
import gc

import pytest

from agent_nexus.federation import Peer, RoutingError


pytestmark = pytest.mark.asyncio


class Socket:
    def __init__(self, *, block=None, expected=1):
        self.sent = []
        self.block = block
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.expected = expected

    async def send_json(self, message):
        self.sent.append(message)
        if len([item for item in self.sent if item["method"] == "tool_call"]) >= self.expected:
            self.entered.set()
        if message["method"] == self.block:
            await self.release.wait()


async def finish(task, timeout=.5):
    # wait_for() waits for cancellation cleanup, which is itself one of the
    # regressions exercised here. Keep the test harness independently bounded.
    done, _ = await asyncio.wait({task}, timeout=timeout)
    assert task in done, "RPC remained stuck beyond its bounded wait"
    return await task


async def cleanup(*tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("blocked", ["slot", "lock"])
async def test_timeout_includes_admission_and_send_lock(blocked):
    socket = Socket()
    peer = Peer("pc", socket, {}, timeout=.02)
    if blocked == "slot":
        peer.slots = asyncio.Semaphore(0)
    else:
        await peer.lock.acquire()
    task = asyncio.create_task(peer.call("tool_call", {}))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await finish(task)
        assert not socket.sent
        assert not peer.pending
    finally:
        if blocked == "slot":
            peer.slots.release()
        else:
            peer.lock.release()
        await cleanup(task)


async def test_timeout_includes_socket_send_and_releases_capacity():
    socket = Socket(block="tool_call")
    peer = Peer("pc", socket, {}, timeout=.02)
    peer.slots = asyncio.Semaphore(1)
    task = asyncio.create_task(peer.call("tool_call", {}))
    try:
        await asyncio.wait_for(socket.entered.wait(), .5)
        with pytest.raises(asyncio.TimeoutError):
            await finish(task)
        assert [item["method"] for item in socket.sent] == ["tool_call", "cancel"]
        assert not peer.pending
        assert not peer.slots.locked()
        assert not peer.lock.locked()
    finally:
        socket.release.set()
        await cleanup(task)


async def test_result_timeout_survives_stalled_cancel_notification():
    socket = Socket(block="cancel")
    peer = Peer("pc", socket, {}, timeout=.02)
    task = asyncio.create_task(peer.call("tool_call", {}))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await finish(task)
        assert [item["method"] for item in socket.sent] == ["tool_call", "cancel"]
        assert not peer.pending
        assert not peer.lock.locked()
    finally:
        socket.release.set()
        await cleanup(task)


async def test_caller_cancellation_survives_stalled_cancel_notification():
    socket = Socket(block="cancel")
    peer = Peer("pc", socket, {}, timeout=.05)
    task = asyncio.create_task(peer.call("tool_call", {}))
    try:
        await asyncio.wait_for(socket.entered.wait(), .5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await finish(task)
        assert [item["method"] for item in socket.sent] == ["tool_call", "cancel"]
        assert not peer.pending
        assert not peer.lock.locked()
    finally:
        socket.release.set()
        await cleanup(task)


@pytest.mark.parametrize("stage", ["slot", "lock", "send", "reply"])
async def test_disconnect_wakes_calls_at_every_wait_stage(stage):
    socket = Socket(block="tool_call" if stage == "send" else None)
    peer = Peer("pc", socket, {}, timeout=10)
    if stage == "slot":
        peer.slots = asyncio.Semaphore(0)
    elif stage == "lock":
        await peer.lock.acquire()
    task = asyncio.create_task(peer.call("tool_call", {}))
    try:
        if stage in ("send", "reply"):
            await asyncio.wait_for(socket.entered.wait(), .5)
        else:
            # Let the call register itself and its worker reach admission.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        peer.disconnect()
        peer.disconnect()  # idempotent during overlapping connection teardown
        with pytest.raises(RoutingError, match="disconnected"):
            await finish(task)
        assert not peer.pending
        assert [item["method"] for item in socket.sent] == (
            ["tool_call"] if stage in ("send", "reply") else [])
        sent = len(socket.sent)
        with pytest.raises(RoutingError, match="disconnected"):
            await peer.call("tool_call", {})
        assert len(socket.sent) == sent
    finally:
        socket.release.set()
        if stage == "slot":
            peer.slots.release()
        elif stage == "lock":
            peer.lock.release()
        await cleanup(task)


async def test_peer_disconnected_before_first_call_never_dispatches():
    socket = Socket()
    peer = Peer("pc", socket, {})
    peer.disconnect()
    with pytest.raises(RoutingError, match="disconnected"):
        await peer.call("tool_call", {})
    assert not socket.sent
    assert not peer.pending


async def test_completed_reply_is_preserved_when_send_and_disconnect_race():
    socket = Socket(block="tool_call")
    peer = Peer("pc", socket, {}, timeout=10)
    task = asyncio.create_task(peer.call("tool_call", {}))
    try:
        await asyncio.wait_for(socket.entered.wait(), .5)
        peer.resolve({"id": socket.sent[0]["id"], "result": "confirmed result"})
        peer.disconnect()
        assert await finish(task) == "confirmed result"
        assert not peer.pending
        assert not peer.lock.locked()
    finally:
        socket.release.set()
        await cleanup(task)


async def test_reversed_responses_stay_with_their_requests():
    count = 24
    socket = Socket(expected=count)
    peer = Peer("pc", socket, {}, timeout=2)
    tasks = [asyncio.create_task(peer.call("tool_call", {"value": value}))
             for value in range(count)]
    try:
        await asyncio.wait_for(socket.entered.wait(), .5)
        assert len({item["id"] for item in socket.sent}) == count
        for message in reversed(socket.sent):
            peer.resolve({"id": message["id"], "result": message["params"]["value"]})
        assert await asyncio.gather(*tasks) == list(range(count))
        assert not peer.pending
    finally:
        await cleanup(*tasks)


async def test_one_cancel_and_late_reply_do_not_affect_another_request():
    socket = Socket(expected=2)
    peer = Peer("pc", socket, {}, timeout=2)
    first = asyncio.create_task(peer.call("tool_call", {"name": "first"}))
    second = asyncio.create_task(peer.call("tool_call", {"name": "second"}))
    try:
        await asyncio.wait_for(socket.entered.wait(), .5)
        ids = {item["params"]["name"]: item["id"] for item in socket.sent}
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await finish(first)
        assert not second.done()
        assert socket.sent[-1] == {"method": "cancel", "params": {"id": ids["first"]}}
        peer.resolve({"id": ids["first"], "result": "late"})
        peer.resolve({"id": "unknown", "result": "unrelated"})
        assert not second.done()
        peer.resolve({"id": ids["second"], "result": "correct"})
        assert await finish(second) == "correct"
        assert not peer.pending
    finally:
        await cleanup(first, second)


async def test_timed_out_reply_is_ignored_without_retrying_the_tool():
    socket = Socket()
    peer = Peer("pc", socket, {}, timeout=.02)
    first = asyncio.create_task(peer.call("tool_call", {"name": "first"}))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await finish(first)
        first_id = socket.sent[0]["id"]
        peer.resolve({"id": first_id, "result": "late"})
        assert [item["method"] for item in socket.sent] == ["tool_call", "cancel"]
        peer.timeout = 2
        socket.expected = 2
        socket.entered.clear()
        second = asyncio.create_task(peer.call("tool_call", {"name": "second"}))
        try:
            await asyncio.wait_for(socket.entered.wait(), .5)
            peer.resolve({"id": first_id, "error": "another late response"})
            second_id = socket.sent[-1]["id"]
            peer.resolve({"id": second_id, "result": "new result"})
            assert await finish(second) == "new result"
            assert not peer.pending
        finally:
            await cleanup(second)
    finally:
        await cleanup(first)


async def test_disconnect_during_send_consumes_unawaited_future_exception():
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    errors = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    async def scenario():
        socket = Socket(block="tool_call")
        peer = Peer("pc", socket, {}, timeout=10)
        task = asyncio.create_task(peer.call("tool_call", {}))
        try:
            await asyncio.wait_for(socket.entered.wait(), .5)
            peer.disconnect()
            with pytest.raises(RoutingError, match="disconnected"):
                await finish(task)
        finally:
            socket.release.set()
            await cleanup(task)

    try:
        await scenario()
        gc.collect()
        await asyncio.sleep(0)
        assert not errors
    finally:
        loop.set_exception_handler(previous)


async def test_socket_failure_is_not_retried_and_cleans_pending_state():
    class FailedSocket:
        def __init__(self):
            self.calls = 0

        async def send_json(self, _message):
            self.calls += 1
            raise OSError("simulated broken connection")

    socket = FailedSocket()
    peer = Peer("pc", socket, {}, timeout=.1)
    with pytest.raises(OSError, match="simulated broken"):
        await peer.call("tool_call", {})
    assert socket.calls == 1
    assert not peer.pending
    assert not peer.lock.locked()


class AsyncCleanupSocket(Socket):
    """A cooperative sender whose finally block needs more asynchronous I/O."""

    def __init__(self, block):
        super().__init__(block=block)
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.send_finished = asyncio.Event()
        self.close_calls = 0
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_cleanup_release = asyncio.Event()
        self.close_finished = asyncio.Event()

    async def send_json(self, message):
        self.sent.append(message)
        self.entered.set()
        if message["method"] == self.block:
            try:
                await self.release.wait()
            finally:
                self.cleanup_started.set()
                await self.cleanup_release.wait()
                self.send_finished.set()

    async def close(self, code):
        assert code == 1011
        self.close_calls += 1
        self.close_started.set()
        try:
            await self.close_release.wait()
        finally:
            await self.close_cleanup_release.wait()
            self.close_finished.set()

    def finish_cleanup(self):
        self.release.set()
        self.cleanup_release.set()
        self.close_release.set()
        self.close_cleanup_release.set()


@pytest.mark.parametrize("cause", ["timeout", "cancel"])
async def test_async_send_cleanup_is_bounded_and_retires_the_connection(cause):
    socket = AsyncCleanupSocket("tool_call")
    peer = Peer("pc", socket, {}, timeout=.02)
    first = asyncio.create_task(peer.call("tool_call", {"name": "first"}))
    second = None
    try:
        await asyncio.wait_for(socket.entered.wait(), .5)
        # The first call retains its original budget. This waiter would wait
        # ten seconds unless connection retirement wakes it independently.
        peer.timeout = 10
        second = asyncio.create_task(peer.call("tool_call", {"name": "second"}))
        await asyncio.sleep(0)
        if cause == "cancel":
            first.cancel()
        expected = asyncio.CancelledError if cause == "cancel" else asyncio.TimeoutError
        with pytest.raises(expected):
            await finish(first)
        with pytest.raises(RoutingError, match="disconnected"):
            await finish(second)
        assert socket.cleanup_started.is_set()
        assert socket.close_started.is_set()
        assert socket.close_calls == 1
        assert not socket.send_finished.is_set()
        assert not socket.close_finished.is_set()
        assert [item["method"] for item in socket.sent] == ["tool_call"]
        assert not peer.pending
        with pytest.raises(RoutingError, match="disconnected"):
            await peer.call("tool_call", {})
    finally:
        socket.finish_cleanup()
        await cleanup(first, *([second] if second is not None else []))
        await asyncio.wait_for(socket.send_finished.wait(), .5)
        await asyncio.wait_for(socket.close_finished.wait(), .5)


async def test_cancel_notification_async_cleanup_cannot_hold_the_caller():
    socket = AsyncCleanupSocket("cancel")
    peer = Peer("pc", socket, {}, timeout=.02)
    task = asyncio.create_task(peer.call("tool_call", {}))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await finish(task)
        assert [item["method"] for item in socket.sent] == ["tool_call", "cancel"]
        assert socket.cleanup_started.is_set()
        assert socket.close_calls == 1
        assert not socket.send_finished.is_set()
        assert not peer.pending
    finally:
        socket.finish_cleanup()
        await cleanup(task)
        await asyncio.wait_for(socket.send_finished.wait(), .5)
        await asyncio.wait_for(socket.close_finished.wait(), .5)


async def test_reconnect_replaces_stale_peer_and_fails_its_pending_calls():
    from agent_nexus.federation import Hub

    class Closable(Socket):
        def __init__(self):
            super().__init__()
            self.closed_with = None

        async def close(self, code=None):
            self.closed_with = code

    hub = Hub(timeout=5)
    catalog = {"workspaces": {}, "mcps": {"workspace": {"tools": []}}}
    stale_socket = Closable()
    stale = hub.register("pc", stale_socket, catalog)
    call = asyncio.create_task(stale.call("tool_call", {}))
    await asyncio.wait_for(stale_socket.entered.wait(), 1)

    fresh = hub.register("pc", Socket(), catalog)

    assert hub.peer("pc") is fresh
    with pytest.raises(RoutingError, match="disconnected"):
        await finish(call)
    await asyncio.sleep(0)
    assert stale_socket.closed_with == 1011
    # The old ingress loop unregisters its own peer when it ends; that must not
    # remove the replacement.
    hub.unregister(stale)
    assert hub.peer("pc") is fresh
