"""Environment variables: ``ANX_*`` first, then the legacy ``MCPM_*`` name.

Every variable agent-nexus reads goes through :func:`get`, so ``MCPM_ROOT``,
``MCPM_AGENT_TOKEN`` and the rest keep working until 1.0. A legacy name that
actually takes effect prints one deprecation line per process and name.
"""
from __future__ import annotations

import os
import sys
import threading
from collections.abc import Mapping

PREFIX = "ANX_"
LEGACY_PREFIX = "MCPM_"

_warned: set[str] = set()
_lock = threading.Lock()


def names(name: str) -> tuple[str, str]:
    """``("ANX_<name>", "MCPM_<name>")`` for a bare or prefixed name."""
    bare = name.removeprefix(PREFIX).removeprefix(LEGACY_PREFIX)
    return PREFIX + bare, LEGACY_PREFIX + bare


def get(name: str, default: str | None = None, *, environ: Mapping[str, str] | None = None,
        warn: bool = True) -> str | None:
    """The value of ``ANX_<name>``, else ``MCPM_<name>``, else ``default``.

    Empty values count as unset, as every caller treated them before.
    """
    environ = os.environ if environ is None else environ
    new, old = names(name)
    value = environ.get(new)
    if value:
        return value
    value = environ.get(old)
    if value:
        if warn:
            _deprecated(old, new)
        return value
    return default


def _deprecated(old: str, new: str) -> None:
    with _lock:
        if old in _warned:
            return
        _warned.add(old)
    print(f"agent-nexus: {old} is deprecated; set {new} instead", file=sys.stderr, flush=True)


def both(name: str, value: str) -> dict[str, str]:
    """Both spellings, for child processes that may still read the old one."""
    new, old = names(name)
    return {new: value, old: value}


def resolve_token_env(configured: str | None, bare: str, *, environ: Mapping[str, str] | None = None) -> str:
    """The variable a token is read from.

    An explicitly configured ``token_env`` is data and is used as written. The
    default looks for ``ANX_<bare>`` and falls back to ``MCPM_<bare>``.
    """
    if configured:
        return configured
    environ = os.environ if environ is None else environ
    new, old = names(bare)
    if environ.get(new) or not environ.get(old):
        return new
    return old


def _reset_warnings() -> None:  # tests
    with _lock:
        _warned.clear()
