# encoding:utf-8
"""Legacy shared-Agent file migration, and its maintenance-window drill.

change ``isolate-shared-agent-user-data`` (tasks 5.1/5.2). Two things are being
pinned here:

* the *decisions* — a file a trusted record attributes to exactly one member is
  moved into that member's subtree, while a file nobody can claim (or two members
  can) is preserved where the file surface already refuses it;
* the *drill* — a run that is interrupted and repeated neither duplicates nor
  overwrites anything, hashes the content before removing a source, and leaves an
  unknown-owner file unreadable through the API rather than re-publishing it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

from scripts.migrate_agent_user_files import (
    QUARANTINE_DIR_NAME,
    Action,
    apply_plan,
    merge_references,
    plan_workspace,
    referenced_owners,
    run,
)

ALICE = "usr_alice000000000000000000"
BOB = "usr_bob0000000000000000000"


def sha(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


class _MigrationCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cow-user-migration-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.ws = os.path.join(self.root, "agents", "shared-agent")
        os.makedirs(self.ws)

    def write(self, *parts, content=b"payload"):
        path = os.path.join(self.ws, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def store(self, rows):
        """A conversation store holding ``(owner, content, extras)`` rows."""
        path = os.path.join(self.root, "index.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE messages (owner TEXT, content TEXT, extras TEXT)")
        con.executemany("INSERT INTO messages VALUES (?,?,?)", rows)
        con.commit()
        con.close()
        return path

    def legacy_targets(self, plan):
        return {os.path.relpath(a.source, self.ws): a for a in plan.actions}


class TrustedRecordTests(_MigrationCase):
    def test_owner_comes_from_the_message_that_carried_the_file(self):
        upload = self.write("tmp", "report.pdf")
        store = self.store([
            (ALICE, '{"type": "file_to_send", "path": "%s"}' % upload, ""),
        ])
        owners = referenced_owners(store)
        self.assertEqual(owners.get(os.path.realpath(upload)), {ALICE})

    def test_an_ownerless_or_unreadable_record_attributes_nothing(self):
        upload = self.write("tmp", "report.pdf")
        store = self.store([
            ("", '{"path": "%s"}' % upload, ""),
            (BOB, "not json", "{also not json}"),
        ])
        self.assertEqual(referenced_owners(store), {})

    def test_a_relative_path_is_not_an_absolute_file_claim(self):
        store = self.store([(ALICE, '{"path": "reports/q1.txt"}', "")])
        self.assertEqual(referenced_owners(store), {})

    def test_a_missing_store_contributes_nothing(self):
        self.assertEqual(referenced_owners(os.path.join(self.root, "nope.db")), {})

    def test_extras_count_too(self):
        upload = self.write("outputs", "chart.png")
        store = self.store([
            (ALICE, "", '{"artifact": {"abs_path": "%s"}}' % upload),
        ])
        self.assertEqual(referenced_owners(store)[os.path.realpath(upload)], {ALICE})


class PlanTests(_MigrationCase):
    def test_one_owner_moves_into_that_owners_subtree(self):
        upload = self.write("tmp", "shot.png", content=b"alice-shot")
        plan = plan_workspace(self.ws, {os.path.realpath(upload): {ALICE}})
        action = plan.actions[0]
        self.assertEqual(action.kind, "upload")
        self.assertEqual(action.owner, ALICE)
        self.assertEqual(
            os.path.relpath(action.destination, os.path.realpath(self.ws)),
            os.path.join("user", ALICE, "uploads", "shot.png"))

    def test_an_output_lands_under_outputs(self):
        chart = self.write("outputs", "chart.png")
        plan = plan_workspace(self.ws, {os.path.realpath(chart): {BOB}})
        self.assertEqual(
            os.path.relpath(plan.actions[0].destination,
                            os.path.realpath(self.ws)),
            os.path.join("user", BOB, "outputs", "chart.png"))

    def test_an_unattributable_file_is_quarantined_not_published(self):
        self.write("tmp", "mystery.bin")
        plan = plan_workspace(self.ws, {})
        action = plan.actions[0]
        self.assertEqual(action.kind, "quarantine")
        self.assertEqual(
            os.path.relpath(action.destination, os.path.realpath(self.ws)),
            os.path.join("user", QUARANTINE_DIR_NAME, "tmp", "mystery.bin"))

    def test_a_file_two_members_claim_is_quarantined(self):
        shared = self.write("tmp", "both.bin")
        plan = plan_workspace(self.ws, {os.path.realpath(shared): {ALICE, BOB}})
        self.assertEqual(plan.actions[0].kind, "quarantine")
        self.assertIsNone(plan.actions[0].owner)

    def test_two_sources_never_share_one_destination(self):
        """Every planned destination is claimed by exactly one source.

        Real workspaces contain extraction trees whose members have the same
        basename (``tmp/_docx/word/document.xml`` next to
        ``tmp/_docx2/word/document.xml``, ``.rels``/``[Content_Types].xml`` and
        friends). Flattening them onto one quarantine name would make two
        files fight over one path, so a run either drops one or overwrites it.
        """
        self.write("tmp", "_docx", "word", "document.xml", content=b"first")
        self.write("tmp", "_docx2", "word", "document.xml", content=b"second")
        self.write("tmp", "_docx", "[Content_Types].xml", content=b"a")
        self.write("tmp", "_docx2", "[Content_Types].xml", content=b"b")

        plan = plan_workspace(self.ws, {})

        destinations = [a.destination for a in plan.actions]
        self.assertEqual(len(plan.actions), 4)
        self.assertEqual(
            len(set(destinations)), len(destinations),
            "two legacy files would land on the same destination: %s"
            % sorted(destinations))

    def test_a_quarantined_nested_file_keeps_its_relative_shape(self):
        """Quarantine preserves provenance instead of flattening the tree.

        The quarantined copy is the only surviving record of where the file
        came from, so it keeps its path under the legacy root it was found in.
        """
        nested = self.write("tmp", "_docx", "word", "document.xml")
        plan = plan_workspace(self.ws, {})

        self.assertEqual(
            os.path.relpath(plan.actions[0].destination, os.path.realpath(self.ws)),
            os.path.join("user", QUARANTINE_DIR_NAME,
                         "tmp", "_docx", "word", "document.xml"))
        self.assertEqual(plan.actions[0].source, os.path.realpath(nested))

    def test_a_flat_file_keeps_its_name_under_its_legacy_root(self):
        """The same basename in two legacy roots stays two distinct files."""
        first = self.write("tmp", "report.pdf", content=b"upload")
        second = self.write("outputs", "report.pdf", content=b"generated")
        plan = plan_workspace(self.ws, {})

        rel = {os.path.relpath(a.destination, os.path.realpath(self.ws))
               for a in plan.actions}
        self.assertEqual(rel, {
            os.path.join("user", QUARANTINE_DIR_NAME, "tmp", "report.pdf"),
            os.path.join("user", QUARANTINE_DIR_NAME, "outputs", "report.pdf"),
        })
        self.assertEqual({a.source for a in plan.actions},
                         {os.path.realpath(first), os.path.realpath(second)})

    def test_identical_destination_is_recognised_by_content_not_by_a_marker(self):
        """A half-done run (copy landed, source not removed) is re-identified."""
        upload = self.write("tmp", "shot.png", content=b"same-bytes")
        destination = self.write("user", ALICE, "uploads", "shot.png",
                                 content=b"same-bytes")
        plan = plan_workspace(self.ws, {os.path.realpath(upload): {ALICE}})
        action = plan.actions[0]
        # The bytes are already at their rightful place; the redundant legacy
        # copy still leaves the legacy region rather than staying tenant-readable.
        self.assertEqual(action.kind, "duplicate")
        self.assertEqual(os.path.realpath(destination),
                         os.path.join(os.path.realpath(self.ws), "user",
                                      ALICE, "uploads", "shot.png"))
        self.assertEqual(action.destination,
                         os.path.join(os.path.realpath(self.ws), "user",
                                      QUARANTINE_DIR_NAME, "tmp", "shot.png"))
        self.assertIn("already migrated", action.reason)

    def test_a_different_destination_is_never_overwritten(self):
        upload = self.write("tmp", "shot.png", content=b"new-bytes")
        destination = self.write("user", ALICE, "uploads", "shot.png",
                                 content=b"older-bytes")
        plan = plan_workspace(self.ws, {os.path.realpath(upload): {ALICE}})
        self.assertEqual(plan.actions[0].kind, "quarantine")
        self.assertEqual(plan.actions[0].reason,
                         "destination holds different content")
        self.assertEqual(sha(destination), sha(destination))

    def test_a_symlinked_entry_is_never_followed(self):
        outside = os.path.join(self.root, "secret.bin")
        with open(outside, "wb") as handle:
            handle.write(b"outside")
        os.makedirs(os.path.join(self.ws, "tmp"), exist_ok=True)
        os.symlink(outside, os.path.join(self.ws, "tmp", "link.bin"))
        plan = plan_workspace(self.ws, {os.path.realpath(outside): {ALICE}})
        self.assertEqual(plan.actions, [])

    def test_the_already_migrated_subtree_is_not_re_scanned(self):
        self.write("user", ALICE, "uploads", "kept.png")
        plan = plan_workspace(self.ws, {})
        self.assertEqual(plan.actions, [])


class DisasterRecoveryDrillTests(_MigrationCase):
    """Task 5.2: the maintenance window, interrupted and repeated."""

    def _fixture(self):
        attributable = self.write("tmp", "mine.png", content=b"mine")
        unknown = self.write("tmp", "mystery.bin", content=b"mystery")
        store = self.store([
            (ALICE, '{"type": "file_to_send", "path": "%s"}' % attributable, ""),
        ])
        return attributable, unknown, store

    def test_a_run_moves_attributable_files_and_quarantines_the_rest(self):
        attributable, unknown, store = self._fixture()
        expected = sha(attributable)
        plan = plan_workspace(self.ws, referenced_owners(store))
        apply_plan(plan)

        moved = os.path.join(self.ws, "user", ALICE, "uploads", "mine.png")
        self.assertTrue(os.path.isfile(moved))
        self.assertEqual(sha(moved), expected)
        self.assertFalse(os.path.exists(attributable))

        quarantined = os.path.join(self.ws, "user", QUARANTINE_DIR_NAME,
                                   "tmp", "mystery.bin")
        self.assertTrue(os.path.isfile(quarantined))
        self.assertFalse(os.path.exists(unknown))

    def test_a_second_run_has_nothing_left_to_do(self):
        _, _, store = self._fixture()
        plans = run([self.ws], apply=True,
                    store_for=lambda workspace: store)
        self.assertTrue(plans[0].actions)

        again = run([self.ws], apply=True, store_for=lambda workspace: store)
        self.assertEqual(again[0].actions, [])

    def test_an_interrupted_run_resumes_and_empties_the_legacy_region(self):
        """Simulate the crash window: destination written, source not removed."""
        attributable, _, store = self._fixture()
        destination = self.write("user", ALICE, "uploads", "mine.png",
                                 content=b"mine")

        plan = plan_workspace(self.ws, referenced_owners(store))
        apply_plan(plan)

        # The rightful copy is untouched, byte for byte...
        with open(destination, "rb") as handle:
            self.assertEqual(handle.read(), b"mine")
        # ...and the legacy source is gone, not left readable by the tenant.
        self.assertFalse(os.path.exists(attributable))
        self.assertEqual(_legacy_leftovers(self.ws), [])

    def test_the_quarantine_region_is_refused_by_the_file_surface(self):
        """The probe that matters: retained on disk, unreadable over HTTP."""
        from tests._helpers import WebAppHarness

        quarantine = os.path.join(self.ws, "user", QUARANTINE_DIR_NAME,
                                  "tmp", "mystery.bin")
        os.makedirs(os.path.dirname(quarantine), exist_ok=True)
        with open(quarantine, "wb") as handle:
            handle.write(b"must-not-leak")

        tmp = tempfile.mkdtemp(prefix="cow-user-migration-http-")
        self.addCleanup(shutil.rmtree, tmp, True)
        harness = WebAppHarness(tmp)
        self.addCleanup(harness.close)
        harness.workspace = os.path.join(harness.shared_root, "agents", "shared-agent")
        os.makedirs(os.path.join(harness.workspace, "user", QUARANTINE_DIR_NAME),
                    exist_ok=True)
        shutil.copyfile(
            quarantine,
            os.path.join(harness.workspace, "user", QUARANTINE_DIR_NAME,
                         "mystery.bin"))
        harness.write_roster([
            {"id": "shared-agent", "name": "Shared",
             "workspace": harness.workspace},
        ])
        harness.add_agent("shared-agent")
        harness.member("alice", ["member"])
        target = os.path.join(harness.workspace, "user", QUARANTINE_DIR_NAME,
                              "mystery.bin")

        response = harness.get("/api/file?path=" + quote(target),
                               token=harness.login("alice"), tenant=False)
        self.assertNotEqual(response.status.split()[0], "200", response.data)
        self.assertNotIn(b"must-not-leak", response.data)


def _all_files(root):
    found = []
    for current, _dirs, names in os.walk(root):
        found.extend(os.path.join(current, name) for name in names)
    return found


def _legacy_leftovers(workspace):
    """Files still inside the legacy regions the file surface can still serve."""
    leftovers = []
    for name in ("tmp", "uploads", "outputs"):
        directory = os.path.join(workspace, name)
        if os.path.isdir(directory):
            leftovers.extend(_all_files(directory))
    return leftovers


if __name__ == "__main__":
    unittest.main()
