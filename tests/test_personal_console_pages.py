# encoding:utf-8
"""The member's own console surface as the server projects it (task 3.2).

``member-personal-console`` asks for an *authoritative* page projection: the page
may be read, its configuration may be written, and its runtime consumer may be
open — three separate facts. Collapsing them is how a console ends up promising
a member that they can chat through a channel whose inbound path has never been
accepted, or hiding an already-open catalog because its execution is closed.

Change ``unify-console-by-data-scope`` retires the ``personal.*`` pages from the
menu contract: a member reaches the *same* business pages a tenant administrator
uses, with the caller's object range deciding what is listed or writable. This
file keeps the guarantees those pages established, asserted on the page that now
carries each of them:

======================================  ==========================================
retired page                            carrying page / field
======================================  ==========================================
``personal.agents``                     ``admin.agents`` (``agent`` range; actions)
``personal.memory``                     ``admin.memory`` (registry ``states``)
``personal.tools`` / ``personal.skills``  ``admin.skills`` for the catalogue,
                                        ``resources.tools`` / ``resources.skills``
                                        for the per-kind access classes
``personal.channels``                   ``admin.channels`` — for a member that is
                                        ``scope='self'``, and the ``switches`` /
                                        ``states`` of the retired page travel with it
======================================  ==========================================

The retired ids are no longer *issued at all* (task 8.8): ``_SIGNED_CONSOLE_PAGES``
carries no ``personal.*`` id, so the projection cannot sign one and no caller can
be handed one. What survives is the *mapping* — ``canonical_menu_id`` still reads
``LEGACY_PERSONAL_MENU_MAP`` when grants are read, which is what keeps an
already-written ``nav:personal.agents`` grant resolving to ``nav:admin.agents``
(asserted in ``tests/test_console_menu_mapping.py``).

The pages are also no longer *personal*: every entry is scoped by the caller's
object range, so the pre-existing ``admin.*`` ids keep their original meaning and
a member's own surface never becomes a second way into tenant management.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from auth.policy import LEGACY_PERSONAL_MENU_MAP, personal_page_capabilities
from auth.service import (
    IdentityService,
    IdentityServiceError,
    _SIGNED_CONSOLE_PAGES,
    canonical_menu_id,
)

#: The five ids task 8.8 retired. They are kept as data here (rather than read
#: from a registry) precisely because nothing issues them any more: the tests
#: below assert both halves of the surviving contract — the ids are *gone* from
#: the projection, and the ``LEGACY_PERSONAL_MENU_MAP`` vocabulary that maps them
#: still resolves.
MEMBER_IDS = ("personal.agents", "personal.channels", "personal.memory",
              "personal.tools", "personal.skills")

#: The formal console page each retired personal page became (design D2 /
#: ``LEGACY_PERSONAL_MENU_MAP``). Many-to-one on purpose: the console has a
#: single 工具与技能 page, so both catalog pages land on ``admin.skills``.
CARRIER = {
    "personal.agents": "admin.agents",
    "personal.channels": "admin.channels",
    "personal.memory": "admin.memory",
    "personal.tools": "admin.skills",
    "personal.skills": "admin.skills",
}

#: The pages a member's own surface is reported on, and the range each one
#: answers with. ``agent`` is an *object* range ("what you own, plus what the
#: tenant shares with you"), not a page-level ``self``: the page is the same one
#: the administrator uses, and only the range moves.
MEMBER_PAGES = {
    "admin.agents": "agent",
    "admin.channels": "self",
    "admin.memory": "agent",
    "admin.skills": "agent",
}

#: Page-level action vocabulary (design D1: finite booleans, no inferred verbs).
ACTIONS = frozenset({"create", "update", "enable", "delete", "configure"})


def _db_path():
    return os.path.join(tempfile.mkdtemp(), "identity.db")


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.svc = IdentityService(_db_path())
        self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root="/s/acme")
        self.svc.change_password(
            self.svc.login("root", "Str0ngAdminPass").token,
            "Str0ngAdminPass", "Str0ngRootFinal")
        self.root = [u for u in self.svc.list_platform_users()
                     if u["username"] == "root"][0]
        self.tenant = self.svc.list_tenants()[0]["id"]
        self.member_token = self._member("member1", ["member"])
        self.admin_token = self._member("admin1", ["tenant_admin"])

    def _member(self, username, roles):
        with patch("agent.personal_assistant.get_personal_assistant_provisioner",
                   return_value=type("P", (), {"provision": lambda *a, **k: None})()):
            self.svc.create_member(
                actor_user_id=self.root["id"], tenant_id=self.tenant,
                operation="create-new", username=username, display_name=username,
                temporary_password="MemTempPass1", roles=roles)
        token = self.svc.login(username, "MemTempPass1").token
        self.svc.change_password(token, "MemTempPass1", "MemPassFinal1")
        return self.svc.login(username, "MemPassFinal1").token

    def _pages(self, token):
        return self.svc.context_for_tenant(token, self.tenant)["console_pages"]

    def _user_id(self, token):
        return self.svc.verify_session(token)["user"]["id"]

    def _role_id(self, code):
        return [r for r in self.svc.list_roles(self.tenant)
                if r["code"] == code][0]["id"]

    def _grant(self, role_code, kind, resource_id, action, grant_id="g1"):
        with self.svc._store.connect() as con:
            con.execute(
                "INSERT INTO role_resource_grants(id, tenant_id, role_id,"
                " resource_kind, resource_id, action) VALUES(?,?,?,?,?,?)",
                (grant_id, self.tenant, self._role_id(role_code), kind,
                 resource_id, action))
            con.commit()

    def _withhold_menu(self, *keep):
        """Replace the member role's menu set with *keep* (``nav:<page>`` ids).

        Only a role that carries *some* explicit menu grant is bound by the set;
        this is how a test reproduces "the navigation contract withholds these
        pages" without touching anything else the member holds.
        """
        keep = keep or ("nav:workbench.chat",)
        role_id = self._role_id("member")
        with self.svc._store.connect() as con:
            con.execute(
                "DELETE FROM role_resource_grants WHERE resource_kind='menu'"
                " AND role_id=?", (role_id,))
            for index, resource_id in enumerate(keep):
                con.execute(
                    "INSERT INTO role_resource_grants(id, tenant_id, role_id,"
                    " resource_kind, resource_id, action) VALUES(?,?,?,?,?,?)",
                    ("gate-%d" % index, self.tenant, role_id, "menu",
                     resource_id, "view"))
            con.commit()

    def _drop_permission(self, role_code, permission):
        role = [r for r in self.svc.list_roles(self.tenant)
                if r["code"] == role_code][0]
        kept = [p for p in role["permissions"] if p != permission]
        self.svc.update_role(self.root["id"], self.tenant, role["id"],
                             role["name"], kept, expected_version=role["version"])


class MemberSurfaceAvailabilityTests(_Fixture):
    def test_a_member_reads_their_own_surface_on_the_shared_pages(self):
        """The member's surface is the administrator's page, in the member's range.

        Nothing is "personal" about the *page* any more: it is the same
        ``admin.*`` id with a range derived from the caller's authority and the
        real object ownership, which is why the scope is not uniformly ``self``.
        """
        pages = self._pages(self.member_token)
        for pid, scope in sorted(MEMBER_PAGES.items()):
            entry = pages[pid]
            self.assertTrue(entry["available"], (pid, entry))
            self.assertTrue(entry["read_allowed"], (pid, entry))
            self.assertEqual(entry["scope"], scope, (pid, entry))
            self.assertEqual(entry["reason"], "", (pid, entry))

    def test_the_retired_personal_ids_are_no_longer_issued(self):
        """Retired, not merely withheld (task 8.8).

        While the ids were still signed, closing them meant a menu pass that had
        to keep saying ``menu_not_granted`` — a *navigation* answer for a page no
        host could render. Now the id is not signed at all, so there is no entry
        to explain: the projection answers with a plain absence, and the console
        forwards the old address before it ever asks (task 8.1).
        """
        pages = self._pages(self.member_token)
        for pid in MEMBER_IDS:
            self.assertNotIn(pid, pages, pid)
            self.assertNotIn(pid, _SIGNED_CONSOLE_PAGES, pid)

    def test_the_legacy_grants_still_canonicalize_to_the_carrier_pages(self):
        """The invariant 8.8 must not break: the *mapping* survives the ids."""
        for pid, target in sorted(CARRIER.items()):
            self.assertEqual(canonical_menu_id("nav:%s" % pid),
                             "nav:%s" % target, pid)
            self.assertEqual(LEGACY_PERSONAL_MENU_MAP[pid], target, pid)
            self.assertIn(target, _SIGNED_CONSOLE_PAGES, pid)

    def test_the_carrier_pages_carry_the_retired_pages_capabilities(self):
        """What each retired page *reported* is reported by its carrier.

        The switch block and the three states were the substantive content of the
        retired ``personal.channels``; they are asserted on ``admin.channels`` for
        a member rather than on the id that no longer exists, so the coverage the
        retired page had is not lost with it.
        """
        pages = self._pages(self.member_token)
        self.assertEqual(pages["admin.channels"]["switches"],
                         personal_page_capabilities("personal.channels"))
        self.assertEqual(pages["admin.channels"]["states"],
                         {"read": True, "config": True, "execution": False})
        self.assertEqual(pages["admin.agents"]["scope"], "agent")
        self.assertEqual(pages["admin.memory"]["scope"], "agent")


class MemberSurfaceStateTests(_Fixture):
    """Read / configure / execute are three answers, not one."""

    def test_the_carried_surfaces_report_three_separate_states(self):
        pages = self._pages(self.member_token)
        for pid in ("admin.channels", "admin.memory"):
            states = pages[pid].get("states")
            self.assertIsInstance(states, dict, pid)
            self.assertEqual(set(states), {"read", "config", "execution"}, pid)

    def test_a_member_may_read_and_configure_while_runtime_gaps_show(self):
        pages = self._pages(self.member_token)
        # Channels: the member's configuration slice is open, the inbound
        # execution slice is not (task 7.5 ships every type unaccepted), and the
        # shared page says so instead of promising a conversation. This is the
        # retired ``personal.channels`` answer, carried over field by field.
        channels = pages["admin.channels"]
        self.assertEqual(channels["scope"], "self")
        self.assertEqual(channels["states"],
                         {"read": True, "config": True, "execution": False})
        # The other surfaces are readable and offer the verbs their range carries.
        self.assertTrue(pages["admin.agents"]["available"])
        self.assertEqual(pages["admin.agents"]["actions"],
                         {"create": True, "update": True})
        self.assertTrue(pages["admin.memory"]["available"])
        self.assertTrue(pages["admin.memory"]["states"]["read"])
        self.assertTrue(pages["admin.skills"]["available"])

    def test_an_empty_catalog_is_readable_but_not_configurable(self):
        """The real state of "no tool or skill granted to me".

        The page has to stay readable (the member learns the catalog is empty)
        while reporting that there is nothing to configure or run: an ungranted
        catalog must not be dressed up as an open one. The catalogue page is one
        page for both kinds, so the per-kind answer is ``resources``.
        """
        pages = self._pages(self.member_token)
        self.assertTrue(pages["admin.skills"]["available"])
        for kind in ("tools", "skills"):
            self.assertEqual(pages["resources"][kind],
                             {"catalog": False, "config": False,
                              "execution": False}, kind)

    def test_a_resource_grant_opens_the_classes_it_names(self):
        """Each access class is opened by the grant that names it.

        The retired page collapsed "may use" into one ``config``/``execution``
        pair; the shared projection reports the catalogue classes per kind, and
        they stay separate refusals — ``execute`` on a tool is not a maintenance
        right, and a use grant is not a read grant.
        """
        self._grant("member", "tool", "builtin:alpha", "execute")
        tools = self._pages(self.member_token)["resources"]["tools"]
        self.assertEqual(tools, {"catalog": False, "config": False,
                                 "execution": True})

        self._grant("member", "tool", "builtin:alpha", "configure", grant_id="g2")
        tools = self._pages(self.member_token)["resources"]["tools"]
        self.assertEqual(tools, {"catalog": False, "config": True,
                                 "execution": True})

    def test_a_read_grant_alone_does_not_open_configuration(self):
        """Reading the catalog is not using the resource.

        ``read`` on a tool says the member may see it; ``configure``/``execute``
        are separate grants. The page must keep the difference, or a read-only
        allocation would look like a working configuration.
        """
        self._grant("member", "tool", "builtin:alpha", "read")
        states = self._pages(self.member_token)["resources"]["tools"]
        self.assertTrue(states["catalog"])
        self.assertFalse(states["config"])
        self.assertFalse(states["execution"])

    def test_personal_channel_execution_follows_the_accepted_types(self):
        """Execution has two halves: the per-type acceptance *and* the master switch.

        ``PERSONAL_RUNTIME_ACCEPTED_TYPES`` alone is not enough (task 9.1): the
        deployment-wide ``personal_channel_runtime`` switch ships off because no
        type has a recorded real end-to-end acceptance yet, so a rollback is one
        configuration change. The negative control lives in
        ``test_personal_console_acceptance``.
        """
        import config as config_module
        import channel.channel_instances as channel_instances

        switches = dict(config_module.conf() or {})
        switches["personal_channel_runtime"] = True
        with patch.object(channel_instances, "PERSONAL_RUNTIME_ACCEPTED_TYPES",
                          frozenset({"feishu"})), \
                patch.object(config_module, "conf", return_value=switches):
            states = self._pages(self.member_token)["admin.channels"]["states"]
        self.assertTrue(states["execution"])

    def test_a_tenant_that_disabled_personal_agents_loses_creation(self):
        """The tenant's private-Agent policy is a write-path refusal.

        The retired ``personal.agents`` page could report the policy as a
        ``states.config``/``actions.create`` pair because the page was the
        member's alone. The shared 智能体管理 page is one page for both roles, so
        it cannot report a *member-only* policy as a page flag: it stays readable
        (an Agent the member already holds remains listable and maintainable) and
        the refusal is what the create path itself answers, before any binding,
        credential or workspace is written.
        """
        self.svc.set_private_agent_policy(
            actor_user_id=self.root["id"], tenant_id=self.tenant,
            personal_enabled=False)
        entry = self._pages(self.member_token)["admin.agents"]
        self.assertTrue(entry["available"], "existing agents stay readable")
        self.assertTrue(entry["read_allowed"])

        member_id = self._user_id(self.member_token)
        with self.assertRaises(IdentityServiceError) as caught:
            self.svc.bind_private_agent_with_quota(
                tenant_id=self.tenant, agent_id="a-new-private",
                user_id=member_id, origin="user_created",
                actor_user_id=member_id)
        self.assertEqual(caught.exception.code, "forbidden")
        self.assertIsNone(self.svc.get_agent_binding("a-new-private"))


class MemberSurfaceActionTests(_Fixture):
    """The page's verbs are finite and page-specific (design D1)."""

    def test_actions_are_a_subset_of_the_finite_vocabulary(self):
        pages = self._pages(self.member_token)
        for pid in sorted(MEMBER_PAGES):
            actions = pages[pid].get("actions")
            self.assertIsInstance(actions, dict, pid)
            self.assertTrue(set(actions) <= ACTIONS, (pid, actions))
            for verb, allowed in actions.items():
                self.assertIsInstance(allowed, bool, (pid, verb))

    def test_the_owned_surfaces_offer_create_and_maintenance(self):
        pages = self._pages(self.member_token)
        self.assertEqual(pages["admin.agents"]["actions"],
                         {"create": True, "update": True})
        channels = pages["admin.channels"]["actions"]
        self.assertEqual(channels, {"create": True, "update": True})
        self.assertNotIn("delete", channels,
                         "the personal channel API revokes and unlinks, it does not delete")

    def test_the_memory_page_invents_no_verbs(self):
        """记忆管理 serves the member's own memory and fabricates no verb.

        The retired ``personal.memory`` page advertised ``update``/``delete`` and
        deliberately not ``create`` (a member edits what exists, they do not
        invent memories). The shared page reports an empty page-level vocabulary;
        the editing itself stays on the memory service, which re-checks the
        target, the version and the write switch per request (see
        ``tests/test_memory_console_scope.py`` and the personal-memory switch
        tests) — the one thing that must not happen is the page inventing a
        ``create`` its write path would refuse.
        """
        entry = self._pages(self.member_token)["admin.memory"]
        self.assertTrue(entry["available"])
        self.assertEqual(entry["actions"], {},
                         "记忆管理 advertises no page-level verb")
        self.assertNotIn("create", entry["actions"])
        self.assertNotIn("enable", entry["actions"],
                         "memory has no enable/disable state")

    def test_a_catalog_page_opens_only_the_kind_that_was_granted(self):
        pages = self._pages(self.member_token)
        for kind in ("tools", "skills"):
            self.assertFalse(pages["resources"][kind]["execution"], kind)
        self._grant("member", "skill", "builtin:writer", "use")
        pages = self._pages(self.member_token)
        self.assertTrue(pages["resources"]["skills"]["execution"])
        self.assertFalse(pages["resources"]["tools"]["execution"],
                         "a skill grant must not open the tool catalogue")


