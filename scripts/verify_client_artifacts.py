"""Verify all desktop products and optional complete release without launching them."""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import lzma
import os
import posixpath
import re
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
CORE_SOURCE = (
    "agent_nexus/agent.py", "agent_nexus/federation.py", "agent_nexus/gateway.py", "agent_nexus/cloud.py",
    "agent_nexus/static/index.html", "mcpm/cli.py",
)
#: Python packages whose packaged copy must equal the checkout (``mcpm`` is the shim).
SOURCE_PACKAGES = ("agent_nexus/", "mcpm/")
#: Wheels carry the builtins inside the package.
WHEEL_BUILTINS = "agent_nexus/"
CLIENT_CONFIG = "agent-nexus.json"
DESKTOP_EXE = "agent-nexus.exe"
PRIVATE_DESKTOP_EXE = "agent-nexus-private.exe"
UBUNTU_PACKAGE = "agent-nexus"
UBUNTU_PRIVATE_PACKAGE = "agent-nexus-private"
UBUNTU_LIB = "usr/lib/agent-nexus/"
UBUNTU_LAUNCHER = "agent-nexus"
WECHAT_SOURCE = tuple("builtins/wechat-mcp/" + name for name in (
    "wecom_reader/native_send.js", "wecom_reader/native_sender.py",
    "wecom_reader/rich_text.py", "wecom_reader/db/message.py",
    "wecom_reader/reader.py", "wecom_reader/mcp/server.py", "mcp-service.json",
))
FORBIDDEN = {".git", ".venv", "secrets", "logs", "__pycache__", ".mcp-manager", ".anx"}
#: Public NSIS installer and its minisign updater signature (design section 9.1).
INSTALLER_TEMPLATE = "agent-nexus_{version}_x64-setup.exe"
PRIVATE_PATH = ".private/enrollment.json"
PRIVATE_ALLOW_PATH = ".private/endpoint-allow"


def require(condition, message):
    # Deliberately do not include compared bytes or profile values in errors.
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(name):
    path = PurePosixPath(name)
    require(bool(name) and "\\" not in name and not path.is_absolute()
            and ".." not in path.parts and ":" not in name
            and path.as_posix() == name.rstrip("/"), "Unsafe archive member name")
    return path


def read_zip(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "Duplicate ZIP member names")
        entries = {}
        for entry in archive.infolist():
            safe_name(entry.filename)
            require(not entry.is_dir(), "Unexpected ZIP directory entry")
            require((entry.external_attr >> 16) & 0o170000 != 0o120000,
                    "ZIP symlink is not supported")
            entries[entry.filename] = archive.read(entry)
        return entries


#: modes[] value of a symlink member (its entries[] value is b"symlink:" + target).
SYMLINK_MODE = 0o120777


def verify_manifest(entries):
    require("SHA256.json" in entries, "Runtime manifest is missing")
    manifest = json.loads(entries["SHA256.json"])
    require(isinstance(manifest, dict) and set(manifest) == set(entries) - {"SHA256.json"},
            "Runtime manifest does not cover the exact file set")
    for name, expected in manifest.items():
        require(digest(entries[name]) == expected, "Runtime hash mismatch: " + name)


def expected_endpoint_allow(enrollment):
    """Mirror of build_release.endpoint_allow; the endpoint itself is never in source."""
    url = json.loads(enrollment)["url"].rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    return (url + "\n").encode()


def verify_privacy(entries, enrollment, token, private=False, private_path=PRIVATE_PATH):
    require((private_path in entries) == private, "Unexpected enrollment presence")
    allow_path = private_path[:-len(PRIVATE_PATH)] + PRIVATE_ALLOW_PATH
    require((allow_path in entries) == private, "Unexpected endpoint allow-list presence")
    for name, data in entries.items():
        parts = safe_name(name).parts
        require(not FORBIDDEN.intersection(parts), "Excluded data in package: " + name)
        if name == private_path and private:
            require(data == enrollment, "Embedded enrollment differs from build input")
            continue
        if name == allow_path and private:
            require(data == expected_endpoint_allow(enrollment), "Embedded endpoint allow-list differs from build input")
            continue
        require(".private" not in parts, "Unexpected private package member")
        require(token not in data, "Enrollment token appears outside its intended profile")


