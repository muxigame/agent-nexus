"""A stand-in npm / node for the install tests: ``registry_npm.py npm|node <args...>``.

``npm install -g <package>`` prints npm-like ``--loglevel=http`` output and drops a fake CLI shim into
``<prefix>/bin`` (prefix: NPM_CONFIG_PREFIX, else FAKE_NPM_PREFIX). The shim runs registry_cli.py.
  FAKE_NODE_VERSION  what ``node --version`` prints (default v22.3.0)
  FAKE_NPM_FAIL      "eacces" / "network" / "exit": fail that way
  FAKE_NPM_NO_SHIM   "1": succeed without creating the binary
  FAKE_NPM_DELAY     seconds between fetch lines
  FAKE_NPM_BINARY    the binary name to create (default: derived from the package)
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
tool, args = sys.argv[1], sys.argv[2:]

if tool == "node":
    print(os.environ.get("FAKE_NODE_VERSION", "v22.3.0"))
    sys.exit(0)

prefix = os.environ.get("NPM_CONFIG_PREFIX") or os.environ.get("FAKE_NPM_PREFIX", "")
if args[:3] == ["config", "get", "prefix"]:
    print(prefix)
    sys.exit(0)
if args[:1] in (["install"], ["i"]) and "-g" in args:
    package = next(a for a in args[1:] if not a.startswith("-"))
    delay = float(os.environ.get("FAKE_NPM_DELAY") or 0)
    for index in range(12):
        print(f"npm http fetch GET 200 https://registry.npmjs.org/dep-{index} {40 + index}ms (cache miss)", flush=True)
        time.sleep(delay)
    failure = os.environ.get("FAKE_NPM_FAIL")
    if failure == "eacces":
        print("npm error code EACCES", file=sys.stderr)
        print(f"npm error Error: EACCES: permission denied, mkdir '{prefix}/lib/node_modules/{package}'", file=sys.stderr)
        sys.exit(243)
    if failure == "network":
        print("npm error code ENOTFOUND", file=sys.stderr)
        sys.exit(1)
    if failure == "exit":
        sys.exit(7)
    print("\x1b[1mreify\x1b[0m: timing reifyNode:node_modules/x Completed in 20ms", flush=True)
    print(f"> {package}@9.9.9 postinstall\r> node scripts/postinstall.js", flush=True)
    binary = os.environ.get("FAKE_NPM_BINARY") or package.rsplit("/", 1)[-1].replace("-code", "").replace("-cli", "")
    if os.environ.get("FAKE_NPM_NO_SHIM") != "1":
        folder = os.path.join(prefix, "bin")
        os.makedirs(folder, exist_ok=True)
        shim = os.path.join(folder, binary)
        with open(shim, "w", encoding="utf-8") as handle:
            handle.write(f'#!/bin/sh\nexec "{sys.executable}" "{HERE}/registry_cli.py" {binary} "$@"\n')
        os.chmod(shim, 0o755)
    print("added 12 packages in 1s")
    sys.exit(0)
print(f"npm: unexpected {args}", file=sys.stderr)
sys.exit(1)
