"""Monitor geometry, in the one coordinate space every control tool uses.

All coordinates in this package are **virtual-desktop pixels**: the origin is
wherever Windows puts the top-left of the primary monitor, so a monitor placed
to the left of it has negative x.  Screenshots report the origin of what they
captured for exactly this reason -- image coordinates are never screen
coordinates.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from ctypes import wintypes

from .win32 import MONITORENUMPROC, MONITORINFOEXW, user32, virtual_screen

MONITORINFOF_PRIMARY = 0x00000001


@dataclass(frozen=True)
class Screen:
    index: int
    device: str
    x: int
    y: int
    width: int
    height: int
    work_x: int
    work_y: int
    work_width: int
    work_height: int
    primary: bool
    scale: float
    handle: int

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        """``(left, top, right, bottom)`` in virtual-desktop pixels."""
        return (self.x, self.y, self.x + self.width, self.y + self.height)

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "device": self.device,
            "primary": self.primary,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "scale": self.scale,
            "work_area": {
                "x": self.work_x, "y": self.work_y,
                "width": self.work_width, "height": self.work_height,
            },
        }


def _dpi_scale(handle: int) -> float:
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
        shcore.GetDpiForMonitor.argtypes = [
            wintypes.HMONITOR, ctypes.c_int, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)
        ]
        if shcore.GetDpiForMonitor(handle, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0:
            return round(dpi_x.value / 96.0, 2)
    except (AttributeError, OSError):
        pass
    return 1.0


def list_screens() -> list[Screen]:
    """Every monitor, primary first, then left to right."""
    found: list[Screen] = []

    def callback(handle, _hdc, _rect, _param):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            return True
        monitor, work = info.rcMonitor, info.rcWork
        found.append(Screen(
            index=0,
            device=info.szDevice,
            x=monitor.left, y=monitor.top,
            width=monitor.right - monitor.left, height=monitor.bottom - monitor.top,
            work_x=work.left, work_y=work.top,
            work_width=work.right - work.left, work_height=work.bottom - work.top,
            primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
            scale=_dpi_scale(handle),
            handle=int(handle),
        ))
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(callback), 0)
    found.sort(key=lambda s: (not s.primary, s.x, s.y))
    return [
        Screen(**{**screen.__dict__, "index": index})
        for index, screen in enumerate(found)
    ]


def desktop_bounds() -> dict:
    left, top, width, height = virtual_screen()
    return {"x": left, "y": top, "width": width, "height": height}


def screen_at(x: int, y: int) -> Screen | None:
    for screen in list_screens():
        if screen.contains(x, y):
            return screen
    return None
