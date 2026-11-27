"""files/2: the share's copy-job extensions (docs/design.md §18)."""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import types

import pytest

from agent_nexus import file_share
from agent_nexus.file_share import FileShare, FileShareError, invalid_name, part_name


def share(tmp_path):
    account = types.SimpleNamespace(home=str(tmp_path), session=None, uid=os.getuid() if hasattr(os, "getuid") else 0,
                                    gid=os.getgid() if hasattr(os, "getgid") else 0)
    return FileShare(account=lambda: account)


def put(files, params, data, finish=None):
    started = files.begin(params)
    if started.get("skipped"):
        return started
    files.write_bytes({"id": started["id"]}, data)
    return files.finish({"id": started["id"], **(finish or {})})


def test_on_exists_modes(tmp_path):
    files = share(tmp_path)
    target = tmp_path / "out" / "a.bin"
    first = put(files, {"base": str(tmp_path / "out"), "rel": ["a.bin"], "size": 3, "on_exists": "error"}, b"abc")
    assert first["path"] == str(target) and first["sha256"] == hashlib.sha256(b"abc").hexdigest()
    with pytest.raises(FileShareError) as refused:
        files.begin({"base": str(tmp_path / "out"), "rel": ["a.bin"], "size": 3, "on_exists": "error"})
    assert refused.value.code == "exists"
    put(files, {"base": str(tmp_path / "out"), "rel": ["a.bin"], "size": 4, "on_exists": "overwrite"}, b"wxyz")
    assert target.read_bytes() == b"wxyz"
    renamed = put(files, {"base": str(tmp_path / "out"), "rel": ["a.bin"], "size": 1}, b"r")  # files/1 default
    assert renamed["path"].endswith("a (1).bin")
    mtime = target.stat().st_mtime_ns
    same = files.begin({"base": str(tmp_path / "out"), "rel": ["a.bin"], "size": 4, "mtime_ns": mtime,
                        "on_exists": "skip_same"})
    assert same["skipped"] is True
    with pytest.raises(FileShareError):
        files.begin({"base": str(tmp_path / "out"), "rel": ["a.bin"], "size": 5, "on_exists": "skip_same"})
    assert not [p for p in (tmp_path / "out").iterdir() if p.name.endswith(".anxpart")]


def test_mtime_is_kept_and_checksum_mismatch_deletes_the_part(tmp_path):
    files = share(tmp_path)
    when = 1_600_000_000_123_456_789
    done = put(files, {"base": str(tmp_path), "rel": ["m.txt"], "size": 2, "mtime_ns": when}, b"hi")
    assert (tmp_path / "m.txt").stat().st_mtime_ns == when and done["size"] == 2
    started = files.begin({"base": str(tmp_path), "rel": ["bad.txt"], "size": 2})
    files.write_bytes({"id": started["id"]}, b"hi")
    with pytest.raises(FileShareError) as bad:
        files.finish({"id": started["id"], "sha256": "0" * 64})
    assert bad.value.code == "checksum"
    assert not (tmp_path / "bad.txt").exists()
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".anxpart")]


def test_resume_key_part_survives_abort_and_reap(tmp_path, monkeypatch):
    files = share(tmp_path)
    data = os.urandom(3 * file_share.CHUNK + 11)
    params = {"base": str(tmp_path), "rel": ["big.bin"], "size": len(data), "resume_key": "k" * 16, "resume": True}
    first = files.begin(params)
    assert first["have"] == 0
    files.write_bytes({"id": first["id"]}, data[:1000])
    files.abort({"id": first["id"]})  # no discard: the part stays
    part = tmp_path / part_name("big.bin", "k" * 16)
    assert part.stat().st_size == 1000
    second = files.begin(params)
    assert second["have"] == 1000
    files.write_bytes({"id": second["id"]}, data[1000:5000])
    # idle long enough: the reaper closes the handle and keeps the part
    monkeypatch.setattr(file_share, "UPLOAD_IDLE_S", -1.0)
    files._reap()
    monkeypatch.setattr(file_share, "UPLOAD_IDLE_S", 600.0)
    assert part.stat().st_size == 5000 and not files.uploads
    stats = files.stat_many({"base": str(tmp_path), "rels": [["big.bin"]], "keys": ["k" * 16]})
    assert stats["items"][0] == {"exists": False, "have": 5000} and stats["free"] > 0
    third = files.begin(params)
    assert third["have"] == 5000
    files.write_bytes({"id": third["id"]}, data[5000:])
    # a resumed part is re-hashed; with wait_s the answer may be "hashing" first
    expected = hashlib.sha256(data).hexdigest()
    while True:
        done = files.finish({"id": third["id"], "sha256": expected, "wait_s": 0.0})
        if done.get("state") != "hashing":
            break
    assert done["sha256"] == expected and (tmp_path / "big.bin").read_bytes() == data
    assert not part.exists()
    # a discarding abort deletes the part
    fourth = files.begin({**params, "rel": ["other.bin"]})
    files.write_bytes({"id": fourth["id"]}, b"x")
    files.abort({"id": fourth["id"], "discard": True})
    assert not (tmp_path / part_name("other.bin", "k" * 16)).exists()


