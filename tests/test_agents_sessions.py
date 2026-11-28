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


# --- Codex multi-agent: sub-agent threads, forks, one thread id in several rollouts (synthetic content) ---------

PARENT = "019b0000-0000-7000-8000-00000000a001"
SUB_1 = "019b0000-0000-7000-8000-00000000b001"
SUB_2 = "019b0000-0000-7000-8000-00000000b002"
SUB_NESTED = "019b0000-0000-7000-8000-00000000b003"
FORK = "019b0000-0000-7000-8000-00000000c001"
TWICE = "019b0000-0000-7000-8000-00000000d001"


def _cx_rec(kind, payload, stamp="2026-11-25T08:00:00.000Z"):
    return json.dumps({"timestamp": stamp, "type": kind, "payload": payload}, ensure_ascii=False)


def _cx_user(text, stamp="2026-11-25T08:00:01.000Z"):
    # Real rollouts write the response_item first, then its event_msg (see the fixture under tests/fixtures).
    return [_cx_rec("response_item", {"type": "message", "role": "user",
                                   "content": [{"type": "input_text", "text": text}]}, stamp),
            _cx_rec("event_msg", {"type": "user_message", "message": text}, stamp)]


def _cx_answer(text, stamp="2026-11-25T08:00:02.000Z"):
    return [_cx_rec("response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": text}]}, stamp)]


def _rollout(home, day, session_id, meta, lines, when):
    folder = home / ".codex" / "sessions" / "2026" / day[:2] / day[3:]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-{day}T08-00-00-{session_id}.jsonl"
    head = _cx_rec("session_meta", {"id": session_id, "timestamp": f"2026-{day}T08:00:00.000Z", "cwd": "/home/u/plan",
                                 "originator": "Codex Desktop", "cli_version": "0.171.0", **meta})
    path.write_text("\n".join([head] + lines) + "\n", encoding="utf-8")
    touch(path, when)
    return path


def _spawn(parent, nickname, ordinal):
    return {"source": {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1,
                                                     "agent_path": f"/root/{nickname}", "agent_nickname": nickname}}},
            "forked_from_id": parent, "parent_thread_id": parent, "subagent_history_start_ordinal": ordinal,
            "agent_nickname": nickname, "agent_path": f"/root/{nickname}"}


@pytest.fixture
def multi(home):
    # The fork ordinals count the inherited ``response_item`` records (2: one prompt + one answer).
    now = time.time()
    inherited = _cx_user("新版本导入图纸并生成清单") + _cx_answer("好的，先拆分任务")
    _rollout(home, "11-25", PARENT, {"source": "vscode"}, inherited, now - 50)
    _rollout(home, "11-25", SUB_1, _spawn(PARENT, "Gibbs", 2), inherited + _cx_user("只负责解析第 1-3 页"), now - 40)
    _rollout(home, "11-25", SUB_2, _spawn(PARENT, "Gibbs the 2nd", 2), inherited, now - 30)  # nothing of its own yet
    _rollout(home, "11-25", SUB_NESTED, _spawn(SUB_1, "Noether", 3), inherited + _cx_user("只负责解析第 1-3 页")
             + _cx_user("核对第 2 页的尺寸"), now - 20)
    _rollout(home, "11-26", FORK, {"source": "vscode", "forked_from_id": PARENT, "history_base": 2},
             inherited + _cx_user("换一种方式：先导出 CSV"), now - 10)
    first = _cx_user("整理会议纪要", "2026-10-13T08:00:01.000Z") + _cx_answer("已整理", "2026-10-13T08:00:02.000Z")
    _rollout(home, "10-13", TWICE, {"source": "cli"}, first, now - 400)
    _rollout(home, "11-25", TWICE, {"source": "cli"},
             first + _cx_user("再加一节待办", "2026-11-25T09:00:01.000Z") + _cx_answer("已加上", "2026-11-25T09:00:02.000Z"),
             now - 5)
    return home


