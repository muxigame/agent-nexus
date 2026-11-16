"""The gate: nothing here touches the screen until control has been started.

One machine has one screen and one pointer, so there is one session. It owns
both halves of the promise made to the person at the keyboard:

* while it is open the indicator is up and the pointer is changed, and
* when it closes -- on ``stop_control``, when the user presses ESC, or when the
  process exits -- the screen and the pointer go back exactly as they were.

The ESC key is the person's override: the label on screen promises it, and it
beats whatever the model is in the middle of doing.

The teardown runs from ``finally`` blocks and an ``atexit`` hook, and a marker
file covers the case where the process is killed outright.
"""
from __future__ import annotations

import atexit
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .vision import IconFinder

from . import cursor as cursor_mod
from .observe import Observation, RefTable, diff, settle
from .overlay import Overlay
from .uia import READ_TIMEOUT_S, Element, UiaError, UiaReader, UiaTimeout, windows
from .win32 import enable_dpi_awareness

#: How long to keep looking at a window whose tree is still being built.
READY_RETRIES = 4
READY_DELAY_S = 0.4
#: A tree this small is suspicious enough to look again before trusting it.
CONFIRM_BELOW = 15
CONFIRM_DELAY_S = 0.3

#: How much of a window's height belongs to its frame -- title bar, tabs,
#: toolbar -- rather than to what it is showing. Edge at 1048px tall puts the
#: bottom of its chrome at about 160px, which is where this comes from.
CONTENT_TOP_FRACTION = 0.15
#: Fewer elements than this below that line means the application has a window
#: but has not put anything in it yet. Measured on this machine, opening a
#: browser window on two pages: while loading, Edge reports exactly one element
#: down there (the new-tab button, which sits just under the line), and once
#: loaded it reports 86 on python.org and 149 on zh.wikipedia.org.
CONTENT_MINIMUM = 5
#: Long enough for the slower of those two: Wikipedia held the browser's own 40
#: elements for 2.1 seconds and answered with 200 at 3.4. A fixed delay short
#: enough to be cheap reports the browser's toolbar as if it were the page --
#: which is worse than being slow, because the model then acts on it.
CONTENT_BUDGET_S = 4.0
#: The waiting is capped by that budget but usually ends long before it, because
#: what is worth waiting for is a window that is still drawing itself. Measured
#: both ways on this machine: a browser loading a page changed its picture at
#: every one of five looks 0.3s apart, and a finished self-drawn window changed
#: at none of them. So a window that has stopped painting and still has nothing
#: in its tree is not going to grow one, and waiting on it is pure cost.
PAINT_THUMBNAIL = (64, 36)

#: How long to take an application at its word that it cannot describe itself
#: quickly. One Unity editor here answers a read in 23 seconds, every time,
#: which is not a wait anyone can use -- so after it has failed once, its
#: pixels are read directly. Long enough not to pay the timeout repeatedly,
#: short enough that an application which was merely busy gets another chance.
SLOW_TREE_MEMORY_S = 300.0
SLOW_TREE_NOTE = (
    "this window's application did not answer UI Automation within "
    f"{READ_TIMEOUT_S:g}s, so its text and icons are read from the pixels instead. "
    "Elements read that way can only be clicked."
)

#: How wide to look when the caller asked for one named element. The cap is
#: there to keep a reply small, but applying it before the search means the
#: search cannot see past it: asking python.org for "Downloads" with a limit of
#: 50 found nothing, because its link is the ninety-somethingth element. The
#: cap belongs on the matches, not on the reading.
QUERY_SCAN_LIMIT = 500

#: Window furniture, matched as substrings. It is present before the
#: application's content is, so "there is something actionable here" is not a
#: readiness signal on its own -- and Windows names these buttons after the
#: application ("最小化 设置", "关闭 设置"), which an exact-match list misses.
CAPTION_KEYWORDS = (
    "最小化", "最大化", "关闭", "还原", "系统菜单", "标题栏",
    "Minimize", "Maximize", "Close", "Restore", "System menu", "Title bar",
)

