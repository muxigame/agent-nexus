"""Reading text off the screen, for windows that expose no accessibility tree.

This exists because of a round-trip argument. A ``read_ui`` that answers "this
window has no tree, go and use a screenshot" costs a call to learn nothing: the
model then screenshots, measures a button by eye, and clicks a guess. Three
calls to do what one should. So the fallback happens *inside* the read: UIA
first, and where UIA is blind, the text on screen becomes elements with the
same refs and the same shape. The model never has to know which happened.

The engine is Windows' own (``Windows.Media.Ocr``): on-device, already
installed, no model download, no GPU, and it comes up in Chinese on this
machine. Measured at ~40ms for a small image.

What OCR can and cannot do, plainly: it finds **text**, so a button labelled
"发送" becomes clickable by name, and an icon-only toolbar button does not
appear at all. It also misreads -- "保存" came back as "亻呆存" in testing, and
one label went missing entirely. Every element it produces is therefore marked
``source: "ocr"`` so nobody mistakes it for something the operating system
vouched for.
"""
from __future__ import annotations

import asyncio
import io
import re
from typing import Any, Optional

from .uia import Element

#: UI text is small; recognising a magnified copy is markedly more accurate,
#: and the cost is a resize.
UPSCALE = 2.0
#: Below this a "line" is a tick, a border artefact or a stray pixel.
MIN_EDGE = 6
MIN_TEXT = 1
MAX_LINES = 120
#: Windows refuses images larger than this on either edge.
MAX_DIMENSION = 10_000

CJK = r"　-〿一-鿿＀-￯"
#: The engine reports every CJK glyph as its own word, so the line text arrives
#: as "不 保 存". Spaces between CJK characters are an artefact, not content.
_CJK_GAP = re.compile(rf"(?<=[{CJK}])\s+(?=[{CJK}])")
_PUNCTUATION_ONLY = re.compile(rf"^[\s\W_]+$", re.UNICODE)


def available() -> bool:
    try:
        from winrt.windows.media.ocr import OcrEngine

        return OcrEngine.try_create_from_user_profile_languages() is not None
    except Exception:  # noqa: BLE001 - absent projection or no language pack
        return False


def language() -> str:
    try:
        from winrt.windows.media.ocr import OcrEngine

        engine = OcrEngine.try_create_from_user_profile_languages()
        return engine.recognizer_language.language_tag if engine else ""
    except Exception:  # noqa: BLE001
        return ""


def clean(text: str) -> str:
    return _CJK_GAP.sub("", text).strip()


async def _recognise(png: bytes) -> Any:
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("no OCR language is installed for this user profile")
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(png)
    await writer.store_async()
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    return await engine.recognize_async(bitmap)


def read_text(region: dict[str, int], window_title: str = "", window_hwnd: int = 0,
              upscale: float = UPSCALE,
              max_lines: int = MAX_LINES) -> tuple[list[Element], str]:
    """Text in a window (or a screen region), as elements in screen coordinates.

    Returns ``(elements, warning)``. When a window handle is given the window is
    asked to draw itself, so text is read from *its* pixels rather than from
    whatever happens to be covering it -- getting this wrong reported another
    application's contents as this window's controls.
    """
    from PIL import Image, ImageGrab

    left, top = int(region["x"]), int(region["y"])
    width, height = int(region["width"]), int(region["height"])
    if width <= 0 or height <= 0:
        return [], ""
    warning = ""
    image = None
    if window_hwnd:
        try:
            from . import capture

            image, source, warning = capture.window_pixels(window_hwnd)
            left, top = region["x"], region["y"]
            if source == "screen":
                # Nothing is covering it, so the screen is this window.
                pass
        except Exception as exc:  # noqa: BLE001 - fall back to the screen grab
            warning = f"window capture failed ({type(exc).__name__}); read from the screen instead"
            image = None
    if image is None:
        image = ImageGrab.grab(bbox=(left, top, left + width, top + height), all_screens=True)
    return read_image(image, (left, top), window_title, window_hwnd,
                      upscale=upscale, max_lines=max_lines), warning


def read_image(image, origin: tuple[int, int], window_title: str = "", window_hwnd: int = 0,
               upscale: float = UPSCALE, max_lines: int = MAX_LINES) -> list[Element]:
    """Text in an image already captured, as elements in screen coordinates.

    Separate from :func:`read_text` so that one capture can feed both this and
    the icon detector instead of grabbing the same window twice. The image is
    magnified here rather than by the caller, because the coordinates have to be
    divided by that same factor on the way out.
    """
    from PIL import Image

    left, top = origin
    scale = min(upscale, MAX_DIMENSION / max(image.width, image.height, 1))
    if scale > 1.01:
        image = image.resize((round(image.width * scale), round(image.height * scale)),
                             Image.Resampling.LANCZOS)
    else:
        scale = 1.0
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    result = asyncio.run(_recognise(buffer.getvalue()))
    found: list[Element] = []
    for index, line in enumerate(result.lines):
        text = clean(line.text)
        if len(text) < MIN_TEXT or _PUNCTUATION_ONLY.match(text):
            continue
        rects = [word.bounding_rect for word in line.words]
        if not rects:
            continue
        # The projection gives rectangles per word only, so a line's box is the
        # union of its words'.
        x0 = min(rect.x for rect in rects)
        y0 = min(rect.y for rect in rects)
        x1 = max(rect.x + rect.width for rect in rects)
        y1 = max(rect.y + rect.height for rect in rects)
        box = (
            left + round(x0 / scale), top + round(y0 / scale),
            left + round(x1 / scale), top + round(y1 / scale),
        )
        if box[2] - box[0] < MIN_EDGE or box[3] - box[1] < MIN_EDGE:
            continue
        found.append(Element(
            role="ocr_text",
            name=text,
            rect=box,
            window=window_title,
            window_hwnd=window_hwnd,
            index=index,
            source="ocr",
            # There is no provider behind this, so no pattern can be invoked:
            # a click at the text's own rectangle is all it can offer.
            actions=("click", "double_click", "right_click"),
        ))
        if len(found) >= max_lines:
            break
    return found


