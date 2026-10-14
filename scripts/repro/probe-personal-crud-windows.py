# encoding:utf-8
"""Personal-memory CRUD inside a transaction, on awkward Windows paths.

Change ``harden-windows-desktop-reliability``, task 3.2. The lock defect was in
``file_lock``, but what a user experiences is the *memory* path: save, list,
read, edit, delete. This drives those primitives the way
``agent.memory.personal``'s scope transaction does, on a root that has Chinese
characters, a space and (when another drive exists) a different drive letter, so
"the fix works on a normal path" and "the fix works on this user's path" are the
same statement.

Every scratch directory is created with ``mkdtemp`` and recorded, and cleanup
removes exactly those paths -- never a computed ancestor. (An earlier revision
derived the cleanup path by walking up from the root, which on a ``D:`` root
resolved to the drive itself; this note is here so that class of mistake is not
repeated.)

Run: python scripts/repro/probe-personal-crud-windows.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from common import safe_fs  # noqa: E402
from agent.memory.personal import scope_transaction  # noqa: E402

ENTRY = "memory/会议 纪要 1.md"
BODY = "第一行\n第二行 with spaces\n"

#: Directories this process created; the only things it is allowed to remove.
CREATED = []


def make_root(base=None):
    created = tempfile.mkdtemp(prefix="cow-crud-", dir=base)
    CREATED.append(created)
    return os.path.join(created, "用户 1001", "memory-root")


def candidate_roots():
    """A temp root on the system drive, and one on another drive if there is one."""
    roots = [("system drive", make_root())]
    if os.name == "nt":
        for letter in ("D:", "E:", "F:"):
            base = letter + os.sep
            if os.path.isdir(base):
                try:
                    roots.append(("drive %s" % letter, make_root(base)))
                except OSError as error:
                    roots.append(("drive %s (unusable: %s)" % (letter, error), None))
                break
    return roots


def cycle(label, root):
    print("\n== %s: %s" % (label, root), flush=True)
    results = []

    def step(name, fn):
        try:
            value = fn()
            results.append((name, "ok", repr(value)[:80]))
        except Exception as error:  # noqa: BLE001 - showing all of them is the point
            results.append((name, type(error).__name__, str(error)[:200]))
        print("   %-28s %-18s %s" % (results[-1][0], results[-1][1], results[-1][2]), flush=True)

    def write_transaction():
        with scope_transaction(root):
            return safe_fs.write_text_atomic(root, ENTRY, BODY)

    def read_transaction():
        with scope_transaction(root):
            return safe_fs.read_text(root, ENTRY)

    def edit_transaction():
        with scope_transaction(root):
            safe_fs.write_text_atomic(root, ENTRY, BODY + "第三行\n")
            return safe_fs.read_text(root, ENTRY)

    def delete_transaction():
        with scope_transaction(root):
            return safe_fs.unlink(root, ENTRY)

    step("transaction 1: write", write_transaction)
    step("transaction 2: read back", read_transaction)
    step("list the directory", lambda: safe_fs.list_names(root, "memory"))
    step("stat the entry", lambda: bool(safe_fs.stat(root, ENTRY)))
    step("body matches", lambda: safe_fs.read_text(root, ENTRY) == BODY)
    step("transaction 3: edit + read", edit_transaction)
    step("transaction 4: delete", delete_transaction)
    step("entry is gone", lambda: not safe_fs.exists(root, ENTRY))
    step("lock file survived", lambda: os.path.isfile(os.path.join(root, ".memory.lock")))

    return all(kind == "ok" for name, kind, _ in results if not name.startswith("  "))


def _removable(path):
    """True only for a scratch directory *this run* created.

    Three independent conditions, because getting this wrong once deleted real
    data: a computed ancestor (``%TEMP%``, ``D:\\``) was handed to
    ``shutil.rmtree``. The path must have been recorded by :func:`make_root`,
    must be at least two components deep (a drive root or ``/tmp`` never is),
    and must still carry the scratch prefix.
    """
    if path not in CREATED:
        return False
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    if len(parts) < 2:
        return False
    return os.path.basename(path).startswith("cow-crud-")


def cleanup():
    for path in CREATED:
        if not _removable(path):
            print("REFUSED to remove %s (not a scratch directory of this run)" % path, flush=True)
            continue
        shutil.rmtree(path, ignore_errors=True)
        print("cleaned %s (exists now: %s)" % (path, os.path.exists(path)), flush=True)


def main():
    print("platform: %s  python: %s  _DIR_FD_OK: %s"
          % (sys.platform, sys.version.split()[0], safe_fs._DIR_FD_OK), flush=True)
    ok = True
    try:
        for label, root in candidate_roots():
            if root is None:
                print("\n== %s: skipped" % label, flush=True)
                continue
            ok = cycle(label, root) and ok
    finally:
        cleanup()
    print("\nALL STEPS OK" if ok else "\nSOME STEPS FAILED", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
