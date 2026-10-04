# encoding:utf-8
"""A member's own message channels (change enable-member-personal-console 6.x).

The member console registers channel instances the member *owns*: the tenant and
the owner are fixed from the verified session, the credential is write-only,
and the identity link is created only from a code the member proved control of
by sending it from their own IM account.

These tests lock the properties that make the surface safe to expose:

* scope/owner cannot be named by the client, and the tenant's own channel
  surface keeps its administrator gate untouched (6.1);
* the credential is masked everywhere, and a stale version or a foreign owner is
  refused without touching the stored ciphertext (6.2);
* only channel types declared ready may be onboarded, and one external
  application may not be connected twice across the personal/public boundary
  (6.3);
* the challenge is single-use, attempt-bounded and expiry-bounded, and a link can
  only be created from a triple the *provider* observed (6.4);
* unlinking removes this tenant's route and nothing else (6.5);
* a refused personal inbound contributes no private preview or challenge code to
  the administrator's pending list (6.6);
* "saved" and "connected" are reported separately, and a governance stop wins
  over an owner's enable (6.7).
"""

import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from auth.crypto import decrypt_secret
from auth.service import IdentityService, IdentityServiceError


def _settings_snapshot():
    """A mutable copy of the ambient settings, same type ``conf()`` returns.

    ``Config`` rather than a plain ``dict``: one test patches ``.get`` on the
    object to narrow the ready-type set, and that only works on the real type.
    """
    from config import Config, conf as _conf

    return Config(dict(_conf()))


@contextmanager
def _personal_runtime_on(*channel_types):
    """Raise the staged personal-execution switch for one test.

    Production ships ``PERSONAL_RUNTIME_ACCEPTED_TYPES`` empty (task 7.5: no
    real vendor acceptance has been recorded yet), which keeps every personal
    connection closed. Tests raise it per type so the code paths behind the gate
    are exercised *against* the same gate the deployment keeps shut — never by
    removing the check.

    The switch is *merged* into the ambient settings rather than replacing them:
    ``agent_workspace`` has to survive, because the Agent Registry is resolved
    from it and the personal write paths now verify their target against that
    roster. Replacing ``conf()`` wholesale would resolve the registry against the
    developer's real workspace and let the outcome depend on their roster.
    """
    settings = _settings_snapshot()
    settings["personal_channel_runtime"] = True
    with patch("channel.channel_instances.PERSONAL_RUNTIME_ACCEPTED_TYPES",
               frozenset(channel_types)), \
            patch("channel.channel_instances.PUBLIC_PERSONAL_INGRESS_TYPES",
                  frozenset(channel_types)), \
            patch("config.conf", lambda: settings):
        yield


#: The Agents every test in this module needs in the roster. The two shared ones
#: keep the public path's own semantics covered (a shared instance may route to
#: any Agent of the tenant, bound or not); the private ones are the personal
#: targets, and the disabled one exists so "the target was switched off" is
#: provable rather than assumed.
_ROSTER = (
    "agent-a", "agent-b",
    "alice-assistant", "alice-own", "bob-own", "globex-own",
    "alice-globex", "alice-off",
)


@contextmanager
def _roster_in_place():
    """Run with a real Agent Registry over a private workspace.

    A personal channel write verifies its target against the Agent Registry —
    the same roster the runtime starts a connection from — so a test that
    exercises the write path has to provide one. Pinned to a fresh temp
    workspace so the developer's own roster cannot turn a refusal into a pass.
    """
    settings = _settings_snapshot()
    settings["agent_workspace"] = tempfile.mkdtemp(prefix="channel-console-")
    settings["agents"] = [
        {"id": agent_id, "name": agent_id,
         "enabled": agent_id != "alice-off"}
        for agent_id in _ROSTER
    ]
    settings["default_agent_id"] = "agent-a"
    with patch("config.conf", lambda: settings):
        yield

FEISHU_BUNDLE = {
    "feishu_app_id": "cli_personal_a",
    "feishu_app_secret": "s3cr3t-personal",
    "feishu_bot_name": "Alice Bot",
}
TENANT_BUNDLE = {
    "feishu_app_id": "cli_tenant_shared",
    "feishu_app_secret": "s3cr3t-tenant",
    "feishu_bot_name": "Tenant Bot",
}


def _db_path():
    return os.path.join(tempfile.mkdtemp(), "identity.db")


def _expect_error(test, expected_code, expected_status, fn, *args, **kwargs):
    with test.assertRaises(IdentityServiceError) as caught:
        fn(*args, **kwargs)
    test.assertEqual(caught.exception.code, expected_code, str(caught.exception))
    test.assertEqual(caught.exception.status, expected_status)
    return caught.exception


class _Fixture(unittest.TestCase):
    """One tenant with two members, plus a second tenant to cross over to."""

    MASTER_KEY = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"

    ROOT_PW = "Str0ngRootFinal"
    MEMBER_PW = "Str0ngMemberFinal"

    def setUp(self):
        self._previous_key = os.environ.get("COW_CREDENTIAL_MASTER_KEY")
        os.environ["COW_CREDENTIAL_MASTER_KEY"] = self.MASTER_KEY
        self.addCleanup(self._restore_master_key)

        rosters = _roster_in_place()
        rosters.__enter__()
        self.addCleanup(lambda: rosters.__exit__(None, None, None))
        from agent.registry import set_agent_registry

        set_agent_registry(None)

        self.svc = IdentityService(_db_path())
        self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root="/s/acme")
        self.svc.change_password(
            self.svc.login("root", "Str0ngAdminPass").token,
            "Str0ngAdminPass", self.ROOT_PW)
        self.root = [u for u in self.svc.list_platform_users()
                     if u["username"] == "root"][0]
        self.ta = self.svc.list_tenants()[0]["id"]

        self.svc.create_tenant(
            actor_user_id=self.root["id"], code="globex", name="Globex",
            admin_username="globexadmin", admin_display="Globex Admin",
            admin_password="Str0ngPass9", recent_password=self.ROOT_PW,
            shared_root="/s/globex")
        self.tb = [t for t in self.svc.list_tenants()
                   if t["code"] == "globex"][0]["id"]
        self.svc.change_password(
            self.svc.login("globexadmin", "Str0ngPass9").token,
            "Str0ngPass9", "Str0ngGlobexFinal")
        self.admin_b = [m for m in self.svc.list_members(self.tb)["items"]
                        if m["username"] == "globexadmin"][0]["user_id"]

        # The two *shared* Agents keep every public-path test honest: a shared
        # instance may be unbound, but when it does name a target that target is
        # any Agent of the tenant (``private_owner_user_id IS NULL``).
        self.svc.bind_agent(tenant_id=self.ta, agent_id="agent-a",
                            private_owner_user_id=None)
        self.svc.bind_agent(tenant_id=self.tb, agent_id="agent-b",
                            private_owner_user_id=None)

        self.alice = self._add_member("alice")
        self.bob = self._add_member("bob")

        # A personal instance's target has to be a *private* Agent of its own
        # owner (task 2.1), so the fixture gives each member the two shapes the
        # spec enumerates: the system-supplied assistant and a self-built one.
        self.alice_assistant = self._private_agent(
            self.alice, "alice-assistant", origin="provisioned_assistant")
        self.alice_own = self._private_agent(
            self.alice, "alice-own", origin="user_created")
        # Bound but switched off in the roster: the ownership half of the target
        # predicate holds, the enablement half does not, which is the case that
        # has its own refusal ("switch your assistant back on") rather than
        # collapsing into "not selectable".
        self.alice_off = self._private_agent(self.alice, "alice-off")
        self.bob_own = self._private_agent(self.bob, "bob-own")
        self.admin_b_own = self._private_agent(
            self.admin_b, "globex-own", tenant_id=self.tb)
        # Alice also holds a personal Agent of her own in Globex: one member can
        # own resources in two tenants, and each tenant's target is its own.
        self.alice_globex = self._private_agent(
            self.alice, "alice-globex", tenant_id=self.tb)

    def _private_agent(self, owner_user_id, agent_id, *, tenant_id=None,
                       origin="user_created"):
        """Bind ``agent_id`` as ``owner_user_id``'s own private Agent.

        Written through the same ``bind_agent`` call the product uses, so the
        ownership fact the channel write paths read is the real one — the
        fixture never pokes ``agent_bindings`` directly.
        """
        self.svc.bind_agent(tenant_id=tenant_id or self.ta, agent_id=agent_id,
                            private_owner_user_id=owner_user_id, origin=origin)
        return agent_id

    def _owned_agent(self, owner_user_id):
        """The private Agent that ``owner_user_id`` should target by default."""
        return {self.alice: self.alice_own,
                self.bob: self.bob_own,
                self.admin_b: self.admin_b_own}[owner_user_id]

    def _restore_master_key(self):
        if self._previous_key is None:
            os.environ.pop("COW_CREDENTIAL_MASTER_KEY", None)
        else:
            os.environ["COW_CREDENTIAL_MASTER_KEY"] = self._previous_key

    def _add_member(self, username, tenant_id=None):
        tenant_id = tenant_id or self.ta
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=tenant_id,
            operation="create-new", username=username,
            display_name=username.title(), temporary_password="MemTempPass1",
            roles=["member"])
        user_id = [m for m in self.svc.list_members(tenant_id)["items"]
                   if m["username"] == username][0]["user_id"]
        # The console refuses a forced password change, and a credential write
        # proves presence with the account password, so the member has to be a
        # normal account before any of this is reachable.
        session = self.svc.login(username, "MemTempPass1")
        self.svc.change_password(session.token, "MemTempPass1", self.MEMBER_PW)
        return user_id

    # -- helpers ---------------------------------------------------------

    def _credential_rows(self, tenant_id):
        return [dict(r) for r in self.svc._store.execute(
            "SELECT * FROM credentials WHERE tenant_id=? AND resource_kind='channel'"
            " ORDER BY name", (tenant_id,))]

    def _instance_row(self, instance_id):
        rows = self.svc._store.execute(
            "SELECT * FROM tenant_channel_instances WHERE id=?", (instance_id,))
        return dict(rows[0]) if rows else None

    def _create(self, owner, *, display_name="My Bot",
                credentials=None, channel_type="feishu", agent_id=None,
                tenant_id=None, password=None):
        return self.svc.create_personal_channel_instance(
            actor_user_id=owner, tenant_id=tenant_id or self.ta,
            channel_type=channel_type, display_name=display_name,
            # ``None`` means "the fixture picks the member's own Agent"; ``""``
            # is the *empty target* case a test has to be able to submit, so it
            # must not be quietly replaced by the convenience default.
            agent_id=(self._owned_agent(owner) if agent_id is None else agent_id),
            credentials=dict(credentials or FEISHU_BUNDLE),
            recent_password=password or self.MEMBER_PW)

    def _tenant_instance(self, display_name="Tenant Bot", **over):
        args = dict(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            channel_type="feishu", display_name=display_name,
            agent_id="agent-a", credentials=dict(TENANT_BUNDLE),
            recent_password=self.ROOT_PW)
        args.update(over)
        return self.svc.create_tenant_channel_instance(**args)


