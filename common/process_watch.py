# encoding:utf-8
"""Process lifecycle recording, so a death can be explained after the fact.

``run.log`` says what a process *did*; it says nothing about how it ended. When
the backend disappears without a traceback the log simply stops, the watchdog
restarts it a minute later, and the only visible symptom is a user watching a
failed send. That is the state this module exists to end.

It appends four kinds of line to one file beside ``run.log``
(``app-lifecycle.log``, see :func:`common.log.log_path`):

* a start line with pid, argv, interpreter and working directory;
* a fatal-stack dump for a crash inside native code, which no Python-level
  handler can see (``faulthandler``);
* the traceback of an unhandled exception, in the main thread or in any worker
  thread;
* an explicit "clean interpreter exit" line from :mod:`atexit`.

The set is closed on purpose: because an intentional ``os._exit`` path calls
:func:`record_exit`, and a clean shutdown always runs :mod:`atexit`, the
*absence* of an exit line is itself the evidence — the process was terminated
from outside (``TerminateProcess``, a task kill, the OOM path) rather than
having chosen to exit. That is exactly the question a silent death leaves open.

Nothing in here may raise, block or allocate: observability that can break
startup is worse than no observability, so every step degrades to stderr (which
the launcher redirects into ``run-console.err.log``) and gives up quietly.
"""

from __future__ import annotations

import atexit
import faulthandler
import os
import sys
import threading
import time
import traceback
from typing import IO, Optional

from common.log import log_path

#: Lifecycle record beside ``run.log``. Read by ``scripts/watchdog-app.ps1``
#: when it finds the app gone, so the restart is logged with the death reason.
DEFAULT_FILENAME = "app-lifecycle.log"

#: Escape hatch for a launcher that cannot compute the data root, and for the
#: tests, which must not append to the real file.
ENV_OVERRIDE = "COW_PROCESS_WATCH_LOG"

#: Keep one generation when a crash loop would otherwise grow the file without
#: bound. Sized like the launcher's own rotation (see watchdog-app.ps1).
MAX_BYTES = 1024 * 1024

_handle: Optional[IO[str]] = None
_installed = False
_started_at = time.time()


def lifecycle_path() -> str:
    """Path of this process's lifecycle record."""
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        return os.path.expanduser(override)
    return log_path(DEFAULT_FILENAME)


def _emit(text: str) -> None:
    """Append one block, falling back to stderr when the file is unavailable."""
    if not text.endswith("\n"):
        text += "\n"
    if _handle is not None:
        try:
            _handle.write(text)
            _handle.flush()
            return
        except Exception:
            pass
    try:
        sys.stderr.write(text)
        sys.stderr.flush()
    except Exception:
        pass


def _stamp(event: str, detail: str = "") -> None:
    suffix = f" {detail}" if detail else ""
    _emit("[%s] pid=%s %s%s" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), event, suffix,
    ))


def record_exit(reason: str) -> None:
    """Record an exit that bypasses :mod:`atexit` (an ``os._exit`` path).

    Without this call those paths would be indistinguishable in the file from an
    external kill — the ambiguity this module exists to remove — so every
    ``os._exit`` in the runtime must announce itself here first.
    """
    _stamp("exiting", "reason=%s uptime=%ds" % (reason, int(time.time() - _started_at)))


def _rotate(path: str) -> None:
    try:
        if os.path.getsize(path) > MAX_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass


def _install_faulthandler() -> None:
    try:
        if _handle is not None:
            faulthandler.enable(file=_handle, all_threads=True)
        else:
            faulthandler.enable(all_threads=True)
    except Exception:
        pass


def _install_excepthooks() -> None:
    """Record unhandled exceptions without taking over the default reporting."""
    previous_main = sys.excepthook

    def main_hook(exc_type, exc, tb):
        # Ctrl+C is a user request, not a defect to record as one.
        if not issubclass(exc_type, KeyboardInterrupt):
            _emit("unhandled exception in main thread\n"
                  + "".join(traceback.format_exception(exc_type, exc, tb)))
        previous_main(exc_type, exc, tb)

    sys.excepthook = main_hook

    if not hasattr(threading, "excepthook"):
        return
    previous_thread = threading.excepthook

    def thread_hook(args):
        _emit("unhandled exception in thread %s\n" % args.thread.name
              + "".join(traceback.format_exception(
                  args.exc_type, args.exc_value, args.exc_traceback)))
        previous_thread(args)

    threading.excepthook = thread_hook


def install() -> None:
    """Start recording this process's lifecycle. Idempotent, never raises.

    Called before config load, so a death during startup is as diagnosable as
    one during traffic.
    """
    global _handle, _installed, _started_at

    if _installed:
        return

    path = lifecycle_path()
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        _rotate(path)
        _handle = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        _handle = None

    _installed = True
    _started_at = time.time()
    where = path if _handle is not None else "stderr (lifecycle file not writable)"
    _stamp("process started", "python=%s argv=%s cwd=%s record=%s" % (
        sys.version.split()[0], " ".join(sys.argv), os.getcwd(), where,
    ))

    _install_faulthandler()
    _install_excepthooks()
    atexit.register(_on_exit)


def _on_exit() -> None:
    _stamp("clean interpreter exit",
           "uptime=%ds" % int(time.time() - _started_at))
