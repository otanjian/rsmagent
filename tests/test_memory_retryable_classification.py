# encoding:utf-8
"""A held lock is retryable; a replaced path is a refusal.

Change ``harden-windows-desktop-reliability``, task 3.3. Both conditions used to
arrive at the console as ``unsafe_path``/403 -- "个人记忆目录被替换" -- because the
lock's own failure (``msvcrt.locking`` gives up after ~10s on Windows) was raised
as a plain ``OSError`` or, where callers caught widely, folded into the path
refusal. Telling a user their memory directory was swapped when another process
merely held the lock sends them to re-login, which cannot help either.

These are the two halves of that distinction: the service layer must label them
differently, and the *classification* must come from ``safe_fs`` rather than from
a caller guessing.
"""

import os
import tempfile
import unittest
from unittest import mock

from common import safe_fs
from agent.memory.personal import PersonalMemoryError, scope_transaction


class ServiceClassificationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "users", "1001")

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_held_lock_is_retryable(self):
        with mock.patch.object(safe_fs, "file_lock", side_effect=safe_fs.LockBusyError("held")):
            with self.assertRaises(PersonalMemoryError) as raised:
                with scope_transaction(self.root):
                    pass
        self.assertEqual(raised.exception.code, "busy")
        self.assertEqual(raised.exception.status, 409)
        self.assertIsInstance(raised.exception.__cause__, safe_fs.LockBusyError)

    def test_a_replaced_path_is_still_a_refusal(self):
        with mock.patch.object(safe_fs, "file_lock", side_effect=safe_fs.UnsafePathError("replaced")):
            with self.assertRaises(PersonalMemoryError) as raised:
                with scope_transaction(self.root):
                    pass
        self.assertEqual(raised.exception.code, "unsafe_path")
        self.assertEqual(raised.exception.status, 403)

    def test_the_two_codes_are_not_the_same_code(self):
        """The distinction is the whole point; one code for both is the defect."""
        self.assertNotEqual(safe_fs.LockBusyError, safe_fs.UnsafePathError)
        self.assertFalse(issubclass(safe_fs.LockBusyError, safe_fs.UnsafePathError))
        self.assertFalse(issubclass(safe_fs.UnsafePathError, safe_fs.LockBusyError))

    @unittest.skipUnless(os.name == "nt", "the byte-range lock is Windows-only")
    def test_windows_lock_contention_becomes_a_lock_busy_error(self):
        import msvcrt

        def contending(*_args, **_kwargs):
            raise OSError(36, "Resource deadlock avoided")

        with mock.patch.object(msvcrt, "locking", contending):
            with self.assertRaises(safe_fs.LockBusyError):
                with safe_fs.file_lock(self.root, ".memory.lock"):
                    pass

    def test_a_free_lock_is_not_reported_as_busy(self):
        with scope_transaction(self.root):
            pass
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".memory.lock")))


if __name__ == "__main__":
    unittest.main()
