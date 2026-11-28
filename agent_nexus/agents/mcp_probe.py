"""Which Agent Nexus MCP connector a CLI already has (docs/design.md "本机 MCP", dedup).

A CLI may reach the gateway in several ways, and the 本机 MCP entry must never be added next to one of them (the
model would see two sets of the same tools):

* **local** -- an entry pointing at this console's ``http://127.0.0.1:<port>/mcp`` (ours is named ``agent-nexus``);
* **gateway_config** -- an entry in the CLI's own MCP config that is named like Agent Nexus or points at the gateway
  host (the gateway console's "在 AI 客户端中添加 Agent Nexus" card adds one named ``agent-nexus``);
* **gateway_account** -- a connector added in the vendor account and brought in by the CLI at run time: a claude.ai
  connector (``claude mcp list`` shows ``claude.ai <Name>: <url>``; its tools are ``mcp__claude_ai_<Name>__*``), or a
  ChatGPT app/connector used by Codex when it is signed in with ChatGPT (``codex app-server``: ``app/list``,
  ``mcpServerStatus/list``). Such a connector never shows in the config file.

:func:`resolve` combines the config files (read every time, cheap), the cached account answer (``<root>/.anx/
mcp-probe.json``, from an active probe, a passive observation of a run, or the owner's manual answer) into one
:class:`Resolution`. A chat run never waits for a probe: without an answer the account part is ``unknown``, and the
run's context then tells the model to use whichever Agent Nexus connector it has.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import registry

log = logging.getLogger(__name__)

NAME = "agent-nexus"
KINDS = ("claude", "codex", "gemini")
STATES = ("local", "gateway_config", "gateway_account", "both", "none", "unknown", "name_taken", "unsupported")
CACHE_TTL_S = 6 * 3600.0
CLAUDE_LIST_TIMEOUT_S = 30.0
CODEX_PROBE_TIMEOUT_S = 20.0
CODEX_METHODS = ("app/list", "mcpServerStatus/list")
#: The gateway's routing tools: a server offering one full set is an Agent Nexus connector whatever its name or
#: address (an open-source gateway runs under any domain, and a connector is named by whoever adds it). The legacy
#: names count too: an account connector keeps the tool list it was registered with.
SIGNATURES = (("device_list", "use_device"), ("workspace_list", "set_workspace"))
SIGNATURE = SIGNATURES[0]
#: ...plus one of the gateway's MCP-federation tools, so a look-alike device tool server does not count.
SIGNATURE_EXTRA = ("mcplist", "toolsearch", "tooluse", "mail_send")


def tool_group(name: str) -> tuple[str, str]:
    """``(connector, tool)`` of one tool name as a client lists it: ``workspace.device_list`` (Codex account
    apps), ``mcp__workspace__device_list`` (Claude), or a bare ``device_list``."""
    if name.startswith("mcp__") and name.count("__") >= 2:
        _, server, tool = name.split("__", 2)
        return server, tool
    if "." in name:
        server, tool = name.rsplit(".", 1)
        return server, tool
    return "", name


#: Tools every current gateway lists: a connector without them was registered with an older gateway, and the
#: vendor (ChatGPT) still serves that old tool list until the owner refreshes the connector.
CURRENT_TOOLS = ("device_list", "file_copy")


def stale_groups(names: list[str]) -> list[str]:
    """The Agent Nexus connectors among ``names`` whose tool list is out of date."""
    groups: dict[str, set[str]] = {}
    for name in names:
        server, tool = tool_group(str(name))
        groups.setdefault(server, set()).add(tool)
    return [g for g in nexus_groups(names) if not all(t in groups.get(g, set()) for t in CURRENT_TOOLS)]


def nexus_groups(names: list[str]) -> list[str]:
    """The connectors among ``names`` (tool names) that carry the gateway's signature ("" = ungrouped tools)."""
    groups: dict[str, set[str]] = {}
    for name in names:
        server, tool = tool_group(str(name))
        groups.setdefault(server, set()).add(tool)
    return [server for server, tools in groups.items()
            if any(all(t in tools for t in sig) for sig in SIGNATURES) and any(t in tools for t in SIGNATURE_EXTRA)]
WINDOWS = sys.platform == "win32"


def fold(text: Any) -> str:
    """Lower case without spaces, hyphens, underscores and dots: ``Agent Nexus`` / ``agent_nexus`` -> agentnexus."""
    return re.sub(r"[\s_.\-]+", "", str(text or "")).lower()


