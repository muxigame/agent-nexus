"""Release regression tests use tiny fixtures and a fake compiler, never services or keys."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile


SOURCE = Path(os.environ.get("ANX_BUILD_TEST_ROOT") or os.environ.get("MCPM_BUILD_TEST_ROOT") or Path(__file__).resolve().parents[1])


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SOURCE / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load_script("build_release")
wheels = load_script("build_wheels")
with patch.dict(sys.modules, {"build_release": release}):
    desktop = load_script("build_desktop")
    ubuntu = load_script("build_ubuntu")


class BuildPipelineTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="anx build tests "))
        self.root = Path(temporary).resolve()
        self.out = self.root / "new output"
        self.key = "fixture-only-not-a-real-key-" + "x" * 40
        for name, content in {
            "pyproject.toml": '[project]\nname = "fixture"\nversion = "0.0.0"\n',
            "uv.lock": "fixture lock\n", "README.md": "fixture source\n",
            "build_hooks.py": "# fixture\n", "bundle_files.py": "# fixture\n",
            "agent_nexus/agent.py": "# current agent\n", "agent_nexus/federation.py": "# current peer\n",
            "agent_nexus/static/index.html": "<html>fixture</html>",
            "agent_nexus/static/icon.png": "fixture icon",
            "mcpm/cli.py": "# compatibility shim\n",
            "desktop/dist/index.html": "<html>startup</html>",
            "packaging/ubuntu/ubuntu_launcher.py": "# current launcher\n",
            "packaging/ubuntu/agent-nexus": "#!/bin/sh\nexit 0\n",
            "packaging/ubuntu/agent-nexus.desktop": "[Desktop Entry]\nName=fixture\n",
            "config/client.example.json": json.dumps({"cloud": {"enabled": True}, "services": {"device": {}}}),
            "config/server.example.json": "{}",
            "desktop/src-tauri/Cargo.toml": '[package]\nname = "agent-nexus-desktop"\nversion = "0.2.0"\n',
        }.items():
            self.write(name, content.encode())
        for name in release.bundle.BUILTINS:
            self.write("builtins/" + name + "/README.md", b"fixture builtin\n")
        self.uv = self.write("custom tools/custom uv.exe", b"MZfixture-selected-uv")
        self.uv.chmod(0o755)
        self.profile = self.write("secrets/build-enrollment.json", json.dumps({
            "url": "wss://hub.example.test", "agent_prefix": "portable-", "token": self.key,
        }).encode())
        # Private builds pin an endpoint given independently of the profile.
        self.stack.enter_context(patch.dict(os.environ, {"ANX_ENROLLMENT_ENDPOINT": "wss://hub.example.test"}))
        os.environ.pop("MCPM_ENROLLMENT_ENDPOINT", None)
        self.old = self.write("artifacts/agent-nexus.exe", b"MZold-default-executable")
        for module in (release, desktop, ubuntu, wheels):
            self.stack.enter_context(patch.object(module, "ROOT", self.root))
        self.protected = []
        config = types.ModuleType("mcpkit.config")
        config.protect = lambda path: self.protected.append(Path(path))
        self.stack.enter_context(patch.dict(sys.modules, {"mcpkit": types.ModuleType("mcpkit"), "mcpkit.config": config}))
        self.output = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def zip_files(self, path):
        with zipfile.ZipFile(path) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def test_client_uses_executable_and_uv_from_selected_paths(self):
        executable = self.write("new output/agent-nexus.exe", b"MZnew-selected-executable")
        release.build("client", self.out, uv=self.uv)
        files = self.zip_files(self.out / "agent-nexus-client.zip")
        self.assertEqual(files["agent-nexus.exe"], executable.read_bytes())
        self.assertIn("mcpm/cli.py", files)  # the compatibility shim ships until 1.0
        self.assertIn("agent-nexus.json", files)
        self.assertIn(b" anx --root ", files["anx.ps1"])
        self.assertEqual(files["tools/uv.exe"], self.uv.read_bytes())
        self.assertEqual(self.old.read_bytes(), b"MZold-default-executable")
        self.assertFalse((self.root / "artifacts/agent-nexus-client.zip").exists())

    def test_client_missing_selected_executable_does_not_fall_back_to_old(self):
        with self.assertRaises(FileNotFoundError):
            release.build("client", self.out, uv=self.uv)
        self.assertFalse((self.out / "agent-nexus-client.zip").exists())
        self.assertTrue(self.old.is_file())

    def test_both_wheels_use_the_same_absolute_output_with_spaces(self):
        destination = self.out / "python wheels"
        calls = []

        def fake_uv(command, *, cwd, check):
            project = Path(command[command.index("--project") + 1])
            target = Path(command[command.index("--out-dir") + 1])
            calls.append((project, target))
            self.assertEqual(command[0], str(self.uv))
            self.assertEqual(cwd, self.root)
            self.assertTrue(check)
            name = "manager.whl" if project == self.root else "mcpkit.whl"
            (target / name).write_bytes(b"fixture wheel")

        with patch.object(wheels.subprocess, "run", side_effect=fake_uv):
            wheels.build(destination, self.uv)
        self.assertEqual(calls, [(self.root, destination.resolve()),
                                 (self.root / "builtins/mcpkit", destination.resolve())])
        self.assertEqual({path.name for path in destination.iterdir()}, {"manager.whl", "mcpkit.whl"})

    def test_private_profile_is_only_in_private_runtime(self):
        release.build("runtime", self.out, uv=self.uv)
        release.build("runtime", self.out / "private", self.profile, uv=self.uv)
        release.build("server", self.out)
        for archive in (self.out / "agent-nexus-runtime.zip", self.out / "agent-nexus-server.zip"):
            files = self.zip_files(archive)
            self.assertFalse(any(".private" in Path(name).parts for name in files))
            self.assertFalse(any(self.key.encode() in data for data in files.values()))
        private = self.zip_files(self.out / "private/agent-nexus-runtime.zip")
        self.assertEqual(private[".private/enrollment.json"], self.profile.read_bytes())
        self.assertEqual([name for name, data in private.items() if self.key.encode() in data], [".private/enrollment.json"])
        self.assertNotIn(self.key, self.output.getvalue())

    def test_invalid_enrollment_does_not_echo_credential_contents(self):
        valid = json.loads(self.profile.read_bytes())
        cases = [[], {**valid, "url": "wrong"}, {**valid, "agent_prefix": "wrong"},
                 {**valid, "token": 32}, {**valid, "token": "x" * 31}]
        for profile in cases:
            with self.subTest(profile_type=type(profile).__name__):
                self.profile.write_text(json.dumps(profile), encoding="utf-8")
                with self.assertRaises(ValueError) as error:
                    release.load_enrollment(self.profile)
                self.assertNotIn(self.key, str(error.exception))
        self.profile.write_text('{"token":"' + self.key, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Invalid private enrollment JSON") as error:
            release.load_enrollment(self.profile)
        self.assertNotIn(self.key, str(error.exception))

    def test_desktop_editions_isolate_runtime_and_compiler_outputs(self):
        calls = []

        def compile_fixture(command, *, cwd, env, check):
            target = Path(command[command.index("--target-dir") + 1])
            runtime = Path(env["ANX_RUNTIME_ARCHIVE"])
            self.assertEqual(env["MCPM_RUNTIME_ARCHIVE"], env["ANX_RUNTIME_ARCHIVE"])
            calls.append((target, runtime, env["CARGO_TARGET_DIR"], command[1]))
            self.assertEqual(command[0], "selected-cargo")
            self.assertTrue(check)
            if command[1] == "tauri":
                # Public edition: the Tauri CLI builds the exe and the NSIS installer.
                self.assertEqual(command[1:6], ["tauri", "build", "--bundles", "nsis", "--ci"])
                self.assertEqual(cwd, self.root / "desktop/src-tauri")
                self.assertIn("--locked", command[command.index("--"):])
                nsis = target / "release/bundle/nsis"
                nsis.mkdir(parents=True, exist_ok=True)
                (nsis / "Agent Nexus_0.2.0_x64-setup.exe").write_bytes(b"MZfixture-setup")
            else:
                self.assertEqual(command[command.index("--bin") + 1], "agent-nexus")
                self.assertEqual(cwd, self.root)
            compiled = target / "release/agent-nexus.exe"
            compiled.parent.mkdir(parents=True, exist_ok=True)
            compiled.write_bytes(b"MZfixture-compiled-" + runtime.read_bytes())

        with patch.dict(os.environ, {"ANX_RUNTIME_ARCHIVE": "WRONG-PRIVATE-RUNTIME", "MCPM_RUNTIME_ARCHIVE": "WRONG-PRIVATE-RUNTIME", "CARGO_TARGET_DIR": "WRONG-TARGET", "CARGO_BUILD_TARGET": "",
                                     "TAURI_SIGNING_PRIVATE_KEY": "", "TAURI_SIGNING_PRIVATE_KEY_PASSWORD": ""}), \
                patch.object(desktop.subprocess, "run", side_effect=compile_fixture):
            ordinary = desktop.build(self.out, uv=self.uv, cargo="selected-cargo")
            private = desktop.build(self.out, self.profile, uv=self.uv, cargo="selected-cargo")
        expected_targets = [self.root / "desktop/src-tauri/target", self.root / "desktop/src-tauri/target-portable"]
        self.assertEqual([item[0] for item in calls], expected_targets)
        self.assertEqual([item[1] for item in calls], [self.out / "agent-nexus-runtime.zip", self.out / "private/agent-nexus-runtime.zip"])
        self.assertTrue(all(str(target) == environment for target, _, environment, _ in calls))
        self.assertEqual([item[3] for item in calls], ["tauri", "build"])
        # Unsigned public build: installer without updater artifacts; the private one has none.
        self.assertEqual((self.out / "agent-nexus_0.2.0_x64-setup.exe").read_bytes(), b"MZfixture-setup")
        self.assertFalse((self.out / "agent-nexus_0.2.0_x64-setup.exe.sig").exists())
        self.assertFalse(list((self.out / "private").glob("*setup*")))
        for executable, (_, runtime, _, _) in zip((ordinary, private), calls):
            self.assertIn(runtime.read_bytes(), executable.read_bytes())
        self.assertEqual(self.old.read_bytes(), b"MZold-default-executable")
        self.assertFalse((self.root / "WRONG-TARGET").exists())
        self.assertIn(self.out / "private", self.protected)
        self.assertNotIn(self.key, self.output.getvalue())

    def test_signed_installer_publishes_signature_without_echoing_the_key(self):
        secret = "fixture-signing-key-" + "k" * 30
        keys = self.root / "vault"
        keys.mkdir()
        (keys / "updater.key").write_text(secret + "\n", encoding="utf-8")
        (keys / "updater.key.password").write_text("fixture-password", encoding="utf-8")
        seen = {}

        def tauri_build(command, *, cwd, env, check):
            seen.update(command=command, key=env.get("TAURI_SIGNING_PRIVATE_KEY"),
                        password=env.get("TAURI_SIGNING_PRIVATE_KEY_PASSWORD"))
            target = Path(command[command.index("--target-dir") + 1])
            nsis = target / "release/bundle/nsis"
            nsis.mkdir(parents=True, exist_ok=True)
            (nsis / "Agent Nexus_0.1.0_x64-setup.exe").write_bytes(b"MZstale-older-version")
            (nsis / "Agent Nexus_0.2.0_x64-setup.exe").write_bytes(b"MZsigned-setup")
            (nsis / "Agent Nexus_0.2.0_x64-setup.exe.sig").write_text("fixture-signature")
            (target / "release/agent-nexus.exe").write_bytes(b"MZ" + Path(env["ANX_RUNTIME_ARCHIVE"]).read_bytes())

        with patch.dict(os.environ, {"CARGO_BUILD_TARGET": ""}), \
                patch.object(desktop.subprocess, "run", side_effect=tauri_build):
            desktop.build(self.out, uv=self.uv, signing_key_dir=keys)
        self.assertEqual(seen["key"], secret)
        self.assertEqual(seen["password"], "fixture-password")
        self.assertNotIn("--config", seen["command"])
        self.assertNotIn(secret, " ".join(seen["command"]))
        self.assertEqual((self.out / "agent-nexus_0.2.0_x64-setup.exe").read_bytes(), b"MZsigned-setup")
        self.assertEqual((self.out / "agent-nexus_0.2.0_x64-setup.exe.sig").read_text(), "fixture-signature")
        self.assertNotIn(secret, self.output.getvalue())

    def test_signed_build_requires_the_signature(self):
        def unsigned(command, *, cwd, env, check):
            target = Path(command[command.index("--target-dir") + 1])
            nsis = target / "release/bundle/nsis"
            nsis.mkdir(parents=True, exist_ok=True)
            (nsis / "Agent Nexus_0.2.0_x64-setup.exe").write_bytes(b"MZsetup")
            (target / "release/agent-nexus.exe").write_bytes(b"MZ" + Path(env["ANX_RUNTIME_ARCHIVE"]).read_bytes())

        with patch.dict(os.environ, {"CARGO_BUILD_TARGET": "", "TAURI_SIGNING_PRIVATE_KEY": "fixture",
                                     "TAURI_SIGNING_PRIVATE_KEY_PASSWORD": ""}), \
                patch.object(desktop.subprocess, "run", side_effect=unsigned):
            with self.assertRaisesRegex(RuntimeError, "signature"):
                desktop.build(self.out, uv=self.uv)

    def test_private_desktop_never_sees_the_signing_key(self):
        seen = {}

        def compile_private(command, *, cwd, env, check):
            seen.update(command=command, key=env.get("TAURI_SIGNING_PRIVATE_KEY"))
            target = Path(command[command.index("--target-dir") + 1]) / "release/agent-nexus.exe"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"MZ" + Path(env["ANX_RUNTIME_ARCHIVE"]).read_bytes())

        with patch.dict(os.environ, {"CARGO_BUILD_TARGET": "", "TAURI_SIGNING_PRIVATE_KEY": "fixture-key"}), \
                patch.object(desktop.subprocess, "run", side_effect=compile_private):
            desktop.build(self.out, self.profile, uv=self.uv)
        self.assertEqual(seen["command"][1], "build")
        self.assertIsNone(seen["key"])

    def test_public_desktop_rejects_private_runtime_before_compiling(self):
        release.build("runtime", self.out / "private", self.profile, uv=self.uv)
        with patch.object(desktop.subprocess, "run") as compiler:
            with self.assertRaisesRegex(ValueError, "credential boundary"):
                desktop.build(self.out, runtime=self.out / "private/agent-nexus-runtime.zip")
        compiler.assert_not_called()
        self.assertFalse((self.out / "agent-nexus.exe").exists())

    def test_compiler_output_missing_current_runtime_cannot_replace_artifact(self):
        old = self.write("new output/agent-nexus.exe", b"MZalready-published")

        def compile_stale(command, **kwargs):
            target = Path(command[command.index("--target-dir") + 1]) / "release/agent-nexus.exe"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"MZstale-compiler-output")

        with patch.dict(os.environ, {"CARGO_BUILD_TARGET": ""}), patch.object(desktop.subprocess, "run", side_effect=compile_stale):
            with self.assertRaisesRegex(RuntimeError, "does not embed"):
                desktop.build(self.out, uv=self.uv)
        self.assertEqual(old.read_bytes(), b"MZalready-published")

    def test_failed_publication_rolls_back_existing_artifact(self):
        source = self.write("compile/new.exe", b"MZnew")
        destination = self.write("new output/agent-nexus.exe", b"MZold")
        original_replace = Path.replace

        def fail_publication(path, target):
            if Path(target) == destination:
                raise PermissionError("fixture publish failure")
            return original_replace(path, target)

        with patch.object(Path, "replace", fail_publication):
            with self.assertRaises(PermissionError):
                desktop.publish(source, destination)
        self.assertEqual(destination.read_bytes(), b"MZold")
        self.assertEqual(source.read_bytes(), b"MZnew")
        self.assertEqual(list(self.out.iterdir()), [destination])

    def test_locked_executable_is_preserved_without_stopping_processes(self):
        source = self.write("compile/new.exe", b"MZnew")
        destination = self.write("new output/agent-nexus.exe", b"MZold")
        original_replace = Path.replace
        attempts = 0

        def locked_once(path, target):
            nonlocal attempts
            if Path(target) == destination:
                attempts += 1
                if attempts == 1:
                    raise PermissionError("fixture executable in use")
            return original_replace(path, target)

        with patch.object(Path, "replace", locked_once), patch.object(desktop.subprocess, "run") as process:
            desktop.publish(source, destination)
        process.assert_not_called()
        self.assertEqual(destination.read_bytes(), b"MZnew")
        previous = list(self.out.glob("agent-nexus.exe.previous-*"))
        self.assertEqual(len(previous), 1)
        self.assertEqual(previous[0].read_bytes(), b"MZold")
        self.assertFalse(list(self.out.glob("*.build-*")))

    def test_ubuntu_packages_do_not_need_windows_uv_and_keep_credentials_separate(self):
        linux_uv = self.write("tools/linux uv", b"\x7fELF" + b"\x00" * 14 + b"\x3e\x00" + b"fixture")
        with patch.object(release.shutil, "which", side_effect=AssertionError("Windows uv must not be requested")):
            ubuntu.build(linux_uv, self.out)
            ubuntu.build(linux_uv, self.out, self.profile)
        for name, private in (("agent-nexus", False), ("agent-nexus-private", True)):
            with tarfile.open(self.out / f"{name}_{ubuntu.VERSION}_linux-amd64.tar.xz") as archive:
                files = {item.name: archive.extractfile(item).read() for item in archive if item.isfile()}
            self.assertEqual("runtime/.private/enrollment.json" in files, private)
            if private:
                self.assertEqual(files["runtime/.private/enrollment.json"], self.profile.read_bytes())
            else:
                self.assertFalse(any(self.key.encode() in data for data in files.values()))
            self.assertEqual(files["runtime/tools/uv"], linux_uv.read_bytes())
            self.assertNotIn("runtime/tools/uv.exe", files)
            self.assertEqual(files["ubuntu_launcher.py"], (self.root / "packaging/ubuntu/ubuntu_launcher.py").read_bytes())
        self.assertNotIn(self.key, self.output.getvalue())
        # scripts/verify_ubuntu.py finds both packages by the source version and accepts them
        verify = load_script("verify_ubuntu")
        self.assertEqual(verify.VERSION, ubuntu.VERSION)
        self.assertEqual([p.name for p in verify.packages(self.out)],
                         [f"agent-nexus-private_{ubuntu.VERSION}_amd64.deb", f"agent-nexus_{ubuntu.VERSION}_amd64.deb"])
        self.assertEqual(verify.main([str(self.out)]), 0)
        self.assertEqual(verify.main([str(self.root / "empty")]), 1)


    def test_runtime_builds_without_optional_business_plugins(self):
        import shutil
        for name in release.bundle.OPTIONAL_BUILTINS:
            shutil.rmtree(self.root / "builtins" / name)
        release.build("runtime", self.out, uv=self.uv)
        files = self.zip_files(self.out / "agent-nexus-runtime.zip")
        bundled = {Path(name).parts[1] for name in files if name.startswith("builtins/")}
        self.assertEqual(bundled, set(release.bundle.REQUIRED_BUILTINS))
        with patch.dict(os.environ, {"ANX_REQUIRE_ALL_BUILTINS": "1"}):
            with self.assertRaisesRegex(FileNotFoundError, "missing builtin"):
                release.build("runtime", self.out / "strict", uv=self.uv)
        shutil.rmtree(self.root / "builtins" / "device")
        with self.assertRaisesRegex(FileNotFoundError, "device"):
            release.build("runtime", self.out / "core", uv=self.uv)

    def test_private_runtime_pins_endpoint_from_profile_not_source(self):
        release.build("runtime", self.out / "private", self.profile, uv=self.uv)
        private = self.zip_files(self.out / "private/agent-nexus-runtime.zip")
        self.assertEqual(sorted(name for name in private if name.startswith(".private/")),
                         [".private/endpoint-allow", ".private/enrollment.json"])
        self.assertEqual(private[".private/endpoint-allow"], b"wss://hub.example.test\n")
        manifest = json.loads(private["SHA256.json"])
        self.assertIn(".private/endpoint-allow", manifest)
        with patch.dict(os.environ, {"ANX_ENROLLMENT_ENDPOINT": "wss://other.example.test"}):
            with self.assertRaisesRegex(ValueError, "Invalid private enrollment profile"):
                release.load_enrollment(self.profile)
        with patch.dict(os.environ, {"ANX_ENROLLMENT_ENDPOINT": "https://hub.example.test/"}):
            self.assertEqual(release.load_enrollment(self.profile), self.profile.read_bytes())

    def test_private_builds_require_an_independent_endpoint(self):
        os.environ.pop("ANX_ENROLLMENT_ENDPOINT")
        for build in (lambda: release.load_enrollment(self.profile),
                      lambda: release.build("runtime", self.out / "private", self.profile, uv=self.uv)):
            with self.assertRaisesRegex(ValueError, "require ANX_ENROLLMENT_ENDPOINT") as error:
                build()
            self.assertNotIn(self.key, str(error.exception))
        self.assertFalse((self.out / "private" / "agent-nexus-runtime.zip").exists())
        with patch.dict(os.environ, {"MCPM_ENROLLMENT_ENDPOINT": "wss://hub.example.test"}):
            self.assertEqual(release.load_enrollment(self.profile), self.profile.read_bytes())
        with patch.dict(os.environ, {"ANX_ENROLLMENT_ENDPOINT": "ws://hub.example.test"}):
            with self.assertRaisesRegex(ValueError, "must be a wss:// or https://"):
                release.load_enrollment(self.profile)
        # a public build needs neither the profile nor the endpoint
        release.build("runtime", self.out / "public", uv=self.uv)

    def test_makefile_private_and_full_targets_are_strict(self):
        makefile = (SOURCE / "Makefile").read_text(encoding="utf-8")
        for target in ("private-desktop", "ubuntu-cloud", "check-client-inputs", "build"):
            self.assertRegex(makefile, rf"(?m)^[^\n#]*\b{target}\b[^\n]*: export ANX_REQUIRE_ALL_BUILTINS = 1$")
        self.assertNotRegex(makefile, r"(?m)^(runtime|ubuntu|desktop|python|server)\b[^\n]*: export ANX_REQUIRE_ALL_BUILTINS")


class RipgrepBundleTests(unittest.TestCase):
    def test_a_chocolatey_shim_resolves_to_the_real_binary(self):
        with tempfile.TemporaryDirectory() as scratch:
            base = Path(scratch) / "chocolatey"
            shim = base / "bin" / "rg.exe"
            real = base / "lib" / "ripgrep" / "tools" / "ripgrep-14.1.0-x86_64-pc-windows-msvc" / "rg.exe"
            for path, data in ((shim, b"MZshim"), (real, b"MZreal")):
                path.parent.mkdir(parents=True)
                path.write_bytes(data)
            self.assertEqual(release.find_ripgrep(str(shim)), real)
            plain = Path(scratch) / "tools" / "rg.exe"
            plain.parent.mkdir()
            plain.write_bytes(b"MZ")
            self.assertEqual(release.find_ripgrep(str(plain)), plain)
            self.assertIsNone(release.find_ripgrep(str(Path(scratch) / "missing.exe")))

    def test_windows_runtime_ships_rg_next_to_uv(self):
        with tempfile.TemporaryDirectory() as scratch:
            rg = Path(scratch) / "rg.exe"
            rg.write_bytes(b"MZrg")
            uv = Path(scratch) / "uv.exe"
            uv.write_bytes(b"MZuv")
            uv.chmod(0o755)
            files = dict((name, path) for path, name in release.release_files("runtime", uv=str(uv), rg=str(rg)))
            self.assertEqual(files["tools/rg.exe"], rg)
            self.assertEqual(files["tools/uv.exe"], uv)


if __name__ == "__main__":
    unittest.main()
