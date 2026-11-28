"""Self-contained archive fixtures; never launches packaged applications."""
import base64
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path, PurePosixPath
import sys
import tarfile
import tempfile
import types
import unittest
import warnings
import zipfile

spec = importlib.util.spec_from_file_location(
    "verify_artifacts_tests", Path(__file__).resolve().parents[1] / "scripts/verify_client_artifacts.py")
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)


def zip_bytes(entries, manifest=True):
    entries = dict(entries)
    if manifest:
        entries["SHA256.json"] = json.dumps({p: v.digest(d) for p, d in entries.items()}).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def tar_bytes(entries, fmt=tarfile.GNU_FORMAT, omit_parent=False):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz", format=fmt) as archive:
        parents = {p.as_posix() for name in entries for p in PurePosixPath(name).parents if str(p) != "."}
        for parent in sorted(parents):
            if omit_parent:
                continue
            member = tarfile.TarInfo(parent + "/")
            member.type, member.mode = tarfile.DIRTYPE, 0o755
            archive.addfile(member)
        for name, (data, mode) in entries.items():
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(data), mode
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def ar_member(name, data):
    return f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode() + data + (b"\n" if len(data) % 2 else b"")


def wheel_bytes(entries, distribution):
    entries = dict(entries)
    record = distribution + "-0.1.0.dist-info/RECORD"
    stream = io.StringIO()
    writer = csv.writer(stream)
    for name, data in entries.items():
        sha = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow([name, "sha256=" + sha, len(data)])
    writer.writerow([record, "", ""])
    entries[record] = stream.getvalue().encode()
    return zip_bytes(entries, manifest=False)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="anx-verify-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.out = self.root / "output with spaces"
        self.out.mkdir()
        self.token = b"fixture-enrollment-credential-" + b"z" * 40
        self.enrollment = json.dumps({"url": "wss://hub.example.test", "agent_prefix": "portable-", "token": self.token.decode()}).encode()
        self.profile = self.root / "enrollment.json"
        self.profile.write_bytes(self.enrollment)
        self.source = {name: ("source: " + name).encode() for name in (*v.CORE_SOURCE, *v.WECHAT_SOURCE, "README.md")}
        self.source.update({
            "packaging/ubuntu/ubuntu_launcher.py": b"launcher",
            "packaging/ubuntu/agent-nexus": b"launcher-shell",
            "packaging/ubuntu/agent-nexus.desktop": b"desktop",
            "desktop/dist/index.html": b"startup",
            "agent_nexus/static/icon.png": b"icon",
            "builtins/mcpkit/src/mcpkit/__init__.py": b"mcpkit",
        })
        for name, data in self.source.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.runtime = {name: self.source[name] for name in (*v.CORE_SOURCE, *v.WECHAT_SOURCE, "README.md")}
        self.runtime["agent-nexus.json"] = b'{"cloud":{"enabled":false},"services":{"device":{"settings":{"bash_path":"/bin/bash"}}}}'
        self.runtime["tools/uv.exe"] = b"MZ-fixture-uv"

    def linux_entries(self, private=False):
        runtime = dict(self.runtime)
        runtime.pop("tools/uv.exe")
        runtime["tools/uv"] = b"\x7fELF\x02\x01" + b"\0" * 12 + b"\x3e\x00"
        if private:
            runtime[v.PRIVATE_PATH] = self.enrollment
            runtime[v.PRIVATE_ALLOW_PATH] = v.expected_endpoint_allow(self.enrollment)
        runtime["SHA256.json"] = json.dumps({p: v.digest(d) for p, d in runtime.items()}).encode()
        prefix = "usr/lib/agent-nexus/"
        entries = {prefix + "runtime/" + name: (data, 0o755 if name == "tools/uv" else 0o644) for name, data in runtime.items()}
        for name, src in (("ubuntu_launcher.py", "packaging/ubuntu/ubuntu_launcher.py"), ("agent-nexus", "packaging/ubuntu/agent-nexus"), ("startup.html", "desktop/dist/index.html")):
            entries[prefix + name] = self.source[src], 0o644 if name == "startup.html" else 0o755
        entries.update({
            "usr/bin/agent-nexus": (b'#!/bin/sh\nexec /usr/lib/agent-nexus/agent-nexus "$@"\n', 0o755),
            "usr/bin/anx": (b'#!/bin/sh\nexec /usr/lib/agent-nexus/agent-nexus anx "$@"\n', 0o755),
            "usr/share/applications/agent-nexus.desktop": (self.source["packaging/ubuntu/agent-nexus.desktop"], 0o644),
            "usr/share/icons/hicolor/128x128/apps/agent-nexus.png": (b"icon", 0o644),
        })
        return entries

    def linux_packages(self, private=False):
        entries = self.linux_entries(private)
        name = "agent-nexus-private" if private else "agent-nexus"
        control = {"control": (f"Package: {name}\nVersion: 0.1.4\nArchitecture: amd64\n".encode(), 0o644),
                   "md5sums": ("".join(hashlib.md5(data).hexdigest() + "  " + path + "\n" for path, (data, _) in entries.items()).encode(), 0o644)}
        deb = b"!<arch>\n" + ar_member("debian-binary", b"2.0\n") + ar_member("control.tar.xz", tar_bytes(control)) + ar_member("data.tar.xz", tar_bytes(entries))
        prefix = "usr/lib/agent-nexus/"
        portable = {name[len(prefix):]: value for name, value in entries.items() if name.startswith(prefix)}
        portable["start.sh"] = (b'#!/bin/sh\nset -eu\ncd "$(dirname "$0")"\nexec /usr/bin/python3 "$PWD/ubuntu_launcher.py" "$@"\n', 0o755)
        return deb, tar_bytes(portable)

    def make_complete(self):
        for private in (False, True):
            runtime = dict(self.runtime)
            if private:
                runtime[v.PRIVATE_PATH] = self.enrollment
                runtime[v.PRIVATE_ALLOW_PATH] = v.expected_endpoint_allow(self.enrollment)
            archive = zip_bytes(runtime)
            directory = self.out / "private" if private else self.out
            directory.mkdir(exist_ok=True)
            (directory / "agent-nexus-runtime.zip").write_bytes(archive)
            (self.out / ("agent-nexus-private.exe" if private else "agent-nexus.exe")).write_bytes(b"MZ-fixture-desktop" + archive)
            deb, portable = self.linux_packages(private)
            name = "agent-nexus-private" if private else "agent-nexus"
            (self.out / "ubuntu").mkdir(exist_ok=True)
            (self.out / "ubuntu" / f"{name}_0.1.4_amd64.deb").write_bytes(deb)
            (self.out / "ubuntu" / f"{name}_0.1.4_linux-amd64.tar.xz").write_bytes(portable)
        client = dict(self.runtime, **{"agent-nexus.exe": (self.out / "agent-nexus.exe").read_bytes()})
        (self.out / "agent-nexus-client.zip").write_bytes(zip_bytes(client))
        server = {name: data for name, data in self.runtime.items() if not name.startswith(("builtins/", "tools/"))}
        (self.out / "agent-nexus-server.zip").write_bytes(zip_bytes(server))
        (self.out / "python").mkdir()
        manager = {("agent_nexus/" + name if name.startswith("builtins/") else name): data for name, data in self.runtime.items()
                   if name.startswith(("agent_nexus/", "mcpm/", "builtins/"))}
        (self.out / "python/agent_nexus-0.1.0-py3-none-any.whl").write_bytes(wheel_bytes(manager, "agent_nexus"))
        (self.out / "python/mcpkit-0.1.0-py3-none-any.whl").write_bytes(wheel_bytes({"mcpkit/__init__.py": b"mcpkit"}, "mcpkit"))

    def test_complete_twelve_products_and_space_output(self):
        self.make_complete()
        release = types.ModuleType("build_release")
        release.load_enrollment = lambda path: path.read_bytes()
        ubuntu = types.ModuleType("build_ubuntu")
        ubuntu.VERSION = "0.1.4"
        from unittest.mock import patch
        with patch.dict(sys.modules, {"build_release": release, "build_ubuntu": ubuntu}):
            report = v.verify_products(self.root, self.out, self.profile, complete=True)
        self.assertEqual(len(report["products"]), 12)
        self.assertEqual(len((self.out / "ARTIFACTS.sha256").read_text().splitlines()), 12)
        self.assertNotIn(self.token, (self.out / "client-artifacts.json").read_bytes())

    def test_valid_private_runtime(self):
        entries = v.read_zip(zip_bytes(dict(self.runtime, **{v.PRIVATE_PATH: self.enrollment,
                                                            v.PRIVATE_ALLOW_PATH: b"wss://hub.example.test\n"})))
        v.verify_runtime(entries, self.root, self.enrollment, self.token, private=True)

    def test_reject_private_runtime_without_or_with_altered_endpoint_allow(self):
        for allow in (None, b"wss://other.example.test\n"):
            runtime = dict(self.runtime, **{v.PRIVATE_PATH: self.enrollment})
            if allow is not None:
                runtime[v.PRIVATE_ALLOW_PATH] = allow
            with self.subTest(allow=allow), self.assertRaisesRegex(ValueError, "allow-list"):
                v.verify_runtime(v.read_zip(zip_bytes(runtime)), self.root, self.enrollment, self.token, private=True)

    def test_wechat_source_is_optional_when_plugin_checkout_is_absent(self):
        import shutil
        shutil.rmtree(self.root / "builtins/wechat-mcp")
        runtime = {name: data for name, data in self.runtime.items() if not name.startswith("builtins/wechat-mcp/")}
        v.verify_runtime(v.read_zip(zip_bytes(runtime)), self.root, self.enrollment, self.token)

    def test_reject_stale_source_even_with_valid_hash(self):
        runtime = dict(self.runtime, **{"agent_nexus/agent.py": b"old"})
        with self.assertRaisesRegex(ValueError, "stale"):
            v.verify_runtime(v.read_zip(zip_bytes(runtime)), self.root, self.enrollment, self.token)

    def test_reject_extra_manifest_file(self):
        entries = v.read_zip(zip_bytes(self.runtime))
        entries["extra.txt"] = b"unlisted"
        with self.assertRaisesRegex(ValueError, "exact file set"):
            v.verify_manifest(entries)

    def test_reject_duplicate_zip_name(self):
        buffer = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("same", b"a")
                archive.writestr("same", b"b")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            v.read_zip(buffer.getvalue())

    def test_reject_key_outside_profile_without_echo(self):
        entries = {"note.txt": self.token}
        with self.assertRaises(ValueError) as caught:
            v.verify_privacy(entries, self.enrollment, self.token)
        self.assertNotIn(self.token.decode(), str(caught.exception))

    def test_reject_old_client_executable(self):
        client = dict(self.runtime, **{"agent-nexus.exe": b"MZ-old"})
        with self.assertRaisesRegex(ValueError, "this build"):
            v.verify_source_package(zip_bytes(client), self.root, self.enrollment, self.token, "client", v.digest(b"MZ-new"))

    def test_reject_missing_tar_parent(self):
        with self.assertRaisesRegex(ValueError, "parent directory"):
            v.read_tar(tar_bytes({"a/b.txt": (b"data", 0o644)}, omit_parent=True))

    def test_reject_pax_header(self):
        with self.assertRaisesRegex(ValueError, "PAX"):
            v.read_tar(tar_bytes({"z" * 120: (b"data", 0o644)}, fmt=tarfile.PAX_FORMAT))

    def test_reject_portable_corruption(self):
        deb, portable = self.linux_packages()
        expected = v.verify_deb(deb, self.root, self.enrollment, self.token, private=False, version="0.1.4")
        entries, modes = v.read_tar(portable)
        entries["ubuntu_launcher.py"] = b"old launcher"
        corrupted = tar_bytes({name: (data, modes[name]) for name, data in entries.items()})
        with self.assertRaisesRegex(ValueError, "differ"):
            v.verify_portable(corrupted, expected, self.root, self.enrollment, self.token, private=False)


    def public_desktop(self, runtime=None, signature=None):
        archive = zip_bytes(runtime or self.runtime)
        (self.out / "agent-nexus-runtime.zip").write_bytes(archive)
        (self.out / "agent-nexus.exe").write_bytes(b"MZ-fixture-desktop .private/enrollment.json named in code " + archive)
        setup = self.out / "agent-nexus_0.2.0_x64-setup.exe"
        setup.write_bytes(b"MZ-fixture Nullsoft Install System")
        if signature is None:
            body = base64.b64encode(b"Ed" + b"k" * 8 + b"s" * 64).decode()
            text = f"untrusted comment: signature from tauri secret key\n{body}\ntrusted comment: timestamp:1\n{body}\n"
            signature = base64.b64encode(text.encode())
        (self.out / (setup.name + ".sig")).write_bytes(signature)

    def test_public_desktop_edition_without_a_profile(self):
        self.public_desktop()
        report = v.verify_public_desktop(self.root, self.out, version="0.2.0")
        names = [Path(product["path"]).name for product in report["products"]]
        self.assertEqual(names, ["agent-nexus.exe", "agent-nexus-runtime.zip",
                                 "agent-nexus_0.2.0_x64-setup.exe", "agent-nexus_0.2.0_x64-setup.exe.sig"])
        self.assertTrue(all(product["private"] is False for product in report["products"]))

    def test_public_desktop_rejects_a_private_payload(self):
        runtime = dict(self.runtime, **{v.PRIVATE_PATH: self.enrollment})
        self.public_desktop(runtime)
        with self.assertRaisesRegex(ValueError, "private enrollment payload"):
            v.verify_public_desktop(self.root, self.out, version="0.2.0")

    def test_public_desktop_rejects_a_bad_signature_or_missing_installer(self):
        self.public_desktop(signature=b"not base64 !!")
        with self.assertRaisesRegex(ValueError, "signature"):
            v.verify_public_desktop(self.root, self.out, version="0.2.0")
        (self.out / "agent-nexus_0.2.0_x64-setup.exe.sig").unlink()
        v.verify_public_desktop(self.root, self.out, version="0.2.0")  # unsigned build
        with self.assertRaisesRegex(ValueError, "missing"):
            v.verify_public_desktop(self.root, self.out, version="0.2.0", require_signature=True)
        (self.out / "agent-nexus_0.2.0_x64-setup.exe").unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            v.verify_public_desktop(self.root, self.out, version="0.2.0")
        v.verify_public_desktop(self.root, self.out, version="0.2.0", require_installer=False)

if __name__ == "__main__":
    unittest.main()