#: The menu behind the window icon, with Move, Size and Close in it. UI
#: Automation reports it as a menu item of the window, and on a self-drawn
#: window it can be the *only* thing in the tree that looks actionable -- a Tk
#: window reports exactly the window, this, and the three caption buttons. That
#: passed for content, so the pixels were never read and its icons were
#: invisible. Matched exactly and only on a menu, because an application's own
#: "系统设置" is not the window frame.
SYSTEM_MENU_NAMES = frozenset({"系统", "System"})

#: What to say when a window offers nothing, not even pixels worth reading.
#:
#: Measured on this machine rather than assumed: Edge (221), Explorer (210),
#: Claude (195), QQ (111), Settings (79) and the calculator (69) all expose
#: plenty. Only WeCom (1), VS Code (4) and the Unity editor (17) are genuinely
#: tree-poor. An earlier version of this message blamed Electron in general and
#: was wrong -- QQ and the ChatGPT app had simply been minimised.
NO_TREE_HINT = (
    "this window exposes almost no UI Automation elements, which happens with custom-drawn "
    "applications (WeCom, the Unity editor, games) and with editors like VS Code. Its text and "
    "icons are read from the pixels instead where possible; otherwise use screenshot and click "
    "by coordinate."
)


class ControlError(RuntimeError):
    """Raised when a control tool is used outside an open session."""


@dataclass
class ControlOptions:
    banner_text: str = "ChatGPT 正在控制电脑"
    pulse: bool = True
    show_indicator: bool = True
    change_cursor: bool = True
    #: Read text off the pixels when a window has no accessibility tree.
    use_ocr: bool = True
    #: Where OmniParser's weights live, and how to run it. The model is never
    #: loaded until the vision tool is told to; until then icon-only controls
    #: are simply absent and OCR carries the window.
    vision_model_dir: str = "runtime/models/omniparser"
    vision_device: str = "auto"
    vision_caption: bool = True
    #: What a relative ``vision_model_dir`` is relative to.
    project_root: str = "."


@dataclass
class _State:
    session_id: str
    started_at: float
    reason: str
    last_activity: float
    actions: int = 0
    notes: list[str] = field(default_factory=list)


