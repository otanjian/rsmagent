"""
Registry for shell commands running in the background.

Without this the model hand-rolls backgrounding - `nohup cmd &`, echo the PID,
`sleep 1`, then curl to see whether it came up - which is both verbose and
unreliable (the sleep is always either too short or wasted). Here the tool
starts the process, hands back an id, and later calls read whatever the process
has printed since the last look.

Tool instances are created per call, so the registry has to live at module
level to outlive them.

Processes are deliberately NOT killed when the agent finishes: a background
command is usually a server the user asked to have running. Use kill() to stop
one on purpose.

That last rule is the *serial* path's, and it is inherited here as-is for the
server. A confined local run is the exception, and deliberately so: its whole
`process_group_hook` exists so the run's owner can end everything the run
started, because the acceptance criteria for a local run require the tree to be
gone once the run is (A17/A18/A19) -- a server left behind on the user's machine
after the project closed is a leak, not a feature. The hook is only ever
installed by that caller; without it this module behaves exactly as documented
above.
"""

import os
import subprocess
import sys
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional, Tuple

from agent.tools.bash.decode import decode_output
from agent.tools.bash import launcher
from agent.tools.bash.redaction import StreamRedactor, redact_text

_IS_WIN = sys.platform == "win32"

#: Called with the process-group id of every job started here.
#:
#: Mirrors `Bash.process_group_hook` (see that attribute for the full reasoning);
#: a job is started in its own session too, so its group id is the only handle to
#: the tree and the caller that owns the run needs it.
_process_group_hook: Optional[Callable[[int], None]] = None


def set_process_group_hook(hook: Optional[Callable[[int], None]]) -> None:
    """Install (or clear) the hook that receives each job's process-group id."""
    global _process_group_hook
    _process_group_hook = hook

# Per-job output cap. A chatty server would otherwise grow without bound; the
# oldest output is dropped first since the tail is what matters when checking
# on a process.
_MAX_BUFFER_BYTES = 256 * 1024

# Finished jobs stay readable for a while so a late poll still sees the exit
# code, but the registry must not grow forever.
_MAX_JOBS = 20
_DEFAULT_MAX_RUNNING = 20


class CapacityError(RuntimeError):
    """The process-wide background capacity is currently occupied."""


def _running_limit() -> int:
    try:
        value = int(os.environ.get('COW_BASH_MAX_RUNNING', _DEFAULT_MAX_RUNNING))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_RUNNING
    return value if value > 0 else _DEFAULT_MAX_RUNNING


class _Job:
    def __init__(self, job_id: str, command: str, process: subprocess.Popen,
                 temp_script: Optional[str] = None):
        self.owner = launcher.current_owner()
        self.id = job_id
        self.command = redact_text(command, getattr(process, 'execution_secrets', {}).values())
        self.process = process
        self.temp_script = temp_script
        self.started_at = time.time()
        self.buffer = bytearray()
        self.cursor = 0
        self.dropped = 0
        self.lock = threading.Lock()
        self.cleanup_lock = threading.Lock()
        self.tree_cleaned = False
        self.readers: List[threading.Thread] = []

    def append(self, chunk: bytes) -> None:
        with self.lock:
            self.buffer.extend(chunk)
            overflow = len(self.buffer) - _MAX_BUFFER_BYTES
            if overflow > 0:
                del self.buffer[:overflow]
                self.cursor = max(0, self.cursor - overflow)
                self.dropped += overflow

    def take_new_output(self) -> Tuple[str, int]:
        """Return output printed since the last call, and bytes lost to the cap."""
        with self.lock:
            chunk = bytes(self.buffer[self.cursor:])
            self.cursor = len(self.buffer)
            dropped, self.dropped = self.dropped, 0
        return decode_output(chunk), dropped

    @property
    def running(self) -> bool:
        return self.process.poll() is None


_lock = threading.Lock()
_jobs: Dict[str, _Job] = {}
# Reservations cover slow process creation without holding the registry lock,
# so output polling and cancellation remain available while another job starts.
_starting = 0


def _drain(job: _Job, stream) -> None:
    redactor = StreamRedactor(getattr(job.process, 'execution_secrets', {}).values())
    try:
        while True:
            chunk = os.read(stream.fileno(), 4096)
            job.append(redactor.feed(chunk, final=not chunk))
            if not chunk:
                break
    except (OSError, ValueError):
        pass


def _evict_finished() -> None:
    """Drop the oldest finished jobs once the registry is full."""
    if len(_jobs) < _MAX_JOBS:
        return
    finished = sorted(
        (j for j in _jobs.values() if not j.running),
        key=lambda j: j.started_at,
    )
    for job in finished[: len(_jobs) - _MAX_JOBS + 1]:
        _cleanup(job)
        _jobs.pop(job.id, None)