class MemberSurfaceDenialTests(_Fixture):
    def test_a_missing_functional_permission_denies_the_page(self):
        """A dropped read permission denies the shared page, and the reason says so.

        The menu pass must not overwrite the specific cause (``no_permission``)
        with ``menu_not_granted``: the member's grants changed, the deployment
        did not. ``menu_denied`` is still recorded — it is a separate fact, read
        by the console's entry gate, and forbidding it would have hidden the
        reason the payload actually reports.
        """
        self._drop_permission("member", "memory.read")
        self._withhold_menu()
        entry = self._pages(self.member_token)["admin.memory"]
        self.assertFalse(entry["read_allowed"])
        self.assertFalse(entry["available"])
        self.assertEqual(entry["reason"], "no_permission")
        self.assertEqual(entry["actions"], {})
        self.assertEqual(entry["states"],
                         {"read": False, "config": False, "execution": False})
        self.assertTrue(entry.get("menu_denied"),
                        "the withheld grant is a separate fact from the reason")

    def test_a_withheld_menu_grant_clears_the_actions_and_states(self):
        """A page the menu withholds must not leak its capability flags.

        The client is told to hide the entry; if the same payload still said
        "create: true, config: true", a tampered client could render a usable
        page from the very response that denied it. The invariant is asserted
        over every page the response denies, which now includes the shared pages
        the member's menu actually decides.
        """
        self._withhold_menu()
        pages = self._pages(self.member_token)
        for pid in sorted(MEMBER_PAGES):
            entry = pages[pid]
            self.assertTrue(entry.get("menu_denied"), pid)
            self.assertFalse(entry["available"], pid)
            self.assertEqual(entry["reason"], "menu_not_granted", pid)
            self.assertEqual(entry["actions"], {}, pid)
        for pid, entry in pages.items():
            if not isinstance(entry, dict) or not entry.get("menu_denied"):
                continue
            self.assertEqual(entry["actions"], {}, pid)
            if "states" in entry:
                self.assertEqual(entry["states"],
                                 {"read": False, "config": False, "execution": False},
                                 pid)

    def test_no_identity_is_handed_a_retired_page_even_under_platform_all(self):
        """Nobody gets a retired id — not even platform ``all`` (task 8.8).

        The compat path this test used to cover ("a caller whose roles carry no
        explicit menu grant is not bound by the menu set") is still exercised —
        it is asserted on the *carrier* pages below, which the platform admin
        keeps. What 8.8 changed is that the retired ids are gone for everyone, so
        no scoped path can surface one any more.
        """
        self.svc.change_password(self.svc.login("root", "Str0ngRootFinal").token,
                                 "Str0ngRootFinal", "Str0ngRootFinal2")
        token = self.svc.login("root", "Str0ngRootFinal2").token
        pages = self._pages(token)
        for pid in pages:
            self.assertFalse(pid.startswith("personal."), pid)
        for pid in sorted(MEMBER_PAGES):
            self.assertTrue(pages[pid]["available"], pid)
        # The platform admin's channels page is the *platform* surface, so it
        # carries no member ``states`` block: the retired page never was theirs.
        self.assertNotEqual(pages["admin.channels"]["scope"], "self")

    def test_the_member_page_never_becomes_the_tenant_surface(self):
        """The separation the retired pages provided is carried by the *range*.

        The member's own surface and the tenant's management surface used to be
        different page ids. Now they are the same id, so the separation has to
        hold in the range and in the management qualification: the member's page
        answers ``self``/``agent``, and 成员管理 / 角色权限 / 组织架构 stay closed
        to them.
        """
        pages = self._pages(self.member_token)
        self.assertTrue(pages["admin.channels"]["available"])
        self.assertEqual(pages["admin.channels"]["scope"], "self",
                         "the member's page must not be the tenant surface")
        self.assertTrue(pages["admin.memory"]["available"])
        self.assertEqual(pages["admin.memory"]["scope"], "agent",
                         "the range comes from the object scope, not tenant management")
        for pid in ("admin.members", "admin.roles", "admin.organization"):
            self.assertFalse(pages[pid]["available"], pid)
            self.assertNotEqual(pages[pid]["scope"], "self", pid)


