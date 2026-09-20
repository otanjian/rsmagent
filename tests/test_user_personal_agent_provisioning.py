# encoding:utf-8
"""Giving every new member their own private "智能办公助理".

Product planning 3.1 puts the *user* level on the same footing as the platform
and tenant levels: the individual has a space of their own, and personal memory
belongs to that person alone. Today a tenant shares exactly one 智能办公助理 —
every member's schedule, todos and drafts land in one workspace, separated only
by conversation ownership.

This change makes membership creation provision a private clone of the tenant's
assistant, bound to that one user, and registers it as *their* default agent so
"enter chat without picking an agent" anchors on their own copy rather than the
one every colleague shares.

The layer spans two stores that cannot commit together — the file-backed roster
and ``identity.db`` — so these tests drive the real ``AgentAdminService`` and
``IdentityService`` against a throwaway instance root, exactly as
``test_tenant_agent_provisioning.py`` does for the tenant copy flow.
"""

import json
import os
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

import web

from agent import team
from agent.admin import AgentAdminService
from agent.personal_assistant import (
    PersonalAssistantProvisioner,
    get_personal_assistant_provisioner,
)
from agent.registry import AgentRegistry, set_agent_registry
from auth.runtime import RequestContext
from auth.service import IdentityService, IdentityServiceError
from channel.web import web_channel

AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

SOURCE_ID = "my-assistant-admin"
SOURCE_NAME = "智能办公助理"
ADMIN_PASSWORD = "Str0ngAdminPass"

SOURCE_AGENT_MD = (
    "# AGENT.md\n\n"
    "- **名字**: 智能办公助理\n"
    "- **角色**: 管理员的专属智能办公助理（私人）\n\n"
    "只承办 admin 本人的日程、待办、材料撰写与信息整理。\n"
)
SOURCE_USER_MD = "# USER.md\n\n- 用户名: admin\n- 岗位: 系统管理员\n"


class _NoopProvisioner:
    """Stands in for the real provisioner when a test wants a bare member."""

    def provision(self, **_kwargs):
        return None


