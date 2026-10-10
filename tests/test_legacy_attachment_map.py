# encoding:utf-8
"""The single-hop legacy attachment map and its resolution rules.

change ``unify-private-agent-personal-workspace`` (task 3.3). The map is how an
old absolute path written into a message keeps resolving after the layout
migration moved the file into ``user/<uid>/``. These tests pin the four decisions
(direct / mapped / conflict / missing), the "one hop" refusal, and the promise
that a broken table never breaks a read.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from agent.workspace import legacy_attachment_map as lam


class _MapCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cow-legacy-map-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(lam.reset_cache)
        self.map_path = os.path.join(self.root, "legacy_attachment_map.json")

    def write_map(self, mapping):
        os.makedirs(os.path.dirname(self.map_path), exist_ok=True)
        with open(self.map_path, "w") as handle:
            json.dump({"version": 1, "mapping": mapping}, handle)

    def touch(self, *parts, content=b"x"):
        path = os.path.join(self.root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(content)
        return path


class ResolveTests(_MapCase):
    def test_no_table_leaves_every_path_alone(self):
        path = self.touch("legacy", "report.pdf")
        resolution = lam.resolve_attachment_path(path, map_path=self.map_path)
        self.assertEqual(resolution.status, lam.DIRECT)
        self.assertEqual(resolution.path, os.path.realpath(path))

    def test_a_moved_file_resolves_to_its_new_location_in_one_hop(self):
        moved = self.touch("user", "usr_alice", "outputs", "report.pdf")
        old = os.path.join(self.root, "outputs", "report.pdf")
        self.write_map({os.path.realpath(old): moved})

        resolution = lam.resolve_attachment_path(old, map_path=self.map_path)

        self.assertEqual(resolution.status, lam.MAPPED)
        self.assertEqual(resolution.path, os.path.realpath(moved))
        self.assertTrue(resolution.ok)

    def test_a_reused_old_path_is_a_conflict_not_the_replacement(self):
        replacement = self.touch("outputs", "report.pdf", content=b"new")
        moved = self.touch("user", "usr_alice", "outputs", "report.pdf")
        real_old = os.path.realpath(replacement)
        self.write_map({real_old: moved})

        resolution = lam.resolve_attachment_path(replacement,
                                                 map_path=self.map_path)

        self.assertEqual(resolution.status, lam.CONFLICT)
        self.assertFalse(resolution.ok)

    def test_a_missing_target_is_reported_not_substituted(self):
        old = os.path.join(self.root, "outputs", "gone.pdf")
        self.write_map({os.path.realpath(old): os.path.join(
            self.root, "user", "usr_alice", "outputs", "gone.pdf")})

        resolution = lam.resolve_attachment_path(old, map_path=self.map_path)

        self.assertEqual(resolution.status, lam.MISSING)
        self.assertFalse(resolution.ok)

    def test_a_chained_mapping_is_refused(self):
        """A value that is itself a key is a redirect chain, never followed."""
        middle = os.path.join(self.root, "middle", "a.bin")
        end = os.path.join(self.root, "end", "a.bin")
        old = os.path.join(self.root, "old", "a.bin")
        self.write_map({os.path.realpath(old): middle,
                        os.path.realpath(middle): end})
        os.makedirs(os.path.dirname(end), exist_ok=True)
        with open(end, "wb") as handle:
            handle.write(b"x")

        resolution = lam.resolve_attachment_path(old, map_path=self.map_path)

        self.assertEqual(resolution.status, lam.CONFLICT)

    def test_a_relative_reference_is_not_guessed(self):
        target = self.touch("user", "usr_alice", "outputs", "report.pdf")
        self.write_map({os.path.realpath(
            os.path.join(self.root, "outputs", "report.pdf")): target})

        resolution = lam.resolve_attachment_path("outputs/report.pdf",
                                                  map_path=self.map_path)

        self.assertEqual(resolution.status, lam.DIRECT)

    def test_a_corrupt_table_is_treated_as_no_table(self):
        with open(self.map_path, "w") as handle:
            handle.write("{not json")
        path = self.touch("outputs", "report.pdf")
        resolution = lam.resolve_attachment_path(path, map_path=self.map_path)
        self.assertEqual(resolution.status, lam.DIRECT)

    def test_the_table_is_reloaded_when_it_changes(self):
        old = os.path.join(self.root, "outputs", "report.pdf")
        first = self.touch("user", "usr_alice", "outputs", "report.pdf")
        self.write_map({os.path.realpath(old): first})
        self.assertEqual(
            lam.resolve_attachment_path(old, map_path=self.map_path).status,
            lam.MAPPED)

        second = self.touch("user", "usr_bob", "outputs", "report.pdf")
        self.write_map({os.path.realpath(old): second})
        resolution = lam.resolve_attachment_path(old, map_path=self.map_path)
        self.assertEqual(resolution.path, os.path.realpath(second))

    def test_the_env_override_selects_the_table(self):
        target = self.touch("user", "usr_alice", "outputs", "report.pdf")
        old = os.path.join(self.root, "outputs", "report.pdf")
        self.write_map({os.path.realpath(old): target})
        os.environ[lam.ENV_VAR] = self.map_path
        self.addCleanup(os.environ.pop, lam.ENV_VAR, None)

        self.assertEqual(lam.map_file_path(), os.path.realpath(self.map_path))
        self.assertEqual(lam.load_map(), {os.path.realpath(old): target})

    def test_an_empty_path_resolves_to_nothing(self):
        self.assertEqual(
            lam.resolve_attachment_path("", map_path=self.map_path).status,
            lam.DIRECT)


if __name__ == "__main__":
    unittest.main()
