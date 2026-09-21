# encoding:utf-8
"""A member's own private Agent must be reachable *by that member*.

See ``openspec/changes/fix-private-agent-owner-reachability``.

Joining a tenant clones the tenant's 智能办公助理 into a binding whose
``private_owner_user_id`` is the new member. Ownership used to be implemented
only as a *denial* gate: nothing translated it into a permission for the owner,
and the built-in ``member`` role carries no ``agent:<id>`` grant. So the
member's own assistant was filtered out of every read — the workbench answered
with an empty projection while the Agent existed and was healthy.

The rule these tests pin (spec ``rbac-authorization`` / ``agent-chat-launch``):
ownership *is* a grant for ``read`` and ``use``, for the owner only, inside the
binding's own tenant — and never for ``edit`` / ``enable``.
"""

import os
import tempfile
import unittest

import web  # noqa: F401  (real package preferred so HTTPError is authentic)

from auth.runtime import RequestContext
from auth.service import IdentityService
from channel.web.web_channel import (
    _tenant_agents_projection,
    _tenant_agents_admin_projection,
    _workbench_chat_readiness,
)


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

    def _member(username, display, roles=("member",)):
        return svc.create_member(
            actor_user_id=root["id"], tenant_id=tid, operation="create-new",
            username=username, display_name=display,
            temporary_password="TmpPass123!", roles=list(roles))["user_id"]

    return svc, tid, root, _member("rock", "Rock"), _member("jane", "Jane")


class _FakeProfile:
    """Minimal AgentProfile stand-in for the projection tests."""

    def __init__(self, agent_id, name, enabled=True, agent_type="normal",
                 coding_project_dir=None):
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
        self.coding_project_dir = coding_project_dir

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
    """``list()`` returns whatever profiles the test set up."""

    default_agent_id = "shared-assistant"

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


def _ctx(svc, user_id, tenant_id, *, is_tenant_admin=False, extra=()):
    """A real member context: permissions come from the persisted roles."""
    perms = set(svc.permissions_for(user_id, tenant_id)) | set(extra)
    return RequestContext(
        user_id=user_id, username="u", display_name="U",
        is_platform_admin=False, must_change_password=False,
        tenant_id=tenant_id, membership=None, permissions=perms,
        is_tenant_admin=is_tenant_admin)


class _WebCtxCase(unittest.TestCase):
    """Give ``web.HTTPError`` a response context to write headers into."""

    def setUp(self):
        super().setUp()
        web.ctx.headers = []
        web.ctx.status = "200 OK"


