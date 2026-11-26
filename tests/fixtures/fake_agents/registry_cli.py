"""A stand-in agent CLI for the registry tests: ``registry_cli.py <kind> <args...>``.

Behaviour comes from the environment so one script plays every CLI:
  FAKE_VERSION   what ``--version`` prints (default "1.2.3")
  FAKE_LOGGED_IN "1" / "0": ``claude auth status`` / ``codex login status`` answer
  FAKE_SLOW      seconds to sleep before answering (timeouts)
  FAKE_MARK      a file to append each invocation's argv to (proves what ran)
"""
import json
import os
import sys
import time

kind, args = sys.argv[1], sys.argv[2:]
mark = os.environ.get("FAKE_MARK")
if mark:
    with open(mark, "a", encoding="utf-8") as handle:
        handle.write(json.dumps([kind, *args]) + "\n")
time.sleep(float(os.environ.get("FAKE_SLOW") or 0))
logged_in = os.environ.get("FAKE_LOGGED_IN") == "1"

if args[:1] in (["--version"], ["-v"]):
    version = os.environ.get("FAKE_VERSION", "1.2.3")
    print(f"{version} ({kind})" if kind == "claude" else f"{kind}-cli {version}")
    sys.exit(0)
if kind == "claude" and args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": logged_in, "authMethod": "claude.ai" if logged_in else None}))
    sys.exit(0 if logged_in else 1)
if kind == "codex" and args[:2] == ["login", "status"]:
    # Codex writes this to stderr (docs/agent-chat.md §14).
    print("Logged in using ChatGPT" if logged_in else "Not logged in", file=sys.stderr)
    sys.exit(0 if logged_in else 1)
print(f"unexpected arguments: {args}", file=sys.stderr)
sys.exit(2)
