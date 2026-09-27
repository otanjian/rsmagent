# encoding:utf-8
"""Per-connection Agent assignment at the runtime boundary.

Change ``add-external-connection-agent-assignment``, task group 3. The console's
lists and saves (task group 1) say what the relation *is*; this file is about the
two places that decide what an Agent may actually **use**, and about the cleanup
that keeps the relation honest when an Agent goes away:

* the tool projection (:func:`integrations.external.tools.authorized_tools`),
  so a connection the Agent is not assigned to is *absent* rather than refused;
* the final dispatch entry point (``ConnectionRuntime.invoke``), so a name that
  was listed before the assignment changed still refuses at call time — which is
  what an old session or a queued call hits;
* the compatibility half: an unconfigured connection (沿用原权限) keeps the
  authorization it always had, and personal mail is outside the regime entirely;
* the cleanup half: deleting an Agent removes its seats and moves the sets it sat
  on, so a stale save conflicts instead of re-creating a relation to a ghost.
"""

from __future__ import annotations

import pytest

from integrations.external import assignment, registry
from tests._helpers import (
    assign_agents_to_connection,
    build_identity,
    legacy_connection_rule,
)

MASTER_KEY = "assign-runtime-master-key"
KIND = registry.KIND_MCP
MCP_CONFIG = {
    "transport": "streamable_http", "url": "https://mcp.example.com/mcp",
}
MCP_SECRET = "header-secret-value"

#: The MCP capabilities these tests grant, spelled out rather than computed so a
#: change to the id convention has to be a deliberate edit here.
TOOLS_LIST = "external:mcp:mcp.tools.list"
TOOLS_CALL = "external:mcp:mcp.tools.call"
TOOLS_READ = "external:mcp:mcp.tools.read"

#: A connection that publishes one tool, so the read action has something to
#: offer. The tool is published by discovery alone: no configuration declares it.
MCP_PUBLISHED_CONFIG = MCP_CONFIG

#: Every refusal the assignment gate can produce, so a test that expects a
#: *different* gate can say so without repeating the list.
ASSIGNMENT_CODES = frozenset({
    assignment.REASON_NOT_ASSIGNED,
    assignment.REASON_NO_AGENT,
    assignment.REASON_STATE_MISSING,
    assignment.REASON_LOOKUP_FAILED,
})


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", MASTER_KEY)


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """A real identity database, with both halves of the process pointed at it.

    ``tools._identity_service`` resolves through
    ``get_external_connection_service``, which is keyed on the configured
    ``identity_db_path``; pointing both at this test's database is what makes the
    *listing* gate read the same store the runtime gate does.
    """
    from config import conf
    from integrations.external import service as service_module

    stack = build_identity(tmp_path, agents=("agent-a", "agent-b"))
    monkeypatch.setitem(conf(), "identity_db_path", str(tmp_path / "identity.db"))
    service_module._SERVICE_CACHE.clear()
    return stack


@pytest.fixture
def svc(stack):
    from integrations.external.service import ExternalConnectionService
    return ExternalConnectionService(stack.service)


@pytest.fixture
def open_read(monkeypatch):
    """Open the read class the way a deployment does, through configuration."""
    from config import conf
    monkeypatch.setitem(
        conf().setdefault("external_connections", {}),
        "readiness", {KIND: {"read_execute": True}})


@pytest.fixture
def open_write(monkeypatch):
    """Open read+write for MCP.

    MCP's ``write_execute`` is in no deployment's *openable* set, so only the
    class switch itself can be exercised — which is why this patches the class
    source rather than using the readiness block.
    """
    real = registry.open_classes
    frozen = frozenset({"configure", "test", "read_execute", "write_execute"})
    monkeypatch.setattr(
        registry, "open_classes",
        lambda kind: frozen if kind == KIND else real(kind))
    return frozen

