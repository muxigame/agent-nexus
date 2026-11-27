"""Windows sessions, for the core running as the system service (LocalSystem).

The Windows counterpart of what the Ubuntu system service does with setuid and loginctl:

* :func:`is_system` -- this process is LocalSystem (the "为所有用户安装" service);
* :func:`active_session` -- the session in front of the screen (the console, else an active
  remote desktop session) and its user;
* :func:`spawn` -- start a process as that user, in that session (``WTSQueryUserToken`` +
  ``CreateProcessAsUserW``): the device plugin's screenshots, mouse and UI automation work
  there and nowhere else;
* :class:`ConPty` -- a pseudo console (ConPTY) whose shell runs as that user (remote terminal);
* :func:`peer_session` -- which session a loopback TCP client belongs to (the session helper's
  identity; Windows has no SO_PEERCRED).

Without a user logged in there is no token: callers fall back to LocalSystem.
"""
from __future__ import annotations

import os
import subprocess
import sys

WINDOWS = sys.platform == "win32"

if WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
    _env = ctypes.WinDLL("userenv", use_last_error=True)

    HANDLE = wintypes.HANDLE
    TOKEN_QUERY = 0x0008
    WTS_CURRENT_SERVER_HANDLE = 0
    WTSUserName, WTSDomainName = 5, 7
    WTSActive = 0
    EXTENDED_STARTUPINFO_PRESENT = 0x0008_0000
    CREATE_UNICODE_ENVIRONMENT = 0x0000_0400
    CREATE_NO_WINDOW = 0x0800_0000
    CREATE_NEW_CONSOLE = 0x0000_0010
    STARTF_USESTDHANDLES = 0x0100
    PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x0002_0002
    PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x0002_0016
    NO_SESSION = 0xFFFF_FFFF

    class _StartupInfo(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", HANDLE), ("hStdOutput", HANDLE),
                    ("hStdError", HANDLE)]

    class _StartupInfoEx(ctypes.Structure):
        _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]

    class _ProcessInformation(ctypes.Structure):
        _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE), ("dwProcessId", wintypes.DWORD),
                    ("dwThreadId", wintypes.DWORD)]

    class _SessionInfo(ctypes.Structure):
        _fields_ = [("SessionId", wintypes.DWORD), ("pWinStationName", wintypes.LPWSTR), ("State", ctypes.c_int)]

    class _Coord(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    _k32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
    _k32.ProcessIdToSessionId.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    _k32.GetCurrentProcess.restype = HANDLE
    _k32.CloseHandle.argtypes = (HANDLE,)
    _k32.CreatePipe.argtypes = (ctypes.POINTER(HANDLE), ctypes.POINTER(HANDLE), ctypes.c_void_p, wintypes.DWORD)
    _k32.ReadFile.argtypes = (HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
    _k32.WriteFile.argtypes = (HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
    _k32.CreatePseudoConsole.argtypes = (_Coord, HANDLE, HANDLE, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
    _k32.CreatePseudoConsole.restype = ctypes.c_long
    _k32.ResizePseudoConsole.argtypes = (ctypes.c_void_p, _Coord)
    _k32.ClosePseudoConsole.argtypes = (ctypes.c_void_p,)
    _k32.InitializeProcThreadAttributeList.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                                       ctypes.POINTER(ctypes.c_size_t))
    _k32.UpdateProcThreadAttribute.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                               ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p)
    _k32.DeleteProcThreadAttributeList.argtypes = (ctypes.c_void_p,)
    _k32.WaitForSingleObject.argtypes = (HANDLE, wintypes.DWORD)
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.GetExitCodeProcess.argtypes = (HANDLE, ctypes.POINTER(wintypes.DWORD))
    _k32.TerminateProcess.argtypes = (HANDLE, wintypes.UINT)
    _adv.OpenProcessToken.argtypes = (HANDLE, wintypes.DWORD, ctypes.POINTER(HANDLE))
    _adv.GetTokenInformation.argtypes = (HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD))
    _adv.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    _adv.CreateProcessAsUserW.argtypes = (HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                          wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                          ctypes.POINTER(_StartupInfoEx), ctypes.POINTER(_ProcessInformation))
    _wts.WTSQueryUserToken.argtypes = (wintypes.ULONG, ctypes.POINTER(HANDLE))
    _wts.WTSQuerySessionInformationW.argtypes = (HANDLE, wintypes.DWORD, ctypes.c_int,
                                                 ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD))
    _wts.WTSEnumerateSessionsW.argtypes = (HANDLE, wintypes.DWORD, wintypes.DWORD,
                                           ctypes.POINTER(ctypes.POINTER(_SessionInfo)), ctypes.POINTER(wintypes.DWORD))
    _wts.WTSFreeMemory.argtypes = (ctypes.c_void_p,)
    _env.CreateEnvironmentBlock.argtypes = (ctypes.POINTER(ctypes.c_void_p), HANDLE, wintypes.BOOL)
    _env.DestroyEnvironmentBlock.argtypes = (ctypes.c_void_p,)


