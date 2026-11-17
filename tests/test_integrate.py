"""``anx hook`` payloads and ``anx integrate claude|codex`` (design §8.3).

Every file lives under a temporary HOME / USERPROFILE / CLAUDE_CONFIG_DIR /
CODEX_HOME: the developer's real ~/.claude and ~/.codex are never read or
written here. Payloads and transcripts are synthetic.
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import tomllib
from pathlib import Path

import pytest

from agent_nexus import cli, integrate
from agent_nexus.hooks import claude_home, codex_home, first_line, hook_payload

ANX = ["C:\\Tools\\anx\\anx.exe", "--root", "C:\\anx-root"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(root / ".codex"))
    assert claude_home() == root / ".claude" and codex_home() == root / ".codex"
    assert Path(os.path.expanduser("~")) == root
    return root


def _jsonl(path: Path, records: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


# -- hook payloads ----------------------------------------------------------------------------

def test_claude_title_sources(tmp_path, home):
    transcript = _jsonl(tmp_path / "s.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "hello"}},
        {"type": "ai-title", "aiTitle": "Generated title", "sessionId": "s-1"},
        {"type": "agent-name", "agentName": "helper", "sessionId": "s-1"},
        {"type": "custom-title", "customTitle": "Old name", "sessionId": "s-1"},
        "not an object",
        {"type": "custom-title", "customTitle": "Renamed session", "sessionId": "s-1"},
    ])
    base = {"session_id": "s-1", "cwd": str(tmp_path), "transcript_path": str(transcript),
            "hook_event_name": "UserPromptSubmit", "prompt": "Fix the build\nsecond line"}
    assert hook_payload("claude", json.dumps(base)) == {
        "kind": "claude", "cwd": str(tmp_path), "event": "UserPromptSubmit", "session_id": "s-1",
        "title": "Renamed session", "title_source": "session"}
    assert hook_payload("claude", json.dumps({**base, "session_title": "From stdin"}))["title"] == "From stdin"
    only_ai = _jsonl(tmp_path / "a.jsonl", [{"type": "ai-title", "aiTitle": "Generated", "sessionId": "s"}])
    assert hook_payload("claude", json.dumps({**base, "transcript_path": str(only_ai)}))["title"] == "Generated"
    no_title = {**base, "transcript_path": str(tmp_path / "missing.jsonl")}
    payload = hook_payload("claude", json.dumps(no_title))
    assert payload["title"] == "Fix the build" and payload["title_source"] == "prompt"
    slash = hook_payload("claude", json.dumps({**no_title, "prompt": "<command-name>/clear</command-name>"}))
    assert "title" not in slash
    start = hook_payload("claude", json.dumps({"session_id": "s-1", "cwd": "/p", "hook_event_name": "SessionStart",
                                               "source": "startup"}), "SessionStart")
    assert start == {"kind": "claude", "cwd": "/p", "event": "SessionStart", "session_id": "s-1"}
    assert hook_payload("claude", "not json") is None and hook_payload("claude", "[1]") is None


def test_large_transcript_is_read_at_head_and_tail(tmp_path, home, monkeypatch):
    from agent_nexus import hooks
    monkeypatch.setattr(hooks, "WHOLE_FILE_BYTES", 1000)
    monkeypatch.setattr(hooks, "HEAD_TAIL_BYTES", 300)
    records = [{"type": "custom-title", "customTitle": "early", "sessionId": "s"}]
    records += [{"type": "assistant", "pad": "x" * 50} for _ in range(100)]
    records += [{"type": "ai-title", "aiTitle": "late", "sessionId": "s"}]
    path = _jsonl(tmp_path / "big.jsonl", records)
    assert hooks.claude_title({"transcript_path": str(path)}) == ("early", "session")


def test_codex_notify_and_hook_payloads(tmp_path, home):
    _jsonl(home / ".codex" / "session_index.jsonl", [
        {"id": "t-1", "thread_name": "First name", "updated_at": "2026-11-10T08:00:00Z"},
        {"id": "t-2", "thread_name": "Other", "updated_at": "2026-11-10T08:00:00Z"},
        {"id": "t-1", "thread_name": "Current name", "updated_at": "2026-11-10T09:00:00Z"},
    ])
    notify = {"type": "agent-turn-complete", "thread-id": "t-1", "turn-id": "u-1", "cwd": "/repo",
              "client": "codex-tui", "input-messages": ["Refactor the parser"], "last-assistant-message": "done"}
    assert hook_payload("codex", json.dumps(notify)) == {
        "kind": "codex", "cwd": "/repo", "event": "turn", "session_id": "t-1",
        "title": "Current name", "title_source": "session"}
    unknown = hook_payload("codex", json.dumps({**notify, "thread-id": "t-9"}))
    assert unknown["title"] == "Refactor the parser" and unknown["title_source"] == "prompt"
    hook = {"session_id": "t-3", "cwd": "/repo", "hook_event_name": "UserPromptSubmit", "prompt": "Add tests",
            "turn_id": "u", "model": "m", "permission_mode": "default", "transcript_path": None}
    payload = hook_payload("codex", json.dumps(hook))
    assert payload["event"] == "UserPromptSubmit" and payload["session_id"] == "t-3" and payload["title"] == "Add tests"


def test_first_line():
    assert first_line("\n  hi there \nmore") == "hi there" and first_line("") is None and first_line(None) is None
    assert len(first_line("x" * 1000)) == 200


def test_hook_command_never_fails_or_prints(tmp_path, home, monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(cli.Api, "call", lambda self, method, path, body=None, timeout=60: sent.append(
        (method, path, body, timeout)) or (200, {}))
    payload = json.dumps({"session_id": "s-1", "cwd": "/p", "hook_event_name": "UserPromptSubmit",
                          "prompt": "Ship it"})
    assert cli.main(["--root", str(tmp_path), "hook", "claude", payload]) == 0
    assert sent == [("POST", "/api/agents/presence", {"kind": "claude", "cwd": "/p", "event": "UserPromptSubmit",
                                                       "session_id": "s-1", "title": "Ship it",
                                                       "title_source": "prompt"}, cli.HOOK_POST_TIMEOUT_S)]
    # Bad arguments, a failing manager, garbage input: still 0, still silent.
    assert cli.main(["hook", "claude", "--no-such-flag"]) == 0
    assert cli.main(["hook", "gemini"]) == 0
    monkeypatch.setattr(cli.Api, "call", lambda *a, **k: (_ for _ in ()).throw(OSError("refused")))
    assert cli.main(["--root", str(tmp_path), "hook", "codex", "{not json"]) == 0
    assert cli.main(["--root", str(tmp_path), "hook", "codex", payload]) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    assert cli._is_hook_call(["--root", "x", "hook", "claude"]) and not cli._is_hook_call(["plugin", "hook"])


def test_hook_stdin_that_never_closes_does_not_hold_the_hook(monkeypatch):
    release = threading.Event()

    class Stuck:
        def read(self, size=-1):
            release.wait(10)
            return b""

    class Stdin:
        buffer = Stuck()

        @staticmethod
        def isatty():
            return False

    monkeypatch.setattr(sys, "stdin", Stdin())
    started = time.monotonic()
    assert cli._read_stdin(0.2) is None
    assert time.monotonic() - started < 2
    release.set()
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": io.BytesIO('{"a": "é"}'.encode()),
                                                    "isatty": staticmethod(lambda: False)})())
    assert cli._read_stdin(1.0) == '{"a": "é"}'


def test_local_presence_route_passes_the_title(tmp_path):
    from starlette.testclient import TestClient
    from agent_nexus.supervisor import Supervisor
    from agent_nexus.web import build_app
    app = build_app(Supervisor(tmp_path))
    calls = []

    async def fake_call(op, args=None, **_):
        calls.append((op, args))
        return {"matched": None}

    with TestClient(app, base_url="http://127.0.0.1:8399") as client:  # in-process: nothing listens
        hub = app.app.state.hub
        hub.attach(lambda message: None, {"features": ["hub_call/1", "wake/1"]})
        hub.call = fake_call
        body = {"kind": "claude", "cwd": "/p", "session_id": "s-1", "event": "UserPromptSubmit",
                "title": "T" * 500, "title_source": "prompt"}
        response = client.post("/api/agents/presence", json=body, headers={"X-ANX": "1"})
        assert response.status_code == 200, response.text
    op, args = calls[-1]
    assert op == "presence_report" and args["title"] == "T" * 200 and args["title_source"] == "prompt"


# -- anx integrate claude -------------------------------------------------------------------------

USER_SETTINGS = {
    "env": {"FOO": "1"},
    "enabledPlugins": {"x@y": True},
    "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "node other.js"}]}],
              "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]}]},
}


def _backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + ".anx-backup-*"))


ROOT = Path("C:/anx-root") if os.name == "nt" else Path("/anx-root")


def test_integrate_claude_adds_is_idempotent_backs_up_and_removes(home, capsys):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps(USER_SETTINGS, indent=4), encoding="utf-8")
    original = settings.read_text(encoding="utf-8")

    assert integrate.run("claude", dry_run=True, root=ROOT) == 0
    assert settings.read_text(encoding="utf-8") == original and not _backups(settings)
    assert "would add hooks.SessionStart" in capsys.readouterr().out

    assert integrate.run("claude", root=ROOT) == 0
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["env"] == {"FOO": "1"} and data["enabledPlugins"] == {"x@y": True}
    assert data["hooks"]["PreToolUse"] == USER_SETTINGS["hooks"]["PreToolUse"]
    start = data["hooks"]["SessionStart"]
    assert start[0] == USER_SETTINGS["hooks"]["SessionStart"][0]
    ours = start[1]["hooks"][0]
    assert ours["type"] == "command" and ours["timeout"] == 5 and ours["async"] is True
    assert ours["command"].endswith(f" --root {ROOT.as_posix()} hook claude --event SessionStart")
    assert "\\" not in ours["command"]
    prompt = data["hooks"]["UserPromptSubmit"]
    assert len(prompt) == 1 and "matcher" not in prompt[0]
    assert prompt[0]["hooks"][0]["command"].endswith("hook claude --event UserPromptSubmit")
    backups = _backups(settings)
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == original

    capsys.readouterr()
    assert integrate.run("claude", root=ROOT) == 0  # idempotent: no change, no new backup
    assert "nothing to change" in capsys.readouterr().out and len(_backups(settings)) == 1

    assert integrate.run("claude", remove=True) == 0
    assert json.loads(settings.read_text(encoding="utf-8")) == USER_SETTINGS
    assert integrate.run("claude", remove=True) == 0 and len(_backups(settings)) == 2


def test_integrate_claude_on_a_missing_or_broken_file(home, capsys):
    settings = home / ".claude" / "settings.json"
    assert integrate.run("claude") == 0
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert set(data) == {"hooks"} and not _backups(settings)
    assert integrate.run("claude", remove=True) == 0
    # The file stays, emptied: integrate cannot tell its own file from one the owner already had.
    assert json.loads(settings.read_text(encoding="utf-8")) == {}
    settings.write_text("{broken", encoding="utf-8")
    assert integrate.run("claude") == 1 and settings.read_text(encoding="utf-8") == "{broken"
    assert "not valid JSON" in capsys.readouterr().out


def test_integrate_updates_an_old_command_in_place(home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    old = {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "anx hook claude",
                                                      "timeout": 30}]}]}}
    settings.write_text(json.dumps(old), encoding="utf-8")
    plan = integrate.plan_hooks_json(settings, "claude", ANX, integrate.CLAUDE_EVENTS, remove=False)
    data = json.loads(plan.text)
    handlers = [h for group in data["hooks"]["UserPromptSubmit"] for h in group["hooks"]]
    assert len(handlers) == 1 and handlers[0]["timeout"] == 5
    assert handlers[0]["command"] == "C:/Tools/anx/anx.exe --root C:/anx-root hook claude --event UserPromptSubmit"


def test_shell_command_quoting():
    assert integrate.shell_command(["C:\\Program Files\\anx\\anx.exe", "hook", "claude"]) == \
        '"C:/Program Files/anx/anx.exe" hook claude'
    assert integrate._is_ours('"C:/Program Files/anx/anx.exe" hook claude --event X', "claude")
    assert integrate._is_ours(["C:\\x\\anx.exe", "hook", "codex"], "codex")
    assert integrate._is_ours("python -m agent_nexus.cli hook claude", "claude")
    assert not integrate._is_ours("node hook claude-helper.js", "claude")
    assert not integrate._is_ours(["C:\\x\\codex-computer-use.exe", "turn-ended"], "codex")


# -- anx integrate codex ----------------------------------------------------------------------------

CODEX_CONFIG = """model = "gpt-5"
approval_policy = "on-request"

