# encoding:utf-8
"""The session model selector must only ever offer/keep authorized models.

Two leaks are covered:

* ``GET /api/sessions/<id>/settings`` ran outside a DB scope, so
  ``_authorized_model_codes()`` saw an empty identity and reported the whole
  catalog as unrestricted even in ``database`` identity mode.
* the matching ``POST`` did its write and echoed state outside a DB scope, so a
  chosen model was stored under the default agent's workspace instead of the
  tenant's shared root and came back as an empty, unselectable state.
* an inherited default (session pin / Agent / role / global) pointing at an
  un-authorized model was still reported as the effective model.
"""

import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ``web`` (vendored bottle) is a runtime dependency. Prefer the real package;
# only stub it when truly unavailable so importing web_channel never clobbers
# the real module for the rest of the test process.
try:
    import web  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - environment fallback
    _web = types.ModuleType("web")
    _web.HTTPError = type("HTTPError", (Exception,), {})
    _web.cookies = lambda: {}
    _web.header = lambda *a, **k: None
    _web.data = lambda: b"{}"
    _web.input = lambda **k: types.SimpleNamespace(**k)
    _web.setcookie = lambda *a, **k: None
    _web.seeother = lambda *a, **k: Exception("seeother")
    _web.notfound = lambda *a, **k: Exception("notfound")
    _web.badrequest = lambda *a, **k: Exception("badrequest")
    _web.application = lambda *a, **k: types.SimpleNamespace(wsgifunc=lambda: None)
    _web.httpserver = types.SimpleNamespace(
        LogMiddleware=type("LogMiddleware", (), {"log": lambda *a, **k: None}),
        StaticMiddleware=lambda app: app,
        WSGIServer=lambda *a, **k: types.SimpleNamespace(serve_forever=lambda: None),
    )
    sys.modules["web"] = _web

from auth.service import IdentityService


def _db_path():
    return os.path.join(tempfile.mkdtemp(), "identity.db")


def _seed(svc):
    svc.bootstrap(tenant_code="acme", tenant_name="Acme", admin_username="root",
                  admin_display="Root", admin_password="Str0ngAdminPass",
                  shared_root="/s/acme")
    root = [u for u in svc.list_platform_users() if u["username"] == "root"][0]
    tenant = svc.list_tenants()[0]
    return root, tenant


def _member_with_model_grant(svc, root, tenant, code, key="deepseek"):
    # The tenant ceiling comes first: ``tenant_resource_grants`` bounds what the
    # tenant may allocate at all, so a role grant is only effective inside it
    # (see ``IdentityService.resource_ids_for``).
    svc.set_tenant_resource_grants(
        actor_user_id=root["id"], tenant_id=tenant["id"],
        grants=[
            {"resource_kind": "model", "resource_id": f"provider:{key}:{code}",
             "action": "read"},
            {"resource_kind": "model", "resource_id": f"provider:{key}:{code}",
             "action": "use"},
        ],
        expected_version=tenant["version"])
    role = svc.create_role(
        root["id"], tenant["id"], "modeler", "Modeler", ["model.read", "model.use"],
        resource_grants=[
            {"resource_kind": "model", "resource_id": f"provider:{key}:{code}",
             "action": "read"},
            {"resource_kind": "model", "resource_id": f"provider:{key}:{code}",
             "action": "use"},
        ])
    member = svc.create_member(
        actor_user_id=root["id"], tenant_id=tenant["id"], operation="create-new",
        username="plainuser", display_name="Plain",
        temporary_password="TmpPass123!", roles=[role["code"]])
    return svc._find_user_by_id(member["user_id"])


CATALOG = [{
    "id": "deepseek",
    "label": {"zh": "DeepSeek", "en": "DeepSeek"},
    "models": ["deepseek-v4-flash", "deepseek-v4-flash-vision-exp", "deepseek-v4-pro"],
}]


class AuthorizedModelCodesTests(unittest.TestCase):
    def test_database_mode_without_identity_fails_closed(self):
        from common.runtime_identity import EMPTY_IDENTITY, use_identity
        from channel.web import web_channel

        with mock.patch.object(web_channel, "conf",
                               return_value={"identity_mode": "database"}):
            with use_identity(EMPTY_IDENTITY):
                self.assertEqual(web_channel._authorized_model_codes(), set())

    def test_missing_identity_fails_closed_even_with_legacy_conf_pin(self):
        """Explicit legacy conf must not restore unrestricted model catalog."""
        from common.runtime_identity import EMPTY_IDENTITY, use_identity
        from channel.web import web_channel

        with mock.patch.object(web_channel, "conf",
                               return_value={"identity_mode": "legacy"}):
            with use_identity(EMPTY_IDENTITY):
                self.assertEqual(web_channel._authorized_model_codes(), set())