class OwnerReachabilityServiceTests(_WebCtxCase):
    """The authorization layers: ``resource_ids_for`` / ``check_resource_action``."""

    def setUp(self):
        super().setUp()
        (self.svc, self.tid, self.root,
         self.rock, self.jane) = _seed()
        self.svc.bind_agent(tenant_id=self.tid, agent_id="shared-assistant")
        self.svc.bind_agent(tenant_id=self.tid, agent_id="rock-assistant",
                            private_owner_user_id=self.rock)
        self.rock_role = self.svc.roles_for_user(self.rock, self.tid) \
            if hasattr(self.svc, "roles_for_user") else None

    # --- the fix: ownership is a read/use grant for the owner -------------

    def test_owner_has_use_on_own_private_agent(self):
        self.assertTrue(self.svc.check_resource_action(
            self.rock, self.tid, "agent", "agent:rock-assistant", "use",
            permission="agent.use"))

    def test_owner_has_read_on_own_private_agent(self):
        self.assertTrue(self.svc.check_resource_action(
            self.rock, self.tid, "agent", "agent:rock-assistant", "read",
            permission="agent.read"))

    def test_owner_read_set_includes_own_private_agent(self):
        ids = self.svc.resource_ids_for(
            self.rock, self.tid, "agent", "read", permission="agent.read")
        self.assertIn("agent:rock-assistant", ids)

    def test_owner_use_set_includes_own_private_agent(self):
        ids = self.svc.resource_ids_for(
            self.rock, self.tid, "agent", "use", permission="agent.use")
        self.assertIn("agent:rock-assistant", ids)

    # --- maintenance: ownership is the authority for the owner's own Agent --

    def test_owner_may_edit_own_private_agent_without_grant(self):
        """Task 3.1: the owner maintains their own object, grant or not."""
        self.assertTrue(self.svc.check_resource_action(
            self.rock, self.tid, "agent", "agent:rock-assistant", "edit",
            permission="agent.edit"))

    def test_owner_may_enable_own_private_agent_without_the_permission(self):
        """The member role has no ``agent.enable``; ownership covers ``enable``."""
        self.assertTrue(self.svc.check_resource_action(
            self.rock, self.tid, "agent", "agent:rock-assistant", "enable",
            permission="agent.enable"))

    def test_edit_set_includes_own_private_agent(self):
        ids = self.svc.resource_ids_for(
            self.rock, self.tid, "agent", "edit", permission="agent.edit")
        self.assertIn("agent:rock-assistant", ids)

    # --- the bound: somebody else's private Agent stays private -----------

    def test_other_member_cannot_read_or_use_it(self):
        for action, permission in (("read", "agent.read"), ("use", "agent.use")):
            self.assertFalse(self.svc.check_resource_action(
                self.jane, self.tid, "agent", "agent:rock-assistant",
                action, permission=permission), action)

    def test_other_member_read_set_excludes_it(self):
        ids = self.svc.resource_ids_for(
            self.jane, self.tid, "agent", "read", permission="agent.read")
        self.assertNotIn("agent:rock-assistant", ids)

    # --- the bound: no functional permission, no relaxation ---------------

    def test_missing_functional_permission_is_not_relaxed(self):
        """A role without ``agent.read`` gets nothing, ownership notwithstanding."""
        self.svc.create_role(
            self.root["id"], self.tid, "launcher", "Launcher", ["chat.use"])
        bare = self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            operation="create-new", username="bare", display_name="Bare",
            temporary_password="TmpPass123!", roles=["launcher"])["user_id"]
        self.svc.bind_agent(tenant_id=self.tid, agent_id="bare-assistant",
                            private_owner_user_id=bare)
        self.assertEqual(
            self.svc.resource_ids_for(
                bare, self.tid, "agent", "read", permission="agent.read"),
            set())
        self.assertFalse(self.svc.check_resource_action(
            bare, self.tid, "agent", "agent:bare-assistant", "read",
            permission="agent.read"))

    # --- the bound: the tenant dimension is still checked ------------------

    def test_owner_of_another_tenants_agent_is_not_reachable(self):
        self.svc.create_tenant(
            actor_user_id=self.root["id"], code="beta", name="Beta",
            shared_root="/s/beta", admin_username="broot",
            admin_display="BRoot", admin_password="Str0ngPass2",
            recent_password="Str0ngAdminPass")
        beta = [t for t in self.svc.list_tenants() if t["code"] == "beta"][0]
        self.svc.bind_agent(tenant_id=beta["id"], agent_id="beta-assistant",
                            private_owner_user_id=self.rock)
        self.assertFalse(self.svc.check_resource_action(
            self.rock, self.tid, "agent", "agent:beta-assistant", "read",
            permission="agent.read"))
        self.assertNotIn(
            "agent:beta-assistant",
            self.svc.resource_ids_for(
                self.rock, self.tid, "agent", "read", permission="agent.read"))

    # --- a shared Agent is unaffected by this rule ------------------------

    def test_shared_agent_still_needs_a_grant(self):
        self.assertFalse(self.svc.check_resource_action(
            self.rock, self.tid, "agent", "agent:shared-assistant", "read",
            permission="agent.read"))
        self.assertNotIn(
            "agent:shared-assistant",
            self.svc.resource_ids_for(
                self.rock, self.tid, "agent", "read", permission="agent.read"))

    # --- non-agent kinds are untouched ------------------------------------

    def test_other_kinds_are_unaffected(self):
        self.assertFalse(self.svc.check_resource_action(
            self.rock, self.tid, "skill", "custom:whatever", "read",
            permission="skill.read"))


