# encoding:utf-8
"""Typed resource references, and where they actually live (change task 8.4).

The spec asks for three things at once here, and each is a way this can go wrong:

* **技能资源使用明确资源根、业务相对路径使用项目根.** A skill reads its template
  from *its* resource root and writes `output/报告.docx` into the *project*. If a
  single flat "relative path" rule served both, one of the two would silently
  land in the wrong tree -- and the destructive direction is a skill writing into
  its own read-only cache, or a business artefact appearing in the shared
  resource directory.
* **远程部署 SHALL 给模型和工具提供可解析的逻辑资源位置，并在本机解析实际路径.**
  The model must be able to *name* a skill resource in a way that resolves here,
  rather than being handed a server path.
* **MUST NOT 将服务器绝对路径直接传入本机脚本，也不得用全局文本替换修改技能代码.**
  A server path is refused, not translated; and nothing here rewrites the skill's
  own source to paper over a path that does not exist locally.

The failure modes pinned below:

* a missing skill resource being satisfied by a *same-named project file* (or the
  server's copy) -- "不把同名本机文件或服务器副本当作正确输入";
* a skill resource resolving outside the version that was authorized;
* a write into the read-only skill cache being allowed because it looked like a
  project-relative path;
* "fixing" a hardcoded server path by rewriting the skill's files, which hides
  the incompatibility instead of reporting it;
* `backend:` inputs being passed to a local script as if the server's filesystem
  were this one;
* a `bash` command string being text-substituted because it mentioned a path.
"""

from __future__ import annotations

import os
import tempfile
import unittest


