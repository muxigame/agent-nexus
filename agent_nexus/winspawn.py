"""Start servers as children of the desktop shell, not of the manager (Windows).

The desktop shell (``agent-nexus.exe``, named "Agent Nexus" with its icon in
Task Manager) outlives manager restarts. Servers are created with the shell as
their parent (``PROC_THREAD_ATTRIBUTE_PARENT_PROCESS``), so

* every server sits under one "Agent Nexus" node in the process tree, and
* a manager restart (crash, update of the manager, the shell's watchdog) does
  not orphan them: the next manager adopts them where they are, with no
  downtime.

A process created this way inherits handles from the *specified* parent, so
its standard handles are first duplicated into the shell, named explicitly in
a handle list (nothing else of the shell's leaks in), and closed in the shell
again once the server has them.

Without a shell (``anx serve`` from a terminal, other platforms) the
supervisor falls back to children of the manager in its kill-on-close job
(winjob.py).
"""
from __future__ import annotations

import logging
import os
import subprocess

import psutil

log = logging.getLogger(__name__)

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    PROCESS_CREATE_PROCESS = 0x0080
    PROCESS_DUP_HANDLE = 0x0040
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    DUPLICATE_CLOSE_SOURCE = 0x1
    DUPLICATE_SAME_ACCESS = 0x2
    EXTENDED_STARTUPINFO_PRESENT = 0x0008_0000
    CREATE_UNICODE_ENVIRONMENT = 0x0000_0400
    STARTF_USESTDHANDLES = 0x0100
    PROC_THREAD_ATTRIBUTE_PARENT_PROCESS = 0x0002_0000
    PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x0002_0002
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 0x102
    INFINITE = 0xFFFF_FFFF

    class _StartupInfo(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class _StartupInfoEx(ctypes.Structure):
        _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]

    class _ProcessInformation(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.DuplicateHandle.restype = wintypes.BOOL
    _k32.DuplicateHandle.argtypes = (wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE),
                                     wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    _k32.InitializeProcThreadAttributeList.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                                       ctypes.POINTER(ctypes.c_size_t))
    _k32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    _k32.UpdateProcThreadAttribute.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                               ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p)
    _k32.DeleteProcThreadAttributeList.argtypes = (ctypes.c_void_p,)
    _k32.CreateProcessW.restype = wintypes.BOOL
    _k32.CreateProcessW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
                                    wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                    ctypes.POINTER(_StartupInfoEx), ctypes.POINTER(_ProcessInformation))
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _k32.TerminateProcess.restype = wintypes.BOOL
    _k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def holder_pid(environ: dict | None = None) -> int | None:
    """The desktop shell that started this manager (``ANX_DESKTOP_APP``), if it is an ancestor."""
    environ = os.environ if environ is None else environ
    app = environ.get("ANX_DESKTOP_APP") or environ.get("MCPM_DESKTOP_APP")
    if os.name != "nt" or not app:
        return None
    try:
        parents = psutil.Process().parents()
    except psutil.Error:
        return None
    for parent in parents:
        try:
            if _same_file(parent.exe(), app):
                return parent.pid
        except psutil.Error:
            continue
    return None


class HeldProcess:
    """The part of ``subprocess.Popen`` the supervisor uses, for a process started here."""

    def __init__(self, pid: int, handle: int) -> None:
        self.pid = pid
        self._handle = handle
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is None and _k32.WaitForSingleObject(self._handle, 0) == WAIT_OBJECT_0:
            code = wintypes.DWORD()
            _k32.GetExitCodeProcess(self._handle, ctypes.byref(code))
            self.returncode = int(code.value)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        milliseconds = INFINITE if timeout is None else int(timeout * 1000)
        if self.poll() is None and _k32.WaitForSingleObject(self._handle, milliseconds) == WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired("server", timeout)
        return self.poll()  # type: ignore[return-value]

    def kill(self) -> None:
        if self.poll() is None:
            _k32.TerminateProcess(self._handle, 1)

    terminate = kill

    def __del__(self) -> None:
        handle, self._handle = getattr(self, "_handle", None), None
        if handle and os.name == "nt":
            _k32.CloseHandle(handle)


def _environment_block(env: dict[str, str]) -> ctypes.Array:
    # Windows wants the block sorted case-insensitively, double-NUL terminated.
    items = sorted(env.items(), key=lambda item: item[0].upper())
    return ctypes.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in items) + "\0\0")


def spawn_under(parent_pid: int, argv: list[str], cwd, env: dict[str, str] | None, log_file, flags: int) -> HeldProcess:
    """Start ``argv`` as a child of ``parent_pid``; stdout and stderr go to ``log_file``, stdin is NUL."""
    if os.name != "nt":
        raise OSError("spawn_under is Windows-only")
    parent = _k32.OpenProcess(PROCESS_CREATE_PROCESS | PROCESS_DUP_HANDLE | PROCESS_QUERY_LIMITED_INFORMATION,
                              False, parent_pid)
    if not parent:
        raise ctypes.WinError(ctypes.get_last_error())
    me = _k32.GetCurrentProcess()
    null = open(os.devnull, "rb")
    remote: list[int] = []
    attributes = None
    try:
        for local in (msvcrt.get_osfhandle(null.fileno()), msvcrt.get_osfhandle(log_file.fileno())):
            target = wintypes.HANDLE()
            if not _k32.DuplicateHandle(me, local, parent, ctypes.byref(target), 0, True, DUPLICATE_SAME_ACCESS):
                raise ctypes.WinError(ctypes.get_last_error())
            remote.append(target.value)
        size = ctypes.c_size_t()
        _k32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        attributes = ctypes.create_string_buffer(size.value)
        if not _k32.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        parent_value = wintypes.HANDLE(parent)
        handle_list = (wintypes.HANDLE * len(remote))(*remote)
        if not _k32.UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_PARENT_PROCESS,
                                              ctypes.byref(parent_value), ctypes.sizeof(parent_value), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
        if not _k32.UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                                              handle_list, ctypes.sizeof(handle_list), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
        startup = _StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(_StartupInfoEx)
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = remote[0]
        startup.StartupInfo.hStdOutput = remote[1]
        startup.StartupInfo.hStdError = remote[1]
        startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
        info = _ProcessInformation()
        block = _environment_block(env) if env is not None else None
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(item) for item in argv]))
        if not _k32.CreateProcessW(None, command, None, None, True,
                                   flags | EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
                                   block, str(cwd) if cwd else None, ctypes.byref(startup), ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        _k32.CloseHandle(info.hThread)
        return HeldProcess(int(info.dwProcessId), info.hProcess)
    finally:
        if attributes is not None:
            _k32.DeleteProcThreadAttributeList(attributes)
        for value in remote:  # the shell's copies; the server has its own now
            _k32.DuplicateHandle(parent, value, None, None, 0, False, DUPLICATE_CLOSE_SOURCE)
        null.close()
        _k32.CloseHandle(parent)
