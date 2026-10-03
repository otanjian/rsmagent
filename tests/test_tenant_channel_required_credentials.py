# encoding:utf-8
"""The minimum credential set a tenant channel needs before it can start.

Change ``scan-onboarding-and-inbound-anchor`` group 5. The bundle is written
encrypted and *then* handed to the live channel, so a bundle that cannot start
fails later, in a different place, as a startup error the operator has to go
looking for. Worse, the console told the operator that a blank secret field
"keeps the stored value", while a rotation replaced the whole bundle: retyping
only the secret silently dropped the app id.

These tests pin both halves: a write that would leave a required key missing is
refused before anything is stored, and a rotation of the fields the operator
actually retyped keeps the ones they did not.
"""

import os
import tempfile
import unittest

from auth.crypto import decrypt_secret
from auth.service import IdentityService, IdentityServiceError
from channel.channel_instances import (
    CREDENTIAL_KEYS,
    MULTI_INSTANCE_READY,
    REQUIRED_CREDENTIAL_KEYS,
    required_credential_keys,
    tenant_channel_types,
)

MASTER_KEY = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"

#: A complete feishu bundle, so "missing" tests differ from it in exactly one way.
FEISHU_BUNDLE = {
    "feishu_app_id": "cli_tenant_a",
    "feishu_app_secret": "s3cr3t-app-secret",
    "feishu_bot_name": "Acme Bot",
}


def _db_path():
    return os.path.join(tempfile.mkdtemp(), "identity.db")


class RequiredCredentialKeyDeclarationTests(unittest.TestCase):
    """5.1 — the declaration itself, checked against the full key list."""

    def test_every_required_key_is_also_a_declared_key(self):
        for channel_type, required in REQUIRED_CREDENTIAL_KEYS.items():
            self.assertIn(channel_type, MULTI_INSTANCE_READY, channel_type)
            declared = CREDENTIAL_KEYS[channel_type]
            for key in required:
                self.assertIn(key, declared,
                              f"{channel_type}: {key} is required but not declared")

    def test_the_verified_types_are_covered_and_the_unverified_ones_are_not(self):
        self.assertEqual(
            set(REQUIRED_CREDENTIAL_KEYS),
            {"feishu", "wecom_bot", "qq", "telegram", "slack", "discord",
             "wechatcom_app", "wechat_kf", "wechatmp"})
        # Deliberately unverified: inventing a minimum set for these would reject
        # writes that work today.
        self.assertEqual(required_credential_keys("dingtalk"), ())
        self.assertEqual(required_credential_keys("weixin"), ())

    def test_a_type_without_a_verified_set_is_not_given_one(self):
        self.assertEqual(required_credential_keys("nonsense"), ())

    def test_the_form_contract_carries_required_ness_from_the_same_declaration(self):
        for entry in tenant_channel_types():
            required = required_credential_keys(entry["channel_type"])
            marked = {f["key"] for f in entry["credential_fields"] if f.get("required")}
            self.assertEqual(marked, set(required), entry["channel_type"])


class _ChannelWriteFixture(unittest.TestCase):
    MASTER_KEY = MASTER_KEY

    def setUp(self):
        self._previous_key = os.environ.get("COW_CREDENTIAL_MASTER_KEY")
        os.environ["COW_CREDENTIAL_MASTER_KEY"] = self.MASTER_KEY
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
        self.tenant = self.svc.list_tenants()[0]["id"]
        self.svc.bind_agent(tenant_id=self.tenant, agent_id="agent-a",
                            private_owner_user_id=None)

    def _restore_master_key(self):
        if self._previous_key is None:
            os.environ.pop("COW_CREDENTIAL_MASTER_KEY", None)
        else:
            os.environ["COW_CREDENTIAL_MASTER_KEY"] = self._previous_key

    def _create(self, credentials, *, channel_type="feishu", display_name="Support"):
        return self.svc.create_tenant_channel_instance(
            actor_user_id=self.root["id"], tenant_id=self.tenant,
            channel_type=channel_type, display_name=display_name,
            agent_id="agent-a", credentials=credentials,
            recent_password="Str0ngRootFinal")

    def _rotate(self, instance_id, credentials, *, expected_version):
        return self.svc.update_tenant_channel_instance(
            actor_user_id=self.root["id"], tenant_id=self.tenant,
            instance_id=instance_id, expected_version=expected_version,
            recent_password="Str0ngRootFinal", credentials=credentials)

    def _instances(self):
        return self.svc.list_tenant_channel_instances(
            actor_user_id=self.root["id"], tenant_id=self.tenant)["items"]

    def _bundle(self, instance_id):
        return self.svc.channel_instance_credentials(self.tenant, instance_id)


