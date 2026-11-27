"""Helper processes that end with the manager, however the manager ends.

``muxi-p2p-agent`` (the sidecar and the ``connect`` forwarders) used to survive a manager that
crashed or was killed (``os._exit``, Task Manager, SIGKILL): the orphan kept its log file locked
(so the root folder could not be deleted on Windows) and kept calling the old control plane.
:func:`spawn` starts a helper so that the operating system ends it with the manager:

* Windows: the helper starts suspended, goes into the manager's kill-on-close job object
  (:mod:`agent_nexus.winjob`), then resumes. The job handle dies with the manager, and the kernel
  kills everything in the job.
* Linux (and Android): ``prctl(PR_SET_PDEATHSIG, SIGTERM)`` between fork and exec. The signal
  follows the *thread* that forked, which is the event loop's: it lives as long as the manager.
* macOS and other POSIX systems: a ``/bin/sh`` watchdog whose stdin is a pipe only the manager
  holds. When the manager dies the kernel closes the pipe, ``read`` returns, and the watchdog
  sends the helper SIGTERM. Nothing is needed from the helper itself, and nothing polls.

The normal shutdown (terminate, wait, kill) is unchanged; :meth:`Lifeline.release` only removes
the watchdog once the helper has exited.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys

log = logging.getLogger(__name__)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PR_SET_PDEATHSIG = 1

#: how a helper is tied to the manager here: "job", "pdeathsig", "watchdog" or None
METHOD: str | None
if os.name == "nt":
    METHOD = "job"
elif sys.platform.startswith("linux"):
    METHOD = "pdeathsig"
else:
    METHOD = "watchdog"

_prctl = None
if sys.platform.startswith("linux"):
    try:
        import ctypes
        # Looked up now, not after fork: loading a library in the child of a threaded process
        # can deadlock.
        _prctl = ctypes.CDLL(None, use_errno=True).prctl
        _prctl.restype = ctypes.c_int
        _prctl.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong)
    except (OSError, AttributeError) as error:
        log.debug("lifeline: no prctl (%s)", error)
        _prctl = None


def _die_with(manager: int):
    def preexec() -> None:
        _prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
        if os.getppid() != manager:  # the manager already died before prctl took effect
            os._exit(1)
    return preexec


class Lifeline:
    """What ties one helper to the manager; :meth:`release` once the helper has exited."""

    def __init__(self, watchdog: subprocess.Popen | None = None, pipe: int | None = None):
        self.watchdog, self.pipe = watchdog, pipe

    def release(self) -> None:
        watchdog, self.watchdog = self.watchdog, None
        if watchdog is not None:
            # Kill the watchdog itself first: closing the pipe would make it signal a pid
            # that may already belong to someone else.
            with contextlib.suppress(OSError):
                watchdog.kill()
            with contextlib.suppress(Exception):
                watchdog.wait(5)
        pipe, self.pipe = self.pipe, None
        if pipe is not None:
            with contextlib.suppress(OSError):
                os.close(pipe)


def _watch(pid: int) -> Lifeline:
    """A ``/bin/sh`` that sends ``pid`` SIGTERM when this process's end of a pipe closes."""
    read_end, write_end = os.pipe()  # both non-inheritable: only the watchdog gets the read end
    try:
        watchdog = subprocess.Popen(
            ["/bin/sh", "-c", 'read _ ; kill -TERM "$1" 2>/dev/null', "anx-lifeline", str(pid)],
            stdin=read_end, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)  # not in the terminal's group: a Ctrl+C must not end it first
    except OSError:
        os.close(write_end)
        raise
    finally:
        os.close(read_end)
    return Lifeline(watchdog, write_end)


async def spawn(*args: str, method: str | None = None, **kwargs) -> tuple[asyncio.subprocess.Process, Lifeline]:
    """``asyncio.create_subprocess_exec`` for a helper that must not outlive this process.

    Without a way to tie it (no job object, no prctl, no /bin/sh) the helper still starts, as
    before, and a warning says it may outlive the manager.
    """
    method = METHOD if method is None else method
    flags = kwargs.pop("creationflags", NO_WINDOW)
    if method == "job":
        from . import winjob
        held = winjob.job() is not None
        process = await asyncio.create_subprocess_exec(
            *args, creationflags=flags | (winjob.CREATE_SUSPENDED if held else 0), **kwargs)
        if held:
            popen = process._transport.get_extra_info("subprocess")  # type: ignore[attr-defined]
            if not winjob.assign(int(popen._handle)):
                log.warning("lifeline: %s (pid %s) may outlive the manager", args[0], process.pid)
            try:
                import psutil
                psutil.Process(process.pid).resume()
            except Exception:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                raise
        return process, Lifeline()
    if method == "pdeathsig" and _prctl is not None:
        process = await asyncio.create_subprocess_exec(
            *args, preexec_fn=_die_with(os.getpid()), creationflags=flags, **kwargs)
        return process, Lifeline()
    process = await asyncio.create_subprocess_exec(*args, creationflags=flags, **kwargs)
    if method in ("watchdog", "pdeathsig"):  # pdeathsig without prctl: the watchdog instead
        try:
            return process, _watch(process.pid)
        except OSError as error:
            log.warning("lifeline: no watchdog for %s (%s); it may outlive the manager", args[0], error)
    return process, Lifeline()
