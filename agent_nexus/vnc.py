"""A small built-in VNC (RFB 3.8) server for remote desktop over the P2P tunnel.

It serves one viewer per stream that :class:`~agent_nexus.remote_hub.RemoteHub` hands it after
the ticket check, so it needs no VNC password ("None" security). The screen is captured with
``mss`` (Windows, X11). The pointer is never drawn into the picture -- the viewer shows its
own, local pointer at once, and the server only sends the pointer's *shape* (RFB Cursor
pseudo-encoding) when it changes on this machine (arrow, I-beam, hand...).

Video: H.264 (RFB encoding 50, noVNC decodes it with WebCodecs -- in hardware) from the first
encoder that works here: NVENC, Quick Sync, AMF, Media Foundation (Windows), VAAPI (Linux),
then libx264 -- all low latency (no B-frames, zero-latency tuning, no periodic key frames), a
frame only when the screen or the pointer changed. Fences pace the frames to what the link carries
and the bit rate follows it (:class:`Flow`); below the best quality a large screen is scaled to
about 1920x1200. A viewer without H.264 gets Tight/JPEG of the changed area, or
Raw. Input goes in with ``SendInput`` on Windows and XTEST on X11 (``python-xlib``); without
an input backend the session is view-only.
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
from collections import deque

log = logging.getLogger(__name__)
RAW, TIGHT, H264 = 0, 7, 50
DESKTOP_SIZE = -223
CURSOR = -239
#: ContinuousUpdates: the server pushes frames instead of waiting for each request (one fewer
#: round trip per frame -- the frame rate no longer depends on the network latency)
CONTINUOUS_UPDATES = -313
JPEG_QUALITY = {0: 30, 1: 40, 2: 50, 3: 60, 4: 65, 5: 70, 6: 75, 7: 80, 8: 85, 9: 92}
#: noVNC's quality levels (-32 .. -23 => 0 .. 9)
QUALITY_BASE = -32
FRAME_INTERVAL = 1 / 60
IDLE_POLL = 0.016
#: a screen that has not changed for a while is looked at less often
IDLE_POLL_STILL = 0.05
#: After a change, this many more frames of the same picture: encoders that keep a frame or two
#: (Media Foundation, Quick Sync, AMF) only release the last change when more frames come in --
#: without these the picture stayed on an old frame until the screen changed again.
SETTLE_FRAMES = 4
#: RFB Fence (flow control, :class:`Flow`): pseudo-encoding, and the "request" flag
FENCE = -312
FENCE_REQUEST = 1 << 31
#: updates in flight at most, and the bytes allowed before the first measure / at the least
MAX_INFLIGHT = 4
INITIAL_WINDOW = 512 * 1024
MIN_WINDOW = 96 * 1024
#: seconds of fence answers behind the round-trip minimum and the delivery rate
RTT_WINDOW = 10.0
RATE_WINDOW = 2.0
#: no answer to a fence for this long: the viewer (or the tunnel) is gone
STALL_TIMEOUT = 20.0
#: the bit rate a session starts at and never goes below (bits/s); it climbs to the quality's ceiling
START_BITRATE = 3_000_000
MIN_BITRATE = 400_000
DEFAULT_QUALITY = 6
#: below the best quality the picture is sent at no more than about 1920x1200
MAX_SCALED_PIXELS = 1920 * 1200
#: H.264 bitrate (bits/s) per noVNC quality level 0..9
H264_BITRATE = {0: 1_500_000, 1: 2_000_000, 2: 3_000_000, 3: 4_000_000, 4: 5_000_000, 5: 6_000_000,
                6: 8_000_000, 7: 10_000_000, 8: 14_000_000, 9: 20_000_000}


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

    def grab_raw(self):
        """(BGRA bytes, (w, h)) -- comparing bytes is far cheaper than comparing images."""
        shot = self.mss.grab(self.monitor)
        return shot.bgra, shot.size

    def close(self):
        with contextlib.suppress(Exception):
            self.mss.close()

    def cursor(self, known=None):
        """(key, (hot_x, hot_y), RGBA image) of the pointer's current shape, or None.

        ``key`` changes when the shape does; when it equals ``known`` the image is not read
        (it costs ~10 ms) and (key, None, None) comes back."""
        try:
            if sys.platform == "win32":
                return _windows_cursor(known)
            return _x11_cursor(self)
        except Exception:
            return None


def _windows_cursor(known=None):
    import ctypes
    from ctypes import wintypes
    from PIL import Image

    user32, gdi32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
    # 64-bit handles: every function needs its types declared
    H = wintypes.HANDLE
    user32.GetCursorInfo.argtypes = [ctypes.c_void_p]
    user32.GetIconInfo.argtypes = [H, ctypes.c_void_p]
    user32.GetDC.argtypes, user32.GetDC.restype = [H], H
    user32.ReleaseDC.argtypes = [H, H]
    user32.DrawIconEx.argtypes = [H, ctypes.c_int, ctypes.c_int, H, ctypes.c_int, ctypes.c_int, wintypes.UINT, H, wintypes.UINT]
    gdi32.GetObjectW.argtypes = [H, ctypes.c_int, ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [H]
    gdi32.CreateCompatibleDC.argtypes, gdi32.CreateCompatibleDC.restype = [H], H
    gdi32.CreateDIBSection.argtypes = [H, ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, H, wintypes.DWORD]
    gdi32.CreateDIBSection.restype = H
    gdi32.SelectObject.argtypes, gdi32.SelectObject.restype = [H, H], H
    gdi32.DeleteDC.argtypes = [H]

    class CURSORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                    ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]

    class BITMAP(ctypes.Structure):
        _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG), ("bmHeight", wintypes.LONG),
                    ("bmWidthBytes", wintypes.LONG), ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                    ("bmBits", ctypes.c_void_p)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    info = CURSORINFO(cbSize=ctypes.sizeof(CURSORINFO))
    if not user32.GetCursorInfo(ctypes.byref(info)) or not info.flags & 1 or not info.hCursor:
        return None
    handle = info.hCursor
    key = int(handle)
    if key == known:
        return key, None, None
    icon = ICONINFO()
    if not user32.GetIconInfo(handle, ctypes.byref(icon)):
        return None
    try:
        bitmap = BITMAP()
        gdi32.GetObjectW(icon.hbmColor or icon.hbmMask, ctypes.sizeof(BITMAP), ctypes.byref(bitmap))
        width = bitmap.bmWidth
        height = bitmap.bmHeight if icon.hbmColor else bitmap.bmHeight // 2  # mask-only: AND + XOR halves
        width, height = max(1, min(width, 256)), max(1, min(height, 256))
        hot = (int(icon.xHotspot), int(icon.yHotspot))
    finally:
        for bm in (icon.hbmMask, icon.hbmColor):
            if bm:
                gdi32.DeleteObject(bm)

    # Draw the cursor on black and on white; the difference gives alpha, whatever its format.
    screen_dc = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen_dc)
    header = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=width, biHeight=-height,
                              biPlanes=1, biBitCount=32, biCompression=0)
    bits = ctypes.c_void_p()
    dib = gdi32.CreateDIBSection(dc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(dc, dib)
    size = width * height * 4
    try:
        planes = []
        for fill in (0x00, 0xFF):
            ctypes.memset(bits, fill, size)
            user32.DrawIconEx(dc, 0, 0, handle, width, height, 0, None, 0x0003)  # DI_NORMAL
            planes.append(ctypes.string_at(bits, size))
    finally:
        gdi32.SelectObject(dc, old)
        gdi32.DeleteObject(dib)
        gdi32.DeleteDC(dc)
        user32.ReleaseDC(None, screen_dc)
    on_black, on_white = planes
    rgba = bytearray(size)
    for i in range(0, size, 4):
        b0, g0, r0 = on_black[i], on_black[i + 1], on_black[i + 2]
        alpha = 255 - min(255, max(0, on_white[i + 1] - g0))
        rgba[i:i + 4] = bytes((r0, g0, b0, alpha))
    return key, hot, Image.frombytes("RGBA", (width, height), bytes(rgba))


def _x11_cursor(screen):
    from PIL import Image
    if not hasattr(screen, "_xdisplay"):
        from Xlib import display
        screen._xdisplay = display.Display()
        screen._xdisplay.xfixes_query_version()
    image = screen._xdisplay.xfixes_get_cursor_image(screen._xdisplay.screen().root)
    width, height = image.width, image.height
    data = bytearray()
    for argb in image.cursor_image[: width * height]:
        data += bytes(((argb >> 16) & 255, (argb >> 8) & 255, argb & 255, (argb >> 24) & 255))
    return image.cursor_serial, (image.xhot, image.yhot), Image.frombytes("RGBA", (width, height), bytes(data))


class H264Encoder:
    """PyAV (FFmpeg) H.264, the first encoder that opens here; Annex B access units out.

    ``width``/``height`` are the encoded size: the screen is scaled to it in the same pass that
    converts BGRA to the encoder's pixel format. The encoder that worked is remembered for the
    process, so the next session opens it once, without trying the others first."""

    CANDIDATES = {
        "win32": ("h264_nvenc", "h264_qsv", "h264_amf", "h264_mf", "libx264"),
        "linux": ("h264_nvenc", "h264_vaapi", "h264_qsv", "libx264"),
    }
    OPTIONS = {
        "h264_nvenc": {"preset": "p1", "tune": "ull", "zerolatency": "1", "rc": "cbr", "delay": "0", "forced-idr": "1"},
        "h264_qsv": {"preset": "veryfast", "async_depth": "1", "look_ahead": "0", "forced_idr": "1"},
        "h264_amf": {"usage": "ultralowlatency", "quality": "speed", "rc": "cbr", "forced_idr": "1"},
        "h264_mf": {"scenario": "display_remoting", "rate_control": "cbr", "hw_encoding": "1"},
        "h264_vaapi": {"rc_mode": "CBR"},
        "libx264": {"preset": "ultrafast", "tune": "zerolatency"},
    }
    #: these follow a new ``bit_rate`` on the next frame (FFmpeg reconfigures them); the others reopen
    DYNAMIC_BITRATE = frozenset({"h264_nvenc", "libx264"})
    #: the encoder that worked in this process (tried first, and without a probe)
    working: str | None = None

    def __init__(self, width: int, height: int, bitrate: int):
        import av
        self.av = av
        self.width, self.height = width - width % 2, height - height % 2
        self.bitrate = bitrate
        self.context = None
        self.name = None
        self._source = None  # the reused BGRA frame
        platform = "win32" if sys.platform == "win32" else "linux"
        errors = []
        # ANX_DESKTOP_ENCODER=h264_mf (etc.) pins one encoder: troubleshooting, or a driver to avoid.
        pinned = os.environ.get("ANX_DESKTOP_ENCODER", "").strip()
        names = [pinned] if pinned else list(self.CANDIDATES[platform])
        known = type(self).working
        if known in names:
            names.remove(known)
            names.insert(0, known)
        for name in names:
            try:
                if name != known:
                    self.context = self._open(name)
                    self._probe()
                    # The probe used up the stream's first IDR frame: start the real stream fresh,
                    # so the viewer's decoder gets a key frame first.
                self.context = self._open(name)
                self.name = name
                type(self).working = name
                break
            except Exception as exc:  # no such GPU / driver: try the next one
                errors.append(f"{name}: {type(exc).__name__}")
                self.context = None
                if name == known:
                    type(self).working = None
        if self.context is None:
            raise RuntimeError("no H.264 encoder: " + ", ".join(errors))
        self.started = time.monotonic()
        self.pts = -1

    def _open(self, name: str):
        context = self.av.CodecContext.create(name, "w")
        context.width, context.height = self.width, self.height
        context.pix_fmt = "yuv420p" if name == "libx264" else "nv12"
        context.time_base = __import__("fractions").Fraction(1, 1000)
        context.framerate = 60
        context.bit_rate = self.bitrate
        # no periodic key frames: the stream is reliable, a viewer that needs one asks (a full request)
        context.gop_size = 100000
        context.max_b_frames = 0
        context.options = dict(self.OPTIONS.get(name, {}))
        context.open()
        return context

    def _probe(self) -> None:
        frame = self.av.VideoFrame(self.width, self.height, self.context.pix_fmt)
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        frame.pts = 0
        list(self.context.encode(frame))

    def _frame(self, raw: bytes, size: tuple[int, int]):
        """The screen's BGRA bytes as a frame of the encoder's format and size (scaled if need be)."""
        width, height = size
        source = self._source
        if source is None or (source.width, source.height) != (width, height):
            source = self._source = self.av.VideoFrame(width, height, "bgra")
        plane = source.planes[0]
        if plane.line_size == width * 4 and plane.buffer_size == len(raw):
            plane.update(raw)
        else:  # padded rows: through PIL
            from PIL import Image
            image = Image.frombytes("RGB", size, raw, "raw", "BGRX")
            source = self.av.VideoFrame.from_image(image)
        return source.reformat(width=self.width, height=self.height, format=self.context.pix_fmt,
                               interpolation="BILINEAR" if (width, height) != (self.width, self.height) else None)

    def encode(self, raw: bytes, size: tuple[int, int], keyframe: bool = False) -> bytes:
        frame = self._frame(raw, size)
        # real time stamps: the rate control spends the bits over real time, whatever the frame rate
        self.pts = max(self.pts + 1, int((time.monotonic() - self.started) * 1000))
        frame.pts = self.pts
        if keyframe:
            frame.pict_type = self.av.video.frame.PictureType.I
        return b"".join(bytes(packet) for packet in self.context.encode(frame))

    def set_bitrate(self, bitrate: int) -> bool:
        """Change the bit rate in place; False when this encoder must be reopened for it."""
        if self.name not in self.DYNAMIC_BITRATE:
            return False
        self.bitrate = bitrate
        self.context.bit_rate = bitrate
        return True

    def close(self) -> None:
        context, self.context = self.context, None
        close = getattr(context, "close", None)  # PyAV >= 14 frees the encoder with the object
        if close is not None:
            with contextlib.suppress(Exception):
                close()


def h264_available() -> tuple[bool, str]:
    try:
        import av  # noqa: F401
    except ImportError:
        return False, "PyAV not installed"
    return True, ""


def encoded_size(width: int, height: int, quality: int) -> tuple[int, int]:
    """The size the picture is sent at: the screen's, down to about 1920x1200 below the best quality
    (a 4K screen is four times the bits, and the viewer scales it down to its window anyway)."""
    if quality < 9 and width * height > MAX_SCALED_PIXELS:
        factor = (MAX_SCALED_PIXELS / (width * height)) ** 0.5
        width, height = round(width * factor), round(height * factor)
    return max(2, width - width % 2), max(2, height - height % 2)


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


class _NoH264(Exception):
    pass


class Flow:
    """Updates on their way to the viewer, acknowledged with RFB Fences.

    The tunnel agent takes what is written at loopback speed and queues it; with a link slower than
    the encoder (a relay, a weak uplink) seconds of frames piled up there, and the picture fell
    further and further behind -- it looked frozen. After every update the server sends a fence,
    which the viewer answers once it has processed everything before it. Only a few updates and
    about two bandwidth-delay products of bytes are in flight; the delivery rate measured from the
    answers drives the encoder's bit rate (:meth:`Session._control`)."""

    def __init__(self):
        self.enabled = False
        self.seq = 0
        self.inflight: deque = deque()  # (seq, size, sent at)
        self.bytes = 0
        self.event = asyncio.Event()
        self.rtts: deque = deque()  # (time, rtt)
        self.acked: deque = deque()  # (time, size)
        self.blocked = 0.0  # seconds spent waiting for answers since the last control step
        self.last_ack = time.monotonic()

    def fence(self, size: int) -> bytes:
        self.seq = (self.seq + 1) % 0xFFFFFFFF
        now = time.monotonic()
        if not self.inflight:
            self.last_ack = now  # nothing was outstanding: no answer was late
        self.inflight.append((self.seq, size, now))
        self.bytes += size
        payload = b"%08x" % self.seq  # ASCII: noVNC echoes the payload as a string
        return struct.pack(">BxxxIB", 248, FENCE_REQUEST, len(payload)) + payload

    def ack(self, payload: bytes) -> None:
        try:
            seq = int(payload.decode("ascii"), 16)
        except ValueError:
            return
        if not any(entry[0] == seq for entry in self.inflight):
            return
        now = time.monotonic()
        while self.inflight:
            done, size, sent = self.inflight.popleft()
            self.bytes -= size
            self.acked.append((now, size))
            if done == seq:
                self.rtts.append((now, now - sent))
                break
        self.last_ack = now
        while self.rtts and now - self.rtts[0][0] > RTT_WINDOW:
            self.rtts.popleft()
        while self.acked and now - self.acked[0][0] > RATE_WINDOW:
            self.acked.popleft()
        self.event.set()

    def rtt_min(self) -> float:
        return min((rtt for _, rtt in self.rtts), default=0.1)

    def rtt_recent(self) -> float:
        recent = [rtt for _, rtt in list(self.rtts)[-4:]]
        return sum(recent) / len(recent) if recent else 0.1

    def rate(self) -> float | None:
        """Bytes per second the viewer took in over the last RATE_WINDOW (None: too few answers)."""
        if len(self.acked) < 4:
            return None
        span = self.acked[-1][0] - self.acked[0][0]
        if span < 0.3:
            return None
        return sum(size for _, size in list(self.acked)[1:]) / span

    def budget(self) -> int:
        rate = self.rate()
        if rate is None:
            return INITIAL_WINDOW
        return int(max(MIN_WINDOW, rate * max(self.rtt_min(), 0.03) * 2.5))

    async def wait(self) -> None:
        """Until another update may go out; raises when the viewer stopped answering."""
        if not self.enabled:
            return
        started = None
        while self.inflight and (len(self.inflight) >= MAX_INFLIGHT or self.bytes >= self.budget()):
            now = time.monotonic()
            if now - self.last_ack > STALL_TIMEOUT:
                raise ConnectionError("the viewer stopped answering")
            started = started or now
            self.event.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.event.wait(), 1.0)
        if started is not None:
            self.blocked += time.monotonic() - started


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
        self.quality = DEFAULT_QUALITY
        self.pending: asyncio.Event = asyncio.Event()
        self.full = True
        self.previous = None
        self.encoder: H264Encoder | None = None
        self.encoder_failed = False
        self.warm: asyncio.Future | None = None
        self.last_raw = None
        self.cursor_key = None
        self.announced = set()
        self.continuous = False
        self.settle = 0
        self.encode_failures = 0
        self.flow = Flow()
        #: the framebuffer the viewer sees (the encoded size; the screen is scaled to it)
        self.fb = (0, 0)
        self.relayout = False
        self.target = START_BITRATE
        self.control_at = time.monotonic()
        self.raised_at = 0.0
        self.reopened_at = 0.0
        self.last_sent = 0.0
        self.last_capture = 0.0
        self.changed_at = 0.0
        #: the next picture, captured while the current one is encoded and sent: (started at, future)
        self.prefetch: tuple[float, asyncio.Future] | None = None

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self.screen = await loop.run_in_executor(self.capture, Screen)
        self.input = make_input(self.screen)
        self.fb = encoded_size(self.screen.width, self.screen.height, self.quality)
        if h264_available()[0]:
            # Opening a hardware encoder takes a few hundred ms: meanwhile the handshake runs.
            self.warm = loop.run_in_executor(None, H264Encoder, *self.fb, self._bitrate())
            self.warm.add_done_callback(lambda f: f.cancelled() or f.exception())
        try:
            await self._handshake()
            sender = asyncio.create_task(self._updates())
            sender.add_done_callback(self._sender_done)
            reader = asyncio.create_task(self._messages())
            try:
                # Either side ending ends the session: a stopped sender must not leave the
                # viewer on a frozen picture of a session that still looks connected.
                done, _ = await asyncio.wait({sender, reader}, return_when=asyncio.FIRST_COMPLETED)
                if reader in done:
                    reader.result()  # the viewer left (IncompleteReadError etc.)
            finally:
                for task in (sender, reader):
                    task.cancel()
                for task in (sender, reader):
                    with contextlib.suppress(BaseException):
                        await task
        finally:
            if self.warm is not None:
                self.warm.add_done_callback(lambda f: f.cancelled() or f.exception() or f.result().close())
            if self.encoder is not None:
                self.encoder.close()
            with contextlib.suppress(Exception):
                await loop.run_in_executor(self.capture, self.screen.close)
            self.capture.shutdown(wait=False)

    @staticmethod
    def _sender_done(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            exc = task.exception()
            if not isinstance(exc, (ConnectionError, asyncio.IncompleteReadError)):
                log.error("remote desktop: frame sender stopped", exc_info=exc)

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
        w.write(struct.pack(">HH", *self.fb) + self.format.pack() + struct.pack(">I", len(name)) + name)
        await w.drain()

    def _bitrate(self) -> int:
        """The bit rate to encode at: the quality's ceiling, held down to what the link carries
        when the viewer answers fences (without them there is nothing to measure)."""
        cap = H264_BITRATE.get(self.quality, 8_000_000)
        return int(min(self.target, cap)) if self.flow.enabled else cap

    def _screen_point(self, x: int, y: int) -> tuple[int, int]:
        """Viewer (framebuffer) coordinates -> screen pixels."""
        fw, fh = self.fb
        if (fw, fh) == (self.screen.width, self.screen.height) or not fw or not fh:
            return x, y
        return (min(self.screen.width - 1, round(x * self.screen.width / fw)),
                min(self.screen.height - 1, round(y * self.screen.height / fh)))

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
                if levels and levels[0] - QUALITY_BASE != self.quality:
                    self.quality = levels[0] - QUALITY_BASE
                    self.target = min(max(self.target, START_BITRATE), H264_BITRATE.get(self.quality, 8_000_000))
                    self.relayout = True
                    self.pending.set()
                self.flow.enabled = FENCE in values
                if CONTINUOUS_UPDATES in values and "continuous" not in self.announced:
                    self.announced.add("continuous")
                    self.writer.write(b"\x96")  # EndOfContinuousUpdates: "this server can push"
                    await self.writer.drain()
            elif kind == 3:  # FramebufferUpdateRequest
                incremental, *_ = struct.unpack(">BHHHH", await r.readexactly(9))
                if not incremental:
                    self.full = True  # also how a viewer whose decoder failed asks for a key frame
                self.pending.set()
            elif kind == 4:  # KeyEvent
                down, _, keysym = struct.unpack(">BHI", await r.readexactly(7))
                if self.input is not None:
                    await asyncio.to_thread(self.input.key, bool(down), keysym)
            elif kind == 5:  # PointerEvent
                mask, x, y = struct.unpack(">BHH", await r.readexactly(5))
                if self.input is not None:
                    await asyncio.to_thread(self.input.pointer, mask, *self._screen_point(x, y))
            elif kind == 150:  # EnableContinuousUpdates
                enable, *_ = struct.unpack(">BHHHH", await r.readexactly(9))
                self.continuous = bool(enable)
                self.full = self.full or self.continuous
                self.pending.set()
            elif kind == 248:  # ClientFence: the answer to one of ours
                head = await r.readexactly(8)
                length = head[7]
                self.flow.ack(await r.readexactly(length))
            elif kind == 6:  # ClientCutText
                head = await r.readexactly(7)
                length = struct.unpack(">I", head[3:7])[0]
                if length > 1 << 20:
                    raise ValueError("clipboard too large")
                await r.readexactly(length)
            else:
                raise ValueError(f"unknown RFB message {kind}")

    def _want_h264(self) -> bool:
        return H264 in self.encodings and not self.encoder_failed and h264_available()[0]

    def _interval(self) -> float:
        """Fewer, better frames on a thin link."""
        if not self._want_h264():
            return FRAME_INTERVAL
        bitrate = self.encoder.bitrate if self.encoder is not None else self._bitrate()
        return FRAME_INTERVAL if bitrate >= 3_000_000 else 1 / 30 if bitrate >= 1_200_000 else 1 / 15

    async def _send(self, rects: list[bytes]) -> None:
        """One FramebufferUpdate (and the fence that tracks it)."""
        body = struct.pack(">BxH", 0, len(rects)) + b"".join(rects)
        if self.flow.enabled:
            body += self.flow.fence(len(body))
        self.writer.write(body)
        await self.writer.drain()
        self.last_sent = time.monotonic()

    async def _pace(self) -> None:
        """Before a capture: a request is due, the viewer has room, and the frame interval passed."""
        await self.pending.wait()
        await self.flow.wait()
        self._control()
        # The interval counts from the last capture: a capture (~25 ms with GDI) and the encode are part
        # of it, not added to it.
        wait = self._interval() - (time.monotonic() - self.last_capture)
        if wait > 0:
            await asyncio.sleep(wait)
        if self.relayout or (self.screen.width, self.screen.height) != self._screen_size:
            await self._layout()

    @property
    def _screen_size(self) -> tuple[int, int]:
        return getattr(self, "_known_screen", (self.screen.width, self.screen.height))

    async def _layout(self) -> None:
        """The quality (above or below "best") or the screen changed: maybe a new framebuffer size."""
        self.relayout = False
        self._known_screen = (self.screen.width, self.screen.height)
        size = encoded_size(self.screen.width, self.screen.height, self.quality)
        if size == self.fb or DESKTOP_SIZE not in self.encodings:
            return  # the same size, or a viewer that cannot resize: scaled into the old one
        self.fb = size
        self.cursor_key = None  # the pointer's shape is scaled with the picture
        self.full = True
        self.previous = None
        if self.encoder is not None:
            self.encoder.close()
            self.encoder = None
        await self._send([struct.pack(">HHHHi", 0, 0, *size, DESKTOP_SIZE)])

    def _control(self) -> None:
        """Once a second: follow the link. Congested (waiting for answers, round trips growing):
        down to what the viewer took in; clear: up again, step by step, to the quality's ceiling."""
        now = time.monotonic()
        span = now - self.control_at
        if span < 1.0 or not self.flow.enabled:
            return
        blocked, self.flow.blocked = self.flow.blocked / span, 0.0
        self.control_at = now
        cap = H264_BITRATE.get(self.quality, 8_000_000)
        rate = self.flow.rate()
        queued = self.flow.rtt_recent() - self.flow.rtt_min()
        target = self.target
        if blocked > 0.3 and rate is not None:
            target = min(target, rate * 8 * 0.85)
        elif blocked < 0.05 and queued < max(0.04, self.flow.rtt_min() * 0.5) and now - self.raised_at > 2.0:
            target *= 1.5
            self.raised_at = now
        self.target = max(MIN_BITRATE, min(cap, target))
        encoder = self.encoder
        if encoder is None or abs(self._bitrate() - encoder.bitrate) < encoder.bitrate * 0.15:
            return
        if encoder.set_bitrate(self._bitrate()):
            return
        change = max(self._bitrate(), encoder.bitrate) / max(1, min(self._bitrate(), encoder.bitrate))
        if change >= 1.5 and now - self.reopened_at > 5.0:
            encoder.close()
            self.encoder = None  # reopened at the new rate (with a key frame) on the next frame
            self.reopened_at = now

    async def _updates(self) -> None:
        while True:
            # Decide only once the viewer asked: its SetEncodings comes before its first request.
            await self.pending.wait()
            if self._want_h264():
                try:
                    await self._h264_update()
                    continue
                except _NoH264:
                    pass
            await self._jpeg_update()

    async def _grab(self, raw: bool):
        """The screen (BGRA bytes and size, or an image at the framebuffer size), retried until it works."""
        loop = asyncio.get_running_loop()
        while True:
            try:
                if raw:
                    prefetch, self.prefetch = self.prefetch, None
                    if prefetch is not None:
                        started, future = prefetch
                        picture = await future
                        if time.monotonic() - started <= max(0.05, 2 * self._interval()):
                            self.last_capture = started
                            return picture  # fresh enough: captured while the last frame was encoded
                    self.last_capture = time.monotonic()
                    return await loop.run_in_executor(self.capture, self.screen.grab_raw)
                image = await loop.run_in_executor(self.capture, self.screen.grab)
                if image.size != self.fb:
                    from PIL import Image
                    image = await asyncio.to_thread(image.resize, self.fb, Image.BILINEAR)
                return image
            except Exception as exc:  # lock screen / UAC (secure desktop), display change
                await self._recapture(exc)

    def _prefetch(self) -> None:
        """Start capturing the next picture now (the capture thread is idle while this one is encoded)."""
        if self.prefetch is None:
            future = asyncio.get_running_loop().run_in_executor(self.capture, self.screen.grab_raw)
            future.add_done_callback(lambda f: f.cancelled() or f.exception())
            self.prefetch = (time.monotonic(), future)

    def _idle_poll(self) -> float:
        """Look again soon after a change; less often on a still screen (a capture costs ~20 ms of CPU)."""
        return IDLE_POLL if time.monotonic() - self.changed_at < 2.0 else IDLE_POLL_STILL

    async def _jpeg_update(self) -> None:
        from PIL import ImageChops

        await self._pace()
        while True:
            image = await self._grab(raw=False)
            if self.full or self.previous is None or self.previous.size != image.size:
                box = (0, 0, image.width, image.height)
            else:
                box = await asyncio.to_thread(lambda: ImageChops.difference(image, self.previous).getbbox())
            if box:
                break
            await asyncio.sleep(self._idle_poll())
        self.changed_at = time.monotonic()
        self.pending.clear()
        self.full = False
        self.previous = image
        payload = await asyncio.to_thread(self._rect, image, box)
        cursor = await self._cursor_rect()
        if "jpeg" not in self.announced:
            self.announced.add("jpeg")
            log.info("remote desktop: %s (the viewer offered no H.264)", "Tight/JPEG" if TIGHT in self.encodings else "Raw")
        if self.continuous:
            self.pending.set()
        await self._send([payload] + ([cursor] if cursor else []))

    async def _encoder(self) -> H264Encoder:
        """The encoder for the current framebuffer: the one opened during the handshake, or a new one."""
        if self.encoder is not None:
            return self.encoder
        loop = asyncio.get_running_loop()
        encoder = None
        if self.warm is not None:
            warm, self.warm = self.warm, None
            with contextlib.suppress(Exception):
                encoder = await warm
            if encoder is not None and ((encoder.width, encoder.height) != self.fb):
                encoder.close()
                encoder = None
        try:
            if encoder is None:
                encoder = await loop.run_in_executor(None, H264Encoder, *self.fb, self._bitrate())
            elif encoder.bitrate != self._bitrate():
                encoder.set_bitrate(self._bitrate())
        except Exception as exc:
            log.warning("remote desktop: H.264 unavailable (%s); using JPEG", exc)
            self.encoder_failed = True
            raise _NoH264() from None
        if "h264" not in self.announced:
            self.announced.add("h264")
            log.info("remote desktop: H.264 with %s, %dx%d of %dx%d", encoder.name, *self.fb,
                     self.screen.width, self.screen.height)
        self.encoder = encoder
        self.full = True
        return encoder

    async def _h264_update(self) -> None:
        """One H.264 access unit for the whole screen, once something changed."""
        loop = asyncio.get_running_loop()
        await self._pace()
        encoder = await self._encoder()
        while True:
            raw, size = await self._grab(raw=True)
            if size != (self.screen.width, self.screen.height):
                self.screen.width, self.screen.height = size
                return  # the next _pace() lays the new size out
            cursor = await self._cursor_rect()
            if self.full or raw != self.last_raw or self.settle > 0:
                break
            if cursor:  # only the pointer's shape changed
                if not self.continuous:
                    self.pending.clear()
                await self._send([cursor])
                return
            await asyncio.sleep(self._idle_poll())
        changed = raw != self.last_raw
        keyframe = self.full
        if changed or self.full:
            self.changed_at = time.monotonic()
        self.settle = SETTLE_FRAMES if changed or self.full else self.settle - 1
        self.pending.clear()
        self.full = False
        self.last_raw = raw
        if changed:
            self._prefetch()  # the screen is moving: the next picture is likely needed soon

        try:
            data = await loop.run_in_executor(None, encoder.encode, raw, size, keyframe)
            self.encode_failures = 0
        except Exception as exc:
            self.encode_failures += 1
            log.warning("remote desktop: %s failed (%s)", encoder.name, exc)
            encoder.close()
            self.encoder = None  # a fresh encoder (and key frame) next time
            if self.encode_failures >= 3:
                self.encoder_failed = True
                raise _NoH264() from None
            self.pending.set()
            return
        if not data:  # the encoder kept this frame: the settle frames bring it out
            self.pending.set()
            return
        flags = 1 if keyframe else 0  # bit 0: reset the decoder context
        header = struct.pack(">HHHHi", 0, 0, encoder.width, encoder.height, H264)
        rects = [header + struct.pack(">II", len(data), flags) + data]
        if cursor:
            rects.append(cursor)
        if self.continuous:
            self.pending.set()
        await self._send(rects)

    async def _recapture(self, exc: Exception) -> None:
        """Capture failed: wait, then start a new capture (the desktop may have switched)."""
        if "capture" not in self.announced:
            self.announced.add("capture")
            log.warning("remote desktop: screen capture failed (%s); retrying", exc)
        await asyncio.sleep(0.5)
        self.prefetch = None
        loop = asyncio.get_running_loop()
        with contextlib.suppress(Exception):
            await loop.run_in_executor(self.capture, self.screen.close)
        with contextlib.suppress(Exception):
            self.screen = await loop.run_in_executor(self.capture, Screen)
        self.full = True

    async def _cursor_rect(self) -> bytes | None:
        """A Cursor pseudo-rectangle when the pointer's shape changed (and the viewer wants them)."""
        if CURSOR not in self.encodings:
            return None
        shape = await asyncio.get_running_loop().run_in_executor(self.capture, self.screen.cursor, self.cursor_key)
        if shape is None or shape[0] == self.cursor_key:
            return None
        self.cursor_key, (hot_x, hot_y), image = shape
        scale = self.fb[0] / self.screen.width if self.screen.width else 1.0
        if scale < 0.95:  # the picture is scaled down: so is the pointer
            from PIL import Image
            size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
            image = image.resize(size, Image.LANCZOS)
            hot_x, hot_y = min(size[0] - 1, round(hot_x * scale)), min(size[1] - 1, round(hot_y * scale))
        width, height = image.size
        pixels = self.format.raw(image.convert("RGB"))
        alpha = image.getchannel("A").point(lambda a: 255 if a >= 128 else 0).convert("1")
        row = (width + 7) // 8
        mask = bytearray(row * height)
        data = alpha.load()
        for y in range(height):
            for x in range(width):
                if data[x, y]:
                    mask[y * row + x // 8] |= 0x80 >> (x % 8)
        return struct.pack(">HHHHi", hot_x, hot_y, width, height, CURSOR) + pixels + bytes(mask)

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
