"""Opt-in, private distribution enrollment; ordinary packages carry no credential."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from mcpkit.config import ConfigError, ServiceConfig, protect, write_secret

from . import paths
from .federation import valid_id
from .registry import RegistryError, read_json


ENDPOINT_ALLOW = ".private/endpoint-allow"


def canonical_endpoint(url) -> str | None:
    """``wss://`` form of an enrollment URL (https:// is accepted as an alias)."""
    if not isinstance(url, str) or not url.isascii() or any(c.isspace() for c in url):
        return None
    url = url.rstrip("/")
    for scheme in ("wss://", "https://"):
        if url.startswith(scheme):
            rest = url[len(scheme):]
            host = rest.split("/", 1)[0]
            if not host or "@" in host or "?" in rest or "#" in rest:
                return None
            return "wss://" + rest
    return None


def allowed_endpoints(root: Path) -> set[str]:
    """Endpoints pinned at build time; the public source names no endpoint at all."""
    path = root / ENDPOINT_ALLOW
    if not path.is_file():
        return set()
    allowed = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            endpoint = canonical_endpoint(line)
            if endpoint:
                allowed.add(endpoint)
    return allowed


def enroll_if_needed(root: Path) -> bool:
    profile_path = root / ".private/enrollment.json"
    if not profile_path.is_file():
        return False
    try:
        data = read_json(paths.config_path(root))
        if data.get("cloud", {}).get("enabled"):
            return False  # Never replace an existing machine's identity or credentials.
        profile = read_json(profile_path)
        token = profile["token"]
        if not isinstance(token, str) or not 32 <= len(token) <= 4096 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("invalid enrollment credential")
        url = canonical_endpoint(profile["url"])
        if url is None or url not in allowed_endpoints(root):
            raise ValueError("invalid enrollment endpoint")
        prefix = profile.get("agent_prefix", "portable-")
        if len(prefix) < 6 or not prefix.endswith("-"):
            raise ValueError("invalid enrollment prefix")
        agent_id = valid_id(profile.get("agent_id") or prefix + uuid.uuid4().hex)
        secret = "cloud/enrolled-agent"
        write_secret(ServiceConfig(name="manager", root=root, dir=root), secret, token)
        protect(profile_path.parent)
        protect(profile_path)
        workspace = profile.get("workdir") or os.environ.get("USERPROFILE") or str(Path.home())
        data["cloud"] = {"enabled": True, "agent_id": agent_id, "url": url,
                         "token_secret": secret, "workspaces": {"home": {"path": workspace, "mcp": "device"}}}
        # The legacy file, if that is what was read, stays untouched.
        path = paths.write_path(root)
        temporary = path.with_suffix(".json.enrollment.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
        return True
    except (KeyError, ValueError, OSError, ConfigError) as exc:
        # Diagnostics never contain the private profile or its token.
        raise RegistryError("private cloud enrollment failed; check the profile and file permissions") from exc
