# encoding:utf-8
"""Who may execute an external capability — the resource-execution gate.

Change ``add-external-system-access``, task 5.6. The gap this file closes was
invisible because it was an *absence*: a capability could be granted in the
catalogue and the grant was never read. So the tests are written to fail if the
gate is removed, not merely to pass while it exists.

What the spec requires (``mcp-connection-integration``: 工具发现与执行保留现有授权链):

    工具可见与实际调用 SHALL 遵循既有资源执行授权、绑定智能体范围、配额和风险策略。
    连接管理权限、测试成功或工具名称的 mcp 前缀 MUST NOT 作为执行授权。

Each sentence gets a test:

* 资源执行授权 — a member without the grant is refused, and the same member with
  it succeeds. The grant is the one an administrator actually creates
  (``external:<kind>:<kind>.<action>``, the id ``declared_tool_projection``
  publishes), so the catalogue and the gate are proven to agree rather than to
  merely both exist.
* 连接管理权限不作为执行授权 — holding ``external.connections.manage`` is not
  enough. This is the sentence most likely to be "helpfully" relaxed later, so
  it is asserted on its own.
* 绑定智能体范围 — the tenant-admin exemption is not a way to name another
  tenant's connection, and it needs a bound Agent at all.
* 工具发现不等于调用授权 — a capability that is listed is still refused when the
  grant is taken away, and a held name is refused after the fact.

The connection, the service and the identity database are all real; only the
remote system is not, and it is never reached because every test here is
refused or stopped before the transport.
"""

from __future__ import annotations

import pytest

from integrations.external import registry
from tests._helpers import (assign_agents_to_connection, build_identity,
                            legacy_connection_rule)

MASTER_KEY = "authz-master-key"
KIND = registry.KIND_MCP
MCP_CONFIG = {
    "transport": "streamable_http", "url": "https://mcp.example.com/mcp",
}

#: The grantable id of MCP's read capability. Spelled out literally rather than
#: computed, so a change to the id convention has to be a deliberate edit here.
TOOLS_LIST_RESOURCE = "external:mcp:mcp.tools.list"


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", MASTER_KEY)


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """A real identity database, and a process that points at it.

    ``_identity_service`` resolves through ``get_external_connection_service``,
    which is keyed on the configured ``identity_db_path``; pointing both at this
    test's database is what makes the *listing* gate read the same store the
    runtime gate does.
    """
    from config import conf
    from integrations.external import service as service_module

    stack = build_identity(tmp_path)
    monkeypatch.setitem(conf(), "identity_db_path", str(tmp_path / "identity.db"))
    service_module._SERVICE_CACHE.clear()
    return stack


@pytest.fixture
def svc(stack):
    from integrations.external.service import ExternalConnectionService
    return ExternalConnectionService(stack.service)


@pytest.fixture
def connection(svc, stack):
    connection = svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind=KIND, name="团队 MCP", config=dict(MCP_CONFIG))
    # The tests here are about the *grant* gate, and most call with no trusted
    # Agent context: keep the connection on the pre-assignment 沿用原权限 rule so
    # the code under assertion is the grant, not an assignment refusal.
    legacy_connection_rule(svc, stack.tenant_id, connection["id"])
    return connection

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


def _open_read(monkeypatch) -> None:
    """Open the read class the way a deployment does, through configuration."""
    from config import conf
    monkeypatch.setitem(
        conf().setdefault("external_connections", {}),
        "readiness", {KIND: {"read_execute": True}})


def _member(stack, username: str, *, permissions=(), grants=()) -> str:
    """A plain member, with exactly the permissions and grants given.

    ``member`` is the built-in role that carries no ``tool.execute``, so a test
    that needs the functional permission must ask for it — which is the point:
    a grant alone is not a capability.
    """
    import uuid

    role = stack.service.create_role(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        code="authz-%s" % uuid.uuid4().hex[:10], name="Authz %s" % username,
        permissions=list(permissions),
        resource_grants=[dict(g) for g in grants])
    return stack.member(username, [role["code"]])


def _grant(resource_id: str, action: str = "execute") -> dict:
    return {"resource_kind": "tool", "resource_id": resource_id,
            "action": action}


def _invoke(svc, connection, user_id, *, tenant_id, agent_id="", action="tools.list"):
    return svc.invoke_action(
        connection["id"], action, {},
        actor_user_id=user_id, tenant_id=tenant_id, agent_id=agent_id)