class PersonalSurfaceTests(_Fixture):
    """6.1 — a registered personal surface with a fixed tenant and owner."""

    def test_create_forces_the_callers_own_scope_and_owner(self):
        created = self._create(self.alice)
        row = self._instance_row(created["id"])
        self.assertEqual(row["scope"], "user")
        self.assertEqual(row["owner_user_id"], self.alice)
        self.assertEqual(row["tenant_id"], self.ta)
        # The client-facing projection does not even carry the owner field, so
        # there is nothing to forge and nothing to overwrite.
        self.assertNotIn("owner_user_id", created)

    def test_the_listing_shows_only_my_own_instances(self):
        mine = self._create(self.alice, display_name="Alice Bot")
        self._create(self.bob, display_name="Bob Bot",
                     credentials=dict(FEISHU_BUNDLE,
                                      feishu_app_id="cli_bob"))
        listing = self.svc.list_personal_channel_instances(
            actor_user_id=self.alice, tenant_id=self.ta)
        self.assertEqual([i["id"] for i in listing["items"]], [mine["id"]])

    def test_another_members_instance_is_not_addressable(self):
        bobs = self._create(self.bob, credentials=dict(FEISHU_BUNDLE,
                                                       feishu_app_id="cli_bob"))
        _expect_error(
            self, "forbidden", 403, self.svc.get_personal_channel_instance,
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=bobs["id"])
        _expect_error(
            self, "forbidden", 403, self.svc.set_personal_channel_instance_active,
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=bobs["id"], active=False, expected_version=1,
            recent_password=self.MEMBER_PW)

    def test_another_tenants_personal_instance_is_out_of_reach(self):
        elsewhere = self.svc.create_tenant_channel_instance(
            actor_user_id=self.admin_b, tenant_id=self.tb,
            channel_type="feishu", display_name="Globex Bot",
            agent_id=self.admin_b_own, credentials=dict(FEISHU_BUNDLE),
            recent_password="Str0ngGlobexFinal",
            scope="user", owner_user_id=self.admin_b)
        # The id exists; the (tenant, id) pair does not.
        _expect_error(
            self, "not_found", 404, self.svc.get_personal_channel_instance,
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=elsewhere["id"])

    def test_the_public_surface_keeps_its_administrator_gate(self):
        """The administrative branch is still refused to a member (task 6.1).

        The console now *routes* a member to the owner branch, but the decision
        did not move into the browser: reaching the tenant branch directly — as a
        stale client, a script or a future caller would — is still a 403, and the
        test exists to keep it that way.
        """
        _expect_error(
            self, "forbidden", 403, self.svc.create_tenant_channel_instance,
            actor_user_id=self.alice, tenant_id=self.ta, channel_type="feishu",
            display_name="Sneaky", agent_id="agent-a",
            credentials=dict(FEISHU_BUNDLE), recent_password=self.MEMBER_PW)
        _expect_error(
            self, "forbidden", 403,
            self.svc.set_tenant_channel_instance_active,
            actor_user_id=self.alice, tenant_id=self.ta, instance_id="ci_x",
            active=False, expected_version=1, recent_password=self.MEMBER_PW)
        # The *list* is the one verb a member does reach, and it answers with
        # their own range rather than the tenant's — an empty list here, because
        # Alice owns nothing yet. Refusing it would hide the surface that
        # replaced the personal workbench.
        listing = self.svc.list_tenant_channel_instances(
            actor_user_id=self.alice, tenant_id=self.ta)
        self.assertEqual(listing["items"], [])
        self.assertEqual(listing["total"], 0)

    def test_a_non_member_cannot_register_anything(self):
        _expect_error(
            self, "forbidden", 403, self.svc.create_personal_channel_instance,
            actor_user_id=self.admin_b, tenant_id=self.ta, channel_type="feishu",
            display_name="Outsider", agent_id="agent-a",
            credentials=dict(FEISHU_BUNDLE),
            recent_password="Str0ngGlobexFinal")


class CredentialMaskingTests(_Fixture):
    """6.2 — masked projections, versioned rotation, owner-scoped revoke."""

    def test_the_projection_reports_only_names_and_a_version(self):
        created = self._create(self.alice)
        credential = created["credential"]
        self.assertTrue(credential["configured"])
        self.assertEqual(credential["version"], 1)
        self.assertIn("feishu_app_id", credential["fields"])
        blob = json.dumps(created, default=str)
        self.assertNotIn("s3cr3t-personal", blob)
        self.assertNotIn("cli_personal_a", blob)

    def test_a_rotation_appends_a_version_and_keeps_one_credential_row(self):
        created = self._create(self.alice)
        before = self._credential_rows(self.ta)
        self.assertEqual(len(before), 1)
        updated = self.svc.update_personal_channel_instance(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"], expected_version=created["version"],
            credentials={"feishu_app_id": "cli_personal_a",
                         "feishu_app_secret": "rotated-secret"},
            recent_password=self.MEMBER_PW)
        after = self._credential_rows(self.ta)
        self.assertEqual(len(after), 1, "an instance has exactly one credential")
        self.assertEqual(after[0]["version"], 2)
        versions = [dict(r) for r in self.svc._store.execute(
            "SELECT version, action FROM credential_versions WHERE credential_id=?"
            " ORDER BY version", (after[0]["id"],))]
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertEqual(updated["credential"]["version"], 2)

    def test_the_running_bundle_is_the_new_one_not_the_old(self):
        created = self._create(self.alice)
        before = self.svc.channel_instance_credentials(self.ta, created["id"])
        self.assertEqual(before["feishu_app_secret"], "s3cr3t-personal")
        self.svc.update_personal_channel_instance(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"], expected_version=created["version"],
            credentials={"feishu_app_secret": "rotated-secret"},
            recent_password=self.MEMBER_PW)
        bundle = self.svc.channel_instance_credentials(self.ta, created["id"])
        self.assertEqual(bundle["feishu_app_secret"], "rotated-secret")
        self.assertEqual(bundle["feishu_app_id"], "cli_personal_a")

    def test_a_stale_version_is_refused_and_changes_nothing(self):
        created = self._create(self.alice)
        self.svc.update_personal_channel_instance(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"], expected_version=created["version"],
            display_name="Renamed once", recent_password=self.MEMBER_PW)
        rows_before = self._credential_rows(self.ta)
        _expect_error(
            self, "conflict", 409, self.svc.update_personal_channel_instance,
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"], expected_version=created["version"],
            display_name="Renamed twice", recent_password=self.MEMBER_PW)
        row = self._instance_row(created["id"])
        self.assertEqual(row["display_name"], "Renamed once")
        self.assertEqual(self._credential_rows(self.ta)[0]["ciphertext"],
                         rows_before[0]["ciphertext"])

    def test_another_member_cannot_rotate_my_credential(self):
        created = self._create(self.alice)
        before = self._credential_rows(self.ta)[0]["ciphertext"]
        _expect_error(
            self, "forbidden", 403, self.svc.update_personal_channel_instance,
            actor_user_id=self.bob, tenant_id=self.ta,
            instance_id=created["id"], expected_version=created["version"],
            credentials={"feishu_app_secret": "stolen"},
            recent_password=self.MEMBER_PW)
        self.assertEqual(self._credential_rows(self.ta)[0]["ciphertext"], before)

    def test_revoke_deactivates_the_credential_and_stops_the_instance(self):
        created = self._create(self.alice)
        revoked = self.svc.revoke_personal_channel_credentials(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"], expected_version=created["version"],
            recent_password=self.MEMBER_PW)
        self.assertFalse(revoked["active"])
        self.assertFalse(revoked["credential"]["configured"])
        self.assertFalse(self._credential_rows(self.ta)[0]["active"])
        # "Revoked" has to mean "unusable": the runtime path must not be able to
        # run the instance with the withdrawn credential.
        _expect_error(self, "not_found", 404,
                      self.svc.channel_instance_credentials, self.ta,
                      created["id"])
        actions = [r["action"] for r in self.svc._store.execute(
            "SELECT action FROM audit_events ORDER BY rowid")]
        self.assertIn("channel.credential.revoke", actions)

    def test_revoke_requires_the_owners_password(self):
        created = self._create(self.alice)
        _expect_error(
            self, "invalid_old", 401,
            self.svc.revoke_personal_channel_credentials,
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"], expected_version=created["version"],
            recent_password="wrong-password")


