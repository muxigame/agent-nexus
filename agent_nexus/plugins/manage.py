"""The console's view of plugins: list entries, configuration, enable/disable, install jobs.

``/api/plugins`` (docs/ui-spec.md §4.2) is the user view of the supervisor's
``/api/services``; everything here builds on :class:`~agent_nexus.supervisor.Supervisor`
snapshots plus the lock file and the manifest.
"""
from __future__ import annotations

import secrets as _secrets
import threading
import time
from pathlib import Path
from typing import Any

from .. import paths, sections
from . import installer, schema

#: Runtime parameters the configuration form edits next to the schema fields.
RUNTIME_FIELDS = {"port": int, "autostart": bool, "autorestart": bool}
#: ``x-anx-target: "service"`` keys a form may write (mcpkit ``SERVICE_KEYS`` minus what the manager owns).
SERVICE_TARGET_KEYS = {"public_url", "path", "stateless", "log_requests", "host"}


class PluginError(Exception):
    def __init__(self, message: str, status: int = 400, **extra: Any):
        super().__init__(message)
        self.status = status
        self.extra = extra


def _manifest(directory: Path) -> dict[str, Any]:
    try:
        return installer.read_json(Path(directory) / installer.MANIFEST_NAME)
    except Exception:
        return {}


def _bundled_dirs(root: Path) -> list[Path]:
    here = Path(__file__).resolve().parents[1]
    return [p.resolve() for p in (Path(root) / "builtins", here / "builtins", here.parent / "builtins") if p.is_dir()]


def source_of(root: Path, name: str, spec: Any, lock: dict[str, Any]) -> dict[str, Any]:
    record = lock.get(name) if isinstance(lock.get(name), dict) else None
    if record is not None:
        source = dict(record.get("source") or {})
        source.setdefault("type", "link" if record.get("mode") == "link" else "path")
        return source
    if not getattr(spec, "installed", True):
        return {"type": "none"}
    directory = Path(spec.dir).resolve()
    if any(directory.is_relative_to(base) for base in _bundled_dirs(root)):
        return {"type": "builtin"}
    return {"type": "external", "path": str(directory)}


def plugin_view(sup: Any, name: str, lock: dict[str, Any] | None = None) -> dict[str, Any]:
    snap = sup.snapshot(name)
    spec = sup.spec(name)
    lock = installer.read_lock(sup.cfg.root) if lock is None else lock
    manifest = _manifest(spec.dir) if spec.installed else {}
    info = schema.effective(name, manifest)
    record = lock.get(name) if isinstance(lock.get(name), dict) else {}
    version = spec.version or str(record.get("version") or "")
    if not version and spec.installed:
        try:
            version = installer.plugin_version(Path(spec.dir), manifest)
            version = "" if version == "0.0.0" else version
        except Exception:
            version = ""
    source = source_of(sup.cfg.root, name, spec, lock)
    has_schema = bool(schema.schema_of(info).get("properties")) or bool(info["secrets"])
    if snap["status"] == "needs_config" and spec.missing:
        snap = {**snap, "message": "缺少：" + "、".join(_title(info, key) for key in spec.missing)}
    return {
        **snap,
        "title": info["display_name"],
        "version": version,
        "source": source,
        "installed": spec.installed,
        "builtin": name in installer.BUILTIN,
        "removable": name not in installer.BUILTIN and (bool(record) or not spec.installed),
        "needs_config": snap["status"] == "needs_config",
        "missing": list(spec.missing),
        "missing_titles": [_title(info, key) for key in spec.missing],
        "has_config_schema": has_schema,
        "installed_at": record.get("installed_at"),
        "log_sources": snap.get("logs", []),
    }


def _title(info: dict[str, Any], key: str) -> str:
    prop = (schema.schema_of(info).get("properties") or {}).get(key)
    if isinstance(prop, dict) and prop.get("title"):
        return str(prop["title"])
    for secret in info["secrets"]:
        if secret["name"] == key:
            return str(secret.get("title") or key)
    return key


