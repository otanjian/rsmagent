# encoding:utf-8
"""External-connection control service, task group 2 (G1 control plane).

These tests drive the real :class:`IdentityService` against a real ``identity.db``
and assert the properties the specs make non-negotiable:

* ownership (``scope``/``tenant_id``/``owner_user_id``) is derived by the server
  and cannot be moved by a request body;
* a mutation is one transaction carrying the row, the secret *reference* and the
  redacted audit event — with an ``If-Match`` version that a concurrent writer
  loses;
* a secret is a reference to the credential tables, and keep / replace / clear
  are three distinct requests that a blank or masked value can never turn into a
  new secret;
* an unauthorized or disabled platform template is invisible to a tenant, a
  tenant override carries its own credentials, and deleting a still-referenced
  connection is refused with the reference summary;
* the capability projection reports ``test``/``execute`` as closed with the
  deployment reason instead of claiming a form implies a working adapter.

Creates use the same keyed master key the deployment uses, so a leaked column is
never enough to recover a secret (checked via the audit and credential rows).
"""

from __future__ import annotations

import json

import pytest

from tests._helpers import build_identity

MASTER_KEY = "unit-test-master-key"

ERP_RFC = {
    "provider": "rfc", "ashost": "sap.example.com", "sysnr": "00",
    "client": "100", "user": "sapuser", "lang": "EN",
}
MCP_HEADER = {
    "transport": "streamable_http", "url": "https://mcp.example.com/mcp",
    "auth": "header", "header_name": "X-Api-Key",
}
OA_LOGIN = {"base_url": "https://oa.example.com", "username": "alice"}
EMAIL_BOTH = {
    "imap": {"enabled": True, "host": "imap.example.com", "user": "alice@example.com"},
    "smtp": {"enabled": True, "host": "smtp.example.com", "user": "alice@example.com",
             "from_addr": "alice@example.com"},
}
SECRET = "S3cretPassw0rd!"


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", MASTER_KEY)


@pytest.fixture
def stack(tmp_path):
    return build_identity(tmp_path)


@pytest.fixture
def svc(stack):
    from integrations.external.service import ExternalConnectionService
    return ExternalConnectionService(stack.service)


def _role(stack, code, permissions):
    return stack.service.create_role(
        actor_user_id=stack.root, tenant_id=stack.tenant_id, code=code,
        name=code, permissions=list(permissions))


def _member_with(stack, code, permissions, username=None):
    role = _role(stack, code, permissions)
    username = username or code
    return stack.member(username, [role["code"]])


def _audit_actions(store, connection_id):
    return [
        row["action"] for row in store.execute(
            "SELECT action FROM audit_events WHERE target=? ORDER BY time",
            ("external_connection:%s" % connection_id,))
    ]


# -- creation and ownership ------------------------------------------------

def test_create_tenant_erp_stores_config_and_secret_reference(stack, svc):
    connection = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="erp", name="SAP 生产", config=ERP_RFC, secrets={"password": SECRET})

    assert connection["id"].startswith("conn_")
    assert connection["scope"] == "tenant"
    assert connection["kind"] == "erp"
    assert connection["version"] == 1
    assert connection["config"]["provider"] == "rfc"
    assert connection["secrets"]["password"] == {"configured": True}
    # The secret is referenced, not embedded: config_json has no plaintext and
    # the credential row is what the credential tables own.
    assert SECRET not in json.dumps(connection)
    credential = stack.service._store.execute(
        "SELECT name, resource_kind, resource_id, active, owner_user_id, ciphertext"
        " FROM credentials WHERE resource_id=?", (connection["id"],))[0]
    assert credential["name"] == "conn:%s:password" % connection["id"]
    assert credential["resource_kind"] == "external_connection"
    assert credential["active"] == 1
    assert credential["owner_user_id"] is None
    assert SECRET not in credential["ciphertext"]
    assert svc.resolve_secret(connection_id=connection["id"], slot="password",
                              scope="tenant", tenant_id=stack.tenant_id) == SECRET


