# encoding:utf-8
"""Menu resource grants must actually gate console page visibility.

Regression: a custom role with an explicit ``menu`` grant set that omitted
``nav:workbench.history`` still saw the session-history entry, because the
console-page projection only consulted the functional permission and the
frontend skipped all non-``admin.*`` pages. The compat rule under test:

* a member whose effective roles carry at least one explicit ``menu`` grant is
  restricted to that set (intersected with the functional read permission);
* a member whose roles carry **no** ``menu`` grant (built-in roles and legacy
  custom roles) keeps the functional-permission behaviour;
* platform ``all`` is never restricted by menu grants.
"""

import os
import tempfile
import unittest

from auth.service import IdentityService


def _mk_db():
    return os.path.join(tempfile.mkdtemp(), "identity.db")


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.svc = IdentityService(_mk_db())
        self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root="/s/acme")
        self.svc.change_password(
            self.svc.login("root", "Str0ngAdminPass").token,
            "Str0ngAdminPass", "Str0ngRootFinal")
        self.root = [u for u in self.svc.list_platform_users()
                     if u["username"] == "root"][0]
        self.ta = self.svc.list_tenants()[0]["id"]
        self.token_root = self.svc.login("root", "Str0ngRootFinal").token

    def _member(self, username, roles):
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            operation="create-new", username=username, display_name=username,
            temporary_password="MemTempPass1", roles=roles)
        token = self.svc.login(username, "MemTempPass1").token
        self.svc.change_password(token, "MemTempPass1", "MemPassFinal1")
        return self.svc.login(username, "MemPassFinal1").token

    def _page(self, token, key):
        return self.svc.context_for_tenant(token, self.ta)["console_pages"][key]


