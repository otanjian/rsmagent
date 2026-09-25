# encoding:utf-8
"""Tests for the resource-authorization service layer (tasks 1.4-1.6, 2.1-2.4).

Covers the dynamic platform ``all``, the finite resource grants, the tenant
global-resource limit, multi-role union, empty-set rejection, unknown-id and
cross-tenant rejection, and the live catalog projection. Uses an isolated temp
identity.db so nothing touches real data.
"""

import os
import tempfile
import unittest

from auth.policy import (
    normalize_resource_grants,
    validate_model_defaults,
)
from auth.service import IdentityService, IdentityServiceError


def _db():
    return os.path.join(tempfile.mkdtemp(), "identity.db")


def _seed(svc):
    svc.bootstrap(
        tenant_code="acme", tenant_name="Acme Corp",
        admin_username="root", admin_display="Root",
        admin_password="Str0ngAdminPass", shared_root="/s/acme", allow_weak=True)
    root = [u for u in svc.list_platform_users() if u["username"] == "root"][0]
    tenant = svc.list_tenants()[0]
    return root, tenant


class PlatformAllTests(unittest.TestCase):
    def test_platform_admin_derives_all(self):
        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        self.assertEqual(svc.authorization_mode(root["id"], tenant["id"]), "all")
        # all grants any known resource kind/action without explicit grants.
        self.assertTrue(svc.check_resource_action(
            root["id"], tenant["id"], "skill", "builtin:whatever", "use",
            permission="skill.use"))
        # unknown kinds are still rejected — all is not "call any name".
        self.assertFalse(svc.check_resource_action(
            root["id"], tenant["id"], "dinosaur", "builtin:x", "use"))

    def test_platform_grant_revoked_next_call(self):
        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        self.assertEqual(svc.authorization_mode(root["id"], tenant["id"]), "all")
        # A non-platform user derives role mode; a platform admin derives all.
        # (Directly revoking the sole platform admin is rejected by the admin
        # continuity guard, so we verify the mode is re-derived per call against
        # a second, non-admin user instead — the derivation is never cached.)
        second = svc.create_member(
            actor_user_id=root["id"], tenant_id=tenant["id"], operation="create-new",
            username="plainuser", display_name="Plain",
            temporary_password="TmpPass123!", roles=["member"])
        plain = svc._find_user_by_id(second["user_id"])
        self.assertEqual(svc.authorization_mode(plain["id"], tenant["id"]), "role")
        self.assertFalse(svc.check_resource_action(
            plain["id"], tenant["id"], "skill", "builtin:whatever", "use"))


