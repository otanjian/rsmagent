"""Changing a conversation's team must rebuild that conversation's runtime.

``agent_delegate`` is decided when the Agent runtime is assembled
(``AgentInitializer._load_tools`` gates it on ``_is_shared_conversation``),
while the roster is read per turn via ``_get_teammates``. So inviting someone
after the first message left the Agent seeing the team in its prompt while
holding no tool to hand work to it.

Observed in the console: session created 20:33:12 (solo, so no tool), members
added 20:34:12 and 20:34:14, Agent reports no ``agent_delegate`` at 20:35:32.

Writing ``members`` therefore has to drop the cached runtime, so the next turn
rebuilds it against the new roster.
"""

import json
import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.registry import AgentRegistry
from bridge.agent_bridge import AgentBridge


class _FakeInitializer:
    """Stands in for the expensive build; counts how often it was asked to."""

    def __init__(self, registry):
        self.registry = registry
        self.calls = []

    def initialize_agent(self, session_id=None, agent_id=None, host_agent_id=None):
        profile = self.registry.get(agent_id)
        self.calls.append((profile.id, session_id, host_agent_id))
        return SimpleNamespace(
            agent_id=profile.id,
            workspace_dir=profile.workspace,
            messages=[],
            messages_lock=threading.RLock(),
        )


@pytest.fixture()
def bridge(tmp_path):
    """A real AgentBridge cache with a fake build, so rebuilds are observable."""
    registry = AgentRegistry.from_config(
        {
            "default_agent_id": "primary",
            "agents": [
                {"id": "primary", "name": "Primary", "workspace": str(tmp_path / "primary")},
                {"id": "ops", "name": "运营助手", "workspace": str(tmp_path / "ops")},
            ],
        }
    )
    instance = object.__new__(AgentBridge)
    instance.agent_registry = registry
    instance._agent_instances = {}
    instance._default_agents = {}
    instance._agents_lock = threading.RLock()
    instance.agents = {}
    instance.default_agent = None
    instance.initializer = _FakeInitializer(registry)
    return instance


@pytest.fixture()
def post_settings(tmp_path, monkeypatch, bridge):
    """Drive ``SessionSettingsHandler.POST`` against *bridge* and a temp store."""
    import common.state_dir as state_dir
    import bridge.bridge as bridge_module
    from channel.web import web_channel

    shared = Path(tempfile.mkdtemp())
    monkeypatch.setattr(state_dir, "shared_root", lambda: shared)
    monkeypatch.setattr(
        bridge_module, "Bridge", lambda: SimpleNamespace(get_agent_bridge=lambda: bridge)
    )
    monkeypatch.setattr(web_channel, "conf", lambda: {"identity_mode": "legacy"})
    monkeypatch.setattr(web_channel.web, "header", lambda *a, **k: None)
    # The model/permission projection is unrelated here and needs a DB to run.
    monkeypatch.setattr(
        web_channel,
        "_session_settings_state",
        lambda session_id, agent_id=None: {"model": {}, "permission": {}, "team": {}},
    )

    @contextmanager
    def _fake_db_scope():
        # Admin so the model-grant check passes without a real identity service.
        yield SimpleNamespace(
            user_id="u1", tenant_id="t1", is_platform_admin=True, is_tenant_admin=True
        )

    monkeypatch.setattr(web_channel, "_db_scope", _fake_db_scope)

    # The roster boundary (type / tenant / enabled / use grant) is exercised by
    # ``test_coding_agent_type_boundary.py`` and the coding route suite against
    # the real WSGI app. This file is about the *runtime rebuild* obligation, so
    # the guards are stubbed to accept the fixture's local registry.
    import agent.registry as registry_module

    monkeypatch.setattr(
        registry_module, "get_agent_registry", lambda: bridge.agent_registry
    )
    monkeypatch.setattr(
        web_channel, "_require_tenant_agent_binding", lambda ctx, agent_id: agent_id
    )
    monkeypatch.setattr(
        web_channel, "_require_agent_action",
        lambda ctx, agent_id, action, permission: None,
    )

    def post(session_id, body):
        monkeypatch.setattr(web_channel.web, "data", lambda: json.dumps(body).encode())
        return json.loads(web_channel.SessionSettingsHandler().POST(session_id))

    return post


def _build_count(bridge, session_id="s1", agent_id="primary"):
    bridge.get_agent(session_id=session_id, agent_id=agent_id)
    return len(bridge.initializer.calls)


def test_inviting_a_member_rebuilds_the_conversation_runtime(bridge, post_settings):
    assert _build_count(bridge) == 1

    resp = post_settings("s1", {"members": ["ops"]})
    assert resp["status"] == "success"

    from agent.workspace import session_prefs

    assert session_prefs.get_prefs("s1", None)["members"] == ["ops"]

    # The next turn must be served by a runtime assembled against the new team,
    # which is the only way `agent_delegate` reaches the toolset.
    assert _build_count(bridge) == 2


def test_removing_the_last_member_rebuilds_the_conversation_runtime(bridge, post_settings):
    assert _build_count(bridge) == 1
    post_settings("s1", {"members": ["ops"]})
    assert _build_count(bridge) == 2

    resp = post_settings("s1", {"members": None})
    assert resp["status"] == "success"
    assert _build_count(bridge) == 3


def test_unchanged_roster_does_not_churn_the_runtime(bridge, post_settings):
    """Saving the same team again is not a roster change, so a warm runtime —
    and the messages it holds — is left alone."""
    assert _build_count(bridge) == 1
    post_settings("s1", {"members": ["ops"]})
    assert _build_count(bridge) == 2

    resp = post_settings("s1", {"members": ["ops"]})
    assert resp["status"] == "success"
    assert _build_count(bridge) == 2


def test_a_settings_write_without_members_leaves_the_runtime_alone(bridge, post_settings):
    assert _build_count(bridge) == 1

    resp = post_settings("s1", {"model": "deepseek-v4-flash", "provider": "deepseek"})
    assert resp["status"] == "success"
    assert _build_count(bridge) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
