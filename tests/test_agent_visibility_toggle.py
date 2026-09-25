# encoding:utf-8
"""智能体可见性展示与私有↔共享转换（change ``show-and-toggle-agent-visibility``）。

规范见 ``openspec/changes/show-and-toggle-agent-visibility/specs/
user-private-agent-management/spec.md``：ADDED「智能体可见性展示与私有↔共享转换」，
MODIFIED「所有者可以维护和启停本人私有智能体」。

「共享」沿用既有口径：``agent_bindings.private_owner_user_id`` 为空。本 change 不改
表结构、不改既有两个服务原语的门禁，只补两件事——把对象在两个状态之间搬动的显式
入口，以及控制台判断「显示哪个徽标、提供哪个转换」所需的投影字段。

这一层要钉住的关键事实，也是原始缺陷的成因：私有对象的可见性由**归属**决定，判定
先于角色资源授权。所以给一个成员授予 ``agent:<id>`` 的 use 权，对一个「归属他人私有」
的对象完全没有效果；只有把它变成共享之后，那份授权才开始生效。
"""

import os
import tempfile
import unittest

import web  # noqa: F401  (real package preferred so HTTPError is authentic)

from auth.object_scope import ObjectScope
from auth.runtime import RequestContext
from auth.service import IdentityService, IdentityServiceError
from channel.web.web_channel import _tenant_agents_admin_projection


def _db():
    return os.path.join(tempfile.mkdtemp(), "identity.db")


def _seed():
    svc = IdentityService(_db())
    svc.bootstrap(
        tenant_code="acme", tenant_name="Acme", admin_username="root",
        admin_display="Root", admin_password="Str0ngAdminPass",
        shared_root="/s/acme", allow_weak=True)
    root = svc.list_platform_users()[0]
    tid = svc.list_tenants()[0]["id"]
    return svc, tid, root


def _member(svc, root, tid, username, roles=("member",)):
    return svc.create_member(
        actor_user_id=root["id"], tenant_id=tid, operation="create-new",
        username=username, display_name=username.title(),
        temporary_password="TmpPass123!", roles=list(roles))["user_id"]


def _ctx(svc, user_id, tenant_id, *, is_tenant_admin=False):
    """A real member context: permissions come from the persisted roles."""
    return RequestContext(
        user_id=user_id, username="u", display_name="U",
        is_platform_admin=False, must_change_password=False,
        tenant_id=tenant_id, membership=None,
        permissions=set(svc.permissions_for(user_id, tenant_id)),
        is_tenant_admin=is_tenant_admin)


class _FakeProfile:
    """Minimal AgentProfile stand-in for the projection tests."""

    def __init__(self, agent_id, name, enabled=True, agent_type="normal"):
        self.id = agent_id
        self.name = name
        self.workspace = "/tmp/%s" % agent_id
        self.enabled = enabled
        self.description = ""
        self.avatar = None
        self.position = ""
        self.category = ""
        self.tags = ()
        self.agent_type = agent_type
        self.coding_project_dir = None

    @property
    def is_coding(self):
        return self.agent_type == "coding"

    @property
    def workspace_path(self):
        from pathlib import Path
        return Path(self.workspace)

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "workspace": self.workspace,
            "enabled": self.enabled, "description": self.description,
            "avatar": self.avatar, "position": self.position,
            "category": self.category, "tags": list(self.tags),
        }


class _FakeRegistry:
    default_agent_id = "shared-agent"

    def __init__(self, profiles):
        self._profiles = profiles

    def list(self, include_disabled=False):
        if include_disabled:
            return list(self._profiles)
        return [p for p in self._profiles if p.enabled]

    def get(self, agent_id, require_enabled=False):
        for profile in self._profiles:
            if profile.id == agent_id:
                if require_enabled and not profile.enabled:
                    return None
                return profile
        return None


class _RegistryScope:
    """Patch target: the fake registry backed by the test's profile list."""

    def __init__(self, profiles):
        self._profiles = profiles

    def __enter__(self):
        from unittest.mock import patch

        self._patcher = patch("agent.registry.get_agent_registry",
                              return_value=_FakeRegistry(self._profiles))
        return self._patcher.__enter__()

    def __exit__(self, *exc):
        return self._patcher.__exit__(*exc)


class _WebCtxCase(unittest.TestCase):
    """Give ``web.HTTPError`` a response context to write headers into."""

    def setUp(self):
        super().setUp()
        web.ctx.headers = []
        web.ctx.status = "200 OK"


