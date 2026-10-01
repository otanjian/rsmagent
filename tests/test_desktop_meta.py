# encoding:utf-8
"""Desktop metadata handshake: public reachability and honest capability state.

Change ``add-desktop-remote-web-workbench``, tasks 2.4/2.5. ``GET
/api/desktop/meta`` is the surface a desktop client reads before it holds any
session, so this file locks two properties the design calls out:

* it is reachable *without* a credential — its job is to explain why a
  capability is unavailable (or confirm it is available), which it cannot do
  if it is itself gated or 503s;
* it reports the declaration's honest state (``implemented``/``accepted``/
  ``configured``/``reason``) and the *real* shell entry paths, never user,
  tenant or directory data.

The switch reflection is asserted through the composed
``capability_matrix.availability`` helper directly as well, including the
``disabled_by_deployment`` branch when acceptance is present but the switch is
off.
"""

import json
import os
import tempfile
import unittest

from tests._helpers import WebAppHarness


class DesktopMetaWireTests(unittest.TestCase):
    """The endpoint over the real WSGI app."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="desktop-meta-")
        cls.app = WebAppHarness(os.path.join(cls._tmp.name, "instance"))

    @classmethod
    def tearDownClass(cls):
        cls.app.close()
        cls._tmp.cleanup()

    def _body(self, response):
        self.assertEqual(response.status.split()[0], "200", response.status)
        return WebAppHarness.json(response)["data"]

    def test_public_without_a_session(self):
        """No cookie, no tenant header: still answered."""
        response = self.app.get("/api/desktop/meta", token=None, tenant=False)
        body = self._body(response)
        self.assertIn("remote_web", body)
        self.assertIn("protocols", body)

    def test_reports_protocol_versions(self):
        body = self._body(self.app.get("/api/desktop/meta"))
        self.assertEqual(body["protocols"]["web_session"], {"major": 1, "minor": 0})
        self.assertEqual(body["protocols"]["bridge"], {"major": 1, "minor": 0})
        self.assertEqual(body["protocols"]["files"], {"major": 1, "minor": 0})

    def test_only_running_local_gateway_is_advertised(self):
        from integrations.desktop.local_gateway import local_gateway
        self.assertNotIn("local_gateway_port", self._body(self.app.get("/api/desktop/meta")))
        local_gateway.start(self.app.service)
        try:
            body = self._body(self.app.get("/api/desktop/meta"))
            self.assertEqual(body["local_gateway_port"], local_gateway.port)
        finally:
            local_gateway.stop()
        self.assertNotIn("local_gateway_port", self._body(self.app.get("/api/desktop/meta")))

    def test_capabilities_report_opened_phases(self):
        """Phase 1/2/3A are open; phase 3B (local processing) stays closed."""
        body = self._body(self.app.get("/api/desktop/meta"))
        remote = body["remote_web"]
        self.assertTrue(remote["available"])
        self.assertTrue(remote["implemented"])
        self.assertTrue(remote["accepted"])
        self.assertTrue(remote["configured"])
        self.assertEqual(remote["reason"], "")
        self.assertTrue(body["features"]["local_files"]["available"])
        self.assertTrue(body["features"]["notifications"]["available"])
        processing = body["features"]["local_processing"]
        self.assertFalse(processing["available"])
        self.assertEqual(processing["reason"], "not_implemented")

    def test_entry_paths_are_real_shell_routes(self):
        """``console_entry_paths`` is derived from the registry, not copied."""
        from channel.web.route_registry import ROUTES, shell_entry_paths

        body = self._body(self.app.get("/api/desktop/meta"))
        paths = body["console_entry_paths"]
        self.assertEqual(paths, shell_entry_paths())
        registered = {entry.pattern for entry in ROUTES}
        for path in paths:
            self.assertIn(path, registered, path)
        # Content pages must never be advertised as a loadable shell entry.
        for forbidden in ("/preview", "/uploads", "/api/file"):
            self.assertNotIn(forbidden, paths)

    def test_no_user_or_tenant_data_leaks(self):
        body = self._body(self.app.get("/api/desktop/meta"))
        blob = json.dumps(body)
        self.assertNotIn(self.app.tenant_id, blob)
        self.assertNotIn(self.app.admin_id, blob)
        for key in ("user", "user_id", "tenant_id", "devices", "directories"):
            self.assertNotIn(key, body, key)

    def test_no_store(self):
        response = self.app.get("/api/desktop/meta")
        cache = ""
        for name, value in getattr(response, "header_items", None) or []:
            if name.lower() == "cache-control":
                cache = value
        self.assertIn("no-store", cache)

    def test_existing_routes_keep_their_policy(self):
        """Registering the new route must not disturb the derived table."""
        from channel.web.route_registry import derive_route_policy

        policy = derive_route_policy()
        self.assertEqual(policy["/api/desktop/meta"]["GET"]["policy"], "public")
        # A pre-existing slice-backed route is untouched.
        self.assertNotEqual(policy["/api/scheduler"]["GET"].get("policy"), "closed")


class DesktopMetaSwitchReflectionTests(unittest.TestCase):
    """The switch is reflected, and never opens a capability on its own."""

    def test_configured_true_without_acceptance_stays_unavailable(self):
        from auth import capability_matrix as cm

        spec = cm.slice_for("desktop_remote_web")
        saved = (spec.implemented, spec.accepted)
        try:
            spec.implemented, spec.accepted = True, False
            entry = cm.availability("desktop_remote_web", configured=True)
            self.assertTrue(entry["configured"])
            self.assertFalse(entry["available"])
            self.assertEqual(entry["reason"], "not_accepted")
        finally:
            spec.implemented, spec.accepted = saved

    def test_acceptance_and_switch_together_open_it(self):
        from auth import capability_matrix as cm

        spec = cm.slice_for("desktop_remote_web")
        saved = (spec.implemented, spec.accepted)
        try:
            spec.implemented, spec.accepted = True, True
            self.assertTrue(
                cm.availability("desktop_remote_web", configured=True)["available"])
            closed = cm.availability("desktop_remote_web", configured=False)
            self.assertFalse(closed["available"])
            self.assertEqual(closed["reason"], "disabled_by_deployment")
        finally:
            spec.implemented, spec.accepted = saved

    def test_missing_implementation_is_blamed_before_the_switch(self):
        from auth import capability_matrix as cm

        # local_processing is still undeclared as implemented (phase 3B).
        spec = cm.slice_for("desktop_local_processing")
        saved = (spec.implemented, spec.accepted)
        try:
            spec.implemented, spec.accepted = False, True
            entry = cm.availability("desktop_local_processing", configured=False)
            self.assertEqual(entry["reason"], "not_implemented")
        finally:
            spec.implemented, spec.accepted = saved


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
