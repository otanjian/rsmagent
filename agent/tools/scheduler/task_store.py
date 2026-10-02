"""
Task storage management for scheduler
"""

import json
import os
import threading
from datetime import datetime
from typing import Dict, List, Optional
from common.atomic_write import write_text_atomic
from common.utils import expand_path


_store_locks = {}
_store_locks_guard = threading.Lock()


def _lock_for_path(store_path: str):
    normalized_path = os.path.normcase(os.path.realpath(store_path))
    with _store_locks_guard:
        return _store_locks.setdefault(normalized_path, threading.RLock())


class TaskRevisionConflict(Exception):
    """A write was refused because the stored revision moved under it.

    Raised when a caller supplies ``expected_revision`` and the task has been
    written since. It exists so two editors — the Web console and an Agent tool
    call in the same conversation, say — cannot silently overwrite each other:
    the loser is told to reload rather than having its change disappear.
    """


class MultiWriterDeploymentError(RuntimeError):
    """A second process is writing this task store.

    ``TaskStore`` coordinates writers with an in-process lock, so two processes
    sharing a store would interleave read-modify-write cycles and lose tasks
    (``load`` in process A, ``load`` in process B, ``save`` A, ``save`` B — A's
    change is gone). The store therefore takes a pid lease next to the file and
    refuses when another *live* process holds it: a refused write is a loud
    deployment error, a silently merged write is data loss.
    """


class TaskWriteLease:
    """A pid lease on one task store, so multi-writer deployments fail loudly.

    The lease is advisory and cheap: the file records the writing pid, and a
    lease whose pid is gone is taken over (a crashed process must not lock the
    scheduler out for ever). Same-pid holders — several ``TaskStore`` instances
    inside one process, which is what tests and the Web layer both do — are
    always allowed, because the shared in-process lock already covers them.
    """

    def __init__(self, store_path: str):
        self.path = store_path + ".writer"
        self.pid = os.getpid()
        self._held = False

    def _read_pid(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return int((handle.read() or "0").strip() or 0)
        except (OSError, ValueError):
            return 0

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if pid == os.getpid():
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Another user's process holds it; still alive, still a conflict.
            return True
        except OSError:
            return False
        return True

    def acquire(self) -> None:
        if self._held:
            return
        holder = self._read_pid()
        if holder and holder != self.pid and self._pid_alive(holder):
            raise MultiWriterDeploymentError(
                "another process (pid %s) is writing %s; multi-writer "
                "deployments are not supported" % (holder, self.path))
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                handle.write(str(self.pid))
        except OSError:
            # No lease file: fall back to the in-process lock only. Refusing
            # every write on a read-only directory would be worse than the risk
            # this advisory lease removes.
            return
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            if self._read_pid() == self.pid:
                os.unlink(self.path)
        except OSError:
            pass
def _read_tasks(path: str):
    """Return ``(raw_text, tasks)`` from a store file, raising if it is unusable."""
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    data = json.loads(text)
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, dict) or any(not isinstance(task, dict) for task in tasks.values()):
        raise ValueError(f"invalid task store payload: {path}")
    return text, tasks


class _DescStr:
    """Sort a string descending inside an otherwise-ascending sort key tuple.

    Lets ``sort_key`` mix an ascending rank (enabled-first) with a descending
    field (newest ``created_at`` on top) in one ``sort`` call, without a second
    pass or reversing the whole list.
    """

    __slots__ = ("value",)

    def __init__(self, value: str):
        self.value = value or ""

    def __lt__(self, other: "_DescStr") -> bool:
        # Reversed comparison => larger (later) strings sort first.
        return self.value > other.value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _DescStr) and self.value == other.value


