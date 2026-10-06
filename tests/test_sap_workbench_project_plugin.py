# encoding:utf-8
"""Runs the project-plugin contract (node:test).

The assertions live in ``tests/test_sap_workbench_project_plugin.cjs``; this
wrapper runs them with the rest of the suite. Node is optional in this
repository, so the test is skipped rather than failing when it is absent (same
convention as the other ``*_frontend.py`` wrappers).
"""
import shutil
import subprocess
import unittest
from pathlib import Path


class ProjectPluginTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the plugin contract")
    def test_plugin_behavior(self):
        result = subprocess.run(
            [shutil.which("node"), "--test",
             str(Path(__file__).with_name("test_sap_workbench_project_plugin.cjs"))],
            capture_output=True, timeout=90,
            # node prints its own status glyphs, and the console codec on a
            # Chinese Windows host is not UTF-8; decoding with the locale codec
            # raises inside the reader thread and leaves stdout as None.
            encoding="utf-8", errors="replace",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