class ReadinessTests(_Fixture):
    """6.3 — only declared-ready types, and one application per connection."""

    def test_the_type_catalog_carries_a_verdict_per_type(self):
        from channel.channel_instances import personal_channel_types

        catalog = {item["channel_type"]: item for item in personal_channel_types()}
        # A type the console can offer is a type the server accepts: the field
        # contract and the readiness verdict come from one declaration.
        self.assertTrue(catalog["feishu"]["ready"])
        self.assertEqual(catalog["feishu"]["reason"], "")
        self.assertTrue(catalog["feishu"]["credential_fields"])
        for item in catalog.values():
            # "ready" and "no reason" are the same statement, or the console
            # would show a form whose save is refused.
            self.assertEqual(item["ready"], item["reason"] == "")

    def test_an_unknown_type_is_refused_with_an_actionable_reason(self):
        from channel.channel_instances import personal_channel_ready

        ready, reason = personal_channel_ready("unknown_channel")
        self.assertFalse(ready)
        self.assertIn(reason, {"not_multi_instance", "no_credential_contract"})
        _expect_error(
            self, "channel_type_not_ready", 403,
            self.svc.create_personal_channel_instance,
            actor_user_id=self.alice, tenant_id=self.ta,
            channel_type="unknown_channel", display_name="Weird", agent_id="agent-a",
            credentials={"unknown_token": "x"},
            recent_password=self.MEMBER_PW)

    def test_configuration_can_narrow_the_ready_set_but_never_widen_it(self):
        from config import conf

        with patch.object(conf(), "get",
                          side_effect=lambda key, default=None: (
                              ["dingtalk"] if key == "personal_channel_ready_types"
                              else default)):
            from channel.channel_instances import personal_channel_ready

            self.assertEqual(personal_channel_ready("dingtalk"), (True, ""))
            self.assertEqual(personal_channel_ready("feishu"),
                             (False, "not_allowed"))
            # A type outside the base set cannot be opened by configuration.
            self.assertFalse(personal_channel_ready("wechat_kf")[0])
            _expect_error(
                self, "channel_type_not_ready", 403,
                self.svc.create_personal_channel_instance,
                actor_user_id=self.alice, tenant_id=self.ta,
                channel_type="feishu", display_name="Narrowed",
                agent_id="agent-a", credentials=dict(FEISHU_BUNDLE),
                recent_password=self.MEMBER_PW)

    def test_a_personal_instance_may_not_borrow_the_tenants_application(self):
        self._tenant_instance()
        _expect_error(
            self, "app_conflict", 409, self._create, self.alice,
            credentials=dict(TENANT_BUNDLE))

    def test_two_personal_instances_may_not_share_an_application(self):
        self._create(self.alice)
        _expect_error(self, "app_conflict", 409, self._create, self.bob)

    def test_two_shared_instances_may_still_share_an_application(self):
        """The upstream tenant surface's own behaviour is not narrowed here."""
        self._tenant_instance(display_name="Tenant Bot")
        self._tenant_instance(display_name="Tenant Bot 2")
        listing = self.svc.list_tenant_channel_instances(
            actor_user_id=self.root["id"], tenant_id=self.ta)
        self.assertEqual(listing["total"], 2)

    def test_a_rotation_onto_a_taken_application_is_refused(self):
        self._tenant_instance()
        mine = self._create(self.alice)
        before = self._instance_row(mine["id"])["app_fingerprint"]
        _expect_error(
            self, "app_conflict", 409,
            self.svc.update_personal_channel_instance,
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=mine["id"], expected_version=mine["version"],
            credentials={"feishu_app_id": "cli_tenant_shared"},
            recent_password=self.MEMBER_PW)
        # The refusal left the old application in place.
        self.assertEqual(self._instance_row(mine["id"])["app_fingerprint"], before)

    def test_the_fingerprint_is_not_the_application_id(self):
        created = self._create(self.alice)
        row = self._instance_row(created["id"])
        self.assertTrue(row["app_fingerprint"])
        self.assertNotIn("cli_personal_a", row["app_fingerprint"])

    def test_enabling_an_instance_whose_application_is_taken_is_refused(self):
        first = self._create(self.alice)
        second = self._create(self.bob, credentials=dict(FEISHU_BUNDLE,
                                                         feishu_app_id="cli_bob"))
        # Move Bob's instance onto Alice's application while Alice's is off, so
        # the write itself is legal…
        disabled_second = self.svc.set_personal_channel_instance_active(
            actor_user_id=self.bob, tenant_id=self.ta,
            instance_id=second["id"], active=False,
            expected_version=second["version"], recent_password=self.MEMBER_PW)
        rotated = self.svc.update_personal_channel_instance(
            actor_user_id=self.bob, tenant_id=self.ta,
            instance_id=second["id"], expected_version=disabled_second["version"],
            credentials={"feishu_app_id": "cli_personal_a"},
            recent_password=self.MEMBER_PW)
        # …and the collision is caught when one of them is actually enabled,
        # which is when the vendor connection would fight.
        _expect_error(
            self, "app_conflict", 409,
            self.svc.set_personal_channel_instance_active,
            actor_user_id=self.bob, tenant_id=self.ta,
            instance_id=second["id"], active=True,
            expected_version=rotated["version"],
            recent_password=self.MEMBER_PW)
        self.assertFalse(self.svc.get_personal_channel_instance(
            actor_user_id=self.bob, tenant_id=self.ta,
            instance_id=second["id"])["active"])
        del first


