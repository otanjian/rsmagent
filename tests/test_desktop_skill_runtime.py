# encoding:utf-8
"""The skill-package path must be reachable from the running product (8.1–8.4).

A manifest builder, a version cache and a typed-reference resolver are all
*libraries*. This repository has already shipped a case where the library and its
tests were correct while the production entry point never called it -- cleanup
code that existed, passed its own suite, and never ran. So this file drives the
seams the product actually goes through:

* ``SkillManager`` → ``LocalSkillRuntime.prepare`` → cache → pinned ``RunSkillSet``;
* ``AgentStream._stage_local_inputs`` → ``prepare_tool_inputs`` → a real local path.

The failure modes pinned here:

* a skill reference resolving to a *project* path, so a missing skill looks like a
  missing file and the model reads whatever happens to share the name;
* the runtime holding a pin nobody releases, so every run leaks a version and
  ``gc`` can never reclaim it;
* a skill deployed for a platform the device is not;
* an authorization revoked after the prompt was built still deploying.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from agent.skills.manager import SkillManager
from agent.skills.types import Skill, SkillEntry, SkillMetadata
from common.runtime_identity import RuntimeIdentity


def write_skill(root: str, name: str, *, files: dict | None = None,
                frontmatter: str | None = None) -> str:
    base = os.path.join(root, name)
    bundle = dict(files or {
        "SKILL.md": f"---\nname: {name}\ndescription: d\n---\n",
        "templates/report.xlsx": b"template-bytes",
    })
    if frontmatter is not None:
        bundle["SKILL.md"] = frontmatter
    for relative, body in bundle.items():
        full = os.path.join(base, *relative.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        mode = "wb" if isinstance(body, bytes) else "w"
        with open(full, mode) as handle:
            handle.write(body)
    return base


def make_skill(base: str, name: str, *, os_list=None, requires=None) -> SkillEntry:
    text = open(os.path.join(base, "SKILL.md"), encoding="utf-8").read()
    skill = Skill(name=name, description="d",
                  file_path=os.path.join(base, "SKILL.md"), base_dir=base,
                  source="builtin", content=text, frontmatter={})
    return SkillEntry(skill=skill,
                      metadata=SkillMetadata(os=list(os_list or []),
                                             requires=dict(requires or {})))


class _Manager:
    """A stand-in manager exposing only what the runtime is allowed to use."""

    def __init__(self, entries, *, authorized=True):
        self._entries = list(entries)
        self.authorized = authorized

    def filter_skills(self, skill_filter=None, include_disabled=False):
        if skill_filter is None:
            return list(self._entries)
        return [e for e in self._entries if e.skill.name in set(skill_filter)]

    def is_authorized(self, resource_id):
        return self.authorized


class _RuntimeCase(unittest.TestCase):
    def setUp(self):
        from agent.desktop_local import skill_cache, skill_runtime

        self.cache_mod = skill_cache
        self.mod = skill_runtime
        self._tmp = tempfile.TemporaryDirectory(prefix="skill-runtime-")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.cache_root = os.path.join(self.root, "cache")
        os.makedirs(self.cache_root, exist_ok=True)
        self.scope = skill_cache.SkillScope(
            origin="https://master.example", tenant_id="t-1", user_id="u-1")
        self.cache = skill_cache.SkillCache(self.cache_root)

    def runtime(self, entries, **kwargs):
        return self.mod.LocalSkillRuntime(
            manager=_Manager(entries, authorized=kwargs.pop("authorized", True)),
            cache=self.cache, scope=self.scope, platform=kwargs.pop("platform", "posix"),
            **kwargs)


class DeploymentTests(_RuntimeCase):
    def test_an_authorized_skill_is_deployed_and_pinned(self):
        base = write_skill(self.root, "excel")
        runtime = self.runtime([make_skill(base, "excel")])

        skills = runtime.prepare()

        self.assertEqual(runtime.problems, [])
        self.assertEqual(len(runtime.deployments), 1)
        deployed = runtime.deployments[0]
        self.assertEqual(deployed.skill_id, "builtin:excel")
        self.assertTrue(os.path.isdir(deployed.path))
        self.assertTrue(os.path.isfile(os.path.join(deployed.path, "templates", "report.xlsx")))
        self.assertEqual(self.cache.refcount(self.scope, "builtin:excel", deployed.digest), 1)

    def test_release_drops_the_pin_so_gc_can_reclaim(self):
        base = write_skill(self.root, "excel")
        runtime = self.runtime([make_skill(base, "excel")])
        runtime.prepare()
        deployed = runtime.deployments[0]

        runtime.release()

        self.assertEqual(self.cache.refcount(self.scope, "builtin:excel", deployed.digest), 0)
        self.assertEqual(list(self.cache.gc(self.scope)), [deployed.digest])

    def test_prepare_twice_does_not_leak_a_reference_per_call(self):
        base = write_skill(self.root, "excel")
        runtime = self.runtime([make_skill(base, "excel")])
        runtime.prepare()
        runtime.prepare()
        deployed = runtime.deployments[0]

        self.assertEqual(self.cache.refcount(self.scope, "builtin:excel", deployed.digest), 1)
        runtime.release()
        self.assertEqual(self.cache.refcount(self.scope, "builtin:excel", deployed.digest), 0)

    def test_a_skill_for_another_platform_is_skipped_with_a_reason(self):
        base = write_skill(self.root, "excel")
        runtime = self.runtime(
            [make_skill(base, "excel", os_list=["darwin"])], platform="win32")

        runtime.prepare()

        self.assertEqual(runtime.deployments, [])
        self.assertEqual(len(runtime.problems), 1)
        self.assertEqual(runtime.problems[0].code, "platform_unsupported")
        self.assertIn("excel", runtime.failure_message())

    def test_one_broken_skill_does_not_take_the_other_down(self):
        good = write_skill(self.root, "excel")
        broken = os.path.join(self.root, "broken", "gone")
        orphan = Skill(name="broken", description="d",
                       file_path=os.path.join(broken, "SKILL.md"), base_dir=broken,
                       source="builtin", content="", frontmatter={})
        runtime = self.runtime([
            make_skill(good, "excel"),
            SkillEntry(skill=orphan, metadata=SkillMetadata()),
        ])

        runtime.prepare()

        self.assertEqual([d.skill_id for d in runtime.deployments], ["builtin:excel"])
        self.assertEqual(len(runtime.problems), 1)
        self.assertEqual(runtime.problems[0].code, "skill_unavailable")

    def test_an_unauthorized_skill_is_not_deployed(self):
        base = write_skill(self.root, "excel")
        runtime = self.runtime([make_skill(base, "excel")], authorized=False)

        runtime.prepare()

        self.assertEqual(runtime.deployments, [])
        self.assertEqual(runtime.problems[0].code, "not_authorized")

    def test_a_revocation_after_selection_stops_the_deployment(self):
        """The grant is re-checked at deploy time, not assumed from selection."""
        base = write_skill(self.root, "excel")
        manager = _Manager([make_skill(base, "excel")], authorized=True)
        runtime = self.mod.LocalSkillRuntime(
            manager=manager, cache=self.cache, scope=self.scope, platform="posix")
        manager.authorized = False

        runtime.prepare()

        self.assertEqual(runtime.deployments, [])
        self.assertEqual(runtime.problems[0].code, "not_authorized")

    def test_the_roots_are_the_pinned_versions(self):
        base = write_skill(self.root, "excel")
        runtime = self.runtime([make_skill(base, "excel")])
        runtime.prepare()

        self.assertEqual([os.path.realpath(r) for r in runtime.roots()],
                         [os.path.realpath(runtime.deployments[0].path)])

    def test_no_skills_means_no_roots_and_no_failure(self):
        runtime = self.runtime([])
        runtime.prepare()

        self.assertEqual(runtime.deployments, [])
        self.assertEqual(runtime.problems, [])
        self.assertEqual(runtime.roots(), [])

    def test_a_credential_inside_a_skill_is_refused_at_deploy_time(self):
        base = write_skill(self.root, "excel", files={
            "SKILL.md": "---\nname: excel\ndescription: d\n---\n",
            ".env": "MODEL_API_KEY=secret\n",
        })
        runtime = self.runtime([make_skill(base, "excel")])

        runtime.prepare()

        self.assertEqual(runtime.deployments, [])
        self.assertEqual(runtime.problems[0].code, "secret_refused")


class RuntimeWiringTests(_RuntimeCase):
    """The runtime is only useful if the *run* reaches it."""

    def test_a_stream_builds_the_runtime_from_its_agent(self):
        """The seam the product goes through, driven with a real SkillManager.

        A hand-rolled stand-in would prove the runtime can be *called*; using the
        real manager proves the skill the model was actually offered is the one
        that gets deployed -- including the manager's own enable/selection gates.
        """
        from agent.protocol.agent_stream import AgentStreamExecutor
        from agent.skills.manager import SkillManager

        # ``builtin_dir`` is pointed at an empty directory on purpose: the default
        # is the machine's shared skills library, and a test that deploys
        # whatever happens to be installed there is not a test of this seam.
        skills_dir = os.path.join(self.root, "workspace", "skills")
        empty_builtin = os.path.join(self.root, "workspace", "no-builtins")
        os.makedirs(empty_builtin, exist_ok=True)
        write_skill(skills_dir, "excel")
        manager = SkillManager(builtin_dir=empty_builtin, custom_dir=skills_dir)
        self.assertEqual(sorted(manager.skills), ["excel"],
                         "the fixture must be the only skill in play")

        agent = type("A", (), {"skill_manager": manager})()
        stream = AgentStreamExecutor.__new__(AgentStreamExecutor)
        stream.agent = agent
        stream._local_skill_runtime = "unset"

        with patch("agent.desktop_local.skill_runtime.default_cache_root",
                   return_value=self.cache_root):
            # An identity with no verified subject, so runtime authorization is
            # "unrestricted" and this test is about the seam rather than about
            # grants (which `test_desktop_run_authorization` covers).
            with patch("common.runtime_identity.current_identity",
                       return_value=RuntimeIdentity()):
                runtime = stream._local_skills()

        self.assertIsNotNone(runtime, "the production seam must reach the runtime")
        self.assertEqual([d.name for d in runtime.deployments], ["excel"])
        deployed = runtime.deployments[0]
        self.assertTrue(deployed.skill_id.endswith(":excel"))

        stream._release_local_skills()
        self.assertEqual(
            self.cache.refcount(self.scope, deployed.skill_id, deployed.digest), 0)

    def test_a_stream_without_a_manager_degrades_to_no_skills(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        stream = AgentStreamExecutor.__new__(AgentStreamExecutor)
        stream.agent = type("A", (), {"skill_manager": None})()
        stream._local_skill_runtime = "unset"

        self.assertIsNone(stream._local_skills())

    def test_releasing_a_stream_that_never_prepared_is_harmless(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        stream = AgentStreamExecutor.__new__(AgentStreamExecutor)
        stream._local_skill_runtime = None
        stream._release_local_skills()


class ToolStagingTests(_RuntimeCase):
    """``prepare_tool_inputs`` must resolve typed references, not guess."""

    def _source(self, root):
        from agent.desktop_local.source_resolver import Source, DESKTOP
        return Source(kind=DESKTOP, root=root)

    def test_a_skill_reference_resolves_into_the_pinned_version(self):
        from agent.desktop_local.source_resolver import prepare_tool_inputs

        base = write_skill(self.root, "excel")
        runtime = self.runtime([make_skill(base, "excel")])
        skills = runtime.prepare()

        staged = prepare_tool_inputs(
            self._source(self.root), "read",
            {"path": "skill:builtin:excel/templates/report.xlsx"}, skills=skills)

        self.assertTrue(staged.ok, staged.message())
        self.assertTrue(os.path.isfile(staged.arguments["path"]))
        self.assertEqual(
            os.path.realpath(staged.arguments["path"]),
            os.path.realpath(os.path.join(runtime.deployments[0].path,
                                          "templates", "report.xlsx")))

    def test_a_skill_reference_without_deployment_is_refused_not_reinterpreted(self):
        """The trap: reading it as a project path finds ``skill:builtin:excel/...``."""
        from agent.desktop_local.source_resolver import prepare_tool_inputs

        staged = prepare_tool_inputs(
            self._source(self.root), "read",
            {"path": "skill:builtin:excel/templates/report.xlsx"}, skills=None)

        self.assertFalse(staged.ok)
        self.assertIn("builtin:excel", staged.message())
        # Refused *whole*: the argument is untouched, so running it could never
        # silently look for a project directory literally called
        # "skill:builtin:excel".
        self.assertEqual(staged.arguments["path"],
                         "skill:builtin:excel/templates/report.xlsx")

    def test_a_backend_reference_is_refused_for_a_local_run(self):
        from agent.desktop_local.source_resolver import prepare_tool_inputs

        staged = prepare_tool_inputs(
            self._source(self.root), "read",
            {"path": "backend:/srv/data/report.csv"}, skills=None)

        self.assertFalse(staged.ok)
        self.assertIn("/srv/data/report.csv", staged.message())

    def test_a_project_reference_still_behaves_exactly_as_before(self):
        from agent.desktop_local.source_resolver import prepare_tool_inputs

        with open(os.path.join(self.root, "notes.txt"), "w", encoding="utf-8") as h:
            h.write("x")

        for raw in ("notes.txt", "project:notes.txt", "./notes.txt"):
            with self.subTest(raw=raw):
                staged = prepare_tool_inputs(
                    self._source(self.root), "read", {"path": raw})
                self.assertTrue(staged.ok, staged.message())
                self.assertEqual(staged.arguments["path"], "notes.txt")

    def test_an_untyped_absolute_path_is_still_refused(self):
        from agent.desktop_local.source_resolver import prepare_tool_inputs

        staged = prepare_tool_inputs(
            self._source(self.root), "read", {"path": "/srv/data/report.csv"})

        self.assertFalse(staged.ok)


if __name__ == "__main__":
    unittest.main()