class ControlSession:
    """Start/stop plus the guard every control tool calls first."""

    def __init__(self, options: ControlOptions, marker: Path,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None):
        self.options = options
        self.marker = marker
        self.on_event = on_event or (lambda name, payload: None)
        self._lock = threading.RLock()
        self._state: _State | None = None
        self._overlay: Overlay | None = None
        self._cursor = cursor_mod.SystemCursor(marker)
        self._dpi = "unknown"
        self.refs = RefTable()
        self._ui: Optional[UiaReader] = None
        self._last: Optional[Observation] = None
        self._icons: Optional["IconFinder"] = None
        #: The best element count seen per window, used to notice a tree that
        #: is being rebuilt rather than one that is simply small.
        self._counts: dict[int, int] = {}
        #: Windows that were given time to produce a tree and did not. They get
        #: the waiting once, not once per read: a self-drawn window is never
        #: going to fill up, and charging it the full wait every time made
        #: reading one cost six seconds instead of one.
        self._treeless: set[int] = set()
        #: Windows whose provider is too slow to be worth asking again soon.
        self._slow_tree: dict[int, float] = {}
        atexit.register(self._emergency_stop)
        if sys.platform == "win32":
            # A previous process may have been killed with the cursors swapped;
            # the marker is how that survives a crash, so clear it at startup.
            cursor_mod.restore_if_left_over(marker)

    # -- state -------------------------------------------------------------------------

    @property
    def active(self) -> bool:
        with self._lock:
            return self._state is not None

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._state is None:
                return {"active": False, "indicator": "not shown", "cursor": "unchanged"}
            now = time.time()
            return {
                "active": True,
                "session_id": self._state.session_id,
                "reason": self._state.reason,
                "started_at": _iso(self._state.started_at),
                "seconds_active": round(now - self._state.started_at, 1),
                "idle_seconds": round(now - self._state.last_activity, 1),
                "actions": self._state.actions,
                "indicator": self._indicator_state(),
                "cursor": "replaced" if self._cursor.active else "unchanged",
                "ends_when": "stop_control is called, or the user presses ESC",
                "notes": list(self._state.notes),
            }

    def _indicator_state(self) -> str:
        if self._overlay is None or not self._overlay.alive:
            return "not shown"
        return "shown (hidden from screenshots)" if self._overlay.hidden_from_capture else "shown"

    # -- lifecycle ---------------------------------------------------------------------

    def start(self, reason: str = "") -> dict[str, Any]:
        if sys.platform != "win32":
            raise ControlError("screen control is implemented for Windows only")
        with self._lock:
            if self._state is not None:
                status = self.status()
                status["already_active"] = True
                return status

            self._dpi = enable_dpi_awareness()
            # A previous process may have died with the cursors swapped.
            cursor_mod.restore_if_left_over(self.marker)

            state = _State(
                session_id=secrets.token_hex(6),
                started_at=time.time(),
                reason=reason.strip(),
                last_activity=time.time(),
            )
            try:
                if self.options.show_indicator:
                    self._overlay = Overlay(self.options.banner_text, pulse=self.options.pulse,
                                            on_escape=self._stop_from_escape)
                    self._overlay.start()
                    if not self._overlay.hidden_from_capture:
                        state.notes.append(
                            "this Windows build cannot hide the indicator from screen capture, "
                            "so the glow and label will appear in screenshots"
                        )
                    if not self._overlay.escape_armed:
                        # The label says ESC works. If the hook did not install,
                        # say so rather than letting the screen make a promise.
                        state.notes.append(
                            "the ESC key could not be hooked, so the label's "
                            "'press ESC to exit' will not work; use stop_control"
                        )
                if self.options.change_cursor:
                    self._cursor.apply()
            except Exception:
                self._teardown()
                raise

            self._state = state
            self.on_event("control_started", {
                "session_id": state.session_id, "reason": state.reason, "dpi_awareness": self._dpi,
            })
            status = self.status()
            status["dpi_awareness"] = self._dpi
            return status

    def stop(self, reason: str = "requested") -> dict[str, Any]:
        with self._lock:
            if self._state is None:
                return {"active": False, "was_active": False,
                        "indicator": "not shown", "cursor": "unchanged"}
            state = self._state
            self._state = None
            self._teardown()
            summary = {
                "active": False,
                "was_active": True,
                "session_id": state.session_id,
                "stopped_because": reason,
                "seconds_active": round(time.time() - state.started_at, 1),
                "actions": state.actions,
                "indicator": "removed",
                "cursor": "restored",
            }
            self.on_event("control_stopped", dict(summary))
            return summary

    def _stop_from_escape(self) -> None:
        """The user pressed ESC. Their key beats whatever the model was doing."""
        self.stop("the user pressed ESC")

    def _teardown(self) -> None:
        # Refs describe a screen that no longer has an indicator on it; keeping
        # them would let a later session click something it never looked at.
        self.refs.clear()
        self._last = None
        if self._overlay is not None:
            try:
                self._overlay.stop()
            finally:
                self._overlay = None
        if self._cursor.active:
            self._cursor.restore()

    def _emergency_stop(self) -> None:
        """Last line of defence: never leave the machine visibly taken over."""
        try:
            if self._state is not None:
                self.stop("process exit")
            else:
                cursor_mod.restore_if_left_over(self.marker)
        except Exception:  # noqa: BLE001 - interpreter shutdown must not raise
            pass

    # -- observation -------------------------------------------------------------------

    @property
    def ui(self) -> UiaReader:
        """Built on first use: it starts a COM thread, which is not free."""
        if self._ui is None:
            self._ui = UiaReader()
        return self._ui

    @property
    def icons(self) -> "IconFinder":
        """The icon model's handle. Constructing it loads nothing."""
        if self._icons is None:
            from .vision import IconFinder

            directory = Path(self.options.vision_model_dir)
            if not directory.is_absolute():
                directory = Path(self.options.project_root) / directory
            self._icons = IconFinder(directory, self.options.vision_device,
                                     self.options.vision_caption)
        return self._icons

    def element(self, ref: str) -> Element:
        """The element a ref names, or a KeyError explaining how to get one."""
        return self.refs.get(ref)

    def _scope_hwnd(self, scope: str) -> int:
        if scope in ("", "focused"):
            return self.ui.foreground()[0]
        if scope.startswith("window:"):
            try:
                return int(scope.split(":", 1)[1], 0)
            except ValueError as exc:
                raise ValueError(f"{scope!r} needs a numeric window handle") from exc
        if scope.startswith("ref:"):
            element = self.refs.get(scope.split(":", 1)[1])
            return element.hwnd or element.window_hwnd
        raise ValueError(
            f"unknown scope {scope!r}: use 'focused', 'windows', 'window:<hwnd>' or 'ref:<ref>'"
        )

    def observe(self, scope: str = "focused", filter_mode: str = "interactive",
                limit: int = 200, query: str = "", wait_for_content: bool = True) -> Observation:
        """Read elements and give them refs the model can point at.

        ``query`` filters by name before the result is built. A file manager or
        a browser easily reaches the element cap, and a model that only wants
        "the button called 保存" should not have to read two hundred rows to
        find it.
        """
        scope = (scope or "focused").strip()
        foreground = self.ui.foreground()[1]
        if scope in ("windows", "desktop"):
            found = self.refs.put(_matching(windows(), query))
            observation = Observation("windows", foreground, found)
        else:
            hwnd = self._scope_hwnd(scope)
            scan = max(limit, QUERY_SCAN_LIMIT) if query else limit
            elements, truncated, note = self._read_when_ready(hwnd, filter_mode, scan,
                                                              wait_for_content)
            # The fallback happens here, not in the model's next turn. A window
            # with no usable tree still comes back as elements with refs -- read
            # off the pixels and marked as such -- so a GUI step stays one call
            # instead of read, screenshot, measure, click.
            if not _has_content(elements):
                elements, note = self._add_pixel_elements(hwnd, elements, note)
            if query:
                matched = _matching(elements, query)
                if not matched and elements:
                    note = (f"nothing matched {query!r}; the window has {len(elements)} "
                            "element(s), read it without a query to see them") + (f" ({note})" if note else "")
                elements, truncated = matched[:limit], len(matched) > limit
            observation = Observation(scope or "focused", foreground,
                                      self.refs.put(elements), truncated, note)
        self._last = observation
        return observation

    def _read_when_ready(self, hwnd: int, filter_mode: str, limit: int,
                         wait_for_content: bool = True) -> tuple[list[Element], bool, str]:
        """Read a window's elements, allowing for one that is still starting up.

        A freshly launched application has a window before it has a tree: the
        calculator answered with a single element half a second after launch and
        sixty-nine a moment later. Without this, a model reads "one element",
        concludes the window is inaccessible, and drops to pixel-clicking an
        application it could have driven by name.

        Waiting unconditionally would tax the windows that genuinely have no
        tree, so the signal is growth: a loading tree gets bigger, an absent one
        does not.
        """
        # One budget for all the waiting in here, not one per loop: the retry
        # below and the wait for content are asking the same question, and
        # letting them add up charged a window with no tree 5.6 seconds.
        deadline = time.time() + CONTENT_BUDGET_S
        asked = self._slow_tree.get(hwnd, 0.0)
        if time.time() - asked < SLOW_TREE_MEMORY_S:
            return [], False, SLOW_TREE_NOTE
        try:
            elements, truncated = self.ui.elements(hwnd, filter_mode, limit)
        except UiaTimeout:
            self._slow_tree[hwnd] = time.time()
            return [], False, SLOW_TREE_NOTE
        except UiaError as exc:
            return [], False, str(exc)
        self._slow_tree.pop(hwnd, None)
        if not _has_content(elements):
            # A packaged app's window is only a frame; its content lives in a
            # CoreWindow child owned by a different process, and asking the
            # frame never reaches it. This is not a timing problem, so it is
            # worth checking before waiting on anything.
            elements, truncated = self._include_hosted_content(hwnd, elements, truncated,
                                                               filter_mode, limit)
        frame = None
        for _attempt in range(READY_RETRIES if hwnd not in self._treeless else 0):
            if _has_content(elements) or time.time() >= deadline:
                break
            # Not "stop when the tree stops growing" -- that was tried and it
            # broke Explorer, which answers with the same five frame elements
            # twice before its eighty-five arrive. The signal is the window's
            # picture instead: Explorer is busy drawing its file list during
            # those two reads, while a window that has finished painting and
            # still has no tree is not about to grow one.
            previous, frame = frame, _paint_frame(hwnd)
            if _attempt and frame is not None and frame == previous:
                break
            time.sleep(READY_DELAY_S)
            try:
                again, truncated_again = self.ui.elements(hwnd, filter_mode, limit)
            except UiaError:
                break
            if not _has_content(again):
                again, truncated_again = self._include_hosted_content(
                    hwnd, again, truncated_again, filter_mode, limit)
            if len(again) > len(elements) or _has_content(again):
                elements, truncated = again, truncated_again
        # "Has content" and "has finished loading" are different things, and no
        # classification of elements can tell them apart: Explorer's first read
        # offers five real elements and its file list arrives afterwards. Two
        # signals earn a confirming look -- a suspiciously thin tree, and a tree
        # that has shrunk since this window was last read, which is what a view
        # change looks like while it rebuilds. QQ dropped from 108 elements to
        # 25 after switching panes, with the screen already settled.
        best = self._counts.get(hwnd, 0)
        first_look = hwnd not in self._counts
        suspicious = first_look or len(elements) < CONFIRM_BELOW or len(elements) * 2 < best
        if (wait_for_content and suspicious and hwnd not in self._treeless
                and not _has_body_content(hwnd, elements)):
            # The window is there and its frame is populated, but nothing is in
            # the part that matters. Counting does not catch this: a browser
            # sits at exactly its own 40 elements for one to two seconds, so a
            # tree that has "stopped growing" has not necessarily started.
            frame = _paint_frame(hwnd)
            while time.time() < deadline:
                time.sleep(READY_DELAY_S)
                # Ask the cheap question first. Looking at the window costs
                # about 0.05s; asking a slow application to describe itself
                # again costs 1.4s, and there is no point paying that for a
                # window that has not redrawn a pixel since the last look.
                previous, frame = frame, _paint_frame(hwnd)
                if frame is not None and frame == previous:
                    break  # nothing is being drawn; a tree is not on its way
                try:
                    again, truncated_again = self.ui.elements(hwnd, filter_mode, limit)
                except UiaError:
                    break
                if len(again) > len(elements):
                    elements, truncated = again, truncated_again
                if _has_body_content(hwnd, elements):
                    break
        elif len(elements) < CONFIRM_BELOW or len(elements) * 2 < best:
            time.sleep(CONFIRM_DELAY_S)
            try:
                again, truncated_again = self.ui.elements(hwnd, filter_mode, limit)
                if len(again) > len(elements):
                    elements, truncated = again, truncated_again
            except UiaError:
                pass
        self._counts[hwnd] = max(best, len(elements))
        # Whether it earned another grace period next time. A window that fills
        # up later -- a browser that finally loaded -- clears the mark, so a
        # navigation after that is waited for again.
        if _has_content(elements) and _has_body_content(hwnd, elements):
            self._treeless.discard(hwnd)
        else:
            self._treeless.add(hwnd)
        return elements, truncated, ""

    def _include_hosted_content(self, hwnd: int, elements: list[Element], truncated: bool,
                                filter_mode: str, limit: int) -> tuple[list[Element], bool]:
        """Merge in the elements of a hosted content window, if there is one."""
        seen = {(element.role, element.name, element.rect) for element in elements}
        merged = list(elements)
        for child in _hosted_children(hwnd):
            try:
                extra, extra_truncated = self.ui.elements(child, filter_mode, limit)
            except UiaError:
                continue
            for element in extra:
                key = (element.role, element.name, element.rect)
                if key not in seen:
                    seen.add(key)
                    merged.append(element)
            truncated = truncated or extra_truncated
            if _has_content(merged):
                break
        return merged, truncated

    def _add_pixel_elements(self, hwnd: int, elements: list[Element],
                             note: str) -> tuple[list[Element], str]:
        """Top up an empty accessibility tree with the text on screen."""
        from .win32 import user32

        if hwnd and user32.IsIconic(hwnd):
            # A minimised window sits at (-32000, -32000) and has no pixels to
            # read. This is why QQ and the ChatGPT app looked tree-less at first:
            # they were minimised, not inaccessible.
            return elements, (
                "this window is minimised, so it has neither a readable tree nor pixels; "
                "restore it first with element(action='restore', ref=<its ref from "
                "read_ui(scope='windows')>)"
            )
        wants_icons = self._icons is not None and self._icons.state.loaded
        if not self.options.use_ocr and not wants_icons:
            return elements, note or NO_TREE_HINT
        region = _window_region(hwnd)
        if region is None:
            return elements, note or NO_TREE_HINT
        try:
            from . import capture, ocr

            # One capture feeds both readers: grabbing the same window twice
            # doubles the cost and can catch it mid-repaint.
            image, _source, warning = capture.window_pixels(hwnd)
            if image is None:
                return elements, f"{note} {warning}".strip() or NO_TREE_HINT
            origin = (region["x"], region["y"])
            text = ocr.read_image(image, origin, window_hwnd=hwnd) if self.options.use_ocr else []
            icons: list[Element] = []
            if wants_icons:
                try:
                    icons = self.icons.detect(image, origin, window_hwnd=hwnd)
                except Exception as exc:  # noqa: BLE001 - the model must not break a read
                    warning = f"{warning} icon detection failed ({type(exc).__name__}: {exc})".strip()
            # An icon with words printed under it is already named by the
            # interface; that beats anything a caption model produces.
            icons = ocr.label_icons(icons, text) if icons and text else icons
            # The accessibility tree wins, then text, then icons: an icon that
            # sits on a control the tree already named is the same target.
            fused = ocr.fuse(elements, text)
            fused = ocr.fuse(fused, icons)
            added = len(fused) - len(elements)
            if not added:
                return fused, f"{note} {warning}".strip() or NO_TREE_HINT
            # Count what survived, not what was detected. Fusion drops every
            # guess that lands on something already named, and a note promising
            # 47 icons when 23 are listed sends the reader looking for 24 that
            # were never there.
            kept = {"ocr": 0, "vision": 0}
            for element in fused:
                if element.source in kept:
                    kept[element.source] += 1
            parts = [f"{added} element(s) recognised from the window's pixels"]
            if kept["ocr"]:
                parts.append(f"{kept['ocr']} from Windows OCR (text only, names can be wrong)")
            if kept["vision"]:
                parts.append(f"{kept['vision']} icons from OmniParser "
                             "(named by a caption model)")
            parts.append("all of them can only be clicked.")
            extra = "; ".join(parts)
            if warning:
                extra = f"{extra} {warning}"
            return fused, f"{note} {extra}".strip() if note else extra
        except Exception as exc:  # noqa: BLE001 - reading pixels is a bonus, never a blocker
            failed = f"reading the pixels failed ({type(exc).__name__}: {exc}). {NO_TREE_HINT}"
            return elements, f"{note} {failed}".strip() if note else failed

    def after_action(self, mode: str = "auto") -> tuple[dict[str, Any], Optional[bytes], str]:
        """The state an action produced, as a diff or (when there is no tree) an image.

        ``auto`` is the useful default: a window with an accessibility tree gets
        a cheap textual diff, and a window without one gets a picture, because
        for Electron and self-drawn applications a picture is the only state
        there is.
        """
        if mode == "none":
            return {}, None, ""
        previous = self._last
        waited = settle()
        # Look at whatever is in front now, not at the scope that was read
        # before: an action that closes a dialog moves the user's attention to
        # the window behind it, and so should the report.
        # Not wait_for_content: settle() above already waited for the screen to
        # stop changing, and this read is a report on what the action did, not a
        # request for a page. Waiting again cost 4 seconds every time an action
        # left some small helper window in front -- a click on a link reported
        # itself in 7.5s, nearly all of it spent waiting for a window that was
        # never going to fill up.
        observation = self.observe("windows" if previous and previous.scope == "windows"
                                   else "focused", wait_for_content=False)
        payload: dict[str, Any] = {"settle": waited}
        # "Nothing I can name and act on" is the test that matters, not "no
        # elements at all": a window that exposes only its own frame gives a
        # diff of one useless row, and a picture instead is what helps.
        actionable = _has_content([element for _ref, element in observation.elements])
        wants_image = mode == "image" or (mode == "auto" and not actionable)
        if mode in ("ui", "full"):
            payload |= observation.to_dict()
        else:
            payload |= diff(previous, observation)
            if observation.note:
                payload["note"] = observation.note
        if not wants_image:
            return payload, None, ""
        from . import capture

        try:
            shot = capture.capture(region=_window_region(self.ui.foreground()[0]), max_edge=1100)
            payload["image"] = shot.to_dict()
            return payload, shot.data, shot.mime
        except Exception as exc:  # noqa: BLE001 - an action must not fail over its report
            payload["image_error"] = f"{type(exc).__name__}: {exc}"
            return payload, None, ""

    # -- the guard ---------------------------------------------------------------------

    def require(self, action: str) -> _State:
        with self._lock:
            if self._state is None:
                raise ControlError(
                    f"{action} needs an open control session: call start_control first "
                    "(it shows the on-screen indicator), and stop_control when finished"
                )
            self._state.last_activity = time.time()
            self._state.actions += 1
            return self._state