class ExistingAdminPageTests(_Fixture):
    """The shared pages keep each operator's own range and their own answers."""

    def test_the_admin_ids_keep_their_own_scope(self):
        pages = self._pages(self.admin_token)
        self.assertEqual(pages["admin.memory"]["scope"], "agent")
        self.assertIn(pages["admin.channels"]["scope"], ("tenant", "platform"))
        self.assertIn(pages["admin.skills"]["scope"], ("agent", "tenant"))
        for pid, entry in pages.items():
            if pid.startswith("admin."):
                self.assertNotEqual(entry.get("scope"), "self", pid)

    def test_a_personal_runtime_change_leaves_the_admin_projection_alone(self):
        """Same tenant, same pages: a personal runtime record moves nothing else.

        The record alone does not open the member's execution either — the
        deployment-wide master switch still ships off — so both projections are
        expected to be byte-for-byte unchanged, which is the property the old
        "only the personal ids changed shape" assertion protected.
        """
        before_admin = self._pages(self.admin_token)
        before_member = self._pages(self.member_token)
        with patch("channel.channel_instances.PERSONAL_RUNTIME_ACCEPTED_TYPES",
                   frozenset({"feishu"})):
            after_admin = self._pages(self.admin_token)
            after_member = self._pages(self.member_token)
        for pid, entry in before_admin.items():
            self.assertEqual(entry, after_admin[pid], pid)
        self.assertEqual(before_member["admin.channels"],
                         after_member["admin.channels"])
        self.assertFalse(after_member["admin.channels"]["states"]["execution"])


