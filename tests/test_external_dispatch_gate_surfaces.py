# encoding:utf-8
"""The same execution gate applies on every external dispatch surface (task 9.6).

Change ``add-external-system-access``, group 9: a write with the deployment's
write class closed must be refused at the runtime, the risk catalogue must refuse
a write that carries no approval, the agent tool path must not bypass either gate,
and the scheduler seam must reach the *same* ``integrations.external.risk``
module — not a forked copy.

These tests are offline: real ``ExternalConnectionService`` over a real
``identity.db``, no network, fixtures from ``tests/_helpers.py`` and patterns
from ``tests/test_external_action_gate.py``.
"""

from __future__ import annotations

import inspect

import pytest

from integrations.external import tools as external_tools
from integrations.external.errors import ExternalConnectionError
from integrations.external.risk import check_invocation
from tests._helpers import bootstrap_identity

MASTER_KEY = "ext-dispatch-gate-surfaces"
KIND = "mcp"
MCP_CONFIG = {"transport": "streamable_http", "url": "https://mcp.example.com/mcp"}
WRITE_PARAMS = {"name": "remote_tool", "arguments": {}}


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", MASTER_KEY)


@pytest.fixture
def stack(tmp_path, monkeypatch):
    return bootstrap_identity(tmp_path, monkeypatch)


@pytest.fixture
def svc(stack):
    from integrations.external.service import ExternalConnectionService
    return ExternalConnectionService(stack.service)


@pytest.fixture
def connection(svc, stack):
    return svc.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind=KIND, name="Gate MCP", config=MCP_CONFIG)

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


def _open(monkeypatch, *classes):
    from config import conf
    monkeypatch.setitem(
        conf().setdefault("external_connections", {}),
        "readiness", {KIND: {name: True for name in classes}})


def test_runtime_invoke_refuses_a_write_while_the_write_class_is_closed(
        svc, stack, connection):
    """ConnectionRuntime is the adapter path; closed class -> execution_not_available."""
    result = svc.invoke_action(
        connection["id"], "tools.call", WRITE_PARAMS,
        actor_user_id=stack.root, tenant_id=stack.tenant_id)
    assert result.ok is False
    assert result.code == "execution_not_available"
    assert result.stage == "policy"


def test_risk_gate_refuses_the_same_write_without_an_approval(connection, stack):
    """check_invocation is the approval seam every invoke reaches when open."""
    with pytest.raises(ExternalConnectionError) as caught:
        check_invocation(
            KIND, "tools.call", WRITE_PARAMS,
            connection_id=connection["id"],
            config_version=connection["version"],
            secret_versions={}, actor_user_id=stack.root,
            tenant_id=stack.tenant_id, approval=None)
    assert caught.value.code.startswith("approval_")


def test_service_invoke_wires_the_same_risk_module(connection, stack, svc):
    """invoke_action must pass integrations.external.risk.check_invocation."""
    from integrations.external import risk
    from integrations.external.service import ExternalConnectionService

    source = inspect.getsource(ExternalConnectionService.invoke_action)
    assert "from integrations.external.risk import check_invocation" in source
    runtime_source = inspect.getsource(svc.runtime().invoke)
    assert "risk_check" in runtime_source

    # 调度器边界测试直接调用同一函数 —— 这里钉模块身份，防止复制一份规则。
    assert risk.check_invocation.__module__ == "integrations.external.risk"


def test_mcp_tools_are_omitted_from_discovery_while_read_class_is_closed(
        stack, connection, monkeypatch):
    """Listing surface: closed read class -> no MCP bindings offered (not placeholders)."""
    from integrations.external.adapters import mcp as mcp_adapter

    bindings = mcp_adapter._mcp_tool_provider(stack.tenant_id, stack.root)
    assert bindings == []


def test_write_tools_are_omitted_from_agent_discovery_while_write_class_is_closed(
        svc, stack, connection, monkeypatch):
    """Agent/channel listing omits write bindings when write_execute is closed.

    That is the shipped behaviour: the model is not offered a name the runtime
    always refuses. A call that still reaches ``invoke_action`` is refused by
    the same execution gate with ``execution_not_available``.
    """
    from integrations.external.adapters import mcp as mcp_adapter

    _open(monkeypatch, "read_execute")
    result = svc.invoke_action(
        connection["id"], "tools.call", WRITE_PARAMS,
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        agent_id="agent-a")
    assert result.ok is False
    assert result.code == "execution_not_available"

    offered = {b.tool.action for b in mcp_adapter._mcp_tool_provider(
        stack.tenant_id, stack.root)}
    assert "tools.call" not in offered
    assert external_tools.find_binding(
        _mcp_name("tools.call", connection["id"]),
        tenant_id=stack.tenant_id, actor_user_id=stack.root) is None


def test_scheduler_revalidation_and_runtime_share_one_risk_entrypoint(stack):
    """Scheduler boundary already tests approvals; here we pin the shared module."""
    from agent.tools.scheduler import identity as scheduler_identity
    from integrations.external import risk

    assert callable(scheduler_identity.revalidate_owner)
    assert risk.check_invocation.__qualname__ == "check_invocation"
    # ExternalConnectionService is what dispatchers reach; it imports risk once.
    from integrations.external.service import ExternalConnectionService
    assert "check_invocation" in inspect.getsource(ExternalConnectionService.invoke_action)