def entry_url(config: Any) -> str:
    """The URL of an MCP server entry (``url`` / ``httpUrl`` / ``serverUrl``), or ""."""
    if not isinstance(config, dict):
        return ""
    for key in ("url", "httpUrl", "serverUrl"):
        if isinstance(config.get(key), str):
            return config[key]
    return ""


def is_local_url(url: str, local_url: str) -> bool:
    """``url`` is this console's 本机 MCP (any loopback name, same port and path)."""
    if not url or not local_url:
        return False
    try:
        a, b = urlsplit(url), urlsplit(local_url)
        from ..local_mcp import is_loopback
        return is_loopback(a.hostname) and a.port == b.port and (a.path.rstrip("/") or "/") == b.path.rstrip("/")
    except ValueError:
        return False


def claude_prefix(name: str) -> str:
    """The tool prefix Claude Code gives a server (its own normalisation, claude.ai connectors included)."""
    normal = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    if name.startswith("claude.ai "):
        normal = re.sub(r"_+", "_", normal).strip("_")
    return f"mcp__{normal}__"


def tool_prefix(kind: str, name: str | None) -> str:
    if not name:
        return ""
    if kind == "claude":
        return claude_prefix(name)
    return f"{name}."  # codex / gemini show server.tool; only a hint for the model


def _claude_entries(home: Path, env: dict, workdir: str | None) -> list[tuple[str, Any]]:
    """Claude Code's servers that a run in ``workdir`` loads: the user scope (top-level ``mcpServers`` of
    ``.claude.json``), plus, for a given ``workdir``, its local scope (``projects[<workdir>].mcpServers``) and its
    project scope (``<workdir>/.mcp.json``). Another folder's entries never count: they are not loaded here."""
    def read_json(path: Path) -> Any:
        try:
            if path.stat().st_size > 32 * 1024 * 1024:
                return None
            return json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, ValueError):
            return None

    def servers(block: Any) -> list[tuple[str, Any]]:
        return [(str(k), v) for k, v in block.items()] if isinstance(block, dict) else []

    def norm(path: str) -> str:
        return os.path.normcase(os.path.normpath(path))

    found: list[tuple[str, Any]] = []
    config_dir = Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else None
    for path in ([config_dir / ".claude.json"] if config_dir else []) + [Path(home) / ".claude.json"]:
        data = read_json(path)
        if not isinstance(data, dict):
            continue
        found += servers(data.get("mcpServers"))
        projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
        if workdir:
            for key, project in projects.items():
                if isinstance(project, dict) and norm(str(key)) == norm(workdir):
                    found += servers(project.get("mcpServers"))
        break  # the first file that exists is the one Claude Code reads
    if workdir:
        data = read_json(Path(workdir) / ".mcp.json")
        if isinstance(data, dict):
            found += servers(data.get("mcpServers"))
    return found


def _is_gateway_entry(config: Any, url: str) -> bool:
    """An entry that looks like a gateway connector whatever its host (an IP, an alias, the public URL): a
    non-loopback URL, or a bearer header."""
    from ..local_mcp import is_loopback
    headers = config.get("headers") or config.get("http_headers") if isinstance(config, dict) else None
    if isinstance(headers, dict) and any(str(k).lower() == "authorization" for k in headers):
        return True
    if isinstance(config, dict) and (config.get("bearer_token_env_var") or config.get("bearer_token")):
        return True
    if url and "://" in url:
        with contextlib.suppress(ValueError):
            return not is_loopback(urlsplit(url).hostname)
    return False


def nexus_entries(kind: str, home: Path, env: dict, gateway_host: str, local_url: str,
                  workdir: str | None = None) -> dict | None:
    """``{"local": [names], "config": [names], "taken": bool}`` from ``kind``'s MCP config; None when unknown.
    Claude: only the user scope, plus ``workdir``'s own scopes when given (a run there)."""
    if kind == "claude":
        entries = _claude_entries(Path(home), env, workdir)
    else:
        entries = registry._mcp_entries(kind, Path(home), env)
    if entries is None:
        return None
    host = (gateway_host or "").lower()
    found: dict = {"local": [], "config": [], "taken": False}
    for name, config in entries:
        url = entry_url(config)
        text = (name + " " + json.dumps(config, ensure_ascii=False, default=str)).lower()
        if is_local_url(url, local_url):
            if name not in found["local"]:
                found["local"].append(name)
            continue
        gateway = bool(host) and host in text
        if name == NAME and not gateway and not _is_gateway_entry(config, url) and isinstance(config, dict):
            found["taken"] = True  # a loopback URL on another port, or a stdio server: not ours, not the gateway
            continue
        if (gateway or any(marker in text for marker in registry._NEXUS_MARKERS)) and name not in found["config"]:
            found["config"].append(name)
    return found