def test_codex_subagents_fold_into_their_parent(multi):
    items = S.discover(None, multi, kind="codex", env={})
    ids = [s["id"] for s in items]
    assert SUB_1 not in ids and SUB_2 not in ids and SUB_NESTED not in ids
    assert ids.count(PARENT) == 1
    parent = by_id(items)[("codex", PARENT)]
    assert parent["subagents"] == 3  # the nested one counts towards the top-level thread
    assert parent["title"] == "新版本导入图纸并生成清单"
    assert "subagents" not in by_id(items)[("codex", CODEX_1)]
    every = by_id(S.discover(None, multi, kind="codex", env={}, include_subagents=True))
    sub = every[("codex", SUB_1)]
    assert sub["parent_id"] == PARENT and sub["agent_nickname"] == "Gibbs"
    assert sub["title"] == "只负责解析第 1-3 页"  # its own first prompt, not the inherited one
    assert every[("codex", SUB_NESTED)]["title"] == "核对第 2 页的尺寸"
    assert every[("codex", SUB_2)]["title"] == "新版本导入图纸并生成清单"  # nothing of its own: the inherited one


def test_codex_subagent_history_still_opens_by_id(multi):
    events = S.read(None, multi, "codex", SUB_1, env={})
    users = [e["text"] for e in events if e["type"] == "user"]
    assert users[-1] == "只负责解析第 1-3 页"


def test_codex_fork_title_comes_after_the_fork_point(multi):
    fork = by_id(S.discover(None, multi, kind="codex", env={}))[("codex", FORK)]
    assert fork["title"] == "换一种方式：先导出 CSV" and fork["forked_from"] == PARENT
    assert "parent_id" not in fork  # a fork is a session of its own


def test_codex_one_entry_per_thread_id_across_rollouts(multi):
    items = S.discover(None, multi, kind="codex", env={})
    assert [s["id"] for s in items].count(TWICE) == 1
    entry = by_id(items)[("codex", TWICE)]
    assert len(entry["files"]) == 2 and "11-25" in entry["file"].replace("\\", "/")  # the newest rollout
    assert entry["title"] == "整理会议纪要"
    assert entry["updated_at"] >= max(Path(f).stat().st_mtime for f in entry["files"]) - 1
    assert items[0]["id"] == TWICE  # sorted by its newest activity
    events = S.read(None, multi, "codex", TWICE, env={})
    users = [e["text"] for e in events if e["type"] == "user"]
    assert users == ["整理会议纪要", "再加一节待办"]  # the copied history once, then the continuation
    assert S.codex_rollout(multi, {}, TWICE) == Path(entry["file"])


def test_codex_join_appends_a_continuation_only_rollout(multi):
    # A second rollout holding only the new turns is appended after the older one.
    _rollout(multi, "11-27", TWICE, {"source": "cli"}, _cx_user("第三轮", "2026-11-27T09:00:01.000Z"), time.time())
    events = S.read(None, multi, "codex", TWICE, env={})
    assert [e["text"] for e in events if e["type"] == "user"] == ["整理会议纪要", "再加一节待办", "第三轮"]
    S.clear_cache()
    entry = by_id(S.discover(None, multi, kind="codex", env={}))[("codex", TWICE)]
    assert entry["title"] == "整理会议纪要"  # the oldest rollout names the thread, not the continuation


def test_codex_join_skips_a_retimestamped_copy_of_the_history(home):
    # The newer rollout copies the earlier turns with new timestamps: they are shown once.
    old = _cx_user("第一问", "2026-10-13T08:00:01.000Z") + _cx_answer("答", "2026-10-13T08:00:02.000Z")
    new = (_cx_user("第一问", "2026-11-25T08:00:01.000Z") + _cx_answer("答", "2026-11-25T08:00:02.000Z")
           + _cx_user("第二问", "2026-11-25T08:00:03.000Z"))
    _rollout(home, "10-13", TWICE, {"source": "cli"}, old, time.time() - 100)
    _rollout(home, "11-25", TWICE, {"source": "cli"}, new, time.time())
    events = S.read(None, home, "codex", TWICE, env={})
    assert [e["text"] for e in events if e["type"] == "user"] == ["第一问", "第二问"]


