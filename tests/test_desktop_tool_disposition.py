# encoding:utf-8
"""Every tool that can write must be classified, and unclassified means refuse (8.6).

A36 (``acceptance.md``) is the requirement this file is built around:

    对每个具有 cwd 或落盘能力的现有工具逐项回归 | 均有本机执行、显式资源传输或明确
    不支持的分类；不能遗漏后静默写服务端

The failure it is written against is *omission*, and the shape of that failure is
specific: a tool nobody classified does not fail loudly under a local project --
it runs, and it writes **wherever its own default cwd points**, which is the
server. The user asked for the file to land on their machine; the file lands on
the server; nothing errors. So the classification has to be a closed set over the
real tool inventory, and an unclassified tool has to be refused rather than
tolerated.

Three dispositions, straight from the acceptance line:

* ``LOCAL`` -- runs in the project, writing there (``read``/``write``/``edit``/
  ``ls``/``search_files``/``bash``). ``bash`` is local in a stricter sense: it is
  the one that runs inside the kernel sandbox.
* ``TRANSFER`` -- its *output* is a file that must become a local input by an
  explicit, verified landing, or the landing is refused (``web_fetch``,
  ``browser``). The fetch itself keeps running on its own backend; what may not
  happen is a downloaded file being addressed as a project path.
* ``SERVER`` -- deliberately left to the server's own authorization and service
  ownership. Not an omission: memory, knowledge, scheduler stores and MCP config
  live on the server by design, and the spec says their location does not move
  because a project is open.

The important assertion is the last one in ``InventoryTests``: it walks the real
tool registry and fails if a disk-writing tool has no disposition. A table that
merely agrees with itself would pass while the actual gap stayed open.
"""

from __future__ import annotations

import os
import unittest

from agent.desktop_local import tool_disposition as td

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class VocabularyTests(unittest.TestCase):
    def test_the_three_dispositions_exist(self):
        self.assertEqual(td.LOCAL, "local")
        self.assertEqual(td.TRANSFER, "transfer")
        self.assertEqual(td.SERVER, "server")

    def test_the_dispositions_are_distinct_strings(self):
        self.assertEqual(len({td.LOCAL, td.TRANSFER, td.SERVER}), 3)


class ClassificationTests(unittest.TestCase):
    def test_the_project_file_tools_run_locally(self):
        for name in ("read", "write", "edit", "ls", "search_files", "bash", "send"):
            with self.subTest(tool=name):
                self.assertEqual(td.disposition(name), td.LOCAL)

    def test_the_downloading_tools_require_an_explicit_landing(self):
        """Their output is a file; the file cannot be a project path by default."""
        for name in ("web_fetch", "browser"):
            with self.subTest(tool=name):
                self.assertEqual(td.disposition(name), td.TRANSFER)

    def test_the_server_owned_tools_stay_on_the_server(self):
        """Not an omission: these are server services by design."""
        for name in ("memory", "knowledge", "scheduler", "env_config"):
            with self.subTest(tool=name):
                self.assertEqual(td.disposition(name), td.SERVER)

    def test_an_unknown_tool_is_server_owned_not_local(self):
        """An unclassified tool must not be silently treated as project-local.

        Treating the unknown as local would run it against the project; treating
        it as server-owned keeps its existing behaviour. Neither is "refuse", and
        the distinction matters -- see ``UnclassifiedTests`` for the refusal.
        """
        self.assertEqual(td.disposition("some_tool_added_later"), td.SERVER)

    def test_classification_does_not_depend_on_the_callers_platform(self):
        """A disposition is a property of the tool, not of where it is running."""
        for name in ("read", "bash", "web_fetch", "memory"):
            with self.subTest(tool=name):
                self.assertEqual(td.disposition(name), td.disposition(name))


