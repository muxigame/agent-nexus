"""Chat attachments (agent_nexus/agents/attachments.py, docs/agent-chat.md §18): the upload store, how each adapter
delivers them, and the runner's handling (fake CLIs in tests/fixtures/fake_agents)."""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import pytest

from agent_nexus.agents import adapters, attachments, registry, tmpdirs
from agent_nexus.agents.attachments import AttachError, Attachments
from agent_nexus.agents.runner import RunError, Runs

FAKES = Path(__file__).parent / "fixtures" / "fake_agents"
CLAUDE = str(FAKES / "fake_claude.py")
CODEX = str(FAKES / "fake_codex.py")
ACP = str(FAKES / "fake_acp.py")
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==")

posix = pytest.mark.skipif(sys.platform == "win32", reason="fake CLIs run through their shebang; O_NOFOLLOW walks")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@pytest.fixture
def store(tmp_path):
    base = tmp_path / ".anx" / "agents" / "tmp"
    (base / "r1").mkdir(parents=True)
    (base / "r2").mkdir(parents=True)
    return Attachments(base)


def upload(store: Attachments, rid: str, name: str, data: bytes, chunk: int = attachments.CHUNK_MAX) -> dict:
    result, offset, upload_id = None, 0, None
    while True:
        piece = data[offset:offset + chunk]
        result = store.put(rid, {"name": name, "size": len(data), "offset": offset, "data": b64(piece),
                                 **({"id": upload_id} if upload_id else {})})
        upload_id, offset = result["id"], result["have"]
        if result["done"]:
            return result


# ---------------------------------------------------------------------------------------------- the store

def test_names_are_safe_for_every_cli_and_launcher():
    assert attachments.safe_name("../../etc/passwd") == "passwd"
    assert attachments.safe_name("C:\\Users\\me\\a,b.png") == "a_b.png", "codex splits --image on commas"
    assert attachments.safe_name("-rf.txt") == "rf.txt"
    assert attachments.safe_name('50% "off" & <x>|y!.pdf') == "50___off_____x__y_.pdf"
    assert attachments.safe_name(".bashrc") == "bashrc"
    assert attachments.safe_name("CON.txt") == "_CON.txt"
    assert attachments.safe_name("设计 稿.png") == "设计_稿.png"
    assert attachments.safe_name("My  Report 2026.pdf") == "My_Report_2026.pdf"
    assert attachments.safe_name("") == "file"
    long = attachments.safe_name("x" * 400 + ".jpeg")
    assert long.endswith(".jpeg") and len(long) <= attachments.NAME_MAX


def test_chunked_upload_renames_on_the_last_byte_and_dedupes(store, tmp_path):
    data = os.urandom(attachments.CHUNK_MAX * 2 + 17)
    first = upload(store, "r1", "shot.png", data)
    folder = store.base / "r1" / "attachments"
    assert first["path"] == str(folder / "shot.png") and first["mime"] == "image/png"
    assert (folder / "shot.png").read_bytes() == data
    assert not [n for n in os.listdir(folder) if n.endswith(".part")]
    second = upload(store, "r1", "shot.png", b"two")
    assert second["name"] == "shot-2.png"
    empty = upload(store, "r1", "empty.txt", b"")
    assert empty["done"] and Path(empty["path"]).stat().st_size == 0
    # out of order, too big, bad data, too large a file
    started = store.put("r1", {"name": "a.bin", "size": 10, "offset": 0, "data": b64(b"12345")})
    with pytest.raises(AttachError) as caught:
        store.put("r1", {"id": started["id"], "name": "a.bin", "size": 10, "offset": 7, "data": b64(b"x")})
    assert caught.value.code == "bad_offset"
    with pytest.raises(AttachError):
        store.put("r1", {"id": started["id"], "size": 10, "offset": 5, "data": b64(b"123456")})
    with pytest.raises(AttachError):
        store.put("r1", {"name": "b", "size": 3, "offset": 0, "data": "not base64!"})
    with pytest.raises(AttachError) as caught:
        store.put("r1", {"name": "big", "size": attachments.FILE_MAX + 1, "offset": 0, "data": ""})
    assert caught.value.code == "too_large"
    with pytest.raises(AttachError):  # an upload belongs to its folder
        store.put("r2", {"id": started["id"], "size": 10, "offset": 5, "data": b64(b"x")})
    assert store.drop("r1", {"id": started["id"]})["ok"]
    assert not [n for n in os.listdir(folder) if n.endswith(".part")]