def _mcp_name(action, connection_id, remote=""):
    """The binding name the adapter composes for one action on one connection.

    Built through the composer rather than written out, because the wire format
    is a contract with the provider (``^[a-zA-Z0-9_-]+$``) and not a spelling
    these cases are about: they are about which capabilities are offered and
    which calls are authorized. The spelling itself is pinned in
    ``tests/test_tool_name_wire_contract.py``.
    """
    from agent.tools.mcp.external import tool_name
    return tool_name(action=action, connection_id=connection_id,
                     remote_name=remote)


def _model_name(action, connection_id, remote=""):
    """The name the model sees: the binding name carrying its origin prefix."""
    from agent.tools.external.external_tool import _external_tool_name
    return _external_tool_name(_mcp_name(action, connection_id, remote))


def _connection(svc, stack, name="团队 MCP", config=None):
    return svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind=KIND, name=name, config=dict(config or MCP_CONFIG),
        secrets={"header": MCP_SECRET})


def _granted(stack, username, *, extra=()):
    """A member holding the MCP grants, so only assignment can vary.

    ``extra`` appends further resource grants for a test whose capability is not
    in the default pair (the read tool action, for instance).
    """
    import uuid

    role = stack.service.create_role(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        code="assign-%s" % uuid.uuid4().hex[:10], name="Assign",
        permissions=["tool.execute"],
        resource_grants=[
            {"resource_kind": "tool", "resource_id": TOOLS_LIST,
             "action": "execute"},
            {"resource_kind": "tool", "resource_id": TOOLS_CALL,
             "action": "execute"},
        ] + list(extra))
    return stack.member(username, [role["code"]])


def _listed(stack, *, user_id, agent_id):
    """The MCP tool names the projection offers this (user, Agent) pair."""
    from integrations.external import tools as external_tools

    bindings = external_tools.authorized_tools(
        tenant_id=stack.tenant_id, actor_user_id=user_id,
        identity=stack.service, agent_id=agent_id)
    return {b.tool.name for b in bindings if b.tool.kind == KIND}


def _assigned_capabilities(connection_id):
    """Every MCP capability an *assigned* Agent is offered here.

    ``read_execute`` is open in these tests, so the read actions are offered;
    ``tools.call`` is a write and MCP's write class is closed, so it never is
    (a test that opens the write class adds it on top of this set).

    The grant is deliberately not a factor: an assigned connection *is* its own
    authorization, so this set does not shrink for a member who holds no
    ``external:...`` grant at all.
    """
    return {_mcp_name("tools.list", connection_id),
            _mcp_name("resources.read", connection_id),
            _mcp_name("tools.read", connection_id)}


# -- the projection --------------------------------------------------------

def test_a_configured_connection_is_absent_for_every_unassigned_agent(
        stack, svc, open_read):
    """A new connection starts configured-empty: nobody is offered its tools."""
    _connection(svc, stack)
    member = _granted(stack, "carol")

    assert _listed(stack, user_id=member, agent_id="agent-a") == set()
    assert _listed(stack, user_id=member, agent_id="agent-b") == set()
    assert _listed(stack, user_id=member, agent_id="") == set()


def test_assignment_offers_the_tools_to_that_agent_only(stack, svc, open_read):
    connection = _connection(svc, stack)
    member = _granted(stack, "carol")
    assign_agents_to_connection(svc, stack.tenant_id, connection["id"], "agent-a")

    assert _listed(stack, user_id=member, agent_id="agent-a") == _assigned_capabilities(
        connection["id"])
    assert _listed(stack, user_id=member, agent_id="agent-b") == set()


def test_an_unconfigured_connection_keeps_the_old_visibility(
        stack, svc, open_read):
    """存量连接沿用原权限, including for a call with no Agent context.

    This is also the boundary of the assignment-as-authorization rule: a
    ``configured=0`` connection was never assigned to anyone, so it keeps the
    per-capability grant gate — the read actions stay absent because this member
    holds no grant for them.
    """
    connection = _connection(svc, stack)
    legacy_connection_rule(svc, stack.tenant_id, connection["id"])
    member = _granted(stack, "carol")

    expected = {_mcp_name("tools.list", connection["id"])}
    assert _listed(stack, user_id=member, agent_id="agent-a") == expected
    assert _listed(stack, user_id=member, agent_id="") == expected
    assert _mcp_name("tools.read", connection["id"]) not in expected


