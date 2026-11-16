"""Mouse and keyboard injection through ``SendInput``.

Coordinates are virtual-desktop pixels and are normalised per call, so a second
monitor to the left of the primary (negative x) works without special cases.

Moves are interpolated rather than teleported. Some applications only start a
drag after they have seen motion, and a pointer that visibly travels is far
easier for the person at the machine to follow than one that jumps.
"""
from __future__ import annotations

import ctypes
import time

from .win32 import (
    INPUT,
    INPUT_KEYBOARD,
    INPUT_MOUSE,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_HWHEEL,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MIDDLEDOWN,
    MOUSEEVENTF_MIDDLEUP,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    MOUSEEVENTF_VIRTUALDESK,
    MOUSEEVENTF_WHEEL,
    WHEEL_DELTA,
    user32,
    virtual_screen,
)

BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

MODIFIERS = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "menu": 0x12,
    "win": 0x5B, "super": 0x5B, "cmd": 0x5B, "meta": 0x5B,
}

#: Named keys.  The second element marks keys that need ``KEYEVENTF_EXTENDEDKEY``
#: -- without it the arrow keys and the navigation block send their numpad twins.
KEYS: dict[str, tuple[int, bool]] = {
    "enter": (0x0D, False), "return": (0x0D, False), "tab": (0x09, False),
    "esc": (0x1B, False), "escape": (0x1B, False), "space": (0x20, False),
    "backspace": (0x08, False), "delete": (0x2E, True), "del": (0x2E, True),
    "insert": (0x2D, True), "home": (0x24, True), "end": (0x23, True),
    "pageup": (0x21, True), "pagedown": (0x22, True), "pgup": (0x21, True), "pgdn": (0x22, True),
    "up": (0x26, True), "down": (0x28, True), "left": (0x25, True), "right": (0x27, True),
    "capslock": (0x14, False), "numlock": (0x90, True), "scrolllock": (0x91, False),
    "printscreen": (0x2C, True), "prtsc": (0x2C, True), "pause": (0x13, False),
    "apps": (0x5D, True), "menu_key": (0x5D, True),
    "volumeup": (0xAF, False), "volumedown": (0xAE, False), "volumemute": (0xAD, False),
    "mediaplay": (0xB3, False), "medianext": (0xB0, False), "mediaprev": (0xB1, False),
    **{f"f{n}": (0x6F + n, False) for n in range(1, 25)},
    **{name: (vk, vk == 0x5B) for name, vk in MODIFIERS.items()},
}


def _send(*inputs: INPUT) -> int:
    array = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise OSError(f"SendInput delivered {sent}/{len(inputs)} events "
                      f"(error {ctypes.get_last_error()}); a more privileged window may have focus")
    return sent


def _mouse(flags: int, x: int = 0, y: int = 0, data: int = 0) -> INPUT:
    event = INPUT(type=INPUT_MOUSE)
    event.mi.dx, event.mi.dy = x, y
    event.mi.mouseData = data & 0xFFFFFFFF
    event.mi.dwFlags = flags
    return event


def _key(vk: int, up: bool = False, extended: bool = False) -> INPUT:
    event = INPUT(type=INPUT_KEYBOARD)
    event.ki.wVk = vk
    event.ki.dwFlags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if extended else 0)
    return event


def _unicode(code_unit: int, up: bool = False) -> INPUT:
    event = INPUT(type=INPUT_KEYBOARD)
    event.ki.wScan = code_unit
    event.ki.dwFlags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return event


def to_absolute(x: int, y: int) -> tuple[int, int]:
    """Virtual-desktop pixels -> the 0..65535 space ``SendInput`` expects."""
    left, top, width, height = virtual_screen()
    nx = round((x - left) * 65535 / max(1, width - 1))
    ny = round((y - top) * 65535 / max(1, height - 1))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def position() -> tuple[int, int]:
    from .cursor import current_position

    return current_position()