def _error() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


# --------------------------------------------------------------------------- who and where

_system: bool | None = None


def is_system() -> bool:
    """This process runs as LocalSystem (S-1-5-18): the system service."""
    global _system
    if not WINDOWS:
        return False
    if _system is None:
        token = HANDLE()
        _system = False
        if _adv.OpenProcessToken(_k32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
            try:
                size = wintypes.DWORD()
                _adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
                buffer = ctypes.create_string_buffer(size.value)
                if _adv.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                    text = wintypes.LPWSTR()
                    if _adv.ConvertSidToStringSidW(ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents.Sid,
                                                   ctypes.byref(text)):
                        _system = text.value == "S-1-5-18"
                        ctypes.windll.kernel32.LocalFree(text)
            finally:
                _k32.CloseHandle(token)
    return _system


def _session_text(session: int, kind: int) -> str:
    buffer, size = wintypes.LPWSTR(), wintypes.DWORD()
    if not _wts.WTSQuerySessionInformationW(WTS_CURRENT_SERVER_HANDLE, session, kind, ctypes.byref(buffer),
                                            ctypes.byref(size)):
        return ""
    try:
        return buffer.value or ""
    finally:
        _wts.WTSFreeMemory(buffer)


def session_user(session: int) -> tuple[str, str]:
    """(domain, user) of a session; ("", "") when nobody is logged on there."""
    return _session_text(session, WTSDomainName), _session_text(session, WTSUserName)


def active_session() -> dict | None:
    """The session in front of the screen with a user in it: {id, uid (= session id), user, domain, type}."""
    if not WINDOWS:
        return None
    candidates = []
    console = _k32.WTSGetActiveConsoleSessionId()
    if console != NO_SESSION:
        candidates.append(int(console))
    pointer, count = ctypes.POINTER(_SessionInfo)(), wintypes.DWORD()
    if _wts.WTSEnumerateSessionsW(WTS_CURRENT_SERVER_HANDLE, 0, 1, ctypes.byref(pointer), ctypes.byref(count)):
        try:
            for index in range(count.value):
                item = pointer[index]
                if item.State == WTSActive and item.SessionId not in candidates:
                    candidates.append(int(item.SessionId))
        finally:
            _wts.WTSFreeMemory(pointer)
    for session in candidates:
        domain, user = session_user(session)
        if user:
            return {"id": str(session), "uid": session, "user": user, "domain": domain,
                    "type": "console" if session == console else "rdp"}
    return None


def process_session(pid: int) -> int | None:
    value = wintypes.DWORD()
    if WINDOWS and _k32.ProcessIdToSessionId(pid, ctypes.byref(value)):
        return int(value.value)
    return None


def peer_session(peer: tuple, local: tuple) -> int | None:
    """The session of the process on the other end of a loopback TCP connection."""
    import psutil
    try:
        for conn in psutil.net_connections(kind="tcp4"):
            if conn.pid and conn.laddr and conn.raddr and (conn.laddr.ip, conn.laddr.port) == tuple(peer[:2]) \
                    and (conn.raddr.ip, conn.raddr.port) == tuple(local[:2]):
                return process_session(conn.pid)
    except (psutil.Error, OSError):
        return None
    return None


def peer_pid(peer: tuple, local: tuple) -> int | None:
    """The process on the other end of a loopback TCP connection (from the system's table, not its word)."""
    import psutil
    try:
        for conn in psutil.net_connections(kind="tcp4"):
            if conn.pid and conn.laddr and conn.raddr and (conn.laddr.ip, conn.laddr.port) == tuple(peer[:2]) \
                    and (conn.raddr.ip, conn.raddr.port) == tuple(local[:2]):
                return conn.pid
    except (psutil.Error, OSError):
        return None
    return None


def ensure_window(exe: str | None, session: int) -> bool:
    """The tray window in ``session`` (an update closed it): start it as that user if it is not there."""
    import psutil
    if not exe or not os.path.isfile(exe):
        return False
    name = os.path.basename(exe).lower()
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            if (proc.info["name"] or "").lower() == name and process_session(proc.info["pid"]) == session:
                return False
        except psutil.Error:
            continue
    try:
        spawn(session, [exe, "--autostart"], os.path.dirname(exe), environment(session))
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- the user's token

class _Token:
    def __init__(self, session: int):
        self.handle = HANDLE()
        if not _wts.WTSQueryUserToken(session, ctypes.byref(self.handle)):
            raise _error()

    def __enter__(self):
        return self.handle

    def __exit__(self, *_):
        _k32.CloseHandle(self.handle)


def environment(session: int) -> dict[str, str]:
    """The user's own environment (their profile folders, PATH, TEMP), as their logon gives it."""
    with _Token(session) as token:
        block = ctypes.c_void_p()
        if not _env.CreateEnvironmentBlock(ctypes.byref(block), token, False):
            raise _error()
        try:
            result, offset = {}, 0
            while True:
                text = ctypes.wstring_at(block.value + offset)
                if not text:
                    break
                offset += (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
                key, sep, value = text.partition("=")
                if sep and key:
                    result[key] = value
            return result
        finally:
            _env.DestroyEnvironmentBlock(block)


def _environment_block(env: dict[str, str]):
    items = sorted(env.items(), key=lambda item: item[0].upper())
    return ctypes.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in items) + "\0\0")