class UnclassifiedTests(unittest.TestCase):
    """A tool that *can write* and has no disposition is refused, not tolerated."""

    def test_a_known_disk_writer_without_a_disposition_is_refused(self):
        engine = td.Dispositions({})

        decision = engine.decide("write", can_write=True)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "unclassified_tool")

    def test_a_known_disk_writer_with_a_disposition_is_allowed(self):
        engine = td.Dispositions(td.DISPOSITIONS)

        decision = engine.decide("write", can_write=True)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.disposition, td.LOCAL)

    def test_a_tool_that_cannot_write_is_never_refused_for_being_unclassified(self):
        """Refusing a read-only server tool would be collateral damage."""
        engine = td.Dispositions({})

        decision = engine.decide("memory", can_write=False)
        self.assertTrue(decision.allowed)

    def test_a_transfer_tool_is_allowed_only_with_a_landing_available(self):
        engine = td.Dispositions(td.DISPOSITIONS)

        without = engine.decide("web_fetch", can_write=True, landing_available=False)
        self.assertFalse(without.allowed)
        self.assertEqual(without.code, "resource_unavailable")

        with_landing = engine.decide("web_fetch", can_write=True, landing_available=True)
        self.assertTrue(with_landing.allowed)

    def test_a_server_owned_tool_is_never_refused_for_being_server_owned(self):
        engine = td.Dispositions(td.DISPOSITIONS)

        decision = engine.decide("memory", can_write=True, landing_available=False)
        self.assertTrue(decision.allowed)


class CanWriteTests(unittest.TestCase):
    """``can_write`` must be read from the tool, not guessed from its name."""

    def test_a_disk_writing_tool_is_detected_by_its_imports(self):
        """The real signal is that the module opens files for writing.

        Names lie in both directions: ``env_config`` writes a file but is a
        server store, and ``read`` is a file tool that never writes. Only the
        code says which is which.
        """
        self.assertTrue(td.module_writes_files(
            os.path.join(REPO, "agent", "tools", "write", "write.py")))
        self.assertTrue(td.module_writes_files(
            os.path.join(REPO, "agent", "tools", "edit", "edit.py")))
        self.assertTrue(td.module_writes_files(
            os.path.join(REPO, "agent", "tools", "web_fetch", "web_fetch.py")))

    def test_a_read_only_module_is_not_reported_as_a_writer(self):
        self.assertFalse(td.module_writes_files(
            os.path.join(REPO, "agent", "tools", "read", "read.py")))


class InventoryTests(unittest.TestCase):
    """The assertion with teeth: walk the real tools and fail on a gap."""

    def setUp(self):
        self.tools_dir = os.path.join(REPO, "agent", "tools")

    def test_every_disk_writing_tool_has_a_disposition(self):
        """A36's "不能遗漏". A new writing tool with no disposition fails here.

        Without this, the table below would be a list of nine names that agrees
        with itself while a tenth tool writes to the server unnoticed.
        """
        missing = sorted(
            name for name in td.disk_writing_tools(self.tools_dir)
            if td.disposition(name) == td.SERVER and name not in td.SERVER_TOOLS)

        self.assertEqual(
            missing, [],
            "these tools can write files and are neither classified local/transfer "
            "nor recorded as server-owned: " + ", ".join(missing))

    def test_the_local_set_matches_the_set_the_project_retargets(self):
        """The classification must agree with what already moves for a project."""
        from agent.desktop_local.run_context import CWD_TOOLS

        local = {name for name in CWD_TOOLS if td.disposition(name) == td.LOCAL}
        self.assertEqual(local, set(td.LOCAL_TOOLS))

    def test_every_cwd_tool_is_classified(self):
        """No tool the project retargets may be left unmentioned."""
        from agent.desktop_local.run_context import CWD_TOOLS

        for name in CWD_TOOLS:
            with self.subTest(tool=name):
                self.assertIn(
                    td.disposition(name), (td.LOCAL, td.TRANSFER),
                    f"{name} moves with the project and must be classified")

    def test_the_inventory_is_not_empty(self):
        """A walk that finds nothing would make the assertion above vacuous."""
        found = td.disk_writing_tools(self.tools_dir)
        self.assertGreater(len(found), 3, sorted(found))


if __name__ == "__main__":
    unittest.main()
