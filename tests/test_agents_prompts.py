"""System-prompt fragments, presets, $variables and delivery (docs/agent-chat.md §6)."""
from __future__ import annotations

import json
import tomllib

import pytest

from agent_nexus.agents import prompts as P

CTX = {"kind": "codex", "mode": "direct", "device": "pc", "device_id": "dev_1", "os": "Linux", "workdir": "/w",
       "tmpdir": "/t/r1", "session": "codex-w", "address": "codex-w@pc", "owner": "小王", "now": 1764150000}


@pytest.fixture
def store(tmp_path):
    return P.Prompts(tmp_path, context_defaults=lambda: {"device": "pc", "os": "Linux"})


# ------------------------------------------------------------------ expansion

def test_expand_longest_name_and_escape():
    values = P.variables(CTX)
    text, unknown = P.expand("$device_id|$device|$workdir里|$work|$$HOME|${workdir}_x|$workdirs|$5|$", values)
    assert text == "dev_1|pc|/w里|$work|$HOME|/w_x|/ws|$5|$"
    assert unknown == ["work"]


def test_expand_unknown_braced_and_order():
    text, unknown = P.expand("${nope} $zzz $nope $zzz", P.variables({}))
    assert text == "${nope} $zzz $nope $zzz"
    assert unknown == ["nope", "zzz"]


def test_variables_defaults_and_placeholders():
    values = P.variables({"kind": "claude", "now": 0})
    assert values["agent"] == "Claude Code" and values["owner"] == "用户"
    assert values["date"] and values["time"] and values["os"]
    assert values["handle"] == ""
    preview = P.variables({"kind": "dsh"}, placeholders=True)
    assert preview["handle"].startswith("‹") and preview["agent"] == "DeepSeek Harness"
    assert set(values) == set(P.VARIABLE_NAMES)


# ------------------------------------------------------------------ store + render

def test_builtins_and_default_render(store):
    doc = store.load()
    ids = [f["id"] for f in doc["fragments"]]
    assert ids == ["identity", "workdir", "communicate", "team", "nexus_mcp", "devices_cli", "gateway", "project"]
    assert all(f["builtin"] and not f["modified"] for f in doc["fragments"])
    assert {p["id"] for p in doc["presets"]} == {"default", "minimal"}
    out = store.render("default", CTX)
    seg_ids = [s["id"] for s in out["segments"]]
    assert "gateway" not in seg_ids and seg_ids[0] == "identity"
    assert {"id": "gateway", "title": "网关工作方式", "reason": "mode"} in out["skipped"]
    assert out["unknown_vars"] == []
    assert "/t/r1" in out["text"] and "codex-w@pc" in out["text"] and "小王" in out["text"]
    assert "$" not in out["text"].replace('"', "")
    assert all(s["chars"] > 0 and s["enabled"] for s in out["segments"])
    assert out["text"] == "\n\n".join(  # segments are the text, in order
        P.expand(f["text"], P.variables(CTX))[0].strip() for f in doc["fragments"] if f["id"] in seg_ids)


def test_gateway_mode_includes_handle(store):
    out = store.render(None, {**CTX, "mode": "gateway", "handle": "ws_abc", "context_device": "nas",
                              "context_workdir": "/srv"})
    assert "gateway" in [s["id"] for s in out["segments"]]
    assert "workdir" not in [s["id"] for s in out["segments"]]  # the run's own workdir is not the target
    assert 'workspace_id="ws_abc"' in out["text"] and "nas" in out["text"]


def test_minimal_preset(store):
    out = store.render("minimal", CTX)
    assert [s["id"] for s in out["segments"]] == ["identity", "workdir"]


def test_unknown_preset_falls_back_to_default(store):
    assert store.render("missing", CTX)["preset"] == "default"


def test_crud_reorder_enable_reset(store, tmp_path):
    doc = store.upsert_fragment({"id": "style", "title": "代码风格", "text": "用 $agent 写测试 $mystery", "kinds": ["codex"]})
    assert doc["fragments"][-1]["id"] == "style" and not doc["fragments"][-1]["builtin"]
    assert "style" in next(p for p in doc["presets"] if p["id"] == "default")["order"]
    out = store.render("default", CTX)
    assert out["segments"][-1]["id"] == "style" and out["unknown_vars"] == ["mystery"]
    assert "style" not in [s["id"] for s in store.render("default", {**CTX, "kind": "claude"})["segments"]]

    store.reorder("default", ["style", "identity"])
    assert [s["id"] for s in store.render("default", CTX)["segments"]][:2] == ["style", "identity"]
    store.set_enabled("default", "identity", False)
    assert "identity" not in [s["id"] for s in store.render("default", CTX)["segments"]]
    assert "identity" in [s["id"] for s in store.render("minimal", CTX)["segments"]]  # per preset

    store.upsert_fragment({"id": "identity", "text": "改过的 $device"})
    assert store.fragment("identity")["modified"]
    store.reset("identity")
    assert not store.fragment("identity")["modified"]
    assert store.fragment("identity")["text"] == P._BUILTIN["identity"]["text"]

    with pytest.raises(P.PromptError):
        store.delete_fragment("identity")
    store.delete_fragment("style")
    assert store.fragment("style") is None
    assert all("style" not in p["order"] for p in store.load()["presets"])

    # persisted, and a second store reads the same thing
    stored = json.loads((tmp_path / ".anx" / "agents" / "prompts.json").read_text(encoding="utf-8"))
    assert "builtin" not in stored["fragments"][0]
    again = P.Prompts(tmp_path)
    assert next(p for p in again.load()["presets"] if p["id"] == "default")["enabled"]["identity"] is False


