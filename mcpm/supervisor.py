"""Compatibility shim for ``agent_nexus.supervisor`` (removed in 1.0)."""
import sys as _sys

from agent_nexus import supervisor as _target

_sys.modules[__name__] = _target