def verify_current_source(entries, root, *, wechat, wheel=False, readme=False):
    required = list(CORE_SOURCE)
    # The WeCom plugin lives in a private repository; only check it when a checkout is present.
    if wechat and (root / "builtins/wechat-mcp").is_dir():
        required.extend(WECHAT_SOURCE)
    for relative in required:
        member = WHEEL_BUILTINS + relative if wheel and relative.startswith("builtins/") else relative
        require(member in entries, "Required source missing: " + member)
    if readme:
        require("README.md" in entries, "Release README is missing")
    for name, data in entries.items():
        relative = name.removeprefix(WHEEL_BUILTINS) if wheel and name.startswith(WHEEL_BUILTINS + "builtins/") else name
        if (relative.startswith(SOURCE_PACKAGES) or relative.startswith("builtins/wechat-mcp/")
                or (readme and relative == "README.md")):
            source = root / relative
            require(source.is_file(), "Packaged source no longer exists: " + relative)
            require(data == source.read_bytes(), "Packaged source is stale: " + relative)


def verify_runtime(entries, root, enrollment, token, *, private=False, linux=False):
    verify_manifest(entries)
    verify_privacy(entries, enrollment, token, private)
    verify_current_source(entries, root, wechat=True, readme=True)
    require(CLIENT_CONFIG in entries and "mcp-manager.json" not in entries,
            "Runtime must ship its initial configuration as " + CLIENT_CONFIG)
    config = json.loads(entries[CLIENT_CONFIG])
    require(config.get("cloud", {}).get("enabled") is False,
            "Runtime initial cloud configuration must remain disabled")
    if linux:
        require("tools/uv.exe" not in entries and "tools/uv" in entries,
                "Ubuntu runtime must contain only Linux uv")
        uv = entries["tools/uv"]
        require(len(uv) >= 20 and uv[:4] == b"\x7fELF" and uv[4:6] == b"\x02\x01"
                and uv[18:20] == b"\x3e\x00", "Ubuntu uv is not Linux x86_64 ELF")
        require(config.get("services", {}).get("device", {}).get("settings", {}).get("bash_path") == "/bin/bash",
                "Ubuntu device plugin shell is not /bin/bash")
    else:
        require(entries.get("tools/uv.exe", b"").startswith(b"MZ"),
                "Windows runtime uv is not a PE executable")


def read_ar(data):
    require(data.startswith(b"!<arch>\n"), "Invalid Debian ar signature")
    entries, offset = {}, 8
    while offset < len(data):
        header = data[offset:offset + 60]
        require(len(header) == 60 and header[58:60] == b"`\n", "Malformed Debian ar header")
        name = header[:16].decode("ascii").strip().rstrip("/")
        safe_name(name)
        size = int(header[48:58])
        end = offset + 60 + size
        require(size >= 0 and end <= len(data), "Truncated Debian ar member")
        require(name not in entries, "Duplicate Debian ar member")
        entries[name] = data[offset + 60:end]
        if size % 2:
            require(data[end:end + 1] == b"\n", "Invalid Debian ar padding")
        offset = end + size % 2
    require(list(entries) == ["debian-binary", "control.tar.xz", "data.tar.xz"],
            "Unexpected Debian ar member set/order")
    require(entries["debian-binary"] == b"2.0\n", "Unsupported Debian package format")
    return entries


