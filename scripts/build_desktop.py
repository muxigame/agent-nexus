"""Build either Windows desktop edition with an explicit runtime and Cargo output.

The public edition is a Tauri NSIS installer (design section 9.1):
``cargo tauri build --bundles nsis`` (Tauri CLI 2, ``cargo install tauri-cli
--version "^2" --locked``) produces the raw ``agent-nexus.exe``, the
``*-setup.exe`` and, with ``TAURI_SIGNING_PRIVATE_KEY`` (and its password) in the
environment, the updater signature ``*-setup.exe.sig``. The private edition stays
a single portable exe built with ``cargo build`` and never gets an installer or
an updater signature (section 11.4).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid
import zipfile

from build_release import (DESKTOP_EXE, PRIVATE_DESKTOP_EXE, ROOT, artifact_name, build as build_release,
                           load_enrollment, offline_default, private_entries)

#: Cargo binary (Tauri ``mainBinaryName``) of desktop/src-tauri.
CARGO_BIN = "agent-nexus"
TAURI_DIR = Path("desktop/src-tauri")
#: Updater signing (minisign) key and password, read by the Tauri CLI.
SIGNING_KEY_ENV = "TAURI_SIGNING_PRIVATE_KEY"
SIGNING_PASSWORD_ENV = "TAURI_SIGNING_PRIVATE_KEY_PASSWORD"
#: File names inside ``--signing-key-dir`` (the credentials vault keeps them there).
SIGNING_KEY_FILE = "updater.key"
SIGNING_PASSWORD_FILE = "updater.key.password"


def installer_name(version):
    """Published installer name; no spaces, so release URLs stay predictable."""
    return f"agent-nexus_{version}_x64-setup.exe"


def desktop_version(root=None):
    """``[package].version`` of the desktop crate (equal to ``agent_nexus.__version__``)."""
    text = (Path(root or ROOT) / TAURI_DIR / "Cargo.toml").read_text(encoding="utf-8")
    match = re.search(r'^\[package\][^\[]*?^version\s*=\s*"([^"]+)"', text, re.M | re.S)
    if not match:
        raise ValueError("desktop/src-tauri/Cargo.toml has no [package] version")
    return match.group(1)


def signing_environment(key_dir):
    """The two signing variables from a key folder. Values are never printed."""
    key_dir = Path(key_dir)
    key = (key_dir / SIGNING_KEY_FILE).read_text(encoding="utf-8").strip()
    password = (key_dir / SIGNING_PASSWORD_FILE).read_text(encoding="utf-8").strip()
    if not key:
        raise ValueError("The updater signing key file is empty")
    return {SIGNING_KEY_ENV: key, SIGNING_PASSWORD_ENV: password}


def publish(source, destination, private=False):
    """Replace a build artifact without stopping an executable using the old file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".build-" + uuid.uuid4().hex)
    try:
        shutil.copy2(source, temporary)
        if private:
            from mcpkit.config import protect
            protect(temporary)
        try:
            temporary.replace(destination)
        except PermissionError:
            if not destination.is_file():
                raise
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
            previous = destination.with_name(destination.name + ".previous-" + stamp + "-" + uuid.uuid4().hex[:8])
            destination.rename(previous)
            try:
                temporary.replace(destination)
            except BaseException:
                previous.rename(destination)
                raise
            print("Previous executable preserved; no running process was stopped.", flush=True)
    finally:
        temporary.unlink(missing_ok=True)


def collect_installer(binary_dir, out, version, signed):
    """Publish ``bundle/nsis/*_<version>_*-setup.exe`` (and its ``.sig``) to ``out``."""
    nsis = Path(binary_dir) / "release" / "bundle" / "nsis"
    candidates = sorted(path for path in nsis.glob("*-setup.exe") if f"_{version}_" in path.name)
    if len(candidates) != 1:
        raise RuntimeError(f"Expected exactly one NSIS installer for {version} in {nsis}, found {len(candidates)}")
    setup = candidates[0]
    destination = Path(out) / installer_name(version)
    publish(setup, destination)
    published = [destination]
    signature = setup.with_name(setup.name + ".sig")
    stale = destination.with_name(destination.name + ".sig")
    if signed:
        if not signature.is_file() or signature.stat().st_mtime < setup.stat().st_mtime - 1:
            raise RuntimeError("The updater signature is missing or older than the installer")
        publish(signature, stale)
        published.append(stale)
    else:
        stale.unlink(missing_ok=True)
    return published


