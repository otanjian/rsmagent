# encoding:utf-8
"""The A36 gate, the server run-input source, and the run-scoped landing root (8.6).

Three things this file pins, each the part of 8.6 that needs more than "the
mechanism exists":

* **The A36 gate has teeth.** ``unclassified_writer_refusal`` must refuse a tool
  that writes files and has no disposition, and must *not* refuse the classified
  ones. A gate that never fires is decoration, so the case that makes it fire is
  built from a real temporary tool directory rather than by mutating the real
  table.

* **The source is authorized and its digest is real.** ``RunInputFetcher`` must
  scope the lookup to the caller (another user's input is "not found"), and must
  compute the digest from the bytes on disk -- reading a *stored* digest and
  comparing it to itself would verify nothing, which is the whole point of 8.6's
  "摘要校验".

* **The landing root is per run and created only on demand.** Resolving where a
  landing would go must not leave a directory in the user's project; only an
  actual landing may.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from unittest import mock

from agent.desktop_local import tool_disposition as td


class GateTests(unittest.TestCase):
    def test_a_classified_writer_is_allowed(self):
        for name in ("write", "edit", "bash", "web_fetch", "env_config"):
            with self.subTest(tool=name):
                self.assertEqual(td.unclassified_writer_refusal(name), "")

    def test_a_tool_that_writes_nothing_is_allowed_whatever_its_name(self):
        self.assertEqual(td.unclassified_writer_refusal("read"), "")
        self.assertEqual(td.unclassified_writer_refusal("totally_new_tool"), "")

    def test_an_unclassified_writer_is_refused(self):
        """Found by the inventory, absent from the table -> refused, by name."""
        from unittest.mock import patch

        with patch.object(td, "inventory",
                          return_value=frozenset({"new_writer"})):
            message = td.unclassified_writer_refusal("new_writer")

        self.assertIn("new_writer", message)
        self.assertIn("没有执行", message)

    def test_the_inventory_really_finds_a_writing_module(self):
        """The signal the gate reads is a real write in a real module.

        Built from a temporary tool directory, so this pins the *scan* rather than
        a mock of it -- the "a tenth tool appeared" case has to be findable before
        it can be refused.
        """
        with tempfile.TemporaryDirectory(prefix="tools-") as tools_dir:
            module_dir = os.path.join(tools_dir, "new_writer")
            os.makedirs(module_dir)
            with open(os.path.join(module_dir, "new_writer.py"), "w",
                      encoding="utf-8") as handle:
                handle.write("def run(path):\n"
                             "    with open(path, 'w') as f:\n"
                             "        f.write('x')\n")

            self.assertIn("new_writer", td.inventory(tools_dir))
            # The real module-level inventory is unchanged by the probe.
            self.assertNotIn("new_writer", td.inventory())

    def test_the_refusal_names_the_tool_and_says_nothing_ran(self):
        """The message is what the model sees, so it has to be actionable."""
        from unittest.mock import patch

        with patch.object(td, "inventory", return_value=frozenset({"write"})):
            original = dict(td.DISPOSITIONS)
            try:
                td.DISPOSITIONS.pop("write", None)
                message = td.unclassified_writer_refusal("write")
            finally:
                td.DISPOSITIONS.clear()
                td.DISPOSITIONS.update(original)

        self.assertIn("write", message)
        self.assertIn("没有执行", message)

    def test_the_gate_survives_its_table_being_restored(self):
        """The test above mutates a module global; make sure it is whole again."""
        self.assertEqual(td.unclassified_writer_refusal("write"), "")


class RunInputFetcherTests(unittest.TestCase):
    """The source behind a ``resource:`` reference."""

    def setUp(self):
        from agent.desktop_local import run_inputs

        self.mod = run_inputs
        self._tmp = tempfile.TemporaryDirectory(prefix="runin-")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def _store(self, rows):
        class _Store:
            def execute(self, sql, params=()):
                return [r for r in rows
                        if not params or r.get("id") == params[0]]
        return _Store()

    def _fetcher(self, rows, *, identity=None, data=b"payload", filename="a.txt",
                 work_root=None):
        """A fetcher with its store and its base injected, so the seam is testable.

        The store injection is deliberate: production resolves it through the web
        auth layer, but a test of *what counts as an authorized input* should not
        need an identity store, a transfer ledger and a staging root.

        ``work_root`` stands in for the Agent's user work dir -- the base
        ``artifact_rel`` is relative to (see the module docstring).
        """
        with open(os.path.join(self.root, filename), "wb") as handle:
            handle.write(data)

        root = self.root

        class _Publisher:
            _svc = type("S", (), {"_store": self._store(rows)})()

        return self.mod.RunInputFetcher(
            identity=identity, publisher=_Publisher(),
            work_root=work_root if work_root is not None else root)

    def _identity(self, user="u1", tenant="t1"):
        return type("I", (), {"user_id": user, "tenant_id": tenant})()

    def _row(self, **overrides):
        row = {"id": "rin_1", "tenant_id": "t1", "user_id": "u1",
               "agent_id": "ag1", "run_id": "run1",
               "artifact_rel": "a.txt", "source_version": "v3",
               "deleted_at": None, "retained_until": 0}
        row.update(overrides)
        return row

    def test_an_owned_input_is_described_with_its_version(self):
        engine = self._fetcher([self._row()], identity=self._identity())
        ref = engine.describe("rin_1")

        self.assertIsNotNone(ref)
        self.assertEqual(ref.resource_id, "rin_1")
        self.assertEqual(ref.version, "v3")
        self.assertEqual(ref.artifact_rel, "a.txt")

    def test_another_users_input_is_not_found(self):
        """Reports absence, not existence: the other user's attachment is private."""
        engine = self._fetcher([self._row(user_id="someone_else")],
                               identity=self._identity())
        self.assertIsNone(engine.describe("rin_1"))

    def test_another_tenants_input_is_not_found(self):
        engine = self._fetcher([self._row(tenant_id="t2")],
                               identity=self._identity())
        self.assertIsNone(engine.describe("rin_1"))

    def test_a_deleted_input_is_not_found(self):
        engine = self._fetcher([self._row(deleted_at=123)],
                               identity=self._identity())
        self.assertIsNone(engine.describe("rin_1"))

    def test_an_expired_input_is_not_found(self):
        engine = self._fetcher([self._row(retained_until=1)],
                               identity=self._identity())
        self.assertIsNone(engine.describe("rin_1"))

    def test_an_anonymous_caller_gets_nothing(self):
        engine = self._fetcher([self._row()], identity=None)
        engine._identity = type("I", (), {"user_id": "", "tenant_id": ""})()
        self.assertIsNone(engine.describe("rin_1"))

    def test_the_fetched_digest_is_of_the_bytes_on_disk(self):
        """Not of a stored column: a stored digest would verify nothing.

        This is what makes the landing's check mean something -- a file replaced on
        disk after the run input was recorded is caught here.
        """
        data = b"the real bytes"
        engine = self._fetcher([self._row()], identity=self._identity(), data=data)

        fetched = engine("rin_1")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.data, data)
        self.assertEqual(fetched.digest, hashlib.sha256(data).hexdigest())
        self.assertEqual(fetched.version, "v3")

    def test_a_missing_artifact_file_yields_nothing_rather_than_an_exception(self):
        engine = self._fetcher([self._row(artifact_rel="gone.txt")],
                               identity=self._identity())
        self.assertIsNone(engine("rin_1"))

    def test_an_unknown_resource_id_yields_nothing(self):
        engine = self._fetcher([self._row()], identity=self._identity())
        self.assertIsNone(engine("rin_missing"))

    def test_the_base_is_the_work_dir_not_the_publish_staging_root(self):
        """``artifact_rel`` resolves against the Agent user work dir.

        The same column name means something else on ``desktop_publish_ledger``
        (staging-root-relative). Resolving this table's column through the publish
        service looks in the wrong directory, so the work dir has to be the base.
        """
        work = os.path.join(self.root, "work")
        os.makedirs(work)
        with open(os.path.join(work, "a.txt"), "wb") as handle:
            handle.write(b"payload")

        engine = self._fetcher([self._row()], identity=self._identity(),
                               work_root=work)
        fetched = engine.pull("rin_1")
        self.assertIsNotNone(fetched, "the artifact under the work dir was not found")
        self.assertEqual(fetched[1], b"payload")

    def test_a_run_input_that_escapes_the_work_dir_is_refused(self):
        """A stored ``../`` must not read outside the user's work dir."""
        work = os.path.join(self.root, "work")
        os.makedirs(work)
        with open(os.path.join(self.root, "outside.txt"), "wb") as handle:
            handle.write(b"secret")

        engine = self._fetcher([self._row(artifact_rel="../outside.txt")],
                               identity=self._identity(), work_root=work)
        self.assertIsNone(engine.pull("rin_1"))

    def test_no_user_work_dir_means_the_input_is_unresolvable(self):
        """With no user to resolve a work dir for, refuse rather than guess."""
        engine = self.mod.RunInputFetcher(
            identity=self._identity(),
            publisher=type("P", (), {"_svc": type(
                "S", (), {"_store": self._store([self._row()])})()})(),
            work_root=None)
        with mock.patch("common.state_dir.agent_user_work_dir",
                        return_value=None):
            self.assertIsNone(engine.pull("rin_1"))

    def test_production_resolution_uses_the_work_dir_base(self):
        """The real (un-injected) base is the work dir, not the staging root.

        The other tests inject ``work_root``; this one leaves it unset so the
        production resolution actually runs, and places a same-named decoy under
        the staging root to prove which base was used.
        """
        work = os.path.join(self.root, "work")
        os.makedirs(work)
        with open(os.path.join(work, "a.txt"), "wb") as handle:
            handle.write(b"payload")

        staging = os.path.join(self.root, "staging")
        os.makedirs(staging)
        with open(os.path.join(staging, "a.txt"), "wb") as handle:
            handle.write(b"decoy")

        engine = self.mod.RunInputFetcher(
            identity=self._identity(),
            publisher=type("P", (), {
                "_svc": type("S", (), {"_store": self._store([self._row()])})(),
                "_root": lambda self: staging,
            })())
        with mock.patch("common.state_dir.agent_user_work_dir",
                        return_value=work):
            pulled = engine.pull("rin_1")
        self.assertIsNotNone(pulled)
        self.assertEqual(pulled[1], b"payload",
                         "the artifact was resolved against the wrong base")