def reject_pax(data):
    # tarfile consumes PAX metadata while reading; inspect raw headers as well.
    with lzma.LZMAFile(io.BytesIO(data)) as stream:
        while True:
            header = stream.read(512)
            if not header or header == b"\0" * 512:
                return
            require(len(header) == 512, "Truncated tar header")
            entry = tarfile.TarInfo.frombuf(header, "utf-8", "surrogateescape")
            require(entry.type not in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE),
                    "PAX extension headers are not supported in these packages")
            stream.seek((entry.size + 511) // 512 * 512, io.SEEK_CUR)


def read_tar(data):
    reject_pax(data)
    entries, modes, directories, seen = {}, {}, set(), set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            safe_name(name)
            require(name not in seen, "Duplicate tar member name")
            seen.add(name)
            require(not member.pax_headers, "PAX metadata is not supported")
            require(member.uid == member.gid == 0, "Package members must be root-owned")
            require(member.mode & 0o7000 == 0, "Unexpected special permission bits")
            if member.isdir():
                require(member.mode == 0o755, "Unexpected directory permissions: " + name)
                directories.add(name)
            elif member.issym():
                # Only uv's cache links (relative, staying inside the bundled cache).
                require("tools/uv-cache/" in name and not member.linkname.startswith("/"),
                        "Unexpected symlink: " + name)
                cache = name[:name.index("tools/uv-cache/") + len("tools/uv-cache")]
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), member.linkname))
                require(resolved.startswith(cache + "/"), "Symlink leaves the bundled cache: " + name)
                entries[name], modes[name] = b"symlink:" + member.linkname.encode(), SYMLINK_MODE
            else:
                require(member.isfile(), "Tar links and special files are not supported")
                stream = archive.extractfile(member)
                require(stream is not None, "Tar member data is missing")
                entries[name], modes[name] = stream.read(), member.mode
        require(not archive.pax_headers, "Global PAX metadata is not supported")
    for name in seen:
        for parent in PurePosixPath(name).parents:
            if parent.as_posix() != ".":
                require(parent.as_posix() in directories, "Required parent directory missing: " + parent.as_posix())
    return entries, modes


def verify_launcher(entries, modes, root, prefix):
    source_map = {
        "ubuntu_launcher.py": "packaging/ubuntu/ubuntu_launcher.py",
        UBUNTU_LAUNCHER: "packaging/ubuntu/" + UBUNTU_LAUNCHER,
        "startup.html": "desktop/dist/index.html",
    }
    for name, relative in source_map.items():
        path = prefix + name
        require(path in entries and entries[path] == (root / relative).read_bytes(),
                "Ubuntu launcher source mismatch: " + name)
        require(modes[path] == (0o644 if name == "startup.html" else 0o755),
                "Ubuntu launcher permissions mismatch: " + name)


def verify_deb(data, root, enrollment, token, *, private, version):
    members = read_ar(data)
    control, control_modes = read_tar(members["control.tar.xz"])
    require(set(control) == {"control", "md5sums"}, "Unexpected Debian control file set")
    require(all(mode == 0o644 for mode in control_modes.values()), "Unexpected Debian control permissions")
    fields = dict(line.split(": ", 1) for line in control["control"].decode().splitlines() if ": " in line and not line.startswith(" "))
    package = UBUNTU_PRIVATE_PACKAGE if private else UBUNTU_PACKAGE
    require(fields.get("Package") == package and fields.get("Version") == version
            and fields.get("Architecture") == "amd64", "Debian package identity mismatch")
    entries, modes = read_tar(members["data.tar.xz"])
    md5s = {}
    for line in control["md5sums"].decode().splitlines():
        expected, name = line.split("  ", 1)
        require(name not in md5s, "Duplicate Debian md5 entry")
        md5s[name] = expected
    files = {name for name in entries if modes[name] != SYMLINK_MODE}
    require(set(md5s) == files, "Debian md5 list does not cover the exact data file set")
    for name, expected in md5s.items():
        require(hashlib.md5(entries[name]).hexdigest() == expected, "Debian md5 mismatch: " + name)
    prefix = UBUNTU_LIB
    runtime_prefix = prefix + "runtime/"
    runtime = {name[len(runtime_prefix):]: value for name, value in entries.items() if name.startswith(runtime_prefix)}
    verify_runtime(runtime, root, enrollment, token, private=private, linux=True)
    verify_privacy(entries, enrollment, token, private, runtime_prefix + PRIVATE_PATH)
    verify_launcher(entries, modes, root, prefix)
    required = {
        "usr/bin/" + UBUNTU_LAUNCHER: (f'#!/bin/sh\nexec /{UBUNTU_LIB}{UBUNTU_LAUNCHER} "$@"\n'.encode(), 0o755),
        "usr/bin/anx": (f'#!/bin/sh\nexec /{UBUNTU_LIB}{UBUNTU_LAUNCHER} anx "$@"\n'.encode(), 0o755),
        f"usr/share/applications/{UBUNTU_PACKAGE}.desktop": ((root / f"packaging/ubuntu/{UBUNTU_PACKAGE}.desktop").read_bytes(), 0o644),
        f"usr/share/icons/hicolor/128x128/apps/{UBUNTU_PACKAGE}.png": ((root / "agent_nexus/static/icon.png").read_bytes(), 0o644),
    }
    for name, (expected, mode) in required.items():
        require(entries.get(name) == expected and modes.get(name) == mode,
                "Debian integration file mismatch: " + name)
    for name in runtime:
        # The bundled CPython and package cache keep their executables executable.
        allowed = ({0o755} if name == "tools/uv" else {0o644, 0o755, SYMLINK_MODE}
                   if name.startswith(("tools/python/", "tools/uv-cache/")) else {0o644})
        require(modes[runtime_prefix + name] in allowed, "Debian runtime permissions mismatch: " + name)
    return {name[len(prefix):]: (digest(data), modes[name]) for name, data in entries.items() if name.startswith(prefix)}