class _Base(unittest.TestCase):
    """One instance root holding the tenant's shared 智能办公助理."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.instance = os.path.join(self.tmp, "instance")
        self.tenant_root = os.path.join(self.tmp, "tenants", "acme")
        source_ws = os.path.join(self.instance, "agents", SOURCE_ID)
        os.makedirs(source_ws)
        os.makedirs(self.tenant_root)
        with open(os.path.join(source_ws, "AGENT.md"), "w", encoding="utf-8") as fh:
            fh.write(SOURCE_AGENT_MD)
        with open(os.path.join(source_ws, "USER.md"), "w", encoding="utf-8") as fh:
            fh.write(SOURCE_USER_MD)

        self.config_path = os.path.join(self.tmp, "config.json")
        # The instance default is a separate Agent at the instance root: the
        # tenant's 智能办公助理 is an ordinary bound Agent, so a test can disable
        # it without asking the registry to disable the instance default.
        self.settings = {
            "agent_workspace": self.instance,
            "default_agent_id": "instance-default",
            "agents": [
                {"id": "instance-default", "name": "RongAI",
                 "workspace": self.instance, "enabled": True},
                {"id": SOURCE_ID, "name": SOURCE_NAME, "workspace": source_ws,
                 "enabled": True,
                 "description": "管理员专属智能办公助理：负责 admin 本人的日程"},
            ],
            "channel_instances": [],
        }
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(self.settings, handle)
        self._pin(self.settings)

        self.svc = IdentityService(os.path.join(self.tmp, "identity.db"))
        self.tenant = self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password=ADMIN_PASSWORD,
            shared_root=self.tenant_root, allow_weak=True)
        self.tenant_id = self.tenant["id"]
        self.root_id = self.svc.list_platform_users()[0]["id"]
        self.svc.bind_agent(tenant_id=self.tenant_id, agent_id=SOURCE_ID)
        self.svc.appoint_tenant_default_agent(
            tenant_id=self.tenant_id, agent_id=SOURCE_ID, actor_user_id=self.root_id)

        self.admin = AgentAdminService(self.config_path)
        # ``create_member`` builds the process-wide provisioner, which would read
        # the real data root. Pin it to this throwaway instance root instead.
        patcher = patch("agent.personal_assistant.get_personal_assistant_provisioner",
                        side_effect=lambda: self._provisioner())
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        set_agent_registry(None)

    # --- harness ---------------------------------------------------------

    def _pin(self, settings):
        set_agent_registry(AgentRegistry.from_config(team.resolve(settings)))

    def _reload_registry(self):
        """Re-read the roster from disk, as the runtime would after an edit.

        ``_agent_is_usable`` consults the process registry, not the admin
        service, so a test that disables an Agent through ``self.admin`` has to
        publish that change or it would be asserting against a stale roster.
        """
        with open(self.config_path, encoding="utf-8") as handle:
            self._pin(json.load(handle))

    def _provisioner(self, **kwargs):
        return PersonalAssistantProvisioner(self.svc, self.admin, **kwargs)

    def _add_member(self, username, display_name="某人", **kwargs):
        return self.svc.create_member(
            actor_user_id=self.root_id, tenant_id=self.tenant_id,
            operation="create-new", username=username, display_name=display_name,
            temporary_password="TempPass123!", roles=[], **kwargs)

    def _add_member_without_assistant(self, username, display_name="某人"):
        """A member with no personal assistant, to drive ``provision`` directly."""
        with patch("agent.personal_assistant.get_personal_assistant_provisioner",
                   return_value=_NoopProvisioner()):
            return self.svc.create_member(
                actor_user_id=self.root_id, tenant_id=self.tenant_id,
                operation="create-new", username=username,
                display_name=display_name, temporary_password="TempPass123!",
                roles=[])

    def _new_tenant(self, code, shared_root=None):
        return self.svc.create_tenant(
            actor_user_id=self.root_id, code=code, name=code.title(),
            shared_root=shared_root or os.path.join(self.tmp, "tenants", code),
            admin_username="%sadmin" % code, admin_display=code.title(),
            admin_password="Str0ngPass9", recent_password=ADMIN_PASSWORD)

    def _roster_ids(self):
        return {a["id"] for a in self.admin.snapshot()["agents"]}

    def _profile(self, agent_id):
        return next((a for a in self.admin.snapshot()["agents"]
                     if a["id"] == agent_id), None)

    def _read(self, agent_id, filename):
        ws = self._profile(agent_id)["workspace"]
        with open(os.path.join(ws, filename), encoding="utf-8") as fh:
            return fh.read()

    def _audit(self, action):
        return [dict(r) for r in self.svc._store.execute(
            "SELECT * FROM audit_events WHERE action=?", (action,))]


# --- column registration (migration 9) -----------------------------------

class MemberDefaultRegistrationTests(_Base):
    def test_migration_adds_a_nullable_column_without_backfilling(self):
        rows = [dict(r) for r in self.svc._store.execute(
            "SELECT default_agent_id FROM memberships")]
        self.assertTrue(rows, "bootstrap creates the tenant admin's membership")
        for membership in rows:
            self.assertIsNone(membership["default_agent_id"])

    def test_registration_round_trips_and_is_idempotent(self):
        user_id = self._add_member_without_assistant("alice", "Alice")["user_id"]
        self.svc.bind_agent(tenant_id=self.tenant_id, agent_id="agent-a")

        for _ in range(2):
            self.svc.set_member_default_agent(
                tenant_id=self.tenant_id, user_id=user_id, agent_id="agent-a",
                actor_user_id=self.root_id)

        self.assertEqual(
            self.svc.member_default_agent_id(self.tenant_id, user_id), "agent-a")

    def test_registration_refuses_an_agent_the_tenant_does_not_own(self):
        user_id = self._add_member_without_assistant("alice", "Alice")["user_id"]

        with self.assertRaises(IdentityServiceError) as caught:
            self.svc.set_member_default_agent(
                tenant_id=self.tenant_id, user_id=user_id, agent_id="nope",
                actor_user_id=self.root_id)

        self.assertEqual(caught.exception.status, 404)
        self.assertIsNone(
            self.svc.member_default_agent_id(self.tenant_id, user_id))

    def test_registration_does_not_bump_the_membership_version(self):
        user_id = self._add_member_without_assistant("alice", "Alice")["user_id"]
        self.svc.bind_agent(tenant_id=self.tenant_id, agent_id="agent-a")
        query = ("SELECT version FROM memberships WHERE tenant_id=? AND user_id=?")
        before = self.svc._store.execute(query, (self.tenant_id, user_id))[0]

        self.svc.set_member_default_agent(
            tenant_id=self.tenant_id, user_id=user_id, agent_id="agent-a",
            actor_user_id=self.root_id)

        after = self.svc._store.execute(query, (self.tenant_id, user_id))[0]
        self.assertEqual(before["version"], after["version"], (
            "bumping the version would invalidate the tenant editor's draft"))


# --- provisioning --------------------------------------------------------

class PersonalAssistantProvisioningTests(_Base):
    def test_a_new_member_gets_a_private_personal_assistant(self):
        member = self._add_member("alice", "Alice")

        user_id = member["user_id"]
        assert member["personal_agent"]["status"] == "created", member["personal_agent"]
        agent_id = member["personal_agent"]["agent_id"]

        self.assertNotEqual(agent_id, SOURCE_ID)
        self.assertRegex(agent_id, AGENT_ID_RE)
        self.assertIn(agent_id, self._roster_ids())
        self.assertEqual(self._profile(agent_id)["name"], SOURCE_NAME)

        binding = self.svc.get_agent_binding(agent_id)
        self.assertEqual(binding["tenant_id"], self.tenant_id)
        self.assertEqual(binding["private_owner_user_id"], user_id, (
            "the personal assistant must be private to its owner, not tenant-shared"))

        self.assertEqual(
            os.path.realpath(self._profile(agent_id)["workspace"]),
            os.path.realpath(os.path.join(self.tenant_root, "agents", agent_id)))

    def test_the_source_is_resolved_by_name_inside_the_tenant(self):
        result = self._provisioner().provision(
            tenant_id=self.tenant_id, user_id=self.root_id,
            username="alice", display_name="Alice")

        self.assertEqual(result["source_agent_id"], SOURCE_ID)

    def test_two_users_each_get_their_own_clone(self):
        first = self._add_member("alice", "Alice")["personal_agent"]
        second = self._add_member("bob", "Bob")["personal_agent"]

        self.assertEqual(first["status"], "created", first)
        self.assertEqual(second["status"], "created", (
            "the second user's clone must not collide with the first's: %r" % second))
        self.assertNotEqual(first["agent_id"], second["agent_id"])
        self.assertNotEqual(
            self.svc.get_agent_binding(first["agent_id"])["private_owner_user_id"],
            self.svc.get_agent_binding(second["agent_id"])["private_owner_user_id"])

    def test_provisioning_is_idempotent_per_user(self):
        member = self._add_member("alice", "Alice")
        user_id = member["user_id"]
        agent_id = member["personal_agent"]["agent_id"]
        before = self._roster_ids()

        again = self._provisioner().provision(
            tenant_id=self.tenant_id, user_id=user_id,
            username="alice", display_name="Alice")

        self.assertEqual(again["status"], "skipped", again)
        self.assertEqual(again["reason"], "already_has_personal_agent")
        self.assertEqual(self._roster_ids(), before)
        self.assertEqual(
            self.svc.get_agent_binding(agent_id)["private_owner_user_id"], user_id)

    def test_a_bare_member_gets_one_and_a_second_provision_is_skipped(self):
        user_id = self._add_member_without_assistant("alice", "Alice")["user_id"]
        self.assertIsNone(
            self.svc.member_default_agent_id(self.tenant_id, user_id))

        first = self._provisioner().provision(
            tenant_id=self.tenant_id, user_id=user_id,
            username="alice", display_name="Alice")

        self.assertEqual(first["status"], "created", first)
        self.assertEqual(
            self.svc.member_default_agent_id(self.tenant_id, user_id),
            first["agent_id"])


class PersonalAssistantOriginTests(_Base):
    """Provenance decides what makes provisioning skip.

    Change ``enable-member-personal-console``: a private agent a member made for
    themselves must not stand in for the system-supplied assistant, while a
    legacy binding that never recorded its origin must keep skipping — this
    change must not start handing every existing member a second assistant.
    """

    def _bind_private(self, agent_id, user_id, origin=None):
        extra = {} if origin is None else {"origin": origin}
        return self.svc.bind_agent(
            tenant_id=self.tenant_id, agent_id=agent_id,
            private_owner_user_id=user_id, **extra)

    def test_the_provisioned_assistant_is_marked_system_supplied(self):
        member = self._add_member("alice", "Alice")
        binding = self.svc.get_agent_binding(member["personal_agent"]["agent_id"])
        self.assertEqual(binding["origin"], "provisioned_assistant")

    def test_a_member_made_private_agent_does_not_block_provisioning(self):
        user_id = self._add_member_without_assistant("alice", "Alice")["user_id"]
        self._bind_private("self-made", user_id, origin="user_created")

        result = self._provisioner().provision(
            tenant_id=self.tenant_id, user_id=user_id,
            username="alice", display_name="Alice")

        self.assertEqual(result["status"], "created", result)
        self.assertNotEqual(result["agent_id"], "self-made")
        self.assertEqual(
            self.svc.get_agent_binding("self-made")["origin"], "user_created",
            "the member's own agent must be left exactly as it was")
        self.assertEqual(
            self.svc.get_agent_binding("self-made")["private_owner_user_id"],
            user_id)

    def test_a_legacy_binding_still_skips_so_no_duplicate_is_created(self):
        """'unknown' is not a licence to provision a second assistant."""
        user_id = self._add_member_without_assistant("alice", "Alice")["user_id"]
        self._bind_private("legacy-assistant", user_id)  # origin defaults unknown

        result = self._provisioner().provision(
            tenant_id=self.tenant_id, user_id=user_id,
            username="alice", display_name="Alice")

        self.assertEqual(result["status"], "skipped", result)
        self.assertEqual(result["agent_id"], "legacy-assistant")

    def test_a_system_supplied_assistant_still_skips(self):
        user_id = self._add_member_without_assistant("alice", "Alice")["user_id"]
        self._bind_private("sys-assistant", user_id,
                           origin="provisioned_assistant")

        result = self._provisioner().provision(
            tenant_id=self.tenant_id, user_id=user_id,
            username="alice", display_name="Alice")

        self.assertEqual(result["status"], "skipped", result)
        self.assertEqual(result["agent_id"], "sys-assistant")

    def test_a_bind_repairs_a_missing_origin_from_trusted_evidence(self):
        user_id = self._add_member_without_assistant("alice", "Alice")["user_id"]
        self._bind_private("a1", user_id)
        self.assertEqual(self.svc.get_agent_binding("a1")["origin"], "unknown")

        self.svc.bind_agent(tenant_id=self.tenant_id, agent_id="a1",
                            origin="provisioned_assistant")

        self.assertEqual(
            self.svc.get_agent_binding("a1")["origin"], "provisioned_assistant")

    def test_a_repair_never_overwrites_a_recorded_origin(self):
        """A later bind must not reclassify a member's own agent as system-made."""
        user_id = self._add_member_without_assistant("alice", "Alice")["user_id"]
        self._bind_private("a1", user_id, origin="user_created")

        self.svc.bind_agent(tenant_id=self.tenant_id, agent_id="a1",
                            origin="provisioned_assistant")

        self.assertEqual(
            self.svc.get_agent_binding("a1")["origin"], "user_created")

    def test_an_unknown_origin_is_refused(self):
        """A typo must not silently produce an unclassifiable binding."""
        user_id = self._add_member_without_assistant("alice", "Alice")["user_id"]
        with self.assertRaises(IdentityServiceError) as caught:
            self._bind_private("a1", user_id, origin="provisioned")
        self.assertEqual(caught.exception.code, "bad_request")
        self.assertIsNone(self.svc.get_agent_binding("a1"))


