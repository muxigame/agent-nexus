"""Validate the actual Debian archives without trusting build-time assumptions.

The package version comes from the single version source, ``agent_nexus/__init__.py``
(read through ``scripts/version.py``; never a literal here).
"""
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("anx_version_source", ROOT / "scripts" / "version.py")
_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_version)

VERSION = _version.read_version(ROOT)
PRIVATE_PACKAGE = "agent-nexus-private"


def packages(base, version=VERSION):
    return sorted(Path(base).glob(f"*_{version}_amd64.deb"))


def verify_package(package):
    raw = package.read_bytes()
    assert raw[:8] == b"!<arch>\n"
    position, members = 8, {}
    while position < len(raw):
        header = raw[position:position+60]
        assert header[-2:] == b"`\n"
        name = header[:16].decode().strip().rstrip("/")
        size = int(header[48:58])
        members[name] = raw[position+60:position+60+size]
        position += 60 + size + size % 2
    assert members["debian-binary"] == b"2.0\n"
    with tarfile.open(fileobj=io.BytesIO(members["control.tar.xz"])) as control:
        metadata = control.extractfile("control").read().decode()
        assert "Architecture: amd64" in metadata and "gir1.2-webkit2-4.1" in metadata
        assert f"\nVersion: {VERSION}\n" in metadata, "control Version differs from agent_nexus.__version__"
        sums = dict(line.split("  ", 1)[::-1] for line in control.extractfile("md5sums").read().decode().splitlines())
    with tarfile.open(fileobj=io.BytesIO(members["data.tar.xz"])) as data:
        assert all(not item.pax_headers for item in data), "dpkg does not support PAX extended headers"
        directory_names = {item.name.rstrip("/") for item in data if item.isdir()}
        for item in data:
            assert not Path(item.name).is_absolute() and ".." not in Path(item.name).parts
            if item.isfile():
                assert all(parent.as_posix() in directory_names for parent in Path(item.name).parents if parent.as_posix() != "."), item.name
        files = {item.name: data.extractfile(item).read() for item in data if item.isfile()}
        for path, payload in files.items():
            assert hashlib.md5(payload).hexdigest() == sums[path]
            assert not any(part in {".venv", "__pycache__", "secrets", "logs"} for part in Path(path).parts)
        prefix = "usr/lib/agent-nexus/runtime/"
        manifest = json.loads(files[prefix + "SHA256.json"])
        for path, digest in manifest.items():
            assert hashlib.sha256(files[prefix + path]).hexdigest() == digest
        uv = files[prefix + "tools/uv"]
        assert uv[:4] == b"\x7fELF" and uv[18:20] == b"\x3e\x00"
        assert not any(path.endswith("uv.exe") or path.endswith(("agent-nexus.exe", "mcpm-desktop.exe")) for path in files)
        private = package.name.startswith(PRIVATE_PACKAGE + "_")
        assert (prefix + ".private/enrollment.json" in files) == private
        assert not json.loads(files[prefix + "agent-nexus.json"])["cloud"]["enabled"]
        compile(files["usr/lib/agent-nexus/ubuntu_launcher.py"], "ubuntu_launcher.py", "exec")
    print(package.name + ": Debian structure, hashes, Linux runtime and credential boundary passed")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    base = Path(argv[0]) if argv else ROOT / "artifacts/ubuntu"
    found = packages(base)
    if not found:
        print(f"no *_{VERSION}_amd64.deb under {base}", file=sys.stderr)
        return 1
    for package in found:
        verify_package(package)
    return 0


if __name__ == "__main__":
    sys.exit(main())
