# encoding:utf-8
"""Service-account API key access (retire-legacy-identity-mode 2.4–2.7).

External OpenAI-compatible API authenticates as a real service-account User via
encrypted ``sak_`` keys (credential-management), not ``external_api_token``.
"""

import hashlib

import pytest

MASTER = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", MASTER)


@pytest.fixture
def svc(tmp_path):
    from auth.service import IdentityService

    service = IdentityService(str(tmp_path / "identity.db"))
    tenant = service.bootstrap(
        tenant_code="acme", tenant_name="Acme", admin_username="root",
        admin_display="Root", admin_password="Str0ngAdminPass",
        shared_root=str(tmp_path / "shared"), allow_weak=True,
    )["id"]
    root = service.list_platform_users()[0]["id"]
    sa = service.create_member(
        actor_user_id=root, tenant_id=tenant, operation="create-new",
        username="svc-bot", display_name="Service Bot",
        temporary_password="TempPass123!", roles=["member"],
    )
    member = service.create_member(
        actor_user_id=root, tenant_id=tenant, operation="create-new",
        username="member", display_name="Member",
        temporary_password="TempPass123!", roles=["member"],
    )["user_id"]
    return Simple(
        svc=service, tenant=tenant, root=root,
        sa_user=sa["user_id"], member=member,
        db_path=str(tmp_path / "identity.db"),
    )


class Simple:
    def __init__(self, svc, tenant, root, sa_user, member, db_path):
        self.svc = svc
        self.tenant = tenant
        self.root = root
        self.sa_user = sa_user
        self.member = member
        self.db_path = db_path


# --- lifecycle -------------------------------------------------------------

def test_create_returns_plaintext_once_list_is_masked(svc):
    s = svc.svc
    created = s.create_service_account_api_key(
        actor_user_id=svc.root, tenant_id=svc.tenant, user_id=svc.sa_user,
    )
    assert created["user_id"] == svc.sa_user
    assert created["id"]
    api_key = created["api_key"]
    assert api_key.startswith("sak_")
    assert len(api_key) > 20

    listing = s.list_service_account_api_keys(
        actor_user_id=svc.root, tenant_id=svc.tenant, user_id=svc.sa_user,
    )
    assert listing["total"] >= 1
    item = next(i for i in listing["items"] if i["id"] == created["id"])
    assert "api_key" not in item
    assert api_key not in str(item)
    assert item.get("masked")
    assert api_key not in item["masked"]


def test_authenticate_valid_key_returns_identity_context(svc):
    s = svc.svc
    created = s.create_service_account_api_key(
        actor_user_id=svc.root, tenant_id=svc.tenant, user_id=svc.sa_user,
    )
    ctx = s.authenticate_service_account_api_key(created["api_key"])
    assert ctx["user_id"] == svc.sa_user
    assert ctx["tenant_id"] == svc.tenant
    assert "permissions" in ctx
    assert ctx.get("username")


def test_rotate_invalidates_old_key(svc):
    s = svc.svc
    created = s.create_service_account_api_key(
        actor_user_id=svc.root, tenant_id=svc.tenant, user_id=svc.sa_user,
    )
    old_key = created["api_key"]
    rotated = s.rotate_service_account_api_key(
        actor_user_id=svc.root, tenant_id=svc.tenant, user_id=svc.sa_user,
        credential_id=created["id"],
    )
    new_key = rotated["api_key"]
    assert new_key.startswith("sak_")
    assert new_key != old_key

    with pytest.raises(Exception) as err:
        s.authenticate_service_account_api_key(old_key)
    assert err.value.status == 401

    ctx = s.authenticate_service_account_api_key(new_key)
    assert ctx["user_id"] == svc.sa_user


def test_revoke_invalidates_key(svc):
    s = svc.svc
    created = s.create_service_account_api_key(
        actor_user_id=svc.root, tenant_id=svc.tenant, user_id=svc.sa_user,
    )
    assert s.revoke_service_account_api_key(
        actor_user_id=svc.root, tenant_id=svc.tenant,
        credential_id=created["id"],
    ) is True
    with pytest.raises(Exception) as err:
        s.authenticate_service_account_api_key(created["api_key"])
    assert err.value.status == 401


def test_non_controller_cannot_create_key(svc):
    s = svc.svc
    with pytest.raises(Exception) as err:
        s.create_service_account_api_key(
            actor_user_id=svc.member, tenant_id=svc.tenant, user_id=svc.sa_user,
        )
    assert err.value.status == 403


def test_cross_tenant_create_rejected(svc):
    s = svc.svc
    other = s.create_tenant(
        actor_user_id=svc.root, code="other", name="Other", shared_root="",
        admin_username="other", admin_display="Other",
        admin_password="OtherPass123!", recent_password="Str0ngAdminPass",
    )["id"]
    with pytest.raises(Exception) as err:
        s.create_service_account_api_key(
            actor_user_id=svc.root, tenant_id=other, user_id=svc.sa_user,
        )
    assert err.value.status in (403, 404)


