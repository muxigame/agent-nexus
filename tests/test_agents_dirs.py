"""Working-directory picker and per-run temporary directories (docs/agent-chat.md §7, §8)."""
from __future__ import annotations

import os
import sys
import time

import pytest

from agent_nexus.agents import tmpdirs as T
from agent_nexus.agents import workdirs as W

DAY = 86400


@pytest.fixture
def tree(tmp_path):
    home = tmp_path / "home"
    for sub in ("proj/src", "proj/.git", "docs", ".cache", "b-dir"):
        (home / sub).mkdir(parents=True)
    (home / "file.txt").write_text("x")
    outside = tmp_path / "outside"
    (outside / "secret").mkdir(parents=True)
    ws = tmp_path / "ws"
    (ws / "repo").mkdir(parents=True)
    return home, ws, outside


# ------------------------------------------------------------------ list_dirs
# (drives=False: these test the confinement itself; Windows adds the drive roots by default, tested below)

def test_roots_listing(tree):
    home, ws, _ = tree
    roots = W.default_roots(home, [{"name": "工作区", "path": ws}, str(ws), "/does/not/exist"], drives=False)
    assert [r["kind"] for r in roots] == ["home", "workspace"]
    out = W.list_dirs(None, roots)
    assert out["path"] is None and [d["path"] for d in out["dirs"]] == [str(home), str(ws)]


def test_lists_only_dirs_sorted_hidden_toggle(tree):
    home, ws, _ = tree
    roots = W.default_roots(home, [ws], drives=False)
    out = W.list_dirs(str(home), roots)
    assert [d["name"] for d in out["dirs"]] == ["b-dir", "docs", "proj"]
    assert out["hidden"] == 1 and out["parent"] is None and out["root"]["kind"] == "home"
    shown = W.list_dirs(str(home), roots, hidden=True)
    assert [d["name"] for d in shown["dirs"]] == ["b-dir", "docs", "proj", ".cache"]
    sub = W.list_dirs(str(home / "proj"), roots)
    assert [d["name"] for d in sub["dirs"]] == ["src"] and sub["parent"] == str(home)
    assert sub["dirs"][0]["path"] == str(home / "proj" / "src")


def test_tilde_and_relative(tree):
    home, ws, _ = tree
    roots = W.default_roots(home, [ws], drives=False)
    assert W.list_dirs("~", roots)["path"] == str(home)
    assert W.list_dirs("~/proj", roots)["path"] == str(home / "proj")
    assert W.list_dirs("proj", roots)["path"] == str(home / "proj")


def test_no_escape(tree):
    home, ws, outside = tree
    roots = W.default_roots(home, [ws], drives=False)
    for bad in (str(outside), str(home / ".." / "outside"), "~/../outside", "/"):
        out = W.list_dirs(bad, roots)
        assert out["code"] == "outside", bad
        assert "roots" in out
    assert W.list_dirs(str(home / "missing"), roots)["code"] == "not_found"
    assert W.list_dirs(str(home / "file.txt"), roots)["code"] == "not_found"
    assert W.list_dirs("a\x00b", roots)["code"] == "bad_path"
    assert W.list_dirs(None, [])["code"] == "no_roots"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlinks_cannot_escape(tree):
    home, ws, outside = tree
    os.symlink(outside, home / "link-out")
    os.symlink(ws / "repo", home / "link-in")
    roots = W.default_roots(home, [ws], drives=False)
    names = [d["name"] for d in W.list_dirs(str(home), roots)["dirs"]]
    assert "link-out" not in names and "link-in" in names
    assert W.list_dirs(str(home / "link-out"), roots)["code"] == "outside"
    assert W.list_dirs(str(home / "link-out" / "secret"), roots)["code"] == "outside"
    inside = W.list_dirs(str(home / "link-in"), roots)
    assert "error" not in inside


def test_parent_of_nested_workspace(tree):
    home, ws, _ = tree
    roots = W.default_roots(home, [ws], drives=False)
    out = W.list_dirs(str(ws / "repo"), roots)
    assert out["parent"] == str(ws)
    assert W.list_dirs(str(ws), roots)["parent"] is None


def test_drive_roots_off_windows():
    if sys.platform != "win32":
        assert W.drive_roots() == []


