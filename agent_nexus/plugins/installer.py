"""Installing and removing plugins (design §6.4, §6.5; M8).

Sources: a local folder (copied, or registered in ``link`` mode and run in
place), a git URL (+ ref), or a ``.zip`` / ``.tar.gz`` archive (a local file,
an upload, or an http(s) URL). Every source ends up as one directory holding
``mcp-service.json``; v1 manifests (today's) and v2 ones (§6.2) are both read.

Layout::

    <root>/plugins/<name>/<version>/   code, with the plugin's own .venv
    <root>/plugins/<name>/current      "<version>"
    <root>/plugins/<name>/<dep>/       relative path dependencies (``../mcpkit``)
    <root>/plugins/plugins.lock.json   how each plugin was installed
    <root>/plugins/.staging/           downloads, clones, unpacked archives

Differences from the letter of §6.5, both deliberate:

* install steps run in the final version directory, not in ``.staging``: a
  ``uv`` virtual environment and its editable ``../mcpkit`` link hold absolute
  paths, so a venv built in staging would be broken after the move. A failed
  install removes the version directory again, so the effect is the same.
* the private plugins depend on ``mcpkit = {path = "../mcpkit"}``. When that
  path does not exist next to the installed copy, the runtime's own mcpkit is
  copied there (or, for any other relative dependency, the folder next to the
  source the plugin was copied from), so ``uv sync`` resolves it unchanged.

A failure anywhere - fetch, validation, install steps, the first start's health
check - rolls back to what was there before: the previous version and its
``current``, the lock entry and the configuration entry. A plugin that needs
configuration (required settings or secrets, or mcpkit exiting with code 2) is
not a failure: it stays installed as ``needs_config``.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import secrets as _secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .. import paths, sections
from . import schema

MANIFEST_NAME = "mcp-service.json"
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
#: Shipped with the runtime: can be disabled, never removed (design §6.1).
BUILTIN = frozenset({"device"})
#: Never copied from a folder: rebuilt by the install steps, or plain clutter.
COPY_EXCLUDE = frozenset({".venv", ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox"})
STEP_TIMEOUT_S = 600
HEALTH_TIMEOUT_S = 90
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
STAGING = ".staging"

Log = Callable[[str], None]


class InstallError(Exception):
    def __init__(self, message: str, code: str = "install_failed"):
        super().__init__(message)
        self.code = code


@dataclass
class Source:
    """Where a plugin comes from. ``type`` is ``path``, ``git``, ``archive`` or ``hub``.

    ``hub`` is a package from the gateway's plugin repository (design 6.12), already
    downloaded to ``path``; the lock records ``{type: "hub", name, version, sha256}``.
    """

    type: str
    path: str = ""
    url: str = ""
    ref: str = ""
    sha256: str = ""
    link: bool = False
    filename: str = ""
    package: str = ""
    package_version: str = ""

    def record(self) -> dict[str, Any]:
        if self.type == "hub":
            return {"type": "hub", "name": self.package, "version": self.package_version, "sha256": self.sha256}
        data = {k: v for k, v in asdict(self).items() if v not in ("", False)}
        if self.type == "path" and self.link:
            data["type"] = "link"
        data.pop("link", None)
        return data


def parse_source(text: str, *, ref: str = "", link: bool = False, sha256: str = "") -> Source:
    """``anx plugin install <text>``: a folder, an archive (file or URL) or a git URL."""
    text = text.strip()
    if not text:
        raise InstallError("需要来源：文件夹路径、git 地址或压缩包", "invalid_source")
    candidate = Path(text).expanduser()
    if candidate.is_dir() and (candidate / "HEAD").is_file() and (candidate / "objects").is_dir():
        return Source("git", url=str(candidate.resolve()), ref=ref)  # a bare repository on disk
    if candidate.is_dir():
        return Source("path", path=str(candidate.resolve()), link=link)
    if candidate.is_file():
        return Source("archive", path=str(candidate.resolve()), sha256=sha256, filename=candidate.name)
    lowered = text.lower().split("?", 1)[0]
    if lowered.startswith(("http://", "https://")) and lowered.endswith((".zip", ".tar.gz", ".tgz")):
        return Source("archive", url=text, sha256=sha256, filename=PurePosixPath(urllib.parse.urlsplit(text).path).name)
    if re.match(r"^(https?|ssh|git|file)://", text) or re.match(r"^[\w.-]+@[\w.-]+:", text) or lowered.endswith(".git"):
        return Source("git", url=text, ref=ref)
    raise InstallError(f"找不到 {text}：不是已有的文件夹、压缩包，也不像 git 地址", "invalid_source")


def source_from_body(body: dict[str, Any], uploads: Path | None = None) -> Source:
    """The console's ``{"source": "path"|"git"|"archive", ...}`` (docs/ui-spec.md §4.2)."""
    kind = str(body.get("source") or "")
    if kind == "path":
        path = str(body.get("path") or "").strip().strip('"')
        if not path:
            raise InstallError("请填写插件文件夹的路径", "invalid_source")
        folder = Path(path).expanduser()
        if not folder.is_dir():
            raise InstallError(f"文件夹不存在：{path}", "invalid_source")
        return Source("path", path=str(folder.resolve()), link=bool(body.get("link")))
    if kind == "git":
        url = str(body.get("url") or "").strip()
        if not url:
            raise InstallError("请填写仓库地址", "invalid_source")
        return Source("git", url=url, ref=str(body.get("ref") or "").strip())
    if kind in ("archive", "zip"):
        upload = str(body.get("upload_id") or "")
        if upload:
            if uploads is None or not re.fullmatch(r"u_[0-9a-f]{16}", upload):
                raise InstallError("上传的文件已失效，请重新选择", "invalid_source")
            files = [p for p in (uploads / upload).glob("*") if p.is_file()] if (uploads / upload).is_dir() else []
            if not files:
                raise InstallError("上传的文件已失效，请重新选择", "invalid_source")
            return Source("archive", path=str(files[0]), sha256=str(body.get("sha256") or ""), filename=files[0].name)
        if body.get("url"):
            return parse_source(str(body["url"]), sha256=str(body.get("sha256") or ""))
        if body.get("path"):
            path = Path(str(body["path"]).strip().strip('"'))
            if not path.is_file():
                raise InstallError(f"文件不存在：{path}", "invalid_source")
            return Source("archive", path=str(path.resolve()), sha256=str(body.get("sha256") or ""), filename=path.name)
        raise InstallError("请选择压缩包", "invalid_source")
    raise InstallError("来源只能是 path、git 或 archive", "invalid_source")


