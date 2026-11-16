"""The on-screen indicator: a blue glow around every monitor, plus a top label.

This is the part of the feature the *user* experiences, so it is deliberate
about a few things:

* **Per-pixel alpha.** A layered window updated through ``UpdateLayeredWindow``
  can fade from a solid edge to nothing, which a colour-keyed window cannot.
* **Click-through.** ``WS_EX_TRANSPARENT`` means the glow never eats a click,
  so the machine stays usable while it is shown.
* **Invisible to capture.** ``WDA_EXCLUDEFROMCAPTURE`` keeps the indicator out
  of the screenshots the model reads, so the model sees the desktop rather than
  its own badge -- and cannot be confused into treating it as part of the UI.
* **Its own thread with a real message pump.** Windows requires the thread that
  owns a window to pump messages; the pump also drives the slow pulse.
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

from .screens import Screen, list_screens
from .win32 import (
    AC_SRC_ALPHA,
    AC_SRC_OVER,
    BI_RGB,
    BITMAPINFO,
    BLENDFUNCTION,
    DIB_RGB_COLORS,
    HOOKPROC,
    HWND_TOPMOST,
    KBDLLHOOKSTRUCT,
    LLKHF_INJECTED,
    VK_ESCAPE,
    WH_KEYBOARD_LL,
    WM_KEYDOWN,
    WM_SYSKEYDOWN,
    SW_SHOWNOACTIVATE,
    SWP_NOACTIVATE,
    SWP_NOMOVE,
    SWP_NOSIZE,
    ULW_ALPHA,
    WDA_EXCLUDEFROMCAPTURE,
    WM_APP_QUIT,
    WM_DISPLAYCHANGE,
    WNDCLASSEXW,
    WNDPROC,
    WS_EX_LAYERED,
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_EX_TOPMOST,
    WS_EX_TRANSPARENT,
    WS_POPUP,
    gdi32,
    kernel32,
    set_window_display_affinity,
    user32,
)

CLASS_NAME = "AgentNexusControlIndicator"
GLOW_COLOR = (59, 130, 246)
GLOW_THICKNESS = 96  # logical pixels, scaled per monitor
GLOW_ALPHA = 165
#: A bright hairline right at the edge. Without it the falloff alone reads as a
#: smudge on the display rather than as a deliberate frame.
EDGE_LINE_ALPHA = 225
EDGE_LINE_WIDTH = 3
EDGE_LINE_COLOR = (147, 197, 253)
BANNER_BG = (17, 24, 39)
BANNER_TEXT = (255, 255, 255)
BANNER_DOT = (96, 165, 250)
BANNER_HINT = (156, 163, 175)
BANNER_DIVIDER = (75, 85, 99)
#: The label promises this, so the hook that implements it is not optional.
ESCAPE_HINT = "按 ESC 退出"
PULSE_PERIOD_S = 3.2
PULSE_FLOOR = 0.72
PULSE_FPS = 8
FONTS = (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhl.ttc",
         r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\segoeui.ttf")


def _font(size: int):
    from PIL import ImageFont

    for path in FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _glow_mask(width: int, height: int, thickness: int, peak: int):
    """An alpha ramp that is strongest at the edge and gone ``thickness`` in."""
    from PIL import Image, ImageDraw

    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    thickness = max(1, min(thickness, width // 2, height // 2))
    for step in range(thickness):
        # Quadratic falloff reads as a glow; linear reads as a border.
        alpha = round(peak * ((thickness - step) / thickness) ** 2.0)
        if alpha <= 0:
            continue
        draw.rectangle([step, step, width - 1 - step, height - 1 - step], outline=alpha, width=1)
    return mask


def _edge_mask(width: int, height: int, thickness: int):
    from PIL import Image, ImageDraw

    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    for step in range(max(1, thickness)):
        draw.rectangle([step, step, width - 1 - step, height - 1 - step],
                       outline=EDGE_LINE_ALPHA, width=1)
    return mask


def _colored(mask, color):
    """Tint an alpha mask, keeping the work in PIL's C paths."""
    from PIL import Image

    channels = [mask.point(lambda a, c=component: a * c // 255) for component in color]
    return Image.merge("RGBA", (*channels, mask))


def _banner(text: str, scale: float, hint: str = ESCAPE_HINT):
    """The label pill: a dot, what is happening, and how to stop it."""
    from PIL import Image, ImageDraw

    font = _font(max(12, round(19 * scale)))
    hint_font = _font(max(11, round(16 * scale)))
    pad_x, pad_y, dot = round(20 * scale), round(11 * scale), round(9 * scale)
    gap = round(10 * scale)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    left, top, right, bottom = probe.textbbox((0, 0), text, font=font)
    text_w, text_h = right - left, bottom - top

    hint_w = hint_h = hint_left = hint_top = 0
    divider = round(14 * scale) if hint else 0
    if hint:
        hint_left, hint_top, hint_right, hint_bottom = probe.textbbox((0, 0), hint, font=hint_font)
        hint_w, hint_h = hint_right - hint_left, hint_bottom - hint_top

    width = pad_x * 2 + dot + gap + text_w + (divider * 2 + hint_w if hint else 0)
    height = pad_y * 2 + max(text_h, dot)
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([0, 0, width - 1, height - 1], radius=height // 2, fill=(*BANNER_BG, 236))
    cy = height // 2
    draw.ellipse([pad_x, cy - dot // 2, pad_x + dot, cy + dot // 2], fill=(*BANNER_DOT, 255))
    cursor_x = pad_x + dot + gap
    draw.text((cursor_x - left, cy - text_h // 2 - top), text, font=font, fill=(*BANNER_TEXT, 255))
    if hint:
        cursor_x += text_w + divider
        draw.line([cursor_x, cy - text_h // 2, cursor_x, cy + text_h // 2],
                  fill=(*BANNER_DIVIDER, 255), width=max(1, round(scale)))
        cursor_x += divider
        draw.text((cursor_x - hint_left, cy - hint_h // 2 - hint_top),
                  hint, font=hint_font, fill=(*BANNER_HINT, 255))
    return image


def render(screen: Screen, text: str, hint: str = ESCAPE_HINT):
    """The full overlay image for one monitor, premultiplied and BGRA-ordered.

    ``UpdateLayeredWindow`` wants premultiplied alpha; ``ImageChops.multiply``
    does exactly that channel-wise, which keeps a 4K frame in C rather than in
    a Python loop.
    """
    from PIL import Image, ImageChops

    width, height = screen.width, screen.height
    scale = max(1.0, screen.scale)
    image = _colored(_glow_mask(width, height, round(GLOW_THICKNESS * scale), GLOW_ALPHA), GLOW_COLOR)
    image = Image.alpha_composite(
        image, _colored(_edge_mask(width, height, round(EDGE_LINE_WIDTH * scale)), EDGE_LINE_COLOR)
    )

    if text:
        banner = _banner(text, scale, hint)
        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        layer.paste(banner, ((width - banner.width) // 2, round(18 * scale)))
        image = Image.alpha_composite(image, layer)

    red, green, blue, alpha = image.split()
    premultiplied = Image.merge("RGBA", (
        ImageChops.multiply(blue, alpha),
        ImageChops.multiply(green, alpha),
        ImageChops.multiply(red, alpha),
        alpha,
    ))
    return premultiplied.tobytes()


@dataclass
class _Window:
    hwnd: int
    memory_dc: int
    bitmap: int
    old_bitmap: int
    width: int
    height: int
    x: int
    y: int


def _window_proc(hwnd, message, wparam, lparam):
    """The indicator draws itself; nothing here needs to react to a message."""
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


#: Registered once per process, and its window procedure has to outlive every
#: window created from it. Keeping the callback on the Overlay instance crashed
#: the process: the registration survives the object it came from, so the first
#: window created after an earlier Overlay had been collected called into freed
#: memory -- an access violation inside CreateWindowExW, on the overlay thread,
#: with nothing in the traceback to say why.
_WINDOW_PROC = WNDPROC(_window_proc)
_CLASS_LOCK = threading.Lock()
_CLASS_REGISTERED = False


def _register_indicator_class() -> None:
    global _CLASS_REGISTERED

    with _CLASS_LOCK:
        if _CLASS_REGISTERED:
            return
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.lpfnWndProc = _WINDOW_PROC
        cls.hInstance = kernel32.GetModuleHandleW(None)
        cls.lpszClassName = CLASS_NAME
        user32.RegisterClassExW(ctypes.byref(cls))  # 1410 = already there, which is fine
        _CLASS_REGISTERED = True


class _EscapeHook:
    """A physical ESC ends control, on a thread that does nothing else.

    Low-level keyboard hooks are called on the thread that installed them, and
    only while that thread is pumping messages -- so Windows holds every
    keystroke in the system until it gets there. Sharing the indicator's thread
    made that measurable: typing 40 characters took 0.32s with no session,
    0.34s with the indicator off and 1.89s with it on, because each key waited
    behind a frame of the glow. This thread blocks in GetMessage and draws
    nothing, so the hook answers at once and typing costs what it should.
    """

    def __init__(self, on_escape: Callable[[], None]):
        self.on_escape = on_escape
        self._hook = None
        self._proc = None
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._error = ""

    @property
    def armed(self) -> bool:
        return bool(self._hook)

    def start(self, timeout: float = 5.0) -> None:
        self._thread = threading.Thread(target=self._run, name="control-escape-hook",
                                        daemon=True)
        self._thread.start()
        # The banner promises ESC works, so it has to be true before the caller
        # is told control is on.
        if not self._ready.wait(timeout):
            self._error = "the escape hook did not come up in time"

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_APP_QUIT, 0, 0)
        self._thread.join(timeout)
        self._thread = None
        self._ready.clear()

    def _run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()

        def proc(code, wparam, lparam):
            if code == 0 and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                event = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                # Only keys a person pressed: the model must be able to send ESC
                # to an application without closing the session it works in.
                if event.vkCode == VK_ESCAPE and not event.flags & LLKHF_INJECTED:
                    # Stopping joins this thread, so it cannot run on it.
                    threading.Thread(target=self.on_escape, name="control-escape",
                                     daemon=True).start()
                    return 1  # swallow it, so the application below sees nothing
            return user32.CallNextHookEx(None, code, wparam, lparam)

        self._proc = HOOKPROC(proc)  # kept alive for as long as the hook is
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        if not self._hook:
            self._error = f"SetWindowsHookExW failed: {ctypes.get_last_error()}"
        self._ready.set()
        try:
            message = wintypes.MSG()
            # Blocking, not polling: this thread's only job is to be available.
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_APP_QUIT:
                    break
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
            self._proc = None


class Overlay:
    """Shows the indicator on every monitor until :meth:`stop`."""

    def __init__(self, text: str, pulse: bool = True, hint: str = ESCAPE_HINT,
                 on_escape: Callable[[], None] | None = None):
        self.text = text
        self.hint = hint
        self.pulse = pulse
        self.on_escape = on_escape
        self._escape = _EscapeHook(on_escape) if on_escape is not None else None
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._windows: list[_Window] = []
        self._error: str = ""
        self._excluded_from_capture = False
        self._started_at = 0.0

    # -- lifecycle ---------------------------------------------------------------------

    def start(self, timeout: float = 10.0) -> None:
        if self._thread is not None:
            return
        self._started_at = time.time()
        self._thread = threading.Thread(target=self._run, name="control-overlay", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("the control indicator did not come up in time")
        if self._error:
            raise RuntimeError(self._error)

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_APP_QUIT, 0, 0)
        self._thread.join(timeout)
        self._thread = None
        self._ready.clear()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def hidden_from_capture(self) -> bool:
        return self._excluded_from_capture

    @property
    def escape_armed(self) -> bool:
        """Whether a physical ESC really does end control."""
        return self._escape is not None and self._escape.armed

    # -- the overlay thread ------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._thread_id = kernel32.GetCurrentThreadId()
            _register_indicator_class()
            self._build_windows()
            # Installed before start() returns: the label promises ESC works, so
            # it must be true by the time the caller is told control is on.
            if self._escape is not None:
                self._escape.start()
        except Exception as exc:  # noqa: BLE001 - reported to start(), not swallowed
            self._error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
            return
        self._ready.set()
        try:
            self._pump()
        finally:
            if self._escape is not None:
                self._escape.stop()
            self._destroy_windows()

    def _build_windows(self) -> None:
        for screen in list_screens():
            hwnd = user32.CreateWindowExW(
                WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                CLASS_NAME, "Agent Nexus control indicator", WS_POPUP,
                screen.x, screen.y, screen.width, screen.height,
                None, None, kernel32.GetModuleHandleW(None), None,
            )
            if not hwnd:
                raise OSError(f"CreateWindowExW failed: {ctypes.get_last_error()}")
            # Keep the badge out of screenshots before the window is ever shown.
            if set_window_display_affinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
                self._excluded_from_capture = True
            window = self._prepare_bitmap(hwnd, screen)
            self._windows.append(window)
            self._blit(window, 1.0)
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _prepare_bitmap(self, hwnd: int, screen: Screen) -> _Window:
        payload = render(screen, self.text, self.hint)
        screen_dc = user32.GetDC(None)
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(info.bmiHeader)
        info.bmiHeader.biWidth = screen.width
        info.bmiHeader.biHeight = -screen.height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(
            screen_dc, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
        )
        user32.ReleaseDC(None, screen_dc)
        if not bitmap:
            gdi32.DeleteDC(memory_dc)
            raise OSError("CreateDIBSection failed for the indicator")
        ctypes.memmove(bits, payload, len(payload))
        old = gdi32.SelectObject(memory_dc, bitmap)
        return _Window(hwnd, memory_dc, bitmap, old, screen.width, screen.height, screen.x, screen.y)

    def _blit(self, window: _Window, strength: float) -> None:
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, max(0, min(255, round(255 * strength))), AC_SRC_ALPHA)
        position = wintypes.POINT(window.x, window.y)
        size = wintypes.SIZE(window.width, window.height)
        source = wintypes.POINT(0, 0)
        screen_dc = user32.GetDC(None)
        try:
            user32.UpdateLayeredWindow(
                window.hwnd, screen_dc, ctypes.byref(position), ctypes.byref(size),
                window.memory_dc, ctypes.byref(source), 0, ctypes.byref(blend), ULW_ALPHA,
            )
        finally:
            user32.ReleaseDC(None, screen_dc)

    def _pump(self) -> None:
        """Message loop; also drives the pulse without redrawing the bitmap.

        Only the layer's constant alpha changes per frame, so a pulse costs a
        composite, not a 4K image rebuild.
        """
        import math

        message = wintypes.MSG()
        interval = 1.0 / PULSE_FPS
        next_frame = time.time()
        while True:
            while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):  # PM_REMOVE
                if message.message == WM_APP_QUIT:
                    return
                if message.message == WM_DISPLAYCHANGE:
                    self._destroy_windows()
                    self._build_windows()
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            now = time.time()
            if self.pulse and now >= next_frame:
                phase = (now - self._started_at) % PULSE_PERIOD_S / PULSE_PERIOD_S
                strength = PULSE_FLOOR + (1 - PULSE_FLOOR) * (0.5 - 0.5 * math.cos(2 * math.pi * phase))
                for window in self._windows:
                    self._blit(window, strength)
                next_frame = now + interval
            time.sleep(0.02)

    def _destroy_windows(self) -> None:
        for window in self._windows:
            gdi32.SelectObject(window.memory_dc, window.old_bitmap)
            gdi32.DeleteObject(window.bitmap)
            gdi32.DeleteDC(window.memory_dc)
            user32.DestroyWindow(window.hwnd)
        self._windows = []