class SessionSettingsScopeTests(unittest.TestCase):
    def setUp(self):
        import auth.service as asvc
        from channel.web import web_channel

        self.asvc = asvc
        self.web_channel = web_channel
        self.db_path = _db_path()
        self.svc = IdentityService(self.db_path)
        root, tenant = _seed(self.svc)
        self.root = root
        self.tenant = tenant
        self.member = _member_with_model_grant(
            self.svc, root, tenant, "deepseek-v4-flash")
        self.config = {
            "identity_mode": "database",
            "identity_db_path": self.db_path,
            "model": "deepseek-v4-flash-vision-exp",
            "bot_type": "deepseek",
        }

    def _state(self, prefs=None):
        from agent.workspace import session_prefs
        from common.runtime_identity import RuntimeIdentity, use_identity

        wc = self.web_channel
        with mock.patch.object(wc, "conf", return_value=self.config), \
                mock.patch.object(wc, "_session_model_catalog", return_value=CATALOG), \
                mock.patch.object(session_prefs, "get_prefs",
                                  return_value=dict(prefs or {})), \
                mock.patch.object(self.asvc, "get_identity_service",
                                  return_value=self.svc), \
                use_identity(RuntimeIdentity(user_id=self.member["id"],
                                             tenant_id=self.tenant["id"])):
            return wc._session_settings_state("s1", None)

    def _offered(self, state):
        return [m for group in state["model"]["providers"]
                for m in group.get("models", [])]

    def test_only_granted_model_is_offered(self):
        state = self._state()
        self.assertEqual(self._offered(state), ["deepseek-v4-flash"])

    def test_model_removed_from_the_tenant_allocation_is_not_offered(self):
        """The picker must not offer a model the platform took out of the tenant.

        Narrowing ``tenant_resource_grants`` does not rewrite the roles that held
        a wider set, so the stale ``role_resource_grants`` row would otherwise
        keep the model in the menu — the tenant admin sees a model the tenant was
        never allocated (and can no longer allocate).
        """
        self.svc.set_tenant_resource_grants(
            actor_user_id=self.root["id"], tenant_id=self.tenant["id"],
            grants=[{"resource_kind": "model",
                     "resource_id": "provider:deepseek:deepseek-v4-flash-vision-exp",
                     "action": "use"}],
            expected_version=self.svc.get_tenant(self.tenant["id"])["version"])
        state = self._state()
        self.assertEqual(self._offered(state), [],
                         "a model outside the tenant allocation is not offered")
        self.assertTrue(state["model"].get("selection_required"))

    def test_session_pin_within_grants_is_kept(self):
        state = self._state({"model": "deepseek-v4-flash", "provider": "deepseek"})
        self.assertFalse(state["model"].get("selection_required"))
        self.assertEqual(state["model"]["model"], "deepseek-v4-flash")
        self.assertIn("deepseek-v4-flash", self._offered(state))

    def test_session_pin_outside_grants_requires_reselection(self):
        state = self._state({"model": "deepseek-v4-pro", "provider": "deepseek"})
        self.assertTrue(state["model"].get("selection_required"))
        self.assertFalse(state["model"]["model"])
        self.assertEqual(self._offered(state), ["deepseek-v4-flash"])

    def test_inherited_global_model_outside_grants_requires_reselection(self):
        state = self._state()
        self.assertTrue(state["model"].get("selection_required"))
        self.assertFalse(state["model"]["model"])
        self.assertEqual(self._offered(state), ["deepseek-v4-flash"])

    def test_database_mode_permission_is_owned_by_roles(self):
        """A session pin must not be offered as a self-service knob: in
        database mode the role's tool.execute grant governs execution."""
        state = self._state({"permission": "read-only"})
        self.assertEqual(state["permission"]["source"], "role")
        self.assertEqual(state["permission"]["modes"], [])

    def test_platform_admin_keeps_full_catalog(self):
        from common.runtime_identity import RuntimeIdentity, use_identity

        root = [u for u in self.svc.list_platform_users()
                if u["username"] == "root"][0]
        wc = self.web_channel
        with mock.patch.object(wc, "conf", return_value=self.config), \
                mock.patch.object(wc, "_session_model_catalog", return_value=CATALOG), \
                mock.patch.object(self.asvc, "get_identity_service",
                                  return_value=self.svc), \
                use_identity(RuntimeIdentity(user_id=root["id"],
                                             tenant_id=self.tenant["id"])):
            state = wc._session_settings_state("s1", None)
        self.assertEqual(state["model"]["model"], "deepseek-v4-flash-vision-exp")
        self.assertEqual(len(self._offered(state)), 3)