#: Window classes that host a packaged app's real content, usually in another
#: process. The frame window's own tree stops before reaching them.
HOSTED_CLASSES = (
    "Windows.UI.Core.CoreWindow",
    "Windows.UI.Composition.DesktopWindowContentBridge",
    "Microsoft.UI.Content.DesktopChildSiteBridge",
)


def _hosted_children(hwnd: int) -> list[int]:
    import ctypes

    from .win32 import WNDENUMPROC, user32

    found: list[int] = []

    def visit(child, _param):
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(child, name, len(name))
        if name.value in HOSTED_CLASSES:
            found.append(int(child))
        return True

    if hwnd:
        user32.EnumChildWindows(hwnd, WNDENUMPROC(visit), 0)
    return found


def _is_furniture(element: Element) -> bool:
    """Whether this element is part of the window frame rather than the app."""
    return (
        element.role == "window"
        or "TitleBar" in element.class_name
        or (element.role in ("menu", "menuitem") and element.name in SYSTEM_MENU_NAMES)
        or any(keyword in element.name for keyword in CAPTION_KEYWORDS)
    )


def _paint_frame(hwnd: int) -> Optional[bytes]:
    """A thumbnail of one window, small enough to compare in a moment."""
    from . import capture

    try:
        image, _fallback = capture.window_image(hwnd)
    except OSError:
        return None
    return image.resize(PAINT_THUMBNAIL).tobytes()