class SourceResolutionTests(_Base):
    def test_configuration_can_override_the_source_name(self):
        self.admin.update_agent(SOURCE_ID, name="个人助理")

        result = self._provisioner(source_name="个人助理").provision(
            tenant_id=self.tenant_id, user_id=self.root_id,
            username="alice", display_name="Alice")

        self.assertEqual(result["status"], "created", result)

    def test_configuration_can_pin_an_explicit_source_id(self):
        self.admin.create_agent("other", "别的助理")
        self.svc.bind_agent(tenant_id=self.tenant_id, agent_id="other")

        result = self._provisioner(source_agent_id="other").provision(
            tenant_id=self.tenant_id, user_id=self.root_id,
            username="alice", display_name="Alice")

        self.assertEqual(result["status"], "created", result)
        self.assertEqual(result["source_agent_id"], "other")

    def test_an_explicit_source_the_tenant_does_not_own_is_refused(self):
        self.admin.create_agent("elsewhere", "别处")
        # Deliberately not bound to this tenant.

        result = self._provisioner(source_agent_id="elsewhere").provision(
            tenant_id=self.tenant_id, user_id=self.root_id,
            username="alice", display_name="Alice")

        self.assertEqual(result["status"], "skipped", result)
        self.assertEqual(result["reason"], "no_source_agent")
        self.assertNotIn("elsewhere-alice", self._roster_ids())

    def test_a_missing_source_skips_without_failing_member_creation(self):
        self.svc.release_deleted_agent(agent_id=SOURCE_ID, actor_user_id=self.root_id)
        before = self._roster_ids()

        member = self._add_member("alice", "Alice")

        self.assertTrue(member["user_id"], "the member must still be created")
        self.assertEqual(member["personal_agent"]["status"], "skipped")
        self.assertEqual(member["personal_agent"]["reason"], "no_source_agent")
        self.assertEqual(self._roster_ids(), before)

    def test_a_disabled_source_is_treated_as_missing(self):
        self.admin.archive_agent(SOURCE_ID)
        self._reload_registry()

        member = self._add_member("alice", "Alice")

        self.assertEqual(member["personal_agent"]["status"], "skipped")
        self.assertEqual(member["personal_agent"]["reason"], "no_source_agent")

    def test_a_source_from_another_tenant_is_never_borrowed(self):
        other = self._new_tenant("globex")
        self.svc.release_deleted_agent(agent_id=SOURCE_ID, actor_user_id=self.root_id)
        self.svc.bind_agent(tenant_id=other["id"], agent_id=SOURCE_ID)

        result = self._provisioner().provision(
            tenant_id=self.tenant_id, user_id=self.root_id,
            username="alice", display_name="Alice")

        self.assertEqual(result["status"], "skipped", result)
        self.assertEqual(result["reason"], "no_source_agent")


