# encoding:utf-8
"""Declared skill dependencies vs what the desktop bundle actually ships (8.5).

Two declarative mechanisms already exist and neither is enforced:

* ``metadata.requires`` (``bins`` / ``anyBins`` / ``env`` / ``anyEnv``, and any
  other kind the frontmatter names) -- used for *enablement*, so a skill with a
  missing binary is hidden from the model. That is the right behaviour for an
  optional capability, and the wrong one for a library the skill's script calls
  unconditionally.
* ``metadata.install`` (:class:`SkillInstallSpec`: ``brew`` / ``pip`` / ``npm`` /
  ``download``) -- **parsed and then never consumed by anything.** A skill can
  declare exactly how to obtain what it needs and no code acts on it.

Meanwhile the desktop bundle's library set is maintained by hand in
``desktop/build/requirements-desktop.txt`` and checked against nothing. The
concrete consequence, which this file pins: the representative business skill
``rfq-quote`` writes its deliverables through ``xlsxwriter``
(``skills/rfq-quote/scripts/quote.py``, imported inside ``Report.__init__``), and
``xlsxwriter`` is **not** in the desktop requirements. The import is lazy, so the
failure lands at output time, inside a sandboxed subprocess, as an opaque
``ModuleNotFoundError`` -- after the model has reported success.

The distinction this file is built around:

* a **declared and shipped** dependency is fine;
* a **declared but unshipped** one is ``dependency_missing`` -- an error naming the
  skill, the module and the distribution to add;
* a **declared module with no known distribution** is ``dependency_undeclared``.
  Passing it would be the silent-skip failure: the check cannot know whether it is
  shipped, so it must say so rather than assume the best.
"""

from __future__ import annotations

import os
import textwrap
import unittest

from agent.skills.types import Skill, SkillEntry, SkillInstallSpec, SkillMetadata

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_PATH = os.path.join(REPO, "desktop", "build", "requirements-desktop.txt")


def read_lock() -> str:
    with open(LOCK_PATH, encoding="utf-8") as handle:
        return handle.read()


def entry_for(name: str, *, requires=None, install=None, base_dir=None) -> SkillEntry:
    skill = Skill(name=name, description="d", file_path="/dev/null",
                  base_dir=base_dir or "/dev/null", source="builtin",
                  content="", frontmatter={})
    return SkillEntry(skill=skill,
                      metadata=SkillMetadata(requires=dict(requires or {}),
                                             install=list(install or [])))


class LockParsingTests(unittest.TestCase):
    def setUp(self):
        from agent.skills import dependencies

        self.mod = dependencies

    def test_a_plain_requirement_is_recognised(self):
        lock = self.mod.parse_lock("requests>=2.28.2\n")

        self.assertIn("requests", lock)

    def test_a_name_is_normalised_case_and_separator_insensitively(self):
        """``PyYAML`` and ``pyyaml`` are the same distribution."""
        lock = self.mod.parse_lock("PyYAML>=6.0\npython-docx\n")

        self.assertIn("pyyaml", lock)
        self.assertIn("pythondocx", lock)

    def test_comments_and_blank_lines_are_ignored(self):
        lock = self.mod.parse_lock("# a note\n\nrequests\n  # indented note\n")

        self.assertEqual(sorted(lock), ["requests"])

    def test_an_environment_marker_line_is_still_a_shipped_distribution(self):
        """A marker means "on some Pythons"; it is still declared as shipped."""
        lock = self.mod.parse_lock('aiohttp>=3.10; python_version >= "3.13"\n')

        self.assertIn("aiohttp", lock)

    def test_an_extras_requirement_is_recognised_by_its_base_name(self):
        lock = self.mod.parse_lock("uvicorn[standard]>=0.20\n")

        self.assertIn("uvicorn", lock)

    def test_a_direct_reference_is_recognised_by_its_base_name(self):
        lock = self.mod.parse_lock(
            "web.py @ git+https://github.com/webpy/webpy.git\n")

        self.assertIn("webpy", lock)

    def test_the_real_lock_parses(self):
        lock = self.mod.parse_lock(read_lock())

        for expected in ("requests", "openpyxl", "pyyaml", "pillow", "playwright"):
            with self.subTest(dist=expected):
                self.assertIn(expected, lock)