def test_codex_join_keeps_real_repeats_and_is_bounded(home, monkeypatch):
    lines = _cx_user("再试一次") + _cx_answer("失败") + _cx_user("再试一次") + _cx_answer("失败")
    _rollout(home, "10-13", TWICE, {"source": "cli"}, lines[:3], time.time() - 100)
    _rollout(home, "11-25", TWICE, {"source": "cli"}, lines, time.time())
    events = S.read(None, home, "codex", TWICE, env={})
    assert [e["text"] for e in events if e["type"] == "user"] == ["再试一次", "再试一次"]
    files = [Path(f) for f in by_id(S.discover(None, home, kind="codex", env={}))[("codex", TWICE)]["files"]]
    monkeypatch.setattr(S, "READ_BYTES", 600)
    data, dropped = S._codex_join(files)
    assert len(data) <= 601 and dropped > 0


def test_codex_fork_title_when_the_inherited_history_ends_with_a_prompt(home):
    # The last inherited record is a prompt: its event_msg (written after the response_item) sits at the fork
    # point and must not name the fork.
    now = time.time()
    _rollout(home, "11-25", PARENT, {"source": "vscode"}, _cx_user("父问题"), now - 50)
    _rollout(home, "11-26", FORK, {"source": "vscode", "forked_from_id": PARENT, "history_base": 1},
             _cx_user("父问题") + _cx_answer("答") + _cx_user("自己的"), now - 10)
    assert by_id(S.discover(None, home, kind="codex", env={}))[("codex", FORK)]["title"] == "自己的"


def test_codex_fork_scan_title_when_the_inherited_history_ends_with_a_prompt(home, monkeypatch):
    big = _cx_answer("x" * 3000)
    _rollout(home, "11-26", FORK, {"source": "vscode", "forked_from_id": PARENT, "history_base": 4},
             big * 3 + _cx_user("父问题") + big * 3 + _cx_user("自己的"), time.time())
    monkeypatch.setattr(S, "FULL_SCAN_BYTES", 1000)
    monkeypatch.setattr(S, "HEAD_BYTES", 4000)
    monkeypatch.setattr(S, "TAIL_BYTES", 4000)
    S.clear_cache()
    assert by_id(S.discover(None, home, kind="codex", env={}))[("codex", FORK)]["title"] == "自己的"


def test_codex_large_subagent_is_not_scanned_for_its_title(multi, monkeypatch):
    big = _cx_answer("x" * 3000)
    _rollout(multi, "11-25", SUB_2, _spawn(PARENT, "Gibbs the 2nd", 7),
             _cx_user("新版本导入图纸并生成清单") + big * 6 + _cx_user("子代理自己的"), time.time())
    monkeypatch.setattr(S, "FULL_SCAN_BYTES", 1000)
    monkeypatch.setattr(S, "HEAD_BYTES", 4000)
    monkeypatch.setattr(S, "TAIL_BYTES", 4000)
    S.clear_cache()
    calls = []
    real = S._codex_scan_title
    monkeypatch.setattr(S, "_codex_scan_title", lambda *a, **k: calls.append(a) or real(*a, **k))
    every = by_id(S.discover(None, multi, kind="codex", env={}, include_subagents=True))
    assert every[("codex", SUB_2)]["parent_id"] == PARENT and not calls


def test_codex_orphan_subagent_is_listed(multi):
    orphan = "019b0000-0000-7000-8000-00000000f001"
    _rollout(multi, "11-25", orphan, _spawn("019b0000-0000-7000-8000-0000000000ff", "Lost", 0), _cx_user("孤儿任务"),
             time.time())
    entry = by_id(S.discover(None, multi, kind="codex", env={}))[("codex", orphan)]
    assert entry["orphan"] and entry["title"] == "孤儿任务"