def plugin_views(sup: Any) -> list[dict[str, Any]]:
    lock = installer.read_lock(sup.cfg.root)
    views = [plugin_view(sup, name, lock) for name in sorted(sup.specs)]
    order = {"device": 0}
    return sorted(views, key=lambda v: (order.get(v["name"], 1), not v["installed"], v["name"]))


# -- configuration ---------------------------------------------------------------------

def get_config(sup: Any, name: str) -> dict[str, Any]:
    spec = sup.spec(name)
    if not spec.installed:
        raise PluginError(f"{name} 尚未安装", 409)
    root = sup.cfg.root
    manifest = _manifest(spec.dir)
    entry = installer.read_entry(root, name) or {}
    info = schema.effective(name, manifest)
    plain = schema.current_values(info, entry, manifest)
    secrets = []
    for secret in info["secrets"]:
        state = schema.secret_state(root, name, secret["name"])
        secrets.append({"name": secret["name"], "title": secret.get("title") or secret["name"],
                        "description": secret.get("description") or "", "required": bool(secret.get("required")),
                        "generate": bool(secret.get("generate")), **state})
    props = schema.schema_of(info).get("properties") or {}
    for key, prop in props.items():  # a schema field marked secret is write-only too
        if isinstance(prop, dict) and prop.get("format") == "secret":
            plain[key] = schema.secret_state(root, name, key)
    raw = None
    if not props and not info["secrets"]:
        raw = {k: v for k, v in {**(manifest.get("settings") or {}), **(entry.get("settings") or {})}.items()
               if not sections.is_secret_ref(v)}
    return {
        "name": name, "title": info["display_name"], "schema": schema.schema_of(info),
        "ui": (info.get("config") or {}).get("ui") or {}, "values": plain, "secrets": secrets,
        "settings": raw, "hinted": info["hinted"],
        "runtime": {"port": spec.port, "autostart": spec.autostart, "autorestart": spec.autorestart},
        "missing": list(spec.missing),
        "missing_titles": [_title(info, key) for key in spec.missing],
    }


