"""Screenshots, with the arithmetic the model needs to click on what it sees.

A downscaled screenshot is a different coordinate space from the desktop, and a
model that forgets this clicks in the wrong place.  Every capture therefore
reports the region it came from and the scale it was reduced by, and the tool
description spells out the conversion.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from .screens import Screen, list_screens
from .win32 import virtual_screen

MAX_EDGE = 1568
#: Past this, a PNG screenshot costs more than the detail it preserves.
PNG_BUDGET = 1_200_000


@dataclass
class Capture:
    data: bytes
    mime: str
    #: Where the capture came from, in virtual-desktop pixels.
    x: int
    y: int
    width: int
    height: int
    image_width: int
    image_height: int

    @property
    def scale(self) -> float:
        return round(self.image_width / self.width, 4) if self.width else 1.0

    def to_dict(self) -> dict:
        return {
            "region": {"x": self.x, "y": self.y, "width": self.width, "height": self.height},
            "image": {"width": self.image_width, "height": self.image_height, "mime": self.mime,
                      "bytes": len(self.data)},
            "scale": self.scale,
            "to_screen_coordinates": (
                f"screen_x = {self.x} + image_x / {self.scale}, "
                f"screen_y = {self.y} + image_y / {self.scale}"
            ),
        }


def _encode(image) -> tuple[bytes, str]:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    if buffer.tell() <= PNG_BUDGET:
        return buffer.getvalue(), "image/png"
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=82, optimize=True)
    return buffer.getvalue(), "image/jpeg"


def resolve_region(screen: int | None = None,
                   region: dict | None = None) -> tuple[int, int, int, int]:
    """Pick what to capture: an explicit region, one monitor, or everything."""
    if region:
        try:
            x, y = int(region["x"]), int(region["y"])
            width, height = int(region["width"]), int(region["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("region needs integer x, y, width and height") from exc
        if width <= 0 or height <= 0:
            raise ValueError("region width and height must be positive")
        return x, y, width, height
    if screen is not None:
        screens = list_screens()
        match = next((s for s in screens if s.index == screen), None)
        if match is None:
            raise ValueError(f"no screen with index {screen}; there are {len(screens)}")
        return match.x, match.y, match.width, match.height
    return virtual_screen()


def capture(screen: int | None = None, region: dict | None = None,
            max_edge: int = MAX_EDGE) -> Capture:
    from PIL import ImageGrab

    x, y, width, height = resolve_region(screen, region)
    image = ImageGrab.grab(bbox=(x, y, x + width, y + height), all_screens=True)
    if max_edge and max(image.size) > max_edge:
        from PIL import Image

        ratio = max_edge / max(image.size)
        image = image.resize(
            (max(1, round(image.width * ratio)), max(1, round(image.height * ratio))),
            Image.Resampling.LANCZOS,
        )
    data, mime = _encode(image)
    return Capture(data, mime, x, y, width, height, image.width, image.height)


#: Ask the window to render itself, including content the compositor owns.
PW_RENDERFULLCONTENT = 2
#: Only an almost entirely black frame counts as a failed render.
#:
#: A finer threshold was tried and removed. Edge came back 43.7% black and that
#: looked like ``PrintWindow`` losing GPU-composited content -- until the same
#: window was grabbed from the screen while nothing covered it and measured
#: 43.7% black as well. It is a dark theme. Without a reference image, "half
#: the window is black" and "the window is dark" are the same measurement, so
#: the heuristic only ever produced false alarms.
BLACK_LIMIT = 0.95
#: Above this much of a window hidden behind others, a screen grab of its
#: rectangle is somebody else's content.
OVERLAP_LIMIT = 0.05


def black_fraction(image) -> float:
    """How much of the image is pure black, sampled rather than counted."""
    small = image.convert("L").resize((60, 40))
    data = list(small.getdata())
    return sum(1 for value in data if value < 12) / len(data)


def occlusion(hwnd: int) -> float:
    """How much of this window is covered by windows above it in z-order."""
    from .uia import windows

    order = windows()
    target = next((index for index, window in enumerate(order) if window.hwnd == hwnd), None)
    if target is None:
        return 0.0
    left, top, right, bottom = order[target].rect
    area = max(1, (right - left) * (bottom - top))
    covered = 0
    for above in order[:target]:
        al, at, ar, ab = above.rect
        covered += (max(0, min(right, ar) - max(left, al))
                    * max(0, min(bottom, ab) - max(top, at)))
    return min(1.0, covered / area)


def bring_forward(hwnd: int) -> bool:
    """Raise a window and give it focus. Returns whether Windows allowed it."""
    import time

    from .win32 import SW_RESTORE, user32

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    time.sleep(0.35)
    return int(user32.GetForegroundWindow() or 0) == hwnd


def window_pixels(hwnd: int, allow_focus: bool = True) -> tuple[Any, str, str]:
    """The most trustworthy picture of one window, and how it was obtained.

    Three cases, in order of preference:

    * the window renders itself fine -- use that, it works even when covered;
    * it renders mostly black (GPU-composited content, as Edge does) but nothing
      is in front of it -- then the screen really is this window;
    * it renders partially *and* something covers it -- neither source is
      complete, so the window is brought forward and the screen is read.

    That last step changes which window has focus. It is done anyway, on the
    operator's instruction: while a control session is running the machine has
    already been handed over, and answering with half a window's content is
    worse than raising it. It is reported in the note, and it is why ``read_ui``
    does not claim to be read-only.
    """
    from PIL import ImageGrab

    from .win32 import user32

    import ctypes
    from ctypes import wintypes

    def grab() -> Any:
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom),
                              all_screens=True)

    covered = occlusion(hwnd)
    try:
        image, fell_back = window_image(hwnd)
    except OSError as exc:
        if covered <= OVERLAP_LIMIT:
            return grab(), "screen", ""
        if allow_focus and bring_forward(hwnd):
            return grab(), "screen", "brought this window forward to read it"
        return None, "none", f"cannot capture this window ({exc})"

    if not fell_back and black_fraction(image) <= BLACK_LIMIT:
        return image, "window", ""
    if covered <= OVERLAP_LIMIT:
        return grab(), "screen", ""
    if allow_focus and bring_forward(hwnd):
        return grab(), "screen", "brought this window forward to read it reliably"
    return image, "window", (
        f"this window renders itself only partially and {covered:.0%} of it is behind other "
        "windows, so some content may be missing; Windows also refused to bring it forward"
    )


def window_image(hwnd: int):
    """A picture of one window's own content, even when something covers it.

    Grabbing the screen where a window happens to sit is wrong whenever another
    window is in front: the pixels belong to the thing on top. That is not a
    cosmetic problem -- text recognised from those pixels gets reported as this
    window's elements, which is a confident lie. ``PrintWindow`` asks the window
    to draw itself instead.

    Returns ``(image, occluded_fallback)``; the flag is True when the window
    refused to render and the screen had to be grabbed after all.
    """
    import ctypes
    from ctypes import wintypes

    from PIL import Image

    from .win32 import BI_RGB, BITMAPINFO, DIB_RGB_COLORS, gdi32, user32

    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError(f"window {hwnd} has no rectangle")
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise OSError(f"window {hwnd} has no area")

    screen_dc = user32.GetDC(None)
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(info.bmiHeader)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = BI_RGB
    bits = ctypes.c_void_p()
    bitmap = gdi32.CreateDIBSection(screen_dc, ctypes.byref(info), DIB_RGB_COLORS,
                                    ctypes.byref(bits), None, 0)
    try:
        if not bitmap:
            raise OSError("CreateDIBSection failed")
        old = gdi32.SelectObject(memory_dc, bitmap)
        user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        user32.PrintWindow.restype = wintypes.BOOL
        ok = user32.PrintWindow(hwnd, memory_dc, PW_RENDERFULLCONTENT)
        buffer = ctypes.string_at(bits, width * height * 4)
        gdi32.SelectObject(memory_dc, old)
        image = Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1)
        # A window that declined to draw leaves the bitmap blank.
        if not ok or not image.convert("L").getbbox():
            from PIL import ImageGrab

            grabbed = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom),
                                     all_screens=True)
            return grabbed.convert("RGB"), True
        return image.convert("RGB"), False
    finally:
        if bitmap:
            gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(None, screen_dc)


def describe_screens() -> list[dict]:
    return [screen.to_dict() for screen in list_screens()]


def primary() -> Screen | None:
    return next((s for s in list_screens() if s.primary), None)
