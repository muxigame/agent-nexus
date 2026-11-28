"""What this server does, as opposed to how it is reached.

Transport, public URL and authentication now come from mcpkit
(``agent-nexus.json`` → ``services.device``, or the legacy ``services.workspace``). What is left here is the
execution environment itself: the shell, the filesystem boundary, resource
ceilings, the companion and the watcher store. ``Config`` stays a frozen
dataclass because the policy, exec, watcher and audit modules take one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import sys
from pathlib import Path
from typing import Any

from mcpkit import Service
from pydantic import BaseModel, ConfigDict, Field

#: Command patterns refused outright unless ``allow_dangerous`` is on.
DEFAULT_COMMAND_DENY = [
    r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+/(?:\s|$)",  # rm -rf /
    r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+/\*",
    r":\(\)\s*\{.*\}\s*;\s*:",  # fork bomb
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\b[^\n]*\bof=/dev/[sh]d[a-z]",
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r"\bdiskpart\b",
    r"\bformat\s+[a-zA-Z]:",
    r"\bdel\s+/[sS]\s+/[qQ]\s+[a-zA-Z]:\\",
    r"\bRemove-Item\b[^\n]*-Recurse[^\n]*-Force[^\n]*\b[a-zA-Z]:\\\s*$",
    r"\breg\s+delete\b[^\n]*\bHKLM\b",
    r"\bvssadmin\s+delete\s+shadows\b",
    r"\bbcdedit\b",
    r"\bcipher\s+/w\b",
]


@dataclass(frozen=True)
class Limits:
    """Hard resource ceilings. All sizes are characters of captured output."""

    command_timeout_s: float = 120.0
    max_output_chars: int = 60_000
    max_sessions: int = 6
    max_buffer_chars: int = 200_000
    idle_session_ttl_s: float = 3600.0
    #: Finished sessions whose output was never collected are released after this.
    finished_session_ttl_s: float = 600.0
    default_wait_s: float = 30.0
    max_wait_s: float = 180.0
    #: Longest one tool call may block (exec_command, write_stdin, exec_wait,
    #: sleep, request_user_input). MCP clients cut a call at about 60 s, so a
    #: wait returns a resumable handle before that.
    call_wait_max_s: float = 50.0
    #: When a command emits more than ``max_output_chars``, the full text is
    #: spilled to a file so the model can read the rest instead of losing it.
    spill_enabled: bool = True
    max_spill_chars: int = 20_000_000
    #: Characters returned from each end when output is truncated.
    truncate_head_chars: int = 30_000
    truncate_tail_chars: int = 20_000


class LimitsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_timeout_s: float = 120.0
    max_output_chars: int = 60_000
    max_sessions: int = 6
    max_buffer_chars: int = 200_000
    idle_session_ttl_s: float = 3600.0
    finished_session_ttl_s: float = 600.0
    default_wait_s: float = 30.0
    max_wait_s: float = 180.0
    call_wait_max_s: float = Field(default=50.0, ge=1.0, le=170.0)
    spill_enabled: bool = True
    max_spill_chars: int = 20_000_000
    truncate_head_chars: int = 30_000
    truncate_tail_chars: int = 20_000


class ControlSettings(BaseModel):
    """``services.workspace.settings.control`` -- the screen-control session."""

    model_config = ConfigDict(extra="forbid")

    #: What the on-screen label says while control is active. The label also
    #: tells the user they can press ESC to end it, and that is enforced.
    banner_text: str = "ChatGPT 正在控制电脑"
    pulse: bool = True
    #: Both default to on: the indicator and the pointer are the user's only
    #: cue that something else is driving the machine.
    show_indicator: bool = True
    change_cursor: bool = True
    #: When a window exposes no accessibility tree, read its text with Windows'
    #: own OCR so read_ui still returns elements instead of an apology.
    use_ocr: bool = True
    #: OmniParser lives in this process and in its video memory, but only once
    #: the vision tool loads it; nothing is loaded at startup.
    vision_model_dir: str = "runtime/models/omniparser"
    vision_device: str = "auto"
    vision_caption: bool = True
    max_screenshot_edge: int = 1568


@dataclass(frozen=True)
class ControlConf:
    banner_text: str = "ChatGPT 正在控制电脑"
    pulse: bool = True
    show_indicator: bool = True
    change_cursor: bool = True
    use_ocr: bool = True
    vision_model_dir: str = "runtime/models/omniparser"
    vision_device: str = "auto"
    vision_caption: bool = True
    max_screenshot_edge: int = 1568


class DeviceSettings(BaseModel):
    """``services.workspace.settings``."""

    model_config = ConfigDict(extra="forbid")

    bash_path: str = r"C:\Program Files\Git\bin\bash.exe" if sys.platform == "win32" else "/bin/bash"
    #: Where a tool call starts when it omits workdir. A starting point, not a
    #: boundary: ``allowed_roots`` is the boundary.
    default_workdir: str = ""
    #: Roots a session's working directory must live under. Empty = no restriction.
    allowed_roots: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    #: Owner overrides for exec_command's named shells: {"bash": "D:\\Git\\bin\\bash.exe"}.
    shells: dict[str, str] = Field(default_factory=dict)
    command_deny: list[str] = Field(default_factory=lambda: list(DEFAULT_COMMAND_DENY))
    allow_dangerous: bool = False
    companion_url: str = "http://127.0.0.1:17891"
    watch_db: str = "logs/watchers.sqlite3"
    watch_poll_interval_s: float = 1.0
    watch_notify_timeout_s: float = 2.0
    #: How long request_user_input waits for a person before handing the caller
    #: back a question_id to poll. A human is slower than a process, but the
    #: calling MCP client has its own request timeout, so this stays well under
    #: the few minutes such clients typically allow.
    ask_timeout_s: float = 240.0
    ask_poll_interval_s: float = 0.75
    audit_log: str = "logs/audit.jsonl"
    audit_enabled: bool = True
    limits: LimitsSettings = Field(default_factory=LimitsSettings)
    control: ControlSettings = Field(default_factory=ControlSettings)


@dataclass(frozen=True)
class Config:
    bash_path: str = r"C:\Program Files\Git\bin\bash.exe" if sys.platform == "win32" else "/bin/bash"
    default_workdir: str = ""
    allowed_roots: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    shells: dict[str, str] = field(default_factory=dict)
    command_deny: list[str] = field(default_factory=lambda: list(DEFAULT_COMMAND_DENY))
    allow_dangerous: bool = False
    companion_url: str = "http://127.0.0.1:17891"
    watch_db: str = "logs/watchers.sqlite3"
    watch_poll_interval_s: float = 1.0
    watch_notify_timeout_s: float = 2.0
    ask_timeout_s: float = 240.0
    ask_poll_interval_s: float = 0.75
    limits: Limits = field(default_factory=Limits)
    control: ControlConf = field(default_factory=ControlConf)
    audit_log: str = "logs/audit.jsonl"
    audit_enabled: bool = True
    project_root: str = "."

    def control_marker(self) -> Path:
        """Records that the system cursors were swapped, so a killed process
        can still be cleaned up after on the next start."""
        return Path(self.project_root) / "runtime" / "control" / "cursors-replaced"

    def resolved_roots(self) -> list[Path]:
        return [Path(r).expanduser().resolve() for r in self.allowed_roots]

    def audit_path(self) -> Path:
        p = Path(self.audit_log)
        return p if p.is_absolute() else Path(self.project_root) / p

    def watch_db_path(self) -> Path:
        p = Path(self.watch_db)
        return p if p.is_absolute() else Path(self.project_root) / p


def from_service(service: Service) -> Config:
    settings = service.settings(DeviceSettings)
    return Config(
        bash_path=settings.bash_path,
        default_workdir=settings.default_workdir,
        allowed_roots=list(settings.allowed_roots),
        env=dict(settings.env),
        shells=dict(settings.shells),
        command_deny=list(settings.command_deny),
        allow_dangerous=settings.allow_dangerous,
        companion_url=settings.companion_url,
        watch_db=settings.watch_db,
        watch_poll_interval_s=settings.watch_poll_interval_s,
        watch_notify_timeout_s=settings.watch_notify_timeout_s,
        ask_timeout_s=settings.ask_timeout_s,
        ask_poll_interval_s=settings.ask_poll_interval_s,
        limits=Limits(**settings.limits.model_dump()),
        control=ControlConf(**settings.control.model_dump()),
        audit_log=settings.audit_log,
        audit_enabled=settings.audit_enabled,
        project_root=str(service.config.dir),
    )


def public_config(config: Config, service: Service | None = None) -> dict[str, Any]:
    """The policy in force, for reports: never a secret."""
    summary: dict[str, Any] = {
        "bash_path": config.bash_path,
        "shells": dict(config.shells),
        "default_workdir": config.default_workdir or str(Path.home()),
        "allowed_roots": config.allowed_roots or ["<unrestricted>"],
        "allow_dangerous": config.allow_dangerous,
        "audit_log": str(config.audit_path()),
        "companion_url": config.companion_url,
        "watch_db": str(config.watch_db_path()),
        "watch_poll_interval_s": config.watch_poll_interval_s,
        "ask_timeout_s": config.ask_timeout_s,
        "limits": {
            "command_timeout_s": config.limits.command_timeout_s,
            "max_output_chars": config.limits.max_output_chars,
            "max_sessions": config.limits.max_sessions,
            "max_buffer_chars": config.limits.max_buffer_chars,
            "idle_session_ttl_s": config.limits.idle_session_ttl_s,
            "finished_session_ttl_s": config.limits.finished_session_ttl_s,
            "call_wait_max_s": config.limits.call_wait_max_s,
        },
        "control": {
            "banner_text": config.control.banner_text,
            "show_indicator": config.control.show_indicator,
            "change_cursor": config.control.change_cursor,
        },
    }
    if service is not None:
        summary |= {
            "endpoint": service.config.resource or f"http://{service.config.host}:{service.config.port}{service.config.path}",
            "auth_mode": service.config.auth.mode,
        }
    return summary
