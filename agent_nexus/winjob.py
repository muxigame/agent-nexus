"""One kill-on-close Windows job object per manager process.

Every server the supervisor starts goes into it, so servers always end with
the manager that started them. A manager that crashes or is killed leaves no
orphaned plugin processes behind, and the next manager starts them again as
its own children: the process tree (and Task Manager's grouping) stays in one
piece under ``agent-nexus.exe``.

The job allows breakaway, so a server that deliberately starts something
meant to outlive it (``CREATE_BREAKAWAY_FROM_JOB``) still can. Elsewhere than
Windows every function here is a no-op.
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger(__name__)

JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0000_0800
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x0000_2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
CREATE_SUSPENDED = 0x0000_0004

_lock = threading.Lock()
_job: int | None = None
_failed = False

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.IsProcessInJob.restype = wintypes.BOOL
    _kernel32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))


def job() -> int | None:
    """The process-wide job handle, created on first use; None where unavailable.

    The handle is never closed: the kernel closes it when this process ends,
    which is exactly what kills the servers with it.
    """
    global _job, _failed
    if os.name != "nt" or _failed:
        return None
    with _lock:
        if _job is None and not _failed:
            handle = _kernel32.CreateJobObjectW(None, None)
            info = _ExtendedLimits()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK
            if not handle or not _kernel32.SetInformationJobObject(
                    handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)):
                log.warning("cannot create the server job object (error %s); servers will not end with the manager",
                            ctypes.get_last_error())
                _failed = True
                return None
            _job = handle
        return _job


def assign(process_handle: int) -> bool:
    """Put a process (by its handle) into the job; False when that is not possible."""
    handle = job()
    if handle is None:
        return False
    if _kernel32.AssignProcessToJobObject(handle, process_handle):
        return True
    log.warning("cannot add a server to the job object (error %s)", ctypes.get_last_error())
    return False


def contains(process_handle: int) -> bool:
    """Whether the process is in this manager's job (tests and diagnostics)."""
    handle = job()
    if handle is None:
        return False
    result = wintypes.BOOL()
    return bool(_kernel32.IsProcessInJob(process_handle, handle, ctypes.byref(result))) and bool(result.value)