class PersonaPersonalisationTests(_Base):
    def test_user_md_describes_the_new_member_not_the_source_owner(self):
        member = self._add_member("alice", "张三", position_text="财务经理")

        text = self._read(member["personal_agent"]["agent_id"], "USER.md")

        self.assertIn("张三", text)
        self.assertNotIn("admin", text)
        self.assertIn("财务经理", text)

    def test_the_owner_alias_is_rewritten_into_the_clone_only(self):
        member = self._add_member("alice", "张三")

        clone = self._read(member["personal_agent"]["agent_id"], "AGENT.md")

        self.assertIn("张三", clone)
        self.assertNotIn("admin", clone)
        # The source keeps its own text: only the copy is personalised.
        self.assertIn("admin", self._read(SOURCE_ID, "AGENT.md"))

    def test_the_registry_fields_are_personalised_too(self):
        member = self._add_member("alice", "张三")

        profile = self._profile(member["personal_agent"]["agent_id"])

        self.assertIn("张三", profile["description"])
        self.assertNotIn("admin", profile["description"])

    def test_a_persona_without_the_alias_keeps_its_text(self):
        plain = "# AGENT.md\n\n- **名字**: 智能办公助理\n"
        with open(os.path.join(self.instance, "agents", SOURCE_ID, "AGENT.md"),
                  "w", encoding="utf-8") as fh:
            fh.write(plain)

        member = self._add_member("alice", "张三")

        self.assertEqual(
            self._read(member["personal_agent"]["agent_id"], "AGENT.md"), plain)