def test_walk_pages_with_a_cursor_statelessly(tmp_path):
    files = share(tmp_path)
    root = tmp_path / "tree"
    for folder in range(3):
        sub = root / f"d{folder}"
        sub.mkdir(parents=True)
        for index in range(900):
            (sub / f"f{index:04d}.csv").write_bytes(b"x" * (index % 7))
    (root / "empty").mkdir()
    (root / "top.txt").write_text("t")
    os.symlink(root / "d0", root / "zlink")
    seen, cursor, pages = [], None, 0
    while True:
        page = FileShare(account=files.account).walk({"path": str(root), "cursor": cursor, "limit": 2000})
        pages += 1
        seen.extend(page["entries"])
        cursor = page["next"]
        if not cursor:
            break
    assert pages == 2
    rels = [tuple(e["rel"]) for e in seen]
    assert len(rels) == len(set(rels)) == 3 + 2700 + 1 + 1 + 1
    assert rels == sorted(rels, key=lambda r: list(r))  # pre-order, sorted by name
    link = next(e for e in seen if e["rel"] == ["zlink"])
    assert link["link"] is True and not link["dir"]
    assert not any(e["rel"][0] == "zlink" and len(e["rel"]) > 1 for e in seen)
    one = files.walk({"path": str(root / "top.txt")})
    assert one["entries"] == [{"rel": [], "dir": False, "link": False, "size": 1, "mtime_ns": one["root"]["mtime_ns"],
                               "mode": (root / "top.txt").stat().st_mode & 0o7777}]
    with pytest.raises(FileShareError) as missing:
        files.walk({"path": str(root / "nope")})
    assert missing.value.code == "missing"


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="permission bits do not stop root (or Windows)")
def test_walk_reports_an_unreadable_folder_and_goes_on(tmp_path):
    files = share(tmp_path)
    (tmp_path / "a" / "locked").mkdir(parents=True)
    (tmp_path / "a" / "locked" / "x").write_text("x")
    (tmp_path / "a" / "ok.txt").write_text("ok")
    os.chmod(tmp_path / "a" / "locked", 0)
    try:
        page = files.walk({"path": str(tmp_path / "a")})
    finally:
        os.chmod(tmp_path / "a" / "locked", 0o755)
    by_rel = {tuple(e["rel"]): e for e in page["entries"]}
    assert "error" in by_rel[("locked",)] and ("ok.txt",) in by_rel


def test_stat_many_mkdirs_and_hash(tmp_path):
    files = share(tmp_path)
    (tmp_path / "f.bin").write_bytes(b"0123456789")
    result = files.stat_many({"base": str(tmp_path / "new" / "deeper"), "rels": [[], ["x"]]})
    assert result["items"] == [{"exists": False}, {"exists": False}] and result["free"] > 0
    assert result["platform"] == sys.platform
    files.mkdirs({"base": str(tmp_path / "new"), "rels": [["a", "b"], ["c"]]})
    assert (tmp_path / "new" / "a" / "b").is_dir() and (tmp_path / "new" / "c").is_dir()
    found = files.stat_many({"base": str(tmp_path), "rels": [["f.bin"], ["new"], ["..", "x"]]})["items"]
    assert found[0]["exists"] and found[0]["size"] == 10 and found[1]["dir"] is True and found[2]["code"] == "name"
    assert files.hash({"path": str(tmp_path / "f.bin")})["sha256"] == hashlib.sha256(b"0123456789").hexdigest()
    assert files.hash({"base": str(tmp_path), "rel": ["f.bin"], "offset": 4, "length": 3})["sha256"] == \
        hashlib.sha256(b"456").hexdigest()


def test_relay_handlers_carry_codes(tmp_path):
    files = share(tmp_path)
    handlers = files.handlers()
    (tmp_path / "x").write_text("x")

    async def run():
        refused = await handlers["fx_put_begin"]({"base": str(tmp_path), "rel": ["x"], "size": 1, "on_exists": "error"})
        walked = await handlers["fx_walk"]({"path": str(tmp_path)})
        return refused, walked

    refused, walked = asyncio.run(run())
    assert refused["code"] == "exists" and "目标已存在" in refused["error"]
    assert [e["rel"] for e in walked["entries"]] == [["x"]]


