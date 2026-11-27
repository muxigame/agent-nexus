"""Build relocatable client and cloud server source distributions with manifests."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bundle_files", ROOT / "bundle_files.py")
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)
_offline_spec = importlib.util.spec_from_file_location("offline_runtime", ROOT / "scripts" / "offline_runtime.py")
offline_runtime = importlib.util.module_from_spec(_offline_spec)
_offline_spec.loader.exec_module(offline_runtime)


#: The platform packages are built for (always this machine; tests pin it).
TARGET = sys.platform


def offline_default(kind):
    """Windows and macOS clients bundle Python and their packages unless --online is given."""
    return kind in ("client", "runtime") and TARGET in ("win32", "darwin")


def bundled_uv_name():
    """``tools/<name>`` of the uv a runtime carries (bootstrap.rs ``UV_BIN``)."""
    return "uv.exe" if TARGET == "win32" else "uv"


def zip_info(path, name):
    """A zip member for ``path`` that keeps its Unix mode (and symlinks), or None on Windows.

    The desktop shell (bootstrap.rs ``extract_into``) restores both on macOS: the bundled
    CPython and uv's cache need their executable bits and links.
    """
    if TARGET == "win32":
        return None
    info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
    info.create_system = 3  # Unix, so external_attr carries st_mode
    if path.is_symlink():
        info.external_attr = (0o120777 << 16)
        return info, os.readlink(path).encode()
    mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
    info.external_attr = ((0o100000 | mode) << 16)
    info.compress_type = zipfile.ZIP_DEFLATED
    return info, None


PRIVATE_ENROLLMENT = ".private/enrollment.json"
PRIVATE_ENDPOINT_ALLOW = ".private/endpoint-allow"


def canonical_endpoint(url):
    """Return the ``wss://`` form of an enrollment URL, or None when it is not acceptable.

    The private endpoint is never written into source; it comes from the local
    enrollment profile at build time and is pinned in ``.private/endpoint-allow``.
    """
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


ENDPOINT_ENV = ("ANX_ENROLLMENT_ENDPOINT", "MCPM_ENROLLMENT_ENDPOINT")


def _expected_endpoint():
    """The endpoint a private build must pin, given independently of the profile.

    Required: pinning the profile's own ``url`` with nothing to compare it to
    would be circular, so a swapped or tampered profile would go unnoticed.
    """
    for name in ENDPOINT_ENV:
        value = os.environ.get(name)
        if value:
            endpoint = canonical_endpoint(value)
            if endpoint is None:
                raise ValueError(f"{name} must be a wss:// or https:// endpoint URL")
            return endpoint
    raise ValueError("Private builds require ANX_ENROLLMENT_ENDPOINT=wss://<hub-domain>, "
                     "the endpoint the enrollment profile must match")


def load_enrollment(path):
    """Validate private build input without including credential values in errors."""
    expected = _expected_endpoint()
    data = Path(path).read_bytes()
    try:
        profile = json.loads(data)
    except (ValueError, UnicodeError):
        raise ValueError("Invalid private enrollment JSON") from None
    endpoint = canonical_endpoint(profile.get("url")) if isinstance(profile, dict) else None
    if endpoint is None or endpoint != expected \
            or profile.get("agent_prefix") != "portable-" \
            or not isinstance(profile.get("token"), str) or len(profile["token"]) < 32:
        raise ValueError("Invalid private enrollment profile")
    return data


def endpoint_allow(enrollment):
    """The endpoint allow-list written next to an embedded enrollment profile."""
    return (canonical_endpoint(json.loads(enrollment)["url"]) + "\n").encode()


def private_entries(enrollment):
    """Every ``.private/`` member of a private runtime, in archive order."""
    return {PRIVATE_ENROLLMENT: enrollment, PRIVATE_ENDPOINT_ALLOW: endpoint_allow(enrollment)}


#: Artifact names. ``kind`` is client, server (the gateway) or runtime.
ARTIFACT_PREFIX = "agent-nexus"
DESKTOP_EXE = "agent-nexus.exe"
PRIVATE_DESKTOP_EXE = "agent-nexus-private.exe"
CLIENT_CONFIG = "agent-nexus.json"
GATEWAY_CONFIG = "gateway.json"
#: The package and its ``mcpm`` compatibility shim (kept until 1.0).
PACKAGES = ("agent_nexus", "mcpm")


#: Operator files shipped in the gateway zip (design §10): upgrade.py travels with each release.
GATEWAY_DEPLOY_DIR = "deploy/gateway"


def source_version(root=None):
    """``__version__`` from agent_nexus/__init__.py, the single version source."""
    import re
    text = ((root or ROOT) / "agent_nexus" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r"""^__version__\s*=\s*["']([^"']+)["']""", text, re.MULTILINE)
    if match is None:
        raise ValueError("agent_nexus/__init__.py has no __version__")
    return match.group(1)


def artifact_name(kind):
    """``gateway`` is the versioned server package ``agent-nexus-gateway-<ver>.zip``."""
    if kind == "gateway":
        return f"{ARTIFACT_PREFIX}-gateway-{source_version()}.zip"
    return f"{ARTIFACT_PREFIX}-{kind}.zip"


def find_ripgrep(rg=None):
    """The real rg.exe to ship as tools/rg.exe (the device plugin's file_grep uses it), or None.

    A Chocolatey ``bin/rg.exe`` is only a shim that forwards to the binary under
    ``lib/ripgrep``; that binary is what gets bundled.
    """
    candidate = rg or os.environ.get("ANX_RIPGREP") or shutil.which("rg")
    if not candidate:
        return None
    path = Path(candidate)
    if path.parent.name.lower() == "bin" and "chocolatey" in str(path).lower():
        found = sorted((path.parent.parent / "lib" / "ripgrep").rglob("rg.exe"))
        if found:
            path = found[-1]
    return path if path.is_file() else None


def release_files(kind, desktop=None, uv=None, include_windows_uv=True, rg=None):
    for package in PACKAGES:
        for path in sorted((ROOT / package).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and "builtins" not in path.parts:
                yield path, path.relative_to(ROOT).as_posix()
    for name in ("pyproject.toml", "uv.lock", "README.md", "build_hooks.py", "bundle_files.py"):
        yield ROOT / name, name
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        if (ROOT / name).is_file():
            yield ROOT / name, name
    # muxi-p2p-signaling: a path dependency (pyproject [tool.uv.sources]) from the submodule.
    signaling = ROOT / "third_party/muxi-p2p/signaling"
    if not (signaling / "pyproject.toml").is_file():
        # uv sync fails on the target without it; say so here, where it can still be fixed.
        print("warning: third_party/muxi-p2p is missing (git submodule update --init --recursive)", file=sys.stderr)
    for path in [signaling / "pyproject.toml", signaling / "LICENSE", *sorted(signaling.glob("muxi_p2p_signaling/*.py"))]:
        if path.is_file():
            yield path, path.relative_to(ROOT).as_posix()
    for path in (ROOT / "config").glob("*.json"):
        yield path, path.relative_to(ROOT).as_posix()
    for path in sorted((ROOT / "schemas").glob("*.json")):
        yield path, path.relative_to(ROOT).as_posix()
    if kind in ("client", "runtime"):
        for path, relative in bundle.builtin_files(ROOT):
            yield path, "builtins/" + relative.as_posix()
        # The P2P tunnel tools (scripts/build_p2p_tools.py) for remote terminal and desktop.
        p2p_tools = os.environ.get("ANX_P2P_TOOLS")
        if p2p_tools:
            for path in sorted(Path(p2p_tools).iterdir()):
                if path.is_file():
                    yield path, "tools/" + path.name
        if kind == "client":
            desktop = Path(desktop) if desktop is not None else ROOT / "artifacts" / DESKTOP_EXE
            if not desktop.is_file():
                raise FileNotFoundError("Build the desktop EXE in the selected output directory before packaging the client")
            yield desktop, DESKTOP_EXE
        if include_windows_uv:
            executable = shutil.which(str(uv or "uv"))
            if not executable:
                raise FileNotFoundError("uv is required to build the portable client package")
            yield Path(executable), "tools/" + bundled_uv_name()
            ripgrep = find_ripgrep(rg)
            if ripgrep is not None:
                yield ripgrep, "tools/rg.exe" if TARGET == "win32" else "tools/rg"
            else:
                print("warning: ripgrep (rg.exe) not found; file_grep will use its built-in search", file=sys.stderr)
    else:
        # Cloud uses shared authentication/schema helpers, never Windows MCPs.
        for path, relative in bundle.builtin_files(ROOT):
            if relative.parts[0] == "mcpkit":
                yield path, "builtins/" + relative.as_posix()
        if kind == "gateway":
            for path in sorted((ROOT / GATEWAY_DEPLOY_DIR).rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    yield path, path.relative_to(ROOT).as_posix()


def build(kind, destination, enrollment_file=None, desktop=None, uv=None, offline=False, rg=None):
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / artifact_name(kind)
    manifest = {}
    if enrollment_file and kind != "runtime":
        raise ValueError("private enrollment is only supported in the dedicated runtime build")
    enrollment = load_enrollment(enrollment_file) if enrollment_file else None
    if kind == "client" and desktop is None:
        desktop = destination / DESKTOP_EXE
    sources = list(release_files(kind, desktop=desktop, uv=uv, rg=rg))
    scratch = tempfile.TemporaryDirectory(prefix="anx-offline-") if offline and kind in ("client", "runtime") else None
    if scratch is not None:
        sources += offline_runtime.bundle(sources, shutil.which(str(uv or "uv")) or "uv", Path(scratch.name))
    with scratch or contextlib.nullcontext(), zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, name in sources:
            unix = zip_info(path, name) if kind in ("client", "runtime") else None
            if unix is not None and unix[1] is not None:  # a symlink: its target is the member's data
                archive.writestr(unix[0], unix[1])
                manifest[name] = hashlib.sha256(b"symlink:" + unix[1]).hexdigest()
                continue
            data = path.read_bytes()
            if kind in ("server", "gateway") and name == "pyproject.toml":
                # Source server package doesn't carry the client wheel hook.
                data = data.decode().replace("\r\n", "\n").replace('[tool.hatch.build.targets.wheel.hooks.custom]\npath = "build_hooks.py"\n', "").encode()
            archive.writestr(unix[0] if unix is not None else name, data)
            manifest[name] = hashlib.sha256(data).hexdigest()
        config = "client.example.json" if kind in ("client", "runtime") else "server.example.json"
        name = CLIENT_CONFIG if kind in ("client", "runtime") else GATEWAY_CONFIG
        data = (ROOT / "config" / config).read_bytes()
        if kind == "runtime":
            initial = json.loads(data)
            initial["cloud"]["enabled"] = False
            data = (json.dumps(initial, ensure_ascii=False, indent=2) + "\n").encode()
        archive.writestr(name, data)
        manifest[name] = hashlib.sha256(data).hexdigest()
        if kind in ("client", "runtime"):
            ps = '$ErrorActionPreference = "Stop"\n$clientRoot = $PSScriptRoot\n$clientArgs = @($args)\nif ($clientArgs.Count -eq 0) { $clientArgs = @("serve", "--tray", "--open") }\n$bundledUv = Join-Path $clientRoot "tools\\uv.exe"\n$uv = if (Test-Path -LiteralPath $bundledUv) { $bundledUv } else { (Get-Command uv -ErrorAction Stop).Source }\n& $uv run --project $clientRoot anx --root $clientRoot @clientArgs\nexit $LASTEXITCODE\n'
            sh = '#!/usr/bin/env sh\nset -e\nclient_root="$(cd "$(dirname "$0")" && pwd)"\n[ "$#" -eq 0 ] && set -- serve --open\nexec uv run --project "$client_root" anx --root "$client_root" "$@"\n'
            archive.writestr("anx.ps1", ps)
            archive.writestr("anx.sh", sh)
            manifest["anx.ps1"] = hashlib.sha256(ps.encode()).hexdigest()
            manifest["anx.sh"] = hashlib.sha256(sh.encode()).hexdigest()
        if enrollment is not None:
            for name, data in private_entries(enrollment).items():
                archive.writestr(name, data)
                manifest[name] = hashlib.sha256(data).hexdigest()
        archive.writestr("SHA256.json", json.dumps(manifest, indent=2, sort_keys=True))
    print(f"artifact: {output} ({len(manifest)} files)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["client", "server", "gateway", "runtime"],
                        help="gateway = server package plus deploy/gateway, named agent-nexus-gateway-<ver>.zip")
    parser.add_argument("--out", type=Path, default=ROOT / "artifacts")
    parser.add_argument("--enrollment-file", type=Path, help="private runtime only; never use for public packages")
    parser.add_argument("--desktop-exe", type=Path, help="client EXE; defaults to the selected output directory")
    parser.add_argument("--uv", help="uv executable to bundle in Windows client/runtime packages")
    parser.add_argument("--rg", help="ripgrep executable to bundle as tools/rg.exe (default: rg on PATH)")
    parser.add_argument("--online", action="store_true",
                        help="skip the bundled Python and package cache (smaller; first start downloads)")
    args = parser.parse_args()
    build(args.kind, args.out.resolve(), args.enrollment_file, args.desktop_exe, args.uv,
          offline=offline_default(args.kind) and not args.online, rg=args.rg)