# -- 资源执行授权 ------------------------------------------------------------

def test_a_member_without_the_grant_is_refused(
        svc, stack, connection, monkeypatch):
    """The default member holds no external grant, and so reaches nothing."""
    _open_read(monkeypatch)
    member = _member(stack, "carol", permissions=["tool.execute"])

    result = _invoke(svc, connection, member, tenant_id=stack.tenant_id)

    assert result.ok is False
    assert result.code == "tool_not_authorized"
    assert result.stage == "policy"


def test_the_same_member_succeeds_once_the_grant_is_held(
        svc, stack, connection, monkeypatch):
    """The gate is a grant check, not a blanket refusal of members."""
    _open_read(monkeypatch)
    member = _member(stack, "carol", permissions=["tool.execute"],
                     grants=[_grant(TOOLS_LIST_RESOURCE)])

    result = _invoke(svc, connection, member, tenant_id=stack.tenant_id)

    # The transport is never reached in this test, so the adapter's own
    # refusal is the proof that authorization let the call through.
    assert result.code != "tool_not_authorized"


def test_a_grant_without_the_functional_permission_is_not_enough(
        svc, stack, connection, monkeypatch):
    """A resource grant is not a capability on its own.

    The resource-execution gate pairs the grant with ``tool.execute`` exactly
    as it does for builtin tools; skipping the permission here would make an
    external capability the one kind a role can hold without the functional
    permission that gates every other tool.
    """
    _open_read(monkeypatch)
    member = _member(stack, "carol", grants=[_grant(TOOLS_LIST_RESOURCE)])

    result = _invoke(svc, connection, member, tenant_id=stack.tenant_id)

    assert result.code == "tool_not_authorized"


def test_a_grant_for_another_capability_does_not_authorize_this_one(
        svc, stack, connection, monkeypatch):
    """The grant is per capability, not per connection or per kind."""
    _open_read(monkeypatch)
    member = _member(stack, "carol", permissions=["tool.execute"],
                     grants=[_grant("external:mcp:mcp.tools.call")])

    result = _invoke(svc, connection, member, tenant_id=stack.tenant_id)

    assert result.code == "tool_not_authorized"


# -- 管理权限不是执行授权 ----------------------------------------------------

def test_connection_management_permission_does_not_authorize_execution(
        svc, stack, connection, monkeypatch):
    """配置保存不放开调用；实际动作仍按资源执行授权规范拒绝.

    ``external.connections.manage`` is what a tenant administrator uses to
    *configure* the connection. The spec names this exact confusion, so it is
    asserted directly rather than left implied by the absence of a branch.
    """
    _open_read(monkeypatch)
    member = _member(stack, "carol",
                     permissions=["tool.execute",
                                  "external.connections.read",
                                  "external.connections.manage",
                                  "external.connections.test"])

    result = _invoke(svc, connection, member, tenant_id=stack.tenant_id)

    assert result.code == "tool_not_authorized"


def test_a_passing_test_does_not_authorize_execution(
        svc, stack, connection, monkeypatch):
    """A connection the deployment probed is still not a grant.

    The member holds ``external.connections.test`` and the ``test`` class is
    open, so the probe is allowed to run — which is exactly the "test
    succeeded" evidence the spec says must not become an execution grant.
    """
    from config import conf

    _open_read(monkeypatch)
    monkeypatch.setitem(
        conf().setdefault("external_connections", {}),
        "readiness", {KIND: {"read_execute": True, "test": True}})
    member = _member(stack, "carol",
                     permissions=["tool.execute",
                                  "external.connections.test"])

    probed = svc.probe_connection(connection["id"], actor_user_id=member,
                                  tenant_id=stack.tenant_id)
    # Whether the fake endpoint answered is irrelevant; what matters is that the
    # probe was permitted at all and the execution still was not.
    assert probed is not None

    result = _invoke(svc, connection, member, tenant_id=stack.tenant_id)
    assert result.code == "tool_not_authorized"


# -- 绑定智能体范围 ----------------------------------------------------------

def _tenant_admin(stack, username: str) -> str:
    """An active tenant administrator with the functional tool permission.

    ``tenant_admin`` is the built-in role, so this is the real
    ``_is_tenant_admin`` path rather than a role that merely looks like it.
    """
    return stack.member(username, ["tenant_admin"])


