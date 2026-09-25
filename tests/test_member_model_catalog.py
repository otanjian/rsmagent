# encoding:utf-8
"""模型与接入 is one page with two relative scopes (task 5.4).

The page used to be signed ``platform``, so an ordinary member was told
``{"available": false, "reason": "consumer_closed"}`` while the member-catalog
branch of the projection that answers their real question was unreachable dead
code. This file pins the *two* answers the page now carries, because collapsing
them is the failure this change exists to prevent:

* ``available`` / ``read_allowed`` — whether the caller has a model directory at
  all. It is the same question ``authorization_catalog_minimal`` answers for
  ``kind=model``, so the page and the endpoint behind it cannot disagree.
* ``actions.manage`` — whether the caller may maintain the *public* model
  service address and key. That is the platform qualification alone, which is
  also what ``/api/models`` and ``/config`` gate on.

Both directions are asserted, so "open for nobody" and "open for everybody"
each fail: a member with a grant opens the page and reads exactly the granted
rows, and a member with no grant is refused — while a platform admin keeps the
management actions and a member never reaches the vendor/key interface.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import web

import config
from auth.policy import BUILTIN_MENU_DEFAULTS
from auth.service import IdentityService
from channel.web import web_channel

MODELS_PAGE = "admin.models"

#: The models the fixture's *catalog* offers. Only two providers' worth, because
#: the projection reads the live catalog: a granted model that is not in it
#: cannot be listed, and the fixture has to be able to tell "granted" from
#: "on offer".
CATALOG = [
    {"id": "deepseek", "label": {"zh": "DeepSeek", "en": "DeepSeek"},
     "models": ["deepseek-v4-flash", "deepseek-v4-pro"]},
    {"id": "openai", "label": {"zh": "OpenAI", "en": "OpenAI"},
     "models": ["gpt-4o"]},
]


def _mk_db():
    return os.path.join(tempfile.mkdtemp(), "identity.db")


class _Fixture(unittest.TestCase):
    """acme with a platform admin, a tenant admin and two members."""

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
        self.token_root = self.svc.login("root", "Str0ngRootFinal").token

        self._mk_member("acmeadmin", "Acme Admin", ["tenant_admin"])
        # Each member gets their own role, so "holds a model" and "holds none"
        # are the controlled difference between them (a shared role cannot say
        # that, and the difference is exactly what these tests are about).
        self.role_granted = self._mk_role("modeler_granted")
        self.role_ungranted = self._mk_role("modeler_ungranted")
        self._mk_member("acmegranted", "Acme Granted",
                        [self.role_granted["code"]])
        self._mk_member("acmeungranted", "Acme Ungranted",
                        [self.role_ungranted["code"]])
        self.token_admin = self.svc.login("acmeadmin", "MemPassFinal1").token
        self.token_granted = self.svc.login("acmegranted", "MemPassFinal1").token
        self.token_ungranted = self.svc.login("acmeungranted",
                                             "MemPassFinal1").token

    def _mk_role(self, code):
        """A catalog-only role: it may see and use the models it is granted."""
        return self.svc.create_role(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            code=code, name=code, permissions=["model.read", "model.use"])

    def _allocate_to_tenant(self, resource_id, action, grant_id=None):
        """Allocate a model to the tenant — the platform's ceiling.

        ``tenant_resource_grants`` bounds what a tenant may allocate at all, and
        a member's effective grants are their roles' grants *intersected* with it
        (``IdentityService.resource_ids_for``). A fixture that grants a role a
        model without allocating it to the tenant describes a state the console
        can no longer produce, so every role grant here is paired with the
        matching tenant allocation.
        """
        with self.svc._store.connect() as con:
            con.execute(
                "INSERT INTO tenant_resource_grants(id, tenant_id, resource_kind,"
                " resource_id, action) VALUES(?,?,?,?,?)",
                (grant_id or ("t-" + action + "-" + resource_id), self.ta,
                 "model", resource_id, action))
            con.commit()

    def _deallocate_from_tenant(self, resource_id, action):
        """Take a model back out of the tenant's allocatable limit."""
        with self.svc._store.connect() as con:
            con.execute(
                "DELETE FROM tenant_resource_grants WHERE tenant_id=?"
                " AND resource_kind='model' AND resource_id=? AND action=?",
                (self.ta, resource_id, action))
            con.commit()

    def _grant(self, role_code, resource_id, action, grant_id=None):
        role = [r for r in self.svc.list_roles(self.ta)
                if r["code"] == role_code][0]
        self._allocate_to_tenant(resource_id, action)
        with self.svc._store.connect() as con:
            con.execute(
                "INSERT INTO role_resource_grants(id, tenant_id, role_id,"
                " resource_kind, resource_id, action) VALUES(?,?,?,?,?,?)",
                (grant_id or ("g-" + action), self.ta, role["id"], "model",
                 resource_id, action))
            con.commit()

    def _grant_one_model(self, action="use", resource_id=None):
        self._grant("modeler_granted",
                    resource_id or "provider:deepseek:deepseek-v4-flash",
                    action)

    def _mk_member(self, username, display, roles):
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            operation="create-new", username=username, display_name=display,
            temporary_password="MemTempPass1", roles=roles)
        token = self.svc.login(username, "MemTempPass1").token
        self.svc.change_password(token, "MemTempPass1", "MemPassFinal1")

    def _pages(self, token):
        return self.svc.context_for_tenant(token, self.ta)["console_pages"]

    def _page(self, token):
        return self._pages(token)[MODELS_PAGE]