class BindingChallengeTests(_Fixture):
    """6.4 — proof of control, single use, bounded attempts, unique triples."""

    def test_a_started_challenge_stores_only_a_hash(self):
        created = self._create(self.alice)
        challenge = self.svc.start_personal_channel_binding(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"])
        code = challenge["code"]
        self.assertTrue(code)
        rows = [dict(r) for r in self.svc._store.execute(
            "SELECT * FROM binding_challenges")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["instance_id"], created["id"])
        self.assertEqual(rows[0]["user_id"], self.alice)
        self.assertNotIn(code, rows[0]["code_hash"])
        self.assertGreater(rows[0]["expires_at"], 0)

    def test_a_code_is_redeemed_once_and_links_the_observed_sender(self):
        created = self._create(self.alice)
        challenge = self.svc.start_personal_channel_binding(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"])
        redeemed = self.svc.redeem_personal_channel_challenge(
            tenant_id=self.ta, instance_id=created["id"], code=challenge["code"],
            provider="feishu", issuer="cli_personal_a", subject="ou_alice")
        self.assertEqual(redeemed["link"]["subject"], "ou_alice")
        status = self.svc.personal_channel_binding_status(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"])
        self.assertEqual(status["link"]["provider"], "feishu")
        # The owner confirms *which* account is linked without reading back the
        # identifier the provider minted.
        self.assertTrue(status["link"]["subject_masked"].startswith("••••"))
        self.assertNotIn("ou_alice", json.dumps(status, default=str))

    def test_the_same_code_cannot_be_replayed(self):
        created = self._create(self.alice)
        challenge = self.svc.start_personal_channel_binding(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"])
        self.svc.redeem_personal_channel_challenge(
            tenant_id=self.ta, instance_id=created["id"], code=challenge["code"],
            provider="feishu", issuer="cli_personal_a", subject="ou_alice")
        _expect_error(
            self, "not_found", 404,
            self.svc.redeem_personal_channel_challenge,
            tenant_id=self.ta, instance_id=created["id"],
            code=challenge["code"], provider="feishu",
            issuer="cli_personal_a", subject="ou_mallory")
        # …and the replay did not relink the instance to the second sender.
        link = self.svc.personal_channel_link(
            tenant_id=self.ta, user_id=self.alice, instance_id=created["id"])
        self.assertEqual(link["subject"], "ou_alice")

    def test_one_code_arriving_twice_at_once_links_only_its_sender(self):
        """The claim is what makes single-use true, not the lookup that precedes
        it: two inbounds carrying one code must resolve to exactly one link, and
        the loser must not relink the instance to the sender it observed."""
        created = self._create(self.alice)
        challenge = self.svc.start_personal_channel_binding(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"])
        outcomes = []
        lock = threading.Lock()
        barrier = threading.Barrier(2, timeout=10)

        def _redeem(subject):
            barrier.wait()
            try:
                self.svc.redeem_personal_channel_challenge(
                    tenant_id=self.ta, instance_id=created["id"],
                    code=challenge["code"], provider="feishu",
                    issuer="cli_personal_a", subject=subject)
                with lock:
                    outcomes.append((subject, "ok"))
            except IdentityServiceError as e:
                with lock:
                    outcomes.append((subject, e.code))

        threads = [threading.Thread(target=_redeem, args=(subject,))
                   for subject in ("ou_alice", "ou_mallory")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([o[1] for o in outcomes].count("ok"), 1, outcomes)
        link = self.svc.personal_channel_link(
            tenant_id=self.ta, user_id=self.alice, instance_id=created["id"])
        winner = [o[0] for o in outcomes if o[1] == "ok"][0]
        self.assertEqual(link["subject"], winner,
                         "the loser must not have overwritten the winner")
        consumed = self.svc._store.execute(
            "SELECT consumed_at FROM binding_challenges WHERE id=?",
            (challenge["challenge_id"],))[0]
        self.assertIsNotNone(consumed["consumed_at"])

    def test_a_wrong_code_burns_an_attempt_and_then_locks(self):
        from auth.service import CHALLENGE_MAX_ATTEMPTS

        created = self._create(self.alice)
        challenge = self.svc.start_personal_channel_binding(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"])
        for _ in range(CHALLENGE_MAX_ATTEMPTS):
            _expect_error(
                self, "bad_request", 400,
                self.svc.redeem_personal_channel_challenge,
                tenant_id=self.ta, instance_id=created["id"], code="00000000",
                provider="feishu", issuer="cli_personal_a", subject="ou_alice")
        attempts = self.svc._store.execute(
            "SELECT attempts, consumed_at FROM binding_challenges"
            " WHERE id=?", (challenge["challenge_id"],))[0]
        self.assertEqual(attempts["attempts"], CHALLENGE_MAX_ATTEMPTS)
        self.assertIsNone(attempts["consumed_at"])
        # Even the right code is now refused: the budget, not the code's
        # entropy, is what bounds guessing.
        _expect_error(
            self, "too_many_requests", 429,
            self.svc.redeem_personal_channel_challenge,
            tenant_id=self.ta, instance_id=created["id"], code=challenge["code"],
            provider="feishu", issuer="cli_personal_a", subject="ou_alice")

    def test_an_expired_code_is_usable_no_more(self):
        from auth import service as service_module

        created = self._create(self.alice)
        challenge = self.svc.start_personal_channel_binding(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"])
        with patch.object(service_module.time, "time",
                          return_value=challenge["expires_at"] + 1):
            _expect_error(
                self, "expired", 410,
                self.svc.redeem_personal_channel_challenge,
                tenant_id=self.ta, instance_id=created["id"],
                code=challenge["code"], provider="feishu",
                issuer="cli_personal_a", subject="ou_alice")
            self.assertIsNone(self.svc.personal_channel_binding_status(
                actor_user_id=self.alice, tenant_id=self.ta,
                instance_id=created["id"])["challenge"])

    def test_a_code_for_another_instance_does_not_link_mine(self):
        mine = self._create(self.alice)
        bobs = self._create(self.bob, credentials=dict(FEISHU_BUNDLE,
                                                       feishu_app_id="cli_bob"))
        bobs_challenge = self.svc.start_personal_channel_binding(
            actor_user_id=self.bob, tenant_id=self.ta, instance_id=bobs["id"])
        # The code is scoped to bob's instance: presenting it against mine is a
        # lookup miss, not a check a caller could influence.
        _expect_error(
            self, "not_found", 404,
            self.svc.redeem_personal_channel_challenge,
            tenant_id=self.ta, instance_id=mine["id"],
            code=bobs_challenge["code"], provider="feishu",
            issuer="cli_bob", subject="ou_bob")

    def test_a_triple_bound_to_someone_else_is_never_taken_over(self):
        bobs = self._create(self.bob, credentials=dict(FEISHU_BUNDLE,
                                                       feishu_app_id="cli_bob"))
        bobs_challenge = self.svc.start_personal_channel_binding(
            actor_user_id=self.bob, tenant_id=self.ta, instance_id=bobs["id"])
        self.svc.redeem_personal_channel_challenge(
            tenant_id=self.ta, instance_id=bobs["id"],
            code=bobs_challenge["code"], provider="feishu",
            issuer="cli_bob", subject="ou_shared")
        # A separate instance holding the *same* identity is Bob's own business,
        # but Alice must not be able to bind an identity that resolves to Bob.
        mine = self._create(self.alice)
        challenge = self.svc.start_personal_channel_binding(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=mine["id"])
        _expect_error(
            self, "conflict", 409,
            self.svc.redeem_personal_channel_challenge,
            tenant_id=self.ta, instance_id=mine["id"], code=challenge["code"],
            provider="feishu", issuer="cli_bob", subject="ou_shared")

    def test_the_console_surface_offers_no_way_to_name_a_subject(self):
        """只有提供方观察到的三元组能建绑：控制台的 action 里没有「按 subject 绑定」。"""
        import inspect

        from channel.web import web_channel

        source = inspect.getsource(web_channel.PersonalChannelInstanceHandler)
        for forbidden in ('body.get("subject")', "body.get('subject')",
                          '"bind"'):
            self.assertNotIn(forbidden, source)
        for action in ("update", "revoke", "start_binding", "unlink"):
            self.assertIn(f'action == "{action}"', source)
        # enable/disable share one branch, which is still a closed enumeration:
        # anything else answers "unknown action".
        self.assertIn('action in ("enable", "disable")', source)
        self.assertIn('"unknown action: {action}"', source)

    def test_a_shared_instance_has_no_self_service_link(self):
        shared = self._tenant_instance()
        # A shared instance has no owner, so there is no instance-level link to
        # establish: its personal routes exist only as per-member challenge rows
        # that a member mints for themselves (task 7.2). A code nobody minted
        # against it therefore matches nothing, and — importantly — the refusal
        # creates no route either.
        _expect_error(
            self, "not_found", 404,
            self.svc.redeem_personal_channel_challenge,
            tenant_id=self.ta, instance_id=shared["id"], code="12345678",
            provider="feishu", issuer="cli_tenant_shared", subject="ou_alice")
        self.assertIsNone(self.svc.personal_channel_link(
            tenant_id=self.ta, user_id=self.alice, instance_id=shared["id"]))


class IdentityLinkTests(_Fixture):
    """6.5 — the owner's own route only, and other tenants stay intact."""

    def _link(self, owner, instance_id, subject):
        challenge = self.svc.start_personal_channel_binding(
            actor_user_id=owner, tenant_id=self.ta, instance_id=instance_id)
        return self.svc.redeem_personal_channel_challenge(
            tenant_id=self.ta, instance_id=instance_id, code=challenge["code"],
            provider="feishu", issuer="cli_personal_a", subject=subject)

    def test_unlinking_removes_the_route_and_keeps_the_identity(self):
        mine = self._create(self.alice)
        self._link(self.alice, mine["id"], "ou_alice")
        self.svc.unlink_personal_channel_instance(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=mine["id"])
        self.assertIsNone(self.svc.personal_channel_link(
            tenant_id=self.ta, user_id=self.alice, instance_id=mine["id"]))
        # The global mapping survives: the same person may still be using it in
        # another tenant, and the admin bind flow may depend on it.
        self.assertIsNotNone(self.svc.find_user_for_external_identity(
            "feishu", "cli_personal_a", "ou_alice"))

    def test_unlinking_here_does_not_touch_another_tenants_mapping(self):
        mine = self._create(self.alice)
        self._link(self.alice, mine["id"], "ou_alice")
        # Alice is also a member of Globex with her own personal instance there.
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tb,
            operation="bind-existing", username="alice", display_name="Alice",
            temporary_password="MemTempPass1", roles=["member"])
        foreign = self.svc.create_personal_channel_instance(
            actor_user_id=self.alice, tenant_id=self.tb, channel_type="feishu",
            display_name="Globex Bot", agent_id=self.alice_globex,
            credentials=dict(FEISHU_BUNDLE), recent_password=self.MEMBER_PW)
        challenge = self.svc.start_personal_channel_binding(
            actor_user_id=self.alice, tenant_id=self.tb,
            instance_id=foreign["id"])
        self.svc.redeem_personal_channel_challenge(
            tenant_id=self.tb, instance_id=foreign["id"],
            code=challenge["code"], provider="feishu",
            issuer="cli_personal_a", subject="ou_alice")

        self.svc.unlink_personal_channel_instance(
            actor_user_id=self.alice, tenant_id=self.ta, instance_id=mine["id"])

        self.assertIsNotNone(self.svc.personal_channel_link(
            tenant_id=self.tb, user_id=self.alice, instance_id=foreign["id"]))

    def test_another_member_cannot_unlink_my_route(self):
        mine = self._create(self.alice)
        self._link(self.alice, mine["id"], "ou_alice")
        _expect_error(
            self, "forbidden", 403,
            self.svc.unlink_personal_channel_instance,
            actor_user_id=self.bob, tenant_id=self.ta, instance_id=mine["id"])
        self.assertIsNotNone(self.svc.personal_channel_link(
            tenant_id=self.ta, user_id=self.alice, instance_id=mine["id"]))


class AttemptRedactionTests(_Fixture):
    """6.6 — a personal inbound contributes no private content."""

    def test_a_personal_attempt_stores_no_preview_or_nickname(self):
        created = self._create(self.alice)
        self.svc.record_external_identity_attempt(
            provider="feishu", issuer="cli_personal_a", subject="ou_stranger",
            tenant_id=self.ta, channel_type="feishu",
            instance_id=created["id"],
            sender_name="Stranger", message_preview="my private message 1234",
            is_group=False)
        row = dict(self.svc._store.execute(
            "SELECT * FROM external_identity_attempts")[0])
        self.assertEqual(row["instance_id"], created["id"])
        self.assertEqual(row["message_preview"], "")
        self.assertEqual(row["sender_name"], "")
        # The triple is kept: it is what the administrator binds, and it is not
        # private content.
        self.assertEqual(row["subject"], "ou_stranger")

    def test_a_challenge_code_never_reaches_the_store(self):
        created = self._create(self.alice)
        challenge = self.svc.start_personal_channel_binding(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"])
        self.svc.record_external_identity_attempt(
            provider="feishu", issuer="cli_personal_a", subject="ou_stranger",
            tenant_id=self.ta, channel_type="feishu",
            instance_id=created["id"], personal_flow=True,
            message_preview=f"bind {challenge['code']}", is_group=False)
        row = dict(self.svc._store.execute(
            "SELECT * FROM external_identity_attempts")[0])
        self.assertNotIn(challenge["code"], json.dumps(row, default=str))

    def test_a_flagged_flow_is_redacted_even_on_a_shared_instance(self):
        shared = self._tenant_instance()
        self.svc.record_external_identity_attempt(
            provider="feishu", issuer="cli_tenant_shared", subject="ou_x",
            tenant_id=self.ta, channel_type="feishu", instance_id=shared["id"],
            personal_flow=True, sender_name="Stranger",
            message_preview="private text", is_group=False)
        row = dict(self.svc._store.execute(
            "SELECT * FROM external_identity_attempts")[0])
        self.assertEqual(row["message_preview"], "")
        self.assertEqual(row["sender_name"], "")

    def test_an_ordinary_shared_attempt_keeps_its_evidence(self):
        shared = self._tenant_instance()
        self.svc.record_external_identity_attempt(
            provider="feishu", issuer="cli_tenant_shared", subject="ou_y",
            tenant_id=self.ta, channel_type="feishu", instance_id=shared["id"],
            sender_name="Someone", message_preview="hello", is_group=False)
        row = dict(self.svc._store.execute(
            "SELECT * FROM external_identity_attempts")[0])
        self.assertEqual(row["message_preview"], "hello")
        self.assertEqual(row["sender_name"], "Someone")

    def test_the_administrator_list_redacts_personal_rows(self):
        created = self._create(self.alice)
        # A row written before this rule existed (or by a caller that could not
        # know) must not be displayed either: the read path is the last line.
        with self.svc._tx() as con:
            con.execute(
                "INSERT INTO external_identity_attempts(provider, issuer, subject,"
                " tenant_id, channel_type, instance_id, attempts, last_seen_at,"
                " sender_name, message_preview, is_group)"
                " VALUES ('feishu','cli_personal_a','ou_old',?, 'feishu', ?, 1,"
                " unixepoch(), 'Alice', 'old private text', 0)",
                (self.ta, created["id"]))
            con.commit()
        listed = self.svc.list_external_identity_attempts(
            actor_user_id=self.root["id"], tenant_id=self.ta)
        item = listed["items"][0]
        self.assertEqual(item["message_preview"], "")
        self.assertEqual(item["sender_name"], "")


class RuntimeStateTests(_Fixture):
    """6.7 — saved is not connected, governance wins, the owner re-enables."""

    def test_disabling_keeps_the_credential_and_enabling_restores_it(self):
        created = self._create(self.alice)
        disabled = self.svc.set_personal_channel_instance_active(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"], active=False,
            expected_version=created["version"], recent_password=self.MEMBER_PW)
        self.assertFalse(disabled["active"])
        self.assertTrue(disabled["credential"]["configured"])
        enabled = self.svc.set_personal_channel_instance_active(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"], active=True,
            expected_version=disabled["version"], recent_password=self.MEMBER_PW)
        self.assertTrue(enabled["active"])

    def test_a_governance_stop_outranks_the_owners_enable(self):
        created = self._create(self.alice)
        self.svc.set_personal_instance_governance(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            instance_id=created["id"], disabled=True,
            recent_password=self.ROOT_PW, reason="policy")
        row = self._instance_row(created["id"])
        _expect_error(
            self, "governance_disabled", 403,
            self.svc.set_personal_channel_instance_active,
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"], active=True,
            expected_version=row["version"], recent_password=self.MEMBER_PW)
        # Lifting the stop does not turn it back on — the owner has to say so.
        self.svc.set_personal_instance_governance(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            instance_id=created["id"], disabled=False,
            recent_password=self.ROOT_PW)
        still_off = self.svc.get_personal_channel_instance(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"])
        self.assertFalse(still_off["active"])
        back = self.svc.set_personal_channel_instance_active(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"], active=True,
            expected_version=still_off["version"],
            recent_password=self.MEMBER_PW)
        self.assertTrue(back["active"])

    def test_a_governance_stop_also_blocks_a_credential_rotation(self):
        created = self._create(self.alice)
        self.svc.set_personal_instance_governance(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            instance_id=created["id"], disabled=True,
            recent_password=self.ROOT_PW)
        row = self._instance_row(created["id"])
        _expect_error(
            self, "governance_disabled", 403,
            self.svc.update_personal_channel_instance,
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"], expected_version=row["version"],
            credentials={"feishu_app_secret": "resurrected"},
            recent_password=self.MEMBER_PW)

    def test_the_owner_listing_shows_the_governance_flag(self):
        created = self._create(self.alice)
        self.svc.set_personal_instance_governance(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            instance_id=created["id"], disabled=True,
            recent_password=self.ROOT_PW)
        listed = self.svc.list_personal_channel_instances(
            actor_user_id=self.alice, tenant_id=self.ta)
        self.assertTrue(listed["items"][0]["governance_disabled"])
        # …and nothing about who stopped it.
        self.assertNotIn("governance_disabled_by",
                         json.dumps(listed["items"][0], default=str))

    def test_a_saved_instance_reports_a_failed_connection_separately(self):
        from channel.web import web_channel

        ctx = _FakeCtx(self.alice, self.ta)
        payload = json.dumps({
            "channel_type": "feishu", "display_name": "My Bot",
            "agent_id": self.alice_own, "credentials": dict(FEISHU_BUNDLE),
            "recent_password": self.MEMBER_PW,
        }).encode()
        with _handler_scope(web_channel, ctx, self.svc), \
                patch("channel.channel_instances.apply_tenant_instance_runtime",
                      side_effect=RuntimeError("vendor refused the bot token")), \
                patch.object(web_channel.web, "header"), \
                patch.object(web_channel.web, "data", return_value=payload):
            body = json.loads(web_channel.PersonalChannelHandler().POST())
        self.assertEqual(body["status"], "success")
        self.assertFalse(body["runtime"]["applied"])
        self.assertTrue(body["runtime"]["pending"])
        self.assertIn("vendor refused", body["runtime"]["error"])
        # The instance really is stored, so "saved" is not a lie.
        self.assertEqual(
            self.svc.list_personal_channel_instances(
                actor_user_id=self.alice, tenant_id=self.ta)["total"], 1)

    def test_the_runtime_report_is_not_a_second_save(self):
        """A failed connection must not make the console retry a committed write."""
        from channel.web import web_channel

        created = self._create(self.alice)
        with patch("channel.channel_instances.apply_tenant_instance_runtime",
                   return_value={"applied": False, "pending": True,
                                 "error": "reconnect failed: invalid app secret"}):
            report = web_channel._apply_personal_channel_runtime(created["id"])
        self.assertFalse(report["applied"])
        self.assertEqual(self.svc.list_personal_channel_instances(
            actor_user_id=self.alice, tenant_id=self.ta)["total"], 1)

    def test_a_connection_failure_is_diagnosable_not_silent(self):
        from channel.channel_instances import apply_tenant_instance_runtime

        created = self._create(self.alice)
        with patch("auth.service.get_identity_service", return_value=self.svc), \
                _personal_runtime_on("feishu"), \
                patch("channel.channel_instances._runtime_manager",
                      return_value=_ExplodingManager()):
            result = apply_tenant_instance_runtime(created["id"])
        self.assertFalse(result["applied"])
        self.assertIn("refused", result["error"])

    def test_the_execution_switch_keeps_an_unaccepted_type_disconnected(self):
        """7.5 — no real acceptance recorded means no live connection.

        The instance is saved and inspectable, but the vendor connection stays
        closed and the report says *pending*, not "connected": a type whose real
        inbound boundary has not been verified must not route a member's private
        conversations.
        """
        from channel.channel_instances import apply_tenant_instance_runtime

        created = self._create(self.alice)
        with patch("auth.service.get_identity_service", return_value=self.svc), \
                patch("channel.channel_instances._runtime_manager",
                      return_value=_ExplodingManager()):
            result = apply_tenant_instance_runtime(created["id"])
        self.assertFalse(result["applied"])
        self.assertTrue(result["pending"])
        self.assertIn("not enabled", result["error"])

    def test_a_disabled_instance_reports_nothing_to_run(self):
        from channel.channel_instances import apply_tenant_instance_runtime

        created = self._create(self.alice)
        disabled = self.svc.set_personal_channel_instance_active(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"], active=False,
            expected_version=created["version"], recent_password=self.MEMBER_PW)
        with patch("auth.service.get_identity_service", return_value=self.svc):
            result = apply_tenant_instance_runtime(created["id"])
        self.assertTrue(result["applied"])
        self.assertFalse(disabled["active"])


class IntegrationTests(_Fixture):
    """6.8 — concurrency, rollback and quota under the real service."""

    def test_two_pages_saving_the_same_version_produce_one_winner(self):
        created = self._create(self.alice)
        outcomes = []
        lock = threading.Lock()

        def _save(name):
            try:
                self.svc.update_personal_channel_instance(
                    actor_user_id=self.alice, tenant_id=self.ta,
                    instance_id=created["id"],
                    expected_version=created["version"],
                    display_name=name, recent_password=self.MEMBER_PW)
                with lock:
                    outcomes.append((name, "ok"))
            except IdentityServiceError as e:
                with lock:
                    outcomes.append((name, e.code))

        threads = [threading.Thread(target=_save, args=(name,))
                   for name in ("FIRST-PAGE", "SECOND-PAGE")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([o[1] for o in outcomes].count("ok"), 1, outcomes)
        self.assertEqual([o[1] for o in outcomes].count("conflict"), 1, outcomes)
        row = self._instance_row(created["id"])
        self.assertIn(row["display_name"], ("FIRST-PAGE", "SECOND-PAGE"))

    def test_a_failed_audit_rolls_the_whole_create_back(self):
        from auth.service import IdentityService as _Svc

        with patch.object(_Svc, "_audit_in_tx",
                          side_effect=RuntimeError("audit store down")):
            with self.assertRaises(RuntimeError):
                self._create(self.alice)
        self.assertEqual(self._credential_rows(self.ta), [])
        self.assertEqual(self.svc.list_personal_channel_instances(
            actor_user_id=self.alice, tenant_id=self.ta)["total"], 0)

    def test_a_quota_refusal_leaves_no_partial_row(self):
        self.svc.set_tenant_channel_policy(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            personal_enabled=True, allowed_types=[],
            personal_instance_limit=1, tenant_personal_instance_limit=-1,
            recent_password=self.ROOT_PW)
        self._create(self.alice)
        _expect_error(self, "quota_exceeded", 403, self._create, self.alice,
                      display_name="Second Bot",
                      credentials=dict(FEISHU_BUNDLE,
                                       feishu_app_id="cli_personal_b"))
        rows = [dict(r) for r in self.svc._store.execute(
            "SELECT scope, owner_user_id FROM tenant_channel_instances")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(self._credential_rows(self.ta)), 1)

    def test_a_disabled_type_cannot_be_re_enabled_after_the_policy_narrows(self):
        created = self._create(self.alice)
        disabled = self.svc.set_personal_channel_instance_active(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"], active=False,
            expected_version=created["version"], recent_password=self.MEMBER_PW)
        self.svc.set_tenant_channel_policy(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            personal_enabled=True, allowed_types=["telegram"],
            personal_instance_limit=-1, tenant_personal_instance_limit=-1,
            recent_password=self.ROOT_PW)
        _expect_error(
            self, "channel_type_not_allowed", 403,
            self.svc.set_personal_channel_instance_active,
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"], active=True,
            expected_version=disabled["version"], recent_password=self.MEMBER_PW)

    def test_an_unlinked_route_stops_resolving_its_sender(self):
        """解绑失效: after unlinking, the sender no longer maps to this route."""
        created = self._create(self.alice)
        challenge = self.svc.start_personal_channel_binding(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"])
        self.svc.redeem_personal_channel_challenge(
            tenant_id=self.ta, instance_id=created["id"], code=challenge["code"],
            provider="feishu", issuer="cli_personal_a", subject="ou_alice")
        self.assertIsNotNone(self.svc.personal_channel_link(
            tenant_id=self.ta, user_id=self.alice, instance_id=created["id"]))
        self.svc.unlink_personal_channel_instance(
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=created["id"])
        self.assertIsNone(self.svc.personal_channel_link(
            tenant_id=self.ta, user_id=self.alice, instance_id=created["id"]))

    def test_the_public_route_policies_are_unchanged(self):
        from auth.http_policy import ROUTE_POLICY

        self.assertEqual(ROUTE_POLICY["/api/tenant/channels"]["POST"]["policy"],
                         "tenant")
        self.assertEqual(
            ROUTE_POLICY["/api/tenant/channels/([^/]+)"]["POST"]["policy"],
            "tenant")
        self.assertEqual(
            ROUTE_POLICY["/api/tenant/channels/([^/]+)/active"]["POST"]["policy"],
            "tenant")
        for path in ("/api/personal/channels", "/api/personal/channels/([^/]+)"):
            self.assertEqual(ROUTE_POLICY[path]["GET"]["policy"], "personal")
            self.assertEqual(ROUTE_POLICY[path]["POST"]["policy"], "personal")


class WorkbenchProjectionTests(_Fixture):
    """2.3 — one list response is enough to decide what the page may offer.

    Every input to that decision is server state, so the test asks the service
    the same question the console does and asserts on a *projection*, never on a
    role: a member who bypasses the page must still meet the write rules.
    """

    def test_both_shapes_of_a_private_target_are_offered_and_nothing_else(self):
        options = self.svc.personal_channel_workspace(
            actor_user_id=self.alice, tenant_id=self.ta)["agent_options"]
        self.assertEqual([o["id"] for o in options],
                         [self.alice_assistant, self.alice_off, self.alice_own])
        # The system-supplied assistant is labelled as such so the picker can
        # group it, but it is a candidate on exactly the same ownership terms as
        # a self-built one.
        by_id = {o["id"]: o for o in options}
        self.assertTrue(by_id[self.alice_assistant]["is_system_assistant"])
        self.assertFalse(by_id[self.alice_own]["is_system_assistant"])
        self.assertEqual(set(options[0]),
                         {"id", "name", "is_system_assistant", "enabled"})

    def test_a_shared_or_foreign_agent_is_never_a_candidate(self):
        ids = [o["id"] for o in self.svc.personal_channel_workspace(
            actor_user_id=self.alice, tenant_id=self.ta)["agent_options"]]
        self.assertNotIn("agent-a", ids, "a tenant-shared Agent is not personal")
        self.assertNotIn(self.bob_own, ids, "another member's private Agent")
        self.assertNotIn(self.admin_b_own, ids, "another tenant's Agent")

    def test_an_administrator_gets_only_their_own_candidates(self):
        """Being a control user must not widen the *personal* candidate list."""
        options = self.svc.personal_channel_workspace(
            actor_user_id=self.admin_b, tenant_id=self.tb)["agent_options"]
        self.assertEqual([o["id"] for o in options], [self.admin_b_own])

    def test_a_disabled_target_is_offered_as_disabled_not_hidden(self):
        """It is still *theirs*, so the picker can say which one to switch back on."""
        options = {o["id"]: o for o in self.svc.personal_channel_workspace(
            actor_user_id=self.alice, tenant_id=self.ta)["agent_options"]}
        self.assertIn("alice-off", options)
        self.assertFalse(options["alice-off"]["enabled"])
        self.assertTrue(options[self.alice_own]["enabled"])
        self.assertTrue(options[self.alice_assistant]["enabled"])

    def test_the_create_verdict_is_closed_with_a_reason_when_targets_are_unusable(self):
        """``bob`` owns a target, so narrowing his roster is what closes create."""
        workspace = self.svc.personal_channel_workspace(
            actor_user_id=self.bob, tenant_id=self.ta)
        self.assertTrue(workspace["actions"]["create"])
        self.assertEqual(workspace["create_unavailable_reason"], "")
        # Disabling every target the member owns closes the entry point with
        # "you have no usable assistant", not with an empty candidate list that
        # reads like a loading failure.
        with patch.object(self.svc, "_agent_enabled", lambda agent_id: False):
            workspace = self.svc.personal_channel_workspace(
                actor_user_id=self.bob, tenant_id=self.ta)
        self.assertFalse(workspace["actions"]["create"])
        self.assertEqual(workspace["create_unavailable_reason"], "no_agent")
        self.assertTrue(workspace["agent_options"],
                        "the candidates are still listed, so the member can be told"
                        " *which* of their assistants to switch back on")
        self.assertFalse(any(o["enabled"] for o in workspace["agent_options"]))

    def test_a_withdrawn_tenant_policy_closes_create_with_its_own_reason(self):
        self.svc.set_tenant_channel_policy(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            recent_password=self.ROOT_PW, personal_enabled=False)
        workspace = self.svc.personal_channel_workspace(
            actor_user_id=self.alice, tenant_id=self.ta)
        self.assertFalse(workspace["actions"]["create"])
        self.assertEqual(workspace["create_unavailable_reason"],
                         "personal_access_disabled")

    def test_the_type_list_is_the_declarations_narrowed_by_the_tenant(self):
        from common import const

        everything = self.svc.personal_channel_workspace(
            actor_user_id=self.alice, tenant_id=self.ta)["channel_types"]
        self.assertIn(const.FEISHU,
                      [t["channel_type"] for t in everything if t["ready"]])
        self.svc.set_tenant_channel_policy(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            recent_password=self.ROOT_PW, allowed_types=[const.DINGTALK])
        narrowed = self.svc.personal_channel_workspace(
            actor_user_id=self.alice, tenant_id=self.ta)["channel_types"]
        ready = {t["channel_type"]: t for t in narrowed if t["ready"]}
        self.assertEqual(set(ready), {const.DINGTALK})
        feishu = [t for t in narrowed if t["channel_type"] == const.FEISHU][0]
        self.assertFalse(feishu["ready"])
        self.assertEqual(feishu["reason"], "channel_type_not_allowed")
        # The *deployment* verdict is still the first word: a type the process
        # has not declared ready stays unready however the tenant list reads.
        self.assertEqual([t["channel_type"] for t in narrowed
                          if t["ready"] and t["reason"]], [])

    def test_the_quota_projection_counts_what_the_write_will_count(self):
        self.svc.set_tenant_channel_policy(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            recent_password=self.ROOT_PW, personal_instance_limit=1)
        before = self.svc.personal_channel_workspace(
            actor_user_id=self.alice, tenant_id=self.ta)
        self.assertEqual(before["quota"]["owner_remaining"], 1)
        self.assertTrue(before["actions"]["create"])
        self._create(self.alice)
        after = self.svc.personal_channel_workspace(
            actor_user_id=self.alice, tenant_id=self.ta)
        self.assertEqual(after["quota"]["owner_used"], 1)
        self.assertEqual(after["quota"]["owner_remaining"], 0)
        self.assertFalse(after["actions"]["create"])
        self.assertEqual(after["create_unavailable_reason"], "quota_exceeded")
        # The member's own count is not another member's: the projection is
        # scoped by owner, and bob is unaffected by alice's exhausted allowance.
        self.assertTrue(self.svc.personal_channel_workspace(
            actor_user_id=self.bob, tenant_id=self.ta)["actions"]["create"])

    def test_the_projection_needs_no_management_page_permission(self):
        """A plain member reaches it; nothing here is gated on ``admin.*``."""
        workspace = self.svc.personal_channel_workspace(
            actor_user_id=self.alice, tenant_id=self.ta)
        self.assertEqual(set(workspace),
                         {"agent_options", "channel_types", "quota", "actions",
                          "create_unavailable_reason"})
        self.assertFalse(self.svc._is_control(self.alice, self.ta))

    def test_a_non_member_gets_no_workspace_at_all(self):
        _expect_error(
            self, "forbidden", 403, self.svc.personal_channel_workspace,
            actor_user_id="stranger", tenant_id=self.ta)


class WriteBoundaryTargetTests(_Fixture):
    """2.1/2.2 — one target predicate, enforced on every path that opens service."""

    def test_an_empty_target_is_a_malformed_write_not_a_default(self):
        _expect_error(self, "personal_agent_required", 400,
                      self._create, self.alice, agent_id="")
        # The same verdict on the shared write path, which a scan submit reaches
        # directly — the wrapper is not where the rule lives.
        _expect_error(
            self, "personal_agent_required", 400,
            self.svc.create_tenant_channel_instance,
            actor_user_id=self.root["id"], tenant_id=self.ta,
            channel_type="feishu", display_name="No Target", agent_id="",
            credentials=dict(FEISHU_BUNDLE), recent_password=self.ROOT_PW,
            scope="user", owner_user_id=self.alice)

    def test_a_shared_agent_is_refused_as_a_personal_target(self):
        _expect_error(self, "personal_agent_forbidden", 403,
                      self._create, self.alice, agent_id="agent-a")

    def test_another_members_private_agent_is_refused(self):
        _expect_error(self, "personal_agent_forbidden", 403,
                      self._create, self.alice, agent_id=self.bob_own)

    def test_a_cross_tenant_target_is_refused(self):
        """Alice owns an Agent in Globex; that is not a target in Acme."""
        caught = _expect_error(
            self, "forbidden", 403, self._create, self.alice,
            agent_id=self.alice_globex)
        self.assertNotIn(self.alice_globex, str(caught))

    def test_no_target_shape_is_probeable_through_the_refusal(self):
        """Whatever the shape, the refusal discloses nothing about the object.

        The *codes* differ — a foreign tenant is caught by the tenant check the
        public path shares, a foreign member by the personal predicate — but
        neither message names the Agent, its owner, or whether it exists.
        """
        for agent_id in ("agent-does-not-exist", self.bob_own, self.admin_b_own):
            with self.subTest(target=agent_id):
                with self.assertRaises(IdentityServiceError) as caught:
                    self._create(self.alice, agent_id=agent_id)
                self.assertEqual(caught.exception.status, 403)
                message = str(caught.exception)
                self.assertNotIn(agent_id, message)
                self.assertNotIn(self.bob, message)

    def test_a_disabled_target_is_its_own_reason(self):
        """The member owns it, so "switch it back on" is sayable."""
        _expect_error(self, "personal_agent_disabled", 403,
                      self._create, self.alice, agent_id="alice-off")

    def test_the_shared_write_path_enforces_it_too(self):
        """The wrapper is not the boundary: ``scope='user'`` creation is direct."""
        _expect_error(
            self, "personal_agent_forbidden", 403,
            self.svc.create_tenant_channel_instance,
            actor_user_id=self.root["id"], tenant_id=self.ta,
            channel_type="feishu", display_name="Sneaky Personal",
            agent_id="agent-a", credentials=dict(FEISHU_BUNDLE),
            recent_password=self.ROOT_PW, scope="user", owner_user_id=self.alice)

    def test_a_repair_cannot_move_ownership_to_the_operator(self):
        created = self._create(self.alice)
        _expect_error(
            self, "forbidden", 403, self.svc.update_personal_channel_instance,
            actor_user_id=self.root["id"], tenant_id=self.ta,
            instance_id=created["id"], expected_version=created["version"],
            agent_id=self.bob_own, recent_password=self.ROOT_PW)

    def test_a_target_still_may_not_be_kept_once_it_stops_being_yours(self):
        """A pure rename keeps the target — but a write that *reselects* may not.

        The failure refuses only the verbs that put the instance back into
        service. What must never happen is the stale "it was selectable when the
        page loaded" fact being treated as the authorization.
        """
        created = self._create(self.alice)
        disabled = self.svc.set_personal_channel_instance_active(
            actor_user_id=self.alice, tenant_id=self.ta, instance_id=created["id"],
            active=False, expected_version=created["version"],
            recent_password=self.MEMBER_PW)
        version = disabled["version"]
        # The Agent the member selected is turned tenant-shared afterwards, by
        # the same operator action production uses.
        self.svc.make_agent_tenant_shared(
            agent_id=self.alice_own, actor_user_id=self.root["id"])
        for label, call in (
            ("rotate", {"credentials": dict(FEISHU_BUNDLE,
                                            feishu_app_secret="rotated")}),
            ("retarget-to-shared", {"agent_id": "agent-a"}),
        ):
            with self.subTest(action=label):
                _expect_error(
                    self, "personal_agent_forbidden", 403,
                    self.svc.update_personal_channel_instance,
                    actor_user_id=self.alice, tenant_id=self.ta,
                    instance_id=created["id"], expected_version=version,
                    recent_password=self.MEMBER_PW, **call)
        _expect_error(
            self, "personal_agent_forbidden", 403,
            self.svc.set_personal_channel_instance_active,
            actor_user_id=self.alice, tenant_id=self.ta, instance_id=created["id"],
            active=True, expected_version=version, recent_password=self.MEMBER_PW)
        row = self._instance_row(created["id"])
        self.assertEqual(row["agent_id"], self.alice_own)
        self.assertEqual(row["version"], version,
                         "no refusal may have moved the version")
        self.assertEqual(row["active"], 0)
        # A plain rename still works, so the row is not frozen by its bad target.
        renamed = self.svc.update_personal_channel_instance(
            actor_user_id=self.alice, tenant_id=self.ta, instance_id=created["id"],
            expected_version=version, display_name="Renamed",
            recent_password=self.MEMBER_PW)
        self.assertEqual(renamed["display_name"], "Renamed")

    def test_a_refused_target_write_leaves_no_trace(self):
        self.assertEqual(self._credential_rows(self.ta), [])
        _expect_error(self, "personal_agent_forbidden", 403,
                      self._create, self.alice, agent_id=self.bob_own)
        self.assertEqual(self._credential_rows(self.ta), [])
        self.assertEqual(self.svc.list_personal_channel_instances(
            actor_user_id=self.alice, tenant_id=self.ta)["total"], 0)


class TargetStatusAndRepairTests(_Fixture):
    """2.5/5.1 — an unusable target is *shown and fixable*, never silently swapped."""

    def _legacy_instance(self, owner, agent_id="", *, tenant_id=None,
                         display_name="Legacy Bot", active=1):
        """Seed a row the way an *older* client could have left it.

        Written straight to storage on purpose: every current write path refuses
        an empty target, so a fixture that went through the service could not
        produce the very rows this class is about.
        """
        tenant_id = tenant_id or self.ta
        instance_id = "ci_legacy_%s_%s" % (owner[-6:], display_name.replace(" ", "_"))
        self.svc._store.execute(
            "INSERT INTO tenant_channel_instances(id, tenant_id, channel_type,"
            " display_name, agent_id, active, scope, owner_user_id, created_by)"
            " VALUES(?,?,?,?,?,?,'user',?,?)",
            (instance_id, tenant_id, "feishu", display_name, agent_id, active,
             owner, owner))
        return instance_id

    def test_a_healthy_target_reads_as_ok(self):
        created = self._create(self.alice)
        target = created["target"]
        self.assertEqual(target["state"], "ok")
        self.assertEqual(target["agent_id"], self.alice_own)
        self.assertTrue(target["enabled"])
        self.assertFalse(created["actions"]["repair_target"])

    def test_a_legacy_empty_target_is_reported_as_missing_and_repairable(self):
        instance_id = self._legacy_instance(self.alice, "")
        view = self.svc.get_personal_channel_instance(
            actor_user_id=self.alice, tenant_id=self.ta, instance_id=instance_id)
        self.assertEqual(view["target"]["state"], "missing")
        self.assertEqual(view["target"]["reason"], "personal_agent_required")
        self.assertTrue(view["actions"]["repair_target"])
        # Repairing is the *only* opening verb offered; the row is still closable.
        self.assertFalse(view["actions"]["enable"])
        self.assertFalse(view["actions"]["bind"])
        self.assertTrue(view["actions"]["edit"])

    def test_a_legacy_shared_target_is_flagged_without_disclosing_it(self):
        """The old row keeps its id; the projection names nothing it may not."""
        instance_id = self._legacy_instance(self.alice, self.bob_own)
        view = self.svc.get_personal_channel_instance(
            actor_user_id=self.alice, tenant_id=self.ta, instance_id=instance_id)
        self.assertEqual(view["target"]["state"], "invalid")
        self.assertEqual(view["target"]["agent_id"], self.bob_own)
        self.assertEqual(view["target"]["name"], "",
                         "an unusable target's name is never projected")
        self.assertEqual(self._instance_row(instance_id)["agent_id"], self.bob_own,
                         "the row is left exactly as stored: never auto-reassigned")

    def test_a_legacy_row_can_be_repaired_but_only_by_its_owner(self):
        instance_id = self._legacy_instance(self.alice, "")
        repaired = self.svc.update_personal_channel_instance(
            actor_user_id=self.alice, tenant_id=self.ta, instance_id=instance_id,
            expected_version=1, agent_id=self.alice_own,
            recent_password=self.MEMBER_PW)
        self.assertEqual(repaired["target"]["state"], "ok")
        self.assertEqual(self._instance_row(instance_id)["agent_id"], self.alice_own)
        self.assertEqual(self._instance_row(instance_id)["id"], instance_id,
                         "repairing is an update: the instance id is preserved")

    def test_an_invalid_target_withholds_enable_and_binding_but_not_closing(self):
        instance_id = self._legacy_instance(self.alice, "", active=0)
        _expect_error(
            self, "personal_agent_required", 400,
            self.svc.set_personal_channel_instance_active,
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=instance_id, active=True, expected_version=1,
            recent_password=self.MEMBER_PW)
        _expect_error(
            self, "personal_agent_required", 400,
            self.svc.start_personal_channel_binding,
            actor_user_id=self.alice, tenant_id=self.ta,
            instance_id=instance_id, expected_version=1)
        # Disabling and unlinking stay reachable, so a member is never trapped.
        disabled = self.svc.set_personal_channel_instance_active(
            actor_user_id=self.alice, tenant_id=self.ta, instance_id=instance_id,
            active=False, expected_version=1, recent_password=self.MEMBER_PW)
        self.assertFalse(disabled["active"])

    def test_a_disabled_target_withholds_rotation_and_reports_why(self):
        created = self._create(self.alice)
        with patch.object(self.svc, "_agent_enabled", lambda agent_id: False):
            view = self.svc.get_personal_channel_instance(
                actor_user_id=self.alice, tenant_id=self.ta,
                instance_id=created["id"])
        self.assertEqual(view["target"]["state"], "disabled")
        self.assertEqual(view["target"]["reason"], "personal_agent_disabled")
        self.assertFalse(view["actions"]["enable"])
        self.assertTrue(view["actions"]["repair_target"])
        # Rotation would put the instance back into service with a target that
        # cannot run, so it is refused; the closing verbs are not.
        with patch.object(self.svc, "_agent_enabled", lambda agent_id: False):
            _expect_error(
                self, "personal_agent_disabled", 403,
                self.svc.update_personal_channel_instance,
                actor_user_id=self.alice, tenant_id=self.ta,
                instance_id=created["id"], expected_version=created["version"],
                credentials=dict(FEISHU_BUNDLE,
                                 feishu_app_secret="rotated-secret"),
                recent_password=self.MEMBER_PW)

    def test_the_runtime_refuses_to_start_an_instance_with_an_unusable_target(self):
        """Startup and the hot path share the gate, so a restart cannot re-open it."""
        from channel import channel_instances

        connected = []

        class _Manager:
            def restart(self, inst):
                connected.append(inst.instance_id)

            def remove_channel(self, instance_id):
                pass

        instance_id = self._legacy_instance(self.alice, "")
        with _personal_runtime_on("feishu"), \
                patch("auth.service.get_identity_service", return_value=self.svc), \
                patch.object(channel_instances, "_runtime_manager",
                             staticmethod(lambda: _Manager())):
            state = channel_instances.apply_tenant_instance_runtime(instance_id)
        self.assertFalse(state["applied"])
        self.assertIn("personal_agent_required", state["error"])
        self.assertEqual(connected, [],
                         "an unusable target must not be connected at all")

    def test_the_startup_synthesis_applies_the_same_target_gate(self):
        """A restart must not re-open what the console reports as needing repair."""
        from channel import channel_instances

        with patch("auth.service.get_identity_service",
                   return_value=self.svc), _personal_runtime_on("feishu"):
            healthy = self._create(self.alice)
            broken = self._legacy_instance(self.alice, "")
            loaded = channel_instances.load_tenant_channel_instances()
        loaded_ids = [inst.instance_id for inst in loaded]
        self.assertIn(healthy["id"], loaded_ids)
        self.assertNotIn(broken, loaded_ids)

    def test_the_runtime_projection_separates_saved_from_connected(self):
        created = self._create(self.alice)
        # Runtime is closed for every personal type in production, so the honest
        # reading is "saved, not connected" — never "connected" from active=true.
        self.assertEqual(created["runtime"]["state"], "saved")
        self.assertEqual(created["runtime"]["reason"], "runtime_not_open")
        self.assertFalse(created["runtime"]["connected"])
        self.assertFalse(created["runtime"]["talkable"])
        view = self._runtime_view(
            created["id"], {"applied": False, "pending": True, "error": ""})
        self.assertEqual(view["runtime"]["state"], "connecting")
        self.assertFalse(view["runtime"]["connected"])
        self.assertFalse(view["runtime"]["talkable"])

    def test_a_connected_but_unlinked_instance_is_not_talkable(self):
        """连接成功但本人未关联：可用性不能用 active 或扫码结果合成."""
        created = self._create(self.alice)
        view = self._runtime_view(
            created["id"], {"applied": True, "pending": False, "error": ""})
        self.assertEqual(view["runtime"]["state"], "connected")
        self.assertTrue(view["runtime"]["connected"])
        self.assertFalse(view["runtime"]["linked"])
        self.assertFalse(view["runtime"]["talkable"],
                         "a connection without the member's own link cannot talk")

    def test_a_connection_error_is_reported_rather_than_flattened(self):
        created = self._create(self.alice)
        view = self._runtime_view(
            created["id"], {"applied": False, "pending": False,
                            "error": "vendor refused the bot token"})
        self.assertEqual(view["runtime"]["state"], "failed")
        self.assertEqual(view["runtime"]["reason"], "connect_failed")
        self.assertIn("refused", view["runtime"]["error"])

    def _runtime_view(self, instance_id, observed):
        """The instance projection over a *given* runtime observation.

        ``instance_runtime_state`` is the runtime's own report, so pinning it is
        how these tests isolate the projection: the mapping from "what the
        runtime last saw" to "what the member is told" is the contract under
        test, not the manager's behaviour.
        """
        with _personal_runtime_on("feishu"), patch(
                "channel.channel_instances.instance_runtime_state",
                return_value=dict(observed)):
            return self.svc.get_personal_channel_instance(
                actor_user_id=self.alice, tenant_id=self.ta,
                instance_id=instance_id)


class _FakeCtx:
    def __init__(self, user_id, tenant_id):
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.is_platform_admin = False
        self.must_change_password = False
        self.permissions = frozenset({"chat.use"})


class _FakeScope:
    def __init__(self, ctx):
        self.ctx = ctx

    def __enter__(self):
        return self.ctx

    def __exit__(self, *exc):
        return False


def _handler_scope(web_channel, ctx, service):
    return _ComposedPatch(
        patch.object(web_channel, "_db_scope", lambda: _FakeScope(ctx)),
        patch.object(web_channel, "_personal_channel_service",
                     lambda: service))


class _ComposedPatch:
    """Two patches entered as one ``with`` (keeps the test's intent readable)."""

    def __init__(self, *patches):
        self._patches = patches

    def __enter__(self):
        return [p.__enter__() for p in self._patches]

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.__exit__(*exc)
        return False


class _ExplodingManager:
    def restart(self, instance):
        raise RuntimeError("vendor refused the bot token")


if __name__ == "__main__":
    unittest.main()
