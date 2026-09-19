# encoding:utf-8
"""``todo.assign`` and the projectable member directory it unlocks.

Three things are pinned here:

* the built-in defaults — ``todo.assign`` reaches ``member`` and
  ``tenant_admin``, while ``tenant.members.read`` deliberately stays off
  ``member`` (the existing posture ``tests/test_builtin_role_editing.py``
  records);
* ``_migration_31`` backfills already-provisioned tenants, union-only,
  idempotently, and without touching custom roles or widening anything else;
* the delegation receiver projection returns the *minimum* — a login name and a
  display name — and is confined to one tenant with active membership, account
  and tenant.
"""

import json
import os
import tempfile
import unittest

from auth.policy import (
    MEMBER_DEFAULT_PERMISSIONS,
    PERMISSION_CATALOG,
    PERMISSION_METADATA,
    TENANT_ADMIN_DEFAULT_PERMISSIONS,
)
from auth.service import IdentityService
from auth.store import _migration_31, migration_versions

ASSIGN = "todo.assign"
MEMBERS_READ = "tenant.members.read"


def _mk_db():
    return os.path.join(tempfile.mkdtemp(), "identity.db")


class TodoAssignDefaultsTests(unittest.TestCase):
    def test_catalogue_and_metadata_carry_it(self):
        self.assertIn(ASSIGN, PERMISSION_CATALOG)
        self.assertEqual(PERMISSION_CATALOG.count(ASSIGN), 1)
        meta = PERMISSION_METADATA[ASSIGN]
        self.assertEqual(meta["group"], "待办")
        self.assertTrue(meta["label"])
        self.assertTrue(meta["description"])
        self.assertTrue(meta["assignable"])

    def test_both_builtin_roles_receive_it(self):
        self.assertIn(ASSIGN, MEMBER_DEFAULT_PERMISSIONS)
        self.assertIn(ASSIGN, TENANT_ADMIN_DEFAULT_PERMISSIONS)

    def test_member_defaults_still_exclude_the_member_directory(self):
        """The delegation feature must not widen the member directory.

        ``tenant.members.read`` is deliberately absent from the member set; this
        change introduces its own narrow projection instead of relaxing it.
        """
        self.assertNotIn(MEMBERS_READ, MEMBER_DEFAULT_PERMISSIONS)
        self.assertNotIn("tenant.org.read", MEMBER_DEFAULT_PERMISSIONS)


class _TenantFixture(unittest.TestCase):
    def setUp(self):
        self.db = _mk_db()
        self.svc = IdentityService(self.db)
        self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root=os.path.join(tempfile.mkdtemp(), "acme"))
        self.ta = self.svc.list_tenants()[0]["id"]
        self.root = [u for u in self.svc.list_platform_users()
                     if u["username"] == "root"][0]

    def add_member(self, username, display_name="", roles=("member",)):
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            operation="create-new", username=username,
            display_name=display_name or username,
            temporary_password="MemTempPass1", roles=list(roles))
        return [m for m in self.svc.list_members(self.ta)["items"]
                if m["username"] == username][0]