class ModuleMappingTests(unittest.TestCase):
    def setUp(self):
        from agent.skills import dependencies

        self.mod = dependencies

    def test_the_well_known_mismatches_are_mapped(self):
        for module, distribution in (
            ("yaml", "pyyaml"),
            ("PIL", "pillow"),
            ("docx", "pythondocx"),
            ("pptx", "pythonpptx"),
            ("dotenv", "pythondotenv"),
            ("Crypto", "pycryptodome"),
        ):
            with self.subTest(module=module):
                self.assertEqual(self.mod.module_distribution(module), distribution)

    def test_an_identical_name_maps_to_itself(self):
        self.assertEqual(self.mod.module_distribution("requests"), "requests")

    def test_a_dotted_submodule_maps_by_its_root(self):
        """``google.generativeai`` is provided by the ``google-generativeai`` dist."""
        self.assertEqual(self.mod.module_distribution("google.generativeai"),
                         "googlegenerativeai")

    def test_an_unknown_module_is_none_not_a_guess(self):
        """Returning the bare name would make an unverifiable dep look shipped."""
        self.assertIsNone(self.mod.module_distribution("some_unknown_module_xyz"))


class DeclarationTests(unittest.TestCase):
    def setUp(self):
        from agent.skills import dependencies

        self.mod = dependencies

    def test_requires_python_modules_are_declared(self):
        found = self.mod.declarations(
            entry_for("excel", requires={"python": ["xlsxwriter"]}))

        self.assertEqual([(d.kind, d.name) for d in found], [("python", "xlsxwriter")])

    def test_install_pip_package_is_declared_as_a_distribution(self):
        found = self.mod.declarations(entry_for(
            "excel", install=[SkillInstallSpec(kind="pip", package="xlsxwriter")]))

        self.assertEqual([(d.kind, d.name) for d in found], [("pip", "xlsxwriter")])

    def test_an_install_spec_for_another_platform_is_not_a_requirement_here(self):
        found = self.mod.declarations(
            entry_for("excel", install=[SkillInstallSpec(
                kind="brew", formula="foo", os=["darwin"])]),
            platform="win32")

        self.assertEqual(found, [])

    def test_bins_are_declared_too_so_the_check_can_report_them(self):
        found = self.mod.declarations(entry_for("excel", requires={"bins": ["curl"]}))

        self.assertEqual([(d.kind, d.name) for d in found], [("bins", "curl")])


