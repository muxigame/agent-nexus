"""System-prompt fragments with ``$variables`` (docs/agent-chat.md §6, §13).

The store is ``<root>/.anx/agents/prompts.json``::

    {"version": 1,
     "fragments": [{"id", "title", "text", "enabled", "builtin", "scope": "all|direct|gateway", "kinds": [...]}],
     "presets":   [{"id", "name", "order": [fragment ids], "enabled": {fragment id: bool}}]}

* Built-in fragments (:data:`BUILTIN_FRAGMENTS`) are always present. They can be edited (``modified`` is computed by
  comparing with the shipped text, so an untouched built-in follows upgrades) and :meth:`Prompts.reset` restores one.
  They cannot be deleted: a save that leaves one out keeps the stored copy.
* A preset orders the fragments and may override each fragment's own ``enabled``; fragments a preset does not list
  follow in document order. A fragment applies to a run when it is on, its ``scope`` is ``all`` or the run's mode,
  and ``kinds`` is empty or names the run's CLI.
* :func:`expand` substitutes ``$name`` / ``${name}`` (``$$`` is a literal ``$``). For ``$name`` the longest known
  variable name that starts the identifier wins (``$device_id`` is not ``$device`` + ``_id``; ``$work`` is unknown,
  not a cut-off ``$workdir``). Unknown variables are kept as written and reported.
* :func:`system_prompt_args` / :func:`prefix_message` deliver the rendered text to a CLI (§6): Claude
  ``--append-system-prompt``, Codex ``-c developer_instructions=<TOML string>``, anything else as a prefix of the first
  message. (``adapters.py`` carries the same rules for live runs; these helpers are the reference and serve previews.)

Every public method returns plain dicts; :meth:`Prompts.handlers` wraps the three RPC ops so they never raise.
"""
from __future__ import annotations

import copy
import json
import os
import platform
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

MAX_FRAGMENTS = 100
MAX_PRESETS = 30
TITLE_MAX = 60
NAME_MAX = 40
TEXT_MAX = 20000
SCOPES = ("all", "direct", "gateway")
MODES = ("direct", "gateway")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_KIND = re.compile(r"[a-z][a-z0-9_-]{0,31}")

KIND_LABELS = {"claude": "Claude Code", "codex": "Codex", "dsh": "DeepSeek Harness", "gemini": "Gemini CLI"}

#: (name, description) of every variable, in the order the editor offers them.
VARIABLES: tuple[tuple[str, str], ...] = (
    ("device", "设备名称"),
    ("device_id", "设备 ID"),
    ("os", "操作系统"),
    ("workdir", "工作目录"),
    ("tmpdir", "本次对话的临时目录"),
    ("session", "会话名"),
    ("address", "会话的邮箱地址"),
    ("agent", "CLI 名称"),
    ("date", "今天的日期"),
    ("time", "当前时间"),
    ("owner", "主人的称呼"),
    ("gateway", "网关地址"),
    ("handle", "网关工作区句柄（仅经网关）"),
    ("context_device", "要操作的设备（经网关）"),
    ("context_workdir", "要操作的目录（经网关）"),
    ("target_device", "工具要操作的设备（经网关时是目标设备，否则是运行设备）"),
    ("target_workdir", "工具要操作的目录"),
    ("mcp", "本次使用的 Agent Nexus MCP 连接器名"),
    ("mcp_prefix", "该连接器的工具名前缀"),
    ("mcp_state", "连接器状态（local / gateway_config / gateway_account / both / none / unknown）"),
    ("mcp_hint", "已接入的 Agent Nexus MCP 和选好的工作区（一段说明）"),
)
VARIABLE_NAMES = tuple(name for name, _ in VARIABLES)
_DESCRIPTIONS = dict(VARIABLES)

# --------------------------------------------------------------------------------------------- built-ins

