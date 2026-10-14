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


class ConcurrentCreateTests(SafeFsCase):
    """Two writers creating the same level is the ordinary case, not a race to lose.

    The console's file panel uploads three files at once, so the first drop into
    a folder has all three running ``mkdir`` over the same levels. Every creation
    site is a check followed by a create, and on the ``dir_fd``-less path
    (Windows) there is no already-open descriptor to lean on, so the loser of
    that race used to get ``FileExistsError`` for a request that had asked for
    nothing unusual.
    """

    def test_a_level_created_by_a_peer_is_not_an_error(self):
        real_mkdir = os.mkdir

        def peer_wins(path, *args, **kwargs):
            # The other request created it in the window between this one's
            # existence check and its own attempt.
            real_mkdir(path, *args, **kwargs)
            raise FileExistsError(17, "File exists", str(path))

        os.mkdir = peer_wins
        try:
            safe_fs.mkdir(self.root, "drop/tree/g00/s00")
        finally:
            os.mkdir = real_mkdir
        self.assertTrue(os.path.isdir(
            os.path.join(self.root, "drop", "tree", "g00", "s00")))
        # ... and the level is usable: the file that raced for it lands.
        safe_fs.write_text_atomic(self.root, "drop/tree/g00/s00/f.txt", "x")
        self.assertEqual(
            safe_fs.read_text(self.root, "drop/tree/g00/s00/f.txt"), "x")

    def test_a_level_planted_as_a_link_in_the_gap_is_still_refused(self):
        real_mkdir = os.mkdir
        outside = self.write("elsewhere/target/keep.md", "secret")

        def peer_plants_a_link(path, *args, **kwargs):
            real_mkdir(path, *args, **kwargs)
            os.rmdir(path)
            os.symlink(os.path.dirname(outside), path)
            raise FileExistsError(17, "File exists", str(path))

        os.mkdir = peer_plants_a_link
        try:
            with self.assertRaises(UnsafePathError):
                safe_fs.mkdir(self.root, "drop/planted")
        finally:
            os.mkdir = real_mkdir

    def test_a_level_planted_as_a_file_in_the_gap_is_still_refused(self):
        real_mkdir = os.mkdir

        def peer_plants_a_file(path, *args, **kwargs):
            real_mkdir(path, *args, **kwargs)
            os.rmdir(path)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("not a directory")
            raise FileExistsError(17, "File exists", str(path))

        os.mkdir = peer_plants_a_file
        try:
            with self.assertRaises(UnsafePathError):
                safe_fs.mkdir(self.root, "drop/planted")
        finally:
            os.mkdir = real_mkdir


class LockFileTests(SafeFsCase):
    """``file_lock`` has to keep working on the *second* call, everywhere.

    The lock file used to be resolved through ``_resolve_chain`` on platforms
    without ``dir_fd`` -- and that helper requires every component to be a
    directory, so once ``.memory.lock`` existed the next lock was refused with
    "non-directory component '.memory.lock'". Callers that read
    ``UnsafePathError`` as "the directory was replaced"
    (``agent.memory.personal.scope_transaction``) turned that into a permanent
    403: the first memory access succeeded and every one after it claimed the
    memory directory had been swapped (Windows, reported 2026-10-13).
    """

    def test_the_lock_can_be_taken_again(self):
        for _ in range(3):
            with safe_fs.file_lock(self.root, ".memory.lock"):
                pass
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".memory.lock")))

    def test_the_path_fallback_survives_a_second_lock(self):
        """The same guarantee with ``dir_fd`` support forced off.

        Forcing the flag is what keeps this covered on POSIX CI, where the
        descriptor branch is what normally runs and the defect was invisible.
        """
        original = safe_fs._DIR_FD_OK
        safe_fs._DIR_FD_OK = False
        try:
            for _ in range(3):
                with safe_fs.file_lock(self.root, ".memory.lock"):
                    pass
        finally:
            safe_fs._DIR_FD_OK = original

    def test_the_lock_file_is_reused_not_replaced(self):
        """All processes must lock the same inode: never unlink, never replace."""
        path = os.path.join(self.root, ".memory.lock")
        with safe_fs.file_lock(self.root, ".memory.lock"):
            pass
        before = os.stat(path)
        with safe_fs.file_lock(self.root, ".memory.lock"):
            pass
        after = os.stat(path)
        self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))

    def test_a_missing_root_and_its_lock_are_created(self):
        root = os.path.join(self.root, "users", "1001")
        with safe_fs.file_lock(root, ".memory.lock"):
            pass
        self.assertTrue(os.path.isfile(os.path.join(root, ".memory.lock")))

    def test_a_symlinked_lock_path_is_refused_on_the_fallback_path(self):
        link = os.path.join(self.root, ".memory.lock")
        try:
            os.symlink(self.write("elsewhere/target.md", "secret"), link)
        except (OSError, NotImplementedError):  # pragma: no cover - platform
            self.skipTest("symlinks unavailable on this platform")
        original = safe_fs._DIR_FD_OK
        safe_fs._DIR_FD_OK = False
        try:
            with self.assertRaises(UnsafePathError):
                with safe_fs.file_lock(self.root, ".memory.lock"):
                    pass
        finally:
            safe_fs._DIR_FD_OK = original

    def test_a_symlinked_lock_path_is_refused_as_shipped(self):
        """Whatever the branch, a link at the lock path never becomes the lock."""
        link = os.path.join(self.root, ".memory.lock")
        target = self.write("elsewhere/target.md", "secret")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):  # pragma: no cover - platform
            self.skipTest("symlinks unavailable on this platform")
        with self.assertRaises((UnsafePathError, OSError)):
            with safe_fs.file_lock(self.root, ".memory.lock"):
                pass
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "secret", "the link target is untouched")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