def _cleanup(job: _Job) -> None:
    with job.cleanup_lock:
        if job.running or job.tree_cleaned:
            return
        # Kill once, immediately on exit. Never signal an old numeric process
        # group on a later output poll: the OS may have reused its identifier.
        _kill_process(job.process)
        job.tree_cleaned = True
        launcher.release(job.process)
        if job.temp_script:
            try:
                os.remove(job.temp_script)
            except OSError:
                pass
            job.temp_script = None


def _watch(job: _Job) -> None:
    job.process.wait()
    _cleanup(job)
    for reader in job.readers:
        reader.join(timeout=2)
    job.process.execution_secrets = {}


def start(command: str, cwd: str, env: dict, temp_script: Optional[str] = None) -> str:
    """Launch *command* in the background and return its job id."""
    global _starting
    reserved = False
    process = None
    job = None
    try:
        limit = _running_limit()
        with _lock:
            if _starting + sum(not item.tree_cleaned for item in _jobs.values()) >= limit:
                raise CapacityError(
                    f'Background task capacity reached ({limit} running or starting jobs). '
                    'Wait for a task to finish or stop one of your jobs, then retry.')
            _starting += 1
            reserved = True
        process = launcher.popen(
            command,
            shell=True,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=not _IS_WIN,
        )
        # A new session means the group id is the pid: reported before anything
        # waits, so the run's owner can stop the shell and its children.
        if _process_group_hook is not None and not _IS_WIN:
            try:
                _process_group_hook(process.pid)
            except Exception:
                pass
        job = _Job(f"bash_{uuid.uuid4().hex[:8]}", command, process, temp_script)
        reader = threading.Thread(target=_drain, args=(job, process.stdout), daemon=True)
        job.readers.append(reader)
        reader.start()
        threading.Thread(target=_watch, args=(job,), daemon=True).start()
        with _lock:
            _evict_finished()
            _jobs[job.id] = job
            _starting -= 1
            reserved = False
        return job.id
    except BaseException:
        if process is not None:
            _kill_process(process)
            process.wait(timeout=5)
            if job is not None:
                _cleanup(job)
                for reader in job.readers:
                    if reader.ident is not None:
                        reader.join(timeout=2)
            else:
                launcher.release(process)
            if process.stdout is not None:
                process.stdout.close()
        if temp_script:
            try:
                os.remove(temp_script)
            except OSError:
                pass
        raise
    finally:
        if reserved:
            with _lock:
                _starting -= 1


def read(job_id: str) -> Optional[dict]:
    """Output printed since the last read, plus current status.

    Returns None when *job_id* is unknown.
    """
    with _lock:
        job = _jobs.get(job_id)
    if job is None or not launcher.may_access(job.owner):
        return None

    output, dropped = job.take_new_output()
    running = job.running
    if not running:
        # Give the reader a moment to flush whatever was buffered at exit.
        for reader in job.readers:
            reader.join(timeout=1)
        tail, more_dropped = job.take_new_output()
        output += tail
        dropped += more_dropped
        _cleanup(job)

    return {
        "id": job.id,
        "command": job.command,
        "running": running,
        "exit_code": None if running else job.process.returncode,
        "output": output,
        "dropped_bytes": dropped,
        "elapsed": round(time.time() - job.started_at, 1),
    }


def kill(job_id: str) -> Optional[bool]:
    """Terminate a background job. Returns None when *job_id* is unknown."""
    with _lock:
        job = _jobs.get(job_id)
    if job is None or not launcher.may_access(job.owner):
        return None
    with job.cleanup_lock:
        if not job.tree_cleaned:
            _kill_process(job.process)
            job.process.wait()
    _cleanup(job)
    return True


def list_jobs() -> List[dict]:
    with _lock:
        jobs = list(_jobs.values())
    return [
        {
            "id": j.id,
            "command": j.command,
            "running": j.running,
            "elapsed": round(time.time() - j.started_at, 1),
        }
        for j in jobs if launcher.may_access(j.owner)
    ]


def _kill_process(process: subprocess.Popen) -> None:
    """Kill the whole process group - a shell command is usually a tree."""
    if _IS_WIN:
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=5,
            )
            if result.returncode != 0 and process.poll() is None:
                process.kill()
        except (OSError, subprocess.SubprocessError):
            if process.poll() is None:
                process.kill()
    else:
        import signal
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError):
            if process.poll() is None:
                process.kill()


def reset() -> None:
    """Kill everything and clear the registry (tests)."""
    with _lock:
        jobs = list(_jobs.values())
        _jobs.clear()
    for job in jobs:
        with job.cleanup_lock:
            if not job.tree_cleaned:
                _kill_process(job.process)
                job.process.wait()
        _cleanup(job)
