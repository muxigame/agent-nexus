"""Choosing the model of a run (docs/agent-chat.md §16.7): the flags, the ACP calls, the checks."""
from __future__ import annotations

import json

import pytest

from agent_nexus.agents import adapters, registry
from agent_nexus.agents.adapters import AdapterOptions
from agent_nexus.agents.runner import RunError, _model

DEFS = registry.builtin_definitions()


def opts(**kw) -> AdapterOptions:
    return AdapterOptions(binary=kw.pop("binary", "cli"), workdir="/w", **kw)


def test_command_argv_puts_the_model_flag_at_options_only_when_asked():
    for kind, flag in (("claude", "--model"), ("codex", "-m"), ("gemini", "-m")):
        d = DEFS[kind]
        with_model = registry.command_argv(d, "bin", values={"model": "m-1"})
        assert with_model[with_model.index(flag) + 1] == "m-1", kind
        assert flag not in registry.command_argv(d, "bin", values={}), kind
    # dsh: no flag at all (ACP only)
    assert registry.command_argv(DEFS["dsh"], "dsh", values={"model": "x"}) == ["dsh", "--profile", "acp"]
    assert DEFS["dsh"].models() == {"flag": False, "protocol": True, "hints": []}
    assert DEFS["claude"].models()["flag"] and any(h["id"] == "opus" for h in DEFS["claude"].models()["hints"])


def test_custom_clis_take_model_args_and_checked_hints():
    defs = registry.definitions({"agents": {"custom": {"mine": {
        "command": ["{bin}", "{options...}", "{prompt}"], "model_args": ["--llm", "{model}"],
        "model_hints": ["fast", {"id": "big-1", "label": "Big"}, {"id": "-bad"}, {"id": "a b"}, 7]}}}})
    d = defs["mine"]
    assert registry.command_argv(d, "mine", values={"model": "big-1", "prompt": "hi"}) == ["mine", "--llm", "big-1", "hi"]
    assert d.model_hints == [{"id": "fast"}, {"id": "big-1", "label": "Big"}]
    assert registry.info(d, {})["models"]["flag"] is True


@pytest.mark.parametrize("value", ["opus", "gpt-5.6-sol", "sonnet[1m]", "deepseek/deepseek-v4", "a:b@c+d=e"])
def test_model_names_that_pass(value):
    assert registry.model_ok(value) and _model({"model": value}) == value


@pytest.mark.parametrize("value", ["-m", "--dangerously-bypass-approvals-and-sandbox", "a b", 'x"y', "%PATH%", "a!b",
                                   "x" * 101, 5, ["opus"]])
def test_model_names_that_are_refused(value):
    with pytest.raises(RunError) as info:
        _model({"model": value})
    assert info.value.code == "bad_model"


def test_no_model_or_an_empty_one_means_the_clis_default():
    assert _model({}) is None and _model({"model": None}) is None and _model({"model": "  "}) == ""


def test_claude_takes_model_and_switches_a_live_process():
    adapter = adapters.ClaudeStream(DEFS["claude"], opts(model="opus"))
    argv = adapter.argv()
    assert argv[argv.index("--model") + 1] == "opus"
    adapter.session_id = "s-1"
    resumed = adapter.argv()
    assert resumed.index("--resume") < resumed.index("--model")
    adapter.outbox.clear()
    assert adapter.set_model("haiku", live=True) == []
    sent = json.loads(adapter.outbox[-1])
    assert sent["type"] == "control_request" and sent["request"] == {"subtype": "set_model", "model": "haiku"}
    refused = adapter.feed(json.dumps({"type": "control_response", "response": {
        "subtype": "error", "request_id": sent["request_id"], "error": "unknown model"}}))
    assert refused and refused[0]["level"] == "warn" and "haiku" in refused[0]["text"]
    adapter.outbox.clear()
    adapter.set_model("sonnet", live=False)  # no process: only the next argv
    assert adapter.outbox == [] and "sonnet" in adapter.argv()


def test_codex_puts_the_model_before_resume_and_reports_it():
    adapter = adapters.CodexExec(DEFS["codex"], opts(binary="codex", model="gpt-6.1-sol"))
    adapter.session_id = "thread-1"
    argv = adapter.argv("hi")
    assert argv[argv.index("-m") + 1] == "gpt-6.1-sol" and argv.index("-m") < argv.index("resume")
    assert adapter._model_arg() == "gpt-6.1-sol"
    adapter.turn_active = False
    adapter.set_model("")
    assert "-m" not in adapter.argv("again")


