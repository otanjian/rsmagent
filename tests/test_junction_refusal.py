# encoding:utf-8
"""A junction must be refused exactly like a symlink.

Change ``harden-windows-desktop-reliability``, task 3.4. On Windows a directory
junction is a reparse point that redirects a path, but from Python 3.12
``os.path.islink`` -- and ``lstat``'s ``S_ISLNK`` -- report one as an ordinary
directory. Every anti-replacement guard in ``common/safe_fs`` and in the memory
services used to ask ``islink``, so on 3.12 a junction at a personal-memory root
was followed and another user's ``MEMORY.md`` was read
(docs/design/windows-desktop-verification-2026-10-13.md evidence; reproduced by
scripts/repro/probe-junction-memory-root.py).

The junction itself is created with ``mklink /J``, which needs no privilege --
unlike ``os.symlink``, which is unavailable in some CI sessions.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from common import safe_fs


def _make_junction(link: str, target: str) -> bool:
    if os.name != "nt":
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            return False
        return os.path.exists(link)
    result = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                            capture_output=True, text=True)
    return result.returncode == 0 and os.path.exists(link)


class JunctionRefusalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.target = os.path.join(self.base, "somewhere-else")
        os.makedirs(self.target)
        # No trailing newline: text mode would turn "\n" into "\r\n" on Windows
        # and the comparison below would be about the platform, not the guard.
        with open(os.path.join(self.target, "MEMORY.md"), "w", encoding="utf-8") as handle:
            handle.write("not mine")

    def tearDown(self):
        self._tmp.cleanup()

    def _junction(self, name):
        link = os.path.join(self.base, name)
        if not _make_junction(link, self.target):
            self.skipTest("this platform/session cannot create a redirecting directory")
        return link

    def test_is_link_like_answers_for_ordinary_paths_too(self):
        plain = os.path.join(self.base, "plain")
        os.makedirs(plain)
        self.assertFalse(safe_fs.is_link_like(plain))
        self.assertFalse(safe_fs.is_link_like(os.path.join(self.base, "missing")))

    def test_is_link_like_follows_the_junction_api(self):
        """The junction branch is exercised even where only symlinks exist."""
        if getattr(os.path, "isjunction", None) is None:
            self.skipTest("this interpreter has no os.path.isjunction")
        plain = os.path.join(self.base, "plain")
        os.makedirs(plain)
        with mock.patch.object(os.path, "isjunction", lambda path: path == plain):
            self.assertTrue(safe_fs.is_link_like(plain))

    def test_a_junction_root_is_refused(self):
        root = self._junction("root")
        self.assertTrue(safe_fs.is_link_like(root))
        with self.assertRaises(safe_fs.UnsafePathError):
            safe_fs.read_text(root, "MEMORY.md")
        with self.assertRaises(safe_fs.UnsafePathError):
            safe_fs.write_text_atomic(root, "MEMORY.md", "mine")

    def test_a_junction_component_inside_the_root_is_refused(self):
        root = os.path.join(self.base, "real-root")
        os.makedirs(root)
        link = os.path.join(root, "memory")
        if not _make_junction(link, self.target):
            self.skipTest("this platform/session cannot create a redirecting directory")
        with self.assertRaises(safe_fs.UnsafePathError):
            safe_fs.list_names(root, "memory")
        with self.assertRaises(safe_fs.UnsafePathError):
            safe_fs.read_text(root, "memory/MEMORY.md")
        self.assertTrue(safe_fs.is_symlink(root, "memory"),
                        "is_symlink has to answer for junctions as well")

    def test_the_target_itself_is_still_readable(self):
        """Refusing the redirect must not refuse the real directory."""
        self.assertEqual(safe_fs.read_text(self.target, "MEMORY.md"), "not mine")

    @unittest.skipUnless(os.name == "nt", "the memory lock is a Windows byte-range lock")
    def test_scope_transaction_refuses_a_junction_root(self):
        from agent.memory.personal import PersonalMemoryError, scope_transaction
        root = self._junction("service-root")
        with self.assertRaises(PersonalMemoryError) as raised:
            with scope_transaction(root):
                pass
        self.assertEqual(raised.exception.code, "unsafe_path")
        self.assertEqual(raised.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