class ProjectionTests(_Fixture):
    """The page's two answers, asserted in both directions."""

    def test_a_member_with_a_use_grant_opens_the_page(self):
        """A ``use`` grant is what the member's catalog is built from.

        Gating the page on ``read`` alone would hide it from a member whose only
        model grant is ``use`` — the page closed while the endpoint behind it
        returns rows. This is the "granted but unreachable" half.
        """
        self._grant_one_model("use")
        page = self._page(self.token_granted)
        self.assertTrue(page["available"], page)
        self.assertTrue(page["read_allowed"], page)
        self.assertEqual(page["reason"], "", page)

    def test_a_member_with_a_read_grant_alone_also_opens_the_page(self):
        """The other half of the union: ``read`` without ``use`` is a directory.

        The member may see the model without being allowed to run it. The page
        is a catalog, so it opens; the per-kind report keeps the classes apart
        (``catalog`` true, ``execution`` false), which the console reads before
        advertising a use the request would refuse.
        """
        self._grant_one_model("read", resource_id="provider:openai:gpt-4o")
        page = self._page(self.token_granted)
        self.assertTrue(page["read_allowed"], page)
        models = self._pages(self.token_granted)["resources"]["models"]
        self.assertTrue(models["catalog"], models)
        self.assertFalse(models["execution"], models)

    def test_a_member_with_no_model_grant_gets_a_closed_page(self):
        """The control: the page is not offered to everyone.

        Without this, "make the member branch reachable" could be satisfied by
        opening the page to any caller — which would hand the vendor/key
        interface (both tabs) to every member.
        """
        page = self._page(self.token_ungranted)
        self.assertFalse(page["available"], page)
        self.assertFalse(page["read_allowed"], page)
        self.assertEqual(page["reason"], "no_resource_grant", page)
        self.assertFalse(page["actions"].get("manage"), page)

    def test_only_the_platform_qualification_carries_the_management_action(self):
        """``actions.manage`` is what gates the public address/key editors.

        Asserted over all three callers so neither "always true" nor "always
        false" can pass: the platform admin holds it, the tenant admin and the
        member do not (a tenant admin is not the platform's model-service
        maintainer), and the member's page can be open in the same payload.
        """
        self._grant_one_model("use")
        self.assertTrue(self._page(self.token_root)["actions"]["manage"])
        self.assertFalse(self._page(self.token_admin)["actions"]["manage"])
        member = self._page(self.token_granted)
        self.assertTrue(member["available"])
        self.assertFalse(member["actions"]["manage"])

    def test_the_page_and_the_kind_report_agree(self):
        """``read_allowed`` and ``resources.models.catalog`` are one question.

        They are computed from the same grants; reporting them from two
        different rules is how a console ends up showing a closed directory on
        an open page (the shape the dead branch produced in the other direction).
        """
        for token, expected in ((self.token_root, True),
                                (self.token_ungranted, False)):
            pages = self._pages(token)
            page = pages[MODELS_PAGE]
            self.assertEqual(page["read_allowed"],
                             pages["resources"]["models"]["catalog"], token)
            self.assertEqual(page["read_allowed"], expected, token)


