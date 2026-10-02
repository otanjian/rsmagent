# encoding:utf-8
"""``common.safe_fs``: anchored access refuses links and path tricks.

Tasks 5.6/6.1 depend on these guarantees, so they are pinned here once rather
than re-derived (and re-broken) per caller.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from common import safe_fs
from common.safe_fs import UnsafePathError


class SafeFsCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, text="body"):
        full = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)
        return full


class PathGrammarTests(SafeFsCase):
    def test_plain_relative_paths_split(self):
        self.assertEqual(safe_fs.split_relative("a/b.md"), ["a", "b.md"])

    def test_illegal_paths_are_refused_before_any_syscall(self):
        for bad in ["/etc/passwd", "..", "../x", "a/../b", "", "a//b",
                    "./a", "a/./b"]:
            with self.subTest(path=bad):
                with self.assertRaises(UnsafePathError):
                    safe_fs.split_relative(bad)

    def test_traversal_cannot_reach_outside(self):
        with self.assertRaises(UnsafePathError):
            safe_fs.read_text(self.root, "../outside.md")


class ReadTests(SafeFsCase):
    def test_reads_a_regular_file(self):
        self.write("memory/notes.md", "hi")
        self.assertEqual(safe_fs.read_text(self.root, "memory/notes.md"), "hi")

    def test_missing_file_is_none_not_an_error(self):
        self.assertIsNone(safe_fs.read_text(self.root, "memory/none.md"))
        self.assertFalse(safe_fs.is_file(self.root, "memory/none.md"))

    def test_lists_names_without_following_links(self):
        self.write("memory/a.md", "a")
        self.write("memory/b.md", "b")
        os.symlink(os.path.join(self.root, "memory", "a.md"),
                   os.path.join(self.root, "memory", "link.md"))
        names = safe_fs.list_names(self.root, "memory", suffix=".md")
        self.assertEqual(sorted(names), ["a.md", "b.md"])

    def test_stat_reports_a_symlink_as_a_symlink(self):
        target = self.write("outside.md", "secret")
        os.makedirs(os.path.join(self.root, "memory"), exist_ok=True)
        os.symlink(target, os.path.join(self.root, "memory", "entry.md"))
        self.assertTrue(safe_fs.is_symlink(self.root, "memory/entry.md"))
        self.assertFalse(safe_fs.is_file(self.root, "memory/entry.md"))


class SymlinkRefusalTests(SafeFsCase):
    def test_symlinked_entry_cannot_be_read(self):
        target = self.write("outside.md", "secret")
        os.makedirs(os.path.join(self.root, "memory"), exist_ok=True)
        os.symlink(target, os.path.join(self.root, "memory", "entry.md"))
        with self.assertRaises(UnsafePathError):
            safe_fs.read_text(self.root, "memory/entry.md")

    def test_symlinked_intermediate_directory_is_refused(self):
        real = self.write("real/entry.md", "secret")
        os.symlink(os.path.dirname(real), os.path.join(self.root, "memory"))
        with self.assertRaises(UnsafePathError):
            safe_fs.read_text(self.root, "memory/entry.md")
        with self.assertRaises(UnsafePathError):
            safe_fs.list_names(self.root, "memory")

    def test_symlinked_root_is_refused(self):
        self.write("real/entry.md", "secret")
        link = os.path.join(self.root, "rootlink")
        os.symlink(os.path.join(self.root, "real"), link)
        with self.assertRaises(UnsafePathError):
            safe_fs.read_text(link, "entry.md")

    def test_unlink_refuses_a_link_instead_of_removing_the_target(self):
        target = self.write("outside.md", "secret")
        os.makedirs(os.path.join(self.root, "memory"), exist_ok=True)
        os.symlink(target, os.path.join(self.root, "memory", "entry.md"))
        with self.assertRaises(UnsafePathError):
            safe_fs.unlink(self.root, "memory/entry.md")
        self.assertTrue(os.path.exists(target))

    def test_write_refuses_to_follow_a_link(self):
        target = self.write("outside.md", "secret")
        os.makedirs(os.path.join(self.root, "memory"), exist_ok=True)
        os.symlink(target, os.path.join(self.root, "memory", "entry.md"))
        with self.assertRaises(UnsafePathError):
            safe_fs.write_text_atomic(self.root, "memory/entry.md", "pwned")
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "secret")


class WriteTests(SafeFsCase):
    def test_write_and_read_round_trip(self):
        safe_fs.write_text_atomic(self.root, "memory/notes.md", "hello")
        self.assertEqual(safe_fs.read_text(self.root, "memory/notes.md"),
                         "hello")

    def test_write_replaces_existing_content_atomically(self):
        self.write("memory/notes.md", "old")
        safe_fs.write_text_atomic(self.root, "memory/notes.md", "new")
        self.assertEqual(safe_fs.read_text(self.root, "memory/notes.md"),
                         "new")
        leftovers = [n for n in os.listdir(os.path.join(self.root, "memory"))
                     if n.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_write_creates_the_parent_directory_inside_the_root(self):
        safe_fs.write_text_atomic(self.root, "absent/notes.md", "x")
        self.assertEqual(safe_fs.read_text(self.root, "absent/notes.md"), "x")

    def test_unlink_reports_absence(self):
        self.assertFalse(safe_fs.unlink(self.root, "memory/none.md"))
        self.write("memory/notes.md", "x")
        self.assertTrue(safe_fs.unlink(self.root, "memory/notes.md"))
        self.assertFalse(safe_fs.is_file(self.root, "memory/notes.md"))


class ReplacementRaceTests(SafeFsCase):
    """A swap between validation and use cannot retarget the operation."""

    @unittest.skipUnless(safe_fs._DIR_FD_OK, 'descriptor-anchored POSIX root access')
    def test_root_ancestor_link_cannot_redirect_owner_access(self):
        self.write('owner/MEMORY.md', 'OWN')
        os.symlink(os.path.join(self.root, 'owner'), os.path.join(self.root, 'alias'))
        with self.assertRaises(UnsafePathError):
            safe_fs.read_text(os.path.join(self.root, 'alias', 'nested'), 'MEMORY.md')

    def test_swapping_the_parent_to_a_link_does_not_read_the_target(self):
        self.write("memory/notes.md", "mine")
        away = self.write("elsewhere/notes.md", "theirs")
        memory = os.path.join(self.root, "memory")
        os.rename(memory, os.path.join(self.root, "memory-away"))
        os.symlink(os.path.dirname(away), memory)
        with self.assertRaises(UnsafePathError):
            safe_fs.read_text(self.root, "memory/notes.md")
        self.assertEqual(safe_fs.read_text(self.root, "memory-away/notes.md"),
                         "mine")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
