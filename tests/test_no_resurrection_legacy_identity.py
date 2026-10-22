# encoding:utf-8
"""No-resurrection assertions for retired legacy identity symbols."""

from __future__ import annotations

import os
import unittest


from tests._helpers import web_layer_source
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(_REPO, rel), encoding="utf-8") as fh:
        return fh.read()


class NoResurrectionTests(unittest.TestCase):
    def test_web_channel_auth_is_database_only(self):
        from channel.web import web_channel as w

        self.assertTrue(w._is_database_identity())
        # Thin wrappers only — no shared-password login body.
        src = web_layer_source()
        self.assertIn("return DbAuthLoginHandler().POST()", src)
        self.assertNotIn("Wrong password", src)
        self.assertNotIn('web.setcookie("cow_auth_token"', src)

    def test_legacy_auth_helpers_are_absent(self):
        """Retired shared-password / HMAC helpers must not be redefined."""
        src = web_layer_source()
        for name in (
            "def _require_auth",
            "def _get_web_password",
            "def _check_auth",
            "def _create_auth_token",
            "def _verify_auth_token",
            "def _get_query_token",
            "def _get_bearer_token",
            "def _is_password_enabled",
        ):
            self.assertNotIn(name, src, "resurrected helper: %s" % name)

    def test_check_auth_ignores_hmac_and_query_token(self):
        src = web_layer_source()
        # Definition may remain for upstream merge, but _check_auth must not call it.
        start = src.find("def _check_auth")
        if start < 0:
            self.assertNotIn("def _verify_auth_token", src)
            self.assertNotIn("def _get_query_token", src)
            return
        end = src.find("\ndef ", start + 1)
        check = src[start:end]
        self.assertNotIn("_verify_auth_token", check)
        self.assertNotIn("_get_query_token", check)
        self.assertNotIn("_is_password_enabled", check)

    def test_config_default_is_database(self):
        from config import available_setting
        self.assertEqual(available_setting.get("identity_mode"), "database")

    def test_config_template_has_no_shared_password_or_external_api_token(self):
        cfg = _read("config-template.json")
        self.assertNotIn('"web_password"', cfg)
        self.assertNotIn('"external_api_token"', cfg)
        self.assertIn('"identity_mode": "database"', cfg)

    def test_explicit_legacy_refused(self):
        from unittest.mock import patch
        from common import startup_hooks

        with patch("config.conf", return_value={"identity_mode": "legacy"}):
            with self.assertRaises(RuntimeError):
                startup_hooks._identity_mode_consistency()

    def test_is_database_mode_always_true(self):
        from channel.external_identity import is_database_mode
        from agent.permission.isolation import database_mode
        from auth.http_policy import _is_database_mode as policy_db

        self.assertTrue(is_database_mode())
        self.assertTrue(database_mode())
        self.assertTrue(policy_db())

    def test_branding_writes_require_platform_admin_not_shared_password(self):
        src = web_layer_source()
        start = src.find("def _branding_require_write")
        self.assertGreater(start, 0)
        end = src.find("\ndef ", start + 1)
        require = src[start:end]
        self.assertIn("_branding_require_platform_admin", require)
        self.assertNotIn("_branding_auth_token", require)
        self.assertNotIn("_branding_csrf_ok", require)

    def test_branding_and_openai_fixtures_are_database(self):
        branding = _read("tests/test_branding.py")
        openai = _read("tests/test_openai_chat_api.py")
        self.assertNotIn('identity_mode="legacy"', branding)
        # Refuse-only mentions of the retired cookie name are OK; fixtures must
        # not mint or accept cow_auth_token as a valid credential.
        self.assertNotIn('Cookie": "cow_auth_token=" +', branding)
        self.assertNotIn("_create_auth_token", branding)
        self.assertNotIn('identity_mode": "legacy"', openai)
        self.assertIn("identity_mode\": \"database\"", openai)
        self.assertNotIn("web_password", _read("tests/test_todo_tool_identity.py"))
        self.assertNotIn("web_password", _read("tests/test_web_chat_boundary.py"))
        self.assertNotIn("web_password", _read("tests/test_chat_identity_context.py"))

    def test_auth_route_wrappers_remain_database_delegates(self):
        """Keep thin Auth* wrappers; do not REMOVED /auth/* while they exist."""
        registry = _read("channel/web/route_registry.py")
        self.assertIn('RouteEntry("/auth/login", "AuthLoginHandler"', registry)
        self.assertIn("database account login", registry)
        self.assertIn("thin ``Auth*`` wrappers", registry)
        # Live RouteEntry lines must not declare REMOVED policy.
        for line in registry.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("RouteEntry(") and (
                "/auth/login" in line or "/auth/check" in line or "/auth/logout" in line
            ):
                self.assertNotIn("REMOVED", line)

    def test_deploy_scripts_have_no_shared_password(self):
        for rel in (
            "webhelp/assets/deploy/docker-compose.yml",
            "docker/docker-compose.yml",
            "config-template.json",
            "webhelp/config.json",
        ):
            text = _read(rel)
            self.assertNotIn("WEB_PASSWORD:", text)
            self.assertNotIn("web_password", text)
            self.assertNotIn("external_api_token", text)
        self.assertIn("identity_mode", _read("webhelp/config.json"))
        self.assertIn("IDENTITY_MODE", _read("webhelp/assets/deploy/docker-compose.yml"))


if __name__ == "__main__":
    unittest.main()