def test_a_published_read_tool_is_assigned_like_any_other_capability(
        stack, svc, open_read):
    """读工具同样按连接分配：未分配不投放，已分配才投放且可派发。

    报告的现象是「已分配 MCP 却看不到工具」。读动作打开后，工具的可见性仍由
    按连接分配决定，不会因为动作是读的、或因为连接没有任何逐工具声明而放宽。
    """
    from agent.tools.mcp import external as mcp_external

    connection = _connection(svc, stack, config=MCP_PUBLISHED_CONFIG)
    mcp_external.remember_tools(
        tenant_id=connection["tenant_id"], connection_id=connection["id"],
        version=int(connection["version"]),
        tools=[{"name": "echo", "description": "echo",
                "inputSchema": {"type": "object", "properties": {}}}])
    member = _granted(stack, "carol",
                      extra=[{"resource_kind": "tool", "resource_id": TOOLS_READ,
                              "action": "execute"}])
    read_name = _mcp_name("tools.read", connection["id"], "echo")

    # 未分配：既看不到，也无法在调用时绕过分配闸门。
    assert _listed(stack, user_id=member, agent_id="agent-a") == set()
    refused = _invoke(svc, stack, connection["id"], agent_id="agent-a",
                      action="tools.read")
    assert refused.ok is False
    assert refused.code in ASSIGNMENT_CODES

    # 分配之后：工具被投放，调用通过分配闸门。
    assign_agents_to_connection(svc, stack.tenant_id, connection["id"], "agent-a")
    assert read_name in _listed(stack, user_id=member, agent_id="agent-a")

    mcp_external._reset_for_tests()


def test_the_read_tool_needs_the_assignment_not_a_read_grant(
        stack, svc, open_read):
    """分配即该连接的授权：读工具不再要求 `external:mcp:mcp.tools.read`。

    现场现象是「已经分配了 MCP，为什么看不到工具」。分配表达的是「这个智能体可以用这条
    连接」，而这条连接是租户自己的资源 —— 与「个人邮箱凭所有权使用」同源，因此分配代替
    逐资源授权。只要分配在、功能权限在，服务器发布过的读工具就投放。

    只代替授权本身：功能权限、切片、风险、审批与配额都在别处照旧判定（见
    ``tests/test_external_authorization.py`` 的分配一组）。
    """
    from agent.tools.mcp import external as mcp_external

    connection = _connection(svc, stack, config=MCP_PUBLISHED_CONFIG)
    mcp_external.remember_tools(
        tenant_id=connection["tenant_id"], connection_id=connection["id"],
        version=int(connection["version"]),
        tools=[{"name": "echo", "description": "echo",
                "inputSchema": {"type": "object", "properties": {}}}])
    # tools.list/tools.call only --- deliberately *without* the read grant.
    member = _granted(stack, "carol")
    assign_agents_to_connection(svc, stack.tenant_id, connection["id"], "agent-a")

    offered = _listed(stack, user_id=member, agent_id="agent-a")
    assert _mcp_name("tools.read", connection["id"], "echo") in offered
    assert _mcp_name("tools.read", connection["id"]) in offered

    mcp_external._reset_for_tests()