def test_invalid_key_returns_401(svc):
    s = svc.svc
    with pytest.raises(Exception) as err:
        s.authenticate_service_account_api_key("sak_not-a-real-key-value")
    assert err.value.status == 401


def test_store_failure_returns_503(svc, monkeypatch):
    s = svc.svc
    created = s.create_service_account_api_key(
        actor_user_id=svc.root, tenant_id=svc.tenant, user_id=svc.sa_user,
    )

    def _boom(*_a, **_k):
        raise OSError("identity store down")

    monkeypatch.setattr(s._store, "execute", _boom)
    with pytest.raises(Exception) as err:
        s.authenticate_service_account_api_key(created["api_key"])
    assert err.value.status == 503


def test_create_rotate_revoke_are_audited(svc):
    s = svc.svc
    created = s.create_service_account_api_key(
        actor_user_id=svc.root, tenant_id=svc.tenant, user_id=svc.sa_user,
    )
    secret = created["api_key"]
    s.rotate_service_account_api_key(
        actor_user_id=svc.root, tenant_id=svc.tenant, user_id=svc.sa_user,
        credential_id=created["id"],
    )
    s.revoke_service_account_api_key(
        actor_user_id=svc.root, tenant_id=svc.tenant,
        credential_id=created["id"],
    )
    actions = [e["action"] for e in s.list_audit(tenant_id=svc.tenant)]
    assert "credential.create" in actions or "service_api_key.create" in actions
    assert "credential.rotate" in actions or "service_api_key.rotate" in actions
    assert "credential.revoke" in actions or "service_api_key.revoke" in actions
    for event in s.list_audit(tenant_id=svc.tenant):
        assert secret not in str(event)


def test_key_stored_as_hash_named_encrypted_credential(svc):
    s = svc.svc
    created = s.create_service_account_api_key(
        actor_user_id=svc.root, tenant_id=svc.tenant, user_id=svc.sa_user,
    )
    api_key = created["api_key"]
    expected_name = "sak:" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    rows = s._store.execute(
        "SELECT name, resource_kind, resource_id, ciphertext FROM credentials WHERE id=?",
        (created["id"],),
    )
    assert rows
    assert rows[0]["name"] == expected_name
    assert rows[0]["resource_kind"] == "service_api_key"
    assert rows[0]["resource_id"] == svc.sa_user
    assert api_key not in rows[0]["ciphertext"]


# --- openai_api wiring -----------------------------------------------------

def test_openai_db_mode_accepts_sak_bearer(svc, monkeypatch):
    """Database mode: ``sak_`` Bearer authenticates as the bound service user."""
    from channel.web import openai_api

    s = svc.svc
    # Use platform admin as the bound user so chat/agent authorization_mode=all.
    created = s.create_service_account_api_key(
        actor_user_id=svc.root, tenant_id=svc.tenant, user_id=svc.root,
    )
    s.bind_agent(tenant_id=svc.tenant, agent_id="bot-agent")
    # The public endpoint also verifies that the bound Agent is enabled in the registry.
    from types import SimpleNamespace
    class Registry:
        def get_addressed(self, ident):
            if ident != 'bot-agent':
                raise KeyError(ident)
            return SimpleNamespace(id='bot-agent')
    monkeypatch.setattr('agent.registry.get_agent_registry', lambda: Registry())

    monkeypatch.setattr(openai_api, "_is_database_mode", lambda: True)
    monkeypatch.setattr("auth.service.get_identity_service", lambda: s)

    import web
    env = {
        "HTTP_AUTHORIZATION": f"Bearer {created['api_key']}",
        "HTTP_X_TENANT_ID": svc.tenant,
        "HTTP_X_AGENT_ID": "bot-agent",
    }
    monkeypatch.setattr(web, "ctx", type("C", (), {"env": env})(), raising=False)

    ident, agent_id = openai_api._db_request_identity_and_agent()
    assert agent_id == "bot-agent"
    assert ident.user_id == svc.root
    assert ident.tenant_id == svc.tenant


def test_openai_db_mode_rejects_invalid_sak(svc, monkeypatch):
    from channel.web import openai_api

    monkeypatch.setattr(openai_api, "_is_database_mode", lambda: True)
    monkeypatch.setattr("auth.service.get_identity_service", lambda: svc.svc)

    import web
    env = {
        "HTTP_AUTHORIZATION": "Bearer sak_totally-invalid",
        "HTTP_X_TENANT_ID": svc.tenant,
    }
    monkeypatch.setattr(web, "ctx", type("C", (), {"env": env})(), raising=False)

    with pytest.raises(openai_api.OpenAIAPIError) as err:
        openai_api._db_request_identity_and_agent()
    assert err.value.status_code == 401