def config_mtime(kind: str, home: Path, env: dict) -> float:
    from .mcp_link import config_path
    path = config_path(kind, Path(home), env)
    try:
        return path.stat().st_mtime if path else 0.0
    except OSError:
        return 0.0


# -- active probes (never on chat open) --------------------------------------------------------------------------

def run_user(argv: list[str], env: dict, *, run_as: dict | None, timeout: float, stdin: bytes | None = None,
             cwd: str | None = None) -> tuple[int | None, str, str]:
    """``(exit code, stdout, stderr)`` of ``argv`` started as the run-as account (the Windows service: the user at
    the screen); ``(None, "", reason)`` when it could not run or timed out."""
    cwd = cwd or registry._home(env) or None
    try:
        if WINDOWS and run_as and run_as.get("session") is not None:  # pragma: no cover - Windows service
            from .runner import _session_popen
            proc = _session_popen(int(run_as["session"]), argv, cwd or "", env)
        else:
            extra: dict[str, Any] = {}
            if WINDOWS:
                extra["creationflags"] = registry.NO_WINDOW
            elif run_as and "user" in run_as:
                extra.update({key: run_as[key] for key in ("user", "group", "extra_groups") if key in run_as})
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    cwd=cwd, env=env, **extra)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return None, "", str(exc)
    out: list[bytes] = []
    err: list[bytes] = []

    def pump(stream, sink):
        with contextlib.suppress(Exception):
            for chunk in iter(lambda: stream.read(65536), b""):
                sink.append(chunk)

    threads = [threading.Thread(target=pump, args=(proc.stdout, out), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, err), daemon=True)]
    for thread in threads:
        thread.start()
    with contextlib.suppress(Exception):
        if stdin:
            proc.stdin.write(stdin)
        proc.stdin.close()
    try:
        code = proc.wait(timeout=timeout)
    except Exception:  # noqa: BLE001 - TimeoutExpired, or the Windows handle's own timeout
        with contextlib.suppress(Exception):
            proc.kill()
        return None, b"".join(out).decode("utf-8", "replace"), "timeout"
    for thread in threads:
        thread.join(2)
    return code, b"".join(out).decode("utf-8", "replace"), b"".join(err).decode("utf-8", "replace")


_CLAUDE_AI = re.compile(r"^claude\.ai (.+?): (\S+)")


def parse_claude_list(text: str, gateway_host: str) -> list[dict]:
    """The claude.ai connectors in ``claude mcp list`` output that are Agent Nexus (``[{name, url, prefix}]``)."""
    host = (gateway_host or "").lower()
    found = []
    for line in (text or "").splitlines():
        match = _CLAUDE_AI.match(line.strip())
        if not match:
            continue
        name, url = match.group(1), match.group(2)
        url_host = (urlsplit(url).hostname or "").lower() if "://" in url else ""
        if "agentnexus" in fold(name) or (host and url_host == host):
            full = f"claude.ai {name}"
            found.append({"name": full, "url": url, "prefix": claude_prefix(full)})
    return found


def probe_claude(binary: str, env: dict, run_as: dict | None, gateway_host: str) -> dict:
    """``{state: present|absent|unknown, connectors, error?}`` from ``claude mcp list`` (it health-checks every
    server, so up to 30 s)."""
    code, out, err = run_user(registry.runnable(binary, ["mcp", "list"]), env, run_as=run_as,
                              timeout=CLAUDE_LIST_TIMEOUT_S)
    if code is None or code != 0:
        return {"state": "unknown", "connectors": [], "error": (err or out or "failed")[-300:]}
    connectors = parse_claude_list(out, gateway_host)
    return {"state": "present" if connectors else "absent", "connectors": connectors}


