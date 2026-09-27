# encoding:utf-8
"""Per-connection Agent assignment: relation, permission, delta save, cleanup.

Change ``add-external-connection-agent-assignment``, task group 1.

Drives the real service over a real identity database. The properties under test
are the ones the spec makes non-negotiable:

* a connection's relation is keyed on the *logical* connection, so a platform
  template and the tenant's override share one set;
* a save is a **delta** — loading one page and removing one row must not clear
  the rows the caller never saw;
* targets are re-qualified at save time, and an invisible/foreign/private target
  refuses the whole request instead of partially applying;
* the relation carries its own revision, so assignment does not invalidate the
  connection's version or its test badge;
* a legal connection delete clears the relation in the same transaction.
"""

from __future__ import annotations

import pytest

from tests._helpers import build_identity

MASTER_KEY = "unit-test-master-key"

OA_LOGIN = {"base_url": "https://oa.example.com", "username": "alice"}
SECRET = "S3cretPassw0rd!"
MCP_HEADER = {
    "transport": "streamable_http", "url": "https://mcp.example.com/mcp",
    "auth": "header", "header_name": "X-Api-Key",
}


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", MASTER_KEY)


@pytest.fixture
def stack(tmp_path):
    return build_identity(tmp_path, agents=("agent-a", "agent-b", "agent-c"))


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
    return stack.member(username or code, [role["code"]])


def _oa(svc, stack, name="OA"):
    return svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind="oa", name=name, config=OA_LOGIN, secrets={"password": SECRET})


def _set_configured(stack, connection_id, configured):
    stack.service._store.execute(
        "UPDATE external_connection_agent_assignment_sets SET configured=?"
        " WHERE tenant_id=? AND logical_connection_id=?",
        (1 if configured else 0, stack.tenant_id, connection_id))


# -- schema and defaults ---------------------------------------------------

def test_new_connection_starts_configured_and_empty(stack, svc):
    connection = _oa(svc, stack)
    snapshot = svc.list_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"])
    assert snapshot["configured"] is True
    assert snapshot["revision"] == 1
    assert snapshot["total"] == 0
    state = stack.service._store.execute(
        "SELECT configured, revision FROM"
        " external_connection_agent_assignment_sets WHERE tenant_id=?"
        " AND logical_connection_id=?",
        (stack.tenant_id, connection["id"]))[0]
    assert state["configured"] == 1


def test_migration_tables_exist(stack):
    names = {row["name"] for row in stack.service._store.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "external_connection_agent_assignments" in names
    assert "external_connection_agent_assignment_sets" in names


def test_unconfigured_connection_reports_and_allows_compat(stack, svc):
    from integrations.external import assignment
    connection = _oa(svc, stack)
    _set_configured(stack, connection["id"], False)
    snapshot = svc.list_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"])
    assert snapshot["configured"] is False
    allowed, reason = assignment.assignment_allows(
        stack.service._store, tenant_id=stack.tenant_id,
        logical_id=connection["id"], agent_id="agent-a")
    assert allowed is True and reason == assignment.REASON_UNCONFIGURED


def test_configured_empty_set_denies_everyone(stack, svc):
    from integrations.external import assignment
    connection = _oa(svc, stack)
    for agent_id in ("agent-a", ""):
        allowed, reason = assignment.assignment_allows(
            stack.service._store, tenant_id=stack.tenant_id,
            logical_id=connection["id"], agent_id=agent_id)
        assert allowed is False
        assert reason in (assignment.REASON_NOT_ASSIGNED,
                          assignment.REASON_NO_AGENT)


def test_missing_state_refuses_instead_of_compat(stack, svc):
    from integrations.external import assignment
    connection = _oa(svc, stack)
    stack.service._store.execute(
        "DELETE FROM external_connection_agent_assignment_sets"
        " WHERE tenant_id=? AND logical_connection_id=?",
        (stack.tenant_id, connection["id"]))
    allowed, reason = assignment.assignment_allows(
        stack.service._store, tenant_id=stack.tenant_id,
        logical_id=connection["id"], agent_id="agent-a")
    assert allowed is False and reason == assignment.REASON_STATE_MISSING


# -- saving ----------------------------------------------------------------

def test_save_adds_and_removes_and_bumps_only_assignment_revision(stack, svc):
    connection = _oa(svc, stack)
    result = svc.save_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_revision=1,
        add_agent_ids=["agent-a", "agent-b"])
    assert result["added"] == 2 and result["removed"] == 0
    assert result["configured"] is True and result["revision"] == 2
    # The connection's own version is untouched: assignment must not expire the
    # test badge or the If-Match token.
    row = stack.service._store.execute(
        "SELECT version FROM external_connections WHERE id=?",
        (connection["id"],))[0]
    assert int(row["version"]) == 1

    listed = svc.list_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"])
    assert {item["id"] for item in listed["items"]} == {"agent-a", "agent-b"}

    removed = svc.save_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_revision=2,
        remove_agent_ids=["agent-a"])
    assert removed["removed"] == 1 and removed["revision"] == 3
    listed = svc.list_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"])
    assert {item["id"] for item in listed["items"]} == {"agent-b"}


