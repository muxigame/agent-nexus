"""A stand-in agent CLI for the hub tests: ``hub_cli.py <kind> <args...>``.

``--version`` and the login checks answer here (FAKE_VERSION, FAKE_LOGGED_IN as in registry_cli.py); anything
else runs the matching conversation fake (fake_claude.py / fake_codex.py / fake_acp.py) with the same arguments.
"""
import json
import os
import sys

kind, args = sys.argv[1], sys.argv[2:]
here = os.path.dirname(os.path.abspath(__file__))
logged_in = os.environ.get("FAKE_LOGGED_IN", "1") == "1"
if args[:1] in (["--version"], ["-v"]):
    version = os.environ.get("FAKE_VERSION", "2.3.4")
    print(f"{version} (Claude Code)" if kind == "claude" else f"{kind}-cli {version}")
    sys.exit(0)
if kind == "claude" and args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": logged_in, "authMethod": "claude.ai" if logged_in else None}))
    sys.exit(0 if logged_in else 1)
if kind == "codex" and args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT" if logged_in else "Not logged in", file=sys.stderr)
    sys.exit(0 if logged_in else 1)
script = {"claude": "fake_claude.py", "codex": "fake_codex.py"}.get(kind, "fake_acp.py")
os.execv(sys.executable, [sys.executable, os.path.join(here, script), *args])
