# encoding:utf-8
"""Built-in roles reach 系统接入 after the page opens (migration 30).

The console entry is gated by both a ``menu`` grant and
``external.connections.read``. New tenants get both from
``BUILTIN_MENU_DEFAULTS`` / ``MEMBER_DEFAULT_PERMISSIONS`` /
``TENANT_ADMIN_DEFAULT_PERMISSIONS``; an already-provisioned tenant needs
``_migration_30``. This file pins that both halves land, and that a custom
role / a role with no menu grants is left alone.
"""

import json
import os
import tempfile
import unittest

from auth.policy import (
    BUILTIN_MENU_DEFAULTS,
    MEMBER_DEFAULT_PERMISSIONS,
    TENANT_ADMIN_DEFAULT_PERMISSIONS,
)
from auth.service import IdentityService
from auth.store import _migration_30, migration_versions

PAGE = "admin.external_connections"
PAGE_GRANT = "nav:" + PAGE
READ = "external.connections.read"
MANAGE = "external.connections.manage"


def _mk_db():
    return os.path.join(tempfile.mkdtemp(), "identity.db")


class BuiltinDefaultsCarryThePageTests(unittest.TestCase):
    def test_menu_defaults_list_the_page(self):
        for code in ("member", "tenant_admin"):
            self.assertIn(PAGE_GRANT, BUILTIN_MENU_DEFAULTS[code], code)

    def test_permission_defaults_carry_read(self):
        self.assertIn(READ, MEMBER_DEFAULT_PERMISSIONS)
        self.assertIn(READ, TENANT_ADMIN_DEFAULT_PERMISSIONS)

    def test_only_tenant_admin_gets_manage_by_default(self):
        self.assertNotIn(MANAGE, MEMBER_DEFAULT_PERMISSIONS)
        self.assertIn(MANAGE, TENANT_ADMIN_DEFAULT_PERMISSIONS)

    def test_test_permission_stays_off_the_defaults(self):
        self.assertNotIn("external.connections.test", MEMBER_DEFAULT_PERMISSIONS)
        self.assertNotIn(
            "external.connections.test", TENANT_ADMIN_DEFAULT_PERMISSIONS)


class FreshTenantSeesThePageTests(unittest.TestCase):
    def setUp(self):
        self.db = _mk_db()
        self.svc = IdentityService(self.db)
        self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root=os.path.join(tempfile.mkdtemp(), "acme"))
        self.svc.change_password(
            self.svc.login("root", "Str0ngAdminPass").token,
            "Str0ngAdminPass", "Str0ngRootFinal")
        self.root = [u for u in self.svc.list_platform_users()
                     if u["username"] == "root"][0]
        self.ta = self.svc.list_tenants()[0]["id"]
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            operation="create-new", username="acmeadmin",
            display_name="Acme Admin", temporary_password="MemTempPass1",
            roles=["tenant_admin"])
        token = self.svc.login("acmeadmin", "MemTempPass1").token
        self.svc.change_password(token, "MemTempPass1", "MemPassFinal1")
        self.token_admin = self.svc.login("acmeadmin", "MemPassFinal1").token

    def _role(self, code):
        return [r for r in self.svc.list_roles(self.ta) if r["code"] == code][0]

    def test_migration_30_is_registered(self):
        self.assertIn(30, migration_versions())
        self.assertEqual(migration_versions().count(30), 1)

    def test_fresh_tenant_admin_has_the_menu_grant(self):
        grants = {
            g["resource_id"]
            for g in self._role("tenant_admin")["resource_grants"]
            if g["resource_kind"] == "menu"
        }
        self.assertIn(PAGE_GRANT, grants)

    def test_fresh_tenant_admin_holds_read_and_manage(self):
        perms = set(self._role("tenant_admin")["permissions"])
        self.assertIn(READ, perms)
        self.assertIn(MANAGE, perms)

    def test_the_page_is_available_to_the_tenant_admin(self):
        page = self.svc.context_for_tenant(
            self.token_admin, self.ta)["console_pages"][PAGE]
        self.assertFalse(page.get("menu_denied"), page)
        self.assertTrue(page["available"], page)
        self.assertTrue(page["read_allowed"], page)

    def test_the_role_editor_lists_the_page_under_its_displayed_name(self):
        # The same name the sidebar and the page title show. This projection is
        # what a tenant admin reads when granting the menu, so a label left
        # behind would offer a page by a name the product no longer uses.
        items = self.svc.authorization_catalog(self.ta, kind="menu")["items"]
        entry = [i for i in items if i["resource_id"] == PAGE_GRANT]
        self.assertEqual(len(entry), 1, "the page is grantable")
        self.assertEqual(entry[0]["name"], "系统接入")