class SetAgentVisibilityTests(_WebCtxCase):
    """The conversion primitive: who may move an object between the two states."""

    def setUp(self):
        super().setUp()
        self.svc, self.tid, self.root = _seed()
        self.rock = _member(self.svc, self.root, self.tid, "rock")
        self.jane = _member(self.svc, self.root, self.tid, "jane")
        self.tadmin = _member(self.svc, self.root, self.tid, "tadmin",
                              roles=("tenant_admin",))
        self.svc.bind_agent(tenant_id=self.tid, agent_id="shared-agent")
        self.svc.bind_agent(tenant_id=self.tid, agent_id="rock-agent",
                            private_owner_user_id=self.rock,
                            origin="user_created")

    def _owner(self, agent_id):
        return self.svc.get_agent_binding(agent_id)["private_owner_user_id"]

    # --- 私有 → 租户共享 -------------------------------------------------

    def test_the_owner_turns_their_own_private_agent_into_a_tenant_shared_one(self):
        result = self.svc.set_agent_visibility(
            agent_id="rock-agent", actor_user_id=self.rock, visibility="tenant")
        self.assertEqual(result["visibility"], "tenant")
        self.assertTrue(result["changed"])
        self.assertIsNone(self._owner("rock-agent"))

    def test_a_tenant_admin_may_also_share_it(self):
        self.svc.set_agent_visibility(
            agent_id="rock-agent", actor_user_id=self.tadmin, visibility="tenant")
        self.assertIsNone(self._owner("rock-agent"))

    def test_a_non_owner_non_admin_cannot_share_it(self):
        with self.assertRaises(IdentityServiceError) as caught:
            self.svc.set_agent_visibility(
                agent_id="rock-agent", actor_user_id=self.jane, visibility="tenant")
        self.assertEqual(caught.exception.code, "forbidden")
        # The object is untouched: still private, still only the owner's.
        self.assertEqual(self._owner("rock-agent"), self.rock)

    def test_sharing_an_already_shared_agent_is_idempotent_for_an_admin(self):
        self.svc.set_agent_visibility(
            agent_id="rock-agent", actor_user_id=self.rock, visibility="tenant")
        again = self.svc.set_agent_visibility(
            agent_id="rock-agent", actor_user_id=self.tadmin, visibility="tenant")
        self.assertFalse(again["changed"])
        self.assertEqual(again["visibility"], "tenant")

    def test_the_owner_cannot_re_share_what_is_no_longer_theirs(self):
        """共享即交出独占：重复提交是失败，不是「又一次成功」。"""
        self.svc.set_agent_visibility(
            agent_id="rock-agent", actor_user_id=self.rock, visibility="tenant")
        with self.assertRaises(IdentityServiceError) as caught:
            self.svc.set_agent_visibility(
                agent_id="rock-agent", actor_user_id=self.rock, visibility="tenant")
        self.assertEqual(caught.exception.code, "forbidden")

    def test_sharing_from_the_console_is_audited_like_the_operator_path(self):
        """两条入口写同一个审计动作，运维与自助在审计里读起来一致。"""
        self.svc.set_agent_visibility(
            agent_id="rock-agent", actor_user_id=self.rock, visibility="tenant")
        events = [dict(r) for r in self.svc._store.execute(  # type: ignore[attr-defined]
            "SELECT * FROM audit_events WHERE action='agent.make_tenant_shared'")]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor_user_id"], self.rock)
        self.assertEqual(events[0]["target"], "agent:rock-agent")

    # --- 租户共享 → 私有 -------------------------------------------------

    def test_an_admin_restores_it_to_an_explicit_owner(self):
        self.svc.set_agent_visibility(
            agent_id="shared-agent", actor_user_id=self.tadmin, visibility="private",
            owner_user_id=self.jane)
        self.assertEqual(self._owner("shared-agent"), self.jane)

    def test_restoring_requires_an_explicit_owner(self):
        with self.assertRaises(IdentityServiceError) as caught:
            self.svc.set_agent_visibility(
                agent_id="shared-agent", actor_user_id=self.tadmin,
                visibility="private")
        self.assertEqual(caught.exception.code, "bad_request")
        self.assertIsNone(self._owner("shared-agent"))

    def test_a_member_cannot_restore_anything(self):
        with self.assertRaises(IdentityServiceError) as caught:
            self.svc.set_agent_visibility(
                agent_id="shared-agent", actor_user_id=self.jane,
                visibility="private", owner_user_id=self.jane)
        self.assertEqual(caught.exception.code, "forbidden")
        self.assertIsNone(self._owner("shared-agent"))

    def test_restoring_the_tenant_default_is_refused(self):
        self.svc.set_agent_visibility(
            agent_id="shared-agent", actor_user_id=self.tadmin, visibility="private",
            owner_user_id=self.jane)
        self.svc.set_agent_visibility(
            agent_id="shared-agent", actor_user_id=self.tadmin, visibility="tenant")
        self.svc.appoint_tenant_default_agent(
            tenant_id=self.tid, agent_id="shared-agent",
            actor_user_id=self.tadmin)
        with self.assertRaises(IdentityServiceError) as caught:
            self.svc.set_agent_visibility(
                agent_id="shared-agent", actor_user_id=self.tadmin,
                visibility="private", owner_user_id=self.jane)
        self.assertEqual(caught.exception.code, "agent_is_tenant_default")
        self.assertIsNone(self._owner("shared-agent"))
        self.assertEqual(self.svc.tenant_default_agent_id(self.tid), "shared-agent")

    def test_restoring_to_a_non_member_is_refused(self):
        self.svc.create_tenant(
            actor_user_id=self.root["id"], code="beta", name="Beta",
            shared_root="/s/beta", admin_username="broot",
            admin_display="BRoot", admin_password="Str0ngPass2",
            recent_password="Str0ngAdminPass")
        beta = [t for t in self.svc.list_tenants() if t["code"] == "beta"][0]
        beta_member = _member(self.svc, self.root, beta["id"], "bob")
        # ``bob`` is a member of beta, not of acme: naming them as the owner of an
        # acme-bound Agent must be refused, so ownership never names someone who
        # cannot reach the object.
        with self.assertRaises(IdentityServiceError) as caught:
            self.svc.set_agent_visibility(
                agent_id="shared-agent", actor_user_id=self.tadmin,
                visibility="private", owner_user_id=beta_member)
        self.assertEqual(caught.exception.code, "not_found")
        self.assertIsNone(self._owner("shared-agent"))

    def test_restoring_an_object_that_already_has_a_different_owner_is_refused(self):
        """Recovery is not transfer: an owned object is never re-pointed."""
        with self.assertRaises(IdentityServiceError) as caught:
            self.svc.set_agent_visibility(
                agent_id="rock-agent", actor_user_id=self.tadmin, visibility="private",
                owner_user_id=self.jane)
        self.assertEqual(caught.exception.code, "agent_already_owned")
        self.assertEqual(self._owner("rock-agent"), self.rock)

    # --- 入参校验 ---------------------------------------------------------

    def test_an_unknown_visibility_is_refused(self):
        with self.assertRaises(IdentityServiceError) as caught:
            self.svc.set_agent_visibility(
                agent_id="rock-agent", actor_user_id=self.rock, visibility="public")
        self.assertEqual(caught.exception.code, "bad_request")
        self.assertEqual(self._owner("rock-agent"), self.rock)

    def test_an_unbound_agent_is_not_found(self):
        with self.assertRaises(IdentityServiceError) as caught:
            self.svc.set_agent_visibility(
                agent_id="no-such-agent", actor_user_id=self.tadmin,
                visibility="tenant")
        self.assertEqual(caught.exception.code, "not_found")


