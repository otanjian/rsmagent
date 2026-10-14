# encoding:utf-8
"""Second ``file_lock`` on the same root: the Windows fallback refuses its own lock file.

Hypothesis (from the 403 in the report's own run.log, which says
``[WebChannel] Memory API error: 个人记忆目录被替换``):

``common/safe_fs.py::file_lock`` has two paths. With ``dir_fd`` support (POSIX) it
opens the parent directory as a descriptor and creates the lock file relative to
it. Without it (Windows, ``_DIR_FD_OK is False``) it falls back to
``_resolve_chain(root, ['.memory.lock'], allow_missing_tail=True)`` -- and that
helper requires *every* component to be a directory. The first call creates the
lock file; from the second call on, the file exists, is not a directory, and the
helper raises ``UnsafePathError("non-directory component '.memory.lock'")``.

``agent/memory/personal.py::scope_transaction`` catches exactly ``UnsafePathError``
and re-raises ``PersonalMemoryError('个人记忆目录被替换', code='unsafe_path', 403)``
-- the message the console page renders. That also explains the shape of the
report: the first access works and every later one fails, permanently.

Run: python scripts/repro/repro-lock-second-call.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from common import safe_fs  # noqa: E402


def lock(root):
    with safe_fs.file_lock(root, ".memory.lock"):
        return "locked"


def twice(label, root):
    print("\n== %s" % label)
    print("   root: %s" % root)
    print("   _DIR_FD_OK: %s" % safe_fs._DIR_FD_OK)
    for attempt in (1, 2, 3):
        try:
            lock(root)
            present = os.path.exists(os.path.join(root, ".memory.lock"))
            print("   call %d: ok (lock file exists: %s)" % (attempt, present))
        except Exception as error:  # noqa: BLE001 - the failure is the finding
            print("   call %d: %s: %s" % (attempt, type(error).__name__, error))
            print("      " + traceback.format_exc(limit=4).replace("\n", "\n      "))


def main():
    base = tempfile.mkdtemp(prefix="cow-lock-repro-")
    root = os.path.join(base, "用户 1001", "memory-root")
    os.makedirs(root)

    twice("as shipped on this platform", root)

    # Same call sequence with the descriptor path forced on: this is what POSIX
    # does, and it isolates "the fallback is the bug" from "the guard is the bug".
    print("\n== same sequence with _DIR_FD_OK forced True (the POSIX path)")
    original = safe_fs._DIR_FD_OK
    safe_fs._DIR_FD_OK = True
    try:
        twice("forced descriptor path", root)
    except Exception as error:  # noqa: BLE001
        print("   forcing the flag is not a faithful stand-in here: %s" % error)
    finally:
        safe_fs._DIR_FD_OK = original

    print("\n== the effect seen at the service layer")
    from agent.memory.personal import scope_transaction, PersonalMemoryError
    try:
        with scope_transaction(root):
            print("   scope_transaction: entered")
    except PersonalMemoryError as error:
        print("   scope_transaction raised: code=%s status=%s message=%s"
              % (error.code, error.status, error))
        print("   __cause__: %r" % (error.__cause__,))
    except Exception as error:  # noqa: BLE001
        print("   scope_transaction raised %s: %s" % (type(error).__name__, error))


if __name__ == "__main__":
    main()
