"""The ``edit`` capability: ``edit_file`` (exact text replacement) and ``patch_file`` (unified diff).

Both tools are all-or-nothing: every change is computed and checked in memory
first, and nothing is written unless all of it applies. Text is read and
written as UTF-8; a file's byte-order mark and its line-ending style (LF or
CRLF) survive an edit, so a patch written with LF line endings applies to a
CRLF file and leaves it CRLF. Every path goes through ``Policy.resolve_path``,
so ``allowed_roots`` bounds these tools exactly as it bounds the ``file_*`` ones.

``patch_file`` takes the unified diff format that ``git diff`` and ``diff -u``
produce: ``--- old`` / ``+++ new`` headers, ``@@ -a,b +c,d @@`` hunks, ``/dev/null``
for created and deleted files, git's ``rename from`` / ``rename to`` lines and
``\\ No newline at end of file`` markers. A hunk applies where its old lines
match; when the file has moved on since the diff was made, the nearest match to
the stated line number wins, and a match that ignores trailing whitespace is
accepted as a last resort (reported per hunk as ``whitespace_fuzz``).
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .policy import Policy, PolicyError

BOM = b"\xef\xbb\xbf"
DEV_NULL = "/dev/null"
MAX_EDIT_CHARS = 10_000_000
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
#: git's extended header lines carry nothing this applier needs (modes, blob ids).
_IGNORED_HEADER = re.compile(
    r"^(diff |index |old mode |new mode |deleted file mode |new file mode |similarity index |"
    r"dissimilarity index |copy from |copy to |Binary files |GIT binary patch|Only in )"
)


class EditError(ValueError):
    pass


# -- text files that remember their encoding details -------------------------
@dataclass
class TextFile:
    """A file's lines without terminators, plus what is needed to write it back the same way."""

    lines: list[str]
    final_newline: bool = True
    crlf: bool = False
    bom: bool = False
    #: "utf-8", or "gb18030" for a file that is not valid UTF-8 (GBK files on
    #: Chinese Windows); the file is written back in the same encoding.
    encoding: str = "utf-8"

    @classmethod
    def load(cls, path: Path) -> "TextFile":
        if not path.is_file():
            raise EditError(f"not a file: {path}")
        data = path.read_bytes()
        bom = data.startswith(BOM)
        if bom:
            data = data[len(BOM):]
        if b"\0" in data[:8192]:
            raise EditError(f"{path} looks binary; these tools edit text files only")
        encoding = "utf-8"
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            if bom:
                raise EditError(f"{path} is not valid UTF-8 ({exc.reason} at byte {exc.start})") from exc
            try:
                text, encoding = data.decode("gb18030"), "gb18030"
            except UnicodeDecodeError:
                raise EditError(f"{path} is neither UTF-8 nor GB18030 ({exc.reason} at byte {exc.start})") from exc
        crlf = "\r\n" in text
        text = text.replace("\r\n", "\n")
        return cls.from_text(text, crlf=crlf, bom=bom, encoding=encoding)

    @classmethod
    def from_text(cls, text: str, *, crlf: bool = False, bom: bool = False,
                  encoding: str = "utf-8") -> "TextFile":
        if not text:
            return cls([], final_newline=False, crlf=crlf, bom=bom, encoding=encoding)
        final_newline = text.endswith("\n")
        lines = text.split("\n")
        if final_newline:
            lines.pop()
        return cls(lines, final_newline=final_newline, crlf=crlf, bom=bom, encoding=encoding)

    def text(self) -> str:
        """The content with LF line endings, as the edit logic sees it."""
        body = "\n".join(self.lines)
        return body + "\n" if self.lines and self.final_newline else body

    def to_bytes(self) -> bytes:
        body = self.text()
        if self.crlf:
            body = body.replace("\n", "\r\n")
        try:
            data = body.encode(self.encoding)
        except UnicodeEncodeError as exc:
            raise EditError(f"the new text has characters that {self.encoding} cannot store: {exc.object[exc.start:exc.end]!r}") from exc
        return (BOM if self.bom else b"") + data


