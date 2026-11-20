"""A small built-in VNC (RFB 3.8) server for remote desktop over the P2P tunnel.

It serves one viewer per stream that :class:`~agent_nexus.remote_hub.RemoteHub` hands it after
the ticket check, so it needs no VNC password ("None" security). The screen is captured with
``mss`` (Windows, X11); changed areas are found with Pillow and sent as Tight/JPEG (noVNC and
most viewers), or Raw for a viewer without Tight. Input goes in with ``SendInput`` on Windows
and XTEST on X11 (``python-xlib``); without an input backend the session is view-only.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import struct
import sys
import time

log = logging.getLogger(__name__)
RAW, TIGHT = 0, 7
DESKTOP_SIZE = -223
JPEG_QUALITY = {0: 30, 1: 40, 2: 50, 3: 60, 4: 65, 5: 70, 6: 75, 7: 80, 8: 85, 9: 92}
#: noVNC's quality levels (-32 .. -23 => 0 .. 9)
QUALITY_BASE = -32
FRAME_INTERVAL = 1 / 30
IDLE_POLL = 0.05


def available() -> tuple[bool, str]:
    """Whether this device has a desktop to share (and why not)."""
    try:
        import mss  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        return False, f"missing {exc.name}"
    if sys.platform.startswith("linux"):
        if not os.environ.get("DISPLAY"):
            return False, "no X11 display (a server without a desktop offers the shell only)"
    if sys.platform == "win32":
        try:
            import ctypes
            if ctypes.windll.user32.GetDesktopWindow() == 0:
                return False, "no interactive desktop"
        except Exception:
            return False, "no interactive desktop"
    return True, ""


# --------------------------------------------------------------------------- capture

class Screen:
    def __init__(self):
        import mss
        self.mss = mss.mss()
        monitors = self.mss.monitors
        self.monitor = monitors[1] if len(monitors) > 1 else monitors[0]
        self.left, self.top = self.monitor["left"], self.monitor["top"]
        self.width, self.height = self.monitor["width"], self.monitor["height"]

    def grab(self):
        from PIL import Image
        shot = self.mss.grab(self.monitor)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def close(self):
        with contextlib.suppress(Exception):
            self.mss.close()


# --------------------------------------------------------------------------- input

# X11 keysyms -> Windows virtual keys (the ones that are not plain characters)
KEYSYM_VK = {
    0xFF08: 0x08, 0xFF09: 0x09, 0xFF0D: 0x0D, 0xFF1B: 0x1B, 0xFFFF: 0x2E, 0xFF63: 0x2D,
    0xFF50: 0x24, 0xFF57: 0x23, 0xFF55: 0x21, 0xFF56: 0x22,
    0xFF51: 0x25, 0xFF52: 0x26, 0xFF53: 0x27, 0xFF54: 0x28,
    0xFFE1: 0xA0, 0xFFE2: 0xA1, 0xFFE3: 0xA2, 0xFFE4: 0xA3, 0xFFE9: 0xA4, 0xFFEA: 0xA5,
    0xFFE7: 0x5B, 0xFFE8: 0x5C, 0xFFEB: 0x5B, 0xFFEC: 0x5C, 0xFF67: 0x5D,
    0xFFE5: 0x14, 0xFF7F: 0x90, 0xFF14: 0x91, 0xFF13: 0x13, 0xFF61: 0x2C,
    0xFF8D: 0x0D, 0xFFAA: 0x6A, 0xFFAB: 0x6B, 0xFFAD: 0x6D, 0xFFAE: 0x6E, 0xFFAF: 0x6F,
    **{0xFFBE + i: 0x70 + i for i in range(12)},  # F1..F12
    **{0xFFB0 + i: 0x60 + i for i in range(10)},  # keypad 0..9
}
EXTENDED_VK = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0xA3, 0xA5, 0x5B, 0x5C, 0x5D, 0x6F}


class WindowsInput:
    def __init__(self, screen: Screen):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.screen = screen
        self.buttons = 0
        ULONG_PTR = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

        class UNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", UNION)]

        self.INPUT, self.MOUSEINPUT, self.KEYBDINPUT = INPUT, MOUSEINPUT, KEYBDINPUT
        self.vx = self.user32.GetSystemMetrics(76)
        self.vy = self.user32.GetSystemMetrics(77)
        self.vw = max(1, self.user32.GetSystemMetrics(78))
        self.vh = max(1, self.user32.GetSystemMetrics(79))

    def _send(self, *inputs):
        array = (self.INPUT * len(inputs))(*inputs)
        self.user32.SendInput(len(inputs), array, self.ctypes.sizeof(self.INPUT))

    def _mouse(self, flags, dx=0, dy=0, data=0):
        item = self.INPUT(type=0)
        item.u.mi = self.MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
        return item

    def pointer(self, mask: int, x: int, y: int) -> None:
        sx = self.screen.left + x
        sy = self.screen.top + y
        ax = int((sx - self.vx) * 65535 / max(1, self.vw - 1))
        ay = int((sy - self.vy) * 65535 / max(1, self.vh - 1))
        events = [self._mouse(0x0001 | 0x8000 | 0x4000, ax, ay)]  # MOVE | ABSOLUTE | VIRTUALDESK
        for bit, down, up in ((1, 0x0002, 0x0004), (2, 0x0020, 0x0040), (4, 0x0008, 0x0010)):
            if (mask & bit) != (self.buttons & bit):
                events.append(self._mouse(down if mask & bit else up))
        if mask & 8 and not self.buttons & 8:
            events.append(self._mouse(0x0800, data=120))
        if mask & 16 and not self.buttons & 16:
            events.append(self._mouse(0x0800, data=-120))
        if mask & 32 and not self.buttons & 32:
            events.append(self._mouse(0x01000, data=-120))
        if mask & 64 and not self.buttons & 64:
            events.append(self._mouse(0x01000, data=120))
        self.buttons = mask
        self._send(*events)

    def key(self, down: bool, keysym: int) -> None:
        flags = 0 if down else 0x0002
        vk = KEYSYM_VK.get(keysym)
        if vk is None and 0x20 <= keysym <= 0x7E:
            scan = self.user32.VkKeyScanW(keysym)
            if scan != -1 and (scan & 0xFF) != 0xFF:
                vk = scan & 0xFF
        if vk is not None:
            if vk in EXTENDED_VK:
                flags |= 0x0001
            item = self.INPUT(type=1)
            item.u.ki = self.KEYBDINPUT(vk, 0, flags, 0, 0)
            self._send(item)
            return
        char = keysym - 0x01000000 if keysym >= 0x01000000 else keysym
        if 0 < char <= 0xFFFF:
            item = self.INPUT(type=1)
            item.u.ki = self.KEYBDINPUT(0, char, flags | 0x0004, 0, 0)  # KEYEVENTF_UNICODE
            self._send(item)


class X11Input:
    def __init__(self, screen: Screen):
        from Xlib import X, XK, display  # python-xlib
        from Xlib.ext import xtest

        self.X, self.xtest = X, xtest
        self.display = display.Display()
        self.screen = screen
        self.buttons = 0

    def pointer(self, mask: int, x: int, y: int) -> None:
        d, X = self.display, self.X
        self.xtest.fake_input(d, X.MotionNotify, x=self.screen.left + x, y=self.screen.top + y)
        for bit, button in ((1, 1), (2, 2), (4, 3), (8, 4), (16, 5), (32, 6), (64, 7)):
            if (mask & bit) != (self.buttons & bit):
                self.xtest.fake_input(d, X.ButtonPress if mask & bit else X.ButtonRelease, button)
        self.buttons = mask
        d.sync()

    def key(self, down: bool, keysym: int) -> None:
        code = self.display.keysym_to_keycode(keysym)
        if code:
            self.xtest.fake_input(self.display, self.X.KeyPress if down else self.X.KeyRelease, code)
            self.display.sync()


def make_input(screen: Screen):
    try:
        if sys.platform == "win32":
            return WindowsInput(screen)
        return X11Input(screen)
    except Exception as exc:
        log.warning("remote desktop input unavailable (%s); view only", exc)
        return None


# --------------------------------------------------------------------------- RFB

def _compact_length(n: int) -> bytes:
    out = bytearray([n & 0x7F])
    if n > 0x7F:
        out[0] |= 0x80
        out.append((n >> 7) & 0x7F)
        if n > 0x3FFF:
            out[1] |= 0x80
            out.append((n >> 14) & 0xFF)
    return bytes(out)


class PixelFormat:
    def __init__(self, bpp=32, depth=24, big_endian=0, true_color=1, red_max=255, green_max=255, blue_max=255,
                 red_shift=16, green_shift=8, blue_shift=0):
        self.bpp, self.depth, self.big_endian, self.true_color = bpp, depth, big_endian, true_color
        self.red_max, self.green_max, self.blue_max = red_max, green_max, blue_max
        self.red_shift, self.green_shift, self.blue_shift = red_shift, green_shift, blue_shift

    def pack(self) -> bytes:
        return struct.pack(">BBBBHHHBBB3x", self.bpp, self.depth, self.big_endian, self.true_color,
                           self.red_max, self.green_max, self.blue_max, self.red_shift, self.green_shift, self.blue_shift)

    @classmethod
    def unpack(cls, data: bytes) -> "PixelFormat":
        return cls(*struct.unpack(">BBBBHHHBBB3x", data))

    def raw(self, image) -> bytes:
        """RGB image -> this pixel format (32 bpp true colour, 8 bits per channel)."""
        r, g, b = image.split()
        from PIL import Image
        alpha = Image.new("L", image.size, 0)
        order = sorted(((self.red_shift, r), (self.green_shift, g), (self.blue_shift, b), (24, alpha)), key=lambda x: x[0])
        planes = [plane for _, plane in order]
        if self.big_endian:
            planes.reverse()
        return Image.merge("RGBA", planes).tobytes()


class Session:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, name: str):
        self.reader, self.writer, self.name = reader, writer, name
        # mss keeps per-thread handles: every capture call runs on this one thread.
        from concurrent.futures import ThreadPoolExecutor
        self.capture = ThreadPoolExecutor(max_workers=1, thread_name_prefix="anx-vnc")
        self.screen = None
        self.input = None
        self.format = PixelFormat()
        self.encodings: list[int] = [RAW]
        self.quality = 6
        self.pending: asyncio.Event = asyncio.Event()
        self.full = True
        self.previous = None

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self.screen = await loop.run_in_executor(self.capture, Screen)
        self.input = make_input(self.screen)
        try:
            await self._handshake()
            sender = asyncio.create_task(self._updates())
            try:
                await self._messages()
            finally:
                sender.cancel()
                with contextlib.suppress(BaseException):
                    await sender
        finally:
            with contextlib.suppress(Exception):
                await loop.run_in_executor(self.capture, self.screen.close)
            self.capture.shutdown(wait=False)

    async def _handshake(self) -> None:
        w, r = self.writer, self.reader
        w.write(b"RFB 003.008\n")
        await w.drain()
        version = await r.readexactly(12)
        if not version.startswith(b"RFB 003."):
            raise ValueError("not an RFB client")
        minor = int(version[8:11] or b"8")
        if minor >= 7:
            w.write(b"\x01\x01")  # one security type: None
            await w.drain()
            await r.readexactly(1)
        else:
            w.write(struct.pack(">I", 1))
        if minor >= 8:
            w.write(struct.pack(">I", 0))  # SecurityResult OK
        await w.drain()
        await r.readexactly(1)  # ClientInit (shared flag)
        name = self.name.encode("utf-8")[:255]
        w.write(struct.pack(">HH", self.screen.width, self.screen.height) + self.format.pack() + struct.pack(">I", len(name)) + name)
        await w.drain()

    async def _messages(self) -> None:
        r = self.reader
        while True:
            kind = (await r.readexactly(1))[0]
            if kind == 0:  # SetPixelFormat
                data = await r.readexactly(19)
                self.format = PixelFormat.unpack(data[3:19])
            elif kind == 2:  # SetEncodings
                head = await r.readexactly(3)
                count = struct.unpack(">H", head[1:3])[0]
                values = struct.unpack(f">{count}i", await r.readexactly(4 * count))
                self.encodings = list(values)
                levels = [v for v in values if QUALITY_BASE <= v <= QUALITY_BASE + 9]
                if levels:
                    self.quality = levels[0] - QUALITY_BASE
            elif kind == 3:  # FramebufferUpdateRequest
                incremental, *_ = struct.unpack(">BHHHH", await r.readexactly(9))
                if not incremental:
                    self.full = True
                self.pending.set()
            elif kind == 4:  # KeyEvent
                down, _, keysym = struct.unpack(">BHI", await r.readexactly(7))
                if self.input is not None:
                    await asyncio.to_thread(self.input.key, bool(down), keysym)
            elif kind == 5:  # PointerEvent
                mask, x, y = struct.unpack(">BHH", await r.readexactly(5))
                if self.input is not None:
                    await asyncio.to_thread(self.input.pointer, mask, x, y)
            elif kind == 6:  # ClientCutText
                head = await r.readexactly(7)
                length = struct.unpack(">I", head[3:7])[0]
                if length > 1 << 20:
                    raise ValueError("clipboard too large")
                await r.readexactly(length)
            else:
                raise ValueError(f"unknown RFB message {kind}")

    async def _updates(self) -> None:
        from PIL import ImageChops

        last_sent = 0.0
        while True:
            await self.pending.wait()
            wait = FRAME_INTERVAL - (time.monotonic() - last_sent)
            if wait > 0:
                await asyncio.sleep(wait)
            while True:
                image = await asyncio.get_running_loop().run_in_executor(self.capture, self.screen.grab)
                if self.full or self.previous is None or self.previous.size != image.size:
                    box = (0, 0, image.width, image.height)
                else:
                    box = await asyncio.to_thread(lambda: ImageChops.difference(image, self.previous).getbbox())
                if box:
                    break
                await asyncio.sleep(IDLE_POLL)
            self.pending.clear()
            self.full = False
            self.previous = image
            payload = await asyncio.to_thread(self._rect, image, box)
            self.writer.write(struct.pack(">BxH", 0, 1) + payload)
            await self.writer.drain()
            last_sent = time.monotonic()

    def _rect(self, image, box) -> bytes:
        x0, y0, x1, y1 = box
        crop = image.crop(box)
        header = struct.pack(">HHHH", x0, y0, x1 - x0, y1 - y0)
        if TIGHT in self.encodings:
            buffer = io.BytesIO()
            crop.save(buffer, "JPEG", quality=JPEG_QUALITY.get(self.quality, 75), optimize=False)
            data = buffer.getvalue()
            return header + struct.pack(">i", TIGHT) + b"\x90" + _compact_length(len(data)) + data
        return header + struct.pack(">i", RAW) + self.format.raw(crop)


class DesktopService:
    """The ``desktop`` service of the remote hub."""

    def __init__(self, name: str):
        self.name = name

    async def serve(self, request: dict, reader, writer) -> None:
        from . import remote_hub as rh
        ok, why = available()
        if not ok:
            writer.write(rh.frame(rh.ERROR, {"error": f"这台设备不能共享桌面：{why}"}))
            await writer.drain()
            return
        writer.write(rh.frame(rh.READY, {"service": "desktop"}))
        await writer.drain()
        await Session(reader, writer, self.name).run()