class _RequestCtx:
    is_platform_admin = False
    is_tenant_admin = False

    def __init__(self, user_id, tenant_id):
        self.user_id = user_id
        self.tenant_id = tenant_id


class SessionSettingsPostScopeTests(unittest.TestCase):
    """``POST /api/sessions/<id>/settings`` must run inside the DB scope.

    When the write and the echoed state ran outside ``_db_scope``, the session
    pin fell back to the default agent's workspace (not the tenant's shared
    root), so the granted model never stuck from the tenant's point of view.
    """

    def setUp(self):
        import auth.service as asvc
        from channel.web import web_channel

        self.asvc = asvc
        self.web_channel = web_channel
        self.db_path = _db_path()
        self.svc = IdentityService(self.db_path)
        root, tenant = _seed(self.svc)
        self.tenant = tenant
        self.member = _member_with_model_grant(
            self.svc, root, tenant, "deepseek-v4-flash")
        self.config = {
            "identity_mode": "database",
            "identity_db_path": self.db_path,
            "model": "deepseek-v4-flash-vision-exp",
            "bot_type": "deepseek",
        }
        base = tempfile.mkdtemp()
        self.tenant_root = Path(base) / "tenant"
        self.global_root = Path(base) / "global"
        self.tenant_root.mkdir()
        self.global_root.mkdir()

    @contextmanager
    def _fake_db_scope(self):
        from common.runtime_identity import RuntimeIdentity, use_identity

        with use_identity(RuntimeIdentity(user_id=self.member["id"],
                                          tenant_id=self.tenant["id"])):
            yield _RequestCtx(self.member["id"], self.tenant["id"])

    def _fake_shared_root(self):
        from common.runtime_identity import current_identity

        ident = current_identity()
        return self.tenant_root if ident.tenant_id else self.global_root

    def _post(self, body):
        import common.state_dir as state_dir
        from channel.web import web_channel

        with mock.patch.object(web_channel, "conf", return_value=self.config), \
                mock.patch.object(web_channel, "_session_model_catalog",
                                  return_value=CATALOG), \
                mock.patch.object(web_channel, "_db_scope", self._fake_db_scope), \
                mock.patch.object(web_channel.web, "data",
                                  lambda: json.dumps(body).encode()), \
                mock.patch.object(web_channel.web, "header", lambda *a, **k: None), \
                mock.patch.object(state_dir, "shared_root", self._fake_shared_root), \
                mock.patch.object(self.asvc, "get_identity_service",
                                  return_value=self.svc):
            return json.loads(
                web_channel.SessionSettingsHandler().POST("s1"))

    def test_pin_lands_in_tenant_root_and_granted_model_is_echoed(self):
        resp = self._post({"model": "deepseek-v4-flash", "provider": "deepseek"})

        self.assertEqual(resp["status"], "success")
        # The granted model must be reported back, not blanked by a fail-closed
        # empty identity.
        self.assertEqual(resp["model"]["model"], "deepseek-v4-flash")
        self.assertIn("deepseek-v4-flash", [
            m for group in resp["model"]["providers"]
            for m in group.get("models", [])])

        # The pin must persist under the tenant's shared root, never the global
        # fallback the unscoped write used.
        pin = self.tenant_root / "session_prefs.json"
        self.assertTrue(pin.is_file(), "tenant session_prefs.json was not written")
        stored = json.loads(pin.read_text(encoding="utf-8"))
        self.assertEqual(
            stored["sessions"]["default::s1"]["model"], "deepseek-v4-flash")
        self.assertFalse((self.global_root / "session_prefs.json").exists())

    def test_permission_override_is_ignored_in_database_mode(self):
        """POST must not persist a session permission pin in database mode;
        the model change in the same request still applies."""
        resp = self._post({"permission": "read-only",
                           "model": "deepseek-v4-flash", "provider": "deepseek"})

        self.assertEqual(resp["status"], "success")
        self.assertEqual(resp["permission"]["source"], "role")
        self.assertEqual(resp["permission"]["modes"], [])
        stored = json.loads(
            (self.tenant_root / "session_prefs.json").read_text(encoding="utf-8"))
        session = stored["sessions"]["default::s1"]
        self.assertNotIn("permission", session)
        self.assertEqual(session["model"], "deepseek-v4-flash")


if __name__ == "__main__":
    unittest.main()