def test_windows_name_rules_are_a_pure_function():
    for bad in ("CON", "nul.txt", "a:b", "x?", "trail.", "trail ", "a\\b", "COM1.log", "\x01"):
        assert invalid_name(bad, windows=True), bad
    for ok in ("data.csv", "2021-09-01.parquet", "中文.txt", "CONSOLE.txt"):
        assert invalid_name(ok, windows=True) is None, ok
    assert invalid_name("a:b", windows=False) is None and invalid_name("a/b", windows=False)
    assert invalid_name("..", windows=False) and invalid_name("", windows=False)


def test_new_folders_are_given_to_the_person(tmp_path, monkeypatch):
    files = share(tmp_path)
    given = []
    monkeypatch.setattr(files, "_give", lambda path: given.append(path))
    put(files, {"base": str(tmp_path / "x" / "y"), "rel": ["z.txt"], "size": 1}, b"z")
    assert tmp_path / "x" in given and tmp_path / "x" / "y" in given and tmp_path / "x" / "y" / "z.txt" in given


def test_a_resumed_upload_without_the_source_digest_is_refused(tmp_path):
    files = share(tmp_path)
    params = {"base": str(tmp_path), "rel": ["r.bin"], "size": 6, "resume_key": "r" * 16, "resume": True}
    first = files.begin(params)
    files.write_bytes({"id": first["id"]}, b"abc")
    files.abort({"id": first["id"]})
    second = files.begin(params)
    assert second["have"] == 3
    files.write_bytes({"id": second["id"]}, b"def")
    with pytest.raises(FileShareError) as refused:
        files.finish({"id": second["id"], "wait_s": 5})
    assert refused.value.code == "unverified" and not (tmp_path / "r.bin").exists()
    assert not (tmp_path / part_name("r.bin", "r" * 16)).exists()
    done = put(files, {"base": str(tmp_path), "rel": ["n.bin"], "size": 2}, b"ok",
               {"sha256": hashlib.sha256(b"ok").hexdigest()})
    assert done["checked"] is True
    assert put(files, {"base": str(tmp_path), "rel": ["m.bin"], "size": 2}, b"ok")["checked"] is False


def test_old_parts_are_swept_when_a_job_looks_at_their_folder(tmp_path):
    files = share(tmp_path)
    old = tmp_path / part_name("gone.csv", "o" * 16)
    old.write_bytes(b"x")
    fresh = tmp_path / part_name("kept.csv", "f" * 16)
    fresh.write_bytes(b"y")
    long_ago = 1_000_000_000
    os.utime(old, (long_ago, long_ago))
    stats = files.stat_many({"base": str(tmp_path), "rels": [["kept.csv"]], "keys": ["f" * 16]})
    assert stats["items"][0]["have"] == 1 and stats["base"] == str(tmp_path)
    assert not old.exists() and fresh.exists()


def test_a_paged_walk_stats_each_entry_about_once(tmp_path, monkeypatch):
    """Not once per page for every entry before the cursor (that is quadratic in a big folder)."""
    files = share(tmp_path)
    flat = tmp_path / "flat"
    flat.mkdir()
    for index in range(6000):
        (flat / f"{index:05d}.csv").touch()
    calls = {"n": 0}
    original = FileShare._lfacts

    def counting(path):
        calls["n"] += 1
        return original(path)
    monkeypatch.setattr(FileShare, "_lfacts", staticmethod(counting))
    seen, cursor, pages = 0, None, 0
    while True:
        page = files.walk({"path": str(flat), "cursor": cursor, "limit": 500})
        seen += len(page["entries"])
        pages += 1
        cursor = page["next"]
        if not cursor:
            break
    assert seen == 6000 and pages == 12
    assert calls["n"] <= 6000 + 2 * pages, calls


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership")
def test_a_part_that_is_not_a_plain_file_of_ours_is_started_over(tmp_path):
    files = share(tmp_path)
    key = "h" * 16
    part = tmp_path / part_name("h.bin", key)
    (tmp_path / "other").write_bytes(b"12345")
    os.link(tmp_path / "other", part)  # a hard link: two names, not ours alone
    stats = files.stat_many({"base": str(tmp_path), "rels": [["h.bin"]], "keys": [key]})
    assert stats["items"][0]["have"] == 0
    started = files.begin({"base": str(tmp_path), "rel": ["h.bin"], "size": 2, "resume_key": key, "resume": True})
    assert started["have"] == 0
    files.write_bytes({"id": started["id"]}, b"ok")
    files.finish({"id": started["id"], "sha256": hashlib.sha256(b"ok").hexdigest()})
    assert (tmp_path / "other").read_bytes() == b"12345" and (tmp_path / "h.bin").read_bytes() == b"ok"