BUILTIN_FRAGMENTS: tuple[dict, ...] = (
    {"id": "identity", "title": "身份与环境", "scope": "all", "kinds": [], "text": (
        "你是 $agent，通过 Agent Nexus 为$owner工作。\n"
        "- 运行设备：$device（$os）。现在是 $date $time。\n"
        "- 本次会话名为 $session，邮箱地址 $address。\n"
        "- 用户在 Agent Nexus 控制台的对话窗口里看你的输出。回复用用户的语言（默认简体中文），"
        "先说结论和做了什么，再说细节；不要复述用户的话。")},
    {"id": "workdir", "title": "工作目录与临时目录", "scope": "direct", "kinds": [], "text": (
        "- 工作目录是 $workdir。项目文件在这里读写；目录之外的东西不要改，除非用户明确要求。\n"
        "- 临时文件（草稿、日志、下载、构建中间产物、一次性脚本）放到 $tmpdir，不要写进工作目录或系统临时目录。"
        "这个目录在对话结束 7 天后会被自动清理，要保留的结果请移到工作目录，或在回复里告诉用户位置。")},
    {"id": "communicate", "title": "与用户沟通", "scope": "all", "kinds": [], "text": (
        "用户不一定一直盯着这个窗口。\n"
        "- 需要用户做决定或补充信息时，用 Agent Nexus 的 ask_user 工具提问：一次问全，给出 2–4 个具体选项并说明你倾向哪个。"
        "得到回答前不要做删除、覆盖、推送、付费之类不可逆的操作。\n"
        "- 不需要立刻回答的进展、阶段结果或较长的报告，用 mail_send 发给 \"me\"（用户本人），标题写清楚是什么。\n"
        "- 自己能查清楚的事不要问。没有这些工具时，直接在回复里提问。")},
    {"id": "team", "title": "团队协作", "scope": "all", "kinds": [], "text": (
        "可能有其他 Agent 和你同时工作，每个会话都有一个邮箱地址（名字@设备），你的是 $address。\n"
        "- 动手做较大的改动前，用 mail_who 看看谁在线、在做什么，避免同时改同一批文件。\n"
        "- 需要别人配合时用 mail_send 发给对方的地址；确实需要所有人知道的事才发给 @all。\n"
        "- 工具结果末尾提示有未读邮件时，用 mail_inbox / mail_read 查看，回复里写清楚结论。")},
    {"id": "nexus_mcp", "title": "Agent Nexus MCP 与工作区", "scope": "all", "kinds": [], "text": "$mcp_hint"},
    {"id": "gateway", "title": "网关工作方式", "scope": "gateway", "kinds": [], "text": (
        "这次要操作的是另一台设备：$context_device 上的 $context_workdir，不是你所在的 $device。\n"
        "- 通过上面说的 Agent Nexus MCP 的设备工具操作它（file_read、file_write、edit_file、file_grep、exec_command 等），"
        "每次调用都带上 workspace_id=\"$handle\"。\n"
        "- 不要用 use_device 换到别的设备或目录，除非用户要求。workspace_id 失效时，"
        "调用 use_device(device=\"$context_device\", workdir=\"$context_workdir\") 重新获取。\n"
        "- 你自带的文件工具和 shell 只作用于 $device，不要用它们去改 $context_device 上的文件。\n"
        "- 临时文件放到 $device 上的 $tmpdir，对话结束 7 天后自动清理。")},
    {"id": "project", "title": "项目说明", "scope": "all", "kinds": [], "text": (
        "开始前先读项目根目录里的 AGENTS.md 和 CLAUDE.md（改动某个子目录时，也看那里的同名文件）。"
        "其中的约定——怎么构建、怎么测试、代码风格、哪些事不能做——优先于你的默认习惯；"
        "没有这些文件时，照项目现有的写法来。")},
)
_BUILTIN = {f["id"]: f for f in BUILTIN_FRAGMENTS}
_COMPARED = ("title", "text", "scope", "kinds")

