"""Device-side waking (design §8.7, milestone M7).

The manager answers the gateway's ``session_wake`` and ``session_probe``
reverse RPCs itself -- not through the device plugin's exec policy, so waking
Claude / Codex works even when the plugin is down:

* ``adapters``: command templates, executable lookup and the permission flags
  of decision D2;
* ``runner``: starts the woken process detached, logs it under
  ``.anx/wake/<job>.log``, survives a manager restart through
  ``.anx/wake/<job>.json`` and reports the exit with ``hub_call wake_report``;
* ``probe``: is the session in use right now (process, session file, hook)?
* ``doctor``: ``anx wake doctor``.
"""
from .adapters import WakeError, build_argv, permission_flags, resolve_executable
from .runner import WakeRunner

__all__ = ["WakeError", "WakeRunner", "build_argv", "permission_flags", "resolve_executable"]