def verify_portable(data, expected, root, enrollment, token, *, private):
    entries, modes = read_tar(data)
    require(set(entries) == set(expected) | {"start.sh"}, "Portable file set differs from Debian payload")
    for name, (sha256, mode) in expected.items():
        require(digest(entries[name]) == sha256 and modes[name] == mode,
                "Portable content/permissions differ from Debian payload: " + name)
    require(entries["start.sh"] == b'#!/bin/sh\nset -eu\ncd "$(dirname "$0")"\nexec /usr/bin/python3 "$PWD/ubuntu_launcher.py" "$@"\n'
            and modes["start.sh"] == 0o755, "Portable start script mismatch")
    verify_launcher(entries, modes, root, "")
    verify_privacy(entries, enrollment, token, private, "runtime/" + PRIVATE_PATH)


def verify_source_package(data, root, enrollment, token, kind, desktop_sha256):
    entries = read_zip(data)
    verify_manifest(entries)
    verify_privacy(entries, enrollment, token)
    verify_current_source(entries, root, wechat=kind == "client", readme=True)
    if kind == "client":
        require(DESKTOP_EXE in entries and digest(entries[DESKTOP_EXE]) == desktop_sha256,
                "Client ZIP does not contain this build's public desktop executable")
    else:
        require(all(not name.startswith("builtins/") or name.startswith("builtins/mcpkit/") for name in entries),
                "Server package contains client-only builtins")


def verify_wheel(data, root, enrollment, token, manager):
    entries = read_zip(data)
    verify_privacy(entries, enrollment, token)
    records = [name for name in entries if name.endswith(".dist-info/RECORD")]
    require(len(records) == 1, "Wheel must contain exactly one RECORD")
    record = records[0]
    rows = list(csv.reader(io.StringIO(entries[record].decode())))
    require(all(len(row) == 3 for row in rows), "Malformed wheel RECORD")
    require(len(rows) == len({row[0] for row in rows}) and {row[0] for row in rows} == set(entries),
            "Wheel RECORD does not cover the exact file set")
    for name, encoded, size in rows:
        if name == record:
            require(not encoded and not size, "Wheel RECORD must not hash itself")
            continue
        expected = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(entries[name]).digest()).rstrip(b"=").decode()
        require(encoded == expected and size == str(len(entries[name])), "Wheel RECORD mismatch: " + name)
    if manager:
        verify_current_source(entries, root, wechat=True, wheel=True)
    else:
        require("mcpkit/__init__.py" in entries, "mcpkit wheel package is missing")
        for name, data in entries.items():
            if name.startswith("mcpkit/"):
                source = root / "builtins/mcpkit/src" / name
                require(source.is_file() and source.read_bytes() == data, "mcpkit wheel source mismatch: " + name)


