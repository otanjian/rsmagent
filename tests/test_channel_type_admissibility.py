# encoding:utf-8
"""Admissibility gates, including a simulated deployment missing adapters.

All master types now stamp identity. The gate tests deliberately withdraw the
new adapters to retain coverage of refusal, repair and re-enable boundaries.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from auth.service import IdentityService, IdentityServiceError
from channel.channel_instances import (
    CREDENTIAL_KEYS,
    INBOUND_IDENTITY_STAMPING_TYPES,
    MULTI_INSTANCE_READY,
    PERSONAL_NOT_READY_REASONS,
    inbound_identity_admissible,
    personal_channel_ready,
    personal_channel_types,
    public_personal_ingress_ready,
    tenant_channel_types,
)

MASTER_KEY = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"

#: Every type the catalogue offers, with a bundle the *credential* rules accept.
#: A refusal must therefore come from the admissibility guard and not from a
#: malformed bundle, or the test would pass for the wrong reason.
FEISHU_BUNDLE = {"feishu_app_id": "cli_admin_a", "feishu_app_secret": "s3cr3t"}
DINGTALK_BUNDLE = {
    "dingtalk_client_id": "ding_admin_a",
    "dingtalk_client_secret": "s3cr3t",
    "dingtalk_robot_code": "robot_adm",
}
WECOM_BUNDLE = {"wecom_bot_id": "bot_adm", "wecom_bot_secret": "s3cr3t"}
SLACK_BUNDLE = {"slack_bot_token": "xoxb-1", "slack_app_token": "xapp-1"}
TELEGRAM_BUNDLE = {"telegram_token": "123:abc"}
DISCORD_BUNDLE = {"discord_token": "s3cr3t"}
QQ_BUNDLE = {"qq_app_id": "app_adm", "qq_app_secret": "s3cr3t"}
WEIXIN_BUNDLE = {"weixin_token": "wx-token-adm"}

#: Types whose adapter never stamps — the defect.
UNSTAMPED_BUNDLES = {
    "slack": SLACK_BUNDLE,
    "telegram": TELEGRAM_BUNDLE,
    "discord": DISCORD_BUNDLE,
    "qq": QQ_BUNDLE,
    "weixin": WEIXIN_BUNDLE,
}
#: The negative control: these must keep working.
STAMPING_BUNDLES = {
    "feishu": FEISHU_BUNDLE,
    "dingtalk": DINGTALK_BUNDLE,
    "wecom_bot": WECOM_BUNDLE,
}


def _db_path():
    return os.path.join(tempfile.mkdtemp(), "identity.db")


class DeclarationTests(unittest.TestCase):
    """The declaration itself: one set, and a predicate derived from it."""

    def test_the_stamping_set_covers_every_master_adapter(self):
        self.assertEqual(INBOUND_IDENTITY_STAMPING_TYPES,
                         {"feishu", "dingtalk", "wecom_bot", "weixin", "qq",
                          "telegram", "slack", "discord", "wechatcom_app",
                          "wechat_kf", "wechatmp"})

    def test_the_stamping_set_is_a_subset_of_what_can_run_instances(self):
        # The two declarations answer different questions, and both have to hold
        # for a type to be configurable: a stamping adapter that cannot run per
        # tenant is still not ownable.
        self.assertTrue(INBOUND_IDENTITY_STAMPING_TYPES <= MULTI_INSTANCE_READY)
        for channel_type in INBOUND_IDENTITY_STAMPING_TYPES:
            self.assertTrue(CREDENTIAL_KEYS.get(channel_type), channel_type)

    def test_the_predicate_admits_exactly_the_stamping_types(self):
        for channel_type in sorted(MULTI_INSTANCE_READY):
            self.assertEqual(inbound_identity_admissible(channel_type),
                             channel_type in INBOUND_IDENTITY_STAMPING_TYPES,
                             channel_type)

    def test_the_predicate_is_mode_aware_so_a_non_database_path_is_not_narrowed(self):
        # Production is database-only today, so this is the "self-disables if the
        # mode ever changes" half: the requirement is "database identity mode has
        # to prove the sender", not "these types are bad".
        with patch("channel.external_identity.is_database_mode", lambda: False):
            self.assertTrue(inbound_identity_admissible("slack"))
            self.assertEqual(personal_channel_ready("slack"), (True, ""))
        # ...and the default is the narrowed one, so the test above is not
        # passing because the predicate ignores the mode entirely.
        with patch("channel.channel_instances.INBOUND_IDENTITY_STAMPING_TYPES",
                   frozenset({"feishu", "dingtalk", "wecom_bot"})):
            self.assertFalse(inbound_identity_admissible("slack"))

    def test_the_new_reason_code_is_a_declared_one(self):
        self.assertIn("no_inbound_identity", PERSONAL_NOT_READY_REASONS)
        for channel_type in sorted(MULTI_INSTANCE_READY):
            ready, reason = personal_channel_ready(channel_type)
            self.assertEqual(ready, reason == "", channel_type)
            self.assertIn(reason, PERSONAL_NOT_READY_REASONS | {""}, channel_type)


@patch("channel.channel_instances.INBOUND_IDENTITY_STAMPING_TYPES",
       frozenset({"feishu", "dingtalk", "wecom_bot"}))
class CatalogueTests(unittest.TestCase):
    """The catalogue serves candidates *and* the field contract; keep both."""

    def test_the_personal_verdict_names_the_real_blocker(self):
        for channel_type in sorted(UNSTAMPED_BUNDLES):
            self.assertEqual(personal_channel_ready(channel_type),
                             (False, "no_inbound_identity"), channel_type)
        for channel_type in sorted(STAMPING_BUNDLES):
            self.assertEqual(personal_channel_ready(channel_type), (True, ""),
                             channel_type)

    def test_the_personal_catalogue_keeps_every_entry_and_reports_the_verdict(self):
        catalog = {item["channel_type"]: item for item in personal_channel_types()}
        # No entry may disappear: the console resolves a member's existing row
        # through the same list.
        self.assertEqual(set(catalog), set(MULTI_INSTANCE_READY))
        for channel_type in sorted(UNSTAMPED_BUNDLES):
            entry = catalog[channel_type]
            self.assertFalse(entry["ready"], channel_type)
            self.assertEqual(entry["reason"], "no_inbound_identity", channel_type)
            self.assertTrue(entry["credential_fields"], channel_type)

    def test_the_tenant_catalogue_keeps_the_field_contract_of_an_inadmissible_type(self):
        catalog = {item["channel_type"]: item for item in tenant_channel_types()}
        self.assertEqual(set(catalog), set(MULTI_INSTANCE_READY))
        slack = catalog["slack"]
        # The verdict is what a candidate list narrows on...
        self.assertIs(slack["inbound_admissible"], False)
        # ...while the contract an existing Slack row's edit form needs is intact.
        self.assertEqual([f["key"] for f in slack["credential_fields"]],
                         ["slack_bot_token", "slack_app_token"])
        for channel_type in sorted(STAMPING_BUNDLES):
            self.assertIs(catalog[channel_type]["inbound_admissible"], True,
                          channel_type)

    def test_an_inadmissible_type_cannot_carry_personal_routes_either(self):
        # The personal route on a shared instance needs the same per-sender
        # proof, so readiness closing closes this with it — even with the switch
        # and the per-type acceptance record opened.
        with patch("channel.channel_instances._personal_runtime_capability_enabled",
                   lambda: True), \
                patch("channel.channel_instances.PUBLIC_PERSONAL_INGRESS_TYPES",
                      frozenset({"feishu", "slack"})):
            self.assertTrue(public_personal_ingress_ready("feishu"))
            self.assertFalse(public_personal_ingress_ready("slack"))


class _WriteFixture(unittest.TestCase):
    """One tenant, one shared Agent, a real credential master key."""

    def setUp(self):
        self._previous_key = os.environ.get("COW_CREDENTIAL_MASTER_KEY")
        os.environ["COW_CREDENTIAL_MASTER_KEY"] = MASTER_KEY
        self.addCleanup(self._restore_master_key)

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
        self.ta = self.svc.list_tenants()[0]["id"]
        self.svc.bind_agent(tenant_id=self.ta, agent_id="agent-a",
                            private_owner_user_id=None)

    def _restore_master_key(self):
        if self._previous_key is None:
            os.environ.pop("COW_CREDENTIAL_MASTER_KEY", None)
        else:
            os.environ["COW_CREDENTIAL_MASTER_KEY"] = self._previous_key

    def _create(self, channel_type, credentials, *, display_name=None):
        return self.svc.create_tenant_channel_instance(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            channel_type=channel_type,
            display_name=display_name or f"Bot {channel_type}",
            agent_id="agent-a", credentials=credentials,
            recent_password="Str0ngRootFinal")

    def _instance_rows(self):
        return [dict(r) for r in self.svc._store.execute(
            "SELECT * FROM tenant_channel_instances WHERE tenant_id=?", (self.ta,))]

    def _credential_rows(self):
        return [dict(r) for r in self.svc._store.execute(
            "SELECT * FROM credentials WHERE tenant_id=? AND resource_kind='channel'",
            (self.ta,))]

    def _audit_count(self, action):
        rows = self.svc._store.execute(
            "SELECT COUNT(*) c FROM audit_events WHERE tenant_id=? AND action=?",
            (self.ta, action))
        return int(rows[0]["c"])


@patch("channel.channel_instances.INBOUND_IDENTITY_STAMPING_TYPES",
       frozenset({"feishu", "dingtalk", "wecom_bot"}))
class MemberWriteGateTests(_WriteFixture):
    """The one gate member create *and* enable both pass through."""

    def test_the_member_gate_refuses_an_unstamped_type_and_admits_a_stamping_one(self):
        # `_enforce_personal_instance_policy` is reached by create and by
        # re-enable, so pinning it here is what makes "the fold-in closes both"
        # an observation rather than a claim. The personal capability switches
        # are a different concern with their own tests, so they are stubbed out
        # instead of being set up.
        with patch.object(self.svc, "require_personal_capability", lambda name: None):
            with self.assertRaises(IdentityServiceError) as caught:
                with self.svc._tx() as con:
                    self.svc._enforce_personal_instance_policy(
                        con, self.ta, "slack", self.root["id"])
            self.assertEqual(caught.exception.code, "channel_type_not_ready")
            # Control: the gate is not refusing everything.
            with self.svc._tx() as con:
                self.svc._enforce_personal_instance_policy(
                    con, self.ta, "feishu", self.root["id"])


@patch("channel.channel_instances.INBOUND_IDENTITY_STAMPING_TYPES",
       frozenset({"feishu", "dingtalk", "wecom_bot"}))
class CreateBackstopTests(_WriteFixture):
    """A caller posting past the console must not be able to store a dead type."""

    def test_a_public_create_of_an_unstamped_type_is_refused_and_stores_nothing(self):
        before_instances = len(self._instance_rows())
        before_credentials = len(self._credential_rows())
        before_audit = self._audit_count("channel.instance.create")
        with self.assertRaises(IdentityServiceError) as caught:
            self._create("slack", dict(SLACK_BUNDLE))
        self.assertEqual(caught.exception.code, "bad_request", str(caught.exception))
        self.assertEqual(caught.exception.status, 400)
        # Observable effects, not just the exception: no row, no orphan
        # credential, and no audit event claiming a create that did not happen.
        self.assertEqual(len(self._instance_rows()), before_instances)
        self.assertEqual(len(self._credential_rows()), before_credentials)
        self.assertEqual(self._audit_count("channel.instance.create"), before_audit)

    def test_every_unstamped_type_is_refused_and_every_stamping_type_is_not(self):
        for channel_type, bundle in sorted(UNSTAMPED_BUNDLES.items()):
            with self.assertRaises(IdentityServiceError) as caught:
                self._create(channel_type, dict(bundle))
            self.assertEqual(caught.exception.code, "bad_request", channel_type)
        # The control: "refuse everything" must not pass.
        for channel_type, bundle in sorted(STAMPING_BUNDLES.items()):
            created = self._create(channel_type, dict(bundle))
            self.assertTrue(created["id"], channel_type)
        self.assertEqual(
            sorted(r["channel_type"] for r in self._instance_rows()),
            sorted(STAMPING_BUNDLES))

    def test_an_existing_unstamped_row_stays_editable_and_rotatable(self):
        # The deliberate consequence of the backstop living in the *create*
        # branch: a row written before the fix must not become a one-way door.
        # Its owner may still rotate the credential and rename it, even though no
        # new instance of that type can be created.
        seeded = self._downgrade_to(self._create("feishu", dict(FEISHU_BUNDLE))["id"],
                                    "slack", SLACK_BUNDLE)
        rotated = self.svc.update_tenant_channel_instance(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            instance_id=seeded, expected_version=self._version(seeded),
            recent_password="Str0ngRootFinal",
            display_name="Legacy Slack",
            credentials={"slack_bot_token": "xoxb-rotated"})
        self.assertEqual(rotated["display_name"], "Legacy Slack")
        bundle = self.svc.channel_instance_credentials(self.ta, seeded)
        self.assertEqual(bundle["slack_bot_token"], "xoxb-rotated")
        self.assertEqual(bundle["slack_app_token"], "xapp-1",
                         "the field the operator did not retype was dropped")
        # ...and the create door stays shut for that same type.
        with self.assertRaises(IdentityServiceError) as caught:
            self._create("slack", dict(SLACK_BUNDLE))
        self.assertEqual(caught.exception.code, "bad_request")

    def _version(self, instance_id):
        rows = self.svc._store.execute(
            "SELECT version FROM tenant_channel_instances WHERE id=?",
            (instance_id,))
        return int(rows[0]["version"])

    def _downgrade_to(self, instance_id, channel_type, bundle):
        """Re-point a stored row at *channel_type*, as a pre-fix install would.

        The create path can no longer produce one of these (that is the fix), so
        the "row that already exists" case is reconstructed with the real store
        and a really encrypted bundle — never by stubbing the service.
        """
        import json as _json

        from auth.crypto import encrypt_secret

        ciphertext = encrypt_secret(_json.dumps(bundle, sort_keys=True))
        with self.svc._tx() as con:
            con.execute(
                "UPDATE tenant_channel_instances SET channel_type=? WHERE id=?",
                (channel_type, instance_id))
            con.execute(
                "UPDATE credentials SET ciphertext=? WHERE tenant_id=? AND name=?",
                (ciphertext, self.ta, f"channel:{instance_id}"))
            con.commit()
        return instance_id


@patch("channel.channel_instances.INBOUND_IDENTITY_STAMPING_TYPES",
       frozenset({"feishu", "dingtalk", "wecom_bot"}))
class ReEnableBackstopTests(_WriteFixture):
    """Task 7.8: the last door into the 7.7 defect, closed at the switch.

    The create backstop cannot see rows written before it existed, and the
    member path re-decides readiness while the public path did not — so an
    administrator could switch such a row back on and get an instance reported
    ``connected`` whose every inbound is refused. These tests pin the refusal
    *and* the asymmetry it rests on: stop and repair keep working, because
    stranding a live instance would be worse than the defect.
    """

    def _seed_legacy_public_row(self, channel_type, bundle, app_id):
        """Store a row the pre-fix install could have written, with real crypto.

        The create path can no longer produce one (that is the fix), so the row
        is reconstructed through the real store and a really encrypted bundle —
        never by stubbing the service.
        """
        import json as _json

        from auth.crypto import encrypt_secret

        seeded = self._create(
            "feishu", {"feishu_app_id": app_id, "feishu_app_secret": "s3cr3t"})
        ciphertext = encrypt_secret(_json.dumps(bundle, sort_keys=True))
        with self.svc._tx() as con:
            con.execute(
                "UPDATE tenant_channel_instances SET channel_type=? WHERE id=?",
                (channel_type, seeded["id"]))
            con.execute(
                "UPDATE credentials SET ciphertext=? WHERE tenant_id=? AND name=?",
                (ciphertext, self.ta, f"channel:{seeded['id']}"))
            con.commit()
        return seeded["id"]

    def _row(self, instance_id):
        rows = self.svc._store.execute(
            "SELECT * FROM tenant_channel_instances WHERE id=?", (instance_id,))
        return dict(rows[0])

    def _set_active(self, instance_id, active):
        return self.svc.set_tenant_channel_instance_active(
            actor_user_id=self.root["id"], tenant_id=self.ta,
            instance_id=instance_id, active=active,
            expected_version=int(self._row(instance_id)["version"]),
            recent_password="Str0ngRootFinal")

    def test_an_existing_inadmissible_public_row_can_still_be_stopped(self):
        # The control the guard must never break: an operator has to be able to
        # shut a running instance off, *including* one that can never work.
        for channel_type, bundle in sorted(UNSTAMPED_BUNDLES.items()):
            seeded = self._seed_legacy_public_row(
                channel_type, dict(bundle), f"cli_stop_{channel_type}")
            self.assertEqual(self._row(seeded)["active"], 1)
            before = self._audit_count("channel.instance.disable")
            self._set_active(seeded, False)
            self.assertEqual(self._row(seeded)["active"], 0, channel_type)
            self.assertEqual(self._audit_count("channel.instance.disable"),
                             before + 1, channel_type)

    def test_an_existing_inadmissible_public_row_cannot_be_re_enabled(self):
        for channel_type, bundle in sorted(UNSTAMPED_BUNDLES.items()):
            seeded = self._seed_legacy_public_row(
                channel_type, dict(bundle), f"cli_enable_{channel_type}")
            self._set_active(seeded, False)
            before = self._row(seeded)
            with self.assertRaises(IdentityServiceError) as caught:
                self._set_active(seeded, True)
            self.assertEqual(caught.exception.code, "channel_type_not_ready",
                             channel_type)
            self.assertEqual(caught.exception.status, 403, channel_type)
            # Observable effects: the switch stayed off, the row was not bumped,
            # and no audit event claims an enable that did not happen.
            after = self._row(seeded)
            self.assertEqual(after["active"], 0, channel_type)
            self.assertEqual(after["version"], before["version"], channel_type)
            self.assertEqual(self._audit_count("channel.instance.enable"), 0,
                             channel_type)

    def test_an_existing_admissible_public_row_still_re_enables(self):
        # The negative control: the guard keys on the *type*, not on "refuse
        # every re-enable", so a suite where everything is refused cannot pass.
        seeded = self._create(
            "feishu", {"feishu_app_id": "cli_admissible", "feishu_app_secret": "s3cr3t"})["id"]
        self._set_active(seeded, False)
        self._set_active(seeded, True)
        self.assertEqual(self._row(seeded)["active"], 1)
        self.assertEqual(self._audit_count("channel.instance.enable"), 1)


if __name__ == "__main__":
    unittest.main()