def _attribute_list(count: int):
    size = ctypes.c_size_t()
    _k32.InitializeProcThreadAttributeList(None, count, 0, ctypes.byref(size))
    attributes = ctypes.create_string_buffer(size.value)
    if not _k32.InitializeProcThreadAttributeList(attributes, count, 0, ctypes.byref(size)):
        raise _error()
    return attributes


def _create(session: int, argv: list[str], cwd, env: dict[str, str] | None, flags: int, startup,
            inherit: bool = True) -> tuple[int, int]:
    info = _ProcessInformation()
    command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(item) for item in argv]))
    block = _environment_block(env) if env is not None else None
    with _Token(session) as token:
        if not _adv.CreateProcessAsUserW(token, None, command, None, None, inherit,
                                         flags | EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
                                         block, str(cwd) if cwd else None, ctypes.byref(startup), ctypes.byref(info)):
            raise _error()
    _k32.CloseHandle(info.hThread)
    return int(info.dwProcessId), info.hProcess


def spawn(session: int, argv: list[str], cwd, env: dict[str, str] | None, log_file=None, flags: int = 0):
    """Start ``argv`` as the user of ``session``, on their desktop; stdout/stderr to ``log_file``, stdin NUL.

    Returns the supervisor's view of a process (winspawn.HeldProcess)."""
    from .winspawn import HeldProcess
    null = open(os.devnull, "rb")
    attributes = None
    try:
        handles = [msvcrt.get_osfhandle(null.fileno())]
        if log_file is not None:
            handles.append(msvcrt.get_osfhandle(log_file.fileno()))
        for handle in handles:
            os.set_handle_inheritable(handle, True)
        attributes = _attribute_list(1)
        handle_list = (HANDLE * len(handles))(*handles)
        if not _k32.UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handle_list,
                                              ctypes.sizeof(handle_list), None, None):
            raise _error()
        startup = _StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(_StartupInfoEx)
        startup.StartupInfo.lpDesktop = "winsta0\\default"
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = handles[0]
        startup.StartupInfo.hStdOutput = handles[-1] if log_file is not None else None
        startup.StartupInfo.hStdError = handles[-1] if log_file is not None else None
        startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
        pid, process = _create(session, argv, cwd, env, flags, startup)
        return HeldProcess(pid, process)
    finally:
        if attributes is not None:
            _k32.DeleteProcThreadAttributeList(attributes)
        null.close()