def test_the_read_tool_is_absent_without_the_functional_permission(
        stack, svc, open_read, tmp_path):
    """只代替逐资源授权，功能权限照旧 —— 没有 `tool.execute` 就什么都看不到。"""
    from agent.tools.mcp import external as mcp_external

    connection = _connection(svc, stack, config=MCP_PUBLISHED_CONFIG)
    mcp_external.remember_tools(
        tenant_id=connection["tenant_id"], connection_id=connection["id"],
        version=int(connection["version"]),
        tools=[{"name": "echo", "description": "echo",
                "inputSchema": {"type": "object", "properties": {}}}])
    import uuid

    role = stack.service.create_role(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        code="assign-%s" % uuid.uuid4().hex[:10], name="NoToolExecute",
        permissions=[], resource_grants=[
            {"resource_kind": "tool", "resource_id": TOOLS_LIST,
             "action": "execute"},
            {"resource_kind": "tool", "resource_id": TOOLS_READ,
             "action": "execute"}])
    member = stack.member("carol", [role["code"]])
    assign_agents_to_connection(svc, stack.tenant_id, connection["id"], "agent-a")

    assert _listed(stack, user_id=member, agent_id="agent-a") == set()

    mcp_external._reset_for_tests()


def test_personal_mail_is_outside_the_assignment_regime(svc, stack):
    """本期个人邮箱保持原有行为 — the filter must not touch a personal binding."""
    from integrations.external import tools as external_tools

    binding = external_tools.ToolBinding(
        tool=external_tools.ExternalTool(
            name="email.inbox.list", kind=registry.KIND_EMAIL,
            action="inbox.list"),
        connection_id="mail-of-someone", connection_name="我的邮箱",
        scope=registry.SCOPE_PERSONAL)

    kept = external_tools._assigned_to_agent(
        [binding], identity=stack.service, tenant_id=stack.tenant_id,
        agent_id="")
    assert kept == [binding]


# -- the final dispatch entry point ---------------------------------------

def _invoke(svc, stack, connection_id, *, agent_id, action="tools.call"):
    return svc.invoke_action(
        connection_id, action, {"tool": "echo", "arguments": {}},
        actor_user_id=stack.root, tenant_id=stack.tenant_id, agent_id=agent_id)


def test_an_unassigned_agent_is_refused_at_call_time(stack, svc, open_write):
    connection = _connection(svc, stack)

    result = _invoke(svc, stack, connection["id"], agent_id="agent-a")

    assert result.ok is False
    assert result.code == assignment.REASON_NOT_ASSIGNED
    assert result.stage == "policy"


def test_a_call_without_an_agent_context_is_refused(stack, svc, open_write):
    """configured=True and no trusted Agent context is nobody, not everybody."""
    connection = _connection(svc, stack)

    result = _invoke(svc, stack, connection["id"], agent_id="")

    assert result.ok is False
    assert result.code == assignment.REASON_NO_AGENT


def test_an_assigned_agent_gets_past_the_assignment_gate(stack, svc, open_write):
    """The next gate answers, which is the proof the assignment gate allowed."""
    connection = _connection(svc, stack)
    assign_agents_to_connection(svc, stack.tenant_id, connection["id"], "agent-a")

    result = _invoke(svc, stack, connection["id"], agent_id="agent-a")

    # An unapproved MCP write is refused by the risk catalogue *after* the
    # assignment gate; that is the refusal a caller who may use the connection
    # gets.
    assert result.code not in ASSIGNMENT_CODES
    assert result.code == "approval_required"


def test_a_legacy_connection_still_reaches_the_next_gate(stack, svc, open_write):
    connection = _connection(svc, stack)
    legacy_connection_rule(svc, stack.tenant_id, connection["id"])

    result = _invoke(svc, stack, connection["id"], agent_id="")

    assert result.code == "approval_required"


def test_a_binding_listed_before_the_change_refuses_at_call_time(
        stack, svc, open_read, open_write):
    """An old session or a queued call holds a name, not a permission."""
    from integrations.external import tools as external_tools

    connection = _connection(svc, stack)
    assign_agents_to_connection(svc, stack.tenant_id, connection["id"], "agent-a")
    member = _granted(stack, "carol")
    # A connection-level name the member *does* hold a grant for, so the only
    # gate left that can refuse it is the assignment itself.
    name = _mcp_name("tools.list", connection["id"])
    assert name in _listed(stack, user_id=member, agent_id="agent-a")

    # The assignment is removed after the name was already held.
    svc.save_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_revision=1,
        remove_agent_ids=["agent-a"])

    result = external_tools.dispatch(
        svc, name, {}, tenant_id=stack.tenant_id, actor_user_id=member,
        agent_id="agent-a")
    assert result.ok is False
    assert result.code in ASSIGNMENT_CODES