def test_first_save_may_be_empty_and_locks_the_connection(stack, svc):
    from integrations.external import assignment
    connection = _oa(svc, stack)
    _set_configured(stack, connection["id"], False)
    result = svc.save_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_revision=1)
    assert result["configured"] is True and result["added"] == 0
    assert result["revision"] == 2
    allowed, reason = assignment.assignment_allows(
        stack.service._store, tenant_id=stack.tenant_id,
        logical_id=connection["id"], agent_id="agent-a")
    assert allowed is False and reason == assignment.REASON_NOT_ASSIGNED


def test_delta_preserves_relations_outside_the_request(stack, svc):
    """Removing one visible row must not clear rows the caller never sent."""
    connection = _oa(svc, stack)
    svc.save_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_revision=1,
        add_agent_ids=["agent-a", "agent-b", "agent-c"])
    svc.save_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_revision=2,
        remove_agent_ids=["agent-a"])
    remaining = {row["agent_id"] for row in stack.service._store.execute(
        "SELECT agent_id FROM external_connection_agent_assignments"
        " WHERE tenant_id=? AND logical_connection_id=?",
        (stack.tenant_id, connection["id"]))}
    assert remaining == {"agent-b", "agent-c"}


def test_stale_revision_conflicts_without_partial_write(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    connection = _oa(svc, stack)
    with pytest.raises(ExternalConnectionError) as caught:
        svc.save_agent_assignments(
            actor_user_id=stack.root, tenant_id=stack.tenant_id,
            connection_id=connection["id"], expected_revision=99,
            add_agent_ids=["agent-a"])
    assert caught.value.code == "assignment_version_conflict"
    assert not stack.service._store.execute(
        "SELECT 1 FROM external_connection_agent_assignments WHERE tenant_id=?"
        " AND logical_connection_id=?", (stack.tenant_id, connection["id"]))


def test_no_net_change_returns_current_state_without_bumping(stack, svc):
    connection = _oa(svc, stack)
    svc.save_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_revision=1,
        add_agent_ids=["agent-a"])
    again = svc.save_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_revision=2,
        add_agent_ids=["agent-a"])
    assert again["unchanged"] is True and again["revision"] == 2


# -- validation ------------------------------------------------------------

