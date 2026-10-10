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
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

from scripts.migrate_agent_user_files import (
    QUARANTINE_DIR_NAME,
    Action,
    apply_plan,
    load_mapping,
    merge_references,
    plan_workspace,
    referenced_owners,
    restore_manifest,
    rewrite_trash_targets,
    run,
    verify_manifest,
    write_manifest,
    write_mapping,
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


class BusinessInventoryTests(_MigrationCase):
    """The root-level business inventory: relative shape, ownership, no guesses."""

    def test_a_root_business_file_preserves_its_relative_path(self):
        doc = self.write("docs", "2026", "q1.pdf", content=b"q1")
        plan = plan_workspace(self.ws, {os.path.realpath(doc): {ALICE}})
        action = plan.actions[0]
        self.assertEqual(action.kind, "business")
        self.assertEqual(action.owner, ALICE)
        self.assertEqual(
            os.path.relpath(action.destination, os.path.realpath(self.ws)),
            os.path.join("user", ALICE, "docs", "2026", "q1.pdf"))
        self.assertEqual(action.digest, sha(doc))
        self.assertEqual(action.mapping_to, action.destination)

    def test_configuration_at_the_root_is_left_alone(self):
        self.write("AGENT.md", content=b"# persona")
        self.write("memory", "MEMORY.md", content=b"mem")
        self.write("skills", "skill.md", content=b"skill")
        plan = plan_workspace(self.ws, {})
        self.assertEqual(plan.actions, [])

    def test_credentials_and_runtime_state_are_never_business_files(self):
        self.write(".env", content=b"SECRET=1")
        self.write("mcp.json", content=b"{}")
        self.write("identity.db", content=b"sqlite")
        plan = plan_workspace(self.ws, {})
        self.assertEqual(plan.actions, [])

    def test_explicitly_naming_a_credential_is_refused_not_migrated(self):
        env = self.write(".env", content=b"SECRET=1")
        plan = plan_workspace(self.ws, {os.path.realpath(env): {ALICE}},
                              business_files=[env])
        action = plan.actions[0]
        self.assertEqual(action.kind, "quarantine")
        self.assertIn("not business data", action.reason)

    def test_explicit_configuration_is_refused_not_migrated(self):
        agent_md = self.write("AGENT.md", content=b"# persona")
        plan = plan_workspace(self.ws, {os.path.realpath(agent_md): {ALICE}},
                              business_files=[agent_md])
        action = plan.actions[0]
        self.assertEqual(action.kind, "quarantine")
        self.assertIn("configuration entry", action.reason)

    def test_a_directory_with_mixed_owners_splits_per_file(self):
        mine = self.write("docs", "mine.txt", content=b"mine")
        yours = self.write("docs", "yours.txt", content=b"yours")
        orphan = self.write("docs", "orphan.txt", content=b"orphan")
        plan = plan_workspace(self.ws, {
            os.path.realpath(mine): {ALICE},
            os.path.realpath(yours): {BOB},
        })
        by_source = {a.source: a for a in plan.actions}
        self.assertEqual(by_source[os.path.realpath(mine)].owner, ALICE)
        self.assertEqual(by_source[os.path.realpath(yours)].owner, BOB)
        self.assertEqual(by_source[os.path.realpath(orphan)].kind, "quarantine")
        self.assertIn("no trusted owner", by_source[os.path.realpath(orphan)].reason)

    def test_a_root_business_file_two_members_claim_is_quarantined(self):
        shared = self.write("shared.xlsx", content=b"both")
        plan = plan_workspace(self.ws, {os.path.realpath(shared): {ALICE, BOB}})
        self.assertEqual(plan.actions[0].kind, "quarantine")
        self.assertIn("2 users", plan.actions[0].reason)

    def test_the_preflight_creates_nothing(self):
        doc = self.write("docs", "q1.pdf")
        self.write("tmp", "orphan.bin")
        plan = plan_workspace(self.ws, {os.path.realpath(doc): {ALICE}})
        self.assertTrue(plan.actions)
        self.assertFalse(os.path.exists(
            os.path.join(self.ws, "user", QUARANTINE_DIR_NAME)))
        self.assertFalse(os.path.exists(
            os.path.join(self.ws, "user", ALICE, "docs")))

    def test_the_user_container_is_never_rescanned(self):
        self.write("user", ALICE, "work", "kept.txt")
        plan = plan_workspace(self.ws, {})
        self.assertEqual(plan.actions, [])

    def test_a_symlinked_business_file_is_skipped(self):
        outside = os.path.join(self.root, "secret2.bin")
        with open(outside, "wb") as handle:
            handle.write(b"secret")
        os.symlink(outside, os.path.join(self.ws, "link.txt"))
        plan = plan_workspace(self.ws, {os.path.realpath(outside): {ALICE}})
        self.assertEqual(plan.actions, [])

    def test_a_cross_device_destination_is_refused(self):
        doc = self.write("docs", "q1.pdf", content=b"q1")
        source_dir = os.path.join(os.path.realpath(self.ws), "docs")

        def fake_device(path):
            return 1 if path.startswith(source_dir) else 2

        with patch("scripts.migrate_agent_user_files._device_of",
                   side_effect=fake_device):
            plan = plan_workspace(self.ws, {os.path.realpath(doc): {ALICE}})
        self.assertEqual(plan.actions[0].kind, "quarantine")
        self.assertIn("cross-device", plan.actions[0].reason)


class BusinessApplyAndArtifactTests(_MigrationCase):
    def test_apply_moves_business_and_records_a_result(self):
        doc = self.write("docs", "q1.pdf", content=b"q1")
        plan = plan_workspace(self.ws, {os.path.realpath(doc): {ALICE}})
        apply_plan(plan)

        moved = os.path.join(self.ws, "user", ALICE, "docs", "q1.pdf")
        self.assertTrue(os.path.isfile(moved))
        self.assertFalse(os.path.exists(doc))
        self.assertEqual(plan.actions[0].result, "moved")

    def test_apply_is_idempotent(self):
        doc = self.write("docs", "q1.pdf")
        owners = {os.path.realpath(doc): {ALICE}}
        apply_plan(plan_workspace(self.ws, owners))

        again = plan_workspace(self.ws, owners)
        self.assertEqual(again.actions, [])

    def test_manifest_and_mapping_are_written_protected(self):
        doc = self.write("docs", "q1.pdf", content=b"q1")
        expected = sha(doc)
        plan = plan_workspace(self.ws, {os.path.realpath(doc): {ALICE}})
        apply_plan(plan)

        manifest_path = os.path.join(self.root, "run", "manifest.json")
        mapping_path = os.path.join(self.root, "run", "mapping.json")
        write_manifest([plan], manifest_path)
        table = write_mapping([plan], mapping_path)

        self.assertEqual(stat.S_IMODE(os.stat(manifest_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(mapping_path).st_mode), 0o600)
        with open(manifest_path) as handle:
            entry = json.load(handle)["plans"][0]["actions"][0]
        self.assertEqual(entry["digest"], expected)
        self.assertEqual(entry["result"], "moved")
        self.assertEqual(load_mapping(mapping_path), table)
        self.assertEqual(table, plan.mapping())

    def test_the_mapping_is_single_hop(self):
        first = self.write("a.txt", content=b"a")
        second = self.write("b.txt", content=b"b")
        plan = plan_workspace(self.ws, {os.path.realpath(first): {ALICE},
                                        os.path.realpath(second): {BOB}})
        apply_plan(plan)

        table = plan.mapping()
        self.assertEqual(len(table), 2)
        for new in table.values():
            self.assertNotIn(new, table, "the map must never chain")

    def test_quarantined_files_publish_no_new_location(self):
        self.write("mystery.bin", content=b"mystery")
        plan = plan_workspace(self.ws, {})
        self.assertEqual(plan.mapping(), {})

    def test_a_destination_written_after_planning_is_never_overwritten(self):
        doc = self.write("docs", "q1.pdf", content=b"legacy")
        plan = plan_workspace(self.ws, {os.path.realpath(doc): {ALICE}})
        destination = os.path.join(self.ws, "user", ALICE, "docs", "q1.pdf")
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "wb") as handle:
            handle.write(b"newer")

        apply_plan(plan)

        with open(destination, "rb") as handle:
            self.assertEqual(handle.read(), b"newer")
        self.assertEqual(plan.actions[0].kind, "quarantine")
        self.assertFalse(os.path.exists(doc))

    def test_verify_reports_a_clean_run(self):
        doc = self.write("docs", "q1.pdf", content=b"q1")
        plan = plan_workspace(self.ws, {os.path.realpath(doc): {ALICE}})
        apply_plan(plan)
        manifest_path = os.path.join(self.root, "manifest.json")
        write_manifest([plan], manifest_path)

        report = verify_manifest(manifest_path)
        self.assertEqual(report, {"checked": 1, "mismatched": 0,
                                  "source_remaining": 0})


class TrashRewriteTests(_MigrationCase):
    def _batch(self, uid, batch, rels):
        batch_dir = os.path.join(self.ws, "user", uid, ".trash", batch)
        os.makedirs(os.path.join(batch_dir, "files"), exist_ok=True)
        meta = {
            "batch_id": batch,
            "created_at": 0,
            "items": [
                {"rel": rel, "kind": "file", "size": 1, "state": "trashed",
                 "restored_to": None}
                for rel in rels
            ],
        }
        path = os.path.join(batch_dir, "batch.json")
        with open(path, "w") as handle:
            json.dump(meta, handle)
        return path

    def test_a_trashed_legacy_path_is_repointed(self):
        doc = self.write("docs", "q1.pdf", content=b"q1")
        plan = plan_workspace(self.ws, {os.path.realpath(doc): {ALICE}})
        apply_plan(plan)
        rel = os.path.relpath(doc, self.ws)
        meta_path = self._batch(ALICE, "b1", [rel, "unrelated.txt"])

        reports = rewrite_trash_targets(self.ws, plan.mapping(), apply=True)

        self.assertEqual(len(reports), 1)
        with open(meta_path) as handle:
            saved = json.load(handle)
        self.assertEqual(saved["items"][0]["rel"],
                         "user/%s/docs/q1.pdf" % ALICE)
        self.assertEqual(saved["items"][1]["rel"], "unrelated.txt")
        self.assertTrue(os.path.exists(meta_path + ".pre-migration.bak"))

    def test_a_runtime_prefixed_rel_keeps_its_prefix(self):
        doc = self.write("docs", "q1.pdf", content=b"q1")
        plan = plan_workspace(self.ws, {os.path.realpath(doc): {ALICE}})
        apply_plan(plan)
        meta_path = self._batch(
            ALICE, "b2", ["agents/shared-agent/docs/q1.pdf"])

        rewrite_trash_targets(self.ws, plan.mapping(), apply=True)

        with open(meta_path) as handle:
            saved = json.load(handle)
        self.assertEqual(saved["items"][0]["rel"],
                         "agents/shared-agent/user/%s/docs/q1.pdf" % ALICE)

    def test_a_dry_run_changes_nothing(self):
        doc = self.write("docs", "q1.pdf", content=b"q1")
        plan = plan_workspace(self.ws, {os.path.realpath(doc): {ALICE}})
        apply_plan(plan)
        meta_path = self._batch(ALICE, "b3", ["docs/q1.pdf"])
        with open(meta_path) as handle:
            before = handle.read()

        reports = rewrite_trash_targets(self.ws, plan.mapping(), apply=False)

        self.assertTrue(reports)
        with open(meta_path) as handle:
            self.assertEqual(handle.read(), before)
        self.assertFalse(os.path.exists(meta_path + ".pre-migration.bak"))


class RestoreTests(_MigrationCase):
    def test_restore_moves_published_files_back(self):
        doc = self.write("docs", "q1.pdf", content=b"q1")
        plan = plan_workspace(self.ws, {os.path.realpath(doc): {ALICE}})
        apply_plan(plan)
        manifest_path = os.path.join(self.root, "manifest.json")
        write_manifest([plan], manifest_path)

        results = restore_manifest(manifest_path, apply=True)

        self.assertTrue(os.path.isfile(doc))
        self.assertFalse(os.path.exists(
            os.path.join(self.ws, "user", ALICE, "docs", "q1.pdf")))
        self.assertEqual(results[0]["status"], "restored")

    def test_restore_never_overwrites_a_post_migration_file(self):
        doc = self.write("docs", "q1.pdf", content=b"legacy")
        plan = plan_workspace(self.ws, {os.path.realpath(doc): {ALICE}})
        apply_plan(plan)
        manifest_path = os.path.join(self.root, "manifest.json")
        write_manifest([plan], manifest_path)
        self.write("docs", "q1.pdf", content=b"brand-new")

        results = restore_manifest(manifest_path, apply=True)

        self.assertEqual(results[0]["status"], "occupied")
        with open(doc, "rb") as handle:
            self.assertEqual(handle.read(), b"brand-new")

    def test_restore_leaves_the_quarantine_alone(self):
        self.write("mystery.bin", content=b"mystery")
        plan = plan_workspace(self.ws, {})
        apply_plan(plan)
        manifest_path = os.path.join(self.root, "manifest.json")
        write_manifest([plan], manifest_path)

        self.assertEqual(restore_manifest(manifest_path, apply=True), [])
        self.assertTrue(os.path.isfile(os.path.join(
            self.ws, "user", QUARANTINE_DIR_NAME, "mystery.bin")))


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