[projects.'C:\\\\work']
trust_level = "trusted"

[mcp_servers.x]
command = "x"
"""


def test_integrate_codex_notify_add_idempotent_remove(home, monkeypatch, capsys):
    monkeypatch.setattr(integrate, "anx_argv", lambda root=None: list(ANX))
    config = home / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(CODEX_CONFIG, encoding="utf-8")
    before = tomllib.loads(CODEX_CONFIG)

    assert integrate.run("codex") == 0
    text = config.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    assert parsed["notify"] == [*ANX, "hook", "codex"]
    assert {k: v for k, v in parsed.items() if k != "notify"} == before
    assert text.index("notify =") < text.index("[projects.")
    assert len(_backups(config)) == 1

    assert integrate.run("codex") == 0 and len(_backups(config)) == 1
    assert integrate.run("codex", remove=True) == 0
    assert config.read_text(encoding="utf-8") == CODEX_CONFIG


def test_integrate_codex_refuses_a_foreign_notify(home, monkeypatch, capsys):
    monkeypatch.setattr(integrate, "anx_argv", lambda root=None: list(ANX))
    config = home / ".codex" / "config.toml"
    config.parent.mkdir()
    foreign = 'notify = ["C:\\\\Apps\\\\codex-computer-use.exe", "turn-ended"]\n' + CODEX_CONFIG
    config.write_text(foreign, encoding="utf-8")
    assert integrate.run("codex") == 1
    out = capsys.readouterr().out
    assert "codex-computer-use.exe" in out and "--hooks" in out
    assert config.read_text(encoding="utf-8") == foreign and not _backups(config)
    assert integrate.run("codex", remove=True) == 0 and config.read_text(encoding="utf-8") == foreign


def test_integrate_codex_without_a_config_and_with_hooks_json(home, monkeypatch, capsys):
    monkeypatch.setattr(integrate, "anx_argv", lambda root=None: list(ANX))
    config = home / ".codex" / "config.toml"
    assert integrate.run("codex") == 0
    assert tomllib.loads(config.read_text(encoding="utf-8")) == {"notify": [*ANX, "hook", "codex"]}
    assert integrate.run("codex", hooks=True) == 0
    hooks = json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    assert set(hooks["hooks"]) == {"SessionStart", "UserPromptSubmit"}
    assert "trust" in capsys.readouterr().out
    assert integrate.run("codex", hooks=True, remove=True) == 0
    hooks_json = home / ".codex" / "hooks.json"
    assert json.loads(hooks_json.read_text(encoding="utf-8")) == {}  # emptied, never deleted


def test_integrate_cli_entry(home, capsys):
    assert cli.main(["--root", str(home), "integrate", "claude", "--dry-run"]) == 0
    assert "would add" in capsys.readouterr().out
    assert not (home / ".claude" / "settings.json").exists()


# -- verifier regressions ----------------------------------------------------------------------------

def test_slash_commands_are_not_titles():
    assert first_line("/model") is None and first_line("\n  /clear  \nthen more") is None
    assert first_line("fix a/b paths") == "fix a/b paths"


def test_codex_hooks_json_runs_in_the_background(home, monkeypatch):
    monkeypatch.setattr(integrate, "anx_argv", lambda root=None: list(ANX))
    (plan,) = integrate.plans("codex", remove=False, hooks=True, base_argv=list(ANX))
    for groups in json.loads(plan.text)["hooks"].values():
        assert groups[0]["hooks"][0]["async"] is True
    # An entry written before (no async) is updated in place.
    path = home / ".codex" / "hooks.json"
    path.parent.mkdir()
    old = json.loads(plan.text)
    for groups in old["hooks"].values():
        groups[0]["hooks"][0].pop("async")
    path.write_text(json.dumps(old), encoding="utf-8")
    assert integrate.run("codex", hooks=True) == 0
    for groups in json.loads(path.read_text(encoding="utf-8"))["hooks"].values():
        assert len(groups) == 1 and groups[0]["hooks"][0]["async"] is True


def test_integrate_codex_with_a_bracket_line_in_a_multiline_string(home, monkeypatch):
    monkeypatch.setattr(integrate, "anx_argv", lambda root=None: list(ANX))
    config = home / ".codex" / "config.toml"
    config.parent.mkdir()
    original = ('model = "gpt"\r\ninstructions = """\r\n[not a table]\r\nnotify = ["x"]\r\n"""\r\n\r\n'
                '[mcp_servers.x]\r\ncommand = "y"\r\n')
    config.write_bytes(original.encode("utf-8"))
    before = tomllib.loads(original)
    assert integrate.run("codex") == 0
    parsed = tomllib.loads(config.read_bytes().decode("utf-8"))
    assert parsed["notify"] == [*ANX, "hook", "codex"]
    assert {k: v for k, v in parsed.items() if k != "notify"} == before
    assert integrate.run("codex") == 0 and len(_backups(config)) == 1  # idempotent
    # Updating an older command of ours finds the real notify line, not the one inside the string.
    monkeypatch.setattr(integrate, "anx_argv", lambda root=None: ["C:/new/anx.exe"])
    assert integrate.run("codex") == 0
    parsed = tomllib.loads(config.read_bytes().decode("utf-8"))
    assert parsed["notify"] == ["C:/new/anx.exe", "hook", "codex"] and parsed["instructions"] == before["instructions"]
    assert integrate.run("codex", remove=True) == 0
    assert config.read_bytes().decode("utf-8") == original


def test_integrate_codex_keeps_a_utf8_bom(home, monkeypatch):
    monkeypatch.setattr(integrate, "anx_argv", lambda root=None: list(ANX))
    config = home / ".codex" / "config.toml"
    config.parent.mkdir()
    original = b"\xef\xbb\xbf" + 'model = "gpt"\r\n\r\n[mcp_servers.x]\r\ncommand = "y"\r\n'.encode("utf-8")
    config.write_bytes(original)
    assert integrate.run("codex") == 0
    added = config.read_bytes()
    assert added.startswith(b"\xef\xbb\xbf# agent-nexus") and added.count(b"\xef\xbb\xbf") == 1
    assert tomllib.loads(added.decode("utf-8-sig"))["notify"] == [*ANX, "hook", "codex"]
    assert _backups(config)[0].read_bytes() == original
    assert integrate.run("codex", remove=True) == 0
    assert config.read_bytes() == original  # byte for byte, BOM included


def test_integrate_claude_keeps_a_utf8_bom(home, monkeypatch):
    monkeypatch.setattr(integrate, "anx_argv", lambda root=None: list(ANX))
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_bytes(b"\xef\xbb\xbf" + json.dumps({"model": "opus"}).encode("utf-8"))
    assert integrate.run("claude") == 0
    written = settings.read_bytes()
    assert written.startswith(b"\xef\xbb\xbf{") and written.count(b"\xef\xbb\xbf") == 1
    assert integrate.run("claude", remove=True) == 0
    assert json.loads(settings.read_bytes().decode("utf-8-sig")) == {"model": "opus"}
    assert settings.read_bytes().startswith(b"\xef\xbb\xbf{")


# -- second verifier round: edits keep the owner's file byte for byte -------------------------------

PLUGIN_SETTINGS = (
    '{\r\n    "model": "opus",\r\n    "attribution": {"commit": "","pr": ""},\r\n    "hooks": {\r\n'
    '        "SessionStart": [\r\n            {\r\n                "matcher": "startup",\r\n'
    '                "hooks": [\r\n                    {\r\n                        "type": "command",\r\n'
    '                        "command": "plugin-start"\r\n                    }\r\n                ]\r\n'
    '            }\r\n        ],\r\n        "UserPromptSubmit": [\r\n            {\r\n'
    '                "hooks": [{"type": "command", "command": "plugin-prompt"}]\r\n            }\r\n        ],\r\n'
    '        "Stop": [\r\n            {\r\n                "hooks": [{"type": "command", "command": "plugin-stop"}]\r\n'
    '            }\r\n        ]\r\n    }\r\n}\r\n').encode("utf-8")


@pytest.mark.parametrize("original", [
    PLUGIN_SETTINGS,
    b'{\n  "model": "opus",\n  "attribution": {"commit": "","pr": ""}\n}\n',
    b'{"model": "opus", "attribution": {"commit": "","pr": ""}}',
    b"\xef\xbb\xbf" + PLUGIN_SETTINGS,
], ids=["crlf-4-space-plugin-hooks", "lf-2-space-compact-line", "one-line", "bom-crlf"])
def test_integrate_claude_edits_in_place_and_remove_restores_the_bytes(home, monkeypatch, original):
    monkeypatch.setattr(integrate, "anx_argv", lambda root=None: list(ANX))
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_bytes(original)
    before = json.loads(original.decode("utf-8-sig"))
    assert integrate.run("claude") == 0
    added = settings.read_bytes()
    text = added.decode("utf-8-sig")
    # Only our entries were added: the owner's lines are still there exactly as written.
    for line in original.decode("utf-8-sig").splitlines():
        if line.strip() not in ("}", "]", "},", "],", ""):
            assert line.rstrip(",}") in text, line
    if b"\r\n" in original:
        assert added.count(b"\n") == added.count(b"\r\n")  # no LF-only line
    data = json.loads(text)
    assert {k: v for k, v in data.items() if k != "hooks"} == {k: v for k, v in before.items() if k != "hooks"}
    for event, groups in before.get("hooks", {}).items():
        assert data["hooks"][event][:len(groups)] == groups  # the plugin hooks stay first, unchanged
    assert set(data["hooks"]) == set(before.get("hooks", {})) | set(integrate.CLAUDE_EVENTS)
    assert integrate.run("claude") == 0 and settings.read_bytes() == added  # idempotent
    assert integrate.run("claude", remove=True) == 0
    assert settings.read_bytes() == original  # byte for byte


def test_edit_json_touches_only_what_changes():
    old = {"a": [1, 2, 3], "b": {"c": True}}
    text = '{"a": [1, 2, 3],\n "b": {"c": true}}\n'
    assert integrate.edit_json(text, old, {"a": [1, 3], "b": {"c": True}}) == '{"a": [1, 3],\n "b": {"c": true}}\n'
    assert integrate.edit_json(text, old, {"a": [1, 2], "b": {"c": True}}) == '{"a": [1, 2],\n "b": {"c": true}}\n'
    assert integrate.edit_json(text, old, {"a": [], "b": {"c": True}}) == '{"a": [],\n "b": {"c": true}}\n'
    assert integrate.edit_json(text, old, {"a": [1, 2, 3], "b": {"c": 1}}) == '{"a": [1, 2, 3],\n "b": {"c": 1}}\n'
    reordered = integrate.edit_json(text, old, {"b": {"c": False}, "a": [1]})
    assert json.loads(reordered) == {"b": {"c": False}, "a": [1]}
    assert json.loads(integrate.edit_json("", {}, {"x": 1})) == {"x": 1}
    assert integrate.edit_json("{}\n", {}, {"x": 1}) == '{\n  "x": 1\n}\n'


def test_hook_with_a_root_that_does_not_exist_reports_nothing(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(cli.Api, "call", lambda self, *a, **k: sent.append(a) or (200, {}))
    payload = json.dumps({"session_id": "s-1", "cwd": "/p", "hook_event_name": "SessionStart"})
    assert cli.main(["--root", str(tmp_path / "no" / "such" / "root"), "hook", "claude", payload]) == 0
    assert sent == []  # not the default manager port, which may be another installation's manager
    assert cli.main(["--root", str(tmp_path), "hook", "claude", payload]) == 0 and len(sent) == 1


def test_integrate_codex_notify_remove_empties_but_keeps_the_config(home, monkeypatch):
    monkeypatch.setattr(integrate, "anx_argv", lambda root=None: list(ANX))
    config = home / ".codex" / "config.toml"
    assert integrate.run("codex") == 0 and config.exists()
    assert integrate.run("codex", remove=True) == 0
    assert config.exists() and tomllib.loads(config.read_text(encoding="utf-8")) == {}


@pytest.mark.parametrize("text", ["{}", "{}\n", "{\n  \"hooks\": {}\n}\n", "   \n"])
def test_integrate_remove_never_deletes_an_owner_settings_file(home, text):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(text, encoding="utf-8")
    assert integrate.run("claude") == 0
    assert integrate.run("claude", remove=True) == 0
    assert settings.exists()
    assert "hooks" not in json.loads(settings.read_text(encoding="utf-8")) or         json.loads(settings.read_text(encoding="utf-8"))["hooks"] == {}
