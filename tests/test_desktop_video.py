"""Remote desktop video (H.264, the pointer's shape, continuous updates) and Ubuntu in-app updates."""
from __future__ import annotations

import asyncio
import json
import struct

import pytest


class FakeScreen:
    left = top = 0
    width, height = 64, 48

    def __init__(self):
        self.frames = [b"\x10" * (64 * 48 * 4), b"\x20" * (64 * 48 * 4)]

    def grab_raw(self):
        return (self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]), (64, 48)

    def grab(self):
        from PIL import Image
        return Image.new("RGB", (64, 48))

    def cursor(self, known=None):
        from PIL import Image
        if known == 7:
            return 7, None, None
        image = Image.new("RGBA", (4, 2), (255, 255, 255, 255))
        image.putpixel((3, 1), (0, 0, 0, 0))
        return 7, (1, 0), image

    def close(self):
        pass


class FakeEncoder:
    name = "fake"

    def __init__(self, width, height, bitrate):
        self.width, self.height = width, height
        self.bitrate = bitrate
        self.frames = []

    def encode(self, raw, size, keyframe=False):
        self.frames.append(keyframe)
        return b"\x00\x00\x00\x01" + (b"\x65" if keyframe else b"\x41") + b"data"

    def set_bitrate(self, bitrate):
        self.bitrate = bitrate
        return True

    def close(self):
        pass


async def _handshake(reader, writer):
    await reader.readexactly(12)
    writer.write(b"RFB 003.008\n")
    await reader.readexactly(2)
    writer.write(b"\x01")
    await reader.readexactly(4)
    writer.write(b"\x01")
    await reader.readexactly(20)
    await reader.readexactly(struct.unpack(">I", await reader.readexactly(4))[0])