class CheckTests(unittest.TestCase):
    def setUp(self):
        from agent.skills import dependencies

        self.mod = dependencies

    def test_a_shipped_dependency_produces_no_problem(self):
        problems = self.mod.check_declarations(
            self.mod.declarations(entry_for("excel", requires={"python": ["openpyxl"]})),
            self.mod.parse_lock("openpyxl\n"))

        self.assertEqual(problems, [])

    def test_a_declared_but_unshipped_dependency_is_reported_by_name(self):
        problems = self.mod.check_declarations(
            self.mod.declarations(entry_for("rfq-quote", requires={"python": ["xlsxwriter"]})),
            self.mod.parse_lock("openpyxl\n"))

        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0].code, "dependency_missing")
        self.assertEqual(problems[0].skill, "rfq-quote")
        self.assertIn("xlsxwriter", problems[0].message)
        # The message must be actionable: which line to add, and where.
        self.assertIn("requirements-desktop.txt", problems[0].message)

    def test_an_unmappable_module_is_reported_rather_than_assumed_shipped(self):
        """The silent-skip failure: "cannot tell" must not read as "fine"."""
        problems = self.mod.check_declarations(
            self.mod.declarations(entry_for("odd", requires={"python": ["totally_unknown_xyz"]})),
            self.mod.parse_lock("openpyxl\n"))

        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0].code, "dependency_undeclared")
        self.assertIn("totally_unknown_xyz", problems[0].message)

    def test_a_missing_binary_is_reported_when_a_probe_is_given(self):
        """Binaries are the machine's business, so they need a ``which`` probe.

        Without one they are *not* reported -- an absent probe means "unknown",
        and this check must not turn unknown into either pass or fail. With one,
        they get the same "required dependency absent" code as a library, and a
        message that says which kind it was.
        """
        problems = self.mod.check_declarations(
            self.mod.declarations(entry_for("scraper", requires={"bins": ["definitely-not-here-xyz"]})),
            self.mod.parse_lock("openpyxl\n"),
            which=lambda _name: None)

        self.assertEqual(problems[0].code, "dependency_missing")
        self.assertIn("definitely-not-here-xyz", problems[0].message)

    def test_a_present_binary_is_not_a_problem(self):
        problems = self.mod.check_declarations(
            self.mod.declarations(entry_for("scraper", requires={"bins": ["curl"]})),
            self.mod.parse_lock("openpyxl\n"),
            which=lambda _name: "/usr/bin/curl")

        self.assertEqual(problems, [])

    def test_a_binary_without_a_probe_is_left_alone(self):
        problems = self.mod.check_declarations(
            self.mod.declarations(entry_for("scraper", requires={"bins": ["curl"]})),
            self.mod.parse_lock("openpyxl\n"))

        self.assertEqual(problems, [])

    def test_an_install_pip_package_absent_from_the_lock_is_reported(self):
        problems = self.mod.check_declarations(
            self.mod.declarations(entry_for(
                "excel", install=[SkillInstallSpec(kind="pip", package="xlsxwriter")])),
            self.mod.parse_lock("openpyxl\n"))

        self.assertEqual(problems[0].code, "dependency_missing")
        self.assertIn("xlsxwriter", problems[0].message)

    def test_the_same_library_declared_on_both_surfaces_is_reported_once(self):
        """``requires.python`` and ``install: pip`` name the same defect.

        Both declarations are meaningful -- one says the script imports it, the
        other says which distribution provides it -- but a doubled message reads
        like two separate problems and sends the reader looking for a second one.
        """
        entry = entry_for("rfq-quote",
                          requires={"python": ["xlsxwriter"]},
                          install=[SkillInstallSpec(kind="pip", package="xlsxwriter")])

        problems = self.mod.check_declarations(
            self.mod.declarations(entry), self.mod.parse_lock("openpyxl\n"))

        self.assertEqual(len(problems), 1, [p.message for p in problems])
        self.assertEqual(problems[0].code, "dependency_missing")

    def test_two_genuinely_different_missing_libraries_are_both_reported(self):
        entry = entry_for("excel", requires={"python": ["xlsxwriter", "openpyxl"]})

        problems = self.mod.check_declarations(
            self.mod.declarations(entry), self.mod.parse_lock("numpy\n"))

        self.assertEqual(len(problems), 2)
        self.assertEqual({p.name for p in problems}, {"xlsxwriter", "openpyxl"})

    def test_a_requires_module_and_a_pip_spec_for_the_same_distro_dedupe(self):
        """Different names, one distribution: ``yaml`` is ``PyYAML``."""
        entry = entry_for("wiki",
                          requires={"python": ["yaml"]},
                          install=[SkillInstallSpec(kind="pip", package="PyYAML")])

        problems = self.mod.check_declarations(
            self.mod.declarations(entry), self.mod.parse_lock("numpy\n"))

        self.assertEqual(len(problems), 1, [p.message for p in problems])


