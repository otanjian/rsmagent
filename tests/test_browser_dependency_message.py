# encoding:utf-8
"""A missing playwright *module* is reported as a missing module.

Change ``harden-windows-desktop-reliability``, task 2.2. The first-use engine
check already existed; what it did not do was tell a missing **package** apart
from a missing **browser**. Those have different fixes -- one is "finish the
environment / reinstall the app", the other is "download a browser engine" --
and only one of them can be done from inside the chat. The verification report
found this machine with ``playwright`` missing from the interpreter
(docs/design/windows-desktop-verification-2026-10-13.md section 4.6), where the
old message would have sent the user after the wrong problem.

The check reuses ``capability_summary()``'s existing ``has_playwright`` field:
no new dependency registry, no scan of the tool set.
"""

import unittest
from unittest import mock

from agent.tools.browser.browser_tool import BrowserTool


def _summary(has_playwright, ready=False):
    return {
        "ready": ready,
        "has_playwright": has_playwright,
        "has_system_chrome": False,
        "has_downloaded_chromium": False,
        "is_frozen": False,
        "is_desktop": True,
        "engine": {"mode": "none" if not ready else "system-chrome", "has_playwright": has_playwright},
    }


class MissingPlaywrightMessageTests(unittest.TestCase):
    def _check(self, summary):
        tool = BrowserTool({})
        with mock.patch(
            "agent.tools.browser.browser_env.capability_summary", return_value=summary
        ):
            return tool._check_engine_ready()

    def test_a_missing_package_is_named_as_a_missing_module(self):
        result = self._check(_summary(has_playwright=False))
        self.assertIsNotNone(result, "the tool must not proceed without playwright")
        message = str(result.result)
        self.assertIn("playwright", message)
        self.assertIn("module", message.lower())
        self.assertIn("desktop/build/requirements-desktop.txt", message,
                      "a development checkout needs the file that lists the dependency")

    def test_a_missing_package_does_not_ask_for_a_browser_download(self):
        result = self._check(_summary(has_playwright=False))
        message = str(result.result)
        self.assertNotIn("/install-browser", message,
                         "downloading an engine cannot fix a missing Python module")
        self.assertNotIn("cow install-browser", message)

    def test_a_missing_browser_still_gets_the_onboarding_message(self):
        result = self._check(_summary(has_playwright=True, ready=False))
        self.assertIsNotNone(result)
        self.assertIn("/install-browser", str(result.result),
                      "with playwright present the remaining problem really is the engine")

    def test_a_ready_engine_proceeds(self):
        self.assertIsNone(self._check(_summary(has_playwright=True, ready=True)))


if __name__ == "__main__":
    unittest.main()