class RunLandingTests(unittest.TestCase):
    """The landing root is per run, inside the authorized project, made on demand."""

    def setUp(self):
        from agent.desktop_local import run_inputs

        self.mod = run_inputs
        self._tmp = tempfile.TemporaryDirectory(prefix="project-")
        self.addCleanup(self._tmp.cleanup)
        self.project = self._tmp.name

    def _landing(self, root=""):
        return self.mod.RunLanding(
            identity=None, root=root,
            fetch=lambda _rid: __import__(
                "agent.desktop_local.resource_landing", fromlist=["FetchedResource"]
            ).FetchedResource(version="v1", data=b"x",
                              digest=hashlib.sha256(b"x").hexdigest()))

    def test_landing_writes_into_the_given_directory(self):
        root = os.path.join(self.project, "inputs")
        os.makedirs(root)
        engine = self._landing(root=root)

        result = engine.land(
            __import__("agent.desktop_local.resource_landing",
                       fromlist=["LandingRequest"]).LandingRequest("att_1"))

        self.assertTrue(result.ok, result.message)
        self.assertTrue(result.absolute.startswith(os.path.realpath(root)))

    def test_a_landing_with_no_directory_refuses_rather_than_guessing(self):
        """No identity and no explicit root means no run input directory.

        Refusing is the point: guessing a directory would put an authorized
        attachment somewhere nobody granted.
        """
        engine = self.mod.RunLanding(identity=None, root="",
                                     fetch=lambda _rid: None)
        engine._identity = type("I", (), {})()
        result = engine.land(
            __import__("agent.desktop_local.resource_landing",
                       fromlist=["LandingRequest"]).LandingRequest("att_1"))

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "resource_unavailable")