def test_codex_argv_with_the_per_run_local_mcp_and_a_model():
    extra = {"mcp_attach": {"name": "agent-nexus", "url": "http://127.0.0.1:8399/mcp"}, "mcp_approve": ["agent-nexus"],
             "mcp_enabled_tools": ["file_read", "mail_send"]}
    argv = adapters.CodexExec(DEFS["codex"], opts(binary="codex", model="gpt-6-astra", extra=extra)).argv("hi")
    at = argv.index("exec")
    head = argv[at + 1:at + 9]
    assert 'mcp_servers.agent-nexus.url="http://127.0.0.1:8399/mcp"' in head
    assert 'mcp_servers.agent-nexus.default_tools_approval_mode="approve"' in argv
    assert any(a.startswith("mcp_servers.agent-nexus.enabled_tools=") for a in argv)
    assert argv[argv.index("-m") + 1] == "gpt-6-astra"


def acp(kind: str, **kw) -> adapters.Acp:
    adapter = adapters.Acp(DEFS[kind], opts(binary=kind, **kw))
    adapter.start()
    return adapter


def sent(adapter) -> list[dict]:
    out = [json.loads(line) for line in adapter.outbox]
    adapter.outbox.clear()
    return out


def test_gemini_gets_the_flag_and_set_model_when_the_session_offers_another_one():
    adapter = acp("gemini", model="flash")
    assert adapter.argv()[-2:] == ["-m", "flash"]
    sent(adapter)
    adapter.begin_turn("hello")
    adapter.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"agentCapabilities": {}}}))
    sent(adapter)
    events = adapter.feed(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s", "models": {
        "currentModelId": "gemini-2.5-pro", "availableModels": [
            {"modelId": "gemini-2.5-pro", "name": "Pro"}, {"modelId": "flash", "name": "Flash"}]}}}))
    model = next(e for e in events if e["type"] == "model")
    assert model["model"] == "gemini-2.5-pro" and [o["id"] for o in model["options"]] == ["gemini-2.5-pro", "flash"]
    calls = sent(adapter)
    assert [c["method"] for c in calls] == ["session/set_model"]  # the prompt waits for the switch
    assert calls[0]["params"] == {"sessionId": "s", "modelId": "flash"}
    events = adapter.feed(json.dumps({"jsonrpc": "2.0", "id": calls[0]["id"], "result": {}}))
    assert any(e["type"] == "model" and e["model"] == "flash" for e in events)
    assert [c["method"] for c in sent(adapter)] == ["session/prompt"]


DSH_MODEL_OPTION = {"id": "model", "category": "model", "type": "select", "currentValue": '["deepseek","deepseek-chat"]',
                    "options": [{"group": "deepseek", "name": "DeepSeek", "options": [
                        {"value": '["deepseek","deepseek-chat"]', "name": "V3"},
                        {"value": '["deepseek","deepseek-reasoner"]', "name": "R1"}]}]}


def test_dsh_maps_the_model_to_its_config_option_and_resumes_with_session_resume():
    adapter = acp("dsh", model="deepseek-reasoner", resume_id="old-1")
    sent(adapter)
    adapter.begin_turn("hello")
    adapter.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"agentCapabilities": {
        "sessionCapabilities": {"resume": {}}}}}))
    calls = sent(adapter)
    assert calls[0]["method"] == "session/resume" and calls[0]["params"]["sessionId"] == "old-1"
    assert calls[0]["params"]["cwd"] == "/w"
    events = adapter.feed(json.dumps({"jsonrpc": "2.0", "id": calls[0]["id"], "result": {"configOptions": [DSH_MODEL_OPTION]}}))
    assert not any("不支持续接" in e.get("text", "") for e in events)
    model = next(e for e in events if e["type"] == "model")
    assert model["model"] == "deepseek/deepseek-chat"
    assert {o["id"] for o in model["options"]} == {"deepseek/deepseek-chat", "deepseek/deepseek-reasoner"}
    calls = sent(adapter)
    assert calls[0]["method"] == "session/set_config_option"
    assert calls[0]["params"] == {"sessionId": "old-1", "configId": "model", "value": '["deepseek","deepseek-reasoner"]'}
    adapter.feed(json.dumps({"jsonrpc": "2.0", "id": calls[0]["id"], "result": {}}))
    assert [c["method"] for c in sent(adapter)] == ["session/prompt"]
    assert adapter._current_model() == "deepseek/deepseek-reasoner"


def test_dsh_says_so_when_it_does_not_offer_the_model():
    adapter = acp("dsh", model="gpt-6")
    sent(adapter)
    adapter.begin_turn("hello")
    adapter.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"agentCapabilities": {}}}))
    sent(adapter)
    events = adapter.feed(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s",
                                                                            "configOptions": [DSH_MODEL_OPTION]}}))
    assert any(e.get("level") == "warn" and "没有提供模型 gpt-6" in e["text"] for e in events)
    assert [c["method"] for c in sent(adapter)] == ["session/prompt"]  # the prompt is not held back


