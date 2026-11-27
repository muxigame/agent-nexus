"""The session helper: remote desktop from a core that runs as root (the Ubuntu system service).

The core has no display: it runs as a system service, outside every login. The person's
graphical session runs ``agent-nexus session`` (XDG autostart), which keeps a control connection
to the core's socket::

    helper -> core   ANX-SESSION/1 {"hello": {...display, session id...}}\\n
    core   -> helper {"open": "<id>", "request": {...}}\\n          (one per desktop viewer)
    helper -> core   ANX-SESSION/1 {"attach": "<id>"}\\n            (a new connection)
                     ... then the remote hub's desktop stream (READY/ERROR frames, RFB) ...

The core finds the helper of the *active* session (loginctl: Active=yes, x11/wayland, local) by
the uid the kernel reports for the connection (SO_PEERCRED), and only pipes bytes: capture and
input happen in the person's own session, with their own rights.

Windows (the LocalSystem service): the same protocol over loopback TCP (the port is in
``<root>/session.port``); a helper is known by its *session* (winsession.peer_session), which
plays the uid's part, and the active session is the console's (else an active remote desktop).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__

log = logging.getLogger("agent_nexus.session")

WINDOWS = sys.platform == "win32"
SOCKET_DIR = Path(os.environ.get("ANX_SESSION_DIR") or "/run/agent-nexus")
PORT_FILE = "session.port"
SOCKET_NAME = "session.sock"
MAGIC = b"ANX-SESSION/1 "
ATTACH_TIMEOUT = 15.0
#: a login's helper is replaced (another release than the core) at most this often
REPLACE_INTERVAL_S = 60.0


def socket_path() -> Path:
    return SOCKET_DIR / SOCKET_NAME


def supported() -> bool:
    """The core side: Linux as root, Windows as LocalSystem (a per-user core has its own display)."""
    if WINDOWS:
        from . import winsession
        return winsession.is_system()
    return sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0


def graphical_target() -> bool:
    """A machine with a desktop: a display manager is running (a server has none). Windows: always."""
    if WINDOWS:
        return True
    try:
        result = subprocess.run(["systemctl", "is-active", "--quiet", "display-manager"], timeout=5)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def active_session() -> dict | None:
    """The local graphical session in front of the screen: {id, uid, user, type}, or None."""
    if WINDOWS:
        from . import winsession
        return winsession.active_session()
    try:
        listing = subprocess.run(["loginctl", "list-sessions", "--no-legend"], capture_output=True, text=True,
                                 timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in listing.splitlines():
        parts = line.split()
        if not parts:
            continue
        try:
            shown = subprocess.run(["loginctl", "show-session", parts[0], "-p", "Active", "-p", "Type", "-p", "User",
                                    "-p", "Name", "-p", "Remote", "-p", "Class"],
                                   capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        props = dict(item.split("=", 1) for item in shown.splitlines() if "=" in item)
        if props.get("Active") == "yes" and props.get("Type") in ("x11", "wayland") \
                and props.get("Remote") != "yes" and props.get("Class", "user") == "user":
            # systemd calls it User (the uid); list-sessions' second column is the same number.
            try:
                uid = int(props.get("User") or props.get("Uid") or (parts[1] if len(parts) > 1 else ""))
            except ValueError:
                continue
            return {"id": parts[0], "uid": uid, "user": props.get("Name", ""), "type": props.get("Type")}
    return None


#: what the helper needs from the session it shares (taken from that session's own processes)
SESSION_ENV = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
               "XDG_SESSION_ID", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "LANG")


def session_environment(active: dict) -> dict | None:
    """The display environment of the active session, read from one of its processes."""
    best = None
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != active["uid"]:
                continue
            raw = (entry / "environ").read_bytes()
        except OSError:
            continue
        env = dict(item.split("=", 1) for item in raw.decode("utf-8", "replace").split("\0") if "=" in item)
        if not env.get("DISPLAY"):
            continue
        picked = {key: env[key] for key in SESSION_ENV if key in env}
        if env.get("XDG_SESSION_ID") == active["id"]:
            return picked
        best = best or picked
    return best


def _launch_windows(root: Path, active: dict) -> bool:
    from . import winsession
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    project = Path(__file__).resolve().parent.parent
    try:
        env = winsession.environment(active["uid"])
        env.update(ANX_ROOT=str(root), PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        winsession.spawn(active["uid"], [str(windowless if windowless.exists() else python), "-m",
                                         "agent_nexus.session_helper"], project, env,
                         flags=winsession.CREATE_NO_WINDOW)
    except OSError as exc:
        log.warning("could not start the session helper in session %s: %s", active["uid"], exc)
        return False
    log.info("started a session helper for %s (session %s)", active["user"], active["id"])
    return True


def launch_helper(root: Path, active: dict) -> bool:
    """Start the helper in the active session (when its autostart entry did not run)."""
    if WINDOWS:
        return _launch_windows(root, active)
    import pwd
    env = session_environment(active)
    python = Path(root) / ".venv/bin/python"
    if env is None or not python.exists():
        return False
    try:
        entry = pwd.getpwuid(active["uid"])
    except KeyError:
        return False
    import grp
    groups = sorted({g.gr_gid for g in grp.getgrall() if entry.pw_name in g.gr_mem} | {entry.pw_gid})
    env.update(HOME=entry.pw_dir, USER=entry.pw_name, LOGNAME=entry.pw_name, SHELL=entry.pw_shell or "/bin/sh",
               PATH="/usr/local/bin:/usr/bin:/bin", ANX_ROOT=str(root), PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    try:
        subprocess.Popen([str(python), "-m", "agent_nexus.session_helper"], cwd=str(root), env=env,
                         user=entry.pw_uid, group=entry.pw_gid, extra_groups=groups,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except OSError as exc:
        log.warning("could not start the session helper for %s: %s", entry.pw_name, exc)
        return False
    log.info("started a session helper for %s (session %s)", entry.pw_name, active["id"])
    return True


def _peer_pid(writer: asyncio.StreamWriter) -> int | None:
    """The helper's process, as the system reports it (never the pid a hello claims)."""
    if WINDOWS:
        from . import winsession
        return winsession.peer_pid(writer.get_extra_info("peername"), writer.get_extra_info("sockname"))
    sock = writer.get_extra_info("socket")
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", raw)[0] or None
    except (OSError, AttributeError):
        return None


