"""The connector registry (design §7.1, §7.2; docs/ui-spec.md §3.4).

One registry, on the gateway: ``connectors`` in ``gateway.sqlite3`` holds each
connector's definition **without** secrets. Secret values (a bearer token,
header values, an OAuth client secret) and OAuth tokens live in files under
``<state_dir>/connectors/<name>/`` (owner-only, written atomically) and are
write-only: no API response and no catalog entry ever contains them.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)

NAME_RULE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
HEADER_NAME = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,100}")
TRANSPORTS = ("auto", "http", "sse")
AUTH_TYPES = ("none", "bearer", "headers", "oauth")
EXPOSE = ("search", "direct")
MAX_HEADERS = 32
MAX_SECRET_CHARS = 16384
DEFAULT_TIMEOUT_S = 120
#: Names a connector may not take: ``hub/<name>`` must never shadow a gateway word.
RESERVED_NAMES = frozenset({"hub", "me", "all", "device", "workspace", "export", "import", "probe"})


class ConnectorError(Exception):
    """A readable refusal with a stable ``code`` and an HTTP status."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def body(self) -> dict:
        return {"error": self.message, "code": self.code}


def fingerprint(url: str | None) -> str | None:
    """``sha256(normalised URL)[:12]`` (design §7.2): scheme and host lower-cased, no trailing slash."""
    if not url:
        return None
    parts = urlsplit(url.strip())
    normal = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", parts.query, ""))
    return hashlib.sha256(normal.encode()).hexdigest()[:12]