class OwnerReachabilityProjectionTests(_WebCtxCase):
    """The read path a member actually hits: the workbench projection."""

    def setUp(self):
        super().setUp()
        (self.svc, self.tid, self.root,
         self.rock, self.jane) = _seed()
        self.svc.bind_agent(tenant_id=self.tid, agent_id="shared-assistant")
        self.svc.bind_agent(tenant_id=self.tid, agent_id="rock-assistant",
                            private_owner_user_id=self.rock)
        # The member's own assistant is registered as *their* default, which is
        # what makes it the resolved default for that member and nobody else.
        self.svc.set_member_default_agent(
            tenant_id=self.tid, user_id=self.rock, agent_id="rock-assistant")
        self.profiles = [
            _FakeProfile("shared-assistant", "Shared"),
            _FakeProfile("rock-assistant", "Rock Assistant"),
            _FakeProfile("bogus", "Bogus"),
        ]

    def _run_projection(self, ctx):
        from unittest.mock import patch
        import channel.web.web_channel as wc
        with _RegistryScope(self.profiles), \
             patch("auth.service.get_identity_service", return_value=self.svc), \
             patch.object(wc, "_tenant_ids_for_context",
                          return_value=self.svc.tenant_agent_ids(self.tid)):
            return _tenant_agents_projection(ctx), _tenant_agents_admin_projection(ctx)

    def test_member_sees_own_private_assistant(self):
        ctx = _ctx(self.svc, self.rock, self.tid)
        workbench, admin = self._run_projection(ctx)
        ids = {a["id"] for a in workbench["agents"]}
        self.assertIn("rock-assistant", ids, (
            "a member's own private assistant must appear in their workbench"))
        self.assertNotIn("shared-assistant", ids, (
            "a non-default shared Agent still needs an explicit grant"))
        self.assertIn("rock-assistant", {a["id"] for a in admin["agents"]})

    def test_member_can_chat_with_own_private_assistant(self):
        ctx = _ctx(self.svc, self.rock, self.tid)
        workbench, _ = self._run_projection(ctx)
        card = next(a for a in workbench["agents"] if a["id"] == "rock-assistant")
        self.assertTrue(card["can_chat"], card.get("unavailable_reason"))
        self.assertTrue(card["is_default"])

    def test_other_member_does_not_see_it(self):
        ctx = _ctx(self.svc, self.jane, self.tid)
        workbench, admin = self._run_projection(ctx)
        self.assertNotIn("rock-assistant", {a["id"] for a in workbench["agents"]})
        self.assertNotIn("rock-assistant", {a["id"] for a in admin["agents"]})

    def test_visibility_does_not_depend_on_being_the_default(self):
        """Clearing the registration must not make the owner's Agent vanish."""
        self.svc.set_member_default_agent(
            tenant_id=self.tid, user_id=self.rock, agent_id="shared-assistant")
        ctx = _ctx(self.svc, self.rock, self.tid)
        workbench, _ = self._run_projection(ctx)
        ids = {a["id"] for a in workbench["agents"]}
        self.assertIn("rock-assistant", ids)
        # ...and the tenant default it was swapped to is reachable too, because
        # the shared-default relaxation is untouched.
        self.assertIn("shared-assistant", ids)

    def test_owner_may_edit_without_a_grant(self):
        """Task 3.1: the owner's own Agent no longer needs a hand-written grant."""
        ctx = _ctx(self.svc, self.rock, self.tid)
        from channel.web.web_channel import _require_agent_action
        from unittest.mock import patch
        with patch("auth.service.get_identity_service", return_value=self.svc):
            _require_agent_action(ctx, "rock-assistant", "edit", "agent.edit")

    def test_another_member_is_still_refused_edit(self):
        ctx = _ctx(self.svc, self.jane, self.tid)
        with self.assertRaises(web.HTTPError) as exc:
            from channel.web.web_channel import _require_agent_action
            from unittest.mock import patch
            with patch("auth.service.get_identity_service", return_value=self.svc):
                _require_agent_action(ctx, "rock-assistant", "edit", "agent.edit")
        self.assertEqual(exc.exception.args[0], "403 Forbidden")

    def test_shared_default_relaxation_still_applies_without_a_grant(self):
        """The pre-existing member entry must not regress."""
        self.svc._appoint_tenant_default_agent(
            tenant_id=self.tid, agent_id="shared-assistant",
            actor_user_id=self.root["id"])
        ctx = _ctx(self.svc, self.jane, self.tid)
        workbench, _ = self._run_projection(ctx)
        ids = {a["id"] for a in workbench["agents"]}
        self.assertIn("shared-assistant", ids)
        card = next(a for a in workbench["agents"] if a["id"] == "shared-assistant")
        self.assertTrue(card["can_chat"])

    def test_readiness_agrees_with_the_projection(self):
        ctx = _ctx(self.svc, self.rock, self.tid)
        from unittest.mock import patch
        with patch("auth.service.get_identity_service", return_value=self.svc):
            can_chat, reason = _workbench_chat_readiness(ctx, "rock-assistant")
        self.assertTrue(can_chat, reason)

    def test_send_path_use_gate_admits_the_owner(self):
        """Readiness alone is not enough: the send path has its own gate."""
        from unittest.mock import patch
        from channel.web.web_channel import _require_agent_action
        ctx = _ctx(self.svc, self.rock, self.tid)
        with patch("auth.service.get_identity_service", return_value=self.svc):
            _require_agent_action(ctx, "rock-assistant", "use", "agent.use")

    def test_send_path_still_refuses_another_tenants_member(self):
        self.svc.create_tenant(
            actor_user_id=self.root["id"], code="beta", name="Beta",
            shared_root="/s/beta", admin_username="broot",
            admin_display="BRoot", admin_password="Str0ngPass2",
            recent_password="Str0ngAdminPass")
        beta = [t for t in self.svc.list_tenants() if t["code"] == "beta"][0]
        from unittest.mock import patch
        from channel.web.web_channel import _require_agent_action
        outsider = _ctx(self.svc, self.jane, beta["id"])
        with self.assertRaises(web.HTTPError) as exc:
            with patch("auth.service.get_identity_service", return_value=self.svc):
                _require_agent_action(outsider, "rock-assistant", "use", "agent.use")
        self.assertEqual(exc.exception.args[0], "403 Forbidden")

    def test_revoking_ownership_takes_effect_on_the_next_call(self):
        """No caching of the authorization conclusion across calls."""
        self.assertTrue(self.svc.check_resource_action(
            self.rock, self.tid, "agent", "agent:rock-assistant", "use",
            permission="agent.use"))
        self.svc.make_agent_tenant_shared(
            agent_id="rock-assistant", actor_user_id=self.root["id"])
        self.assertFalse(self.svc.check_resource_action(
            self.rock, self.tid, "agent", "agent:rock-assistant", "use",
            permission="agent.use"))

    def test_owner_entry_survives_a_different_tenant_shared_default(self):
        """The two relaxation branches must not crowd each other out."""
        self.svc.appoint_tenant_default_agent(
            tenant_id=self.tid, agent_id="shared-assistant",
            actor_user_id=self.root["id"])
        ctx = _ctx(self.svc, self.rock, self.tid)
        workbench, _ = self._run_projection(ctx)
        by_id = {a["id"]: a for a in workbench["agents"]}
        # The member's own assistant is the resolved default for *this* member,
        # so it stays their entry even though the tenant default is shared.
        self.assertIn("rock-assistant", by_id)
        self.assertTrue(by_id["rock-assistant"]["is_default"])
        self.assertEqual(by_id["rock-assistant"].get("unavailable_reason"), None)