class Migration30BackfillTests(unittest.TestCase):
    def setUp(self):
        self.db = _mk_db()
        self.svc = IdentityService(self.db)
        self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root=os.path.join(tempfile.mkdtemp(), "acme"))
        self.ta = self.svc.list_tenants()[0]["id"]

    def _role_id(self, code):
        return [r for r in self.svc.list_roles(self.ta)
                if r["code"] == code][0]["id"]

    def _drop_page_grant(self, code):
        with self.svc._store.connect() as con:
            con.execute(
                "DELETE FROM role_resource_grants WHERE role_id=?"
                " AND resource_kind='menu' AND resource_id=?",
                (self._role_id(code), PAGE_GRANT))
            con.commit()

    def _strip_permissions(self, code, *ids):
        role_id = self._role_id(code)
        with self.svc._store.connect() as con:
            row = con.execute(
                "SELECT permissions_json FROM roles WHERE id=?",
                (role_id,)).fetchone()
            perms = [p for p in json.loads(row["permissions_json"] or "[]")
                     if p not in ids]
            con.execute(
                "UPDATE roles SET permissions_json=?, version=version+1"
                " WHERE id=?",
                (json.dumps(perms), role_id))
            con.commit()

    def _run(self, fn):
        with self.svc._store.connect() as con:
            fn(con)
            con.commit()

    def _menu_grants(self, code):
        rows = self.svc._store.execute(
            "SELECT resource_id FROM role_resource_grants WHERE role_id=?"
            " AND resource_kind='menu'", (self._role_id(code),))
        return {r["resource_id"] for r in rows}

    def _perms(self, code):
        row = self.svc._store.execute(
            "SELECT permissions_json FROM roles WHERE id=?",
            (self._role_id(code),))[0]
        return set(json.loads(row["permissions_json"] or "[]"))

    def test_backfills_menu_and_permissions_once(self):
        self._drop_page_grant("member")
        self._drop_page_grant("tenant_admin")
        self._strip_permissions("member", READ, MANAGE)
        self._strip_permissions("tenant_admin", READ, MANAGE)

        self._run(_migration_30)

        self.assertIn(PAGE_GRANT, self._menu_grants("member"))
        self.assertIn(PAGE_GRANT, self._menu_grants("tenant_admin"))
        self.assertIn(READ, self._perms("member"))
        self.assertNotIn(MANAGE, self._perms("member"))
        self.assertIn(READ, self._perms("tenant_admin"))
        self.assertIn(MANAGE, self._perms("tenant_admin"))

        self._run(_migration_30)
        rows = self.svc._store.execute(
            "SELECT COUNT(*) c FROM role_resource_grants WHERE role_id=?"
            " AND resource_kind='menu' AND resource_id=?",
            (self._role_id("member"), PAGE_GRANT))
        self.assertEqual(rows[0]["c"], 1)

    def test_does_not_switch_gating_on_for_a_role_with_no_menu_grants(self):
        with self.svc._store.connect() as con:
            con.execute(
                "DELETE FROM role_resource_grants WHERE role_id=?"
                " AND resource_kind='menu'", (self._role_id("member"),))
            con.commit()
        self._run(_migration_30)
        self.assertEqual(self._menu_grants("member"), set())

    def test_leaves_a_custom_role_alone(self):
        root = [u for u in self.svc.list_platform_users()
                if u["username"] == "root"][0]
        self.svc.create_role(
            actor_user_id=root["id"], tenant_id=self.ta,
            code="conn-viewer", name="conn-viewer",
            permissions=["chat.use"],
            resource_grants=[{"resource_kind": "menu",
                              "resource_id": "nav:workbench.chat",
                              "action": "view"}])
        self._run(_migration_30)
        role = [r for r in self.svc.list_roles(self.ta)
                if r["code"] == "conn-viewer"][0]
        grants = {
            g["resource_id"] for g in role.get("resource_grants", [])
            if g["resource_kind"] == "menu"
        }
        self.assertEqual(grants, {"nav:workbench.chat"})
        self.assertNotIn(READ, role["permissions"])


if __name__ == "__main__":
    unittest.main()