def foreground_window() -> str:
    """Title of the window input is currently going to.

    Returned with every action so the model can tell that its keystrokes landed
    where it meant them to, instead of typing into whatever stole focus.
    """
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    buffer = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def move(x: int, y: int, steps: int | None = None, duration: float = 0.12) -> tuple[int, int]:
    """Glide the pointer to ``(x, y)``."""
    start_x, start_y = position()
    distance = max(abs(x - start_x), abs(y - start_y))
    if steps is None:
        steps = max(1, min(24, distance // 40))
    for index in range(1, steps + 1):
        fraction = index / steps
        # Ease out: fast away from the start, settling onto the target.
        eased = 1 - (1 - fraction) ** 3
        nx, ny = to_absolute(round(start_x + (x - start_x) * eased),
                             round(start_y + (y - start_y) * eased))
        _send(_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny))
        if index < steps:
            time.sleep(duration / steps)
    return position()


def click(button: str = "left", count: int = 1, interval: float = 0.06) -> None:
    down, up = BUTTONS[button]
    for index in range(count):
        _send(_mouse(down), _mouse(up))
        if index + 1 < count:
            time.sleep(interval)


def button_down(button: str = "left") -> None:
    _send(_mouse(BUTTONS[button][0]))


def button_up(button: str = "left") -> None:
    _send(_mouse(BUTTONS[button][1]))


def drag(to_x: int, to_y: int, button: str = "left", from_x: int | None = None,
         from_y: int | None = None) -> tuple[int, int]:
    if from_x is not None and from_y is not None:
        move(from_x, from_y)
        time.sleep(0.05)
    button_down(button)
    time.sleep(0.08)
    move(to_x, to_y, steps=18, duration=0.35)
    time.sleep(0.08)
    button_up(button)
    return position()


def scroll(amount: int, horizontal: bool = False) -> None:
    """Scroll by notches; positive is up (or right)."""
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    delta = amount * WHEEL_DELTA
    _send(_mouse(flag, data=delta if delta >= 0 else (delta + (1 << 32))))


def parse_chord(chord: str) -> tuple[list[tuple[int, bool]], tuple[int, bool]]:
    """``"ctrl+shift+s"`` -> the modifiers to hold and the key to strike."""
    parts = [part.strip().lower() for part in chord.split("+") if part.strip()]
    if not parts:
        raise ValueError("empty key chord")
    *modifier_names, key_name = parts
    modifiers = []
    for name in modifier_names:
        if name not in MODIFIERS:
            raise ValueError(f"unknown modifier: {name!r}")
        modifiers.append((MODIFIERS[name], MODIFIERS[name] == 0x5B))
    if key_name in KEYS:
        return modifiers, KEYS[key_name]
    if len(key_name) == 1:
        scan = user32.VkKeyScanW(key_name)
        if scan == -1:
            raise ValueError(f"{key_name!r} cannot be typed on the current keyboard layout")
        vk = scan & 0xFF
        if scan & 0x100 and not any(vk == 0x10 for vk, _ in modifiers):
            modifiers.append((0x10, False))  # the layout needs shift for this character
        return modifiers, (vk, False)
    raise ValueError(f"unknown key: {key_name!r}")


def press(chords: str, interval: float = 0.04) -> list[str]:
    """Strike one or more chords, e.g. ``"ctrl+a delete"``."""
    struck: list[str] = []
    for chord in chords.split():
        modifiers, (vk, extended) = parse_chord(chord)
        events = [_key(mod, extended=ext) for mod, ext in modifiers]
        events.append(_key(vk, extended=extended))
        events.append(_key(vk, up=True, extended=extended))
        events += [_key(mod, up=True, extended=ext) for mod, ext in reversed(modifiers)]
        _send(*events)
        struck.append(chord)
        time.sleep(interval)
    return struck


def type_text(text: str, chunk: int = 40, interval: float = 0.01) -> int:
    """Type literal text, including characters the layout has no key for.

    ``KEYEVENTF_UNICODE`` takes UTF-16 code units, so anything outside the BMP
    is delivered as its surrogate pair.

    A newline is struck as Return rather than sent as U+000A, which almost
    nothing acts on: typing a URL that ends in a newline into a browser's
    address bar
    filled the bar in and then sat there, because the page only navigates on a
    real Return.
    """
    for index, line in enumerate(text.replace("\r\n", "\n").split("\n")):
        if index:
            press("enter")
        if line:
            _type_units(line, chunk, interval)
    return len(text)


def _type_units(text: str, chunk: int, interval: float) -> None:
    units = list(text.encode("utf-16-le"))
    code_units = [units[i] | (units[i + 1] << 8) for i in range(0, len(units), 2)]
    for start in range(0, len(code_units), chunk):
        events: list[INPUT] = []
        for unit in code_units[start:start + chunk]:
            events.append(_unicode(unit))
            events.append(_unicode(unit, up=True))
        _send(*events)
        time.sleep(interval)
