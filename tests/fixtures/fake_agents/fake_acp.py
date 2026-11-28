#!/usr/bin/env python3
"""A stand-in ACP agent (``dsh --profile acp`` / ``gemini --experimental-acp``) for the runner tests.

``--profile headless --json [--session-id id] <prompt>`` prints JSON lines instead (the text fallback).
FAKE_ACP: "error" (initialize fails), "silent" (never answers), "noload" (no loadSession), "image" (takes image
blocks: the answer counts them), "refuse_load" (session/load fails, like dsh in another folder).
Prompt words: "perm" (asks session/request_permission), "slow" (until session/cancel), "fs" (calls an
unsupported client method and reports the error code).
"""
import json
import os
import queue
import sys
import threading
import time

args = sys.argv[1:]
if os.environ.get("FAKE_ARGV_LOG"):
    with open(os.environ["FAKE_ARGV_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(args) + "\n")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


if "headless" in args:
    sid = args[args.index("--session-id") + 1] if "--session-id" in args else "hs-1"
    emit({"type": "session.started", "sessionId": sid})
    emit({"type": "assistant", "text": f"headless: {args[-1]}"})
    print("plain line")
    sys.exit(0)

mode = os.environ.get("FAKE_ACP", "")
inbox = queue.Queue()


def reader():
    for line in sys.stdin:
        if line.strip():
            inbox.put(json.loads(line))
    inbox.put(None)


threading.Thread(target=reader, daemon=True).start()
session = None
next_id = 100


def update(what, **fields):
    emit({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": session, "update": {"sessionUpdate": what, **fields}}})


def request(method, params):
    global next_id
    next_id += 1
    emit({"jsonrpc": "2.0", "id": next_id, "method": method, "params": params})
    while True:
        item = inbox.get()
        if item is None:
            sys.exit(0)
        if item.get("id") == next_id and "method" not in item:
            return item
        pending.append(item)


pending = []


def take():
    if pending:
        return pending.pop(0)
    return inbox.get()


while True:
    msg = take()
    if msg is None:
        break
    method, rid = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}
    if mode == "silent":
        continue
    if method == "initialize":
        if mode == "error":
            emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "no credentials"}})
            continue
        emit({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1,
                                                      "agentCapabilities": {
                                                          "loadSession": mode != "noload",
                                                          "promptCapabilities": {"embeddedContext": mode == "embedded",
                                                                                 "image": mode == "image"}}}})
    elif method == "session/new":
        session = "acp-sess-1"
        emit({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": session}})
    elif method == "session/load" and mode == "refuse_load":
        emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "session cwd does not match"}})
    elif method == "session/load":
        session = params["sessionId"]
        update("user_message_chunk", content={"type": "text", "text": "old question"})
        update("agent_message_chunk", content={"type": "text", "text": "old answer"})
        emit({"jsonrpc": "2.0", "id": rid, "result": None})
    elif method == "session/prompt":
        text = params["prompt"][0]["text"]
        resources = [b["resource"].get("text", "") for b in params["prompt"][1:] if b.get("type") == "resource"]
        if resources:
            text += f" [res:{resources[0]}]"
        images = [b for b in params["prompt"][1:] if b.get("type") == "image"]
        if images:
            text += " [images=%d %s]" % (len(images), images[0].get("uri", ""))
        update("agent_thought_chunk", content={"type": "text", "text": "thinking"})
        update("future_update_kind", whatever=1)
        if "perm" in text:
            update("tool_call", toolCallId="tc1", title="Run rm -rf build", kind="execute", status="pending",
                   rawInput={"command": "rm -rf build"})
            answer = request("session/request_permission", {
                "sessionId": session, "toolCall": {"toolCallId": "tc1", "title": "Run rm -rf build", "kind": "execute",
                                                   "rawInput": {"command": "rm -rf build"}},
                "options": [{"optionId": "yes", "name": "Allow", "kind": "allow_once"},
                            {"optionId": "always", "name": "Always", "kind": "allow_always"},
                            {"optionId": "no", "name": "Reject", "kind": "reject_once"}]})
            outcome = answer["result"]["outcome"]
            chosen = outcome.get("optionId", outcome["outcome"])
            update("tool_call_update", toolCallId="tc1",
                   status="completed" if chosen in ("yes", "always") else "failed",
                   content=[{"type": "content", "content": {"type": "text", "text": f"chosen={chosen}"}},
                            {"type": "diff", "path": "b.txt", "oldText": "a\n", "newText": "b\n"}])
            update("agent_message_chunk", content={"type": "text", "text": f"perm: {chosen}"})
            emit({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        elif "otherkind" in text:
            update("tool_call", toolCallId="tc2", title="read", kind="other", status="pending",
                   rawInput={"file_path": "a.txt"})
            update("tool_call_update", toolCallId="tc2", status="completed",
                   content=[{"type": "content", "content": {"type": "text", "text": "hello"}}])
            emit({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        elif "slow" in text:
            update("agent_message_chunk", content={"type": "text", "text": "working"})
            cancelled = False
            deadline = time.time() + 30
            while not cancelled and time.time() < deadline:
                try:
                    item = inbox.get(timeout=0.05)
                except queue.Empty:
                    continue
                if item and item.get("method") == "session/cancel":
                    cancelled = True
            emit({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "cancelled"}})
        elif "fs" in text:
            answer = request("fs/read_text_file", {"sessionId": session, "path": "/etc/hosts"})
            update("agent_message_chunk", content={"type": "text", "text": f"fs-error:{answer['error']['code']}"})
            emit({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        else:
            update("plan", entries=[{"content": "step one", "status": "completed"},
                                    {"content": "step two", "status": "pending"}])
            for piece in ("acp: ", text):
                update("agent_message_chunk", content={"type": "text", "text": piece})
            emit({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
    elif method == "session/cancel":
        pass
    elif rid is not None:
        emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "nope"}})
