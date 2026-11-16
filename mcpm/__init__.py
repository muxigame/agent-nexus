"""Compatibility shim: ``mcpm`` is now ``agent_nexus``.

Kept until 1.0 so that ``python -m mcpm.cli serve --tray`` (old desktop
bootstraps), ``python -m mcpm.cloud`` (old systemd units) and ``import mcpm.X``
keep working. Every ``mcpm.X`` module *is* ``agent_nexus.X`` (same module
object), so state and monkeypatches are shared.
"""
from agent_nexus import __version__  # noqa: F401
