"""The headless Python worker the desktop runs inside a local project.

Change ``align-desktop-project-execution-with-master`` (task 4.1 / 4.2 / 4.3).

Why a separate process rather than a function call: the desktop's *sandbox* is
the boundary (task 4.4/4.5), and a boundary only exists for a process the OS can
confine. So this module is a thin, dependency-injected adapter over the **existing**
tool classes -- ``Read``, ``Write``, ``Edit``, ``Bash``, ``Ls``, ``SearchFiles``,
the same objects the server's Agent uses, with the same schemas -- so that
"works on the server" and "works locally" cannot drift into two behaviours.

The three seams the design calls out are respected here:

* the working directory is set the way ``Agent.apply_project_dir()`` sets it (the
  tools' ``cwd``), never with a global ``os.chdir()`` -- one process may hold
  several tool instances and a process-wide chdir would be a cross-call race;
* the environment is not inherited wholesale: :func:`build_worker_env` strips
  anything that is not needed to run a tool, so no bearer, API key or identity DB
  path reaches this process;
* nothing here starts a model loop, a Web backend or a scheduler. A frame names
  one tool; the answer is that tool's own ``ToolResult``.

Run it as ``python -m agent.desktop_local.worker`` with the project root, the
run's temp directory and the environment already prepared by the launcher.
"""

from __future__ import annotations

import io
import json
import os
import signal
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from agent.desktop_local import protocol as proto
from common.log import logger

#: Environment variables a tool may legitimately need. Everything else is
#: dropped, so a token or an internal path cannot leak in by inheritance.
#:
#: The temp variables are deliberately **not** here: they are set from the run's
#: granted temp directory instead (see :func:`build_worker_env`), because
#: inheriting the parent's value points them at a directory outside the sandbox's
#: writable grant -- and then ``tempfile.gettempdir()`` finds nothing usable.
_ENV_KEEP = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL",
    "LANG", "LC_ALL", "LC_CTYPE", "TERM", "PWD",
    # Windows needs these to start processes and resolve DLLs at all.
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATHEXT", "COMSPEC",
    "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "OS",
    # The master build already keys desktop-specific wording off this.
    "COW_DESKTOP",
)

#: Set rather than kept: the run's temp directory, under every name a tool may
#: consult.
_ENV_TEMP_VARS = ("TMPDIR", "TEMP", "TMP")

#: Variable *name* fragments that must never survive into the worker, whatever
#: else the keep list says. Belt and braces: a deployment that adds a new secret
#: to the parent environment does not have to remember this file.
_ENV_DROP_MARKERS = (
    "TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "APIKEY",
    "CREDENTIAL", "PRIVATE_KEY", "SESSION", "COOKIE", "AUTH",
)


def build_worker_env(
    base: Optional[Dict[str, str]] = None,
    temp: Optional[str] = None,
) -> Dict[str, str]:
    """The environment for a worker process: only what a tool needs to run.

    Deliberately an allowlist with a second, name-based deny pass. The deny pass
    runs last so it wins: a variable named ``COW_IDENTITY_DB_TOKEN`` is dropped
    even though its prefix is otherwise kept.

    ``temp`` becomes ``TMPDIR``/``TEMP``/``TMP``. It must be the run's *granted*
    temp directory, not the parent's: that one is outside the sandbox's writable
    grant, so Python's ``tempfile`` finds no usable directory and every tool that
    spills to a temp file fails.
    """
    source = os.environ if base is None else base
    clean: Dict[str, str] = {}
    for key in _ENV_KEEP:
        value = source.get(key)
        if value is not None:
            clean[key] = value
    for key, value in source.items():
        upper = key.upper()
        if any(marker in upper for marker in _ENV_DROP_MARKERS):
            clean.pop(key, None)
    if temp:
        for key in _ENV_TEMP_VARS:
            clean[key] = temp
    return clean


