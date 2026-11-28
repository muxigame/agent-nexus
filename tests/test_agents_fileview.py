"""§17: agent_file_stat / agent_file_read -- paths named in chat messages, resolved on the device."""
from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from agent_nexus.agents.fileview import READ_MAX, FileView


@pytest.fixture
def view(tmp_path):
    home = tmp_path / "home"
    (home / "proj" / "src").mkdir(parents=True)
    root = tmp_path / "root"
    (root / ".anx" / "agents" / "tmp" / "r1").mkdir(parents=True)
    (root / ".anx" / "keys.json").write_text("{}")
    (root / ".anx" / "agents" / "tmp" / "r1" / "a.txt").write_text("tmp ok")
    v = FileView(lambda: home, lambda: [str(root / ".anx"), str(root / ".mcp-manager")],
                 lambda: [str(root / ".anx" / "agents" / "tmp")])
    return v, home, root


def test_stat_resolves_home_relative_and_kinds(view):
    v, home, root = view
    (home / "proj" / "src" / "app.py").write_text("print(1)\n")
    (home / "notes.md").write_text("# hi\n")
    (home / "proj" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
    (home / "proj" / "blob.dat").write_bytes(b"ab\x00cd")
    base = str(home / "proj")
    out = v.op_stat({"base": base, "paths": ["src/app.py", "~/notes.md", "logo.png", "blob.dat", "src", "nope.txt",
                                             str(home / "proj" / "src" / "app.py")]})["files"]
    app, notes, logo, blob, src, nope, absolute = out
    assert app["exists"] and app["kind"] == "text" and app["path"] == str(home / "proj" / "src" / "app.py")
    assert notes["exists"] and notes["path"] == str(home / "notes.md")
    assert logo["kind"] == "image" and logo["mime"] == "image/png"
    assert blob["kind"] == "binary"
    assert src["exists"] and src["dir"]
    assert not nope["exists"]
    assert absolute["path"] == app["path"]
    assert v.op_stat({"paths": ["rel/x.py"]})["files"][0]["code"] == "bad_path"   # relative without a folder
    assert v.op_stat({"paths": []})["code"] == "bad_request"
    assert v.op_stat({"paths": ["a"] * 201})["code"] == "bad_request"


def test_state_dir_is_refused_but_the_tmp_area_is_not(view):
    v, home, root = view
    files = v.op_stat({"paths": [str(root / ".anx" / "keys.json"), str(root / ".anx" / "agents" / "tmp" / "r1" / "a.txt")]})["files"]
    assert files[0]["code"] == "not_allowed" and not files[0]["exists"]
    assert files[1]["exists"] and files[1]["kind"] == "text"
    assert v.op_read({"path": str(root / ".anx" / "keys.json")})["code"] == "not_allowed"
    # a link from HOME into the state dir is refused too (links are followed before the check)
    if os.name != "nt":
        (home / "sneaky.json").symlink_to(root / ".anx" / "keys.json")
        assert v.op_read({"path": "~/sneaky.json"})["code"] == "not_allowed"
        assert v.op_read({"path": "../../root/.anx/keys.json", "base": str(home / "proj")})["code"] == "not_allowed"


def test_text_window_around_a_line_and_gb18030(view):
    v, home, _ = view
    (home / "big.txt").write_text("".join(f"line {i}\n" for i in range(1, 5001)))
    out = v.op_read({"path": "~/big.txt", "mode": "text", "line": 3000})
    assert out["first_line"] <= 3000 < out["first_line"] + 1000
    lines = out["text"].split("\n")
    assert lines[3000 - out["first_line"]] == "line 3000"
    assert out["truncated"] and out["total_lines"] == 5000 and out["encoding"] == "utf-8"
    (home / "gbk.txt").write_bytes("中文内容\n第二行\n".encode("gb18030"))
    out = v.op_read({"path": "~/gbk.txt", "mode": "text"})
    assert out["text"] == "中文内容\n第二行" and out["encoding"] == "gb18030" and not out["truncated"]
    (home / "bin.dat").write_bytes(b"\x00\x01")
    assert v.op_read({"path": "~/bin.dat", "mode": "text"})["code"] == "binary"


def test_bytes_are_chunked(view):
    v, home, _ = view
    data = os.urandom(READ_MAX + 10)
    (home / "x.bin").write_bytes(data)
    first = v.op_read({"path": "~/x.bin", "mode": "bytes", "offset": 0, "length": READ_MAX * 4})
    assert len(base64.b64decode(first["data"])) == READ_MAX and not first["eof"] and first["size"] == len(data)
    rest = v.op_read({"path": "~/x.bin", "offset": READ_MAX})
    assert base64.b64decode(first["data"]) + base64.b64decode(rest["data"]) == data and rest["eof"]
    assert v.op_read({"path": "~/missing.bin"})["code"] == "missing"
    assert v.op_read({"path": "~/x.bin", "mode": "html"})["code"] == "bad_request"


def test_handlers_never_raise(view):
    v, _, _ = view
    for name, handler in v.handlers().items():
        for params in (None, {}, {"paths": [5, None, "\x00"]}, {"path": 7, "base": []}):
            result = handler(params)
            assert isinstance(result, dict), name


@pytest.mark.parametrize("raw", ["\\\\evil.example\\share\\a.png", "//evil.example/share/a.png",
                                 "\\\\?\\UNC\\evil.example\\share\\a.png", "\\\\.\\pipe\\x",
                                 "file:////evil.example/share/a.png", "file://evil.example/share/a.png"])
def test_network_paths_are_refused_before_any_stat(view, raw, monkeypatch):
    v, home, root = view
    touched = []
    real_stat, real_realpath = os.stat, os.path.realpath
    monkeypatch.setattr(os, "stat", lambda p, *a, **k: touched.append(p) or real_stat(p, *a, **k))
    monkeypatch.setattr(os.path, "realpath", lambda p, *a, **k: touched.append(p) or real_realpath(p, *a, **k))
    out = v.op_stat({"base": str(home), "paths": [raw]})["files"][0]
    assert out["exists"] is False and out["code"] == "network"
    assert v.op_read({"path": raw, "mode": "bytes"})["code"] == "network"
    assert touched == []


def test_network_base_is_refused(view):
    v, home, root = view
    out = v.op_stat({"base": "\\\\evil.example\\share", "paths": ["a.png"]})["files"][0]
    assert out["code"] == "network"


def test_confined_view_reads_only_the_tmp_area(view):
    v, home, root = view
    (home / "secret.txt").write_text("no")
    c = v.confined_view()
    assert c.op_read({"path": str(home / "secret.txt"), "mode": "text"})["code"] == "files_disabled"
    assert c.op_stat({"paths": [str(home / "secret.txt")]})["files"][0]["code"] == "files_disabled"
    ok = c.op_read({"path": str(root / ".anx" / "agents" / "tmp" / "r1" / "a.txt"), "mode": "text"})
    assert ok["text"] == "tmp ok"
    assert c.op_read({"path": str(root / ".anx" / "keys.json"), "mode": "text"})["code"] == "files_disabled"
