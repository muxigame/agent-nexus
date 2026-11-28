"""The agent CLIs the chat can drive (docs/agent-chat.md §2, §13): what each one is, where it is installed,
which version, and whether it is signed in.

* :func:`definitions` -- the built-in ``claude`` / ``codex`` / ``dsh`` / ``gemini`` plus ``agents.custom``
  from ``agent-nexus.json`` (override fields of a built-in, add a new CLI, or ``"enabled": false`` to hide one).
* :func:`locate` -- the binary as the run-as user would run it: their PATH, then the places installers put it
  that are on no PATH (``agent_cli.candidates``: npm/nvm/volta/bun folders, ``~/.local/bin``, ``%APPDATA%\\npm``,
  the Codex desktop app...), then npm's global prefix (``npm config get prefix``).
* :func:`probe` -- ``{installed, path, version, login, login_detail}``; version and login run in parallel with a
  timeout and the answer is cached for :data:`PROBE_TTL` seconds (the Agent menu asks on every open).

``env`` is always the run-as user's environment (``accounts.effective(root, "terminal").env()``): HOME / USERPROFILE
decide whose credentials are looked at, PATH decides which binary is found.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from .. import agent_cli

WINDOWS = sys.platform == "win32"
PROBE_TTL = 30.0
PREFIX_TTL = 600.0
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ADAPTERS = ("claude-stream", "codex-exec", "acp", "text", "jsonl")
#: Adapters a custom (non built-in) CLI may use: the other three need a CLI that speaks that protocol.
CUSTOM_ADAPTERS = ("text", "jsonl", "acp")
PERMISSION_MODES = ("ask", "edits", "readonly", "full")
LOGIN_STATES = ("ok", "missing", "unknown")
_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?(?:[-+._][0-9A-Za-z.]+)?")
_KIND = re.compile(r"[a-z][a-z0-9_-]{0,31}")
#: A model name a console may ask for (``agent_run_open`` / ``agent_run_send`` ``model``): its own argv item, never a
#: leading "-" (it must not read as an option), no spaces, quotes, ``%`` or ``!`` (cmd.exe shims).
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+=\[\]-]{0,99}")
MODEL_ERROR = "模型名只能包含字母、数字和 . _ - : / @ [ ] + =，最长 100 个字符"


def model_ok(value: Any) -> bool:
    """``value`` is a model name :data:`MODEL_RE` accepts."""
    return isinstance(value, str) and bool(MODEL_RE.fullmatch(value))


def os_key(platform: str | None = None) -> str:
    """``windows`` / ``darwin`` / ``linux``: the keys of :attr:`AgentDef.install`."""
    platform = platform or sys.platform
    return "windows" if platform == "win32" else "darwin" if platform == "darwin" else "linux"


@dataclass
class AgentDef:
    """One CLI. Command templates use ``{bin} {workdir} {resume_id} {prompt} {system_prompt}``;
    ``{options...}`` is where :func:`command_argv` puts the permission flags and the system-prompt arguments."""
    kind: str
    label: str
    binaries: tuple[str, ...]
    install: dict[str, list[str]] = field(default_factory=dict)  # {"windows"|"darwin"|"linux": argv}; argv[0] "npm"...
    version_args: tuple[str, ...] = ("--version",)
    adapter: str = "text"
    command: list[str] = field(default_factory=list)
    resume: bool = False
    approvals: bool = False
    permission_flags: dict[str, list[str]] = field(default_factory=dict)
    session_globs: list[str] = field(default_factory=list)
    #: Extra template pieces: a resumed turn's command (when it differs), arguments carrying the system prompt
    #: (inserted at ``{options...}``; empty = the prompt is prefixed to the first message), a degraded command.
    resume_command: list[str] = field(default_factory=list)
    system_prompt_args: list[str] = field(default_factory=list)
    fallback_command: list[str] = field(default_factory=list)
    #: The official installer when npm cannot be used: {"windows"|"darwin"|"linux": argv} and a line for people.
    install_alt: dict[str, list[str]] = field(default_factory=dict)
    install_hint: str = ""
    npm_package: str = ""
    node_min: int = 0  # Node.js major version the npm package needs
    #: {"method": "claude"|"codex"|"files", "env": [...], "files": [... relative to HOME], "otherwise": "unknown"}
    login_probe: dict[str, Any] = field(default_factory=dict)
    login_command: list[str] = field(default_factory=list)  # what "在终端登录" types
    stream: bool = True
    path: str = ""  # agents.custom.<kind>.path: an explicit binary
    #: Install places on no PATH (desktop apps), as templates: ``{HOME}``, ``{LOCALAPPDATA}``, ``{VAR:default}``
    #: (the variable, else ``default`` under HOME); keyed by OS like ``install``.
    extra_paths: dict[str, list[str]] = field(default_factory=dict)
    builtin: bool = True
    docs_url: str = ""
    #: How a model is chosen (§16.7): template pieces with ``{model}``, inserted at ``{options...}`` only when the
    #: run asks for a model (empty: the adapter chooses it over its protocol, or the CLI cannot), and the models
    #: the menu offers before the CLI tells (``[{id, label?, desc?}]``).
    model_args: list[str] = field(default_factory=list)
    model_hints: list[dict] = field(default_factory=list)

    def supports(self) -> dict[str, bool]:
        return {"resume": self.resume, "approvals": self.approvals, "stream": self.stream}

    def models(self) -> dict[str, Any]:
        """``{flag, protocol, hints}``: whether a model can be chosen by flag or over the CLI's protocol (ACP)."""
        return {"flag": bool(self.model_args), "protocol": self.adapter == "acp",
                "hints": [dict(h) for h in self.model_hints]}

    def view(self) -> dict[str, Any]:
        """What the console shows about the definition itself (no probing)."""
        return {"kind": self.kind, "label": self.label, "binaries": list(self.binaries), "adapter": self.adapter,
                "builtin": self.builtin, "supports": self.supports(), "npm_package": self.npm_package,
                "install_hint": self.install_hint, "login_command": list(self.login_command),
                "permissions": [mode for mode in PERMISSION_MODES if mode in self.permission_flags],
                "docs_url": self.docs_url, "models": self.models()}