class InterfaceTests(_Fixture):
    """The member's catalog endpoint is reachable and the surface is not."""

    def _patch_db(self):
        settings = {
            "identity_mode": "database",
            "identity_db_path": self.db,
            "model": "deepseek-v4-flash",
            "bot_type": "deepseek",
        }
        for target in (config, web_channel):
            p = patch.object(target, "conf", return_value=settings)
            p.start()
            self.addCleanup(p.stop)

    def _get(self, path, token):
        return web_channel.build_web_app().request(
            path, method="GET",
            headers={"Host": "test", "Cookie": f"cow_session={token}",
                     "X-Tenant-ID": self.ta})

    def _catalog(self, token, kind="model"):
        with patch.object(web_channel, "_session_model_catalog",
                          return_value=CATALOG):
            resp = self._get(
                f"/api/tenant/authorization/catalog?kind={kind}&purpose=use",
                token)
        self.assertTrue(str(resp.status).startswith("200"), resp.data[:300])
        return json.loads(resp.data.decode("utf-8"))

    def test_a_member_reads_exactly_the_model_they_are_granted(self):
        """Filtering is the observable effect, not the status code.

        Both models are on offer in the same catalog, so a response that
        contains only the granted one is evidence of the grant filter rather
        than of an empty catalog.
        """
        self._patch_db()
        self._grant_one_model("use")
        body = self._catalog(self.token_granted)
        self.assertEqual([row["name"] for row in body["items"]],
                         ["deepseek-v4-flash"], body)

    def test_a_member_without_a_grant_gets_an_empty_catalog(self):
        """The control for the read above: no grant, no rows — not all rows."""
        self._patch_db()
        body = self._catalog(self.token_ungranted)
        self.assertEqual(body["items"], [], body)

    def test_a_model_taken_out_of_the_tenant_allocation_leaves_the_catalog(self):
        """The tenant allocation is a ceiling, not just an assignment menu.

        Narrowing ``tenant_resource_grants`` does not rewrite the roles that held
        a wider set, so without the intersection the stale ``role_resource_grants``
        row would keep the model in the member's catalog — and in the chat model
        picker — after the platform removed it from the tenant.
        """
        self._patch_db()
        self._grant_one_model("use")
        resource_id = "provider:deepseek:deepseek-v4-flash"
        self.assertEqual([row["name"] for row in self._catalog(self.token_granted)["items"]],
                         ["deepseek-v4-flash"])

        self._deallocate_from_tenant(resource_id, "use")
        body = self._catalog(self.token_granted)
        self.assertEqual(body["items"], [], body)
        self.assertFalse(self._page(self.token_granted)["available"])

    def test_a_member_still_cannot_reach_the_public_model_service(self):
        """The member catalog must not become a door into the platform surface.

        ``/api/models`` (the vendor grid, addresses and keys) and ``/config``
        stay platform-gated: a member reading their granted catalog is not the
        maintainer of the service it points at.
        """
        self._patch_db()
        self._grant_one_model("use")
        for path in ("/api/models", "/config"):
            resp = self._get(path, self.token_granted)
            self.assertTrue(str(resp.status).startswith("403"),
                            (path, resp.status))

    def test_the_public_model_service_answers_the_platform_admin(self):
        """The control for the refusal above: it is a gate, not an outage."""
        self._patch_db()
        resp = self._get("/api/models", self.token_root)
        self.assertTrue(str(resp.status).startswith("200"), resp.data[:300])


