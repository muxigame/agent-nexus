"""Windows UI Automation: read the screen as named elements instead of pixels.

Why this exists: clicking by coordinate means reading a screenshot, measuring a
button, and hoping. Clicking by element means asking for the control called
"不保存" and getting its exact rectangle -- or a clean error saying it is gone.
The second one is the difference between an agent that works and one that
occasionally types into the wrong window.

Three design decisions carry most of the weight:

**One cross-process call per read.** A naive walk asks the target process for
every property of every element, which takes seconds on a big window.
``FindAllBuildCache`` fetches a whole filtered subtree with all the properties
we want in a single call, and the filter runs inside the provider.

**No live COM pointers are kept between tool calls.** UIA elements go stale the
moment the UI changes. Each element is stored as a *recipe* for finding it
again (window handle, control type, name, index, runtime id, rectangle); acting
on it re-finds it and verifies identity first. If the recipe no longer matches,
the tool says so instead of clicking where the thing used to be.

**A dedicated MTA thread owns the client.** COM objects belong to the thread
that made them, and tool calls arrive on arbitrary thread-pool threads. This
thread must never be the overlay thread: that one runs the low-level keyboard
hook for the ESC hatch, and Windows removes a hook whose thread stalls.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from functools import reduce
from typing import Any, Callable, Optional

import comtypes
import comtypes.client

UIA = comtypes.client.GetModule("UIAutomationCore.dll")

TREE_ELEMENT = 1
TREE_CHILDREN = 2
TREE_DESCENDANTS = 4
TREE_SUBTREE = 7
ELEMENT_MODE_NONE = 0  # cached properties only: no live reference, much cheaper

#: Properties fetched for every element, in one bulk request.
PROPERTIES = (
    UIA.UIA_NamePropertyId,
    UIA.UIA_ControlTypePropertyId,
    UIA.UIA_LocalizedControlTypePropertyId,
    UIA.UIA_BoundingRectanglePropertyId,
    UIA.UIA_IsOffscreenPropertyId,
    UIA.UIA_IsEnabledPropertyId,
    UIA.UIA_IsKeyboardFocusablePropertyId,
    UIA.UIA_AutomationIdPropertyId,
    UIA.UIA_ClassNamePropertyId,
    UIA.UIA_NativeWindowHandlePropertyId,
    UIA.UIA_RuntimeIdPropertyId,
    UIA.UIA_ValueValuePropertyId,
    UIA.UIA_IsPasswordPropertyId,
    UIA.UIA_HelpTextPropertyId,
    UIA.UIA_IsInvokePatternAvailablePropertyId,
    UIA.UIA_IsValuePatternAvailablePropertyId,
    UIA.UIA_IsTogglePatternAvailablePropertyId,
    UIA.UIA_IsExpandCollapsePatternAvailablePropertyId,
    UIA.UIA_IsSelectionItemPatternAvailablePropertyId,
    UIA.UIA_IsScrollItemPatternAvailablePropertyId,
)

#: Control types worth showing a model: things you can click, type into, or read
#: a state from. Everything else (panes, groups, separators, decorative images)
#: is structure, and structure inflates the tree without helping anyone decide.
INTERACTABLE = {
    UIA.UIA_ButtonControlTypeId,
    UIA.UIA_CheckBoxControlTypeId,
    UIA.UIA_ComboBoxControlTypeId,
    UIA.UIA_EditControlTypeId,
    UIA.UIA_HyperlinkControlTypeId,
    UIA.UIA_ListItemControlTypeId,
    UIA.UIA_MenuItemControlTypeId,
    UIA.UIA_RadioButtonControlTypeId,
    UIA.UIA_TabItemControlTypeId,
    UIA.UIA_TreeItemControlTypeId,
    UIA.UIA_SliderControlTypeId,
    UIA.UIA_SpinnerControlTypeId,
    UIA.UIA_SplitButtonControlTypeId,
    UIA.UIA_DocumentControlTypeId,
    UIA.UIA_DataItemControlTypeId,
    UIA.UIA_HeaderItemControlTypeId,
    UIA.UIA_MenuControlTypeId,
    UIA.UIA_ListControlTypeId,
    UIA.UIA_TabControlTypeId,
    UIA.UIA_TreeControlTypeId,
    UIA.UIA_WindowControlTypeId,
}
#: Shown only when they carry a name: a labelled static text tells the model
#: what a dialog says, an unlabelled one is noise.
LABELLED = {
    UIA.UIA_TextControlTypeId,
    UIA.UIA_ImageControlTypeId,
    UIA.UIA_GroupControlTypeId,
    UIA.UIA_ProgressBarControlTypeId,
    UIA.UIA_StatusBarControlTypeId,
    UIA.UIA_ToolTipControlTypeId,
}
#: Below this, a rectangle is a separator or a rendering artefact, not a target.
MIN_EDGE = 3
MAX_ELEMENTS = 200
MAX_NAME = 160

CONTROL_NAMES = {
    value: name[len("UIA_"):-len("ControlTypeId")].lower()
    for name, value in vars(UIA).items()
    if name.startswith("UIA_") and name.endswith("ControlTypeId")
}

#: Action name -> the property that says whether this control really supports it.
#:
#: ``GetCachedPattern`` is not a reliable test: with cached patterns it hands
#: back a proxy for controls that do not implement the pattern at all, which
#: made every button claim it could be toggled and expanded. The
#: ``Is*PatternAvailable`` properties are the documented answer, and they are
#: plain cacheable booleans, so they also cost nothing extra to fetch.
PATTERN_AVAILABILITY = {
    "invoke": UIA.UIA_IsInvokePatternAvailablePropertyId,
    "set_value": UIA.UIA_IsValuePatternAvailablePropertyId,
    "toggle": UIA.UIA_IsTogglePatternAvailablePropertyId,
    "expand": UIA.UIA_IsExpandCollapsePatternAvailablePropertyId,
    "select": UIA.UIA_IsSelectionItemPatternAvailablePropertyId,
    "scroll_into_view": UIA.UIA_IsScrollItemPatternAvailablePropertyId,
}


@dataclass(frozen=True)
class Element:
    """One control, plus everything needed to find it again later."""

    role: str
    name: str
    rect: tuple[int, int, int, int]
    control_type: int = 0
    value: str = ""
    enabled: bool = True
    focusable: bool = False
    password: bool = False
    automation_id: str = ""
    class_name: str = ""
    hwnd: int = 0
    runtime_id: tuple[int, ...] = ()
    window: str = ""
    window_hwnd: int = 0
    index: int = 0
    minimised: bool = False
    #: Where this element came from: ``"uia"`` means the operating system named
    #: it, ``"ocr"`` means it was read off the pixels and may be wrong. UFO²
    #: carries the same field and then drops it before the model sees it; the
    #: model is exactly who needs to know.
    source: str = "uia"
    actions: tuple[str, ...] = ()

    @property
    def centre(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return (left + right) // 2, (top + bottom) // 2

    def to_dict(self, ref: str) -> dict[str, Any]:
        """Compact form for a tool result: empty fields are left out."""
        left, top, right, bottom = self.rect
        out: dict[str, Any] = {
            "ref": ref,
            "role": self.role,
            "name": self.name[:MAX_NAME],
            "rect": {"x": left, "y": top, "width": right - left, "height": bottom - top},
            "centre": {"x": self.centre[0], "y": self.centre[1]},
        }
        if self.value:
            out["value"] = self.value[:MAX_NAME]
        if not self.enabled:
            out["enabled"] = False
        if self.password:
            out["password"] = True
        if self.automation_id:
            out["automation_id"] = self.automation_id
        if self.minimised:
            out["minimised"] = True
        if self.role == "window":
            out["hwnd"] = self.hwnd
        if self.source != "uia":
            out["source"] = self.source
        if self.actions:
            out["actions"] = list(self.actions)
        return out

    def identity(self) -> tuple:
        """What must still match for a re-found element to be the same one."""
        return (self.window_hwnd, self.control_type, self.name, self.index)


class UiaError(RuntimeError):
    pass


class UiaTimeout(UiaError):
    """Raised when a provider does not answer in time.

    Worth its own type because the answer is different: the window is not
    broken and the element is not gone, the application simply is not
    answering, so the caller should read its pixels instead of retrying. One
    Unity editor on this machine takes 23 seconds to return its seventeen
    elements, every time.
    """


#: Shell windows have no title, so the ordinary filter drops them -- and with
#: them the taskbar and the desktop, which are perfectly readable: the taskbar
#: names every button ("企业微信 - 3 个运行窗口 已固定", "任务视图") and the desktop
#: exposes each icon as an invokable item. They are given names here so a model
#: can ask for them by name like any other window.
SHELL_WINDOWS = {
    "Shell_TrayWnd": "任务栏",
    "Shell_SecondaryTrayWnd": "任务栏（副屏）",
    "Progman": "桌面",
    "WorkerW": "桌面",
}


def windows() -> list[Element]:
    """Top-level windows, in z-order, as elements.

    Deliberately Win32 rather than UIA. Asking UIA for the desktop's children
    queries every process that owns a window, and one slow or hung application
    stalls the whole enumeration -- measured at 5.97s for seventeen windows on
    this machine, against 0.004s for thirty-six here. ``EnumWindows`` reads the
    window manager's own list, and UIA is then only needed *inside* the window
    that was chosen.
    """
    import ctypes
    from ctypes import wintypes

    from .win32 import GWL_EXSTYLE, WNDENUMPROC, WS_EX_TOOLWINDOW, user32

    found: list[Element] = []

    def visit(hwnd, _param):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, len(cls))
        shell_name = SHELL_WINDOWS.get(cls.value)
        # Tool windows are palettes and helpers, not places a task happens --
        # except the taskbar, which is one, and is somewhere a task very much
        # happens.
        if not shell_name and user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, len(title))
        if not title.value.strip() and not shell_name:
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if rect.right - rect.left < MIN_EDGE or rect.bottom - rect.top < MIN_EDGE:
            return True
        minimised = bool(user32.IsIconic(hwnd))
        found.append(Element(
            role="window",
            # The friendly name wins for shell windows: the desktop's real title
            # is "Program Manager", which nobody would think to ask for.
            name=shell_name or title.value.strip(),
            rect=(rect.left, rect.top, rect.right, rect.bottom),
            control_type=UIA.UIA_WindowControlTypeId,
            class_name=cls.value,
            hwnd=int(hwnd),
            window=shell_name or title.value.strip(),
            window_hwnd=int(hwnd),
            index=len(found),
            minimised=minimised,
            actions=("focus", "restore") if minimised else ("focus",),
        ))
        return True

    user32.EnumWindows(WNDENUMPROC(visit), 0)
    return found


#: How long to wait for a window to describe itself. Reading is the one call
#: that has a cheap alternative -- the pixels -- so it does not get the long
#: timeout that acting on an element does.
READ_TIMEOUT_S = 4.0


class _Worker:
    """Runs every UIA call on one COM-initialised thread."""

    def __init__(self) -> None:
        #: Set when a call was abandoned. The thread is still inside it, so
        #: everything queued behind it would wait too: one slow application
        #: made reading every *other* window take twenty seconds as well.
        self.stuck = False
        self._jobs: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._error = ""
        self.automation = None
        self._thread = threading.Thread(target=self._run, name="control-uia", daemon=True)
        self._thread.start()
        if not self._ready.wait(20):
            raise UiaError("the UI Automation thread did not start")
        if self._error:
            raise UiaError(self._error)

    def _run(self) -> None:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            self.automation = comtypes.CoCreateInstance(
                UIA.CUIAutomation._reg_clsid_, interface=UIA.IUIAutomation,
                clsctx=comtypes.CLSCTX_INPROC_SERVER,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the constructor
            self._error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
            return
        self._ready.set()
        while True:
            job, reply = self._jobs.get()
            if job is None:
                return
            try:
                reply.put(("ok", job()))
            except UiaError as exc:
                reply.put(("error", exc))
            except Exception as exc:  # noqa: BLE001 - travels back to the caller
                # Everything leaving this module is a UiaError. A raw COMError
                # would escape the callers' `except` clauses and turn an
                # ordinary "that control is gone" into a crashed tool call.
                reply.put(("error", UiaError(f"{type(exc).__name__}: {exc}")))

    def call(self, job: Callable[[], Any], timeout: float = 30.0) -> Any:
        reply: queue.Queue = queue.Queue()
        self._jobs.put((job, reply))
        try:
            status, payload = reply.get(timeout=timeout)
        except queue.Empty as exc:
            # The job is still running on the thread and will eventually finish
            # and answer a queue nobody is reading. Retiring the thread costs
            # one thread until the call returns; not retiring it costs every
            # call made in the meantime.
            self.stuck = True
            self._jobs.put((None, None))
            raise UiaTimeout(f"a UI Automation call took longer than {timeout}s") from exc
        if status == "error":
            raise payload
        return payload


class UiaReader:
    """Reads elements, and re-finds one when it is time to act on it."""

    def __init__(self) -> None:
        self._worker: Optional[_Worker] = None
        self._lock = threading.Lock()

    # -- plumbing ----------------------------------------------------------------------

    @property
    def worker(self) -> _Worker:
        with self._lock:
            if self._worker is None or self._worker.stuck:
                self._worker = _Worker()
            return self._worker

    def _cache_request(self, mode: int = ELEMENT_MODE_NONE):
        request = self.worker.automation.CreateCacheRequest()
        request.AutomationElementMode = mode
        for prop in PROPERTIES:
            request.AddProperty(prop)
        return request

    def _condition_any(self):
        return self.worker.automation.CreateTrueCondition()

    def _condition_controls(self, filter_mode: str):
        """The filter, expressed so that the *target process* applies it.

        This is the trick worth having: a condition handed to
        ``FindAllBuildCache`` runs inside the provider, so a window with ten
        thousand elements only sends back the few hundred that matter. Filtering
        in Python instead means marshalling all ten thousand across a process
        boundary first.

        Microsoft's own UFO² also puts ``IsEnabled == True`` in here. We do not:
        a greyed-out button is worth reporting -- "the OK button is disabled"
        tells a model to go fill the form in -- so disabled controls come back
        marked ``enabled: false`` instead of vanishing.
        """
        automation = self.worker.automation
        if filter_mode == "all":
            return automation.CreateTrueCondition()
        types = [
            automation.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, control_type)
            for control_type in sorted(INTERACTABLE | LABELLED)
        ]
        any_type = reduce(lambda a, b: automation.CreateOrCondition(a, b), types)
        return reduce(lambda a, b: automation.CreateAndCondition(a, b), [
            # Offscreen means scrolled out of view or on a hidden tab: not a target.
            automation.CreatePropertyCondition(UIA.UIA_IsOffscreenPropertyId, False),
            # Providers mark pure structure as non-control; it only inflates the tree.
            automation.CreatePropertyCondition(UIA.UIA_IsControlElementPropertyId, True),
            any_type,
        ])

    # -- reading -----------------------------------------------------------------------

    def foreground(self) -> tuple[int, str]:
        from .inputs import foreground_window
        from .win32 import user32

        return int(user32.GetForegroundWindow() or 0), foreground_window()

    def elements(self, hwnd: int, filter_mode: str = "interactive",
                 max_elements: int = MAX_ELEMENTS) -> tuple[list[Element], bool]:
        """Every interesting control inside one window, plus whether it was cut short."""
        def job() -> tuple[list[Element], bool]:
            automation = self.worker.automation
            try:
                window = automation.ElementFromHandle(hwnd)
            except Exception as exc:  # noqa: BLE001
                # Do not guess why. The provider can be absent because the
                # window closed, because it runs elevated and this process may
                # not read it, or because it never implemented UIA at all --
                # and the useful part is what to do next, not the diagnosis.
                raise UiaError(
                    f"window {hwnd} did not answer UI Automation ({_com_code(exc)}); it may have "
                    "closed, or be elevated, or expose no accessibility tree. Use screenshot and "
                    "coordinates for it."
                ) from exc
            title = _string(window.CurrentName)
            found = window.FindAllBuildCache(TREE_SUBTREE, self._condition_controls(filter_mode),
                                             self._cache_request())
            kept: list[Element] = []
            seen: set[tuple] = set()
            for index in range(found.Length):
                element = self._read(found.GetElement(index), title, hwnd, index)
                if element is None or not self._keep(element, filter_mode):
                    continue
                signature = (element.control_type, element.name, element.rect)
                if signature in seen:
                    continue  # providers routinely expose the same control twice
                seen.add(signature)
                kept.append(element)
                if len(kept) >= max_elements:
                    return kept, index + 1 < found.Length
            return kept, False
        return self.worker.call(job, timeout=READ_TIMEOUT_S)

    def _keep(self, element: Element, filter_mode: str) -> bool:
        left, top, right, bottom = element.rect
        if right - left < MIN_EDGE or bottom - top < MIN_EDGE:
            return False
        if filter_mode == "all":
            return True
        if element.control_type in INTERACTABLE:
            # A nameless edit box is still a target; a nameless list is not.
            return bool(element.name or element.value or element.focusable)
        if element.control_type in LABELLED:
            return bool(element.name)
        return False

    def _read(self, raw, window_title: str, window_hwnd: int, index: int) -> Optional[Element]:
        try:
            control_type = int(raw.GetCachedPropertyValue(UIA.UIA_ControlTypePropertyId) or 0)
            rect = raw.GetCachedPropertyValue(UIA.UIA_BoundingRectanglePropertyId)
            if not rect or len(rect) != 4:
                return None
            left, top, width, height = (int(v) for v in rect)
            patterns = tuple(
                action for action, prop in PATTERN_AVAILABILITY.items()
                if raw.GetCachedPropertyValue(prop)
            )
            runtime = raw.GetCachedPropertyValue(UIA.UIA_RuntimeIdPropertyId)
            return Element(
                role=CONTROL_NAMES.get(control_type, f"type_{control_type}"),
                name=_string(raw.GetCachedPropertyValue(UIA.UIA_NamePropertyId)),
                rect=(left, top, left + width, top + height),
                control_type=control_type,
                value=_string(raw.GetCachedPropertyValue(UIA.UIA_ValueValuePropertyId)),
                enabled=bool(raw.GetCachedPropertyValue(UIA.UIA_IsEnabledPropertyId)),
                focusable=bool(raw.GetCachedPropertyValue(UIA.UIA_IsKeyboardFocusablePropertyId)),
                password=bool(raw.GetCachedPropertyValue(UIA.UIA_IsPasswordPropertyId)),
                automation_id=_string(raw.GetCachedPropertyValue(UIA.UIA_AutomationIdPropertyId)),
                class_name=_string(raw.GetCachedPropertyValue(UIA.UIA_ClassNamePropertyId)),
                hwnd=int(raw.GetCachedPropertyValue(UIA.UIA_NativeWindowHandlePropertyId) or 0),
                runtime_id=tuple(int(v) for v in (runtime or ())),
                window=window_title,
                window_hwnd=window_hwnd or int(
                    raw.GetCachedPropertyValue(UIA.UIA_NativeWindowHandlePropertyId) or 0),
                index=index,
                actions=patterns,
            )
        except Exception:  # noqa: BLE001 - one unreadable element must not lose the rest
            return None

    # -- acting ------------------------------------------------------------------------

    def _resolve_here(self, element: Element) -> tuple[Any, Element]:
        """Find the live element again, refusing anything that is not clearly it.

        Runs **on** the worker thread: going back through the queue from inside
        a job would be the thread waiting for itself.

        Runtime ids are the strongest key but not every provider keeps them
        stable, so a name-and-type search with position is the fallback. Either
        way the result is checked before it is handed back -- a wrong match here
        is a click on the wrong control.
        """
        automation = self.worker.automation
        try:
            window = automation.ElementFromHandle(element.window_hwnd)
        except Exception as exc:  # noqa: BLE001 - a closed window is expected, not exceptional
            raise UiaError(
                f"the window that held {element.role} {element.name!r} is gone; "
                "read the elements again"
            ) from exc
        request = self._cache_request(mode=1)  # Full: live patterns are needed
        # Ask for this control specifically rather than re-reading the window:
        # on a large window the second walk would cost as much as the first.
        narrow = automation.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId,
                                                    element.control_type)
        if element.name:
            narrow = automation.CreateAndCondition(
                narrow,
                automation.CreatePropertyCondition(UIA.UIA_NamePropertyId, element.name),
            )
        found = window.FindAllBuildCache(TREE_SUBTREE, narrow, request)
        by_shape = []
        for index in range(found.Length):
            candidate = found.GetElement(index)
            read = self._read(candidate, element.window, element.window_hwnd, index)
            if read is None:
                continue
            if element.runtime_id and read.runtime_id == element.runtime_id:
                return candidate, read
            if read.control_type == element.control_type and read.name == element.name:
                by_shape.append((candidate, read))
        if len(by_shape) == 1:
            return by_shape[0]
        for candidate, read in by_shape:
            if read.index == element.index or read.rect == element.rect:
                return candidate, read
        raise UiaError(
            f"the element {element.role} {element.name!r} is no longer in this window "
            "(the UI changed); read the elements again"
        )

    def resolve(self, element: Element) -> Element:
        """The element as it is right now, for checking it is still there."""
        return self.worker.call(lambda: self._resolve_here(element)[1])

    def _wait_ready(self, element: Element, timeout: float = 2.0) -> tuple[Any, Element]:
        """Re-find the control and wait for it to be usable.

        A control can exist but not yet be ready -- a dialog's button is often
        disabled for a moment while the dialog settles. Polling briefly here
        turns a spurious failure into a short pause, and a genuine one into an
        error that names the state.
        """
        deadline = time.time() + timeout
        live, read = self._resolve_here(element)
        while (not read.enabled) and time.time() < deadline:
            time.sleep(0.15)
            live, read = self._resolve_here(element)
        if not read.enabled:
            raise UiaError(
                f"{read.role} {read.name!r} is disabled, so it cannot be used yet"
            )
        return live, read

    def act(self, element: Element, action: str, text: str = "") -> dict[str, Any]:
        """Use the control's own pattern rather than a synthesised click."""
        def job() -> dict[str, Any]:
            live, read = self._wait_ready(element)
            if action == "invoke":
                _pattern(live, UIA.UIA_InvokePatternId, UIA.IUIAutomationInvokePattern).Invoke()
            elif action == "set_value":
                if read.password:
                    raise UiaError("refusing to write into a password field")
                _pattern(live, UIA.UIA_ValuePatternId,
                         UIA.IUIAutomationValuePattern).SetValue(text)
            elif action == "toggle":
                _pattern(live, UIA.UIA_TogglePatternId, UIA.IUIAutomationTogglePattern).Toggle()
            elif action in ("expand", "collapse"):
                pattern = _pattern(live, UIA.UIA_ExpandCollapsePatternId,
                                   UIA.IUIAutomationExpandCollapsePattern)
                pattern.Expand() if action == "expand" else pattern.Collapse()
            elif action == "select":
                _pattern(live, UIA.UIA_SelectionItemPatternId,
                         UIA.IUIAutomationSelectionItemPattern).Select()
            elif action == "scroll_into_view":
                _pattern(live, UIA.UIA_ScrollItemPatternId,
                         UIA.IUIAutomationScrollItemPattern).ScrollIntoView()
            elif action == "focus":
                live.SetFocus()
            else:
                raise UiaError(f"unsupported element action: {action}")
            return {"action": action, "role": read.role, "name": read.name}
        return self.worker.call(job)

    def clickable_point(self, element: Element) -> tuple[int, int]:
        """Where to click: the provider's own point when it offers one."""
        def job() -> tuple[int, int]:
            live, read = self._resolve_here(element)
            try:
                point, available = live.GetClickablePoint()
                if available:
                    return int(point.x), int(point.y)
            except Exception:  # noqa: BLE001 - fall back to the rectangle's middle
                pass
            return read.centre
        return self.worker.call(job)


def _pattern(live, pattern_id: int, interface):
    got = live.GetCurrentPattern(pattern_id)
    if not got:
        raise UiaError("this control does not support that action; try a mouse click instead")
    return got.QueryInterface(interface)


def _string(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _com_code(exc: Exception) -> str:
    """The HRESULT alone: the accompanying COM text is noise in a tool result."""
    code = getattr(exc, "hresult", None)
    if code is None:
        args = getattr(exc, "args", ())
        code = args[0] if args and isinstance(args[0], int) else None
    return f"0x{code & 0xFFFFFFFF:08X}" if isinstance(code, int) else type(exc).__name__
