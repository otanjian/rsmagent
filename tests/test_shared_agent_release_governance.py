# encoding:utf-8
"""Tests for shared-Agent release governance (add-shared-agent-release-governance).

Covers the state machine (draft -> released -> suspended -> rollback), the
approval/eligibility/quota gates, member visibility/default resolution, the
runtime snapshot override and the default-off compatibility switch.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from auth.service import IdentityService, IdentityServiceError
from config import Config


def _cfg(**overrides):
    values = {"shared_agent_release_governance": True}
    values.update(overrides)
    return Config(values)


def _on():
    return patch("config.conf", return_value=_cfg())


def _off():
    return patch("config.conf", return_value=Config({}))


class ReleaseGovernanceTest(unittest.TestCase):
    def setUp(self):
        self.svc = IdentityService(os.path.join(tempfile.mkdtemp(), "identity.db"))
        self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root="/s/acme")
        self.tid = self.svc.list_tenants()[0]["id"]
        self.root = self.svc.list_platform_users()[0]["id"]
        with self.svc._tx() as con:
            con.execute(
                "INSERT INTO agent_bindings(agent_id, tenant_id) VALUES (?,?)",
                ("agent-shared", self.tid))
            con.execute(
                "INSERT INTO agent_bindings(agent_id, tenant_id, private_owner_user_id)"
                " VALUES (?,?,?)", ("agent-priv", self.tid, self.root))
        # A second control user so an approval cannot be self-approved.
        self.svc.create_member(
            actor_user_id=self.root, tenant_id=self.tid, operation="create-new",
            username="admin2", display_name="Admin Two",
            temporary_password="Str0ngPass2", roles=["tenant_admin"])
        self.admin2 = self.svc._find_user_by_username("admin2")["id"]

    # --- visibility / default resolution ----------------------------------

    def test_flag_off_shared_agent_is_resolvable(self):
        self.assertEqual(self.svc.resolved_default_agent_id(self.tid), "agent-shared")

    def test_unreleased_shared_agent_hidden_under_governance(self):
        with _on():
            self.assertIsNone(self.svc.resolved_default_agent_id(self.tid))
            self.assertEqual(
                self.svc.agent_release_status("agent-shared")["release_state"], "")

    def test_private_agent_still_owner_visible_under_governance(self):
        with _on():
            # The caller's own private Agent is unaffected by governance.
            self.assertEqual(
                self.svc.resolved_default_agent_id(self.tid, self.root), "agent-priv")

    # --- approval + publish flow ------------------------------------------

    def test_publish_requires_an_approval(self):
        with _on():
            with self.assertRaises(IdentityServiceError) as ctx:
                self.svc.release_agent(
                    actor_user_id=self.root, tenant_id=self.tid,
                    agent_id="agent-shared", approval_id="apr_missing")
            self.assertIn(ctx.exception.code,
                          ("approval_unknown", "approval_mismatch"))

    def test_publish_flow_and_member_visibility(self):
        with _on():
            approval = self.svc.request_agent_release(
                actor_user_id=self.root, tenant_id=self.tid, agent_id="agent-shared")
            # Separation of duties: the requester cannot approve their own.
            with self.assertRaises(IdentityServiceError):
                self.svc.decide_approval(
                    actor_user_id=self.root, tenant_id=self.tid,
                    approval_id=approval["id"], approve=True)
            self.svc.decide_approval(
                actor_user_id=self.admin2, tenant_id=self.tid,
                approval_id=approval["id"], approve=True)
            result = self.svc.release_agent(
                actor_user_id=self.root, tenant_id=self.tid,
                agent_id="agent-shared", approval_id=approval["id"])
            self.assertEqual(result["version"], 1)
            self.assertEqual(result["release_state"], "released")
            status = self.svc.agent_release_status("agent-shared")
            self.assertEqual(status["release_state"], "released")
            self.assertTrue(status["released_snapshot_hash"])
            self.assertEqual(self.svc.resolved_default_agent_id(self.tid),
                             "agent-shared")
            self.assertIsNotNone(self.svc.released_snapshot("agent-shared"))

    def test_approval_for_another_action_cannot_publish(self):
        with _on():
            approval = self.svc.request_agent_release(
                actor_user_id=self.root, tenant_id=self.tid,
                agent_id="agent-shared", kind="suspend")
            self.svc.decide_approval(
                actor_user_id=self.admin2, tenant_id=self.tid,
                approval_id=approval["id"], approve=True)
            with self.assertRaises(IdentityServiceError) as ctx:
                self.svc.release_agent(
                    actor_user_id=self.root, tenant_id=self.tid,
                    agent_id="agent-shared", approval_id=approval["id"])
            self.assertEqual(ctx.exception.code, "approval_mismatch")

    # --- suspend / rollback -----------------------------------------------

    def _publish(self):
        approval = self.svc.request_agent_release(
            actor_user_id=self.root, tenant_id=self.tid, agent_id="agent-shared")
        self.svc.decide_approval(
            actor_user_id=self.admin2, tenant_id=self.tid,
            approval_id=approval["id"], approve=True)
        return self.svc.release_agent(
            actor_user_id=self.root, tenant_id=self.tid,
            agent_id="agent-shared", approval_id=approval["id"])

    def test_suspend_hides_again_and_keeps_history(self):
        with _on():
            self._publish()
            approval = self.svc.request_agent_release(
                actor_user_id=self.root, tenant_id=self.tid,
                agent_id="agent-shared", kind="suspend")
            self.svc.decide_approval(
                actor_user_id=self.admin2, tenant_id=self.tid,
                approval_id=approval["id"], approve=True)
            result = self.svc.suspend_agent_release(
                actor_user_id=self.root, tenant_id=self.tid,
                agent_id="agent-shared", approval_id=approval["id"])
            self.assertEqual(result["release_state"], "suspended")
            self.assertIsNone(self.svc.resolved_default_agent_id(self.tid))
            # History survives suspension.
            history = self.svc.list_agent_releases(
                actor_user_id=self.root, tenant_id=self.tid, agent_id="agent-shared")
            self.assertEqual(len(history), 1)

    def test_rollback_releases_a_new_version(self):
        with _on():
            first = self._publish()
            # A second publish creates version 2 (content unchanged, still a row).
            second = self._publish()
            self.assertEqual(second["version"], 2)
            approval = self.svc.request_agent_release(
                actor_user_id=self.root, tenant_id=self.tid,
                agent_id="agent-shared", kind="rollback", version=first["version"])
            self.svc.decide_approval(
                actor_user_id=self.admin2, tenant_id=self.tid,
                approval_id=approval["id"], approve=True)
            result = self.svc.rollback_agent_release(
                actor_user_id=self.root, tenant_id=self.tid, agent_id="agent-shared",
                version=first["version"], approval_id=approval["id"])
            self.assertEqual(result["version"], 3)
            self.assertEqual(result["release_state"], "released")
            history = self.svc.list_agent_releases(
                actor_user_id=self.root, tenant_id=self.tid, agent_id="agent-shared")
            self.assertEqual(len(history), 3)

    # --- authorization / switch -------------------------------------------

    def test_publish_refused_when_switch_off(self):
        with _off():
            with self.assertRaises(IdentityServiceError) as ctx:
                self.svc.release_agent(
                    actor_user_id=self.root, tenant_id=self.tid,
                    agent_id="agent-shared", approval_id="apr_x")
            self.assertEqual(ctx.exception.code, "release_governance_disabled")

    def test_private_binding_is_not_release_governed(self):
        with _on():
            with self.assertRaises(IdentityServiceError) as ctx:
                self.svc.request_agent_release(
                    actor_user_id=self.root, tenant_id=self.tid,
                    agent_id="agent-priv")
            self.assertEqual(ctx.exception.code, "private_agent_not_releasable")

    def test_member_cannot_release(self):
        with _on():
            self.svc.create_member(
                actor_user_id=self.root, tenant_id=self.tid, operation="create-new",
                username="member1", display_name="Member One",
                temporary_password="Str0ngPass3", roles=["member"])
            member = self.svc._find_user_by_username("member1")["id"]
            with self.assertRaises(IdentityServiceError) as ctx:
                self.svc.request_agent_release(
                    actor_user_id=member, tenant_id=self.tid, agent_id="agent-shared")
            self.assertEqual(ctx.exception.code, "forbidden")

    # --- migration backfill ------------------------------------------------

    def test_backfill_registers_shared_bindings_only(self):
        with _on():
            result = self.svc.backfill_agent_releases()
            self.assertTrue(result["enabled"])
            self.assertEqual(result["released"], 1)  # agent-shared
            self.assertEqual(
                self.svc.agent_release_status("agent-shared")["release_state"], "released")
            self.assertEqual(
                self.svc.agent_release_status("agent-priv")["release_state"], "")
            # Idempotent: a second run changes nothing.
            again = self.svc.backfill_agent_releases()
            self.assertEqual(again["released"], 0)
            self.assertEqual(again["skipped"], 1)

    def test_backfill_is_noop_when_switch_off(self):
        with _off():
            result = self.svc.backfill_agent_releases()
            self.assertFalse(result["enabled"])
            self.assertEqual(result["released"], 0)

    def test_sharing_forms_a_draft_and_unsharing_keeps_history(self):
        with _on():
            # Sharing a private Agent under governance forms a draft.
            self.svc.set_agent_visibility(
                agent_id="agent-priv", actor_user_id=self.root, visibility="tenant")
            status = self.svc.agent_release_status("agent-priv")
            self.assertEqual(status["release_state"], "draft")
            self.assertIsNone(self.svc.resolved_default_agent_id(self.tid))

            # Narrowing it back restores owner-only visibility, keeps the binding
            # and any history.
            self.svc.set_agent_visibility(
                agent_id="agent-priv", actor_user_id=self.root,
                visibility="private", owner_user_id=self.root)
            self.assertEqual(
                self.svc.agent_release_status("agent-priv")["release_state"], "draft")
            self.assertIsNone(self.svc.resolved_default_agent_id(self.tid))
            self.assertEqual(
                self.svc.resolved_default_agent_id(self.tid, self.root), "agent-priv")

    # --- gates: approval, enterprise enablement, quota --------------------

    def test_enterprise_disabled_refuses_publish(self):
        with _on():
            self.svc._store.execute(
                "UPDATE tenants SET active=0 WHERE id=?", (self.tid,))
            approval = self.svc.request_agent_release(
                actor_user_id=self.root, tenant_id=self.tid, agent_id="agent-shared")
            self.svc.decide_approval(
                actor_user_id=self.admin2, tenant_id=self.tid,
                approval_id=approval["id"], approve=True)
            with self.assertRaises(IdentityServiceError) as ctx:
                self.svc.release_agent(
                    actor_user_id=self.root, tenant_id=self.tid,
                    agent_id="agent-shared", approval_id=approval["id"])
            self.assertEqual(ctx.exception.code, "enterprise_disabled")
            self.assertEqual(
                self.svc.agent_release_status("agent-shared")["release_state"], "")

    def test_quota_exhaustion_refuses_publish(self):
        with _on():
            self.svc._store.execute(
                "INSERT INTO quota_limits(tenant_id, user_id, metric, hard_limit)"
                " VALUES (?,?,?,?)", (self.tid, "", "messages", 1))
            self.svc._store.execute(
                "INSERT INTO quota_usage(tenant_id, user_id, metric, window_start,"
                " used) VALUES (?,?,?,?,?)", (self.tid, "", "messages", 0, 5))
            approval = self.svc.request_agent_release(
                actor_user_id=self.root, tenant_id=self.tid, agent_id="agent-shared")
            self.svc.decide_approval(
                actor_user_id=self.admin2, tenant_id=self.tid,
                approval_id=approval["id"], approve=True)
            with self.assertRaises(IdentityServiceError) as ctx:
                self.svc.release_agent(
                    actor_user_id=self.root, tenant_id=self.tid,
                    agent_id="agent-shared", approval_id=approval["id"])
            self.assertEqual(ctx.exception.code, "quota_exceeded")
            self.assertEqual(
                self.svc.agent_release_status("agent-shared")["release_state"], "")


class ReleasedOverrideFunctionTest(unittest.TestCase):
    """``released_capability_overrides``: tri-state, fail-closed semantics."""

    def setUp(self):
        self.svc = IdentityService(os.path.join(tempfile.mkdtemp(), "identity.db"))
        self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root="/s/acme")
        self.tid = self.svc.list_tenants()[0]["id"]
        self.root = self.svc.list_platform_users()[0]["id"]
        with self.svc._tx() as con:
            con.execute(
                "INSERT INTO agent_bindings(agent_id, tenant_id) VALUES (?,?)",
                ("agent-shared", self.tid))
            con.execute(
                "INSERT INTO agent_bindings(agent_id, tenant_id, private_owner_user_id)"
                " VALUES (?,?,?)", ("agent-priv", self.tid, self.root))
        self.svc.create_member(
            actor_user_id=self.root, tenant_id=self.tid, operation="create-new",
            username="admin2", display_name="Admin Two",
            temporary_password="Str0ngPass2", roles=["tenant_admin"])
        self.admin2 = self.svc._find_user_by_username("admin2")["id"]

    def _wired(self):
        return patch("auth.service.get_identity_service", return_value=self.svc)

    def test_private_agent_is_out_of_scope(self):
        from auth.agent_release import released_capability_overrides
        with _on(), self._wired():
            self.assertIsNone(released_capability_overrides("agent-priv"))

    def test_unreleased_shared_agent_fails_closed(self):
        from auth.agent_release import (
            released_capability_overrides, ReleasedSurfaceUnavailable)
        with _on(), self._wired():
            with self.assertRaises(ReleasedSurfaceUnavailable):
                released_capability_overrides("agent-shared")

    def test_released_shared_agent_returns_the_surface(self):
        from auth.agent_release import released_capability_overrides
        with _on(), self._wired():
            approval = self.svc.request_agent_release(
                actor_user_id=self.root, tenant_id=self.tid, agent_id="agent-shared")
            self.svc.decide_approval(
                actor_user_id=self.admin2, tenant_id=self.tid,
                approval_id=approval["id"], approve=True)
            self.svc.release_agent(
                actor_user_id=self.root, tenant_id=self.tid,
                agent_id="agent-shared", approval_id=approval["id"])
            overrides = released_capability_overrides("agent-shared")
        self.assertIsInstance(overrides, dict)
        self.assertIn("tools_allowlist", overrides)

    def test_suspended_shared_agent_fails_closed(self):
        from auth.agent_release import (
            released_capability_overrides, ReleasedSurfaceUnavailable)
        with _on(), self._wired():
            first = self.svc.request_agent_release(
                actor_user_id=self.root, tenant_id=self.tid, agent_id="agent-shared")
            self.svc.decide_approval(
                actor_user_id=self.admin2, tenant_id=self.tid,
                approval_id=first["id"], approve=True)
            self.svc.release_agent(
                actor_user_id=self.root, tenant_id=self.tid,
                agent_id="agent-shared", approval_id=first["id"])
            suspend = self.svc.request_agent_release(
                actor_user_id=self.root, tenant_id=self.tid,
                agent_id="agent-shared", kind="suspend")
            self.svc.decide_approval(
                actor_user_id=self.admin2, tenant_id=self.tid,
                approval_id=suspend["id"], approve=True)
            self.svc.suspend_agent_release(
                actor_user_id=self.root, tenant_id=self.tid,
                agent_id="agent-shared", approval_id=suspend["id"])
            with self.assertRaises(ReleasedSurfaceUnavailable):
                released_capability_overrides("agent-shared")


class FailClosedResolutionTest(unittest.TestCase):
    """The resolver denies tools when the released surface is unverifiable."""

    def test_unverifiable_surface_denies_all_tools(self):
        from agent.registry import AgentProfile
        from agent.effective_capabilities import (
            resolve_effective_capabilities, is_tool_allowed)
        profile = AgentProfile(id="agent-x", name="X", workspace="/w/x",
                               tools_allowlist=("bash", "read"))
        with patch("auth.agent_release.released_capability_overrides",
                   side_effect=RuntimeError("store down")):
            effective = resolve_effective_capabilities(profile)
        self.assertFalse(is_tool_allowed("bash", effective))
        self.assertFalse(is_tool_allowed("read", effective))


class ReleaseSnapshotOverrideTest(unittest.TestCase):
    """The runtime seam: a released surface overrides the editable draft."""

    def _profile(self):
        from agent.registry import AgentProfile
        return AgentProfile(
            id="agent-x", name="X", workspace="/w/x",
            tools_allowlist=("bash", "read"), skills=("a",),
            persona_summary="draft persona")

    def test_released_surface_wins_over_draft(self):
        from agent.effective_capabilities import (
            resolve_effective_capabilities, is_tool_allowed)
        frozen = {
            "persona_summary": "released persona",
            "greeting": None, "scene_id": None,
            "skills": ["b"], "knowledge_ids": None, "sops": [],
            "tools_allowlist": ["read"], "tools_denylist": [],
        }
        with patch("auth.agent_release.released_capability_overrides",
                   return_value=frozen):
            effective = resolve_effective_capabilities(self._profile())
        self.assertEqual(effective.persona_summary, "released persona")
        self.assertEqual(tuple(effective.skills), ("b",))
        self.assertTrue(is_tool_allowed("read", effective))
        self.assertFalse(is_tool_allowed("bash", effective))

    def test_ignore_release_uses_the_draft(self):
        from agent.effective_capabilities import resolve_effective_capabilities
        frozen = {"persona_summary": "released persona", "skills": ["b"],
                  "tools_allowlist": ["read"], "tools_denylist": []}
        with patch("auth.agent_release.released_capability_overrides",
                   return_value=frozen):
            effective = resolve_effective_capabilities(
                self._profile(), ignore_release=True)
        self.assertEqual(effective.persona_summary, "draft persona")
        self.assertEqual(tuple(effective.skills), ("a",))

    def test_no_override_keeps_the_draft(self):
        from agent.effective_capabilities import resolve_effective_capabilities
        with patch("auth.agent_release.released_capability_overrides",
                   return_value=None):
            effective = resolve_effective_capabilities(self._profile())
        self.assertEqual(effective.persona_summary, "draft persona")


class ReleaseSnapshotHashTest(unittest.TestCase):
    def test_content_hash_is_order_insensitive_and_stable(self):
        from auth.agent_release import build_snapshot, snapshot_hash
        left = build_snapshot({"name": "A", "skills": ["x", "y"]},
                              connections=["c2", "c1"], channels=["h1"])
        right = build_snapshot({"skills": ["y", "x"], "name": "A"},
                               connections=["c1", "c2"], channels=["h1"])
        self.assertEqual(snapshot_hash(left), snapshot_hash(right))

    def test_content_hash_changes_with_content(self):
        from auth.agent_release import build_snapshot, snapshot_hash
        one = build_snapshot({"name": "A"})
        two = build_snapshot({"name": "B"})
        self.assertNotEqual(snapshot_hash(one), snapshot_hash(two))


if __name__ == "__main__":
    unittest.main()
