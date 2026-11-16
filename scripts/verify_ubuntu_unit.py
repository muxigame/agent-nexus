"""Validate the generated unit offline, without starting or changing production services."""
import importlib.util
import os
from pathlib import Path
import subprocess

base = Path("/opt/agent-nexus/ubuntu-package-smoke")
os.environ["ANX_ROOT"] = str(base / "home/client")
os.environ["XDG_CONFIG_HOME"] = str(base / "unit-check")
spec = importlib.util.spec_from_file_location("ubuntu_shell", base / "install/usr/lib/agent-nexus/ubuntu_launcher.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
run = subprocess.run
calls = []
try:
    subprocess.run = lambda command, **kwargs: calls.append(command)
    module.install_unit()
finally:
    subprocess.run = run
unit = base / "unit-check/systemd/user/agent-nexus.service"
assert "KillMode=mixed" in unit.read_text()
assert "ANX_LOG_STDIO=1" in unit.read_text() and "[Install]" in unit.read_text()
run(["systemd-analyze", "verify", str(unit)], check=True)
print("Generated systemd unit validated; no service was started")