def verify_installer(data):
    """An NSIS installer: a PE file carrying the Nullsoft stub's signature."""
    require(data.startswith(b"MZ") and b"Nullsoft" in data, "Installer is not an NSIS executable")


def verify_signature(data):
    """A Tauri updater signature: base64 of a minisign signature file."""
    try:
        text = base64.b64decode(data.strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Updater signature is not base64 minisign text") from None
    lines = text.splitlines()
    require(len(lines) >= 4 and lines[0].startswith("untrusted comment:") and lines[2].startswith("trusted comment:"),
            "Updater signature is not a minisign signature")
    try:
        signature = base64.b64decode(lines[1], validate=True)
    except ValueError:
        raise ValueError("Updater signature body is not base64") from None
    require(len(signature) == 74 and signature[:2] in (b"Ed", b"ED"), "Updater signature has an unexpected algorithm or size")


def verify_public_desktop(root, out, *, version, require_installer=True, require_signature=False):
    """The public desktop edition alone: the raw exe embeds this build's public
    runtime (no ``.private`` payload anywhere in it), and the installer and its
    updater signature are present. Needs no enrollment profile."""
    root, out = Path(root).resolve(), Path(out).resolve()
    receipt = []

    def read_product(path):
        require(path.is_file(), "Required artifact missing: " + str(path))
        data = path.read_bytes()
        return data, {"path": str(path), "sha256": digest(data), "size": len(data), "private": False}

    binary, product = read_product(out / DESKTOP_EXE)
    runtime, runtime_product = read_product(out / "agent-nexus-runtime.zip")
    require(binary.startswith(b"MZ") and runtime in binary, "Current runtime missing from " + DESKTOP_EXE)
    # Any ZIP member under .private/ (local header or central directory), wherever
    # it sits in the exe; the shell's own code may name the path as a string.
    require(not re.search(rb"(PK\x03\x04.{26}|PK\x01\x02.{42})\.private/", binary, re.S),
            "Public desktop carries a private enrollment payload")
    # No profile exists for the public edition: a random marker stands in for its
    # token, so every ``.private`` and enrollment check still runs.
    verify_runtime(read_zip(runtime), root, None, os.urandom(24).hex().encode(), private=False)
    product.update(embedded_runtime_verified=True)
    runtime_product.update(runtime_manifest_verified=True)
    receipt += [product, runtime_product]
    setup_path = out / INSTALLER_TEMPLATE.format(version=version)
    if require_installer or setup_path.exists():
        setup, setup_product = read_product(setup_path)
        verify_installer(setup)
        setup_product.update(installer_verified=True)
        receipt.append(setup_product)
        signature_path = setup_path.with_name(setup_path.name + ".sig")
        if require_signature or signature_path.exists():
            signature, signature_product = read_product(signature_path)
            verify_signature(signature)
            signature_product.update(updater_signature_format_verified=True)
            receipt.append(signature_product)
    return {"version": version, "products": receipt}


def verify_products(root, out, enrollment_file, *, complete=False, version=None):
    from build_release import load_enrollment
    from build_ubuntu import VERSION

    root, out = Path(root).resolve(), Path(out).resolve()
    enrollment = load_enrollment(Path(enrollment_file))
    token = json.loads(enrollment)["token"].encode()
    version = VERSION if version is None else version
    receipt = []

    def read_product(path):
        require(path.is_file(), "Required artifact missing: " + str(path))
        data = path.read_bytes()
        return data, {"path": str(path), "sha256": digest(data), "size": len(data)}

    private_runtime = (out / "private/agent-nexus-runtime.zip").read_bytes()
    desktop_sha256 = None
    for name, runtime_name, private in (
        (DESKTOP_EXE, "agent-nexus-runtime.zip", False),
        (PRIVATE_DESKTOP_EXE, "private/agent-nexus-runtime.zip", True),
    ):
        binary, product = read_product(out / name)
        runtime, runtime_product = read_product(out / runtime_name)
        require(binary.startswith(b"MZ") and runtime in binary, "Current runtime missing from " + name)
        if not private:
            require(private_runtime not in binary, "Public desktop embeds the private runtime")
        require(token not in binary.replace(runtime, b""), "Enrollment token occurs outside the embedded runtime")
        verify_runtime(read_zip(runtime), root, enrollment, token, private=private)
        product.update(private=private, embedded_runtime_verified=True)
        runtime_product.update(private=private, runtime_manifest_verified=True)
        receipt.append(product)
        receipt.append(runtime_product)
        if not private:
            desktop_sha256 = product["sha256"]
    del private_runtime, binary, runtime

    for name, private in ((UBUNTU_PACKAGE, False), (UBUNTU_PRIVATE_PACKAGE, True)):
        data, product = read_product(out / "ubuntu" / f"{name}_{version}_amd64.deb")
        expected = verify_deb(data, root, enrollment, token, private=private, version=version)
        product.update(private=private, debian_layout_verified=True)
        receipt.append(product)
        data, product = read_product(out / "ubuntu" / f"{name}_{version}_linux-amd64.tar.xz")
        verify_portable(data, expected, root, enrollment, token, private=private)
        product.update(private=private, portable_matches_deb=True)
        receipt.append(product)
    del data, expected

    for name in [INSTALLER_TEMPLATE.format(version=version)]:
        path = out / name
        if path.exists():
            data, product = read_product(path)
            verify_installer(data)
            product.update(private=False, installer_verified=True)
            receipt.append(product)
            data, product = read_product(path.with_name(path.name + ".sig"))
            verify_signature(data)
            product.update(private=False, updater_signature_format_verified=True)
            receipt.append(product)

    for kind in ("client", "server"):
        path = out / f"agent-nexus-{kind}.zip"
        if complete or path.exists():
            data, product = read_product(path)
            verify_source_package(data, root, enrollment, token, kind, desktop_sha256)
            product.update(private=False, source_package_verified=True)
            receipt.append(product)
    for distribution, manager in (("agent_nexus", True), ("mcpkit", False)):
        wheels = sorted((out / "python").glob(distribution + "-*-py3-none-any.whl"))
        if complete or wheels:
            require(len(wheels) == 1, "Expected exactly one current wheel for " + distribution)
            data, product = read_product(wheels[0])
            verify_wheel(data, root, enrollment, token, manager)
            product.update(private=False, wheel_record_verified=True)
            receipt.append(product)

    report = {"ubuntu_version": version, "complete": complete, "products": receipt}
    (out / "client-artifacts.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    checksums = "".join(product["sha256"] + "  " + Path(product["path"]).relative_to(out).as_posix() + "\n"
                        for product in receipt)
    (out / "ARTIFACTS.sha256").write_text(checksums, encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "artifacts")
    parser.add_argument("--enrollment-file", type=Path, default=ROOT / "secrets/build-enrollment.json")
    parser.add_argument("--complete", action="store_true", help="also require client/server ZIP and both wheels")
    parser.add_argument("--desktop-only", action="store_true",
                        help="only the public desktop edition: exe, runtime, NSIS installer and updater signature")
    parser.add_argument("--require-signature", action="store_true", help="with --desktop-only: the .sig must exist")
    args = parser.parse_args(argv)
    if args.desktop_only:
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        from build_ubuntu import VERSION
        report = verify_public_desktop(ROOT, args.out, version=VERSION, require_signature=args.require_signature)
        (Path(args.out) / "desktop-artifacts.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        for product in report["products"]:
            print(f"{product['sha256']}  {product['size']:>10}  {Path(product['path']).name}")
        signed = any(p["path"].endswith(".sig") for p in report["products"])
        print(f"Verified the public desktop edition {VERSION}: no private payload, installer present, "
              + ("updater signature well-formed" if signed else "NOT signed for the updater"))
        return
    report = verify_products(ROOT, args.out, args.enrollment_file, complete=args.complete)
    print(f"Verified {len(report['products'])} release products: source, manifests, platform layout and credential boundaries")
    print("Artifact receipt:", args.out.resolve() / "client-artifacts.json")


if __name__ == "__main__":
    main()