class CreateNeedsTheMinimumSetTests(_ChannelWriteFixture):
    """5.2 — a create missing a required key lands nothing."""

    def _assert_rejected(self, credentials, *, channel_type="feishu"):
        before = self._instances()
        with self.assertRaises(IdentityServiceError) as caught:
            self._create(credentials, channel_type=channel_type)
        self.assertEqual(caught.exception.code, "bad_request")
        self.assertEqual(caught.exception.status, 400)
        # Nothing half-written: no instance row, and no orphan credential here.
        self.assertEqual([i["id"] for i in self._instances()],
                         [i["id"] for i in before])
        return caught.exception

    def test_a_create_without_the_secret_is_refused(self):
        error = self._assert_rejected({"feishu_app_id": "cli_tenant_a"})
        self.assertIn("feishu_app_secret", str(error))

    def test_a_create_without_the_app_id_is_refused(self):
        error = self._assert_rejected({"feishu_app_secret": "s3cr3t"})
        self.assertIn("feishu_app_id", str(error))

    def test_a_required_key_that_is_only_whitespace_is_refused(self):
        self._assert_rejected(
            {"feishu_app_id": "   ", "feishu_app_secret": "s3cr3t"})

    def test_an_optional_key_alone_does_not_satisfy_the_set(self):
        self._assert_rejected({"feishu_bot_name": "Acme Bot"})

    def test_every_verified_type_is_checked_not_just_feishu(self):
        complete = {
            "wecom_bot": {"wecom_bot_id": "bot", "wecom_bot_secret": "s3cr3t"},
            "qq": {"qq_app_id": "app", "qq_app_secret": "s3cr3t"},
            "telegram": {"telegram_token": "123:abc"},
            "slack": {"slack_bot_token": "xoxb-1", "slack_app_token": "xapp-1"},
            "discord": {"discord_token": "s3cr3t"},
        }
        for channel_type, bundle in complete.items():
            for key in list(bundle):
                partial = {k: v for k, v in bundle.items() if k != key}
                self._assert_rejected(partial, channel_type=channel_type)

    def test_a_complete_bundle_still_creates(self):
        created = self._create(dict(FEISHU_BUNDLE))
        self.assertTrue(created["id"])
        self.assertEqual(self._bundle(created["id"])["feishu_app_secret"],
                         "s3cr3t-app-secret")

    def test_a_type_without_a_verified_minimum_keeps_the_old_rule(self):
        # dingtalk has no verified set: one declared field is still accepted.
        created = self._create({"dingtalk_client_id": "ding"},
                               channel_type="dingtalk", display_name="Ding")
        self.assertTrue(created["id"])


class RotationKeepsWhatWasNotRetypedTests(_ChannelWriteFixture):
    """5.2/5.7 — rotation merges, then the merged set is what gets validated."""

    def test_retyping_only_the_secret_keeps_the_app_id(self):
        created = self._create(dict(FEISHU_BUNDLE))
        self._rotate(created["id"], {"feishu_app_secret": "rotated-secret"},
                     expected_version=created["version"])
        bundle = self._bundle(created["id"])
        self.assertEqual(bundle["feishu_app_secret"], "rotated-secret")
        self.assertEqual(bundle["feishu_app_id"], "cli_tenant_a",
                         "the app id the operator did not retype was dropped")
        self.assertEqual(bundle["feishu_bot_name"], "Acme Bot")

    def test_the_rotation_is_still_a_new_credential_version(self):
        created = self._create(dict(FEISHU_BUNDLE))
        self._rotate(created["id"], {"feishu_app_secret": "rotated-secret"},
                     expected_version=created["version"])
        rows = self.svc.list_tenant_channel_instances(
            actor_user_id=self.root["id"], tenant_id=self.tenant)["items"]
        self.assertEqual(rows[0]["version"], created["version"] + 1)
        # One credential row for the instance, so no second bundle was created.
        self.assertEqual(len([c for c in self.svc.list_credentials(
            actor_user_id=self.root["id"], tenant_id=self.tenant)["items"]
            if c["name"] == f"channel:{created['id']}"]), 1)

    def test_a_rotation_that_would_leave_the_set_incomplete_is_refused(self):
        # An instance stored before the minimum set existed can be incomplete.
        # Re-enabling it is allowed; editing it must complete the set.
        created = self._create(dict(FEISHU_BUNDLE))
        incomplete = self._force_incomplete_bundle(
            created["id"], {"feishu_app_id": "cli_tenant_a"})
        with self.assertRaises(IdentityServiceError) as caught:
            self._rotate(incomplete, {"feishu_bot_name": "Renamed"},
                         expected_version=created["version"])
        self.assertEqual(caught.exception.code, "bad_request")
        self.assertIn("feishu_app_secret", str(caught.exception))
        self.assertEqual(self._bundle(created["id"]),
                         {"feishu_app_id": "cli_tenant_a"},
                         "the refused write must not have touched the bundle")

    def test_completing_the_set_on_edit_is_accepted(self):
        created = self._create(dict(FEISHU_BUNDLE))
        incomplete = self._force_incomplete_bundle(
            created["id"], {"feishu_app_id": "cli_tenant_a"})
        self._rotate(incomplete, {"feishu_app_secret": "late-secret"},
                     expected_version=created["version"])
        self.assertEqual(self._bundle(created["id"]),
                         {"feishu_app_id": "cli_tenant_a",
                          "feishu_app_secret": "late-secret"})

    def test_an_incomplete_stored_instance_is_left_alone_by_a_plain_list(self):
        created = self._create(dict(FEISHU_BUNDLE))
        incomplete = self._force_incomplete_bundle(
            created["id"], {"feishu_app_id": "cli_tenant_a"})
        rows = [i for i in self._instances() if i["id"] == incomplete]
        self.assertEqual(len(rows), 1, "the legacy row must not be dropped")
        self.assertEqual(self._bundle(incomplete),
                         {"feishu_app_id": "cli_tenant_a"},
                         "an incomplete bundle must not be silently rewritten")

    def _force_incomplete_bundle(self, instance_id, bundle):
        """Rewrite an instance's stored bundle as a pre-minimum-set install.

        Uses the same store the service uses, so the test exercises the real
        decryption path rather than a stub.
        """
        import json as _json

        from auth.crypto import encrypt_secret

        ciphertext = encrypt_secret(_json.dumps(bundle, sort_keys=True))
        with self.svc._tx() as con:
            con.execute(
                "UPDATE credentials SET ciphertext=? WHERE tenant_id=? AND name=?",
                (ciphertext, self.tenant, f"channel:{instance_id}"))
            con.commit()
        self.assertEqual(decrypt_secret(ciphertext), _json.dumps(bundle, sort_keys=True))
        return instance_id


if __name__ == "__main__":
    unittest.main()