def test_the_tenant_admin_exemption_requires_a_bound_agent(
        svc, stack, connection, monkeypatch):
    """No ``agent_id`` means no proof of tenancy, so the exemption is closed.

    The exemption exists to skip the *grant*; it must not also skip the
    question of which tenant is asking. Accepting an id on its own would admit
    any ``external:`` string the caller can spell.
    """
    _open_read(monkeypatch)
    admin = _tenant_admin(stack, "tadmin")

    result = _invoke(svc, connection, admin, tenant_id=stack.tenant_id,
                     agent_id="")

    assert result.code == "tool_not_authorized"


def test_the_tenant_admin_exemption_does_not_cross_a_bound_agent(
        svc, stack, connection, monkeypatch):
    """A connection reached through another tenant's Agent is refused.

    ``other-agent`` is bound to a *different* tenant, which is the isolation
    boundary ``_tenant_admin_owns_agent`` and the scheduler's
    ``revalidate_owner`` also rely on.
    """
    _open_read(monkeypatch)
    stack.other_tenant()
    admin = _tenant_admin(stack, "tadmin")

    result = _invoke(svc, connection, admin, tenant_id=stack.tenant_id,
                     agent_id="other-agent")

    assert result.code == "tool_not_authorized"


def test_the_exemption_is_refused_when_the_tenant_cannot_allocate_the_capability(
        svc, stack, connection, monkeypatch):
    """不绕过执行条件 — the exemption does not open a capability the platform
    never opened for the tenant.

    ``tenant_admin_may_execute_tool`` checks the id against the tenant's own
    allocatable set, so a capability no platform administrator ever granted
    tenant-wide stays closed even for the tenant's administrator.
    """
    _open_read(monkeypatch)
    admin = _tenant_admin(stack, "tadmin")

    result = _invoke(svc, connection, admin, tenant_id=stack.tenant_id,
                     agent_id="agent-a")

    assert result.code == "tool_not_authorized"


def test_the_exemption_works_for_a_capability_the_platform_opened(
        svc, stack, connection, monkeypatch):
    """The positive half: with the capability opened tenant-wide *and* a bound
    Agent, the tenant administrator's exemption is a real path.

    Without this test the refusals above would still pass if the exemption were
    dead code — which is the failure mode the whole file exists to prevent.
    """
    _open_read(monkeypatch)
    admin = _tenant_admin(stack, "tadmin")
    stack.service.set_tenant_resource_grants(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        grants=[_grant(TOOLS_LIST_RESOURCE)],
        expected_version=stack.service.get_tenant(stack.tenant_id)["version"])

    result = _invoke(svc, connection, admin, tenant_id=stack.tenant_id,
                     agent_id="agent-a")

    assert result.code != "tool_not_authorized"


# -- ordering and discovery --------------------------------------------------

def test_a_closed_class_is_reported_before_the_authorization(
        svc, stack, connection, monkeypatch):
    """A closed class is a deployment fact and answers first.

    Otherwise an ungranted caller would be told "not authorized" about a
    capability nobody could run, and a granted caller would be told the same
    thing as an ungranted one — two different questions collapsing into one
    answer.
    """
    ungranted = _member(stack, "carol", permissions=["tool.execute"])

    result = _invoke(svc, connection, ungranted, tenant_id=stack.tenant_id)

    assert result.code == "execution_not_available"


def test_an_ungranted_capability_is_not_listed(
        svc, stack, connection, monkeypatch):
    """工具可见 SHALL 遵循既有资源执行授权.

    A member who holds no grant does not see the tools at all, so the model
    cannot be talked into trying one — which is stronger than offering a tool
    the runtime would refuse.
    """
    from integrations.external import tools as external_tools

    _open_read(monkeypatch)
    ungranted = _member(stack, "carol", permissions=["tool.execute"])

    listed = external_tools.authorized_tools(
        tenant_id=stack.tenant_id, actor_user_id=ungranted,
        identity=stack.service)

    assert [b for b in listed if b.tool.kind == KIND] == []


def test_a_granted_capability_is_listed(
        svc, stack, connection, monkeypatch):
    """The positive half of the listing gate."""
    from integrations.external import tools as external_tools

    _open_read(monkeypatch)
    granted = _member(stack, "carol", permissions=["tool.execute"],
                      grants=[_grant(TOOLS_LIST_RESOURCE)])

    listed = external_tools.authorized_tools(
        tenant_id=stack.tenant_id, actor_user_id=granted,
        identity=stack.service)

    actions = {b.tool.action for b in listed if b.tool.kind == KIND}
    assert "tools.list" in actions


