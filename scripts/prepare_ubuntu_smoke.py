"""Prepare a separate package-root instance on the Ubuntu build host."""
import json
import os
import subprocess
from pathlib import Path

base = Path("/opt/agent-nexus/ubuntu-package-smoke")
root = base / "home/client"
env = dict(os.environ, ANX_ROOT=str(root), HOME=str(base / "home"), UV_NATIVE_TLS="true", UV_PYTHON_INSTALL_DIR="/opt/agent-nexus/python")
launcher = base / "install/usr/lib/agent-nexus/ubuntu_launcher.py"
subprocess.run(["/usr/bin/python3", str(launcher), "--prepare"], env=env, check=True)
path = root / "agent-nexus.json"
config = json.loads(path.read_text())
config["manager"]["port"] = 18449
config["services"]["workspace"].update(port=18549)
config["services"]["workspace"]["settings"].update(default_workdir=str(base / "home"), allowed_roots=[str(base / "home")])
config["cloud"]["refresh_interval_s"] = 2
path.write_text(json.dumps(config))
output = (base / "launch.log").open("wb")
proc = subprocess.Popen(["/usr/bin/python3", str(launcher), "--serve"], env=env, stdout=output, stderr=output, start_new_session=True)
print("Ubuntu package instance launched:", proc.pid)