class Migration31BackfillTests(_TenantFixture):
    def _role_id(self, code):
        return [r for r in self.svc.list_roles(self.ta)
                if r["code"] == code][0]["id"]

    def _perms(self, code):
        row = self.svc._store.execute(
            "SELECT permissions_json, version FROM roles WHERE id=?",
            (self._role_id(code),))[0]
        return set(json.loads(row["permissions_json"] or "[]")), row["version"]

    def _strip(self, code, *ids):
        perms, _ = self._perms(code)
        with self.svc._store.connect() as con:
            con.execute(
                "UPDATE roles SET permissions_json=? WHERE id=?",
                (json.dumps(sorted(perms - set(ids))), self._role_id(code)))
            con.commit()

    def _run(self, fn):
        with self.svc._store.connect() as con:
            fn(con)
            con.commit()

    def test_is_registered_once(self):
        self.assertIn(31, migration_versions())
        self.assertEqual(migration_versions().count(31), 1)

    def test_backfills_both_builtin_roles(self):
        self._strip("member", ASSIGN, MEMBERS_READ)
        self._strip("tenant_admin", ASSIGN)

        self._run(_migration_31)

        self.assertIn(ASSIGN, self._perms("member")[0])
        self.assertIn(ASSIGN, self._perms("tenant_admin")[0])
        # The backfill grants delegation and nothing else.
        self.assertNotIn(MEMBERS_READ, self._perms("member")[0])

    def test_rerun_is_a_noop(self):
        self._strip("member", ASSIGN)
        self._run(_migration_31)
        _, version_after_first = self._perms("member")

        self._run(_migration_31)
        perms, version_after_second = self._perms("member")

        self.assertIn(ASSIGN, perms)
        self.assertEqual(
            version_after_first, version_after_second,
            "a re-run must not rewrite a role that already holds the permission")

    def test_preserves_existing_edits(self):
        self._strip("member", ASSIGN)
        self.add_custom_permission("member", "knowledge.read")

        self._run(_migration_31)

        perms, _ = self._perms("member")
        self.assertIn(ASSIGN, perms)
        self.assertIn("knowledge.read", perms)

    def add_custom_permission(self, code, permission):
        perms, _ = self._perms(code)
        with self.svc._store.connect() as con:
            con.execute(
                "UPDATE roles SET permissions_json=? WHERE id=?",
                (json.dumps(sorted(perms | {permission})), self._role_id(code)))
            con.commit()

    def test_leaves_a_custom_role_alone(self):
        self.svc.create_role(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            code="todo-viewer", name="todo-viewer",
            permissions=["chat.use"],
            resource_grants=[{"resource_kind": "menu",
                              "resource_id": "nav:workbench.todos",
                              "action": "view"}])

        self._run(_migration_31)

        role = [r for r in self.svc.list_roles(self.ta)
                if r["code"] == "todo-viewer"][0]
        self.assertNotIn(ASSIGN, role["permissions"])


class AssignableMemberProjectionTests(_TenantFixture):
    def test_projection_is_only_login_and_display_name(self):
        self.add_member("alice", "Alice Zhang")

        rows = self.svc.list_assignable_members(self.ta)

        self.assertTrue(rows)
        usernames = {r["username"] for r in rows}
        self.assertIn("alice", usernames)
        self.assertIn("root", usernames)
        for row in rows:
            self.assertEqual(
                set(row), {"username", "display_name"},
                "the receiver picker must not carry roles, org or identities")

    def test_inactive_membership_account_or_tenant_is_excluded(self):
        self.add_member("alice")

        with self.svc._store.connect() as con:
            con.execute(
                "UPDATE memberships SET active=0 WHERE tenant_id=? AND user_id=?"
                " AND id=(SELECT id FROM memberships WHERE tenant_id=?"
                "         AND user_id=(SELECT id FROM users WHERE username='alice'))",
                (self.ta, self._user_id("alice"), self.ta))
            con.commit()
        self.assertNotIn("alice", {r["username"]
                                   for r in self.svc.list_assignable_members(self.ta)})

    def test_resolution_is_confined_to_one_tenant(self):
        self.add_member("alice", "Alice")

        found = self.svc.resolve_assignable_member(self.ta, "alice")
        self.assertIsNotNone(found)
        self.assertEqual(found["username"], "alice")
        # The resolver is server-internal and must carry the id the assignee is
        # stored under (``owner_id`` holds a user id, so the two must be
        # comparable). The *picker* projection is the one that withholds it.
        self.assertEqual(found["user_id"], self._user_id("alice"))
        for row in self.svc.list_assignable_members(self.ta):
            self.assertNotIn("user_id", row)

        # A different tenant has no such member, and a missing name is the same
        # answer as an invisible one.
        self.assertIsNone(self.svc.resolve_assignable_member("tenant-other", "alice"))
        self.assertIsNone(self.svc.resolve_assignable_member(self.ta, "nobody"))

    def _user_id(self, username):
        return self.svc._store.execute(
            "SELECT id FROM users WHERE username=?", (username,))[0]["id"]


if __name__ == "__main__":
    unittest.main()
