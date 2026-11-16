"""The agent-nexus device plugin: shell, file editing, images, plans, watchers and screen control.

The tools are the point of this server; everything around them - transport,
public URL, authentication, discovery, health - now comes from mcpkit, which
also serves the OAuth authorization server ChatGPT connects through.

``destructive_hint=False`` on every writing tool is a deliberate operator
choice: this server is driven by a human who is already trusting it with a
shell, and the per-call confirmation some clients derive from the default was
pure friction. It does not make ``exec_command``, ``edit_file``, ``patch_file`` or
``exec_kill`` safe. Real containment comes from ``allowed_roots``,
``command_deny`` and ``allow_dangerous``, not from a hint a client may ignore.
``read_only_hint`` stays honest: it is set only where the tool genuinely
cannot change anything.
"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import mimetypes
import platform as host_platform
import secrets
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from fastmcp import FastMCP
from fastmcp.tools.base import ToolResult
from mcp_types import ImageContent, TextContent, ToolAnnotations
from mcpkit import Service, main
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__
from . import agents as agent_sessions_mod
from . import edit as edit_mod
from . import files as files_mod
from . import manager_asks as manager_asks_mod
from . import search as search_mod
from .audit import AuditLog
from .exec_runtime import MAX_WAIT_MS, UnifiedExecManager
from .platform import CONTRACT, Contract
if sys.platform == "win32":
    from .control import ControlError, ControlOptions, ControlSession
    from .control import capture as capture_mod
    from .control import inputs
    from .control import screens as screens_mod
    from .control.uia import UiaError
    from .control.vision import VisionUnavailable
    from .control.win32 import virtual_screen

#: The Windows-only extras (capabilities screen, uia, input). Kept for callers
#: of the pre-contract gate; ``src.platform`` is what decides registration now.
WINDOWS_TOOLS = {"start_control", "stop_control", "control_status", "list_screens",
                 "screenshot", "read_ui", "element", "mouse", "keyboard", "vision"}
from .policy import Policy, PolicyError
from .settings import Config, from_service, public_config
from .watchers import WatchManager

#: How long a ``view_image`` URL stays valid.
MEDIA_TTL_SECONDS = 3 * 24 * 60 * 60


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


#: Mouse actions, and the button each implies.
CLICK_ACTIONS = {"click": None, "double_click": None, "right_click": "right", "middle_click": "middle"}


def _on_desktop(x: int, y: int) -> None:
    """Refuse coordinates off the desktop instead of letting them clamp silently."""
    left, top, width, height = virtual_screen()
    if not (left <= x < left + width and top <= y < top + height):
        raise ValueError(
            f"({x}, {y}) is outside the desktop, which spans x {left}..{left + width - 1}, "
            f"y {top}..{top + height - 1}. Call list_screens for the monitor layout."
        )


def _do_mouse(action: str, x: int | None, y: int | None, button: str, clicks: int,
              to_x: int | None, to_y: int | None, amount: int, horizontal: bool) -> dict[str, Any]:
    """The blocking half of the mouse tool; runs off the event loop."""
    if button not in inputs.BUTTONS:
        raise ValueError(f"button must be one of {sorted(inputs.BUTTONS)}")
    if action == "position":
        position = inputs.position()
        return {"action": action, "position": {"x": position[0], "y": position[1]},
                "foreground_window": inputs.foreground_window()}

    if x is not None and y is not None:
        _on_desktop(x, y)
        inputs.move(x, y)
    elif action == "move":
        raise ValueError("move needs both x and y")

    if action == "move":
        pass
    elif action in CLICK_ACTIONS:
        count = 2 if action == "double_click" else max(1, min(5, clicks))
        inputs.click(CLICK_ACTIONS[action] or button, count)
    elif action == "down":
        inputs.button_down(button)
    elif action == "up":
        inputs.button_up(button)
    elif action == "drag":
        if to_x is None or to_y is None:
            raise ValueError("drag needs to_x and to_y")
        _on_desktop(to_x, to_y)
        inputs.drag(to_x, to_y, button)
    elif action == "scroll":
        if not amount:
            raise ValueError("scroll needs a non-zero amount, in notches")
        inputs.scroll(amount, horizontal)
    else:
        raise ValueError(
            "action must be one of: move, click, double_click, right_click, middle_click, "
            "down, up, drag, scroll, position"
        )

    position = inputs.position()
    return {"action": action, "position": {"x": position[0], "y": position[1]},
            "foreground_window": inputs.foreground_window()}


#: Element actions that are really just a click at the control's own point.
ELEMENT_CLICKS = {"click": "click", "double_click": "double_click", "right_click": "right_click"}
#: Elements that came out of the window's pixels rather than from the
#: application, and how to say so.
PIXEL_SOURCES = {
    "ocr": "recognised from the screen by OCR",
    "vision": "found in the window's picture by the icon model",
}


def _do_element(session: ControlSession, target: Any, action: str, text: str) -> dict[str, Any]:
    """Act on one element: a click at its own point, or its own pattern."""
    described = {"action": action, "role": target.role, "name": target.name}
    if target.source in PIXEL_SOURCES:
        # A guess read off the pixels has no provider behind it, so there is no
        # pattern to invoke and no rectangle to re-verify against the tree.
        # Clicking where it appeared is the only honest option.
        if action not in ELEMENT_CLICKS:
            raise ValueError(
                f"{target.name!r} was {PIXEL_SOURCES[target.source]}, not reported by the "
                f"application, so only {sorted(ELEMENT_CLICKS)} work on it"
            )
        x, y = target.centre
        _on_desktop(x, y)
        return described | {"source": target.source} | _do_mouse(
            ELEMENT_CLICKS[action], x, y, "left", 1, None, None, 0, False)
    if action in ELEMENT_CLICKS:
        x, y = session.ui.clickable_point(target)
        _on_desktop(x, y)
        return described | _do_mouse(ELEMENT_CLICKS[action], x, y, "left", 1,
                                     None, None, 0, False)
    if action in ("focus", "restore") and target.role == "window":
        return described | _focus_window(target.hwnd or target.window_hwnd, action == "restore")
    return described | session.ui.act(target, action, text)


def _focus_window(hwnd: int, restore: bool) -> dict[str, Any]:
    """Bring a window forward, and say so only if it actually came forward.

    Windows refuses foreground changes from a process the user is not
    interacting with, so this has to be checked rather than assumed.
    """
    from .control.win32 import SW_RESTORE, user32

    if restore or user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    time.sleep(0.15)
    arrived = int(user32.GetForegroundWindow() or 0) == hwnd
    out = {"foreground": arrived, "foreground_window": inputs.foreground_window()}
    if not arrived:
        out["note"] = (
            "Windows declined the foreground change; click the window's title bar with the "
            "mouse tool instead"
        )
    return out


def _brief(payload: dict[str, Any]) -> str:
    """One line describing the call, so a client that ignores structured
    content still shows the user something meaningful."""
    parts: list[str] = [str(payload.get("summary") or payload.get("action") or "ok")]
    after = payload.get("after") or {}
    if after.get("foreground_window"):
        parts.append(f"window: {after['foreground_window']}")
    if after.get("foreground_changed_from"):
        parts.append("focus changed")
    if after.get("appeared"):
        parts.append(f"{len(after['appeared'])} new element(s)")
    if after.get("gone"):
        parts.append(f"{len(after['gone'])} gone")
    if after.get("unchanged"):
        parts.append("screen unchanged")
    if after.get("note"):
        parts.append(str(after["note"]))
    return " · ".join(parts)


def _tool_result(payload: dict[str, Any], image: tuple[bytes, str] | None = None) -> ToolResult:
    blocks: list[Any] = [TextContent(type="text", text=_brief(payload))]
    if image is not None:
        data, mime = image
        blocks.append(ImageContent(type="image", data=base64.b64encode(data).decode("ascii"),
                                   mimeType=mime))
    return ToolResult(content=blocks, structured_content=payload)


def _do_keyboard(action: str, text: str, keys: str) -> dict[str, Any]:
    if action == "type":
        if not text:
            raise ValueError("type needs text")
        typed = inputs.type_text(text)
        return {"action": action, "characters": typed,
                "foreground_window": inputs.foreground_window()}
    if action == "press":
        if not keys.strip():
            raise ValueError("press needs keys, for example 'ctrl+s' or 'ctrl+a delete'")
        struck = inputs.press(keys)
        return {"action": action, "pressed": struck,
                "foreground_window": inputs.foreground_window()}
    raise ValueError("action must be 'type' or 'press'")


TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    # -- read-only -------------------------------------------------------
    "exec_list": ToolAnnotations(read_only_hint=True, open_world_hint=False),
    "curr_time": ToolAnnotations(read_only_hint=True, open_world_hint=False),
    "sleep": ToolAnnotations(read_only_hint=True, open_world_hint=False),
    "companion_routes": ToolAnnotations(read_only_hint=True, open_world_hint=True),
    "request_user_input_result": ToolAnnotations(read_only_hint=True, open_world_hint=True),
    "watch_list": ToolAnnotations(read_only_hint=True, open_world_hint=False),
    "watch_event": ToolAnnotations(read_only_hint=True, open_world_hint=False),
    "watch_events": ToolAnnotations(read_only_hint=True, open_world_hint=False),
    # -- anx.device/1 additions (design 5.2) -----------------------------
    "device_info": ToolAnnotations(read_only_hint=True, open_world_hint=False),
    "file_list": ToolAnnotations(read_only_hint=True, open_world_hint=False),
    "file_stat": ToolAnnotations(read_only_hint=True, open_world_hint=False),
    "file_read": ToolAnnotations(read_only_hint=True, open_world_hint=False),
    "file_glob": ToolAnnotations(read_only_hint=True, open_world_hint=False),
    "file_grep": ToolAnnotations(read_only_hint=True, open_world_hint=False),
    "agent_sessions": ToolAnnotations(read_only_hint=True, open_world_hint=False),
    "file_write": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ),
    # Puts something in front of a person; dry_run only validates.
    "notify": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
    # Only replaces the in-memory plan shown alongside the conversation.
    "update_plan": ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
    # -- writing, declared non-destructive -------------------------------
    "exec_command": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
    "write_stdin": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
    "exec_kill": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
    # Asking changes nothing on the machine, but it does put a card in front of
    # a person and wait for them, so it is not "read-only" in the sense a model
    # should treat as free.
    "request_user_input": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
    "request_user_input_async": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
    "edit_file": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ),
    "patch_file": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ),
    "view_image": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
    "watch_create": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
    "watch_training": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
    "watch_cancel": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
    # -- screen control ---------------------------------------------------
    # Reading the screen changes nothing, but starting and stopping do: they
    # put the indicator up and swap the system cursor.
    "control_status": ToolAnnotations(read_only_hint=True, open_world_hint=False),
    "list_screens": ToolAnnotations(read_only_hint=True, open_world_hint=False),
    "screenshot": ToolAnnotations(read_only_hint=True, open_world_hint=False),
    # Not read-only, and the reason is worth stating: when a window renders
    # itself only partially *and* something covers it, read_ui raises that
    # window so the answer is complete. Focus is user-visible state, so the
    # hint stays honest even though reading is what the tool is for.
    "read_ui": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
    "element": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
    # Loading takes video memory and fetching downloads about a gigabyte.
    "vision": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
    ),
    "start_control": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
    "stop_control": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
    "mouse": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
    "keyboard": ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
}


INSTRUCTIONS = """\
This server is an agent-nexus device: it works on the user's own computer.