def test_codex_fork_title_in_a_large_rollout(multi, monkeypatch):
    # Past the head (a big file): the streaming scan keeps counting the inherited records.
    big = _cx_answer("x" * 3000)
    _rollout(multi, "11-26", FORK, {"source": "vscode", "forked_from_id": PARENT, "history_base": 4},
             _cx_user("旧问题") + big * 3 + _cx_user("分叉后的新问题") + big * 3, time.time())
    monkeypatch.setattr(S, "FULL_SCAN_BYTES", 1000)
    monkeypatch.setattr(S, "HEAD_BYTES", 4000)
    monkeypatch.setattr(S, "TAIL_BYTES", 4000)
    S.clear_cache()
    fork = by_id(S.discover(None, multi, kind="codex", env={}))[("codex", FORK)]
    assert fork["title"] == "分叉后的新问题"


def test_codex_subagents_do_not_crowd_out_the_window(multi):
    # limit counts top-level threads: the sub-agent rollouts (newer than the parent) do not use up the window.
    for n in range(30):
        _rollout(multi, "11-25", f"019b0000-0000-7000-8000-0000000e{n:04d}", _spawn(PARENT, f"A{n}", 2), [], time.time())
    S.clear_cache()
    items = S.discover(None, multi, kind="codex", env={}, limit=1)
    assert PARENT in [s["id"] for s in S.discover(None, multi, kind="codex", env={}, limit=3)]
    assert len(items) == 1


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
    assert kinds == ["user", "thinking", "tool", "tool", "file_change", "tool", "message", "usage", "status"]
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
    assert kinds == ["user", "thinking", "tool", "tool", "file_change", "file_change", "tool", "message", "usage",
                     "status"]
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



# ------------------------------------------------------------------------------------------ context usage (§16.3)

def test_read_claude_ends_with_the_last_context_usage(home):
    events = S.read(None, home, "claude", CLAUDE_1, env={})
    usage = events[-2]
    # the last main-chain assistant message: input + cache read + cache creation + output; the model carries over
    assert usage["type"] == "usage" and usage["context"] == {"used": 1730, "limit": 200000,
                                                            "model": "claude-sonnet-4-5"}
    assert usage["turn"] == {"input": 1700, "output": 30}
    assert usage["t"] == events[-1]["t"]


def test_read_claude_context_over_200k_is_the_1m_window(home, tmp_path):
    folder = home / ".claude" / "projects" / "-home-u-big"
    folder.mkdir(parents=True)
    sid = "12345678-1234-4234-8234-123456789abc"
    records = [
        {"type": "user", "uuid": "u1", "cwd": "/home/u/big", "sessionId": sid, "timestamp": "2026-11-20T10:00:00Z",
         "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "uuid": "a1", "sessionId": sid, "timestamp": "2026-11-20T10:00:01Z",
         "message": {"id": "m1", "model": "claude-opus-4-5", "role": "assistant",
                     "content": [{"type": "text", "text": "ok"}],
                     "usage": {"input_tokens": 5, "cache_read_input_tokens": 300000, "output_tokens": 10}}},
        {"type": "assistant", "uuid": "a2", "sessionId": sid, "isSidechain": True, "timestamp": "2026-11-20T10:00:02Z",
         "message": {"id": "m2", "model": "claude-haiku-4-5", "content": [{"type": "text", "text": "sub"}],
                     "usage": {"input_tokens": 9}}},
        {"type": "assistant", "uuid": "a3", "sessionId": sid, "timestamp": "2026-11-20T10:00:03Z",
         "message": {"id": "m3", "model": "<synthetic>", "content": [{"type": "text", "text": "No response"}],
                     "usage": {"input_tokens": 0, "output_tokens": 0}}},
    ]
    (folder / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in records))
    usage = next(e for e in S.read(None, home, "claude", sid, env={}) if e["type"] == "usage")
    assert usage["context"] == {"used": 300015, "limit": 1000000, "model": "claude-opus-4-5"}
    assert usage["turn"] == {"input": 300005, "output": 10, "cached": 300000}


