"""Element references, settling, and "what changed" -- the observe half of act.

The point of this module is to make one tool call a whole observe-act cycle.
Without it a model spends two calls and a screenshot on every step: click, then
look. With it, the click comes back carrying the new state.

Three pieces:

* :class:`RefTable` hands out the ``ref_N`` names the model points at, and
  keeps the recipe needed to find each element again. Refs belong to a control
  session and die with it, because a ref that outlives the screen it described
  is a way to click the wrong thing.
* :func:`settle` waits for the screen to stop moving, so the state reported
  after an action is the state the action produced rather than a menu caught
  mid-animation.
* :func:`diff` says what appeared and disappeared, which is far cheaper than
  re-sending a whole element list or a screenshot.
"""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .uia import Element

#: Polling interval and ceiling for settling. The ceiling matters more than the
#: interval: a menu opens in 50ms, a dialog in 300ms, and an application that
#: takes longer than this is better handled by the model asking again.
SETTLE_POLL_S = 0.08
SETTLE_TIMEOUT_S = 1.5
#: The screen is compared at this size. Small enough to be free, large enough
#: that an opening menu or a changed button shows up.
THUMBNAIL = (48, 27)


@dataclass
class Observation:
    """What the screen looked like at one moment."""

    scope: str
    foreground: str
    elements: list[tuple[str, Element]] = field(default_factory=list)
    truncated: bool = False
    note: str = ""

    def to_dict(self, include_elements: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "scope": self.scope,
            "foreground_window": self.foreground,
            "count": len(self.elements),
        }
        if self.truncated:
            out["truncated"] = True
        if self.note:
            out["note"] = self.note
        if include_elements:
            out["elements"] = [element.to_dict(ref) for ref, element in self.elements]
        return out

    def signatures(self) -> dict[tuple, str]:
        return {
            (element.role, element.name, element.rect): ref
            for ref, element in self.elements
        }


class RefTable:
    """``ref_N`` -> the element it named, for as long as the session lives."""

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self._elements: dict[str, Element] = {}
        self._lock = threading.Lock()

    def put(self, elements: list[Element]) -> list[tuple[str, Element]]:
        """Name a batch of elements, reusing a ref for an element already known.

        Reuse matters: a model that read the screen, clicked, and read again
        should see the unchanged button keep its ref instead of watching every
        number shift by one.
        """
        with self._lock:
            known = {self._key(element): ref for ref, element in self._elements.items()}
            out: list[tuple[str, Element]] = []
            for element in elements:
                ref = known.get(self._key(element))
                if ref is None:
                    ref = f"ref_{next(self._counter)}"
                self._elements[ref] = element
                out.append((ref, element))
            return out

    def get(self, ref: str) -> Element:
        with self._lock:
            element = self._elements.get(ref.strip())
        if element is None:
            raise KeyError(
                f"unknown element reference {ref!r}: call read_ui first, and use a ref from "
                "its result (refs are dropped when control stops)"
            )
        return element

    def clear(self) -> None:
        with self._lock:
            self._elements.clear()
            self._counter = itertools.count(1)

    @staticmethod
    def _key(element: Element) -> tuple:
        return (element.window_hwnd, element.role, element.name, element.rect)

    def __len__(self) -> int:
        return len(self._elements)


def _thumbnail(rect: Optional[tuple[int, int, int, int]]) -> bytes:
    """A tiny render of the screen (or one window), for change detection."""
    try:
        from PIL import ImageGrab

        image = ImageGrab.grab(bbox=rect, all_screens=True)
        return image.convert("L").resize(THUMBNAIL).tobytes()
    except Exception:  # noqa: BLE001 - settling is best effort, never fatal
        return b""


def settle(rect: Optional[tuple[int, int, int, int]] = None,
           timeout: float = SETTLE_TIMEOUT_S) -> dict[str, Any]:
    """Wait until the screen stops changing, and say what happened.

    Two consecutive identical frames is the signal. This is what UFO² leaves to
    the model -- it ships with no settle step at all and lets the model judge
    from a screenshot -- but a fixed sleep is either too slow or too short, and
    the model should not be spending a turn on it.
    """
    from .inputs import foreground_window

    started = time.time()
    previous = _thumbnail(rect)
    title = foreground_window()
    while time.time() - started < timeout:
        time.sleep(SETTLE_POLL_S)
        current = _thumbnail(rect)
        now_title = foreground_window()
        if current == previous and now_title == title:
            return {"settled": True, "waited_s": round(time.time() - started, 2)}
        previous, title = current, now_title
    return {"settled": False, "waited_s": round(time.time() - started, 2),
            "note": "the screen was still changing when the wait ran out"}


def diff(before: Optional[Observation], after: Observation) -> dict[str, Any]:
    """What changed between two observations, in as few words as possible."""
    out: dict[str, Any] = {"foreground_window": after.foreground}
    if before is None:
        out["elements"] = len(after.elements)
        return out
    if before.foreground != after.foreground:
        out["foreground_changed_from"] = before.foreground
    was, now = before.signatures(), after.signatures()
    appeared = [ref for key, ref in now.items() if key not in was]
    vanished = [ref for key, ref in was.items() if key not in now]
    if appeared:
        out["appeared"] = [
            element.to_dict(ref) for ref, element in after.elements if ref in set(appeared)
        ]
    if vanished:
        out["gone"] = vanished
    if not appeared and not vanished and "foreground_changed_from" not in out:
        out["unchanged"] = True
    return out