def apply_temp_environment(temp: str) -> str:
    """Point this process (and so every tool and script it starts) at ``temp``.

    Called once the handshake names the root and its temp directory. The launcher
    already sets these, so this is belt and braces -- but it is the half that
    cannot be forgotten by a caller, because the worker derives it from the frame
    it just validated rather than from its own inherited environment.
    """
    resolved = os.path.realpath(temp)
    for key in _ENV_TEMP_VARS:
        os.environ[key] = resolved
    # `tempfile` caches its answer on first use; without this a value read before
    # the handshake would keep pointing at the unwritable parent directory.
    import tempfile

    tempfile.tempdir = None
    return resolved


class _ProcessGroups:
    """Every process group this worker's commands have created.

    Needed because a command is started in its own session: the ids are the only
    handle to the trees it spawns, and a tree whose shell has already exited --
    ``sleep 100 &`` returns immediately -- is invisible to every other mechanism.
    Killing the worker's own process group therefore does nothing for them, which
    is how a "cancelled" run leaves processes running on the user's machine.

    The registry is pruned as it goes: a group id whose group no longer exists is
    dropped, so a long session does not accumulate them.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._groups: List[int] = []

    def record(self, pgid: int) -> None:
        """Remember one command's process group."""
        if not pgid or pgid == os.getpid():
            # Never track our own group: killing it would kill the worker from
            # inside its own handler, before it can answer the caller.
            return
        with self._lock:
            self._prune_locked()
            if pgid not in self._groups:
                self._groups.append(pgid)

    def kill_all(self, settle: float = 0.25) -> "Tuple[int, List[int]]":
        """SIGKILL every remembered group, then check that the tree is really gone.

        Returns ``(killed, unreaped)``. ``unreaped`` are the groups still standing
        afterwards, which needs explaining because it is not an ordinary failure:

        The sandbox permits signalling only this process's live children, and a
        command that backgrounds something and exits -- ``sleep 300 &`` -- leaves
        that process reparented to init. It is then in a group we created but no
        longer our child, so the kernel refuses the signal *for that member only*
        and ``killpg`` still returns success. Assuming the kill worked is exactly
        how a cancelled command keeps running, so the group is checked and any
        survivor is named for the desktop -- which is not sandboxed -- to finish.
        """
        with self._lock:
            groups = list(self._groups)
            self._groups = []
        killed = 0
        unreaped: List[int] = []
        for pgid in groups:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except OSError:
                pass
            deadline = time.monotonic() + settle
            while self._group_alive(pgid) and time.monotonic() < deadline:
                time.sleep(0.02)
            if self._group_alive(pgid):
                unreaped.append(pgid)
            else:
                killed += 1
        return killed, unreaped

    @staticmethod
    def _group_alive(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            # Present but not signalable: still there.
            return True

    def _prune_locked(self) -> None:
        alive: List[int] = []
        for pgid in self._groups:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                continue
            except OSError:
                # Present but not signalable: still a live group, keep tracking.
                alive.append(pgid)
                continue
            alive.append(pgid)
        self._groups = alive


class _Home:
    """The scoped directories a tool may touch, and the path guard over them."""

    def __init__(self, root: str, temp: str) -> None:
        self.root = os.path.realpath(root)
        self.temp = os.path.realpath(temp)

    def allows(self, candidate: str) -> bool:
        """Whether ``candidate`` names a location inside the project or its temp.

        A relative path is resolved against the project root, which is what the
        tools themselves do with their ``cwd`` -- so the guard and the tool agree
        about where ``notes.txt`` is, and a relative path is never judged against
        whatever directory this process happened to start in.
        """
        base = candidate if os.path.isabs(candidate) else os.path.join(
            self.root, candidate)
        real = os.path.realpath(base)
        for allowed in (self.root, self.temp):
            if real == allowed or real.startswith(allowed + os.sep):
                return True
        return False


class LocalToolWorker:
    """One project, one tool set, one call at a time.

    ``hello`` establishes the project; every later frame is interpreted against
    it. A ``call`` while another call is running is refused rather than queued:
    the desktop already serializes side-effecting calls per project (task 7.4),
    and a queue here would only hide a second writer.
    """

    def __init__(self, *, timeout: Optional[float] = None) -> None:
        self.home: Optional[_Home] = None
        self.tools: Dict[str, Any] = {}
        self.protocol_version = proto.PROTOCOL_VERSION
        self._cancel = threading.Event()
        self._busy = threading.Lock()
        #: Process groups created by commands this worker ran. See
        #: :class:`_ProcessGroups` for why the worker has to own this.
        self.process_groups = _ProcessGroups()
        #: Set by the launcher (via the environment) to the directory it confined
        #: this process to; a hello for any other directory is refused.
        self.expected_root: Optional[str] = None
        self._timeout = float(
            timeout if timeout is not None
            else os.environ.get("COW_DESKTOP_WORKER_TIMEOUT")
            or proto.DEFAULT_CALL_TIMEOUT_SECONDS)
        self._stopping = False

    # -- handshake ----------------------------------------------------------

    def hello(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        root = proto.require_str(frame, "root")
        temp = proto.require_str(frame, "temp", required=False)
        if not temp:
            temp = os.path.join(root, ".cow-tmp")
        if not os.path.isdir(root):
            raise proto.WorkerProtocolError("invalid_request",
                                            "the project root is not a directory")
        real_root = os.path.realpath(root)
        if self.expected_root and real_root != self.expected_root:
            raise proto.WorkerProtocolError(
                "invalid_request",
                "the requested root is not the directory this worker was confined to")
        os.makedirs(temp, exist_ok=True)
        self.home = _Home(real_root, temp)
        # Before any tool is built: a tool that spills to a temp file (the bash
        # tool does, the moment output exceeds its inline limit) needs a temp
        # directory it can actually write, which the inherited one is not.
        apply_temp_environment(self.home.temp)
        self.tools = self._build_tools(self.home)
        return {
            "protocol_version": self.protocol_version,
            "root": self.home.root,
            "tools": list(self.tools.keys()),
            "pid": os.getpid(),
        }

    def _build_tools(self, home: _Home) -> Dict[str, Any]:
        """Instantiate the master build's tools, retargeted at the project.

        The classes are imported lazily and by name so this module stays
        importable (and testable) without the whole tool stack.
        """
        from agent.tools.bash import background as background_mod
        from agent.tools.bash.bash import Bash
        from agent.tools.edit.edit import Edit
        from agent.tools.ls.ls import Ls
        from agent.tools.read.read import Read
        from agent.tools.search_files.search_files import SearchFiles
        from agent.tools.write.write import Write

        classes = {
            "read": Read, "write": Write, "edit": Edit,
            "bash": Bash, "ls": Ls, "search_files": SearchFiles,
        }
        tools: Dict[str, Any] = {}
        for name in proto.ALLOWED_TOOLS:
            cls = classes[name]
            config: Dict[str, Any] = {"cwd": home.root}
            if name == "bash":
                config["timeout"] = self._timeout
            tool = cls(config)
            # The process-launch seam (task 4.2): the tool reports the group id of
            # every command it starts, and this worker owns the policy of when a
            # tree must die. Without it a cancelled command's tree is unreachable
            # from outside, because each command runs in its own session.
            if name == "bash":
                tool.process_group_hook = self.process_groups.record
                # Background jobs (`run_in_background`) spawn their own sessions
                # too, so they need the same reporting or a cancelled run leaves a
                # server running on the user's machine.
                background_mod.set_process_group_hook(self.process_groups.record)
            # Same retargeting `Agent.apply_project_dir` performs, so the two
            # entry points cannot disagree about which directory a relative path
            # resolves against.
            tool.cwd = home.root
            if isinstance(getattr(tool, "config", None), dict):
                tool.config["cwd"] = home.root
            reter = getattr(tool, "set_cwd", None)
            if callable(reter):
                reter(home.root)
            tool.cancel_event = self._cancel
            tools[name] = tool
        return tools

    # -- ops ----------------------------------------------------------------

    def describe(self) -> Dict[str, Any]:
        self._require_hello()
        return {"tools": [self.tools[name].get_json_schema()
                          for name in proto.ALLOWED_TOOLS]}

    def call(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        """Run one tool call, refusing if the project is already busy."""
        if not self._busy.acquire(blocking=False):
            raise proto.WorkerProtocolError(
                "invalid_request", "another call is already running")
        try:
            return self.run_call(frame)
        finally:
            self._busy.release()

    def run_call(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        """The call itself, with the project already claimed by the caller.

        Separate from :meth:`call` so the serving loop can claim the project on
        the reader thread (refusing a concurrent call *immediately*) while the
        work happens on another thread -- and still keep reading, so ``cancel``
        can reach a running tool.
        """
        self._require_hello()
        name = proto.require_str(frame, "tool")
        if name not in proto.ALLOWED_TOOLS:
            # Refuse by name, not by lookup: an unknown tool is never resolved.
            raise proto.WorkerProtocolError("unknown_tool", f"unknown tool: {name!r}")
        arguments = proto.require_arguments(frame)
        self._check_paths(name, arguments)
        started = time.monotonic()
        result = self.tools[name].execute(dict(arguments))
        elapsed = time.monotonic() - started
        if self._cancel.is_set() and getattr(result, "status", None) != "success":
            raise proto.WorkerProtocolError("cancelled", "the call was cancelled")
        return {
            "tool": name,
            "status": getattr(result, "status", None) or "success",
            "result": proto.bounded_result(getattr(result, "result", None)),
            "display": proto.bounded_result(getattr(result, "display", None)),
            "ext_data": proto.bounded_result(getattr(result, "ext_data", None)),
            "duration_ms": int(elapsed * 1000),
        }

    def cancel(self) -> Dict[str, Any]:
        self._cancel.set()
        # Setting the flag only stops the *next* call; the command already running
        # is a separate process tree that must be killed explicitly.
        killed, unreaped = self.process_groups.kill_all()
        return {"cancelled": True, "groups_killed": killed, "unreaped_groups": unreaped}

    # -- internals ----------------------------------------------------------

    def _require_hello(self) -> None:
        if self.home is None:
            raise proto.WorkerProtocolError(
                "not_hello", "send a hello frame before anything else")

    def _check_paths(self, tool: str, arguments: Dict[str, Any]) -> None:
        """Refuse a *file tool* argument that names a location outside the project.

        This is a guard, not the boundary -- the OS sandbox is what enforces the
        boundary (tasks 4.4/4.5). Its job is to turn a mistake into a clear
        refusal instead of a permission error the model will retry.

        ``bash`` is deliberately exempt. Its argument is a shell program, and
        deciding from the text what it will read or write is the "string filter
        standing in for isolation" the design rejects: it would both miss
        ``$(cat ...)`` and refuse an ordinary ``ls /usr/bin``. What a command may
        actually touch is decided by the sandbox, which is why the two are built
        together.
        """
        if tool == "bash":
            return
        home = self.home
        assert home is not None
        for key in _PATH_ARGUMENTS:
            if key not in arguments:
                continue
            for candidate in _iter_path_like(arguments[key]):
                if not home.allows(candidate):
                    raise proto.WorkerProtocolError(
                        "path_outside_project",
                        f"{key} points outside the project: {candidate}")

    def handle(self, frame: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Dispatch one frame. ``None`` means "no reply" (shutdown)."""
        request_id = frame.get("id")
        if not isinstance(request_id, str) or not request_id:
            raise proto.WorkerProtocolError("invalid_request", "id is required")
        op = proto.require_str(frame, "op")
        if op == proto.OP_HELLO:
            return proto.ok(request_id, **self.hello(frame))
        if op == proto.OP_DESCRIBE:
            return proto.ok(request_id, **self.describe())
        if op == proto.OP_CALL:
            return proto.ok(request_id, **self.call(frame))
        if op == proto.OP_CANCEL:
            return proto.ok(request_id, **self.cancel())
        if op == proto.OP_PING:
            return proto.ok(request_id, pong=True, protocol_version=self.protocol_version)
        if op == proto.OP_SHUTDOWN:
            self._stopping = True
            # A clean shutdown must still take the trees with it: the commands this
            # worker started are in their own sessions, so the desktop's own sweep
            # of the worker's process group cannot reach them. Whatever this
            # process may not signal is named, for the desktop to finish.
            killed, unreaped = self.process_groups.kill_all()
            return proto.ok(request_id, stopping=True, groups_killed=killed,
                            unreaped_groups=unreaped)
        raise proto.WorkerProtocolError("unknown_op", f"unknown op: {op!r}")

    def serve(self, reader: io.TextIOBase, writer: io.TextIOBase) -> int:
        """Read frames until EOF or shutdown, answering each one.

        ``call`` runs on its own thread so the reader keeps reading while a tool
        works: that is what makes ``cancel`` able to reach a running ``bash`` at
        all. The reader thread handles the cheap ops (``ping``, ``cancel``,
        ``describe``) inline and refuses a second ``call`` instead of queueing
        it, so there is still exactly one writer per project.

        A refused frame is answered and *not* fatal: one bad frame must not lose
        a worker that is mid-project.
        """
        write_lock = threading.Lock()
        inflight: List[threading.Thread] = []

        def _emit(reply: Dict[str, Any]) -> None:
            # One line per frame; the lock keeps a background call's reply from
            # interleaving with a ping answered on the reader thread.
            with write_lock:
                writer.write(proto.encode(reply) + "\n")
                writer.flush()

        def _run_call(request_id: str, frame: Dict[str, Any]) -> None:
            """Run one call and always release the project, even on a crash."""
            try:
                payload = self.run_call(frame)
                _emit(proto.ok(request_id, **payload))
            except proto.WorkerProtocolError as e:
                _emit(proto.fail(request_id, e.code, e.message))
            except Exception as e:  # pragma: no cover - defensive
                logger.error(f"[DesktopWorker] call failed: {e}", exc_info=True)
                _emit(proto.fail(request_id, "tool_failed", "the tool failed"))
            finally:
                self._busy.release()

        for line in reader:
            if self._stopping:
                break
            try:
                frame = proto.decode(line)
                request_id = frame.get("id")
                if not isinstance(request_id, str) or not request_id:
                    raise proto.WorkerProtocolError("invalid_request", "id is required")
                op = proto.require_str(frame, "op")
                if op == proto.OP_CALL:
                    # Claim the project before starting; the reader stays free.
                    if not self._busy.acquire(blocking=False):
                        raise proto.WorkerProtocolError(
                            "invalid_request", "another call is already running")
                    self._cancel.clear()
                    thread = threading.Thread(
                        target=_run_call, args=(request_id, frame), daemon=True)
                    inflight.append(thread)
                    thread.start()
                    continue
                reply = self.handle(frame)
                if self._stopping:
                    # Shutdown must end the loop now: the next read would block
                    # until the desktop closed the pipe, which it may never do.
                    if reply is not None:
                        _emit(reply)
                    break
            except proto.WorkerProtocolError as e:
                request_id = None
                try:
                    request_id = proto.decode(line).get("id")
                except Exception:
                    pass
                _emit(proto.fail(request_id, e.code, e.message))
                continue
            except Exception as e:  # pragma: no cover - defensive
                logger.error(f"[DesktopWorker] unexpected failure: {e}", exc_info=True)
                _emit(proto.fail(None, "internal", "the worker failed"))
                continue
            if reply is not None:
                _emit(reply)

        # EOF or shutdown: let an in-flight call reach its own terminal state so
        # a half-finished write is not reported as a clean exit.
        for thread in inflight:
            thread.join(timeout=max(self._timeout, 1.0) + 5.0)
        return 0


def _iter_path_like(value: Any):
    """Every string in ``value`` that names a location rather than a value.

    Walks lists/dicts because a tool may take a list of paths. A string is
    treated as a path when it is absolute or contains a separator; a bare
    pattern or search term is not, so ``pattern="beta"`` is never mistaken for
    one.
    """
    if isinstance(value, str):
        if os.path.isabs(value) or os.sep in value or "/" in value:
            yield value
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_path_like(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_path_like(item)


#: The arguments of a file tool that name a filesystem location. Named rather
#: than "every string", so a replacement *text* that happens to contain a slash
#: is not mistaken for a path the tool is about to open.
_PATH_ARGUMENTS = ("path", "paths", "file_path", "directory", "dir")


#: The flag the packaged bundle is started with to become the sandboxed worker
#: (see ``app.py``). Kept in sync by ``tests/test_desktop_local_worker.py``.
WORKER_FLAG = "--desktop-local-worker"

#: Ask the runtime to describe itself, then exit. The desktop needs this because
#: a frozen bundle cannot answer ``python -c '<probe>'``: the sandbox read list
#: has to be derived from the binary that will actually run, and for an installed
#: app that binary is this one.
PROBE_FLAG = "--probe"


def interpreter_report() -> Dict[str, Any]:
    """Where the interpreter that is running lives, in one round trip.

    ``basePrefix``/``stdlib``/``purelib`` are what a virtualenv probe would report
    and are still reported here (a frozen build may leave them pointing at the
    interpreter it was built with). ``bundleRoot``/``frozen`` are what actually
    describes a packaged run: PyInstaller puts the Python runtime and the
    standard library inside the bundle, so the read list has to include that
    directory rather than a build machine's prefix.
    """
    import sysconfig

    paths = sysconfig.get_paths()
    return {
        "realPath": sys.executable,
        "basePrefix": sys.base_prefix,
        "stdlib": paths.get("stdlib", ""),
        "purelib": paths.get("purelib", ""),
        "frozen": bool(getattr(sys, "frozen", False)),
        "bundleRoot": getattr(sys, "_MEIPASS", "") or "",
    }


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point: talk the protocol on stdin/stdout.

    Nothing but frames goes to stdout -- a stray print would corrupt the stream --
    so diagnostics go to stderr. The project root arrives in the desktop's
    ``hello`` frame, not on the command line: the frame and the OS-level
    confinement the launcher applied then describe the same directory, and
    ``COW_DESKTOP_WORKER_ROOT`` (when the launcher set it) is compared against
    the frame so a confused caller cannot point the worker at a project the
    launcher did not confine it to.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args == [PROBE_FLAG]:
        # JSON on stdout and nothing else: the desktop parses the last `{`-line,
        # and a probe that also printed a banner would be read as a failure.
        import json

        sys.stdout.write(json.dumps(interpreter_report()) + "\n")
        sys.stdout.flush()
        return 0
    if args:
        sys.stderr.write(
            "usage: python -m agent.desktop_local.worker"
            "  (the project root is sent in the hello frame)\n")
        return 2
    expected_root = os.environ.get("COW_DESKTOP_WORKER_ROOT", "").strip()
    worker = LocalToolWorker()
    if expected_root:
        worker.expected_root = os.path.realpath(expected_root)
    _install_signal_cleanup(worker)
    return worker.serve(sys.stdin, sys.stdout)


def _install_signal_cleanup(worker: LocalToolWorker) -> None:
    """Take the command trees down when this worker is asked to stop.

    The desktop stops a worker by signalling its process group. The commands this
    worker ran are in their *own* sessions, so they do not receive that signal and
    would be orphaned -- a cancelled run leaving a process behind on the user's
    machine. Handling SIGTERM here is what makes the desktop's kill a real stop
    rather than a stop of the worker alone.

    SIGKILL cannot be handled, which is why the order matters: the desktop sends
    SIGTERM first and only escalates after this has had its chance.

    The exit is deliberate in both directions. Raising :class:`SystemExit` from
    the handler is reported as "Exception ignored" whenever the signal lands
    while the interpreter is already finalising -- a real race, since the desktop
    also stops a worker it has just seen exit, and that report would land on the
    stderr the desktop shows the user. ``os._exit`` after the trees are down is
    unambiguous, and nothing is lost by it: the only teardown this worker owns is
    the one already performed above.
    """

    def _handler(signum, _frame):  # noqa: ANN001 - signal handler signature
        worker.process_groups.kill_all()
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:  # pragma: no cover - defensive
                continue
        os._exit(128 + signum)
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, _handler)
        except (ValueError, OSError):
            # Not on the main thread, or not permitted: the desktop's own sweep
            # still runs, so this is a degraded path rather than a broken one.
            continue


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
