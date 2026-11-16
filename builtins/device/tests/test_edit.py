"""edit_file and patch_file: exact replacement and unified diffs, all or nothing."""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import device_server
from src.edit import EditError, edit_file, patch_file
from src.policy import Policy, PolicyError
from src.settings import Config


def policy_for(root: Path) -> Policy:
    return Policy(Config(allowed_roots=[str(root)], default_workdir=str(root)))


# -- edit_file --------------------------------------------------------------------
def test_edit_replaces_a_unique_occurrence(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("def f():\n    return 1\n\ndef g():\n    return 2\n", encoding="utf-8")
    result = edit_file(policy_for(tmp_path), "a.py", "    return 2\n", "    return 3\n")
    assert result["replacements"] == 1 and result["first_line"] == 5
    assert target.read_text(encoding="utf-8").endswith("    return 3\n")


def test_edit_refuses_ambiguous_and_missing_text(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x = 1\nx = 1\n", encoding="utf-8")
    policy = policy_for(tmp_path)
    with pytest.raises(EditError, match="occurs 2 times"):
        edit_file(policy, "a.txt", "x = 1", "x = 2")
    with pytest.raises(EditError, match="not found"):
        edit_file(policy, "a.txt", "y = 1", "y = 2")
    assert target.read_text(encoding="utf-8") == "x = 1\nx = 1\n"
    result = edit_file(policy, "a.txt", "x = 1", "x = 2", replace_all=True)
    assert result["replacements"] == 2
    assert target.read_text(encoding="utf-8") == "x = 2\nx = 2\n"


def test_edit_hints_at_trailing_whitespace(tmp_path: Path) -> None:
    (tmp_path / "w.txt").write_text("alpha   \nbeta\n", encoding="utf-8")
    with pytest.raises(EditError, match="trailing whitespace"):
        edit_file(policy_for(tmp_path), "w.txt", "alpha\nbeta", "ALPHA\nbeta")


def test_edit_keeps_crlf_bom_and_missing_final_newline(tmp_path: Path) -> None:
    target = tmp_path / "win.txt"
    target.write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\nthree")
    edit_file(policy_for(tmp_path), "win.txt", "one\ntwo", "ONE\ntwo")
    assert target.read_bytes() == b"\xef\xbb\xbfONE\r\ntwo\r\nthree"


def test_edit_refuses_empty_old_string_binary_and_outside_roots(tmp_path: Path) -> None:
    policy = policy_for(tmp_path)
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    with pytest.raises(EditError, match="file_write"):
        edit_file(policy, "a.txt", "", "x")
    (tmp_path / "b.bin").write_bytes(b"\x00\x01")
    with pytest.raises(EditError, match="binary"):
        edit_file(policy, "b.bin", "a", "b")
    outside = tmp_path.parent / "outside-edit.txt"
    with pytest.raises(PolicyError):
        edit_file(policy, str(outside), "a", "b")


# -- patch_file -------------------------------------------------------------------
GIT_DIFF = """\
diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,4 +1,4 @@
 import os
-
+import sys
 def main():
     return 0
@@ -8,3 +8,4 @@ def helper():
 def tail():
     pass
 # end
+# appended
diff --git a/new.txt b/new.txt
new file mode 100644
--- /dev/null
+++ b/new.txt
@@ -0,0 +1,2 @@
+hello
+world
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
--- a/gone.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-bye
-now
"""


def make_project(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text(
        "import os\n\ndef main():\n    return 0\n\ndef helper():\n    pass\ndef tail():\n    pass\n# end\n",
        encoding="utf-8")
    (root / "gone.txt").write_text("bye\nnow\n", encoding="utf-8")


def test_patch_modifies_creates_and_deletes(tmp_path: Path) -> None:
    make_project(tmp_path)
    result = patch_file(policy_for(tmp_path), GIT_DIFF)
    assert [item["action"] for item in result["files"]] == ["modified", "created", "deleted"]
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == (
        "import os\nimport sys\ndef main():\n    return 0\n\ndef helper():\n    pass\ndef tail():\n"
        "    pass\n# end\n# appended\n")
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hello\nworld\n"
    assert not (tmp_path / "gone.txt").exists()


def test_patch_is_all_or_nothing(tmp_path: Path) -> None:
    make_project(tmp_path)
    broken = GIT_DIFF.replace("-bye\n-now\n", "-bye\n-later\n")
    with pytest.raises(EditError, match="does not match"):
        patch_file(policy_for(tmp_path), broken)
    assert "import sys" not in (tmp_path / "src" / "app.py").read_text(encoding="utf-8")
    assert not (tmp_path / "new.txt").exists() and (tmp_path / "gone.txt").exists()


def test_patch_dry_run_writes_nothing(tmp_path: Path) -> None:
    make_project(tmp_path)
    result = patch_file(policy_for(tmp_path), GIT_DIFF, dry_run=True)
    assert result["dry_run"] is True and len(result["files"]) == 3
    assert not (tmp_path / "new.txt").exists() and (tmp_path / "gone.txt").exists()


def test_patch_finds_drifted_hunks_and_reports_the_offset(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("intro\nintro2\na\nb\nc\n", encoding="utf-8")
    diff = "--- f.txt\n+++ f.txt\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
    result = patch_file(policy_for(tmp_path), diff)
    assert result["files"][0]["hunks"] == [{"hunk": 1, "line": 3, "offset": 2}]
    assert target.read_text(encoding="utf-8") == "intro\nintro2\na\nB\nc\n"


def test_patch_keeps_crlf_and_handles_missing_final_newline(tmp_path: Path) -> None:
    target = tmp_path / "w.txt"
    target.write_bytes(b"one\r\ntwo")
    diff = ("--- a/w.txt\n+++ b/w.txt\n@@ -1,2 +1,2 @@\n one\n-two\n\\ No newline at end of file\n"
            "+TWO\n")
    patch_file(policy_for(tmp_path), diff)
    assert target.read_bytes() == b"one\r\nTWO\r\n"


def test_patch_renames_with_git_headers(tmp_path: Path) -> None:
    (tmp_path / "old.txt").write_text("keep\nchange\n", encoding="utf-8")
    diff = ("diff --git a/old.txt b/new.txt\nsimilarity index 50%\nrename from old.txt\nrename to new.txt\n"
            "--- a/old.txt\n+++ b/new.txt\n@@ -1,2 +1,2 @@\n keep\n-change\n+changed\n")
    result = patch_file(policy_for(tmp_path), diff)
    assert result["files"][0]["action"] == "renamed"
    assert not (tmp_path / "old.txt").exists()
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "keep\nchanged\n"


def test_patch_rejects_bad_input_and_paths_outside_roots(tmp_path: Path) -> None:
    policy = policy_for(tmp_path)
    with pytest.raises(EditError, match="no file changes"):
        patch_file(policy, "just some text\n")
    with pytest.raises(EditError, match="outside a hunk"):
        patch_file(policy, "--- /dev/null\n+++ b/x.txt\n@@ -0,0 +1 @@\n+a\n+b\n")
    with pytest.raises(EditError, match="truncated"):
        patch_file(policy, "--- a/x.txt\n+++ b/x.txt\n@@ -1,3 +1,3 @@\n a\n")
    with pytest.raises(PolicyError):
        patch_file(policy, "--- /dev/null\n+++ b/../escape.txt\n@@ -0,0 +1 @@\n+no\n")
    assert not (tmp_path.parent / "escape.txt").exists()


# -- through the MCP server ---------------------------------------------------------
async def test_tools_report_errors_as_results(tmp_path: Path) -> None:
    server = device_server(tmp_path, default_workdir=str(tmp_path))
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    ok = (await server.mcp.call_tool("edit_file", {"path": "a.txt", "old_string": "hello",
                                                   "new_string": "hi"})).structured_content
    assert ok["status"] == "ok"
    missing = (await server.mcp.call_tool("edit_file", {"path": "a.txt", "old_string": "hello",
                                                        "new_string": "x"})).structured_content
    assert missing["status"] == "error" and "not found" in missing["error"]
    patched = (await server.mcp.call_tool("patch_file", {
        "patch": "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-hi\n+hey\n"})).structured_content
    assert patched["status"] == "ok" and patched["files"][0]["action"] == "modified"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hey\n"
