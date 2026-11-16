"""One configuration for every WorkSpace/mcp server.

Where things live:

* ``<root>/mcp-manager.json`` - the manager's file and the single place a
  service's runtime configuration is edited. ``services.<name>`` holds
  ``public_url``, ``auth`` and ``settings`` next to the manager's own overrides
  (port, autostart, ...).
* ``<service>/mcp-service.json`` - the service's manifest: name, host, port and
  defaults. The central entry overrides it key by key, as mcpm merges them;
  ``auth`` and ``settings`` merge one level deeper, like mcpm merges ``env``,
  so changing one auth field from the console does not wipe the others.
* ``<root>/secrets/`` - passwords and tokens. The config only names them.

``<root>`` is ``$MCPM_ROOT`` when mcpm started the process, else the nearest
directory upward holding ``mcp-manager.json``. A service outside any root uses
its own directory, so it still runs standalone.

Business data - payment methods, watcher databases, QR images - is not
configuration and stays with the service that owns it.
"""
from __future__ import annotations

import json
import locale
import os
import re
import secrets as _secrets
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

MANIFEST_NAME = "mcp-service.json"
CENTRAL_NAME = "mcp-manager.json"
SECRETS_DIR_NAME = "secrets"
#: Keys a service reads from its manifest / central entry. Everything else in
#: those objects belongs to the manager (command, autostart, ...).
SERVICE_KEYS = ("host", "port", "path", "public_url", "stateless", "log_requests", "auth", "settings")
#: Keys mcpm itself reads from the same objects. Anything outside both sets is
#: most likely a typo and is reported - as a warning, so a key mcpm adds later
#: can never stop a service from starting.
MANAGER_KEYS = ("name", "description", "dir", "cwd", "command", "env", "health", "logs", "icon",
                "platforms", "autostart", "autorestart", "enabled", "kind", "observe")
DEEP_MERGED = ("auth", "settings")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

#: ChatGPT's connector callbacks - the only OAuth clients these servers have had.
CHATGPT_REDIRECTS = (
    r"https://chatgpt\.com/connector_platform_oauth_redirect",
    r"https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]{1,200}",
    r"https://chat\.openai\.com/connector_platform_oauth_redirect",
    r"https://chat\.openai\.com/connector/oauth/[A-Za-z0-9_-]{1,200}",
)
_SECRET_NAME = re.compile(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*")


class ConfigError(Exception):
    """A configuration problem, phrased so it is safe to print: never a secret."""


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: ``none`` (loopback only), ``bearer`` (static tokens) or ``oauth``
    #: (owner-password authorization server, plus any ``bearer_secrets``).
    mode: Literal["none", "bearer", "oauth"] = "none"
    bearer_secrets: list[str] = Field(default_factory=list)
    password_secret: str | None = None
    #: OAuth state database, relative to the root. Default:
    #: ``secrets/<service>/oauth.sqlite3``.
    store: str | None = None
    scopes: list[str] = Field(default_factory=lambda: ["mcp", "offline_access"])
    required_scopes: list[str] = Field(default_factory=lambda: ["mcp"])
    #: Seconds an access token is valid; 0 means it never expires.
    access_token_ttl_s: int = Field(3600, ge=0)
    refresh_token_ttl_s: int = Field(30 * 86400, gt=0)
    #: Regular expressions a client's redirect URI must fully match
    #: (``scheme://host/path``; query strings are always refused).
    redirect_uris: list[str] = Field(default_factory=lambda: list(CHATGPT_REDIRECTS))
    subject: str = "owner"
    #: Shown on the login page and in resource metadata.
    title: str | None = None
    #: One paragraph on the login page saying what approving grants.
    consent: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> AuthConfig:
        if self.mode == "bearer" and not self.bearer_secrets:
            raise ValueError("auth.mode 'bearer' needs at least one entry in bearer_secrets")
        if self.mode == "oauth" and not self.password_secret:
            raise ValueError("auth.mode 'oauth' needs password_secret")
        if not self.required_scopes or not set(self.required_scopes) <= set(self.scopes):
            raise ValueError("auth.required_scopes must be a non-empty subset of auth.scopes")
        for name in [*self.bearer_secrets, *([self.password_secret] if self.password_secret else [])]:
            check_secret_name(name)
        for pattern in self.redirect_uris:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"auth.redirect_uris: invalid pattern {pattern!r}: {exc}") from None
        return self


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    root: Path
    dir: Path
    host: str = "127.0.0.1"
    port: int | None = Field(None, gt=0, lt=65536)
    path: str = "/mcp"
    #: Public origin the service is reached at (``https://x.example``, no path).
    #: OAuth issuer and resource identifiers derive from it.
    public_url: str | None = None
    stateless: bool = True
    #: Uvicorn access log. Off by default: OAuth URLs carry codes and state.
    log_requests: bool = False
    auth: AuthConfig = Field(default_factory=AuthConfig)
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not value.startswith("/") or value != value.rstrip("/") or len(value) < 2:
            raise ValueError("path must start with '/' and not end with one, e.g. '/mcp'")
        return value

    @field_validator("public_url")
    @classmethod
    def _public_url(cls, value: str | None) -> str | None:
        if not value:
            return None
        value = value.rstrip("/")
        p = urlsplit(value)
        try:
            p.port  # noqa: B018 - parsing the port is what rejects an out-of-range one
        except ValueError:
            raise ValueError("public_url has an invalid port") from None
        local = p.hostname in LOOPBACK_HOSTS
        if (p.scheme not in (("http", "https") if local else ("https",)) or not p.hostname
                or p.path or p.query or p.fragment or p.username or p.password):
            raise ValueError("public_url must be an https origin with no path, e.g. https://x.example")
        return value

    @model_validator(mode="after")
    def _oauth_needs_origin(self) -> ServiceConfig:
        if self.auth.mode == "oauth" and not self.public_url:
            raise ValueError("auth.mode 'oauth' needs public_url: issuer and resource derive from it")
        return self

    @property
    def secrets_dir(self) -> Path:
        return self.root / SECRETS_DIR_NAME

    @property
    def resource(self) -> str | None:
        return self.public_url + self.path if self.public_url else None

    @property
    def oauth_store(self) -> Path:
        rel = self.auth.store or f"{SECRETS_DIR_NAME}/{self.name}/oauth.sqlite3"
        path = Path(rel)
        return path if path.is_absolute() else self.root / path


