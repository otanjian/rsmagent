# encoding:utf-8
"""The Python window the desktop probes for is the window the build enforces.

Change ``harden-windows-desktop-reliability``, task 2.1. Three places state which
interpreters are acceptable:

* ``agent/skills/dependencies.py`` -- ``MIN_PYTHON`` / ``MAX_PYTHON_EXCLUSIVE``,
  the authority: the bundle's build check refuses to package a skill dependency
  set that the shipped interpreter cannot satisfy;
* ``desktop/src/main/python-probe.ts`` -- the same window, applied *before* the
  shell spawns a local backend, so the user gets a specific reason instead of
  "exited during startup (code 9009)";
* ``desktop/build/setup-windows.ps1`` -- the same window again, when preparing a
  Windows environment.

A window that drifts in either direction is a real failure: too permissive and
the backend starts under an interpreter the bundle was never built for; too
strict and a working environment is rejected. So it is asserted, not commented.
"""

import os
import re
import unittest

from agent.skills import dependencies

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(REPO, "desktop", "src", "main", "python-probe.ts")
SETUP = os.path.join(REPO, "desktop", "build", "setup-windows.ps1")


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class ProbeWindowTests(unittest.TestCase):
    def test_the_probe_window_matches_the_skill_dependency_window(self):
        text = _read(PROBE)
        for ts_name, authority in (
            ("MIN_PYTHON", dependencies.MIN_PYTHON),
            ("MAX_PYTHON_EXCLUSIVE", dependencies.MAX_PYTHON_EXCLUSIVE),
        ):
            match = re.search(
                r"export const %s: readonly \[number, number\] = \[(\d+), (\d+)\]" % ts_name,
                text,
            )
            self.assertIsNotNone(match, "%s not found in python-probe.ts" % ts_name)
            found = (int(match.group(1)), int(match.group(2)))
            self.assertEqual(
                found,
                tuple(authority),
                "%s in python-probe.ts is %s but the build check enforces %s"
                % (ts_name, found, tuple(authority)),
            )

    def test_the_windows_preparation_script_uses_the_same_window(self):
        text = _read(SETUP)
        minimum = re.search(r"^\$minMinor\s*=\s*(\d+)", text, re.MULTILINE)
        maximum = re.search(r"^\$maxMinorExclusive\s*=\s*(\d+)", text, re.MULTILINE)
        self.assertIsNotNone(minimum, "setup-windows.ps1 does not state a minimum minor version")
        self.assertIsNotNone(maximum, "setup-windows.ps1 does not state an exclusive maximum")
        self.assertEqual(int(minimum.group(1)), dependencies.MIN_PYTHON[1])
        self.assertEqual(int(maximum.group(1)), dependencies.MAX_PYTHON_EXCLUSIVE[1])

    def test_the_window_is_a_window(self):
        """Guards the assertion above from silently comparing two empty tuples."""
        self.assertLess(dependencies.MIN_PYTHON, dependencies.MAX_PYTHON_EXCLUSIVE)
        self.assertEqual(dependencies.MIN_PYTHON[0], 3)
        self.assertEqual(dependencies.MAX_PYTHON_EXCLUSIVE[0], 3)


if __name__ == "__main__":
    unittest.main()