def _has_body_content(hwnd: int, elements: list[Element]) -> bool:
    """Whether anything has been drawn below the window's own frame.

    "Is there a tree" and "has the application filled it in" are different
    questions, and the second one is geometric: a window that is still loading
    answers with its title bar, its tabs and its toolbar, all of which live in
    a strip along the top. Anything further down is content.
    """
    region = _window_region(hwnd)
    if region is None:
        return True  # minimised or gone: waiting cannot help
    line = region["y"] + int(region["height"] * CONTENT_TOP_FRACTION)
    below = sum(1 for element in elements
                if element.rect[1] > line and not _is_furniture(element))
    return below >= CONTENT_MINIMUM


def _has_content(elements: list[Element]) -> bool:
    """Whether anything here is the application rather than its window frame."""
    return any(element.actions and not _is_furniture(element) for element in elements)


def _matching(elements: list[Element], query: str) -> list[Element]:
    """Elements whose name or value contains ``query``, case-insensitively."""
    if not query:
        return elements
    needle = query.strip().lower()
    return [
        element for element in elements
        if needle in element.name.lower() or needle in element.value.lower()
    ]


def _window_region(hwnd: int) -> Optional[dict[str, int]]:
    """The focused window's rectangle, for a picture of just what changed."""
    import ctypes
    from ctypes import wintypes

    from .win32 import user32

    if not hwnd:
        return None
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    if rect.left < -30000 or rect.top < -30000:
        return None  # minimised windows park at (-32000, -32000)
    return {"x": rect.left, "y": rect.top, "width": width, "height": height}


def _iso(stamp: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds")