class EmptyReasonTests(_WebCtxCase):
    """An empty projection must say *why* it is empty."""

    def setUp(self):
        super().setUp()
        (self.svc, self.tid, self.root,
         self.rock, self.jane) = _seed()

    def _run(self, ctx, profiles):
        from unittest.mock import patch
        import channel.web.web_channel as wc
        with _RegistryScope(profiles), \
             patch("auth.service.get_identity_service", return_value=self.svc), \
             patch.object(wc, "_tenant_ids_for_context",
                          return_value=self.svc.tenant_agent_ids(self.tid)):
            return _tenant_agents_projection(ctx)

    def test_no_bound_agents_reports_the_plain_empty_case(self):
        ctx = _ctx(self.svc, self.rock, self.tid)
        data = self._run(ctx, [_FakeProfile("ghost", "Ghost")])
        self.assertEqual(data["agents"], [])
        self.assertEqual(data["empty_reason"], "no_agents")

    def test_candidates_blocked_by_authorization_are_distinguished(self):
        """Someone else's private Agent is bound, enabled — and out of reach."""
        self.svc.bind_agent(tenant_id=self.tid, agent_id="rock-assistant",
                            private_owner_user_id=self.rock)
        ctx = _ctx(self.svc, self.jane, self.tid)
        data = self._run(ctx, [_FakeProfile("rock-assistant", "Rock Assistant")])
        self.assertEqual(data["agents"], [])
        self.assertEqual(data["empty_reason"], "no_reachable_agents")

    def test_reason_does_not_leak_the_hidden_agents_identity(self):
        self.svc.bind_agent(tenant_id=self.tid, agent_id="confidential-agent",
                            private_owner_user_id=self.rock)
        ctx = _ctx(self.svc, self.jane, self.tid)
        data = self._run(ctx, [_FakeProfile("confidential-agent", "Confidential")])
        blob = repr(data)
        self.assertNotIn("confidential-agent", blob)
        self.assertNotIn("Confidential", blob)

    def test_non_empty_projection_has_no_reason(self):
        self.svc.bind_agent(tenant_id=self.tid, agent_id="rock-assistant",
                            private_owner_user_id=self.rock)
        ctx = _ctx(self.svc, self.rock, self.tid)
        data = self._run(ctx, [_FakeProfile("rock-assistant", "Rock Assistant")])
        self.assertEqual([a["id"] for a in data["agents"]], ["rock-assistant"])
        self.assertIsNone(data.get("empty_reason"))