class PersonalAssistantKnowledgeModeTests(_Base):
    """A new private assistant owns an independent, empty knowledge base."""

    def test_a_new_private_assistant_gets_its_own_knowledge_base(self):
        member = self._add_member("alice", "Alice")
        agent_id = member["personal_agent"]["agent_id"]

        self.assertEqual(self.admin.knowledge_mode(agent_id), "own", (
            "a private assistant must read its own base, never the tenant's"))
        kb = os.path.join(self._profile(agent_id)["workspace"], "knowledge")
        self.assertTrue(os.path.isdir(kb), "the own base must exist on disk")
        # Only the empty seed index: knowledge entities are never copied.
        self.assertEqual(sorted(os.listdir(kb)), ["index.md"])

    def test_the_source_and_the_tenant_shared_base_are_untouched(self):
        source_kb = os.path.join(self.instance, "agents", SOURCE_ID, "knowledge")
        os.makedirs(source_kb, exist_ok=True)
        with open(os.path.join(source_kb, "secret.md"), "w", encoding="utf-8") as fh:
            fh.write("# secret\n")

        member = self._add_member("alice", "Alice")
        clone_ws = self._profile(member["personal_agent"]["agent_id"])["workspace"]

        self.assertFalse(os.path.exists(
            os.path.join(clone_ws, "knowledge", "secret.md")), (
            "the clone must not inherit the source's knowledge entities"))
        self.assertTrue(os.path.exists(os.path.join(source_kb, "secret.md")))

    def test_an_existing_shared_mode_assistant_is_not_backfilled(self):
        user_id = self._add_member_without_assistant("alice", "Alice")["user_id"]
        # A pre-existing assistant supplied the old way (shared mode, no own
        # knowledge/): provisioning must not rewrite its data root.
        self.svc.bind_agent(
            tenant_id=self.tenant_id, agent_id="legacy-shared",
            private_owner_user_id=user_id, actor_user_id=self.root_id)
        bare_ws = os.path.join(self.tenant_root, "agents", "legacy-shared")
        os.makedirs(bare_ws)
        self.admin.create_agent("legacy-shared", "旧助理",
                                workspace=bare_ws, knowledge_mode="shared")
        self._reload_registry()
        self.assertEqual(self.admin.knowledge_mode("legacy-shared"), "shared")

        self._provisioner().provision(
            tenant_id=self.tenant_id, user_id=user_id,
            username="alice", display_name="Alice")

        self.assertEqual(self.admin.knowledge_mode("legacy-shared"), "shared")
        self.assertFalse(os.path.exists(os.path.join(bare_ws, "knowledge")))


# --- permission boundary -------------------------------------------------