# -- loading ---------------------------------------------------------------------------

def read_json(path: Path) -> dict[str, Any]:
    try:
        # utf-8-sig: PowerShell 5.1 writes a BOM that plain json.loads rejects.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from None
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a JSON object")
    return data


def find_service_dir(anchor: str | Path) -> Path:
    """The directory holding ``mcp-service.json`` at or above ``anchor``."""
    start = Path(anchor).resolve()
    for directory in (start, *start.parents) if start.is_dir() else start.parents:
        if (directory / MANIFEST_NAME).is_file():
            return directory
    raise ConfigError(f"no {MANIFEST_NAME} found at or above {start}")


def find_root(service_dir: Path, environ: dict[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    if environ.get("MCPM_ROOT"):
        root = Path(environ["MCPM_ROOT"]).resolve()
        if (root / CENTRAL_NAME).is_file():
            return root
    for directory in service_dir.parents:
        if (directory / CENTRAL_NAME).is_file():
            return directory
    return service_dir


def merge(manifest: dict[str, Any], central: dict[str, Any]) -> dict[str, Any]:
    merged = {k: manifest[k] for k in SERVICE_KEYS if k in manifest}
    for key in SERVICE_KEYS:
        if key not in central:
            continue
        if key in DEEP_MERGED and isinstance(central[key], dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **central[key]}
        else:
            merged[key] = central[key]
    return merged


def load(anchor: str | Path, *, root: Path | None = None, environ: dict[str, str] | None = None,
         **overrides: Any) -> ServiceConfig:
    """Configuration for the service that ``anchor`` (a file or directory) belongs to.

    ``overrides`` (typically ``host`` / ``port`` from the command line mcpm
    runs) win over both files.
    """
    service_dir = find_service_dir(anchor)
    manifest = read_json(service_dir / MANIFEST_NAME)
    name = str(manifest.get("name") or service_dir.name)
    root = (root or find_root(service_dir, environ)).resolve()
    central_path = root / CENTRAL_NAME
    central = read_json(central_path) if central_path.is_file() else {}
    entry = (central.get("services") or {}).get(name) or {}
    entry = entry if isinstance(entry, dict) else {}
    for source, keys in ((service_dir / MANIFEST_NAME, manifest), (central_path, entry)):
        unknown = sorted(set(keys) - set(SERVICE_KEYS) - set(MANAGER_KEYS))
        if unknown:
            print(f"mcpkit: warning: {source}: unknown key(s) ignored for service {name!r}: {', '.join(unknown)}",
                  file=sys.stderr)
    data = merge(manifest, entry)
    # One documented escape hatch: a JSON object in MCPKIT_SETTINGS overrides
    # settings for this process only (a test run, a one-off with another
    # profile). It never touches the files.
    raw = (environ or os.environ).get("MCPKIT_SETTINGS")
    if raw:
        try:
            temporary = json.loads(raw)
        except ValueError as exc:
            raise ConfigError(f"MCPKIT_SETTINGS is not valid JSON: {exc}") from None
        if not isinstance(temporary, dict):
            raise ConfigError("MCPKIT_SETTINGS must be a JSON object of settings")
        data["settings"] = {**data.get("settings", {}), **temporary}
    data.update({k: v for k, v in overrides.items() if v is not None})
    try:
        return ServiceConfig(name=name, root=root, dir=service_dir, **data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'config'}: {err['msg'].removeprefix('Value error, ')}"
            for err in exc.errors())
        raise ConfigError(f"service {name!r} ({central_path}): {problems}") from None


def update_settings(config: ServiceConfig, **values: Any) -> None:
    """Write ``values`` into ``services.<name>.settings`` of the central file.

    Read-modify-replace in one step, preserving every other key, so a tool that
    learns a setting (ones' e2e detects the team's manhour mode) can persist it.
    """
    path = config.root / CENTRAL_NAME
    data = read_json(path) if path.is_file() else {}
    entry = data.setdefault("services", {}).setdefault(config.name, {})
    entry.setdefault("settings", {}).update(values)
    write_json(path, data)


def write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# -- secrets -------------------------------------------------------------------------------

def check_secret_name(name: str) -> str:
    if not _SECRET_NAME.fullmatch(name) or any(part in (".", "..") for part in name.split("/")):
        raise ValueError(f"invalid secret name {name!r}: use letters, digits, '_', '-', '.' and '/'")
    return name


def secret_path(config: ServiceConfig, name: str) -> Path:
    try:
        check_secret_name(name)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    return config.secrets_dir.joinpath(*name.split("/"))


def read_secret(config: ServiceConfig, name: str, *, min_length: int = 32) -> str:
    """A secret's value. Errors say which file and what is wrong with it, never
    what is in it."""
    path = secret_path(config, name)
    hint = f"secret {name!r} ({path})"
    try:
        if path.stat().st_size > 8192:
            raise ConfigError(f"{hint} is larger than 8KB")
        value = path.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError:
        raise ConfigError(f"{hint} does not exist; create it with `python -m mcpkit secret new {name}`") from None
    except PermissionError:
        raise ConfigError(f"{hint} is not readable by this account; check its ACL") from None
    except (OSError, UnicodeError):
        raise ConfigError(f"{hint} cannot be read as UTF-8 text") from None
    if not min_length <= len(value) <= 4096:
        raise ConfigError(f"{hint} must be {min_length}-4096 characters")
    if not value.isascii() or any(c.isspace() for c in value):
        raise ConfigError(f"{hint} must be ASCII without whitespace")
    return value


def write_secret(config: ServiceConfig, name: str, value: str | None = None) -> Path:
    """Create or replace a secret; a random 64-character token when no value is given."""
    path = secret_path(config, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    protect(config.secrets_dir)
    path.write_text((value if value is not None else _secrets.token_urlsafe(48)) + "\n", encoding="utf-8")
    protect(path)
    return path


def protect(path: Path) -> None:
    """Owner-only access: the account running the servers and SYSTEM."""
    if sys.platform != "win32":
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
        return
    user = f"{os.environ.get('USERDOMAIN', '.')}\\{os.environ.get('USERNAME', '')}"
    grant = "(OI)(CI)F" if path.is_dir() else "F"
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:{grant}", f"*S-1-5-18:{grant}"],
        capture_output=True)
    if result.returncode != 0:
        # icacls emits the Windows code page even when PYTHONUTF8=1. Decode
        # only diagnostics, outside subprocess's reader threads.
        diagnostic = (result.stdout or result.stderr or b"").decode(locale.getencoding(), errors="replace").strip()
        raise ConfigError(f"could not restrict access to {path}: {diagnostic}")
