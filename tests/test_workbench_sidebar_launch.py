# encoding:utf-8
"""Validate the refined-sidebar presentation switch (task 4.1).

``workbench_sidebar_launch_v2`` is a temporary, layout-only switch: it decides
whether the Web shell renders the four-zone sidebar with its five-row recent
preview. It must never change authentication, authorization or identity mode,
and it must fall back to off when the value is absent, malformed or a typo —
during development and acceptance the old sidebar is the default.

``ChatHandler.GET`` and ``WebChannel.chat_page`` inject the same validated value
so the client never reads an arbitrary string.

These tests import the real ``web`` module (present in this venv); they patch
only the header call and the config value, so no request context is required.
"""

import os
import sys
import unittest
from unittest.mock import mock_open, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _import_wc():
    import channel.web.web_channel as wc
    return wc


class TestWorkbenchSidebarLaunchSwitch(unittest.TestCase):
    def _value_for(self, config_value):
        web_channel = _import_wc()
        # Only set the key when a value is supplied so we can test "absent".
        effective = {} if config_value is None else {
            "workbench_sidebar_launch_v2": config_value}
        with patch.object(web_channel, "conf", lambda: effective):
            return web_channel._workbench_sidebar_launch_v2()

    def test_defaults_to_off_when_absent(self):
        self.assertEqual(self._value_for(None), "0")

    def test_accepts_boolean_and_string_truthy_values(self):
        for value in (True, "true", "TRUE", " 1 ", "yes", "on"):
            self.assertEqual(self._value_for(value), "1", msg=f"value={value!r}")

    def test_rejects_everything_else(self):
        # Including the spellings the evolution config reads as *false*: this
        # switch is off unless the value is explicitly true, so a typo can
        # never half-enable the new layout.
        for value in (False, "", "0", "false", "no", "off", "bogus", "2", "true1"):
            self.assertEqual(self._value_for(value), "0", msg=f"value={value!r}")

    def test_a_layout_switch_never_touches_authorization_state(self):
        # The reader consults one config key and returns a presentation value;
        # it must not read or write identity mode, tenants or grants. Only the
        # code is scanned — the docstring is allowed to *name* what it gates.
        import inspect

        web_channel = _import_wc()
        parts = inspect.getsource(web_channel._workbench_sidebar_launch_v2).split('"""')
        code = parts[2] if len(parts) >= 3 else parts[0]
        for forbidden in ("_identity_mode", "tenant", "grant", "authorization",
                          "_require_agent", "roster"):
            self.assertNotIn(forbidden, code, msg=f"must not touch {forbidden}")

    def _chat_handler_output(self, config_value):
        from channel.web.web_channel import ChatHandler

        web_channel = _import_wc()
        effective = {} if config_value is None else {
            "workbench_sidebar_launch_v2": config_value}
        html = "<html>{{COW_DEFAULT_LANG}}/{{COW_NAVIGATION_MODE}}/{{COW_WORKBENCH_SIDEBAR_LAUNCH_V2}}</html>"
        with patch.object(web_channel, "conf", lambda: effective):
            with patch("builtins.open", mock_open(read_data=html)):
                with patch.object(web_channel.web, "header", lambda *a, **k: None):
                    return ChatHandler().GET()

    def test_chat_handler_injects_the_enabled_value(self):
        out = self._chat_handler_output(True)
        self.assertIn("/1</html>", out)
        self.assertNotIn("{{COW_WORKBENCH_SIDEBAR_LAUNCH_V2}}", out)

    def test_chat_handler_injects_off_for_invalid_values(self):
        out = self._chat_handler_output("bogus")
        self.assertIn("/0</html>", out)
        self.assertNotIn("{{COW_WORKBENCH_SIDEBAR_LAUNCH_V2}}", out)

    def test_chat_page_injects_the_same_value(self):
        from channel.web.fork.runtime import WebChannel

        web_channel = _import_wc()
        html = "<html>{{COW_DEFAULT_LANG}}/{{COW_NAVIGATION_MODE}}/{{COW_WORKBENCH_SIDEBAR_LAUNCH_V2}}</html>"
        for config_value, expected in ((True, "/1</html>"), (None, "/0</html>")):
            effective = {} if config_value is None else {
                "workbench_sidebar_launch_v2": config_value}
            with patch.object(web_channel, "conf", lambda: effective):
                with patch.object(web_channel.i18n, "get_language", lambda: "zh"):
                    with patch("builtins.open", mock_open(read_data=html)):
                        out = WebChannel().chat_page()
            self.assertIn(expected, out)
            self.assertNotIn("{{COW_WORKBENCH_SIDEBAR_LAUNCH_V2}}", out)

    def test_the_shell_declares_the_switch_before_console_js_runs(self):
        # The console reads the value from the page, so the inline script must
        # declare it ahead of the deferred console script.
        html_path = os.path.join(
            os.path.dirname(__file__), "..", "channel", "web", "chat.html")
        with open(html_path, "r", encoding="utf-8") as handle:
            shell = handle.read()
        declared = shell.index("__COW_WORKBENCH_SIDEBAR_LAUNCH_V2__")
        console = shell.index("assets/js/console.js")
        self.assertLess(declared, console)
        self.assertIn("{{COW_WORKBENCH_SIDEBAR_LAUNCH_V2}}", shell)


if __name__ == "__main__":
    unittest.main()