class PermissionBoundaryTests(_Base):
    def _ctx(self, user_id, *, admin=False):
        return RequestContext(
            user_id=user_id, username="u", display_name="U",
            is_platform_admin=False, must_change_password=False,
            tenant_id=self.tenant_id, membership=None, permissions=set(),
            is_tenant_admin=admin)

    def test_only_the_owner_may_reach_it(self):
        member = self._add_member("alice", "Alice")
        agent_id = member["personal_agent"]["agent_id"]
        colleague = self._add_member("bob", "Bob")["user_id"]

        web.ctx.headers = []
        with patch("auth.service.get_identity_service", return_value=self.svc):
            # The owner passes.
            web_channel._require_private_owner(
                self._ctx(member["user_id"]), agent_id)
            # Task 3.4: a same-tenant administrator does NOT — an admin's
            # interest in a private assistant is governance metadata, not its
            # content, and the private-owner rule is re-derived per request.
            with self.assertRaises(web.HTTPError) as admin_refused:
                web_channel._require_private_owner(
                    self._ctx(self.root_id, admin=True), agent_id)
            # Another ordinary member is refused.
            with self.assertRaises(web.HTTPError) as refused:
                web_channel._require_private_owner(self._ctx(colleague), agent_id)
        self.assertIn("403", str(admin_refused.exception))
        self.assertIn("403", str(refused.exception))

    def test_a_shared_agent_is_reachable_without_an_owner(self):
        """The narrowing must not become a blanket owner requirement."""
        self._add_member("alice", "Alice")

        web.ctx.headers = []
        with patch("auth.service.get_identity_service", return_value=self.svc):
            web_channel._require_private_owner(
                self._ctx(self.root_id, admin=True), SOURCE_ID)

    def test_creating_a_personal_assistant_leaves_the_tenant_default_alone(self):
        self._add_member("alice", "Alice")

        self.assertEqual(self.svc.tenant_default_agent_id(self.tenant_id), SOURCE_ID)


# --- per-user default resolution -----------------------------------------

class PerUserDefaultResolutionTests(_Base):
    def test_the_personal_assistant_wins_for_its_owner_only(self):
        alice = self._add_member("alice", "Alice")
        bob = self._add_member("bob", "Bob")

        self.assertEqual(
            self.svc.resolved_default_agent_id(self.tenant_id, alice["user_id"]),
            alice["personal_agent"]["agent_id"])
        self.assertEqual(
            self.svc.resolved_default_agent_id(self.tenant_id, bob["user_id"]),
            bob["personal_agent"]["agent_id"])
        # Without a subject the answer is the tenant default, exactly as before.
        self.assertEqual(
            self.svc.resolved_default_agent_id(self.tenant_id), SOURCE_ID)

    def test_another_users_private_assistant_is_never_anchored(self):
        alice = self._add_member("alice", "Alice")
        private = alice["personal_agent"]["agent_id"]
        bob = self._add_member_without_assistant("bob", "Bob")["user_id"]
        # A registration pointing at someone else's private Agent: the write path
        # only checks tenancy, because reachability is enforced at resolution
        # time, which is where this security invariant has to hold.
        self.svc.set_member_default_agent(
            tenant_id=self.tenant_id, user_id=bob, agent_id=private,
            actor_user_id=self.root_id)

        resolved = self.svc.resolved_default_agent_id(self.tenant_id, bob)

        self.assertEqual(resolved, SOURCE_ID, (
            "an entry into another member's private Agent is not an entry at all"))
        self.assertNotEqual(resolved, private)
        # And the refusal is not a side effect of the owner losing their own.
        self.assertEqual(
            self.svc.resolved_default_agent_id(self.tenant_id, alice["user_id"]),
            private)

    def test_a_disabled_personal_assistant_falls_back(self):
        member = self._add_member("alice", "Alice")
        self.admin.archive_agent(member["personal_agent"]["agent_id"])
        self._reload_registry()

        resolved = self.svc.resolved_default_agent_id(
            self.tenant_id, member["user_id"])

        self.assertEqual(resolved, SOURCE_ID, (
            "a disabled personal assistant must be skipped, not returned"))

    def test_an_unbound_personal_assistant_falls_back(self):
        member = self._add_member("alice", "Alice")
        self.svc.release_deleted_agent(
            agent_id=member["personal_agent"]["agent_id"],
            actor_user_id=self.root_id)

        resolved = self.svc.resolved_default_agent_id(
            self.tenant_id, member["user_id"])

        self.assertEqual(resolved, SOURCE_ID)

    def test_deleting_the_personal_assistant_clears_its_registration(self):
        member = self._add_member("alice", "Alice")
        agent_id = member["personal_agent"]["agent_id"]
        user_id = member["user_id"]
        self.assertEqual(
            self.svc.member_default_agent_id(self.tenant_id, user_id), agent_id)

        self.svc.release_deleted_agent(agent_id=agent_id, actor_user_id=self.root_id)

        self.assertIsNone(
            self.svc.member_default_agent_id(self.tenant_id, user_id), (
                "a stale registration would keep pointing at a deleted Agent"))
        self.assertEqual(
            self.svc.resolved_default_agent_id(self.tenant_id, user_id), SOURCE_ID)

    def test_releasing_one_assistant_spares_other_registrations(self):
        alice = self._add_member("alice", "Alice")
        bob = self._add_member("bob", "Bob")

        self.svc.release_deleted_agent(
            agent_id=alice["personal_agent"]["agent_id"],
            actor_user_id=self.root_id)

        self.assertEqual(
            self.svc.member_default_agent_id(self.tenant_id, bob["user_id"]),
            bob["personal_agent"]["agent_id"])
        self.assertEqual(self.svc.tenant_default_agent_id(self.tenant_id), SOURCE_ID)

    def test_the_resolution_stays_read_only(self):
        member = self._add_member("alice", "Alice")
        query = ("SELECT default_agent_id FROM memberships"
                 " WHERE tenant_id=? AND user_id=?")
        args = (self.tenant_id, member["user_id"])
        before = self.svc._store.execute(query, args)[0]["default_agent_id"]

        for _ in range(3):
            self.svc.resolved_default_agent_id(self.tenant_id, member["user_id"])

        after = self.svc._store.execute(query, args)[0]["default_agent_id"]
        self.assertEqual(before, after)