def _end_process(pid: int) -> None:
    import signal
    try:
        os.kill(pid, signal.SIGTERM)  # Windows: TerminateProcess
    except (OSError, ProcessLookupError):
        pass


def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
    if WINDOWS:  # the session of the connecting process stands in for the uid
        from . import winsession
        return winsession.peer_session(writer.get_extra_info("peername"), writer.get_extra_info("sockname"))
    sock = writer.get_extra_info("socket")
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid
    except (OSError, AttributeError):
        return None


async def _read_hello(reader: asyncio.StreamReader) -> dict:
    line = await asyncio.wait_for(reader.readline(), timeout=10)
    if not line.startswith(MAGIC):
        raise ValueError("not a session helper")
    return json.loads(line[len(MAGIC):])


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- core side

@dataclass
class _Helper:
    uid: int
    hello: dict
    writer: asyncio.StreamWriter
    pending: dict = field(default_factory=dict)  # id -> Future[(reader, writer)]
    #: from another release than the core: replaced by a helper of this one (see SessionRegistry._replace)
    stale: bool = False


class SessionRegistry:
    """The core's end of the socket, and the remote hub's ``desktop`` service when it runs as root."""

    def __init__(self, path: Path | None = None, *, root: Path | None = None, active=active_session,
                 launch=launch_helper, peer_pid=_peer_pid, end_process=_end_process):
        self.path = path or socket_path()
        self.root = root
        self._launch = launch
        self.helpers: list[_Helper] = []
        self._server: asyncio.base_events.Server | None = None
        self._active = active
        self._peer_pid = peer_pid
        self._end_process = end_process
        self._stopping = False
        #: uid -> when its helper was last replaced (a launch that still starts another release must not loop)
        self._replaced: dict[int, float] = {}

    async def start(self) -> None:
        if WINDOWS:
            self._server = await asyncio.start_server(self._accept, host="127.0.0.1", port=0)
            port = self._server.sockets[0].getsockname()[1]
            self._port_file = Path(self.root or os.environ.get("ANX_ROOT") or ".") / PORT_FILE
            self._port_file.write_text(str(port), encoding="ascii")
            log.info("session helpers: listening on 127.0.0.1:%s", port)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o755)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(self._accept, path=str(self.path))
        os.chmod(self.path, 0o666)  # any login may connect; what it may do is decided by its uid
        log.info("session helpers: listening on %s", self.path)

    async def stop(self) -> None:
        self._stopping = True
        if self._server is not None:
            self._server.close()
            self._server = None
        for helper in list(self.helpers):
            try:
                helper.writer.close()
            except Exception:
                pass

    def status(self) -> dict:
        active = self._active()
        helper = self._helper_for(active) if active else None
        return {"active": active, "helper": helper is not None,
                "helpers": [{"uid": h.uid, "session": h.hello.get("session")} for h in self.helpers]}

    def _helper_for(self, active: dict) -> _Helper | None:
        own = [helper for helper in self.helpers if helper.uid == active["uid"] and not helper.stale]
        for helper in own:
            if str(helper.hello.get("session") or "") == active["id"]:
                return helper
        return own[-1] if own else None

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        uid = _peer_uid(writer)
        try:
            hello = await _read_hello(reader)
        except (ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, json.JSONDecodeError):
            writer.close()
            return
        if uid is None:
            writer.close()
            return
        if "attach" in hello:
            for helper in self.helpers:
                future = helper.pending.get(str(hello["attach"]))
                if future is not None and helper.uid == uid and not future.done():
                    future.set_result((reader, writer))
                    return
            writer.close()
            return
        helper = _Helper(uid=uid, hello=dict(hello.get("hello") or {}), writer=writer)
        self.helpers.append(helper)
        log.info("session helper connected (uid %s, session %s, version %s)", uid, helper.hello.get("session"),
                 helper.hello.get("version") or "older than 0.8.27")
        if str(helper.hello.get("version") or "") != __version__:
            # A helper outlives the core's updates (it runs in the login, started once): without this
            # the remote desktop kept the release of the day of the login -- weeks old.
            import time
            if time.monotonic() - self._replaced.get(uid, -1e9) > REPLACE_INTERVAL_S:
                self._replaced[uid] = time.monotonic()
                helper.stale = True
                self._replace(helper, self._peer_pid(writer))
            else:
                log.warning("the new session helper of %s still runs %s; keeping it", uid,
                            helper.hello.get("version") or "an older release")
        try:
            while await reader.readline():  # the helper says nothing more; EOF = gone
                pass
        except (ConnectionError, OSError):
            pass
        finally:
            self.helpers.remove(helper)
            if helper.stale and not self._stopping:
                task = asyncio.ensure_future(self._relaunch(helper.uid))
                task.add_done_callback(lambda done: done.cancelled() or done.exception())
            for future in helper.pending.values():
                if not future.done():
                    future.cancel()
            log.info("session helper gone (uid %s)", uid)

    def _replace(self, helper: _Helper, pid: int | None) -> None:
        """Ask a helper of another release to go (one that does not know how is ended); its connection
        closing starts one of this release (_relaunch)."""
        log.info("session helper of %s runs %s; replacing it with %s", helper.uid,
                 helper.hello.get("version") or "an older release", __version__)
        if helper.hello.get("version"):
            try:
                helper.writer.write(json.dumps({"exit": True}).encode() + b"\n")
            except (ConnectionError, OSError, RuntimeError):
                pass
        elif pid:
            self._end_process(pid)

    async def _relaunch(self, uid: int) -> None:
        await asyncio.sleep(1.0)  # the old process lets go of its single-instance lock
        if self._stopping or self.root is None or any(h.uid == uid and not h.stale for h in self.helpers):
            return
        active = await asyncio.to_thread(self._active)
        if active and active["uid"] == uid:
            await asyncio.to_thread(self._launch, self.root, active)

    async def serve(self, request: dict, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """The remote hub's desktop service: hand the stream to the active session's helper."""
        from . import remote_hub as rh
        active = await asyncio.to_thread(self._active)
        helper = self._helper_for(active) if active else None
        if helper is None and active and self.root is not None \
                and await asyncio.to_thread(self._launch, self.root, active):
            for _ in range(40):  # it connects within a second or two
                await asyncio.sleep(0.25)
                helper = self._helper_for(active)
                if helper is not None:
                    break
        if helper is None:
            why = "没有人登录桌面（锁屏前的登录界面不能共享）" if not active else \
                f"{active['user']} 的桌面会话里没有运行 agent-nexus 会话助手（重新登录一次桌面即可）"
            writer.write(rh.frame(rh.ERROR, {"error": f"这台设备不能共享桌面：{why}"}))
            await writer.drain()
            return
        token = secrets.token_urlsafe(18)
        future = asyncio.get_running_loop().create_future()
        helper.pending[token] = future
        try:
            helper.writer.write(json.dumps({"open": token, "request": request}).encode() + b"\n")
            await helper.writer.drain()
            their_reader, their_writer = await asyncio.wait_for(future, timeout=ATTACH_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError, ConnectionError):
            writer.write(rh.frame(rh.ERROR, {"error": "这台设备不能共享桌面：会话助手没有响应"}))
            await writer.drain()
            return
        finally:
            helper.pending.pop(token, None)
        await asyncio.gather(_pipe(reader, their_writer), _pipe(their_reader, writer))


# --------------------------------------------------------------------------- helper side

async def _connect():
    """The core's socket (Linux) or loopback port (Windows: ``<ANX_ROOT>/session.port``)."""
    if WINDOWS:
        port = int((Path(os.environ.get("ANX_ROOT") or ".") / PORT_FILE).read_text(encoding="ascii").strip())
        return await asyncio.open_connection("127.0.0.1", port)
    return await asyncio.open_unix_connection(str(socket_path()))


async def _serve_one(token: str, request: dict, name: str) -> None:
    from . import vnc
    reader, writer = await _connect()
    writer.write(MAGIC + json.dumps({"attach": token}).encode() + b"\n")
    await writer.drain()
    try:
        await vnc.DesktopService(name).serve(request, reader, writer)
    except (ConnectionError, OSError, asyncio.IncompleteReadError):
        pass
    except Exception:
        log.exception("desktop session failed")
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def run_helper() -> None:
    from . import join as joining
    name = joining.local_hostname()
    session = os.environ.get("XDG_SESSION_ID", "")
    if WINDOWS:
        from . import winsession
        session = str(winsession.process_session(os.getpid()))
    hello = {"session": session, "display": os.environ.get("DISPLAY", ""),
             "wayland": os.environ.get("WAYLAND_DISPLAY", ""), "pid": os.getpid(), "version": __version__}
    tasks: set[asyncio.Task] = set()
    while True:
        try:
            reader, writer = await _connect()
        except (FileNotFoundError, ConnectionError, OSError, ValueError):
            await asyncio.sleep(5)  # the core is not running (yet), or restarting after an update
            continue
        log.info("connected to the core")
        writer.write(MAGIC + json.dumps({"hello": hello}).encode() + b"\n")
        await writer.drain()
        try:
            while line := await reader.readline():
                message = json.loads(line)
                if message.get("exit"):  # the core runs another release: it starts a helper of its own
                    log.info("the core asked this helper to exit (it runs another release)")
                    return
                if "open" in message:
                    task = asyncio.create_task(_serve_one(str(message["open"]), dict(message.get("request") or {}), name))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            writer.close()
        await asyncio.sleep(2)


def _single_instance():
    """One helper per login: autostart, the window and the core may all start one."""
    if WINDOWS:
        import msvcrt
        import tempfile
        from . import winsession
        session = winsession.process_session(os.getpid())
        handle = open(os.path.join(tempfile.gettempdir(), f"agent-nexus-session-{session}.lock"), "a+")
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return None
        return handle
    import fcntl
    directory = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    session_id = os.environ.get("XDG_SESSION_ID") or "x"
    handle = open(os.path.join(directory, f"agent-nexus-session-{os.getuid()}-{session_id}.lock"), "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if (hasattr(os, "geteuid") and os.geteuid() == 0) or supported():
        print("agent-nexus session: run this in the desktop session (as the logged-in user), not as root/SYSTEM",
              file=sys.stderr)
        return 2
    lock = _single_instance()
    if lock is None:
        return 0  # this session already has its helper
    try:
        asyncio.run(run_helper())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