def test_read_codex_ends_with_the_last_token_count(home):
    events = S.read(None, home, "codex", CODEX_1, env={})
    usage = events[-2]
    # last_token_usage; no model_context_window in this rollout: the built-in table for the turn_context's model
    assert usage["context"] == {"used": 110, "limit": 272000, "model": "gpt-5-codex"}
    assert usage["turn"] == {"input": 100, "output": 10}


def test_sessions_without_usage_get_no_usage_event(home):
    for kind, sid in (("gemini", GEMINI_1), ("dsh", "acp-demo-1"), ("dsh", DSH_INPUT)):
        assert not [e for e in S.read(None, home, kind, sid, env={}) if e["type"] == "usage"]


def test_codex_context_reads_the_rollout_tail(home, monkeypatch):
    path = S.codex_rollout(home, {}, CODEX_1)
    assert path is not None and path.name.endswith(f"{CODEX_1}.jsonl")
    assert S.codex_context(home, {}, CODEX_1) == {"used": 110, "limit": None, "model": "gpt-5-codex",
                                                  "turn": {"input": 100, "output": 10}}
    with path.open("a") as handle:
        handle.write("x" * 300 + "\n")
        handle.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 9000, "cached_input_tokens": 8000, "output_tokens": 100},
            "model_context_window": 258400}}}) + "\n")
    monkeypatch.setattr(S, "ROLLOUT_TAIL_BYTES", 400)  # only the tail: the turn_context is out of reach
    assert S.codex_context(home, {}, CODEX_1) == {"used": 9100, "limit": 258400, "model": None,
                                                  "turn": {"input": 9000, "output": 100, "cached": 8000}}
    # CODEX_HOME moves the rollouts; bad or unknown ids find nothing
    moved = home.parent / "codex-home"
    shutil.move(str(home / ".codex"), str(moved))
    assert S.codex_rollout(home, {}, CODEX_1) is None
    assert S.codex_rollout(home, {"CODEX_HOME": str(moved)}, CODEX_1) is not None
    assert S.codex_rollout(home, {"CODEX_HOME": str(moved)}, "../../etc") is None
    assert S.codex_rollout(home, {"CODEX_HOME": str(moved)}, "*") is None
    assert S.codex_context(home, {"CODEX_HOME": str(moved)}, "019a0000-0000-7000-8000-000000000000") is None


def test_context_window_table():
    assert S.context_window("gpt-5-codex") == 272000
    assert S.context_window("gpt-5.1-codex-max") == 272000
    assert S.context_window("openai/gpt-4.1-mini") == 1047576
    assert S.context_window("claude-sonnet-4-5[1m]") == 1000000
    assert S.context_window("claude-sonnet-4-5", 250000) == 1000000
    assert S.context_window("claude-sonnet-4-5", 1000) == 200000
    assert S.context_window("mystery") is None and S.context_window(None) is None

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
    events = S.read(None, home, "claude", CLAUDE_1, env={}, max_events=4)
    assert len(events) == 4
    assert [e["type"] for e in events] == ["tool", "message", "usage", "status"]  # the usage event is always kept
    assert events[-1]["dropped"] == 5
    assert [e["seq"] for e in events] == [1, 2, 3, 4]


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


def _codex_line(kind: str, payload: dict) -> str:
    return json.dumps({"timestamp": "2026-11-20T08:00:00Z", "type": kind, "payload": payload}, ensure_ascii=False)