class KnowledgeWriteProjectionTests(_WebCtxCase):
    """``can_write_knowledge`` mirrors the data-root + ownership decision."""

    def setUp(self):
        super().setUp()
        (self.svc, self.tid, self.root, self.rock, self.jane) = _seed()
        tmp = tempfile.mkdtemp()
        own_ws = os.path.join(tmp, "own")
        os.makedirs(os.path.join(own_ws, "knowledge"), exist_ok=True)
        shared_ws = os.path.join(tmp, "shared")
        os.makedirs(shared_ws, exist_ok=True)

        own = _FakeProfile("own-assistant", "Own")
        own.workspace = own_ws
        shared = _FakeProfile("shared-assistant", "Shared")
        shared.workspace = shared_ws
        self.profiles = [own, shared]

        self.svc.bind_agent(tenant_id=self.tid, agent_id="own-assistant",
                            private_owner_user_id=self.rock)
        self.svc.bind_agent(tenant_id=self.tid, agent_id="shared-assistant",
                            private_owner_user_id=self.rock)

    def _env(self):
        from contextlib import ExitStack
        from unittest.mock import patch

        import channel.web.web_channel as wc

        stack = ExitStack()
        stack.enter_context(_RegistryScope(self.profiles))
        stack.enter_context(
            patch("auth.service.get_identity_service", return_value=self.svc))
        stack.enter_context(patch.object(
            wc, "_tenant_ids_for_context",
            return_value=self.svc.tenant_agent_ids(self.tid)))
        return stack

    def test_projection_matches_the_write_path_for_every_visible_agent(self):
        from channel.web.web_channel import _knowledge_write_authorized

        ctx = _ctx(self.svc, self.rock, self.tid)
        with self._env():
            agents = {a["id"]: a for a in _tenant_agents_admin_projection(ctx)["agents"]}
            expected = {aid: _knowledge_write_authorized(ctx, aid) for aid in agents}

        self.assertTrue(set(agents), "the owner must see their own agents")
        for aid, agent in agents.items():
            self.assertEqual(agent["can_write_knowledge"], expected[aid], aid)

    def test_the_owner_writes_the_independent_base_but_not_the_shared_one(self):
        ctx = _ctx(self.svc, self.rock, self.tid)
        with self._env():
            agents = {a["id"]: a for a in _tenant_agents_admin_projection(ctx)["agents"]}

        self.assertTrue(agents["own-assistant"]["can_write_knowledge"])
        self.assertFalse(agents["shared-assistant"]["can_write_knowledge"], (
            "a private Agent left on shared mode must not become a write"
            " channel into the tenant's shared base"))

    def test_the_projection_does_not_leak_the_owner_identifier(self):
        ctx = _ctx(self.svc, self.rock, self.tid)
        with self._env():
            agents = {a["id"]: a for a in _tenant_agents_admin_projection(ctx)["agents"]}

        for agent in agents.values():
            self.assertNotIn("private_owner_user_id", agent)
            self.assertNotIn("workspace", agent)