def _npm(package: str) -> dict[str, list[str]]:
    argv = ["npm", "install", "-g", package]
    return {"windows": list(argv), "darwin": list(argv), "linux": list(argv)}


def _script(unix: str, windows: str) -> dict[str, list[str]]:
    return {"windows": ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", windows],
            "darwin": ["bash", "-c", unix], "linux": ["bash", "-c", unix]}


def builtin_definitions() -> dict[str, AgentDef]:
    claude = AgentDef(
        kind="claude", label="Claude Code", binaries=("claude",),
        install=_npm("@anthropic-ai/claude-code"), npm_package="@anthropic-ai/claude-code", node_min=18,
        install_alt=_script("curl -fsSL https://claude.ai/install.sh | bash", "irm https://claude.ai/install.ps1 | iex"),
        install_hint="也可以用官方安装脚本：macOS / Linux 执行 curl -fsSL https://claude.ai/install.sh | bash，"
                     "Windows 在 PowerShell 执行 irm https://claude.ai/install.ps1 | iex",
        adapter="claude-stream",
        command=["{bin}", "-p", "--output-format", "stream-json", "--verbose", "--input-format", "stream-json",
                 "{options...}"],
        resume_command=["{bin}", "-p", "--output-format", "stream-json", "--verbose", "--input-format", "stream-json",
                        "--resume", "{resume_id}", "{options...}"],
        system_prompt_args=["--append-system-prompt", "{system_prompt}"],
        # the Claude desktop app keeps its own Claude Code here (seen: 2.1.331 on macOS); it is a full CLI, signed
        # out until `claude auth login` -- found when no standalone claude is installed
        extra_paths={"darwin": ["{HOME}/Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude"],
                     "windows": ["{APPDATA}/Claude/claude-code/*/claude.exe"]},  # seen: 2.1.280 on Windows
        resume=True, approvals=True,
        # "ask": the runner adds --mcp-config with anx's approval server and fills {approval_tool} (§5).
        permission_flags={"ask": ["--permission-prompt-tool", "{approval_tool}"],
                          "edits": ["--permission-mode", "acceptEdits"],
                          "readonly": ["--permission-mode", "plan"],
                          "full": ["--dangerously-skip-permissions"]},
        session_globs=[".claude/projects/*/*.jsonl"],
        # `claude auth status` only from 2.1 on; an older claude would read "auth status" as a prompt.
        login_probe={"method": "claude", "env": ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"],
                     "files": [".claude/.credentials.json"], "command_min_version": "2.1.0"},
        login_command=["claude", "auth", "login"],
        docs_url="https://docs.anthropic.com/en/docs/claude-code",
        model_args=["--model", "{model}"],
        model_hints=[{"id": "sonnet", "label": "Sonnet", "desc": "日常编码"},
                     {"id": "opus", "label": "Opus", "desc": "更强，更慢"},
                     {"id": "haiku", "label": "Haiku", "desc": "最快"},
                     {"id": "opusplan", "label": "Opus Plan", "desc": "规划用 Opus，执行用 Sonnet"}],
    )
    codex = AgentDef(
        kind="codex", label="Codex", binaries=("codex",),
        install=_npm("@openai/codex"), npm_package="@openai/codex", node_min=16,
        install_alt=_script("curl -fsSL https://chatgpt.com/codex/install.sh | sh",
                            "irm https://chatgpt.com/codex/install.ps1 | iex"),
        install_hint="也可以用官方安装脚本：macOS / Linux 执行 curl -fsSL https://chatgpt.com/codex/install.sh | sh，"
                     "Windows 在 PowerShell 执行 irm https://chatgpt.com/codex/install.ps1 | iex",
        adapter="codex-exec",
        command=["{bin}", "exec", "--json", "--skip-git-repo-check", "-C", "{workdir}", "{options...}", "{prompt}"],
        resume_command=["{bin}", "exec", "--json", "--skip-git-repo-check", "-C", "{workdir}", "{options...}",
                        "resume", "{resume_id}", "{prompt}"],
        system_prompt_args=["-c", "developer_instructions={system_prompt}"],
        resume=True, approvals=False,  # exec mode cannot ask: the sandbox refuses what is out of bounds
        # `codex exec` has no --ask-for-approval: the same setting as a config override.
        permission_flags={"ask": ["--sandbox", "workspace-write", "-c", 'approval_policy="never"'],
                          "edits": ["--sandbox", "workspace-write"],
                          "readonly": ["--sandbox", "read-only"],
                          "full": ["--dangerously-bypass-approvals-and-sandbox"]},
        session_globs=[".codex/sessions/*/*/*/rollout-*.jsonl"],
        login_probe={"method": "codex", "env": ["CODEX_API_KEY"]},
        login_command=["codex", "login"],
        docs_url="https://github.com/openai/codex",
        # placed before `resume` by the template; the models come from the CLI itself (agent_models)
        model_args=["-m", "{model}"],
    )
    dsh = AgentDef(
        kind="dsh", label="DeepSeek Harness", binaries=("dsh",),
        install=_npm("@deepseek-ai/dsh"), npm_package="@deepseek-ai/dsh", node_min=22,
        install_hint="也可以安装 DeepSeek Harness 桌面版，它自带 dsh；用 npm 安装需要 Node.js 22 或更高版本（https://nodejs.org）",
        adapter="acp",
        command=["{bin}", "--profile", "acp", "{options...}"],
        # ACP failed to start: headless --json, one process per turn, resumed with --session-id (§14).
        fallback_command=["{bin}", "--profile", "headless", "--json", "{prompt}"],
        resume=True, approvals=True,
        permission_flags={"ask": []},
        session_globs=[".dsh/sessions/*/*/session.v*.jsonl.zstd",
                       ".dsh/storages/session_projcache/sessions/*.json"],
        # The desktop app's launcher is on no PATH (docs/agent-chat.md §14).
        extra_paths={"windows": [r"{LOCALAPPDATA}\Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd"],
                     "darwin": ["/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh",
                                "{HOME}/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"],
                     "linux": ["{HOME}/.local/share/deepseek-harness/resources/runtime/cli/bin/dsh",
                               "{HOME}/.local/share/DeepSeek Harness/resources/runtime/cli/bin/dsh",
                               "/opt/DeepSeek Harness/resources/runtime/cli/bin/dsh"]},
        # .credentials.yaml present = model credentials configured; its content is never read.
        login_probe={"method": "files", "env": ["DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"],
                     "files": ["{DSH_HOME:.dsh}/.credentials.yaml", "{DSH_HOME:.dsh}/settings.yaml"],
                     "otherwise": "unknown"},
        login_command=["dsh"],
    )
    gemini = AgentDef(
        kind="gemini", label="Gemini CLI", binaries=("gemini",),
        install=_npm("@google/gemini-cli"), npm_package="@google/gemini-cli", node_min=20,
        install_hint="Gemini CLI 用 npm 安装，需要 Node.js 20 或更高版本；macOS 也可以 brew install gemini-cli",
        adapter="acp",
        command=["{bin}", "--experimental-acp", "{options...}"],
        resume=False, approvals=True,
        permission_flags={"ask": [], "edits": ["--approval-mode", "auto_edit"], "full": ["--yolo"]},
        session_globs=[".gemini/tmp/*/chats/*.json", ".gemini/tmp/*/logs.json"],
        login_probe={"method": "files", "env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
                     "files": [".gemini/oauth_creds.json"], "otherwise": "missing",
                     "unknown_env": ["GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT"]},
        login_command=["gemini"],
        docs_url="https://github.com/google-gemini/gemini-cli",
        model_args=["-m", "{model}"],
        model_hints=[{"id": "auto", "label": "自动", "desc": "Gemini CLI 按任务选择"},
                     {"id": "pro", "label": "Pro"}, {"id": "flash", "label": "Flash"},
                     {"id": "flash-lite", "label": "Flash Lite"}],
    )
    return {d.kind: d for d in (claude, codex, dsh, gemini)}