def build_published_skill(root: str, *, name: str = "excel",
                          files: dict | None = None):
    """A verified skill version in a real cache, plus the objects to address it.

    Built through the 8.1/8.2 path (manifest -> payloads -> cache) rather than by
    hand, so a test cannot end up with a cache entry the real deploy path would
    never produce.
    """
    from agent.desktop_local.skill_cache import SkillCache, SkillScope
    from agent.skills.manifest import build_skill_manifest, read_skill_payloads
    from agent.skills.types import Skill, SkillEntry

    bundle = dict(files or {
        "SKILL.md": b"---\nname: excel\ndescription: d\n---\n",
        "templates/report.xlsx": b"template-bytes",
        "scripts/fill.py": b"print('fill')\n",
    })
    base = os.path.join(root, "authoring", name)
    for relative, body in bundle.items():
        full = os.path.join(base, *relative.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as handle:
            handle.write(body)
    skill = Skill(name=name, description="d",
                  file_path=os.path.join(base, "SKILL.md"), base_dir=base,
                  source="builtin",
                  content=bundle["SKILL.md"].decode("utf-8"), frontmatter={})
    manifest = build_skill_manifest(
        SkillEntry(skill=skill), platform="posix", is_authorized=lambda _sid: True)

    cache = SkillCache(os.path.join(root, "cache"))
    scope = SkillScope(origin="https://master.example", tenant_id="t-1", user_id="u-1")
    published = cache.publish(scope, manifest.to_dict(), read_skill_payloads(manifest))
    return {
        "cache": cache, "scope": scope, "published": published,
        "manifest": manifest, "skill_id": manifest.skill_id, "source_dir": base,
    }


class _RefCase(unittest.TestCase):
    def setUp(self):
        from agent.desktop_local import resource_refs

        self.mod = resource_refs
        self._tmp = tempfile.TemporaryDirectory(prefix="resource-refs-")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.project = os.path.join(self.root, "project")
        os.makedirs(os.path.join(self.project, "output"), exist_ok=True)
        with open(os.path.join(self.project, "notes.txt"), "w", encoding="utf-8") as h:
            h.write("project notes")
        self.skill = build_published_skill(self.root)
        self.skills = self.mod.RunSkillSet(
            self.skill["cache"], self.skill["scope"],
            authorized=lambda _sid: True)
        self.skills.pin(self.skill["skill_id"], self.skill["published"].digest)

    def resolve(self, raw, **kwargs):
        kwargs.setdefault("project_root", self.project)
        kwargs.setdefault("skills", self.skills)
        return self.mod.resolve_ref(raw, **kwargs)

    def codes(self):
        return self.mod.ResourceRefError


class ParsingTests(_RefCase):
    def test_a_bare_relative_path_is_a_project_reference(self):
        ref = self.mod.parse_ref("output/报告.docx")
        self.assertEqual(ref.kind, self.mod.PROJECT)
        self.assertEqual(ref.relative, "output/报告.docx")

    def test_the_project_prefix_is_accepted_and_equivalent(self):
        explicit = self.mod.parse_ref("project:output/报告.docx")
        bare = self.mod.parse_ref("output/报告.docx")
        self.assertEqual(explicit.kind, self.mod.PROJECT)
        self.assertEqual(explicit.relative, bare.relative)

    def test_a_skill_reference_carries_the_skill_and_the_resource(self):
        ref = self.mod.parse_ref("skill:builtin:excel/templates/report.xlsx")
        self.assertEqual(ref.kind, self.mod.SKILL)
        self.assertEqual(ref.skill_id, "builtin:excel")
        self.assertEqual(ref.relative, "templates/report.xlsx")

    def test_a_backend_reference_is_recognized_as_such(self):
        ref = self.mod.parse_ref("backend:/srv/data/report.csv")
        self.assertEqual(ref.kind, self.mod.BACKEND)

    def test_a_logical_reference_round_trips_through_its_text_form(self):
        for text in ("project:output/报告.docx",
                     "skill:builtin:excel/templates/report.xlsx",
                     "backend:/srv/data/report.csv"):
            with self.subTest(text=text):
                self.assertEqual(self.mod.format_ref(self.mod.parse_ref(text)), text)

    def test_an_untyped_absolute_path_is_not_a_typed_reference(self):
        """Ambiguous on purpose: it must be decided by the caller's rule, not
        guessed into ``backend`` here."""
        self.assertIsNone(self.mod.parse_ref("/srv/data/report.csv"))

    def test_a_skill_reference_without_a_resource_is_refused(self):
        with self.assertRaises(self.codes()) as caught:
            self.mod.parse_ref("skill:builtin:excel")
        self.assertEqual(caught.exception.code, "invalid_reference")

    def test_empty_input_is_refused(self):
        with self.assertRaises(self.codes()):
            self.mod.parse_ref("   ")


class ProjectResolutionTests(_RefCase):
    def test_a_project_reference_resolves_under_the_project_root(self):
        resolved = self.resolve("notes.txt")

        self.assertTrue(resolved.exists)
        self.assertFalse(resolved.read_only)
        self.assertEqual(os.path.realpath(resolved.absolute),
                         os.path.realpath(os.path.join(self.project, "notes.txt")))

    def test_a_project_reference_may_name_a_file_that_does_not_exist_yet(self):
        """A `write` to a new project file is an ordinary correct call."""
        resolved = self.resolve("output/报告.docx")

        self.assertFalse(resolved.exists)
        self.assertEqual(
            os.path.realpath(resolved.absolute),
            os.path.realpath(os.path.join(self.project, "output", "报告.docx")))
        self.mod.ensure_writable(resolved)

    def test_a_project_reference_that_escapes_the_project_is_refused(self):
        with self.assertRaises(self.codes()) as caught:
            self.resolve("../outside.txt")
        self.assertEqual(caught.exception.code, "outside_project")


class SkillResolutionTests(_RefCase):
    def test_a_skill_resource_resolves_into_its_pinned_version(self):
        resolved = self.resolve("skill:builtin:excel/templates/report.xlsx")

        self.assertTrue(resolved.exists)
        self.assertTrue(resolved.read_only, "skill resources are read-only")
        self.assertEqual(os.path.realpath(resolved.absolute),
                         os.path.realpath(os.path.join(
                             self.skill["published"].path, "templates", "report.xlsx")))

    def test_a_skill_and_a_project_file_of_the_same_name_are_different_files(self):
        """The whole reason the roots are separate."""
        decoy_dir = os.path.join(self.project, "templates")
        os.makedirs(decoy_dir, exist_ok=True)
        with open(os.path.join(decoy_dir, "report.xlsx"), "wb") as h:
            h.write(b"project decoy")

        skill_ref = self.resolve("skill:builtin:excel/templates/report.xlsx")
        project_ref = self.resolve("templates/report.xlsx")

        self.assertNotEqual(os.path.realpath(skill_ref.absolute),
                            os.path.realpath(project_ref.absolute))
        with open(skill_ref.absolute, "rb") as h:
            self.assertEqual(h.read(), b"template-bytes")

    def test_a_missing_skill_resource_is_reported_and_not_substituted(self):
        """Spec: 不把同名本机文件或服务器副本当作正确输入."""
        decoy_dir = os.path.join(self.project, "scripts")
        os.makedirs(decoy_dir, exist_ok=True)
        with open(os.path.join(decoy_dir, "missing.py"), "w", encoding="utf-8") as h:
            h.write("print('SAME NAME, WRONG FILE')\n")

        with self.assertRaises(self.codes()) as caught:
            self.resolve("skill:builtin:excel/scripts/missing.py")

        self.assertEqual(caught.exception.code, "incompatible_skill")
        self.assertIn("missing.py", caught.exception.message)
        self.assertIn("excel", caught.exception.message)

    def test_a_skill_that_was_not_pinned_for_this_run_is_incompatible(self):
        with self.assertRaises(self.codes()) as caught:
            self.resolve("skill:builtin:other/SKILL.md")

        self.assertEqual(caught.exception.code, "incompatible_skill")
        self.assertIn("builtin:other", caught.exception.message)

    def test_a_skill_resource_reaching_outside_its_version_is_refused(self):
        with self.assertRaises(self.codes()) as caught:
            self.resolve("skill:builtin:excel/../../etc/passwd")

        self.assertIn(caught.exception.code, ("outside_project", "invalid_reference"))

    def test_an_absolute_path_inside_a_skill_reference_is_refused(self):
        with self.assertRaises(self.codes()):
            self.resolve("skill:builtin:excel//etc/passwd")


class ReadOnlyTests(_RefCase):
    """Spec: 技能缓存 SHALL 只读，项目产出不进入技能缓存."""

    def test_a_skill_resource_may_not_be_written(self):
        resolved = self.resolve("skill:builtin:excel/templates/report.xlsx")

        with self.assertRaises(self.codes()) as caught:
            self.mod.ensure_writable(resolved)
        self.assertEqual(caught.exception.code, "skill_cache_read_only")

    def test_a_skill_resource_may_be_read(self):
        resolved = self.resolve("skill:builtin:excel/scripts/fill.py")
        self.mod.ensure_readable(resolved)

    def test_project_output_never_resolves_into_the_skill_cache(self):
        """A business artefact must land in the project, not the shared cache."""
        resolved = self.resolve("output/报告.docx")

        self.mod.ensure_writable(resolved)
        skill_root = os.path.realpath(self.skill["cache"].root)
        self.assertFalse(
            os.path.realpath(resolved.absolute).startswith(skill_root + os.sep))

    def test_the_read_only_roots_are_what_the_sandbox_should_grant(self):
        """The value that has to reach the grant's ``skillRoots``."""
        roots = self.skills.roots()

        self.assertEqual([os.path.realpath(r) for r in roots],
                         [os.path.realpath(self.skill["published"].path)])


class ServerPathTests(_RefCase):
    def test_a_backend_reference_is_refused_for_a_local_run(self):
        with self.assertRaises(self.codes()) as caught:
            self.resolve("backend:/srv/data/report.csv")

        self.assertEqual(caught.exception.code, "server_path_not_local")
        self.assertIn("/srv/data/report.csv", caught.exception.message)

    def test_a_hardcoded_server_path_is_reported_and_not_rewritten(self):
        """Spec: 旧技能依赖本机不可达的绝对路径 → 报告具体资源不兼容.

        The point is that a server path is *reported*, never translated into a
        same-named local file and never edited out of the skill.
        """
        with self.assertRaises(self.codes()) as caught:
            self.resolve("skill:builtin:excel//srv/data/legacy.xlsx")

        self.assertIn(caught.exception.code,
                      ("invalid_reference", "incompatible_skill", "server_path_not_local"))


class NoGlobalReplacementTests(_RefCase):
    """Spec: 不得用全局文本替换修改技能代码来掩盖路径错误."""

    def test_resolving_a_skill_resource_does_not_touch_the_skill_source(self):
        source_files = {}
        for root, _dirs, files in os.walk(self.skill["source_dir"]):
            for name in files:
                full = os.path.join(root, name)
                with open(full, "rb") as handle:
                    source_files[full] = handle.read()
        published_files = {}
        for root, _dirs, files in os.walk(self.skill["published"].path):
            for name in files:
                full = os.path.join(root, name)
                with open(full, "rb") as handle:
                    published_files[full] = handle.read()

        for target in ("skill:builtin:excel/templates/report.xlsx",
                       "skill:builtin:excel/scripts/fill.py"):
            self.resolve(target)

        current_source = {}
        for root, _dirs, files in os.walk(self.skill["source_dir"]):
            for name in files:
                full = os.path.join(root, name)
                with open(full, "rb") as handle:
                    current_source[full] = handle.read()
        current_published = {}
        for root, _dirs, files in os.walk(self.skill["published"].path):
            for name in files:
                full = os.path.join(root, name)
                with open(full, "rb") as handle:
                    current_published[full] = handle.read()

        self.assertEqual(current_source, source_files, "the skill source was modified")
        self.assertEqual(current_published, published_files, "the cached version was modified")

    def test_a_shell_command_string_is_never_text_substituted(self):
        """A `bash` argument is a program, not a path.

        Rewriting prose inside it is the "string filter standing in for
        isolation" the design rejects: it would miss ``$(cat ...)`` and still
        corrupt any command that happens to mention a path.
        """
        command = "cat /srv/data/report.csv | head -5 && echo templates/report.xlsx"
        resolved = self.mod.resolve_arguments(
            {"command": command, "path": "notes.txt"},
            project_root=self.project, skills=self.skills)

        self.assertEqual(resolved["command"], command)
        # The `path` argument *is* a path, so it is resolved to a real location.
        self.assertEqual(os.path.realpath(resolved["path"]),
                         os.path.realpath(os.path.join(self.project, "notes.txt")))

    def test_an_unknown_resource_is_reported_rather_than_patched_around(self):
        """There is no code path that edits a skill to make a reference work."""
        with self.assertRaises(self.codes()):
            self.resolve("skill:builtin:excel/does/not/exist.txt")
        # And the version on disk is still exactly as published.
        self.assertTrue(self.skill["cache"].verify(
            self.skill["scope"], self.skill["skill_id"],
            self.skill["published"].digest))


class IntegrationTests(_RefCase):
    """The seam a model's argument actually travels through."""

    def test_arguments_resolve_a_typed_skill_reference_to_a_real_path(self):
        resolved = self.mod.resolve_arguments(
            {"path": "skill:builtin:excel/templates/report.xlsx"},
            project_root=self.project, skills=self.skills)

        self.assertTrue(os.path.isfile(resolved["path"]))
        self.assertEqual(os.path.realpath(resolved["path"]),
                         os.path.realpath(os.path.join(
                             self.skill["published"].path, "templates", "report.xlsx")))

    def test_arguments_resolve_a_project_reference_to_the_project(self):
        resolved = self.mod.resolve_arguments(
            {"path": "notes.txt"},
            project_root=self.project, skills=self.skills)

        self.assertEqual(os.path.realpath(resolved["path"]),
                         os.path.realpath(os.path.join(self.project, "notes.txt")))

    def test_arguments_refuse_a_backend_reference_whole(self):
        with self.assertRaises(self.codes()) as caught:
            self.mod.resolve_arguments(
                {"path": "backend:/srv/data/report.csv"},
                project_root=self.project, skills=self.skills)
        self.assertEqual(caught.exception.code, "server_path_not_local")

    def test_arguments_leave_a_non_path_argument_alone(self):
        resolved = self.mod.resolve_arguments(
            {"query": "templates/report.xlsx", "path": "notes.txt"},
            project_root=self.project, skills=self.skills)

        self.assertEqual(resolved["query"], "templates/report.xlsx")

    def test_nested_arguments_are_resolved_too(self):
        resolved = self.mod.resolve_arguments(
            {"paths": ["notes.txt", "skill:builtin:excel/scripts/fill.py"]},
            project_root=self.project, skills=self.skills)

        self.assertEqual(os.path.realpath(resolved["paths"][0]),
                         os.path.realpath(os.path.join(self.project, "notes.txt")))
        self.assertEqual(os.path.realpath(resolved["paths"][1]),
                         os.path.realpath(os.path.join(
                             self.skill["published"].path, "scripts", "fill.py")))


class RunSkillSetTests(_RefCase):
    def test_pinning_the_same_version_twice_holds_one_reference(self):
        """A double-acquire with a single release would leak the version.

        The cache count could then never reach zero, so garbage collection would
        never reclaim it -- a leak that presents as "the cache only grows" and is
        invisible until disk fills up. Pinning is therefore idempotent per skill.
        """
        before = self.skill["cache"].refcount(
            self.skill["scope"], self.skill["skill_id"], self.skill["published"].digest)
        self.assertEqual(before, 1, "setUp pinned once")

        self.skills.pin(self.skill["skill_id"], self.skill["published"].digest)
        self.assertEqual(
            self.skill["cache"].refcount(self.skill["scope"], self.skill["skill_id"],
                                         self.skill["published"].digest), 1)

        self.skills.release_all()
        self.assertEqual(
            self.skill["cache"].refcount(self.skill["scope"], self.skill["skill_id"],
                                         self.skill["published"].digest), 0)

    def test_pinning_the_same_version_does_not_release_it_first(self):
        """A balanced release-then-reacquire is still a race, and this pins it.

        Refcounting alone cannot tell "keep the pin" from "drop it and take it
        again": both leave the count where they found it. But the second leaves a
        window in which the count is zero, and a concurrent ``gc`` -- another run
        finishing, or the cleanup sweep -- may reclaim the directory in that
        window, so the version vanishes from under a run that still holds it.
        Asserting the count is therefore weaker than asserting no release happens.
        """
        from unittest.mock import patch

        skill_id = self.skill["skill_id"]
        digest = self.skill["published"].digest
        with patch.object(self.skill["cache"], "release",
                          wraps=self.skill["cache"].release) as release:
            self.skills.pin(skill_id, digest)

        release.assert_not_called()
        self.assertEqual(
            self.skill["cache"].refcount(self.skill["scope"], skill_id, digest), 1)

    def test_releasing_twice_does_not_drive_the_count_negative(self):
        self.skills.release_all()
        self.skills.release_all()
        self.assertEqual(
            self.skill["cache"].refcount(self.skill["scope"], self.skill["skill_id"],
                                         self.skill["published"].digest), 0)

    def test_revoking_authorization_stops_resolution_even_while_pinned(self):
        """Spec: 撤权后不能借缓存继续使用."""
        revoked = self.mod.RunSkillSet(
            self.skill["cache"], self.skill["scope"],
            authorized=lambda _sid: False)

        with self.assertRaises(self.codes()) as caught:
            revoked.pin(self.skill["skill_id"], self.skill["published"].digest)
        self.assertEqual(caught.exception.code, "not_authorized")

    def test_an_unpinned_skill_yields_no_roots(self):
        empty = self.mod.RunSkillSet(
            self.skill["cache"], self.skill["scope"], authorized=lambda _sid: True)
        self.assertEqual(empty.roots(), [])

    def test_pins_report_the_versions_the_run_authorized(self):
        """The portable form of ``roots()`` (task 8.9).

        A second machine has neither this cache nor these paths, so what travels
        is the *version*, not a directory.
        """
        self.assertEqual(self.skills.pins(), [
            {"skill_id": self.skill["skill_id"],
             "digest": self.skill["published"].digest},
        ])

    def test_pins_are_in_canonical_order_regardless_of_pin_order(self):
        """Sorted by ``skill_id``, the same form the command digest uses.

        Otherwise the set a run reports and the set its digest covers would be
        two orderings of one thing, and a device would see a conflict that is
        really a reshuffle.
        """
        other = build_published_skill(self.root, name="alpha")
        skills = self.mod.RunSkillSet(
            other["cache"], other["scope"], authorized=lambda _sid: True)
        # Pin the alphabetically *later* skill first.
        skills.pin(self.skill["skill_id"], self.skill["published"].digest)
        skills.pin(other["skill_id"], other["published"].digest)

        ids = [entry["skill_id"] for entry in skills.pins()]

        self.assertEqual(ids, sorted(ids))

    def test_an_unpinned_skill_reports_no_pins(self):
        empty = self.mod.RunSkillSet(
            self.skill["cache"], self.skill["scope"], authorized=lambda _sid: True)
        self.assertEqual(empty.pins(), [])

    def test_releasing_a_run_takes_its_pins_with_it(self):
        self.skills.release_all()

        self.assertEqual(self.skills.pins(), [])