def open_window(session: int, argv: list[str], cwd, env: dict[str, str] | None) -> int:
    """Start ``argv`` as the user of ``session`` in a window of its own on their desktop (a console
    program gets a new console; nothing is inherited, no standard handles): what 在本机终端打开
    needs from the system service, whose own children would run unseen in session 0. The pid."""
    attributes = _attribute_list(1)  # empty, but EXTENDED_STARTUPINFO_PRESENT wants one
    try:
        startup = _StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(_StartupInfoEx)
        startup.StartupInfo.lpDesktop = "winsta0\\default"
        startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
        pid, process = _create(session, argv, cwd, env, CREATE_NEW_CONSOLE, startup, inherit=False)
        _k32.CloseHandle(process)
        return pid
    finally:
        _k32.DeleteProcThreadAttributeList(attributes)


# --------------------------------------------------------------------------- ConPTY as the user

class ConPty:
    """A pseudo console with ``argv`` running in it as the user of ``session`` (the remote terminal).

    The same surface terminal._Pty uses from pywinpty: read / write / setwinsize / terminate /
    wait / exitstatus -- with bytes instead of str."""

    def __init__(self, session: int, argv: list[str], cwd, env: dict[str, str] | None, cols: int, rows: int):
        self.exitstatus: int | None = None
        in_read, in_write, out_read, out_write = HANDLE(), HANDLE(), HANDLE(), HANDLE()
        if not _k32.CreatePipe(ctypes.byref(in_read), ctypes.byref(in_write), None, 0):
            raise _error()
        if not _k32.CreatePipe(ctypes.byref(out_read), ctypes.byref(out_write), None, 0):
            raise _error()
        self._console = ctypes.c_void_p()
        result = _k32.CreatePseudoConsole(_Coord(cols, rows), in_read, out_write, 0, ctypes.byref(self._console))
        _k32.CloseHandle(in_read)
        _k32.CloseHandle(out_write)
        if result != 0:
            raise OSError(f"CreatePseudoConsole failed (0x{result & 0xFFFFFFFF:08x})")
        self._input, self._output = in_write, out_read
        attributes = _attribute_list(1)
        try:
            if not _k32.UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, self._console,
                                                  ctypes.sizeof(ctypes.c_void_p), None, None):
                raise _error()
            startup = _StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(_StartupInfoEx)
            startup.StartupInfo.lpDesktop = "winsta0\\default"
            # NULL standard handles, explicitly: otherwise the shell gets this process's own (the
            # service's NUL stdin) instead of the pseudo console, reads EOF and exits at once.
            startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
            startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
            self.pid, self._process = _create(session, argv, cwd, env, 0, startup, inherit=False)
        except OSError:
            self._close_console()
            raise
        finally:
            _k32.DeleteProcThreadAttributeList(attributes)
        import threading
        # A pseudo console outlives its shell: close it when the shell ends, so read() sees EOF.
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self) -> None:
        self.wait()
        self._close_console()

    def read(self, size: int = 65536) -> bytes:
        buffer = ctypes.create_string_buffer(size)
        done = wintypes.DWORD()
        if not _k32.ReadFile(self._output, buffer, size, ctypes.byref(done), None) or done.value == 0:
            raise EOFError("the terminal closed")
        return buffer.raw[:done.value]

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            done = wintypes.DWORD()
            chunk = bytes(view[:65536])
            if not _k32.WriteFile(self._input, chunk, len(chunk), ctypes.byref(done), None):
                raise _error()
            view = view[done.value:]

    def setwinsize(self, rows: int, cols: int) -> None:
        _k32.ResizePseudoConsole(self._console, _Coord(cols, rows))

    def wait(self, timeout: float | None = None) -> int | None:
        _k32.WaitForSingleObject(self._process, 0xFFFF_FFFF if timeout is None else int(timeout * 1000))
        code = wintypes.DWORD()
        if _k32.GetExitCodeProcess(self._process, ctypes.byref(code)) and code.value != 259:  # STILL_ACTIVE
            self.exitstatus = int(code.value)
        return self.exitstatus

    def _close_console(self) -> None:
        console, self._console = self._console, None
        if console:
            _k32.ClosePseudoConsole(console)  # ends the output pipe: read() returns EOF

    def terminate(self, force: bool = True) -> None:
        if getattr(self, "_process", None):
            _k32.TerminateProcess(self._process, 1)
        self._close_console()

    def __del__(self):
        for name in ("_input", "_output", "_process"):
            handle = getattr(self, name, None)
            if handle:
                try:
                    _k32.CloseHandle(handle)
                except Exception:
                    pass