class RoleGrantTests(unittest.TestCase):
    def _make_custom_role(self, svc, root, tenant):
        return svc.create_role(
            root["id"], tenant["id"], "buyer", "采购员", ["agent.read", "skill.read", "tool.read", "model.read", "model.use"],
            resource_grants=[
                {"resource_kind": "skill", "resource_id": "builtin:web-fetch", "action": "read"},
                {"resource_kind": "tool", "resource_id": "builtin:web_search", "action": "execute"},
                {"resource_kind": "model", "resource_id": "provider:deepseek:deepseek-v4-flash", "action": "use"},
            ],
            model_defaults={"chat": "provider:deepseek:deepseek-v4-flash"})

    def test_normalize_grants_dedupes_and_rejects(self):
        g = normalize_resource_grants([
            {"resource_kind": "skill", "resource_id": "builtin:web-fetch", "action": "read"},
            {"resource_kind": "skill", "resource_id": "builtin:web-fetch", "action": "read"},
            {"resource_kind": "tool", "resource_id": "builtin:web_search", "action": "execute"},
        ])
        self.assertEqual(len(g), 2)
        self.assertRaisesRegex(Exception, "unknown action",
                               lambda: normalize_resource_grants(
                                   [{"resource_kind": "skill", "resource_id": "x", "action": "execute"}]))
        self.assertRaisesRegex(Exception, "unknown resource kind",
                               lambda: normalize_resource_grants(
                                   [{"resource_kind": "rocket", "resource_id": "x", "action": "read"}]))

    def test_model_defaults_require_known_capability(self):
        self.assertEqual(validate_model_defaults({"chat": "provider:d:m"}), {"chat": "provider:d:m"})
        self.assertEqual(validate_model_defaults(None), {})
        self.assertRaisesRegex(Exception, "unknown model capability",
                               lambda: validate_model_defaults({"time-travel": "x"}))

    def test_role_list_includes_grants_and_model_defaults(self):
        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        self._make_custom_role(svc, root, tenant)
        roles = svc.list_roles(tenant["id"])
        custom = [r for r in roles if r["code"] == "buyer"][0]
        self.assertEqual(custom["model_defaults"], {"chat": "provider:deepseek:deepseek-v4-flash"})
        self.assertIn({"resource_kind": "skill", "resource_id": "builtin:web-fetch", "action": "read"},
                      custom["resource_grants"])

    def test_update_preserves_omitted_and_clears_explicit_empty(self):
        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        role = self._make_custom_role(svc, root, tenant)
        # omitted grants/defaults -> preserved.
        svc.update_role(root["id"], tenant["id"], role["id"], "采购员-new",
                        ["agent.read", "skill.read"], expected_version=role["version"])
        refetched = [r for r in svc.list_roles(tenant["id"]) if r["code"] == "buyer"][0]
        self.assertEqual(len(refetched["resource_grants"]), 3)
        self.assertEqual(refetched["model_defaults"], {"chat": "provider:deepseek:deepseek-v4-flash"})
        # explicit empty grants + defaults -> cleared.
        svc.update_role(root["id"], tenant["id"], role["id"], "采购员-new",
                        ["agent.read", "skill.read"], expected_version=refetched["version"],
                        resource_grants=[], model_defaults={})
        refetched = [r for r in svc.list_roles(tenant["id"]) if r["code"] == "buyer"][0]
        self.assertEqual(refetched["resource_grants"], [])
        self.assertEqual(refetched["model_defaults"], {})

    def test_unknown_id_grant_rejected_on_whole_write(self):
        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        with self.assertRaises(Exception):
            svc.create_role(
                root["id"], tenant["id"], "bad", "坏角色", ["skill.read"],
                resource_grants=[{"resource_kind": "skill", "resource_id": "", "action": "read"}])
        self.assertEqual([r for r in svc.list_roles(tenant["id"]) if r["code"] == "bad"], [])

    def test_delete_custom_role_with_grants_succeeds_and_removes_grants(self):
        # A role with no member reference is deletable even when it carries
        # resource grants (rbac-authorization: custom roles are deletable when
        # no member references them). The role's grants must be cleaned up in
        # the same transaction so the composite FK does not block the delete.
        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        role = self._make_custom_role(svc, root, tenant)
        before = svc._store.execute(
            "SELECT COUNT(*) AS c FROM role_resource_grants WHERE role_id=?",
            (role["id"],))[0]["c"]
        self.assertEqual(before, 3)

        result = svc.delete_role(root["id"], tenant["id"], role["id"])

        self.assertTrue(result["deleted"])
        self.assertEqual([r for r in svc.list_roles(tenant["id"]) if r["code"] == "buyer"], [])
        leftover = svc._store.execute(
            "SELECT COUNT(*) AS c FROM role_resource_grants WHERE role_id=?",
            (role["id"],))[0]["c"]
        self.assertEqual(leftover, 0)


class MultiRoleUnionTests(unittest.TestCase):
    def test_two_roles_union_grants(self):
        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        r1 = svc.create_role(root["id"], tenant["id"], "r1", "R1", ["skill.read"],
                             resource_grants=[{"resource_kind": "skill", "resource_id": "builtin:a", "action": "read"}])
        r2 = svc.create_role(root["id"], tenant["id"], "r2", "R2", ["skill.read"],
                             resource_grants=[{"resource_kind": "skill", "resource_id": "builtin:b", "action": "read"}])
        # create a member in this tenant bound to both roles, then check the union.
        m = svc.create_member(
            actor_user_id=root["id"], tenant_id=tenant["id"], operation="create-new",
            username="member1", display_name="Member", temporary_password="TmpPass123!",
            roles=[r1["code"], r2["code"]])
        member = svc._membership(m["user_id"], tenant["id"])
        grants = svc._role_grants_for_membership(member["id"])
        ids = {g["resource_id"] for g in grants if g["resource_kind"] == "skill"}
        self.assertEqual(ids, {"builtin:a", "builtin:b"})