# -- fusion ------------------------------------------------------------------------------

#: UFO²'s constant: a vision candidate overlapping a known control by more than
#: this is the same thing seen twice (screenshot.py:1316-1344).
IOU_THRESHOLD = 0.1
#: Ours in addition. IoU alone misses the common case: the word "确定" inside a
#: large button is the *same target*, but its area is small enough that the
#: ratio stays under the threshold and the duplicate survives.
CONTAINMENT_THRESHOLD = 0.8
#: ...but a label lives inside its button, not inside "the window". Containment
#: in something this much larger says nothing about duplication -- applied
#: naively it deleted all 78 recognised lines of a chat window, because the
#: window itself was one of the known elements.
CONTAINER_RATIO = 20


def _area(rect: tuple[int, int, int, int]) -> int:
    return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])


def _intersection(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    return (max(0, min(a[2], b[2]) - max(a[0], b[0]))
            * max(0, min(a[3], b[3]) - max(a[1], b[1])))


def iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    overlap = _intersection(a, b)
    union = _area(a) + _area(b) - overlap
    return overlap / union if union else 0.0


def contained(inner: tuple[int, int, int, int], outer: tuple[int, int, int, int]) -> float:
    """How much of ``inner`` lies inside ``outer``."""
    area = _area(inner)
    return _intersection(inner, outer) / area if area else 0.0


#: How far below an icon its label may sit, as a multiple of the icon's own
#: height, and how much of its width must line up. A label under a button is
#: close and centred; a caption belonging to something else is not.
LABEL_GAP_RATIO = 1.2
LABEL_OVERLAP = 0.4
#: A label names a control; a sentence is prose that happens to be nearby.
LABEL_MAX_CHARS = 12


def label_icons(icons: list[Element], text: list[Element]) -> list[Element]:
    """Name an icon after the words printed under or beside it.

    Worth doing before any model is asked to guess, because this answer is
    exact and the guess is not: the caption model calls Edge's icon "the
    mozilla firefox logo", while the interface itself often prints the real
    name right there. Measured against the taskbar, where the tree knows the
    truth, generated captions were right about a quarter of the time.
    """
    import dataclasses

    named: list[Element] = []
    for icon in icons:
        left, top, right, bottom = icon.rect
        width, height = right - left, bottom - top
        best, distance = None, None
        for line in text:
            if not line.name.strip() or len(line.name.strip()) > LABEL_MAX_CHARS:
                continue
            other_left, other_top, other_right, other_bottom = line.rect
            overlap = min(right, other_right) - max(left, other_left)
            below = other_top - bottom
            beside = other_left - right
            if overlap >= LABEL_OVERLAP * width and 0 <= below <= LABEL_GAP_RATIO * height:
                gap = below
            elif (0 <= beside <= height
                  and min(bottom, other_bottom) - max(top, other_top) >= 0.5 * height):
                gap = beside
            else:
                continue
            if distance is None or gap < distance:
                best, distance = line, gap
        if best is not None:
            named.append(dataclasses.replace(icon, name=best.name.strip()))
        else:
            named.append(icon)
    return named


def fuse(known: list[Element], candidates: list[Element],
         threshold: float = IOU_THRESHOLD) -> list[Element]:
    """Known controls win; text that describes one of them is dropped.

    The accessibility tree is authoritative: it names a control the operating
    system knows about, with actions it really supports. Recognised text is a
    guess about pixels. So anything the tree already covers is removed from the
    guesses, and what remains is appended -- never interleaved, so the model
    sees the trustworthy elements first.
    """
    kept: list[Element] = []
    for candidate in candidates:
        area = _area(candidate.rect)
        duplicate = any(
            iou(candidate.rect, element.rect) > threshold
            or (_area(element.rect) <= CONTAINER_RATIO * area
                and contained(candidate.rect, element.rect) > CONTAINMENT_THRESHOLD)
            for element in known
        )
        if not duplicate:
            kept.append(candidate)
    return list(known) + kept


def summarise(region: Optional[dict[str, int]], count: int) -> str:
    where = f" in {region['width']}x{region['height']}" if region else ""
    return (
        f"{count} text element(s) recognised{where} by Windows OCR because this window exposes "
        "no accessibility tree. Names are read from pixels, so they can be wrong or missing, and "
        "icon-only controls do not appear at all; each one can only be clicked."
    )