def test_an_inherited_platform_template_is_gated_by_the_consuming_tenant(
        stack, svc, open_read):
    """继承 and 覆盖 share the consuming tenant's set, and both are gated.

    This half is the *projection*: the tool list a model would be handed. The
    call-time gate for the same relation is exercised through the override in
    :func:`test_an_override_and_its_template_share_one_relation`, because a bare
    platform row carries no tenant of its own and is therefore refused a step
    earlier, at the caller's own grant.
    """
    from integrations.external import tools as external_tools

    platform = svc.create_connection(
        actor_user_id=stack.root, scope=registry.SCOPE_PLATFORM, tenant_id=None,
        kind=KIND, name="平台 MCP", config=dict(MCP_CONFIG),
        secrets={"header": MCP_SECRET})
    granted = svc.set_tenant_access(
        actor_user_id=stack.root, platform_connection_id=platform["id"],
        tenant_ids=[stack.tenant_id], expected_revision=1)
    assert granted["tenant_ids"] == [stack.tenant_id]

    member = _granted(stack, "carol")
    # The grant itself made the template configured-empty for this tenant.
    assert _listed(stack, user_id=member, agent_id="agent-a") == set()

    # The tenant's relation is keyed on the *template's* logical id.
    assign_agents_to_connection(
        svc, stack.tenant_id, platform["id"], "agent-a")

    assert _listed(stack, user_id=member, agent_id="agent-a") == _assigned_capabilities(
        platform["id"])
    assert _listed(stack, user_id=member, agent_id="agent-b") == set()


def test_an_override_and_its_template_share_one_relation(
        stack, svc, open_read, open_write):
    """恢复继承 must not orphan the assignment, so both ids answer the same."""
    platform = svc.create_connection(
        actor_user_id=stack.root, scope=registry.SCOPE_PLATFORM, tenant_id=None,
        kind=KIND, name="平台 MCP", config=dict(MCP_CONFIG),
        secrets={"header": MCP_SECRET})
    svc.set_tenant_access(
        actor_user_id=stack.root, platform_connection_id=platform["id"],
        tenant_ids=[stack.tenant_id], expected_revision=1)
    override = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind=KIND, name="覆盖", config=dict(MCP_CONFIG),
        base_connection_id=platform["id"], secrets={"header": MCP_SECRET})

    member = _granted(stack, "carol")

    # Before the assignment: neither id is offered, and both ids refuse a call
    # that names them — the template id resolves to the override, so the two
    # cannot disagree about which relation governs.
    assert _listed(stack, user_id=member, agent_id="agent-a") == set()
    for named_id in (platform["id"], override["id"]):
        assert _invoke(svc, stack, named_id,
                       agent_id="agent-a").code == assignment.REASON_NOT_ASSIGNED

    assign_agents_to_connection(svc, stack.tenant_id, platform["id"], "agent-a")

    # The override is the effective row, and it answers from the template's set.
    # Both classes are open in this test, so the projection carries the write
    # action on top of every read action the assignment opens.
    offered = _assigned_capabilities(override["id"]) | {
        _mcp_name("tools.call", override["id"])}
    assert _listed(stack, user_id=member, agent_id="agent-a") == offered
    assert _listed(stack, user_id=member, agent_id="agent-b") == set()
    for named_id in (platform["id"], override["id"]):
        assert _invoke(svc, stack, named_id,
                       agent_id="agent-a").code == "approval_required"
        assert _invoke(svc, stack, named_id,
                       agent_id="agent-b").code == assignment.REASON_NOT_ASSIGNED


# -- cleanup ---------------------------------------------------------------