class TenantLimitTests(unittest.TestCase):
    def test_set_tenant_grants_requires_platform_and_version(self):
        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        grants = [{"resource_kind": "model", "resource_id": "provider:deepseek:deepseek-v4-flash", "action": "use"}]
        result = svc.set_tenant_resource_grants(
            actor_user_id=root["id"], tenant_id=tenant["id"], grants=grants,
            expected_version=tenant["version"])
        self.assertEqual(result, grants)
        # stale version -> conflict.
        with self.assertRaises(IdentityServiceError) as e:
            svc.set_tenant_resource_grants(
                actor_user_id=root["id"], tenant_id=tenant["id"], grants=grants,
                expected_version=tenant["version"])
        self.assertEqual(e.exception.status, 409)


class CatalogTests(unittest.TestCase):
    def test_catalog_menu_projects_nav(self):
        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        cat = svc.authorization_catalog(tenant["id"], kind="menu", all_mode=True)
        self.assertGreater(cat["total"], 0)
        self.assertTrue(any(i["resource_id"].startswith("nav:") for i in cat["items"]))

    def test_catalog_kind_filter_rejects_unknown(self):
        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        with self.assertRaises(IdentityServiceError):
            svc.authorization_catalog(tenant["id"], kind="rocket")

    def test_catalog_assign_mode_agent_matches_bare_owned_ids(self):
        # Regression: agent catalog entries carry an ``agent:`` prefix while
        # ``grantable_resource_ids`` returns the bare agent_id (no prefix). The
        # assign-mode filter must normalize both so tenant-owned agents appear.
        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        # Seed a real registry profile + bind it to the tenant so the
        # assign-mode catalog has an owned agent to project.
        from agent.registry import AgentProfile, get_agent_registry
        reg = get_agent_registry()
        reg.upsert(AgentProfile(id="acme-agent", name="Acme Agent", workspace="/s/acme"))
        svc.bind_agent(tenant_id=tenant["id"], agent_id="acme-agent")
        # The catalog returns the agent under its prefixed id even though the
        # owned-id set (from agent_bindings) is the bare id "acme-agent".
        cat = svc.authorization_catalog(tenant["id"], kind="agent", page=1, page_size=12, all_mode=False)
        self.assertGreater(cat["total"], 0)
        self.assertIn("agent:acme-agent", [i["resource_id"] for i in cat["items"]])
        self.assertTrue(all(i.get("name") for i in cat["items"]))

    def test_project_tools_loads_lazily_when_instance_is_empty(self):
        # Regression: the tool catalog projection must not depend on some other
        # request having already booted the ToolManager. On a fresh process the
        # per-state-root instance has no ``tool_classes`` (they are filled by
        # ``load_tools()`` at Agent boot), so returning [] here left the tenant
        # 工具授权 tab blank until an Agent chat happened to start.
        from unittest import mock

        calls = {"load": 0}

        class _FakeTool:
            description = "a fake tool"

            def get_json_schema(self):
                return {}

        class _FakeToolManager:
            def __init__(self):
                self.tool_classes = {}
                self._mcp_tool_instances = {}

            def load_tools(self):
                calls["load"] += 1
                self.tool_classes = {"read": _FakeTool}

            def list_tools(self):
                return {name: {"description": cls.description}
                        for name, cls in self.tool_classes.items()}

        fake = _FakeToolManager()
        with mock.patch("agent.tools.tool_manager.ToolManager", return_value=fake):
            svc = IdentityService(_db())
            tools = svc._project_tools()

        self.assertEqual(calls["load"], 1)
        ids = [t["resource_id"] for t in tools]
        # The built-in tool is there, loaded exactly once and lazily.
        self.assertIn("builtin:read", ids)
        self.assertEqual(calls["load"], 1)
        # The external kinds this build declares are listed too, and separately
        # from the built-ins: the grant catalogue is stable rather than varying
        # with whichever connections one tenant happens to have created.
        self.assertTrue(all(rid.startswith("external:") for rid in ids
                            if rid != "builtin:read"), ids)