Core tools:
- exec_command: run a shell command directly; no explicit session setup is needed.
- write_stdin: continue/poll a still-running exec_command session.
- file_read / file_list / file_stat / file_write: read, list, inspect and create files.
- edit_file: replace an exact piece of text in one file (old_string -> new_string).
- patch_file: apply a unified diff (the `git diff` / `diff -u` format) to one or more files.
- view_image: inspect an image from the local filesystem; it also returns a temporary user-viewable HTTPS URL when the user should see the image.
- update_plan: keep a small task plan when work is multi-step.
- start_control / stop_control: take and release control of the physical desktop. While a session
  is open the screen shows a blue glow and a label and the mouse pointer changes, so the person at
  the machine can see it. list_screens, screenshot, mouse and keyboard refuse outside a session.
  Prefer the shell for anything a command can do; use the screen when the task is genuinely a GUI
  one. Stop the session as soon as the task is finished.
- watch_training / watch_create: persistently monitor long-running local work and route tiny
  events through GPT Companion so the bookmarked ChatGPT conversation can be awakened.

Working style for source-code and text-file tasks:
- Use the shell for navigation and search. Prefer fast targeted commands such as `rg`,
  `git grep`, `find`, `head`, `tail` and `git diff` when they answer the question directly;
  use file_read to read a file or a range of its lines.
- Edit with edit_file for a focused change and patch_file for changes across many places or
  files. Do not rewrite files through shell string replacement, heredocs, or ad-hoc scripts when
  these tools can express the change clearly. Both keep a file's encoding, BOM and line endings,
  and both change nothing unless the whole edit applies.
- Before editing, inspect the relevant context rather than reflexively dumping every related file.
  After editing, verify with a targeted diff, search, build, or test instead of automatically
  printing the entire modified file again.
- Full-file or large-output reads are allowed when they are actually useful: for example when the
  file is small, structure outside the local region matters, a complete configuration must be
  audited, or the user explicitly asks for the full content. These are guidance rules, not output
  limits; do not omit necessary context merely to keep tool output short.
- Avoid repeating the same large content across consecutive tool calls unless the task requires it.
- Use `exec_command` for shell-native work (search/navigation, git, builds, tests, scripts and
  process execution); use edit_file / patch_file for edits. Pick the tool by job rather than treating
  `exec_command` as the default way to modify files.
- When `workdir` is omitted, begin from configured `default_workdir`; this is a discovery starting
  point, not an access boundary. Access boundaries are controlled separately by policy.

Limits:
- tty=true is accepted but commands currently run with pipes, not a real PTY.
- Working directories and file/patch/image paths obey the configured filesystem policy; allowed_roots
  may be left empty for unrestricted access under the OS account running the server.
