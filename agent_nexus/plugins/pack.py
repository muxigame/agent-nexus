"""Plugin packages: ``anx plugin pack`` and the checks the gateway's 插件库 repeats (design §6.12).

:func:`pack` turns a plugin folder into a zip that the M8 installer installs
as an archive source (``plugins/<name>/<version>/``, the same state as a
folder install). Left out of the package:

* what the install steps rebuild or nobody needs to run the plugin:
  ``.venv``, ``.git``, ``node_modules``, ``__pycache__``, test caches, the
  plugin's own ``.gitignore`` entries, and per-plugin clutter (e.g. unity's
  ``TestProjects``, ``website``, ``docs``);
* everything that belongs to the owner rather than to the code: ``logs``,
  ``secrets``, ``config.json`` (``*.example.json`` stays), ``*.sqlite3*``,
  ``*.log``, ``.env*``, ``*keys*.json``, ``oauth*.json``, and the data paths
  the manifest declares (``data[]``, the legacy hints below, and directories
  its ``env`` points into with ``{dir}/…``).

A package that still contains anything secret-looking (a private key, a
token with a known prefix, a credential assigned in a config file, a file
named like a key store) is refused with the file and the rule that matched,
never the value. There is no override: fix the folder or its ``.gitignore``.

Names are compared **case-insensitively** everywhere (``Config.json``,
``.ENV``, ``Secrets/``, ``Logs/run.LOG``): on Windows and macOS they are the
same files as their lower-case spellings, so a plugin would read them as its
config. Credential assignments are looked for in every text file, quoted
(``API_KEY = "..."`` in a ``.py`` too) and, in yaml/ini/properties/env/shell
files, unquoted (``password: ...``, ``password = ...``).

:func:`inspect_package` is what the gateway runs on an upload: a safe zip, a
manifest with a valid name and version, and the same secret scan.
"""
from __future__ import annotations

import datetime as _dt
import fnmatch
import hashlib
import io
import json
import math
import os
import re
import stat
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from . import installer

PACK_INFO = "pack.json"
PACK_FORMAT = 1
#: Directory names never packed, at any depth.
EXCLUDED_DIRS = frozenset({
    ".venv", "venv", ".git", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".runtime-test", "logs", "secrets", ".idea", ".vscode", ".worktrees", ".claude"})
#: Not needed to install or run a plugin; test fixtures also carry fake credentials on purpose.
NOT_NEEDED_DIRS = frozenset({"tests"})
#: File name patterns never packed, at any depth (``*.example.json`` is kept).
EXCLUDED_FILES = ("config.json", "*.sqlite3", "*.sqlite3-*", "*.log", ".env", ".env.*", "*keys*.json", "oauth*.json",
                  "*.pyc", "*.pyo", ".DS_Store", "Thumbs.db")
KEEP_FILES = ("*.example.json",)
EXCLUDED_DIR_PATTERNS = ("*.egg-info",)
#: What the gateway refuses inside an uploaded package: owner data, never code.
SECRET_DIRS = frozenset({"logs", "secrets", ".git", ".runtime-test"})
SECRET_FILES = ("config.json", "*.sqlite3", "*.sqlite3-*", "*.log", ".env", ".env.*", "*keys*.json", "oauth*.json")
#: Data paths of the private v1 plugins, whose manifests do not declare ``data[]`` yet (design §6.8).
LEGACY_DATA: dict[str, tuple[str, ...]] = {
    "payment": ("config.json", "qrcodes/", "secrets/", "secrets-backups/", "logs/"),
    "wechat": ("runtime/", "wxwork_decrypted/", "wxwork_keys.json"),
    "jubian-go": ("config.json", "runtime/"),
    "ones": ("logs/",),
}
#: Folders that are clearly not needed to install and run a plugin (tests projects, sites, docs).
LEGACY_PRUNE: dict[str, tuple[str, ...]] = {
    "unity": ("TestProjects/", "website/", "docs/", ".github/", "unity-mcp-skill/", "scripts/local-test/"),
}
MAX_SCAN_BYTES = 2 * 1024 * 1024
#: File names that are key stores or credential files whatever their content.
SECRET_NAMES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks",
                "*.keystore", "*.kdbx", "*.ppk", ".netrc", "_netrc", ".git-credentials", ".pypirc", "credentials.json",
                "credentials*.json", "*secret*.json", "token", "*.token", "bearer-token", "device-token",
                "service-account*.json")