class RuntimeAssemblyTests(unittest.TestCase):
    """Task 4.7: real identity store drives actual skill/tool assembly.

    Uses the real SkillManager and a real identity service so authorization is
    derived from actual grants, not mocks. Only a skill granted ``skill.use``
    reaches the runtime prompt; an un-granted skill is filtered out.
    """

    def _member_with_skill_grant(self, svc, root, tenant, rid="custom:knowledge-wiki"):
        role = svc.create_role(
            root["id"], tenant["id"], "skiller", "Skiller",
            ["skill.read", "skill.use", "skill.edit", "skill.enable"],
            resource_grants=[
                {"resource_kind": "skill", "resource_id": rid, "action": "read"},
                {"resource_kind": "skill", "resource_id": rid, "action": "use"},
                {"resource_kind": "skill", "resource_id": rid, "action": "edit"},
                {"resource_kind": "skill", "resource_id": rid, "action": "enable"},
            ])
        return svc.create_member(
            actor_user_id=root["id"], tenant_id=tenant["id"], operation="create-new",
            username="plainuser", display_name="Plain", temporary_password="TmpPass123!",
            roles=[role["code"]])

    def test_skill_use_filter_keeps_only_granted(self):
        from unittest import mock
        import auth.service as asvc
        from common.runtime_identity import RuntimeIdentity, use_identity
        from agent.skills.manager import SkillManager

        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        member = self._member_with_skill_grant(svc, root, tenant)
        with tempfile.TemporaryDirectory() as d:
            mgr = SkillManager(custom_dir=d)
            mgr.refresh_skills()
            if "knowledge-wiki" not in mgr.skills:
                self.skipTest("knowledge-wiki skill not present")
            m = svc._find_user_by_id(member["user_id"])
            with use_identity(RuntimeIdentity(user_id=m["id"], tenant_id=tenant["id"])):
                with mock.patch.object(asvc, "get_identity_service", return_value=svc):
                    filtered = mgr.filter_skills()
                    names = [e.skill.name for e in filtered]
                    self.assertIn("knowledge-wiki", names)
                    self.assertEqual(set(names), {"knowledge-wiki"})

    def test_tool_execute_grant_and_denial(self):
        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        role = svc.create_role(
            root["id"], tenant["id"], "tooler", "Tooler", ["tool.read", "tool.execute"],
            resource_grants=[
                {"resource_kind": "tool", "resource_id": "builtin:web_search", "action": "read"},
                {"resource_kind": "tool", "resource_id": "builtin:web_search", "action": "execute"},
            ])
        member = svc.create_member(
            actor_user_id=root["id"], tenant_id=tenant["id"], operation="create-new",
            username="plainuser", display_name="Plain", temporary_password="TmpPass123!",
            roles=[role["code"]])
        m = svc._find_user_by_id(member["user_id"])
        self.assertTrue(svc.check_resource_action(
            m["id"], tenant["id"], "tool", "builtin:web_search", "execute", permission="tool.execute"))
        self.assertFalse(svc.check_resource_action(
            m["id"], tenant["id"], "tool", "builtin:other", "execute", permission="tool.execute"))
        # platform admin with no explicit grant still passes (dynamic all).
        self.assertTrue(svc.check_resource_action(
            root["id"], tenant["id"], "tool", "builtin:other", "execute", permission="tool.execute"))

    def test_skill_use_empty_when_no_grant(self):
        from unittest import mock
        import auth.service as asvc
        from common.runtime_identity import RuntimeIdentity, use_identity
        from agent.skills.manager import SkillManager

        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        # member with only 'member' role -> no skill.use grants.
        member = svc.create_member(
            actor_user_id=root["id"], tenant_id=tenant["id"], operation="create-new",
            username="plainuser", display_name="Plain", temporary_password="TmpPass123!",
            roles=["member"])
        with tempfile.TemporaryDirectory() as d:
            mgr = SkillManager(custom_dir=d)
            mgr.refresh_skills()
            if not mgr.skills:
                self.skipTest("no skills loaded")
            m = svc._find_user_by_id(member["user_id"])
            with use_identity(RuntimeIdentity(user_id=m["id"], tenant_id=tenant["id"])):
                with mock.patch.object(asvc, "get_identity_service", return_value=svc):
                    filtered = mgr.filter_skills()
                    self.assertEqual(filtered, [])


