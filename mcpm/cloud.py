"""Compatibility shim for ``agent_nexus.cloud`` (removed in 1.0)."""
import sys as _sys

from agent_nexus import cloud as _target

if __name__ == "__main__":  # python -m mcpm.cloud
    _sys.exit(_target.main())
_sys.modules[__name__] = _target
