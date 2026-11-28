#!/usr/bin/env python3
"""A stand-in for ``codex exec --json`` (runner tests): one turn per process.

``exec ... [resume <id>] <prompt|->``; the prompt decides: "slow" (sleeps until killed), "fail" (turn.failed),
"crash" (stderr + exit 2), otherwise a reasoning item, a command, a file change, a todo list and the answer.
FAKE_ARGV_LOG: append argv (and the stdin prompt) as JSON. With CODEX_HOME set, a finished turn also appends
to the thread's rollout (turn_context + token_count) unless the prompt says "norollout".
FAKE_CODEX_NOTICE=1 (or "notice" in the prompt): every turn opens with the CLI's config notice, sent twice (as an
``error`` event and an ``error`` item), the way Codex reports an unrecognized config.toml setting (§16.6).
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
        handle.write(json.dumps({"argv": args, "prompt": prompt, "path": os.environ.get("PATH"),
                                 "console": os.environ.get("ANX_CONSOLE_URL")}) + "\n")
# `exec resume [--image=<file>...] <id> <prompt>`: the first argument after "resume" that is not an option
thread = next(a for a in args[args.index("resume") + 1:] if not a.startswith("-")) if "resume" in args \
    else str(uuid.uuid4())


def emit(obj):
    print(json.dumps(obj), flush=True)


emit({"type": "thread.started", "thread_id": thread})
emit({"type": "turn.started"})
if os.environ.get("FAKE_CODEX_NOTICE") == "1" or "notice" in prompt:
    note = "Codex is ignoring 1 unrecognized configuration setting: model_reasoning_summary_format"
    emit({"type": "error", "message": note})
    emit({"type": "item.completed", "item": {"id": "item_n", "type": "error", "message": note}})
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
if os.environ.get("CODEX_HOME") and "norollout" not in prompt:
    # what the real CLI writes next to its output: the rollout, with the context window (§16.3)
    folder = os.path.join(os.environ["CODEX_HOME"], "sessions", "2026", "11", "27")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"rollout-2026-11-27T10-00-00-{thread}.jsonl")
    with open(path, "a", encoding="utf-8") as handle:
        handle.writelines(json.dumps(record) + "\n" for record in (
                {"type": "session_meta", "payload": {"id": thread, "cwd": os.getcwd()}},
                {"type": "turn_context", "payload": {"model": "gpt-5-codex", "cwd": os.getcwd()}},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": 5000, "output_tokens": 70},
                    "last_token_usage": {"input_tokens": 1200, "cached_input_tokens": 1000, "output_tokens": 30},
                    "model_context_window": 258400}}}))
emit({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 7}})