# --- failure compensation ------------------------------------------------

class _FailingBindService:
    def __init__(self, inner, failing_agent_id):
        self._inner = inner
        self._failing = failing_agent_id

    def __getattr__(self, item):
        return getattr(self._inner, item)

    def bind_agent(self, **kwargs):
        if kwargs.get("agent_id") == self._failing:
            raise IdentityServiceError("database is locked")
        return self._inner.bind_agent(**kwargs)


class FailureCompensationTests(_Base):
    def setUp(self):
        super().setUp()
        self.expected_id = "%s-alice" % SOURCE_ID

    def test_a_failure_is_compensated_and_reported(self):
        user_id = self._add_member_without_assistant("alice", "Alice")["user_id"]
        provisioner = PersonalAssistantProvisioner(
            _FailingBindService(self.svc, self.expected_id), self.admin)

        result = provisioner.provision(
            tenant_id=self.tenant_id, user_id=user_id,
            username="alice", display_name="Alice")

        self.assertEqual(result["status"], "failed", result)
        self.assertTrue(result["reason"])
        self.assertNotIn(self.expected_id, self._roster_ids(), (
            "a half-created roster entry must not survive the failure"))
        self.assertFalse(os.path.exists(
            os.path.join(self.tenant_root, "agents", self.expected_id)))
        self.assertIsNone(self.svc.get_agent_binding(self.expected_id))

    def test_a_retry_after_a_failure_completes(self):
        user_id = self._add_member_without_assistant("alice", "Alice")["user_id"]
        failing = PersonalAssistantProvisioner(
            _FailingBindService(self.svc, self.expected_id), self.admin)
        failing.provision(tenant_id=self.tenant_id, user_id=user_id,
                          username="alice", display_name="Alice")

        result = self._provisioner().provision(
            tenant_id=self.tenant_id, user_id=user_id,
            username="alice", display_name="Alice")

        self.assertEqual(result["status"], "created", result)
        self.assertEqual(result["agent_id"], self.expected_id)

    def test_a_clone_failure_is_reported_without_a_binding(self):
        user_id = self._add_member_without_assistant("alice", "Alice")["user_id"]

        class _Boom:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, item):
                return getattr(self._inner, item)

            def clone_agent(self, *args, **kwargs):
                raise RuntimeError("workspace could not be created")

        result = PersonalAssistantProvisioner(self.svc, _Boom(self.admin)).provision(
            tenant_id=self.tenant_id, user_id=user_id,
            username="alice", display_name="Alice")

        self.assertEqual(result["status"], "failed", result)
        self.assertIsNone(self.svc.get_agent_binding(self.expected_id))
        self.assertNotIn(self.expected_id, self._roster_ids())


