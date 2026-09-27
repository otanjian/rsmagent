"""Catalog optimization keeps permissions fresh and work bounded per request."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auth.runtime import RequestContext
from auth.service import IdentityService, agent_catalog_read_scope, get_identity_service
from auth.store import IdentityStore


@pytest.fixture
def identity(tmp_path):
    db = str(tmp_path / "identity.db")
    svc = IdentityService(db)
    svc.bootstrap(tenant_code="acme", tenant_name="Acme", admin_username="root",
                  admin_display="Root", admin_password="Testing123!", allow_weak=True,
                  shared_root=str(tmp_path / "shared"))
    tenant = svc.list_tenants()[0]["id"]
    user = svc.list_platform_users()[0]["id"]
    with patch("auth.service.identity_db_path", return_value=db):
        yield svc, tenant, user


def test_catalog_batches_bindings_and_preserves_projection(identity):
    import web
    from channel.web.web_channel import AgentsHandler
    import json

    svc, tenant, user = identity
    profiles = []
    for i in range(70):
        aid = f"agent-{i:03}"
        svc.bind_agent(tenant_id=tenant, agent_id=aid)
        profiles.append(SimpleNamespace(id=aid, name=aid, enabled=True, description="",
                                        avatar=None, position="", category="", tags=[],
                                        agent_type="normal"))
    registry = SimpleNamespace(list=lambda: profiles,
                               get=lambda aid, **kwargs: next((p for p in profiles if p.id == aid), None))
    ctx = RequestContext(user_id=user, username="root", display_name="Root",
                         is_platform_admin=True, must_change_password=False,
                         tenant_id=tenant, membership=None,
                         permissions={"agent.read", "agent.use", "chat.use"}, is_tenant_admin=True)

    @contextmanager
    def db_scope():
        yield ctx

    queries = []
    execute = IdentityStore.execute

    def counted(store, sql, params=()):
        queries.append(sql)
        return execute(store, sql, params)

    with patch("agent.registry.get_agent_registry", return_value=registry), \
         patch("channel.web.web_channel._db_scope", db_scope), \
         patch.object(web, "header"), \
         patch.object(web, "input", return_value=SimpleNamespace(view="workbench")), \
         patch.object(IdentityStore, "execute", counted), \
         patch.object(IdentityStore, "_migrate", autospec=True,
                      side_effect=IdentityStore._migrate) as migrate:
        data = json.loads(AgentsHandler().GET())
    assert data["status"] == "success"
    assert len(data["agents"]) == 70
    assert all(a["can_chat"] and a["visibility"] == "tenant" for a in data["agents"])
    binding_reads = [q for q in queries if "FROM agent_bindings" in q]
    assert len(binding_reads) == 1, binding_reads
    # Three stores belonging to one service, not three per Agent/gate.
    assert migrate.call_count == 3
    assert len(queries) < 15, queries


def test_next_read_and_write_observe_changed_owner(identity):
    svc, tenant, user = identity
    member = svc.create_member(actor_user_id=user, tenant_id=tenant, operation="create-new",
                               username="member", display_name="Member",
                               temporary_password="Testing123!", roles=["member"])["user_id"]
    svc.bind_agent(tenant_id=tenant, agent_id="shared")
    with agent_catalog_read_scope(tenant):
        read = get_identity_service()
        assert read.check_resource_action(user, tenant, "agent", "agent:shared", "use")
        row = read.get_agent_binding("shared")
        row["private_owner_user_id"] = "client-mutation"
        assert read.get_agent_binding("shared")["private_owner_user_id"] is None
    svc.bind_agent(tenant_id=tenant, agent_id="shared", private_owner_user_id=member)
    # No cached administrator bypass may survive a change to private ownership.
    with agent_catalog_read_scope(tenant):
        read = get_identity_service()
        assert not read.check_resource_action(user, tenant, "agent", "agent:shared", "use")
        assert read.check_resource_action(member, tenant, "agent", "agent:shared", "use")
    assert not get_identity_service().check_resource_action(
        user, tenant, "agent", "agent:shared", "use")


def test_grants_are_reloaded_on_next_request(identity):
    svc, tenant, user = identity
    member = svc.create_member(actor_user_id=user, tenant_id=tenant, operation="create-new",
                               username="member", display_name="Member",
                               temporary_password="Testing123!", roles=["member"])["user_id"]
    svc.bind_agent(tenant_id=tenant, agent_id="shared")
    role = svc._store.execute("SELECT id FROM roles WHERE tenant_id=? AND code='member'", (tenant,))[0]["id"]
    with svc._tx() as con:
        con.execute("INSERT INTO role_resource_grants(tenant_id, role_id, resource_kind, resource_id, action)"
                    " VALUES (?, ?, 'agent', 'agent:shared', 'use')", (tenant, role))
    with agent_catalog_read_scope(tenant):
        read = get_identity_service()
        for _ in range(3):
            assert read.check_resource_action(member, tenant, "agent", "agent:shared", "use")
    with svc._tx() as con:
        con.execute("DELETE FROM role_resource_grants WHERE role_id=? AND resource_kind='agent'", (role,))
    with agent_catalog_read_scope(tenant):
        assert not get_identity_service().check_resource_action(
            member, tenant, "agent", "agent:shared", "use")


def test_catalog_scope_is_thread_local_and_cleans_up_on_error(identity):
    svc, tenant, user = identity
    svc.bind_agent(tenant_id=tenant, agent_id="mine")
    with pytest.raises(RuntimeError), agent_catalog_read_scope(tenant):
        caller = get_identity_service()
        with ThreadPoolExecutor(max_workers=1) as pool:
            other = pool.submit(get_identity_service).result()
        assert other is not caller
        assert other._catalog_bindings is None
        assert get_identity_service() is caller
        raise RuntimeError("response failed")
    assert get_identity_service()._catalog_reads is None
    with agent_catalog_read_scope("another-tenant"):
        assert get_identity_service().get_agent_binding("mine") is None
