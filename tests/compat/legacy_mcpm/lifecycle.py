"""Manager lifecycle record: why a manager ended, and where it was stuck.

A manager that disappears leaves nothing behind: its stdout goes to a log the
desktop app truncates on the next launch, and a hard kill or a native crash
prints nothing at all. This keeps a persistent ``mcpm.log`` and a small state
file so the next start can say how the previous one ended, and so the desktop
app can tell a requested exit from a death.
"""
from __future__ import annotations

import asyncio
import atexit
import faulthandler
import json
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import psutil

log = logging.getLogger(__name__)

STATE_NAME = "mcpm-manager.state"
LOG_NAME = "mcpm.log"
HEARTBEAT_S = 10.0
STALL_S = 30.0
LOOP_BEAT_S = 2.0


def state_path(root: Path) -> Path:
    return root / ".mcp-manager" / "run" / STATE_NAME


def log_path(root: Path) -> Path:
    return root / ".mcp-manager" / "logs" / LOG_NAME


def _stamp(at: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)) if at else "-"


class Lifecycle:
    def __init__(self, root: Path):
        self.root = root
        self.path = state_path(root)
        self.started = time.time()
        self.exit_reason: str | None = None
        self.requested = False
        self.loop_beat: float | None = None
        self._stalled = False
        self._stop = threading.Event()
        self._log_file = None

    # -- setup ---------------------------------------------------------------

    def install(self) -> None:
        target = log_path(self.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(target, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s]: %(message)s",
                                               "%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(handler)
        logging.getLogger("mcpm").setLevel(logging.INFO)
        # Native crashes (access violations in GUI/COM libraries) print a
        # Python traceback here instead of vanishing.
        self._log_file = open(target.with_name("mcpm-faults.log"), "a", encoding="utf-8")
        faulthandler.enable(self._log_file, all_threads=True)
        previous_hook = threading.excepthook

        def thread_hook(args):
            log.error("uncaught exception in thread %s", args.thread.name if args.thread else "?",
                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
            previous_hook(args)

        threading.excepthook = thread_hook
        self._report_previous()
        self._write("running")
        parent = None
        try:
            parent = psutil.Process(os.getppid())
            parent = f"{parent.name()} ({parent.pid})"
        except psutil.Error:
            parent = str(os.getppid())
        log.info("manager started: pid %s, parent %s, python %s, argv %s",
                 os.getpid(), parent, sys.version.split()[0], " ".join(sys.argv))
        atexit.register(self._at_exit)
        threading.Thread(target=self._watch, name="mcpm-lifecycle", daemon=True).start()

    def _report_previous(self) -> None:
        try:
            previous = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if previous.get("state") == "running" and not _alive(previous):
            log.warning("previous manager (pid %s, started %s) ended without a clean exit; last heartbeat %s. "
                        "It was killed or crashed natively; see mcpm-faults.log for a native traceback.",
                        previous.get("pid"), _stamp(previous.get("started")), _stamp(previous.get("heartbeat")))
        elif previous.get("state") == "exited":
            log.info("previous manager (pid %s) exited at %s: %s", previous.get("pid"),
                     _stamp(previous.get("exited")), previous.get("reason"))

    # -- events --------------------------------------------------------------

    def request_exit(self, reason: str) -> None:
        """An exit someone asked for. The desktop app must not resurrect it."""
        if self.exit_reason is None:
            self.exit_reason, self.requested = reason, True
            log.info("exit requested: %s", reason)

    def note_exit(self, reason: str) -> None:
        """An exit nobody asked for, e.g. the tray loop or console server ending."""
        if self.exit_reason is None:
            self.exit_reason = reason
            log.warning("manager stopping on its own: %s", reason)

    async def beat_loop(self) -> None:
        """Runs on the event loop shared by the console and the cloud agent."""
        while True:
            self.loop_beat = time.monotonic()
            await asyncio.sleep(LOOP_BEAT_S)

    def finish(self) -> None:
        self._stop.set()
        reason = self.exit_reason or "manager returned without a recorded reason"
        self._write("exited", reason=reason, requested=self.requested, exited=time.time())
        log.info("manager exited cleanly: %s", reason)

    # -- internals -----------------------------------------------------------

    def _at_exit(self) -> None:
        # finish() normally ran already; this catches sys.exit from elsewhere.
        try:
            current = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
        if current.get("pid") == os.getpid() and current.get("state") == "running":
            self.note_exit("interpreter exiting")
            self.finish()

    def _watch(self) -> None:
        while not self._stop.wait(HEARTBEAT_S):
            self._write("running")
            self.check_loop()

    def check_loop(self) -> None:
        beat = self.loop_beat
        if beat is not None:
            stale = time.monotonic() - beat
            if stale > STALL_S and not self._stalled:
                self._stalled = True
                log.error("event loop (console + cloud agent) has not run for %.0fs; thread stacks follow in %s",
                          stale, self._log_file.name if self._log_file else "mcpm-faults.log")
                if self._log_file:
                    self._log_file.write(f"\n===== event loop stalled {stale:.0f}s at {_stamp(time.time())}\n")
                    self._log_file.flush()
                    faulthandler.dump_traceback(self._log_file, all_threads=True)
                    self._log_file.flush()
            elif stale <= STALL_S and self._stalled:
                self._stalled = False
                log.warning("event loop running again")

    def _write(self, state: str, **extra) -> None:
        data = {"pid": os.getpid(), "create_time": _create_time(), "started": self.started,
                "heartbeat": time.time(), "state": state, **extra}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            pass


def _create_time() -> float | None:
    try:
        return psutil.Process(os.getpid()).create_time()
    except psutil.Error:
        return None


def _alive(record: dict) -> bool:
    try:
        proc = psutil.Process(int(record["pid"]))
        created = record.get("create_time")
        return created is None or abs(proc.create_time() - float(created)) < 1.0
    except (psutil.Error, KeyError, TypeError, ValueError):
        return False
