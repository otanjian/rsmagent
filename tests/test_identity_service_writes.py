# encoding:utf-8
"""Focused tests for member/role/department service invariants.

Covers the tenant-admin-only write gate, admin-continuity protection, version
conflict handling, cross-tenant object rejection, and the last-admin guard.
"""

import os
import tempfile
import time
import unittest
from unittest.mock import patch

from auth.service import IdentityService, IdentityServiceError


def _db_path():
    return os.path.join(tempfile.mkdtemp(), "identity.db")


def _setup():
    svc = IdentityService(_db_path())
    svc.bootstrap(
        tenant_code="acme", tenant_name="Acme", admin_username="root",
        admin_display="Root", admin_password="Str0ngAdminPass", shared_root="/s/acme")
    tid = svc.list_tenants()[0]["id"]
    root = svc.list_platform_users()[0]
    return svc, tid, root


class MemberWriteGateTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.tid, self.root = _setup()

    def test_member_create_by_admin(self):
        m = self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid, operation="create-new",
            username="alice", display_name="Alice", temporary_password="Str0ngPassTmp",
            roles=[], department_id=None, position_text="")
        self.assertTrue(m["membership_id"])

    def test_member_create_rejects_weak_password(self):
        with self.assertRaises(IdentityServiceError):
            self.svc.create_member(
                actor_user_id=self.root["id"], tenant_id=self.tid, operation="create-new",
                username="bob", display_name="Bob", temporary_password="password", roles=[])

    def test_member_create_short_password_is_structured_rejection(self):
        # Regression: a temporary password shorter than MIN_PASSWORD_LENGTH used
        # to reach hash_password first, whose PasswordError is not an
        # IdentityServiceError — so it escaped the handler as a 500/HTML body
        # and the console reported a generic "load-failed". The rejection must
        # be the same structured weak_password the in-transaction validation
        # produces.
        with self.assertRaises(IdentityServiceError) as e:
            self.svc.create_member(
                actor_user_id=self.root["id"], tenant_id=self.tid, operation="create-new",
                username="bob", display_name="Bob", temporary_password="123456", roles=[])
        self.assertEqual(e.exception.code, "weak_password")

    def test_member_create_blank_password_is_structured_rejection(self):
        # A whitespace-only password passes the length check but hash_password
        # rejects it as blank; that too must surface as weak_password, not a 500.
        with self.assertRaises(IdentityServiceError) as e:
            self.svc.create_member(
                actor_user_id=self.root["id"], tenant_id=self.tid, operation="create-new",
                username="bob", display_name="Bob", temporary_password="        ", roles=[])
        self.assertEqual(e.exception.code, "weak_password")

    def test_non_admin_cannot_write_members(self):
        # a fresh non-admin user with no membership is rejected before any write
        with self.assertRaises(IdentityServiceError) as e:
            self.svc.create_member(
                actor_user_id="someone", tenant_id=self.tid, operation="create-new",
                username="carl", display_name="Carl", temporary_password="Str0ngPassTmp", roles=[])
        self.assertEqual(e.exception.status, 403)

    def test_bind_existing_preserves_global_password(self):
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid, operation="create-new",
            username="alice", display_name="Alice", temporary_password="Str0ngPassTmp", roles=[])
        # bind the same user into a second tenant, ensure password unchanged
        self.svc.create_tenant(
            actor_user_id=self.root["id"], code="beta", name="Beta", shared_root="/s/beta",
            admin_username="betaadmin", admin_display="Beta", admin_password="Str0ngPass2",
            recent_password="Str0ngAdminPass")
        beta = [t for t in self.svc.list_tenants() if t["code"] == "beta"][0]
        # alice is root's member; bind root (existing) into beta, must not error
        self.svc.set_tenant_admin(actor_user_id=self.root["id"], tenant_id=beta["id"],
                                  user_id=self.root["id"], display_name="Root",
                                  recent_password="Str0ngAdminPass")


class AdminContinuityTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.tid, self.root = _setup()

    def test_cannot_remove_last_tenant_admin(self):
        membership = self.svc.get_membership(self.root["id"], self.tid)
        # attempt to demote the only admin to member
        with self.assertRaises(IdentityServiceError) as e:
            self.svc.update_member(
                actor_user_id=self.root["id"], tenant_id=self.tid,
                member_id=membership["id"], display_name="Root", active=True,
                roles=["member"], department_id=None, position_text="",
                expected_version=membership["version"])
        self.assertEqual(e.exception.code, "last_admin")

    def test_add_second_admin_then_remove_first_ok(self):
        # create a second member and make them admin
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid, operation="create-new",
            username="alice", display_name="Alice", temporary_password="Str0ngPassTmp",
            roles=["tenant_admin"], department_id=None)
        members = self.svc.list_members(self.tid)["items"]
        alice = [m for m in members if m["username"] == "alice"][0]
        # now demote root (no longer last admin) - should succeed
        root_membership = self.svc.get_membership(self.root["id"], self.tid)
        result = self.svc.update_member(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            member_id=root_membership["id"], display_name="Root", active=True,
            roles=["member"], department_id=None, position_text="",
            expected_version=root_membership["version"])
        self.assertTrue(result)


class VersionConflictTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.tid, self.root = _setup()

    def test_expected_version_guard(self):
        membership = self.svc.get_membership(self.root["id"], self.tid)
        with self.assertRaises(IdentityServiceError) as e:
            self.svc.update_member(
                actor_user_id=self.root["id"], tenant_id=self.tid,
                member_id=membership["id"], display_name="Root2", active=True,
                roles=["tenant_admin"], department_id=None, position_text="",
                expected_version=membership["version"] + 5)
        self.assertEqual(e.exception.code, "conflict")


class DepartmentTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.tid, self.root = _setup()

    def test_create_department_and_delete_after_clear(self):
        dept = self.svc.create_department(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            code="eng", name="Engineering", parent_id=None, sort_order=0)
        self.assertTrue(dept["id"])
        # delete with no children/members -> ok
        result = self.svc.delete_department(
            actor_user_id=self.root["id"], tenant_id=self.tid, dept_id=dept["id"])
        self.assertTrue(result["deleted"])

    def test_delete_department_in_use_rejected(self):
        dept = self.svc.create_department(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            code="eng", name="Engineering", parent_id=None)
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid, operation="create-new",
            username="alice", display_name="Alice", temporary_password="Str0ngPassTmp",
            roles=[], department_id=dept["id"])
        with self.assertRaises(IdentityServiceError) as e:
            self.svc.delete_department(
                actor_user_id=self.root["id"], tenant_id=self.tid, dept_id=dept["id"])
        self.assertEqual(e.exception.code, "in_use")

    def test_cannot_delete_virtual_root(self):
        root_dept = [d for d in self.svc.list_departments(self.tid) if d["code"] == "__root__"][0]
        with self.assertRaises(IdentityServiceError) as e:
            self.svc.delete_department(
                actor_user_id=self.root["id"], tenant_id=self.tid, dept_id=root_dept["id"])
        self.assertEqual(e.exception.code, "forbidden")