def _codex_scan(value: Any, gateway_host: str, stale: set | None = None) -> list[str]:
    """Names of Agent Nexus apps / servers anywhere in an app-server answer (``stale`` collects the ones whose
    tool list is out of date)."""
    host = (gateway_host or "").lower()
    names: list[str] = []
    tools: set[str] = set()

    def walk(node: Any, owner: str | None = None) -> None:
        if isinstance(node, dict):
            if node.get("isAccessible") is False or node.get("is_accessible") is False or node.get("enabled") is False:
                return
            label = next((str(node[key]) for key in ("name", "title", "id") if isinstance(node.get(key), str)), None)
            for key in ("url", "serverUrl", "mcpUrl", "connectorUrl"):
                url = node.get(key)
                if isinstance(url, str) and host and (urlsplit(url).hostname or "").lower() == host:
                    names.append(label or "Agent Nexus")
            if label and "agentnexus" in fold(label):
                names.append(label)
            for key, child in node.items():
                if key == "tools" and isinstance(child, (dict, list)):
                    listed = child.keys() if isinstance(child, dict) else [
                        t.get("name") for t in child if isinstance(t, dict)]
                    found = [str(t) for t in listed if t]
                    old_ones = stale_groups(found)
                    for group in nexus_groups(found):
                        names.append(group or label or owner or "codex_apps")
                        if stale is not None and group in old_ones:
                            stale.add(group or label or owner or "codex_apps")
                walk(child, label or owner)
        elif isinstance(node, list):
            for item in node:
                walk(item, owner)

    walk(value)
    out: list[str] = []
    for name in names:
        if name not in out:
            out.append(name)
    return out


def codex_rpc(binary: str, env: dict, run_as: dict | None, calls: list[tuple[str, dict]], *,
              timeout: float = CODEX_PROBE_TIMEOUT_S, done: Any = None) -> dict:
    """``calls`` to ``codex app-server`` (stdio JSON-RPC) after its ``initialize``: ``{"answers": {index: message},
    "initialized": bool}`` or ``{"error": ...}``. ``done(answers)`` may end the wait early. Never raises."""
    from .. import __version__
    requests = [{"id": 1, "method": "initialize",
                 "params": {"clientInfo": {"name": "agent-nexus", "title": "Agent Nexus", "version": __version__}}},
                {"method": "initialized"}]
    requests += [{"id": 10 + n, "method": method, "params": params} for n, (method, params) in enumerate(calls)]
    argv = registry.runnable(binary, ["app-server"])
    cwd = registry._home(env) or None
    try:
        if WINDOWS and run_as and run_as.get("session") is not None:  # pragma: no cover - Windows service
            from .runner import _session_popen
            proc = _session_popen(int(run_as["session"]), argv, cwd or "", env)
        else:
            extra: dict[str, Any] = {}
            if WINDOWS:
                extra["creationflags"] = registry.NO_WINDOW
            elif run_as and "user" in run_as:
                extra.update({key: run_as[key] for key in ("user", "group", "extra_groups") if key in run_as})
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    cwd=cwd, env=env, **extra)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"error": str(exc)}
    lines: queue.Queue = queue.Queue()

    def pump():
        with contextlib.suppress(Exception):
            for raw in iter(proc.stdout.readline, b""):
                lines.put(raw)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    raw_answers: dict[int, dict] = {}
    deadline = time.monotonic() + timeout
    try:
        def write(message: dict) -> None:
            proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            proc.stdin.flush()

        write(requests[0])
        sent = False
        while time.monotonic() < deadline:
            try:
                raw = lines.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                break
            if raw is None:
                break
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, dict) or not isinstance(message.get("id"), int) or "method" in message:
                continue
            raw_answers[message["id"]] = message
            if message["id"] == 1 and not sent:
                for request in requests[1:]:
                    write(request)
                sent = True
            answers = {i - 10: m for i, m in raw_answers.items() if i >= 10}
            if len(answers) == len(calls) or (done is not None and done(answers)):
                break
    except (OSError, ValueError) as exc:
        return {"error": str(exc)}
    finally:
        with contextlib.suppress(Exception):
            proc.stdin.close()
        with contextlib.suppress(Exception):
            proc.kill()
    return {"answers": {i - 10: m for i, m in raw_answers.items() if i >= 10}, "initialized": 1 in raw_answers}