def test_check_accepts_only_regular_files_of_the_given_folders(store, tmp_path):
    shot = upload(store, "r1", "shot.png", PNG)
    doc = upload(store, "r1", "doc.pdf", b"%PDF-1.4")
    checked = store.check([shot["path"], {"path": doc["path"]}, shot["path"]], ["r1"])
    assert [f["name"] for f in checked] == ["shot.png", "doc.pdf"]
    assert checked[0] == {"path": shot["path"], "name": "shot.png", "size": len(PNG), "mime": "image/png"}
    assert store.check(None, ["r1"]) == []
    for bad in ([shot["path"]], ):
        with pytest.raises(AttachError):
            store.check(bad, ["r2"])  # another run's upload
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    for path in (str(outside), str(store.base / "r1" / "attachments" / ".." / ".." / ".." / ".." / "secret.txt"),
                 str(store.base / "r1" / "attachments" / "nested" / "x"), "relative.txt", ""):
        with pytest.raises(AttachError):
            store.check([path], ["r1"])
    with pytest.raises(AttachError) as caught:
        store.check([shot["path"]] * (attachments.MESSAGE_FILES + 1), ["r1"])
    assert caught.value.code == "too_many"
    # drop: only inside the folder
    assert store.drop("r1", {"path": doc["path"]})["ok"] and not Path(doc["path"]).exists()
    with pytest.raises(AttachError):
        store.drop("r2", {"path": shot["path"]})


@posix
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs")
def test_a_fifo_swapped_in_never_blocks_the_store(store, tmp_path):
    import threading
    started = store.put("r1", {"name": "a.bin", "size": 10, "offset": 0, "data": b64(b"12345")})
    folder = store.base / "r1" / "attachments"
    part = next(n for n in os.listdir(folder) if n.endswith(".part"))
    os.unlink(folder / part)
    os.mkfifo(folder / part)
    os.mkfifo(folder / "pipe.png")
    done: list = []

    def work():
        with pytest.raises(AttachError):
            store.put("r1", {"id": started["id"], "size": 10, "offset": 5, "data": b64(b"67890")})
        loaded = attachments.load_images([{"path": str(folder / "pipe.png"), "name": "pipe.png", "size": 10}],
                                         attachments.CLAUDE_IMAGE_MAX)
        done.append("data" not in loaded[0])
    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(5)
    assert done == [True], "a FIFO must be refused, not block"
    assert upload(store, "r1", "after.txt", b"ok")["done"], "the store's lock is free again"