def test_conflicting_change_is_refused(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    connection = _oa(svc, stack)
    with pytest.raises(ExternalConnectionError) as caught:
        svc.save_agent_assignments(
            actor_user_id=stack.root, tenant_id=stack.tenant_id,
            connection_id=connection["id"], expected_revision=1,
            add_agent_ids=["agent-a"], remove_agent_ids=["agent-a"])
    assert caught.value.code == "conflicting_change"


def test_over_limit_change_is_refused(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    connection = _oa(svc, stack)
    too_many = ["agent-%03d" % i for i in range(101)]
    with pytest.raises(ExternalConnectionError) as caught:
        svc.save_agent_assignments(
            actor_user_id=stack.root, tenant_id=stack.tenant_id,
            connection_id=connection["id"], expected_revision=1,
            add_agent_ids=too_many)
    assert caught.value.code == "too_many_changes"


def test_malformed_agent_id_is_refused(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    connection = _oa(svc, stack)
    with pytest.raises(ExternalConnectionError) as caught:
        svc.save_agent_assignments(
            actor_user_id=stack.root, tenant_id=stack.tenant_id,
            connection_id=connection["id"], expected_revision=1,
            add_agent_ids=["bad id!"])
    assert caught.value.code == "field_invalid"


def test_unknown_agent_is_refused(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    connection = _oa(svc, stack)
    with pytest.raises(ExternalConnectionError) as caught:
        svc.save_agent_assignments(
            actor_user_id=stack.root, tenant_id=stack.tenant_id,
            connection_id=connection["id"], expected_revision=1,
            add_agent_ids=["ghost-agent"])
    assert caught.value.code == "agent_not_assignable"


# -- authorization ---------------------------------------------------------

def test_plain_member_cannot_read_assignments(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    connection = _oa(svc, stack)
    # A member with no connection grant at all (the built-in ``member`` role's
    # exact permission set is exercised elsewhere; this pins the assignment
    # surface's own read gate).
    alice = _member_with(stack, "no-connection-access", ["chat.use"],
                         username="alice")
    with pytest.raises(ExternalConnectionError) as caught:
        svc.list_agent_assignments(
            actor_user_id=alice, tenant_id=stack.tenant_id,
            connection_id=connection["id"])
    assert caught.value.status == 403


def test_visible_but_unmanageable_agent_cannot_be_added(stack, svc):
    """A member with connection-manage may not assign a *shared* Agent."""
    from integrations.external.errors import ExternalConnectionError
    manager = _member_with(
        stack, "conn-manager",
        ["external.connections.read", "external.connections.manage"],
        username="manager")
    connection = _oa(svc, stack)  # created by root
    listed = svc.list_agent_assignments(
        actor_user_id=manager, tenant_id=stack.tenant_id,
        connection_id=connection["id"])
    assert listed["can_assign"] is True
    # agent-a is shared → visible but not manageable by a plain manager.
    candidates = svc.search_agent_candidates(
        actor_user_id=manager, tenant_id=stack.tenant_id,
        connection_id=connection["id"], q="agent-a")
    assert candidates["items"][0]["manageable"] is False
    with pytest.raises(ExternalConnectionError) as caught:
        svc.save_agent_assignments(
            actor_user_id=manager, tenant_id=stack.tenant_id,
            connection_id=connection["id"], expected_revision=1,
            add_agent_ids=["agent-a"])
    assert caught.value.status == 403


def test_owner_may_assign_their_own_private_agent(stack, svc):
    manager = _member_with(
        stack, "conn-manager",
        ["external.connections.read", "external.connections.manage"],
        username="manager")
    stack.service.bind_agent(
        tenant_id=stack.tenant_id, agent_id="agent-c",
        private_owner_user_id=manager, origin="user_created")
    connection = _oa(svc, stack)
    result = svc.save_agent_assignments(
        actor_user_id=manager, tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_revision=1,
        add_agent_ids=["agent-c"])
    assert result["added"] == 1


def test_another_users_private_agent_is_never_listed_or_assignable(stack, svc):
    """A private Agent is invisible to — and unmanageable by — everyone else."""
    from integrations.external.errors import ExternalConnectionError
    owner = _member_with(
        stack, "conn-owner",
        ["external.connections.read", "external.connections.manage"],
        username="owner")
    other = _member_with(
        stack, "conn-other",
        ["external.connections.read", "external.connections.manage"],
        username="other")
    stack.service.bind_agent(
        tenant_id=stack.tenant_id, agent_id="agent-c",
        private_owner_user_id=owner, origin="user_created")
    connection = _oa(svc, stack)
    svc.save_agent_assignments(
        actor_user_id=owner, tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_revision=1,
        add_agent_ids=["agent-c"])
    # Another member with the same connection rights cannot see it …
    listed = svc.list_agent_assignments(
        actor_user_id=other, tenant_id=stack.tenant_id,
        connection_id=connection["id"])
    assert listed["total"] == 0 and listed["items"] == []
    candidates = svc.search_agent_candidates(
        actor_user_id=other, tenant_id=stack.tenant_id,
        connection_id=connection["id"], q="agent-c")
    assert candidates["items"] == []
    # … and cannot remove it by naming its id directly.
    with pytest.raises(ExternalConnectionError):
        svc.save_agent_assignments(
            actor_user_id=other, tenant_id=stack.tenant_id,
            connection_id=connection["id"], expected_revision=2,
            remove_agent_ids=["agent-c"])


def test_revoked_target_qualification_refuses_whole_save(stack, svc):
    from integrations.external.errors import ExternalConnectionError
    manager = _member_with(
        stack, "conn-manager",
        ["external.connections.read", "external.connections.manage"],
        username="manager")
    stack.service.bind_agent(
        tenant_id=stack.tenant_id, agent_id="agent-c",
        private_owner_user_id=manager, origin="user_created")
    connection = _oa(svc, stack)
    # The agent is re-owned by somebody else between "did you mean to add this"
    # and the save.
    stack.service._store.execute(
        "UPDATE agent_bindings SET private_owner_user_id=? WHERE agent_id=?",
        (stack.root, "agent-c"))
    with pytest.raises(ExternalConnectionError):
        svc.save_agent_assignments(
            actor_user_id=manager, tenant_id=stack.tenant_id,
            connection_id=connection["id"], expected_revision=1,
            add_agent_ids=["agent-c"])


# -- inheritance and cleanup ----------------------------------------------

def test_override_and_inherited_template_share_one_set(stack, svc):
    platform = svc.create_connection(
        actor_user_id=stack.root, scope="platform", kind="mcp", name="MCP",
        config=MCP_HEADER, secrets={"header": SECRET})
    svc.set_tenant_access(
        actor_user_id=stack.root, platform_connection_id=platform["id"],
        tenant_ids=[stack.tenant_id], expected_revision=1)
    override = svc.create_override(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        platform_connection_id=platform["id"], name="MCP override",
        config=MCP_HEADER, secrets={"header": SECRET})
    # Both ids resolve to the template's logical set (created by the grant).
    svc.save_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=override["id"], expected_revision=1,
        add_agent_ids=["agent-a"])
    by_template = svc.list_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=platform["id"])
    assert [item["id"] for item in by_template["items"]] == ["agent-a"]
    # Restoring inheritance keeps the assignment.
    svc.restore_inheritance(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        platform_connection_id=platform["id"],
        expected_version=override["version"])
    still = svc.list_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=platform["id"])
    assert [item["id"] for item in still["items"]] == ["agent-a"]


def test_delete_connection_clears_relation_and_audits(stack, svc):
    connection = _oa(svc, stack)
    svc.save_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_revision=1,
        add_agent_ids=["agent-a"])
    svc.delete_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_version=1)
    assert not stack.service._store.execute(
        "SELECT 1 FROM external_connection_agent_assignments WHERE tenant_id=?"
        " AND logical_connection_id=?", (stack.tenant_id, connection["id"]))
    assert not stack.service._store.execute(
        "SELECT 1 FROM external_connection_agent_assignment_sets"
        " WHERE tenant_id=? AND logical_connection_id=?",
        (stack.tenant_id, connection["id"]))
    audit = stack.service._store.execute(
        "SELECT redacted_changes FROM audit_events WHERE target=? AND"
        " action='external_connection.delete'",
        ("external_connection:%s" % connection["id"],))
    assert "agent_assignments_cleared" in audit[0]["redacted_changes"]


