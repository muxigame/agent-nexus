"""What a plugin can be configured with, and what it still needs (design §6.3, §6.6).

A v2 manifest declares ``config`` (a JSON Schema subset plus UI hints) and
``secrets[]``. The six private plugins still ship v1 manifests, which declare
neither, yet four of them do not start without settings or a credential. Until
their repositories move to v2 (design §6.8), :data:`LEGACY_HINTS` supplies the
missing declarations for those names, so the console can render their forms and
show ``needs_config`` instead of a crash loop. A manifest that declares its own
``config`` or ``secrets`` always wins over a hint.

Values live where mcpkit reads them:

* settings: ``plugins.<name>.settings.<key>`` (``x-anx-target: "service"`` keys
  go to ``plugins.<name>.<key>``);
* secrets: ``<root>/secrets/<name>/<secret>``. A secret with ``setting`` also
  puts ``{"$secret": "<name>/<secret>"}`` at ``settings.<setting>``, for plugins
  that read a password from their settings (mcpkit resolves the reference).

The configuration file never holds a secret value.
"""
from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SECRETS_DIR = "secrets"
SECRET_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")

#: v1 plugins whose settings and credentials are known (design §6.8 lists them).
LEGACY_HINTS: dict[str, dict[str, Any]] = {
    "ones": {
        "display_name": "ONES",
        "config": {
            "schema": {"type": "object", "required": ["team_id"], "properties": {
                "base_url": {"type": "string", "format": "uri", "title": "实例地址", "default": "https://sz.ones.cn"},
                "team_id": {"type": "string", "title": "团队 ID", "minLength": 1,
                            "description": "ONES 网址里 /team/ 后面那一段"},
                "user_id": {"type": "string", "title": "用户 ID", "x-anx-advanced": True,
                            "description": "token 所属用户，可留空"},
                "manhour_mode": {"type": "string", "title": "工时模式", "enum": ["", "simple", "summary"], "default": "",
                                 "x-anx-advanced": True},
                "profile": {"type": "string", "title": "工具集", "enum": ["official", "full"], "default": "official",
                            "description": "full 会额外提供原始 OpenAPI 接口（api_*）"},
                "timeout": {"type": "number", "title": "请求超时（秒）", "default": 30, "minimum": 1, "x-anx-advanced": True},
            }},
            "ui": {"groups": [{"title": "常用", "fields": ["base_url", "team_id", "profile"]},
                              {"title": "高级", "fields": ["user_id", "manhour_mode", "timeout"], "collapsed": True}]},
        },
        "secrets": [{"name": "token", "title": "个人访问 Token", "required": True,
                     "description": "ONES → 个人设置 → 个人访问令牌"}],
    },
    "jubian-go": {
        "display_name": "聚变云",
        "config": {
            "schema": {"type": "object", "required": ["username"], "properties": {
                "base_url": {"type": "string", "format": "uri", "title": "站点地址", "default": "https://go.jubianyun.com"},
                "username": {"type": "string", "title": "账号", "minLength": 1},
                "timeout_s": {"type": "number", "title": "请求超时（秒）", "default": 30, "minimum": 1, "maximum": 300,
                              "x-anx-advanced": True},
            }},
        },
        "secrets": [{"name": "password", "title": "登录密码", "required": True, "setting": "password"}],
    },
    "wechat": {
        "display_name": "企业微信",
        "config": {
            "schema": {"type": "object", "required": [], "properties": {
                "media_ttl_s": {"type": "integer", "title": "附件链接有效期（秒）", "default": 86400, "minimum": 60},
                "journal_template_id": {"type": "string", "title": "周报模板 ID（可选）", "x-anx-advanced": True},
            }},
        },
        "secrets": [
            {"name": "journal-cookie", "title": "周报提交 Cookie（可选）", "required": False,
             "description": "只有提交周报用到；收发消息不需要任何凭据"},
            {"name": "journal-sid", "title": "周报 sid（可选）", "required": False},
        ],
    },
    "payment": {
        "display_name": "收款目录",
        "config": {
            "schema": {"type": "object", "required": [], "properties": {
                "config_file": {"type": "string", "format": "path", "title": "收款目录文件", "default": "config.json",
                                "description": "相对插件目录；从旧文件夹安装时会一起复制过来"},
            }},
        },
        "secrets": [],
    },
    "chrome-devtools": {
        "display_name": "Edge 浏览器调试",
        "config": {
            "schema": {"type": "object", "required": [], "properties": {
                "node": {"type": "string", "format": "path", "title": "node 可执行文件", "default": "node"},
                "entry": {"type": "string", "title": "chrome-devtools-mcp 入口", "x-anx-advanced": True,
                          "default": "node_modules/chrome-devtools-mcp/build/src/bin/chrome-devtools-mcp.js"},
            }},
        },
        "secrets": [],
    },
    "unity": {"display_name": "Unity 编辑器", "config": {}, "secrets": []},
    "device": {"display_name": "本机能力（device）"},
}


