# encoding:utf-8
"""The authorized skill manifest (change task 8.1).

A skill that runs on a local project is not "the folder on the server": it is a
*specific authorized version* of a skill, its resources, and the platform it will
actually run on. The manifest is the object that fixes all of that, and the
failure modes each test below pins:

* a manifest built for a skill the identity may not use -- authorization has to
  be re-checked when the manifest is built, not assumed from a previous turn;
* a digest that covers the description but not the scripts, so swapping a script
  leaves the digest unchanged and the device mounts the wrong code;
* a digest that depends on directory-listing order, so the same package hashes
  differently on two machines and every deploy looks like a new version;
* a resource that leaves the skill directory (``..`` or an absolute path) or is a
  link, which turns a "skill resource" into a read of anything on the machine;
* the server's platform leaking into a manifest for a Windows device, which is
  how a Linux server produces a call that only works with Linux paths.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from agent.skills.types import Skill, SkillEntry, SkillMetadata


def write(path: str, body: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)


def make_skill(root: str, name: str = "excel", *, files: dict | None = None,
               frontmatter: str | None = None) -> Skill:
    """A real skill directory on disk, so the digest is computed from files."""
    base = os.path.join(root, name)
    bundle = files or {
        "SKILL.md": f"---\nname: {name}\ndescription: fills spreadsheets\n---\n",
        "scripts/fill.py": "print('fill')\n",
        "templates/report.xlsx": "not-a-real-xlsx",
    }
    for relative, body in bundle.items():
        write(os.path.join(base, relative), body)
    if frontmatter is not None:
        write(os.path.join(base, "SKILL.md"), frontmatter)
    return Skill(
        name=name,
        description="fills spreadsheets",
        file_path=os.path.join(base, "SKILL.md"),
        base_dir=base,
        source="builtin",
        content=open(os.path.join(base, "SKILL.md"), encoding="utf-8").read(),
        frontmatter={},
    )


def entry_for(skill: Skill, metadata: SkillMetadata | None = None) -> SkillEntry:
    return SkillEntry(skill=skill, metadata=metadata or SkillMetadata())


class _ManifestCase(unittest.TestCase):
    def setUp(self):
        from agent.skills import manifest

        self.mod = manifest
        self._tmp = tempfile.TemporaryDirectory(prefix="skill-manifest-")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def manifest(self, skill=None, **kwargs):
        # ``is_authorized`` is required by design; these fixtures are about the
        # package, not about who may deploy it, so they grant it explicitly
        # rather than relying on a default that would hide the check.
        kwargs.setdefault("is_authorized", lambda _sid: True)
        kwargs.setdefault("platform", "posix")
        skill = skill or make_skill(self.root)
        return self.mod.build_skill_manifest(entry_for(skill), **kwargs)


class IdentityTests(_ManifestCase):
    def test_a_manifest_names_the_skill_and_its_content_digest(self):
        built = self.manifest()

        self.assertEqual(built.skill_id, "builtin:excel")
        self.assertEqual(built.name, "excel")
        self.assertTrue(built.digest.startswith("sha256:"))

    def test_every_resource_is_listed_with_its_own_digest_and_size(self):
        built = self.manifest()
        listed = {r.relative_path for r in built.resources}

        self.assertEqual(listed, {"SKILL.md", "scripts/fill.py",
                                  "templates/report.xlsx"})
        for resource in built.resources:
            self.assertTrue(resource.digest.startswith("sha256:"))
            self.assertGreater(resource.size, 0)

    def test_changing_a_script_changes_the_digest(self):
        """A digest over the description alone would miss exactly this."""
        first = self.manifest()
        skill = make_skill(self.root, files={
            "SKILL.md": "---\nname: excel\ndescription: fills spreadsheets\n---\n",
            "scripts/fill.py": "print('MALICIOUS')\n",
            "templates/report.xlsx": "not-a-real-xlsx",
        })
        second = self.manifest(skill)

        self.assertNotEqual(first.digest, second.digest)

    def test_the_digest_does_not_depend_on_directory_listing_order(self):
        """Two machines mounting the same package must agree on its identity."""
        skill = make_skill(self.root)
        first = self.manifest(skill)
        second = self.manifest(skill)

        self.assertEqual(first.digest, second.digest)
        self.assertEqual([r.relative_path for r in first.resources],
                         [r.relative_path for r in second.resources])
        # And the digest is a property of the *set*, not of the order it was
        # assembled in -- the caller cannot make it unstable by reordering.
        self.assertEqual(
            self.mod._content_digest(tuple(reversed(first.resources))),
            self.mod._content_digest(first.resources))
        self.assertEqual([r.relative_path for r in first.resources],
                         sorted(r.relative_path for r in first.resources))

    def test_the_wire_form_carries_what_the_device_needs(self):
        built = self.manifest(dependencies=["openpyxl"], platform="posix")
        wire = built.to_dict()

        self.assertEqual(wire["skill_id"], "builtin:excel")
        self.assertEqual(wire["digest"], built.digest)
        self.assertEqual(wire["platform"], "posix")
        self.assertEqual(wire["dependencies"], ["openpyxl"])
        self.assertEqual(
            sorted(r["relative_path"] for r in wire["resources"]),
            ["SKILL.md", "scripts/fill.py", "templates/report.xlsx"])


class PlatformTests(_ManifestCase):
    def test_the_manifest_records_the_device_platform_not_the_servers(self):
        built = self.manifest(platform="win32")
        self.assertEqual(built.platform, "win32")
        self.assertEqual(built.to_dict()["platform"], "win32")

    def test_a_platform_the_skill_does_not_support_is_refused(self):
        skill = make_skill(self.root)
        metadata = SkillMetadata(os=["darwin"])

        with self.assertRaises(self.mod.SkillManifestError) as caught:
            self.mod.build_skill_manifest(
                entry_for(skill, metadata), platform="win32",
                is_authorized=lambda _sid: True)

        self.assertEqual(caught.exception.code, "platform_unsupported")

    def test_a_platform_the_skill_does_support_is_accepted(self):
        skill = make_skill(self.root)
        metadata = SkillMetadata(os=["darwin"])

        built = self.mod.build_skill_manifest(
            entry_for(skill, metadata), platform="posix",
            is_authorized=lambda _sid: True)
        self.assertEqual(built.platform, "posix")


class DependencyTests(_ManifestCase):
    def test_declared_requirements_become_the_manifests_dependencies(self):
        metadata = SkillMetadata(
            requires={"bins": ["python3"], "python": ["openpyxl"]})
        skill = make_skill(self.root)
        built = self.mod.build_skill_manifest(
            entry_for(skill, metadata), platform="posix",
            is_authorized=lambda _sid: True)

        self.assertIn("openpyxl", built.dependencies)

    def test_a_requirement_that_cannot_apply_here_is_reported(self):
        metadata = SkillMetadata(requires={"bins": ["excel-cli"]})
        skill = make_skill(self.root)
        built = self.mod.build_skill_manifest(
            entry_for(skill, metadata), platform="posix",
            is_authorized=lambda _sid: True,
            which=lambda _name: None)

        self.assertIn("excel-cli", built.missing_dependencies)


class RefusalTests(_ManifestCase):
    def test_a_resource_reaching_outside_the_skill_is_refused(self):
        skill = make_skill(self.root)
        escape = os.path.join(self.root, "outside.txt")
        write(escape, "secret")
        os.symlink(escape, os.path.join(skill.base_dir, "escape.txt"))

        with self.assertRaises(self.mod.SkillManifestError) as caught:
            self.manifest(skill)

        self.assertEqual(caught.exception.code, "link_refused")

    def test_a_symlinked_directory_is_refused_too(self):
        skill = make_skill(self.root)
        os.symlink("/etc", os.path.join(skill.base_dir, "etc"))

        with self.assertRaises(self.mod.SkillManifestError) as caught:
            self.manifest(skill)

        self.assertEqual(caught.exception.code, "link_refused")

    def test_a_credential_file_is_not_packaged_as_skill_content(self):
        skill = make_skill(self.root, files={
            "SKILL.md": "---\nname: excel\ndescription: d\n---\n",
            ".env": "MODEL_API_KEY=secret\n",
        })

        with self.assertRaises(self.mod.SkillManifestError) as caught:
            self.manifest(skill)

        self.assertEqual(caught.exception.code, "secret_refused")

    def test_a_skill_whose_directory_is_gone_is_refused(self):
        skill = make_skill(self.root)
        import shutil
        shutil.rmtree(skill.base_dir)

        with self.assertRaises(self.mod.SkillManifestError) as caught:
            self.manifest(skill)

        self.assertEqual(caught.exception.code, "skill_unavailable")


class AuthorizationTests(_ManifestCase):
    """A manifest is only built for a skill the identity may actually use."""

    def test_building_a_manifest_consults_the_authorized_skill_set(self):
        entry = entry_for(make_skill(self.root))

        with self.assertRaises(self.mod.SkillManifestError) as caught:
            self.mod.build_skill_manifest(
                entry, platform="posix", is_authorized=lambda _sid: False)
        self.assertEqual(caught.exception.code, "not_authorized")

    def test_the_authorization_check_is_required_not_optional(self):
        """Omitting the check must not silently mean "authorized".

        The whole point is that a manifest is a deployment decision; a default of
        "yes" would make every caller that forgot to pass the check a hole.
        """
        entry = entry_for(make_skill(self.root))
        with self.assertRaises(TypeError):
            self.mod.build_skill_manifest(entry, platform="posix")

    def test_building_a_manifest_calls_the_check_every_time(self):
        calls = []
        entry = entry_for(make_skill(self.root))

        def check(skill_id):
            calls.append(skill_id)
            return True

        self.mod.build_skill_manifest(entry, platform="posix", is_authorized=check)
        self.mod.build_skill_manifest(entry, platform="posix", is_authorized=check)

        self.assertEqual(calls, ["builtin:excel", "builtin:excel"])


class BundlingTests(_ManifestCase):
    def test_the_payload_bundle_matches_the_manifest_it_was_built_from(self):
        """The bytes a caller ships are the bytes the digest covers."""
        built = self.manifest()
        payloads = self.mod.read_skill_payloads(built)

        self.assertEqual(sorted(payloads), sorted(r.relative_path for r in built.resources))
        # And a device-side cache accepts them without modification.
        from agent.desktop_local.skill_cache import SkillCache, SkillScope
        cache = SkillCache(os.path.join(self.root, "cache"))
        published = cache.publish(
            SkillScope(origin="https://master.example", tenant_id="t", user_id="u"),
            built.to_dict(), payloads)

        self.assertEqual(published.digest, built.digest)
        self.assertTrue(os.path.isfile(os.path.join(published.path, "SKILL.md")))
        self.assertTrue(os.path.isfile(
            os.path.join(published.path, "scripts", "fill.py")))