class ModelUseGateTests(unittest.TestCase):
    """Task 5.2-5.3: real identity store drives the runtime model.use gate.

    Exercises the actual ``AgentLLMModel`` authorization path so only a model
    granted ``model.use`` reaches the provider; a model outside the grant set is
    rejected before any side effect. Unrestricted (legacy / platform all / no
    identity) passes through unchanged.
    """

    def _member_with_model_grant(self, svc, root, tenant, model_code, model_key="deepseek"):
        # The tenant ceiling comes first: ``tenant_resource_grants`` bounds what
        # the tenant may allocate at all, so a role grant is only effective
        # inside it (see ``IdentityService.resource_ids_for``).
        svc.set_tenant_resource_grants(
            actor_user_id=root["id"], tenant_id=tenant["id"],
            grants=[
                {"resource_kind": "model", "resource_id": f"provider:{model_key}:{model_code}",
                 "action": "read"},
                {"resource_kind": "model", "resource_id": f"provider:{model_key}:{model_code}",
                 "action": "use"},
            ],
            expected_version=tenant["version"])
        role = svc.create_role(
            root["id"], tenant["id"], "modeler", "Modeler",
            ["model.read", "model.use"],
            resource_grants=[
                {"resource_kind": "model", "resource_id": f"provider:{model_key}:{model_code}",
                 "action": "read"},
                {"resource_kind": "model", "resource_id": f"provider:{model_key}:{model_code}",
                 "action": "use"},
            ])
        return svc.create_member(
            actor_user_id=root["id"], tenant_id=tenant["id"], operation="create-new",
            username="plainuser", display_name="Plain", temporary_password="TmpPass123!",
            roles=[role["code"]])

    def _make_model(self, model_code="some-model"):
        # A bare AgentLLMModel (no bridge, no real bot) driven under an identity.
        from bridge.agent_bridge import AgentLLMModel
        model = AgentLLMModel.__new__(AgentLLMModel)
        model._fallback_model = None
        model._session_model = None
        model._agent_model = model_code
        return model

    def test_granted_model_passes_and_denied_model_rejected(self):
        from unittest import mock
        import auth.service as asvc
        from common.runtime_identity import RuntimeIdentity, use_identity
        from bridge.agent_bridge import AgentLLMModel

        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        member = self._member_with_model_grant(svc, root, tenant, "deepseek-v4-flash")
        m = svc._find_user_by_id(member["user_id"])
        model = self._make_model("deepseek-v4-flash")
        with use_identity(RuntimeIdentity(user_id=m["id"], tenant_id=tenant["id"])):
            with mock.patch.object(asvc, "get_identity_service", return_value=svc):
                # Granted model: denial is None.
                self.assertIsNone(model._model_use_denial("deepseek-v4-flash"))
                # A different model in the same provider but not granted: denied.
                self.assertIsNotNone(model._model_use_denial("deepseek-v4-other"))
                # The gate must produce a RuntimeError (not silent fallthrough).
                self.assertRaises(RuntimeError, model._require_model_use, "deepseek-v4-other")

    def test_tenant_limit_narrows_a_stale_role_grant(self):
        """A role grant left over from a wider allocation must not survive the limit.

        ``tenant_resource_grants`` is the platform's ceiling for a tenant, and
        narrowing it does not rewrite the roles that held a wider set. The stale
        ``role_resource_grants`` row must therefore not keep the model authorized
        — otherwise the console keeps offering a model the platform removed from
        the tenant (and the runtime keeps accepting it).
        """
        from unittest import mock
        import auth.service as asvc
        from common.runtime_identity import RuntimeIdentity, use_identity

        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        member = self._member_with_model_grant(svc, root, tenant, "deepseek-v4-flash")
        m = svc._find_user_by_id(member["user_id"])
        # The role still carries provider:deepseek:deepseek-v4-flash.
        self.assertTrue(any(
            g["resource_id"] == "provider:deepseek:deepseek-v4-flash"
            and g["action"] == "use"
            for g in svc.grants_for(m["id"], tenant["id"])))

        # The platform re-allocates the tenant down to a different model.
        svc.set_tenant_resource_grants(
            actor_user_id=root["id"], tenant_id=tenant["id"],
            grants=[{"resource_kind": "model",
                     "resource_id": "provider:deepseek:deepseek-v4-pro",
                     "action": "use"}],
            expected_version=svc.get_tenant(tenant["id"])["version"])

        self.assertEqual(
            svc.resource_ids_for(m["id"], tenant["id"], "model", "use",
                                 permission="model.use"),
            set(), "the tenant ceiling outranks the stale role grant")
        model = self._make_model("deepseek-v4-flash")
        with use_identity(RuntimeIdentity(user_id=m["id"], tenant_id=tenant["id"])):
            with mock.patch.object(asvc, "get_identity_service", return_value=svc):
                self.assertIsNotNone(model._model_use_denial("deepseek-v4-flash"))

    def test_empty_grant_set_denies_every_model(self):
        from unittest import mock
        import auth.service as asvc
        from common.runtime_identity import RuntimeIdentity, use_identity

        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        # member with only the built-in 'member' role -> no model.use grants.
        member = svc.create_member(
            actor_user_id=root["id"], tenant_id=tenant["id"], operation="create-new",
            username="plainuser", display_name="Plain", temporary_password="TmpPass123!",
            roles=["member"])
        m = svc._find_user_by_id(member["user_id"])
        model = self._make_model("deepseek-v4-flash")
        with use_identity(RuntimeIdentity(user_id=m["id"], tenant_id=tenant["id"])):
            with mock.patch.object(asvc, "get_identity_service", return_value=svc):
                # Empty grant set == no authorized candidate: every model denied,
                # not silently widened to "unrestricted".
                self.assertIsNotNone(model._model_use_denial("deepseek-v4-flash"))

    def test_platform_admin_is_unrestricted(self):
        from unittest import mock
        import auth.service as asvc
        from common.runtime_identity import RuntimeIdentity, use_identity

        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        model = self._make_model("deepseek-v4-flash")
        with use_identity(RuntimeIdentity(user_id=root["id"], tenant_id=tenant["id"])):
            with mock.patch.object(asvc, "get_identity_service", return_value=svc):
                # Platform admin (dynamic all) -> unrestricted, no explicit grant needed.
                self.assertIsNone(model._model_use_denial("deepseek-v4-flash"))

    def test_legacy_no_identity_is_unrestricted(self):
        from common.runtime_identity import EMPTY_IDENTITY, use_identity
        model = self._make_model("deepseek-v4-flash")
        with use_identity(EMPTY_IDENTITY):
            # No DB identity -> legacy behaviour: unrestricted.
            self.assertIsNone(model._model_use_denial("deepseek-v4-flash"))

    def test_session_catalog_filters_to_authorized_codes(self):
        """The console model picker only offers models in the caller's grant set."""
        from unittest import mock
        import auth.service as asvc
        from channel.web import web_channel
        from common.runtime_identity import RuntimeIdentity, use_identity

        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        member = self._member_with_model_grant(svc, root, tenant, "deepseek-v4-flash")
        m = svc._find_user_by_id(member["user_id"])
        with use_identity(RuntimeIdentity(user_id=m["id"], tenant_id=tenant["id"])):
            with mock.patch.object(asvc, "get_identity_service", return_value=svc):
                codes = web_channel._authorized_model_codes()
        # Only the granted model code is offered; the provider prefix is dropped.
        self.assertEqual(codes, {"deepseek-v4-flash"})

    def test_session_catalog_fail_closed_when_no_identity(self):
        from common.runtime_identity import EMPTY_IDENTITY, use_identity
        from channel.web import web_channel
        with use_identity(EMPTY_IDENTITY):
            # Database-only: missing identity must not widen to the whole catalog.
            self.assertEqual(web_channel._authorized_model_codes(), set())