def test_links_the_run_as_account_planted_are_never_followed(store, tmp_path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    folder = store.base / "r1" / "attachments"
    folder.mkdir()
    (folder / "link.png").symlink_to(secret)
    with pytest.raises(AttachError):
        store.check([str(folder / "link.png")], ["r1"])
    loaded = attachments.load_images([{"path": str(folder / "link.png"), "name": "link.png", "size": len(PNG)}],
                                     attachments.CLAUDE_IMAGE_MAX)
    assert "data" not in loaded[0]
    # the attachments folder itself swapped for a link: writes refuse it
    (store.base / "r2" / "attachments").symlink_to(tmp_path)
    with pytest.raises(AttachError):
        store.put("r2", {"name": "x.txt", "size": 1, "offset": 0, "data": b64(b"x")})
    assert not (tmp_path / "x.txt").exists()


def test_move_and_load_images(store):
    shot = upload(store, "r1", "shot.png", PNG)
    moved = store.move(store.check([shot["path"]], ["r1"]), "r2")
    assert moved[0]["path"] == str(store.base / "r2" / "attachments" / "shot.png") and not Path(shot["path"]).exists()
    loaded = attachments.load_images(moved, attachments.CLAUDE_IMAGE_MAX)
    assert base64.b64decode(loaded[0]["data"]) == PNG
    assert "data" not in attachments.load_images(moved, 10)[0], "over the limit: named, not inline"
    assert "data" not in attachments.public(loaded)[0]


def test_clean_session_removes_only_that_sessions_inactive_folders(tmp_path):
    dirs = tmpdirs.TmpDirs(tmp_path, active=lambda: {"live"})
    for rid, session in (("a", "s1"), ("b", "s1"), ("live", "s1"), ("c", "s2")):
        dirs.create(rid, kind="claude", session=session)
    assert sorted(dirs.clean_session("s1", "claude")) == ["a", "b"]
    assert {i["rid"] for i in dirs.list()["items"]} == {"live", "c"}
    assert dirs.clean_session("") == [] and dirs.clean_session("s2", "codex") == []


# ---------------------------------------------------------------------------------------------- adapters

def _opts(**kw) -> adapters.AdapterOptions:
    return adapters.AdapterOptions(binary="/bin/x", workdir="/w", **kw)


def test_codex_images_are_one_flag_each_in_the_right_place():
    defs = registry.builtin_definitions()
    files = [{"path": "/t/attachments/a.png", "name": "a.png"}, {"path": "/t/attachments/b.jpg", "name": "b.jpg"},
             {"path": "/t/attachments/notes.pdf", "name": "notes.pdf"}]
    codex = adapters.CodexExec(defs["codex"], _opts())
    codex.turn_files = files
    argv = codex.argv("看看")
    prompt = argv[-1]
    assert argv[-3:-1] == ["--image=/t/attachments/a.png", "--image=/t/attachments/b.jpg"]
    assert prompt.startswith("看看") and "/t/attachments/notes.pdf" in prompt and "a.png" not in prompt
    resumed = adapters.CodexExec(defs["codex"], _opts(resume_id="019a-1"))
    resumed.turn_files = files[:1]
    argv = resumed.argv("again")
    at = argv.index("resume")
    assert argv[at + 1:at + 3] == ["--image=/t/attachments/a.png", "019a-1"] and argv[-1] == "again"


def test_claude_sends_image_blocks_names_the_rest_and_reaches_its_tmpdir():
    defs = registry.builtin_definitions()
    claude = adapters.ClaudeStream(defs["claude"], _opts(tmpdir="/t"))
    assert "--add-dir=/t" in claude.argv()
    claude.turn_files = [{"path": "/t/attachments/a.png", "name": "a.png", "mime": "image/png", "data": b64(PNG)},
                         {"path": "/t/attachments/huge.png", "name": "huge.png", "mime": "image/png"},
                         {"path": "/t/attachments/x.pdf", "name": "x.pdf"}]
    claude.begin_turn("what is this")
    message = json.loads(claude.outbox[-1])["message"]
    text, image = message["content"]
    assert image == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64(PNG)}}
    assert text["text"].startswith("what is this") and "/t/attachments/huge.png" in text["text"] \
        and "/t/attachments/x.pdf" in text["text"] and "a.png" not in text["text"]


def test_acp_sends_images_only_when_the_agent_takes_them_and_queued_turns_keep_them():
    defs = registry.builtin_definitions()
    files = [{"path": "/t/attachments/a.png", "name": "a.png", "mime": "image/png", "data": b64(PNG)}]
    for takes in (True, False):
        acp = adapters.Acp(defs["gemini"], _opts())
        acp.turn_files = files
        acp.begin_turn("hi")  # not ready: queued
        assert acp._queued == [("hi", files)]
        acp.start()
        acp.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"agentCapabilities": {
            "promptCapabilities": {"image": takes}}}}))
        acp.feed(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s"}}))
        prompt = json.loads(acp.outbox[-1])["params"]["prompt"]
        if takes:
            assert prompt[0] == {"type": "text", "text": "hi"}
            assert prompt[1]["type"] == "image" and prompt[1]["data"] == b64(PNG) and prompt[1]["uri"].startswith("file://")
        else:
            assert len(prompt) == 1 and "/t/attachments/a.png" in prompt[0]["text"]


def test_acp_strict_resume_fails_instead_of_starting_afresh():
    defs = registry.builtin_definitions()
    for strict in (False, True):
        acp = adapters.Acp(defs["dsh"], _opts(resume_id="old", strict_resume=strict))
        acp.start()
        acp.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"agentCapabilities": {"loadSession": True}}}))
        acp.feed(json.dumps({"jsonrpc": "2.0", "id": 2, "error": {"message": "session cwd does not match"}}))
        if strict:
            assert acp.failed and "session cwd does not match" in acp.failed and acp.session_id == "old"
        else:
            assert not acp.failed and json.loads(acp.outbox[-1])["method"] == "session/new"


# ---------------------------------------------------------------------------------------------- the runner

@pytest.fixture
def runs(tmp_path):
    instance = Runs(registry.builtin_definitions(), tmp_root=tmp_path / "tmp", stop_grace_s=3)
    yield instance
    instance.close_all()


@pytest.fixture
def staged(tmp_path):
    """An attachments folder shaped like the hub's: ``<base>/<rid>/attachments`` with an image and a text file."""
    base = tmp_path / "anx-tmp"
    (base / "run1").mkdir(parents=True)
    store = Attachments(base)
    shot, notes = upload(store, "run1", "shot.png", PNG), upload(store, "run1", "notes.txt", b"hello")
    return {"base": base, "tmpdir": str(base / "run1"), "files": store.check([shot["path"], notes["path"]], ["run1"])}