def test_create_writes_a_redacted_audit_event_in_the_same_transaction(stack, svc):
    connection = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="erp", name="SAP", config=ERP_RFC, secrets={"password": SECRET})

    assert _audit_actions(stack.service._store, connection["id"]) == [
        "external_connection.create"]
    event = stack.service._store.execute(
        "SELECT actor_user_id, tenant_id, target_tenant_id, redacted_changes, result"
        " FROM audit_events WHERE target=?",
        ("external_connection:%s" % connection["id"],))[0]
    assert event["actor_user_id"] == stack.root
    assert event["tenant_id"] == stack.tenant_id
    assert event["target_tenant_id"] == stack.tenant_id
    assert event["result"] == "success"
    # Redacted means no secret text ever reaches the audit trail; the slot
    # *name* is reported (it is not a secret) so the event says what changed.
    assert SECRET not in event["redacted_changes"]
    assert json.loads(event["redacted_changes"])["slots"] == ["password"]
    assert "S3cret" not in event["redacted_changes"]


def test_ownership_is_never_taken_from_the_request(stack, svc):
    """A personal connection is owned by the caller, whatever a body claims."""
    alice = stack.member("alice", ["member"])
    other = stack.member("bob", ["member"])
    connection = svc.create_connection(
        actor_user_id=alice, scope="personal", tenant_id=stack.tenant_id,
        kind="email", name="我的邮箱", config=EMAIL_BOTH,
        secrets={"imap_password": SECRET, "smtp_password": SECRET})

    row = stack.service._store.execute(
        "SELECT owner_user_id, tenant_id, scope, source FROM external_connections"
        " WHERE id=?", (connection["id"],))[0]
    assert row["owner_user_id"] == alice
    assert row["owner_user_id"] != other
    assert row["tenant_id"] == stack.tenant_id
    assert row["scope"] == "personal"
    assert row["source"] == "created"