class TenantProfileTests(unittest.TestCase):
    """``set_tenant_profile``: name + active commit in one transaction.

    The tenant page edits both fields with a single save, so the write must bump
    the version exactly once, audit exactly once, and still apply both existing
    guards (enable requires a valid tenant_admin; disable must not strand an
    enabled account without an active tenant). Any rejected guard rolls back the
    name change too - there is no partial apply.
    """

    def setUp(self):
        self.svc, self.tid, self.root = _setup()

    def _bare(self, code="beta"):
        """A tenant with no members: safe to disable, impossible to enable."""
        return self.svc.create_tenant(
            actor_user_id=self.root["id"], code=code, name=code.title(),
            shared_root="/s/" + code, recent_password="Str0ngAdminPass")

    def _actions(self):
        return [r["action"] for r in self.svc._store.execute(
            "SELECT action FROM audit_events ORDER BY rowid")]

    def test_rename_and_disable_commits_once_with_single_audit(self):
        tid = self._bare()["id"]
        result = self.svc.set_tenant_profile(
            actor_user_id=self.root["id"], tenant_id=tid, name="Beta Renamed",
            active=False, expected_version=1, recent_password="Str0ngAdminPass")
        after = dict(self.svc.get_tenant(tid))
        self.assertEqual(after["name"], "Beta Renamed")
        self.assertEqual(after["active"], 0)
        # One user action, one version bump - not two.
        self.assertEqual(after["version"], 2)
        self.assertEqual(result["version"], 2)
        actions = self._actions()
        self.assertEqual(actions.count("tenant.set_profile"), 1)
        self.assertNotIn("tenant.rename", actions)
        self.assertNotIn("tenant.set_status", actions)

    def test_rename_and_enable_without_valid_admin_is_rejected_wholly(self):
        tid = self._bare()["id"]
        self.svc.set_tenant_status(self.root["id"], tid, False, 1, "Str0ngAdminPass")
        with self.assertRaises(IdentityServiceError) as e:
            self.svc.set_tenant_profile(
                actor_user_id=self.root["id"], tenant_id=tid, name="Beta Renamed",
                active=True, expected_version=2, recent_password="Str0ngAdminPass")
        self.assertEqual(e.exception.code, "no_admin")
        self.assertEqual(e.exception.status, 409)
        after = dict(self.svc.get_tenant(tid))
        # The rejected enable must not leave the rename behind.
        self.assertEqual(after["name"], "Beta")
        self.assertEqual(after["active"], 0)
        self.assertEqual(after["version"], 2)

    def test_rename_and_disable_breaking_continuity_is_rejected_wholly(self):
        # acme's only active member is the platform admin, so disabling it would
        # leave that enabled account with no active tenant.
        with self.assertRaises(IdentityServiceError) as e:
            self.svc.set_tenant_profile(
                actor_user_id=self.root["id"], tenant_id=self.tid,
                name="Acme Renamed", active=False, expected_version=1,
                recent_password="Str0ngAdminPass")
        self.assertEqual(e.exception.code, "last_active_tenant_required")
        after = dict(self.svc.get_tenant(self.tid))
        self.assertEqual(after["name"], "Acme")
        self.assertEqual(after["active"], 1)
        self.assertEqual(after["version"], 1)

    def test_profile_version_conflict_changes_nothing(self):
        tid = self._bare()["id"]
        with self.assertRaises(IdentityServiceError) as e:
            self.svc.set_tenant_profile(
                actor_user_id=self.root["id"], tenant_id=tid, name="Nope",
                active=False, expected_version=99, recent_password="Str0ngAdminPass")
        self.assertEqual(e.exception.code, "conflict")
        self.assertEqual(e.exception.status, 409)
        after = dict(self.svc.get_tenant(tid))
        self.assertEqual(after["name"], "Beta")
        self.assertEqual(after["active"], 1)
        self.assertEqual(after["version"], 1)

    def test_profile_rolls_back_when_audit_write_fails(self):
        tid = self._bare()["id"]
        with patch.object(self.svc, "_audit_in_tx",
                          side_effect=RuntimeError("audit store down")):
            with self.assertRaises(RuntimeError):
                self.svc.set_tenant_profile(
                    actor_user_id=self.root["id"], tenant_id=tid, name="Nope",
                    active=False, expected_version=1,
                    recent_password="Str0ngAdminPass")
        after = dict(self.svc.get_tenant(tid))
        self.assertEqual(after["name"], "Beta")
        self.assertEqual(after["active"], 1)
        self.assertEqual(after["version"], 1)
        self.assertNotIn("tenant.set_profile", self._actions())

    def test_profile_rejected_paths_change_nothing(self):
        tid = self._bare()["id"]
        with self.assertRaises(IdentityServiceError) as e:
            self.svc.set_tenant_profile(
                actor_user_id="nobody", tenant_id=tid, name="X", active=True,
                expected_version=1, recent_password="Str0ngAdminPass")
        self.assertEqual(e.exception.status, 403)
        with self.assertRaises(IdentityServiceError) as e:
            self.svc.set_tenant_profile(
                actor_user_id=self.root["id"], tenant_id=tid, name="X", active=True,
                expected_version=1, recent_password="WrongPassword")
        self.assertEqual(e.exception.code, "invalid_old")
        with self.assertRaises(IdentityServiceError) as e:
            self.svc.set_tenant_profile(
                actor_user_id=self.root["id"], tenant_id=tid, name="   ",
                active=True, expected_version=1,
                recent_password="Str0ngAdminPass")
        self.assertEqual(e.exception.code, "bad_request")
        after = dict(self.svc.get_tenant(tid))
        self.assertEqual(after["name"], "Beta")
        self.assertEqual(after["active"], 1)
        self.assertEqual(after["version"], 1)


