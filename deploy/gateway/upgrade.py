#!/usr/bin/env python3
"""Install a verified agent-nexus gateway release and switch to it, rolling back on failure.

Runs on the hub host as root (installed as ``/opt/agent-nexus/bin/anx-upgrade``; the
deploy user may run only that through sudo). It needs nothing but the Python 3
standard library and ``uv``; the same file ships inside every gateway zip at
``deploy/gateway/upgrade.py``, so the script always matches the release it installs.

Steps (any failure before the switch leaves the running gateway untouched):

1. verify the zip: its sha256, then every member against ``SHA256.json`` (exact file
   set, no absolute or ``..`` paths, no ``.private``/``secrets``/``.venv``/``logs``);
2. extract to ``<base>/releases/<version>-<sha8>`` and ``uv sync --frozen --no-dev``
   (re-deploying the live package extracts to a fresh ``<version>-<sha8>-<n>`` instead,
   so the running release and its ``.venv`` are never touched before the switch);
3. ``python -m agent_nexus.cloud --config <config> --check-config`` with the env file
   loaded (``--check-config`` on this script stops here, nothing is switched);
4. back up the config, the env file and every ``*.sqlite3`` in the state directory
   (SQLite online backup) to ``<base>/backups/<timestamp>``;
5. switch the ``<base>/current`` symlink atomically, restart the unit and poll
   ``/healthz`` until it reports ``status: ok`` and the new version;
6. on any failure after the switch: point ``current`` back at the previous release,
   restore the databases from step 4 and restart, then exit non-zero.

Every path is a parameter. A second instance (for example the trial gateway on
127.0.0.1:8441 run by ``agent-nexus-gateway@trial.service``) passes its own
``--base``, ``--config-dir``, ``--state-dir`` and ``--service``; see README.md.

Usage::

    anx-upgrade --package /opt/agent-nexus/incoming/agent-nexus-gateway-0.2.0.zip --sha256 <hex>
    anx-upgrade --package ... --sha256 ... --check-config      # verify + sync + check only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

EXCLUDED_PARTS = frozenset({".private", "secrets", ".venv", "logs"})
SERVICE_NAMES = frozenset({"agent-nexus-gateway", "workspace-cloud"})
VERSION_FILE = "agent_nexus/__init__.py"
VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class UpgradeError(RuntimeError):
    pass


def check(condition, message):
    # Explicit checks: unlike assert, these survive ``python -O``.
    if not condition:
        raise UpgradeError(message)


def log(*parts):
    print("[anx-upgrade]", *parts, flush=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--package", type=Path, required=True, help="gateway zip (agent-nexus-gateway-<ver>.zip)")
    parser.add_argument("--sha256", required=True, help="expected SHA-256 of the zip (64 hex characters)")
    parser.add_argument("--base", type=Path, default=Path("/opt/agent-nexus"),
                        help="install base holding releases/, backups/ and the current symlink")
    parser.add_argument("--config-dir", type=Path, default=Path("/etc/agent-nexus"),
                        help="directory holding the gateway config and env file")
    parser.add_argument("--config-name", default="gateway.json", help="config file in --config-dir (legacy: cloud.json)")
    parser.add_argument("--env-name", default="gateway.env", help="env file in --config-dir (legacy: server.env)")
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/agent-nexus"),
                        help="directory holding oauth.sqlite3 and gateway.sqlite3")
    parser.add_argument("--service", default="agent-nexus-gateway.service", help="systemd unit to restart")
    parser.add_argument("--port", type=int, help="gateway port for the health probe (default: the config's port, else 8440)")
    parser.add_argument("--health-url", help="override the probe URL (default http://127.0.0.1:<port>/healthz)")
    parser.add_argument("--health-timeout", type=float, default=60.0, help="seconds to wait for a healthy restart")
    parser.add_argument("--python", default="3.12", help="Python version uv uses for the release environment")
    parser.add_argument("--uv", default="uv", help="uv executable")
    parser.add_argument("--module", default="agent_nexus.cloud", help="gateway module (legacy: mcpm.cloud)")
    parser.add_argument("--check-config", action="store_true",
                        help="verify, extract, sync and run --check-config only; switch nothing")
    parser.add_argument("--keep-releases", type=int, default=5, help="old releases kept after a successful switch")
    return parser.parse_args(argv)


# ---- package -------------------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_version(archive: zipfile.ZipFile) -> str:
    try:
        text = archive.read(VERSION_FILE).decode("utf-8")
    except KeyError:
        raise UpgradeError(f"package has no {VERSION_FILE}") from None
    match = VERSION_RE.search(text)
    check(match is not None, "package version not found")
    version = match.group(1)
    check(re.fullmatch(r"[0-9A-Za-z.+-]{1,64}", version) is not None, "invalid package version")
    return version


def verify_package(package: Path, expected: str) -> str:
    """Check the zip against ``expected`` and its own manifest; return the package version."""
    check(HEX64.match(expected) is not None, "--sha256 must be 64 lowercase hex characters")
    check(package.is_file(), f"package not found: {package}")
    check(sha256_file(package) == expected, "package sha256 mismatch")
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        check(len(names) == len(set(names)), "duplicate archive members")
        try:
            manifest = json.loads(archive.read("SHA256.json"))
        except KeyError:
            raise UpgradeError("package has no SHA256.json") from None
        check(isinstance(manifest, dict), "SHA256.json is not an object")
        check(set(names) == set(manifest) | {"SHA256.json"}, "SHA256.json does not cover the exact file set")
        for name, digest in manifest.items():
            path = PurePosixPath(name)
            check(not path.is_absolute() and ".." not in path.parts and "\\" not in name and ":" not in name,
                  "unsafe member path: " + name)
            check(not (EXCLUDED_PARTS & set(path.parts)), "excluded data in package: " + name)
            check(hashlib.sha256(archive.read(name)).hexdigest() == digest, "member hash mismatch: " + name)
        return package_version(archive)


def live_release(base: Path):
    """The directory ``<base>/current`` resolves to, or None."""
    current = base / "current"
    return current.resolve() if current.is_symlink() else None


def release_dir(base: Path, name: str, live) -> Path:
    """``releases/<name>``, unless that is the live release: then ``<name>-2``, ``-3``, ...

    The live release is never replaced in place: deleting it before the switch would
    take the running gateway's code and ``.venv`` away even when the check then fails.
    """
    releases = base / "releases"
    release = releases / name
    suffix = 1
    while live is not None and (release.exists() or release.is_symlink()) and release.resolve() == live:
        suffix += 1
        release = releases / f"{name}-{suffix}"
    return release


def extract(package: Path, release: Path, live=None) -> None:
    check(live is None or release.resolve() != live, f"refusing to replace the live release {release}")
    staging = release.with_name(release.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with zipfile.ZipFile(package) as archive:
        archive.extractall(staging)
    shutil.rmtree(release, ignore_errors=True)  # a stale, never-activated extraction
    staging.rename(release)


# ---- environment and commands --------------------------------------------------------------------

def read_env_file(path: Path) -> dict:
    """``KEY=value`` lines; blank lines, ``#`` comments and an ``export`` prefix are accepted."""
    values = {}
    if not path.is_file():
        log("warning: env file not found:", path)
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            raise UpgradeError(f"malformed line in {path.name} (expected KEY=value)")
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def venv_python(release: Path) -> Path:
    windows = release / ".venv" / "Scripts" / "python.exe"
    return windows if os.name == "nt" else release / ".venv" / "bin" / "python"


