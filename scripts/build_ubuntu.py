"""Build native Ubuntu amd64 Debian and relocatable packages, including bundled Linux uv."""
import argparse
import hashlib
import io
import json
import re
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from build_release import CLIENT_CONFIG, ROOT, offline_runtime, release_files, load_enrollment, private_entries


def source_version(root=ROOT):
    """The single version source, agent_nexus/__init__.py (design section 10; see scripts/version.py)."""
    text = (Path(root) / "agent_nexus/__init__.py").read_text(encoding="utf-8")
    return re.search(r"""^__version__\s*=\s*["']([^"']+)["']""", text, re.M).group(1)


#: Debian package version; never a literal here (scripts/version.py check enforces it).
VERSION = source_version()

PACKAGE = "agent-nexus"
PRIVATE_PACKAGE = "agent-nexus-private"
#: Installed program directory, launcher command and desktop entry / icon name.
LIB = "usr/lib/agent-nexus/"
LAUNCHER = "agent-nexus"
DESKTOP_ID = "agent-nexus"


def tar_bytes(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz", format=tarfile.GNU_FORMAT, preset=3) as archive:
        directories = {parent.as_posix() for name in entries for parent in PurePosixPath(name).parents
                       if parent.as_posix() != "."}
        for directory in sorted(directories, key=lambda path: (path.count("/"), path)):
            info = tarfile.TarInfo(directory + "/")
            info.type, info.mode, info.uid, info.gid = tarfile.DIRTYPE, 0o755, 0, 0
            info.uname = info.gname = "root"
            archive.addfile(info)
        for name, (data, mode) in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.uid, info.gid = len(data), mode, 0, 0
            info.uname = info.gname = "root"
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def ar_member(name, data):
    header = f"{name+'/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode("ascii")
    assert len(header) == 60
    return header + data + (b"\n" if len(data) % 2 else b"")


def build(uv, out, enrollment=None, offline=False):
    """``offline`` bundles CPython and every locked package (scripts/offline_runtime.py),
    so the first start never downloads; it must run on Linux x86_64 with ``uv`` = that Linux uv."""
    binary = uv.read_bytes()
    if binary[:4] != b"\x7fELF" or binary[18:20] != b"\x3e\x00":
        raise ValueError("--uv must be a Linux x86_64 ELF executable")
    private = bool(enrollment)
    enrollment_data = load_enrollment(enrollment) if private else None
    name = PRIVATE_PACKAGE if private else PACKAGE
    entries = {}
    manifest = {}
    files = list(release_files("runtime", include_windows_uv=False))
    for source, relative in files:
        data = source.read_bytes()
        entries[LIB + "runtime/" + relative] = (data, 0o644)
        manifest[relative] = hashlib.sha256(data).hexdigest()
    if offline:
        with tempfile.TemporaryDirectory(prefix="anx-ubuntu-offline-") as scratch:
            for source, relative in offline_runtime.bundle(files, str(uv), Path(scratch)):
                data = source.read_bytes()
                entries[LIB + "runtime/" + relative] = (data, 0o755 if source.stat().st_mode & 0o111 else 0o644)
                manifest[relative] = hashlib.sha256(data).hexdigest()
    entries[LIB + "runtime/tools/uv"] = (binary, 0o755)
    manifest["tools/uv"] = hashlib.sha256(binary).hexdigest()
    config = json.loads((ROOT / "config/client.example.json").read_bytes())
    config["cloud"]["enabled"] = False
    config["services"]["device"]["settings"] = {"bash_path": "/bin/bash"}
    data = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode()
    entries[LIB + "runtime/" + CLIENT_CONFIG] = (data, 0o644)
    manifest[CLIENT_CONFIG] = hashlib.sha256(data).hexdigest()
    if enrollment:
        for relative, data in private_entries(enrollment_data).items():
            entries[LIB + "runtime/" + relative] = (data, 0o644)
            manifest[relative] = hashlib.sha256(data).hexdigest()
    entries[LIB + "runtime/SHA256.json"] = (json.dumps(manifest, sort_keys=True).encode(), 0o644)
    for filename in ("ubuntu_launcher.py", LAUNCHER):
        entries[LIB + filename] = ((ROOT / "packaging/ubuntu" / filename).read_bytes(), 0o755)
    entries["usr/bin/" + LAUNCHER] = (f'#!/bin/sh\nexec /{LIB}{LAUNCHER} "$@"\n'.encode(), 0o755)
    entries[LIB + "startup.html"] = ((ROOT / "desktop/dist/index.html").read_bytes(), 0o644)
    entries[f"usr/share/applications/{DESKTOP_ID}.desktop"] = ((ROOT / f"packaging/ubuntu/{DESKTOP_ID}.desktop").read_bytes(), 0o644)
    entries[f"usr/share/icons/hicolor/128x128/apps/{DESKTOP_ID}.png"] = ((ROOT / "agent_nexus/static/icon.png").read_bytes(), 0o644)
    control = f"""Package: {name}
Version: {VERSION}
Architecture: amd64
Maintainer: agent-nexus maintainers
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-3.0, gir1.2-webkit2-4.1, gir1.2-ayatanaappindicator3-0.1, ca-certificates, systemd, xdg-utils
Recommends: gnome-shell-extension-appindicator
Provides: agent-nexus-desktop
Conflicts: {PACKAGE if private else PRIVATE_PACKAGE}
Replaces: {PACKAGE if private else PRIVATE_PACKAGE}
Section: devel
Priority: optional
Description: Agent Nexus desktop, device plugin and outbound gateway connector
 Per-user GTK/WebKit console, system tray and independent managed Python runtime.
 Close the window to keep services running; quit from the tray to stop all owned services.
"""
    control_entries = {"control": (control.encode(), 0o644),
                       "md5sums": ("\n".join(hashlib.md5(data).hexdigest() + "  " + path for path, (data, mode) in sorted(entries.items())).encode() + b"\n", 0o644)}
    out.mkdir(parents=True, exist_ok=True)
    deb = out / f"{name}_{VERSION}_amd64.deb"
    deb.write_bytes(b"!<arch>\n" + ar_member("debian-binary", b"2.0\n") + ar_member("control.tar.xz", tar_bytes(control_entries)) + ar_member("data.tar.xz", tar_bytes(entries)))
    portable = {path.removeprefix(LIB): value for path, value in entries.items() if path.startswith(LIB)}
    portable["start.sh"] = (b'#!/bin/sh\nset -eu\ncd "$(dirname "$0")"\nexec /usr/bin/python3 "$PWD/ubuntu_launcher.py" "$@"\n', 0o755)
    portable_file = out / f"{name}_{VERSION}_linux-amd64.tar.xz"
    portable_file.write_bytes(tar_bytes(portable))
    if private:
        from mcpkit.config import protect
        protect(deb)
        protect(portable_file)
    print("Ubuntu artifacts:", deb.name, "and portable tar.xz")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=ROOT / "artifacts/ubuntu")
    parser.add_argument("--enrollment-file", type=Path)
    parser.add_argument("--online", action="store_true",
                        help="skip the bundled Python and package cache (the first start then downloads them)")
    args = parser.parse_args()
    build(args.uv, args.out, args.enrollment_file, offline=not args.online)
