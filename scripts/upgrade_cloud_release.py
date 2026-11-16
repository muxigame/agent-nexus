"""Compatibility wrapper: the gateway upgrade lives in ``deploy/gateway/upgrade.py``.

Kept so existing runbooks keep working::

    python3 upgrade_cloud_release.py <sha256-of-package> [--package PATH] [upgrade.py options...]

The positional hash becomes ``--sha256``; ``--package`` defaults to
``<base>/bootstrap/agent-nexus-server-next.zip`` as before. ``--health-url`` and
every other option are passed through unchanged (see ``upgrade.py --help``).
"""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CANDIDATES = (HERE.parent / "deploy" / "gateway" / "upgrade.py", HERE / "upgrade.py")


def load():
    for path in CANDIDATES:
        if path.is_file():
            spec = importlib.util.spec_from_file_location("anx_gateway_upgrade", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise SystemExit("deploy/gateway/upgrade.py not found next to this script")


def translate(argv):
    argv = list(argv)
    if argv and not argv[0].startswith("-"):
        argv = ["--sha256", argv[0]] + argv[1:]
    if "--package" not in argv:
        base = argv[argv.index("--base") + 1] if "--base" in argv else "/opt/agent-nexus"
        argv += ["--package", str(Path(base) / "bootstrap" / "agent-nexus-server-next.zip")]
    return argv


if __name__ == "__main__":
    sys.exit(load().main(translate(sys.argv[1:])))