class PythonVersionTests(unittest.TestCase):
    def setUp(self):
        from agent.skills import dependencies

        self.mod = dependencies

    def test_the_supported_version_is_declared_not_implied(self):
        self.assertTrue(self.mod.MIN_PYTHON, "the minimum must be a declared fact")
        self.assertTrue(self.mod.MAX_PYTHON_EXCLUSIVE)

    def test_a_version_below_the_minimum_is_incompatible(self):
        problems = self.mod.check_python_version((3, 7))

        self.assertEqual(problems[0].code, "python_incompatible")

    def test_a_supported_version_is_fine(self):
        self.assertEqual(self.mod.check_python_version(self.mod.MIN_PYTHON), [])

    def test_a_version_at_the_exclusive_ceiling_is_incompatible(self):
        problems = self.mod.check_python_version(self.mod.MAX_PYTHON_EXCLUSIVE)

        self.assertEqual(problems[0].code, "python_incompatible")


class RepresentativeSkillTests(unittest.TestCase):
    """The invariant, over the repository's real skills and real lock.

    This is the test that has teeth: it is what would have caught the shipped
    ``xlsxwriter`` gap before a customer did, and it keeps a future skill from
    silently depending on a library the bundle does not carry.
    """

    def setUp(self):
        from agent.skills import dependencies

        self.mod = dependencies
        self.lock = self.mod.parse_lock(read_lock())

    def test_rfq_quote_declares_the_library_its_report_writer_needs(self):
        """The declaration half of the fix: the skill says what it needs."""
        from agent.skills.manager import SkillManager

        manager = SkillManager(
            builtin_dir=os.path.join(REPO, "skills"),
            custom_dir=os.path.join(REPO, "does-not-exist"))
        entry = manager.skills.get("rfq-quote")
        self.assertIsNotNone(entry, "the representative skill must load")

        declared = {d.name for d in self.mod.declarations(entry)}
        self.assertIn("xlsxwriter", declared,
                      "rfq-quote writes its deliverables with xlsxwriter and must declare it")

    def test_the_library_rfq_quote_needs_is_actually_shipped(self):
        """The shipping half: the desktop lock must carry it."""
        self.assertIn("xlsxwriter", self.lock)

    def test_the_confirmed_import_is_the_one_the_skill_declares(self):
        """Pin the fact, so the declaration cannot drift from the code."""
        script = os.path.join(REPO, "skills", "rfq-quote", "scripts", "quote.py")
        with open(script, encoding="utf-8") as handle:
            text = handle.read()

        self.assertIn("import xlsxwriter", text,
                      "the test's premise: quote.py imports xlsxwriter")

    def test_every_declared_dependency_of_every_shipped_skill_is_shipped(self):
        """The whole-repository guard, as an assertion rather than a script run.

        A skill that needs something the bundle lacks is a support ticket waiting
        to happen; the fix is either to declare it correctly or to ship it.
        """
        from agent.skills.manager import SkillManager

        manager = SkillManager(
            builtin_dir=os.path.join(REPO, "skills"),
            custom_dir=os.path.join(REPO, "does-not-exist"))
        self.assertTrue(manager.skills, "the builtin skills must load")

        problems = []
        for entry in manager.skills.values():
            found = self.mod.declarations(entry)
            problems.extend(self.mod.check_declarations(
                found, self.lock, python_version=self.mod.MIN_PYTHON))
        self.assertEqual(
            problems, [],
            "a shipped skill needs a library the desktop bundle does not carry: "
            + "; ".join(f"{p.skill}:{p.name}({p.code})" for p in problems))

    def test_the_scene_requirements_are_not_a_substitute_for_the_desktop_lock(self):
        """``requirements-scenes.txt`` is a separate, server-side install.

        Treating it as "already shipped" would be exactly the silent assumption
        this check exists to prevent: it is installed on the server, and the
        desktop bundle is built from ``requirements-desktop.txt`` alone.
        """
        scene_path = os.path.join(REPO, "requirements-scenes.txt")
        self.assertTrue(os.path.exists(scene_path))
        scene = self.mod.parse_lock(open(scene_path, encoding="utf-8").read())

        # `xlrd` is a good witness: it is in the scene set and not the desktop
        # set, and the two must not be conflated.
        self.assertIn("xlrd", scene)
        self.assertNotIn("xlrd", self.lock)


if __name__ == "__main__":
    unittest.main()