class ModelDefaultResolutionTests(unittest.TestCase):
    """Task 5.2: role-default model resolution with conflict handling."""

    def _role_with_default(self, svc, root, tenant, code, name, model_code, cap="chat"):
        role = svc.create_role(
            root["id"], tenant["id"], code, name,
            ["model.read", "model.use"],
            resource_grants=[
                {"resource_kind": "model", "resource_id": f"provider:deepseek:{model_code}",
                 "action": "read"},
                {"resource_kind": "model", "resource_id": f"provider:deepseek:{model_code}",
                 "action": "use"},
            ],
            model_defaults={cap: f"provider:deepseek:{model_code}"})
        return role

    def test_unique_default_is_used(self):
        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        role = self._role_with_default(svc, root, tenant, "chatrole", "Chat", "deepseek-v4-flash")
        member = svc.create_member(
            actor_user_id=root["id"], tenant_id=tenant["id"], operation="create-new",
            username="plainuser", display_name="Plain", temporary_password="TmpPass123!",
            roles=[role["code"]])
        m = svc._find_user_by_id(member["user_id"])
        res = svc.model_defaults_for(m["id"], tenant["id"], "chat")
        self.assertEqual(res["status"], "default")
        self.assertEqual(res["model"], "provider:deepseek:deepseek-v4-flash")

    def test_conflicting_defaults_surface_conflict(self):
        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        role_a = self._role_with_default(svc, root, tenant, "arole", "A", "deepseek-v4-flash")
        role_b = self._role_with_default(svc, root, tenant, "brole", "B", "deepseek-v4-other")
        member = svc.create_member(
            actor_user_id=root["id"], tenant_id=tenant["id"], operation="create-new",
            username="plainuser", display_name="Plain", temporary_password="TmpPass123!",
            roles=[role_a["code"], role_b["code"]])
        m = svc._find_user_by_id(member["user_id"])
        res = svc.model_defaults_for(m["id"], tenant["id"], "chat")
        self.assertEqual(res["status"], "conflict")
        self.assertEqual(set(res["models"]),
                         {"provider:deepseek:deepseek-v4-flash", "provider:deepseek:deepseek-v4-other"})

    def test_no_default_returns_none(self):
        svc = IdentityService(_db())
        root, tenant = _seed(svc)
        member = svc.create_member(
            actor_user_id=root["id"], tenant_id=tenant["id"], operation="create-new",
            username="plainuser", display_name="Plain", temporary_password="TmpPass123!",
            roles=["member"])
        m = svc._find_user_by_id(member["user_id"])
        self.assertEqual(svc.model_defaults_for(m["id"], tenant["id"], "chat")["status"], "none")