def test_untouched_builtin_follows_upgrade(store, tmp_path, monkeypatch):
    store.set_enabled("default", "team", False)  # writes the file with the current builtin text
    path = tmp_path / ".anx" / "agents" / "prompts.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["fragments"] = [f for f in stored["fragments"] if f["id"] != "project"]  # an older version lacked it
    path.write_text(json.dumps(stored), encoding="utf-8")
    doc = P.Prompts(tmp_path).load()
    assert [f["id"] for f in doc["fragments"]][-1] == "project"


def test_save_whole_document_validates(store):
    doc = store.load()
    doc["fragments"].append({"id": "bad id!", "title": "x", "text": ""})
    result = store.op_save(doc)
    assert result["code"] == "invalid" and "ID" in result["error"]
    doc = store.load()
    doc["fragments"][0]["scope"] = "everywhere"
    assert store.op_save(doc)["code"] == "invalid"
    doc = store.load()
    doc["fragments"] = [f for f in doc["fragments"] if f["id"] != "team"]  # built-ins cannot be dropped
    saved = store.op_save(doc)
    assert "team" in [f["id"] for f in saved["fragments"]]
    assert store.op_save({})["code"] == "bad_request"
    assert store.op_save({"reset": "nope"})["code"] == "not_builtin"
    assert store.op_save({"delete_preset": "default"})["code"] == "builtin"


def test_presets_add_and_delete(store):
    doc = store.save_preset({"id": "review", "name": "代码评审"})
    review = next(p for p in doc["presets"] if p["id"] == "review")
    assert review["order"][:2] == ["identity", "workdir"] and not review["builtin"]
    store.save_preset({"id": "review", "enabled": {"team": False}})
    assert "team" not in [s["id"] for s in store.render("review", CTX)["segments"]]
    store.delete_preset("review")
    assert "review" not in [p["id"] for p in store.load()["presets"]]


def test_preview_with_draft_and_ops(store):
    draft = store.load()
    draft["fragments"].append({"id": "custom-1", "title": "新片段", "text": "看 $handle 和 $oops", "scope": "gateway"})
    out = store.op_preview({"preset": "default", "context": {"kind": "codex", "mode": "gateway"}, "draft": draft})
    assert "error" not in out
    assert out["segments"][-1]["id"] == "custom-1" and out["unknown_vars"] == ["oops"]
    assert "‹网关工作区句柄（仅经网关）›" in out["text"]
    assert out["variables"]["device"] == "pc"
    assert store.fragment("custom-1") is None  # a draft is not saved
    listing = store.op_prompts({})
    assert listing["variables"][0]["name"] == "device" and listing["variables"][0]["desc"]
    assert set(store.handlers()) == {"agent_prompts", "agent_prompt_save", "agent_prompt_preview"}


def test_damaged_file_is_quarantined(tmp_path):
    path = tmp_path / ".anx" / "agents" / "prompts.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    doc = P.Prompts(tmp_path).load()
    assert len(doc["fragments"]) == len(P.BUILTIN_FRAGMENTS)
    assert path.with_name("prompts.json.bad").exists()


def test_disabled_everything_gives_empty_text(store):
    for fragment in store.load()["fragments"]:
        store.set_enabled("default", fragment["id"], False)
    out = store.render("default", CTX)
    assert out["text"] == "" and out["segments"] == []


# ------------------------------------------------------------------ delivery

def test_delivery_claude_codex_other():
    text = '第一行 "引号" \\ 反斜杠\n第二行\t$x\x7f'
    assert P.system_prompt_args("claude", text) == ["--append-system-prompt", text]
    args = P.system_prompt_args("codex", text)
    assert args[0] == "-c" and args[1].startswith("developer_instructions=")
    value = args[1].split("=", 1)[1]
    assert "\n" not in value
    assert tomllib.loads(f"developer_instructions = {value}")["developer_instructions"] == text
    assert P.system_prompt_args("dsh", text) == []
    assert P.delivery("dsh", text) == {"mode": "prefix", "argv": []}
    assert P.delivery("claude", "")["mode"] == "none"
    assert P.prefix_message("说明", "你好") == "说明\n\n---\n\n你好"
    assert P.prefix_message("", "你好") == "你好"


def test_devices_cli_fragment_follows_nexus_mcp_and_is_dropped_when_empty(store):
    hint = "也可以用 anx exec <设备> -- <命令>；命令含 $ 时用 --stdin"
    for preset in ("default", "minimal"):
        out = store.render(preset, {**CTX, "mcp_hint": "MCP 已接入", "devices_cli": hint})
        ids = [s["id"] for s in out["segments"]]
        assert "devices_cli" in ids and hint in out["text"]  # a value's own $ is never expanded again
        if "nexus_mcp" in ids:
            assert ids.index("devices_cli") == ids.index("nexus_mcp") + 1
    out = store.render("default", CTX)
    assert {"id": "devices_cli", "title": "用命令行操作其他设备", "reason": "empty"} in out["skipped"]


def test_a_stored_document_gets_the_new_builtin_back_in_place(tmp_path):
    old = [f for f in P.BUILTIN_FRAGMENTS if f["id"] != "devices_cli"]
    doc = P.normalize({"fragments": [dict(f) for f in old]})
    ids = [f["id"] for f in doc["fragments"]]
    assert ids.index("devices_cli") == ids.index("nexus_mcp") + 1