def _write_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` in one step, so a crash never leaves half a file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        if path.exists():
            try:
                os.chmod(temp, path.stat().st_mode)
            except OSError:
                pass
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


# -- edit_file ----------------------------------------------------------------
def edit_file(policy: Policy, path: str, old_string: str, new_string: str,
              replace_all: bool = False) -> dict[str, Any]:
    """Replace ``old_string`` with ``new_string`` in one file.

    ``old_string`` must occur exactly once unless ``replace_all`` is set. Line
    endings in both strings are normalised to the file's own style.
    """
    if not old_string:
        raise EditError("old_string must not be empty; use file_write to create a file")
    if old_string == new_string:
        raise EditError("old_string and new_string are identical; nothing to change")
    if len(new_string) > MAX_EDIT_CHARS:
        raise EditError(f"new_string is larger than {MAX_EDIT_CHARS} characters")
    target = policy.resolve_path(path)
    document = TextFile.load(target)
    text = document.text()
    old = old_string.replace("\r\n", "\n")
    new = new_string.replace("\r\n", "\n")
    count = text.count(old)
    if count == 0:
        hint = ""
        stripped = "\n".join(line.rstrip() for line in old.split("\n"))
        if stripped and stripped in "\n".join(line.rstrip() for line in text.split("\n")):
            hint = " (it does occur when trailing whitespace is ignored; copy the exact text from file_read)"
        raise EditError(f"old_string was not found in {target}{hint}")
    if count > 1 and not replace_all:
        raise EditError(f"old_string occurs {count} times in {target}; include more surrounding "
                        "text to make it unique, or set replace_all=true")
    first_line = text[:text.index(old)].count("\n") + 1
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    result = TextFile.from_text(updated, crlf=document.crlf, bom=document.bom, encoding=document.encoding)
    _write_atomic(target, result.to_bytes())
    payload: dict[str, Any] = {"status": "ok", "path": str(target), "replacements": count if replace_all else 1,
                               "first_line": first_line,
                               "snippet": _snippet(updated, first_line, max(1, len(new.split("\n")) - new.endswith("\n")))}
    if document.encoding != "utf-8":
        payload["encoding"] = document.encoding
    return payload


SNIPPET_CONTEXT = 3
SNIPPET_MAX_LINES = 30


def _snippet(text: str, first_line: int, changed_lines: int) -> str:
    """Numbered lines around an edit, so the caller can check it without reading the file again."""
    lines = text.split("\n")
    start = max(1, first_line - SNIPPET_CONTEXT)
    end = min(len(lines), first_line + changed_lines - 1 + SNIPPET_CONTEXT, start + SNIPPET_MAX_LINES - 1)
    shown = [f"{number:>6}\t{lines[number - 1]}" for number in range(start, end + 1)]
    if text.endswith("\n") and end == len(lines) and lines[-1] == "":
        shown.pop()
    return "\n".join(shown)


# -- patch_file: parsing --------------------------------------------------------
@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    #: (" " | "-" | "+", text) in diff order.
    lines: list[tuple[str, str]] = field(default_factory=list)
    old_missing_newline: bool = False
    new_missing_newline: bool = False

    @property
    def old_lines(self) -> list[str]:
        return [text for kind, text in self.lines if kind != "+"]

    @property
    def new_lines(self) -> list[str]:
        return [text for kind, text in self.lines if kind != "-"]


@dataclass
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)


def _header_path(raw: str) -> str | None:
    """The path in a ``---``/``+++`` line, without git's a/ b/ prefix or a trailing timestamp."""
    value = raw.split("\t", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        # git quotes unusual paths C-style, with non-ASCII bytes as octal escapes.
        try:
            value = value[1:-1].encode("ascii").decode("unicode_escape").encode("latin-1").decode("utf-8")
        except (UnicodeError, ValueError):
            value = value[1:-1]
    if value == DEV_NULL:
        return None
    if value[:2] in ("a/", "b/"):
        value = value[2:]
    if not value:
        raise EditError(f"empty path in diff header: {raw!r}")
    return value


def parse_unified_diff(patch: str) -> list[FilePatch]:
    lines = patch.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    files: list[FilePatch] = []
    rename_from: str | None = None
    rename_to: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("rename from "):
            rename_from = line[len("rename from "):].strip()
            index += 1
            continue
        if line.startswith("rename to "):
            rename_to = line[len("rename to "):].strip()
            index += 1
            # A pure rename has no ---/+++ lines; record it now unless headers follow.
            if not (index < len(lines) and lines[index].startswith("--- ")) and rename_from:
                files.append(FilePatch(rename_from, rename_to))
                rename_from = rename_to = None
            continue
        if line.startswith("--- "):
            if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
                raise EditError(f"line {index + 1}: '---' header is not followed by a '+++' header")
            current = FilePatch(_header_path(line[4:]), _header_path(lines[index + 1][4:]))
            if rename_from and rename_to:
                current.old_path, current.new_path = rename_from, rename_to
            rename_from = rename_to = None
            files.append(current)
            index += 2
            while index < len(lines) and lines[index].startswith("@@"):
                index = _parse_hunk(lines, index, current)
            continue
        if line.startswith("@@"):
            raise EditError(f"line {index + 1}: hunk without a '---'/'+++' file header")
        if line.strip() == "" or _IGNORED_HEADER.match(line):
            if line.startswith(("Binary files ", "GIT binary patch")):
                raise EditError("binary patches are not supported")
            index += 1
            continue
        # Free text before the first diff (a commit message, say) is tolerated.
        if not files:
            index += 1
            continue
        raise EditError(f"line {index + 1}: unexpected text outside a hunk: {line[:80]!r}")
    if not files:
        raise EditError("no file changes found; patch_file expects a unified diff with ---/+++ headers")
    return files


def _parse_hunk(lines: list[str], index: int, target: FilePatch) -> int:
    match = _HUNK_HEADER.match(lines[index])
    if not match:
        raise EditError(f"line {index + 1}: malformed hunk header {lines[index]!r}")
    old_start, old_count, new_start, new_count = (
        int(match.group(1)), int(match.group(2) or 1), int(match.group(3)), int(match.group(4) or 1))
    hunk = Hunk(old_start, old_count, new_start, new_count)
    remaining_old, remaining_new = old_count, new_count
    index += 1
    last_kind = ""
    while index < len(lines) and (remaining_old > 0 or remaining_new > 0):
        line = lines[index]
        if line.startswith("\\"):
            _mark_missing_newline(hunk, last_kind)
            index += 1
            continue
        kind, text = (line[0], line[1:]) if line else (" ", "")  # editors strip a lone space
        if kind == " ":
            remaining_old -= 1
            remaining_new -= 1
        elif kind == "-":
            remaining_old -= 1
        elif kind == "+":
            remaining_new -= 1
        else:
            raise EditError(f"line {index + 1}: hunk ended early; expected {remaining_old} more old and "
                            f"{remaining_new} more new lines (check the @@ counts)")
        if remaining_old < 0 or remaining_new < 0:
            raise EditError(f"line {index + 1}: hunk has more lines than its @@ header declares")
        hunk.lines.append((kind, text))
        last_kind = kind
        index += 1
    if remaining_old > 0 or remaining_new > 0:
        raise EditError(f"hunk at line {old_start} is truncated: {remaining_old} old and "
                        f"{remaining_new} new lines missing")
    if index < len(lines) and lines[index].startswith("\\"):
        _mark_missing_newline(hunk, last_kind)
        index += 1
    target.hunks.append(hunk)
    return index


def _mark_missing_newline(hunk: Hunk, kind: str) -> None:
    if kind in ("-", " "):
        hunk.old_missing_newline = True
    if kind in ("+", " "):
        hunk.new_missing_newline = True


# -- patch_file: applying -------------------------------------------------------
def _find(lines: list[str], block: list[str], start: int, floor: int) -> tuple[int, bool] | None:
    """Where ``block`` occurs at or after ``floor``, nearest to ``start``; exact matches first."""
    if not block:
        return max(floor, min(start, len(lines))), False
    last = len(lines) - len(block)
    if last < floor:
        return None
    order = sorted(range(floor, last + 1), key=lambda position: (abs(position - start), position))
    for loose in (False, True):
        wanted = [line.rstrip() for line in block] if loose else block
        for position in order:
            window = lines[position:position + len(block)]
            if (([line.rstrip() for line in window] if loose else window) == wanted):
                return position, loose
    return None


def apply_hunks(document: TextFile, hunks: list[Hunk], label: str) -> tuple[TextFile, list[dict[str, Any]]]:
    lines = list(document.lines)
    final_newline = document.final_newline
    report: list[dict[str, Any]] = []
    delta = 0
    floor = 0
    for number, hunk in enumerate(hunks, 1):
        old_block = hunk.old_lines
        # A zero-length old side means "insert after line old_start".
        expected = (hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1) + delta
        found = _find(lines, old_block, max(expected, 0), floor)
        if found is None:
            preview = "\n".join(old_block[:3])
            raise EditError(f"{label}: hunk {number} (@@ -{hunk.old_start},{hunk.old_count}) does not match the "
                            f"file; expected lines starting with:\n{preview}")
        position, loose = found
        new_block = hunk.new_lines
        touches_end = position + len(old_block) == len(lines)
        lines[position:position + len(old_block)] = new_block
        if touches_end:
            if hunk.new_missing_newline:
                final_newline = False
            elif hunk.old_missing_newline or not lines:
                final_newline = bool(lines)
        delta += len(new_block) - len(old_block)
        floor = position + len(new_block)
        entry: dict[str, Any] = {"hunk": number, "line": position + 1}
        if position != max(expected, 0) and old_block:
            entry["offset"] = position - expected
        if loose:
            entry["whitespace_fuzz"] = True
        report.append(entry)
    return TextFile(lines, final_newline=final_newline if lines else False,
                    crlf=document.crlf, bom=document.bom, encoding=document.encoding), report


def patch_file(policy: Policy, patch: str, workdir: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Apply a unified diff to one or more files, all or nothing."""
    if not patch or not patch.strip():
        raise EditError("patch is empty")
    base = policy.resolve_cwd(workdir)

    def resolve(relative: str) -> Path:
        candidate = Path(relative).expanduser()
        return policy.resolve_path(str(candidate if candidate.is_absolute() else base / candidate))

    planned: list[tuple[str, Path, bytes | None, Path | None]] = []  # action, target, data, removed
    summary: list[dict[str, Any]] = []
    claimed: set[Path] = set()
    for item in parse_unified_diff(patch):
        old = resolve(item.old_path) if item.old_path else None
        new = resolve(item.new_path) if item.new_path else None
        for path in {p for p in (old, new) if p is not None}:
            if path in claimed:
                raise EditError(f"{path} appears more than once in the patch")
            claimed.add(path)
        if old is None and new is None:
            raise EditError("a file section has /dev/null on both sides")
        if old is None:
            assert new is not None
            if new.exists():
                raise EditError(f"cannot create {new}: it already exists")
            result, hunks = apply_hunks(TextFile([], final_newline=False), item.hunks, str(new))
            result.final_newline = bool(result.lines) and not any(h.new_missing_newline for h in item.hunks[-1:])
            planned.append(("created", new, result.to_bytes(), None))
            summary.append({"action": "created", "path": str(new), "hunks": hunks})
            continue
        document = TextFile.load(old)
        result, hunks = apply_hunks(document, item.hunks, str(old))
        if new is None:
            if result.lines:
                raise EditError(f"{old}: the patch deletes the file but {len(result.lines)} line(s) would remain; "
                                "the file differs from the one the diff was made against")
            planned.append(("deleted", old, None, None))
            summary.append({"action": "deleted", "path": str(old), "hunks": hunks})
        elif new != old:
            if new.exists():
                raise EditError(f"cannot rename {old} to {new}: the destination already exists")
            planned.append(("renamed", new, result.to_bytes(), old))
            summary.append({"action": "renamed", "from": str(old), "path": str(new), "hunks": hunks})
        else:
            planned.append(("modified", old, result.to_bytes(), None))
            summary.append({"action": "modified", "path": str(old), "hunks": hunks})

    if not dry_run:
        for action, target, data, removed in planned:
            if data is None:
                target.unlink()
                continue
            _write_atomic(target, data)
            if removed is not None:
                removed.unlink()
    return {"status": "ok", "dry_run": dry_run, "workdir": str(base), "files": summary}


__all__ = ["EditError", "PolicyError", "TextFile", "edit_file", "parse_unified_diff", "patch_file"]