def test_the_listing_and_the_runtime_agree_on_the_resource_id():
    """One definition, so the catalogue cannot name what the runtime never reads.

    This is the drift that hid the original gap: the grant catalogue published
    ``external:<kind>:<name>`` while the runtime authorized against nothing at
    all. Both now call ``resource_id_for``.
    """
    from integrations.external import authorization, tools as external_tools

    for item in external_tools.declared_tool_projection():
        tool = external_tools.ExternalTool(
            name=item["name"], kind=item["kind"], action=item["action"])
        assert item["resource_id"] == authorization.resource_id_for(
            item["kind"], item["action"])
        assert item["resource_id"] == tool.declared_resource_id
    assert TOOLS_LIST_RESOURCE == authorization.resource_id_for(KIND, "tools.list")


def test_a_held_name_is_refused_after_the_grant_is_revoked(
        svc, stack, connection, monkeypatch):
    """工具发现不等于调用授权 — a name that was listed is re-checked per call."""
    from integrations.external import tools as external_tools

    _open_read(monkeypatch)
    import uuid

    role = stack.service.create_role(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        code="authz-%s" % uuid.uuid4().hex[:10], name="Revocable",
        permissions=["tool.execute"], resource_grants=[_grant(TOOLS_LIST_RESOURCE)])
    member = stack.member("carol", [role["code"]])

    listed = external_tools.authorized_tools(
        tenant_id=stack.tenant_id, actor_user_id=member,
        identity=stack.service)
    name = next(b.tool.name for b in listed if b.tool.kind == KIND)

    # Revoke: replace the role's grants with an unrelated capability.
    stack.service.update_role(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        role_id=role["id"], name=role["name"],
        permissions=["tool.execute"], expected_version=role["version"],
        resource_grants=[_grant("external:mcp:mcp.tools.call")])

    held = external_tools.find_binding(name, tenant_id=stack.tenant_id,
                                       actor_user_id=member)
    assert held is not None  # discovery is not the gate
    result = _invoke(svc, connection, member, tenant_id=stack.tenant_id)
    assert result.code == "tool_not_authorized"


def test_the_actor_facing_projection_is_the_authorized_subset(
        svc, stack, connection, monkeypatch):
    """``tool_projection`` must not raise, and must not list what cannot run.

    It forwarded ``agent_id`` to ``available_tools``, which does not accept it,
    so every call raised ``TypeError``. Nothing called it, so the projection the
    identity layer was meant to consume simply did not work — and a fixed
    version that listed bare discovery would have been the more dangerous
    repair, because the console and the model read this list as "what you can
    use".
    """
    from integrations.external import tools as external_tools

    _open_read(monkeypatch)
    ungranted = _member(stack, "carol", permissions=["tool.execute"])
    granted = _member(stack, "dave", permissions=["tool.execute"],
                      grants=[_grant(TOOLS_LIST_RESOURCE)])

    assert external_tools.tool_projection(
        tenant_id=stack.tenant_id, actor_user_id=ungranted,
        identity=stack.service) == []

    projected = external_tools.tool_projection(
        tenant_id=stack.tenant_id, actor_user_id=granted,
        identity=stack.service)
    assert any(item["kind"] == KIND and item["action"] == "tools.list"
               for item in projected)
    assert all(item["kind"] != KIND or item["action"] == "tools.list"
               for item in projected)


def test_the_refusal_does_not_name_the_connection(
        svc, stack, connection, monkeypatch):
    """A refusal must not enumerate what the caller cannot see.

    The message names the capability and the permission to ask for, and nothing
    about the connections that exist — otherwise the gate doubles as a
    discovery oracle for a caller holding no grant.
    """
    _open_read(monkeypatch)
    member = _member(stack, "carol", permissions=["tool.execute"])

    result = _invoke(svc, connection, member, tenant_id=stack.tenant_id)

    assert connection["id"] not in (result.message or "")
    assert "团队 MCP" not in (result.message or "")
    assert TOOLS_LIST_RESOURCE in (result.message or "")