class ReachabilityTests(_Fixture):
    """The catalog must be *reachable*, which the page projection alone cannot say.

    Menu grants are restrictive, so a page with no ``nav:`` grant is hidden from
    the built-in roles no matter what the projection reports. The two halves live
    in different files (``auth/service.py`` / ``auth/policy.py``) and can drift
    apart silently — an open page that nobody's menu lists is exactly the
    "unreachable branch" this task set out to remove — so the link is asserted.
    """

    def test_the_builtin_defaults_carry_the_page(self):
        for code in ("member", "tenant_admin"):
            self.assertIn("nav:" + MODELS_PAGE,
                          BUILTIN_MENU_DEFAULTS[code], code)

    def test_a_builtin_member_with_a_model_grant_reaches_the_page(self):
        """The end-to-end shape: default grant + model grant = open page."""
        self._mk_member("plainm", "Plain M", ["member"])
        self._grant("member", "provider:deepseek:deepseek-v4-flash", "use")
        page = self._page(self.svc.login("plainm", "MemPassFinal1").token)

        self.assertFalse(page.get("menu_denied"), page)
        self.assertTrue(page["available"], page)
        self.assertTrue(page["read_allowed"], page)

    def test_a_builtin_member_without_a_model_grant_sees_no_entry(self):
        """The control, and the reason the default is safe to widen.

        The page is *granted* by the menu yet reported unavailable, so the
        console's own availability rule hides the entry and a member with no
        model of their own never sees an empty 模型与接入.
        """
        self._mk_member("plainn", "Plain N", ["member"])
        page = self._page(self.svc.login("plainn", "MemPassFinal1").token)

        self.assertFalse(page.get("menu_denied"), page)
        self.assertFalse(page["available"], page)

    def test_a_tenant_that_predates_the_page_gains_it_once(self):
        """``_migration_27`` is the backfill for tenants seeded before it existed."""
        from auth.store import _migration_27

        self._drop_grant("member")
        self._drop_grant("tenant_admin")
        self._run(_migration_27)
        self.assertEqual(
            {r["resource_id"] for r in self.svc._store.execute(
                "SELECT resource_id FROM role_resource_grants WHERE role_id=?"
                " AND resource_kind='menu'", (self._role_id("member"),))}
            & {"nav:" + MODELS_PAGE}, {"nav:" + MODELS_PAGE})

        self._run(_migration_27)

        rows = self.svc._store.execute(
            "SELECT COUNT(*) c FROM role_resource_grants WHERE role_id=?"
            " AND resource_kind='menu' AND resource_id='nav:admin.models'",
            (self._role_id("member"),))
        self.assertEqual(rows[0]["c"], 1, "a re-run must not duplicate the grant")

    def test_it_does_not_switch_gating_on_for_a_role_with_no_menu_grants(self):
        """A role governed by the compat rule must be left exactly as it was.

        Adding a grant to a role that carries none is not a backfill: it turns
        menu gating *on* for that role and hides every page it does not list.
        """
        from auth.store import _migration_27

        self._clear_menu_grants("member")
        self._run(_migration_27)

        self.assertEqual(self._menu_grants("member"), set())

    def test_it_leaves_a_custom_role_alone(self):
        from auth.store import _migration_27

        role = self.svc.create_role(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            code="cataloger", name="cataloger", permissions=["model.read"],
            resource_grants=[{"resource_kind": "menu",
                              "resource_id": "nav:workbench.chat",
                              "action": "view"}])
        self._run(_migration_27)

        rows = {r["resource_id"] for r in self.svc._store.execute(
            "SELECT resource_id FROM role_resource_grants WHERE role_id=?",
            (role["id"],))}
        self.assertEqual(rows, {"nav:workbench.chat"})

    def _role_id(self, code):
        return [r for r in self.svc.list_roles(self.ta)
                if r["code"] == code][0]["id"]

    def _menu_grants(self, code):
        return {r["resource_id"] for r in self.svc._store.execute(
            "SELECT resource_id FROM role_resource_grants WHERE role_id=?"
            " AND resource_kind='menu'", (self._role_id(code),))}

    def _drop_grant(self, code):
        with self.svc._store.connect() as con:
            con.execute(
                "DELETE FROM role_resource_grants WHERE role_id=?"
                " AND resource_kind='menu' AND resource_id='nav:admin.models'",
                (self._role_id(code),))
            con.commit()

    def _clear_menu_grants(self, code):
        with self.svc._store.connect() as con:
            con.execute(
                "DELETE FROM role_resource_grants WHERE role_id=?"
                " AND resource_kind='menu'", (self._role_id(code),))
            con.commit()

    def _run(self, migration):
        with self.svc._store.connect() as con:
            migration(con)
            con.commit()


if __name__ == "__main__":
    unittest.main()