# -- small helpers ---------------------------------------------------------------------

def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _rand() -> str:
    return _secrets.token_hex(4)


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise InstallError(f"{path.name}: 顶层必须是 JSON 对象", "invalid_manifest")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{_rand()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _on_rm_error(func, path, _exc):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def remove_tree(path: Path, attempts: int = 8) -> bool:
    """``rmtree`` that survives read-only files and Windows' brief locks after a process exits."""
    path = Path(path)
    for attempt in range(attempts):
        if not path.exists() and not path.is_symlink():
            return True
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path, onerror=_on_rm_error)
        except OSError:
            pass
        if not path.exists():
            return True
        time.sleep(0.25 * (attempt + 1))
    return not path.exists()


def _replace(src: Path, dst: Path, attempts: int = 8) -> None:
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.25 * (attempt + 1))


def copy_tree(src: Path, dst: Path, *, keep_node_modules: bool) -> None:
    """Copy a plugin folder without virtual environments, VCS data, caches and ``*.log``."""
    src = Path(src)

    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped = {n for n in names if n in COPY_EXCLUDE or (n == "node_modules" and not keep_node_modules)}
        skipped |= {n for n in names if n.endswith((".log", ".pyc")) and (Path(directory) / n).is_file()}
        return skipped

    shutil.copytree(src, dst, ignore=ignore, symlinks=False, dirs_exist_ok=False)


def lock_path(root: Path) -> Path:
    return Path(root) / sections.LOCK_PATH


def read_lock(root: Path) -> dict[str, Any]:
    return sections.read_lock(root)


def write_lock(root: Path, lock: dict[str, Any]) -> None:
    write_json(lock_path(root), lock)


def plugins_dir(root: Path) -> Path:
    return Path(root) / "plugins"


def bundled_mcpkit(root: Path) -> Path | None:
    """The runtime's mcpkit source: the installed root's, else this checkout's."""
    here = Path(__file__).resolve().parents[1]
    for candidate in (Path(root) / "builtins" / "mcpkit", here / "builtins" / "mcpkit", here.parent / "builtins" / "mcpkit"):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


# -- fetching --------------------------------------------------------------------------

def _run(argv: list[str], cwd: Path | None, log: Log, *, env: dict[str, str] | None = None,
         timeout: float = STEP_TIMEOUT_S, quiet: bool = False) -> str:
    exe = shutil.which(argv[0], path=(env or os.environ).get("PATH")) or argv[0]
    if not quiet:
        log("$ " + " ".join(argv))
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.Popen([exe, *argv[1:]], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags)
    except OSError as exc:
        raise InstallError(f"无法运行 {argv[0]}：{exc}", "command_not_found") from None
    output: list[str] = []
    expired = threading.Event()

    def kill() -> None:
        expired.set()
        proc.kill()

    timer = threading.Timer(timeout, kill)
    timer.start()
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            output.append(line)
            if not quiet and line:
                log(line)
        code = proc.wait()
    finally:
        timer.cancel()
    if code != 0:
        tail = "\n".join(output[-12:])
        what = f"{Path(argv[0]).stem} {' '.join(argv[1:2])}".strip()
        reason = f"超过 {int(timeout)} 秒没有完成" if expired.is_set() else f"退出码 {code}"
        raise InstallError(f"{what} 失败（{reason}）\n{tail}".strip(), "step_failed")
    return "\n".join(output)


def _git(args: list[str], cwd: Path | None, log: Log, quiet: bool = False) -> str:
    if not shutil.which("git"):
        raise InstallError("没有找到 git：先安装 Git for Windows，或改用文件夹 / 压缩包安装", "command_not_found")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return _run(["git", *args], cwd, log, env=env, timeout=STEP_TIMEOUT_S, quiet=quiet)


def _semver_key(tag: str) -> tuple:
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$", tag)
    return tuple(int(x) for x in match.groups()) if match else ()


def latest_tag(url: str, log: Log) -> str:
    """The highest ``vX.Y.Z`` tag of a repository, or "" when it has none."""
    try:
        out = _git(["ls-remote", "--tags", "--refs", url], None, log, quiet=True)
    except InstallError:
        return ""
    tags = [line.rsplit("refs/tags/", 1)[-1] for line in out.splitlines() if "refs/tags/" in line]
    tags = [t for t in tags if _semver_key(t)]
    return max(tags, key=_semver_key) if tags else ""


def git_clone(url: str, ref: str, dest: Path, log: Log) -> dict[str, str]:
    ref = ref or latest_tag(url, log)
    args = ["clone", "--depth", "1", "--quiet"]
    if ref:
        args += ["--branch", ref]
    _git([*args, url, str(dest)], None, log)
    commit = _git(["rev-parse", "HEAD"], dest, log, quiet=True).strip()
    log(f"已检出 {ref or '默认分支'} @ {commit[:12]}")
    remove_tree(dest / ".git")
    return {"ref": ref, "commit": commit}