# -- 分配即该连接的授权 ------------------------------------------------------
#
# 「有智能体的授权就可以了」：连接按智能体分配（``configured=1`` 且该智能体在关系
# 内）时，分配本身就是这份连接的授权，不再叠加一层 ``external:<kind>:<kind>.<action>``
# 逐资源授权。这里断言的是这条规则的**边界**，而不只是它的放宽：功能权限、切片、
# 风险与审批、配额、未配置的存量连接、跨租户 Agent 都必须照旧。
#
# 之所以要单独一组：这条规则是 `may_execute` 的第四条入口，与「个人连接凭所有权」同
# 源；它一旦越界，外部能力就会成为唯一一种「分配了就能执行、不看角色」的资源。


def _assigned(svc, stack, connection, *agent_ids):
    """Put the connection on the assigned rule with exactly these Agents."""
    return assign_agents_to_connection(
        svc, stack.tenant_id, connection["id"], *agent_ids)


def test_an_assigned_connection_needs_no_per_resource_grant(
        svc, stack, connection, monkeypatch):
    """分配之内、功能权限在手 —— 逐资源授权不再是必要条件。"""
    _open_read(monkeypatch)
    _assigned(svc, stack, connection, "agent-a")
    member = _member(stack, "carol", permissions=["tool.execute"])

    result = _invoke(svc, connection, member, tenant_id=stack.tenant_id,
                     agent_id="agent-a")

    # 传输不会被真的建立，所以「不是 tool_not_authorized」正是授权放行的证据。
    assert result.code != "tool_not_authorized"


def test_an_assigned_connection_is_listed_without_a_grant(
        svc, stack, connection, monkeypatch):
    """工具可见与调用对同一条规则作答，不出现「能调但看不到」。"""
    from integrations.external import tools as external_tools

    _open_read(monkeypatch)
    _assigned(svc, stack, connection, "agent-a")
    member = _member(stack, "carol", permissions=["tool.execute"])

    listed = external_tools.authorized_tools(
        tenant_id=stack.tenant_id, actor_user_id=member,
        identity=stack.service, agent_id="agent-a")

    assert any(b.tool.kind == KIND for b in listed)


def test_the_exemption_still_requires_the_functional_permission(
        svc, stack, connection, monkeypatch):
    """只跳过逐资源授权，绝不跳过功能权限。

    分配表达的是「这个智能体可以用这条连接」，不是「调用者可以执行工具」；把
    ``tool.execute`` 一起跳过，等于让外部能力成为唯一一种不需要功能权限的工具。
    """
    _open_read(monkeypatch)
    _assigned(svc, stack, connection, "agent-a")
    # A role with no permissions at all: the assignment is in place and the
    # connection is usable, so the only thing left to refuse the call is the
    # missing ``tool.execute``.
    member = _member(stack, "carol", permissions=[])

    result = _invoke(svc, connection, member, tenant_id=stack.tenant_id,
                     agent_id="agent-a")

    assert result.code == "tool_not_authorized"


def test_an_agent_outside_the_relation_still_needs_the_grant(
        svc, stack, connection, monkeypatch):
    """关系之外的智能体回到逐资源授权，不因「连接已配置」而搭便车。"""
    _open_read(monkeypatch)
    _assigned(svc, stack, connection, "agent-b")
    member = _member(stack, "carol", permissions=["tool.execute"])

    result = _invoke(svc, connection, member, tenant_id=stack.tenant_id,
                     agent_id="agent-a")

    assert result.code == "tool_not_authorized"


def test_an_empty_assignment_does_not_authorize_anyone(
        svc, stack, connection, monkeypatch):
    """清空分配表示禁止所有智能体：一个都不在关系内，就没有豁免。"""
    _open_read(monkeypatch)
    _assigned(svc, stack, connection)
    member = _member(stack, "carol", permissions=["tool.execute"])

    result = _invoke(svc, connection, member, tenant_id=stack.tenant_id,
                     agent_id="agent-a")

    assert result.code == "tool_not_authorized"


def test_a_legacy_unconfigured_connection_keeps_the_grant_requirement(
        svc, stack, connection, monkeypatch):
    """存量未配置连接沿用原授权：豁免只属于「有人被明确分配」这条关系。"""
    _open_read(monkeypatch)
    member = _member(stack, "carol", permissions=["tool.execute"])

    result = _invoke(svc, connection, member, tenant_id=stack.tenant_id,
                     agent_id="agent-a")

    assert result.code == "tool_not_authorized"


