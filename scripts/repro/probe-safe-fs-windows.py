# Direct probe of the safe_fs primitives the personal-memory service uses.
#
# Synthetic directories only: a plain root, a root with Chinese characters and a
# space, and a root that does not exist yet (first access on a fresh account).
# Prints one line per primitive so a Windows-specific refusal is visible without
# the identity stack.
#
# Run: python scripts/repro/probe-safe-fs-windows.py
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from common import safe_fs  # noqa: E402


def lock(root):
    with safe_fs.file_lock(root, ".memory.lock"):
        return "locked"


def attempt(results, label, fn):
    try:
        value = fn()
        results.append((label, "ok", repr(value)[:140]))
    except Exception as error:  # noqa: BLE001 - seeing everything is the point
        results.append((label, type(error).__name__, str(error)[:200]))
        results.append(("  traceback", "", traceback.format_exc(limit=8).replace("\n", " | ")[:600]))


def main():
    results = []
    base = tempfile.mkdtemp(prefix="cow-safefs-probe-")
    plain = os.path.join(base, "plain")
    chinese = os.path.join(base, "中文 目录 with space")
    os.makedirs(plain)
    os.makedirs(chinese)

    for root, name in ((plain, "plain"), (chinese, "chinese")):
        attempt(results, "file_lock(%s)" % name, lambda r=root: lock(r))
        attempt(results, "write_text_atomic(%s)" % name,
                lambda r=root: safe_fs.write_text_atomic(r, "memory/notes.md", "hello"))
        attempt(results, "read_text(%s)" % name,
                lambda r=root: safe_fs.read_text(r, "memory/notes.md"))
        attempt(results, "list_names(%s)" % name,
                lambda r=root: safe_fs.list_names(r, "memory"))
        attempt(results, "is_file(%s)" % name,
                lambda r=root: safe_fs.is_file(r, "memory/notes.md"))
        attempt(results, "exists(%s)" % name,
                lambda r=root: safe_fs.exists(r, "memory/notes.md"))
        attempt(results, "stat(%s)" % name,
                lambda r=root: safe_fs.stat(r, "memory/notes.md"))
        attempt(results, "resolve_within(%s)" % name,
                lambda r=root: safe_fs.resolve_within(r, "memory/notes.md"))
        attempt(results, "unlink(%s)" % name,
                lambda r=root: safe_fs.unlink(r, "memory/notes.md"))

    fresh = os.path.join(base, "中文 新账号", "users", "1001")
    attempt(results, "file_lock(root does not exist)", lambda: lock(fresh))

    print("platform: %s  python: %s" % (sys.platform, sys.version.split()[0]))
    print("_DIR_FD_OK: %s  O_NOFOLLOW: %s  data dir: %s"
          % (safe_fs._DIR_FD_OK, safe_fs._O_NOFOLLOW, base))
    for label, kind, detail in results:
        print("%-44s %-20s %s" % (label, kind, detail))


if __name__ == "__main__":
    main()
