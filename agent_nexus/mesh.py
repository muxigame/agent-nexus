"""Keeping this device inside the relay mesh (docs/relay-mesh.md); the client side of it.

A **relay** keeps a standing tunnel to every other online relay. A **member** measures the
round trip to the relays, keeps the two nearest as its *home relays* (standing tunnels, pinned
in the Connector so the idle reaper leaves them alone) and asks each to warm a tunnel back to
it, so that a stream relayed to it does not wait for a punch.

Failures back off (1, 2, 4 ... 30 minutes per relay); the home relays are re-measured every
``REMEASURE_S`` and only replaced when another is clearly nearer (``SWITCH_RATIO``).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import statistics
import time
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

from .remote_hub import PING, PONG
HOME_RELAYS = 2
TICK_S = 60.0
FIRST_TICK_S = 20.0
REMEASURE_S = 600.0
WARM_EVERY_S = 300.0
SWITCH_RATIO = 0.7  # a candidate replaces a home relay only below 70% of its round trip
BACKOFF_MIN_S, BACKOFF_MAX_S = 60.0, 1800.0


async def ping(port: int, count: int = 3, timeout: float = 5.0) -> float:
    """Median round trip in ms of ``ANX-PING`` lines through the tunnel at ``port``."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout)
    samples: list[float] = []
    try:
        for _ in range(count):
            start = time.perf_counter()
            writer.write(PING)
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout)
            if line != PONG:
                raise ConnectionError("no pong (the other side is older than relay/1)")
            samples.append((time.perf_counter() - start) * 1000)
    finally:
        writer.close()
    return statistics.median(samples)


class MeshManager:
    def __init__(self, *, me: Callable[[], str], is_relay: bool,
                 list_devices: Callable[[], Awaitable[list[dict]]],
                 tunnel_port: Callable[[str], Awaitable[tuple[int, str]]],
                 warm: Callable[[str], Awaitable[None]],
                 connector: Any):
        self.me, self.is_relay = me, is_relay
        self.list_devices, self.tunnel_port, self.warm, self.connector = list_devices, tunnel_port, warm, connector
        self.home: list[str] = []
        self.rtt: dict[str, float] = {}          # relay -> last measured ms
        self.routes: dict[str, str] = {}         # relay -> tunnel route
        self.errors: dict[str, str] = {}
        self.backoff: dict[str, tuple[float, float]] = {}  # relay -> (retry at, current delay)
        self.measured_at = 0.0
        self.warmed: dict[str, float] = {}
        self.pinned: set[str] = set()
        self.task: asyncio.Task | None = None
        self.last_tick = 0.0

    # -- lifecycle
    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(BaseException):
                await self.task
        self._pin(set())

    def wake(self) -> None:
        """The network changed (or a link broke): retry everything now."""
        self.backoff.clear()
        self.measured_at = 0.0

    async def _loop(self) -> None:
        await asyncio.sleep(FIRST_TICK_S)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.info("mesh: tick failed: %s", exc)
            await asyncio.sleep(TICK_S)

    # -- one round
    def _ready(self, relay: str) -> bool:
        retry = self.backoff.get(relay)
        return retry is None or time.monotonic() >= retry[0]

    def _failed(self, relay: str, error: str) -> None:
        _, delay = self.backoff.get(relay, (0.0, BACKOFF_MIN_S / 2))
        delay = min(BACKOFF_MAX_S, max(BACKOFF_MIN_S, delay * 2))
        self.backoff[relay] = (time.monotonic() + delay, delay)
        self.errors[relay] = error[:160]
        self.rtt.pop(relay, None)
        with contextlib.suppress(Exception):
            self.connector.forget(relay)

    async def _measure(self, relay: str) -> float | None:
        try:
            port, route = await self.tunnel_port(relay)
            rtt = await ping(port)
        except Exception as exc:
            self._failed(relay, str(getattr(exc, "message", None) or exc) or type(exc).__name__)
            return None
        self.backoff.pop(relay, None)
        self.errors.pop(relay, None)
        self.rtt[relay], self.routes[relay] = rtt, route
        return rtt

    async def tick(self) -> None:
        self.last_tick = time.time()
        me = self.me()
        devices = await self.list_devices()
        relays = [d["device_id"] for d in devices
                  if d.get("relay") and d.get("online") and d.get("device_id") and d["device_id"] != me]
        if self.is_relay:
            # relays keep standing tunnels to all the other relays
            for relay in relays:
                if self._ready(relay):
                    await self._measure(relay)
            self.home = [r for r in relays if r in self.rtt]
            self._pin(set(self.home))
            return
        now = time.monotonic()
        remeasure = now - self.measured_at > REMEASURE_S or not self.home \
            or any(h not in relays for h in self.home)
        if remeasure:
            for relay in relays:
                if self._ready(relay):
                    await self._measure(relay)
            self.measured_at = now
        else:
            for relay in list(self.home):  # keep the standing ones standing (rebuilt if lost)
                if await self._measure(relay) is None:
                    self.home.remove(relay)
        self.home = self._choose([r for r in relays if r in self.rtt])
        self._pin(set(self.home))
        for relay in self.home:
            if now - self.warmed.get(relay, -1e9) > WARM_EVERY_S:
                try:
                    await self.warm(relay)
                    self.warmed[relay] = now
                except Exception as exc:
                    log.info("mesh: warming %s failed: %s", relay, exc)
        log.info("mesh: home relays %s", ", ".join(f"{r} ({self.rtt[r]:.0f} ms)" for r in self.home) or "none")

    def _choose(self, measured: list[str]) -> list[str]:
        """The nearest HOME_RELAYS, keeping a current one unless another is clearly nearer."""
        ranked = sorted(measured, key=lambda r: self.rtt[r])
        keep = [h for h in self.home if h in self.rtt]
        for candidate in ranked:
            if len(keep) < HOME_RELAYS:
                if candidate not in keep:
                    keep.append(candidate)
                continue
            worst = max(keep, key=lambda r: self.rtt[r])
            if candidate not in keep and self.rtt[candidate] < self.rtt[worst] * SWITCH_RATIO:
                keep[keep.index(worst)] = candidate
        return sorted(keep, key=lambda r: self.rtt[r])[:HOME_RELAYS]

    def _pin(self, wanted: set[str]) -> None:
        pin = getattr(self.connector, "pin", None)
        unpin = getattr(self.connector, "unpin", None)
        for relay in self.pinned - wanted:
            if unpin:
                unpin(relay)
        for relay in wanted - self.pinned:
            if pin:
                pin(relay)
        self.pinned = set(wanted)

    # -- for routing and the console
    def preferred(self) -> list[str]:
        """Relays to try, nearest first: the home ones, then the other measured ones."""
        rest = sorted((r for r in self.rtt if r not in self.home), key=lambda r: self.rtt[r])
        return [*self.home, *rest]

    def status(self) -> dict:
        return {"role": "relay" if self.is_relay else "member",
                "home": [{"device_id": r, "rtt_ms": round(self.rtt.get(r, 0)), "route": self.routes.get(r)}
                         for r in self.home],
                "measured": {r: round(v) for r, v in self.rtt.items()},
                "errors": dict(self.errors),
                "backoff": {r: round(at - time.monotonic()) for r, (at, _) in self.backoff.items()},
                "last_tick": self.last_tick}
