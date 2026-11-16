"""Compatibility shim for ``agent_nexus.federation`` (removed in 1.0)."""
import sys as _sys

from agent_nexus import federation as _target

_sys.modules[__name__] = _target