- Destructive commands are screened by server policy unless allow_dangerous is enabled.
"""




class NotifyAction(BaseModel):
    """A button on the notification. ``link`` opens a URL (or ``agentnexus://``) when clicked."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    label: str = Field(min_length=1, max_length=64)
    link: str | None = Field(default=None, max_length=2000)


NOTIFY_LEVELS = ("info", "warn", "urgent")
#: device_info re-checks GPT Companion at most this often.
COMPANION_PROBE_TTL_S = 60.0
MAX_NOTIFY_ACTIONS = 5


class DeviceServer:
    """The device tools over one FastMCP server, with policy and audit."""

    def __init__(self, service: Service, config: Config, *, platform_name: str | None = None) -> None:
        self.service = service
        self.config = config
        # Which anx.device/1 implementation registers tools (design 5.2 c).
        # Tests pass another platform to check its declared surface.
        self.contract = Contract(platform_name)
        #: Where agent_sessions looks for ~/.claude and ~/.codex (tests override it).
        self.agents_home: Path | None = None
        #: The notification backend; tests replace it with a recorder.
        self.notifier = self.contract.notify
        #: request_user_input through the agent-nexus inbox when a manager is
        #: present (design 8.12); ``None`` keeps the local chatx window only.
        self.manager_asks = manager_asks_mod.ManagerAsks.from_env()
        self.policy = Policy(config)
        self.exec_manager = UnifiedExecManager(config, self.policy)
        self.watch_manager = WatchManager(config, self.policy, self.exec_manager)
        self.plan: list[dict[str, str]] = []
        self.plan_explanation: str = ""
        # Short-lived, unguessable public media URLs, used as a cross-client
        # fallback when a client does not render MCP image content inline.
        # token -> (path, mime, expires_at)
        self._media_tokens: dict[str, tuple[Path, str, float]] = {}
        self.audit = AuditLog(config.audit_path(), config.audit_enabled)
        self.control = ControlSession(
            ControlOptions(
                banner_text=config.control.banner_text,
                pulse=config.control.pulse,
                show_indicator=config.control.show_indicator,
                change_cursor=config.control.change_cursor,
                use_ocr=config.control.use_ocr,
                vision_model_dir=config.control.vision_model_dir,
                vision_device=config.control.vision_device,
                vision_caption=config.control.vision_caption,
                project_root=config.project_root,
            ),
            marker=config.control_marker(),
            on_event=lambda event, payload: self.audit.write(event, **payload),
        ) if sys.platform == "win32" and self.contract.platform == "windows" else None
        self.mcp = service.create_server(instructions=INSTRUCTIONS)
        self._add_media_route()
        self._add_companion_route()
        self._register_tools()

    # -- deployment facts the tools report -----------------------------------
    def public_url(self) -> str:
        cfg = self.service.config
        return cfg.public_url or f"http://{cfg.host}:{cfg.port}"

    def public_config(self) -> dict[str, Any]:
        return public_config(self.config, self.service)

    def _add_media_route(self) -> None:
        @self.mcp.custom_route("/media/{token}", methods=["GET"])
        async def media(request: Request) -> Response:
            # Deliberately unauthenticated: the token is the capability, it
            # expires, and it names one file the model was already shown.
            item = self._media_tokens.get(request.path_params.get("token", ""))
            if item is None:
                return Response("Not found", status_code=404)
            target, mime, expires_at = item
            if time.time() > expires_at:
                self._media_tokens.pop(request.path_params["token"], None)
                return Response("Expired", status_code=410)
            try:
                payload = target.read_bytes()
            except OSError:
                self._media_tokens.pop(request.path_params["token"], None)
                return Response("Not found", status_code=404)
            return Response(payload, media_type=mime, headers={
                "Cache-Control": "private, max-age=600",
                "Content-Disposition": f'inline; filename="{target.name}"',
                "X-Content-Type-Options": "nosniff",
            })

    def _add_companion_route(self) -> None:
        @self.mcp.custom_route("/anx/companion-event", methods=["POST"])
        async def companion_event(request: Request) -> Response:
            # The local manager's companion wake adapter (agent-nexus design §8.7).
            # Loopback only, like the rest of this server; the custom header keeps
            # browsers (which cannot send it cross-origin without a preflight) out.
            if request.headers.get("x-anx") != "1":
                return JSONResponse({"status": "error", "error": "X-ANX: 1 required"}, status_code=403)
            try:
                body = await request.json()
            except ValueError:
                body = None
            if not isinstance(body, dict) or not isinstance(body.get("summary"), str) or not body["summary"].strip():
                return JSONResponse({"status": "error", "error": "summary required"}, status_code=400)
            event_type = body.get("event_type") if isinstance(body.get("event_type"), str) else "mail_received"
            result = await asyncio.to_thread(
                self.watch_manager.emit_mail_event, project_id=body.get("project_id"),
                workdir=body.get("workdir") if isinstance(body.get("workdir"), str) else None,
                summary=body["summary"], event_type=event_type[:64])
            self._record("companion_event", result)
            return JSONResponse(result)

    # -- plumbing ----------------------------------------------------------
    def _record(self, tool: str, result: dict[str, Any] | None = None, **extra: Any) -> None:
        payload: dict[str, Any] = {"tool": tool}
        if isinstance(result, dict):
            payload.update({
                k: v for k, v in result.items()
                if k not in {"output", "stdout", "stderr"}
            })
        payload.update(extra)
        self.audit.write("tool_call", **payload)

    def _err(self, message: str) -> dict[str, Any]:
        return {"status": "error", "error": message, "exit_code": None, "stdout": "", "stderr": ""}

    # -- resources / tools -------------------------------------------------
    def _register_tools(self) -> None:
        mcp = self.mcp

        def tool(*, name: str, **kwargs: Any) -> Any:
            """``mcp.tool`` with this tool's entry from ``TOOL_ANNOTATIONS``.

            Keeping the hints in one table rather than on 17 decorators is what
            makes them reviewable as a set -- and makes a missing entry an
            immediate KeyError instead of a tool that quietly ships without
            annotations.
            """
            # The platform's capability table decides registration (it used
            # to be a win32 check). Every tool carries its capability in _meta
            # so the gateway can name a missing one (design 5.2 rule 2).
            capability = self.contract.capability_of(name)
            if capability is None:
                return lambda function: function
            meta = {"anx/capability": capability, "anx/contract": CONTRACT}
            return mcp.tool(name=name, annotations=TOOL_ANNOTATIONS[name], meta=meta, **kwargs)

        @tool(
            name="exec_command",
            title="Run a command",
            description=(
                f"Run a command and return its output and exit_code. cmd is a {self._shell_label()} "
                "command line; shell='powershell' or shell='cmd' runs another shell instead. "
                f"The call waits up to yield_time_ms (default 10000, max {MAX_WAIT_MS}) and returns as "
                "soon as the command exits; a command still running then returns a session_id: poll "
                "it or type into it with write_stdin, stop it with exec_kill. Pass a long "
                "yield_time_ms for builds and tests, a short one to start a server. Output beyond "
                "max_output_tokens (about 4 characters each, default 10000) keeps its head and tail. "
                "workdir defaults to the session's workdir. For reading, searching and editing files "
                "prefer file_read, file_grep, file_glob and edit_file: they need no quoting."
            ),
        )
        async def exec_command(
            cmd: str,
            workdir: str | None = None,
            yield_time_ms: float | None = None,
            max_output_tokens: float | None = None,
            shell: str | None = None,
            login: bool | None = None,
            tty: bool = False,
        ) -> dict[str, Any]:
            try:
                result = await self.exec_manager.exec_command(
                    cmd=cmd,
                    workdir=workdir,
                    tty=tty,
                    yield_time_ms=yield_time_ms,
                    max_output_tokens=max_output_tokens,
                    shell=shell,
                    login=login,
                )
            except (PolicyError, RuntimeError, ValueError) as exc:
                self._record("exec_command", None, ok=False, error=str(exc), cmd=cmd[:300])
                return self._err(str(exc))
            self._record("exec_command", result, ok=True, cmd=cmd[:300], workdir=workdir)
            return result

        @tool(
            name="write_stdin",
            title="Send command input or wait for output",
            description=(
                "Send characters to a command that exec_command left running, then return the output "
                "it has produced since the last call (and exit_code once it has exited). chars='' "
                "just waits for output: up to yield_time_ms (default 5000, or 250 after writing; max "
                f"{MAX_WAIT_MS}), returning early when the command exits. Characters are sent exactly "
                "as given, so end a line with '\\n'; chars='\\u0003' stops the command."
            ),
        )
        async def write_stdin(
            session_id: int,
            chars: str = "",
            yield_time_ms: float | None = None,
            max_output_tokens: float | None = None,
        ) -> dict[str, Any]:
            try:
                result = await self.exec_manager.write_stdin(
                    session_id=session_id,
                    chars=chars,
                    yield_time_ms=yield_time_ms,
                    max_output_tokens=max_output_tokens,
                )
            except (KeyError, RuntimeError, ValueError) as exc:
                self._record("write_stdin", None, ok=False, session_id=session_id, error=str(exc))
                return self._err(str(exc))
            self._record("write_stdin", result, ok=True, session_id=session_id, bytes=len(chars))
            return result

        @tool(
            name="exec_kill",
            title="Stop a command",
            description=(
                "Terminate a still-running exec_command process and everything it started."
            ),
        )
        async def exec_kill(session_id: int) -> dict[str, Any]:
            try:
                result = await self.exec_manager.kill(session_id)
            except (KeyError, RuntimeError) as exc:
                return self._err(str(exc))
            self._record("exec_kill", result, ok=True, session_id=session_id)
            return result

        @tool(
            name="exec_list",
            title="List running commands",
            description="List resumable exec_command processes currently held by this MCP server.",
        )
        async def exec_list() -> dict[str, Any]:
            sessions = self.exec_manager.list_sessions()
            return {"status": "ok", "count": len(sessions), "sessions": sessions}

        @tool(
            name="edit_file",
            title="Edit a file",
            description=(
                "Replace exact text in one file: old_string must match the file character for "
                "character (copy it from file_read, without line-number prefixes) and occur exactly "
                "once, unless replace_all=true replaces every occurrence. Include enough surrounding "
                "lines to make it unique. Line endings may be given as LF: the file's own CRLF/LF, "
                "BOM and encoding (UTF-8 or GBK) are kept. The result has a numbered snippet of the "
                "edited lines, so there is no need to read the file again. Use file_write to create "
                "a file, patch_file for many changes at once."
            ),
        )
        async def edit_file(path: str, old_string: str, new_string: str,
                            replace_all: bool = False) -> dict[str, Any]:
            try:
                result = await asyncio.to_thread(edit_mod.edit_file, self.policy, path, old_string,
                                                 new_string, replace_all)
            except (edit_mod.EditError, PolicyError, OSError) as exc:
                self._record("edit_file", None, ok=False, error=str(exc), path=path)
                return {"status": "error", "error": str(exc)}
            self._record("edit_file", result, ok=True)
            return result

        @tool(
            name="patch_file",
            title="Apply a patch",
            description=(
                "Apply a unified diff (the output format of `git diff` or `diff -u`) to one or more "
                "files: '--- a/path' and '+++ b/path' headers, '@@ -start,count +start,count @@' "
                "hunks, /dev/null to create or delete a file, git 'rename from'/'rename to'. Relative "
                "paths start at workdir (default: default_workdir). All files change or none do; "
                "dry_run=true checks the patch without writing. Hunks may have drifted: the nearest "
                "matching position is used and reported."
            ),
        )
        async def patch_file(patch: str, workdir: str | None = None, dry_run: bool = False) -> dict[str, Any]:
            try:
                result = await asyncio.to_thread(edit_mod.patch_file, self.policy, patch, workdir, dry_run)
            except (edit_mod.EditError, PolicyError, OSError) as exc:
                self._record("patch_file", None, ok=False, error=str(exc), workdir=workdir)
                return {"status": "error", "error": str(exc)}
            self._record("patch_file", None, ok=True, workdir=workdir, dry_run=dry_run,
                         files=[item["path"] for item in result["files"]])
            return result

        @tool(
            name="view_image",
            title="View a local image",
            description=(
                "View a local image file when visual inspection is needed. Returns MCP image "
                "content for the model plus a temporary HTTPS URL. If the image is important "
                "for the user to see, or the user explicitly asks to see it, include that URL "
                "in the assistant response."
            ),
        )
        async def view_image(
            path: str,
            detail: str = "high",
            workdir: str | None = None,
        ) -> ToolResult:
            if detail not in {"high", "original"}:
                return ToolResult(
                    content=[TextContent(type="text", text="detail must be 'high' or 'original'")],
                    is_error=True,
                )
            try:
                cwd = self.policy.resolve_cwd(workdir)
                raw_path = Path(path).expanduser()
                target = (raw_path if raw_path.is_absolute() else cwd / raw_path).resolve()
                roots = self.config.resolved_roots()
                if roots and not any(_path_within(target, root) for root in roots):
                    raise PolicyError(f"image path {target} is outside allowed roots")
                if not target.is_file():
                    raise FileNotFoundError(str(target))
                size = target.stat().st_size
                if size > 20_000_000:
                    raise ValueError(f"image is too large ({size} bytes; limit is 20 MB)")
                mime, _ = mimetypes.guess_type(str(target))
                if not mime or not mime.startswith("image/"):
                    raise ValueError(f"unsupported image type for {target.name}")
                data = base64.b64encode(target.read_bytes()).decode("ascii")
            except (PolicyError, OSError, ValueError) as exc:
                self._record("view_image", None, ok=False, error=str(exc), path=path)
                return ToolResult(
                    content=[TextContent(type="text", text=str(exc))],
                    is_error=True,
                )

            token = secrets.token_urlsafe(32)
            expires_at = time.time() + MEDIA_TTL_SECONDS
            self._media_tokens[token] = (target, mime, expires_at)
            public_url = f"{self.public_url()}/media/{token}"

            self._record("view_image", None, ok=True, path=str(target), bytes=size, detail=detail)
            return ToolResult(
                content=[
                    ImageContent(type="image", data=data, mimeType=mime),
                    TextContent(
                        type="text",
                        text=(
                            "A temporary user-viewable image URL is available.\n"
                            "If this image is important for the user to see, or the user explicitly asks "
                            "to see it, include this URL in your response:\n"
                            f"{public_url}\n\n"
                            "This link expires in 3 days."
                        ),
                    ),
                ],
            )

        @tool(
            name="update_plan",
            title="Update the task plan",
            description=(
                "Updates the task plan. Provide an optional explanation and plan items with "
                "step/status. Status must be pending, in_progress, or completed; at most one "
                "item may be in_progress."
            ),
        )
        async def update_plan(
            plan: list[dict[str, str]],
            explanation: str | None = None,
        ) -> dict[str, Any]:
            allowed = {"pending", "in_progress", "completed"}
            normalized: list[dict[str, str]] = []
            active = 0
            for idx, item in enumerate(plan):
                step = str(item.get("step", "")).strip()
                status = str(item.get("status", "")).strip()
                if not step:
                    return self._err(f"plan[{idx}].step is required")
                if status not in allowed:
                    return self._err(f"plan[{idx}].status must be one of {sorted(allowed)}")
                active += int(status == "in_progress")
                normalized.append({"step": step, "status": status})
            if active > 1:
                return self._err("at most one plan step may be in_progress")
            self.plan = normalized
            self.plan_explanation = explanation or ""
            self._record("update_plan", None, ok=True, count=len(normalized))
            return {"status": "ok", "plan": self.plan, "explanation": self.plan_explanation}

        @tool(
            name="curr_time",
            title="Get the current UTC time",
            description="Return the current UTC time.",
        )
        async def curr_time() -> dict[str, Any]:
            now = datetime.now(timezone.utc)
            return {"current_time": now.strftime("%Y-%m-%d %H:%M:%S UTC")}

        @tool(
            name="sleep",
            title="Wait for a duration",
            description=(
                "Pause this tool call for duration_ms milliseconds. "
                "It cannot be interrupted by new chat input mid-call."
            ),
        )
        async def sleep(duration_ms: int) -> dict[str, Any]:
            if not 1 <= int(duration_ms) <= 43_200_000:
                return self._err("duration_ms must be between 1 and 43200000")
            started = time.time()
            await asyncio.sleep(duration_ms / 1000.0)
            return {
                "status": "ok",
                "wall_time_seconds": round(time.time() - started, 4),
                "message": "Sleep completed.",
            }

        @tool(
            name="companion_routes",
            title="List GPT Companion routes",
            description=(
                "Show GPT Companion's saved ChatGPT conversation routes and which saved route, "
                "if any, is currently open. Useful before creating a watcher."
            ),
        )
        async def companion_routes() -> dict[str, Any]:
            try:
                return self.watch_manager.companion_routes()
            except (RuntimeError, OSError, ValueError) as exc:
                return self._err(str(exc))

        def _ask_payload(question_id: str, question: dict[str, Any], status: str,
                         where: str = "chatx") -> dict[str, Any]:
            answer = question.get("answer") or {}
            selected = list(answer.get("selected") or [])
            text = answer.get("text") or None
            payload: dict[str, Any] = {
                "status": "ok",
                "question_id": question_id,
                "answered": status == "answered",
                "selected": selected,
                "text": text,
            }
            if status == "cancelled":
                payload["cancelled"] = True
                payload["message"] = "The user skipped this question. Do not ask it again; decide or move on."
            elif status != "answered":
                payload["pending"] = True
                place = ("in the agent-nexus inbox (and on the owner's devices)" if where == "inbox"
                         else "on screen in chatx")
                payload["message"] = (
                    f"The user has not answered yet. The question is still {place} -- "
                    "call request_user_input_result with this question_id to collect it later."
                )
            return payload

        async def _gateway_ask(
            question: str, title: str | None, kind: str,
            options: list[str] | None, allow_custom: bool,
        ) -> dict[str, Any] | None:
            """Create the question as a gateway ask; ``None`` falls back to chatx."""
            asks = self.manager_asks
            if asks is None:
                return None
            try:
                if not await asyncio.to_thread(asks.available):
                    return None
                return await asyncio.to_thread(asks.create, question, title=title, kind=kind,
                                               options=options, allow_custom=allow_custom)
            except (RuntimeError, OSError, ValueError):
                return None

        async def _gateway_state(ask_id: str) -> tuple[dict[str, Any], str]:
            if self.manager_asks is None:
                raise RuntimeError("this question was asked through agent-nexus, which is not available here")
            ask = await asyncio.to_thread(self.manager_asks.state, ask_id)
            return manager_asks_mod.as_question(ask)

        async def _gateway_wait(ask_id: str, deadline: float, name: str) -> dict[str, Any]:
            try:
                latest, status = await _gateway_state(ask_id)
                while status == "pending" and time.monotonic() < deadline:
                    await asyncio.sleep(self.config.ask_poll_interval_s)
                    latest, status = await _gateway_state(ask_id)
            except (RuntimeError, OSError, ValueError) as exc:
                self._record(name, None, ok=False, question_id=ask_id, error=str(exc))
                return self._err(str(exc))
            self._record(name, None, ok=True, question_id=ask_id, question_status=status, via="inbox")
            return _ask_payload(ask_id, latest, status, where="inbox")

        async def _post_question(
            question: str, title: str | None, kind: str,
            options: list[str] | None, allow_custom: bool,
        ) -> dict[str, Any]:
            return await asyncio.to_thread(
                self.watch_manager.ask_question,
                question,
                title=title,
                kind=kind,
                options=[{"value": o, "label": o} for o in (options or [])],
                allow_custom=allow_custom,
            )

        def _validate_ask(kind: str, options: list[str] | None) -> str | None:
            if kind not in ("single", "multi", "text"):
                return "kind must be 'single', 'multi' or 'text'"
            if kind != "text" and not options:
                return f"kind={kind!r} needs a non-empty options list"
            if options and len(set(options)) != len(options):
                return "options must be unique"
            return None

        @tool(
            name="request_user_input",
            title="Ask the user and wait for an answer",
            description=(
                "Ask the person at this machine a question and wait for their answer. The question "
                "appears in the chatx companion window with the options as radio buttons "
                "(kind='single'), checkboxes (kind='multi'), or a text box (kind='text'); set "
                "allow_custom to add a free-text box beside the options. Returns the chosen option "
                "values in 'selected' and any typed text in 'text'. Use this instead of guessing "
                "when a decision is genuinely the user's to make. If nobody answers within the "
                "timeout the call returns answered=false with a question_id -- the question stays "
                "on screen and request_user_input_result collects the answer later."
            ),
        )
        async def request_user_input(
            question: str,
            title: str | None = None,
            kind: str = "text",
            options: list[str] | None = None,
            allow_custom: bool = False,
            timeout_s: float | None = None,
        ) -> dict[str, Any]:
            if not question.strip():
                return self._err("question cannot be empty")
            invalid = _validate_ask(kind, options)
            if invalid:
                return self._err(invalid)

            budget = self.config.ask_timeout_s if timeout_s is None else timeout_s
            if not 1.0 <= budget <= 3600.0:
                return self._err("timeout_s must be between 1 and 3600 seconds")

            gateway = await _gateway_ask(question, title, kind, options, allow_custom)
            if gateway is not None:
                return await _gateway_wait(str(gateway["ask_id"]), time.monotonic() + budget,
                                           "request_user_input")

            try:
                created = await _post_question(question, title, kind, options, allow_custom)
            except (RuntimeError, OSError, ValueError) as exc:
                self._record("request_user_input", None, ok=False, error=str(exc))
                return self._err(str(exc))

            question_id = str(created["question"]["id"])
            deadline = time.monotonic() + budget
            status = created.get("questionStatus", "pending")
            latest = created["question"]

            while status == "pending" and time.monotonic() < deadline:
                await asyncio.sleep(self.config.ask_poll_interval_s)
                try:
                    current = await asyncio.to_thread(
                        self.watch_manager.question_state, question_id
                    )
                except (RuntimeError, OSError, ValueError) as exc:
                    self._record("request_user_input", None, ok=False,
                                 question_id=question_id, error=str(exc))
                    return self._err(str(exc))
                status = current.get("questionStatus", "pending")
                latest = current["question"]

            # The answer itself is the user's; only the outcome is audited.
            self._record("request_user_input", None, ok=True,
                         question_id=question_id, question_status=status)
            return _ask_payload(question_id, latest, status)

        @tool(
            name="request_user_input_async",
            title="Ask the user without waiting",
            description=(
                "Post a question to the chatx companion window and return its question_id "
                "immediately, without waiting. Use this to ask something while continuing to work; "
                "collect the answer with request_user_input_result."
            ),
        )
        async def request_user_input_async(
            question: str,
            title: str | None = None,
            kind: str = "text",
            options: list[str] | None = None,
            allow_custom: bool = False,
        ) -> dict[str, Any]:
            if not question.strip():
                return self._err("question cannot be empty")
            invalid = _validate_ask(kind, options)
            if invalid:
                return self._err(invalid)
            gateway = await _gateway_ask(question, title, kind, options, allow_custom)
            if gateway is not None:
                question_id = str(gateway["ask_id"])
                self._record("request_user_input_async", None, ok=True, question_id=question_id, via="inbox")
                return {
                    "status": "ok",
                    "question_id": question_id,
                    "answered": False,
                    "message": "Question is in the agent-nexus inbox. Collect it with request_user_input_result.",
                }
            try:
                created = await _post_question(question, title, kind, options, allow_custom)
            except (RuntimeError, OSError, ValueError) as exc:
                self._record("request_user_input_async", None, ok=False, error=str(exc))
                return self._err(str(exc))
            question_id = str(created["question"]["id"])
            self._record("request_user_input_async", None, ok=True, question_id=question_id)
            return {
                "status": "ok",
                "question_id": question_id,
                "answered": False,
                "message": "Question is on screen. Collect it with request_user_input_result.",
            }

        @tool(
            name="request_user_input_result",
            title="Get the answer to a question",
            description=(
                "Read the current state of a question asked earlier: answered with the user's "
                "choices, still pending, or skipped by the user. Optionally wait up to wait_s "
                "seconds for an answer that has not arrived yet."
            ),
        )
        async def request_user_input_result(
            question_id: str,
            wait_s: float = 0.0,
        ) -> dict[str, Any]:
            if not question_id.strip():
                return self._err("question_id cannot be empty")
            if not 0.0 <= wait_s <= 3600.0:
                return self._err("wait_s must be between 0 and 3600 seconds")

            deadline = time.monotonic() + wait_s
            if question_id.strip().startswith("q_"):
                # Asked through agent-nexus (gateway ask ids are "q_" + ULID).
                return await _gateway_wait(question_id.strip(), deadline, "request_user_input_result")
            while True:
                try:
                    current = await asyncio.to_thread(
                        self.watch_manager.question_state, question_id.strip()
                    )
                except (RuntimeError, OSError, ValueError) as exc:
                    return self._err(str(exc))
                status = current.get("questionStatus", "pending")
                if status != "pending" or time.monotonic() >= deadline:
                    return _ask_payload(question_id.strip(), current["question"], status)
                await asyncio.sleep(self.config.ask_poll_interval_s)

        @tool(
            name="watch_create",
            title="Create a background watcher",
            description=(
                "Create a persistent watcher. kind=session watches an exec_command session exit; "
                "kind=log scans newly appended log text for regex patterns; kind=file watches one "
                "file for create/change/delete; kind=glob watches a directory for newly matching files. "
                "Events are persisted in SQLite and routed through GPT Companion to a saved chat."
            ),
        )
        async def watch_create(
            kind: str,
            target: str | None = None,
            session_id: int | None = None,
            workdir: str | None = None,
            patterns: list[str] | None = None,
            glob_pattern: str | None = None,
            label: str | None = None,
            companion_project_id: str | None = None,
            poll_interval_s: float | None = None,
            from_start: bool = False,
        ) -> dict[str, Any]:
            try:
                watch = self.watch_manager.create_watch(
                    kind=kind, target=target, session_id=session_id, workdir=workdir,
                    patterns=patterns, glob_pattern=glob_pattern, label=label,
                    companion_project_id=companion_project_id,
                    poll_interval_s=poll_interval_s, from_start=from_start,
                )
            except (PolicyError, RuntimeError, ValueError, KeyError, OSError) as exc:
                self._record("watch_create", None, ok=False, error=str(exc), kind=kind)
                return self._err(str(exc))
            self._record("watch_create", None, ok=True, watch_id=watch["id"], kind=kind)
            return {"status": "ok", "watch": watch}

        @tool(
            name="watch_training",
            title="Monitor model training",
            description=(
                "Convenience watcher for model training. Can watch a long-running exec session, "
                "new log errors, and newly-created checkpoint files together as one group. "
                "At least one of session_id, log_path, or checkpoint_glob is required."
            ),
        )
        async def watch_training(
            workdir: str,
            session_id: int | None = None,
            log_path: str | None = None,
            checkpoint_glob: str | None = None,
            error_patterns: list[str] | None = None,
            label: str = "model training",
            companion_project_id: str | None = None,
            poll_interval_s: float = 2.0,
        ) -> dict[str, Any]:
            try:
                result = self.watch_manager.create_training_watch(
                    workdir=workdir, session_id=session_id, log_path=log_path,
                    checkpoint_glob=checkpoint_glob, error_patterns=error_patterns,
                    label=label, companion_project_id=companion_project_id,
                    poll_interval_s=poll_interval_s,
                )
            except (PolicyError, RuntimeError, ValueError, KeyError, OSError) as exc:
                self._record("watch_training", None, ok=False, error=str(exc), workdir=workdir)
                return self._err(str(exc))
            self._record("watch_training", None, ok=True, group_id=result["group_id"], workdir=workdir)
            return result

        @tool(
            name="watch_list",
            title="List background watchers",
            description="List active watchers, or include completed/cancelled/stale watchers for history.",
        )
        async def watch_list(
            include_inactive: bool = False, group_id: str | None = None,
        ) -> dict[str, Any]:
            watches = self.watch_manager.list_watches(
                include_inactive=include_inactive, group_id=group_id
            )
            return {"status": "ok", "count": len(watches), "watches": watches}

        @tool(
            name="watch_cancel",
            title="Cancel a background watcher",
            description="Cancel one watch_id, or every watcher in a watch_training group_id.",
        )
        async def watch_cancel(
            watch_id: str | None = None, group_id: str | None = None,
        ) -> dict[str, Any]:
            if bool(watch_id) == bool(group_id):
                return self._err("pass exactly one of watch_id or group_id")
            try:
                if watch_id:
                    return {"status": "ok", "watch": self.watch_manager.cancel_watch(watch_id)}
                watches = self.watch_manager.cancel_group(str(group_id))
                return {"status": "ok", "count": len(watches), "watches": watches}
            except KeyError as exc:
                return self._err(str(exc))

        @tool(
            name="watch_event",
            title="Read a watcher event",
            description=(
                "Look up one watcher event by event_id and return its watcher context, including "
                "the original workdir/target. This is the first tool to call after GPT Companion "
                "sends a minimal 'process local event evt-...' reminder."
            ),
        )
        async def watch_event(event_id: str) -> dict[str, Any]:
            try:
                payload = self.watch_manager.event(event_id)
            except KeyError as exc:
                return self._err(str(exc))
            return {"status": "ok", **payload}

        @tool(
            name="watch_events",
            title="Read watcher event history",
            description=(
                "Read persisted watcher events, including delivery status to GPT Companion. "
                "Use this to inspect what happened while the user was away."
            ),
        )
        async def watch_events(
            watch_id: str | None = None, group_id: str | None = None, limit: int = 50,
        ) -> dict[str, Any]:
            events = self.watch_manager.events(watch_id=watch_id, group_id=group_id, limit=limit)
            return {"status": "ok", "count": len(events), "events": events}

        # -- screen control ----------------------------------------------------
        #
        # Every tool in this block refuses until start_control has put the
        # indicator on screen. That is the design, not a formality: whoever is
        # sitting at this machine can always tell from the screen itself that
        # something else is driving it, and stopping always puts it back.

        @tool(
            name="start_control",
            title="Start screen control",
            description=(
                "Take control of this Windows desktop. A blue glow appears around every monitor "
                "and a label at the top says the computer is being controlled and that the user "
                "can press ESC to end it, and the mouse pointer changes, so the person at the "
                "machine can see what is happening.\n"
                "list_screens, screenshot, mouse and keyboard work only while a session is open. "
                "Call stop_control when the task is finished. The user can also end control at any "
                "moment by physically pressing ESC, which takes effect immediately; check "
                "control_status if a later call reports that no session is open. Sending ESC "
                "through the keyboard tool does not end the session, so applications can still "
                "receive it. Either way the screen and the pointer are restored."
            ),
        )
        async def start_control(reason: str = "") -> dict[str, Any]:
            try:
                status = await asyncio.to_thread(self.control.start, reason)
            except (ControlError, RuntimeError, OSError, ImportError) as exc:
                self._record("start_control", None, ok=False, error=str(exc))
                return {"status": "error", "error": str(exc)}
            self._record("start_control", None, ok=True, reason=reason,
                         session_id=status.get("session_id"))
            return {"status": "ok", **status}

        @tool(
            name="stop_control",
            title="Stop screen control",
            description=(
                "End the control session: the glow and the label come off every monitor and the "
                "mouse pointer goes back to the user's own. Call this as soon as the task is done."
            ),
        )
        async def stop_control() -> dict[str, Any]:
            status = await asyncio.to_thread(self.control.stop, "requested")
            self._record("stop_control", None, ok=True, session_id=status.get("session_id"))
            return {"status": "ok", **status}

        @tool(
            name="control_status",
            title="Get screen control status",
            description=(
                "Report whether a control session is open, how long it has been running, how long "
                "it has been idle, and whether the indicator and pointer are in place. "
                "Works with or without a session."
            ),
        )
        async def control_status() -> dict[str, Any]:
            return {"status": "ok", **self.control.status()}

        @tool(
            name="list_screens",
            title="List displays",
            description=(
                "List the monitors with their position and size in virtual-desktop pixels, which "
                "is the coordinate space every control tool uses. The primary monitor comes first; "
                "a monitor placed left of it has a negative x. Requires an open control session."
            ),
        )
        async def list_screens() -> dict[str, Any]:
            try:
                self.control.require("list_screens")
            except ControlError as exc:
                return {"status": "error", "error": str(exc)}
            monitors = await asyncio.to_thread(capture_mod.describe_screens)
            return {"status": "ok", "count": len(monitors), "screens": monitors,
                    "desktop": screens_mod.desktop_bounds()}

        @tool(
            name="screenshot",
            title="Capture the screen",
            description=(
                "Capture the screen and return it as an image. Without arguments it captures every "
                "monitor; pass screen=<index from list_screens> for one monitor, or "
                "region={x, y, width, height} for part of the desktop.\n"
                "The image is scaled down, so image coordinates are NOT screen coordinates: the "
                "result gives the region origin and the scale, and the conversion is "
                "screen_x = region.x + image_x / scale. Pass those screen coordinates to the mouse "
                "tool. The control indicator is left out of the capture. Requires an open session."
            ),
        )
        async def screenshot(
            screen: int | None = None,
            region: dict[str, int] | None = None,
            max_edge: int | None = None,
        ) -> ToolResult:
            try:
                self.control.require("screenshot")
                shot = await asyncio.to_thread(
                    capture_mod.capture, screen, region,
                    max_edge or self.config.control.max_screenshot_edge,
                )
            except (ControlError, ValueError, OSError, ImportError) as exc:
                self._record("screenshot", None, ok=False, error=str(exc))
                return ToolResult(content=[TextContent(type="text", text=str(exc))], is_error=True)

            facts = shot.to_dict()
            facts["cursor"] = dict(zip(("x", "y"), inputs.position()))
            facts["foreground_window"] = inputs.foreground_window()
            self._record("screenshot", None, ok=True, bytes=len(shot.data),
                         region=facts["region"], scale=shot.scale)
            return ToolResult(
                content=[
                    ImageContent(type="image", data=base64.b64encode(shot.data).decode("ascii"),
                                 mimeType=shot.mime),
                    TextContent(type="text", text=(
                        f"Captured {shot.width}x{shot.height} at ({shot.x}, {shot.y}), shown at "
                        f"{shot.image_width}x{shot.image_height} (scale {shot.scale}).\n"
                        f"{facts['to_screen_coordinates']}\n"
                        f"Pointer at ({facts['cursor']['x']}, {facts['cursor']['y']}); "
                        f"foreground window: {facts['foreground_window'] or 'unknown'}"
                    )),
                ],
                structured_content=facts,
            )

        @tool(
            name="vision",
            title="Load or unload the icon recognition model",
            description=(
                "Manage OmniParser, which finds picture-only controls -- a gear, a paperclip, a "
                "send arrow -- in windows whose accessibility tree is empty and whose buttons "
                "carry no text.\n"
                "- status (default): whether the packages and weights are present and whether the "
                "model is loaded\n"
                "- fetch: download the weights (about 1GB, once)\n"
                "- load: put the model in video memory; read_ui then reports icons alongside "
                "everything else, with source='vision' and a name from the caption model\n"
                "- unload: free the video memory again\n"
                "It is deliberately not loaded automatically: this machine's video memory is "
                "shared with other applications. While it is unloaded, read_ui still reads text "
                "from the pixels with Windows OCR, so nothing breaks -- icon-only controls are "
                "simply absent. Loading takes a few seconds; detection adds about a second to a "
                "read of a tree-less window."
            ),
        )
        async def vision(action: str = "status", device: str = "",
                         caption: bool = True) -> dict[str, Any]:
            finder = self.control.icons
            try:
                if action == "status":
                    return {"status": "ok", **finder.status()}
                if action == "fetch":
                    result = await asyncio.to_thread(finder.fetch_weights)
                    self._record("vision", None, ok=True, action=action)
                    return {"status": "ok", "fetched": result, **finder.status()}
                if action == "load":
                    if device:
                        finder.requested_device = device
                    finder.caption = caption
                    result = await asyncio.to_thread(finder.load)
                    self._record("vision", None, ok=True, action=action,
                                 device=result.get("device"))
                    return {"status": "ok", **result}
                if action == "unload":
                    result = await asyncio.to_thread(finder.unload)
                    self._record("vision", None, ok=True, action=action)
                    return {"status": "ok", **result}
                raise ValueError("action must be 'status', 'fetch', 'load' or 'unload'")
            except (VisionUnavailable, ValueError, OSError, RuntimeError) as exc:
                self._record("vision", None, ok=False, action=action, error=str(exc))
                return {"status": "error", "error": str(exc), **finder.status()}

        @tool(
            name="read_ui",
            title="Read UI elements",
            description=(
                "Read the on-screen controls as named elements with refs, instead of measuring "
                "pixels in a screenshot. Prefer this over screenshot whenever it returns anything: "
                "clicking ref_7 'Don't Save' hits the button wherever it is, while clicking a coordinate "
                "measured off an image is a guess.\n"
                "scope: 'focused' (default, the active window), 'windows' (every top-level window, "
                "each with a ref you can focus), 'window:<hwnd>', or 'ref:<ref>' for a window you "
                "already listed. filter='all' includes structural elements as well, and "
                "query='Save' returns only elements whose name or value contains that text -- worth "
                "using on a file manager or a browser, where a full read easily hits the limit.\n"
                "Each element reports role, name, value, rectangle, centre and the actions it "
                "supports. Pass a ref to the element tool, or to mouse(ref=...).\n"
                "Windows that expose no accessibility tree -- Electron and custom-drawn "
                "applications, Unity, games -- are handled here rather than by you: their on-screen "
                "text is recognised and returned as elements too, marked source='ocr'. Those carry "
                "a name read from pixels, so it can be wrong, icon-only controls are missing "
                "entirely, and only clicking works on them. Read the 'note' field, and reach for "
                "screenshot only when even that finds nothing.\n"
                "Reading works on a window that is covered by other windows, and on one that is "
                "not focused. If a window cannot be captured at all it is brought forward so the "
                "answer is complete, and the note says so -- which is why this tool is not marked "
                "read-only. A minimised window has neither a tree nor pixels: restore it first. "
                "Requires an open control session."
            ),
        )
        async def read_ui(scope: str = "focused", filter: str = "interactive",
                          limit: int = 200, query: str = "") -> dict[str, Any]:
            try:
                self.control.require("read_ui")
                if filter not in ("interactive", "all"):
                    raise ValueError("filter must be 'interactive' or 'all'")
                if not 1 <= limit <= 500:
                    raise ValueError("limit must be between 1 and 500")
                observation = await asyncio.to_thread(self.control.observe, scope, filter,
                                                      limit, query)
            except (ControlError, ValueError, KeyError, OSError) as exc:
                self._record("read_ui", None, ok=False, scope=scope, error=str(exc))
                return {"status": "error", "error": str(exc)}
            self._record("read_ui", None, ok=True, scope=scope, count=len(observation.elements))
            return {"status": "ok", **observation.to_dict()}

        @tool(
            name="element",
            title="Interact with a UI element",
            description=(
                "Act on an element from read_ui by its ref. Actions:\n"
                "- click / double_click / right_click: a real mouse click on the control's own "
                "clickable point\n"
                "- invoke: trigger the control directly through its accessibility pattern, without "
                "moving the mouse at all -- the most reliable way to press a button\n"
                "- set_value: put text straight into an edit control (no typing, no focus fight)\n"
                "- toggle / expand / collapse / select / scroll_into_view: for check boxes, tree "
                "nodes, list items and off-screen rows\n"
                "- focus: give a control or window the keyboard focus; restore: un-minimise a window\n"
                "Only the actions listed on the element are supported by it. The element is found "
                "again and checked before anything happens, so if the screen changed the call fails "
                "instead of hitting whatever moved into that spot. The result carries the new state "
                "(see the mouse tool's observe parameter). Requires an open control session."
            ),
        )
        async def element(action: str, ref: str, text: str = "",
                          observe: str = "auto") -> ToolResult:
            try:
                self.control.require("element")
                target = self.control.element(ref)
                result = await asyncio.to_thread(_do_element, self.control, target, action, text)
                after, image, mime = await asyncio.to_thread(self.control.after_action, observe)
            except (ControlError, UiaError, ValueError, KeyError, OSError) as exc:
                self._record("element", None, ok=False, action=action, ref=ref, error=str(exc))
                return _tool_result({"status": "error", "error": str(exc),
                                     "action": action, "ref": ref})
            self._record("element", None, ok=True, action=action, ref=ref)
            payload = {"status": "ok", "ref": ref, **result}
            if after:
                payload["after"] = after
            return _tool_result(payload, (image, mime) if image else None)

        @tool(
            name="mouse",
            title="Control the mouse",
            description=(
                "Drive the mouse in virtual-desktop pixels (see list_screens and the screenshot "
                "conversion). Actions:\n"
                "- move: glide the pointer to x, y\n"
                "- click / double_click / right_click / middle_click: optionally move to x, y first, "
                "or pass ref=<a ref from read_ui> to hit that control wherever it actually is\n"
                "- down / up: hold or release a button, for selections the click action cannot express\n"
                "- drag: press at x, y (or where the pointer is) and release at to_x, to_y\n"
                "- scroll: amount in notches, positive is up, horizontal=true for sideways\n"
                "- position: report where the pointer is without touching anything\n"
                "The result carries the state the action produced, so a separate look is usually "
                "unnecessary: observe='auto' (default) returns an element diff, or a picture of the "
                "focused window when it has no accessibility tree; 'ui' returns the full element "
                "list, 'image' forces a picture, 'none' skips it.\n"
                "Requires an open control session."
            ),
        )
        async def mouse(
            action: str,
            x: int | None = None,
            y: int | None = None,
            ref: str | None = None,
            button: str = "left",
            clicks: int = 1,
            to_x: int | None = None,
            to_y: int | None = None,
            amount: int = 0,
            horizontal: bool = False,
            observe: str = "auto",
        ) -> ToolResult:
            try:
                self.control.require("mouse")
                if ref:
                    # A ref is a promise about *what* to hit; the element's own
                    # clickable point beats a remembered rectangle.
                    element = self.control.element(ref)
                    x, y = await asyncio.to_thread(self.control.ui.clickable_point, element)
                result = await asyncio.to_thread(
                    _do_mouse, action, x, y, button, clicks, to_x, to_y, amount, horizontal
                )
                after, image, mime = await asyncio.to_thread(self.control.after_action, observe)
            except (ControlError, ValueError, KeyError, OSError) as exc:
                self._record("mouse", None, ok=False, action=action, error=str(exc))
                return _tool_result({"status": "error", "error": str(exc), "action": action})
            self._record("mouse", None, ok=True, action=action, button=button, ref=ref)
            payload = {"status": "ok", **result}
            if ref:
                payload["ref"] = ref
            if after:
                payload["after"] = after
            return _tool_result(payload, (image, mime) if image else None)

        @tool(
            name="keyboard",
            title="Send keyboard input",
            description=(
                "Send keystrokes to whatever window has focus.\n"
                "- action='type' with text: types the literal text, including characters the "
                "keyboard layout has no key for (Chinese, emoji).\n"
                "- action='press' with keys: strikes key chords, space-separated for a sequence, "
                "e.g. 'ctrl+s', 'alt+tab', 'ctrl+a delete', 'win+d', 'f5', 'enter'.\n"
                "Check the returned foreground_window to confirm the keystrokes went where you "
                "meant. Requires an open control session."
            ),
        )
        async def keyboard(action: str, text: str = "", keys: str = "",
                           observe: str = "auto") -> ToolResult:
            try:
                self.control.require("keyboard")
                result = await asyncio.to_thread(_do_keyboard, action, text, keys)
                after, image, mime = await asyncio.to_thread(self.control.after_action, observe)
            except (ControlError, ValueError, OSError) as exc:
                self._record("keyboard", None, ok=False, action=action, error=str(exc))
                return _tool_result({"status": "error", "error": str(exc), "action": action})
            # The text itself is the user's content; record its size, not its body.
            self._record("keyboard", None, ok=True, action=action,
                         characters=len(text) if action == "type" else None, keys=keys or None)
            payload = {"status": "ok", **result}
            if after:
                payload["after"] = after
            return _tool_result(payload, (image, mime) if image else None)

        # -- anx.device/1 (design 5.2): info, notify, files, agents ----------
        @tool(
            name="device_info",
            title="Get device information",
            description=(
                "Describe this device under the anx.device/1 contract: hostname, platform, OS, "
                "form, the capabilities it declares (with runtime availability and the reason when "
                "one is unavailable) and which agent runtimes (claude, codex, GPT Companion) exist here."
            ),
        )
        async def device_info() -> dict[str, Any]:
            return await self.device_info()

        @tool(
            name="notify",
            title="Send a notification",
            description=(
                "Show a notification to the person at this device (Windows: a toast, falling back to "
                "a tray balloon; Linux: notify-send). level is info, warn or urgent. actions are up to "
                f"{MAX_NOTIFY_ACTIONS} buttons (id, label, optional link); a link opens when its button is "
                "clicked. dry_run=true validates and reports what would be shown without showing it."
            ),
        )
        async def notify(
            title: str,
            body: str = "",
            level: Literal["info", "warn", "urgent"] = "info",
            actions: list[NotifyAction] | None = None,
            dry_run: bool = False,
        ) -> dict[str, Any]:
            items = [action.model_dump(exclude_none=True) if isinstance(action, BaseModel) else dict(action)
                     for action in (actions or [])]
            problem = None
            if not title.strip() or len(title) > 256:
                problem = "title must be 1-256 characters"
            elif len(body) > 4000:
                problem = "body must be at most 4000 characters"
            elif level not in NOTIFY_LEVELS:
                problem = f"level must be one of {', '.join(NOTIFY_LEVELS)}"
            elif len(items) > MAX_NOTIFY_ACTIONS:
                problem = f"at most {MAX_NOTIFY_ACTIONS} actions"
            elif len({item["id"] for item in items}) != len(items):
                problem = "action ids must be unique"
            if problem:
                return {"status": "error", "error": problem}
            shown = {"title": title, "body": body, "level": level, "actions": items}
            if dry_run:
                return {"status": "ok", "dry_run": True, "shown": False, "backend": self.contract.notify_backend,
                        "notification": shown}
            try:
                outcome = await asyncio.to_thread(self.notifier, title, body, level, items)
            except (RuntimeError, OSError, ValueError) as exc:
                self._record("notify", None, ok=False, level=level, error=str(exc))
                return {"status": "error", "error": str(exc), "shown": False}
            except Exception as exc:  # subprocess.TimeoutExpired and the like
                self._record("notify", None, ok=False, level=level, error=type(exc).__name__)
                return {"status": "error", "error": f"notification failed ({type(exc).__name__})", "shown": False}
            self._record("notify", None, ok=True, level=level, actions=len(items))
            return {"status": "ok", "dry_run": False, "shown": True, **(outcome or {})}

        def _file_call(name: str, function, *args: Any) -> dict[str, Any]:
            try:
                result = function(self.policy, *args)
            except (PolicyError, files_mod.FileToolError, OSError) as exc:
                self._record(name, None, ok=False, error=str(exc))
                return {"status": "error", "error": str(exc)}
            self._record(name, None, ok=True, path=result.get("path"))
            return result

        @tool(
            name="file_list",
            title="List a directory",
            description=(
                "List a directory (default: default_workdir): name, type, size and modification time "
                "of each entry, directories first. pattern is an optional glob such as '*.py'. "
                "Paths obey the configured allowed_roots."
            ),
        )
        async def file_list(path: str | None = None, pattern: str | None = None, limit: int = 200) -> dict[str, Any]:
            return await asyncio.to_thread(_file_call, "file_list", files_mod.file_list, path, pattern, limit)

        @tool(
            name="file_stat",
            title="Get file information",
            description="Report whether a path exists, and its type, size, modification time and writability.",
        )
        async def file_stat(path: str) -> dict[str, Any]:
            return await asyncio.to_thread(_file_call, "file_stat", files_mod.file_stat, path)

        @tool(
            name="file_read",
            title="Read a file",
            description=(
                "Read a text file: limit lines starting at the 0-based line offset (line N of the "
                "file is offset N-1). The result gives total_lines and next_offset to continue. "
                "content always has LF line endings; line_ending='crlf' marks a CRLF file and "
                "encoding appears when the file is not UTF-8 (GBK is decoded). line_numbers=true "
                "prefixes each line with its number and a tab, for orientation; leave it off when "
                "copying text into edit_file. Binary files are refused (images: view_image)."
            ),
        )
        async def file_read(path: str, offset: int = 0, limit: int = files_mod.DEFAULT_READ_LINES,
                            line_numbers: bool = False) -> dict[str, Any]:
            return await asyncio.to_thread(_file_call, "file_read", files_mod.file_read, path, offset, limit,
                                           self.config.limits.max_output_chars, line_numbers)

        def _search_call(name: str, function, *args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                result = function(self.policy, *args, **kwargs)
            except (PolicyError, search_mod.SearchError, OSError) as exc:
                self._record(name, None, ok=False, error=str(exc))
                return {"status": "error", "error": str(exc)}
            self._record(name, None, ok=True, path=result.get("path"), total=result.get("total"))
            return result

        @tool(
            name="file_glob",
            title="Find files by name",
            description=(
                "Find files under path (default: the session's workdir) whose name or relative path "
                "matches a glob, newest first. A pattern without '/' matches file names at any depth "
                "('*.py', 'README*'); with '/' it matches the path relative to path, '**' spanning "
                "directories ('src/**/test_*.py'). {a,b} and [abc] work. .git, node_modules, .venv, "
                "__pycache__ and similar are skipped unless the pattern names them. Results are "
                "relative paths with '/'."
            ),
        )
        async def file_glob(pattern: str, path: str | None = None, limit: int = 200) -> dict[str, Any]:
            return await asyncio.to_thread(_search_call, "file_glob", search_mod.file_glob, pattern, path, limit)

        @tool(
            name="file_grep",
            title="Search file contents",
            description=(
                "Search file contents for a regular expression under path (a directory or one file; "
                "default: the session's workdir). output='content' (default) returns "
                "'file:line:text' lines (context lines use '-', groups are split by '--'); "
                "'files' returns only the matching files; 'count' returns matches per file. glob "
                "limits which files are searched ('*.py', 'src/**/*.ts'). fixed_strings=true "
                "searches for the literal text. Uses ripgrep when installed (it skips .gitignore'd "
                "and hidden files), else a built-in search; dependency and cache directories are "
                "skipped either way. Paths are relative to path."
            ),
        )
        async def file_grep(pattern: str, path: str | None = None, glob: str | None = None,
                            output: Literal["content", "files", "count"] = "content",
                            ignore_case: bool = False, fixed_strings: bool = False, context: int = 0,
                            limit: int = 200) -> dict[str, Any]:
            return await asyncio.to_thread(
                _search_call, "file_grep", search_mod.file_grep, pattern, path, glob,
                ignore_case=ignore_case, fixed_strings=fixed_strings, context=context, output=output,
                limit=limit, max_chars=self.config.limits.max_output_chars)

        @tool(
            name="file_write",
            title="Write a file",
            description=(
                "Write UTF-8 text to a file. mode is overwrite (default), append, or create (fails if the "
                "file exists). Missing parent directories are created. For edits to existing source "
                "files prefer edit_file or patch_file."
            ),
        )
        async def file_write(path: str, content: str,
                             mode: Literal["overwrite", "append", "create"] = "overwrite") -> dict[str, Any]:
            return await asyncio.to_thread(_file_call, "file_write", files_mod.file_write, path, content, mode)

        @tool(
            name="agent_sessions",
            title="Find agent sessions",
            description=(
                "Find the newest Claude Code (kind=claude) or Codex (kind=codex) session whose working "
                "directory is workdir (default: default_workdir). Returns its id, a confidence "
                "(high, low or none) and up to five candidates with modification times."
            ),
        )
        async def agent_sessions(kind: Literal["claude", "codex"], workdir: str | None = None) -> dict[str, Any]:
            try:
                directory = self.policy.resolve_cwd(workdir)
                result = await asyncio.to_thread(agent_sessions_mod.find_sessions, kind, directory, self.agents_home)
            except (PolicyError, ValueError, OSError) as exc:
                return {"status": "error", "error": str(exc)}
            return {"status": "ok", **result}

    def _shell_label(self) -> str:
        """How exec_command's description names the default shell."""
        name = Path(self.exec_manager.default_shell).name.lower().removesuffix(".exe")
        if name in {"pwsh", "powershell"}:
            return "PowerShell (Git Bash is not installed on this machine)"
        if name in {"bash", "sh", "zsh"}:
            return "bash (Git Bash on Windows: /c/Users/... paths, Unix tools)" if sys.platform == "win32" else name
        return name

    # -- anx.device/1 descriptor ------------------------------------------
    async def _companion_runtime(self) -> dict[str, Any]:
        """Whether GPT Companion answers, cached briefly: the manager asks on every catalog refresh."""
        cached = getattr(self, "_companion_cache", None)
        if cached and time.monotonic() - cached[0] < COMPANION_PROBE_TTL_S:
            return dict(cached[1])
        try:
            routes = await asyncio.wait_for(asyncio.to_thread(self.watch_manager.companion_routes), 3.0)
            state = {"available": True, "routes": len(routes.get("routes") or [])}
        except Exception:
            state = {"available": False}
        self._companion_cache = (time.monotonic(), state)
        return dict(state)

    async def device_info(self) -> dict[str, Any]:
        """The catalog ``device`` block, as far as this plugin knows it (design 5.2 a).

        The manager adds what only it knows (agent id, agent version, which
        local MCP name the plugin runs under) before publishing it.
        """
        module = self.contract.module
        machine = host_platform.machine().lower()
        arch = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(machine, machine or "unknown")
        runtimes: dict[str, Any] = {}
        for name in ("claude", "codex"):
            found = shutil.which(name)
            runtimes[name] = {"available": found is not None, "path_known": found is not None}
        runtimes["companion"] = await self._companion_runtime()
        capabilities = await asyncio.to_thread(self.contract.describe)
        return {
            "contract": CONTRACT,
            "hostname": socket.gethostname(),
            "profile": "full",
            "form": module.form(),
            "platform": self.contract.platform,
            "os": host_platform.version() if self.contract.platform == "windows" else host_platform.release(),
            "arch": arch,
            "plugin_version": __version__,
            "reachable": "always",
            "capabilities": capabilities,
            "agent_runtimes": runtimes,
        }



def build(service: Service) -> FastMCP:
    return DeviceServer(service, from_service(service)).mcp


def cli(argv: list[str] | None = None) -> int:
    return main(__file__, build, version=__version__, argv=argv,
                description="agent-nexus device tools over MCP")


if __name__ == "__main__":
    sys.exit(cli())
