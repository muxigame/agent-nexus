"""muxi-p2p-agent's environment: on macOS its folder goes on DYLD_LIBRARY_PATH (OpenSSL for QUIC)."""
import os
import sys

import pytest

from agent_nexus.p2p import child_env

EXE = "/Users/someone/Library/Application Support/AgentNexus/runtime/1.0/tools/muxi-p2p-agent"
FOLDER = "/Users/someone/Library/Application Support/AgentNexus/runtime/1.0/tools"


def test_macos_adds_the_agent_folder_once():
    env = child_env(EXE, {"PATH": "/bin"}, platform="darwin")
    assert env["DYLD_LIBRARY_PATH"] == FOLDER and env["PATH"] == "/bin"
    again = child_env(EXE, {"DYLD_LIBRARY_PATH": f"/opt/x:{FOLDER}"}, platform="darwin")
    assert again["DYLD_LIBRARY_PATH"] == f"/opt/x:{FOLDER}"
    other = child_env(EXE, {"DYLD_LIBRARY_PATH": "/opt/x"}, platform="darwin")
    assert other["DYLD_LIBRARY_PATH"] == f"{FOLDER}:/opt/x"


def test_other_platforms_are_left_alone():
    for platform in ("win32", "linux"):
        assert "DYLD_LIBRARY_PATH" not in child_env(EXE, {"PATH": "x"}, platform=platform)


def test_android_binary_comes_from_the_native_library_dir(tmp_path, monkeypatch):
    from agent_nexus import p2p
    monkeypatch.delenv("ANX_MUXI_P2P", raising=False)
    monkeypatch.setenv("ANX_ANDROID_NATIVE_LIBS", str(tmp_path))
    assert p2p.binary() is None  # not packaged: no fallback to PATH or tools/
    (tmp_path / "libmuxip2p.so").write_bytes(b"\x7fELF")
    assert p2p.binary() == str(tmp_path / "libmuxip2p.so")


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need a privilege on Windows")
def test_android_links_openssl_and_sets_the_library_path(tmp_path):
    native, links = tmp_path / "lib", tmp_path / "files"
    env = child_env(str(native / "libmuxip2p.so"),
                    {"ANX_ANDROID_NATIVE_LIBS": str(native), "ANX_ANDROID_LINK_DIR": str(links), "LD_LIBRARY_PATH": "/x"})
    folder = links / "anx-p2p-libs"
    assert env["LD_LIBRARY_PATH"] == f"{folder}:{native}:/x"
    assert os.readlink(folder / "libssl.so") == str(native / "libssl_python.so")
    assert os.readlink(folder / "libcrypto.so.3") == str(native / "libcrypto_python.so")
    # An app update moves nativeLibraryDir: the links follow it.
    moved = tmp_path / "lib2"
    child_env("x", {"ANX_ANDROID_NATIVE_LIBS": str(moved), "ANX_ANDROID_LINK_DIR": str(links)})
    assert os.readlink(folder / "libssl.so") == str(moved / "libssl_python.so")