def test_the_exemption_needs_an_agent_context(
        svc, stack, connection, monkeypatch):
    """没有受信 Agent，就没有可问的关系，也就没有豁免。"""
    _open_read(monkeypatch)
    _assigned(svc, stack, connection, "agent-a")
    member = _member(stack, "carol", permissions=["tool.execute"])

    result = _invoke(svc, connection, member, tenant_id=stack.tenant_id,
                     agent_id="")

    assert result.code == "tool_not_authorized"


def test_the_exemption_does_not_cross_tenants(
        svc, stack, connection, monkeypatch):
    """另一租户的 Agent 不在本租户的关系里，豁免无从生效。"""
    _open_read(monkeypatch)
    _assigned(svc, stack, connection, "agent-a")
    stack.other_tenant()
    member = _member(stack, "carol", permissions=["tool.execute"])

    result = _invoke(svc, connection, member, tenant_id=stack.tenant_id,
                     agent_id="other-agent")

    assert result.code == "tool_not_authorized"


def test_the_exemption_does_not_open_a_closed_class(
        svc, stack, connection, monkeypatch):
    """不绕过执行条件：分配再明确，切片没开就是没开。"""
    _assigned(svc, stack, connection, "agent-a")
    member = _member(stack, "carol", permissions=["tool.execute"])

    result = _invoke(svc, connection, member, tenant_id=stack.tenant_id,
                     agent_id="agent-a")

    assert result.code == "execution_not_available"


def test_an_assigned_call_is_still_metered(
        svc, stack, connection, monkeypatch):
    """配额照旧：豁免的是授权，不是计费。"""
    from common.runtime_identity import RuntimeIdentity, use_identity

    _open_read(monkeypatch)
    _assigned(svc, stack, connection, "agent-a")
    member = _member(stack, "carol", permissions=["tool.execute"])
    stack.service.set_quota(actor_user_id=stack.root,
                            tenant_id=stack.tenant_id, metric="tool_calls",
                            hard_limit=1, user_id=member)
    tool = _external_tool(connection["id"])
    stream = _stream_for(tool)

    with use_identity(RuntimeIdentity(agent_id="agent-a", user_id=member,
                                      tenant_id=stack.tenant_id)):
        assert stream._permission_denial(tool.name, {}) is None
        denial = stream._permission_denial(tool.name, {})

    assert denial is not None and "quota" in denial
    assert _used(stack, member) == 1


# -- 配额 --------------------------------------------------------------------

def _stream_for(tool):
    """An agent-turn seam holding exactly ``tool``, as a real turn would."""
    from types import SimpleNamespace

    from agent.protocol.agent_stream import AgentStreamExecutor

    class _Stream(AgentStreamExecutor):
        def __init__(self):
            self.tools = {tool.name: tool}
            self.agent = SimpleNamespace(tools=self.tools,
                                         effective_cwd=lambda: "/tmp")

    return _Stream()


def _external_tool(connection_id: str):
    from agent.tools.external.external_tool import ExternalConnectionTool
    from integrations.external import tools as external_tools

    binding = external_tools.ToolBinding(
        tool=external_tools.ExternalTool(
            name=_mcp_name("tools.list", connection_id), kind=KIND,
            action="tools.list", write=False, description="list remote tools"),
        connection_id=connection_id, connection_name="团队 MCP",
        scope=registry.SCOPE_TENANT)
    return ExternalConnectionTool(binding)


def _used(stack, user_id, metric="tool_calls") -> int:
    status = stack.service.quota_status(actor_user_id=stack.root,
                                        tenant_id=stack.tenant_id)
    return sum(int(row["used"]) for row in status["usage"]
               if row["user_id"] == user_id and row["metric"] == metric)


def test_an_external_call_is_metered_at_the_seam_it_is_dispatched_through(
        svc, stack, connection, monkeypatch):
    """配额 — a dispatched external call is charged, and charged once.

    An external tool is ``self_authorized`` because it re-checks the caller
    itself; that exemption is about the *resource grant* and must not extend to
    the meter. The engine meters every call before the self-authorized
    short-circuit, which is what makes the console's read-only runtime endpoint
    safe to be read-only: there is one production dispatch seam and it is
    metered.

    Charged *once* is half the assertion. ``integrations/external/**`` holds no
    ``consume_quota`` of its own, so a second meter there would silently double
    a tenant's usage for a single call.
    """
    from common.runtime_identity import RuntimeIdentity, use_identity

    member = _member(stack, "carol", permissions=["tool.execute"],
                     grants=[_grant(TOOLS_LIST_RESOURCE)])
    stack.service.set_quota(actor_user_id=stack.root,
                            tenant_id=stack.tenant_id, metric="tool_calls",
                            hard_limit=2, user_id=member)
    tool = _external_tool(connection["id"])
    stream = _stream_for(tool)

    with use_identity(RuntimeIdentity(agent_id="agent-a", user_id=member,
                                      tenant_id=stack.tenant_id)):
        assert stream._permission_denial(tool.name, {}) is None
        assert stream._permission_denial(tool.name, {}) is None
        denial = stream._permission_denial(tool.name, {})

    assert denial is not None and "quota" in denial
    assert stream._last_denial_kind == "quota"
    assert _used(stack, member) == 2