def put_config(sup: Any, name: str, body: dict[str, Any]) -> dict[str, Any]:
    """Save ``{"values", "secrets", "runtime", "settings"}``; restart or start the plugin as needed."""
    spec = sup.spec(name)
    if not spec.installed:
        raise PluginError(f"{name} 尚未安装", 409)
    root = sup.cfg.root
    manifest = _manifest(spec.dir)
    info = schema.effective(name, manifest)
    props = schema.schema_of(info).get("properties") or {}
    values = body.get("values") if isinstance(body.get("values"), dict) else {}
    secret_values = dict(body.get("secrets") or {}) if isinstance(body.get("secrets"), dict) else {}
    runtime = body.get("runtime") if isinstance(body.get("runtime"), dict) else {}

    plain: dict[str, Any] = {}
    for key, value in values.items():
        prop = props.get(key)
        if not isinstance(prop, dict):
            raise PluginError(f"未知的配置项：{key}", 422, field=key)
        if prop.get("format") == "secret":
            secret_values.setdefault(key, value)
        else:
            plain[key] = value
    entry = dict(installer.read_entry(root, name) or {})
    # Types and ranges of what was entered; a partial form is fine (the plugin stays needs_config).
    entered = {k: v for k, v in plain.items() if v not in (None, "")}
    partial = {**info, "config": {**(info.get("config") or {}),
                                  "schema": {**schema.schema_of(info), "required": []}}}
    problems = schema.validate(partial, entered)
    known_secrets = {s["name"]: s for s in info["secrets"]} | {k: {"name": k} for k, p in props.items()
                                                                   if isinstance(p, dict) and p.get("format") == "secret"}
    for key in secret_values:
        if key not in known_secrets:
            problems.append({"path": key, "message": "未知的凭据"})
    for key, kind in RUNTIME_FIELDS.items():
        if key in runtime and runtime[key] is not None:
            value = runtime[key]
            if kind is bool and not isinstance(value, bool):
                problems.append({"path": f"runtime.{key}", "message": "需要开或关"})
            if kind is int:
                try:
                    if not 0 < int(value) < 65536:
                        raise ValueError
                except (TypeError, ValueError):
                    problems.append({"path": f"runtime.{key}", "message": "端口要在 1–65535 之间"})
    if problems:
        raise PluginError("配置有误：" + "；".join(f"{p['path'] or '配置'} {p['message']}" for p in problems), 422,
                          problems=problems)

    settings = dict(entry.get("settings") or {})
    for key, value in plain.items():
        prop = props[key]
        if schema.target_of(prop) == "service":
            if key not in SERVICE_TARGET_KEYS:
                raise PluginError(f"{key} 不能写到运行参数", 422, field=key)
            if value in ("", None):
                entry.pop(key, None)
            else:
                entry[key] = value
        elif value is None or (value == "" and "default" in prop):
            settings.pop(key, None)
        else:
            settings[key] = value
    if isinstance(body.get("settings"), dict) and not props:  # v1 plugin without a schema: the raw object
        kept_refs = {k: v for k, v in settings.items() if sections.is_secret_ref(v)}
        settings = {**body["settings"], **kept_refs}
    for key, value in secret_values.items():
        spec_secret = known_secrets[key]
        _write_secret(root, name, key, value)
        if spec_secret.get("setting"):
            if value is None:
                settings.pop(spec_secret["setting"], None)
            elif value != "":
                settings[spec_secret["setting"]] = {sections.SECRET_REF: f"{name}/{key}"}
    if settings:
        entry["settings"] = settings
    else:
        entry.pop("settings", None)
    for key, kind in RUNTIME_FIELDS.items():
        if key in runtime and runtime[key] is not None:
            entry[key] = int(runtime[key]) if kind is int else bool(runtime[key])
    installer.write_entry(root, name, entry)
    return _apply(sup, name)


def _write_secret(root: Path, name: str, key: str, value: Any) -> None:
    path = schema.secret_file(root, name, key)
    if value is None:  # clear
        path.unlink(missing_ok=True)
        return
    if isinstance(value, dict) and value.get("generate"):
        value = _secrets.token_urlsafe(48)
    if not isinstance(value, str):
        raise PluginError(f"{key} 必须是文本", 422, field=key)
    value = value.strip()
    if value == "":
        return  # an empty password field means "unchanged"
    if len(value) > 4096 or "\n" in value:
        raise PluginError(f"{key} 太长或包含换行", 422, field=key)
    from mcpkit.config import ServiceConfig, write_secret
    write_secret(ServiceConfig(name="manager", root=Path(root), dir=Path(root)), f"{name}/{key}", value)


def _apply(sup: Any, name: str) -> dict[str, Any]:
    """After a change: rescan; restart a running plugin, start one that just became complete."""
    from ..supervisor import SupervisorError
    rt = sup.rt.get(name)
    was_running = bool(rt and rt.pid is not None)
    sup.rescan()
    spec = sup.spec(name)
    message = "已保存"
    try:
        if was_running and spec.runnable:
            sup.restart(name)
            message = "已保存并重启"
        elif was_running and spec.missing:
            sup.stop(name)
            message = "已保存；还缺少必填项，插件已停止"
        elif not was_running and spec.runnable and spec.autostart:
            sup.start(name)
            message = "已保存并启动"
    except SupervisorError as exc:
        message = f"已保存，但{exc}"
    sup.tick()
    return {"plugin": plugin_view(sup, name), "message": message}


# -- enable / disable --------------------------------------------------------------------