# -- agents.custom -------------------------------------------------------------------------------------------

def _str_list(value: Any) -> list[str] | None:
    if isinstance(value, str) and value.strip():
        return [value]
    if isinstance(value, (list, tuple)) and value and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _argv_table(value: Any) -> dict[str, list[str]] | None:
    """``{"windows": [...], ...}``, or one argv used on every OS."""
    single = _str_list(value) if not isinstance(value, str) else None
    if single is not None:
        return {key: list(single) for key in ("windows", "darwin", "linux")}
    if isinstance(value, dict):
        table = {key: argv for key, argv in ((k, _str_list(v)) for k, v in value.items())
                 if key in ("windows", "darwin", "linux") and argv}
        return table or None
    return None


def _custom_section(cfg: dict | None) -> dict:
    if not isinstance(cfg, dict):
        return {}
    agents = cfg.get("agents") if isinstance(cfg.get("agents"), dict) else cfg
    custom = agents.get("custom") if isinstance(agents, dict) else None
    return custom if isinstance(custom, dict) else {}


def _apply(base: AgentDef, spec: dict) -> AgentDef:
    """``base`` with the well-formed fields of ``spec``; malformed fields are ignored, never fatal."""
    changes: dict[str, Any] = {}
    names = {f.name for f in fields(AgentDef)} - {"kind", "builtin"}
    for key, value in spec.items():
        if key not in names:
            continue
        if key in ("label", "install_hint", "npm_package", "path", "docs_url"):
            if isinstance(value, str):
                changes[key] = value.strip()
        elif key in ("binaries", "version_args"):
            items = _str_list(value) if key == "binaries" else (
                list(value) if isinstance(value, list) and all(isinstance(v, str) for v in value) else None)
            if items is not None:
                changes[key] = tuple(items)
        elif key == "model_hints":
            if isinstance(value, list):
                hints = []
                for item in value:
                    if isinstance(item, str):
                        item = {"id": item}
                    if isinstance(item, dict) and model_ok(item.get("id")):
                        hints.append({k: str(item[k])[:200] for k in ("id", "label", "desc")
                                      if isinstance(item.get(k), str) and item[k]})
                changes[key] = hints
        elif key in ("command", "resume_command", "system_prompt_args", "fallback_command", "session_globs",
                     "login_command", "model_args"):
            items = value if isinstance(value, list) and all(isinstance(v, str) for v in value) else None
            if items is not None:
                changes[key] = list(items)
        elif key == "extra_paths" or key in ("install", "install_alt"):
            table = _argv_table(value)
            if table is not None:
                changes[key] = table
        elif key == "adapter":
            allowed = ADAPTERS if base.builtin else CUSTOM_ADAPTERS
            if value in allowed:
                changes[key] = value
        elif key in ("resume", "approvals", "stream"):
            if isinstance(value, bool):
                changes[key] = value
        elif key == "node_min":
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 100:
                changes[key] = value
        elif key == "permission_flags":
            if isinstance(value, dict):
                flags = dict(base.permission_flags)
                for mode, argv in value.items():
                    if mode in PERMISSION_MODES and isinstance(argv, list) and all(isinstance(v, str) for v in argv):
                        flags[mode] = list(argv)
                changes[key] = flags
        elif key == "login_probe" and isinstance(value, dict):
            changes[key] = {k: v for k, v in value.items() if k in ("method", "env", "files", "otherwise",
                                                                     "unknown_env", "command_min_version")}
    return replace(base, **changes)


