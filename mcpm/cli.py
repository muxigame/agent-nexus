"""Compatibility shim for ``agent_nexus.cli`` (removed in 1.0)."""
import sys as _sys

from agent_nexus import cli as _target

if __name__ == "__main__":  # python -m mcpm.cli
    _sys.exit(_target.main())
_sys.modules[__name__] = _target