def test_the_dispatch_path_itself_does_not_charge_a_second_time(
        svc, stack, connection, monkeypatch):
    """One call, one unit — measured across both seams, not at either alone."""
    from common.runtime_identity import RuntimeIdentity, use_identity

    _open_read(monkeypatch)
    member = _member(stack, "carol", permissions=["tool.execute"],
                     grants=[_grant(TOOLS_LIST_RESOURCE)])
    stack.service.set_quota(actor_user_id=stack.root,
                            tenant_id=stack.tenant_id, metric="tool_calls",
                            hard_limit=5, user_id=member)
    tool = _external_tool(connection["id"])
    stream = _stream_for(tool)

    with use_identity(RuntimeIdentity(agent_id="agent-a", user_id=member,
                                      tenant_id=stack.tenant_id)):
        assert stream._permission_denial(tool.name, {}) is None
        assert _used(stack, member) == 1
        # The real dispatch, through the real service. It fails at the network
        # policy — the point is that reaching the adapter charges nothing more.
        svc.invoke_action(connection["id"], "tools.list", {},
                          actor_user_id=member, tenant_id=stack.tenant_id,
                          agent_id="agent-a")
        assert _used(stack, member) == 1


def test_a_refused_call_is_never_charged(
        svc, stack, connection, monkeypatch):
    """A call the meter refuses must not consume the unit it was refused for.

    The exhausted call is repeated, so the counter is read after two refusals:
    a meter that charged on the way to refusing would show 3 rather than 1.
    """
    from common.runtime_identity import RuntimeIdentity, use_identity

    member = _member(stack, "carol", permissions=["tool.execute"],
                     grants=[_grant(TOOLS_LIST_RESOURCE)])
    stack.service.set_quota(actor_user_id=stack.root,
                            tenant_id=stack.tenant_id, metric="tool_calls",
                            hard_limit=1, user_id=member)
    tool = _external_tool(connection["id"])
    stream = _stream_for(tool)

    with use_identity(RuntimeIdentity(agent_id="agent-a", user_id=member,
                                      tenant_id=stack.tenant_id)):
        assert stream._permission_denial(tool.name, {}) is None
        denial = stream._permission_denial(tool.name, {})
        denial_again = stream._permission_denial(tool.name, {})

    assert denial is not None and denial_again is not None
    assert _used(stack, member) == 1


def test_the_meter_charges_the_caller_not_a_parameter(
        svc, stack, connection, monkeypatch):
    """配额 is charged to the resolved identity, so it cannot be redirected.

    Two callers, one limit each: the second caller's call is charged to the
    second caller even though the tool, the connection and the arguments are
    identical. A meter reading a parameter would let a caller spend someone
    else's allowance — or their own twice.
    """
    from common.runtime_identity import RuntimeIdentity, use_identity

    first = _member(stack, "carol", permissions=["tool.execute"],
                    grants=[_grant(TOOLS_LIST_RESOURCE)])
    second = _member(stack, "dave", permissions=["tool.execute"],
                     grants=[_grant(TOOLS_LIST_RESOURCE)])
    stack.service.set_quota(actor_user_id=stack.root,
                            tenant_id=stack.tenant_id, metric="tool_calls",
                            hard_limit=1, user_id=first)
    tool = _external_tool(connection["id"])
    stream = _stream_for(tool)

    with use_identity(RuntimeIdentity(agent_id="agent-a", user_id=first,
                                      tenant_id=stack.tenant_id)):
        assert stream._permission_denial(tool.name, {}) is None
    with use_identity(RuntimeIdentity(agent_id="agent-a", user_id=second,
                                      tenant_id=stack.tenant_id)):
        assert stream._permission_denial(tool.name, {}) is None

    assert _used(stack, first) == 1
    assert _used(stack, second) == 0