def definitions(cfg: dict | None = None) -> dict[str, AgentDef]:
    """The built-in CLIs with ``agents.custom`` applied. ``cfg`` is ``agent-nexus.json`` (or its ``agents`` part)."""
    defs = builtin_definitions()
    for kind, spec in _custom_section(cfg).items():
        if not isinstance(kind, str) or not _KIND.fullmatch(kind) or not isinstance(spec, dict):
            continue
        if spec.get("enabled") is False:
            defs.pop(kind, None)
            continue
        if kind in defs:
            defs[kind] = _apply(defs[kind], spec)
            continue
        base = AgentDef(kind=kind, label=kind, binaries=(kind,), builtin=False,
                        command=["{bin}", "{options...}", "{prompt}"], permission_flags={"ask": []},
                        login_probe={"method": "files", "otherwise": "unknown"})
        made = _apply(base, spec)
        if not made.binaries and not made.path:
            continue
        defs[kind] = made
    return defs


def load_config(root: Path) -> dict:
    """``agent-nexus.json`` of ``root`` (empty when missing or unreadable)."""
    from .. import paths
    try:
        data = json.loads(paths.config_path(Path(root)).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# -- commands ------------------------------------------------------------------------------------------------

def command_argv(d: AgentDef, binary: str, *, permission: str = "ask", values: dict[str, Any] | None = None,
                 template: str = "command") -> list[str]:
    """Fill a template (``command`` / ``resume_command`` / ``fallback_command``). ``{options...}`` becomes the
    permission flags, :attr:`AgentDef.model_args` when ``values["model"]`` is set, and, when ``values["system_prompt"]``
    is not empty, :attr:`AgentDef.system_prompt_args`.
    Placeholders without a value are left for the caller (``{approval_tool}``)."""
    values = {key: "" if value is None else str(value) for key, value in (values or {}).items()}
    base = getattr(d, template, None) or d.command
    options: list[str] = list(d.permission_flags.get(permission, []))
    if values.get("model"):
        options += d.model_args
    if values.get("system_prompt"):
        options += d.system_prompt_args
    argv: list[str] = []
    for part in base:
        if part == "{bin}":
            argv.append(binary)
        elif part == "{options...}":
            argv += [_fill(item, values) for item in options]
        else:
            argv.append(_fill(part, values))
    return argv


def _fill(part: str, values: dict[str, str]) -> str:
    return re.sub(r"\{([a-z_]+)\}", lambda m: values.get(m.group(1), m.group(0)), part)


def runnable(path: str, args: list[str] | tuple[str, ...] = ()) -> list[str]:
    """``[path, *args]`` as ``subprocess`` should get it. On Windows a ``.cmd`` / ``.bat`` (every npm shim) goes
    through ``cmd.exe /d /c``; only fixed, metacharacter-free arguments are passed this way (checked)."""
    argv = [path, *args]
    if WINDOWS and path.lower().endswith((".cmd", ".bat")):
        from ..link.wake.adapters import popen_args
        popen_args(argv)  # raises WakeError for anything cmd.exe would interpret
        return [os.environ.get("ComSpec") or "cmd.exe", "/d", "/c", *argv]
    return argv


def run_capture(argv: list[str], env: dict, *, timeout: float = 6.0, run_as: dict | None = None,
                cwd: str | None = None) -> tuple[int | None, str]:
    """(exit code, stdout + stderr) of a short command; (None, reason) when it could not run or timed out.
    ``run_as`` (``Account.popen_args()``) applies on POSIX; on Windows the user's env is enough for a probe."""
    extra: dict[str, Any] = {}
    if not WINDOWS and run_as and "user" in run_as:
        extra = {key: run_as[key] for key in ("user", "group", "extra_groups") if key in run_as}
    try:
        done = subprocess.run(argv, check=False, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env,
                              cwd=cwd or _home(env) or None, creationflags=NO_WINDOW, **extra)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return None, str(exc)
    text = (done.stdout or b"").decode("utf-8", "replace") + "\n" + (done.stderr or b"").decode("utf-8", "replace")
    return done.returncode, text


# -- locating ------------------------------------------------------------------------------------------------

def _home(env: dict) -> str:
    key = "USERPROFILE" if WINDOWS else "HOME"
    return env.get(key) or env.get("HOME") or ""


def _path_of(env: dict) -> str:
    return next((value for key, value in env.items() if key.upper() == "PATH"), "")


def expand(template: str, env: dict) -> str | None:
    """``{HOME}`` / ``{APPDATA}`` / ``{VAR:default}`` from the user's env (``default`` is under HOME); a path
    relative to HOME otherwise. None when a needed variable is missing."""
    home = _home(env)
    upper = {key.upper(): value for key, value in env.items() if value}
    missing = False

    def one(match: re.Match) -> str:
        nonlocal missing
        name, default = match.group(1), match.group(2)
        if name == "HOME" and home:
            return home
        if upper.get(name.upper()):
            return upper[name.upper()]
        if default is not None and home:
            return os.path.join(home, default)
        missing = True
        return ""

    text = re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::([^{}]*))?\}", one, template)
    if missing:
        return None
    if text.startswith("~"):
        text = home + text[1:] if home else os.path.expanduser(text)
    if not os.path.isabs(text) and not re.match(r"^[A-Za-z]:[\\/]", text):
        if not home:
            return None
        text = os.path.join(home, text)
    return text