def test_h264_frames_carry_the_pointer_shape_and_are_pushed(monkeypatch):
    from agent_nexus import vnc
    monkeypatch.setattr(vnc, "Screen", FakeScreen)
    monkeypatch.setattr(vnc, "make_input", lambda screen: None)
    monkeypatch.setattr(vnc, "H264Encoder", FakeEncoder)
    monkeypatch.setattr(vnc, "h264_available", lambda: (True, ""))

    async def scenario():
        async def serve(reader, writer):
            with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
                await vnc.Session(reader, writer, "host").run()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        await _handshake(reader, writer)
        encodings = [vnc.H264, vnc.TIGHT, vnc.CURSOR, vnc.CONTINUOUS_UPDATES]
        writer.write(struct.pack(">BxH", 2, len(encodings)) + struct.pack(f">{len(encodings)}i", *encodings))
        assert await asyncio.wait_for(reader.readexactly(1), 5) == b"\x96"  # EndOfContinuousUpdates
        writer.write(struct.pack(">BBHHHH", 150, 1, 0, 0, 64, 48))  # EnableContinuousUpdates
        frames = []
        for _ in range(2):  # pushed without a further request
            head = await asyncio.wait_for(reader.readexactly(4), 5)
            count = struct.unpack(">H", head[2:])[0]
            rects = []
            for _ in range(count):
                x, y, w, h, encoding = struct.unpack(">HHHHi", await reader.readexactly(12))
                if encoding == vnc.H264:
                    length, flags = struct.unpack(">II", await reader.readexactly(8))
                    rects.append(("h264", flags, await reader.readexactly(length)))
                elif encoding == vnc.CURSOR:
                    pixels = await reader.readexactly(w * h * 4)
                    mask = await reader.readexactly(((w + 7) // 8) * h)
                    rects.append(("cursor", (x, y, w, h), mask))
            frames.append(rects)
        assert frames[0][0][:2] == ("h264", 1) and frames[0][0][2][4] == 0x65  # a key frame first
        assert frames[0][1] == ("cursor", (1, 0, 4, 2), bytes([0xF0, 0xE0]))  # hotspot, size, mask
        assert frames[1] == [("h264", 0, b"\x00\x00\x00\x01\x41data")]  # the cursor is sent once
        writer.close()
        server.close()

    asyncio.run(scenario())


class BufferingEncoder(FakeEncoder):
    """Like Media Foundation: a frame comes out one encode() call later."""

    def __init__(self, *args):
        super().__init__(*args)
        self.held = None

    def encode(self, raw, size, keyframe=False):
        out, self.held = self.held, raw[:3]
        return b"" if out is None else b"\x00\x00\x00\x01\x41" + bytes(out)


def test_a_buffering_encoder_still_delivers_the_last_change(monkeypatch):
    from agent_nexus import vnc
    monkeypatch.setattr(vnc, "Screen", FakeScreen)
    monkeypatch.setattr(vnc, "make_input", lambda screen: None)
    monkeypatch.setattr(vnc, "H264Encoder", BufferingEncoder)
    monkeypatch.setattr(vnc, "h264_available", lambda: (True, ""))

    async def scenario():
        async def serve(reader, writer):
            with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
                await vnc.Session(reader, writer, "host").run()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        await _handshake(reader, writer)
        encodings = [vnc.H264, vnc.CONTINUOUS_UPDATES]
        writer.write(struct.pack(">BxH", 2, len(encodings)) + struct.pack(f">{len(encodings)}i", *encodings))
        assert await asyncio.wait_for(reader.readexactly(1), 5) == b"\x96"
        writer.write(struct.pack(">BBHHHH", 150, 1, 0, 0, 64, 48))
        payloads = []
        while b"\x20\x20\x20" not in b"".join(payloads):  # the second (last) picture, held by the encoder
            head = await asyncio.wait_for(reader.readexactly(4), 5)
            for _ in range(struct.unpack(">H", head[2:])[0]):
                _x, _y, _w, _h, encoding = struct.unpack(">HHHHi", await reader.readexactly(12))
                length, _flags = struct.unpack(">II", await reader.readexactly(8))
                payloads.append(await reader.readexactly(length))
        writer.close()
        server.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- Ubuntu updates

class Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def test_ubuntu_update_check_and_manual_install(tmp_path, monkeypatch):
    import httpx

    from agent_nexus import __version__, linux_update
    monkeypatch.setattr(linux_update, "WINDOWS", False)  # the Ubuntu package, wherever the test runs
    monkeypatch.setattr(linux_update, "_gateway", lambda root: "https://hub.example.test")
    deb = {"id": "linux-deb", "version": "99.0.0", "filename": "agent-nexus_99.0.0_amd64.deb",
           "url": "https://cdn.example.test/agent-nexus_99.0.0_amd64.deb", "sha256": "0" * 64}
    monkeypatch.setattr(httpx, "get", lambda url, **_: Response({"items": [deb]}))
    found = linux_update.check(tmp_path)
    assert found["available"] and found["version"] == "99.0.0"
    from agent_nexus import desktop
    assert desktop.read_update(tmp_path)["version"] == "99.0.0"

    monkeypatch.setattr(httpx, "get", lambda url, **_: Response({"items": [{**deb, "version": __version__}]}))
    assert linux_update.check(tmp_path)["available"] is False
    monkeypatch.setattr(httpx, "get", lambda url, **_: Response({"items": [deb]}))
    linux_update.check(tmp_path)

    # a download that does not match the published SHA-256 is refused
    class Stream:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self, size):
            yield b"not the package"

    monkeypatch.setattr(httpx, "stream", Stream)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    with pytest.raises(linux_update.UpdateError):
        linux_update.apply(tmp_path)

    import hashlib
    monkeypatch.setattr(httpx, "get", lambda url, **_: Response({"items": [{**deb, "sha256": hashlib.sha256(b"not the package").hexdigest()}]}))
    linux_update.check(tmp_path)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    # no root, no passwordless sudo (CI runners have one), no desktop session: the command to run
    monkeypatch.setattr(linux_update, "_privileged", lambda unattended: None)
    result = linux_update.apply(tmp_path)
    assert result["installed"] is False and "apt install" in result["manual"]
    assert (tmp_path / "cache" / "agent-nexus" / "agent-nexus_99.0.0_amd64.deb").read_bytes() == b"not the package"


def test_check_without_a_gateway_records_the_reason(tmp_path, monkeypatch):
    from agent_nexus import linux_update
    monkeypatch.setattr(linux_update, "_gateway", lambda root: None)
    result = linux_update.check(tmp_path)
    assert result["available"] is False and "网关" in result["error"]
    assert json.loads((tmp_path / ".anx" / "desktop-update.json").read_text(encoding="utf-8"))["error"]


def test_windows_system_update_picks_the_installer_and_schedules_it(tmp_path, monkeypatch):
    import httpx

    from agent_nexus import linux_update
    monkeypatch.setattr(linux_update, "WINDOWS", True)
    monkeypatch.setattr(linux_update, "_gateway", lambda root: "https://hub.example.test")
    items = [{"id": "linux-deb", "version": "99.0.0", "filename": "agent-nexus_99.0.0_amd64.deb", "url": "u1"},
             {"id": "windows-x64", "version": "99.0.0", "filename": "agent-nexus_99.0.0_x64-setup.exe", "url": "u2",
              "sha256": "0" * 64}]
    monkeypatch.setattr(httpx, "get", lambda url, **_: Response({"items": items}))
    found = linux_update.check(tmp_path)
    assert found["available"] and found["deb"]["filename"].endswith("_x64-setup.exe")
    package = tmp_path / "agent-nexus_99.0.0_x64-setup.exe"
    package.write_bytes(b"MZ")
    monkeypatch.setattr(linux_update, "_download", lambda deb: package)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        import subprocess
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(linux_update.subprocess, "run", run)
    result = linux_update.apply(tmp_path)
    assert result == {"installed": True, "version": "99.0.0", "restart": "system"}
    create = next(c for c in calls if "/Create" in c)
    assert create[create.index("/RU") + 1] == "SYSTEM"
    assert "/S /AllUsers /UPDATE" in create[create.index("/TR") + 1]
    assert any("/Run" in c for c in calls)


# --------------------------------------------------------------------------- rate, pacing, quality

def test_libx264_follows_a_new_bit_rate(monkeypatch):
    """x264 takes a new rate at run time only with VBV on from the start."""
    pytest.importorskip("av")
    import os
    import time as _time

    from agent_nexus import vnc
    monkeypatch.setenv("ANX_DESKTOP_ENCODER", "libx264")
    width, height = 320, 240

    def rate(encoder, seconds=1.2):
        start, total = _time.monotonic(), 0
        while _time.monotonic() - start < seconds:
            total += len(encoder.encode(os.urandom(width * height * 4), (width, height)))
            _time.sleep(1 / 30)
        return total * 8 / (_time.monotonic() - start)

    encoder = vnc.H264Encoder(width, height, 4_000_000)
    rate(encoder, 0.6)
    high = rate(encoder)
    assert encoder.set_bitrate(500_000)
    rate(encoder, 0.6)
    low = rate(encoder)
    assert low < high / 3, (high, low)
    encoder.close()


class NoisyScreen(FakeScreen):
    """Changes on every capture, instantly."""

    def __init__(self):
        self.n = 0

    def grab_raw(self):
        self.n += 1
        return bytes([self.n % 256]) * (64 * 48 * 4), (64, 48)

    def cursor(self, known=None):
        return None


def _push_session(monkeypatch, screen):
    from agent_nexus import vnc
    monkeypatch.setattr(vnc, "Screen", screen)
    monkeypatch.setattr(vnc, "make_input", lambda screen: None)
    monkeypatch.setattr(vnc, "H264Encoder", FakeEncoder)
    monkeypatch.setattr(vnc, "h264_available", lambda: (True, ""))
    return vnc


async def _frames(reader, seconds):
    """Count FramebufferUpdates for ``seconds`` (answering nothing else)."""
    import time as _time
    count, end = 0, _time.monotonic() + seconds
    while _time.monotonic() < end:
        try:
            head = await asyncio.wait_for(reader.readexactly(4), max(0.01, end - _time.monotonic()))
        except asyncio.TimeoutError:
            break
        for _ in range(struct.unpack(">H", head[2:])[0]):
            _x, _y, w, h, encoding = struct.unpack(">HHHHi", await reader.readexactly(12))
            if encoding == 50:
                length, _flags = struct.unpack(">II", await reader.readexactly(8))
                await reader.readexactly(length)
        count += 1
    return count


def test_frames_keep_to_the_frame_interval_with_the_prefetch(monkeypatch):
    vnc = _push_session(monkeypatch, NoisyScreen)
    monkeypatch.setattr(vnc, "FRAME_INTERVAL", 1 / 20)

    async def scenario():
        async def serve(reader, writer):
            with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
                await vnc.Session(reader, writer, "host").run()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        await _handshake(reader, writer)
        encodings = [vnc.H264, vnc.CONTINUOUS_UPDATES, vnc.QUALITY_BASE + 9]  # no fences: the cap, 60 fps rule
        writer.write(struct.pack(">BxH", 2, len(encodings)) + struct.pack(f">{len(encodings)}i", *encodings))
        assert await asyncio.wait_for(reader.readexactly(1), 5) == b"\x96"
        writer.write(struct.pack(">BBHHHH", 150, 1, 0, 0, 64, 48))
        await _frames(reader, 0.3)
        count = await _frames(reader, 1.0)
        assert 12 <= count <= 23, count  # about 20 a second, not twice that
        writer.close()
        server.close()

    asyncio.run(scenario())


def test_a_new_quality_takes_effect_on_a_still_screen(monkeypatch):
    class Big(FakeScreen):
        width, height = 2560, 1440

        def grab_raw(self):
            return b"\x10" * (2560 * 1440 * 4), (2560, 1440)

        def cursor(self, known=None):
            return None

    vnc = _push_session(monkeypatch, Big)

    async def scenario():
        async def serve(reader, writer):
            with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
                await vnc.Session(reader, writer, "host").run()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        await reader.readexactly(12)
        writer.write(b"RFB 003.008\n")
        await reader.readexactly(2)
        writer.write(b"\x01")
        await reader.readexactly(4)
        writer.write(b"\x01")
        width, height = struct.unpack(">HH", await reader.readexactly(4))
        assert (width, height) == vnc.encoded_size(2560, 1440, vnc.DEFAULT_QUALITY) != (2560, 1440)
        await reader.readexactly(16)
        await reader.readexactly(struct.unpack(">I", await reader.readexactly(4))[0])
        encodings = [vnc.H264, vnc.DESKTOP_SIZE, vnc.QUALITY_BASE + 6]
        writer.write(struct.pack(">BxH", 2, len(encodings)) + struct.pack(f">{len(encodings)}i", *encodings))
        writer.write(struct.pack(">BBHHHH", 3, 0, 0, 0, width, height))
        await _frames(reader, 0.5)  # the first (key) frame; then the screen stands still
        encodings[-1] = vnc.QUALITY_BASE + 9  # 原画
        writer.write(struct.pack(">BxH", 2, len(encodings)) + struct.pack(f">{len(encodings)}i", *encodings))
        writer.write(struct.pack(">BBHHHH", 3, 1, 0, 0, width, height))
        head = await asyncio.wait_for(reader.readexactly(4), 3)
        assert struct.unpack(">H", head[2:])[0] == 1
        x, y, w, h, encoding = struct.unpack(">HHHHi", await reader.readexactly(12))
        assert (encoding, w, h) == (vnc.DESKTOP_SIZE, 2560, 1440)
        writer.close()
        server.close()

    asyncio.run(scenario())