def test_dsh_headless_fallback_cannot_choose_a_model():
    adapter = adapters.fallback(DEFS["dsh"], opts(binary="dsh", model="x"), None)
    assert "x" not in adapter.argv("hi")
    events = adapters.Text(DEFS["dsh"], opts(binary="dsh"), template="fallback_command").set_model("x")
    assert events and events[0]["level"] == "notice"


def test_claude_announces_a_live_switch_only_from_its_answer_and_keeps_the_model_it_refused():
    adapter = adapters.ClaudeStream(DEFS["claude"], opts(model="sonnet"))
    assert adapter.set_model("opus", live=False) == [{"type": "model", "model": "opus"}]  # next argv: final
    assert adapter.set_model("haiku", live=True) == []  # asked, not yet switched
    ok = json.loads(adapter.outbox[-1])
    events = adapter.feed(json.dumps({"type": "control_response", "response": {
        "subtype": "success", "request_id": ok["request_id"]}}))
    assert events == [{"type": "model", "model": "haiku"}]
    adapter.set_model("nope", live=True)
    bad = json.loads(adapter.outbox[-1])
    events = adapter.feed(json.dumps({"type": "control_response", "response": {
        "subtype": "error", "request_id": bad["request_id"], "error": "unknown"}}))
    assert [e["type"] for e in events] == ["error"] and adapter.opts.model == "haiku"


GEMINI_MODELS = {"currentModelId": "gemini-2.5-flash", "availableModels": [
    {"modelId": "auto", "name": "auto"}, {"modelId": "gemini-2.5-pro", "name": "gemini-2.5-pro"},
    {"modelId": "gemini-2.5-flash", "name": "gemini-2.5-flash"},
    {"modelId": "gemini-2.5-flash-lite", "name": "gemini-2.5-flash-lite"}]}


def ready_gemini(model: str = "") -> adapters.Acp:
    adapter = acp("gemini", model=model)
    sent(adapter)
    adapter.begin_turn("hello")
    adapter.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"agentCapabilities": {}}}))
    sent(adapter)
    events = adapter.feed(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s", "models": GEMINI_MODELS}}))
    assert not any(e.get("level") == "warn" for e in events), events  # -m flash is gemini-2.5-flash: no warning
    for call in sent(adapter):  # the switch to the chosen model, when it differs: answered
        if call["method"] == "session/set_model":
            adapter.feed(json.dumps({"jsonrpc": "2.0", "id": call["id"], "result": {}}))
    sent(adapter)
    return adapter


def test_gemini_aliases_and_unlisted_ids_switch_a_live_session():
    adapter = ready_gemini("flash")
    assert adapter.set_model("pro", live=True) == []  # announced from the answer, not before
    call = sent(adapter)[-1]
    assert call["method"] == "session/set_model" and call["params"]["modelId"] == "gemini-2.5-pro"
    events = adapter.feed(json.dumps({"jsonrpc": "2.0", "id": call["id"], "result": {}}))
    assert any(e["type"] == "model" and e["model"] == "gemini-2.5-pro" for e in events)
    # an id the session does not list: sent anyway, Gemini's answer decides; a refusal keeps the model
    adapter.set_model("gemini-3-pro-preview", live=True)
    call = sent(adapter)[-1]
    assert call["params"]["modelId"] == "gemini-3-pro-preview"
    events = adapter.feed(json.dumps({"jsonrpc": "2.0", "id": call["id"], "error": {"message": "not found"}}))
    assert any(e.get("level") == "warn" and "沿用当前模型" in e["text"] for e in events)
    assert adapter.opts.model == "pro"


def test_acp_back_to_default_uses_the_reported_default_or_says_it_keeps_the_model():
    adapter = ready_gemini("")  # started on the CLI's default: gemini-2.5-flash
    adapter.set_model("pro", live=True)
    call = sent(adapter)[-1]
    adapter.feed(json.dumps({"jsonrpc": "2.0", "id": call["id"], "result": {}}))
    assert adapter.set_model("", live=True) == []
    call = sent(adapter)[-1]
    assert call["params"]["modelId"] == "gemini-2.5-flash" and adapter.opts.model == ""
    started = ready_gemini("pro")  # started on a chosen model: the default is unknown
    events = started.set_model("", live=True)
    assert events[0]["level"] == "notice" and "保持当前模型" in events[0]["text"]
    assert started.opts.model == "pro" and sent(started) == []


def test_dsh_live_switch_to_a_model_it_does_not_offer_keeps_the_current_one():
    adapter = acp("dsh")
    sent(adapter)
    adapter.begin_turn("hello")
    adapter.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"agentCapabilities": {}}}))
    sent(adapter)
    adapter.feed(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s", "configOptions": [DSH_MODEL_OPTION]}}))
    sent(adapter)
    events = adapter.set_model("gpt-6", live=True)
    assert [e["type"] for e in events] == ["error"] and "沿用当前模型" in events[0]["text"]
    assert adapter.opts.model == "" and sent(adapter) == []
