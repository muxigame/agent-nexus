"""Build the P2P tunnel tools the client packages carry in ``tools/``.

    uv run --no-project python scripts/build_p2p_tools.py --rid win-x64 --out build/p2p
    uv run --no-project python scripts/build_p2p_tools.py --rid linux-x64 --out build/p2p
    uv run --no-project python scripts/build_p2p_tools.py --rid osx-arm64 --out build/p2p

* ``muxi-p2p-agent[.exe]``: published from the submodule third_party/muxi-p2p/agent
  (self-contained, single file; needs the .NET 9 SDK).
* MsQuic next to it, so QUIC hole punching works where the OS has none (Windows 10, Server 2019,
  Ubuntu without Microsoft's repository): ``msquic.dll`` from the NuGet package
  Microsoft.Native.Quic.MsQuic.OpenSSL, ``libmsquic.so.2`` from Microsoft's Ubuntu package.

  The OpenSSL build, not the SChannel one: SChannel's TLS 1.3 only exists from Windows build
  20145 (Windows 11 / Server 2022), and .NET refuses QUIC below that with an SChannel msquic
  ("Current Windows version (10.0.19042) is not supported by QUIC") -- every Windows 10 fell back
  to TCP punching. The OpenSSL build carries its own TLS, so .NET does not check the version
  (MC shipped this build before the split; verified on Windows 10 20H2).
  Both downloads are pinned by version and SHA-256. MIT, see ``LICENSE-msquic.txt``.

* macOS (``osx-arm64``, ``osx-x64``, built on a Mac of that architecture): Microsoft publishes no
  MsQuic for macOS, and .NET's QUIC client also needs OpenSSL there (its OpenSSL shim checks the
  peer certificate). Both are built here from pinned sources: ``libmsquic.dylib`` (MsQuic with its
  bundled quictls, linked statically) and ``libssl.3.dylib`` / ``libcrypto.3.dylib`` (OpenSSL LTS),
  with ``@loader_path`` install names and an ad-hoc signature. agent_nexus/p2p.py puts the folder
  on ``DYLD_LIBRARY_PATH`` for the agent. Needs cmake and perl. Apache-2.0, ``LICENSE-openssl.txt``.

The build scripts take the folder from ``ANX_P2P_TOOLS``.

Android (``--rid linux-bionic-arm64``, run on Linux x64; the APK build takes the folder from
``ANX_ANDROID_P2P_TOOLS``):

* ``libmuxip2p.so``: the agent as a NativeAOT executable for bionic, linked with the NDK's clang.
  Android only executes files from the app's ``nativeLibraryDir``, and only ``lib*.so`` names are
  extracted there, hence the name. ``InvariantGlobalization``: bionic has no ICU for .NET.
* ``libmsquic.so``: msquic built from its pinned tag with quictls linked in statically (needs only
  libc/libm/libdl), for API 29 (msquic uses ``glob``, which bionic has from 28).

Needs the .NET 9 SDK, the NDK (``ANDROID_NDK_ROOT``, else ``$ANDROID_HOME/ndk/27.3.13750724``),
``git``, ``cmake``, ``make`` and ``perl`` (all on GitHub's ubuntu-latest). At run time .NET's
OpenSSL shim opens ``libssl.so`` / ``libcrypto.so`` by those exact names (not ``.so.3``), so the app
links them to its own OpenSSL in a folder on ``LD_LIBRARY_PATH`` (see agent_nexus.p2p.child_env).
``libmuxip2p.so selftest`` checks the lot on a device without any credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import lzma
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "third_party/muxi-p2p/agent/src/MuxiP2P.Agent/MuxiP2P.Agent.csproj"
MSQUIC_VERSION = "2.6.4"
DOWNLOADS = {
    "win-x64": (f"https://api.nuget.org/v3-flatcontainer/microsoft.native.quic.msquic.openssl/{MSQUIC_VERSION}/"
                f"microsoft.native.quic.msquic.openssl.{MSQUIC_VERSION}.nupkg",
                "370baf73845ac516a7acb53394ebf889ca947f7826a0e74f8f8c332b3627adb5"),
    "linux-x64": (f"https://packages.microsoft.com/ubuntu/22.04/prod/pool/main/libm/libmsquic/libmsquic_{MSQUIC_VERSION}_amd64.deb",
                  "c81352050683672eca2a7794c8129da73caca1556b1ce1ff2b7784dd24897227"),
}


def download(url: str, sha256: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != sha256:
        raise SystemExit(f"{url}: sha256 {digest} != pinned {sha256}")
    return data


def ar_members(data: bytes) -> dict[str, bytes]:
    if not data.startswith(b"!<arch>\n"):
        raise SystemExit("not a .deb (ar) archive")
    members, offset = {}, 8
    while offset + 60 <= len(data):
        header = data[offset:offset + 60]
        name = header[:16].decode().strip().rstrip("/")
        size = int(header[48:58].decode().strip())
        members[name] = data[offset + 60:offset + 60 + size]
        offset += 60 + size + (size % 2)
    return members


def msquic(rid: str) -> tuple[str, bytes, bytes]:
    url, sha = DOWNLOADS[rid]
    data = download(url, sha)
    if rid == "win-x64":
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            return "msquic.dll", package.read("build/native/bin/x64/msquic.dll"), package.read("LICENSE")
    members = ar_members(data)
    payload = next(v for k, v in members.items() if k.startswith("data.tar"))
    with tarfile.open(fileobj=io.BytesIO(lzma.decompress(payload) if payload[:6] == b"\xfd7zXZ\x00" else payload)) as tar:
        library = next(m for m in tar.getmembers() if m.name.endswith(f"libmsquic.so.{MSQUIC_VERSION}"))
        return "libmsquic.so.2", tar.extractfile(library).read(), b"MIT License\n\nCopyright (c) Microsoft Corporation.\n"


#: macOS runtimes: MsQuic and OpenSSL are built from source (see the module docstring).
MACOS = {"osx-arm64": "arm64", "osx-x64": "x86_64"}
OPENSSL_VERSION = "3.5.9"
OPENSSL_URL = f"https://github.com/openssl/openssl/releases/download/openssl-{OPENSSL_VERSION}/openssl-{OPENSSL_VERSION}.tar.gz"
OPENSSL_SHA256 = "603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a"
MACOS_MINIMUM = "11.0"


def run(command: list, **kwargs) -> None:
    print("+", " ".join(str(part) for part in command), flush=True)
    subprocess.run([str(part) for part in command], check=True, **kwargs)


def relocatable(library: Path, names: dict[str, str]) -> None:
    """``@loader_path`` install name and dependencies, then an ad-hoc signature (install_name_tool
    invalidates the old one, and arm64 refuses to load unsigned code)."""
    run(["install_name_tool", "-id", f"@loader_path/{library.name}", library])
    for old, new in names.items():
        run(["install_name_tool", "-change", old, f"@loader_path/{new}", library])
    run(["codesign", "--force", "--sign", "-", library])


def macos_libraries(rid: str, out: Path, scratch: Path) -> None:
    """``libmsquic.dylib``, ``libssl.3.dylib`` and ``libcrypto.3.dylib`` for ``rid`` into ``out``."""
    arch = MACOS[rid]
    env = dict(os.environ, MACOSX_DEPLOYMENT_TARGET=MACOS_MINIMUM)
    jobs = str(os.cpu_count() or 4)
    # OpenSSL (the .NET shim dlopen()s libssl.3.dylib / libcrypto.3.dylib by name).
    source = scratch / "openssl"
    prefix = scratch / "openssl-install"
    with tarfile.open(fileobj=io.BytesIO(download(OPENSSL_URL, OPENSSL_SHA256))) as tar:
        tar.extractall(scratch, filter="data")
    (scratch / f"openssl-{OPENSSL_VERSION}").rename(source)
    run(["perl", "./Configure", f"darwin64-{arch}-cc", "shared", "no-tests", f"--prefix={prefix}",
         f"-mmacosx-version-min={MACOS_MINIMUM}"], cwd=source, env=env)
    run(["make", f"-j{jobs}"], cwd=source, env=env, stdout=subprocess.DEVNULL)
    run(["make", "install_sw"], cwd=source, env=env, stdout=subprocess.DEVNULL)
    for name in ("libcrypto.3.dylib", "libssl.3.dylib"):
        shutil.copy2(prefix / "lib" / name, out / name)
    relocatable(out / "libcrypto.3.dylib", {})
    relocatable(out / "libssl.3.dylib", {str(prefix / "lib" / "libcrypto.3.dylib"): "libcrypto.3.dylib"})
    (out / "LICENSE-openssl.txt").write_bytes(f"OpenSSL {OPENSSL_VERSION} (https://www.openssl.org)\n\n".encode()
                                              + (source / "LICENSE.txt").read_bytes())
    # MsQuic with its bundled quictls (static): depends on system frameworks only.
    quic = scratch / "msquic"
    run(["git", "clone", "--quiet", "--depth", "1", "--branch", f"v{MSQUIC_VERSION}", MSQUIC_REPO, quic])
    run(["git", "submodule", "update", "--init", "--depth", "1", "submodules/quictls"], cwd=quic)
    build = scratch / "msquic-build"
    run(["cmake", "-S", quic, "-B", build, "-DCMAKE_BUILD_TYPE=Release", "-DQUIC_TLS_LIB=quictls",
         "-DQUIC_BUILD_SHARED=ON", "-DQUIC_BUILD_TEST=OFF", "-DQUIC_BUILD_TOOLS=OFF", "-DQUIC_BUILD_PERF=OFF",
         f"-DCMAKE_OSX_ARCHITECTURES={arch}", f"-DCMAKE_OSX_DEPLOYMENT_TARGET={MACOS_MINIMUM}"], env=env)
    run(["cmake", "--build", build, "-j", jobs], env=env, stdout=subprocess.DEVNULL)
    shutil.copy2(build / "bin" / "Release" / f"libmsquic.{MSQUIC_VERSION}.dylib", out / "libmsquic.dylib")
    relocatable(out / "libmsquic.dylib", {})
    (out / "LICENSE-msquic.txt").write_bytes(b"MsQuic " + MSQUIC_VERSION.encode() + b" (https://github.com/microsoft/msquic)\n\n"
                                             + (quic / "LICENSE").read_bytes())
    for library in ("libmsquic.dylib", "libssl.3.dylib", "libcrypto.3.dylib"):
        linked = subprocess.run(["otool", "-L", out / library], check=True, capture_output=True, text=True).stdout
        stray = [line.split()[0] for line in linked.splitlines()[1:]
                 if not line.strip().startswith(("/usr/lib/", "/System/", "@loader_path/"))]
        if stray:
            raise SystemExit(f"{library} still depends on {stray}")


ANDROID_RID = "linux-bionic-arm64"
NDK_VERSION = "27.3.13750724"
ANDROID_API = 29
MSQUIC_REPO = "https://github.com/microsoft/msquic.git"


def ndk_root() -> Path:
    explicit = os.environ.get("ANDROID_NDK_ROOT") or os.environ.get("ANDROID_NDK_HOME")
    candidates = [Path(explicit)] if explicit else []
    for sdk in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT")):
        if sdk:
            candidates.append(Path(sdk) / "ndk" / NDK_VERSION)
    for path in candidates:
        if (path / "build/cmake/android.toolchain.cmake").is_file():
            return path
    raise SystemExit(f"Android NDK {NDK_VERSION} not found: set ANDROID_NDK_ROOT or install it under $ANDROID_HOME/ndk/")


def build_android(dotnet: str, out: Path) -> None:
    ndk = ndk_root()
    llvm = ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin"
    env = {**os.environ, "ANDROID_NDK_ROOT": str(ndk), "PATH": f"{llvm}{os.pathsep}{os.environ['PATH']}"}
    for tool in ("git", "cmake", "make", "perl"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} is required for the Android build")
    out.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="anx-p2p-android-") as scratch:
        scratch = Path(scratch)
        subprocess.run([
            dotnet, "publish", str(PROJECT), "-c", "Release", "-r", ANDROID_RID, "-o", str(scratch / "agent"), "-nologo",
            "-p:PublishAot=true", "-p:PublishSingleFile=false", "-p:SelfContained=true",
            "-p:DisableUnsupportedError=true", "-p:PublishAotUsingRuntimePack=true",
            "-p:CppCompilerAndLinker=aarch64-linux-android24-clang", "-p:ObjCopyName=llvm-objcopy",
            "-p:StripSymbols=true", "-p:InvariantGlobalization=true",
        ], check=True, env=env)
        shutil.copy2(scratch / "agent/muxi-p2p-agent", out / "libmuxip2p.so")

        source = scratch / "msquic"
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", f"v{MSQUIC_VERSION}", MSQUIC_REPO, str(source)], check=True, env=env)
        subprocess.run(["git", "-C", str(source), "submodule", "update", "--init", "--depth", "1",
                        "submodules/quictls", "submodules/clog"], check=True, env=env)
        build = source / "build-android"
        subprocess.run([
            "cmake", "-S", str(source), "-B", str(build), "-G", "Unix Makefiles",
            f"-DCMAKE_TOOLCHAIN_FILE={ndk}/build/cmake/android.toolchain.cmake",
            "-DANDROID_ABI=arm64-v8a", f"-DANDROID_PLATFORM=android-{ANDROID_API}", "-DCMAKE_BUILD_TYPE=Release",
            "-DQUIC_TLS_LIB=quictls", "-DQUIC_BUILD_SHARED=on",
            "-DQUIC_BUILD_TEST=off", "-DQUIC_BUILD_TOOLS=off", "-DQUIC_BUILD_PERF=off",
        ], check=True, env=env)
        subprocess.run(["make", "-C", str(build), f"-j{os.cpu_count() or 4}", "msquic"], check=True, env=env)
        subprocess.run([str(llvm / "llvm-strip"), "-o", str(out / "libmsquic.so"),
                        str(build / "bin/Release/libmsquic.so")], check=True, env=env)
        license_text = (source / "LICENSE").read_bytes()
        quictls = source / "submodules/quictls/LICENSE.txt"
        if quictls.is_file():
            license_text += b"\n\n--- quictls (OpenSSL), linked into libmsquic.so ---\n\n" + quictls.read_bytes()
    (out / "LICENSE-msquic.txt").write_bytes(
        b"MsQuic " + MSQUIC_VERSION.encode() + b" (https://github.com/microsoft/msquic)\n\n" + license_text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rid", choices=sorted([*DOWNLOADS, *MACOS, ANDROID_RID]), required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not PROJECT.is_file():
        raise SystemExit(f"{PROJECT} is missing: git submodule update --init --recursive")
    if args.rid in MACOS and sys.platform != "darwin":
        raise SystemExit(f"{args.rid}: MsQuic and OpenSSL for macOS are built on a Mac")
    dotnet = shutil.which("dotnet") or sys.exit("the .NET 9 SDK (dotnet) is required")
    out = args.out.resolve()
    shutil.rmtree(out, ignore_errors=True)
    if args.rid == ANDROID_RID:
        build_android(dotnet, out)
        for path in sorted(out.iterdir()):
            print(f"{path.name:24} {path.stat().st_size:>10}  {hashlib.sha256(path.read_bytes()).hexdigest()[:16]}")
        return 0
    with tempfile.TemporaryDirectory(prefix="anx-p2p-") as scratch:
        subprocess.run([dotnet, "publish", str(PROJECT), "-c", "Release", "-r", args.rid, "-o", scratch, "-nologo"], check=True)
        out.mkdir(parents=True)
        exe = "muxi-p2p-agent.exe" if args.rid.startswith("win") else "muxi-p2p-agent"
        shutil.copy2(Path(scratch) / exe, out / exe)
    if args.rid in MACOS:
        with tempfile.TemporaryDirectory(prefix="anx-p2p-native-") as scratch:
            macos_libraries(args.rid, out, Path(scratch))
    else:
        name, library, license_text = msquic(args.rid)
        (out / name).write_bytes(library)
        (out / "LICENSE-msquic.txt").write_bytes(b"MsQuic " + MSQUIC_VERSION.encode() + b" (https://github.com/microsoft/msquic)\n\n" + license_text)
    for path in sorted(out.iterdir()):
        print(f"{path.name:24} {path.stat().st_size:>10}  {hashlib.sha256(path.read_bytes()).hexdigest()[:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
