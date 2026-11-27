#!/usr/bin/env python3
"""A stand-in for ``claude -p --output-format stream-json --input-format stream-json`` (runner tests).

Reads stream-json user messages on stdin, answers in stream-json. What a turn does depends on its text:
  "approve"  -- asks anx's approval MCP server (from --mcp-config) before a Bash tool call
  "edit"     -- an Edit tool call (file_change event)
  "slow"     -- works until an ``interrupt`` control request arrives
  "die"      -- writes to stderr and exits 3
  "sysprompt"-- answers with the --append-system-prompt value
  otherwise  -- "echo: <text>" (token deltas with --include-partial-messages)
Replies carry usage and the result modelUsage (§16.3).
Environment: FAKE_ARGV_LOG (append argv as JSON), FAKE_OLD_CLAUDE=1 (reject --include-partial-messages).
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid

args = sys.argv[1:]
if os.environ.get("FAKE_ARGV_LOG"):
    with open(os.environ["FAKE_ARGV_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(args) + "\n")
if os.environ.get("FAKE_OLD_CLAUDE") and "--include-partial-messages" in args:
    print("error: unknown option '--include-partial-messages'", file=sys.stderr, flush=True)
    sys.exit(1)


def value(flag):
    return args[args.index(flag) + 1] if flag in args else None


session = value("--resume") or str(uuid.uuid4())
partial = "--include-partial-messages" in args
system_prompt = value("--append-system-prompt") or ""
inbox = queue.Queue()


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def reader():
    for line in sys.stdin:
        line = line.strip()
        if line:
            inbox.put(json.loads(line))
    inbox.put(None)


def ask_approval(tool, data):
    with open(value("--mcp-config"), encoding="utf-8") as handle:
        config = json.load(handle)["mcpServers"]["anx"]
    env = dict(os.environ, **config.get("env", {}))
    proc = subprocess.Popen([config["command"], *config["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            env=env, text=True)

    def rpc(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()
        if "id" in obj:
            return json.loads(proc.stdout.readline())

    init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["capabilities"]["tools"] is not None
    rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
    tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert tools[0]["name"] == "approve"
    answer = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "approve", "arguments": {"tool_name": tool, "input": data,
                                                               "tool_use_id": "toolu_9"}}})
    proc.stdin.close()
    proc.wait(5)
    return json.loads(answer["result"]["content"][0]["text"])


def result(text, error=False, subtype="success"):
    emit({"type": "result", "subtype": subtype, "is_error": error, "result": text, "session_id": session,
          "total_cost_usd": 0.0123, "usage": {"input_tokens": 10, "cache_read_input_tokens": 2, "output_tokens": 5},
          "modelUsage": {"fake-haiku": {"inputTokens": 1, "outputTokens": 1, "contextWindow": 100000},
                         "fake-model": {"inputTokens": 10, "outputTokens": 5, "cacheReadInputTokens": 2,
                                        "contextWindow": 1000000, "costUSD": 0.0123}}})


def say(text, msg_id):
    if partial:
        emit({"type": "stream_event", "session_id": session,
              "event": {"type": "message_start", "message": {"id": msg_id}}})
        emit({"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
                                                "content_block": {"type": "thinking"}}})
        emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                "delta": {"type": "thinking_delta", "thinking": "hmm"}}})
        emit({"type": "assistant", "message": {"id": msg_id, "content": [{"type": "thinking", "thinking": "hmm"}]}})
        emit({"type": "stream_event", "event": {"type": "content_block_start", "index": 1,
                                                "content_block": {"type": "text"}}})
        half = len(text) // 2
        for piece in (text[:half], text[half:]):
            emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 1,
                                                    "delta": {"type": "text_delta", "text": piece}}})
    emit({"type": "assistant", "session_id": session, "parent_tool_use_id": "toolu_sub", "message": {
        "id": msg_id + "sub", "model": "fake-haiku", "role": "assistant", "content": [],
        "usage": {"input_tokens": 999999, "output_tokens": 1}}})  # a subagent's: not this context
    emit({"type": "assistant", "session_id": session, "unknown_field": 1, "parent_tool_use_id": None,
          "message": {"id": msg_id, "model": "fake-model", "role": "assistant",
                      "content": [{"type": "text", "text": text}],
                      "usage": {"input_tokens": 3, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200,
                                "output_tokens": 40}}})


threading.Thread(target=reader, daemon=True).start()
emit({"type": "system", "subtype": "init", "session_id": session, "model": "fake-model", "tools": ["Bash"]})
turn = 0
while True:
    message = inbox.get()
    if message is None:
        break
    if message.get("type") == "control_request":
        emit({"type": "control_response", "response": {"subtype": "success", "request_id": message["request_id"]}})
        continue
    if message.get("type") != "user":
        continue
    turn += 1
    text = message["message"]["content"][0]["text"]
    msg_id = f"msg_{turn}"
    if "die" in text:
        print("boom: something broke", file=sys.stderr, flush=True)
        sys.exit(3)
    if "approve" in text:
        data = {"command": "rm -rf build", "description": "clean"}
        emit({"type": "assistant", "message": {"id": msg_id, "content": [
            {"type": "tool_use", "id": "toolu_9", "name": "Bash", "input": data}]}})
        decision = ask_approval("Bash", data)
        if decision["behavior"] == "allow":
            emit({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_9", "content": "removed build"}]}})
            say("done: allowed", msg_id + "b")
        else:
            emit({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_9", "is_error": True,
                 "content": [{"type": "text", "text": decision["message"]}]}]}})
            say("done: denied", msg_id + "b")
        result("ok")
        continue
    if "edit" in text:
        data = {"file_path": "src/a.py", "old_string": "x = 1\n", "new_string": "x = 2\n"}
        emit({"type": "assistant", "message": {"id": msg_id, "content": [
            {"type": "tool_use", "id": "toolu_e", "name": "Edit", "input": data}]}})
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_e",
                                                       "content": "ok"}]}})
        result("ok")
        continue
    if "slow" in text:
        say("working", msg_id)
        deadline = time.time() + 30
        stopped = False
        while time.time() < deadline and not stopped:
            try:
                item = inbox.get(timeout=0.05)
            except queue.Empty:
                continue
            if item and item.get("type") == "control_request" and item["request"]["subtype"] == "interrupt":
                emit({"type": "control_response",
                      "response": {"subtype": "success", "request_id": item["request_id"]}})
                stopped = True
        result("interrupted", error=True, subtype="error_during_execution")
        continue
    if "sysprompt" in text:
        say(f"system: {system_prompt}", msg_id)
        result("ok")
        continue
    say(f"echo: {text}", msg_id)
    result(f"echo: {text}")