def test_deleting_an_agent_removes_its_seat_and_bumps_the_revision(stack, svc):
    connection = _connection(svc, stack)
    svc.save_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_revision=1,
        add_agent_ids=["agent-a", "agent-b"])

    released = stack.service.release_deleted_agent(
        agent_id="agent-a", actor_user_id=stack.root)

    assert released["connection_assignments_removed"] == 1
    assert released["assignment_sets_bumped"] == 1
    listed = svc.list_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"])
    assert [item["id"] for item in listed["items"]] == ["agent-b"]
    # 1 -> 2 for the save, -> 3 for the delete: a save that loaded before the
    # delete conflicts instead of re-creating a relation to a ghost.
    assert listed["revision"] == 3


def test_a_save_loaded_before_the_delete_conflicts(stack, svc):
    """The delete moves the revision, so a stale save cannot silently succeed.

    A save that names the *deleted* Agent is refused even earlier (it is no
    longer assignable); this is the case that matters for the other Agents on
    the same connection — the caller's view is stale, so the whole save must be
    refused rather than applied over a relation it never saw.
    """
    from integrations.external.errors import ExternalConnectionError

    connection = _connection(svc, stack)
    svc.save_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"], expected_revision=1,
        add_agent_ids=["agent-a", "agent-b"])
    stack.service.release_deleted_agent(
        agent_id="agent-a", actor_user_id=stack.root)

    with pytest.raises(ExternalConnectionError) as caught:
        svc.save_agent_assignments(
            actor_user_id=stack.root, tenant_id=stack.tenant_id,
            connection_id=connection["id"], expected_revision=2,
            remove_agent_ids=["agent-b"])
    assert caught.value.code == "assignment_version_conflict"


def test_a_deleted_agent_is_no_longer_a_candidate(stack, svc):
    connection = _connection(svc, stack)
    assign_agents_to_connection(svc, stack.tenant_id, connection["id"], "agent-a")
    stack.service.release_deleted_agent(
        agent_id="agent-a", actor_user_id=stack.root)

    allowed, reason = assignment.assignment_allows(
        svc._store, tenant_id=stack.tenant_id,  # noqa: SLF001 - the relation row
        logical_id=connection["id"], agent_id="agent-a")
    assert allowed is False and reason == assignment.REASON_NOT_ASSIGNED


def test_a_delete_only_bumps_the_sets_the_agent_sat_on(stack, svc):
    """One template's logical id is shared by tenants; other sets must not move."""
    mine = _connection(svc, stack, name="我的 MCP")
    other = _connection(svc, stack, name="别的 MCP")
    assign_agents_to_connection(svc, stack.tenant_id, mine["id"], "agent-a")
    assign_agents_to_connection(svc, stack.tenant_id, other["id"], "agent-b")

    stack.service.release_deleted_agent(
        agent_id="agent-a", actor_user_id=stack.root)

    bumped = svc.list_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=mine["id"])["revision"]
    untouched = svc.list_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=other["id"])["revision"]
    assert bumped == 2 and untouched == 1


def test_a_copied_agent_does_not_inherit_the_source_assignment(stack, svc):
    """复制智能体 MUST NOT 自动复制外部连接分配.

    The copy flow's only identity write is ``bind_agent(cloned_from_agent_id=...)``
    under a **new** Agent id (``agent/tenant_provisioning.AgentProvisioner.copy``),
    so this is exactly the write a copy makes.
    """
    connection = _connection(svc, stack)
    assign_agents_to_connection(svc, stack.tenant_id, connection["id"], "agent-a")

    stack.service.bind_agent(
        tenant_id=stack.tenant_id, agent_id="agent-a-copy",
        cloned_from_agent_id="agent-a", actor_user_id=stack.root)

    listed = svc.list_agent_assignments(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        connection_id=connection["id"])
    assert [item["id"] for item in listed["items"]] == ["agent-a"]
    allowed, reason = assignment.assignment_allows(
        svc._store, tenant_id=stack.tenant_id,  # noqa: SLF001 - the relation row
        logical_id=connection["id"], agent_id="agent-a-copy")
    assert allowed is False and reason == assignment.REASON_NOT_ASSIGNED