def probe_codex(binary: str, env: dict, run_as: dict | None, gateway_host: str,
                timeout: float = CODEX_PROBE_TIMEOUT_S) -> dict:
    """Ask ``codex app-server`` (stdio JSON-RPC) for the account's apps and the MCP server status.

    The method names are not assumed: each candidate is sent, and one the server does not know (a JSON-RPC error)
    simply does not count. Seen in codex-cli 0.164.0's binary: ``app/list`` and ``mcpServerStatus/list``. No
    answer to either → ``unknown``."""
    status_at = CODEX_METHODS.index("mcpServerStatus/list")

    def found(answers: dict) -> bool:
        status = answers.get(status_at)
        # found: app/list (a full catalogue, ~1 min on a busy account) is not needed
        return bool(status and "result" in status and _codex_scan(status["result"], gateway_host))

    out = codex_rpc(binary, env, run_as, [(method, {}) for method in CODEX_METHODS], timeout=timeout, done=found)
    if "error" in out:
        return {"state": "unknown", "connectors": [], "error": out["error"]}
    answers = out["answers"]
    results = [answers[n].get("result") for n in range(len(CODEX_METHODS)) if n in answers and "result" in answers[n]]
    if not results:
        return {"state": "unknown", "connectors": [],
                "error": "codex app-server 没有回答" if not out["initialized"] else "codex app-server 不支持查询连接器"}
    stale: set = set()
    connectors = _codex_scan(results, gateway_host, stale)
    return {"state": "present" if connectors else "absent",
            "connectors": [{"name": name, "prefix": "", "stale": name in stale} for name in connectors]}


def codex_models(binary: str, env: dict, run_as: dict | None, timeout: float = CODEX_PROBE_TIMEOUT_S) -> dict:
    """The models this Codex account may use (``codex app-server`` ``model/list``, §16.7):
    ``{"models": [{id, label, desc, default?}], "source": "cli"}`` or ``{"error": ...}``."""
    out = codex_rpc(binary, env, run_as, [("model/list", {"includeHidden": False, "limit": 100})], timeout=timeout)
    if "error" in out:
        return {"error": out["error"]}
    answer = out["answers"].get(0) or {}
    result = answer.get("result") if isinstance(answer.get("result"), dict) else None
    if result is None:
        return {"error": "codex app-server 没有回答" if not out["initialized"] else "codex app-server 不支持列出模型"}
    models = []
    for item in result.get("data") or []:
        if not isinstance(item, dict) or item.get("hidden"):
            continue
        model_id = str(item.get("model") or item.get("id") or "")
        if not registry.model_ok(model_id):
            continue
        entry = {"id": model_id, "label": str(item.get("displayName") or model_id),
                 "desc": str(item.get("description") or "")}
        if item.get("isDefault"):
            entry["default"] = True
        models.append(entry)
    return {"models": models, "source": "cli"}


