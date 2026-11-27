"""Build the Android app (APK): the Agent Nexus core on the official CPython Android build.

    uv run --no-project python scripts/build_android.py wheels   # native packages, once per lock change
    uv run --no-project python scripts/build_android.py apk --out dist

Needs a Linux or macOS host with ANDROID_HOME (platform 36, build-tools, NDK ``NDK_VERSION``),
JDK 17, Gradle (``GRADLE`` or on PATH), uv, and for ``wheels`` rustup with the
``aarch64-linux-android`` target and cibuildwheel (``uv tool run``).

The app (android/) runs ``python -m agent_nexus.cli serve`` as a foreground service and shows
the console in a WebView. What it carries:

* ``jniLibs/arm64-v8a``: libpython and its OpenSSL/SQLite from the python.org Android release,
  and ``libpyexe.so``, a ``python`` executable built here (Android lets an app execute files only
  from its native library folder; plugins run as child processes of it).
* ``assets/runtime.zip``: the standard library (``python/``), the manager's packages (``site/``)
  and the release files (``app/``, as in the desktop runtime) with each Python builtin's own
  packages in ``app/builtins/<name>/.site`` (agent_nexus/android.py starts plugins with them).

PyPI has no Android wheels for the compiled dependencies, so ``wheels`` cross-compiles them
(cibuildwheel ``--platform android``) and builds pure wheels of those whose C speedups are
optional. Remote desktop (av, mss) and the tray (pystray, pillow) are not part of the app.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

PYTHON_VERSION = "3.14.10"  # the build CIBUILDWHEEL uses for cp314 (one download)
PYTHON_SHA256 = "423c80b957330cf0ebb92b9b20f2294c292144a441bdb0ceb8c69d14928e569b"
PYTHON_TGZ = f"python-{PYTHON_VERSION}-aarch64-linux-android.tar.gz"
#: ANX_ANDROID_PYTHON_MIRROR replaces https://www.python.org/ftp/python (the SHA-256 pin still applies).
PYTHON_URL = f"{os.environ.get('ANX_ANDROID_PYTHON_MIRROR') or 'https://www.python.org/ftp/python'}/{PYTHON_VERSION}/{PYTHON_TGZ}"
CIBUILDWHEEL = "cibuildwheel==4.3.5"
NDK_VERSION = "27.3.13750724"
API_LEVEL = 24  # the CPython build's minimum; the app itself needs 29
ABI = "arm64-v8a"
TRIPLET = "aarch64-linux-android"
BUILD = ROOT / "build" / "android"
WHEELS = BUILD / "wheels"
ANDROID = ROOT / "android"
#: Not part of the app: remote desktop video and capture, the desktop tray, platform-only packages.
EXCLUDED = {"av", "mss", "pystray", "pillow", "pyobjc-core", "pyobjc-framework-cocoa", "pyobjc-framework-quartz",
            "python-xlib", "pywinpty"}
#: Compiled packages cross-compiled with cibuildwheel.
NATIVE = {"pydantic-core", "rpds-py", "watchfiles", "cryptography", "psutil", "cffi"}
#: cffi needs libffi, which CPython's Android build keeps inside _ctypes: built here, static.
LIBFFI_VERSION = "3.8.1"
LIBFFI_SHA256 = "81e879c094cc5ec3186c48510c6385ac6b3e866192e8bc71a35245703757ee40"
LIBFFI_URL = f"https://github.com/libffi/libffi/releases/download/v{LIBFFI_VERSION}/libffi-{LIBFFI_VERSION}.tar.gz"
#: patchelf (PyPI) records libpython as a dependency of extension modules built without it.
PATCHELF = "patchelf==0.17.2.4"
#: Packages whose C extensions are optional: built as pure wheels.
PURE = {"pyyaml", "caio"}
#: Python projects that make up the app: the manager and each Python builtin that runs with `uv run`.
PROJECTS = {"manager": ROOT, "device": ROOT / "builtins" / "device"}
#: Local (path) packages installed into the sites alongside the locked ones.
LOCAL = {"manager": [ROOT / "builtins" / "mcpkit", ROOT / "third_party" / "muxi-p2p" / "signaling"],
         "device": [ROOT / "builtins" / "mcpkit"]}
#: Standard library parts the app never uses.
STDLIB_DROP = {"test", "idlelib", "tkinter", "turtledemo", "ensurepip", "lib2to3", "pydoc_data", "__phello__"}


def run(command, **kwargs):
    print("+", " ".join(str(part) for part in command), flush=True)
    return subprocess.run([str(part) for part in command], check=True, **kwargs)


def uv() -> str:
    return shutil.which("uv") or sys.exit("uv is required")


def source_version() -> str:
    text = (ROOT / "agent_nexus" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r"""^__version__\s*=\s*["']([^"']+)["']""", text, re.M).group(1)


def version_code(version: str) -> int:
    """0.8.13 -> 8013 (Android needs a growing integer); a pre-release stays below its release."""
    main, _, pre = version.partition("-")
    major, minor, patch = (int(part) for part in main.split("."))
    return ((major * 100 + minor) * 1000 + patch) * 10 + (0 if pre else 9)


def android_home() -> Path:
    home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not home:
        sys.exit("ANDROID_HOME is not set")
    return Path(home)


def ndk_bin() -> Path:
    host = "darwin-x86_64" if sys.platform == "darwin" else "linux-x86_64"
    path = android_home() / "ndk" / NDK_VERSION / "toolchains" / "llvm" / "prebuilt" / host / "bin"
    if not path.is_dir():
        sys.exit(f"NDK {NDK_VERSION} is missing: sdkmanager 'ndk;{NDK_VERSION}'")
    return path


def python_prefix() -> Path:
    """The python.org Android build, downloaded once and checked against its pinned SHA-256."""
    target = BUILD / f"python-{PYTHON_VERSION}"
    if not (target / "prefix" / "lib" / "libpython3.14.so").is_file():
        with urllib.request.urlopen(PYTHON_URL, timeout=300) as response:
            data = response.read()
        digest = hashlib.sha256(data).hexdigest()
        if digest != PYTHON_SHA256:
            sys.exit(f"{PYTHON_URL}: sha256 {digest} != pinned {PYTHON_SHA256}")
        # cibuildwheel looks for the same archive in its cache before downloading it.
        cache = Path(os.environ.get("CIBW_CACHE_PATH") or Path.home() / (
            "Library/Caches/cibuildwheel" if sys.platform == "darwin" else ".cache/cibuildwheel"))
        cache.mkdir(parents=True, exist_ok=True)
        (cache / PYTHON_TGZ).write_bytes(data)
        shutil.rmtree(target, ignore_errors=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            archive.extractall(target, filter="data")
    return target / "prefix"


def locked(project: Path) -> dict[str, str]:
    import tomllib
    data = tomllib.loads((project / "uv.lock").read_text(encoding="utf-8"))
    return {package["name"]: package["version"] for package in data["package"] if "version" in package}


def requirements(project: Path) -> list[str]:
    """The project's locked runtime requirements for Android, without the excluded packages."""
    text = subprocess.run([uv(), "export", "--frozen", "--no-dev", "--no-emit-project", "--no-emit-local",
                           "--no-hashes", "--quiet"], cwd=project, check=True, capture_output=True, text=True).stdout
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-e", "./", "../")):
            continue
        name = re.split(r"[=<>;\[ ]", line, maxsplit=1)[0].lower()
        if name not in EXCLUDED:
            lines.append(line)
    return lines