WINDOWS_BAD_CHARS = frozenset('<>:"|?*')
WINDOWS_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                              *(f"LPT{i}" for i in range(1, 10))})


def _safe_member(name: str) -> PurePosixPath:
    clean = name.replace("\\", "/")
    path = PurePosixPath(clean)
    if (clean.startswith("/") or re.match(r"^[A-Za-z]:", clean) or any(part == ".." for part in path.parts)):
        raise InstallError(f"压缩包里有不安全的路径：{name}", "unsafe_archive")
    # Windows rewrites these on write: ``config.json.``, ``config.json `` and ``config.json::$DATA`` all become
    # ``config.json``, so a name check on the raw member would miss them. Refuse them outright.
    for part in path.parts:
        if part.rstrip(". ") != part or any(ch in WINDOWS_BAD_CHARS or ord(ch) < 32 for ch in part)                 or part.split(".", 1)[0].upper() in WINDOWS_RESERVED:
            raise InstallError(f"压缩包里有 Windows 上不合法的文件名（结尾的点或空格、冒号、保留名等）：{name!r}",
                               "unsafe_archive")
    return path


def extract_archive(archive: Path, dest: Path) -> None:
    """Unpack a ``.zip`` / ``.tar.gz``, refusing absolute paths, ``..`` and links."""
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            members = zf.infolist()
            for info in members:
                _safe_member(info.filename)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise InstallError(f"压缩包里有符号链接：{info.filename}", "unsafe_archive")
                total += info.file_size
            if total > MAX_ARCHIVE_BYTES * 4:
                raise InstallError("压缩包解开后太大", "unsafe_archive")
            for info in members:
                target = dest.joinpath(*_safe_member(info.filename).parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        return
    try:
        tf = tarfile.open(archive, "r:*")
    except tarfile.TarError:
        raise InstallError("不认识的压缩包格式：只支持 .zip 和 .tar.gz", "invalid_archive") from None
    with tf:
        members = tf.getmembers()
        for member in members:
            _safe_member(member.name)
            if not (member.isfile() or member.isdir()):
                raise InstallError(f"压缩包里有链接或特殊文件：{member.name}", "unsafe_archive")
            total += member.size
        if total > MAX_ARCHIVE_BYTES * 4:
            raise InstallError("压缩包解开后太大", "unsafe_archive")
        for member in members:
            target = dest.joinpath(*_safe_member(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                continue
            with src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def download(url: str, dest: Path, log: Log) -> Path:
    log(f"下载 {url}")
    opener = urllib.request.build_opener()
    with opener.open(urllib.request.Request(url, headers={"User-Agent": "agent-nexus"}), timeout=60) as resp:
        size = 0
        with open(dest, "wb") as out:
            while chunk := resp.read(1 << 16):
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise InstallError("压缩包超过 200 MB", "too_large")
                out.write(chunk)
    return dest


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def find_plugin_dir(base: Path) -> Path:
    """``base`` or its single top-level folder, whichever holds the manifest."""
    if (base / MANIFEST_NAME).is_file():
        return base
    children = [p for p in base.iterdir() if not p.name.startswith((".", "__MACOSX"))]
    if len(children) == 1 and children[0].is_dir() and (children[0] / MANIFEST_NAME).is_file():
        return children[0]
    raise InstallError(f"没有找到 {MANIFEST_NAME}：插件文件夹的根目录里需要这个文件", "invalid_manifest")


def fetch(source: Source, work: Path, log: Log) -> tuple[Path, dict[str, str]]:
    """The plugin directory for ``source`` (inside ``work`` unless link mode), plus what was resolved."""
    if source.type == "path":
        folder = Path(source.path)
        plugin = find_plugin_dir(folder)
        if source.link:
            return plugin.resolve(), {}
        dest = work / "src"
        log(f"复制 {plugin}")
        copy_tree(plugin, dest, keep_node_modules=shutil.which("npm") is None)
        return dest, {}
    if source.type == "git":
        dest = work / "src"
        log(f"克隆 {source.url}")
        return dest, git_clone(source.url, source.ref, dest, log)
    if source.type in ("archive", "hub"):
        archive = Path(source.path) if source.path else download(source.url, work / (source.filename or "plugin.zip"), log)
        if source.sha256:
            actual = sha256_of(archive)
            if actual.lower() != source.sha256.lower():
                raise InstallError(f"sha256 不匹配：期望 {source.sha256}，实际 {actual}", "checksum_mismatch")
        dest = work / "src"
        log(f"解压 {source.filename or archive.name}")
        extract_archive(archive, dest)
        return find_plugin_dir(dest), {"sha256": sha256_of(archive)}
    raise InstallError(f"未知来源类型：{source.type}", "invalid_source")


# -- manifests -------------------------------------------------------------------------

def read_manifest(directory: Path) -> dict[str, Any]:
    try:
        return read_json(directory / MANIFEST_NAME)
    except (OSError, ValueError) as exc:
        raise InstallError(f"{MANIFEST_NAME} 读不出来：{exc}", "invalid_manifest") from None


def _pyproject(directory: Path) -> dict[str, Any]:
    path = directory / "pyproject.toml"
    if not path.is_file():
        return {}
    import tomllib
    try:
        return tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def project_dir(directory: Path, manifest: dict[str, Any]) -> Path | None:
    """Where the plugin's Python project is: its ``cwd`` when that holds a pyproject, else the plugin root."""
    cwd = directory / str(manifest.get("cwd") or ".")
    for candidate in (cwd, directory):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def plugin_version(directory: Path, manifest: dict[str, Any]) -> str:
    version = str(manifest.get("version") or "").strip()
    if not version:
        project = project_dir(directory, manifest)
        version = str((_pyproject(project).get("project") or {}).get("version") or "") if project else ""
    version = version or "0.0.0"
    if not VERSION_PATTERN.match(version):
        raise InstallError(f"版本号不合法：{version!r}", "invalid_manifest")
    return version


def plugin_name(directory: Path, manifest: dict[str, Any]) -> str:
    name = str(manifest.get("name") or directory.name.removesuffix("-mcp")).strip()
    if not NAME_PATTERN.match(name):
        raise InstallError(f"插件名不合法：{name!r}（小写字母、数字和 -，最长 40）", "invalid_manifest")
    return name


def _command(manifest: dict[str, Any]) -> list[str]:
    command = manifest.get("command")
    if isinstance(command, dict):
        command = command.get(sys.platform) or command.get("default")
    return [str(a) for a in command] if isinstance(command, list) else []


def install_steps(directory: Path, manifest: dict[str, Any], root: Path) -> list[tuple[list[str], Path]]:
    """``install.steps`` (v2), else what a v1 plugin needs: ``npm ci`` and ``uv sync``."""
    declared = (manifest.get("install") or {}).get("steps") if isinstance(manifest.get("install"), dict) else None
    data = schema_data_dir(root, plugin_name(directory, manifest))
    subst = {"{dir}": str(directory), "{root}": str(root), "{data}": str(data), "{wheels}": str(Path(root) / "wheels")}

    def expand(argv: list[Any]) -> list[str]:
        out = []
        for arg in argv:
            arg = str(arg)
            for key, value in subst.items():
                arg = arg.replace(key, value)
            out.append(arg)
        return out

    if isinstance(declared, list):
        steps = []
        for step in declared:
            if not isinstance(step, dict):
                raise InstallError("install.steps 的每一步必须是对象", "invalid_manifest")
            cwd = directory / str(step.get("cwd") or ".")
            if "run" in step:
                argv = step["run"]
                if isinstance(argv, dict):
                    argv = argv.get(sys.platform) or argv.get("default")
                    if argv is None:
                        continue
            elif "uv" in step:
                argv = ["uv", *(step["uv"] or [])]
            elif "npm" in step:
                argv = ["npm", *(step["npm"] or [])]
            else:
                raise InstallError(f"install.steps 里有不认识的步骤：{sorted(step)}", "invalid_manifest")
            if not isinstance(argv, list) or not argv:
                raise InstallError("install.steps 的命令必须是非空数组", "invalid_manifest")
            steps.append((expand(argv), cwd))
        return steps

    steps: list[tuple[list[str], Path]] = []
    if (directory / "package.json").is_file() and not (directory / "node_modules").is_dir():
        npm = ["npm", "ci" if (directory / "package-lock.json").is_file() else "install",
               "--omit=dev", "--no-audit", "--no-fund"]
        steps.append((npm, directory))
    project = project_dir(directory, manifest)
    if project is not None:
        argv = ["uv", "sync"]
        command = _command(manifest)
        for i, arg in enumerate(command):
            if arg in ("--extra", "--group") and i + 1 < len(command):
                argv += [arg, command[i + 1]]
            elif arg in ("--all-extras", "--no-dev"):
                argv.append(arg)
        steps.append((argv, project))
    return steps


def schema_data_dir(root: Path, name: str) -> Path:
    return Path(root) / "plugin-data" / name


def path_dependencies(project: Path) -> list[tuple[str, str]]:
    """``[tool.uv.sources]`` entries with a relative ``path``: ``[(name, path)]``."""
    sources = ((_pyproject(project).get("tool") or {}).get("uv") or {}).get("sources") or {}
    result = []
    for dep, spec in sources.items():
        if isinstance(spec, dict) and isinstance(spec.get("path"), str) and not Path(spec["path"]).is_absolute():
            result.append((str(dep), spec["path"]))
    return result


def provide_path_dependencies(root: Path, name: str, installed: Path, manifest: dict[str, Any],
                              original: Path | None, log: Log) -> list[str]:
    """Make each relative path dependency of the installed copy resolve (see the module docstring)."""
    project = project_dir(installed, manifest)
    if project is None:
        return []
    home = (plugins_dir(root) / name).resolve()
    provided = []
    for dep, rel in path_dependencies(project):
        target = (project / rel).resolve()
        if target.exists():
            continue
        origin = None
        if original is not None:
            rel_project = project.relative_to(installed)
            candidate = (original / rel_project / rel).resolve()
            if (candidate / "pyproject.toml").is_file() or (candidate / "setup.py").is_file():
                origin = candidate
        if origin is None and dep == "mcpkit":
            origin = bundled_mcpkit(root)
        if origin is None:
            log(f"注意：依赖 {dep} 的路径 {rel} 不存在，uv sync 可能失败")
            continue
        if not target.is_relative_to(home):
            raise InstallError(f"依赖 {dep} 的路径 {rel} 指到插件目录之外，无法自动提供；请改用 git 或 PyPI 依赖",
                               "unsupported_dependency")
        if dep == "mcpkit" and origin != bundled_mcpkit(root) and bundled_mcpkit(root) is not None:
            origin = bundled_mcpkit(root)  # one mcpkit for every plugin: the runtime's (design §6.7)
        log(f"提供依赖 {dep}：{origin} → {target}")
        remove_tree(target)
        shutil.copytree(origin, target, ignore=shutil.ignore_patterns(*COPY_EXCLUDE, "tests", "node_modules", "*.pyc"))
        provided.append(dep)
    return provided


def describe(directory: Path, manifest: dict[str, Any], root: Path) -> dict[str, Any]:
    """What the install dialog shows before anything is installed (``POST /api/plugins/inspect``)."""
    name = plugin_name(directory, manifest)
    version = plugin_version(directory, manifest)
    info = schema.effective(name, manifest)
    project = project_dir(directory, manifest)
    requirements, warnings = [], []
    if project is not None:
        python = (_pyproject(project).get("project") or {}).get("requires-python")
        requirements.append(f"Python {python}" if python else "Python")
        requirements.append("uv（自动创建插件自己的环境）")
    if (directory / "package.json").is_file():
        requirements.append("Node.js / npm")
        if not shutil.which("npm") and not (directory / "node_modules").is_dir():
            warnings.append("没有找到 npm：需要先安装 Node.js")
    platforms = [str(p) for p in manifest.get("platforms") or []]
    if platforms and sys.platform not in platforms:
        warnings.append(f"只支持 {', '.join(platforms)}，这台电脑是 {sys.platform}")
    for binary in ((manifest.get("requires") or {}).get("binaries") or []) if isinstance(manifest.get("requires"), dict) else []:
        if isinstance(binary, dict) and binary.get("hint"):
            warnings.append(str(binary["hint"]))
    lock = read_lock(root)
    current = lock.get(name) if isinstance(lock.get(name), dict) else None
    try:
        steps = [" ".join(argv) for argv, _ in install_steps(directory, manifest, root)]
    except InstallError as exc:
        steps, warnings = [], [*warnings, str(exc)]
    secrets_needed = [s["name"] for s in info["secrets"] if s.get("required")]
    required = (schema.schema_of(info).get("required") or [])
    return {
        "name": name, "version": version, "title": info["display_name"],
        "description": str(manifest.get("description") or ""),
        "kind": str(manifest.get("kind") or "process"), "transport": "MCP over HTTP",
        "port": manifest.get("port"), "platforms": platforms, "schema_version": int(manifest.get("schema_version") or 1),
        "requirements": requirements, "warnings": warnings, "install_steps": steps,
        "permissions": ((manifest.get("permissions") or {}).get("declared") or []) if isinstance(manifest.get("permissions"), dict) else [],
        "needs_config": bool(secrets_needed or required), "config_fields": [*required, *secrets_needed],
        "exists": current is not None, "installed_version": (current or {}).get("version"),
        "builtin": name in BUILTIN,
    }


# -- configuration entries ---------------------------------------------------------------

def read_entry(root: Path, name: str) -> dict[str, Any] | None:
    data = paths.read_config(root)
    return sections.plugin_entries(data, keep_install=True).get(name)


def write_entry(root: Path, name: str, entry: dict[str, Any] | None) -> None:
    """Replace (or with ``None`` remove) the plugin's configuration entry, in both sections."""
    path = paths.config_path(root)
    data = paths.read_config(root) if path.is_file() else {"manager": {"host": "127.0.0.1", "port": 8399}, "plugins": {}}
    section = sections.entry_section(data)
    for other in ("services", "plugins"):
        if isinstance(data.get(other), dict):
            data[other].pop(name, None)
    if entry is not None:
        data.setdefault(section, {})[name] = entry
    write_json(paths.write_path(root), data)


def update_entry(root: Path, name: str, **updates: Any) -> dict[str, Any]:
    entry = dict(read_entry(root, name) or {})
    for key, value in updates.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    write_entry(root, name, entry)
    return entry


# -- the installer -----------------------------------------------------------------------

@dataclass
class Result:
    name: str
    version: str
    state: str  # running | installed | needs_config | conflict | disabled
    message: str = ""
    log: list[str] = field(default_factory=list)


class Installer:
    """Install / uninstall against a root, using a supervisor for stop, start and the health check.

    ``sup`` is the live supervisor when the manager runs (the console); without
    one (``anx plugin install`` with no manager running) a private one is used
    and the plugin is stopped again after its health check.
    """

    def __init__(self, root: Path, sup: Any = None, log: Log | None = None, *, health_timeout: float | None = None,
                 lock: threading.Lock | None = None):
        self.root = Path(root).resolve()
        self.sup = sup
        self.log_lines: list[str] = []
        self._log = log
        self.health_timeout = health_timeout
        self._lock = lock or threading.Lock()

    def log(self, line: str) -> None:
        self.log_lines.append(line)
        if self._log is not None:
            self._log(line)
        try:
            path = self.root / paths.STATE_DIR_NAME / "logs" / "plugins" / "install.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as out:
                out.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
        except OSError:
            pass

    # -- supervisor plumbing --

    def _supervisor(self):
        if self.sup is None:
            from ..supervisor import Supervisor
            self.sup = Supervisor(self.root)
            self._private = True
        return self.sup

    def _running(self, name: str) -> bool:
        sup = self._supervisor()
        sup.rescan()
        if name not in sup.specs:
            return False
        rt = sup.rt.get(name)
        return bool(rt and rt.pid is not None)

    def _stop(self, name: str) -> None:
        sup = self._supervisor()
        if name in sup.specs and not sup.specs[name].observed:
            try:
                self.log(sup.stop(name))
            except Exception as exc:  # SupervisorError: unknown or observe-only
                self.log(f"停止 {name}：{exc}")

    def _await_health(self, name: str) -> tuple[str, str]:
        """Start ``name`` and wait for its first health result: ``(state, message)``."""
        from ..supervisor import SupervisorError
        sup = self._supervisor()
        sup.rescan()
        spec = sup.spec(name)
        if spec.missing:
            return "needs_config", "缺少：" + "、".join(spec.missing)
        if not spec.enabled:
            return "disabled", ""
        try:
            self.log(sup.start(name))
        except SupervisorError as exc:
            text = str(exc)
            if "is held by" in text:
                return "conflict", f"端口 {spec.port} 被其他程序占用：{text}；在配置里换一个端口"
            raise InstallError(f"启动失败：{text}", "start_failed") from None
        timeout = self.health_timeout or float(self._manifest_health(spec.dir).get("startup_timeout_s") or HEALTH_TIMEOUT_S)
        self.log(f"等待健康检查（最长 {int(timeout)} 秒）…")
        deadline = time.monotonic() + timeout
        background = getattr(sup, "_thread", None) is not None
        last = ""
        while time.monotonic() < deadline:
            if not background:
                sup.tick()
            snap = sup.snapshot(name)
            status, last = snap["status"], snap.get("message") or ""
            if status == "running":
                return "running", ""
            if status == "needs_config":
                return "needs_config", last
            if status in ("crashed", "invalid", "conflict"):
                raise InstallError(f"插件启动后退出了：{last or status}\n{self._log_tail(name)}", "health_failed")
            time.sleep(0.5)
        raise InstallError(f"{int(timeout)} 秒内没有通过健康检查（{status}：{last}）\n{self._log_tail(name)}", "health_failed")

    @staticmethod
    def _manifest_health(directory: Path) -> dict[str, Any]:
        try:
            health = read_json(directory / MANIFEST_NAME).get("health")
        except (InstallError, OSError, ValueError):
            return {}
        return health if isinstance(health, dict) else {}

    def _log_tail(self, name: str, lines: int = 15) -> str:
        from .. import logs
        try:
            text, _ = logs.tail(self._supervisor().stdout_log(name), lines)
        except OSError:
            return ""
        return text.strip()

    # -- install --

    def install(self, source: Source, *, enable: bool = True, port: int | None = None) -> Result:
        with self._lock:
            return self._install(source, enable=enable, port=port)

    def _install(self, source: Source, *, enable: bool, port: int | None) -> Result:
        root = self.root
        staging = plugins_dir(root) / STAGING
        work = staging / f"w-{_rand()}"
        work.mkdir(parents=True, exist_ok=True)
        undo: list[Callable[[], None]] = []
        name = version = ""
        restart_after_rollback = False
        try:
            plugin, resolved = fetch(source, work, self.log)
            manifest = read_manifest(plugin)
            name, version = plugin_name(plugin, manifest), plugin_version(plugin, manifest)
            self.log(f"找到插件 {name} {version}")
            if source.type == "hub" and (name, version) != (source.package, source.package_version):
                raise InstallError(f"插件库的包是 {name} {version}，不是请求的 {source.package} {source.package_version}",
                                   "checksum_mismatch")
            self._validate(name, manifest, plugin, source)

            lock_before = read_lock(root)
            entry_before = read_entry(root, name)
            previous = lock_before.get(name) if isinstance(lock_before.get(name), dict) else None
            was_running = self._running(name)
            undo.append(lambda: write_lock(root, lock_before) if lock_before or lock_path(root).exists() else None)
            undo.append(lambda: write_entry(root, name, entry_before))
            if was_running:
                self._stop(name)
                restart_after_rollback = True

            record = {"version": version, "source": {**source.record(), **resolved}, "installed_at": _now(),
                      "manifest_sha256": sha256_of(plugin / MANIFEST_NAME)}
            if previous and previous.get("version") and previous.get("version") != version:
                record["previous"] = previous.get("version")
            original = Path(source.path) if source.type == "path" else None
            if source.type == "path" and source.link:
                record.update(mode="link", path=str(plugin))
                target = plugin
            else:
                record["mode"] = "managed"
                target = self._place(name, version, plugin, previous, undo)
                self._carry_data(name, manifest, target, undo)
            if original is not None:
                original = find_plugin_dir(original)
            provide_path_dependencies(root, name, target, manifest, original, self.log)

            env = self._step_env(name, target)
            steps = install_steps(target, manifest, root)
            for argv, cwd in steps:
                from ..supervisor import resolve_executable
                argv = [resolve_executable(argv[0], env, root), *argv[1:]]
                timeout = float((manifest.get("install") or {}).get("timeout_s") or STEP_TIMEOUT_S) \
                    if isinstance(manifest.get("install"), dict) else STEP_TIMEOUT_S
                _run(argv, cwd, self.log, env=env, timeout=timeout)
            if not steps:
                self.log("没有安装步骤")

            if record["mode"] == "managed":
                (plugins_dir(root) / name / "current").write_text(version + "\n", encoding="utf-8")
            lock = read_lock(root)
            lock[name] = record
            write_lock(root, lock)
            updates: dict[str, Any] = {"enabled": bool(enable)}
            if enable:
                updates["autostart"] = True
            if port is not None:
                updates["port"] = int(port)
            entry = dict(entry_before or {})
            entry.pop("install", None)
            entry.update(updates)
            for key in ("dir", "command"):  # a stale config-only definition must not shadow the manifest
                entry.pop(key, None)
            write_entry(root, name, entry)

            state, message = ("disabled", "已安装，尚未启用") if not enable else self._await_health(name)
            if self._private_supervisor() and state == "running":
                self._stop(name)  # the manager starts it; this was only the health check
                state, message = "installed", "已通过健康检查；agent-nexus 启动时会自动运行它"
            self.log({"running": f"{name} {version} 已安装并在运行",
                      "needs_config": f"{name} {version} 已安装，需要配置：{message}",
                      "conflict": f"{name} {version} 已安装，但{message}",
                      }.get(state, f"{name} {version} 已安装（{message or state}）"))
            self._prune(name, keep={version, record.get("previous")})
            self._rescan()
            return Result(name, version, state, message, list(self.log_lines))
        except Exception as exc:
            error = exc if isinstance(exc, InstallError) else InstallError(f"{type(exc).__name__}: {exc}")
            self.log(f"安装失败：{error}")
            if name:
                self._stop_quietly(name)
            for step in reversed(undo):
                try:
                    step()
                except Exception as undo_exc:  # keep rolling back the rest
                    self.log(f"回滚时出错：{undo_exc}")
            if undo:
                self.log("已回滚到安装前的状态")
            self._rescan()
            if restart_after_rollback:
                self._restart_after_rollback(name)
            raise error from None
        finally:
            remove_tree(work)

    def _private_supervisor(self) -> bool:
        return bool(getattr(self, "_private", False))

    def _rescan(self) -> None:
        if self.sup is not None:
            try:
                self.sup.rescan()
                self.sup.tick()
            except Exception:
                pass

    def _stop_quietly(self, name: str) -> None:
        try:
            sup = self._supervisor()
            sup.rescan()
            if name in sup.specs and sup.rt.get(name) and sup.rt[name].pid is not None:
                sup.stop(name)
        except Exception:
            pass

    def _restart_after_rollback(self, name: str) -> None:
        sup = self._supervisor()
        sup.rescan()
        if name in sup.specs and sup.specs[name].runnable:
            try:
                self.log(sup.start(name))
            except Exception as exc:
                self.log(f"恢复运行 {name} 失败：{exc}")

    def _validate(self, name: str, manifest: dict[str, Any], plugin: Path, source: Source) -> None:
        if name in BUILTIN:
            raise InstallError(f"{name} 是内置插件，随 Agent Nexus 一起更新，不能另外安装", "builtin")
        kind = str(manifest.get("kind") or "process")
        if kind not in ("process", "observe"):
            raise InstallError(f"不支持的插件类型 {kind!r}", "invalid_manifest")
        if kind == "process" and not _command(manifest):
            raise InstallError(f"{MANIFEST_NAME} 缺少 command", "invalid_manifest")
        platforms = [str(p) for p in manifest.get("platforms") or []]
        if platforms and sys.platform not in platforms:
            raise InstallError(f"{name} 只支持 {', '.join(platforms)}", "unsupported_platform")
        requires = manifest.get("requires") if isinstance(manifest.get("requires"), dict) else {}
        if isinstance(requires.get("agent_nexus"), str) and not _satisfies(requires["agent_nexus"]):
            from .. import __version__
            raise InstallError(f"{name} 需要 agent-nexus {requires['agent_nexus']}，当前是 {__version__}：请先更新 Agent Nexus",
                               "incompatible")
        # The same name from somewhere else (a scanned folder, a builtin) would be shadowed silently.
        from ..registry import discover, load_manager_config
        lock = read_lock(self.root)
        if name not in lock:
            spec = discover(load_manager_config(self.root)).get(name)
            if spec is not None and spec.installed and spec.source != "config":
                raise InstallError(f"已经有一个 {name}（{spec.dir}），不是通过插件页安装的；先删除或改名再安装",
                                   "name_taken")

    def _place(self, name: str, version: str, plugin: Path, previous: dict | None, undo: list) -> Path:
        home = plugins_dir(self.root) / name
        home.mkdir(parents=True, exist_ok=True)
        target = home / version
        current_file = home / "current"
        current_before = current_file.read_text(encoding="utf-8").strip() if current_file.is_file() else None
        if previous is None:
            undo.append(lambda: self._remove_if_empty(home))
        undo.append(lambda: (current_file.write_text(current_before + "\n", encoding="utf-8") if current_before
                             else current_file.unlink(missing_ok=True)))
        # The owner's data lives in the version that is current now (``_carry_data`` moves it into ``target``).
        managed = (previous or {}).get("mode", "managed") == "managed"
        self._data_from = home / current_before if current_before and current_before != version and managed else None
        if target.exists():  # reinstalling the same version: keep the old copy until this one works
            backup = home / f".old-{version}-{_rand()}"
            _replace(target, backup)
            undo.append(lambda: (remove_tree(target), _replace(backup, target)))
            self._cleanup_later.append(backup)
            if current_before == version or not current_before:
                self._data_from = backup
        else:
            undo.append(lambda: remove_tree(target))
        _replace(plugin, target)
        return target

    def _carry_data(self, name: str, manifest: dict[str, Any], target: Path, undo: list) -> None:
        """Move the owner's data (``data[]``, legacy hints) from the version being replaced into ``target``.

        v1 plugins such as payment keep ``config.json``, ``qrcodes/`` and ``secrets/`` in their code folder; without
        this an update would strand them in the old version folder, and a reinstall of the same version (or the
        next update) would delete them with the backup it prunes (design §6.8: ``data[]`` is kept on update).
        A failed install moves them back.
        """
        source = getattr(self, "_data_from", None)
        if source is None or not source.is_dir():
            return
        from .pack import data_paths
        try:
            old_manifest = read_manifest(source)
        except Exception:
            old_manifest = {}
        wanted = []
        for item in data_paths(name, manifest) + data_paths(name, old_manifest):
            item = item.rstrip("/")
            if (item and item not in wanted and not any(ch in item for ch in "*?[!")
                    and all(part not in ("", ".", "..") for part in item.split("/"))):
                wanted.append(item)
        for item in wanted:
            src, dst = source.joinpath(*item.split("/")), target.joinpath(*item.split("/"))
            if not src.exists() or src.is_symlink():
                continue
            if dst.exists():  # the package shipped its own copy: the owner's data wins
                shipped = target.parent / f".shipped-{_rand()}"
                _replace(dst, shipped)
                undo.append(lambda dst=dst, shipped=shipped: _replace(shipped, dst) if shipped.exists() else None)
                self._cleanup_later.append(shipped)
            dst.parent.mkdir(parents=True, exist_ok=True)
            _replace(src, dst)
            undo.append(lambda src=src, dst=dst: _replace(dst, src) if dst.exists() else None)
            self.log(f"保留数据 {item}（从 {source.name} 搬到 {target.name}）")

    @property
    def _cleanup_later(self) -> list[Path]:
        if not hasattr(self, "_later"):
            self._later: list[Path] = []
        return self._later

    @staticmethod
    def _remove_if_empty(home: Path) -> None:
        if home.is_dir() and not any(p for p in home.iterdir() if p.name != "mcpkit" and not p.name.startswith(".old-")):
            remove_tree(home)

    def _prune(self, name: str, keep: set) -> None:
        for path in self._cleanup_later:
            remove_tree(path)
        self._cleanup_later.clear()
        home = plugins_dir(self.root) / name
        if not home.is_dir():
            return
        for child in home.iterdir():
            if not child.is_dir() or child.name in keep:
                continue
            if (child / MANIFEST_NAME).is_file():  # an older version
                self.log(f"删除旧版本 {child.name}")
                remove_tree(child)

    def _step_env(self, name: str, target: Path) -> dict[str, str]:
        env = dict(os.environ)
        venv = env.pop("VIRTUAL_ENV", None)
        if venv:
            prefix = os.path.normcase(venv)
            env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                          if not os.path.normcase(p).startswith(prefix))
        env.pop("UV_RUN_RECURSION_DEPTH", None)
        data = schema_data_dir(self.root, name)
        env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "ANX_ROOT": str(self.root), "MCPM_ROOT": str(self.root),
                    "ANX_PLUGIN_ID": name, "ANX_PLUGIN_DIR": str(target), "ANX_PLUGIN_DATA": str(data),
                    "ANX_WHEELS": str(self.root / "wheels"), "UV_NO_PROGRESS": "1", "NO_COLOR": "1"})
        return env

    # -- uninstall --

    def uninstall(self, name: str, *, purge: bool = False) -> dict[str, Any]:
        with self._lock:
            return self._uninstall(name, purge=purge)

    def _uninstall(self, name: str, *, purge: bool) -> dict[str, Any]:
        if name in BUILTIN:
            raise InstallError(f"{name} 是内置插件，不能卸载；可以在插件页关闭它", "builtin")
        root = self.root
        lock = read_lock(root)
        record = lock.get(name) if isinstance(lock.get(name), dict) else None
        entry = read_entry(root, name)
        from ..registry import discover, load_manager_config
        spec = discover(load_manager_config(root)).get(name)
        if record is None and spec is not None and spec.installed:
            raise InstallError(f"{name} 不是通过插件页安装的（{spec.dir}）；请直接删除那个文件夹", "not_managed")
        if record is None and entry is None:
            raise InstallError(f"没有插件 {name}", "unknown")
        self._stop_quietly(name)
        if record is not None:
            directory = Path(record["path"]) if record.get("mode") == "link" else plugins_dir(root) / name / str(record.get("version"))
            self._uninstall_steps(name, directory)
            if record.get("mode") != "link":
                if not remove_tree(plugins_dir(root) / name):
                    raise InstallError(f"删除 {plugins_dir(root) / name} 失败：文件可能被占用，关闭相关程序后重试", "busy")
            lock.pop(name, None)
            write_lock(root, lock)
        kept = {k: v for k, v in (entry or {}).items() if k == "settings"} if not purge else {}
        write_entry(root, name, kept or None)
        removed_data = []
        if purge:
            for path in (schema_data_dir(root, name), Path(root) / schema.SECRETS_DIR / name):
                if path.exists():
                    remove_tree(path)
                    removed_data.append(str(path))
        self._rescan()
        self.log(f"已卸载 {name}" + ("，并删除了数据和凭据" if purge else "；数据和凭据保留"))
        return {"removed": True, "name": name, "data_kept": not purge, "purged": removed_data}

    def _uninstall_steps(self, name: str, directory: Path) -> None:
        try:
            manifest = read_manifest(directory)
        except InstallError:
            return
        spec = manifest.get("uninstall") if isinstance(manifest.get("uninstall"), dict) else {}
        for step in spec.get("steps") or []:
            argv = step.get("run") if isinstance(step, dict) else None
            if isinstance(argv, dict):
                argv = argv.get(sys.platform) or argv.get("default")
            if isinstance(argv, list) and argv:
                try:
                    _run([str(a) for a in argv], directory, self.log, env=self._step_env(name, directory), timeout=120)
                except InstallError as exc:  # best effort (design §6.5)
                    self.log(f"卸载步骤失败（忽略）：{exc}")


def _satisfies(spec: str) -> bool:
    """A tiny semver range check: comma-separated ``>=``, ``>``, ``<=``, ``<``, ``==``."""
    from .. import __version__

    def key(text: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", text)[:3]) + (0,) * 3

    current = key(__version__)[:3]
    for part in (p.strip() for p in spec.split(",") if p.strip()):
        match = re.match(r"^(>=|<=|==|>|<)?\s*(.+)$", part)
        if not match:
            continue
        op, value = match.group(1) or "==", key(match.group(2))[:3]
        ok = {">=": current >= value, "<=": current <= value, ">": current > value, "<": current < value,
              "==": current == value}[op]
        if not ok:
            return False
    return True


def inspect(root: Path, source: Source) -> dict[str, Any]:
    """Fetch ``source`` into staging, describe it, throw the copy away."""
    staging = plugins_dir(root) / STAGING
    work = staging / f"i-{_rand()}"
    work.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    try:
        if source.type == "path":  # describing a folder needs no copy
            plugin = find_plugin_dir(Path(source.path))
        else:
            plugin, _ = fetch(source, work, lines.append)
        return describe(plugin, read_manifest(plugin), Path(root))
    finally:
        remove_tree(work)