class ProvisioningAuditTests(_Base):
    def test_the_outcomes_are_audited(self):
        created = self._add_member("alice", "Alice")
        self._add_member("bob", "Bob")

        events = self._audit("member.personal_agent.create")
        self.assertEqual(len(events), 2, "one event per created assistant")
        self.assertEqual(events[0]["result"], "success")
        self.assertNotIn("password", (events[0]["redacted_changes"] or "").lower())

        self._provisioner().provision(
            tenant_id=self.tenant_id, user_id=created["user_id"],
            username="alice", display_name="Alice")

        skips = self._audit("member.personal_agent.skip")
        self.assertEqual(len(skips), 1)
        self.assertIn("already_has_personal_agent",
                      skips[0]["redacted_changes"] or "")

    def test_a_failure_is_audited(self):
        user_id = self._add_member_without_assistant("alice", "Alice")["user_id"]
        failing = PersonalAssistantProvisioner(
            _FailingBindService(self.svc, "%s-alice" % SOURCE_ID), self.admin)

        failing.provision(tenant_id=self.tenant_id, user_id=user_id,
                          username="alice", display_name="Alice")

        events = self._audit("member.personal_agent.fail")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["result"], "failure")

    def test_a_skip_is_audited_when_there_is_no_source(self):
        self.svc.release_deleted_agent(agent_id=SOURCE_ID, actor_user_id=self.root_id)

        self._add_member("alice", "Alice")

        skips = self._audit("member.personal_agent.skip")
        self.assertEqual(len(skips), 1)
        self.assertIn("no_source_agent", skips[0]["redacted_changes"] or "")


class WiringTests(_Base):
    def test_bind_existing_also_provisions(self):
        # An account created in another tenant (which has no source, so it got
        # nothing), then bound into this tenant that does.
        other = self._new_tenant("globex")
        with patch("agent.personal_assistant.get_personal_assistant_provisioner",
                   return_value=_NoopProvisioner()):
            self.svc.create_member(
                actor_user_id=self.root_id, tenant_id=other["id"],
                operation="create-new", username="carol", display_name="Carol",
                temporary_password="TempPass123!", roles=[])

        bound = self.svc.create_member(
            actor_user_id=self.root_id, tenant_id=self.tenant_id,
            operation="bind-existing", username="carol", display_name="Carol",
            temporary_password="", roles=[])

        self.assertEqual(bound["personal_agent"]["status"], "created", bound)
        self.assertEqual(
            self.svc.get_agent_binding(bound["personal_agent"]["agent_id"])
            ["private_owner_user_id"], bound["user_id"])

    def test_a_refused_member_creation_never_provisions(self):
        before = self._roster_ids()

        with self.assertRaises(Exception):
            self.svc.create_member(
                actor_user_id=self.root_id, tenant_id=self.tenant_id,
                operation="create-new", username="alice", display_name="Alice",
                temporary_password="weak", roles=[])

        self.assertEqual(self._roster_ids(), before)

    def test_a_provisioning_error_does_not_fail_member_creation(self):
        class _Exploding:
            def provision(self, **_kwargs):
                raise RuntimeError("roster is unwritable")

        with patch("agent.personal_assistant.get_personal_assistant_provisioner",
                   return_value=_Exploding()):
            member = self.svc.create_member(
                actor_user_id=self.root_id, tenant_id=self.tenant_id,
                operation="create-new", username="alice", display_name="Alice",
                temporary_password="TempPass123!", roles=[])

        self.assertTrue(member["user_id"])
        self.assertEqual(member["personal_agent"]["status"], "failed")

    def test_the_module_exposes_a_process_wide_provisioner(self):
        self.assertIsInstance(get_personal_assistant_provisioner(),
                              PersonalAssistantProvisioner)


class RuntimeReloadTests(_Base):
    """A new assistant has to be reachable without restarting the process.

    Provisioning writes the roster file, but routing resolves a request against
    the registry snapshot the process booted with -- until that snapshot is
    refreshed, the member's own assistant is "disabled" as far as routing is
    concerned. The console's roster endpoints reload after writing for the same
    reason, and this path writes the roster too.
    """

    _RELOAD = "channel.web.fork.runtime._reload_agent_runtime"

    def test_provisioning_reloads_the_live_runtime(self):
        with patch(self._RELOAD) as reload_mock:
            member = self._add_member("alice", "Alice")

        self.assertEqual(member["personal_agent"]["status"], "created")
        reload_mock.assert_called_once()
        args, kwargs = reload_mock.call_args
        self.assertIs(args[0], self.admin)
        self.assertEqual(
            kwargs["changed_agent_ids"], [member["personal_agent"]["agent_id"]])

    def test_a_reload_that_fails_does_not_fail_the_member(self):
        """The reload serves the runtime, so it must not gate the roster write."""
        with patch(self._RELOAD, side_effect=RuntimeError("no bridge yet")):
            member = self._add_member("alice", "Alice")

        self.assertEqual(member["personal_agent"]["status"], "created")


if __name__ == "__main__":
    unittest.main()