class GrantAppliesOnlyAfterSharingTests(_WebCtxCase):
    """The reported defect, pinned at the authorization layer.

    A member holding an explicit ``agent:<id>`` use/read grant on an object that
    is *someone else's private* Agent is refused — the private-owner rule is
    evaluated ahead of every resource grant. Sharing the object is what makes
    that grant start to matter, which is exactly why the console needs to show
    the state and offer the conversion.
    """

    def setUp(self):
        super().setUp()
        self.svc, self.tid, self.root = _seed()
        self.rock = _member(self.svc, self.root, self.tid, "rock")
        self.tadmin = _member(self.svc, self.root, self.tid, "tadmin",
                              roles=("tenant_admin",))
        role = self.svc.create_role(
            self.root["id"], self.tid, "consumer", "Consumer",
            ["agent.read", "agent.use", "chat.use"],
            resource_grants=[
                {"resource_kind": "agent", "resource_id": "agent:rock-agent",
                 "action": "read"},
                {"resource_kind": "agent", "resource_id": "agent:rock-agent",
                 "action": "use"},
            ])
        self.cara = _member(self.svc, self.root, self.tid, "cara",
                            roles=(role["code"],))
        self.svc.bind_agent(tenant_id=self.tid, agent_id="rock-agent",
                            private_owner_user_id=self.rock,
                            origin="user_created")

    def _can(self, user_id, action, permission):
        return self.svc.check_resource_action(
            user_id, self.tid, "agent", "agent:rock-agent", action,
            permission=permission)

    def test_the_grant_is_inert_while_the_object_stays_private(self):
        self.assertFalse(self._can(self.cara, "read", "agent.read"))
        self.assertFalse(self._can(self.cara, "use", "agent.use"))

    def test_the_grant_applies_once_the_owner_shares_it(self):
        self.svc.set_agent_visibility(
            agent_id="rock-agent", actor_user_id=self.rock, visibility="tenant")
        self.assertTrue(self._can(self.cara, "read", "agent.read"))
        self.assertTrue(self._can(self.cara, "use", "agent.use"))

    def test_the_owner_loses_the_management_range_after_sharing(self):
        def manages(user_id, *, is_admin):
            ctx = _ctx(self.svc, user_id, self.tid, is_tenant_admin=is_admin)
            binding = self.svc.get_agent_binding("rock-agent")
            return ObjectScope.from_context(ctx).allows_agent(
                binding, action="manage")

        self.assertTrue(manages(self.rock, is_admin=False))
        self.svc.set_agent_visibility(
            agent_id="rock-agent", actor_user_id=self.rock, visibility="tenant")
        # The object is no longer the owner's to manage; the tenant's
        # administration is who maintains it from here.
        self.assertFalse(manages(self.rock, is_admin=False))
        self.assertTrue(manages(self.tadmin, is_admin=True))