def test_frequent_cwds():
    sessions = [{"kind": "codex", "cwd": "/a", "updated_at": 5}, {"kind": "claude", "cwd": "/a", "updated_at": 9},
                {"kind": "codex", "cwd": "/b", "updated_at": 50}, {"kind": "codex", "cwd": None}]
    ranked = W.frequent_cwds(sessions)
    assert [r["path"] for r in ranked] == ["/a", "/b"] and ranked[0]["count"] == 2
    assert set(ranked[0]["kinds"]) == {"codex", "claude"}
    assert [r["path"] for r in W.frequent_cwds(sessions, kind="claude")] == ["/a"]


def test_recent(tmp_path, tree):
    home, ws, _ = tree
    recent = W.Recent(tmp_path)
    recent.add(str(home / "proj"), kind="codex", now=1)
    recent.add(str(ws), kind="claude", now=2)
    recent.add(str(home / "proj"), kind="claude", now=3)
    items = recent.list()
    assert [i["path"] for i in items] == [str(home / "proj"), str(ws)]
    assert items[0]["count"] == 2 and items[0]["kinds"] == ["codex", "claude"]
    assert recent.list(kind="codex")[0]["path"] == str(home / "proj")
    recent.add(str(tmp_path / "gone"))
    assert str(tmp_path / "gone") not in [i["path"] for i in recent.list()]
    assert str(tmp_path / "gone") in [i["path"] for i in recent.list(existing=False)]
    recent.remove(str(ws))
    assert str(ws) not in [i["path"] for i in W.Recent(tmp_path).list()]
    small = W.Recent(tmp_path / "x", limit=3)
    for n in range(5):
        small.add(f"/p{n}")
    assert len(small.list(existing=False)) == 3


# ------------------------------------------------------------------ TmpDirs

def _fill(path, size):
    path.mkdir(parents=True, exist_ok=True)
    (path / "blob.bin").write_bytes(b"x" * size)


def _age(path, seconds_ago):
    stamp = time.time() - seconds_ago
    for current, dirs, files in os.walk(path):
        for name in files:
            os.utime(os.path.join(current, name), (stamp, stamp))
        os.utime(current, (stamp, stamp))


def test_create_list_and_index(tmp_path):
    active = {"r1"}
    tmp = T.TmpDirs(tmp_path, active=lambda: active)
    path = tmp.create("r1", kind="codex", session="codex-proj")
    assert path == tmp_path / ".anx" / "agents" / "tmp" / "r1" and path.is_dir()
    if sys.platform != "win32":
        assert (path.stat().st_mode & 0o777) == 0o700
    (path / "sub").mkdir()
    (path / "sub" / "a.txt").write_text("hello")
    (path / "b.txt").write_text("hi")
    listing = tmp.list()
    item = listing["items"][0]
    assert item["rid"] == "r1" and item["size"] == 7 and item["files"] == 2
    assert item["active"] and item["kind"] == "codex" and item["session"] == "codex-proj"
    assert listing["total"] == 7
    with pytest.raises(ValueError):
        tmp.create("../evil")
    tmp.update("r1", session="renamed")
    assert tmp.list()["items"][0]["session"] == "renamed"


def test_create_with_account(tmp_path):
    from agent_nexus.accounts import Account
    me = Account("me", os.getuid() if hasattr(os, "getuid") else 0, os.getgid() if hasattr(os, "getgid") else 0,
                 str(tmp_path), "/bin/sh", [])
    tmp = T.TmpDirs(tmp_path)
    path = tmp.create("r2", me)
    assert path.is_dir()
    if hasattr(os, "getuid"):
        assert path.stat().st_uid == os.getuid()


def test_clean_by_rid_refuses_active(tmp_path):
    active = {"live"}
    tmp = T.TmpDirs(tmp_path, active=lambda: active)
    _fill(tmp.create("live"), 10)
    _fill(tmp.create("done"), 20)
    assert tmp.clean(rid="live")["code"] == "active"
    out = tmp.clean(rid="done")
    assert out == {"removed": 1, "freed": 20, "rids": ["done"]}
    assert tmp.clean(rid="done")["code"] == "not_found"
    assert tmp.clean(rid="../x")["code"] == "bad_request"
    assert tmp.clean()["code"] == "bad_request"
    assert tmp.clean(older_than_days="soon")["code"] == "bad_request"
    assert [i["rid"] for i in tmp.list()["items"]] == ["live"]


