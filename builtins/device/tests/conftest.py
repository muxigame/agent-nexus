from pathlib import Path

from mcpkit import Service
from mcpkit.config import ServiceConfig

from src.server import DeviceServer
from src.settings import Config


def local_service(root: Path, **overrides) -> Service:
    """A Service that needs no files: tests exercise tools, not deployment."""
    return Service(ServiceConfig(name="workspace", root=root, dir=root, port=8400, **overrides), version="test")


def device_server(root: Path, platform_name: str | None = None, **config) -> DeviceServer:
    config.setdefault("watch_db", str(root / "watchers.sqlite3"))
    config.setdefault("companion_url", "http://127.0.0.1:9")
    config.setdefault("allowed_roots", [str(root)])
    server = DeviceServer(local_service(root), Config(project_root=str(root), **config), platform_name=platform_name)
    # Never reach a real manager from tests, whatever ANX_MANAGER_URL says.
    server.manager_asks = None
    return server