def effective(name: str, manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    """``display_name``, ``config`` and ``secrets`` for a plugin, hints filled in."""
    manifest = dict(manifest or {})
    hint = LEGACY_HINTS.get(name, {})
    config = manifest.get("config") if isinstance(manifest.get("config"), dict) else None
    secrets = manifest.get("secrets") if isinstance(manifest.get("secrets"), list) else None
    return {
        "display_name": str(manifest.get("display_name") or hint.get("display_name") or name),
        "config": copy.deepcopy(config if config is not None else hint.get("config") or {}),
        "secrets": [dict(s) for s in (secrets if secrets is not None else hint.get("secrets") or [])
                    if isinstance(s, dict) and isinstance(s.get("name"), str) and SECRET_NAME.match(s["name"])],
        "hinted": config is None and secrets is None and bool(hint.get("config") or hint.get("secrets")),
    }


def schema_of(info: Mapping[str, Any]) -> dict[str, Any]:
    schema = (info.get("config") or {}).get("schema")
    return schema if isinstance(schema, dict) and schema.get("type", "object") == "object" else {}


def secret_file(root: Path, name: str, secret: str) -> Path:
    return Path(root) / SECRETS_DIR / name / secret


def secret_state(root: Path, name: str, secret: str) -> dict[str, Any]:
    path = secret_file(root, name, secret)
    try:
        stat = path.stat()
    except OSError:
        return {"set": False, "updated_at": None}
    return {"set": stat.st_size > 0, "updated_at": int(stat.st_mtime)}


def target_of(prop: Mapping[str, Any]) -> str:
    return "service" if prop.get("x-anx-target") == "service" else "settings"


def current_values(info: Mapping[str, Any], entry: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Each schema property's value: the configuration entry, else the manifest, else the default."""
    settings = {**(manifest.get("settings") or {}), **(entry.get("settings") or {})}
    values: dict[str, Any] = {}
    for key, prop in (schema_of(info).get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue
        source = settings if target_of(prop) == "settings" else {**manifest, **entry}
        if key in source and not _is_ref(source[key]):
            values[key] = source[key]
        elif "default" in prop:
            values[key] = prop["default"]
    return values


def _is_ref(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"$secret"}


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def missing(root: Path, name: str, manifest: Mapping[str, Any] | None, entry: Mapping[str, Any] | None) -> list[str]:
    """Required settings without a value and required secrets without a file (keys, in schema order)."""
    manifest, entry = dict(manifest or {}), dict(entry or {})
    info = effective(name, manifest)
    values = current_values(info, entry, manifest)
    result = [key for key in schema_of(info).get("required") or [] if _empty(values.get(key))]
    for secret in info["secrets"]:
        if secret.get("required") and not secret_state(root, name, secret["name"])["set"]:
            result.append(secret["name"])
    return result


def validate(info: Mapping[str, Any], values: Mapping[str, Any]) -> list[dict[str, str]]:
    """Problems with ``values`` against the plugin's schema: ``[{"path", "message"}]``."""
    schema = schema_of(info)
    if not schema:
        return []
    problems: list[dict[str, str]] = []
    try:
        from jsonschema import Draft202012Validator
    except ImportError:  # pragma: no cover - jsonschema comes with fastmcp
        Draft202012Validator = None
    if Draft202012Validator is not None:
        plain = {k: v for k, v in schema.items() if not k.startswith("x-")}
        for error in Draft202012Validator(plain).iter_errors(dict(values)):
            path = ".".join(str(p) for p in error.absolute_path) or (
                error.message.split("'")[1] if error.validator == "required" and "'" in error.message else "")
            problems.append({"path": path, "message": _message(error)})
    return problems


def _message(error: Any) -> str:
    kind = error.validator
    if kind == "required":
        return "必填"
    if kind == "type":
        return {"string": "需要文本", "integer": "需要整数", "number": "需要数字", "boolean": "需要开或关"}.get(
            str(error.validator_value), "类型不对")
    if kind == "enum":
        return "只能是：" + "、".join(str(v) or "（空）" for v in error.validator_value)
    if kind in ("minimum", "exclusiveMinimum"):
        return f"不能小于 {error.validator_value}"
    if kind in ("maximum", "exclusiveMaximum"):
        return f"不能大于 {error.validator_value}"
    if kind == "minLength":
        return "必填" if error.validator_value == 1 else f"至少 {error.validator_value} 个字符"
    return error.message