def build(out, enrollment_file=None, runtime=None, uv="uv", cargo="cargo", target_dir=None, offline=False,
          installer=True, signing_key_dir=None):
    out = Path(out).resolve()
    private = enrollment_file is not None
    if private and runtime is not None:
        raise ValueError("A private build generates its own runtime; do not pass --runtime")
    archive_dir = out / "private" if private else out
    archive_dir.mkdir(parents=True, exist_ok=True)
    if private:
        load_enrollment(enrollment_file)
        from mcpkit.config import protect
        protect(archive_dir)
    if runtime is None:
        build_release("runtime", archive_dir, enrollment_file=enrollment_file, uv=uv, offline=offline)
        runtime = archive_dir / artifact_name("runtime")
    runtime = Path(runtime).resolve()
    with zipfile.ZipFile(runtime) as archive:
        private_names = sorted(name for name in archive.namelist() if name.startswith(".private/"))
        expected = private_entries(load_enrollment(enrollment_file)) if private else {}
        if private_names != sorted(expected):
            raise ValueError("Runtime credential boundary does not match the requested desktop edition")
        if any(archive.read(name) != data for name, data in expected.items()):
            raise ValueError("Private runtime does not match the selected enrollment profile")
    target = Path(target_dir).resolve() if target_dir else ROOT / TAURI_DIR / ("target-portable" if private else "target")
    # build.rs reads ANX_RUNTIME_ARCHIVE (MCPM_RUNTIME_ARCHIVE is the legacy name).
    env = dict(os.environ, ANX_RUNTIME_ARCHIVE=str(runtime), MCPM_RUNTIME_ARCHIVE=str(runtime), CARGO_TARGET_DIR=str(target))
    bundle = installer and not private
    signed = False
    if private:
        # The private edition never carries an updater signature.
        env.pop(SIGNING_KEY_ENV, None)
        env.pop(SIGNING_PASSWORD_ENV, None)
    if bundle:
        if signing_key_dir is not None:
            env.update(signing_environment(signing_key_dir))
        signed = bool(env.get(SIGNING_KEY_ENV))
        command = [str(cargo), "tauri", "build", "--bundles", "nsis", "--ci"]
        if not signed:
            print(f"warning: {SIGNING_KEY_ENV} is not set; building the installer without updater artifacts", flush=True)
            command += ["--config", json.dumps({"bundle": {"createUpdaterArtifacts": False}})]
        command += ["--", "--locked", "--target-dir", str(target)]
        subprocess.run(command, cwd=ROOT / TAURI_DIR, env=env, check=True)
    else:
        subprocess.run([str(cargo), "build", "--release", "--locked", "--manifest-path",
                        str(ROOT / TAURI_DIR / "Cargo.toml"), "--bin", CARGO_BIN,
                        "--target-dir", str(target)], cwd=ROOT, env=env, check=True)
    triple = env.get("CARGO_BUILD_TARGET")
    binary_dir = target / triple if triple else target
    source = binary_dir / "release" / f"{CARGO_BIN}.exe"
    binary = source.read_bytes()
    if not binary.startswith(b"MZ") or runtime.read_bytes() not in binary:
        raise RuntimeError("The built Windows executable does not embed this build's runtime")
    destination = out / (PRIVATE_DESKTOP_EXE if private else DESKTOP_EXE)
    publish(source, destination, private)
    print("artifact:", destination, flush=True)
    if bundle:
        for path in collect_installer(binary_dir, out, desktop_version(), signed):
            print("artifact:", path, flush=True)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "artifacts")
    parser.add_argument("--enrollment-file", type=Path)
    parser.add_argument("--runtime", type=Path, help="already-built public runtime")
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--cargo", default="cargo")
    parser.add_argument("--target-dir", type=Path)
    parser.add_argument("--online", action="store_true", help="generated runtime skips the bundled Python and packages")
    parser.add_argument("--no-installer", action="store_true", help="public edition: raw exe only (cargo build), no NSIS bundle")
    parser.add_argument("--signing-key-dir", type=Path,
                        help=f"folder with {SIGNING_KEY_FILE} and {SIGNING_PASSWORD_FILE}; loaded into the build environment only")
    args = parser.parse_args()
    build(args.out, args.enrollment_file, args.runtime, args.uv, args.cargo, args.target_dir,
          offline=offline_default("runtime") and not args.online, installer=not args.no_installer,
          signing_key_dir=args.signing_key_dir)