def _user(text: str) -> dict:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def test_codex_desktop_fork_title_past_a_huge_compacted_record(tmp_path):
    # Codex desktop forks: two session_meta lines, then a multi-MB ``compacted`` record (seen on a real device), so
    # the session's own first prompt lies past the head and before the tail.
    sid = "019a0000-0000-7000-8000-00000000c0de"
    root = tmp_path / ".codex" / "sessions" / "2026" / "11" / "20"
    root.mkdir(parents=True)
    filler = "x" * (S.HEAD_BYTES + 1024)
    history = [_user("<environment_context>cwd</environment_context>"), _user("父会话的第一个问题")] + \
        [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": filler}]}]
    lines = [_codex_line("session_meta", {"id": sid, "cwd": str(tmp_path)}),
             _codex_line("session_meta", {"id": "parent"}),
             _codex_line("compacted", {"message": "", "replacement_history": history}),
             _codex_line("response_item", _user("<image>")),
             _codex_line("response_item", _user("分叉后的新问题")),
             _codex_line("response_item", {"type": "reasoning", "summary": [], "content": None,
                                           "encrypted_content": "y" * (S.TAIL_BYTES + 1024)})]
    path = root / f"rollout-2026-11-20T08-00-00-{sid}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    found = by_id(S.discover(None, tmp_path, kind="codex", env={}))
    assert found[("codex", sid)]["title"] == "分叉后的新问题"
    assert found[("codex", sid)]["cwd"] == str(tmp_path)

    # no prompt of its own yet: fall back to the compacted history's first plain prompt
    S._CACHE.clear()
    path.write_text("\n".join(lines[:4] + lines[5:]) + "\n", encoding="utf-8")
    found = by_id(S.discover(None, tmp_path, kind="codex", env={}))
    assert found[("codex", sid)]["title"] == "父会话的第一个问题"


def test_read_dsh_v4_records(tmp_path):
    # record shapes from a real DeepSeek Harness 0.2.2 session.v4 (plain .jsonl here; .zstd is the same text)
    sid = "16e88bcd-3cb3-49f4-84b0-a0884471fd7a"
    folder = tmp_path / ".dsh" / "sessions" / "--C-w--" / sid
    folder.mkdir(parents=True)
    recs = [
        {"type": "session", "version": 4, "id": sid, "createdAt": 1795718648661, "cwd": "C:\\w"},
        {"type": "system/message", "seq": 7, "time": 1795718648700,
         "data": {"message": {"role": "system", "content": [{"type": "text", "text": "You are DSH"}]}}},
        {"type": "user/message", "seq": 8, "time": 1795718648857,
         "data": {"content": [{"type": "text", "text": "读 a.txt"},
                              {"type": "resource", "resource": {"uri": "anx://system-prompt", "text": "SYS"}}],
                  "source": {"kind": "user"}, "role": "user"}},
        {"type": "user/message", "seq": 9, "time": 1795718648858,
         "data": {"content": [{"type": "text", "text": "<system-reminder>cwd</system-reminder>"}],
                  "source": {"kind": "runtime-context"}, "role": "user"}},
        {"type": "assistant/message", "seq": 15, "time": 1795718649315,
         "data": {"message": {"role": "assistant", "content": [
             {"type": "reasoning", "text": "use read"},
             {"type": "tool-call", "id": "call_1", "name": "read", "arguments": "{\"file_path\": \"a.txt\"}"}]}}},
        {"type": "tool/call", "seq": 16, "time": 1795718649316, "data": {"callId": "call_1", "name": "read"}},
        {"type": "tool/result", "seq": 17, "time": 1795718649325,
         "data": {"message": {"role": "tool", "toolCallId": "call_1", "content": [{"type": "text", "text": "hello"}],
                              "isError": False}}},
        {"type": "assistant/message", "seq": 21, "time": 1795718649846,
         "data": {"message": {"role": "assistant", "content": [{"type": "reasoning", "text": ""},
                                                                 {"type": "text", "text": "hello"}]}}},
        {"type": "turn/end", "seq": 23, "time": 1795718649847, "data": {"reason": {"kind": "completed"}}},
    ]
    (folder / "session.v4.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    events = S.read(None, tmp_path, "dsh", sid, env={})
    events = events.get("events", events) if isinstance(events, dict) else events
    shape = [(e["type"], e.get("name"), e.get("text")) for e in events if e["type"] != "status"]
    assert shape == [("user", None, "读 a.txt"), ("thinking", None, "use read"), ("tool", "read", None),
                     ("message", None, "hello")]
    tool = next(e for e in events if e["type"] == "tool")
    assert tool["status"] == "ok" and tool["output"] == "hello" and tool["input"] == {"file_path": "a.txt"}


def test_console_started_session_titled_by_its_first_prompt(home, tmp_path):
    # dsh titles a session from its first user message, which (over ACP) carries the system prompt in front
    meta = S.SessionMeta(tmp_path / "root")
    meta.set("dsh", DSH_1, managed=True, first_prompt="  修复   登录页 ")
    meta.set("dsh", DSH_1, first_prompt="later message")  # kept once set
    found = by_id(S.discover(None, home, kind="dsh", env={}, meta=meta))
    assert found[("dsh", DSH_1)]["title"] == "修复 登录页"


def test_codex_fork_that_points_into_its_parent_is_titled_without_a_scan(home, monkeypatch):
    # The shape of a real fork (Codex desktop 0.171): history_base points into the parent's file (thread id,
    # ordinal and byte offset there) and forked_from_ordinal_exclusive counts in the parent; the fork's own
    # records start at once, its first prompt included.
    big = _cx_answer("x" * 3000)
    meta = {"source": "vscode", "forked_from_id": PARENT, "forked_from_ordinal_exclusive": 4980,
            "history_base": {"thread_id": PARENT, "end_ordinal_exclusive": 89600, "end_byte_offset": 388404484}}
    _rollout(home, "11-26", FORK, meta, _cx_user("分叉自己的问题") + big * 6, time.time())
    monkeypatch.setattr(S, "FULL_SCAN_BYTES", 1000)
    monkeypatch.setattr(S, "HEAD_BYTES", 4000)
    monkeypatch.setattr(S, "TAIL_BYTES", 4000)
    S.clear_cache()
    calls = []
    real = S._codex_scan_title
    monkeypatch.setattr(S, "_codex_scan_title", lambda *a, **k: calls.append(a) or real(*a, **k))
    fork = by_id(S.discover(None, home, kind="codex", env={}))[("codex", FORK)]
    assert fork["title"] == "分叉自己的问题" and not calls


# ------------------------------------------------------------------------------------------ moving a session (§19)

def test_claude_project_folder_names_match_the_cli():
    # the rule read from Claude Code 2.1.332: [^A-Za-z0-9] -> "-", cut at 200 plus "-" + base36(|java hash|)
    assert S.claude_project_name("/home/user/agent-nexus") == "-home-user-agent-nexus"
    assert S.claude_project_name("D:\\work\\proj") == "D--work-proj"
    assert S.claude_project_name("/x/é_b") == "-x---b"
    assert S.claude_project_name("/x/😀b") == "-x---b", "UTF-16: a character outside the BMP is two dashes"
    # node -e 'h=0;for(c of ...)...' on the same strings: feo44x and nzr6rt
    long = S.claude_project_name("/" + "a" * 250)
    assert len(long) == 200 + 7 and long.endswith("-feo44x")
    assert S.claude_project_name("/x/😀" + "c" * 240).endswith("-nzr6rt")
    assert S.claude_project_dir(Path("/h"), "/w", {}) == Path("/h/.claude/projects/-w")
    assert S.claude_project_dir(Path("/h"), "/w", {"CLAUDE_CONFIG_DIR": "/cfg"}) == Path("/cfg/projects/-w")


def test_listing_takes_the_folder_of_the_latest_records(home):
    claude = home / ".claude" / "projects" / "-home-u-proj" / f"{CLAUDE_1}.jsonl"
    with claude.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "user", "cwd": "/home/u/moved", "sessionId": CLAUDE_1,
                                 "timestamp": "2026-11-21T10:00:00.000Z",
                                 "message": {"role": "user", "content": "go on"}}) + "\n")
    rollout = next((home / ".codex" / "sessions").rglob(f"*{CODEX_1}.jsonl"))
    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(_codex_line("turn_context", {"cwd": "/home/u/elsewhere", "model": "gpt-5"}) + "\n")
    found = by_id(S.discover(None, home, env={}))
    assert found[("claude", CLAUDE_1)]["cwd"] == "/home/u/moved"
    assert set(found[("claude", CLAUDE_1)]["cwds"]) == {"/home/u/moved", "/home/u/proj"}
    assert found[("codex", CODEX_1)]["cwd"] == "/home/u/elsewhere"
    assert "/home/u/proj" in found[("codex", CODEX_1)]["cwds"]