class AdminKnowledgeWriteProjectionTests(_WebCtxCase):
    """Task 2.1/3.5: the management read is scoped *before* write-capability.

    A tenant admin's console now reads the **management object scope**
    (``auth.object_scope``), so a member's private Agent is not in the roster at
    all: the read never names it, which is what removes the existence leak the
    earlier per-row "not writable" flag still carried. The write path
    (``_knowledge_write_authorized``) has always been owner-first; the projection
    now agrees with it because both answer from the same scope.

    Non-vacuity is kept in both directions: the tenant's **shared** Agent stays in
    the roster and stays writable, and the private Agent's owner still sees and
    writes their own base.
    """

    def setUp(self):
        super().setUp()
        (self.svc, self.tid, self.root, self.rock, self.jane) = _seed()
        # A *tenant* admin, not the platform admin: the platform admin's ``all``
        # mode short-circuits the write decision before ownership is consulted,
        # which is exactly the shortcut this class must not rely on.
        self.tenant_admin = self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            operation="create-new", username="tadmin", display_name="T Admin",
            temporary_password="TmpPass123!", roles=["tenant_admin"])["user_id"]
        self.admin_token = self.svc.login("tadmin", "TmpPass123!").token
        self.svc.change_password(self.admin_token, "TmpPass123!", "TAdminFinal1")
        tmp = tempfile.mkdtemp()
        self.own_ws = os.path.join(tmp, "own")
        os.makedirs(os.path.join(self.own_ws, "knowledge"), exist_ok=True)
        self.shared_ws = os.path.join(tmp, "shared")
        os.makedirs(self.shared_ws, exist_ok=True)
        own = _FakeProfile("own-assistant", "Own")
        own.workspace = self.own_ws
        shared = _FakeProfile("shared-assistant", "Shared")
        shared.workspace = self.shared_ws
        self.profiles = [own, shared]
        # ``own-assistant`` belongs to rock; the admin is a different member.
        self.svc.bind_agent(tenant_id=self.tid, agent_id="own-assistant",
                            private_owner_user_id=self.rock)
        self.svc.bind_agent(tenant_id=self.tid, agent_id="shared-assistant")

    def _env(self):
        from contextlib import ExitStack
        from unittest.mock import patch

        import channel.web.web_channel as wc

        stack = ExitStack()
        stack.enter_context(_RegistryScope(self.profiles))
        stack.enter_context(
            patch("auth.service.get_identity_service", return_value=self.svc))
        stack.enter_context(patch.object(
            wc, "_tenant_ids_for_context",
            return_value=self.svc.tenant_agent_ids(self.tid)))
        return stack

    def _admin_ctx(self):
        return _ctx(self.svc, self.tenant_admin, self.tid, is_tenant_admin=True)

    def test_a_tenant_admin_does_not_see_a_members_private_base(self):
        ctx = self._admin_ctx()
        with self._env():
            agents = {a["id"]: a for a in _tenant_agents_admin_projection(ctx)["agents"]}

        self.assertNotIn("own-assistant", agents, (
            "the management read must not name another member's private Agent:"
            " a per-row 'not writable' flag still leaks that the object exists"))
        self.assertIn("shared-assistant", agents, (
            "the scope is not a blanket refusal: the tenant's shared Agent stays"))

    def test_the_same_projection_still_marks_shared_agents_writable(self):
        """Non-vacuity: the projection is an answer, not a constant ``False``."""
        ctx = self._admin_ctx()
        with self._env():
            agents = {a["id"]: a for a in _tenant_agents_admin_projection(ctx)["agents"]}

        self.assertTrue(agents["shared-assistant"]["can_write_knowledge"])

    def test_the_admin_projection_matches_the_write_path_for_every_agent(self):
        from channel.web.web_channel import _knowledge_write_authorized

        ctx = self._admin_ctx()
        with self._env():
            agents = {a["id"]: a for a in _tenant_agents_admin_projection(ctx)["agents"]}
            expected = {aid: _knowledge_write_authorized(ctx, aid) for aid in agents}

        self.assertTrue(set(agents))
        for aid, agent in agents.items():
            self.assertEqual(agent["can_write_knowledge"], expected[aid], aid)

    def test_the_owner_still_writes_its_own_independent_base(self):
        ctx = _ctx(self.svc, self.rock, self.tid)
        with self._env():
            agents = {a["id"]: a for a in _tenant_agents_admin_projection(ctx)["agents"]}

        self.assertTrue(agents["own-assistant"]["can_write_knowledge"])


if __name__ == "__main__":
    unittest.main()
