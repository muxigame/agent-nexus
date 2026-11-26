"""agent_nexus.agents.sessions: discovery, unified history events, metadata and file deletion (docs/agent-chat.md §3/§4)."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_nexus.agents import sessions as S

FIXTURE = Path(__file__).parent / "fixtures" / "agent_sessions" / "home"
CLAUDE_1 = "7f3c1e2a-5b4d-4c6e-8f90-1a2b3c4d5e6f"
CLAUDE_2 = "0a1b2c3d-1111-4222-8333-444455556666"
CLAUDE_BLANK = "99999999-aaaa-4bbb-8ccc-dddddddddddd"
CODEX_1 = "019a6f2e-3c4d-7e8f-9a0b-1c2d3e4f5a6b"
CODEX_ARCHIVED = "019a1111-2222-7333-8444-555566667777"
DSH_1 = "sess_01JD7ZK9Q8V2W3X4Y5Z6A7B8C9"
DSH_BLANK = "sess_01JD7ZBLANKBLANKBLANKBLANK"
DSH_INPUT = "sess_untitled_only_input"
GEMINI_1 = "4b5c6d7e-aaaa-4bbb-8ccc-0123456789ab"
GEMINI_LOGS = "55555555-6666-4777-8888-999999999999"


@pytest.fixture(autouse=True)
def _fresh_cache():
    S.clear_cache()
    yield
    S.clear_cache()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    target = tmp_path / "home"
    shutil.copytree(FIXTURE, target)
    return target


def by_id(items):
    return {(s["kind"], s["id"]): s for s in items}


def touch(path: Path, when: float) -> None:
    os.utime(path, (when, when))


# ------------------------------------------------------------------------------------------ discovery

def test_discover_all_kinds(home):
    found = by_id(S.discover(None, home, env={}))
    assert set(found) == {("claude", CLAUDE_1), ("claude", CLAUDE_2), ("codex", CODEX_1), ("codex", CODEX_ARCHIVED),
                          ("dsh", DSH_1), ("dsh", DSH_INPUT), ("dsh", "acp-demo-1"),
                          ("gemini", GEMINI_1), ("gemini", GEMINI_LOGS)}
    first = found[("claude", CLAUDE_1)]
    assert first["title"] == "修复登录页面的跳转错误"  # the summary record
    assert first["cwd"] == "/home/u/proj"
    assert first["messages"] == 2  # one typed prompt + one assistant answer; tool results and meta are not messages
    assert first["source"] == "disk" and first["pinned"] is False and first["alias"] is None
    assert first["created_at"] == pytest.approx(S._ts("2026-11-20T10:00:00.000Z"))
    assert first["size"] > 0 and first["label"] == "Claude Code"
    # custom-title (/rename) beats everything; slash commands and their output are never the title
    assert found[("claude", CLAUDE_2)]["title"] == "README 安装说明"
    assert found[("claude", CLAUDE_2)]["cwd"] == "/home/u/other project"


def test_blank_sessions_hidden_unless_asked(home):
    keys = set(by_id(S.discover(None, home, env={})))
    assert ("claude", CLAUDE_BLANK) not in keys and ("dsh", DSH_BLANK) not in keys
    keys = set(by_id(S.discover(None, home, env={}, include_blank=True)))
    assert ("claude", CLAUDE_BLANK) in keys and ("dsh", DSH_BLANK) in keys


def test_codex_titles_cwd_and_archive(home):
    found = by_id(S.discover(None, home, kind="codex", env={}))
    current = found[("codex", CODEX_1)]
    assert current["title"] == "添加 slugify"  # session_index.jsonl, newest line wins
    assert current["cwd"] == "/home/u/proj"
    assert current["messages"] == 2  # <user_instructions> / <environment_context> are not prompts
    archived = found[("codex", CODEX_ARCHIVED)]
    assert archived["archived"] is True and archived["title"] == "旧任务：清理日志"
    assert {k for k, _ in found} == {"codex"}


def test_codex_title_without_index_skips_injected_context(home):
    (home / ".codex" / "session_index.jsonl").unlink()
    found = by_id(S.discover(None, home, kind="codex", env={}))
    assert found[("codex", CODEX_1)]["title"] == "给 utils.py 加一个 slugify 函数并跑测试"


def test_codex_home_and_dsh_home_relocate(home, tmp_path):
    moved = tmp_path / "elsewhere"
    shutil.move(str(home / ".codex"), str(moved / "codex"))
    shutil.move(str(home / ".dsh"), str(moved / "dsh"))
    assert not by_id(S.discover(None, home, kind="codex", env={}))
    env = {"CODEX_HOME": str(moved / "codex"), "DSH_HOME": str(moved / "dsh")}
    found = by_id(S.discover(None, home, env=env))
    assert found[("codex", CODEX_1)]["title"] == "添加 slugify"  # the index moves with CODEX_HOME
    assert ("dsh", DSH_1) in found


def test_dsh_index_and_transcript_merge(home):
    found = by_id(S.discover(None, home, kind="dsh", env={}))
    session = found[("dsh", DSH_1)]
    assert session["title"] == "重构配置加载" and session["cwd"] == "/home/u/proj"
    assert session["messages"] == 3
    assert session["file"].endswith("session.v4.jsonl.zstd")  # history comes from the transcript, not the index
    assert len(session["files"]) == 2
    assert session["created_at"] == pytest.approx(1763800000.0)
    only_input = found[("dsh", DSH_INPUT)]
    assert only_input["title"] == "解释一下 main.rs" and only_input["cwd"] == "/home/u/rust"
    assert only_input["file"] is None and only_input["readable"] is False
    lenient = found[("dsh", "acp-demo-1")]
    assert lenient["title"] == "列出当前目录" and lenient["cwd"] == "/home/u/acp"


def test_gemini_cwd_from_project_hash_and_logs(home):
    found = by_id(S.discover(None, home, env={}))
    chat = found[("gemini", GEMINI_1)]
    assert chat["title"] == "这个项目用的什么测试框架？"
    assert chat["cwd"] == "/home/u/proj"  # sha256("/home/u/proj") is the project folder; Claude knows that cwd
    assert chat["messages"] == 3
    logs_only = found[("gemini", GEMINI_LOGS)]
    assert logs_only["title"] == "总结一下最近的提交"  # "/help" is a command, not a title
    assert logs_only["size"] == 0  # logs.json is shared with other sessions
    # alone, Gemini cannot know the cwd -- unless the caller passes known directories
    alone = by_id(S.discover(None, home, kind="gemini", env={}))
    assert alone[("gemini", GEMINI_1)]["cwd"] is None
    hinted = by_id(S.discover(None, home, kind="gemini", env={}, known_dirs=["/home/u/proj"]))
    assert hinted[("gemini", GEMINI_1)]["cwd"] == "/home/u/proj"
    (home / ".gemini" / "tmp" / chat["project_hash"] / ".project_root").write_text("/srv/app\n", encoding="utf-8")
    S.clear_cache()
    assert by_id(S.discover(None, home, kind="gemini", env={}))[("gemini", GEMINI_1)]["cwd"] == "/srv/app"


def test_unknown_kind_and_missing_home(tmp_path):
    assert S.discover(None, tmp_path / "nobody", env={}) == []
    assert S.discover(None, tmp_path, kind="nope", env={}) == []


def test_sort_pinned_first_then_recent(home, tmp_path):
    now = time.time()
    touch(home / ".claude/projects/-home-u-proj" / f"{CLAUDE_1}.jsonl", now - 5000)
    touch(home / ".claude/projects/-home-u-other-project" / f"{CLAUDE_2}.jsonl", now - 100)
    meta = S.SessionMeta(tmp_path / "root")
    items = S.discover(None, home, kind="claude", env={}, meta=meta)
    assert [s["id"] for s in items] == [CLAUDE_2, CLAUDE_1]
    meta.set("claude", CLAUDE_1, pinned=True)
    items = S.discover(None, home, kind="claude", env={}, meta=meta)
    assert [s["id"] for s in items] == [CLAUDE_1, CLAUDE_2]
    assert items[0]["pinned"] is True


def test_limit_keeps_pinned_sessions(home, tmp_path):
    folder = home / ".claude/projects/-home-u-many"
    folder.mkdir(parents=True)
    now = time.time()
    for i in range(12):
        sid = f"00000000-0000-4000-8000-{i:012d}"
        record = {"type": "user", "sessionId": sid, "cwd": "/home/u/many", "timestamp": "2026-11-01T00:00:00Z",
                  "message": {"role": "user", "content": f"任务 {i}"}}
        path = folder / f"{sid}.jsonl"
        path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        touch(path, now - 1000 * i)
    old = "00000000-0000-4000-8000-000000000011"
    meta = S.SessionMeta(tmp_path / "root")
    meta.set("claude", old, pinned=True)
    items = S.discover(None, home, kind="claude", env={}, limit=3, meta=meta)
    ids = [s["id"] for s in items]
    assert ids[0] == old  # pinned, though far older than the newest three
    assert len(ids) == 4


def test_query_filters(home):
    items = S.discover(None, home, env={}, query="slugify")
    assert [(s["kind"], s["id"]) for s in items] == [("codex", CODEX_1)]
    assert {s["id"] for s in S.discover(None, home, env={}, query="OTHER PROJECT")} == {CLAUDE_2}


def test_cache_by_mtime_and_size(home, monkeypatch):
    calls = []
    real = S._parse
    monkeypatch.setattr(S, "_parse", lambda *a: calls.append(a[1]) or real(*a))
    S.discover(None, home, kind="claude", env={})
    first = len(calls)
    assert first >= 3
    S.discover(None, home, kind="claude", env={})
    assert len(calls) == first  # nothing changed: nothing re-read
    path = home / ".claude/projects/-home-u-proj" / f"{CLAUDE_1}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "custom-title", "customTitle": "新名字", "sessionId": CLAUDE_1}) + "\n")
    found = by_id(S.discover(None, home, kind="claude", env={}))
    assert len(calls) == first + 1
    assert found[("claude", CLAUDE_1)]["title"] == "新名字"


def test_large_file_reads_head_and_tail(home, monkeypatch):
    monkeypatch.setattr(S, "FULL_SCAN_BYTES", 2000)
    monkeypatch.setattr(S, "HEAD_BYTES", 1500)
    monkeypatch.setattr(S, "TAIL_BYTES", 1000)
    path = home / ".claude/projects/-home-u-big" / "11111111-2222-4333-8444-555555555555.jsonl"
    path.parent.mkdir(parents=True)
    lines = [{"type": "user", "cwd": "/home/u/big", "timestamp": "2026-11-01T00:00:00Z",
              "message": {"role": "user", "content": "第一个问题"}}]
    lines += [{"type": "assistant", "message": {"id": f"m{i}", "role": "assistant", "content": [{"type": "text", "text": "x" * 200}]}}
              for i in range(50)]
    lines += [{"type": "custom-title", "customTitle": "很大的会话"}]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in lines) + "\n", encoding="utf-8")
    session = by_id(S.discover(None, home, kind="claude", env={}))[("claude", path.stem)]
    assert session["title"] == "很大的会话" and session["cwd"] == "/home/u/big"
    assert session["messages"] is None  # not counted when only head + tail were read


def test_garbage_files_do_not_break_listing(home):
    folder = home / ".claude/projects/-junk"
    folder.mkdir(parents=True)
    (folder / "abc.jsonl").write_bytes(b"\x00\xff not json\n{broken\n")
    (home / ".gemini/tmp/xyz/chats").mkdir(parents=True)
    (home / ".gemini/tmp/xyz/chats/session-bad.json").write_text("[1, 2", encoding="utf-8")
    keys = set(by_id(S.discover(None, home, env={}, include_blank=True)))
    assert ("claude", "abc") in keys and ("claude", CLAUDE_1) in keys
    assert ("gemini", "session-bad") in keys


def test_defs_objects_and_custom_kind(home):
    (home / ".myagent/history").mkdir(parents=True)
    (home / ".myagent/history/run-42.jsonl").write_text(
        json.dumps({"role": "user", "content": "自定义 CLI 的问题", "cwd": "/w"}, ensure_ascii=False) + "\n"
        + json.dumps({"role": "assistant", "content": "回答"}, ensure_ascii=False) + "\n", encoding="utf-8")
    defs = {"claude": SimpleNamespace(kind="claude", label="Claude Code", session_globs=[".claude/projects/*/*.jsonl"]),
            "mine": SimpleNamespace(kind="mine", label="我的 CLI", session_globs=[".myagent/history/*.jsonl"])}
    items = S.discover(defs, home, env={})
    assert {s["kind"] for s in items} == {"claude", "mine"}
    mine = next(s for s in items if s["kind"] == "mine")
    assert mine["id"] == "run-42" and mine["title"] == "自定义 CLI 的问题" and mine["cwd"] == "/w"
    assert mine["label"] == "我的 CLI"
    events = S.read(defs, home, "mine", "run-42", env={})
    assert [(e["type"], e["text"]) for e in events[:-1]] == [("user", "自定义 CLI 的问题"), ("message", "回答")]
    # plain glob lists work too
    assert S.discover({"claude": [".claude/projects/*/*.jsonl"]}, home, env={})


def test_glob_templates(home, tmp_path):
    env = {"MY_DIR": str(tmp_path / "m")}
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "s1.jsonl").write_text(json.dumps({"role": "user", "content": "hi"}) + "\n", encoding="utf-8")
    items = S.discover({"x": ["{MY_DIR:.x}/*.jsonl"]}, home, env=env)
    assert [s["id"] for s in items] == ["s1"]
    assert S._expand("../etc/*", home, {}) is None


def test_grouped_and_recent_cwds(home):
    items = S.discover(None, home, env={})
    groups = S.grouped(items)
    assert [g["kind"] for g in groups] == ["claude", "codex", "dsh", "gemini"]
    assert groups[0]["label"] == "Claude Code" and groups[0]["count"] == 2
    cwds = S.recent_cwds(items)
    proj = next(c for c in cwds if c["path"] == "/home/u/proj")
    assert set(proj["kinds"]) >= {"claude", "codex", "dsh", "gemini"} and proj["count"] >= 4
    assert all("files" not in S.public(s) for s in items)


# ------------------------------------------------------------------------------------------ read (unified events)

def test_read_claude(home):
    events = S.read(None, home, "claude", CLAUDE_1, env={})
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    kinds = [e["type"] for e in events]
    assert kinds == ["user", "thinking", "tool", "tool", "file_change", "tool", "message", "status"]
    assert events[0]["text"] == "登录后跳转到了 404，帮我看看 src/login.ts"
    assert events[0]["t"] == pytest.approx(S._ts("2026-11-20T10:00:00Z"))
    read_tool, edit_tool, bash_tool = events[2], events[3], events[5]
    assert read_tool["name"] == "Read" and read_tool["status"] == "ok" and "redirect" in read_tool["output"]
    assert edit_tool["title"] == "/home/u/proj/src/login.ts"
    assert bash_tool["title"] == "npm test" and bash_tool["status"] == "error" and bash_tool["output"] == "1 failing"
    change = events[4]
    assert change["kind"] == "edit" and "-redirect('/home')" in change["diff"] and "+redirect('/dashboard')" in change["diff"]
    assert all("子代理" not in (e.get("text") or "") for e in events)  # sidechain records stay out
    status = events[-1]
    assert status["state"] == "idle" and status["session_id"] == CLAUDE_1
    # usage counted once per message id (a message is split over several lines)
    assert status["usage"] == {"input": 1500 + 1500 + 1600 + 1700, "output": 40 + 60 + 20 + 30, "cost_usd": 0}


def test_read_claude_commands_and_write(home):
    events = S.read(None, home, "claude", CLAUDE_2, env={})
    users = [e["text"] for e in events if e["type"] == "user"]
    assert users == ["/clear", "写一个 README 的安装章节"]  # the command shows as what was typed; its output is hidden
    change = next(e for e in events if e["type"] == "file_change")
    assert change["kind"] == "add" and "+# 安装" in change["diff"]


def test_read_codex(home):
    events = S.read(None, home, "codex", CODEX_1, env={})
    kinds = [e["type"] for e in events]
    assert kinds == ["user", "thinking", "tool", "tool", "file_change", "file_change", "tool", "message", "status"]
    assert events[0]["text"] == "给 utils.py 加一个 slugify 函数并跑测试"  # injected context skipped, no duplicate
    assert events[1]["text"].startswith("**Planning**")
    shell = events[2]
    assert shell["title"] == "sed -n 1,80p utils.py" and shell["status"] == "ok" and shell["output"].startswith("def a()")
    patch = events[3]
    assert patch["name"] == "apply_patch" and patch["status"] == "ok"
    assert [(e["path"], e["kind"]) for e in events[4:6]] == [("utils.py", "edit"), ("tests/test_slug.py", "add")]
    assert "+def slugify(s):" in events[4]["diff"]
    assert events[6]["status"] == "error"  # exit code 1
    assert events[7] == {**events[7], "type": "message", "role": "assistant", "text": "已添加 slugify，测试还有 1 个失败。"}
    assert events[-1]["usage"] == {"input": 5400, "output": 420, "cost_usd": 0}


def test_read_gemini(home):
    events = S.read(None, home, "gemini", GEMINI_1, env={})
    assert [e["type"] for e in events] == ["user", "thinking", "tool", "message", "error", "status"]
    tool = events[2]
    assert tool["name"] == "read_file" and tool["output"] == "[tool.pytest]" and tool["status"] == "ok"
    assert events[1]["text"] == "**Inspecting files**\nLook at pyproject."
    assert events[-1]["usage"]["input"] == 1900
    logs = S.read(None, home, "gemini", GEMINI_LOGS, env={})
    assert [e["text"] for e in logs if e["type"] == "user"] == ["/help", "总结一下最近的提交"]


def test_read_dsh_lenient_acp_records(home):
    events = S.read(None, home, "dsh", "acp-demo-1", env={})
    assert [e["type"] for e in events] == ["user", "thinking", "tool", "message", "tool", "file_change", "status"]
    assert events[3]["text"] == "有两个文件：a.txt 和 b.txt。"  # chunks joined
    assert events[2]["output"] == "a.txt\nb.txt" and events[2]["status"] == "ok" and events[2]["title"] == "ls -la"


def test_read_dsh_zstd(home, monkeypatch):
    monkeypatch.setattr(S, "_zstd_decompress", lambda path, limit: None)
    events = S.read(None, home, "dsh", DSH_1, env={})
    assert events[0]["type"] == "error" and "zstandard" in events[0]["text"] and "续接" in events[0]["text"]

    def damaged(path, limit):
        raise ValueError("bad frame")
    monkeypatch.setattr(S, "_zstd_decompress", damaged)
    assert "已损坏" in S.read(None, home, "dsh", DSH_1, env={})[0]["text"]

    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in [
        {"type": "user_message", "text": "把 config.py 的加载逻辑拆出来", "timestamp": 1763800000000},
        {"role": "assistant", "content": [{"type": "text", "text": "已拆到 loader.py。"}], "reasoning": "看一下"},
        {"type": "tool_call", "id": "t1", "name": "shell", "input": {"command": "pytest"}, "output": "ok"},
    ]).encode()
    monkeypatch.setattr(S, "_zstd_decompress", lambda path, limit: body)
    events = S.read(None, home, "dsh", DSH_1, env={})
    assert [e["type"] for e in events] == ["user", "thinking", "message", "tool", "status"]
    assert events[0]["t"] == pytest.approx(1763800000.0)
    assert events[3]["title"] == "pytest" and events[3]["output"] == "ok"


def test_read_index_only_session(home):
    events = S.read(None, home, "dsh", DSH_INPUT, env={})
    assert events[0]["type"] == "error" and "续接" in events[0]["text"]
    assert events[-1]["type"] == "status"


def test_read_max_events_keeps_newest(home):
    events = S.read(None, home, "claude", CLAUDE_1, env={}, max_events=3)
    assert len(events) == 3
    assert [e["type"] for e in events] == ["tool", "message", "status"]
    assert events[-1]["dropped"] == 5
    assert [e["seq"] for e in events] == [1, 2, 3]


def test_read_truncates_long_output(home, monkeypatch):
    monkeypatch.setattr(S, "OUTPUT_MAX", 10)
    events = S.read(None, home, "codex", CODEX_1, env={})
    assert "已截断" in events[2]["output"]


def test_read_unknown_session(home):
    with pytest.raises(LookupError):
        S.read(None, home, "claude", "nope", env={})
    with pytest.raises(LookupError):
        S.read(None, home, "claude", "../../etc/passwd", env={})
    with pytest.raises(LookupError):
        S.read(None, home, "claude", "", env={})


# ------------------------------------------------------------------------------------------ SessionMeta

def test_meta_store(tmp_path):
    root = tmp_path / "root"
    meta = S.SessionMeta(root)
    assert meta.get("claude", "a") == {}
    entry = meta.set("claude", "a", alias="  修  登录 ", pinned=True)
    assert entry["alias"] == "修 登录" and entry["pinned"] is True
    path = root / ".anx" / "agents" / "sessions.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["version"] == 1 and stored["sessions"]["claude:a"]["alias"] == "修 登录"
    assert [p.name for p in path.parent.iterdir()] == ["sessions.json"]  # no temporary files left
    again = S.SessionMeta(root)  # a fresh instance reads the file
    assert again.get("claude", "a")["pinned"] is True
    again.set("claude", "a", alias="", pinned=False)
    assert again.get("claude", "a") == {}  # nothing left: the entry is dropped
    assert "claude:a" not in json.loads(path.read_text(encoding="utf-8"))["sessions"]
    assert meta.get("claude", "a") == {}  # the other instance notices the file changed
    with pytest.raises(ValueError):
        meta.set("claude", "a", alias=123)
    with pytest.raises(ValueError):
        meta.set("claude", "a", alias="x" * 300)
    with pytest.raises(ValueError):
        meta.set("", "a", pinned=True)
    meta.set("claude", "b", hidden=True, bogus=1)
    assert meta.get("claude", "b") == {"hidden": True, "updated": meta.get("claude", "b")["updated"]}
    meta.forget("claude", "b")
    assert meta.get("claude", "b") == {}


def test_meta_damaged_file_is_quarantined(tmp_path):
    path = tmp_path / ".anx" / "agents" / "sessions.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    meta = S.SessionMeta(tmp_path)
    assert meta.all() == {}
    assert (path.parent / "sessions.json.bad").exists()
    meta.set("codex", "x", pinned=True)
    assert meta.get("codex", "x")["pinned"] is True


def test_meta_applied_to_discovery(home, tmp_path):
    meta = S.SessionMeta(tmp_path / "root")
    meta.set("claude", CLAUDE_1, alias="登录修复", archived=True)
    meta.set("claude", CLAUDE_2, hidden=True)
    meta.set("codex", CODEX_ARCHIVED, archived=False)  # un-archive what Codex archived
    found = by_id(S.discover(None, home, env={}, meta=meta))
    assert found[("claude", CLAUDE_1)]["alias"] == "登录修复" and found[("claude", CLAUDE_1)]["archived"] is True
    assert found[("claude", CLAUDE_1)]["title"] == "修复登录页面的跳转错误"  # the CLI's title is kept beside the alias
    assert ("claude", CLAUDE_2) not in found
    assert found[("codex", CODEX_ARCHIVED)]["archived"] is False
    hidden = by_id(S.discover(None, home, env={}, meta=meta, include_hidden=True))
    assert hidden[("claude", CLAUDE_2)]["hidden"] is True


# ------------------------------------------------------------------------------------------ meta_op / deletion

def test_meta_op(home, tmp_path):
    meta = S.SessionMeta(tmp_path / "root")
    assert S.meta_op(None, home, meta, {"kind": "claude"})["code"] == "bad_request"
    assert S.meta_op(None, home, meta, {"kind": "claude", "id": CLAUDE_1})["code"] == "bad_request"
    assert S.meta_op(None, home, meta, {"kind": "claude", "id": CLAUDE_1, "alias": 5})["code"] == "bad_request"
    result = S.meta_op(None, home, meta, {"kind": "claude", "id": CLAUDE_1, "alias": "新名字", "pinned": True})
    assert result["ok"] and result["alias"] == "新名字" and result["pinned"] is True
    assert "error" not in S.meta_op(None, home, meta, {"kind": "claude", "id": CLAUDE_1, "hidden": True})


def test_delete_file_claude(home, tmp_path):
    meta = S.SessionMeta(tmp_path / "root")
    meta.set("claude", CLAUDE_1, pinned=True)
    path = home / ".claude/projects/-home-u-proj" / f"{CLAUDE_1}.jsonl"
    busy = S.meta_op(None, home, meta, {"kind": "claude", "id": CLAUDE_1, "delete_file": True},
                     busy=lambda kind, sid: sid == CLAUDE_1)
    assert busy["code"] == "busy" and path.exists()
    result = S.meta_op(None, home, meta, {"kind": "claude", "id": CLAUDE_1, "delete_file": True})
    assert result == {"ok": True, "deleted": 1}
    assert not path.exists() and path.parent.is_dir()  # the project folder (other sessions) stays
    assert meta.get("claude", CLAUDE_1) == {}
    assert ("claude", CLAUDE_1) not in by_id(S.discover(None, home, env={}))
    again = S.meta_op(None, home, meta, {"kind": "claude", "id": CLAUDE_1, "delete_file": True})
    assert again["code"] == "not_found"


def test_delete_file_dsh_removes_index_and_folder(home, tmp_path):
    deleted = S.delete_files(None, home, "dsh", DSH_1, env={})
    assert len(deleted) == 2
    assert not (home / ".dsh/sessions/--home-u-proj--" / DSH_1).exists()
    assert not (home / ".dsh/storages/session_projcache/sessions" / f"{DSH_1}.json").exists()
    assert (home / ".dsh/storages/session_projcache/sessions" / f"{DSH_INPUT}.json").exists()


def test_delete_never_touches_shared_or_foreign_files(home, tmp_path):
    meta = S.SessionMeta(tmp_path / "root")
    logs = home / ".gemini/tmp" / S.hashlib.sha256(b"/home/u/proj").hexdigest() / "logs.json"
    result = S.meta_op(None, home, meta, {"kind": "gemini", "id": GEMINI_LOGS, "delete_file": True})
    assert result["code"] == "not_found" and logs.exists()
    # deleting a Gemini chat removes the chat file only, never the shared logs.json
    assert len(S.delete_files(None, home, "gemini", GEMINI_1, env={})) == 1 and logs.exists()
    globs = S.kind_globs(None)["claude"]
    assert S._matches_globs(home / ".claude/projects/x/a.jsonl", globs, home, {})
    assert not S._matches_globs(home / ".claude/settings.json", globs, home, {})
    assert not S._matches_globs(home / ".claude/projects/x/y/a.jsonl", globs, home, {})
    assert not S._matches_globs(tmp_path / "elsewhere/a.jsonl", globs, home, {})


def test_delete_respects_given_globs(home, monkeypatch):
    """A file that discovery saw but the registry globs do not cover is never deleted."""
    session = S.find(None, home, "claude", CLAUDE_1, env={})
    monkeypatch.setattr(S, "find", lambda *a, **k: {**session, "own_files": session["own_files"] + [str(home / "secret.txt")]})
    (home / "secret.txt").write_text("keep", encoding="utf-8")
    deleted = S.delete_files(None, home, "claude", CLAUDE_1, env={})
    assert deleted == [session["file"]] and (home / "secret.txt").exists()


def test_parse_budget_lists_names_first(home, monkeypatch):
    monkeypatch.setattr(S, "PARSE_BUDGET_S", -1)  # already over budget: nothing new is parsed
    quick = by_id(S.discover(None, home, kind="claude", env={}, include_blank=True))
    assert ("claude", CLAUDE_1) in quick and quick[("claude", CLAUDE_1)]["title"] == ""
    monkeypatch.setattr(S, "PARSE_BUDGET_S", 5.0)
    full = by_id(S.discover(None, home, kind="claude", env={}))
    assert full[("claude", CLAUDE_1)]["title"] == "修复登录页面的跳转错误"  # the quick answer was not cached


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root to own files as another user")
def test_root_core_only_sees_and_deletes_the_run_as_accounts_own_files(home, tmp_path):
    """anx as root (Ubuntu service) must not list, read or delete a root file the account symlinked in."""
    uid = 54321
    for folder, _dirs, files in os.walk(home):
        os.chown(folder, uid, uid)
        for name in files:
            os.chown(os.path.join(folder, name), uid, uid)
    secret = tmp_path / "rootonly"
    secret.mkdir()
    victim = secret / "aaaaaaaa-1111-4222-8333-444455556666.jsonl"
    victim.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "root secret"},
                                  "sessionId": "aaaaaaaa-1111-4222-8333-444455556666", "cwd": "/root"}) + "\n")
    (home / ".claude" / "projects" / "-evil").symlink_to(secret)
    assert S.required_owner(home) == uid
    found = by_id(S.discover(None, home, env={}, include_blank=True))
    assert ("claude", CLAUDE_1) in found
    assert not any(key[1].startswith("aaaaaaaa") for key in found)
    with pytest.raises(LookupError):
        S.delete_files(None, home, "claude", "aaaaaaaa-1111-4222-8333-444455556666", env={})
    assert victim.exists()