def test_scope_kind_pairs_are_refused(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    with pytest.raises(ExternalConnectionError) as caught:
        svc.create_connection(
            actor_user_id=stack.root, scope="personal",
            tenant_id=stack.tenant_id, kind="erp", name="nope",
            config=ERP_RFC)
    assert caught.value.code == "unsupported_scope"
    assert caught.value.status == 400


def test_unknown_config_key_and_secret_in_config_are_refused(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    with pytest.raises(ExternalConnectionError) as caught:
        svc.create_connection(
            actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
            kind="erp", name="x", config=dict(ERP_RFC, nope="1"))
    assert caught.value.code == "unknown_field"

    with pytest.raises(ExternalConnectionError) as caught:
        svc.create_connection(
            actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
            kind="erp", name="x", config=dict(ERP_RFC, password="leaky"))
    assert caught.value.code == "secret_in_config"


def test_url_with_embedded_credentials_is_refused(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    with pytest.raises(ExternalConnectionError) as caught:
        svc.create_connection(
            actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
            kind="mcp", name="bad",
            config={"transport": "sse", "url": "https://u:p@mcp.example.com/sse"})
    assert caught.value.code == "field_invalid"


# -- authorization ---------------------------------------------------------

def test_plain_member_cannot_manage_tenant_connections(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    alice = stack.member("alice", ["member"])
    with pytest.raises(ExternalConnectionError) as caught:
        svc.create_connection(
            actor_user_id=alice, scope="tenant", tenant_id=stack.tenant_id,
            kind="erp", name="x", config=ERP_RFC)
    assert caught.value.status == 403
    # Nor can they read the tenant catalogue, even though they are a member.
    with pytest.raises(ExternalConnectionError):
        svc.list_catalog(actor_user_id=alice, scope="tenant",
                         tenant_id=stack.tenant_id)


def test_read_and_manage_are_separately_granted(stack, svc):
    viewer = _member_with(stack, "conn-viewer", ["external.connections.read"],
                          username="viewer")
    manager = _member_with(stack, "conn-manager",
                           ["external.connections.read",
                            "external.connections.manage"],
                           username="manager")
    connection = svc.create_connection(
        actor_user_id=manager, scope="tenant", tenant_id=stack.tenant_id,
        kind="oa", name="OA", config=OA_LOGIN, secrets={"password": SECRET})

    catalogue = svc.list_catalog(actor_user_id=viewer, scope="tenant",
                                 tenant_id=stack.tenant_id)
    assert [item["id"] for item in catalogue["items"]] == [connection["id"]]
    assert catalogue["items"][0]["actions"] == ["read"]

    from integrations.external.errors import ExternalConnectionError
    with pytest.raises(ExternalConnectionError) as caught:
        svc.update_connection(
            actor_user_id=viewer, scope="tenant", tenant_id=stack.tenant_id,
            connection_id=connection["id"], expected_version=1,
            name="renamed")
    assert caught.value.status == 403


def test_a_member_cannot_read_another_members_mailbox(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    alice = stack.member("alice", ["member"])
    bob = stack.member("bob", ["member"])
    alice_email = svc.create_connection(
        actor_user_id=alice, scope="personal", tenant_id=stack.tenant_id,
        kind="email", name="alice", config=EMAIL_BOTH,
        secrets={"imap_password": SECRET, "smtp_password": SECRET})

    with pytest.raises(ExternalConnectionError) as caught:
        svc.get_connection(actor_user_id=bob, scope="personal",
                           tenant_id=stack.tenant_id,
                           connection_id=alice_email["id"])
    # Someone else's mailbox answers exactly like a missing one.
    assert caught.value.code == "not_found"

    bob_catalogue = svc.list_catalog(actor_user_id=bob, scope="personal",
                                     tenant_id=stack.tenant_id)
    assert bob_catalogue["items"] == []
    assert bob_catalogue["total"] == 0


def test_platform_scope_requires_platform_admin(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    manager = _member_with(stack, "conn-manager2",
                           ["external.connections.read",
                            "external.connections.manage"],
                           username="manager2")
    with pytest.raises(ExternalConnectionError) as caught:
        svc.create_connection(
            actor_user_id=manager, scope="platform", kind="mcp",
            name="shared", config=MCP_HEADER, secrets={"header": SECRET})
    assert caught.value.status == 403


# -- singletons ------------------------------------------------------------

def test_second_oa_connection_is_refused_with_a_stable_code(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="oa", name="OA", config=OA_LOGIN, secrets={"password": SECRET})
    with pytest.raises(ExternalConnectionError) as caught:
        svc.create_connection(
            actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
            kind="oa", name="OA 2", config=OA_LOGIN)
    assert caught.value.code == "singleton_exists"
    assert caught.value.status == 409


def test_soft_deleting_an_oa_frees_its_singleton_slot(stack, svc):
    first = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="oa", name="OA", config=OA_LOGIN, secrets={"password": SECRET})
    svc.delete_connection(actor_user_id=stack.root, scope="tenant",
                          tenant_id=stack.tenant_id,
                          connection_id=first["id"], expected_version=1)
    second = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="oa", name="OA 2", config=OA_LOGIN)
    assert second["id"] != first["id"]
    deleted = svc._fetch_row(first["id"], include_deleted=True)
    assert deleted["deleted_at"] is not None


def test_each_member_gets_their_own_email_singleton(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    alice = stack.member("alice", ["member"])
    bob = stack.member("bob", ["member"])
    svc.create_connection(
        actor_user_id=alice, scope="personal", tenant_id=stack.tenant_id,
        kind="email", name="alice", config=EMAIL_BOTH,
        secrets={"imap_password": SECRET, "smtp_password": SECRET})
    svc.create_connection(
        actor_user_id=bob, scope="personal", tenant_id=stack.tenant_id,
        kind="email", name="bob", config=EMAIL_BOTH)
    with pytest.raises(ExternalConnectionError) as caught:
        svc.create_connection(
            actor_user_id=alice, scope="personal", tenant_id=stack.tenant_id,
            kind="email", name="alice 2", config=EMAIL_BOTH)
    assert caught.value.code == "singleton_exists"


# -- secret keep / replace / clear ----------------------------------------

def test_blank_secret_keeps_the_stored_value(stack, svc):
    connection = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="erp", name="SAP", config=ERP_RFC, secrets={"password": SECRET})
    before = stack.service._store.execute(
        "SELECT version FROM credentials WHERE resource_id=?",
        (connection["id"],))[0]["version"]

    updated = svc.update_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_version=1, name="SAP 2",
        secrets={"password": "   "})

    after = stack.service._store.execute(
        "SELECT version FROM credentials WHERE resource_id=?",
        (connection["id"],))[0]["version"]
    assert after == before
    assert updated["secrets"]["password"] == {"configured": True}
    assert svc.resolve_secret(connection_id=connection["id"], slot="password",
                              scope="tenant", tenant_id=stack.tenant_id) == SECRET


def test_replacing_a_secret_versions_the_credential(stack, svc):
    connection = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="erp", name="SAP", config=ERP_RFC, secrets={"password": SECRET})
    svc.update_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_version=1,
        secrets={"password": "Replaced1!"})

    versions = stack.service._store.execute(
        "SELECT version, action FROM credential_versions WHERE credential_id=("
        " SELECT credential_id FROM external_connection_secret_refs WHERE"
        " connection_id=? AND slot='password') ORDER BY version",
        (connection["id"],))
    assert [(v["version"], v["action"]) for v in versions] == [
        (1, "create"), (2, "rotated")]
    assert svc.resolve_secret(connection_id=connection["id"], slot="password",
                              scope="tenant", tenant_id=stack.tenant_id) == "Replaced1!"
    # The previous ciphertext survives in the version history, not on the row.
    stored = stack.service._store.execute(
        "SELECT ciphertext FROM credentials WHERE resource_id=?",
        (connection["id"],))[0]["ciphertext"]
    assert "Replaced1!" not in stored


def test_clearing_a_secret_removes_the_reference_and_deactivates_it(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    connection = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="oa", name="OA", config=OA_LOGIN, secrets={"password": SECRET})
    updated = svc.update_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_version=1,
        secrets={"password": None})

    assert updated["secrets"]["password"] == {"configured": False}
    assert stack.service._store.execute(
        "SELECT active FROM credentials WHERE resource_id=?",
        (connection["id"],))[0]["active"] == 0
    assert stack.service._store.execute(
        "SELECT 1 FROM external_connection_secret_refs WHERE connection_id=?",
        (connection["id"],)) == []
    # A cleared required slot makes the connection explicitly unusable, and the
    # projection says which slot is missing.
    assert updated["capabilities"]["missing_secret_slots"] == ["password"]
    with pytest.raises(ExternalConnectionError):
        svc.resolve_secret(connection_id=connection["id"], slot="password",
                           scope="tenant", tenant_id=stack.tenant_id)


