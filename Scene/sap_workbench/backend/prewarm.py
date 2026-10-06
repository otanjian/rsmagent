"""Warm the scene's OpenCode engine before the user asks for a session.

A workbench session pays the whole engine start inside its create request, and
that request is the wait the user sees. Measured on the deployment host, that
start took about 19s with a cold file cache and about 3.4s warm: the cost is Bun
parsing and transpiling the engine source tree and building its effect graph,
not anything SAP-specific.

Opening the workbench is the last cheap moment before a create request, so the
start is paid there in the background instead. This module owns exactly one
throwaway child process and one temporary directory. It never touches a session
lease, the browser gateway, the scene store or any SAP, platform or model
credential, and every failure is silent: a missed warm-up only means the real
start pays its own cost.
"""
from __future__ import annotations

from contextlib import suppress
import atexit
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import threading
import time

from common.log import logger

from .legacy import engine_enabled

#: Set to 0/false/no/off to disable. A deployment that would rather keep a
#: predictable process list can turn the warm-up off without losing correctness.
PREWARM_ENV = 'SAP_WORKBENCH_PREWARM'
#: Seconds before one warm-up may follow another for this process.
COOLDOWN = float(os.environ.get('SAP_WORKBENCH_PREWARM_COOLDOWN', '300'))
#: Ceiling on one warm-up, including the read of its port line.
WAIT_SECONDS = float(os.environ.get('SAP_WORKBENCH_PREWARM_TIMEOUT', '90'))

#: Guards ``_running`` and ``_last_started``.
_lock = threading.Lock()
#: Guards ``_live``, ``_reap_registered`` and ``_generation``. Take it around
#: creating the process, so an exit between the spawn and its registration
#: cannot abandon it.
_registry_lock = threading.Lock()
_live = []
_reap_registered = False
#: Bumped by :func:`cancel`. A warm-up whose generation moved on was aborted and
#: must not report itself as ready.
_generation = 0
_last_started = 0.0
_running = False


def enabled():
    raw = os.environ.get(PREWARM_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {'0', 'false', 'no', 'off'}


def running():
    with _lock:
        return _running


def _reap():
    """Stop a warm-up still in flight and drop its temporary directory.

    A warm-up runs in a daemon thread, so an exiting process can abandon it
    mid-start. Nothing else would stop that host: no session and no browser node
    owns it, and Windows never reparents a child to pid 1.
    """
    global _reap_registered
    with _registry_lock:
        live, _live[:] = list(_live), []
        _reap_registered = False
    for process, base in live:
        with suppress(Exception):
            if process.poll() is None:
                process.kill()
        shutil.rmtree(base, ignore_errors=True)


def host_script():
    return Path(__file__).resolve().parents[3] / 'Scene/sap_workbench/opencode_adapter/server.ts'


def cancel():
    """Stop an in-flight warm-up before a real session starts.

    The card entry reads the configuration and creates a session in the same
    breath, so without this the warm-up would run alongside the very start the
    user is waiting for. That measured ~2s slower than a create with no warm-up
    at all, on the primary entry path. Killing it here hands the CPU and disk
    back to the real start; the pages it already touched stay cached, and its own
    thread still reaps the process and temporary directory.
    """
    global _generation
    with _registry_lock:
        _generation += 1
        live = list(_live)
    for process, _ in live:
        with suppress(Exception):
            if process.poll() is None:
                process.kill()


def _warm():
    """Start and discard one engine, returning its start time in seconds.

    The warm-up gets the same source tree and environment a real host loads and
    nothing else. In particular it has no session location: it runs in its own
    temporary directory, so no project configuration is discovered and no
    project file (such as the navigation plugin) is written anywhere real.
    """
    global _reap_registered
    from .environment import opencode_host_env, runtime_paths, transpiler_cache_dir

    with _registry_lock:
        generation = _generation
    paths = runtime_paths('')
    host = host_script()
    if not paths.bun or not paths.source.is_absolute() or not paths.source.is_dir():
        return None
    # The engine sources a real session requires, plus this scene's own host.
    # Warming a deployment that cannot start would only spend the cost twice.
    if not (paths.source / 'node_modules').is_dir() or not host.is_file():
        return None
    if not (paths.source / 'packages/opencode/src/server/routes/instance/httpapi/server.ts').is_file():
        return None
    started = time.monotonic()
    base = None
    process = None
    try:
        # The spawn and its registration are one critical section: the exit
        # reap must never miss a process that already exists.
        with _registry_lock:
            base = tempfile.mkdtemp(prefix='sap-workbench-prewarm-')
            runtime_root = Path(base) / 'runtime'
            (runtime_root / 'config').mkdir(parents=True, exist_ok=True)
            setup = {'directory': str(runtime_root), 'project': '', 'root': str(paths.source),
                     'token': secrets.token_hex(16), 'modelURL': 'http://127.0.0.1:1/model',
                     'bridgeURL': 'http://127.0.0.1:1/bridge/', 'service': 'sap-workbench-prewarm',
                     'displayMode': 'iframe'}
            try:
                # The shared transpiler cache is the point: a warm-up cancelled
                # by the session that follows still leaves the modules it
                # transpiled behind for that session to reuse.
                process = subprocess.Popen([paths.bun, str(host)], stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           env=opencode_host_env(transpiler_cache_dir()), cwd=str(runtime_root))
            except BaseException:
                shutil.rmtree(base, ignore_errors=True)
                base = None
                raise
            _live.append((process, base))
            if not _reap_registered:
                atexit.register(_reap)
                _reap_registered = True
        # The read below blocks until the host prints its port or exits. A
        # watchdog bounds a host that does neither, so a stuck child cannot
        # hold this thread (or its temporary directory) open.
        watchdog = threading.Timer(WAIT_SECONDS, process.kill)
        watchdog.daemon = True
        watchdog.start()
        try:
            # One line, then the pipe stays open: its end-of-file is how the host
            # notices that this interpreter is gone (see server.ts). The explicit
            # kill below normally wins the race.
            process.stdin.write(json.dumps(setup).encode() + b'\n')
            process.stdin.flush()
            while True:
                line = process.stdout.readline()
                if not line:
                    break
                try:
                    if isinstance(json.loads(line).get('port'), int):
                        break
                except (ValueError, AttributeError):
                    pass
        finally:
            watchdog.cancel()
        if generation != _generation:
            return None  # aborted for a real session start; not a completed warm-up
        return time.monotonic() - started
    finally:
        if process is not None:
            with suppress(Exception):
                process.kill()
            with suppress(Exception):
                process.wait(timeout=5)
        if base is not None:
            with _registry_lock:
                _live[:] = [item for item in _live if item[0] is not process]
            shutil.rmtree(base, ignore_errors=True)


def kick():
    """Start one background warm-up unless one is running or the cooldown holds."""
    global _last_started, _running
    if not engine_enabled() or not enabled():
        # The retired engine path's warm-up: with the rollback switch off there
        # is no engine to warm, so this stays a no-op rather than spending a
        # process on a session that will never be started.
        return False
    now = time.monotonic()
    with _lock:
        if _running or now - _last_started < COOLDOWN:
            return False
        _running = True
        _last_started = now

    def work():
        global _running
        elapsed = None
        try:
            elapsed = _warm()
        except BaseException:
            logger.debug('[SapWorkbench] prewarm failed', exc_info=True)
        finally:
            with _lock:
                _running = False
            if elapsed is not None:
                logger.info('[SapWorkbench] prewarm ready elapsed=%.2fs', elapsed)

    threading.Thread(target=work, name='sap-workbench-prewarm', daemon=True).start()
    return True