class MenuGrantEnforcementTests(_Fixture):
    def test_unlisted_menu_is_not_readable_when_role_has_menu_grants(self):
        self.svc.create_role(
            self.root["id"], self.ta, "restricted", "受限角色",
            ["history.read", "knowledge.read", "agent.read"],
            resource_grants=[
                {"resource_kind": "menu",
                 "resource_id": "nav:workbench.knowledge", "action": "view"},
            ])
        token = self._member("restricteduser", ["restricted"])
        self.assertFalse(self._page(token, "workbench.history")["read_allowed"])
        self.assertTrue(self._page(token, "workbench.knowledge")["read_allowed"])

    def test_listed_menu_stays_readable(self):
        self.svc.create_role(
            self.root["id"], self.ta, "limited", "受限角色",
            ["history.read", "knowledge.read"],
            resource_grants=[
                {"resource_kind": "menu",
                 "resource_id": "nav:workbench.history", "action": "view"},
            ])
        token = self._member("limiteduser", ["limited"])
        self.assertTrue(self._page(token, "workbench.history")["read_allowed"])
        self.assertFalse(self._page(token, "workbench.knowledge")["read_allowed"])

    def test_menu_grants_also_gate_admin_pages(self):
        self.svc.create_role(
            self.root["id"], self.ta, "orgreader", "组织只读",
            ["tenant.members.read", "tenant.org.read"],
            resource_grants=[
                {"resource_kind": "menu",
                 "resource_id": "nav:admin.organization", "action": "view"},
            ])
        token = self._member("orgreaderuser", ["orgreader"])
        self.assertFalse(self._page(token, "admin.members")["read_allowed"])
        # The menu grant reaches the page, but the page is a qualification
        # surface: holding 组织读取 does not confer management qualification, so
        # the console refuses it too even though the functional permission is
        # present (rbac-authorization: 组织读取权限不能打开组织与权限管理).
        entry = self._page(token, "admin.organization")
        self.assertFalse(entry["read_allowed"])
        self.assertFalse(entry["available"])
        self.assertEqual(entry["reason"], "no_permission")

    def test_tenant_admin_keeps_org_perm_pages_despite_menu_grants(self):
        # A tenant admin's membership may also carry a custom role whose explicit
        # ``menu`` grants omit the current-tenant management pages. The built-in
        # tenant_admin qualification owns 组织与权限 unconditionally, so a menu
        # grant held by a *non-admin* role must not strip the admin surface the
        # qualification itself confers.
        self.svc.create_role(
            self.root["id"], self.ta, "restricted-nav", "受限导航",
            ["tenant.members.read", "tenant.org.read"],
            resource_grants=[
                {"resource_kind": "menu",
                 "resource_id": "nav:workbench.chat", "action": "view"},
            ])
        token = self._member("acmeadmin", ["tenant_admin", "restricted-nav"])
        for key in ("admin.members", "admin.roles", "admin.organization"):
            page = self._page(token, key)
            self.assertTrue(page["available"], (key, page))
            self.assertTrue(page["read_allowed"], (key, page))
            self.assertNotEqual(page.get("reason"), "menu_not_granted", (key, page))
            self.assertFalse(page.get("menu_denied", False), (key, page))

    def test_plain_member_without_org_perm_menu_grants_is_still_denied(self):
        # Regression guard: the tenant-admin exemption must not leak to an
        # ordinary member whose restrictive menu grant set omits these pages.
        self.svc.create_role(
            self.root["id"], self.ta, "restricted-nav", "受限导航",
            ["tenant.members.read", "tenant.org.read"],
            resource_grants=[
                {"resource_kind": "menu",
                 "resource_id": "nav:workbench.chat", "action": "view"},
            ])
        token = self._member("plainnav", ["restricted-nav"])
        for key in ("admin.members", "admin.roles", "admin.organization"):
            page = self._page(token, key)
            self.assertFalse(page["read_allowed"], (key, page))
            self.assertTrue(page.get("menu_denied", False), (key, page))

    def test_role_without_menu_grants_keeps_functional_behaviour(self):
        token = self._member("builtinuser", ["member"])
        self.assertTrue(self._page(token, "workbench.history")["read_allowed"])
        self.assertTrue(self._page(token, "workbench.knowledge")["read_allowed"])

    def test_platform_all_is_not_restricted_by_menu_grants(self):
        self.assertTrue(self._page(self.token_root, "workbench.history")["read_allowed"])
        self.assertTrue(self._page(self.token_root, "admin.members")["read_allowed"])

    def test_the_read_permission_alone_does_not_open_the_org_pages(self):
        """组织与权限 answers to qualification, not to the read grant.

        The member here carries both ``tenant.members.read`` and
        ``tenant.org.read`` through a role with *no* menu grants, so the
        functional-permission path is fully open and the refusal must come from
        the missing management qualification. The same rule covers the
        interfaces behind these pages
        (rbac-authorization: 组织读取权限不能打开组织与权限管理).
        """
        self.svc.create_role(
            self.root["id"], self.ta, "org_read_only", "组织只读",
            ["tenant.members.read", "tenant.org.read"])
        token = self._member("orgreadonly", ["org_read_only"])
        for key in ("admin.members", "admin.roles", "admin.organization"):
            entry = self._page(token, key)
            self.assertFalse(entry["available"], (key, entry))
            self.assertFalse(entry["read_allowed"], (key, entry))
            self.assertEqual(entry["reason"], "no_permission", (key, entry))
            # A denied page must not offer an action either way; the projection
            # reports all-false rather than dropping the map, so assert on the
            # meaning (nothing is offered) rather than on the shape.
            self.assertFalse(any(entry["actions"].values()), (key, entry))

    def test_the_platform_accounts_page_is_not_offered_to_a_tenant_admin(self):
        """The platform-account directory is the platform qualification's page.

        The sidebar row is 平台用户管理 (it was mislabelled 系统设置); either way
        it belongs to the platform qualification, not to the tenant one — a
        tenant administrator keeps only its own three pages below.
        """
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            operation="create-new", username="acmeadmin", display_name="Acme Admin",
            temporary_password="MemTempPass1", roles=["tenant_admin"])
        token = self.svc.login("acmeadmin", "MemTempPass1").token
        self.svc.change_password(token, "MemTempPass1", "MemPassFinal1")
        token = self.svc.login("acmeadmin", "MemPassFinal1").token
        entry = self._page(token, "admin.tenants")
        self.assertFalse(entry["available"], entry)
        self.assertEqual(entry["reason"], "no_permission", entry)
        # The tenant qualification still owns its own three pages.
        for key in ("admin.members", "admin.roles", "admin.organization"):
            self.assertTrue(self._page(token, key)["available"], key)


if __name__ == "__main__":
    unittest.main()