def test_relocate_claude_copies_and_the_merge_lists_the_newest_folder(home, tmp_path):
    original = home / ".claude" / "projects" / "-home-u-proj" / f"{CLAUDE_1}.jsonl"
    (original.parent / CLAUDE_1 / "tool-results").mkdir(parents=True)
    (original.parent / CLAUDE_1 / "tool-results" / "r1.txt").write_text("out")
    before = original.read_bytes()
    session = S.find(None, home, "claude", CLAUDE_1, env={})
    new = tmp_path / "new place"
    new.mkdir()
    copy = S.relocate_claude(home, session, str(new), env={})
    assert copy == home / ".claude" / "projects" / S.claude_project_name(os.path.realpath(new)) / f"{CLAUDE_1}.jsonl"
    assert copy.read_bytes() == before and original.read_bytes() == before
    assert (copy.parent / CLAUDE_1 / "tool-results" / "r1.txt").read_text() == "out"
    assert not [p for p in copy.parent.iterdir() if p.name.endswith(".tmp")]
    # the CLI appends to the copy: the listing follows it, the original stays as it was
    with copy.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "user", "cwd": str(new), "sessionId": CLAUDE_1,
                                 "timestamp": "2026-11-29T10:00:00.000Z",
                                 "message": {"role": "user", "content": "继续"}}) + "\n")
    touch(copy, time.time() + 5)
    S.clear_cache()
    listed = S.find(None, home, "claude", CLAUDE_1, env={})
    assert listed["cwd"] == str(new) and listed["file"] == str(copy) and len(listed["files"]) == 2
    assert S.relocate_claude(home, listed, str(new), env={}) == copy, "a second move into the folder copies nothing"
    assert original.read_bytes() == before
    assert S.same_dir(str(new) + "/", str(new)) and not S.same_dir(str(new), str(tmp_path))