def test_a_masked_value_is_refused_as_a_secret(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    connection = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="erp", name="SAP", config=ERP_RFC, secrets={"password": SECRET})
    with pytest.raises(ExternalConnectionError) as caught:
        svc.update_connection(
            actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
            connection_id=connection["id"], expected_version=1,
            secrets={"password": "SAP •••• rd"})
    assert caught.value.code == "masked_secret"


def test_unknown_secret_slot_is_refused(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    with pytest.raises(ExternalConnectionError) as caught:
        svc.create_connection(
            actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
            kind="erp", name="SAP", config=ERP_RFC,
            secrets={"token": SECRET})
    assert caught.value.code == "unknown_secret_slot"


def test_a_missing_master_key_never_stores_a_plaintext_secret(stack, svc, monkeypatch):
    from integrations.external.errors import ExternalConnectionError
    monkeypatch.delenv("COW_CREDENTIAL_MASTER_KEY", raising=False)
    with pytest.raises(ExternalConnectionError) as caught:
        svc.create_connection(
            actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
            kind="erp", name="SAP", config=ERP_RFC, secrets={"password": SECRET})
    assert caught.value.code == "credential_crypto"
    assert caught.value.status == 503
    assert stack.service._store.execute(
        "SELECT 1 FROM external_connections WHERE name='SAP'") == []


def test_a_draft_without_a_secret_can_be_saved_and_reports_it(stack, svc):
    connection = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="erp", name="SAP draft", config=ERP_RFC)

    assert connection["secrets"]["password"] == {"configured": False}
    assert connection["capabilities"]["missing_secret_slots"] == ["password"]


# -- versioning / concurrency ---------------------------------------------

def test_a_stale_version_loses_the_write(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    connection = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="erp", name="SAP", config=ERP_RFC)
    svc.update_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_version=1, name="first")
    with pytest.raises(ExternalConnectionError) as caught:
        svc.update_connection(
            actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
            connection_id=connection["id"], expected_version=1, name="second")
    assert caught.value.code == "version_conflict"
    assert caught.value.status == 409
    assert svc.get_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        connection_id=connection["id"])["name"] == "first"


def test_create_is_idempotent_for_the_same_key_and_payload(stack, svc):
    first = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="erp", name="SAP", config=ERP_RFC, secrets={"password": SECRET},
        idempotency_key="key-1")
    replay = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="erp", name="SAP", config=ERP_RFC, secrets={"password": SECRET},
        idempotency_key="key-1")
    assert replay["id"] == first["id"]
    assert stack.service._store.execute(
        "SELECT COUNT(*) c FROM external_connections WHERE kind='erp'"
    )[0]["c"] == 1
    # The replay body is the redacted projection, never the stored secret.
    assert SECRET not in json.dumps(replay)