# ---- wheels --------------------------------------------------------------------------------------

def sdist(name: str, version: str, scratch: Path) -> Path:
    """The source archive straight from the index (PIP_INDEX_URL, else PyPI): ``pip download``
    would run the build backend to read metadata, which for a Rust package means compiling it."""
    import html
    import urllib.parse
    index = (os.environ.get("PIP_INDEX_URL") or "https://pypi.org/simple").rstrip("/")
    normalized = re.sub(r"[-_.]+", "-", name).lower()
    page_url = f"{index}/{normalized}/"
    with urllib.request.urlopen(page_url, timeout=120) as response:
        page = response.read().decode()
    stem = re.sub(r"[-_.]+", "[-_.]", normalized)
    links = [html.unescape(link) for link in re.findall(r'href="([^"]+)"', page)]
    found = [link for link in links if re.search(rf"/{stem}-{re.escape(version)}\.tar\.gz(#|$)", link, re.I)]
    if not found:
        sys.exit(f"no source archive for {name} {version} at {page_url}")
    url, _, fragment = urllib.parse.urljoin(page_url, found[0]).partition("#")
    with urllib.request.urlopen(url, timeout=300) as response:
        data = response.read()
    expected = dict(part.split("=", 1) for part in fragment.split("&") if "=" in part).get("sha256")
    if expected and hashlib.sha256(data).hexdigest() != expected:
        sys.exit(f"{url}: sha256 mismatch")
    scratch.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(scratch, filter="data")
    return next(path for path in scratch.iterdir() if path.is_dir())


