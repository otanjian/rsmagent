# encoding:utf-8
"""Runs the external-connections browser contract (Playwright) when available.

The assertions live in ``tests/test_external_connections_browser.cjs``. Playwright
and a Chromium download are optional in this repository, so the wrapper skips
(instead of failing) when the package cannot be resolved — the same convention
the other browser wrappers use. Set ``NODE_PATH`` to a Playwright install and
``PLAYWRIGHT_BROWSERS_PATH`` to its browser cache to run it.
"""

import os
import shutil
import subprocess
import unittest
from pathlib import Path


def _playwright_available(node: str) -> bool:
    probe = subprocess.run(
        [node, "-e", "require.resolve('playwright')"],
        capture_output=True, text=True,
        env=os.environ.copy(),
    )
    return probe.returncode == 0


class ExternalConnectionsBrowserTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for console behavior tests")
    def test_conditional_auth_fields_appear(self):
        node = shutil.which("node")
        if not _playwright_available(node):
            # ``test_external_connections_browser.cjs`` prints SKIP and exits 0
            # in the same situation; skipping here keeps the reason visible.
            self.skipTest("playwright is not installed (browser pass not run)")
        result = subprocess.run(
            [node, str(Path(__file__).with_name("test_external_connections_browser.cjs"))],
            capture_output=True, text=True, timeout=300,
            env=os.environ.copy(),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("0 failed", result.stdout)


if __name__ == "__main__":
    unittest.main()