def test_the_same_idempotency_key_with_a_different_body_conflicts(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="erp", name="SAP", config=ERP_RFC, idempotency_key="key-2")
    with pytest.raises(ExternalConnectionError) as caught:
        svc.create_connection(
            actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
            kind="erp", name="SAP other", config=ERP_RFC,
            idempotency_key="key-2")
    assert caught.value.code == "idempotency_conflict"


# -- ERP default and references -------------------------------------------

def test_erp_default_is_a_cas_pointer_and_blocks_deletion(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    first = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="erp", name="A", config=ERP_RFC)
    second = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="erp", name="B", config=ERP_RFC)

    assert svc.get_erp_default(actor_user_id=stack.root,
                               tenant_id=stack.tenant_id) == {
        "connection_id": None, "revision": 1}
    result = svc.set_erp_default(actor_user_id=stack.root,
                                 tenant_id=stack.tenant_id,
                                 connection_id=first["id"], expected_revision=1)
    assert result == {"connection_id": first["id"], "revision": 2}

    with pytest.raises(ExternalConnectionError) as caught:
        svc.set_erp_default(actor_user_id=stack.root, tenant_id=stack.tenant_id,
                            connection_id=second["id"], expected_revision=1)
    assert caught.value.code == "catalog_version_conflict"

    # Deleting the default without a decision is refused with the reason.
    with pytest.raises(ExternalConnectionError) as caught:
        svc.delete_connection(actor_user_id=stack.root, scope="tenant",
                              tenant_id=stack.tenant_id,
                              connection_id=first["id"], expected_version=1)
    assert caught.value.code == "referenced"
    assert caught.value.references[0]["kind"] == "erp_default"
    assert caught.value.references[0]["next_action"] == "set_erp_default"

    # Naming a replacement is the explicit way through.
    svc.delete_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        connection_id=first["id"], expected_version=1,
        default_handling={"action": "replace", "connection_id": second["id"]})
    assert svc.get_erp_default(actor_user_id=stack.root,
                               tenant_id=stack.tenant_id)["connection_id"] == second["id"]


def test_disabling_the_default_requires_an_explicit_decision(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    connection = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="erp", name="A", config=ERP_RFC)
    svc.set_erp_default(actor_user_id=stack.root, tenant_id=stack.tenant_id,
                        connection_id=connection["id"], expected_revision=1)
    with pytest.raises(ExternalConnectionError) as caught:
        svc.update_connection(
            actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
            connection_id=connection["id"], expected_version=1, enabled=False)
    assert caught.value.code == "default_requires_decision"

    svc.update_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_version=1, enabled=False,
        default_handling={"action": "clear"})
    assert svc.get_erp_default(actor_user_id=stack.root,
                               tenant_id=stack.tenant_id)["connection_id"] is None
    assert stack.service._store.execute(
        "SELECT enabled FROM external_connections WHERE id=?",
        (connection["id"],))[0]["enabled"] == 0


def test_a_disabled_connection_cannot_be_the_erp_default(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    connection = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="erp", name="A", config=ERP_RFC, )
    svc.update_connection(actor_user_id=stack.root, scope="tenant",
                          tenant_id=stack.tenant_id,
                          connection_id=connection["id"], expected_version=1,
                          enabled=False)
    with pytest.raises(ExternalConnectionError) as caught:
        svc.set_erp_default(actor_user_id=stack.root, tenant_id=stack.tenant_id,
                            connection_id=connection["id"], expected_revision=1)
    assert caught.value.code == "default_disabled"


# -- platform inheritance and overrides -----------------------------------