def _usable(path: str) -> bool:
    try:
        return os.path.isfile(path) and (WINDOWS or os.access(path, os.X_OK))
    except OSError:
        return False


def find_tool(name: str, env: dict) -> str | None:
    """``name`` (npm, node, bash, powershell, a CLI) on the user's PATH or where installers put it."""
    if os.path.isabs(name):
        return name if _usable(name) else None
    path = shutil.which(name, path=_path_of(env) or os.defpath)
    if path:
        return path
    extra: list[str] = []
    if WINDOWS:
        program = env.get("ProgramW6432") or env.get("ProgramFiles") or os.environ.get("ProgramFiles") or ""
        if name in ("node", "npm") and program:
            extra.append(os.path.join(program, "nodejs", f"{name}.exe" if name == "node" else "npm.cmd"))
        if name == "powershell":
            root = env.get("SystemRoot") or os.environ.get("SystemRoot") or r"C:\Windows"
            extra.append(os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"))
    for candidate in extra + [path for path, _ in agent_cli.candidates(name, env)]:
        if _usable(candidate):
            return candidate
    return None


_prefix_cache: dict[tuple[str, str], tuple[float, str | None]] = {}
_prefix_lock = threading.Lock()


def npm_prefix(env: dict, *, timeout: float = 5.0) -> str | None:
    """npm's global prefix for this user: ``NPM_CONFIG_PREFIX``, ``prefix=`` in ``~/.npmrc``, else
    ``npm config get prefix`` (cached ten minutes)."""
    for key, value in env.items():
        if key.upper() == "NPM_CONFIG_PREFIX" and value:
            return value
    home = _home(env)
    if home:
        try:
            text = Path(home, ".npmrc").read_text(encoding="utf-8", errors="replace")
            match = re.search(r"^\s*prefix\s*=\s*(.+?)\s*$", text, re.MULTILINE)
            if match:
                value = match.group(1).strip("\"'").replace("${HOME}", home)
                return home + value[1:] if value.startswith("~") else value
        except OSError:
            pass
    npm = find_tool("npm", env)
    if not npm:
        return None
    key = (npm, home)
    with _prefix_lock:
        hit = _prefix_cache.get(key)
        if hit and time.monotonic() - hit[0] < PREFIX_TTL:
            return hit[1]
    try:
        code, out = run_capture(runnable(npm, ["config", "get", "prefix"]), env, timeout=timeout)
    except Exception:  # noqa: BLE001
        code, out = None, ""
    prefix = None
    if code == 0:
        line = next((ln.strip() for ln in out.splitlines() if ln.strip()), "")
        prefix = line if line and os.path.isabs(line) else None
    with _prefix_lock:
        _prefix_cache[key] = (time.monotonic(), prefix)
    return prefix


def prefix_bin(prefix: str) -> str:
    """Where ``npm i -g`` puts command shims under ``prefix``."""
    return prefix if WINDOWS else os.path.join(prefix, "bin")


def _version_key(path: str) -> list:
    return [(0, int(part), "") if part.isdigit() else (1, 0, part) for part in re.split(r"(\d+)", path)]


def _newest_glob(pattern: str) -> list[str]:
    """Paths matching ``pattern``, newest version first (``2.1.331`` before ``2.1.40``)."""
    import glob
    return [os.path.normpath(p) for p in sorted(glob.glob(pattern), key=_version_key, reverse=True)]


def locate(d: AgentDef, env: dict) -> str | None:
    """The binary of ``d`` for the user of ``env``, or None."""
    if d.path:
        path = os.path.expanduser(d.path)
        return path if _usable(path) else None
    for name in d.binaries:
        found = agent_cli.find(name, env)
        if found.get("available") and found.get("path"):
            return str(found["path"])
    for name in d.binaries:
        found_generic = find_tool(name, env)
        if found_generic:
            return found_generic
    for template in d.extra_paths.get(os_key(), []):
        path = expand(template, env)
        for candidate in (_newest_glob(path) if path and "*" in path else [path]):
            if candidate and _usable(candidate):
                return candidate
    prefix = npm_prefix(env)
    if prefix:
        folder = prefix_bin(prefix)
        for name in d.binaries:
            for candidate in ([f"{name}.cmd", f"{name}.exe", f"{name}.ps1"] if WINDOWS else [name]):
                path = os.path.join(folder, candidate)
                if not candidate.endswith(".ps1") and _usable(path):
                    return path
    return None


# -- probing -------------------------------------------------------------------------------------------------

def parse_version(text: str) -> str | None:
    for line in text.splitlines():
        match = _VERSION.search(line)
        if match:
            return match.group(0)[:40]
    return None


def _has_env(env: dict, names: list[str] | tuple[str, ...]) -> str | None:
    upper = {key.upper(): value for key, value in env.items()}
    return next((name for name in names if (upper.get(name.upper()) or "").strip()), None)


def _has_file(env: dict, names: list[str] | tuple[str, ...]) -> str | None:
    for name in names:
        path = expand(name, env)
        try:
            if path and os.path.isfile(path):
                return path
        except OSError:
            pass
    return None


def _login_static(spec: dict, env: dict) -> tuple[str, str] | None:
    """("ok", why) when an API key or a credentials file says so, else None."""
    key = _has_env(env, spec.get("env") or [])
    if key:
        return "ok", f"环境变量 {key}"
    path = _has_file(env, spec.get("files") or [])
    if path:
        return "ok", f"凭据文件 {os.path.basename(path)}"
    return None


def _claude_login(binary: str, spec: dict, env: dict, timeout: float, run_as: dict | None) -> tuple[str, str]:
    code, out = run_capture(runnable(binary, ["auth", "status"]), env, timeout=timeout, run_as=run_as)
    data = _json_in(out)
    if code is not None and data is not None:
        method = str(data.get("authMethod") or data.get("auth_method") or "")
        if code == 0 and data.get("loggedIn", True) is not False:
            return "ok", f"已登录（{method}）" if method else "已登录"
        static = _login_static(spec, env)
        return static if static else ("missing", "未登录")
    static = _login_static(spec, env)  # no JSON from `auth status`
    if static:
        return static
    if code is not None and code != 0 and re.search(r"not (logged|signed) in|log ?in required", out, re.IGNORECASE):
        return "missing", "未登录"
    return _login_without_command(spec, env)


def _codex_login(binary: str, spec: dict, env: dict, timeout: float, run_as: dict | None) -> tuple[str, str]:
    signed = agent_cli.codex_signed_in(env)
    if signed:
        return "ok", "已登录（auth.json）"
    static = _login_static(spec, env)
    if static:
        return static
    code, out = run_capture(runnable(binary, ["login", "status"]), env, timeout=timeout, run_as=run_as)
    if code == 0 and not re.search(r"not logged in", out, re.IGNORECASE):
        return "ok", "已登录"
    if code is not None and code != 0:
        return "missing", "未登录"
    return ("unknown", "凭据可能保存在系统钥匙串里，无法确认") if signed is None else ("missing", "未登录")


def _files_login(spec: dict, env: dict) -> tuple[str, str]:
    static = _login_static(spec, env)
    if static:
        return static
    if _has_env(env, spec.get("unknown_env") or []):
        return "unknown", "使用云平台凭据，无法确认"
    otherwise = spec.get("otherwise") if spec.get("otherwise") in LOGIN_STATES else "unknown"
    return otherwise, "没有找到 API 密钥或登录凭据" if otherwise == "missing" else "无法确认登录状态"


def _json_in(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def login_state(d: AgentDef, binary: str, env: dict, *, timeout: float = 6.0,
                run_as: dict | None = None) -> tuple[str, str]:
    spec = d.login_probe or {}
    method = spec.get("method")
    try:
        if method == "claude":
            return _claude_login(binary, spec, env, timeout, run_as)
        if method == "codex":
            return _codex_login(binary, spec, env, timeout, run_as)
        return _files_login(spec, env)
    except Exception as exc:  # noqa: BLE001 -- a probe never breaks the menu
        return "unknown", f"检测失败：{exc}"


_probe_cache: dict[tuple, tuple[float, dict]] = {}
_probe_lock = threading.Lock()


def _cache_key(d: AgentDef, env: dict) -> tuple:
    """What changes the answer: the binary search, HOME, and every variable a login check reads (the values
    only as hashes -- keys stay out of the cache's memory)."""
    names = {name.upper() for name in [*(d.login_probe.get("env") or []), *(d.login_probe.get("unknown_env") or [])]}
    names |= {m.upper() for f in d.login_probe.get("files") or [] for m in re.findall(r"\{([A-Za-z_]\w*)", f)}
    names |= {"CODEX_HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "NPM_CONFIG_PREFIX"}
    relevant = tuple(sorted((k.upper(), hash(v)) for k, v in env.items() if k.upper() in names))
    return (d.kind, d.path, tuple(d.binaries), _home(env), _path_of(env), relevant)


def invalidate(kind: str | None = None) -> None:
    """Forget cached probes (all, or one kind): after an install, a login, a config change."""
    with _probe_lock:
        for key in [k for k in _probe_cache if kind is None or k[0] == kind]:
            _probe_cache.pop(key, None)
    if kind is None:
        with _prefix_lock:
            _prefix_cache.clear()


def probe(d: AgentDef, env: dict, *, timeout: float = 6, fresh: bool = False, run_as: dict | None = None) -> dict:
    """``{installed, path, version, login, login_detail}``; ``login`` is ok / missing / unknown."""
    key = _cache_key(d, env)
    if not fresh:
        with _probe_lock:
            hit = _probe_cache.get(key)
            if hit and time.monotonic() - hit[0] < PROBE_TTL:
                return dict(hit[1])
    try:
        result = _probe(d, env, timeout, run_as)
    except Exception as exc:  # noqa: BLE001
        result = {"installed": False, "path": None, "version": None, "login": "unknown",
                  "login_detail": f"检测失败：{exc}"}
    with _probe_lock:
        _probe_cache[key] = (time.monotonic(), result)
    return dict(result)


def _at_least(version: str | None, minimum: str) -> bool:
    def parts(text: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", text)[:3])
    return bool(version) and parts(version or "") >= parts(minimum)


def _login_without_command(spec: dict, env: dict) -> tuple[str, str]:
    """Login state from API keys / credential files only (a CLI too old for its status command)."""
    static = _login_static(spec, env)
    if static:
        return static
    if sys.platform == "darwin":
        return "unknown", "凭据可能保存在钥匙串里，无法确认"
    return "missing", "没有找到登录凭据"


def _probe(d: AgentDef, env: dict, timeout: float, run_as: dict | None) -> dict:
    binary = locate(d, env)
    if not binary:
        return {"installed": False, "path": None, "version": None, "login": "unknown", "login_detail": "未安装"}
    out: dict[str, Any] = {}

    def version() -> None:
        code, text = run_capture(runnable(binary, list(d.version_args)), env, timeout=timeout, run_as=run_as)
        out["version"] = parse_version(text) if code is not None else None
        out["version_error"] = None if code is not None else ("版本检测超时" if text == "timeout" else "无法运行")

    def login() -> None:
        out["login"] = login_state(d, binary, env, timeout=timeout, run_as=run_as)

    gate = (d.login_probe or {}).get("command_min_version")
    if gate:  # the login command only exists from some version on: ask for the version first
        version()
        if not _at_least(out.get("version"), str(gate)):
            out["login"] = _login_without_command(d.login_probe or {}, env)
        else:
            login()
        threads: list[threading.Thread] = []
    else:
        threads = [threading.Thread(target=version, daemon=True), threading.Thread(target=login, daemon=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout * 2 + 2)
    state, detail = out.get("login") or ("unknown", "检测超时")
    result = {"installed": True, "path": binary, "version": out.get("version"), "login": state,
              "login_detail": detail}
    if out.get("version_error"):
        result["version_error"] = out["version_error"]
    return result


def probe_all(defs: dict[str, AgentDef], env: dict, *, timeout: float = 6, fresh: bool = False,
              run_as: dict | None = None) -> dict[str, dict]:
    """:func:`probe` for every definition, in parallel."""
    results: dict[str, dict] = {}

    def one(kind: str) -> None:
        results[kind] = probe(defs[kind], env, timeout=timeout, fresh=fresh, run_as=run_as)

    threads = [threading.Thread(target=one, args=(kind,), daemon=True) for kind in defs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout * 2 + 4)
    for kind in defs:
        results.setdefault(kind, {"installed": False, "path": None, "version": None, "login": "unknown",
                                  "login_detail": "检测超时"})
    return results


# -- the gateway MCP connector (gateway mode, docs/agent-chat.md §1) --------------------------------------------

_NEXUS_MARKERS = ("agent-nexus", "agent_nexus", "agentnexus")


def _mcp_entries(kind: str, home: Path, env: dict) -> list[tuple[str, Any]] | None:
    """``[(name, server config)]`` of the MCP servers ``kind`` is configured with, or None when unknown."""
    def read_json(path: Path) -> Any:
        try:
            if path.stat().st_size > 32 * 1024 * 1024:
                return None
            return json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, ValueError):
            return None

    def servers(block: Any) -> list[tuple[str, Any]]:
        return [(str(k), v) for k, v in block.items()] if isinstance(block, dict) else []

    if kind == "claude":
        found: list[tuple[str, Any]] = []
        config_dir = Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else None
        for path in ([config_dir / ".claude.json"] if config_dir else []) + [home / ".claude.json"]:
            data = read_json(path)
            if isinstance(data, dict):
                found += servers(data.get("mcpServers"))
                for project in (data.get("projects") or {}).values() if isinstance(data.get("projects"), dict) else []:
                    if isinstance(project, dict):
                        found += servers(project.get("mcpServers"))
        return found
    if kind == "codex":
        import tomllib
        root = Path(env["CODEX_HOME"]) if env.get("CODEX_HOME") else home / ".codex"
        try:
            data = tomllib.loads((root / "config.toml").read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, ValueError):
            return []
        return servers(data.get("mcp_servers"))
    if kind == "gemini":
        data = read_json(home / ".gemini" / "settings.json")
        return servers(data.get("mcpServers")) if isinstance(data, dict) else []
    return None  # dsh and custom CLIs: where they keep MCP servers is not known


def gateway_mcp_names(kind: str, home: Path, env: dict, gateway: str = "") -> list[str] | None:
    """The names of ``kind``'s Agent Nexus MCP connectors (named or pointing at agent-nexus, or at this device's
    gateway host); None when the CLI's MCP config is not known."""
    entries = _mcp_entries(kind, Path(home), env)
    if entries is None:
        return None
    from urllib.parse import urlsplit
    host = (urlsplit(gateway).hostname or "").lower() if gateway else ""
    names: list[str] = []
    for name, config in entries:
        text = (name + " " + json.dumps(config, ensure_ascii=False, default=str)).lower()
        if (any(marker in text for marker in _NEXUS_MARKERS) or (host and host in text)) and name not in names:
            names.append(name)
    return names


def gateway_mcp(kind: str, home: Path, env: dict, gateway: str = "") -> bool | None:
    """Whether ``kind`` has an Agent Nexus MCP connector (gateway mode needs one); None when unknown."""
    names = gateway_mcp_names(kind, home, env, gateway)
    return None if names is None else bool(names)


def info(d: AgentDef, probed: dict, install: dict | None = None) -> dict:
    """The ``agent_info`` entry of one CLI (§2)."""
    return {"kind": d.kind, "label": d.label, "installed": bool(probed.get("installed")),
            "path": probed.get("path"), "version": probed.get("version"), "login": probed.get("login", "unknown"),
            "login_detail": probed.get("login_detail", ""), "install": install, "supports": d.supports(),
            "adapter": d.adapter, "builtin": d.builtin, "login_command": list(d.login_command),
            "install_hint": d.install_hint,
            "permissions": [mode for mode in PERMISSION_MODES if mode in d.permission_flags],
            "models": d.models()}
