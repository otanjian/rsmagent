# encoding:utf-8
"""The mutation harnesses must not report success they did not earn.

A mutation script is a *verifier*: it rewrites the implementation and checks that
the suite notices. When its own failure parser is wrong, it reports "not caught"
for a mutation the suite actually caught -- or, worse, the reverse in a real
regression run. This repository has already been bitten once by judging results
from `rg "^FAILED"`, which silently misses the per-test progress lines; the same
class of bug applies here, so the parser is pinned directly.

The cases below are the formats actually observed in this repo's runs, including
the one that got past the first version of this parser:

* ``FAILED path::Class::test``;
* ``SUBFAILED(name='.env') path::Class::test`` -- how a failure inside
  ``assertRaises`` + ``subTest`` surfaces. ``SUBFAILED`` is followed by
  ``(name=...)`` and *not* whitespace, so an anchored ``FAILED\\s+`` misses it.
"""

from __future__ import annotations

import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCRIPTS = os.path.join(
    REPO, "openspec", "changes", "align-desktop-project-execution-with-master",
    "evidence", "scripts")


def load(name: str):
    path = os.path.join(SCRIPTS, name)
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FailureParserTests(unittest.TestCase):
    def setUp(self):
        self.mod = load("mutate_skill_package.py")

    def test_a_plain_failure_is_named(self):
        output = "FAILED tests/test_x.py::Class::test_thing\n"
        self.assertEqual(self.mod.failed_tests(output), ["test_thing"])

    def test_a_subtest_failure_is_named(self):
        """The format that defeated ``FAILED\\s+``: ``SUBFAILED(name='.env')``."""
        output = ("SUBFAILED(name='.env') tests/test_c.py::Klass::test_secrets\n")
        self.assertEqual(self.mod.failed_tests(output), ["test_secrets"])

    def test_several_subtests_of_one_test_are_deduplicated(self):
        output = (
            "SUBFAILED(name='.env') tests/test_c.py::Klass::test_secrets\n"
            "SUBFAILED(name='id_rsa') tests/test_c.py::Klass::test_secrets\n"
            "FAILED tests/test_c.py::Klass::test_other\n"
        )
        self.assertEqual(self.mod.failed_tests(output), ["test_other", "test_secrets"])

    def test_a_class_less_failure_is_named(self):
        output = "FAILED tests/test_c.py::test_module_level\n"
        self.assertEqual(self.mod.failed_tests(output), ["test_module_level"])

    def test_a_passing_summary_yields_nothing(self):
        output = "53 passed, 7 subtests passed in 0.16s\n"
        self.assertEqual(self.mod.failed_tests(output), [])

    def test_a_progress_line_with_a_stray_f_is_not_a_failure(self):
        """``-q`` prints one character per test; a bare ``F`` is not a name."""
        output = "..............................F.........\n53 passed\n"
        self.assertEqual(self.mod.failed_tests(output), [])


