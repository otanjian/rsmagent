# encoding:utf-8
"""The packaging guard must run, and must be able to say "no" (8.5).

Two separate failure modes are pinned here, and the second is the one this
repository has already been bitten by:

1. **The check itself is too weak.** A guard that passes everything is worse than
   no guard, because it reads as a guarantee. So: a declared-but-unshipped library
   must fail the build, an unreadable skill must fail it too (its declarations are
   unknown, which is not the same as "none"), and "cannot check" must be a
   *different* exit code from "checked and fine".
2. **The check exists but nothing runs it.** The manifest builder and the version
   cache in this change were both correct-and-uncalled until task 8.4, and this
   repository has shipped cleanup logic that passed its own suite and never
   executed. So the wiring is asserted as text: ``build-backend.sh`` and each
   release workflow must invoke the guard *before* PyInstaller, and ``--lock``
   must point at what that line actually ships.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD = os.path.join(REPO, "desktop", "build", "check-skill-dependencies.py")
LOCK = os.path.join(REPO, "desktop", "build", "requirements-desktop.txt")
BUILD_SH = os.path.join(REPO, "desktop", "build", "build-backend.sh")
WORKFLOWS = {
    "release": os.path.join(REPO, ".github", "workflows", "release.yml"),
    "release-overlay": os.path.join(REPO, ".github", "workflows", "release-overlay.yml"),
    "release-win7": os.path.join(REPO, ".github", "workflows", "release-win7.yml"),
}


def load_guard():
    """Import the guard script by path (its filename is not a module name).

    Registered in ``sys.modules`` before execution on purpose: ``@dataclass``
    resolves annotations through ``sys.modules[cls.__module__]``, which is ``None``
    without it, and Python 3.14 raises rather than tolerating that.
    """
    import sys

    spec = importlib.util.spec_from_file_location("check_skill_dependencies", GUARD)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class GuardScriptTests(unittest.TestCase):
    def setUp(self):
        self.guard = load_guard()
        self._tmp = tempfile.TemporaryDirectory(prefix="dep-guard-")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.skills = os.path.join(self.root, "skills")
        os.makedirs(self.skills, exist_ok=True)

    def write_skill(self, name: str, frontmatter: str) -> str:
        base = os.path.join(self.skills, name)
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, "SKILL.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(frontmatter)
        return path

    def write_lock(self, body: str) -> str:
        path = os.path.join(self.root, "lock.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path

    def run_guard(self, *extra: str) -> int:
        return self.guard.main([
            "--lock", self.lock, "--skills", self.skills,
            "--skip-interpreter-check", *extra,
        ])

    def test_a_satisfiable_set_of_declarations_passes(self):
        self.lock = self.write_lock("openpyxl\n")
        self.write_skill("excel", "---\nname: excel\nmetadata:\n  cowagent:\n"
                                  "    requires:\n      python:\n        - openpyxl\n---\n")

        self.assertEqual(self.run_guard(), self.guard.EXIT_OK)

    def test_a_declared_but_unshipped_library_fails_the_build(self):
        self.lock = self.write_lock("openpyxl\n")
        self.write_skill("rfq-quote", "---\nname: rfq-quote\nmetadata:\n  cowagent:\n"
                                      "    requires:\n      python:\n        - xlsxwriter\n---\n")

        self.assertEqual(self.run_guard(), self.guard.EXIT_PROBLEMS)

    def test_an_unshipped_pip_install_spec_also_fails_the_build(self):
        """The second declarative surface must have teeth too."""
        self.lock = self.write_lock("openpyxl\n")
        self.write_skill("excel", "---\nname: excel\nmetadata:\n  cowagent:\n"
                                  "    install:\n      - kind: pip\n"
                                  "        package: xlsxwriter\n---\n")

        self.assertEqual(self.run_guard(), self.guard.EXIT_PROBLEMS)

    def test_an_unreadable_skill_fails_rather_than_being_skipped(self):
        """Unknown declarations are not the same as no declarations."""
        self.lock = self.write_lock("openpyxl\n")
        base = os.path.join(self.skills, "broken")
        os.makedirs(base, exist_ok=True)
        # A SKILL.md that exists but cannot be decoded: the declarations are
        # unknown, and reporting "ok" there would claim a guarantee we lack.
        with open(os.path.join(base, "SKILL.md"), "wb") as handle:
            handle.write(b"---\nname: \xff\xfe\x00broken\n---\n")

        self.assertEqual(self.run_guard(), self.guard.EXIT_PROBLEMS)

    def test_a_missing_lock_cannot_check_and_says_so(self):
        """"Could not check" must not be reported as "checked and fine"."""
        self.lock = os.path.join(self.root, "does-not-exist.txt")

        self.assertEqual(self.run_guard(), self.guard.EXIT_CANNOT_CHECK)

    def test_no_skills_directory_cannot_check(self):
        self.lock = self.write_lock("openpyxl\n")
        self.skills = os.path.join(self.root, "also-missing")

        self.assertEqual(self.run_guard(), self.guard.EXIT_CANNOT_CHECK)

    def test_the_exit_codes_are_distinct(self):
        self.assertNotEqual(self.guard.EXIT_OK, self.guard.EXIT_PROBLEMS)
        self.assertNotEqual(self.guard.EXIT_PROBLEMS, self.guard.EXIT_CANNOT_CHECK)
        self.assertNotEqual(self.guard.EXIT_OK, self.guard.EXIT_CANNOT_CHECK)

    def test_an_install_spec_for_another_platform_is_not_a_failure_here(self):
        self.lock = self.write_lock("openpyxl\n")
        self.write_skill("win-only", "---\nname: win-only\nmetadata:\n  cowagent:\n"
                                     "    install:\n      - kind: pip\n"
                                     "        package: pywin32\n        os:\n"
                                     "          - win32\n---\n")

        self.assertEqual(self.run_guard(), self.guard.EXIT_OK)


class VerifyImportsTests(unittest.TestCase):
    """The only check with no mapping assumption in it."""

    def setUp(self):
        self.guard = load_guard()

    def _decl(self, kind: str, name: str):
        from agent.skills.dependencies import Declaration

        return Declaration(skill="s", kind=kind, name=name)

    def test_an_importable_module_is_not_reported(self):
        self.assertEqual(self.guard.verify_imports([self._decl("python", "json")]), [])

    def test_an_unimportable_module_is_reported(self):
        missing = self.guard.verify_imports(
            [self._decl("python", "definitely_not_installed_xyz")])

        self.assertEqual(len(missing), 1)
        self.assertIn("definitely_not_installed_xyz", missing[0])

    def test_a_pip_spec_is_left_to_the_lock_check(self):
        """The declared name is a distribution, whose import name is not known."""
        self.assertEqual(
            self.guard.verify_imports([self._decl("pip", "some-distribution")]), [])

    def test_a_dotted_module_is_probed_by_its_root(self):
        self.assertEqual(self.guard.verify_imports([self._decl("python", "os.path")]), [])


class PackagingWiringTests(unittest.TestCase):
    """A guard nothing invokes is not a guard."""

    def test_the_local_build_script_runs_the_guard_before_pyinstaller(self):
        text = read(BUILD_SH)

        guard_at = text.index("check-skill-dependencies.py")
        pyinstaller_at = text.index("pyinstaller \"$BUILD_DIR/cowagent-backend.spec\"")
        self.assertLess(guard_at, pyinstaller_at,
                        "the guard must run before the bundle is built")

    def test_the_local_build_script_verifies_imports(self):
        """It runs inside the isolated venv, so the import check is available."""
        self.assertIn("--verify-imports", read(BUILD_SH))

    def test_the_local_build_script_fails_the_build_on_a_problem(self):
        """``set -e`` is what turns a non-zero guard into a stopped build."""
        self.assertIn("set -euo pipefail", read(BUILD_SH))

    def test_every_release_workflow_runs_the_guard(self):
        for name, path in WORKFLOWS.items():
            with self.subTest(workflow=name):
                text = read(path)
                self.assertIn("check-skill-dependencies.py", text)

    def test_every_release_workflow_runs_it_before_pyinstaller(self):
        for name, path in WORKFLOWS.items():
            with self.subTest(workflow=name):
                text = read(path)
                guard_at = text.index("check-skill-dependencies.py")
                pyinstaller_at = text.index("pyinstaller desktop/build/cowagent-backend.spec")
                self.assertLess(guard_at, pyinstaller_at)

    def test_every_release_workflow_verifies_imports(self):
        for name, path in WORKFLOWS.items():
            with self.subTest(workflow=name):
                self.assertIn("--verify-imports", read(path))

    def test_the_windows_7_line_checks_the_lock_it_actually_installs(self):
        """It rewrites the lock to relax playwright, so the repo lock is not it."""
        text = read(WORKFLOWS["release-win7"])

        self.assertIn("requirements-win7.txt", text)
        guard_region = text[text.index("check-skill-dependencies.py"):]
        self.assertIn("--lock /tmp/requirements-win7.txt", guard_region,
                      "the Win7 line installs a relaxed lock and must check that one")

    def test_the_desktop_lock_still_names_the_file_the_messages_point_at(self):
        """The remediation text names a path; it must be the real one."""
        from agent.skills import dependencies

        self.assertTrue(dependencies.LOCK_PATH_HINT.endswith("requirements-desktop.txt"))
        self.assertTrue(
            os.path.exists(os.path.join(REPO, *dependencies.LOCK_PATH_HINT.split("/"))))

    def test_the_guard_file_exists_where_the_workflows_reference_it(self):
        """The workflows call it by relative path from the repo root."""
        self.assertTrue(os.path.isfile(GUARD))
        for name, path in WORKFLOWS.items():
            with self.subTest(workflow=name):
                self.assertIn("desktop/build/check-skill-dependencies.py", read(path))

    def test_the_guard_is_not_gitignored_so_it_actually_reaches_ci(self):
        """A guard that never reaches the runner is a guard that never ran.

        ``desktop/build/*`` is gitignored wholesale and every tracked source in it
        is allow-listed with its own ``!`` line. Adding a new file there without a
        matching line leaves it untracked: the local build passes, and each release
        workflow then fails on a missing file instead of on the dependency the guard
        exists to catch.
        """
        if not shutil.which("git") or not os.path.isdir(os.path.join(REPO, ".git")):
            self.skipTest("not a git checkout")

        proc = subprocess.run(["git", "check-ignore", "--quiet", GUARD], cwd=REPO)
        # 0 means "ignored"; anything else means git will track it.
        self.assertNotEqual(proc.returncode, 0,
                            "desktop/build/check-skill-dependencies.py is gitignored "
                            "and would never be shipped to CI")


if __name__ == "__main__":
    unittest.main()