def set_enabled(sup: Any, name: str, enabled: bool) -> dict[str, Any]:
    from ..supervisor import SupervisorError
    spec = sup.spec(name)
    if not spec.installed:
        raise PluginError(f"{name} 尚未安装：先点“安装”", 409)
    root = sup.cfg.root
    message = ""
    if not enabled:
        if not spec.observed:
            try:
                sup.stop(name)
            except SupervisorError as exc:
                message = str(exc)
        installer.update_entry(root, name, enabled=False)
        sup.rescan()
        sup.tick()
        return {"plugin": plugin_view(sup, name), "message": message or f"已停用 {name}"}
    installer.update_entry(root, name, enabled=True, autostart=None if spec.observed else True)
    sup.rescan()
    spec = sup.spec(name)
    if spec.observed:
        message = f"已启用 {name}"
    elif spec.missing:
        message = f"已启用 {name}；先完成配置才能启动"
    else:
        try:
            message = sup.start(name)
        except SupervisorError as exc:
            message = f"已启用，但启动失败：{exc}"
    sup.tick()
    return {"plugin": plugin_view(sup, name), "message": message}


# -- install jobs ------------------------------------------------------------------------

class Jobs:
    """Background installs for the console (``POST /api/plugins/install`` → ``GET .../jobs/{id}``)."""

    KEEP_S = 3600

    def __init__(self, sup: Any):
        self.sup = sup
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.install_lock = threading.Lock()

    def uploads_dir(self) -> Path:
        return installer.plugins_dir(self.sup.cfg.root) / installer.STAGING / "uploads"

    def save_upload(self, filename: str, data: bytes) -> str:
        if len(data) > installer.MAX_ARCHIVE_BYTES:
            raise PluginError("压缩包超过 200 MB", 413)
        clean = "".join(c for c in Path(filename or "plugin.zip").name if c.isalnum() or c in "._-") or "plugin.zip"
        if not clean.lower().endswith((".zip", ".tar.gz", ".tgz")):
            raise PluginError("只支持 .zip 和 .tar.gz", 400)
        upload_id = f"u_{_secrets.token_hex(8)}"
        target = self.uploads_dir() / upload_id
        target.mkdir(parents=True, exist_ok=True)
        (target / clean).write_bytes(data)
        self._sweep_uploads()
        return upload_id

    def _sweep_uploads(self) -> None:
        base = self.uploads_dir()
        if not base.is_dir():
            return
        for child in base.iterdir():
            try:
                if time.time() - child.stat().st_mtime > self.KEEP_S:
                    installer.remove_tree(child)
            except OSError:
                pass

    def start_hub(self, name: str, version: str, *, sha256: str, path: str, enable: bool = True,
                  origin: str = "local", size: int | None = None) -> str:
        """Download ``name`` ``version`` from the gateway's 插件库 and install it (design §6.12)."""
        from . import hub_source
        if not installer.NAME_PATTERN.fullmatch(str(name)) or not installer.VERSION_PATTERN.fullmatch(str(version)):
            raise PluginError("插件名或版本号不合法", 400)
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise PluginError("缺少插件包的 sha256", 400)
        source = installer.Source("hub", sha256=sha256, package=name, package_version=version,
                                  filename=f"{name}-{version}.zip")
        target = self.uploads_dir() / f"u_{_secrets.token_hex(8)}" / source.filename

        def prepare(log) -> installer.Source:
            log(f"安装来源：网关插件库（{'网关控制台' if origin == 'gateway' else '本机控制台'}发起）")
            try:
                hub_source.download(self.sup.cfg.root, path, target, sha256=sha256, log=log, size=size)
            except BaseException:
                installer.remove_tree(target.parent)
                raise
            source.path = str(target)
            return source
        return self.start(source, enable=enable, prepare=prepare)

    def start(self, source: installer.Source, *, enable: bool, port: int | None = None, prepare=None) -> str:
        job_id = f"j_{_secrets.token_hex(6)}"
        job = {"id": job_id, "state": "running", "log": [], "plugin": None, "error": None, "result": None,
               "source": source.record(), "started_at": time.time()}
        with self.lock:
            self.jobs = {k: v for k, v in self.jobs.items() if time.time() - v["started_at"] < self.KEEP_S}
            self.jobs[job_id] = job

        def run() -> None:
            nonlocal source
            inst = installer.Installer(self.sup.cfg.root, self.sup, log=job["log"].append, lock=self.install_lock)
            try:
                if prepare is not None:
                    source = prepare(inst.log)
                result = inst.install(source, enable=enable, port=port)
                job["result"] = {"name": result.name, "version": result.version, "state": result.state,
                                 "message": result.message}
                try:
                    job["plugin"] = plugin_view(self.sup, result.name)
                except Exception:
                    job["plugin"] = None
                job["state"] = "done"
            except installer.InstallError as exc:
                job["error"], job["code"], job["state"] = str(exc), exc.code, "failed"
            except Exception as exc:  # never leave a job running forever
                job["error"], job["code"], job["state"] = f"{type(exc).__name__}: {exc}", "internal", "failed"
            finally:
                if source.type in ("archive", "hub") and source.path and self.uploads_dir() in Path(source.path).parents:
                    installer.remove_tree(Path(source.path).parent)

        threading.Thread(target=run, name=f"anx-install-{job_id}", daemon=True).start()
        return job_id

    def get(self, job_id: str, since: int = 0) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        return {**{k: v for k, v in job.items() if k != "log"}, "log": job["log"][since:], "log_total": len(job["log"])}

    def uninstall(self, name: str, purge: bool) -> dict[str, Any]:
        inst = installer.Installer(self.sup.cfg.root, self.sup, lock=self.install_lock)
        return inst.uninstall(name, purge=purge)