def patch(name: str, source: Path) -> None:
    """psutil only knows sys.platform "linux"; Android (sys.platform "android") is Linux to it."""
    if name == "psutil":
        common = source / "psutil" / "_common.py"
        text = common.read_text(encoding="utf-8")
        text = text.replace('LINUX = sys.platform.startswith("linux")',
                            'LINUX = sys.platform.startswith(("linux", "android"))')
        common.write_text(text, encoding="utf-8")
    if name == "cffi":
        # cibuildwheel sets CFLAGS/LDFLAGS for Android after the configured environment, so the
        # static libffi is named in setup.py itself.
        ffi = libffi()
        setup = source / "setup.py"
        text = setup.read_text(encoding="utf-8")
        text = text.replace("include_dirs = ['/usr/include/ffi',", f"include_dirs = [{str(ffi / 'include')!r}, '/usr/include/ffi',", 1)
        text = text.replace("library_dirs = []", f"library_dirs = [{str(ffi / 'lib')!r}]", 1)
        setup.write_text(text, encoding="utf-8")


def libffi() -> Path:
    """A static, position-independent libffi for Android (``<prefix>/include``, ``<prefix>/lib/libffi.a``).
    The source archive may be placed in build/android beforehand (its SHA-256 is checked either way)."""
    target = BUILD / f"libffi-{LIBFFI_VERSION}-android"
    if (target / "lib" / "libffi.a").is_file():
        return target
    archive = BUILD / f"libffi-{LIBFFI_VERSION}.tar.gz"
    if not archive.is_file():
        with urllib.request.urlopen(LIBFFI_URL, timeout=300) as response:
            archive.write_bytes(response.read())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != LIBFFI_SHA256:
        archive.unlink()
        sys.exit(f"{archive.name}: sha256 mismatch")
    with tempfile.TemporaryDirectory(prefix="anx-libffi-") as scratch:
        with tarfile.open(archive) as tar:
            tar.extractall(scratch, filter="data")
        source = Path(scratch) / f"libffi-{LIBFFI_VERSION}"
        tools = ndk_bin()
        env = dict(os.environ, CC=str(tools / f"{TRIPLET}{API_LEVEL}-clang"), AR=str(tools / "llvm-ar"),
                   RANLIB=str(tools / "llvm-ranlib"), CFLAGS="-O2 -fPIC")
        run(["./configure", f"--host={TRIPLET}", f"--prefix={target}", "--disable-shared", "--enable-static",
             "--with-pic", "--disable-docs", "--disable-multi-os-directory"], cwd=source, env=env)
        run(["make", f"-j{os.cpu_count() or 2}", "install"], cwd=source, env=env)
    return target


def cibuildwheel(name: str, source: Path, prefix: Path) -> None:
    env = dict(os.environ, CIBW_BUILD="cp314-*", CIBW_ARCHS="arm64_v8a", CIBW_TEST_SKIP="*")
    if name == "cryptography":
        # Link the OpenSSL that ships with CPython's Android build (renamed *_python.so).
        include = prefix / "include"
        env["CIBW_ENVIRONMENT"] = (
            f"OPENSSL_LIB_DIR={prefix / 'lib'} OPENSSL_INCLUDE_DIR={include} OPENSSL_LIBS=ssl_python:crypto_python "
            f"CFLAGS_aarch64_linux_android='-D__BIONIC_NO_PAGE_SIZE_MACRO -I{include} -I{include / 'python3.14'} -DNDEBUG -O3'")
    if "CIBW_ENVIRONMENT" in env:
        # A project's own [tool.cibuildwheel.android] environment (cffi has one) beats the generic variable.
        env["CIBW_ENVIRONMENT_ANDROID"] = env["CIBW_ENVIRONMENT"]
    run([uv(), "tool", "run", "--from", CIBUILDWHEEL, "cibuildwheel", "--platform", "android", "--output-dir", WHEELS, source], env=env)


