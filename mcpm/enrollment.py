"""Compatibility shim for ``agent_nexus.enrollment`` (removed in 1.0)."""
import sys as _sys

from agent_nexus import enrollment as _target

_sys.modules[__name__] = _target
