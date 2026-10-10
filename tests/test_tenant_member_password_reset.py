"""Tenant password reset boundaries, credential lifecycle and write races."""

import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from auth.password import PasswordError, hash_password, verify_password
from auth.service import IdentityService, IdentityServiceError, TEMPORARY_PASSWORD_TTL_SECONDS


class TenantMemberPasswordResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = IdentityService(os.path.join(self.tmp.name, "identity.db"))
        self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="RootPassword9",
            shared_root=os.path.join(self.tmp.name, "acme"), allow_weak=True)
        self.tid = self.svc.list_tenants()[0]["id"]
        self.root = self.svc.list_platform_users()[0]
        self.admin = self._create("tenantadmin", ["tenant_admin"])
        self.member = self._create("ordinary", ["member"])
        self.token = self.svc.login("tenantadmin", "FinalPassword9").token
        self.session_id = self.svc.verify_session(self.token)["session"]["id"]
        self.member_token = self.svc.login("ordinary", "FinalPassword9").token

    def _create(self, name, roles):
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid, operation="create-new",
            username=name, display_name=name, temporary_password="InitialPassword9", roles=roles)
        token = self.svc.login(name, "InitialPassword9").token
        self.svc.change_password(token, "InitialPassword9", "FinalPassword9")
        return next(m for m in self.svc.list_members(self.tid)["items"] if m["username"] == name)

    def _reset(self, **overrides):
        args = dict(actor_user_id=self.admin["user_id"], actor_session_id=self.session_id,
                    tenant_id=self.tid, member_id=self.member["id"],
                    expected_version=self.member["user_version"], recent_password="FinalPassword9")
        args.update(overrides)
        return self.svc.reset_tenant_member_password(**args)

    def _sql(self, sql, params=()):
        with self.svc._store.connect() as con:
            con.execute(sql, params)
            con.commit()

    def _state(self):
        user = dict(self.svc._store.execute(
            "SELECT * FROM users WHERE id=?", (self.member["user_id"],))[0])
        sessions = [dict(s) for s in self.svc._store.execute(
            "SELECT * FROM auth_sessions WHERE user_id=?", (self.member["user_id"],))]
        return user, sessions

    def _denied(self, code, **args):
        before = self._state()
        with self.assertRaises(IdentityServiceError) as caught:
            self._reset(**args)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self._state(), before)

    def _share(self):
        # A disabled membership in a different tenant is still a shared account.
        self._sql("INSERT INTO tenants(id, code, name, shared_root, active) VALUES (?,?,?,?,0)",
                  ("tenant-other", "other", "Other", os.path.join(self.tmp.name, "other")))
        self._sql("INSERT INTO memberships(id, tenant_id, user_id, display_name, active) VALUES (?,?,?,?,0)",
                  ("member-other", "tenant-other", self.member["user_id"], "ordinary"))

    def _promote_target(self):
        self._sql(
            "INSERT INTO membership_roles(tenant_id, membership_id, role_id)"
            " SELECT ?,?,id FROM roles WHERE tenant_id=? AND code='tenant_admin'",
            (self.tid, self.member["id"], self.tid))

    def _race(self, mutation, code):
        def hash_then_mutate(*args, **kwargs):
            value = hash_password(*args, **kwargs)
            mutation()
            return value
        with patch("auth.service.hash_password", side_effect=hash_then_mutate):
            self._denied(code)

    def test_fixed_temporary_password_revokes_sessions_and_forces_change(self):
        result = self._reset()
        self.assertEqual(result["temporary_password"], "123456")
        self.assertTrue(result["must_change_password"])
        self.assertAlmostEqual(result["temporary_password_expires_at"],
                               int(time.time()) + TEMPORARY_PASSWORD_TTL_SECONDS, delta=3)
        self.assertIsNone(self.svc.verify_session(self.member_token))
        with self.assertRaises(IdentityServiceError):
            self.svc.login("ordinary", "FinalPassword9")
        restricted = self.svc.login("ordinary", "123456")
        self.assertTrue(restricted.restricted)
        self.assertTrue(restricted.must_change_password)
        with self.assertRaises((IdentityServiceError, PasswordError)):
            self.svc.change_password(restricted.token, "123456", "123456")
        self.svc.change_password(restricted.token, "123456", "PermanentPassword9")
        self.assertIsNone(self.svc.verify_session(restricted.token))
        with self.assertRaises(IdentityServiceError):
            self.svc.login("ordinary", "123456")
        self.assertFalse(self.svc.login("ordinary", "PermanentPassword9").restricted)

    def test_expired_temporary_password_and_its_session_are_refused(self):
        result = self._reset()
        restricted = self.svc.login("ordinary", "123456")
        with patch("auth.service.time.time", return_value=result["temporary_password_expires_at"] + 1):
            self.assertIsNone(self.svc.verify_session(restricted.token))
            with self.assertRaises(IdentityServiceError):
                self.svc.login("ordinary", "123456")

    def test_audit_is_tenant_scoped_and_never_contains_password(self):
        self._reset()
        rows = self.svc._store.execute("SELECT * FROM audit_events WHERE action='user.password.reset'")
        self.assertEqual(len(rows), 1)
        event = dict(rows[0])
        self.assertEqual(event["tenant_id"], self.tid)
        self.assertEqual(event["target_tenant_id"], self.tid)
        self.assertEqual(event["actor_user_id"], self.admin["user_id"])
        self.assertNotIn("123456", json.dumps(event))
        self.assertNotIn("FinalPassword9", json.dumps(event))
        member = next(m for m in self.svc.list_members(self.tid)["items"] if m["id"] == self.member["id"])
        self.assertNotIn("temporary_password", member)
        self.assertNotIn("password_hash", member)
        self.assertTrue(verify_password("123456", self._state()[0]["password_hash"]))

    def test_account_version_not_membership_version_and_replay_conflict(self):
        self.assertNotEqual(self.member["version"], self.member["user_version"])
        self._denied("conflict", expected_version=self.member["version"])
        self._reset()
        self._denied("conflict")

    def test_wrong_or_missing_operator_password_rejected(self):
        for password in ("", "WrongPassword9"):
            self._denied("invalid_old", recent_password=password)

    def test_only_tenant_admin_can_reset(self):
        self._denied("forbidden", actor_user_id=self.member["user_id"])
        self._denied("session_invalid", actor_session_id="")
        self._denied("session_invalid", actor_session_id=self.svc.verify_session(self.member_token)["session"]["id"])

    def test_self_other_admin_and_platform_admin_rejected(self):
        self._denied("self_reset_forbidden", member_id=self.admin["id"])
        other = self._create("otheradmin", ["tenant_admin"])
        self._denied("platform_reset_required", member_id=other["id"])
        root_member = next(m for m in self.svc.list_members(self.tid)["items"] if m["user_id"] == self.root["id"])
        self._denied("platform_reset_required", member_id=root_member["id"])
        self._sql("INSERT INTO user_platform_roles(user_id, platform_role_id) SELECT ?,id FROM platform_roles WHERE code='platform_admin'",
                  (self.member["user_id"],))
        self._denied("platform_reset_required")

    def test_shared_and_foreign_members_rejected_without_disclosing_tenants(self):
        self._share()
        self._denied("platform_reset_required")
        self._denied("not_found", member_id="member-other")

    def test_list_capability_matches_target_boundaries_and_excludes_self(self):
        items = {m["id"]: m for m in self.svc.list_members(self.tid, actor_user_id=self.admin["user_id"])["items"]}
        self.assertTrue(items[self.member["id"]]["can_reset_password"])
        self.assertFalse(items[self.admin["id"]]["can_reset_password"])
        self.assertFalse(next(m for m in items.values() if m["user_id"] == self.root["id"])["can_reset_password"])
        self._share()
        items = self.svc.list_members(self.tid, actor_user_id=self.admin["user_id"])["items"]
        self.assertFalse(next(m for m in items if m["id"] == self.member["id"])["can_reset_password"])
        items = self.svc.list_members(self.tid, actor_user_id=self.root["id"])["items"]
        self.assertTrue(next(m for m in items if m["id"] == self.member["id"])["can_reset_password"])

    def test_session_revoked_after_password_check_prevents_reset(self):
        self._race(lambda: self.svc._sessions.revoke(self.token), "session_invalid")

    def test_actor_password_changed_after_check_prevents_reset(self):
        self._race(lambda: self._sql("UPDATE users SET version=version+1 WHERE id=?", (self.admin["user_id"],)),
                   "session_invalid")

    def test_admin_role_revoked_after_password_check_prevents_reset(self):
        self._race(lambda: self._sql("DELETE FROM membership_roles WHERE membership_id=?", (self.admin["id"],)),
                   "forbidden")

    def test_tenant_disabled_after_password_check_prevents_reset(self):
        self._race(lambda: self._sql("UPDATE tenants SET active=0 WHERE id=?", (self.tid,)), "forbidden")

    def test_target_shared_after_password_check_prevents_reset(self):
        self._race(self._share, "platform_reset_required")

    def test_target_promoted_after_password_check_prevents_reset(self):
        self._race(self._promote_target, "platform_reset_required")

    def test_audit_failure_rolls_back_password_and_sessions(self):
        with patch.object(self.svc, "_audit_in_tx", side_effect=RuntimeError("audit unavailable")):
            self._denied("identity_unavailable")

    def test_database_lock_failure_returns_unavailable(self):
        with patch.object(self.svc, "_tx", side_effect=sqlite3.OperationalError("database locked")):
            self._denied("identity_unavailable")

    def test_fixed_password_exception_does_not_change_normal_password_rules(self):
        with self.assertRaises(PasswordError):
            hash_password("123456")
        with self.assertRaises(IdentityServiceError) as caught:
            self.svc.create_member(actor_user_id=self.admin["user_id"], tenant_id=self.tid,
                                   operation="create-new", username="weakuser", display_name="Weak",
                                   temporary_password="123456", roles=["member"])
        self.assertEqual(caught.exception.code, "weak_password")
