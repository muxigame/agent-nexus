"""file_glob, file_grep, the reworked file_read, edit_file's snippet and GBK files, exec decoding."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from conftest import device_server
from src import search
from src.edit import edit_file
from src import exec_runtime
from src.exec_runtime import _StreamDecoder, resolve_default_shell
from src.files import file_read
from src.policy import Policy
from src.settings import Config


def policy_for(root: Path) -> Policy:
    return Policy(Config(allowed_roots=[str(root)], default_workdir=str(root)))


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "src" / "app.py").write_text("import os\n\ndef main():\n    return os.getcwd()\n", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "test_app.py").write_text("from app import main\n\ndef test_main():\n    assert main()\n",
                                                         encoding="utf-8")
    (tmp_path / "README.md").write_text("# demo\nmain entry point\n", encoding="utf-8")
    (tmp_path / "node_modules" / "dep" / "index.py").write_text("def main(): pass\n", encoding="utf-8")
    old = time.time() - 100
    os.utime(tmp_path / "README.md", (old, old))
    return tmp_path


# -- glob ------------------------------------------------------------------
def test_glob_regex_semantics() -> None:
    assert search.glob_regex("src/**/*.py").match("src/a/b/c.py")
    assert search.glob_regex("src/**/*.py").match("src/c.py")
    assert not search.glob_regex("src/*.py").match("src/a/c.py")
    assert search.glob_regex("*.{py,md}").match("x.md")
    assert search.glob_regex("[!a]*").match("b.txt") and not search.glob_regex("[!a]*").match("a.txt")


def test_file_glob_matches_names_at_any_depth_and_skips_dependencies(tree: Path) -> None:
    result = search.file_glob(policy_for(tree), "*.py")
    assert set(result["files"]) == {"src/app.py", "src/pkg/test_app.py"}
    named = search.file_glob(policy_for(tree), "node_modules/**/*.py")
    assert named["files"] == ["node_modules/dep/index.py"]
    by_path = search.file_glob(policy_for(tree), "src/**/test_*.py")
    assert by_path["files"] == ["src/pkg/test_app.py"]
    newest = search.file_glob(policy_for(tree), "*")
    assert newest["files"][-1] == "README.md"


# -- grep ------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rg"])
def test_file_grep_modes(tree: Path, engine: str) -> None:
    if engine == "rg" and search._ripgrep() is None:
        pytest.skip("ripgrep is not installed")
    policy = policy_for(tree)
    content = search.file_grep(policy, r"def main", engine=engine)
    assert content["engine"] == engine
    assert content["lines"] == ["src/app.py:3:def main():"]
    files = search.file_grep(policy, "main", output="files", engine=engine)
    assert sorted(files["files"]) == ["README.md", "src/app.py", "src/pkg/test_app.py"]
    counts = search.file_grep(policy, "main", output="count", glob="*.py", engine=engine)
    assert counts["counts"] == {"src/app.py": 1, "src/pkg/test_app.py": 3}
    assert counts["matches"] == 4
    context = search.file_grep(policy, "getcwd", context=1, engine=engine)
    assert context["lines"] == ["src/app.py-3-def main():", "src/app.py:4:    return os.getcwd()"]
    literal = search.file_grep(policy, "os.getcwd()", fixed_strings=True, engine=engine)
    assert literal["total"] == 1
    upper = search.file_grep(policy, "MAIN ENTRY", ignore_case=True, engine=engine)
    assert upper["lines"] == ["README.md:2:main entry point"]
    one_file = search.file_grep(policy, "main", path=str(tree / "README.md"), engine=engine)
    assert one_file["lines"] == ["README.md:2:main entry point"]


def test_file_grep_limits_and_errors(tree: Path) -> None:
    policy = policy_for(tree)
    limited = search.file_grep(policy, "main", output="files", limit=1, engine="python")
    assert len(limited["files"]) == 1 and limited["truncated"] and limited["total"] == 3
    with pytest.raises(search.SearchError, match="invalid regex"):
        search.file_grep(policy, "(", engine="python")
    with pytest.raises(search.SearchError, match="no such file"):
        search.file_grep(policy, "x", path=str(tree / "missing"))


# -- file_read -------------------------------------------------------------
def test_file_read_normalises_crlf_and_numbers_lines(tmp_path: Path) -> None:
    (tmp_path / "w.txt").write_bytes(b"one\r\ntwo\r\nthree")
    result = file_read(policy_for(tmp_path), "w.txt", 1, 10, 1000)
    assert result["content"] == "two\nthree" and result["line_ending"] == "crlf"
    assert result["total_lines"] == 3 and result["next_offset"] is None
    numbered = file_read(policy_for(tmp_path), "w.txt", 0, 2, 1000, line_numbers=True)
    assert numbered["content"] == "     1\tone\n     2\ttwo\n" and numbered["next_offset"] == 2


def test_file_read_splits_on_newlines_only_and_reads_gbk(tmp_path: Path) -> None:
    (tmp_path / "ff.txt").write_text("a\x0cb\nc\n", encoding="utf-8")
    assert file_read(policy_for(tmp_path), "ff.txt", 0, 10, 1000)["total_lines"] == 2
    (tmp_path / "gbk.txt").write_bytes("中文\n".encode("gbk"))
    result = file_read(policy_for(tmp_path), "gbk.txt", 0, 10, 1000)
    assert result["content"] == "中文\n" and result["encoding"] == "gb18030"


def test_file_read_cuts_a_line_longer_than_the_budget(tmp_path: Path) -> None:
    (tmp_path / "min.js").write_text("x" * 500 + "\nshort\n", encoding="utf-8")
    result = file_read(policy_for(tmp_path), "min.js", 0, 10, 100)
    assert result["lines"] == 1 and result["next_offset"] == 1
    assert result["long_line"] == {"line": 1, "chars": 501, "shown": 100}


# -- edit_file additions ----------------------------------------------------
def test_edit_returns_a_numbered_snippet(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("".join(f"line{n}\n" for n in range(1, 11)), encoding="utf-8")
    result = edit_file(policy_for(tmp_path), "a.py", "line5\n", "five\nfive-b\n")
    assert result["snippet"].splitlines() == [
        "     2\tline2", "     3\tline3", "     4\tline4", "     5\tfive", "     6\tfive-b",
        "     7\tline6", "     8\tline7", "     9\tline8"]


def test_edit_keeps_a_gbk_file_gbk(tmp_path: Path) -> None:
    target = tmp_path / "run.bat"
    target.write_bytes("echo 你好\r\npause\r\n".encode("gbk"))
    result = edit_file(policy_for(tmp_path), "run.bat", "你好", "再见")
    assert result["encoding"] == "gb18030"
    assert target.read_bytes() == "echo 再见\r\npause\r\n".encode("gbk")


# -- exec decoding ----------------------------------------------------------
def test_stream_decoder_keeps_characters_split_across_reads() -> None:
    data = "中文输出".encode("utf-8")
    decoder = _StreamDecoder()
    text = decoder.decode(data[:4]) + decoder.decode(data[4:]) + decoder.decode(b"", final=True)
    assert text == "中文输出"


@pytest.mark.skipif(sys.platform != "win32", reason="the code-page fallback is Windows-only")
def test_stream_decoder_falls_back_to_the_console_code_page() -> None:
    import locale
    encoding = locale.getpreferredencoding(False)
    if encoding.lower().replace("-", "") in ("utf8", "cp65001"):
        pytest.skip("this machine's code page is UTF-8")
    decoder = _StreamDecoder()
    sample = "中文"
    try:
        data = sample.encode(encoding)
    except UnicodeEncodeError:
        pytest.skip(f"{encoding} cannot encode the sample")
    assert decoder.decode(b"ok " + data) + decoder.decode(b"", final=True) == "ok " + sample


# -- through MCP --------------------------------------------------------------
@pytest.mark.asyncio
async def test_search_tools_over_mcp(tree: Path) -> None:
    server = device_server(tree, default_workdir=str(tree))
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    assert tools["file_grep"].meta["anx/capability"] == "search"
    assert tools["file_glob"].annotations.read_only_hint is True
    assert "sandbox_permissions" not in tools["exec_command"].parameters["properties"]
    result = (await server.mcp.call_tool("file_grep", {"pattern": "def main", "glob": "*.py"})).structured_content
    assert "src/app.py:3:def main():" in result["lines"]
    globbed = (await server.mcp.call_tool("file_glob", {"pattern": "*.md"})).structured_content
    assert globbed["files"] == ["README.md"]
    bad = (await server.mcp.call_tool("file_grep", {"pattern": "x", "path": str(tree / "nope")})).structured_content
    assert bad["status"] == "error"


def test_default_shell_without_git_bash(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(exec_runtime.sys, "platform", "win32")
    existing = tmp_path / "bash.exe"
    existing.write_bytes(b"")
    assert resolve_default_shell(str(existing)) == str(existing)
    git_root = tmp_path / "Git"
    (git_root / "cmd").mkdir(parents=True)
    (git_root / "bin").mkdir()
    (git_root / "bin" / "bash.exe").write_bytes(b"")
    monkeypatch.setattr(exec_runtime.shutil, "which",
                        lambda name: str(git_root / "cmd" / "git.exe") if name == "git" else None)
    assert resolve_default_shell(str(tmp_path / "missing.exe")) == str(git_root / "bin" / "bash.exe")
    monkeypatch.setattr(exec_runtime.shutil, "which", lambda name: None)
    assert resolve_default_shell(str(tmp_path / "missing.exe")) == "powershell"