BUILTIN_PRESETS: tuple[dict, ...] = (
    {"id": "default", "name": "默认", "order": [f["id"] for f in BUILTIN_FRAGMENTS], "enabled": {}},
    {"id": "minimal", "name": "精简", "order": [f["id"] for f in BUILTIN_FRAGMENTS],
     "enabled": {"communicate": False, "team": False, "project": False}},
)
_BUILTIN_PRESET_IDS = {p["id"] for p in BUILTIN_PRESETS}


class PromptError(ValueError):
    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------------------------- variables

def os_name(platform_name: str | None = None) -> str:
    """A short readable OS name, e.g. ``Windows 11`` / ``macOS 14.5`` / ``Ubuntu 24.04 LTS``."""
    name = platform_name or sys.platform
    try:
        if name == "win32":
            release = platform.release()
            try:
                if int(platform.version().split(".")[-1]) >= 22000:
                    release = "11"
            except (ValueError, IndexError):
                pass
            return f"Windows {release}".strip()
        if name == "darwin":
            return f"macOS {platform.mac_ver()[0]}".strip()
        if name.startswith("linux"):
            try:
                info = platform.freedesktop_os_release()
                pretty = info.get("PRETTY_NAME") or info.get("NAME")
                if pretty:
                    return pretty  # "Ubuntu 24.04.5 LTS", "Debian GNU/Linux 12 (bookworm)"
            except (OSError, AttributeError):
                pass
            return "Linux"
    except Exception:  # noqa: BLE001, S110 - never fail a render over the OS name
        pass
    return name


def variables(context: Mapping[str, Any] | None = None, *, placeholders: bool = False) -> dict[str, str]:
    """Every variable's value for a run. ``context`` may carry any variable by name plus ``kind`` (for ``$agent``)
    and ``now`` (a timestamp for ``$date`` / ``$time``). Missing values are ``""``, or with ``placeholders`` (the
    editor's preview, where no run exists yet) a readable ``‹description›``."""
    context = dict(context or {})
    now = context.get("now")
    try:
        moment = time.localtime(float(now)) if now not in (None, "") else time.localtime()
    except (TypeError, ValueError, OverflowError, OSError):
        moment = time.localtime()
    values: dict[str, str] = {}
    for name in VARIABLE_NAMES:
        value = context.get(name)
        values[name] = "" if value is None else str(value)
    if not values["agent"]:
        kind = str(context.get("kind") or "")
        values["agent"] = KIND_LABELS.get(kind, kind)
    if not values["os"]:
        values["os"] = os_name()
    if not values["date"]:
        values["date"] = time.strftime("%Y-%m-%d", moment)
    if not values["time"]:
        values["time"] = time.strftime("%H:%M", moment)
    if not values["owner"]:
        values["owner"] = "用户"
    if placeholders:
        for name, value in values.items():
            if not value:
                values[name] = f"‹{_DESCRIPTIONS[name]}›"
    return values


def variable_list(values: Mapping[str, str] | None = None) -> list[dict]:
    """``[{name, desc, value}]`` for the editor's ``$`` completion."""
    values = values or {}
    return [{"name": name, "desc": desc, "value": values.get(name, "")} for name, desc in VARIABLES]