def test_a_platform_connection_is_invisible_until_granted(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    platform = svc.create_connection(
        actor_user_id=stack.root, scope="platform", kind="mcp", name="shared",
        config=MCP_HEADER, secrets={"header": SECRET})
    other = stack.other_tenant()
    reader_permissions = ["external.connections.read",
                          "external.connections.manage"]
    role = stack.service.create_role(
        actor_user_id=stack.root, tenant_id=other["tenant_id"],
        code="conn-role", name="conn", permissions=reader_permissions)
    other_user = stack.service.create_member(
        actor_user_id=stack.root, tenant_id=other["tenant_id"],
        operation="create-new", username="other-manager",
        display_name="Other", temporary_password="TempPass123!",
        roles=[role["code"]])["user_id"]
    stack.service.change_password(
        stack.service.login("other-manager", "TempPass123!").token,
        "TempPass123!", "OtherPass123!")

    empty = svc.list_catalog(actor_user_id=other_user, scope="tenant",
                             tenant_id=other["tenant_id"])
    assert empty["items"] == []

    access = svc.list_tenant_access(actor_user_id=stack.root,
                                    platform_connection_id=platform["id"])
    assert access["tenants"] == [] and access["revision"] == 1
    svc.set_tenant_access(actor_user_id=stack.root,
                          platform_connection_id=platform["id"],
                          tenant_ids=[other["tenant_id"]], expected_revision=1)

    inherited = svc.list_catalog(actor_user_id=other_user, scope="tenant",
                                 tenant_id=other["tenant_id"])
    assert [item["id"] for item in inherited["items"]] == [platform["id"]]
    card = inherited["items"][0]
    assert card["source"] == "inherited"
    assert card["effective_id"] == platform["id"]
    assert card["runtime_enabled"] is True

    # A tenant lists the platform template but never its secret material.
    detail = svc.get_connection(actor_user_id=other_user, scope="tenant",
                                tenant_id=other["tenant_id"],
                                connection_id=platform["id"])
    assert SECRET not in json.dumps(detail)

    # Revoking is atomic: the tenant's catalogue goes empty again.
    svc.set_tenant_access(actor_user_id=stack.root,
                          platform_connection_id=platform["id"],
                          tenant_ids=[], expected_revision=2)
    assert svc.list_catalog(actor_user_id=other_user, scope="tenant",
                            tenant_id=other["tenant_id"])["items"] == []


def test_tenant_override_carries_its_own_secret_and_restores_inheritance(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    platform = svc.create_connection(
        actor_user_id=stack.root, scope="platform", kind="mcp", name="shared",
        config=MCP_HEADER, secrets={"header": "platform-secret"})
    svc.set_tenant_access(actor_user_id=stack.root,
                          platform_connection_id=platform["id"],
                          tenant_ids=[stack.tenant_id], expected_revision=1)

    # An override saved without its own credential is a draft that explicitly
    # reports the missing slot — it never borrows the platform secret.
    draft = svc.create_override(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        platform_connection_id=platform["id"], name="override draft",
        config=MCP_HEADER)
    assert draft["capabilities"]["missing_secret_slots"] == ["header"]
    assert "platform-secret" not in json.dumps(draft)
    with pytest.raises(ExternalConnectionError):
        svc.resolve_secret(connection_id=draft["id"], slot="header",
                           scope="tenant", tenant_id=stack.tenant_id)
    svc.restore_inheritance(actor_user_id=stack.root, tenant_id=stack.tenant_id,
                            platform_connection_id=platform["id"],
                            expected_version=1)

    override = svc.create_override(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        platform_connection_id=platform["id"], name="override",
        config=MCP_HEADER, secrets={"header": "tenant-secret"})
    assert override["source"] == "override"
    assert override["base_connection_id"] == platform["id"]
    assert svc.resolve_secret(connection_id=override["id"], slot="header",
                              scope="tenant",
                              tenant_id=stack.tenant_id) == "tenant-secret"

    # A second override of the same platform row is refused.
    with pytest.raises(ExternalConnectionError) as caught:
        svc.create_override(
            actor_user_id=stack.root, tenant_id=stack.tenant_id,
            platform_connection_id=platform["id"], name="override 2",
            config=MCP_HEADER, secrets={"header": "tenant-secret"})
    assert caught.value.code == "override_exists"

    catalogue = svc.list_catalog(actor_user_id=stack.root, scope="tenant",
                                 tenant_id=stack.tenant_id)
    by_id = {item["id"]: item for item in catalogue["items"]}
    assert by_id[platform["id"]]["source"] == "overridden"
    assert by_id[platform["id"]]["effective_id"] == override["id"]
    assert by_id[override["id"]]["source"] == "override"

    # A platform source with live overrides cannot be deleted out from under
    # the tenant data.
    with pytest.raises(ExternalConnectionError) as caught:
        svc.delete_connection(actor_user_id=stack.root, scope="platform",
                              connection_id=platform["id"], expected_version=1)
    assert caught.value.code == "referenced"
    assert {ref["kind"] for ref in caught.value.references} == {
        "tenant_overrides", "tenant_access"}

    svc.restore_inheritance(actor_user_id=stack.root, tenant_id=stack.tenant_id,
                            platform_connection_id=platform["id"],
                            expected_version=1)
    after = svc.list_catalog(actor_user_id=stack.root, scope="tenant",
                             tenant_id=stack.tenant_id)
    assert [item["source"] for item in after["items"]] == ["inherited"]


def test_an_override_of_a_disabled_platform_source_is_refused(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    platform = svc.create_connection(
        actor_user_id=stack.root, scope="platform", kind="mcp", name="shared",
        config=MCP_HEADER)
    svc.update_connection(actor_user_id=stack.root, scope="platform",
                          connection_id=platform["id"], expected_version=1,
                          enabled=False)
    with pytest.raises(ExternalConnectionError) as caught:
        svc.create_override(
            actor_user_id=stack.root, tenant_id=stack.tenant_id,
            platform_connection_id=platform["id"], name="override",
            config=MCP_HEADER)
    assert caught.value.code == "platform_source_disabled"


def test_platform_secrets_never_enter_the_tenant_credential_table(stack, svc):
    platform = svc.create_connection(
        actor_user_id=stack.root, scope="platform", kind="mcp", name="shared",
        config=MCP_HEADER, secrets={"header": SECRET})

    assert stack.service._store.execute(
        "SELECT 1 FROM credentials WHERE name LIKE 'conn:%'", ()) == []
    stored = stack.service._store.execute(
        "SELECT ciphertext FROM platform_connection_secrets WHERE name=?",
        ("conn:%s:header" % platform["id"],))
    assert len(stored) == 1 and SECRET not in stored[0]["ciphertext"]
    # The tenant credential listing cannot see it.
    from auth.service import IdentityServiceError
    with pytest.raises(IdentityServiceError) as caught:
        stack.service.resolve_credential(
            actor_user_id=stack.root, tenant_id=stack.tenant_id,
            name="conn:%s:header" % platform["id"])
    assert caught.value.status == 404


def test_platform_tenant_access_requires_platform_admin(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    platform = svc.create_connection(
        actor_user_id=stack.root, scope="platform", kind="mcp", name="shared",
        config=MCP_HEADER)
    manager = _member_with(stack, "conn-manager3",
                           ["external.connections.read",
                            "external.connections.manage"],
                           username="manager3")
    with pytest.raises(ExternalConnectionError) as caught:
        svc.set_tenant_access(actor_user_id=manager,
                              platform_connection_id=platform["id"],
                              tenant_ids=[stack.tenant_id], expected_revision=1)
    assert caught.value.status == 403


def test_tenant_access_refuses_an_unknown_tenant(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    platform = svc.create_connection(
        actor_user_id=stack.root, scope="platform", kind="mcp", name="shared",
        config=MCP_HEADER)
    with pytest.raises(ExternalConnectionError) as caught:
        svc.set_tenant_access(actor_user_id=stack.root,
                              platform_connection_id=platform["id"],
                              tenant_ids=["tenant-nope"], expected_revision=1)
    assert caught.value.code == "unknown_tenant"


# -- projection ------------------------------------------------------------

def test_capability_projection_reports_test_and_execute_as_closed(stack, svc):
    types = svc.types_projection(actor_user_id=stack.root,
                                 tenant_id=stack.tenant_id)
    by_kind = {item["kind"]: item for item in types["types"]}
    assert set(by_kind) == {"mcp", "erp", "oa", "email"}
    for item in by_kind.values():
        assert item["capabilities"]["open"] == ["configure"]
        assert item["capabilities"]["test_available"] is False
        assert item["capabilities"]["execute_available"] is False
        assert item["capabilities"]["unavailable_reason"].startswith("awaiting_")
    # The ERP type is not a singleton (a tenant may keep several and pick one as
    # the default), but the OA singleton is reflected so the console opens the
    # editor instead of offering a second one.
    svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="oa", name="OA", config=OA_LOGIN)
    after = {item["kind"]: item for item in svc.types_projection(
        actor_user_id=stack.root, tenant_id=stack.tenant_id)["types"]}
    oa_scope = [s for s in after["oa"]["scopes"] if s["scope"] == "tenant"][0]
    assert oa_scope["available"] is False
    assert oa_scope["reason"] == "singleton_exists"
    erp_scope = [s for s in after["erp"]["scopes"] if s["scope"] == "tenant"][0]
    assert erp_scope["available"] is True
    assert [s["scope"] for s in after["email"]["scopes"]] == ["personal"]


def test_the_catalogue_only_counts_visible_rows(stack, svc):
    svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="erp", name="A", config=ERP_RFC)
    svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="oa", name="OA", config=OA_LOGIN)
    alice = stack.member("alice", ["member"])
    svc.create_connection(
        actor_user_id=alice, scope="personal", tenant_id=stack.tenant_id,
        kind="email", name="alice", config=EMAIL_BOTH)

    tenant_catalogue = svc.list_catalog(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id)
    assert tenant_catalogue["total"] == 2
    personal_catalogue = svc.list_catalog(
        actor_user_id=alice, scope="personal", tenant_id=stack.tenant_id)
    assert personal_catalogue["total"] == 1
    # The member's page cannot see the tenant rows at all.
    assert len(personal_catalogue["items"]) == 1


# -- the initial state and the idempotency ledger (change
#    align-mcp-editor-with-master, task 2.4)
#
# The master editor can create a connection that starts disabled, and it submits
# a batch one item at a time. Both properties are about the *insert*, not about
# a follow-up call:
#
#   * a connection created disabled must never be inserted enabled and flipped,
#     because the flip is a second transaction during which anything reading the
#     table sees a connection the operator did not ask to be on;
#   * the create and its idempotency record must commit or fail together, or a
#     retry of the same key after a crash would create a second connection --
#     which the batch import would then show twice.

def test_a_connection_can_be_created_disabled(stack, svc):
    connection = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="mcp", name="Off", config=MCP_HEADER, enabled=False)

    assert connection["enabled"] is False
    stored = stack.service._store.execute(
        "SELECT enabled FROM external_connections WHERE id=?",
        (connection["id"],))[0]
    assert stored["enabled"] == 0
    # Read back through the normal path too: the projection must agree.
    assert svc.get_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        connection_id=connection["id"])["enabled"] is False