def _text(value: Any, field: str, *, limit: int = 200, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise ConnectorError("invalid_input", f"{field} 不能为空")
        return None
    if not isinstance(value, str) or len(value) > limit or any(ord(ch) < 32 for ch in value):
        raise ConnectorError("invalid_input", f"{field} 必须是不超过 {limit} 个字符的单行文本")
    return value.strip()


def _secret(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > MAX_SECRET_CHARS or "\n" in value or "\r" in value:
        raise ConnectorError("invalid_input", f"{field} 必须是单行文本")
    return value.strip()


def valid_name(value: Any) -> str:
    if not isinstance(value, str) or not NAME_RULE.fullmatch(value):
        raise ConnectorError("invalid_name", "名称只能使用小写字母、数字和连字符（1-40 个字符，以字母或数字开头）")
    if value in RESERVED_NAMES:
        raise ConnectorError("reserved_name", f"名称 {value} 是保留字，换一个名称")
    return value


def valid_url(value: Any) -> str:
    url = _text(value, "url", limit=2048, required=True)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConnectorError("invalid_url", "地址必须是 http:// 或 https:// 开头的 MCP 地址")
    if parts.username or parts.password:
        raise ConnectorError("invalid_url", "地址里不能带用户名或密码；请改用 Bearer 或 Header 鉴权")
    if parts.fragment:
        raise ConnectorError("invalid_url", "地址不能包含 # 片段")
    return url


def _names(value: Any, field: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > 500 or not all(isinstance(item, str) and 0 < len(item) <= 200 for item in value):
        raise ConnectorError("invalid_input", f"{field} 必须是工具名列表")
    return [item.strip() for item in value]


def normalize(body: Any, existing: dict | None = None) -> tuple[dict, dict]:
    """Validate a ``ConnectorInput`` (create, or a partial update of ``existing``).

    Returns ``(config, secrets)``: the definition to store (no secret values)
    and the secret values given in this request (``None`` = keep, for updates).
    """
    if not isinstance(body, dict):
        raise ConnectorError("invalid_input", "请求体必须是 JSON 对象")
    base = dict(existing or {})
    creating = existing is None
    if creating:
        name = valid_name(body.get("name"))
    else:
        name = existing["name"]
        if "name" in body and body["name"] not in (None, name):
            raise ConnectorError("rename_unsupported", "不能修改名称；删除后用新名称重新添加")
    if body.get("command") not in (None, [], ""):
        raise ConnectorError("stdio_needs_device", "stdio 命令只能在设备上运行；固定到设备的连接器即将推出", 501)
    run_on = body.get("run_on", base.get("run_on", "gateway")) or "gateway"
    if run_on != "gateway":
        if isinstance(run_on, str) and run_on.startswith("device:"):
            raise ConnectorError("coming_soon", "固定到设备即将推出；现在请使用“网关”运行位置", 501)
        raise ConnectorError("invalid_input", "run_on 必须是 gateway")
    config = {
        "name": name,
        "title": _text(body["title"], "title", limit=64) if "title" in body else base.get("title"),
        "url": valid_url(body["url"]) if "url" in body or creating else base["url"],
        "transport": body.get("transport", base.get("transport", "auto")) or "auto",
        "run_on": "gateway",
        "expose": body.get("expose", base.get("expose", "search")) or "search",
        "timeout_s": body.get("timeout_s", base.get("timeout_s", DEFAULT_TIMEOUT_S)),
        "enabled": body.get("enabled", base.get("enabled", True)),
        "tools": base.get("tools") or {"allow": [], "deny": []},
    }
    if config["transport"] not in TRANSPORTS:
        raise ConnectorError("invalid_input", "transport 必须是 auto、http 或 sse")
    if config["expose"] not in EXPOSE:
        raise ConnectorError("invalid_input", "expose 必须是 search 或 direct")
    timeout = config["timeout_s"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 5 <= timeout <= 600:
        raise ConnectorError("invalid_input", "超时必须在 5 到 600 秒之间")
    config["timeout_s"] = int(timeout)
    if not isinstance(config["enabled"], bool):
        raise ConnectorError("invalid_input", "enabled 必须是 true 或 false")
    if "tools" in body:
        tools = body["tools"] or {}
        if not isinstance(tools, dict):
            raise ConnectorError("invalid_input", "tools 必须是 {allow, deny}")
        config["tools"] = {"allow": _names(tools.get("allow"), "tools.allow"), "deny": _names(tools.get("deny"), "tools.deny")}

    secrets: dict[str, Any] = {}
    old_auth = base.get("auth") or {"type": "none"}
    auth_in = body.get("auth") if "auth" in body else None
    if auth_in is None:
        config["auth"] = old_auth
        return config, secrets
    if not isinstance(auth_in, dict) or auth_in.get("type") not in AUTH_TYPES:
        raise ConnectorError("invalid_input", "auth.type 必须是 none、bearer、headers 或 oauth")
    kind = auth_in["type"]
    same = old_auth.get("type") == kind
    auth: dict[str, Any] = {"type": kind}
    if kind == "bearer":
        token = _secret(auth_in.get("token"), "token")
        if token is None and not (same and old_auth.get("secret_set")):
            raise ConnectorError("missing_secret", "Bearer 鉴权需要填写 Token")
        secrets["token"] = token
        auth["secret_set"] = True
    elif kind == "headers":
        given = auth_in.get("headers") or {}
        if not isinstance(given, dict) or not given or len(given) > MAX_HEADERS:
            raise ConnectorError("invalid_input", f"Header 鉴权需要 1-{MAX_HEADERS} 个 Header")
        headers = {}
        old_names = set(old_auth.get("header_names") or []) if same else set()
        for key, value in given.items():
            if not isinstance(key, str) or not HEADER_NAME.fullmatch(key.strip()):
                raise ConnectorError("invalid_input", f"Header 名称 {key!r} 不合法")
            key = key.strip()
            value = _secret(value, f"Header {key}")
            if value is None and key not in old_names:
                raise ConnectorError("missing_secret", f"Header {key} 需要填写值")
            headers[key] = value  # None = keep the stored value
        secrets["headers"] = headers
        auth["header_names"] = sorted(headers, key=str.lower)
        auth["secret_set"] = True
    elif kind == "oauth":
        scopes = auth_in.get("scopes", old_auth.get("scopes") if same else [])
        if isinstance(scopes, str):
            scopes = scopes.split()
        auth["scopes"] = _names(scopes, "scopes")
        client = auth_in.get("client") or (old_auth.get("client") if same else None) or {"mode": "dynamic"}
        if not isinstance(client, dict) or client.get("mode", "dynamic") not in ("dynamic", "static"):
            raise ConnectorError("invalid_input", "auth.client.mode 必须是 dynamic 或 static")
        if client.get("mode", "dynamic") == "static":
            client_id = _text(client.get("client_id"), "client_id", limit=500, required=True)
            secret = _secret(client.get("client_secret"), "client_secret")
            old_client = (old_auth.get("client") or {}) if same else {}
            kept = old_client.get("mode") == "static" and bool(old_client.get("secret_set"))
            auth["client"] = {"mode": "static", "client_id": client_id, "secret_set": bool(secret) or kept}
            # None keeps a stored secret; a static client without one is a public client.
            secrets["client_secret"] = secret if secret else (None if kept else "")
        else:
            auth["client"] = {"mode": "dynamic"}
    config["auth"] = auth
    return config, secrets


def public_view(config: dict) -> dict:
    """The definition without any secret value (``Connector`` minus live state)."""
    auth = dict(config.get("auth") or {"type": "none"})
    view_auth = {"type": auth.get("type", "none"), "secret_set": bool(auth.get("secret_set")),
                 "header_names": list(auth.get("header_names") or [])}
    if auth.get("type") == "oauth":
        client = dict(auth.get("client") or {"mode": "dynamic"})
        view_auth["scopes"] = list(auth.get("scopes") or [])
        view_auth["client"] = {key: client[key] for key in ("mode", "client_id", "secret_set") if key in client}
        view_auth["secret_set"] = bool(client.get("secret_set"))
    return {"name": config["name"], "title": config.get("title"), "url": config.get("url"), "command": None,
            "transport": config.get("transport", "auto"), "run_on": config.get("run_on", "gateway"),
            "run_on_display": "网关", "auth": view_auth, "source": "registry",
            "expose": config.get("expose", "search"), "timeout_s": config.get("timeout_s", DEFAULT_TIMEOUT_S),
            "enabled": bool(config.get("enabled", True)), "tools": config.get("tools") or {"allow": [], "deny": []},
            "mcp_id": f"hub/{config['name']}", "fingerprint": fingerprint(config.get("url"))}


def export_view(config: dict) -> dict:
    """design §7.2's registry entry (``anx connector export``): no secret values."""
    auth = config.get("auth") or {"type": "none"}
    entry_auth: dict[str, Any] = {"type": auth.get("type", "none")}
    if auth.get("type") == "headers":
        entry_auth["headers"] = {name: "" for name in auth.get("header_names") or []}
    if auth.get("type") == "oauth":
        entry_auth["scopes"] = list(auth.get("scopes") or [])
        client = auth.get("client") or {"mode": "dynamic"}
        entry_auth["client"] = {key: client[key] for key in ("mode", "client_id") if key in client}
    return {key: value for key, value in {
        "title": config.get("title"), "run_on": "gateway", "transport": config.get("transport", "auto"),
        "url": config.get("url"), "auth": entry_auth, "expose": config.get("expose", "search"),
        "session": "shared", "tools": config.get("tools") or {"allow": [], "deny": []},
        "timeout_s": config.get("timeout_s", DEFAULT_TIMEOUT_S), "enabled": bool(config.get("enabled", True)),
    }.items() if value is not None}


# -- secret files ---------------------------------------------------------------------

def _protect(path: Path) -> None:
    try:
        from mcpkit.config import protect
        protect(path)
    except Exception as exc:  # never fatal; the state directory itself is owner-only
        if os.name != "nt":
            with contextlib.suppress(OSError):
                os.chmod(path, 0o700 if path.is_dir() else 0o600)
        log.debug("could not restrict %s (%s)", path.name, type(exc).__name__)


class SecretStore:
    """``<state_dir>/connectors/<name>/{secrets,oauth}.json``; in memory without a state dir."""

    def __init__(self, base: Path | None):
        self.base = base
        self._memory: dict[tuple[str, str], dict] = {}
        self._lock = threading.RLock()

    def _path(self, name: str, kind: str) -> Path:
        return self.base / valid_name(name) / f"{kind}.json"

    def read(self, name: str, kind: str) -> dict:
        with self._lock:
            if self.base is None:
                return json.loads(json.dumps(self._memory.get((name, kind), {})))
            path = self._path(name, kind)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}
            except (OSError, ValueError):
                log.warning("connector %s: unreadable %s.json", name, kind)
                return {}
            return data if isinstance(data, dict) else {}

    def write(self, name: str, kind: str, data: dict) -> None:
        with self._lock:
            if self.base is None:
                self._memory[(name, kind)] = json.loads(json.dumps(data))
                return
            path = self._path(name, kind)
            fresh = not path.parent.exists()
            path.parent.mkdir(parents=True, exist_ok=True)
            if fresh:
                _protect(path.parent)
            handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{kind}.", suffix=".tmp")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False)
                if os.name != "nt":
                    os.chmod(temp, 0o600)
                os.replace(temp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temp)
                raise
            if os.name == "nt":
                _protect(path)

    def remove(self, name: str, kind: str | None = None) -> None:
        with self._lock:
            kinds = [kind] if kind else ["secrets", "oauth"]
            for item in kinds:
                if self.base is None:
                    self._memory.pop((name, item), None)
                    continue
                with contextlib.suppress(FileNotFoundError):
                    self._path(name, item).unlink()
            if kind is None and self.base is not None:
                with contextlib.suppress(OSError):
                    (self.base / name).rmdir()

    def apply(self, name: str, update: dict) -> None:
        """Merge the secret values of one create/update (``None`` keeps a value)."""
        if not update:
            return
        current = self.read(name, "secrets")
        if "token" in update:
            if update["token"] is not None:
                current["token"] = update["token"]
        if "headers" in update:
            old = current.get("headers") or {}
            current["headers"] = {key: (value if value is not None else old.get(key, ""))
                                  for key, value in update["headers"].items()}
        if "client_secret" in update and update["client_secret"] is not None:
            current["client_secret"] = update["client_secret"]
        self.write(name, "secrets", current)


# -- the table ------------------------------------------------------------------------

class Registry:
    """CRUD over the ``connectors`` table (schema migration 5)."""

    def __init__(self, store):
        self.store = store

    def _db(self):
        return self.store._connect()

    def list(self) -> list[dict]:
        with self.store._lock:
            rows = self._db().execute("SELECT config_json, cache_json, created_at, updated_at FROM connectors ORDER BY name").fetchall()
        return [self._row(row) for row in rows]

    def get(self, name: str) -> dict | None:
        with self.store._lock:
            row = self._db().execute("SELECT config_json, cache_json, created_at, updated_at FROM connectors WHERE name = ?",
                                     (name,)).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(row) -> dict:
        config = json.loads(row["config_json"])
        try:
            config["_cache"] = json.loads(row["cache_json"]) if row["cache_json"] else {}
        except ValueError:
            config["_cache"] = {}
        config["created_at"], config["updated_at"] = row["created_at"], row["updated_at"]
        return config

    def insert(self, config: dict) -> None:
        now = self.store.clock()
        with self.store._lock:
            try:
                self._db().execute("INSERT INTO connectors(name, config_json, cache_json, created_at, updated_at) VALUES (?,?,?,?,?)",
                                   (config["name"], json.dumps(self._clean(config), ensure_ascii=False), None, now, now))
            except Exception as exc:
                if "UNIQUE" in str(exc) or "PRIMARY" in str(exc):
                    raise ConnectorError("name_conflict", f"已经有名为 {config['name']} 的连接器", 409) from None
                raise

    def update(self, config: dict) -> None:
        with self.store._lock:
            self._db().execute("UPDATE connectors SET config_json = ?, updated_at = ? WHERE name = ?",
                               (json.dumps(self._clean(config), ensure_ascii=False), self.store.clock(), config["name"]))

    def save_cache(self, name: str, cache: dict) -> None:
        with self.store._lock:
            self._db().execute("UPDATE connectors SET cache_json = ? WHERE name = ?",
                               (json.dumps(cache, ensure_ascii=False), name))

    def delete(self, name: str) -> bool:
        with self.store._lock:
            return self._db().execute("DELETE FROM connectors WHERE name = ?", (name,)).rowcount > 0

    @staticmethod
    def _clean(config: dict) -> dict:
        return {key: value for key, value in config.items() if not key.startswith("_") and key not in ("created_at", "updated_at")}
