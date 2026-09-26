"""Cross-process knowledge-root locks and the mode-switch recovery marker.

Change ``add-traceable-knowledge-ingestion`` (design D6, task 1.4).

File and catalog access must be mutually exclusive with a knowledge-root move.
Two locks, both backed by ``flock`` on a file *outside* the knowledge root (a
move renames the root, so a lock inside it would be renamed away mid-hold):

- a **shared** usage lock that upload, download, task enqueue, conversion,
  index sync and cleanup take for the whole I/O; and
- an **exclusive** lock a mode switch takes after re-checking persisted task
  state, so it can never replace the directory while another process is
  mid-write.

Queued/running tasks and uncommitted version records are reported as a busy
root even when no process currently holds the lock, so a switch cannot slip in
between two tasks.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

try:  # POSIX only; the platform falls back to a single-process no-op elsewhere.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


class KnowledgeBusyError(RuntimeError):
    """A retryable refusal: the knowledge root has work in flight.

    Carries HTTP semantics so the web layer can answer ``409 knowledge_busy``
    instead of degrading into an opaque handler error.
    """

    code = "knowledge_busy"
    status = 409


class KnowledgeUnavailableError(RuntimeError):
    """The root cannot be served safely: a mode switch awaits recovery."""

    code = "knowledge_unavailable"
    status = 503


#: Marker recording an in-flight knowledge mode switch. Lives in the Agent's
#: workspace (not the knowledge root, which is what is being renamed).
MARKER_NAME = ".knowledge_mode_switch.json"


def lock_dir() -> Path:
    """Trusted runtime directory for lock files, outside every knowledge root.

    ``COW_KNOWLEDGE_LOCK_DIR`` overrides it for tests and unusual deployments;
    otherwise the system temp dir is used, which is writable and shared by every
    process on the host.
    """
    override = os.environ.get("COW_KNOWLEDGE_LOCK_DIR")
    path = Path(os.path.expanduser(override)) if override else (
        Path(tempfile.gettempdir()) / "rsmagent-knowledge-locks"
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def lock_file_for(knowledge_root) -> Path:
    """A stable lock filename derived from the resolved root path.

    Keyed on the real path so an Agent that reaches the same base through a
    symlink contends on the same lock instead of a second, independent one.
    """
    real = os.path.realpath(str(knowledge_root))
    digest = hashlib.sha256(real.encode("utf-8")).hexdigest()[:32]
    return lock_dir() / f"{digest}.lock"


class KnowledgeRootLock:
    """Shared/exclusive lock for one knowledge root."""

    def __init__(self, knowledge_root):
        self.knowledge_root = str(knowledge_root)
        self.path = lock_file_for(knowledge_root)

    @contextmanager
    def _acquire(self, flag: int, timeout: float,
                 poll: float = 0.02) -> Iterator[bool]:
        if fcntl is None:  # pragma: no cover - non-POSIX
            yield True
            return
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        acquired = False
        try:
            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                try:
                    fcntl.flock(fd, flag | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(poll)
            yield acquired
        finally:
            if acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(fd)

    @contextmanager
    def usage(self, timeout: float = 30.0) -> Iterator[bool]:
        """Shared lock held across one knowledge operation's I/O."""
        with self._acquire(fcntl.LOCK_SH if fcntl else 0, timeout) as ok:
            yield ok

    @contextmanager
    def exclusive(self, timeout: float = 0.0) -> Iterator[bool]:
        """Exclusive lock for a mode switch; fails fast by default."""
        with self._acquire(fcntl.LOCK_EX if fcntl else 0, timeout) as ok:
            yield ok


def knowledge_root_is_busy(knowledge_root) -> bool:
    """True when the root has queued/running work or uncommitted versions."""
    from agent.knowledge.catalog import KnowledgeCatalog

    catalog = KnowledgeCatalog.open_if_exists(str(knowledge_root))
    return bool(catalog and catalog.has_blocking_tasks())


# ----------------------------------------------------------------------
# Mode-switch recovery marker
# ----------------------------------------------------------------------
def marker_path(workspace) -> Path:
    return Path(workspace) / MARKER_NAME


def read_marker(workspace) -> Optional[dict]:
    """Read the pending switch marker, or ``None`` when the workspace is clean."""
    path = marker_path(workspace)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"corrupt": True}
    return payload if isinstance(payload, dict) else {"corrupt": True}


def write_marker(workspace, payload: dict) -> None:
    path = marker_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(payload)
    record.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    path.write_text(json.dumps(record), encoding="utf-8")


def clear_marker(workspace) -> None:
    try:
        marker_path(workspace).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
