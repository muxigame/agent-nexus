"""OmniParser: the icons that have neither a name in the tree nor any text.

UI Automation names most things on this machine, and Windows' own OCR covers
the text in windows that expose no tree. What is left is the picture-only
control -- the gear, the paperclip, the send arrow in a chat client's toolbar.
That is what this finds: Microsoft's OmniParser v2, a YOLO detector fine-tuned
on interface icons plus a Florence-2 caption model that gives each one a name.

**Nothing here is imported or loaded until asked.** Importing torch costs
seconds and the weights cost video memory, and this machine's 8GB is shared
with a game engine and a browser, so the model is loaded by an explicit tool
call and unloaded by another. While it is unloaded, reading falls back to OCR,
which is how it worked before this file existed.

Licensing, because it matters if any of this is ever distributed: the
OmniParser repository is MIT, but the icon detector is a YOLOv8 derivative and
ultralytics is AGPL-3.0.
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .uia import Element

#: Weights, and the processor that the fine-tuned caption model does not ship.
MODEL_REPO = "microsoft/OmniParser-v2.0"
#: The caption weights are fine-tuned from this, and they are in its original
#: layout, so its original processor is the matching one.
PROCESSOR_REPO = "microsoft/Florence-2-base-ft"
WEIGHT_FILES = (
    "icon_detect/model.pt",
    "icon_caption/config.json",
    "icon_caption/generation_config.json",
    "icon_caption/model.safetensors",
)

#: OmniParser's own defaults (ufo config: BOX_THRESHOLD 0.05, IOU_THRESHOLD 0.1,
#: IMGSZ 640). A low box threshold is deliberate: an icon missed is a control
#: the model cannot reach, while a spurious box is merely ignored.
BOX_THRESHOLD = 0.05
IOU_THRESHOLD = 0.1
IMAGE_SIZE = 640
MAX_ICONS = 60
#: An icon is small in *both* directions, so a box that covers this much of the
#: window is a panel the detector drew by mistake. Measured as area on purpose:
#: the taskbar is 1920x48, where every real icon is taller than half the window
#: and a per-edge rule throws all of them away.
MAX_ICON_AREA_FRACTION = 0.25
#: Below this many pixels on its short side, an icon does not survive being
#: resized to the detector's square input, so a thin window is fed at its own
#: size instead. The taskbar again: scaled to 640 wide it is 16px tall, its
#: icons become 13px, and the detector finds 23 of 30.
MIN_SHORT_EDGE = 64
CAPTION_TASK = "<CAPTION>"
CAPTION_MAX_TOKENS = 24
#: Sixteen crops at a time. Not a guess and not worth raising: on this card the
#: same thirty icons take 3.3s at 16, 43.9s at 32 and 26.1s at 64, because the
#: 8GB is shared with a game engine and a browser and a larger batch spills out
#: of it.
CAPTION_BATCH = 16
#: How many names to remember. An icon's name is a function of its pixels, so a
#: window read twice in a row costs nothing the second time -- which is the
#: difference between a click reporting its result in 0.2s and in 3.5s, because
#: the report is itself a read.
CAPTION_CACHE_SIZE = 1024


@dataclass
class VisionState:
    loaded: bool = False
    device: str = ""
    dtype: str = ""
    loaded_at: float = 0.0
    load_seconds: float = 0.0
    detections: int = 0
    calls: int = 0
    last_error: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        out: dict[str, Any] = {"loaded": self.loaded}
        if self.loaded:
            out |= {
                "device": self.device,
                "dtype": self.dtype,
                "loaded_for_s": round(time.time() - self.loaded_at, 1),
                "load_seconds": self.load_seconds,
                "calls": self.calls,
                "icons_found": self.detections,
            }
        if self.last_error:
            out["last_error"] = self.last_error
        if self.notes:
            out["notes"] = list(self.notes)
        return out | (extra or {})


class VisionUnavailable(RuntimeError):
    """Raised when the vision stack cannot be used, with the reason."""


class IconFinder:
    """OmniParser, loaded on request and unloaded on request."""

    def __init__(self, directory: Path, device: str = "auto", caption: bool = True):
        self.directory = Path(directory)
        self.requested_device = device
        self.caption = caption
        self.state = VisionState()
        self._lock = threading.RLock()
        self._detector = None
        self._captioner = None
        self._processor = None
        self._torch = None
        self._names: "OrderedDict[bytes, str]" = OrderedDict()

    # -- availability --------------------------------------------------------------

    def packages_installed(self) -> tuple[bool, str]:
        import importlib.util

        missing = [
            name for name in ("torch", "ultralytics", "transformers")
            if importlib.util.find_spec(name) is None
        ]
        if missing:
            return False, ("the vision extra is not installed (missing "
                           f"{', '.join(missing)}); run: uv sync --extra vision")
        return True, ""

    def weights_present(self) -> tuple[bool, list[str]]:
        missing = [name for name in WEIGHT_FILES if not (self.directory / name).is_file()]
        return not missing, missing

    def status(self) -> dict[str, Any]:
        installed, why = self.packages_installed()
        present, missing = self.weights_present()
        return self.state.to_dict({
            "packages_installed": installed,
            "weights_present": present,
            "model_dir": str(self.directory),
            **({"packages_note": why} if why else {}),
            **({"missing_weights": missing} if missing else {}),
        })

    # -- weights -------------------------------------------------------------------

    def fetch_weights(self) -> dict[str, Any]:
        """Download the detector, the caption weights and the processor."""
        installed, why = self.packages_installed()
        if not installed:
            raise VisionUnavailable(why)
        from huggingface_hub import snapshot_download

        self.directory.mkdir(parents=True, exist_ok=True)
        started = time.time()
        snapshot_download(MODEL_REPO, local_dir=str(self.directory),
                          allow_patterns=["icon_detect/*", "icon_caption/*"])
        # The fine-tuned caption weights ship without a processor, so the base
        # model's tokenizer and image processor come from upstream -- but only
        # those: pulling the whole repository would fetch half a gigabyte of
        # base-model weights that the fine-tuned ones replace.
        processor_dir = self.directory / "florence_processor"
        # ``*.py`` is Florence-2's own implementation. The caption weights point
        # at it (auto_map), transformers cannot load them without it, and it is
        # how OmniParser itself loads them -- so it is fetched deliberately
        # rather than downloaded behind our back on first use.
        snapshot_download(PROCESSOR_REPO, local_dir=str(processor_dir), allow_patterns=[
            "*.json", "*.txt", "*.py", "*.model", "tokenizer*", "vocab*", "merges*",
        ])
        present, missing = self.weights_present()
        return {
            "model_dir": str(self.directory),
            "seconds": round(time.time() - started, 1),
            "complete": present,
            **({"still_missing": missing} if missing else {}),
        }

    # -- lifecycle -----------------------------------------------------------------

    def load(self) -> dict[str, Any]:
        with self._lock:
            if self.state.loaded:
                return self.status()
            installed, why = self.packages_installed()
            if not installed:
                raise VisionUnavailable(why)
            present, missing = self.weights_present()
            if not present:
                raise VisionUnavailable(
                    f"the weights are not downloaded yet (missing {', '.join(missing)}); "
                    "call vision with action='fetch' first"
                )
            started = time.time()
            notes: list[str] = []
            import torch  # noqa: PLC0415 - deliberately late: seconds of import time
            from ultralytics import YOLO

            self._torch = torch
            device = self.requested_device
            if device in ("", "auto"):
                device = "cuda" if torch.cuda.is_available() else "cpu"
            if device.startswith("cuda") and not torch.cuda.is_available():
                notes.append("CUDA was asked for but is not available; running on the CPU")
                device = "cpu"
            dtype = torch.float16 if device.startswith("cuda") else torch.float32

            self._detector = YOLO(str(self.directory / "icon_detect" / "model.pt"))
            self._detector.to(device)

            if self.caption:
                try:
                    from transformers import AutoModelForCausalLM, AutoProcessor

                    processor_dir = self.directory / "florence_processor"
                    source = str(processor_dir) if processor_dir.is_dir() else PROCESSOR_REPO
                    caption_dir = str(self.directory / "icon_caption")
                    self._processor = AutoProcessor.from_pretrained(
                        source, trust_remote_code=True)
                    # ``attn_implementation="eager"`` is not a preference, it is
                    # the only thing that loads. Florence-2's remote code
                    # exposes _supports_sdpa as a property forwarding to
                    # self.language_model, and transformers 4.5x picks the
                    # attention backend inside PreTrainedModel.__init__ -- which
                    # runs before the subclass has assigned language_model, so
                    # asking for SDPA dies with a confusing
                    # "no attribute '_supports_sdpa'". Eager skips that branch.
                    self._captioner = AutoModelForCausalLM.from_pretrained(
                        caption_dir, dtype=dtype, attn_implementation="eager",
                        trust_remote_code=True,
                    ).to(device).eval()
                except Exception as exc:  # noqa: BLE001 - boxes without names still help
                    notes.append(f"icons will have no names: the caption model failed to load "
                                 f"({type(exc).__name__}: {exc})")
                    self._captioner = self._processor = None

            self.state = VisionState(
                loaded=True, device=device, dtype=str(dtype).replace("torch.", ""),
                loaded_at=time.time(), load_seconds=round(time.time() - started, 1),
                notes=notes,
            )
            return self.status()

    def unload(self) -> dict[str, Any]:
        with self._lock:
            was = self.state.loaded
            self._detector = self._captioner = self._processor = None
            self._names.clear()
            if self._torch is not None:
                import gc

                gc.collect()
                # Dropping the references is not enough on CUDA: the caching
                # allocator holds the memory until it is told to let go, and
                # "unload" has to actually free the card.
                if self._torch.cuda.is_available():
                    self._torch.cuda.empty_cache()
                    self._torch.cuda.ipc_collect()
            self.state = VisionState(loaded=False)
            return {"was_loaded": was, **self.status()}

    # -- detection -----------------------------------------------------------------

    def detect(self, image, origin: tuple[int, int], window_title: str = "",
               window_hwnd: int = 0, max_icons: int = MAX_ICONS,
               box_threshold: float = BOX_THRESHOLD,
               iou_threshold: float = IOU_THRESHOLD) -> list[Element]:
        """Icons in an image, as elements in absolute screen coordinates."""
        with self._lock:
            if not self.state.loaded:
                raise VisionUnavailable("the vision model is not loaded")
            started = time.time()
            boxes = self._boxes(image, box_threshold, iou_threshold, max_icons)
            names = self._captions(image, boxes) if self._captioner else [""] * len(boxes)
            left, top = origin
            found: list[Element] = []
            for index, ((x0, y0, x1, y1), name) in enumerate(zip(boxes, names)):
                found.append(Element(
                    role="icon",
                    name=name or f"未命名图标 {index + 1}",
                    rect=(left + int(x0), top + int(y0), left + int(x1), top + int(y1)),
                    window=window_title,
                    window_hwnd=window_hwnd,
                    index=index,
                    source="vision",
                    actions=("click", "double_click", "right_click"),
                ))
            self.state.calls += 1
            self.state.detections += len(found)
            self.state.last_error = ""
            self._last_seconds = round(time.time() - started, 2)
            return found

    def _boxes(self, image, box_threshold: float, iou_threshold: float,
               limit: int) -> list[tuple[float, float, float, float]]:
        results = self._detector.predict(
            source=image, conf=box_threshold, iou=iou_threshold,
            imgsz=_input_size(image.size), verbose=False, device=self.state.device,
        )
        width, height = image.size
        limit_area = width * height * MAX_ICON_AREA_FRACTION
        boxes: list[tuple[float, float, float, float]] = []
        for result in results:
            for row in result.boxes.xyxy.tolist():
                x0, y0, x1, y1 = row
                if (x1 - x0) * (y1 - y0) > limit_area:
                    continue  # a panel, not an icon
                if x1 - x0 < 4 or y1 - y0 < 4:
                    continue
                boxes.append((x0, y0, x1, y1))
        boxes.sort(key=lambda b: (b[1], b[0]))  # reading order: top to bottom
        return boxes[:limit]

    def _captions(self, image, boxes) -> list[str]:
        """Name each icon. Failures degrade to empty names, never to an error."""
        if not boxes:
            return []
        torch = self._torch
        crops = [image.crop(tuple(int(v) for v in box)).convert("RGB") for box in boxes]
        # The name of an icon is a function of its picture, so the picture is
        # the key. Anything that changes the pixels -- a hover highlight, a
        # badge appearing -- misses and is named again, which is right.
        keys = [hashlib.blake2b(crop.tobytes(), digest_size=16).digest() for crop in crops]
        names: list[str] = [""] * len(boxes)
        todo = [index for index, key in enumerate(keys) if key not in self._names]
        for index, key in enumerate(keys):
            if key in self._names:
                names[index] = self._names[key]
                self._names.move_to_end(key)
        try:
            for start in range(0, len(todo), CAPTION_BATCH):
                batch = todo[start:start + CAPTION_BATCH]
                inputs = self._processor(
                    text=[CAPTION_TASK] * len(batch), images=[crops[i] for i in batch],
                    return_tensors="pt",
                ).to(self.state.device, torch.float16 if "cuda" in self.state.device else torch.float32)
                with torch.inference_mode():
                    generated = self._captioner.generate(
                        input_ids=inputs["input_ids"], pixel_values=inputs["pixel_values"],
                        max_new_tokens=CAPTION_MAX_TOKENS, num_beams=1, do_sample=False,
                        # The remote code reads the cache as a tuple of tuples,
                        # the shape transformers stopped using; with a cache it
                        # crashes on past_key_values[0][0] before it emits a
                        # token. A 0.27B decoder writing two dozen tokens does
                        # not really need one.
                        use_cache=False,
                    )
                decoded = self._processor.batch_decode(generated, skip_special_tokens=True)
                for index, text in zip(batch, decoded):
                    names[index] = _tidy(text)
                    self._names[keys[index]] = names[index]
            while len(self._names) > CAPTION_CACHE_SIZE:
                self._names.popitem(last=False)
        except Exception as exc:  # noqa: BLE001
            self.state.last_error = f"captioning failed: {type(exc).__name__}: {exc}"
        return names


def _input_size(size: tuple[int, int]) -> Any:
    """What to resize a window's picture to before detection.

    Normally the detector's own square input, which ultralytics letterboxes.
    For a window far wider than it is tall -- the taskbar, a toolbar strip --
    that resize shrinks the icons until they are missed, so such a window is
    fed at its own size (rounded up to the stride the network needs). It is
    not more expensive: 1920x64 is fewer pixels than 640x640.
    """
    width, height = size
    long_edge, short_edge = max(size), min(size)
    if long_edge <= IMAGE_SIZE or short_edge * IMAGE_SIZE / long_edge >= MIN_SHORT_EDGE:
        return IMAGE_SIZE
    return (_stride(height), _stride(width))


def _stride(value: int, stride: int = 32) -> int:
    return max(stride, -(-value // stride) * stride)


#: What the caption model says instead of describing the picture. Kept out of
#: element names: "unanswerable" as the name of a button is worse than no name,
#: because it reads like a label the interface actually carries.
NON_ANSWERS = frozenset({"unanswerable", "unknown", "n/a", "none", "no caption",
                         "image", "an image", "the image", "icon", "an icon"})


def _tidy(text: str) -> str:
    cleaned = text.replace(CAPTION_TASK, "").strip().strip(".").strip()
    if cleaned.casefold() in NON_ANSWERS:
        return ""
    return cleaned[:60]