class TaskStore:
    """
    Manages persistent storage of scheduled tasks
    """
    
    def __init__(self, store_path: str = None):
        """
        Initialize task store
        
        Args:
            store_path: Path to tasks.json file. Defaults to ~/cow/scheduler/tasks.json
        """
        if store_path is None:
            # Default to ~/cow/scheduler/tasks.json
            home = expand_path("~")
            store_path = os.path.join(home, "cow", "scheduler", "tasks.json")
        
        self.store_path = store_path
        self.lock = _lock_for_path(store_path)
        self.lease = TaskWriteLease(store_path)
        self._ensure_store_dir()
    
    def _ensure_store_dir(self):
        """Ensure the storage directory exists.

        An empty ``dirname`` is not a failure: it is a bare filename (or a device
        path -- Windows resolves ``os.devnull`` to ``nul``), which the dry-run
        stores the console builds for schedule arithmetic pass in on purpose.
        ``os.makedirs('')`` used to raise there, which turned every console
        create/update on Windows into a 500 before anything was written.
        """
        store_dir = os.path.dirname(self.store_path)
        if not store_dir:
            return
        os.makedirs(store_dir, exist_ok=True)
    
    def load_tasks(self) -> Dict[str, dict]:
        """
        Load all tasks from storage
        
        Returns:
            Dictionary of task_id -> task_data
        """
        with self.lock:
            if not os.path.exists(self.store_path):
                return {}
            
            try:
                return _read_tasks(self.store_path)[1]
            except Exception as e:
                print(f"Error loading tasks: {e}")

            try:
                backup_text, tasks = _read_tasks(f"{self.store_path}.bak")
            except Exception as e:
                print(f"Error loading task backup: {e}")
                return {}

            # Repair the primary before a later save copies it into .bak.
            # Otherwise a new task would overwrite the only good backup.
            try:
                write_text_atomic(self.store_path, backup_text)
            except Exception as e:
                print(f"Error restoring task store from backup: {e}")
            return tasks
    
    def save_tasks(self, tasks: Dict[str, dict]):
        """
        Save all tasks to storage
        
        Args:
            tasks: Dictionary of task_id -> task_data
        """
        with self.lock:
            try:
                # Create backup. The store is written as UTF-8 below and read
                # back as UTF-8 in load_tasks(), so the copy has to go through
                # the same codec: with the platform default it is decoded
                # through the wrong one on Windows (cp936 on a zh-CN box), and
                # the backup ends up either mojibake or -- since the open() for
                # writing already truncated it -- an empty file where a usable
                # one used to be.
                if os.path.exists(self.store_path):
                    backup_path = f"{self.store_path}.bak"
                    try:
                        with open(self.store_path, 'r', encoding='utf-8') as src:
                            previous = src.read()
                        write_text_atomic(backup_path, previous)
                    except Exception:
                        pass

                # Save tasks
                data = {
                    "version": 1,
                    "updated_at": datetime.now().isoformat(),
                    "tasks": tasks
                }

                write_text_atomic(
                    self.store_path, json.dumps(data, ensure_ascii=False, indent=2)
                )
            except Exception as e:
                print(f"Error saving tasks: {e}")
                raise
    
    def add_task(self, task: dict) -> bool:
        """
        Add a new task
        
        Args:
            task: Task data dictionary
            
        Returns:
            True if successful
        """
        with self.lock:
            self.lease.acquire()
            tasks = self.load_tasks()
            task_id = task.get("id")

            if not task_id:
                raise ValueError("Task must have an 'id' field")

            if task_id in tasks:
                raise ValueError(f"Task with id '{task_id}' already exists")

            tasks[task_id] = task
            self.save_tasks(tasks)
        return True
    
    def update_task(self, task_id: str, updates: dict,
                    expected_revision: int = None) -> bool:
        """
        Update an existing task

        Args:
            task_id: Task ID
            updates: Dictionary of fields to update
            expected_revision: When given, the write is refused with
                ``TaskRevisionConflict`` unless the stored task's revision equals
                it. The revision is incremented on every successful write, so a
                caller that read a task and wants to change it can detect that
                someone else wrote in between instead of overwriting them.

        Returns:
            True if successful

        Raises:
            ValueError: no such task.
            TaskRevisionConflict: the stored revision moved.
            MultiWriterDeploymentError: another process holds the store.
        """
        with self.lock:
            self.lease.acquire()
            tasks = self.load_tasks()

            if task_id not in tasks:
                raise ValueError(f"Task '{task_id}' not found")

            current = tasks[task_id]
            if expected_revision is not None:
                actual = current.get("revision")
                if actual is None:
                    # A task written before revisions existed is at revision 1
                    # by definition (creation was the first write).
                    actual = 1
                if int(actual) != int(expected_revision):
                    raise TaskRevisionConflict(
                        "task '%s' revision is %s, expected %s"
                        % (task_id, actual, expected_revision))

            patch = {k: v for k, v in (updates or {}).items() if k != "revision"}
            current.update(patch)
            current["revision"] = int(current.get("revision") or 1) + 1
            current["updated_at"] = datetime.now().isoformat()

            self.save_tasks(tasks)
        return True
    
    def delete_task(self, task_id: str) -> bool:
        """
        Delete a task
        
        Args:
            task_id: Task ID
            
        Returns:
            True if successful
        """
        with self.lock:
            self.lease.acquire()
            tasks = self.load_tasks()

            if task_id not in tasks:
                raise ValueError(f"Task '{task_id}' not found")

            del tasks[task_id]
            self.save_tasks(tasks)
        return True
    
    def get_task(self, task_id: str) -> Optional[dict]:
        """
        Get a specific task
        
        Args:
            task_id: Task ID
            
        Returns:
            Task data or None if not found
        """
        tasks = self.load_tasks()
        return tasks.get(task_id)
    
    def list_tasks(self, enabled_only: bool = False, agent_id: str = None) -> List[dict]:
        """
        List all tasks

        Args:
            enabled_only: If True, only return enabled tasks
            agent_id: If given, only return tasks owned by this Agent. Ownership
                is the task's *effective* owner: for an IM task that is the
                delivery instance's current binding (so re-binding a channel
                re-buckets its tasks with no data change), else the stored
                ``agent_id``, else the default Agent. This keeps the per-Agent
                list identical to what actually runs.

        Returns:
            List of task dictionaries
        """
        tasks = self.load_tasks()
        task_list = list(tasks.values())

        if enabled_only:
            task_list = [t for t in task_list if t.get("enabled", True)]

        if agent_id:
            from agent.tools.scheduler.integration import effective_task_agent_id
            default_id = ""
            try:
                from agent.registry import get_agent_registry
                default_id = get_agent_registry().default_agent_id
            except Exception:
                pass
            task_list = [
                t for t in task_list
                if (effective_task_agent_id(t) or default_id) == agent_id
            ]
        
        # Enabled tasks first, then newest-created on top (a task the user just
        # created should sit at the head of the list rather than wherever its
        # next_run_at happens to fall). created_at is an ISO string so a plain
        # string compare orders it chronologically; a legacy task missing it
        # sorts last within its group.
        def sort_key(t):
            enabled = t.get("enabled", True)
            created = t.get("created_at") or ""
            # Negate the created_at ordering for descending: pair the enabled
            # rank (ascending) with the created string reversed via a wrapper.
            return (0 if enabled else 1, _DescStr(created))

        task_list.sort(key=sort_key)

        return task_list
    
    def enable_task(self, task_id: str, enabled: bool = True) -> bool:
        """
        Enable or disable a task
        
        Args:
            task_id: Task ID
            enabled: True to enable, False to disable
            
        Returns:
            True if successful
        """
        return self.update_task(task_id, {"enabled": enabled})