@pytest.mark.skipif(os.name == "nt", reason="POSIX links; Node keeps a junction's path on Windows")
def test_relocate_claude_through_a_linked_folder_uses_the_physical_path(home, tmp_path):
    """Claude Code names the project folder after process.cwd(), which resolves links."""
    session = S.find(None, home, "claude", CLAUDE_1, env={})
    real = tmp_path / "real" / "work"
    real.mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "real")
    logical = str(tmp_path / "link" / "work")
    copy = S.relocate_claude(home, session, logical, env={})
    projects = home / ".claude" / "projects"
    assert copy == projects / S.claude_project_name(os.path.realpath(real)) / f"{CLAUDE_1}.jsonl"
    assert copy.is_file() and not (projects / S.claude_project_name(logical)).exists()


def test_relocate_claude_as_root_never_follows_the_accounts_links(home, tmp_path):
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        pytest.skip("needs root (the system service case)")
    uid = 54321
    for folder, _dirs, files in os.walk(home):
        os.chown(folder, uid, uid)
        for name in files:
            os.chown(os.path.join(folder, name), uid, uid)
    session = S.find(None, home, "claude", CLAUDE_1, env={})
    work = home / "proj"
    work.mkdir()
    os.chown(work, uid, uid)
    copy = S.relocate_claude(home, session, str(work), env={})
    assert copy.stat().st_uid == uid and copy.parent.stat().st_uid == uid
    # the account points the target project folder at a root-only place: refused, nothing written there
    secret = tmp_path / "rootonly"
    secret.mkdir()
    other = home / "other"
    other.mkdir()
    (home / ".claude" / "projects" / S.claude_project_name(str(other))).symlink_to(secret)
    with pytest.raises(OSError):
        S.relocate_claude(home, session, str(other), env={})
    assert not list(secret.iterdir())