def pure_wheel(spec: str, scratch: Path) -> None:
    """A wheel without its optional C extension, tagged py3-none-any."""
    env = dict(os.environ, PYYAML_FORCE_LIBYAML="0", BUILD_EXTENSION="no")
    run([uv(), "run", "--no-project", "--python", "3.14", "--with", "pip,setuptools,wheel", "pip", "wheel",
         "--no-deps", "--no-binary", ":all:", "-q", "-w", scratch, spec], env=env)
    built = next(scratch.glob("*.whl"))
    unpacked = scratch / "unpacked"
    with zipfile.ZipFile(built) as archive:
        archive.extractall(unpacked)
    for library in [*unpacked.rglob("*.so"), *unpacked.rglob("*.pyd")]:
        library.unlink()
    meta = next(unpacked.glob("*.dist-info")) / "WHEEL"
    lines = [line for line in meta.read_text(encoding="utf-8").splitlines()
             if line and not line.startswith(("Tag:", "Root-Is-Purelib:"))]
    meta.write_text("\n".join([*lines, "Root-Is-Purelib: true", "Tag: py3-none-any"]) + "\n", encoding="utf-8")
    run([uv(), "run", "--no-project", "--with", "wheel", "python", "-m", "wheel", "pack", unpacked, "-d", WHEELS])


def have_wheel(name: str, version: str) -> bool:
    stem = re.sub(r"[-_.]+", "_", name).lower()
    return any(path.name.lower().startswith(f"{stem}-{version}-") for path in WHEELS.glob("*.whl"))


def build_wheels() -> None:
    WHEELS.mkdir(parents=True, exist_ok=True)
    prefix = python_prefix()
    wanted = {}
    for project in PROJECTS.values():
        for name, version in locked(project).items():
            if name in NATIVE | PURE:
                wanted.setdefault((name, version), None)
    for name, version in sorted(wanted):
        if have_wheel(name, version):
            print(f"{name} {version}: wheel present")
            continue
        with tempfile.TemporaryDirectory(prefix=f"anx-android-{name}-") as scratch:
            scratch = Path(scratch)
            if name in PURE:
                pure_wheel(f"{name}=={version}", scratch)
                continue
            source = sdist(name, version, scratch / "sdist")
            patch(name, source)
            cibuildwheel(name, source, prefix)


# ---- apk -----------------------------------------------------------------------------------------

def launcher(prefix: Path, out: Path) -> None:
    """``libpyexe.so``: ``Py_BytesMain`` linked against libpython (found next to it)."""
    source = out.parent / "pyexe.c"
    source.write_text('#include <Python.h>\nint main(int argc, char **argv) { return Py_BytesMain(argc, argv); }\n')
    run([ndk_bin() / f"{TRIPLET}{API_LEVEL}-clang", "-O2", source, f"-I{prefix / 'include' / 'python3.14'}",
         f"-L{prefix / 'lib'}", "-lpython3.14", "-Wl,-rpath,$ORIGIN", "-Wl,-z,max-page-size=16384", "-o", out])
    source.unlink()


def install_site(name: str, target: Path) -> None:
    project = PROJECTS[name]
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        handle.write("\n".join(requirements(project)) + "\n")
    try:
        common = ["--target", target, "--python-platform", "aarch64-linux-android", "--python-version", "3.14",
                  "--no-deps", "--quiet"]
        run([uv(), "pip", "install", *common, "--only-binary", ":all:", "--find-links", WHEELS, "-r", handle.name])
        run([uv(), "pip", "install", *common, *LOCAL[name]])
    finally:
        os.unlink(handle.name)
    for cache in target.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    link_libpython(target)


def link_libpython(target: Path) -> None:
    """Make every extension module depend on libpython. Android's linker resolves a dlopen()ed
    library's symbols through its own dependencies (plus the executable itself, not the executable's
    libraries), and stable-ABI Rust builds (cryptography, watchfiles) are linked without libpython."""
    patchelf = [uv(), "tool", "run", "--from", PATCHELF, "patchelf"]
    for module in sorted(target.rglob("*.so")):
        needed = subprocess.run([*patchelf, "--print-needed", module], check=True, capture_output=True,
                                text=True).stdout.split()
        if not any(name.startswith("libpython3") for name in needed):
            run([*patchelf, "--add-needed", "libpython3.14.so", module])