# -- card summary ----------------------------------------------------------

def test_card_summary_counts_only_visible_assignments(stack, svc):
    owner = _member_with(
        stack, "conn-owner",
        ["external.connections.read", "external.connections.manage"],
        username="owner")
    viewer = _member_with(
        stack, "conn-viewer", ["external.connections.read"], username="viewer")
    stack.service.bind_agent(
        tenant_id=stack.tenant_id, agent_id="agent-c",
        private_owner_user_id=owner, origin="user_created")
    connection = _oa(svc, stack)
    # A shared Agent (visible to every member) and one member's private Agent.
    svc.save_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_revision=1,
        add_agent_ids=["agent-a"])
    svc.save_agent_assignments(
        actor_user_id=owner, tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_revision=2,
        add_agent_ids=["agent-c"])
    catalogue = svc.list_catalog(
        actor_user_id=viewer, scope="tenant", tenant_id=stack.tenant_id)
    card = [c for c in catalogue["items"] if c["id"] == connection["id"]][0]
    # agent.c is private to its owner, so the viewer counts only the shared one;
    # and a read-only member may not assign at all.
    assert card["agent_assignment"]["visible_count"] == 1
    assert card["agent_assignment"]["configured"] is True
    assert card["agent_assignment"]["can_assign"] is False