class AgentVisibilityProjectionTests(_WebCtxCase):
    """The fields the console renders its badge and its action from."""

    def setUp(self):
        super().setUp()
        self.svc, self.tid, self.root = _seed()
        self.rock = _member(self.svc, self.root, self.tid, "rock")
        self.tadmin = _member(self.svc, self.root, self.tid, "tadmin",
                              roles=("tenant_admin",))
        self.svc.bind_agent(tenant_id=self.tid, agent_id="shared-agent")
        self.svc.bind_agent(tenant_id=self.tid, agent_id="rock-agent",
                            private_owner_user_id=self.rock,
                            origin="user_created")
        self.profiles = [
            _FakeProfile("shared-agent", "Shared Agent"),
            _FakeProfile("rock-agent", "Rock Agent"),
        ]

    def _rows(self, user_id, *, is_admin=False):
        from unittest.mock import patch
        import channel.web.web_channel as wc

        ctx = _ctx(self.svc, user_id, self.tid, is_tenant_admin=is_admin)
        with _RegistryScope(self.profiles), \
             patch("auth.service.get_identity_service", return_value=self.svc), \
             patch.object(wc, "_tenant_ids_for_context",
                          return_value=self.svc.tenant_agent_ids(self.tid)):
            data = _tenant_agents_admin_projection(ctx)
        return {row["id"]: row for row in data["agents"]}

    def test_the_owner_sees_their_own_object_as_private_and_sharable(self):
        row = self._rows(self.rock)["rock-agent"]
        self.assertEqual(row["visibility"], "private")
        self.assertTrue(row["can_share"])
        self.assertFalse(row["can_unshare"])

    def test_a_member_does_not_see_the_tenants_shared_object(self):
        """管理页对普通成员只列出其私有对象；共享对象走工作台那条读路径。"""
        self.assertNotIn("shared-agent", self._rows(self.rock))

    def test_an_admin_does_not_see_another_members_private_object(self):
        self.assertNotIn("rock-agent", self._rows(self.tadmin, is_admin=True))

    def test_an_admin_sees_the_shared_object_as_restorable(self):
        row = self._rows(self.tadmin, is_admin=True)["shared-agent"]
        self.assertEqual(row["visibility"], "tenant")
        self.assertTrue(row["can_unshare"])
        # Already shared: there is nothing left to share.
        self.assertFalse(row["can_share"])

    def test_after_sharing_the_owner_no_longer_sees_it(self):
        """共享即交出：该对象从原归属人的管理范围里消失（规范 MODIFIED 场景）。"""
        self.svc.set_agent_visibility(
            agent_id="rock-agent", actor_user_id=self.rock, visibility="tenant")
        self.assertNotIn("rock-agent", self._rows(self.rock))

    def test_after_sharing_the_admin_sees_it_as_restorable(self):
        self.svc.set_agent_visibility(
            agent_id="rock-agent", actor_user_id=self.rock, visibility="tenant")
        row = self._rows(self.tadmin, is_admin=True)["rock-agent"]
        self.assertEqual(row["visibility"], "tenant")
        self.assertTrue(row["can_unshare"])


if __name__ == "__main__":
    unittest.main()