class TenantAdminDisplayNameTests(unittest.TestCase):
    """`set_tenant_admin` must honour display_name for an existing member.

    The tenant editor's "pick an account, then rename it" flow writes a
    per-tenant membership display name. The server only wrote ``display_name``
    when it created a *new* membership, so editing an existing member was
    silently dropped: the operator saw success with nothing changed.
    """

    def setUp(self):
        self.svc, self.tid, self.root = _setup()

    def _membership(self, user_id, tenant_id=None):
        return self.svc.get_membership(user_id, tenant_id or self.tid)

    def _global_name(self, user_id):
        users = self.svc.list_platform_users()
        return [u for u in users if u["id"] == user_id][0]["display_name"]

    def _beta(self):
        self.svc.create_tenant(
            actor_user_id=self.root["id"], code="beta", name="Beta", shared_root="/s/beta",
            admin_username="betaadmin", admin_display="Beta", admin_password="Str0ngPass2",
            recent_password="Str0ngAdminPass")
        return [t for t in self.svc.list_tenants() if t["code"] == "beta"][0]

    def test_existing_member_display_name_is_updated_and_versioned(self):
        before = self._membership(self.root["id"])
        self.assertNotEqual(before["display_name"], "Root Renamed")

        self.svc.set_tenant_admin(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            user_id=self.root["id"], display_name="Root Renamed",
            recent_password="Str0ngAdminPass")

        after = self._membership(self.root["id"])
        self.assertEqual(after["display_name"], "Root Renamed")
        self.assertEqual(after["version"], before["version"] + 1)

    def test_same_display_name_does_not_bump_version(self):
        # Guard against over-eager updates once the write path exists.
        before = self._membership(self.root["id"])
        self.svc.set_tenant_admin(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            user_id=self.root["id"], display_name=before["display_name"],
            recent_password="Str0ngAdminPass")
        after = self._membership(self.root["id"])
        self.assertEqual(after["version"], before["version"])

    def test_rename_leaves_global_name_and_other_tenants_untouched(self):
        beta = self._beta()
        self.svc.set_tenant_admin(
            actor_user_id=self.root["id"], tenant_id=beta["id"],
            user_id=self.root["id"], display_name="Root In Beta",
            recent_password="Str0ngAdminPass")
        beta_before = self._membership(self.root["id"], beta["id"])
        global_before = self._global_name(self.root["id"])

        self.svc.set_tenant_admin(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            user_id=self.root["id"], display_name="Root Renamed",
            recent_password="Str0ngAdminPass")

        beta_after = self._membership(self.root["id"], beta["id"])
        self.assertEqual(beta_after["display_name"], beta_before["display_name"])
        self.assertEqual(beta_after["version"], beta_before["version"])
        self.assertEqual(self._global_name(self.root["id"]), global_before)
        self.assertEqual(self._membership(self.root["id"])["display_name"], "Root Renamed")

    def test_recovered_membership_takes_new_display_name(self):
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid, operation="create-new",
            username="alice", display_name="Alice", temporary_password="Str0ngPassTmp",
            roles=["tenant_admin"], department_id=None)
        alice = [m for m in self.svc.list_members(self.tid)["items"]
                 if m["username"] == "alice"][0]
        # Alice needs a second active tenant before her acme membership can be
        # deactivated: an enabled account must keep at least one active tenant.
        beta = self._beta()
        self.svc.set_tenant_admin(
            actor_user_id=self.root["id"], tenant_id=beta["id"],
            user_id=alice["user_id"], display_name="Alice In Beta",
            recent_password="Str0ngAdminPass")
        self.svc.update_member(
            actor_user_id=self.root["id"], tenant_id=self.tid, member_id=alice["id"],
            display_name="Alice", active=False, roles=["tenant_admin"], department_id=None,
            position_text="", expected_version=alice["version"])
        before = self._membership(alice["user_id"])
        self.assertFalse(before["active"])

        self.svc.set_tenant_admin(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            user_id=alice["user_id"], display_name="Alice Recovered",
            recent_password="Str0ngAdminPass")

        after = self._membership(alice["user_id"])
        self.assertTrue(after["active"])
        self.assertEqual(after["display_name"], "Alice Recovered")
        self.assertEqual(after["version"], before["version"] + 1)