_DOLLAR = re.compile(r"\$(?:(\$)|\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def expand(text: str, values: Mapping[str, str]) -> tuple[str, list[str]]:
    """Substitute variables in ``text``. Returns ``(text, unknown names in order of first use)``.

    ``$$`` -> ``$``; ``${name}`` -> exact name; ``$name...`` -> the longest known name that is a prefix of the
    identifier after ``$`` (the rest of it stays as text). Unknown ones stay exactly as written."""
    names = sorted(values, key=len, reverse=True)
    unknown: list[str] = []
    out: list[str] = []
    last = 0
    for match in _DOLLAR.finditer(text or ""):
        out.append(text[last:match.start()])
        last = match.end()
        escaped, braced, bare = match.groups()
        if escaped:
            out.append("$")
            continue
        if braced is not None:
            if braced in values:
                out.append(values[braced])
            else:
                out.append(match.group(0))
                if braced not in unknown:
                    unknown.append(braced)
            continue
        hit = next((name for name in names if bare.startswith(name)), None)
        if hit is None:
            out.append(match.group(0))
            if bare not in unknown:
                unknown.append(bare)
        else:
            out.append(values[hit] + bare[len(hit):])
    out.append((text or "")[last:])
    return "".join(out), unknown


# --------------------------------------------------------------------------------------------- delivery

def toml_string(text: str) -> str:
    """``text`` as a TOML basic string (double quotes, escapes, one line -- safe for a ``.cmd`` argument's newlines)."""
    out = ['"']
    for ch in text:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def system_prompt_args(kind: str, text: str) -> list[str]:
    """Arguments that carry the system prompt to ``kind``'s CLI; ``[]`` when it goes in the first message instead."""
    if not text:
        return []
    if kind == "claude":
        return ["--append-system-prompt", text]
    if kind == "codex":
        return ["-c", f"developer_instructions={toml_string(text)}"]
    return []


def prefix_message(text: str, prompt: str) -> str:
    """The first message of a CLI without a system-prompt option: the instructions, a rule, then the user's words."""
    if not text:
        return prompt
    return f"{text}\n\n---\n\n{prompt}"


def delivery(kind: str, text: str) -> dict:
    """``{"mode": "argv"|"prefix"|"none", "argv": [...]}`` -- how ``text`` reaches ``kind``'s CLI."""
    if not text:
        return {"mode": "none", "argv": []}
    argv = system_prompt_args(kind, text)
    return {"mode": "argv" if argv else "prefix", "argv": argv}


# --------------------------------------------------------------------------------------------- normalizing

def _builtin_copy(fid: str) -> dict:
    base = copy.deepcopy(_BUILTIN[fid])
    base.update(builtin=True, enabled=True, modified=False)
    return base


def _clean_kinds(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    kinds: list[str] = []
    for kind in value:
        if isinstance(kind, str) and _KIND.fullmatch(kind) and kind not in kinds:
            kinds.append(kind)
    return kinds


def _clean_fragment(raw: Any, *, strict: bool) -> dict | None:
    """A fragment with valid fields. ``strict`` (a save) raises on bad input; otherwise (a stored file) it repairs."""
    def fail(message: str) -> None:
        if strict:
            raise PromptError(message)

    if not isinstance(raw, Mapping):
        fail("片段格式不对")
        return None
    fid = raw.get("id")
    if not isinstance(fid, str) or not _ID.fullmatch(fid):
        fail(f"片段 ID 不合法：{str(fid)[:40]}（只能用字母、数字、- _ .，最长 64 个字符）")
        return None
    title = str(raw.get("title") or "").strip()
    if not title:
        title = _BUILTIN[fid]["title"] if fid in _BUILTIN else fid
    if len(title) > TITLE_MAX:
        fail(f"片段“{title[:20]}…”的标题太长（最多 {TITLE_MAX} 个字）")
        title = title[:TITLE_MAX]
    text = raw.get("text")
    text = "" if text is None else str(text)
    if len(text) > TEXT_MAX:
        fail(f"片段“{title}”太长（最多 {TEXT_MAX} 个字符）")
        text = text[:TEXT_MAX]
    scope = raw.get("scope") or "all"
    if scope not in SCOPES:
        fail(f"片段“{title}”的适用模式只能是 all / direct / gateway")
        scope = "all"
    fragment = {"id": fid, "title": title, "text": text, "enabled": raw.get("enabled") is not False,
                "builtin": fid in _BUILTIN, "scope": scope, "kinds": _clean_kinds(raw.get("kinds"))}
    if fid in _BUILTIN:
        shipped = _BUILTIN[fid]
        fragment["modified"] = any(fragment[key] != shipped[key] for key in _COMPARED)
    return fragment


def _clean_preset(raw: Any, fragment_ids: list[str], *, strict: bool) -> dict | None:
    if not isinstance(raw, Mapping):
        if strict:
            raise PromptError("预设格式不对")
        return None
    pid = raw.get("id")
    if not isinstance(pid, str) or not _ID.fullmatch(pid):
        if strict:
            raise PromptError(f"预设 ID 不合法：{str(pid)[:40]}")
        return None
    name = str(raw.get("name") or "").strip() or pid
    if len(name) > NAME_MAX:
        if strict:
            raise PromptError(f"预设名称太长（最多 {NAME_MAX} 个字）")
        name = name[:NAME_MAX]
    known = set(fragment_ids)
    order: list[str] = []
    for fid in raw.get("order") or []:
        if isinstance(fid, str) and fid in known and fid not in order:
            order.append(fid)
    enabled_raw = raw.get("enabled") if isinstance(raw.get("enabled"), Mapping) else {}
    enabled = {fid: bool(on) for fid, on in enabled_raw.items() if isinstance(fid, str) and fid in known}
    return {"id": pid, "name": name, "order": order, "enabled": enabled}


def normalize(doc: Any, *, strict: bool = False, previous: Mapping | None = None) -> dict:
    """A complete, valid document: built-ins present (missing ones from ``previous``, else shipped), unique ids,
    presets pointing only at existing fragments, at least the built-in presets."""
    doc = doc if isinstance(doc, Mapping) else {}
    raw_fragments = doc.get("fragments") if isinstance(doc.get("fragments"), list) else []
    if strict and len(raw_fragments) > MAX_FRAGMENTS:
        raise PromptError(f"片段太多（最多 {MAX_FRAGMENTS} 个）")
    fragments: list[dict] = []
    seen: set[str] = set()
    for raw in raw_fragments[:MAX_FRAGMENTS]:
        fragment = _clean_fragment(raw, strict=strict)
        if fragment is None:
            continue
        if fragment["id"] in seen:
            if strict:
                raise PromptError(f"片段 ID 重复：{fragment['id']}")
            continue
        seen.add(fragment["id"])
        fragments.append(fragment)
    kept = {f["id"]: f for f in (previous or {}).get("fragments", []) if isinstance(f, Mapping)}
    for index, fid in enumerate(_BUILTIN):
        if fid in seen:
            continue
        fragment = _clean_fragment(kept[fid], strict=False) if fid in kept else _builtin_copy(fid)
        # put a missing built-in back near its shipped position
        position = min(index, len(fragments))
        fragments.insert(position, fragment)
        seen.add(fid)
    ids = [f["id"] for f in fragments]

    raw_presets = doc.get("presets") if isinstance(doc.get("presets"), list) else []
    if strict and len(raw_presets) > MAX_PRESETS:
        raise PromptError(f"预设太多（最多 {MAX_PRESETS} 个）")
    presets: list[dict] = []
    for raw in raw_presets[:MAX_PRESETS]:
        preset = _clean_preset(raw, ids, strict=strict)
        if preset is None:
            continue
        if any(p["id"] == preset["id"] for p in presets):
            if strict:
                raise PromptError(f"预设 ID 重复：{preset['id']}")
            continue
        presets.append(preset)
    if not raw_presets:
        presets = [_clean_preset(p, ids, strict=False) for p in BUILTIN_PRESETS]
    elif not any(p["id"] == "default" for p in presets):
        presets.insert(0, _clean_preset(BUILTIN_PRESETS[0], ids, strict=False))
    for preset in presets:
        preset["builtin"] = preset["id"] in _BUILTIN_PRESET_IDS
    return {"version": 1, "fragments": fragments, "presets": presets}


def ordered(doc: Mapping, preset: Mapping) -> list[dict]:
    """The preset's fragments in order: its ``order`` first, then the rest in document order."""
    by_id = {f["id"]: f for f in doc["fragments"]}
    ids = [fid for fid in preset.get("order", []) if fid in by_id]
    ids += [f["id"] for f in doc["fragments"] if f["id"] not in ids]
    return [by_id[fid] for fid in ids]


def is_on(fragment: Mapping, preset: Mapping) -> bool:
    enabled = preset.get("enabled") or {}
    if fragment["id"] in enabled:
        return bool(enabled[fragment["id"]])
    return fragment.get("enabled") is not False


def render_doc(doc: Mapping, preset_id: str | None, context: Mapping[str, Any] | None = None, *,
               placeholders: bool = False) -> dict:
    """``{text, segments: [{id, title, chars, enabled}], skipped: [{id, title, reason}], unknown_vars, variables,
    preset}`` for ``context`` (``kind``, ``mode`` and the variables). Segments are only the fragments that made it
    into ``text``; ``skipped`` says why the others did not."""
    context = dict(context or {})
    presets = doc["presets"]
    preset = next((p for p in presets if p["id"] == preset_id), None) or \
        next((p for p in presets if p["id"] == "default"), None) or (presets[0] if presets else
                                                                      {"id": "default", "order": [], "enabled": {}})
    kind = str(context.get("kind") or "")
    mode = context.get("mode") if context.get("mode") in MODES else "direct"
    values = variables(context, placeholders=placeholders)
    parts: list[str] = []
    segments: list[dict] = []
    skipped: list[dict] = []
    unknown: list[str] = []
    for fragment in ordered(doc, preset):
        base = {"id": fragment["id"], "title": fragment["title"]}
        if not is_on(fragment, preset):
            skipped.append({**base, "reason": "disabled"})
            continue
        if fragment["scope"] != "all" and fragment["scope"] != mode:
            skipped.append({**base, "reason": "mode"})
            continue
        if fragment["kinds"] and kind and kind not in fragment["kinds"]:
            skipped.append({**base, "reason": "kind"})
            continue
        text, missing = expand(fragment["text"], values)
        text = text.strip()
        for name in missing:
            if name not in unknown:
                unknown.append(name)
        if not text:
            skipped.append({**base, "reason": "empty"})
            continue
        parts.append(text)
        segments.append({**base, "chars": len(text), "enabled": True})
    return {"preset": preset["id"], "text": "\n\n".join(parts), "segments": segments, "skipped": skipped,
            "unknown_vars": unknown, "variables": values}


# --------------------------------------------------------------------------------------------- the store

class Prompts:
    """The fragment store. ``context_defaults()`` supplies this device's variables (device, os, gateway, owner…)
    for previews; a run passes its full context to :meth:`render`."""

    def __init__(self, root: Path, *, path: Path | None = None,
                 context_defaults: Callable[[], Mapping[str, Any]] | None = None):
        self.path = Path(path) if path else Path(root) / ".anx" / "agents" / "prompts.json"
        self.context_defaults = context_defaults
        self.lock = threading.RLock()
        self._doc: dict | None = None
        self._stamp: tuple | None = None

    # -- persistence
    def _stat(self) -> tuple | None:
        try:
            stat = self.path.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def load(self) -> dict:
        """The document (a deep copy), built-ins merged in. A damaged file is kept as ``prompts.json.bad``."""
        with self.lock:
            stamp = self._stat()
            if self._doc is None or stamp != self._stamp:
                raw: Any = {}
                if stamp is not None:
                    try:
                        raw = json.loads(self.path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        try:
                            os.replace(self.path, self.path.with_name(self.path.name + ".bad"))
                        except OSError:
                            pass
                        raw, stamp = {}, None
                self._doc, self._stamp = normalize(raw), stamp
            return copy.deepcopy(self._doc)

    def _write(self, doc: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stored = {"version": 1,
                  "fragments": [{k: v for k, v in f.items() if k not in ("builtin", "modified")} for f in doc["fragments"]],
                  "presets": [{k: v for k, v in p.items() if k != "builtin"} for p in doc["presets"]]}
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(stored, handle, ensure_ascii=False, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass
        self._doc, self._stamp = doc, self._stat()
        return copy.deepcopy(doc)

    def save(self, doc: Mapping) -> dict:
        """Replace the whole document (validated; raises :class:`PromptError`). Built-ins left out are kept."""
        with self.lock:
            clean = normalize(doc, strict=True, previous=self.load())
            return self._write(clean)

    def _mutate(self, change: Callable[[dict], None]) -> dict:
        with self.lock:
            doc = self.load()
            change(doc)
            return self._write(normalize(doc, strict=True, previous=doc))

    # -- fragments
    def fragment(self, fid: str) -> dict | None:
        return next((f for f in self.load()["fragments"] if f["id"] == fid), None)

    def upsert_fragment(self, fragment: Mapping) -> dict:
        """Add a fragment (new id: appended to every preset's order) or update one's fields."""
        def change(doc: dict) -> None:
            fid = fragment.get("id")
            current = next((f for f in doc["fragments"] if f["id"] == fid), None)
            if current is None:
                if not isinstance(fid, str) or not _ID.fullmatch(fid):
                    raise PromptError(f"片段 ID 不合法：{str(fid)[:40]}")
                doc["fragments"].append({"enabled": True, "scope": "all", "kinds": [], "text": "", **dict(fragment)})
                for preset in doc["presets"]:
                    if preset["order"]:
                        preset["order"].append(fid)
            else:
                current.update({k: v for k, v in fragment.items() if k in ("title", "text", "scope", "kinds", "enabled")})
        return self._mutate(change)

    def delete_fragment(self, fid: str) -> dict:
        if fid in _BUILTIN:
            raise PromptError("内置片段不能删除，可以停用或还原", "builtin")

        def change(doc: dict) -> None:
            before = len(doc["fragments"])
            doc["fragments"] = [f for f in doc["fragments"] if f["id"] != fid]
            if len(doc["fragments"]) == before:
                raise PromptError(f"没有这个片段：{fid}", "not_found")
        return self._mutate(change)

    def reset(self, fid: str) -> dict:
        """Restore a built-in fragment's shipped title, text, scope and CLIs (its position and switches stay)."""
        if fid not in _BUILTIN:
            raise PromptError("只有内置片段可以还原", "not_builtin")

        def change(doc: dict) -> None:
            for fragment in doc["fragments"]:
                if fragment["id"] == fid:
                    fragment.update({key: copy.deepcopy(_BUILTIN[fid][key]) for key in _COMPARED})
        return self._mutate(change)

    def reset_all(self) -> dict:
        """Everything back to the shipped state (custom fragments and presets are removed)."""
        with self.lock:
            return self._write(normalize({}))

    # -- presets
    def _preset(self, doc: dict, pid: str) -> dict:
        preset = next((p for p in doc["presets"] if p["id"] == pid), None)
        if preset is None:
            raise PromptError(f"没有这个预设：{pid}", "not_found")
        return preset

    def reorder(self, pid: str, order: list[str]) -> dict:
        def change(doc: dict) -> None:
            self._preset(doc, pid)["order"] = list(order)
        return self._mutate(change)

    def set_enabled(self, pid: str, fid: str, on: bool) -> dict:
        def change(doc: dict) -> None:
            if not any(f["id"] == fid for f in doc["fragments"]):
                raise PromptError(f"没有这个片段：{fid}", "not_found")
            self._preset(doc, pid)["enabled"][fid] = bool(on)
        return self._mutate(change)

    def save_preset(self, preset: Mapping) -> dict:
        """Add or replace a preset. A new one without ``order`` copies the default preset's."""
        def change(doc: dict) -> None:
            pid = preset.get("id")
            current = next((p for p in doc["presets"] if p["id"] == pid), None)
            if current is None:
                if len(doc["presets"]) >= MAX_PRESETS:
                    raise PromptError(f"预设太多（最多 {MAX_PRESETS} 个）")
                base = self._preset(doc, "default") if any(p["id"] == "default" for p in doc["presets"]) else {}
                doc["presets"].append({"order": list(base.get("order", [])), "enabled": dict(base.get("enabled", {})),
                                       **dict(preset)})
            else:
                current.update({k: v for k, v in preset.items() if k in ("name", "order", "enabled")})
        return self._mutate(change)

    def delete_preset(self, pid: str) -> dict:
        if pid == "default":
            raise PromptError("默认预设不能删除", "builtin")

        def change(doc: dict) -> None:
            self._preset(doc, pid)
            doc["presets"] = [p for p in doc["presets"] if p["id"] != pid]
        return self._mutate(change)

    # -- rendering
    def _context(self, context: Mapping[str, Any] | None) -> dict:
        merged: dict[str, Any] = {}
        if self.context_defaults:
            try:
                merged.update(self.context_defaults() or {})
            except Exception:  # noqa: BLE001, S110 - a preview never fails over a default
                pass
        merged.update({k: v for k, v in (context or {}).items() if v not in (None, "")})
        return merged

    def render(self, preset: str | None, context: Mapping[str, Any] | None = None, *, draft: Mapping | None = None,
               placeholders: bool = False) -> dict:
        """Render ``preset`` (``None`` = default) for ``context``. ``draft`` renders an unsaved document instead."""
        doc = normalize(draft, previous=self.load()) if draft is not None else self.load()
        return render_doc(doc, preset, self._context(context), placeholders=placeholders)

    # -- RPC ops (never raise)
    def handlers(self) -> dict[str, Callable[[dict], dict]]:
        return {"agent_prompts": self.op_prompts, "agent_prompt_save": self.op_save,
                "agent_prompt_preview": self.op_preview}

    def op_prompts(self, params: dict | None = None) -> dict:
        try:
            doc = self.load()
            doc["variables"] = variable_list(variables(self._context({}), placeholders=True))
            return doc
        except Exception as error:  # noqa: BLE001
            return {"error": f"读取提示词失败：{error}", "code": "failed"}

    def op_save(self, params: dict | None = None) -> dict:
        """``{reset: id}`` | ``{reset_all: true}`` | ``{delete_fragment: id}`` | ``{delete_preset: id}`` |
        ``{fragment: {...}}`` | ``{preset: {...}}`` | ``{fragments, presets}`` (the whole document)."""
        params = params if isinstance(params, dict) else {}
        try:
            if params.get("reset"):
                return self.reset(str(params["reset"]))
            if params.get("reset_all"):
                return self.reset_all()
            if params.get("delete_fragment"):
                return self.delete_fragment(str(params["delete_fragment"]))
            if params.get("delete_preset"):
                return self.delete_preset(str(params["delete_preset"]))
            if isinstance(params.get("fragment"), dict):
                return self.upsert_fragment(params["fragment"])
            if isinstance(params.get("preset"), dict):
                return self.save_preset(params["preset"])
            if isinstance(params.get("fragments"), list) or isinstance(params.get("presets"), list):
                return self.save({"fragments": params.get("fragments") if isinstance(params.get("fragments"), list)
                                  else self.load()["fragments"],
                                  "presets": params.get("presets") if isinstance(params.get("presets"), list)
                                  else self.load()["presets"]})
            return {"error": "没有要保存的内容", "code": "bad_request"}
        except PromptError as error:
            return {"error": str(error), "code": error.code}
        except OSError as error:
            return {"error": f"提示词没有保存：{error.strerror or error}", "code": "io"}
        except Exception as error:  # noqa: BLE001
            return {"error": f"提示词没有保存：{error}", "code": "failed"}

    def op_preview(self, params: dict | None = None) -> dict:
        """``{preset?, context?: {kind, mode, ...}, draft?: {fragments, presets}}`` -> the render, with
        ``‹…›`` placeholders for values only a real run has."""
        params = params if isinstance(params, dict) else {}
        context = params.get("context") if isinstance(params.get("context"), dict) else {}
        draft = params.get("draft") if isinstance(params.get("draft"), dict) else None
        try:
            return self.render(params.get("preset") or None, context, draft=draft, placeholders=True)
        except Exception as error:  # noqa: BLE001
            return {"error": f"预览失败：{error}", "code": "failed"}