def test_omitting_enabled_keeps_the_historical_default(stack, svc):
    connection = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="mcp", name="On", config=MCP_HEADER)

    assert connection["enabled"] is True


def test_the_disabled_flag_is_part_of_the_idempotency_fingerprint(stack, svc):
    """A replay with a different initial state is not the same request."""
    from integrations.external.errors import ExternalConnectionError
    svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="mcp", name="Off", config=MCP_HEADER, enabled=False,
        idempotency_key="key-off")

    with pytest.raises(ExternalConnectionError) as caught:
        svc.create_connection(
            actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
            kind="mcp", name="Off", config=MCP_HEADER, enabled=True,
            idempotency_key="key-off")
    assert caught.value.code == "idempotency_conflict"


def test_a_failed_create_leaves_no_connection_and_no_ledger_row(
        stack, svc, monkeypatch):
    """The create and the "remember" are one transaction, not two.

    A failure while writing the idempotency record is injected *after* the row
    and its secret have been written, which is exactly the window a two-commit
    implementation would lose: the row would survive, the ledger would not, and
    a retry of the same key would insert a second connection.
    """
    from integrations.external import service as service_module

    def _boom(self, con, **kwargs):
        raise RuntimeError("injected failure while recording the key")

    monkeypatch.setattr(
        service_module.ExternalConnectionService,
        "_remember_idempotent_in_tx", _boom, raising=True)

    with pytest.raises(RuntimeError):
        svc.create_connection(
            actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
            kind="mcp", name="Half", config=MCP_HEADER,
            secrets={"header": SECRET}, idempotency_key="key-atomic")

    assert stack.service._store.execute(
        "SELECT COUNT(*) c FROM external_connections WHERE name='Half'"
    )[0]["c"] == 0
    assert stack.service._store.execute(
        "SELECT COUNT(*) c FROM credentials"
    )[0]["c"] == 0

    # And the key is free again: the retry is a first attempt, not a conflict.
    monkeypatch.undo()
    retry = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="mcp", name="Half", config=MCP_HEADER,
        secrets={"header": SECRET}, idempotency_key="key-atomic")
    assert retry["id"]
    assert stack.service._store.execute(
        "SELECT COUNT(*) c FROM external_connections WHERE name='Half'"
    )[0]["c"] == 1
    replay = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="mcp", name="Half", config=MCP_HEADER,
        secrets={"header": SECRET}, idempotency_key="key-atomic")
    assert replay["id"] == retry["id"]