class TenantAdminAccountCreateTests(unittest.TestCase):
    """Platform-side creation of a brand new tenant-admin account.

    The tenant editor may create an account instead of binding an existing one.
    The account, its membership and the tenant_admin binding must be committed
    together: a failure must not leave an orphan account behind.
    """

    def setUp(self):
        self.svc, self.tid, self.root = _setup()

    def _bare(self, code="beta"):
        """A tenant with no members at all."""
        return self.svc.create_tenant(
            actor_user_id=self.root["id"], code=code, name=code.title(),
            shared_root="/s/" + code, recent_password="Str0ngAdminPass")

    def _create(self, tid, username="newadmin", display_name="New Admin",
                temporary_password="Str0ngPassTmp", actor=None):
        return self.svc.create_tenant_admin_account(
            actor_user_id=actor or self.root["id"], tenant_id=tid,
            username=username, display_name=display_name,
            temporary_password=temporary_password,
            recent_password="Str0ngAdminPass")

    def _actions(self):
        return [r["action"] for r in self.svc._store.execute(
            "SELECT action FROM audit_events ORDER BY rowid")]

    def _user_row(self, username):
        rows = self.svc._store.execute(
            "SELECT id, username, display_name, active, must_change_password,"
            " temp_password_expires_at FROM users WHERE username=?", (username,))
        return dict(rows[0]) if rows else None

    def _admins(self, tid):
        return [m for m in self.svc.list_members(tid)["items"]
                if m["active"] and "tenant_admin" in m["role_codes"]]

    def test_creates_account_membership_admin_binding_and_audit(self):
        tid = self._bare()["id"]
        result = self._create(tid)

        user = self._user_row("newadmin")
        self.assertIsNotNone(user)
        self.assertEqual(user["id"], result["user_id"])
        membership = self.svc.get_membership(user["id"], tid)
        self.assertTrue(membership["active"])
        self.assertEqual([m["username"] for m in self._admins(tid)], ["newadmin"])
        self.assertEqual(self._actions().count("tenant.create_admin"), 1)

    def test_new_account_requires_password_change_with_expiry(self):
        tid = self._bare()["id"]
        self._create(tid)
        user = self._user_row("newadmin")
        self.assertEqual(user["active"], 1)
        self.assertEqual(user["must_change_password"], 1)
        delta = user["temp_password_expires_at"] - int(time.time())
        # Same 3-day window the member-create flow uses.
        self.assertGreater(delta, 86400 * 2)
        self.assertLessEqual(delta, 86400 * 3 + 60)

    def test_duplicate_username_rejected_without_partial_write(self):
        tid = self._bare()["id"]
        before = self._actions()
        with self.assertRaises(IdentityServiceError) as e:
            self._create(tid, username="root", display_name="Clash")
        self.assertEqual(e.exception.code, "conflict")
        self.assertEqual(e.exception.status, 409)
        self.assertEqual(self._admins(tid), [])
        self.assertEqual(self._actions(), before)

    def test_weak_password_rejected_without_account(self):
        tid = self._bare()["id"]
        with self.assertRaises(IdentityServiceError) as e:
            self._create(tid, username="weakling", temporary_password="password")
        self.assertEqual(e.exception.code, "weak_password")
        self.assertIsNone(self._user_row("weakling"))
        self.assertEqual(self._admins(tid), [])

    def test_invalid_username_rejected_without_account(self):
        tid = self._bare()["id"]
        with self.assertRaises(IdentityServiceError) as e:
            self._create(tid, username="bad name!")
        self.assertEqual(e.exception.code, "invalid_username")
        self.assertEqual(self._admins(tid), [])

    def test_missing_admin_role_rolls_back_account_creation(self):
        # No tenant_admin role => the binding step fails. The account INSERT
        # must roll back with it rather than leaving an orphan account.
        tid = self._bare()["id"]
        # The built-in role now carries default menu grants, and
        # ``role_resource_grants`` references ``roles`` by foreign key. The
        # service's own ``delete_role`` clears grants before the role row; this
        # fixture removes the role by hand, so it has to do the same.
        self.svc._store.execute(
            "DELETE FROM role_resource_grants WHERE tenant_id=? AND role_id IN"
            " (SELECT id FROM roles WHERE tenant_id=? AND code=?)",
            (tid, tid, "tenant_admin"))
        self.svc._store.execute(
            "DELETE FROM roles WHERE tenant_id=? AND code=?", (tid, "tenant_admin"))
        with self.assertRaises(IdentityServiceError) as e:
            self._create(tid, username="orphanme")
        self.assertEqual(e.exception.code, "missing_role")
        self.assertIsNone(self._user_row("orphanme"))
        self.assertEqual(self._admins(tid), [])

    def test_wrong_recent_password_rejected_without_account(self):
        tid = self._bare()["id"]
        with self.assertRaises(IdentityServiceError) as e:
            self.svc.create_tenant_admin_account(
                actor_user_id=self.root["id"], tenant_id=tid,
                username="nope", display_name="Nope",
                temporary_password="Str0ngPassTmp", recent_password="WrongPassword")
        self.assertEqual(e.exception.code, "invalid_old")
        self.assertIsNone(self._user_row("nope"))

    def test_audit_failure_rolls_back_account_creation(self):
        # Same failure injection the profile tests use: the audit write blows up
        # mid-transaction, so the account and membership must vanish with it.
        tid = self._bare()["id"]
        with patch.object(self.svc, "_audit_in_tx",
                          side_effect=RuntimeError("audit store down")):
            with self.assertRaises(RuntimeError):
                self._create(tid, username="auditfail")
        self.assertIsNone(self._user_row("auditfail"))
        self.assertEqual(self._admins(tid), [])

    def test_unknown_tenant_rejected(self):
        with self.assertRaises(IdentityServiceError) as e:
            self._create("tnt_does_not_exist", username="ghost")
        self.assertEqual(e.exception.status, 404)
        self.assertIsNone(self._user_row("ghost"))

    def test_created_admin_satisfies_enable_continuity(self):
        tid = self._bare()["id"]
        self.svc.set_tenant_status(self.root["id"], tid, False, 1, "Str0ngAdminPass")
        self._create(tid)
        # Enabling now succeeds because a valid tenant_admin exists.
        self.svc.set_tenant_status(self.root["id"], tid, True, 2, "Str0ngAdminPass")
        self.assertEqual(self.svc.get_tenant(tid)["active"], 1)

    def test_non_platform_admin_rejected_and_gets_no_membership(self):
        tid = self._bare()["id"]
        with self.assertRaises(IdentityServiceError) as e:
            self._create(tid, username="sneaky", actor="nobody")
        self.assertEqual(e.exception.status, 403)
        self.assertIsNone(self._user_row("sneaky"))
        self.assertEqual(self._admins(tid), [])

    def test_operator_does_not_gain_membership_in_target_tenant(self):
        tid = self._bare()["id"]
        self._create(tid)
        self.assertIsNone(self.svc.get_membership(self.root["id"], tid))

    def test_new_membership_takes_requested_display_name(self):
        tid = self._bare()["id"]
        result = self._create(tid, display_name="Ops Admin")
        self.assertEqual(
            self.svc.get_membership(result["user_id"], tid)["display_name"], "Ops Admin")