def config_root_has(root: Path) -> bool:
    return paths.config_path(root).is_file()


# -- where the owner's plugin folders probably are ----------------------------------------

def suggestions(root: Path) -> list[dict[str, str]]:
    """Plugin folders found next to the legacy configuration this root came from (or ``plugin_sources``).

    The install dialog offers them as one-click paths: the six private plugins
    usually sit in the old manager's ``builtins/`` (design §6.8).
    """
    from ..registry import BUILTIN_DIRS
    root = Path(root)
    try:
        data = paths.read_config(root)
    except (OSError, ValueError):
        data = {}
    bases: list[Path] = [Path(p) for p in data.get("plugin_sources") or [] if isinstance(p, str)]
    legacy_files = []
    migrated = data.get("migrated_from")
    if isinstance(migrated, dict) and isinstance(migrated.get("path"), str):
        legacy_files.append(Path(migrated["path"]))
    legacy_root = paths.legacy_installed_root()
    if legacy_root is not None:
        legacy_files.append(legacy_root / paths.LEGACY_CONFIG_NAME)
    for file in legacy_files:
        base = file.parent
        try:
            legacy = installer.read_json(file) if file.is_file() else {}
        except Exception:
            legacy = {}
        scan = legacy.get("scan") if isinstance(legacy.get("scan"), list) else ["."]
        bases += [base / str(s) for s in scan] + [base / rel for rel in BUILTIN_DIRS]
    found: dict[str, dict[str, str]] = {}
    seen: set[Path] = set()
    for base in bases:
        try:
            base = base.resolve()
            if base in seen or not base.is_dir():
                continue
            seen.add(base)
            children = sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            continue
        for child in children:
            manifest = child / installer.MANIFEST_NAME
            if not manifest.is_file():
                continue
            try:
                name = installer.plugin_name(child, installer.read_json(manifest))
            except Exception:
                continue
            if name in installer.BUILTIN or name in found:
                continue
            found[name] = {"name": name, "path": str(child)}
    return sorted(found.values(), key=lambda item: item["name"])