def test_clean_older_than(tmp_path):
    tmp = T.TmpDirs(tmp_path, active=lambda: {"old-active"})
    for rid, days in (("old", 10), ("new", 1), ("old-active", 30)):
        _fill(tmp.create(rid), 5)
        _age(tmp.base / rid, days * DAY)
    out = tmp.op_clean({"older_than_days": 7})
    assert out["rids"] == ["old"] and out["freed"] == 5
    assert sorted(i["rid"] for i in tmp.op_list()["items"]) == ["new", "old-active"]


def test_sweep_closed_age_cap_and_wake_logs(tmp_path):
    active: set[str] = {"running"}
    tmp = T.TmpDirs(tmp_path, active=lambda: active, cap_bytes=300)
    now = time.time()
    # closed 8 days ago (and idle since): removed
    _fill(tmp.create("closed-old"), 10)
    _age(tmp.base / "closed-old", 9 * DAY)
    tmp.close("closed-old", now=now - 8 * DAY)
    # closed 2 days ago, files old: kept (the close time counts)
    _fill(tmp.create("closed-recent"), 100)
    _age(tmp.base / "closed-recent", 20 * DAY)
    tmp.close("closed-recent", now=now - 2 * DAY)
    # never closed and idle for 9 days (crash leftover): removed
    _fill(tmp.base / "orphan", 10)
    _age(tmp.base / "orphan", 9 * DAY)
    # active, old and large: never removed
    _fill(tmp.create("running"), 200)
    _age(tmp.base / "running", 30 * DAY)
    # recent: kept unless over the cap
    _fill(tmp.create("fresh"), 100)
    _age(tmp.base / "fresh", 1 * DAY)

    wake = tmp_path / ".anx" / "wake"
    wake.mkdir(parents=True)
    (wake / "old.log").write_text("x")
    (wake / "new.log").write_text("x")
    (wake / "old.json").write_text("{}")
    for name in ("old.log", "old.json"):
        os.utime(wake / name, (now - 15 * DAY, now - 15 * DAY))

    out = tmp.sweep(now=now)
    # after age: running 200 + closed-recent 100 + fresh 100 = 400 > 300 -> drop the oldest inactive (closed-recent)
    assert sorted(out["rids"]) == ["closed-old", "closed-recent", "orphan"]
    assert out["freed"] == 120 and out["logs"] == 1
    assert sorted(i["rid"] for i in tmp.list()["items"]) == ["fresh", "running"]
    assert not (wake / "old.log").exists() and (wake / "new.log").exists() and (wake / "old.json").exists()
    index = tmp._load()
    assert set(index) <= {"fresh", "running"}


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_remove_does_not_follow_links(tmp_path):
    keep = tmp_path / "keep"
    keep.mkdir()
    (keep / "important").write_text("x")
    tmp = T.TmpDirs(tmp_path)
    tmp.base.mkdir(parents=True)
    os.symlink(keep, tmp.base / "linkrun")
    run = tmp.create("r")
    os.symlink(keep, run / "inner")
    assert tmp.clean(rid="r")["removed"] == 1
    assert tmp.clean(rid="linkrun")["removed"] == 1
    assert (keep / "important").exists()


def test_readonly_files_removed(tmp_path):
    tmp = T.TmpDirs(tmp_path)
    run = tmp.create("ro")
    (run / "f").write_text("x")
    os.chmod(run / "f", 0o444)
    assert tmp.clean(rid="ro")["removed"] == 1


def test_create_with_account_where_chown_does_not_exist(tmp_path, monkeypatch):
    # Windows CI: os.chown is POSIX-only; an account without a Windows session must not crash hand_over
    from agent_nexus import accounts
    monkeypatch.delattr(os, "chown", raising=False)
    path = tmp_path / "run" / "r3"
    accounts.hand_over(accounts.Account("me", 0, 0, str(tmp_path), "/bin/sh", []), path)
    assert path.is_dir()


def test_windows_lists_drive_roots_by_default(tree, monkeypatch):
    home, ws, _ = tree
    drive = home.parent / "drive-d"
    drive.mkdir()
    monkeypatch.setattr(W, "drive_roots", lambda: [str(drive)])
    monkeypatch.setattr(W, "WINDOWS", True)
    assert [r["kind"] for r in W.default_roots(home, [ws])] == ["home", "workspace", "drive"]
    monkeypatch.setattr(W, "WINDOWS", False)
    assert [r["kind"] for r in W.default_roots(home, [ws])] == ["home", "workspace"]