def client_config() -> bytes:
    config = json.loads((ROOT / "config" / "client.example.json").read_bytes())
    config["cloud"]["enabled"] = False
    config["services"]["device"]["settings"] = {"bash_path": "/system/bin/sh"}
    return (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode()


def runtime_zip(prefix: Path, out: Path) -> str:
    """``assets/runtime.zip``; returns its tag (version and content hash)."""
    import build_release
    with tempfile.TemporaryDirectory(prefix="anx-android-runtime-") as scratch:
        stage = Path(scratch)
        install_site("manager", stage / "site")
        install_site("device", stage / "app" / "builtins" / "device" / ".site")
        files = list(build_release.release_files("runtime", include_windows_uv=False))
        digest = hashlib.sha256()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            def add(name: str, data: bytes) -> None:
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
                digest.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
            stdlib = prefix / "lib" / "python3.14"
            for path in sorted(stdlib.rglob("*")):
                relative = path.relative_to(stdlib)
                if path.is_file() and relative.parts[0] not in STDLIB_DROP and "__pycache__" not in relative.parts \
                        and path.suffix != ".pyc":
                    add(f"python/lib/python3.14/{relative.as_posix()}", path.read_bytes())
            for folder, base in ((stage / "site", "site"), (stage / "app", "app")):
                for path in sorted(folder.rglob("*")):
                    if path.is_file():
                        add(f"{base}/{path.relative_to(folder).as_posix()}", path.read_bytes())
            for source, relative in files:
                add(f"app/{relative}", source.read_bytes())
            add(f"app/{build_release.CLIENT_CONFIG}", client_config())
    return f"{source_version()}-{digest.hexdigest()[:12]}"


def gradle() -> str:
    return os.environ.get("GRADLE") or shutil.which("gradle") or sys.exit("Gradle is required (GRADLE or on PATH)")


def build_apk(out: Path) -> Path:
    prefix = python_prefix()
    version = source_version()
    main = ANDROID / "app" / "src" / "main"
    assets, libs, drawable = main / "assets", main / "jniLibs" / ABI, main / "res" / "drawable"
    for folder in (assets, libs, drawable):
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
    for name in ("libpython3.14.so", "libssl_python.so", "libcrypto_python.so", "libsqlite3_python.so"):
        shutil.copy2(prefix / "lib" / name, libs / name)
    launcher(prefix, libs / "libpyexe.so")
    # The P2P tunnel tools (libmuxip2p.so, the agent; libmsquic.so), built for linux-bionic-arm64
    # by the muxi-p2p build; agent_nexus/p2p.py finds them through ANX_ANDROID_NATIVE_LIBS.
    p2p = os.environ.get("ANX_ANDROID_P2P_TOOLS")
    if p2p:
        for library in sorted(Path(p2p).glob("lib*.so")):
            shutil.copy2(library, libs / library.name)
            print(f"p2p: {library.name} {hashlib.sha256(library.read_bytes()).hexdigest()[:16]}")
    else:
        print("warning: ANX_ANDROID_P2P_TOOLS is not set; the app has no P2P (gateway relay only)", flush=True)
    shutil.copy2(ROOT / "desktop" / "src-tauri" / "icons" / "128x128.png", drawable / "ic_launcher.png")
    tag = runtime_zip(prefix, assets / "runtime.zip")
    (assets / "runtime.tag").write_text(tag + "\n", encoding="utf-8")
    run([gradle(), "--no-daemon", "-p", ANDROID, ":app:assembleRelease", f"-PanxVersion={version}",
         f"-PanxVersionCode={version_code(version)}"])
    built = ANDROID / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk"
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"agent-nexus_{version}_arm64.apk"
    shutil.copy2(built, target)
    print(f"artifact: {target} ({target.stat().st_size // (1024 * 1024)} MiB, runtime {tag})")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("wheels", help=f"cross-compile the native packages into {WHEELS.relative_to(ROOT)}")
    apk = sub.add_parser("apk", help="build the APK")
    apk.add_argument("--out", type=Path, default=ROOT / "artifacts")
    args = parser.parse_args()
    if args.command == "wheels":
        build_wheels()
    else:
        build_apk(args.out.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
