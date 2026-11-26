#!/usr/bin/env python3
"""A stand-in for ``codex exec --json`` (runner tests): one turn per process.

``exec ... [resume <id>] <prompt|->``; the prompt decides: "slow" (sleeps until killed), "fail" (turn.failed),
"crash" (stderr + exit 2), otherwise a reasoning item, a command, a file change, a todo list and the answer.
FAKE_ARGV_LOG: append argv (and the stdin prompt) as JSON.
"""
import json
import os
import sys
import time
import uuid

args = sys.argv[1:]
prompt = args[-1]
if prompt == "-":
    prompt = sys.stdin.read()
if os.environ.get("FAKE_ARGV_LOG"):
    with open(os.environ["FAKE_ARGV_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": args, "prompt": prompt}) + "\n")
thread = args[args.index("resume") + 1] if "resume" in args else str(uuid.uuid4())


def emit(obj):
    print(json.dumps(obj), flush=True)


emit({"type": "thread.started", "thread_id": thread})
emit({"type": "turn.started"})
if "crash" in prompt:
    print("fatal: codex crashed", file=sys.stderr, flush=True)
    sys.exit(2)
if "slow" in prompt:
    emit({"type": "item.started", "item": {"id": "item_0", "type": "command_execution", "command": "sleep 30",
                                           "aggregated_output": "", "status": "in_progress"}})
    time.sleep(30)
    sys.exit(0)
if "fail" in prompt:
    emit({"type": "turn.failed", "error": {"message": "model refused"}})
    sys.exit(1)
emit({"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "**Planning**"}})
emit({"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": "bash -lc ls",
                                       "aggregated_output": "", "exit_code": None, "status": "in_progress"}})
emit({"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "command": "bash -lc ls",
                                         "aggregated_output": "a.txt\n", "exit_code": 0, "status": "completed"}})
emit({"type": "item.completed", "item": {"id": "item_2", "type": "file_change", "status": "completed",
                                         "changes": [{"path": "a.txt", "kind": "add"}]}})
emit({"type": "item.updated", "item": {"id": "item_3", "type": "todo_list",
                                       "items": [{"text": "look", "completed": True}, {"text": "fix", "completed": False}]}})
emit({"type": "item.completed", "item": {"id": "item_4", "type": "something_new", "x": 1}})
emit({"type": "item.completed", "item": {"id": "item_5", "type": "agent_message", "text": f"codex: {prompt}"}})
emit({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 7}})
