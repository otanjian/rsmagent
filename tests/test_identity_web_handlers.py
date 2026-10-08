# encoding:utf-8
"""Route-level tests for the database identity backend handlers.

These exercise the handler wiring (auth context resolution, X-Tenant-ID gating,
tenant-admin/protected writes, audit query) without booting a live server. The
service is pointed at a temp identity.db and ``identity_mode`` is patched to
"database" so the handlers take the database path.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import web

from channel.web import web_channel, auth_handlers, admin_handlers
from auth.policy import BUILTIN_ROLES, PERMISSION_CATALOG
from auth.service import IdentityService


def _mk_db():
    return os.path.join(tempfile.mkdtemp(), "identity.db")


class DatabaseAuthHandlerTests(unittest.TestCase):
    def setUp(self):
        auth_handlers.reset_login_rate_limiter()
        self.db = _mk_db()
        self.svc = IdentityService(self.db)
        # A writable, real path: the shared root is where the tenant's skills
        # and workspace live, so a literal like "/s/acme" would make the console
        # read endpoints fail for reasons unrelated to authorization.
        self.shared_root = os.path.join(os.path.realpath(tempfile.mkdtemp()), "acme")
        self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root=self.shared_root, allow_weak=True)
        self.tid = self.svc.list_tenants()[0]["id"]
        self.root = self.svc.list_platform_users()[0]

    def _app(self):
        return web.application(
            (
                "/auth/login", "DbAuthLoginHandler",
                "/auth/check", "DbAuthCheckHandler",
                "/auth/logout", "DbAuthLogoutHandler",
                "/auth/password", "DbAuthPasswordHandler",
                "/auth/me", "DbAuthMeHandler",
                "/auth/context", "DbAuthContextHandler",
                "/api/platform/users", "PlatformUsersHandler",
                "/api/platform/users/([^/]+)/password", "PlatformUserPasswordHandler",
                "/api/platform/users/([^/]+)", "PlatformUsersHandler",
                "/api/platform/tenants", "PlatformTenantsHandler",
                "/api/platform/tenants/([^/]+)", "PlatformTenantHandler",
                "/api/platform/tenants/([^/]+)/admins", "PlatformTenantAdminsHandler",
                "/api/platform/tenants/([^/]+)/resources", "PlatformTenantResourcesHandler",
                "/api/tenant", "TenantInfoHandler",
                "/api/tenant/members", "TenantMembersHandler",
                "/api/tenant/members/([^/]+)/password", "TenantMemberPasswordHandler",
                "/api/tenant/members/([^/]+)", "TenantMemberHandler",
                "/api/tenant/roles", "TenantRolesHandler",
                "/api/tenant/roles/([^/]+)", "TenantRoleHandler",
                "/api/tenant/permissions", "TenantPermissionsHandler",
                "/api/tenant/departments", "TenantDepartmentsHandler",
                "/api/tenant/departments/([^/]+)", "TenantDepartmentHandler",
                "/api/identity/audit", "IdentityAuditHandler",
                "/api/identity/administered-tenants", "IdentityAdministeredTenantsHandler",
            ),
            vars(web_channel),
            autoreload=False,
        )

    def _request(self, path, method="GET", data="", token=None, tenant=None):
        app = self._app()
        kwargs = {"method": method}
        if data:
            kwargs["data"] = data
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if tenant:
            headers["X-Tenant-ID"] = tenant
        if headers:
            kwargs["headers"] = headers

        def _fake_service():
            return self.svc

        with patch.object(auth_handlers, "_get_service", _fake_service), \
                patch.object(admin_handlers, "_get_service", _fake_service):
            return app.request(path, **kwargs)

    @staticmethod
    def _json(resp):
        return json.loads(resp.data.decode("utf-8"))

    def test_login_sets_cookie_and_returns_tenants(self):
        resp = self._request(
            "/auth/login", method="POST",
            data=json.dumps({"username": "root", "password": "Str0ngAdminPass"}))
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        # Design D8: the Web login response is not an authorization grant for a
        # native session. The compatibility ``token`` field stays in the payload
        # but is always empty; the Cookie is the only credential issued here, and
        # Desktop obtains its own session through /auth/desktop/token (PKCE S256).
        self.assertIn("token", data)
        self.assertEqual(data["token"], "")
        self.assertGreaterEqual(len(data["tenants"]), 1)
        # cookie was set in the Set-Cookie header
        self.assertIn("cow_session", str(getattr(resp, "headers", {})))

    def test_login_bad_password(self):
        resp = self._request(
            "/auth/login", method="POST",
            data=json.dumps({"username": "root", "password": "wrong"}))
        data = self._json(resp)
        self.assertEqual(data["status"], "error")
        self.assertEqual(data["code"], "invalid_login")

    def test_platform_tenants_requires_login(self):
        resp = self._request("/api/platform/tenants", method="GET")
        self.assertTrue(str(getattr(resp, "status", "")).startswith("4"))

    def test_platform_tenants_admin_ok(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request("/api/platform/tenants", method="GET", token=token)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        self.assertGreaterEqual(len(data["items"]), 1)

    def test_member_list_requires_tenant_selection(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        # no X-Tenant-ID -> 400
        resp = self._request("/api/tenant/members", method="GET", token=token)
        self.assertTrue(str(getattr(resp, "status", "")).startswith("4"))

    def test_member_list_with_tenant_ok(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request("/api/tenant/members", method="GET", token=token, tenant=self.tid)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")

    # --- 组织与权限 is a qualification surface, not a permission one --------

    def _member_token_with_org_read(self, username="orgreader"):
        """A *plain member* whose role carries both 组织读取 and 成员读取.

        The read permissions are deliberately held: the refusal under test must
        come from the missing management qualification, not from a missing
        functional permission, or it would pass for the wrong reason.
        """
        self.svc.create_role(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            code="org_read_only", name="组织只读",
            permissions=["tenant.members.read", "tenant.org.read"])
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid, operation="create-new",
            username=username, display_name="Org Reader",
            temporary_password="Str0ngPassTmp", roles=["org_read_only"])
        with self.svc._tx() as con:
            con.execute(
                "UPDATE users SET must_change_password=0 WHERE username=?", (username,))
            con.commit()
        return self.svc.login(username, "Str0ngPassTmp").token

    def test_holding_the_read_permission_does_not_open_the_member_surface(self):
        """rbac-authorization: 组织读取权限不能打开组织与权限管理.

        A member holding ``tenant.members.read`` / ``tenant.org.read`` is still
        refused the management interfaces; the console page projection refuses
        the same three pages (see ``_console_pages_projection``), so the entry
        and the data behind it agree.
        """
        token = self._member_token_with_org_read()
        for path in ("/api/tenant/members", "/api/tenant/roles",
                     "/api/tenant/departments"):
            resp = self._request(path, method="GET", token=token, tenant=self.tid)
            status = str(getattr(resp, "status", ""))
            self.assertTrue(status.startswith("403"), (path, status))

    def test_the_tenant_admin_keeps_the_same_surface(self):
        """The negative control: the qualification is what opens it, not the read."""
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid, operation="create-new",
            username="acmeadmin", display_name="Acme Admin",
            temporary_password="Str0ngPassTmp", roles=["tenant_admin"])
        with self.svc._tx() as con:
            con.execute(
                "UPDATE users SET must_change_password=0 WHERE username='acmeadmin'")
            con.commit()
        token = self.svc.login("acmeadmin", "Str0ngPassTmp").token
        for path in ("/api/tenant/members", "/api/tenant/roles",
                     "/api/tenant/departments"):
            resp = self._request(path, method="GET", token=token, tenant=self.tid)
            self.assertEqual(self._json(resp)["status"], "success", path)

    def test_member_create_short_password_is_json_4xx_not_500(self):
        # Regression: a <8-char temporary password raised auth.password.PasswordError
        # out of create_member. The handler only translated IdentityServiceError,
        # so it surfaced as a 500 with an HTML body — which the console could not
        # parse and reported as the generic "load-failed".
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request(
            "/api/tenant/members", method="POST", token=token, tenant=self.tid,
            data=json.dumps({
                "operation": "create-new", "username": "shorty",
                "display_name": "Shorty", "temporary_password": "123456",
                "roles": ["member"]}))
        self.assertTrue(str(getattr(resp, "status", "")).startswith("4"),
                        (resp.status, resp.data))
        data = self._json(resp)
        self.assertEqual(data["status"], "error")
        self.assertEqual(data["code"], "weak_password")

    def test_permission_catalog_with_tenant_ok(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request("/api/tenant/permissions", token=token, tenant=self.tid)
        self.assertTrue(str(resp.status).startswith("200"))
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        # permissions is now a list of {id, group, label, description, scope, assignable}
        self.assertEqual([p["id"] for p in data["permissions"]], list(PERMISSION_CATALOG))
        for p in data["permissions"]:
            self.assertEqual(
                set(p.keys()),
                {"id", "group", "label", "description", "scope", "assignable"},
            )
        self.assertEqual(data["builtin_roles"], BUILTIN_ROLES)

    def test_permission_catalog_requires_login(self):
        resp = self._request("/api/tenant/permissions", tenant=self.tid)
        self.assertTrue(str(resp.status).startswith("401"))
        self.assertEqual(self._json(resp)["code"], "unauthorized")

    def test_permission_catalog_requires_tenant_selection(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request("/api/tenant/permissions", token=token)
        self.assertTrue(str(resp.status).startswith("400"))
        self.assertEqual(self._json(resp)["code"], "missing_tenant")

    def _role_by_code(self, token, code):
        roles = self._json(self._request(
            "/api/tenant/roles", token=token, tenant=self.tid))["items"]
        return next(r for r in roles if r["code"] == code)

    def test_builtin_member_role_is_editable_over_http(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        member = self._role_by_code(token, "member")
        self.assertEqual(int(member["builtin"]), 1)
        wanted = sorted(set(member["permissions"]) | {"tenant.members.read"})
        resp = self._request(
            "/api/tenant/roles/%s" % member["id"], method="POST", token=token,
            tenant=self.tid,
            data=json.dumps({
                "name": member["name"],
                "permissions": wanted,
                "expected_version": member["version"],
            }))
        self.assertTrue(str(resp.status).startswith("200"))
        self.assertEqual(
            sorted(self._json(resp)["role"]["permissions"]), wanted)

    def test_builtin_tenant_admin_self_lock_is_rejected_over_http(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        admin = self._role_by_code(token, "tenant_admin")
        resp = self._request(
            "/api/tenant/roles/%s" % admin["id"], method="POST", token=token,
            tenant=self.tid,
            data=json.dumps({
                "name": admin["name"],
                "permissions": ["tenant.info.read"],
                "expected_version": admin["version"],
            }))
        self.assertTrue(str(resp.status).startswith("409"))
        self.assertEqual(
            self._json(resp)["code"], "builtin_minimum_permissions")

    def test_builtin_role_delete_is_still_rejected_over_http(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        member = self._role_by_code(token, "member")
        resp = self._request(
            "/api/tenant/roles/%s" % member["id"], method="DELETE", token=token,
            tenant=self.tid)
        self.assertTrue(str(resp.status).startswith("4"))
        self.assertEqual(
            self._role_by_code(token, "member")["id"], member["id"])

    def test_custom_role_with_grants_deletes_over_http(self):
        # Regression: a custom role carrying resource grants could not be
        # deleted. The composite FK from ``role_resource_grants`` blocked the
        # DELETE, the handler only translated ``IdentityServiceError``, and the
        # raw IntegrityError surfaced as a 500 with a non-JSON body — which the
        # console rendered as the generic "load-failed".
        token = self.svc.login("root", "Str0ngAdminPass").token
        role = self.svc.create_role(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            code="buyer", name="采购员", permissions=["skill.read"],
            resource_grants=[{"resource_kind": "skill",
                              "resource_id": "builtin:web-fetch", "action": "read"}])
        resp = self._request(
            "/api/tenant/roles/%s" % role["id"], method="DELETE", token=token,
            tenant=self.tid)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        self.assertTrue(data["deleted"])
        self.assertEqual(
            [r for r in self.svc.list_roles(self.tid) if r["code"] == "buyer"], [])

    def test_audit_query_scoped(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request("/api/identity/audit", method="GET", token=token)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        self.assertTrue(len(data["items"]) >= 1)

    def test_self_context_whitelist_shape(self):
        # bootstrap admin (allow_weak=True) is immediately usable; full projection
        token = self.svc.login("root", "Str0ngAdminPass").token
        ctx2 = self.svc.self_context(token)
        self.assertEqual(ctx2["user"]["username"], "root")
        self.assertFalse(ctx2["must_change_password"])
        self.assertGreaterEqual(len(ctx2["tenants"]), 1)
        entry = ctx2["tenants"][0]
        self.assertEqual(set(entry.keys()), {"id", "code", "name", "membership"})
        membership = entry["membership"]
        self.assertIn("roles", membership)
        self.assertTrue(all(set(r.keys()) == {"code", "name"} for r in membership["roles"]))

    def test_self_context_route_requires_session(self):
        resp = self._request("/auth/me", method="GET")
        data = self._json(resp)
        self.assertEqual(data["code"], "unauthorized")
        self.assertTrue(str(getattr(resp, "status", "")).startswith("4"))

    def test_self_context_route_returns_projection(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request("/auth/me", method="GET", token=token)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["user"]["username"], "root")
        self.assertIn("tenants", data)
        # response is no-store
        self.assertIn("no-store", str(getattr(resp, "headers", {})).lower())

    def test_self_context_does_not_depend_on_tenant_header(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request("/auth/me", method="GET", token=token, tenant="bad-tenant")
        data = self._json(resp)
        self.assertEqual(data["status"], "success")

    def test_password_weak_returns_400(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request(
            "/auth/password", method="POST",
            data=json.dumps({"old_password": "Str0ngAdminPass", "new_password": "password"}),
            token=token)
        data = self._json(resp)
        self.assertEqual(data["code"], "weak_password")
        self.assertTrue(str(getattr(resp, "status", "")).startswith("4"))

    def test_password_invalid_old_returns_401_invalid_old(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request(
            "/auth/password", method="POST",
            data=json.dumps({"old_password": "wrong", "new_password": "Str0ngNewPass"}),
            token=token)
        data = self._json(resp)
        self.assertEqual(data["code"], "invalid_old")
        self.assertTrue(str(getattr(resp, "status", "")).startswith("4"))

    def test_password_success_clears_cookie_and_must_relogin(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request(
            "/auth/password", method="POST",
            data=json.dumps({"old_password": "Str0ngAdminPass", "new_password": "Str0ngNewPass"}),
            token=token)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        self.assertTrue(data["must_relogin"])
        # cookie cleared via Set-Cookie with empty value
        headers = str(getattr(resp, "headers", {}))
        self.assertIn("cow_session", headers)

    def test_password_cross_origin_rejected(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request(
            "/auth/password", method="POST",
            data=json.dumps({"old_password": "Str0ngAdminPass", "new_password": "Str0ngNewPass"}),
            token=token)
        # no-set validation on cross-origin: with no Origin header the write is
        # allowed (same-origin default). Verify no-store on the response.
        self.assertIn("no-store", str(getattr(resp, "headers", {})).lower())

    def test_restricted_tenant_access_blocked(self):
        # a genuinely restricted user (forced password change) must be blocked
        # from tenant-scoped admin reads with password_change_required.
        self.svc.create_tenant(
            actor_user_id=self.root["id"], code="other", name="Other",
            shared_root="/s/other", admin_username="root2", admin_display="Root2",
            admin_password="Str0ngAdminPass2", recent_password="Str0ngAdminPass")
        token = self.svc.login("root2", "Str0ngAdminPass2").token
        other_tid = next(t for t in self.svc.list_tenants() if t["code"] == "other")["id"]
        resp = self._request("/api/tenant/members", method="GET", token=token, tenant=other_tid)
        data = self._json(resp)
        self.assertEqual(data["code"], "password_change_required")
        self.assertTrue(str(getattr(resp, "status", "")).startswith("4"))

    def test_department_update_via_handler(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        dept = self.svc.create_department(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            code="eng", name="Eng", parent_id=None)
        payload = json.dumps({
            "name": "Engineering", "sort_order": 3,
            "expected_version": dept["version"]})
        resp = self._request(
            f"/api/tenant/departments/{dept['id']}", method="PUT",
            data=payload, token=token, tenant=self.tid)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["department"]["name"], "Engineering")
        self.assertEqual(data["department"]["sort_order"], 3)

    def test_department_update_cycle_rejected(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        eng = self.svc.create_department(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            code="eng", name="Eng", parent_id=None)
        plat = self.svc.create_department(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            code="plat", name="Platform", parent_id=eng["id"])
        payload = json.dumps({"parent_id": plat["id"], "expected_version": eng["version"]})
        resp = self._request(
            f"/api/tenant/departments/{eng['id']}", method="PUT",
            data=payload, token=token, tenant=self.tid)
        data = self._json(resp)
        self.assertEqual(data["status"], "error")
        self.assertEqual(data["code"], "cycle")

    # --- legacy handlers delegate to database handlers in database mode -----

    def _legacy_app(self):
        # The real web_channel routing resolves /auth/login -> AuthLoginHandler
        # (legacy), which MUST delegate to DbAuthLoginHandler in database mode.
        return web.application(
            ("/auth/login", "AuthLoginHandler",
             "/auth/check", "AuthCheckHandler",
             "/auth/logout", "AuthLogoutHandler"),
            vars(web_channel),
            autoreload=False,
        )

    def _legacy_request(self, path, method="GET", data="", token=None):
        app = self._legacy_app()
        kwargs = {"method": method}
        if data:
            kwargs["data"] = data
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if headers:
            kwargs["headers"] = headers

        def _fake_service():
            return self.svc

        with patch.object(auth_handlers, "_get_service", _fake_service):
            return app.request(path, **kwargs)

    def test_login_delegates_to_db_handler(self):
        resp = self._legacy_request(
            "/auth/login", method="POST",
            data=json.dumps({"username": "root", "password": "Str0ngAdminPass"}))
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        # Design D8: the compatibility ``token`` field is always empty; the
        # Cookie is the only credential the Web login response issues.
        self.assertIn("token", data)
        self.assertEqual(data["token"], "")
        # database-mode cookie is cow_session, not the legacy cow_auth_token
        self.assertIn("cow_session", str(getattr(resp, "headers", {})))
        self.assertNotIn("cow_auth_token", str(getattr(resp, "headers", {})))

    def test_legacy_check_delegates_to_db_handler(self):
        resp = self._legacy_request("/auth/check", method="GET")
        data = self._json(resp)
        # not logged in -> database handler reports identity_mode=database
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["identity_mode"], "database")
        self.assertFalse(data["authenticated"])


    # --- /auth/context (task 5.2) -----------------------------------------
    def test_context_requires_tenant_selection(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request("/auth/context", method="GET", token=token)
        self.assertEqual(self._json(resp)["code"], "missing_tenant")
        self.assertTrue(str(resp.status).startswith("400"))

    def test_context_requires_login(self):
        resp = self._request("/auth/context", method="GET", tenant=self.tid)
        self.assertEqual(self._json(resp)["code"], "unauthorized")
        self.assertTrue(str(resp.status).startswith("401"))

    def test_context_returns_effective_permissions_without_tenant_info_read(self):
        # A plain member (no tenant.info.read granted) can still read context.
        root = self.svc.list_platform_users()[0]
        self.svc.create_member(
            actor_user_id=root["id"], tenant_id=self.tid, operation="create-new",
            username="ctxuser", display_name="Ctx", temporary_password="Str0ngPassTmp",
            roles=["member"])
        # complete the forced password change so the account is unrestricted
        member_token = self.svc.login("ctxuser", "Str0ngPassTmp").token
        self.svc.change_password(member_token, "Str0ngPassTmp", "Str0ngPassNew!")
        member_token = self.svc.login("ctxuser", "Str0ngPassNew!").token
        resp = self._request("/auth/context", method="GET", token=member_token, tenant=self.tid)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        self.assertIn("effective_permissions", data)
        # member default excludes tenant.members.read / tenant.org.read
        self.assertNotIn("tenant.members.read", data["effective_permissions"])
        self.assertFalse(data["is_tenant_admin"])
        self.assertIn("consumers", data)
        self.assertIsInstance(data["consumers"], dict)

    def test_context_admin_has_nine_permissions(self):
        from auth.policy import default_permissions_for, TENANT_ADMIN_CODE
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request("/auth/context", method="GET", token=token, tenant=self.tid)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        # The built-in tenant_admin carries its explicit nine-id default set; the
        # catalogue does NOT auto-widen it when new resource ids are added.
        self.assertEqual(set(data["effective_permissions"]), set(default_permissions_for(TENANT_ADMIN_CODE)))
        self.assertTrue(data["is_tenant_admin"])

    def test_context_missing_membership_forbidden(self):
        other = self.svc.create_tenant(
            actor_user_id=self.root["id"], code="noctx", name="NoCtx",
            shared_root="/s/noctx", admin_username="nobody", admin_display="Nobody",
            admin_password="Str0ngPassTmp2", recent_password="Str0ngAdminPass")
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request("/auth/context", method="GET", token=token, tenant=other["id"])
        self.assertEqual(self._json(resp)["code"], "forbidden")
        self.assertTrue(str(resp.status).startswith("403"))

    # --- platform users list (task 4.1) ------------------------------------
    def test_platform_users_requires_login(self):
        resp = self._request("/api/platform/users", method="GET")
        self.assertTrue(str(resp.status).startswith("401"))

    def test_platform_users_platform_admin_ok(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request("/api/platform/users", method="GET", token=token)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["total"], 1)
        item = data["items"][0]
        self.assertEqual(item["username"], "root")
        self.assertNotIn("password_hash", item)
        self.assertNotIn("temp_password_expires_at", item)

    def test_platform_users_member_rejected(self):
        root = self.svc.list_platform_users()[0]
        self.svc.create_member(
            actor_user_id=root["id"], tenant_id=self.tid, operation="create-new",
            username="plain", display_name="Plain", temporary_password="Str0ngPassTmp",
            roles=["member"])
        mt = self.svc.login("plain", "Str0ngPassTmp").token
        self.svc.change_password(mt, "Str0ngPassTmp", "Str0ngPassNew!")
        mt = self.svc.login("plain", "Str0ngPassNew!").token
        resp = self._request("/api/platform/users", method="GET", token=mt)
        self.assertEqual(self._json(resp)["code"], "forbidden")
        self.assertTrue(str(resp.status).startswith("403"))

    def test_platform_users_filter_and_paging(self):
        root = self.svc.list_platform_users()[0]
        for i in range(3):
            self.svc.create_member(
                actor_user_id=root["id"], tenant_id=self.tid, operation="create-new",
                username=f"user{i}", display_name=f"User {i}", temporary_password="Str0ngPassTmp",
                roles=["member"])
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request("/api/platform/users?status=active&page=1&page_size=2",
                             method="GET", token=token)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["total"], 4)
        self.assertEqual(len(data["items"]), 2)

    def test_platform_users_bad_status_filter(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request("/api/platform/users?status=bogus", method="GET", token=token)
        self.assertEqual(self._json(resp)["code"], "bad_request")
        self.assertTrue(str(resp.status).startswith("4"))

    # --- platform user status/admin (task 4.2) ----------------------------
    def test_platform_user_patch_status(self):
        root = self.svc.list_platform_users()[0]
        # add a second completed platform admin so demoting root is legal
        self.svc.create_member(
            actor_user_id=root["id"], tenant_id=self.tid, operation="create-new",
            username="admin2", display_name="Admin2", temporary_password="Str0ngPassTmp",
            roles=["tenant_admin"])
        m = [x for x in self.svc.list_members(self.tid)["items"] if x["username"] == "admin2"][0]
        self.svc.set_platform_user_status(
            actor_user_id=root["id"], user_id=m["user_id"], active=True,
            is_platform_admin=True, expected_version=m["version"],
            recent_password="Str0ngAdminPass")
        res = self.svc.login("admin2", "Str0ngPassTmp")
        self.svc.change_password(res.token, "Str0ngPassTmp", "Str0ngAdminFinal")
        admin2 = [x for x in self.svc.list_platform_users() if x["username"] == "admin2"][0]

        token = self.svc.login("root", "Str0ngAdminPass").token
        payload = json.dumps({
            "active": True, "is_platform_admin": False,
            "expected_version": root["version"], "recent_password": "Str0ngAdminPass"})
        resp = self._request(f"/api/platform/users/{root['id']}", method="PATCH",
                             data=payload, token=token)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        self.assertFalse(data["user"]["is_platform_admin"])

    def test_platform_user_patch_requires_recent_password(self):
        root = self.svc.list_platform_users()[0]
        token = self.svc.login("root", "Str0ngAdminPass").token
        payload = json.dumps({
            "active": True, "is_platform_admin": False,
            "expected_version": root["version"], "recent_password": "Wrong"})
        resp = self._request(f"/api/platform/users/{root['id']}", method="PATCH",
                             data=payload, token=token)
        self.assertEqual(self._json(resp)["code"], "invalid_old")

    def test_platform_user_patch_last_admin_rejected(self):
        root = self.svc.list_platform_users()[0]
        token = self.svc.login("root", "Str0ngAdminPass").token
        payload = json.dumps({
            "active": True, "is_platform_admin": False,
            "expected_version": root["version"], "recent_password": "Str0ngAdminPass"})
        resp = self._request(f"/api/platform/users/{root['id']}", method="PATCH",
                             data=payload, token=token)
        self.assertEqual(self._json(resp)["code"], "last_admin")

    # --- platform user password reset (task 4.3) --------------------------
    def test_platform_user_reset_password(self):
        root = self.svc.list_platform_users()[0]
        self.svc.create_member(
            actor_user_id=root["id"], tenant_id=self.tid, operation="create-new",
            username="resetme", display_name="Reset", temporary_password="Str0ngPassTmp",
            roles=["member"])
        m = [x for x in self.svc.list_members(self.tid)["items"] if x["username"] == "resetme"][0]
        token = self.svc.login("root", "Str0ngAdminPass").token
        payload = json.dumps({"expected_version": m["version"], "recent_password": "Str0ngAdminPass"})
        resp = self._request(f"/api/platform/users/{m['user_id']}/password",
                             method="POST", data=payload, token=token)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        self.assertTrue(data["temporary_password"])
        self.assertTrue(data["must_change_password"])

    def test_platform_user_reset_self_rejected(self):
        root = self.svc.list_platform_users()[0]
        token = self.svc.login("root", "Str0ngAdminPass").token
        payload = json.dumps({"expected_version": root["version"], "recent_password": "Str0ngAdminPass"})
        resp = self._request(f"/api/platform/users/{root['id']}/password",
                             method="POST", data=payload, token=token)
        self.assertEqual(self._json(resp)["code"], "self_reset_forbidden")

    # --- tenant rename (task 4.4) -----------------------------------------
    def _tenant_reset_fixture(self):
        for name, roles in (("tenantadmin", ["tenant_admin"]), ("resetmember", ["member"])):
            self.svc.create_member(
                actor_user_id=self.root["id"], tenant_id=self.tid, operation="create-new",
                username=name, display_name=name, temporary_password="InitialPassword9", roles=roles)
            token = self.svc.login(name, "InitialPassword9").token
            self.svc.change_password(token, "InitialPassword9", "FinalPassword9")
        token = self.svc.login("tenantadmin", "FinalPassword9").token
        items = self._json(self._request("/api/tenant/members", token=token, tenant=self.tid))["items"]
        member = next(m for m in items if m["username"] == "resetmember")
        return token, member, json.dumps({"expected_version": member["user_version"],
                                         "recent_password": "FinalPassword9"})

    def test_tenant_password_reset_returns_fixed_temporary_credential(self):
        token, member, payload = self._tenant_reset_fixture()
        self.assertTrue(member["can_reset_password"])
        response = self._request(f"/api/tenant/members/{member['id']}/password",
                                 method="POST", data=payload, token=token, tenant=self.tid)
        data = self._json(response)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["temporary_password"], "123456")
        self.assertTrue(data["must_change_password"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_tenant_password_reset_requires_scope_admin_and_password(self):
        token, member, payload = self._tenant_reset_fixture()
        path = f"/api/tenant/members/{member['id']}/password"
        for bad_token, tenant, body, status in (
            (None, self.tid, payload, 401),
            (token, None, payload, 400),
            (token, "other-tenant", payload, 403),
            (self.svc.login("resetmember", "FinalPassword9").token, self.tid, payload, 403),
            (token, self.tid, json.dumps({"expected_version": member["user_version"]}), 401),
            (token, self.tid, "[]", 400),
            (token, self.tid, json.dumps({"expected_version": True}), 400),
        ):
            with self.subTest(status=status, body=body):
                response = self._request(path, method="POST", data=body, token=bad_token, tenant=tenant)
                self.assertEqual(int(response.status.split()[0]), status)

    def test_tenant_password_reset_rejects_cookie_write_without_origin(self):
        token, member, payload = self._tenant_reset_fixture()
        with patch.object(auth_handlers, "_get_service", return_value=self.svc), \
                patch.object(admin_handlers, "_get_service", return_value=self.svc):
            response = self._app().request(
                f"/api/tenant/members/{member['id']}/password", method="POST", data=payload,
                headers={"Cookie": f"cow_session={token}", "X-Tenant-ID": self.tid})
        self.assertEqual(self._json(response)["code"], "csrf_failed")

    def test_tenant_reset_does_not_open_platform_password_endpoint(self):
        token, member, payload = self._tenant_reset_fixture()
        response = self._request(f"/api/platform/users/{member['user_id']}/password",
                                 method="POST", data=payload, token=token, tenant=self.tid)
        self.assertEqual(int(response.status.split()[0]), 403)

    def test_tenant_rename_via_handler(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        v = self.svc.get_tenant(self.tid)["version"]
        payload = json.dumps({"name": "Acme Corp", "expected_version": v,
                              "recent_password": "Str0ngAdminPass"})
        resp = self._request(f"/api/platform/tenants/{self.tid}", method="POST",
                             data=payload, token=token)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["tenant"]["name"], "Acme Corp")

    def test_tenant_rename_version_conflict(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        v = self.svc.get_tenant(self.tid)["version"]
        payload = json.dumps({"name": "Acme Corp", "expected_version": v + 9,
                              "recent_password": "Str0ngAdminPass"})
        resp = self._request(f"/api/platform/tenants/{self.tid}", method="POST",
                             data=payload, token=token)
        self.assertEqual(self._json(resp)["code"], "conflict")

    # --- tenant profile: name + active in one save ------------------------
    def _bare_tenant(self, code="beta"):
        """A member-less tenant: safe to disable, impossible to enable."""
        return self.svc.create_tenant(
            actor_user_id=self.root["id"], code=code, name=code.title(),
            shared_root="/s/" + code, recent_password="Str0ngAdminPass")

    def test_tenant_profile_updates_name_and_active_together(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        tid = self._bare_tenant()["id"]
        payload = json.dumps({
            "operation": "profile", "name": "Beta Renamed", "active": False,
            "expected_version": 1, "recent_password": "Str0ngAdminPass"})
        resp = self._request(f"/api/platform/tenants/{tid}", method="POST",
                             data=payload, token=token)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["tenant"]["name"], "Beta Renamed")
        self.assertEqual(data["tenant"]["active"], False)
        self.assertEqual(data["tenant"]["version"], 2)
        # The enabled flag really landed in the row, not just the response.
        self.assertEqual(dict(self.svc.get_tenant(tid))["active"], 0)

    def test_tenant_profile_conflict_opcode_on_version_mismatch(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        tid = self._bare_tenant()["id"]
        payload = json.dumps({
            "operation": "profile", "name": "Beta Renamed", "active": False,
            "expected_version": 99, "recent_password": "Str0ngAdminPass"})
        resp = self._request(f"/api/platform/tenants/{tid}", method="POST",
                             data=payload, token=token)
        self.assertEqual(self._json(resp)["code"], "conflict")
        self.assertEqual(dict(self.svc.get_tenant(tid))["name"], "Beta")

    def test_tenant_profile_enable_without_admin_is_rejected(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        tid = self._bare_tenant()["id"]
        self.svc.set_tenant_status(self.root["id"], tid, False, 1, "Str0ngAdminPass")
        payload = json.dumps({
            "operation": "profile", "name": "Beta Renamed", "active": True,
            "expected_version": 2, "recent_password": "Str0ngAdminPass"})
        resp = self._request(f"/api/platform/tenants/{tid}", method="POST",
                             data=payload, token=token)
        self.assertEqual(self._json(resp)["code"], "no_admin")
        # The rejected enable must not leave the rename behind.
        self.assertEqual(dict(self.svc.get_tenant(tid))["name"], "Beta")

    def test_tenant_status_operation_disables_without_renaming(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        tid = self._bare_tenant()["id"]
        payload = json.dumps({
            "operation": "status", "active": False,
            "expected_version": 1, "recent_password": "Str0ngAdminPass"})
        resp = self._request(f"/api/platform/tenants/{tid}", method="POST",
                             data=payload, token=token)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        after = dict(self.svc.get_tenant(tid))
        self.assertEqual(after["active"], 0)
        self.assertEqual(after["name"], "Beta")

    def test_tenant_unknown_operation_is_rejected(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        tid = self._bare_tenant()["id"]
        payload = json.dumps({
            "operation": "nonsense", "name": "Beta Renamed", "active": False,
            "expected_version": 1, "recent_password": "Str0ngAdminPass"})
        resp = self._request(f"/api/platform/tenants/{tid}", method="POST",
                             data=payload, token=token)
        # web.HTTPError carries the status as a string on the test path.
        self.assertEqual(str(resp.status), "400")
        self.assertEqual(self._json(resp)["code"], "invalid_request")
        self.assertEqual(dict(self.svc.get_tenant(tid))["name"], "Beta")

    # --- tenant space: read-only projection -------------------------------
    def test_platform_tenant_read_exposes_read_only_space(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request(f"/api/platform/tenants/{self.tid}", method="GET",
                             token=token)
        data = self._json(resp)
        space = data["tenant"]["space"]
        # Logical identifier only - never the host directory.
        self.assertEqual(space["id"], "acme")
        self.assertIn(space["status"], ("ready", "unavailable"))
        self.assertEqual(space["isolation"], "dedicated-root")
        raw = resp.data.decode("utf-8")
        self.assertNotIn("shared_root", raw)
        self.assertNotIn(self.shared_root, raw)

    def test_tenant_space_reports_ready_for_an_existing_root(self):
        root_dir = os.path.realpath(tempfile.mkdtemp())
        created = self.svc.create_tenant(
            actor_user_id=self.root["id"], code="spacey", name="Spacey",
            shared_root=root_dir, recent_password="Str0ngAdminPass")
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request(f"/api/platform/tenants/{created['id']}",
                             method="GET", token=token)
        data = self._json(resp)
        self.assertEqual(data["tenant"]["space"]["status"], "ready")
        self.assertNotIn(root_dir, resp.data.decode("utf-8"))

    def test_member_tenant_read_has_no_space_or_host_path(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request("/api/tenant", method="GET", token=token,
                             tenant=self.tid)
        data = self._json(resp)
        # The member-facing read must not gain the platform-only projection.
        self.assertNotIn("space", data["tenant"])
        raw = resp.data.decode("utf-8")
        self.assertNotIn("shared_root", raw)
        self.assertNotIn(self.shared_root, raw)

    # --- tenant create derives controlled shared_root (task 4.4) ----------
    def test_tenant_create_derives_shared_root_when_omitted(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        # Patch a controlled deployment base (tmp, realpath'd like the real
        # resolver) so derivation does not depend on the ambient registry.
        deploy_base = os.path.realpath(tempfile.mkdtemp())
        payload = json.dumps({
            "code": "derived", "name": "Derived",
            "admin_username": "droot", "admin_display": "DRoot",
            "admin_password": "Str0ngPass9",
            "recent_password": "Str0ngAdminPass",
            # A malicious client-supplied server path must be ignored by the
            # handler (design §4: the web form never accepts a shared_root).
            "shared_root": "/etc",
        })
        with patch("auth.service._deployment_shared_base", lambda svc=None: deploy_base):
            resp = self._request("/api/platform/tenants", method="POST",
                                 data=payload, token=token)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        # The created tenant must not carry the client-supplied path; it should be
        # derived under the deployment base (ends with /tenants/<code>) and never
        # exposed back to the client.
        derived = dict(self.svc.get_tenant(next(t["id"] for t in self.svc.list_tenants() if t["code"] == "derived")))
        self.assertTrue(derived["shared_root"].endswith(os.path.join("tenants", "derived")),
                        derived["shared_root"])
        self.assertNotIn("/etc", derived["shared_root"])
        self.assertNotIn("shared_root", data["tenant"])

    def test_tenant_create_missing_deployment_root_fails_cleanly(self):
        # Unit-level: with no resolvable deployment base, create_tenant must raise
        # a config_error (503) rather than persisting an unusable root.
        with patch("auth.service._deployment_shared_base", lambda svc=None: None), \
             patch.object(self.svc, "_require_recent_password", lambda *a, **k: None):
            with self.assertRaises(Exception) as cm:
                self.svc.create_tenant(
                    actor_user_id=self.root["id"], code="noroot", name="NoRoot",
                    admin_username="nroot", admin_display="NRoot",
                    admin_password="Str0ngPass8", recent_password="Str0ngAdminPass")
            self.assertEqual(getattr(cm.exception, "code", None), "config_error")
            self.assertEqual(getattr(cm.exception, "status", None), 503)

    # --- tenant create no longer provisions an admin ----------------------
    def test_tenant_create_without_admin_fields(self):
        """The form posts only code/name/recent_password: the tenant is created
        without any admin account, membership or temporary password."""
        token = self.svc.login("root", "Str0ngAdminPass").token
        deploy_base = os.path.realpath(tempfile.mkdtemp())
        payload = json.dumps({
            "code": "bare", "name": "Bare",
            "recent_password": "Str0ngAdminPass",
        })
        with patch("auth.service._deployment_shared_base", lambda svc=None: deploy_base):
            resp = self._request("/api/platform/tenants", method="POST",
                                 data=payload, token=token)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        self.assertNotIn("admin_username", data["tenant"])
        self.assertNotIn("admin_password", data["tenant"])
        self.assertNotIn("temporary_password", json.dumps(data))
        tid = data["tenant"]["id"]
        self.assertEqual(self.svc._count_valid_tenant_admins(tid), 0)
        self.assertEqual(self.svc.list_members(tid)["total"], 0)

    def test_tenant_create_ignores_stale_admin_fields(self):
        """A cached frontend still sending admin_* must not mint an account
        with a client-chosen username/password."""
        token = self.svc.login("root", "Str0ngAdminPass").token
        deploy_base = os.path.realpath(tempfile.mkdtemp())
        payload = json.dumps({
            "code": "stale", "name": "Stale",
            "admin_username": "attacker", "admin_display": "Attacker",
            "admin_password": "Str0ngPass9",
            "recent_password": "Str0ngAdminPass",
        })
        with patch("auth.service._deployment_shared_base", lambda svc=None: deploy_base):
            resp = self._request("/api/platform/tenants", method="POST",
                                 data=payload, token=token)
        self.assertEqual(self._json(resp)["status"], "success")
        tid = next(t["id"] for t in self.svc.list_tenants() if t["code"] == "stale")
        self.assertEqual(self.svc._count_valid_tenant_admins(tid), 0)
        usernames = {u["username"] for u in self.svc.list_platform_users()}
        self.assertNotIn("attacker", usernames)

    def test_tenant_create_without_admin_requires_recent_password(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        deploy_base = os.path.realpath(tempfile.mkdtemp())
        payload = json.dumps({
            "code": "noauth", "name": "NoAuth",
            "recent_password": "WrongPassword",
        })
        with patch("auth.service._deployment_shared_base", lambda svc=None: deploy_base):
            resp = self._request("/api/platform/tenants", method="POST",
                                 data=payload, token=token)
        self.assertEqual(self._json(resp)["code"], "invalid_old")
        self.assertFalse(any(t["code"] == "noauth" for t in self.svc.list_tenants()))

    def test_bare_tenant_binds_existing_admin_after_create(self):
        """End-to-end indirection: create bare, then bind an existing account."""
        token = self.svc.login("root", "Str0ngAdminPass").token
        deploy_base = os.path.realpath(tempfile.mkdtemp())
        payload = json.dumps({
            "code": "thenbind", "name": "ThenBind",
            "recent_password": "Str0ngAdminPass",
        })
        with patch("auth.service._deployment_shared_base", lambda svc=None: deploy_base):
            resp = self._request("/api/platform/tenants", method="POST",
                                 data=payload, token=token)
        tid = self._json(resp)["tenant"]["id"]
        self.assertEqual(self.svc._count_valid_tenant_admins(tid), 0)
        self.svc.set_tenant_admin(
            actor_user_id=self.root["id"], tenant_id=tid, user_id=self.root["id"],
            display_name="Root", recent_password="Str0ngAdminPass")
        self.assertEqual(self.svc._count_valid_tenant_admins(tid), 1)

    # --- tenant admin account creation (mode dispatch) --------------------

    def _post_admin(self, tid, body, token=None):
        token = token or self.svc.login("root", "Str0ngAdminPass").token
        return self._request(f"/api/platform/tenants/{tid}/admins", method="POST",
                             data=json.dumps(body), token=token)

    def _usernames(self):
        return {u["username"] for u in self.svc.list_platform_users()}

    def test_admins_existing_mode_binds_without_creating_account(self):
        tid = self._bare_tenant()["id"]
        before = self._usernames()
        resp = self._post_admin(tid, {
            "user_id": self.root["id"], "display_name": "Root",
            "recent_password": "Str0ngAdminPass"})
        self.assertEqual(self._json(resp)["status"], "success")
        self.assertTrue(self.svc.get_membership(self.root["id"], tid)["active"])
        self.assertEqual(self._usernames(), before)

    def test_admins_mode_defaults_to_existing(self):
        # Omitting "mode" must keep the historical behaviour for cached clients.
        tid = self._bare_tenant()["id"]
        resp = self._post_admin(tid, {
            "user_id": self.root["id"],
            "recent_password": "Str0ngAdminPass"})
        self.assertEqual(self._json(resp)["status"], "success")
        self.assertTrue(self.svc.get_membership(self.root["id"], tid)["active"])

    def test_admins_new_mode_creates_account_and_binds_admin(self):
        tid = self._bare_tenant()["id"]
        resp = self._post_admin(tid, {
            "mode": "new", "username": "fresh", "display_name": "Fresh",
            "temporary_password": "Str0ngPassTmp",
            "recent_password": "Str0ngAdminPass"})
        self.assertEqual(self._json(resp)["status"], "success")
        self.assertIn("fresh", self._usernames())
        user = [u for u in self.svc.list_platform_users() if u["username"] == "fresh"][0]
        self.assertTrue(self.svc.get_membership(user["id"], tid)["active"])

    def test_admins_new_mode_does_not_echo_password(self):
        tid = self._bare_tenant()["id"]
        resp = self._post_admin(tid, {
            "mode": "new", "username": "quiet", "display_name": "Quiet",
            "temporary_password": "Str0ngPassTmp",
            "recent_password": "Str0ngAdminPass"})
        raw = resp.data.decode("utf-8")
        self.assertNotIn("Str0ngPassTmp", raw)
        self.assertNotIn("temporary_password", raw)
        self.assertNotIn("password_hash", raw)

    def test_admins_unknown_mode_is_rejected_not_silently_downgraded(self):
        tid = self._bare_tenant()["id"]
        resp = self._post_admin(tid, {
            "mode": "bogus", "user_id": self.root["id"],
            "recent_password": "Str0ngAdminPass"})
        self.assertEqual(str(resp.status), "400")
        self.assertEqual(self._json(resp)["code"], "invalid_request")
        self.assertIsNone(self.svc.get_membership(self.root["id"], tid))

    def test_admins_new_mode_requires_all_fields(self):
        tid = self._bare_tenant()["id"]
        resp = self._post_admin(tid, {
            "mode": "new", "username": "", "display_name": "",
            "temporary_password": "", "recent_password": "Str0ngAdminPass"})
        self.assertEqual(str(resp.status), "400")
        self.assertEqual(self._json(resp)["code"], "invalid_request")
        self.assertFalse(any(u["username"] == "" for u in self.svc.list_platform_users()))

    def test_admins_new_mode_duplicate_username_is_conflict(self):
        tid = self._bare_tenant()["id"]
        resp = self._post_admin(tid, {
            "mode": "new", "username": "root", "display_name": "Clash",
            "temporary_password": "Str0ngPassTmp",
            "recent_password": "Str0ngAdminPass"})
        self.assertEqual(str(resp.status), "409")
        self.assertEqual(self._json(resp)["code"], "conflict")

    # --- reading the current tenant admin (read-only) ---------------------

    def _get_admins(self, tid, token):
        return self._request(f"/api/platform/tenants/{tid}/admins", method="GET", token=token)

    def test_admins_get_returns_current_admin_readonly(self):
        tid = self._bare_tenant()["id"]
        self.svc.set_tenant_admin(
            actor_user_id=self.root["id"], tenant_id=tid, user_id=self.root["id"],
            display_name="Root Ops", recent_password="Str0ngAdminPass")
        token = self.svc.login("root", "Str0ngAdminPass").token

        resp = self._get_admins(tid, token)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        self.assertEqual([a["user_id"] for a in data["items"]], [self.root["id"]])
        self.assertEqual(data["items"][0]["display_name"], "Root Ops")
        row = data["items"][0]
        self.assertIs(row["account_active"], True)
        self.assertIs(row["is_platform_admin"], True)
        self.assertIsInstance(row["account_version"], int)
        raw = resp.data.decode("utf-8")
        self.assertNotIn("password", raw)
        self.assertNotIn("hash", raw)

    def test_admins_get_still_returns_a_disabled_account(self):
        """The card re-enables a disabled admin here, so the read must keep it.

        Only the *account* is disabled: the membership and the ``tenant_admin``
        binding are intact, which is exactly the state the card recovers from.
        """
        tid = self._bare_tenant()["id"]
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=tid, operation="create-new",
            username="dana", display_name="Dana", temporary_password="Str0ngPassTmp",
            roles=["tenant_admin"], department_id=None)
        self.svc._store.execute("UPDATE users SET active=0 WHERE username='dana'")
        token = self.svc.login("root", "Str0ngAdminPass").token

        data = self._json(self._get_admins(tid, token))
        self.assertEqual([a["username"] for a in data["items"]], ["dana"])
        self.assertIs(data["items"][0]["account_active"], False)

    def test_admins_get_empty_for_tenant_without_admin(self):
        tid = self._bare_tenant()["id"]
        token = self.svc.login("root", "Str0ngAdminPass").token
        self.assertEqual(self._json(self._get_admins(tid, token))["items"], [])

    def test_admins_get_unknown_tenant_is_404(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._get_admins("tnt_missing", token)
        self.assertEqual(str(resp.status), "404")
        self.assertEqual(self._json(resp)["code"], "not_found")

    def test_admins_get_requires_platform_admin(self):
        tid = self._bare_tenant()["id"]
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=tid, operation="create-new",
            username="alice", display_name="Alice", temporary_password="Str0ngPassTmp",
            roles=["member"], department_id=None)
        # Clear the forced-password gate: a restricted session is rejected at
        # context resolution (403) for an unrelated reason, which would make this
        # test pass even if the platform-admin guard were missing.
        self.svc._store.execute(
            "UPDATE users SET must_change_password=0 WHERE username='alice'")
        token = self.svc.login("alice", "Str0ngPassTmp").token
        resp = self._get_admins(tid, token)
        self.assertEqual(str(resp.status), "403")
        self.assertEqual(self._json(resp)["code"], "forbidden")

    # --- member filters (task 4.5) ----------------------------------------
    def test_members_status_and_role_filters(self):
        root = self.svc.list_platform_users()[0]
        self.svc.create_member(
            actor_user_id=root["id"], tenant_id=self.tid, operation="create-new",
            username="alice", display_name="Alice", temporary_password="Str0ngPassTmp",
            roles=["member"])
        self.svc.create_member(
            actor_user_id=root["id"], tenant_id=self.tid, operation="create-new",
            username="carol", display_name="Carol", temporary_password="Str0ngPassTmp",
            roles=["tenant_admin"])
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request("/api/tenant/members?role=member", method="GET",
                             token=token, tenant=self.tid)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        usernames = {m["username"] for m in data["items"]}
        self.assertIn("alice", usernames)
        self.assertNotIn("carol", usernames)

    def test_members_bad_status_filter(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request("/api/tenant/members?status=bogus", method="GET",
                             token=token, tenant=self.tid)
        self.assertEqual(self._json(resp)["code"], "bad_request")

    # --- administered-tenants (multi-tenant member assignment) ------------

    def _make_admin_in_beta(self):
        """Create beta + gamma tenants; make root a tenant_admin of beta too."""
        self.svc.create_tenant(
            actor_user_id=self.root["id"], code="beta", name="Beta",
            shared_root="/s/beta", admin_username="root2", admin_display="Root2",
            admin_password="Str0ngAdminPass2", recent_password="Str0ngAdminPass")
        self.svc.create_tenant(
            actor_user_id=self.root["id"], code="gamma", name="Gamma",
            shared_root="/s/gamma", admin_username="root3", admin_display="Root3",
            admin_password="Str0ngAdminPass3", recent_password="Str0ngAdminPass")
        beta_tid = next(t for t in self.svc.list_tenants() if t["code"] == "beta")["id"]
        gamma_tid = next(t for t in self.svc.list_tenants() if t["code"] == "gamma")["id"]
        self.svc.set_tenant_admin(
            actor_user_id=self.root["id"], tenant_id=beta_tid, user_id=self.root["id"],
            display_name="Root", recent_password="Str0ngAdminPass")
        return beta_tid, gamma_tid

    def test_administered_tenants_requires_login(self):
        resp = self._request("/api/identity/administered-tenants", method="GET")
        self.assertTrue(str(getattr(resp, "status", "")).startswith("4"))

    def test_administered_tenants_lists_only_admin_tenants(self):
        self._make_admin_in_beta()
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request("/api/identity/administered-tenants", method="GET", token=token)
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        codes = {t["code"] for t in data["items"]}
        self.assertIn("acme", codes)
        self.assertIn("beta", codes)
        self.assertNotIn("gamma", codes)

    def test_administered_tenants_reports_target_membership(self):
        beta_tid, _gamma_tid = self._make_admin_in_beta()
        member = self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid, operation="create-new",
            username="alice", display_name="Alice", temporary_password="Str0ngPassTmp",
            roles=["member"])
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request(
            "/api/identity/administered-tenants?user_id=" + member["user_id"],
            method="GET", token=token)
        data = self._json(resp)
        items = {t["code"]: t for t in data["items"]}
        self.assertEqual(items["acme"]["member"], True)
        self.assertEqual(items["acme"]["member_id"], member["membership_id"])
        self.assertEqual(items["beta"]["member"], False)
        self.assertIn("member_version", items["acme"])

    def test_a_target_outside_the_administered_tenants_is_refused(self):
        """The ``user_id`` param is not an oracle over arbitrary accounts.

        ``root2`` administers beta only; ``alice`` belongs to acme only. Reading
        alice's membership state would tell a beta administrator about an account
        its tenant has no relationship with, so it must be refused rather than
        answered with ``member: false`` for beta and silence for acme.
        """
        self._make_admin_in_beta()
        alice = self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid, operation="create-new",
            username="alice", display_name="Alice", temporary_password="Str0ngPassTmp",
            roles=["member"])
        self.svc.change_password(
            self.svc.login("root2", "Str0ngAdminPass2").token,
            "Str0ngAdminPass2", "Str0ngPass2New")
        beta_admin_token = self.svc.login("root2", "Str0ngPass2New").token

        resp = self._request(
            "/api/identity/administered-tenants?user_id=" + alice["user_id"],
            method="GET", token=beta_admin_token)

        self.assertTrue(str(resp.status).startswith("403"), (resp.status, resp.data))
        self.assertEqual(self._json(resp)["code"], "forbidden")
        self.assertNotIn(b"member_id", resp.data)
        self.assertNotIn(b"items", resp.data)

    def test_an_unknown_target_is_refused_the_same_way(self):
        """A non-existent id must not be distinguishable from an out-of-scope one."""
        self._make_admin_in_beta()
        self.svc.change_password(
            self.svc.login("root2", "Str0ngAdminPass2").token,
            "Str0ngAdminPass2", "Str0ngPass2New")
        beta_admin_token = self.svc.login("root2", "Str0ngPass2New").token

        resp = self._request(
            "/api/identity/administered-tenants?user_id=usr_does_not_exist",
            method="GET", token=beta_admin_token)

        self.assertTrue(str(resp.status).startswith("403"), (resp.status, resp.data))
        self.assertEqual(self._json(resp)["code"], "forbidden")

    def test_a_target_shared_with_the_administered_tenant_is_allowed(self):
        """The control for the refusal: the same call succeeds for a member of beta."""
        beta_tid, _gamma_tid = self._make_admin_in_beta()
        bob = self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=beta_tid, operation="create-new",
            username="bob", display_name="Bob", temporary_password="Str0ngPassTmp",
            roles=["member"])
        self.svc.change_password(
            self.svc.login("root2", "Str0ngAdminPass2").token,
            "Str0ngAdminPass2", "Str0ngPass2New")
        beta_admin_token = self.svc.login("root2", "Str0ngPass2New").token

        resp = self._request(
            "/api/identity/administered-tenants?user_id=" + bob["user_id"],
            method="GET", token=beta_admin_token)

        self.assertEqual(resp.status, "200 OK", resp.data)
        items = {t["code"]: t for t in self._json(resp)["items"]}
        self.assertEqual(list(items), ["beta"])
        self.assertEqual(items["beta"]["member"], True)
        self.assertEqual(items["beta"]["member_id"], bob["membership_id"])

    # --- tools/skills console read gating (platform admin vs member) ------

    def _member_token_without_catalog_grants(self):
        """Login a member holding a role with neither tool.read nor skill.read.

        The built-in ``member`` role now carries the use/create tier, including
        ``tool.read``/``skill.read``, so the functional catalog-read gate is
        exercised with a zero-permission custom role instead.
        """
        self.svc.create_role(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            code="no_catalog", name="No Catalog", permissions=[])
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid, operation="create-new",
            username="tools-alice", display_name="Alice",
            temporary_password="Str0ngPassTmp", roles=["no_catalog"])
        with self.svc._tx() as con:
            con.execute(
                "UPDATE users SET must_change_password=0 WHERE username='tools-alice'")
            con.commit()
        return self.svc.login("tools-alice", "Str0ngPassTmp").token

    def _request_tools_skills(self, path, token):
        """Drive ToolsHandler/SkillsHandler through the real HTTP policy gate.

        A live IdentityService Bearer session + ``X-Tenant-Id`` satisfies the
        gate; the handler then applies catalog-read authorization against the
        resolved context (platform admin / tenant admin bypass, plain member
        requires ``tool.read`` / ``skill.read``).
        """
        def _fake_service():
            return self.svc

        with patch.object(auth_handlers, "_get_service", _fake_service), \
                patch.object(admin_handlers, "_get_service", _fake_service), \
                patch("auth.service.get_identity_service", return_value=self.svc):
            app = web_channel.build_web_app()
            return app.request(
                path, method="GET",
                headers={
                    "Host": "test",
                    "Authorization": f"Bearer {token}",
                    "X-Tenant-Id": self.tid,
                },
            )

    def test_platform_admin_can_list_tools(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request_tools_skills("/api/tools", token)
        # A platform admin must not be 403-gated by a functional tool.read they
        # do not hold; the catalog is returned (even if empty of tools).
        self.assertEqual(resp.status, "200 OK", resp.data[:200])
        data = json.loads(resp.data.decode("utf-8"))
        self.assertEqual(data["status"], "success")

    def test_platform_admin_can_list_skills(self):
        token = self.svc.login("root", "Str0ngAdminPass").token
        resp = self._request_tools_skills("/api/skills", token)
        self.assertEqual(resp.status, "200 OK", resp.data[:200])
        data = json.loads(resp.data.decode("utf-8"))
        self.assertEqual(data["status"], "success")
        self.assertIn("skills", data)

    def test_member_without_tool_grant_is_rejected(self):
        token = self._member_token_without_catalog_grants()
        resp = self._request_tools_skills("/api/tools", token)
        self.assertEqual(resp.status, "403 Forbidden", resp.data[:200])

    def test_member_without_skill_grant_is_rejected(self):
        token = self._member_token_without_catalog_grants()
        resp = self._request_tools_skills("/api/skills", token)
        self.assertEqual(resp.status, "403 Forbidden", resp.data[:200])


    # --- the tenant editor's three-step save sequence ----------------------
    #
    # The batch spans three endpoints in three transactions, so the contract is
    # the *ordering and version threading*, not atomicity: bind an admin, replace
    # both grant kinds in one PUT, then write the profile using the version the
    # PUT left behind. Kept as tests rather than a throwaway script so the
    # sequence stays protected. (These live in the shared class on purpose --
    # subclassing it would re-run the entire suite.)

    PW = "Str0ngAdminPass"

    def _token(self):
        return self.svc.login("root", self.PW).token

    def _create_tenant(self):
        resp = self._request("/api/platform/tenants", method="POST",
                             data=json.dumps({"code": "beta", "name": "Beta",
                                              "recent_password": self.PW}),
                             token=self._token())
        self.assertEqual(resp.status, "200 OK", resp.data[:300])
        return self._json(resp)["tenant"]["id"]

    def _audit_actions(self):
        with self.svc._tx() as con:
            rows = con.execute("SELECT action FROM audit_events ORDER BY id").fetchall()
        return [r["action"] for r in rows]

    def test_admin_then_grants_then_profile_commits_in_order(self):
        tid = self._create_tenant()
        v0 = self.svc.get_tenant(tid)["version"]

        # Step 1: create and bind the tenant admin. This must happen before the
        # tenant is enabled, because enabling requires a valid admin.
        resp = self._post_admin(tid, {
            "mode": "new", "username": "beta-admin", "display_name": "Beta Admin",
            "temporary_password": "Temp0raryPass!", "recent_password": self.PW,
        })
        self.assertEqual(resp.status, "200 OK", resp.data[:300])
        self.assertNotIn("Temp0raryPass!", resp.data.decode("utf-8"))
        self.assertEqual(self.svc.get_tenant(tid)["version"], v0,
                         "binding an admin must not consume a tenant version")

        # Step 2: both grant kinds in a single wholesale PUT.
        v1 = self.svc.get_tenant(tid)["version"]
        grants = [
            {"resource_kind": "model", "resource_id": "provider:deepseek:deepseek-v4-flash",
             "action": "read"},
            {"resource_kind": "tool", "resource_id": "builtin:web-search", "action": "execute"},
        ]
        resp = self._request(f"/api/platform/tenants/{tid}/resources", method="PUT",
                             data=json.dumps({"grants": grants, "expected_version": v1}),
                             token=self._token())
        self.assertEqual(resp.status, "200 OK", resp.data[:300])
        v2 = self.svc.get_tenant(tid)["version"]
        self.assertEqual(v2, v1 + 1, "the grant step bumps the version exactly once")

        # Step 3: the profile write threads the version step 2 produced.
        resp = self._request(f"/api/platform/tenants/{tid}", method="POST",
                             data=json.dumps({"operation": "profile", "name": "Beta Renamed",
                                              "active": True, "expected_version": v2,
                                              "recent_password": self.PW}),
                             token=self._token())
        self.assertEqual(resp.status, "200 OK", resp.data[:300])
        tenant = self._json(resp)["tenant"]
        self.assertEqual(tenant["name"], "Beta Renamed")
        self.assertTrue(tenant["active"])
        self.assertEqual(tenant["version"], v2 + 1,
                         "the profile step bumps the version exactly once")

        # The order is what made "appoint an admin and enable" possible at all.
        with self.svc._tx() as con:
            admins = self.svc._count_valid_tenant_admins(tid, con)
        self.assertGreaterEqual(admins, 1)

        actions = self._audit_actions()
        self.assertIn("tenant.create_admin", actions)
        self.assertIn("tenant.resource_grants.set", actions)
        self.assertIn("tenant.set_profile", actions)

    def _bind_admin(self, tid, username="beta-admin"):
        resp = self._post_admin(tid, {
            "mode": "new", "username": username, "display_name": "Beta Admin",
            "temporary_password": "Temp0raryPass!", "recent_password": self.PW,
        })
        self.assertEqual(resp.status, "200 OK", resp.data[:300])
        return resp

    def test_creating_without_an_admin_leaves_a_tenant_no_profile_write_can_enable(self):
        """Documents a pre-existing inconsistency this change does NOT fix.

        Creating a tenant defaults it to enabled, but nothing forces an admin to
        exist. Meanwhile *any* profile write carrying ``active=True`` -- even to
        a tenant that is already enabled -- is refused until a valid admin is
        bound. The practical consequence is that the operator must appoint the
        admin before the name/enabled save can go through, which is exactly why
        the editor's batch runs the admin step first.

        Live data already contains tenants in this state (see evidence.md), so
        the behaviour is recorded here rather than silently assumed away.
        """
        tid = self._create_tenant()
        self.assertTrue(self.svc.get_tenant(tid)["active"], "creation defaults to enabled")
        with self.svc._tx() as con:
            self.assertEqual(self.svc._count_valid_tenant_admins(tid, con), 0,
                             "no admin exists, yet the tenant is enabled")

        v0 = self.svc.get_tenant(tid)["version"]
        resp = self._request(f"/api/platform/tenants/{tid}", method="POST",
                             data=json.dumps({"operation": "profile", "name": "Renamed",
                                              "active": True, "expected_version": v0,
                                              "recent_password": self.PW}),
                             token=self._token())
        self.assertEqual(str(resp.status), "409")
        self.assertEqual(self._json(resp)["code"], "no_admin")

        # Binding the admin unblocks the identical write, at the same version.
        self._bind_admin(tid)
        self.assertEqual(self.svc.get_tenant(tid)["version"], v0,
                         "still the same version: binding an admin is not a tenant write")
        resp = self._request(f"/api/platform/tenants/{tid}", method="POST",
                             data=json.dumps({"operation": "profile", "name": "Renamed",
                                              "active": True, "expected_version": v0,
                                              "recent_password": self.PW}),
                             token=self._token())
        self.assertEqual(resp.status, "200 OK", resp.data[:300])
        self.assertEqual(self.svc.get_tenant(tid)["name"], "Renamed")

    def test_replaying_a_consumed_version_is_refused_and_changes_nothing(self):
        # A retry that reuses an already-consumed version must fail loudly
        # rather than silently overwrite; this is the 409 the UI surfaces.
        tid = self._create_tenant()
        self._bind_admin(tid, username="replay-admin")
        v0 = self.svc.get_tenant(tid)["version"]
        first = {"operation": "profile", "name": "First", "active": True,
                 "expected_version": v0, "recent_password": self.PW}
        resp = self._request(f"/api/platform/tenants/{tid}", method="POST",
                             data=json.dumps(first), token=self._token())
        self.assertEqual(resp.status, "200 OK", resp.data[:300])

        replay = dict(first, name="Second")
        resp = self._request(f"/api/platform/tenants/{tid}", method="POST",
                             data=json.dumps(replay), token=self._token())
        self.assertEqual(str(resp.status), "409")
        self.assertEqual(self.svc.get_tenant(tid)["name"], "First",
                         "the refused replay changed nothing")

    def test_a_wrong_current_password_is_refused_without_writing(self):
        # The save-time prompt retries in place on exactly this signal.
        tid = self._create_tenant()
        before = self.svc.get_tenant(tid)["version"]
        resp = self._request(f"/api/platform/tenants/{tid}", method="POST",
                             data=json.dumps({"operation": "profile", "name": "Nope",
                                              "active": True, "expected_version": before,
                                              "recent_password": "not-the-password"}),
                             token=self._token())
        self.assertEqual(str(resp.status), "401")
        self.assertEqual(self._json(resp)["code"], "invalid_old")
        after = self.svc.get_tenant(tid)
        self.assertEqual(after["version"], before, "a refused save does not bump the version")
        self.assertEqual(after["name"], "Beta", "a refused save does not rename")


if __name__ == "__main__":
    unittest.main()
