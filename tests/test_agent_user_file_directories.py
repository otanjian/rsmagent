# encoding:utf-8
"""Agent user file directories: layout helpers and the subtree classifier.

Change ``isolate-shared-agent-user-data`` (task 2.1/2.2). The layout is one
directory per verified user under the Agent's workspace — ``user/<user_id>/``
with ``uploads``/``outputs``/``work`` beneath it — and one pure classifier that
turns any real path into "container", "owned by uid", "unowned" or "outside".
Everything the file surface enforces is built on that classifier, so it is
tested on its own before any handler is involved.
"""

from __future__ import annotations

import os
import unittest

from common.runtime_identity import RuntimeIdentity
from common.state_dir import (
    StateDirError,
    agent_user_outputs_dir,
    agent_user_root,
    agent_user_uploads_dir,
    agent_user_work_dir,
    classify_agent_user_path,
)

USER = "usr_AbC123"


class AgentUserDirectoryTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.ws = tempfile.mkdtemp(prefix="cow-user-dir-")
        self.addCleanup(__import__("shutil").rmtree, self.ws, True)
        self.ident = RuntimeIdentity(agent_id="a1", user_id=USER,
                                     tenant_id="acme")

    def test_root_is_under_workspace_user_container(self):
        root = agent_user_root(self.ident, base=self.ws)
        self.assertEqual(str(root), os.path.join(self.ws, "user", USER))
        # Resolution alone must not create anything (preflight friendliness).
        self.assertFalse(os.path.exists(root))

    def test_subdirs_nest_under_the_user_root(self):
        uploads = agent_user_uploads_dir(self.ident, ensure=True, base=self.ws)
        outputs = agent_user_outputs_dir(self.ident, ensure=True, base=self.ws)
        work = agent_user_work_dir(self.ident, ensure=True, base=self.ws)
        self.assertEqual(os.path.basename(uploads), "uploads")
        self.assertEqual(os.path.basename(outputs), "outputs")
        self.assertEqual(os.path.basename(work), "work")
        for path in (uploads, outputs, work):
            self.assertEqual(os.path.dirname(str(path)),
                             os.path.join(self.ws, "user", USER))
            self.assertTrue(os.path.isdir(path))

    def test_no_verified_user_has_no_user_directory(self):
        anon = RuntimeIdentity(agent_id="a1")
        self.assertIsNone(agent_user_root(anon, base=self.ws))
        self.assertIsNone(agent_user_uploads_dir(anon, base=self.ws))
        # Nothing is created for an identity with no user dimension.
        self.assertFalse(os.path.exists(os.path.join(self.ws, "user")))

    def test_unsafe_user_id_is_refused(self):
        for bad in ("../escape", "a/b", "with space", ".", "x" * 129):
            ident = RuntimeIdentity(agent_id="a1", user_id=bad)
            with self.assertRaises(StateDirError):
                agent_user_root(ident, base=self.ws)

    def test_empty_user_id_is_absent_not_invalid(self):
        ident = RuntimeIdentity(agent_id="a1", user_id="")
        self.assertIsNone(agent_user_root(ident, base=self.ws))

    def test_user_container_must_not_be_a_symlink_or_file(self):
        container = os.path.join(self.ws, "user")
        os.symlink(os.path.join(self.ws, "elsewhere"), container)
        with self.assertRaises(StateDirError):
            agent_user_root(self.ident, base=self.ws)

        os.remove(container)
        with open(container, "w", encoding="utf-8") as handle:
            handle.write("not a directory")
        with self.assertRaises(StateDirError):
            agent_user_root(self.ident, base=self.ws)

    def test_user_subdirectory_must_not_be_a_symlink(self):
        container = os.path.join(self.ws, "user")
        os.makedirs(container)
        os.symlink(os.path.join(self.ws, "elsewhere"),
                   os.path.join(container, USER))
        with self.assertRaises(StateDirError):
            agent_user_root(self.ident, base=self.ws)


class ClassifyAgentUserPathTests(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile

        self.ws = tempfile.mkdtemp(prefix="cow-user-classify-")
        self.addCleanup(shutil.rmtree, self.ws, True)
        self.user_file = os.path.join(self.ws, "user", USER, "uploads", "a.txt")
        os.makedirs(os.path.dirname(self.user_file))

    def test_path_inside_a_user_subtree_names_its_owner(self):
        self.assertEqual(classify_agent_user_path(self.user_file, self.ws),
                         ("user", USER))
        self.assertEqual(
            classify_agent_user_path(os.path.join(self.ws, "user", USER), self.ws),
            ("user", USER))

    def test_container_itself_is_recognised(self):
        self.assertEqual(
            classify_agent_user_path(os.path.join(self.ws, "user"), self.ws),
            ("container", None))

    def test_ordinary_workspace_path_is_not_in_the_container(self):
        ordinary = os.path.join(self.ws, "reports", "q1.csv")
        os.makedirs(os.path.dirname(ordinary))
        self.assertEqual(classify_agent_user_path(ordinary, self.ws),
                         ("none", None))
        # A sibling whose name merely starts with "user" is not the container.
        sibling = os.path.join(self.ws, "userizer", "x")
        os.makedirs(os.path.dirname(sibling))
        self.assertEqual(classify_agent_user_path(sibling, self.ws),
                         ("none", None))
        # The container prefix must be a whole path segment.
        self.assertEqual(classify_agent_user_path(self.ws, self.ws),
                         ("none", None))

    def test_malformed_owner_inside_container_is_unowned(self):
        stray = os.path.join(self.ws, "user", "not a user", "x.txt")
        os.makedirs(os.path.dirname(stray))
        self.assertEqual(classify_agent_user_path(stray, self.ws),
                         ("unowned", None))

    def test_symlink_alias_resolves_to_the_real_owner(self):
        alias = os.path.join(self.ws, "alias")
        os.symlink(os.path.join(self.ws, "user", USER), alias)
        self.assertEqual(
            classify_agent_user_path(os.path.join(alias, "uploads", "a.txt"),
                                     self.ws),
            ("user", USER))

    def test_path_outside_the_workspace_is_not_matched(self):
        import tempfile

        outside = tempfile.mkdtemp(prefix="cow-outside-")
        self.addCleanup(__import__("shutil").rmtree, outside, True)
        self.assertEqual(classify_agent_user_path(outside, self.ws),
                         ("none", None))


if __name__ == "__main__":
    unittest.main()