class MutationDefinitionTests(unittest.TestCase):
    """Every mutation must be anchored and must name the test it expects.

    Run over every mutation script in the change, so a new script cannot be added
    without inheriting the same structural guarantees.
    """

    #: ``script name -> the sources that script is allowed to mutate``.
    SCRIPTS = {
        "mutate_skill_package.py": (
            "agent/desktop_local/skill_cache.py",
            "agent/desktop_local/package_rules.py",
            "agent/skills/manifest.py",
            "agent/desktop_local/resource_refs.py",
            "agent/desktop_local/skill_runtime.py",
            "agent/desktop_local/source_resolver.py",
            "agent/skills/dependencies.py",
            "desktop/build/check-skill-dependencies.py",
        ),
        "mutate_resource_landing.py": (
            "agent/desktop_local/resource_landing.py",
            "agent/desktop_local/source_resolver.py",
            "agent/desktop_local/resource_refs.py",
            "agent/desktop_local/run_inputs.py",
            "agent/desktop_local/run_context.py",
            "agent/desktop_local/tool_disposition.py",
            "agent/protocol/agent_stream.py",
        ),
        "mutate_prompt_priority.py": (
            "agent/prompt/builder.py",
            "agent/protocol/agent.py",
            "agent/desktop_local/script_executor.py",
            "agent/desktop_local/script_tool.py",
        ),
        "mutate_desktop_artifact_source.py": (
            "agent/protocol/artifact.py",
            "agent/protocol/agent_stream.py",
            "channel/web/fork/runtime.py",
            "desktop/src/main/project-execution/artifact.ts",
            "desktop/src/main/project-execution/device-execution.ts",
        ),
        "mutate_project_source.py": (
            "desktop/src/main/project-browser/browser.ts",
            "desktop/src/main/remote/host-bridge.ts",
            "desktop/src/main/remote/local-files-bridge.ts",
            "channel/web/static/js/fork/project-source.js",
        ),
        "mutate_project_watching.py": (
            "desktop/src/main/project-browser/watch.ts",
            "desktop/src/main/project-browser/restore.ts",
            "channel/web/static/js/console.js",
            "channel/web/static/js/workspace.js",
            "channel/web/static/js/fork/desktop-host.js",
        ),
        "mutate_project_native_actions.py": (
            "desktop/src/main/project-browser/native-actions.ts",
            "desktop/src/main/remote/host-bridge.ts",
            "desktop/src/main/remote/local-files-bridge.ts",
            "channel/web/static/js/fork/project-source.js",
            "channel/web/static/js/fork/desktop-host.js",
            "channel/web/static/js/workspace.js",
            "channel/web/chat.html",
        ),
        "mutate_skill_transfer.py": (
            "integrations/desktop/execution_broker.py",
            "desktop/src/main/project-execution/skill-transfer.ts",
            "desktop/src/main/project-execution/skill-cache.ts",
            "desktop/src/main/project-execution/device-execution.ts",
        ),
        "mutate_history_cards.py": (
            "channel/web/fork/runtime.py",
            "agent/desktop_local/__init__.py",
            "channel/web/static/js/fork/project-source.js",
            "channel/web/static/js/workspace.js",
        ),
        "mutate_materialize.py": (
            "desktop/src/main/local-files/materialize.ts",
            "desktop/src/main/local-files/transfer.ts",
            "desktop/src/main/remote/device-ops.ts",
            "desktop/src/main/remote/materialize-transport.ts",
            "channel/web/static/js/workspace.js",
            "channel/web/static/js/i18n/core.js",
        ),
        "mutate_release_gates.py": (
            "auth/store.py",
            "auth/capability_matrix.py",
            "integrations/desktop/execution_capability.py",
            "channel/web/fork/handlers/desktop.py",
            "integrations/desktop/execution_broker.py",
            "agent/desktop_remote/mode.py",
            "agent/desktop_local/run_context.py",
            "agent/desktop_local/__init__.py",
        ),
        "mutate_compatibility_matrix.py": (
            "auth/desktop_contracts_v2.py",
            "auth/capability_matrix.py",
            "channel/web/fork/handlers/desktop.py",
            "agent/desktop_remote/mode.py",
            # A29's cross-row property is about the *contract*: v1's op surface
            # must stay read-only, or "the new channel is unavailable" acquires a
            # fallback that executes a write. The contract file is data, not a
            # module, but it is a source the mutation legitimately rewrites.
            "contracts/desktop/v1.json",
        ),
    }

    def scripts(self):
        for name, allowed in self.SCRIPTS.items():
            yield name, load(name), {os.path.join(REPO, rel) for rel in allowed}

    def test_every_mutation_names_at_least_one_expected_failure(self):
        for name, mod, _allowed in self.scripts():
            for mutation in mod.MUTATIONS:
                with self.subTest(script=name, mutation=mutation["name"]):
                    self.assertTrue(
                        mutation["expect"],
                        "a mutation with no expectation cannot fail the run")

    def test_every_anchor_still_exists_in_the_source(self):
        """A drifted anchor must be a loud failure, not a silent skip.

        If the implementation is refactored and an anchor no longer matches, the
        mutation stops testing anything -- and a script that skips quietly would
        keep reporting success.
        """
        for name, mod, _allowed in self.scripts():
            for mutation in mod.MUTATIONS:
                with self.subTest(script=name, mutation=mutation["name"]):
                    with open(mutation["file"], encoding="utf-8") as handle:
                        source = handle.read()
                    self.assertIn(mutation["old"], source,
                                  f"{mutation['name']}: anchor no longer matches")

    def test_every_anchor_is_the_only_place_it_could_mean(self):
        """An anchor that appears twice mutates the *first* line, not the meant one.

        ``str.replace(old, new, 1)`` rewrites the earliest occurrence, so a line
        like ``if (!checkProjectPath(params.path, false)) {`` -- which is written
        once per project method -- silently retargets the mutation to whichever
        method comes first. The run then reports "the implementation was broken
        and the suite stayed green", which reads as a weak test but is really a
        mis-aimed mutation. This bit this change once already (task 9.4: the
        bridge path check), so it is checked structurally for every script.
        """
        for name, mod, _allowed in self.scripts():
            for mutation in mod.MUTATIONS:
                with self.subTest(script=name, mutation=mutation["name"]):
                    with open(mutation["file"], encoding="utf-8") as handle:
                        source = handle.read()
                    self.assertEqual(
                        source.count(mutation["old"]), 1,
                        f"{mutation['name']}: anchor is ambiguous; extend it with a "
                        "neighbouring line so it can only mean one place")

    def test_each_mutation_changes_its_target_file(self):
        for name, mod, _allowed in self.scripts():
            for mutation in mod.MUTATIONS:
                with self.subTest(script=name, mutation=mutation["name"]):
                    self.assertNotEqual(mutation["old"], mutation["new"])
                    self.assertNotEqual(mutation["new"], "")

    def test_the_mutations_are_aimed_only_at_the_declared_sources(self):
        for name, mod, allowed in self.scripts():
            for mutation in mod.MUTATIONS:
                with self.subTest(script=name, mutation=mutation["name"]):
                    self.assertIn(mutation["file"], allowed)


if __name__ == "__main__":
    unittest.main()
