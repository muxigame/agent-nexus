#!/usr/bin/env python3
"""Gateway self-update (installed as /opt/agent-nexus/bin/anx-auto-upgrade, run by a systemd timer).

Every run: read ``downloads.manifest_url`` from the gateway config (the release's
``downloads.json`` on the download mirror), and when it names a newer stable version than the
one running, download ``agent-nexus-gateway-<version>.zip``, check its SHA-256, and hand it to
``anx-upgrade`` -- which verifies, syncs, checks the config, switches, restarts, probes
``/healthz`` and rolls back on failure.

There is no switch: the timer is (re)enabled by every upgrade. A version whose upgrade failed
is retried after 6 hours, not on every tick. Releases are published by CI; nothing deploys
them but this.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

STATE = Path("/var/lib/agent-nexus/auto-update.json")
RETRY_FAILED_S = 6 * 3600


def log(*parts) -> None:
    print("anx-auto-upgrade:", *parts, flush=True)


def version_key(text: str) -> tuple:
    return tuple(int(part) if part.isdigit() else 0 for part in text.split("."))


def running_version(config: dict) -> str | None:
    port = int(config.get("port") or 8440)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=10) as response:
            return json.load(response).get("version")
    except Exception:
        return None


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def download(url: str, target: Path, sha256: str) -> None:
    digest = hashlib.sha256()
    temporary = target.with_suffix(".part")
    with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as handle:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            handle.write(chunk)
            digest.update(chunk)
    if digest.hexdigest() != sha256:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"{url}: sha256 {digest.hexdigest()} != {sha256}")
    temporary.replace(target)


def decide(manifest: dict, current: str | None, state: dict, now: float) -> tuple[dict | None, str]:
    """(the gateway zip entry to install, or None; why)."""
    latest = str(manifest.get("version") or "")
    entry = next((f for f in manifest.get("files") or [] if str(f.get("filename", "")).startswith("agent-nexus-gateway-")), None)
    if not latest or "-" in latest or entry is None:
        return None, "no stable gateway release in the manifest"
    if current and version_key(latest) <= version_key(current):
        return None, f"up to date ({current})"
    failed = (state.get("failed") or {}).get(latest)
    if failed and now - failed < RETRY_FAILED_S:
        return None, f"{latest} failed {int(now - failed)}s ago; waiting before retrying"
    return {**entry, "version": latest}, f"upgrading {current or '?'} -> {latest}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=Path("/etc/agent-nexus/gateway.json"))
    parser.add_argument("--base", type=Path, default=Path("/opt/agent-nexus"))
    parser.add_argument("--dry-run", action="store_true", help="say what would happen, change nothing")
    args = parser.parse_args(argv)

    import fcntl

    lock = open("/run/anx-auto-upgrade.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another run is in progress")
        return 0

    config = json.loads(args.config.read_text(encoding="utf-8"))
    manifest_url = ((config.get("downloads") or {}).get("manifest_url") or "").strip()
    if not manifest_url:
        log("no downloads.manifest_url in", args.config, "- nothing to follow")
        return 0
    manifest = fetch_json(manifest_url)
    current = running_version(config)
    state = {}
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    entry, why = decide(manifest, current, state, time.time())
    log(why)
    if entry is None or args.dry_run:
        return 0
    latest = entry["version"]
    failed = state.get("failed") or {}

    incoming = args.base / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    package = incoming / entry["filename"]
    download(entry["url"], package, entry["sha256"])
    result = subprocess.run([str(args.base / "bin" / "anx-upgrade"), "--package", str(package), "--sha256", entry["sha256"]])
    if result.returncode != 0:
        failed[latest] = time.time()
        state["failed"] = failed
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state), encoding="utf-8")
        log(f"upgrade to {latest} failed (rolled back); retrying in 6 hours")
        return 1
    state["last"] = {"version": latest, "at": time.time()}
    state["failed"] = {k: v for k, v in failed.items() if k != latest}
    STATE.write_text(json.dumps(state), encoding="utf-8")
    package.unlink(missing_ok=True)
    log("upgraded to", latest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
