# encoding:utf-8
"""Server attachments and knowledge/MCP results as local inputs (task 8.6).

This file is the *seam* half of 8.6: :mod:`test_desktop_resource_landing` pins the
verification, and this pins that the staging path actually uses it and refuses when
it cannot.

The requirement it implements:

    接通获权服务器附件及远程知识/MCP 结果的输入准备与摘要校验；逐个落盘工具明确本机
    适配、显式传输或拒绝，不静默回退服务端。

Four properties, and the fourth is the one that needs stating explicitly:

1. **A pinned resource lands and is used.** ``resource:<id>@<v>#<d>`` becomes a real
   local path in the run's input directory.
2. **A resource that fails verification never reaches the tool.** The call is
   refused whole -- the argument is not dropped, because running a tool with its
   input quietly missing looks like success.
3. **No landing transport means a refusal, not a fallback.** Never the server path,
   never a same-named project file.
4. **Staging is not a second authorization.** The landing's fetcher decides what is
   readable; this seam must not become a weaker copy of that decision, and a
   refused landing must not be turned into a rewritten path by any later branch.

Point 3 is where the "不静默回退服务端" of the requirement actually lives, and the
``backend:`` case is its sharpest form: a server path is refused even when it looks
like it could be a project entry.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest

from agent.desktop_local.resource_landing import (
    FetchedResource, ResourceLanding, digest_of,
)
from agent.desktop_local.source_resolver import Source, prepare_tool_inputs


class StageCase(unittest.TestCase):
    def setUp(self):
        self._project = tempfile.TemporaryDirectory(prefix="proj-")
        self._inputs = tempfile.TemporaryDirectory(prefix="inputs-")
        self.addCleanup(self._project.cleanup)
        self.addCleanup(self._inputs.cleanup)
        self.project = self._project.name
        self.inputs = self._inputs.name

    def desktop(self):
        return Source(kind="desktop", root=self.project)

    def landing(self, data: bytes = b"attachment", *, version="v1", digest=""):
        self.fetched = data
        fetcher = lambda _rid: FetchedResource(  # noqa: E731
            version=version, digest=digest or digest_of(data), data=data)
        return ResourceLanding(fetcher, self.inputs)


class ResourceStagingTests(StageCase):
    def test_a_pinned_resource_becomes_a_real_local_path(self):
        staged = prepare_tool_inputs(
            self.desktop(), "read", {"path": "resource:att_1@v1"},
            landing=self.landing())

        self.assertTrue(staged.ok, staged.message)
        landed = staged.arguments["path"]
        self.assertTrue(os.path.isabs(landed))
        self.assertTrue(os.path.isfile(landed))
        with open(landed, "rb") as handle:
            self.assertEqual(handle.read(), b"attachment")

    def test_the_landed_path_stays_inside_the_runs_input_directory(self):
        staged = prepare_tool_inputs(
            self.desktop(), "read", {"path": "resource:att_1"},
            landing=self.landing())

        landed = staged.arguments["path"]
        self.assertTrue(
            os.path.realpath(landed).startswith(os.path.realpath(self.inputs)),
            "a landing must not write outside the run's input directory")

    def test_the_rewrite_is_recorded_so_a_caller_can_report_it(self):
        staged = prepare_tool_inputs(
            self.desktop(), "read", {"path": "resource:att_1"},
            landing=self.landing())

        self.assertEqual(len(staged.rewrites), 1)
        self.assertEqual(staged.rewrites[0][0], "resource:att_1")

    def test_a_digest_mismatch_refuses_the_whole_call(self):
        staged = prepare_tool_inputs(
            self.desktop(), "read",
            {"path": f"resource:att_1#{digest_of(b'other')}"},
            landing=self.landing(b"attachment"))

        self.assertFalse(staged.ok)
        self.assertIn("digest", staged.message())

    def test_a_refused_landing_leaves_the_argument_unrewritten(self):
        """The original must not survive as a usable local path."""
        raw = f"resource:att_1#{digest_of(b'other')}"
        staged = prepare_tool_inputs(
            self.desktop(), "read", {"path": raw},
            landing=self.landing(b"attachment"))

        self.assertFalse(staged.ok)
        self.assertEqual(staged.arguments["path"], raw,
                         "a refused landing must not rewrite the argument into a path")
        self.assertEqual(staged.rewrites, [])

    def test_a_version_drift_refuses_the_call(self):
        staged = prepare_tool_inputs(
            self.desktop(), "read", {"path": "resource:att_1@v1"},
            landing=self.landing(version="v2"))

        self.assertFalse(staged.ok)

    def test_a_resource_without_a_landing_transport_is_refused_by_name(self):
        """No fallback: not the server path, not a same-named project file."""
        staged = prepare_tool_inputs(
            self.desktop(), "read", {"path": "resource:att_1"})

        self.assertFalse(staged.ok)
        self.assertIn("resource:att_1", staged.message())

    def test_a_resource_is_never_read_as_a_project_directory(self):
        """The ``skill:`` lesson: a typed ref is not a relative path.

        A project directory literally called ``resource:att_1`` must not satisfy
        the reference -- that would turn "not landed" into a silent wrong read.
        """
        decoy = os.path.join(self.project, "resource:att_1")
        os.makedirs(decoy, exist_ok=True)
        with open(os.path.join(decoy, "x"), "w", encoding="utf-8") as handle:
            handle.write("decoy")

        staged = prepare_tool_inputs(
            self.desktop(), "read", {"path": "resource:att_1"})

        self.assertFalse(staged.ok)
        self.assertEqual(staged.arguments["path"], "resource:att_1")

    def test_a_nested_resource_id_lands_nested(self):
        staged = prepare_tool_inputs(
            self.desktop(), "read", {"path": "resource:reports/q1.xlsx"},
            landing=self.landing())

        self.assertTrue(staged.ok, staged.message)
        self.assertTrue(staged.arguments["path"].endswith(os.path.join("reports", "q1.xlsx")))

    def test_an_escaping_resource_id_is_refused(self):
        staged = prepare_tool_inputs(
            self.desktop(), "read", {"path": "resource:../../etc/passwd"},
            landing=self.landing())

        self.assertFalse(staged.ok)

    def test_two_calls_for_the_same_resource_land_it_once(self):
        calls = []
        fetcher = lambda rid: (calls.append(rid), FetchedResource(  # noqa: E731
            version="v1", digest=digest_of(b"a"), data=b"a"))[1]
        engine = ResourceLanding(fetcher, self.inputs)

        first = prepare_tool_inputs(self.desktop(), "read",
                                    {"path": "resource:att_1"}, landing=engine)
        second = prepare_tool_inputs(self.desktop(), "read",
                                     {"path": "resource:att_1"}, landing=engine)

        self.assertTrue(first.ok and second.ok)
        self.assertEqual(calls, ["att_1"], "the second call must reuse the landing")


class NoSilentFallbackTests(StageCase):
    """The "不静默回退服务端" half, stated as its own cases."""

    def test_a_backend_reference_is_refused_even_with_a_landing_available(self):
        """``backend:`` is a server *path*, not a landable resource.

        A resource has an identity the server can pin and verify; a bare path does
        not, so offering a landing must not turn a path into something landable.
        """
        staged = prepare_tool_inputs(
            self.desktop(), "read", {"path": "backend:/srv/report.xlsx"},
            landing=self.landing())

        self.assertFalse(staged.ok)
        # Named, so the report says which path was refused.
        self.assertIn("/srv/report.xlsx", staged.message())

    def test_an_unlanded_absolute_path_is_still_refused(self):
        staged = prepare_tool_inputs(
            self.desktop(), "read", {"path": "/srv/report.xlsx"},
            landing=self.landing())

        self.assertFalse(staged.ok)

    def test_a_server_absolute_path_that_matches_a_project_name_is_not_substituted(self):
        """The failure this prevents: reading a same-named project file instead."""
        same_name = os.path.join(self.project, "report.xlsx")
        with open(same_name, "w", encoding="utf-8") as handle:
            handle.write("a different file")

        staged = prepare_tool_inputs(
            self.desktop(), "read", {"path": "/srv/report.xlsx"},
            landing=self.landing())

        self.assertFalse(staged.ok)

    def test_the_input_directory_is_never_treated_as_the_project(self):
        """A landed path is usable, but must not become the tool's project."""
        staged = prepare_tool_inputs(
            self.desktop(), "write", {"path": "resource:att_1"},
            landing=self.landing())

        self.assertTrue(staged.ok, staged.message)
        # It landed where it landed; the project was not written to.
        self.assertEqual(os.listdir(self.project), [])

    def test_a_server_session_keeps_its_existing_behaviour(self):
        """A non-desktop run is untouched by all of the above."""
        staged = prepare_tool_inputs(
            Source(kind="server"), "read", {"path": "reports/q1.xlsx"})

        self.assertTrue(staged.ok)


if __name__ == "__main__":
    unittest.main()