class OfferedActionAgreesWithTheSurfaceTests(_Fixture):
    """An offered verb must have a surface to act on.

    The projection is what the console trusts to decide whether to show a create
    button. A page that offers ``create`` while its own ``states`` say nothing is
    configurable sends the reader into an empty form — and the interface behind
    it then refuses — which is the "clickable but refused" shape this change set
    out to remove. These two cases pin the pair that had drifted: the member's
    channel catalogue, and the tenant's private-Agent policy.
    """

    def test_an_empty_channel_catalogue_withdraws_create_not_the_page(self):
        """No ready channel type: readable, honestly unconfigurable, no create."""
        with patch("channel.channel_instances.PERSONAL_READY_CHANNEL_TYPES",
                   frozenset()):
            entry = self._pages(self.member_token)["admin.channels"]
        self.assertTrue(entry["available"],
                        "a member may still read the connections they already have")
        self.assertTrue(entry["read_allowed"])
        self.assertFalse(entry["states"]["config"],
                         "with no ready type nothing is configurable")
        self.assertFalse(entry["states"]["execution"])
        self.assertFalse(entry["actions"]["create"],
                         "a create button over an empty catalogue is a false promise")
        self.assertTrue(entry["actions"]["update"],
                        "the rows a member already owns stay maintainable")

    def test_a_ready_channel_type_offers_create_again(self):
        """The negative control: the refusal is the catalogue, not a lost verb."""
        entry = self._pages(self.member_token)["admin.channels"]
        self.assertTrue(entry["states"]["config"])
        self.assertTrue(entry["actions"]["create"])

    def test_a_tenant_that_disabled_personal_agents_withdraws_create(self):
        """The policy is a write refusal, so the projection must not advertise it.

        ``bind_private_agent_with_quota`` answers 403 for a member while the
        tenant's ``personal_enabled`` is off. Reporting ``create: true`` anyway is
        the same false promise as above; the page stays readable because the
        Agents the member already holds remain listable and maintainable.
        """
        self.svc.set_private_agent_policy(
            actor_user_id=self.root["id"], tenant_id=self.tenant,
            personal_enabled=False)
        member = self._pages(self.member_token)["admin.agents"]
        self.assertTrue(member["available"], "existing agents stay readable")
        self.assertFalse(member["actions"]["create"],
                         "the create path refuses, so the page must not offer it")
        self.assertTrue(member["actions"]["update"],
                        "maintaining an object the member already holds is unaffected")

    def test_the_policy_does_not_withdraw_an_administrators_create(self):
        """Control's creation is not what that member-only policy governs."""
        self.svc.set_private_agent_policy(
            actor_user_id=self.root["id"], tenant_id=self.tenant,
            personal_enabled=False)
        admin = self._pages(self.admin_token)["admin.agents"]
        self.assertTrue(admin["available"])
        self.assertTrue(admin["actions"]["create"])

    def test_withdrawing_the_member_surface_switch_withdraws_create(self):
        """The other half of the same promise: the member slice's own switch.

        The registry and the withdrawal semantics belong to
        ``tests/test_personal_capability_switches.py``; what this pins is that
        the projection *consults* the switch for the offered verb, so a member
        slice that is switched off does not advertise a create its write path
        refuses.
        """
        from auth import policy as policy_mod

        real = policy_mod.personal_page_capabilities

        def withdrawn(pid, **kwargs):
            caps = dict(real(pid, **kwargs))
            if pid == "personal.agents":
                caps["user_private_agent_management"] = False
            return caps

        with patch("auth.console_projection.personal_page_capabilities",
                   side_effect=withdrawn):
            entry = self._pages(self.member_token)["admin.agents"]
        self.assertFalse(entry["actions"]["create"])


if __name__ == "__main__":
    unittest.main()