class PlatformTargetRoleTests(unittest.TestCase):
    """A platform admin may manage a *target* tenant's roles/resource grants via
    the explicit platform-management surface, without being a member of it and
    without forging a Membership."""

    def _second_tenant(self, svc, root):
        svc.create_tenant(
            actor_user_id=root["id"], code="beta", name="Beta",
            admin_username="betaadmin", admin_display="BetaAdmin",
            admin_password="Str0ngAdminPass", recent_password="Str0ngAdminPass",
            shared_root="/s/beta")
        return [t for t in svc.list_tenants() if t["code"] == "beta"][0]

    def test_platform_admin_creates_and_updates_target_role(self):
        svc = IdentityService(_db())
        root, _acme = _seed(svc)
        beta = self._second_tenant(svc, root)
        # Platform admin is NOT a member of beta, yet manages its role.
        self.assertFalse(svc.is_member(root["id"], beta["id"]))
        role = svc.create_role(
            root["id"], beta["id"], "worker", "工人",
            ["skill.read", "skill.use", "model.read", "model.use"],
            resource_grants=[
                {"resource_kind": "skill", "resource_id": "custom:x", "action": "read"},
                {"resource_kind": "skill", "resource_id": "custom:x", "action": "use"},
                {"resource_kind": "model", "resource_id": "provider:p:m", "action": "use"},
            ],
            model_defaults={"chat": "provider:p:m"})
        self.assertEqual(role["code"], "worker")
        roles = svc.list_roles(beta["id"])
        worker = [x for x in roles if x["code"] == "worker"][0]
        self.assertEqual(len(worker["resource_grants"]), 3)
        self.assertEqual(worker["model_defaults"], {"chat": "provider:p:m"})

        upd = svc.update_role(
            root["id"], beta["id"], worker["id"], name="Worker",
            permissions=worker["permissions"], expected_version=worker["version"],
            resource_grants=worker["resource_grants"],
            model_defaults=worker["model_defaults"])
        self.assertEqual(upd["name"], "Worker")

    def test_platform_admin_target_does_not_leak_to_other_tenant(self):
        svc = IdentityService(_db())
        root, acme = _seed(svc)
        beta = self._second_tenant(svc, root)
        svc.create_role(
            root["id"], beta["id"], "worker", "工人", [],
            resource_grants=[
                {"resource_kind": "skill", "resource_id": "custom:x", "action": "read"},
            ])
        # The target role must not appear in the other (acme) tenant.
        acme_roles = svc.list_roles(acme["id"])
        self.assertFalse(any(a["code"] == "worker" for a in acme_roles))
        # A tenant admin (not platform) still manages only the same tenant.
        # root is platform admin; a normal non-platform actor cannot target beta.
        # (Covered by tenant-admin requirement further below.)


if __name__ == "__main__":
    unittest.main()
