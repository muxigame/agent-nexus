"""A distinct mouse pointer for the duration of a control session.

The system cursor is replaced machine-wide, so it is the one signal that
survives the model dragging focus between windows: whatever the user is looking
at, the pointer says something else is driving it.  It is restored from the
user's own cursor scheme on stop.

Because the change outlives this process if it is killed, ``mark`` records that
the cursors were swapped and :func:`restore_if_left_over` puts them back the
next time the server starts.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path

from .win32 import (
    ICONINFO,
    OCR_APPSTARTING,
    OCR_HAND,
    OCR_IBEAM,
    OCR_NORMAL,
    SPI_SETCURSORS,
    SPIF_SENDCHANGE,
    BITMAPINFO,
    BI_RGB,
    DIB_RGB_COLORS,
    gdi32,
    user32,
)

#: The pointer is deliberately larger than the system default (32px).
CURSOR_SIZE = 48
SUPERSAMPLE = 4
FILL = (37, 99, 235)  # a strong blue, readable on light and dark backgrounds
EDGE = (255, 255, 255)
#: Arrow outline, in a 12x19 space, scaled to fit the canvas.
ARROW = [(0, 0), (0, 16.2), (4.0, 12.6), (6.6, 18.6), (9.2, 17.5), (6.7, 11.6), (11.4, 11.6)]
#: Where the arrow tip sits once the supersampled drawing is scaled back down.
#: The hotspot only shifts the drawing: clicks land where Windows puts the
#: pointer, so an error here is cosmetic.
HOTSPOT = (3, 2)


def _render(size: int = CURSOR_SIZE):
    """Draw the pointer: soft shadow, white keyline, blue body."""
    from PIL import Image, ImageDraw, ImageFilter

    big = size * SUPERSAMPLE
    scale = (big * 0.80) / 19.0  # leave room for the keyline and shadow
    points = [(x * scale + 3 * SUPERSAMPLE, y * scale + 2 * SUPERSAMPLE) for x, y in ARROW]

    shadow = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    offset = [(x + 2.5 * SUPERSAMPLE, y + 2.5 * SUPERSAMPLE) for x, y in points]
    ImageDraw.Draw(shadow).polygon(offset, fill=(0, 0, 0, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(2.2 * SUPERSAMPLE))

    body = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(body)
    draw.polygon(points, fill=(*FILL, 255), outline=(*EDGE, 255), width=round(1.6 * SUPERSAMPLE))

    image = Image.alpha_composite(shadow, body)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def _color_bitmap(image) -> int:
    """A 32-bit top-down DIB holding the image, as BGRA with straight alpha."""
    width, height = image.size
    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(info.bmiHeader)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height  # negative: rows run top-down
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = BI_RGB

    bits = ctypes.c_void_p()
    bitmap = gdi32.CreateDIBSection(None, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
    if not bitmap:
        raise OSError("CreateDIBSection failed for the cursor bitmap")
    red, green, blue, alpha = image.split()
    from PIL import Image as _Image

    payload = _Image.merge("RGBA", (blue, green, red, alpha)).tobytes()
    ctypes.memmove(bits, payload, len(payload))
    return bitmap


def build_cursor(size: int = CURSOR_SIZE) -> int:
    """A cursor handle for the drawn pointer, hotspot on the arrow tip."""
    image = _render(size)
    color = _color_bitmap(image)
    # A 32-bit colour bitmap carries its own alpha; the mask only has to exist.
    mask = gdi32.CreateBitmap(size, size, 1, 1, None)
    try:
        info = ICONINFO(
            fIcon=False, xHotspot=HOTSPOT[0], yHotspot=HOTSPOT[1], hbmMask=mask, hbmColor=color,
        )
        handle = user32.CreateIconIndirect(ctypes.byref(info))
        if not handle:
            raise OSError("CreateIconIndirect failed for the cursor")
        return handle
    finally:
        gdi32.DeleteObject(color)
        gdi32.DeleteObject(mask)


#: Which system cursors get replaced.  The arrow is the one that matters; the
#: others keep the pointer consistent when it crosses text and links.
REPLACED = (OCR_NORMAL, OCR_IBEAM, OCR_HAND, OCR_APPSTARTING)


class SystemCursor:
    """Swaps the system cursors in, and puts the user's scheme back."""

    def __init__(self, marker: Path | None = None):
        self.marker = marker
        self.active = False

    def apply(self) -> bool:
        handle = build_cursor()
        try:
            if self.marker is not None:
                self.marker.parent.mkdir(parents=True, exist_ok=True)
                self.marker.write_text("cursors replaced by a control session\n", encoding="utf-8")
            for cursor_id in REPLACED:
                # SetSystemCursor destroys the handle it is given, so each id
                # needs its own copy.
                copy = user32.CopyIcon(handle)
                if copy:
                    user32.SetSystemCursor(copy, cursor_id)
            self.active = True
            return True
        finally:
            user32.DestroyIcon(handle)

    def restore(self) -> bool:
        ok = restore_system_cursors()
        if self.marker is not None:
            self.marker.unlink(missing_ok=True)
        self.active = False
        return ok


def restore_system_cursors() -> bool:
    """Reload the user's own cursor scheme from their settings."""
    return bool(user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, SPIF_SENDCHANGE))


def restore_if_left_over(marker: Path) -> bool:
    """Undo a swap that outlived the process that made it."""
    if not marker.exists():
        return False
    restore_system_cursors()
    marker.unlink(missing_ok=True)
    return True


def current_position() -> tuple[int, int]:
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y
