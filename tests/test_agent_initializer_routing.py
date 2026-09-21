"""Covers the real initialize_agent body.

Every other multi-agent test replaces the initializer with a fake, which is how
`name 'profile' is not defined` reached a running gateway: nothing executed the
function. These are slow (they build a full agent) but they exercise the path a
real message takes.
"""

import os

import pytest

from agent.coding import CODING_WEB_ONLY, CodingError
from agent.registry import AgentProfile, AgentRegistry, set_agent_registry
from common.runtime_identity import identity_scope


@pytest.fixture
def two_agents(tmp_path):
    registry = AgentRegistry(
        [
            AgentProfile("sales", "Sales", str(tmp_path / "sales")),
            AgentProfile("support", "Support", str(tmp_path / "support")),
        ],
        default_agent_id="sales",
    )
    set_agent_registry(registry)
    yield registry
    set_agent_registry(None)


@pytest.fixture
def initializer():
    from bridge.agent_bridge import AgentBridge
    from bridge.bridge import Bridge

    return AgentBridge(Bridge()).initializer


def _realpath(path):
    return os.path.realpath(str(path))


def test_initialize_agent_follows_the_ambient_identity(two_agents, initializer, tmp_path):
    with identity_scope(agent_id="support"):
        agent = initializer.initialize_agent(session_id="s1")

    assert agent.agent_id == "support"
    assert agent.agent_profile.name == "Support"
    assert _realpath(agent.workspace_dir) == _realpath(tmp_path / "support")


def test_explicit_agent_id_overrides_the_ambient_identity(two_agents, initializer, tmp_path):
    with identity_scope(agent_id="support"):
        agent = initializer.initialize_agent(session_id="s1", agent_id="sales")

    assert agent.agent_id == "sales"
    assert _realpath(agent.workspace_dir) == _realpath(tmp_path / "sales")


def test_absent_identity_uses_the_default_agent(two_agents, initializer, tmp_path):
    agent = initializer.initialize_agent(session_id="s1")

    assert agent.agent_id == "sales"
    assert _realpath(agent.workspace_dir) == _realpath(tmp_path / "sales")


def test_a_coding_agent_is_refused_before_any_runtime_is_built(two_agents, initializer, tmp_path):
    """The single choke point every non-Web path goes through.

    ``initialize_agent`` is what build-scheduler, task and channel code all call,
    so refusing here is what keeps a coding Agent out of every one of them
    without each of them having to remember the rule. The refusal must happen
    before any workspace, scheduler or process is created.
    """
    registry = AgentRegistry.from_config({
        "agent_workspace": str(tmp_path),
        "default_agent_id": "sales",
        "agents": [
            {"id": "sales", "name": "Sales", "workspace": str(tmp_path / "sales")},
            {
                "id": "erp-coder",
                "name": "ERP Coder",
                "workspace": str(tmp_path / "erp-coder"),
                "agent_type": "coding",
                "coding_project_dir": "/srv/checkouts/erp",
            },
        ],
    })
    set_agent_registry(registry)

    with pytest.raises(CodingError) as caught:
        initializer.initialize_agent(session_id="s1", agent_id="erp-coder")

    assert caught.value.code == CODING_WEB_ONLY
    # 400, not 403: the caller is authorized for this Agent, the platform simply
    # will not run it here, and a 403 would read as a permission problem.
    assert caught.value.status == 400
    # Nothing was built on the way to the refusal.
    assert not (tmp_path / "erp-coder").exists()