class RunInputDirTests(unittest.TestCase):
    """Where a landing would go: per run, and without side effects."""

    def setUp(self):
        from agent.desktop_local import run_context

        self.rc = run_context
        self._tmp = tempfile.TemporaryDirectory(prefix="proj-")
        self.addCleanup(self._tmp.cleanup)
        self.project = self._tmp.name

    def _identity(self, run_id="run1", session_id="sess1", target=None):
        return type("I", (), {
            "run_id": run_id, "session_id": session_id,
            "user_id": "u1", "tenant_id": "t1",
            "execution_target": target, "execution_cwd": self.project,
        })()

    def _desktop(self):
        target = type("T", (), {
            "is_desktop": True, "device_id": "dev1", "workspace_id": "ws1",
            "binding_id": "b1", "grant_version": 1, "project_mode": "project",
        })()
        return target

    def test_the_directory_is_run_scoped_and_inside_the_project(self):
        from unittest import mock

        identity = self._identity()
        with mock.patch.object(self.rc, "run_local_cwd",
                               return_value=(self.project, None)):
            directory, refusal = self.rc.run_input_dir(identity)

        self.assertIsNone(refusal)
        self.assertTrue(
            os.path.realpath(directory).startswith(os.path.realpath(self.project)))
        self.assertIn("run1", directory)

    def test_resolving_the_directory_does_not_create_it(self):
        """Asking where a landing would go must not litter the project."""
        from unittest import mock

        with mock.patch.object(self.rc, "run_local_cwd",
                               return_value=(self.project, None)):
            self.rc.run_input_dir(self._identity())

        self.assertEqual(os.listdir(self.project), [],
                         "resolving the path must have no side effect")

    def test_ensuring_the_directory_creates_it_once(self):
        from unittest import mock

        with mock.patch.object(self.rc, "run_local_cwd",
                               return_value=(self.project, None)):
            directory, refusal = self.rc.ensure_run_input_dir(self._identity())
            again, _ = self.rc.ensure_run_input_dir(self._identity())

        self.assertIsNone(refusal)
        self.assertTrue(os.path.isdir(directory))
        self.assertEqual(directory, again)

    def test_a_run_without_an_identifier_is_refused(self):
        """A shared directory for unrelated runs would mix their inputs."""
        from unittest import mock

        with mock.patch.object(self.rc, "run_local_cwd",
                               return_value=(self.project, None)):
            directory, refusal = self.rc.run_input_dir(
                self._identity(run_id="", session_id=""))

        self.assertIsNone(directory)
        self.assertEqual(refusal, self.rc.REFUSAL_NO_INPUT_SCOPE)

    def test_the_scope_identifier_cannot_carry_a_path_separator(self):
        self.assertEqual(self.rc.run_input_scope(self._identity(run_id="../x")),
                         "x")
        self.assertEqual(self.rc.run_input_scope(self._identity(run_id="a/b")),
                         "ab")

    def test_a_server_run_has_no_input_directory(self):
        from unittest import mock

        with mock.patch.object(self.rc, "run_local_cwd", return_value=(None, None)):
            directory, refusal = self.rc.run_input_dir(self._identity())

        self.assertIsNone(directory)
        self.assertIsNone(refusal)


if __name__ == "__main__":
    unittest.main()
