"""Build the P2P tunnel tools the client packages carry in ``tools/``.

    uv run --no-project python scripts/build_p2p_tools.py --rid win-x64 --out build/p2p
    uv run --no-project python scripts/build_p2p_tools.py --rid linux-x64 --out build/p2p

* ``muxi-p2p-agent[.exe]``: published from the submodule third_party/muxi-p2p/agent
  (self-contained, single file; needs the .NET 9 SDK).
* MsQuic next to it, so QUIC hole punching works where the OS has none (Windows 10, Server 2019,
  Ubuntu without Microsoft's repository): ``msquic.dll`` from the NuGet package
  Microsoft.Native.Quic.MsQuic.Schannel, ``libmsquic.so.2`` from Microsoft's Ubuntu package.
  Both downloads are pinned by version and SHA-256. MIT, see ``LICENSE-msquic.txt``.

The build scripts take the folder from ``ANX_P2P_TOOLS``.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import lzma
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
    "win-x64": (f"https://api.nuget.org/v3-flatcontainer/microsoft.native.quic.msquic.schannel/{MSQUIC_VERSION}/"
                f"microsoft.native.quic.msquic.schannel.{MSQUIC_VERSION}.nupkg",
                "fd0dbcb39bb2069ec31d790a7fa0c1eb4866a3d766aa3752bfde6ccebb778842"),
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rid", choices=sorted(DOWNLOADS), required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not PROJECT.is_file():
        raise SystemExit(f"{PROJECT} is missing: git submodule update --init --recursive")
    dotnet = shutil.which("dotnet") or sys.exit("the .NET 9 SDK (dotnet) is required")
    out = args.out.resolve()
    shutil.rmtree(out, ignore_errors=True)
    with tempfile.TemporaryDirectory(prefix="anx-p2p-") as scratch:
        subprocess.run([dotnet, "publish", str(PROJECT), "-c", "Release", "-r", args.rid, "-o", scratch, "-nologo"], check=True)
        out.mkdir(parents=True)
        exe = "muxi-p2p-agent.exe" if args.rid.startswith("win") else "muxi-p2p-agent"
        shutil.copy2(Path(scratch) / exe, out / exe)
    name, library, license_text = msquic(args.rid)
    (out / name).write_bytes(library)
    (out / "LICENSE-msquic.txt").write_bytes(b"MsQuic " + MSQUIC_VERSION.encode() + b" (https://github.com/microsoft/msquic)\n\n" + license_text)
    for path in sorted(out.iterdir()):
        print(f"{path.name:24} {path.stat().st_size:>10}  {hashlib.sha256(path.read_bytes()).hexdigest()[:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