async def read_all(runs: Runs, rid: str, count: int = 1) -> list[dict]:
    events, cursor = [], 0
    for _ in range(60):
        result = await runs.read({"rid": rid, "cursor": cursor, "wait": 1})
        events += result["events"]
        cursor = result["cursor"]
        if sum(1 for e in events if e["type"] == "status" and e["state"] in ("done", "failed", "stopped")) >= count:
            return events
    raise AssertionError(json.dumps(events, ensure_ascii=False)[:3000])


def answer(events: list[dict]) -> str:
    return "".join(e["text"] for e in events if e["type"] == "message")


@posix
async def test_claude_gets_the_image_inline_and_an_empty_text_is_fine(runs, tmp_path, staged):
    work = tmp_path / "w"
    work.mkdir()
    log = tmp_path / "argv.jsonl"
    opened = await runs.open({"kind": "claude", "workdir": str(work), "prompt": ""}, binary=CLAUDE, run_as=None,
                             env={"FAKE_ARGV_LOG": str(log)}, tmpdir=staged["tmpdir"], files=staged["files"])
    events = await read_all(runs, opened["rid"])
    user = next(e for e in events if e["type"] == "user")
    assert user["text"] == "" and [a["name"] for a in user["attachments"]] == ["shot.png", "notes.txt"]
    assert all("data" not in a for a in user["attachments"]), "the run's events never carry the bytes"
    text = answer(events)
    assert text.startswith("echo: 请查看附件。") and "notes.txt" in text and "[images=1 image/png]" in text
    assert f"--add-dir={staged['tmpdir']}" in json.loads(log.read_text().splitlines()[0])
    # a send with only attachments, and the runner keeps them for the ACP fallback's re-send
    await runs.send({"rid": opened["rid"], "prompt": "again"}, files=staged["files"][:1])
    events = await read_all(runs, opened["rid"], 2)
    assert answer(events).endswith("again [images=1 image/png]")
    assert runs.runs[opened["rid"]].last_files[0]["name"] == "shot.png"
    with pytest.raises(RunError):
        await runs.send({"rid": opened["rid"], "prompt": "  "})


@posix
async def test_codex_gets_image_flags_and_acp_image_blocks(runs, tmp_path, staged):
    work = tmp_path / "w"
    work.mkdir()
    log = tmp_path / "argv.jsonl"
    opened = await runs.open({"kind": "codex", "workdir": str(work), "prompt": "describe"}, binary=CODEX,
                             run_as=None, env={"FAKE_ARGV_LOG": str(log)}, tmpdir=staged["tmpdir"],
                             files=staged["files"])
    events = await read_all(runs, opened["rid"])
    logged = json.loads(log.read_text().splitlines()[0])
    assert f"--image={staged['files'][0]['path']}" in logged["argv"]
    assert logged["prompt"].startswith("describe") and staged["files"][1]["path"] in logged["prompt"]
    assert "codex: describe" in answer(events)

    acp = await runs.open({"kind": "gemini", "workdir": str(work), "prompt": "look"}, binary=ACP, run_as=None,
                          env={"FAKE_ACP": "image"}, tmpdir=staged["tmpdir"], files=staged["files"])
    text = answer(await read_all(runs, acp["rid"]))
    assert "[images=1 file://" in text and staged["files"][1]["path"] in text
    plain = await runs.open({"kind": "dsh", "workdir": str(work), "prompt": "look"}, binary=ACP, run_as=None,
                            tmpdir=staged["tmpdir"], files=staged["files"])
    text = answer(await read_all(runs, plain["rid"]))
    assert "[images=" not in text and staged["files"][0]["path"] in text, "no image capability: the path is named"


@posix
async def test_strict_resume_fails_the_run_instead_of_a_fresh_session(runs, tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    opened = await runs.open({"kind": "dsh", "workdir": str(work), "prompt": "hi", "resume_id": "old-1"},
                             binary=ACP, run_as=None, env={"FAKE_ACP": "refuse_load"}, strict_resume=True)
    events = await read_all(runs, opened["rid"])
    assert events[-1]["state"] == "failed" and "session cwd does not match" in " ".join(
        e.get("text", "") for e in events if e["type"] == "error")
    assert not any("acp: hi" in e.get("text", "") for e in events)