def codex_models_cache(home: Path, env: dict) -> list[dict]:
    """Codex's own ``models_cache.json`` (CODEX_HOME, not a credential), read leniently: ``[{id, label, desc}]``."""
    base = Path(env.get("CODEX_HOME") or Path(home) / ".codex")
    try:
        data = json.loads((base / "models_cache.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    items = data.get("models") if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = items.get("data") or items.get("models")
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("hidden") or item.get("visibility") in ("hide", "hidden"):
            continue
        model_id = str(item.get("slug") or item.get("model") or item.get("id") or "")
        if registry.model_ok(model_id) and all(m["id"] != model_id for m in out):
            out.append({"id": model_id, "label": str(item.get("display_name") or item.get("displayName") or model_id),
                        "desc": str(item.get("description") or "")})
    return out


# -- the cache ---------------------------------------------------------------------------------------------------

class Probe:
    """The account-level answers per (account, kind), in ``<root>/.anx/mcp-probe.json``."""

    def __init__(self, root: Path):
        self.path = Path(root) / ".anx" / "mcp-probe.json"
        self._lock = threading.Lock()
        self.on_change: list = []

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + ".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(temp, self.path)
        except OSError as exc:
            log.warning("mcp probe: cannot save %s: %s", self.path, exc)

    @staticmethod
    def key(account: str | None, kind: str) -> str:
        return f"{account or ''}|{kind}"

    def get(self, account: str | None, kind: str) -> dict | None:
        with self._lock:
            return self._load().get(self.key(account, kind))

    def put(self, account: str | None, kind: str, entry: dict) -> None:
        with self._lock:
            data = self._load()
            before = data.get(self.key(account, kind))
            entry = {**entry, "checked_at": entry.get("checked_at") or time.time()}
            data[self.key(account, kind)] = entry
            self._save(data)
        if (before or {}).get("state") != entry.get("state") or \
                (before or {}).get("connectors") != entry.get("connectors"):
            for callback in list(self.on_change):
                with contextlib.suppress(Exception):
                    callback()

    def account_answer(self, account: str | None, kind: str, *, version: str | None) -> dict | None:
        """The cached account-level answer while valid (same CLI version, under 6 h; a manual one always)."""
        entry = self.get(account, kind)
        if not isinstance(entry, dict):
            return None
        if entry.get("source") == "user":
            return entry
        if entry.get("cli_version") and version and entry["cli_version"] != version:
            return None
        if time.time() - float(entry.get("checked_at") or 0) > CACHE_TTL_S:
            return None
        return entry

    def observe(self, account: str | None, kind: str, connectors: list[dict], *, version: str | None = None,
                source: str = "run") -> None:
        """A run saw what the CLI really loaded (passive, authoritative): overwrite the cached answer."""
        current = self.get(account, kind) or {}
        if current.get("source") == "user":
            return  # the owner's own answer stays
        known = [c for c in current.get("connectors") or [] if isinstance(c, dict) and c.get("name")]
        if known and connectors and all(str(c.get("name") or "") == "codex_apps" for c in connectors):
            # a run only sees the server that carries the app ("codex_apps"): keep the app's own name and the
            # stale flag the app-server scan found, or the stale note and the run's choice would be lost
            connectors = known
        self.put(account, kind, {"state": "present" if connectors else "absent", "connectors": connectors,
                                 "source": source, "cli_version": version or current.get("cli_version")})

    def manual(self, account: str | None, kind: str, connected: bool | None) -> None:
        if connected is None:
            with self._lock:
                data = self._load()
                data.pop(self.key(account, kind), None)
                self._save(data)
            return
        self.put(account, kind, {"state": "present" if connected else "absent", "source": "user",
                                 "connectors": [{"name": "Agent Nexus", "prefix": ""}] if connected else []})


def resolve(kind: str, *, account: str | None, home: Path, env: dict, gateway_host: str, local_url: str,
            probe: Probe | None, version: str | None = None, workdir: str | None = None) -> dict:
    """The :class:`Resolution` of ``kind`` for ``account`` (a dict, also the console's CliMcpState)."""
    base = {"kind": kind, "account": account, "state": "unsupported", "use": None, "tool_prefix": "",
            "source": None, "also": [], "entry": None, "cli_version": version, "checked_at": None, "error": None}
    if kind not in KINDS:
        return base
    try:
        found = nexus_entries(kind, home, env, gateway_host, local_url, workdir)
    except Exception as exc:  # noqa: BLE001 - a broken config file
        found, base["error"] = None, str(exc)[:200]
    if found is None:
        return {**base, "state": "unknown"}
    from .mcp_link import config_path
    path = config_path(kind, Path(home), env)
    answer = probe.account_answer(account, kind, version=version) if probe is not None and kind != "gemini" else None
    account_hits = (answer or {}).get("connectors") or [] if (answer or {}).get("state") == "present" else []
    names: list[tuple[str, str, str]] = []  # (name, source, kind of match)
    for name in found["local"]:
        names.append((name, f"config:{path}", "local"))
    for name in found["config"]:
        names.append((name, f"config:{path}", "config"))
    account_source = "user" if (answer or {}).get("source") == "user" else \
        ("account:claude.ai" if kind == "claude" else "account:chatgpt")
    for hit in account_hits:
        names.append((str(hit.get("name") or "Agent Nexus"), account_source, "account"))
    base["checked_at"] = (answer or {}).get("checked_at")
    has_local = any(n[2] == "local" for n in names)
    others = [n for n in names if n[2] != "local"]
    if has_local and others:
        state = "both"
    elif has_local:
        state = "local"
    elif any(n[2] == "config" for n in others):
        state = "gateway_config"
    elif others:
        state = "gateway_account"
    elif found["taken"]:
        state = "name_taken"
    elif kind == "gemini" or (answer or {}).get("state") == "absent":
        state = "none"
    else:
        state = "unknown"
    # One connector to use: ours when present (no owner token, attributed to this device, known name), else the
    # config entry, else the account connector.
    ordered = sorted(names, key=lambda n: {"local": 0, "config": 1, "account": 2}[n[2]])
    use = ordered[0] if ordered else None
    if use is not None:
        base.update(use=use[0], source=use[1])
        hit = next((h for h in account_hits if h.get("name") == use[0]), None)
        base["stale"] = bool(use[2] == "account" and (hit or {}).get("stale"))
        base["tool_prefix"] = (hit or {}).get("prefix") or tool_prefix(kind, use[0])
        if kind == "codex" and use[2] == "account" and use[0] in ("codex_apps", "Agent Nexus"):
            base["tool_prefix"] = "codex_apps"  # the app's own name is not known: only the server that carries it
        base["also"] = [n[0] for n in ordered[1:]]
    return {**base, "state": state, "account_answer": (answer or {}).get("state"),
            "others_config": any(n[2] == "config" for n in others),
            "config_path": str(path) if path else None}
