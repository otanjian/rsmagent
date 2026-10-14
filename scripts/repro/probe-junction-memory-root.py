# encoding:utf-8
"""Does a *junction* at the personal-memory root get refused?

Change ``harden-windows-desktop-reliability``, task 3.4. On Windows a directory
junction is a reparse point that redirects a path without being a symlink:

* Python <= 3.11 reports it as a symlink (``os.path.islink`` is True), so the
  guards in ``common/safe_fs`` and ``agent/memory/personal`` refuse it;
* Python >= 3.12 reports it as a junction (``os.path.islink`` is False,
  ``os.path.isjunction`` is True) -- and every anti-replacement guard in this
  repository asks ``os.path.islink``.

This script answers the question with a real junction rather than an argument:
it replaces a synthetic personal root with a junction to *another* directory and
then drives the delivered code path (``scope_transaction`` + the safe_fs
primitives). The junction is created with ``mklink /J``, which needs no special
privilege -- unlike ``os.symlink``, which this session cannot use.

Run: python scripts/repro/probe-junction-memory-root.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from common import safe_fs  # noqa: E402
from agent.memory.personal import PersonalMemoryError, scope_transaction  # noqa: E402

CREATED = []


def make_dir(prefix="cow-junction-"):
    path = tempfile.mkdtemp(prefix=prefix)
    CREATED.append(path)
    return path


def make_junction(link: str, target: str) -> bool:
    """Create a directory junction. Returns False when the platform refuses."""
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", link, target],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and os.path.exists(link)


def cleanup():
    for path in CREATED:
        if path in CREATED and os.path.basename(path).startswith("cow-junction-"):
            shutil.rmtree(path, ignore_errors=True)
            print("cleaned %s" % path, flush=True)


def main():
    print("platform=%s python=%s" % (sys.platform, sys.version.split()[0]), flush=True)
    real_user = make_dir()
    other_user = make_dir()
    with open(os.path.join(real_user, "MEMORY.md"), "w", encoding="utf-8") as handle:
        handle.write("own memory\n")
    with open(os.path.join(other_user, "MEMORY.md"), "w", encoding="utf-8") as handle:
        handle.write("SOMEONE ELSE'S MEMORY\n")

    # The personal root the service would use is replaced by a junction that
    # points at another user's directory.
    shared = make_dir()
    users = os.path.join(shared, "users")
    os.makedirs(users)
    root = os.path.join(users, "1001")
    if not make_junction(root, other_user):
        print("RESULT: cannot create a junction on this platform -- nothing to measure")
        return 3

    print("root    : %s  (junction ->)" % root, flush=True)
    print("target  : %s" % other_user, flush=True)
    print("islink=%s isjunction=%s" % (
        os.path.islink(root), getattr(os.path, "isjunction", lambda p: "n/a")(root)), flush=True)

    findings = []
    try:
        with scope_transaction(root):
            findings.append(("scope_transaction", "ENTERED (not refused)"))
    except PersonalMemoryError as error:
        findings.append(("scope_transaction", "refused: code=%s status=%s" % (error.code, error.status)))
    except Exception as error:  # noqa: BLE001
        findings.append(("scope_transaction", "%s: %s" % (type(error).__name__, error)))

    try:
        text = safe_fs.read_text(root, "MEMORY.md")
        findings.append(("read_text(MEMORY.md)", "READ: %r" % text))
    except safe_fs.UnsafePathError as error:
        findings.append(("read_text(MEMORY.md)", "refused: UnsafePathError(%s)" % error))
    except Exception as error:  # noqa: BLE001
        findings.append(("read_text(MEMORY.md)", "%s: %s" % (type(error).__name__, error)))

    leaked = False
    print("\n--- what the delivered code did ---", flush=True)
    for label, outcome in findings:
        if "SOMEONE ELSE" in outcome:
            leaked = True
        print("  %-24s %s" % (label, outcome), flush=True)

    print("\nRESULT: %s" % (
        "LEAK -- a junction root reaches another user's files" if leaked
        else "no leak observed through these calls"), flush=True)
    return 0 if not leaked else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        cleanup()
    sys.exit(code)
