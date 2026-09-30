# encoding:utf-8
"""The synthetic identity matrix the desktop change runs against (task 1.2).

``tests/desktop_identities.py`` declares which operations each actor may perform.
A declaration is worthless if it is not exercised, so this file builds the cast
against a real, temporary identity database and checks that the *boundary*
behaves as declared -- and in particular that the new public metadata endpoint is
reachable by everyone while nothing else about the matrix moved.

The point is not to re-test the console's authorization (other suites own that).
It is to prove the desktop fixture is real: the actors exist, they differ from
one another, and they never touch a developer's own credentials or data.
"""

import os
import tempfile
import unittest

from tests._helpers import WebAppHarness
from tests.desktop_identities import DENIED, PERMITTED, DesktopIdentities


class DesktopIdentityMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="desktop-identities-")
        cls.app = WebAppHarness(os.path.join(cls._tmp.name, "instance"))
        cls.cast = DesktopIdentities.build(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.app.close()
        cls._tmp.cleanup()

    def test_every_actor_is_distinct(self):
        ids = [uid for uid in self.cast.user_ids.values()]
        self.assertEqual(len(ids), len(set(ids)), "actors must be separate accounts")
        self.assertNotIn(self.cast.tenant_b["tenant_id"], ("", self.app.tenant_id))

    def test_the_capability_matrix_is_part_of_the_fixture(self):
        """Opened phases are enabled; phase 3B processing stays closed."""
        from auth import capability_matrix

        for slice_id in ("desktop_remote_web", "desktop_local_files",
                         "desktop_native_notifications"):
            self.assertTrue(capability_matrix.slice_for(slice_id).enabled, slice_id)
        self.assertFalse(
            capability_matrix.slice_for("desktop_local_processing").enabled)

    def test_permitted_operations_succeed(self):
        for identity, paths in PERMITTED.items():
            for path in paths:
                response = self.cast.request(identity, path)
                self.assertEqual(response.status.split()[0], "200",
                                 "%s was refused %s: %s" % (identity, path, response.status))

    def test_denied_operations_are_refused(self):
        for identity, paths in DENIED.items():
            for path in paths:
                response = self.cast.request(identity, path)
                self.assertNotEqual(response.status.split()[0], "200",
                                    "%s unexpectedly reached %s" % (identity, path))

    def test_the_public_metadata_needs_no_credential(self):
        """Every actor, and no actor at all, can read the handshake."""
        for identity in ("ANON", "U1", "UP", "TA", "PA", "FB"):
            response = self.cast.request(identity, "/api/desktop/meta")
            self.assertEqual(response.status.split()[0], "200", identity)

    def test_no_actor_reaches_another_tenants_data(self):
        """Tenant B's member never sees tenant A's members."""
        response = self.cast.request("FB", "/api/tenant/members")
        self.assertIn(response.status.split()[0], ("200", "403", "404"))
        if response.status.split()[0] == "200":
            body = WebAppHarness.json(response)
            blob = str(body)
            self.assertNotIn(self.cast.user_ids["U1"], blob)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
