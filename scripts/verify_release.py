"""Verify release hashes, privacy boundaries and entry points after relocation."""
from __future__ import annotations

import hashlib
import importlib.util
import argparse
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {".git", ".venv", ".private", "secrets", "runtime", "logs", "__pycache__"}
_bundle_spec = importlib.util.spec_from_file_location("bundle_files", ROOT / "bundle_files.py")
bundle = importlib.util.module_from_spec(_bundle_spec)
_bundle_spec.loader.exec_module(bundle)
# Service builtins actually present in this checkout (business plugins are optional).
SERVICE_BUILTINS = tuple(name for name in bundle.present_builtins(ROOT) if name != "mcpkit")


def verify(out=None):
    out = Path(out or ROOT / "artifacts").resolve()
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    env.pop("UV_RUN_RECURSION_DEPTH", None)
    env.pop("PYTHONPATH", None)
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    for variable in ("ANX_SERVER_TOKEN", "ANX_AGENT_TOKEN", "ANX_OFFICE_TOKEN", "ANX_HOME_TOKEN",
                     "MCPM_SERVER_TOKEN", "MCPM_AGENT_TOKEN", "MCPM_OFFICE_TOKEN", "MCPM_HOME_TOKEN"):
        env[variable] = "release-verification-token-" + "x" * 40
    with tempfile.TemporaryDirectory(prefix="anx-release-") as temporary:
        for kind in ("client", "server"):
            destination = Path(temporary) / kind
            with zipfile.ZipFile(out / f"agent-nexus-{kind}.zip") as archive:
                names = set(archive.namelist())
                assert len(names) == len(archive.namelist()), "duplicate archive members"
                manifest = json.loads(archive.read("SHA256.json"))
                assert set(manifest) == names - {"SHA256.json"}
                for name, expected in manifest.items():
                    assert not any(part in FORBIDDEN for part in Path(name).parts), name
                    assert not Path(name).is_absolute() and ".." not in Path(name).parts
                    assert hashlib.sha256(archive.read(name)).hexdigest() == expected, name
                archive.extractall(destination)
            if kind == "client":
                assert all((destination / "builtins" / name / "mcp-service.json").is_file()
                           for name in SERVICE_BUILTINS)
                if "chrome-devtools-mcp" in SERVICE_BUILTINS:
                    assert (destination / "builtins/chrome-devtools-mcp/node_modules/chrome-devtools-mcp/build/src/bin/chrome-devtools-mcp.js").is_file()
                command = ["anx-agent", "--root", str(destination), "--check-config"]
            else:
                assert not (destination / "builtins" / "device").exists()
                command = ["anx-gateway", "--config", str(destination / "gateway.json"), "--check-config"]
            result = subprocess.run(["uv", "run", "--quiet", "--no-dev", "--project", str(destination),
                                     "--directory", str(destination), *command], env=env, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(f"{kind} package entry point failed:\n{result.stdout}\n{result.stderr}")
            print(f"{kind}: hashes, data exclusions, relocation and entry point passed")
        manager_wheel = next((out / "python").glob("agent_nexus-*-py3-none-any.whl"))
        mcpkit_wheel = next((out / "python").glob("mcpkit-*-py3-none-any.whl"))
        with zipfile.ZipFile(manager_wheel) as wheel:
            assert "agent_nexus/cloud.py" in wheel.namelist() and "agent_nexus/agent.py" in wheel.namelist()
            assert "mcpm/cli.py" in wheel.namelist() and "mcpm/cloud.py" in wheel.namelist()  # shim until 1.0
            assert all(f"agent_nexus/builtins/{name}/mcp-service.json" in wheel.namelist()
                       for name in SERVICE_BUILTINS)
            assert not any(part in FORBIDDEN for name in wheel.namelist() for part in Path(name).parts)
        print("manager wheel: builtins and client/server entry points present, data exclusions passed")
        wheel_env = Path(temporary) / "wheel-env"
        python = wheel_env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        commands = [
            ["uv", "venv", "--python", sys.executable, str(wheel_env)],
            ["uv", "pip", "install", "--quiet", "--python", str(python),
             str(manager_wheel), str(mcpkit_wheel)],
        ]
        instance = Path(temporary) / "wheel-instance"
        instance.mkdir()
        (instance / "agent-nexus.json").write_text('{"builtins": true}', encoding="utf-8")
        code = ('import sys; from pathlib import Path; import agent_nexus, mcpm.cli; '
                'from agent_nexus.registry import discover,load_manager_config; '
                'assert Path(agent_nexus.__file__).is_relative_to(Path(sys.argv[2])); '
                'assert mcpm.cli is sys.modules["agent_nexus.cli"]; '
                'specs=discover(load_manager_config(Path(sys.argv[1]))); '
                'assert len(specs)==6 and all(not s.error for s in specs.values()); '
                'print("installed wheel: 6 runnable builtin manifests discovered")')
        commands.append([str(python), "-c", code, str(instance), str(wheel_env)])
        for command in commands:
            result = subprocess.run(command, cwd=temporary, env=env, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(f"wheel installation verification failed:\n{result.stdout}\n{result.stderr}")
        print(result.stdout.strip())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "artifacts")
    verify(parser.parse_args().out)