SECRET_CONTENT: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("私钥", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----")),
    ("AWS 访问密钥", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub 令牌", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("GitLab 令牌", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("Slack 令牌", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("API 密钥（sk-）", re.compile(r"\bsk-(?:ant-|proj-|live-)?[A-Za-z0-9_-]{32,}\b")),
    ("Google API 密钥", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("npm 令牌", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
)
#: ``password = "…"`` / ``"token": "…"`` with a literal value that looks real.
_ASSIGNED = re.compile(
    r"""(?ix)(?P<key>["']?[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|cookie)
    [\w.-]*["']?)\s*[:=]\s*(?P<q>["'])(?P<value>[^"'\s]{12,200})(?P=q)""")
#: ``password: ...`` / ``password = ...`` / ``export TOKEN=...`` without quotes (yaml, ini, properties, env, shell).
_UNQUOTED = re.compile(
    r"""(?imx)^[ \t]*(?:-[ \t]+)?(?:export[ \t]+|set[ \t]+)?
    (?P<key>[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|cookie)[\w.-]*)
    [ \t]*[:=][ \t]*(?P<value>[^\s"'#;]{12,200})[ \t]*(?:[#;].*)?\r?$""")
_PLACEHOLDER = re.compile(r"(?i)example|sample|dummy|placeholder|changeme|change-me|your|xxx|\*\*\*|<|>|\$\{|\{\{|%\(|"
                          r"test|fake|redacted|replace|todo|none|null|secret_name|token_env|env:|0000|1234|abcd")
#: Files whose values may be unquoted (``key: value`` / ``key = value``).
_UNQUOTED_SUFFIXES = (".yaml", ".yml", ".ini", ".cfg", ".conf", ".env", ".properties", ".sh", ".bash", ".bat", ".cmd",
                      ".txt", "")
_FIXED_TIME = (2020, 1, 1, 0, 0, 0)


class PackError(Exception):
    """A refusal; ``findings`` lists ``{path, rule}`` for the secret scan (never the matched value)."""

    def __init__(self, message: str, code: str = "pack_failed", findings: list[dict[str, str]] | None = None):
        super().__init__(message)
        self.code = code
        self.findings = findings or []

    def body(self) -> dict[str, Any]:
        data: dict[str, Any] = {"error": str(self), "code": self.code}
        if self.findings:
            data["findings"] = self.findings
        return data


@dataclass
class Selection:
    files: list[str] = field(default_factory=list)                 # posix paths relative to the plugin folder
    excluded: Counter = field(default_factory=Counter)            # rule -> entries left out
    examples: dict[str, list[str]] = field(default_factory=dict)  # rule -> a few paths


# -- .gitignore (the subset plugin repositories use) -----------------------------------------

@dataclass
class _Rule:
    pattern: str
    negate: bool
    dir_only: bool
    anchored: bool


def _gitignore_rules(text: str) -> list[_Rule]:
    rules = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        line = line.replace("\\ ", " ")
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        anchored = "/" in line
        line = line.lstrip("/")
        if line:
            rules.append(_Rule(line, negate, dir_only, anchored))
    return rules


def _glob_match(pattern: str, path: str) -> bool:
    """fnmatch with ``**`` spanning directories and ``*`` staying inside one."""
    regex = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            regex += "(?:.*/)?"
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            regex += "/.*"
            i += 3
        elif pattern.startswith("**", i):
            regex += ".*"
            i += 2
        elif pattern[i] == "*":
            regex += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            regex += "[^/]"
            i += 1
        elif pattern[i] == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                regex += re.escape(pattern[i])
                i += 1
            else:
                regex += "[" + pattern[i + 1:end].replace("!", "^", 1) + "]"
                i = end + 1
        else:
            regex += re.escape(pattern[i])
            i += 1
    return re.fullmatch(regex, path) is not None


def _ignored(rules: list[_Rule], rel: str, is_dir: bool) -> bool:
    verdict = False
    name = rel.rsplit("/", 1)[-1]
    for rule in rules:
        if rule.dir_only and not is_dir:
            continue
        hit = _glob_match(rule.pattern, rel) if rule.anchored else _glob_match(rule.pattern, name)
        if hit:
            verdict = not rule.negate
    return verdict


# -- what goes in ---------------------------------------------------------------------------

def windows_name(part: str) -> str:
    """The file name Windows actually creates for ``part``: no NTFS stream suffix, no trailing dots or spaces."""
    return part.split(":", 1)[0].rstrip(". ")


def _matches(name: str, patterns: Iterable[str]) -> bool:
    """Case-insensitive: on Windows and macOS ``Config.json`` *is* ``config.json``.

    The name is also reduced to what Windows would write: ``config.json.``, ``config.json `` and
    ``config.json::$DATA`` are all ``config.json`` there (the installer refuses such names too).
    """
    name = windows_name(name).lower()
    return any(fnmatch.fnmatchcase(name, pattern.lower()) for pattern in patterns)


def _first_match(name: str, patterns: Iterable[str]) -> str:
    return next(p for p in patterns if _matches(name, (p,)))


def data_paths(name: str, manifest: dict[str, Any]) -> list[str]:
    """The owner's data inside the plugin folder: ``data[]``, legacy hints, ``env`` values under ``{dir}/``."""
    paths: list[str] = []
    declared = manifest.get("data")
    for item in declared if isinstance(declared, list) else []:
        if isinstance(item, str) and item.strip() and not item.startswith(("{data}", "/")):
            paths.append(item.strip())
    paths += LEGACY_DATA.get(name, ())
    env = manifest.get("env") if isinstance(manifest.get("env"), dict) else {}
    for value in env.values():
        if isinstance(value, str) and value.replace("\\", "/").startswith("{dir}/"):
            first = value.replace("\\", "/")[len("{dir}/"):].split("/", 1)[0]
            if first and first not in (".", ".."):
                paths.append(first + "/")
    cleaned = []
    for item in paths:
        item = item.replace("\\", "/")
        while item.startswith("./"):
            item = item[2:]
        if item and item not in cleaned:
            cleaned.append(item)
    return cleaned


def prune_paths(name: str, manifest: dict[str, Any]) -> list[str]:
    extra = (manifest.get("pack") or {}).get("exclude") if isinstance(manifest.get("pack"), dict) else None
    items = [str(x) for x in extra] if isinstance(extra, list) else []
    return [*items, *LEGACY_PRUNE.get(name, ())]


def _path_rule(paths: list[str], rel: str, is_dir: bool) -> str | None:
    rel = rel.lower()
    for item in paths:
        low = item.lower()
        if low.endswith("/"):
            if is_dir and rel == low.rstrip("/"):
                return item
        elif rel == low or _glob_match(low, rel):
            return item
    return None


def select(folder: Path, manifest: dict[str, Any], name: str) -> Selection:
    """Walk ``folder`` and decide, per entry, whether it goes into the package."""
    folder = Path(folder)
    gitignore = folder / ".gitignore"
    rules = _gitignore_rules(gitignore.read_text(encoding="utf-8", errors="replace")) if gitignore.is_file() else []
    data = data_paths(name, manifest)
    prune = prune_paths(name, manifest)
    result = Selection()

    def skip(rule: str, rel: str) -> None:
        result.excluded[rule] += 1
        examples = result.examples.setdefault(rule, [])
        if len(examples) < 5:
            examples.append(rel)

    def walk(directory: Path, prefix: str) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError as exc:
            raise PackError(f"读不了 {directory}：{exc}") from None
        for entry in entries:
            rel = f"{prefix}{entry.name}"
            if entry.is_symlink():
                skip("符号链接", rel)
                continue
            is_dir = entry.is_dir()
            low = entry.name.lower()
            if is_dir:
                if low in EXCLUDED_DIRS:
                    skip(f"{low}/", rel)
                    continue
                if entry.name in NOT_NEEDED_DIRS:  # exact: unity's Editor/Resources/Tests is code, not tests
                    skip(f"不需要 {entry.name}/", rel)
                    continue
                if _matches(entry.name, EXCLUDED_DIR_PATTERNS):
                    skip("*.egg-info/", rel)
                    continue
            elif not entry.is_file():
                skip("特殊文件", rel)
                continue
            hit = _path_rule(data, rel, is_dir)
            if hit:
                skip(f"数据 {hit}", rel)
                continue
            hit = _path_rule(prune, rel, is_dir)
            if hit:
                skip(f"不需要 {hit}", rel)
                continue
            if not is_dir and _matches(entry.name, EXCLUDED_FILES) and not _matches(entry.name, KEEP_FILES):
                skip(_first_match(entry.name, EXCLUDED_FILES), rel)
                continue
            if rel != ".gitignore" and rules and _ignored(rules, rel, is_dir):
                skip(".gitignore", rel)
                continue
            if is_dir:
                walk(Path(entry.path), rel + "/")
            else:
                result.files.append(rel)

    walk(folder, "")
    return result


# -- the secret scan -------------------------------------------------------------------------

def _entropy(value: str) -> float:
    counts = Counter(value)
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


def _looks_real(value: str) -> bool:
    if _PLACEHOLDER.search(value) or len(set(value)) < 6:
        return False
    if re.fullmatch(r"[a-z_.-]+", value) or re.fullmatch(r"[A-Z_]+", value):  # identifiers, env var names
        return False
    if re.fullmatch(r"[A-Za-z_]{2,}(?:[.:][A-Za-z_]{2,})+", value):  # dotted names: "MyApp.Http.LastToken"
        return False
    if value.startswith(("http://", "https://", "/", "./", "../")) or re.fullmatch(r"[\w.-]+\.(py|js|json|md|txt)", value):
        return False
    classes = sum(bool(re.search(p, value)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    return classes >= 2 and _entropy(value) >= 3.3


def scan_bytes(rel: str, data: bytes) -> list[str]:
    """Rules ``data`` (the content of ``rel``) trips; empty when clean."""
    name = rel.rsplit("/", 1)[-1].lower()
    found = []
    if _matches(name, SECRET_NAMES) and not _matches(name, KEEP_FILES):
        found.append("凭据文件名")
    if b"\x00" in data[:8192]:
        return found  # binary: names only
    text = data[:MAX_SCAN_BYTES].decode("utf-8", errors="replace")
    for label, pattern in SECRET_CONTENT:
        if pattern.search(text):
            found.append(label)
    patterns = [_ASSIGNED]  # quoted values: every text file, source code included
    if PurePosixPath(name).suffix in _UNQUOTED_SUFFIXES or name.startswith(".env"):
        patterns.append(_UNQUOTED)
    for pattern in patterns:
        match = next((m for m in pattern.finditer(text) if _looks_real(m.group("value"))), None)
        if match:
            found.append(f"疑似凭据赋值（{match.group('key').strip(chr(34) + chr(39))}）")
            break
    return found


def scan_files(folder: Path, files: Iterable[str]) -> list[dict[str, str]]:
    findings = []
    for rel in files:
        try:
            with open(Path(folder) / rel, "rb") as fh:
                data = fh.read(MAX_SCAN_BYTES)
        except OSError:
            continue
        findings += [{"path": rel, "rule": rule} for rule in scan_bytes(rel, data)]
    return findings


def _secret_error(findings: list[dict[str, str]], where: str) -> PackError:
    shown = "；".join(f"{f['path']}（{f['rule']}）" for f in findings[:8])
    more = f" 等 {len(findings)} 处" if len(findings) > 8 else ""
    return PackError(f"{where}里有疑似凭据，拒绝打包：{shown}{more}。把它们移出插件文件夹或写进 .gitignore 后重试",
                     "secrets_found", findings)


# -- privacy scrub ---------------------------------------------------------------------------

#: Identifiers of the owner's own accounts that private plugins carry in docs and examples. Packing replaces them
#: with placeholders (the source folder is never changed); ``pack.json`` lists what was replaced. Generic patterns
#: only: the identifiers themselves must never appear in this public repository.
_ZERO_UUID = b"00000000-0000-0000-0000-000000000000"
PRIVACY_RULES: tuple[tuple[str, "re.Pattern[bytes]", bytes, tuple[str, ...] | None], ...] = (
    ("企业微信账号目录", re.compile(rb"(WXWork[/\\]+)\d{6,}"), rb"\1<account-id>", None),
    ("示例里的长数字 ID", re.compile(rb"([\"'])\d{15,}\1"), rb"\1<member-id>\1", (".md",)),
    ("示例里的 UUID", re.compile(rb"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
     _ZERO_UUID, (".md",)),
)


def scrub_bytes(rel: str, data: bytes) -> tuple[bytes, list[str]]:
    """``data`` with the owner's account identifiers replaced, and the labels of the rules that fired."""
    if b"\x00" in data[:8192]:
        return data, []
    suffix = PurePosixPath(rel.lower()).suffix
    fired = []
    for label, pattern, replacement, suffixes in PRIVACY_RULES:
        if suffixes is not None and suffix not in suffixes:
            continue
        if any(m.group(0) != _ZERO_UUID for m in pattern.finditer(data)):
            data = pattern.sub(lambda m: m.group(0) if m.group(0) == _ZERO_UUID else m.expand(replacement), data)
            fired.append(label)
    return data, fired


# -- packing ---------------------------------------------------------------------------------

def _file_sha(path: Path) -> str:
    return installer.sha256_of(path)


def content_digest(entries: Iterable[tuple[str, str]]) -> str:
    """sha256 over ``path\\0sha256\\n`` lines in path order (the package's content, without pack.json)."""
    digest = hashlib.sha256()
    for rel, sha in sorted(entries):
        digest.update(f"{rel}\0{sha}\n".encode("utf-8"))
    return digest.hexdigest()


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def pack(folder: Path | str, out: Path | str | None = None) -> dict[str, Any]:
    """Pack the plugin in ``folder`` into ``out`` (default ``<name>-<version>.zip`` in the current directory).

    Returns ``{path, name, version, sha256, content_sha256, size, files, excluded, created_at}``.
    Raises :class:`PackError` (``secrets_found`` lists the files) or :class:`installer.InstallError`.
    """
    folder = installer.find_plugin_dir(Path(folder).resolve())
    manifest = installer.read_manifest(folder)
    name, version = installer.plugin_name(folder, manifest), installer.plugin_version(folder, manifest)
    if name in installer.BUILTIN:
        raise PackError(f"{name} 是内置插件，随 Agent Nexus 发布，不需要打包", "builtin")
    selection = select(folder, manifest, name)
    if installer.MANIFEST_NAME not in selection.files:
        raise PackError(f"{installer.MANIFEST_NAME} 被排除规则挡掉了", "invalid_manifest")
    if PACK_INFO in selection.files:
        selection.files.remove(PACK_INFO)
        selection.excluded["旧 pack.json"] += 1
    findings = scan_files(folder, selection.files)
    if findings:
        raise _secret_error(findings, "插件文件夹")
    scrubbed: dict[str, bytes] = {}
    redacted: dict[str, list[str]] = {}
    for rel in selection.files:
        if (folder / rel).stat().st_size > MAX_SCAN_BYTES:
            continue
        cleaned, fired = scrub_bytes(rel, (folder / rel).read_bytes())
        if fired:
            scrubbed[rel], redacted[rel] = cleaned, fired
    entries = [(rel, hashlib.sha256(scrubbed[rel]).hexdigest() if rel in scrubbed else _file_sha(folder / rel))
               for rel in selection.files]
    info = {"format": PACK_FORMAT, "name": name, "version": version, "content_sha256": content_digest(entries),
            "files": len(entries), "created_at": _now(),
            "excluded": {"total": sum(selection.excluded.values()), "rules": dict(sorted(selection.excluded.items())),
                         "examples": selection.examples}}
    if redacted:
        info["redacted"] = dict(sorted(redacted.items()))
    target = Path(out) if out else Path.cwd() / f"{name}-{version}.zip"
    if target.resolve().is_relative_to(folder):
        raise PackError("输出文件不能放在插件文件夹里面", "invalid_output")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{installer._rand()}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for rel in sorted(selection.files):
                source = folder / rel
                item = zipfile.ZipInfo(rel, _FIXED_TIME)
                mode = source.stat().st_mode
                item.external_attr = ((0o755 if mode & stat.S_IXUSR and os.name != "nt" else 0o644) | stat.S_IFREG) << 16
                item.compress_type = zipfile.ZIP_DEFLATED
                if rel in scrubbed:  # the owner's account ids replaced (PRIVACY_RULES)
                    with zf.open(item, "w") as dst:
                        dst.write(scrubbed[rel])
                    continue
                with open(source, "rb") as src, zf.open(item, "w") as dst:
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
            meta = zipfile.ZipInfo(PACK_INFO, _FIXED_TIME)
            meta.external_attr = (0o644 | stat.S_IFREG) << 16
            zf.writestr(meta, json.dumps(info, ensure_ascii=False, indent=2) + "\n", compress_type=zipfile.ZIP_DEFLATED)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"path": str(target), "sha256": installer.sha256_of(target), "size": target.stat().st_size, **info}


# -- checking a package (the gateway, on upload) ---------------------------------------------

def _top_folder(names: list[str]) -> str:
    """``""`` when the manifest sits at the root, else the single top folder that holds it."""
    if installer.MANIFEST_NAME in names:
        return ""
    tops = {n.split("/", 1)[0] for n in names if "/" in n and not n.startswith(("__MACOSX", "."))}
    if len(tops) == 1:
        top = tops.pop()
        if f"{top}/{installer.MANIFEST_NAME}" in names:
            return top + "/"
    raise PackError(f"压缩包里没有 {installer.MANIFEST_NAME}（应在根目录或唯一的顶层文件夹里）", "invalid_manifest")


def _json_member(zf: zipfile.ZipFile, name: str) -> dict[str, Any]:
    try:
        data = json.loads(zf.read(name).decode("utf-8-sig"))
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        raise PackError(f"{name} 读不出来：{type(exc).__name__}", "invalid_manifest") from None
    if not isinstance(data, dict):
        raise PackError(f"{name} 顶层必须是 JSON 对象", "invalid_manifest")
    return data


def inspect_package(path: Path | str, *, max_bytes: int | None = None) -> dict[str, Any]:
    """Validate a package file: size, a safe zip, manifest name/version, the secret scan.

    Returns ``{name, version, title, description, sha256, size, files, pack, manifest}``.
    """
    path = Path(path)
    size = path.stat().st_size
    if max_bytes is not None and size > max_bytes:
        raise PackError(f"插件包 {size / 1048576:.1f} MB，超过上限 {max_bytes / 1048576:.0f} MB", "too_large")
    if not zipfile.is_zipfile(path):
        raise PackError("不是 zip 文件：插件库只收 anx plugin pack 生成的 .zip", "invalid_archive")
    with zipfile.ZipFile(path) as zf:
        members = zf.infolist()
        total = 0
        names = []
        for item in members:
            try:
                installer._safe_member(item.filename)
            except installer.InstallError as exc:
                raise PackError(str(exc), "unsafe_archive") from None
            if stat.S_ISLNK(item.external_attr >> 16):
                raise PackError(f"压缩包里有符号链接：{item.filename}", "unsafe_archive")
            total += item.file_size
            if not item.is_dir():
                names.append(item.filename.replace("\\", "/"))
        if total > installer.MAX_ARCHIVE_BYTES * 4:
            raise PackError("压缩包解开后太大", "unsafe_archive")
        top = _top_folder(names)
        manifest = _json_member(zf, top + installer.MANIFEST_NAME)
        info = _json_member(zf, top + PACK_INFO) if top + PACK_INFO in names else {}
        name = str(manifest.get("name") or info.get("name") or top.rstrip("/").removesuffix("-mcp")).strip()
        if not installer.NAME_PATTERN.match(name):
            raise PackError(f"插件名不合法：{name!r}", "invalid_manifest")
        if name in installer.BUILTIN:
            raise PackError(f"{name} 是内置插件，不能放进插件库", "builtin")
        if info.get("name") and info["name"] != name:
            raise PackError(f"pack.json 的名字 {info['name']} 与清单 {name} 不一致", "invalid_manifest")
        version = str(manifest.get("version") or "").strip() or _zip_pyproject_version(zf, top, manifest, names) \
            or str(info.get("version") or "").strip()
        if not version or not installer.VERSION_PATTERN.match(version):
            raise PackError(f"版本号不合法或缺失：{version!r}（写在 mcp-service.json 或 pyproject.toml 里）", "invalid_manifest")
        if info.get("version") and info["version"] != version:
            raise PackError(f"pack.json 的版本 {info['version']} 与插件 {version} 不一致", "invalid_manifest")
        kind = str(manifest.get("kind") or "process")
        if kind == "process" and not installer._command(manifest):
            raise PackError(f"{installer.MANIFEST_NAME} 缺少 command", "invalid_manifest")
        findings = []
        for item in members:
            rel = item.filename.replace("\\", "/")
            if item.is_dir() or rel == top + PACK_INFO:
                continue
            if top and not rel.startswith(top):  # outside the plugin folder: never installed, never needed
                findings.append({"path": rel, "rule": f"在顶层文件夹 {top} 以外"})
                continue
            inner = rel[len(top):]
            parts = inner.split("/")
            lowered = [windows_name(part).lower() for part in parts]  # case-insensitive, as on Windows and macOS
            if any(part in SECRET_DIRS for part in lowered[:-1]):
                findings.append({"path": inner, "rule": f"不该打包的目录 {next(p for p in lowered[:-1] if p in SECRET_DIRS)}/"})
                continue
            if _matches(parts[-1], SECRET_FILES) and not _matches(parts[-1], KEEP_FILES):
                findings.append({"path": inner, "rule": f"不该打包的文件 {parts[-1]}"})
                continue
            if _path_rule(data_paths(name, manifest), inner, False) or any(
                    _path_rule(data_paths(name, manifest), "/".join(parts[:i]), True) for i in range(1, len(parts))):
                findings.append({"path": inner, "rule": "插件数据"})
                continue
            with zf.open(item) as fh:
                head = fh.read(MAX_SCAN_BYTES)
            findings += [{"path": inner, "rule": rule} for rule in scan_bytes(inner, head)]
        if findings:
            raise _secret_error(findings, "插件包")
    schema_info = _display(name, manifest)
    return {"name": name, "version": version, "title": schema_info, "description": str(manifest.get("description") or ""),
            "sha256": installer.sha256_of(path), "size": size, "files": len([n for n in names if n != top + PACK_INFO]),
            "pack": info, "manifest": {k: manifest.get(k) for k in ("platforms", "kind", "port", "schema_version")
                                        if manifest.get(k) is not None}}


def _display(name: str, manifest: dict[str, Any]) -> str:
    from . import schema
    return schema.effective(name, manifest)["display_name"]


def _zip_pyproject_version(zf: zipfile.ZipFile, top: str, manifest: dict[str, Any], names: list[str]) -> str:
    import tomllib
    cwd = str(manifest.get("cwd") or ".").strip("./")
    for candidate in ([f"{top}{cwd}/pyproject.toml"] if cwd else []) + [f"{top}pyproject.toml"]:
        if candidate in names:
            try:
                data = tomllib.loads(zf.read(candidate).decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError):
                return ""
            return str((data.get("project") or {}).get("version") or "").strip()
    return ""


def semver_key(version: str) -> tuple:
    """Order versions: numeric parts first, a pre-release (``-rc1``) before its release."""
    match = re.match(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*)$", str(version))
    if not match:
        return (-1, -1, -1, 0, str(version))
    major, minor, patch, rest = match.groups()
    return (int(major), int(minor or 0), int(patch or 0), 0 if rest.startswith("-") else 1, rest)


def read_pack_info(data: bytes) -> dict[str, Any]:
    """``pack.json`` of an in-memory package (tests, the CLI's summary)."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return _json_member(zf, PACK_INFO)