class Host:
    """Side effects on the host; tests replace this with a recorder."""

    def run(self, command, env=None):
        log("$", " ".join(str(part) for part in command))
        result = subprocess.run([str(part) for part in command], env=env)
        if result.returncode != 0:
            raise UpgradeError(f"command failed with exit code {result.returncode}: {command[0]}")

    def restart(self, service):
        self.run(["systemctl", "restart", service])

    def stop(self, service):
        subprocess.run(["systemctl", "stop", service])

    def health(self, url):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def sleep(self, seconds):
        time.sleep(seconds)


def health_url(args, config: dict) -> str:
    if args.health_url:
        return args.health_url
    port = args.port or config.get("port") or 8440
    return f"http://127.0.0.1:{int(port)}/healthz"


def wait_healthy(host: Host, url: str, version, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while True:
        body = host.health(url)
        if isinstance(body, dict):
            last = body
            if body.get("status") == "ok" and body.get("service") in SERVICE_NAMES \
                    and (version is None or body.get("version") == version):
                return body
        if time.monotonic() >= deadline:
            seen = "no response" if last is None else f"status={last.get('status')!r} version={last.get('version')!r}"
            raise UpgradeError(f"health check failed after {timeout:.0f}s ({seen})")
        host.sleep(1)


# ---- symlink switch and backups ------------------------------------------------------------------

def switch(base: Path, target: Path) -> None:
    """Point ``base/current`` at ``target`` atomically (rename over the old link)."""
    next_link = base / "current.next"
    if next_link.is_symlink() or next_link.exists():
        next_link.unlink()
    next_link.symlink_to(target, target_is_directory=True)
    current = base / "current"
    if os.name == "nt" and current.is_symlink():
        # Windows cannot rename over a directory symlink; only used by local tests.
        current.unlink()
    os.replace(next_link, current)


def backup(args, previous) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = args.base / "backups" / f"deployment-{stamp}"
    suffix = 1
    while folder.exists():
        suffix += 1
        folder = args.base / "backups" / f"deployment-{stamp}-{suffix}"
    folder.mkdir(parents=True, mode=0o700)
    for name in (args.config_name, args.env_name):
        source = args.config_dir / name
        if source.is_file():
            shutil.copy2(source, folder / name)
    (folder / "previous-release.txt").write_text(f"{previous or ''}\n", encoding="utf-8")
    databases = folder / "state"
    databases.mkdir()
    if args.state_dir.is_dir():
        for database in sorted(args.state_dir.glob("*.sqlite3")):
            with sqlite3.connect(str(database)) as source, sqlite3.connect(str(databases / database.name)) as copy:
                source.backup(copy)
    return folder


def restore_databases(folder: Path, state_dir: Path) -> list[str]:
    restored = []
    for copy in sorted((folder / "state").glob("*.sqlite3")):
        target = state_dir / copy.name
        for suffix in ("-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
        with sqlite3.connect(str(copy)) as source, sqlite3.connect(str(target)) as destination:
            source.backup(destination)
        restored.append(copy.name)
    return restored


def prune_releases(base: Path, keep: int, protected: set) -> None:
    releases = base / "releases"
    if keep < 0 or not releases.is_dir():
        return
    candidates = sorted((path for path in releases.iterdir() if path.is_dir() and path.resolve() not in protected),
                        key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates[keep:]:
        shutil.rmtree(path, ignore_errors=True)


def install_self(release: Path, base: Path) -> None:
    """Keep ``<base>/bin/anx-upgrade`` in step with the release just activated."""
    source = release / "deploy" / "gateway" / "upgrade.py"
    target = base / "bin" / "anx-upgrade"
    if not source.is_file() or not target.parent.is_dir():
        return
    if target.is_file() and target.read_bytes() == source.read_bytes():
        return
    temporary = target.with_name(target.name + ".new")
    shutil.copyfile(source, temporary)
    os.chmod(temporary, 0o755)
    os.replace(temporary, target)
    log("updated", target)


# ---- main ----------------------------------------------------------------------------------------

def upgrade(args, host: Host | None = None) -> int:
    host = host or Host()
    expected = args.sha256.strip().lower()
    config_path = args.config_dir / args.config_name
    check(config_path.is_file(), f"config not found: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    check(isinstance(config, dict), "config is not a JSON object")

    version = verify_package(args.package, expected)
    live = live_release(args.base)
    release = release_dir(args.base, f"{version}-{expected[:8]}", live)
    log(f"package verified: version {version}, release {release}")
    extract(args.package, release, live)

    env = {**os.environ, "UV_PYTHON_INSTALL_DIR": str(args.base / "python"), "UV_CACHE_DIR": str(args.base / "cache")}
    host.run([args.uv, "sync", "--project", release, "--frozen", "--no-dev", "--python", args.python], env=env)
    env.update(read_env_file(args.config_dir / args.env_name))
    host.run([venv_python(release), "-m", args.module, "--config", config_path, "--check-config"], env=env)
    if args.check_config:
        log("check-config passed; nothing switched (release left at", str(release) + ")")
        return 0

    previous = live
    folder = backup(args, previous)
    log("backup:", folder)
    url = health_url(args, config)
    switch(args.base, release)
    try:
        host.restart(args.service)
        body = wait_healthy(host, url, version, args.health_timeout)
    except Exception as error:
        log("upgrade failed:", error)
        rollback(args, host, previous, folder, url)
        raise
    log(f"active: {release} (version {body.get('version')}, db_schema {body.get('db_schema')})")
    install_self(release, args.base)
    prune_releases(args.base, args.keep_releases, {release.resolve()} | ({previous} if previous else set()))
    return 0


def rollback(args, host: Host, previous, folder: Path, url: str) -> None:
    if previous is None:
        log("no previous release to roll back to; stopping", args.service)
        host.stop(args.service)
        (args.base / "current").unlink(missing_ok=True)
        return
    log("rolling back to", previous)
    host.stop(args.service)
    switch(args.base, previous)
    restored = restore_databases(folder, args.state_dir)
    if restored:
        log("restored databases:", ", ".join(restored))
    try:
        host.restart(args.service)
        wait_healthy(host, url, None, args.health_timeout)
        log("rollback complete; previous release is healthy")
    except Exception as error:  # report, but keep the original failure as the exit reason
        log("warning: previous release did not come back healthy:", error)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return upgrade(args)
    except UpgradeError as error:
        log("error:", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
