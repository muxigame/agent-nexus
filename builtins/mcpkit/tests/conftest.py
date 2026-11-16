import json
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

APP = textwrap.dedent('''
    import sys
    from mcpkit import main

    def build(service):
        mcp = service.create_server(instructions="test service")

        @mcp.tool
        def echo(text: str) -> str:
            """Echo text back."""
            return "echo:" + text

        return mcp

    if __name__ == "__main__":
        sys.exit(main(__file__, build, version="1.2.3"))
''')


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class Workspace:
    """A temporary mcp root: mcp-manager.json, secrets/, and service directories."""

    def __init__(self, root: Path):
        self.root = root
        self.central = {"manager": {"port": 8399}, "scan": ["."], "services": {}}
        self.save()

    def save(self) -> None:
        write_json(self.root / "mcp-manager.json", self.central)

    def service(self, name: str, manifest: dict | None = None, entry: dict | None = None, app: str = APP) -> Path:
        directory = self.root / name
        write_json(directory / "mcp-service.json", {"name": name, **(manifest or {})})
        (directory / "app.py").write_text(app, encoding="utf-8")
        if entry is not None:
            self.central["services"][name] = entry
            self.save()
        return directory

    def secret(self, name: str, value: str) -> None:
        path = self.root / "secrets" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="utf-8")


@pytest.fixture
def workspace(tmp_path):
    return Workspace(tmp_path)


class Running:
    def __init__(self, proc: subprocess.Popen, port: int):
        self.proc, self.port = proc, port
        self.url = f"http://127.0.0.1:{port}"

    def output(self) -> str:
        return self.proc.stdout.read().decode("utf-8", "replace") if self.proc.stdout else ""

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=15)


def start(directory: Path, *args: str, port: int | None = None, script: str = "app.py", wait: bool = True) -> Running:
    port = port or free_port()
    proc = subprocess.Popen([sys.executable, script, "--transport", "http", "--port", str(port), *args], cwd=directory,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    running = Running(proc, port)
    if not wait:
        return running
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"service exited early ({proc.returncode}):\n{running.output()}")
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return running
        time.sleep(0.2)
    running.stop()
    raise AssertionError(f"service did not listen on {port}:\n{running.output()}")


@pytest.fixture
def run():
    started: list[Running] = []

    def launch(*a, **kw) -> Running:
        r = start(*a, **kw)
        started.append(r)
        return r

    yield launch
    for r in started:
        r.stop()