class TenantAdminReadTests(unittest.TestCase):
    """The read-only projection the tenant editor uses to show the current admin.

    A binding qualifies while its membership is active and it still holds
    ``tenant_admin``. The account itself may be disabled: the card shows the
    account's state and offers its enable/disable control, so a disabled admin
    must remain locatable there to be re-enabled. A disabled account is still
    not a *valid* admin — ``_count_valid_tenant_admins`` keeps excluding it.
    """

    def setUp(self):
        self.svc, self.tid, self.root = _setup()

    def _bare(self, code="beta"):
        return self.svc.create_tenant(
            actor_user_id=self.root["id"], code=code, name=code.title(),
            shared_root="/s/" + code, recent_password="Str0ngAdminPass")

    def _add_admin(self, username, display_name):
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid, operation="create-new",
            username=username, display_name=display_name,
            temporary_password="Str0ngPassTmp", roles=["tenant_admin"], department_id=None)

    def test_lists_valid_admins_earliest_first_without_credentials(self):
        self.svc.set_tenant_admin(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            user_id=self.root["id"], display_name="Root Ops",
            recent_password="Str0ngAdminPass")
        self._add_admin("alice", "Alice")

        admins = self.svc.tenant_admins(self.tid)
        self.assertEqual([a["username"] for a in admins], ["root", "alice"],
                         "the earliest bound admin comes first")
        self.assertEqual(admins[0]["display_name"], "Root Ops",
                         "the membership display name is what the editor shows")
        self.assertEqual(admins[0]["user_id"], self.root["id"])
        for row in admins:
            self.assertEqual(
                set(row),
                {"membership_id", "user_id", "username", "display_name",
                 "account_active", "account_version", "is_platform_admin"},
                "the projection carries no password material or other columns")
            self.assertIsInstance(row["account_active"], bool)
            self.assertIsInstance(row["is_platform_admin"], bool)
            self.assertIsInstance(row["account_version"], int)

    def test_fields_come_from_the_same_account_as_the_binding(self):
        self._add_admin("carol", "Carol")
        carol_id = self.svc._store.execute(
            "SELECT id FROM users WHERE username='carol'")[0]["id"]
        self.svc._store.execute(
            "UPDATE users SET version=7, is_platform_admin=1 WHERE id=?", (carol_id,))
        row = [a for a in self.svc.tenant_admins(self.tid)
               if a["username"] == "carol"][0]
        self.assertEqual(row["user_id"], carol_id)
        self.assertEqual(row["account_version"], 7,
                         "the version belongs to the bound account, not the membership")
        self.assertTrue(row["is_platform_admin"],
                        "the platform qualification is read from the same account")

    def test_keeps_disabled_account_so_the_card_can_reenable_it(self):
        self._add_admin("carol", "Carol")
        self.svc._store.execute("UPDATE users SET active=0 WHERE username='carol'")

        rows = {a["username"]: a for a in self.svc.tenant_admins(self.tid)}
        self.assertIn("carol", rows,
                      "a disabled admin stays readable so the card can re-enable it")
        self.assertFalse(rows["carol"]["account_active"],
                         "the read marks the disabled account as inactive")
        # Disabled is not "valid": the last-admin protection must keep excluding it.
        self.assertEqual(self.svc._count_valid_tenant_admins(self.tid), 1,
                         "the valid-admin count still excludes a disabled account")

    def test_excludes_inactive_membership_non_admin_and_removed_role(self):
        self._add_admin("carol", "Carol")
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid, operation="create-new",
            username="bob", display_name="Bob", temporary_password="Str0ngPassTmp",
            roles=["member"], department_id=None)
        self.svc._store.execute(
            "UPDATE memberships SET active=0 WHERE user_id="
            "(SELECT id FROM users WHERE username='bob')")
        self.svc._store.execute(
            "UPDATE memberships SET active=0 WHERE user_id="
            "(SELECT id FROM users WHERE username='carol')")

        self.assertEqual([a["username"] for a in self.svc.tenant_admins(self.tid)], ["root"],
                         "inactive memberships and non-admin members are not current admins")

    def test_removed_admin_role_drops_the_binding(self):
        self._add_admin("carol", "Carol")
        self.svc._store.execute(
            "DELETE FROM membership_roles WHERE membership_id="
            "(SELECT m.id FROM memberships m JOIN users u ON u.id=m.user_id"
            " WHERE u.username='carol')")
        self.assertEqual([a["username"] for a in self.svc.tenant_admins(self.tid)], ["root"],
                         "a member who no longer holds tenant_admin is not an admin")

    def test_inactive_admin_membership_is_excluded(self):
        self._add_admin("dave", "Dave")
        self.svc._store.execute(
            "UPDATE memberships SET active=0 WHERE user_id="
            "(SELECT id FROM users WHERE username='dave')")
        self.assertEqual([a["username"] for a in self.svc.tenant_admins(self.tid)], ["root"])

    def test_empty_for_tenant_without_admin_and_unknown_tenant(self):
        self.assertEqual(self.svc.tenant_admins(self._bare()["id"]), [])
        self.assertEqual(self.svc.tenant_admins("tnt_missing"), [])


if __name__ == "__main__":
    unittest.main()
