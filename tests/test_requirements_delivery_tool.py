"""The advisor can deliver an agent without borrowing credentials or ownership."""
import json
from pathlib import Path

import pytest

from agent.admin import AgentAdminService
from agent.registry import AgentRegistry
from agent.tools.requirements_delivery import RequirementsDeliveryTool
from auth.service import IdentityService
from common.runtime_identity import RuntimeIdentity, use_identity


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "tenant"
    source = root / "agents" / "advisor"
    source.mkdir(parents=True)
    settings = {"identity_mode": "database", "agent_workspace": str(tmp_path / "instance"),
                "identity_db_path": str(tmp_path / "identity.db"), "knowledge": True,
                "default_agent_id": "advisor", "channel_instances": [],
                "agents": [{"id": "advisor", "name": "Advisor", "workspace": str(source),
                            "tools_allowlist": ["requirements_delivery", "read", "write", "ls"]}]}
    monkeypatch.setattr("config.conf", lambda: settings)
    config_root = tmp_path / "server"
    config_root.mkdir()
    monkeypatch.setattr("config.get_data_root", lambda: str(config_root))
    config = config_root / "config.json"
    config.write_text(json.dumps(settings))
    admin = AgentAdminService(str(config))
    svc = IdentityService(str(tmp_path / "identity.db"))
    tenant = svc.bootstrap(tenant_code="advisor", tenant_name="Advisor", admin_username="owner",
                           admin_display="Owner", admin_password="TestingPassword123!",
                           shared_root=str(root), allow_weak=True)
    login = svc.login("owner", "TestingPassword123!")
    svc.bind_agent(tenant_id=tenant["id"], agent_id="advisor", private_owner_user_id=login.user_id)
    identity = RuntimeIdentity(agent_id="advisor", user_id=login.user_id, tenant_id=tenant["id"],
                               session_id="test-session",
                               web_auth_session_id=svc.verify_session(login.token)["session"]["id"])
    monkeypatch.setattr("auth.service.get_identity_service", lambda: svc)
    monkeypatch.setattr("agent.admin.get_agent_admin_service", lambda: admin)
    monkeypatch.setattr("agent.registry.get_agent_registry", lambda: AgentRegistry.from_config(admin._load()))
    monkeypatch.setattr("channel.web.fork.runtime._reload_agent_runtime", lambda *a, **k: None)
    return svc, admin, identity, login, source


def spec(**changes):
    return {"action": "create_agent", "project_key": "quote-v1", "name": "报价分析",
            "description": "分析用户上传的报价", "instructions": "先核对币种税率，再比价；缺失信息时提问。",
            "tools": ["read", "write"], **changes}


def test_create_private_agent_and_retry_without_duplicate(env):
    svc, admin, identity, _, _ = env
    tool = RequirementsDeliveryTool()
    with use_identity(identity):
        result = tool.execute(spec())
        assert result.status == "success", result.result
        aid = result.result["agent_id"]
        retry = tool.execute(spec())
        assert retry.result["agent_id"] == aid
        assert retry.result["status"] == "existing"
        catalog = tool.execute({"action": "catalog", "query": "报价分析"})
        assert [a["id"] for a in catalog.result["agents"]] == [aid]
        changed = tool.execute(spec(instructions="Changed instructions"))
        assert changed.status == "error"
    binding = svc.get_agent_binding(aid)
    assert binding["private_owner_user_id"] == identity.user_id
    assert binding["tenant_id"] == identity.tenant_id
    profile = next(a for a in admin.snapshot()["agents"] if a["id"] == aid)
    workspace = Path(profile["workspace"])
    assert (workspace / "AGENT.md").read_text() == spec()["instructions"]
    assert (workspace / "knowledge").is_dir()
    assert (workspace / "skills").is_dir()
    assert profile["tools_allowlist"] == ["read", "write"]
    assert result.result["verified"] is False
    assert len(admin.snapshot()["agents"]) == 2


@pytest.mark.parametrize("changes", [
    {"owner_user_id": "another-user"}, {"tenant_id": "another-tenant"},
    {"workspace": "/tmp/outside"}, {"tools": ["requirements_delivery"]},
    {"tools": []}, {"tools": ["bash"]}, {"instructions": ""},
])
def test_reject_untrusted_ownership_or_unavailable_dependencies(env, changes):
    _, admin, identity, _, _ = env
    with use_identity(identity):
        assert RequirementsDeliveryTool().execute(spec(**changes)).status == "error"
    assert len(admin.snapshot()["agents"]) == 1


def test_no_identity_revoked_session_and_wrong_tenant_are_refused(env):
    svc, _, identity, login, _ = env
    tool = RequirementsDeliveryTool()
    assert tool.execute({"action": "catalog"}).status == "error"
    with use_identity(identity.derive(tenant_id="missing")):
        assert tool.execute({"action": "catalog"}).status == "error"
    svc.revoke_session(login.token)
    with use_identity(identity):
        assert tool.execute(spec()).status == "error"


def test_existing_agents_do_not_implicitly_gain_delivery(env):
    _, admin, identity, _, _ = env
    admin.update_agent("advisor", tools_allowlist=["read"])
    with use_identity(identity):
        assert not RequirementsDeliveryTool().is_available()
        assert RequirementsDeliveryTool().execute(spec()).status == "error"


@pytest.mark.parametrize("allowlist", [None, []])
def test_explicit_selection_makes_delivery_visible_to_model_next_turn(env, allowlist):
    from agent.protocol.agent_stream import AgentStreamExecutor

    _, admin, identity, _, _ = env
    tool = RequirementsDeliveryTool()
    executor = AgentStreamExecutor(agent=None, model=None, system_prompt="", tools=[tool])
    admin.update_agent("advisor", tools_allowlist=allowlist)
    with use_identity(identity):
        assert executor._select_tools_for_injection() == []
        assert tool.execute({"action": "catalog"}).status == "error"
        admin.update_agent("advisor", tools_allowlist=["read", "write", "requirements_delivery"])
        assert executor._select_tools_for_injection() == [tool]
        assert tool.execute({"action": "catalog"}).status == "success"
        admin.update_agent("advisor", tools_denylist=["requirements_delivery"])
        assert executor._select_tools_for_injection() == []
        assert tool.execute({"action": "catalog"}).status == "error"


def member_identity(env, roles):
    svc, _, identity, _, _ = env
    svc.create_member(actor_user_id=identity.user_id, tenant_id=identity.tenant_id,
                      operation="create-new", username="colleague", display_name="Colleague",
                      temporary_password="TemporaryPassword123!", roles=roles)
    initial = svc.login("colleague", "TemporaryPassword123!")
    svc.change_password(initial.token, "TemporaryPassword123!", "FinalPassword123!")
    login = svc.login("colleague", "FinalPassword123!")
    return identity.derive(user_id=login.user_id,
                           web_auth_session_id=svc.verify_session(login.token)["session"]["id"])


def test_tenant_admin_can_use_shared_advisor_without_per_agent_grant(env, monkeypatch):
    svc, _, owner, _, _ = env
    svc.make_agent_tenant_shared(agent_id="advisor", actor_user_id=owner.user_id)
    identity = member_identity(env, ["tenant_admin"])
    # The web entry point admits tenant admins even without per-object grants.
    original = svc.check_resource_action
    monkeypatch.setattr(svc, "check_resource_action",
                        lambda u, t, kind, *a, **k: False if kind == "agent" else original(u, t, kind, *a, **k))
    with use_identity(identity):
        tool = RequirementsDeliveryTool()
        assert tool.is_available()
        result = tool.execute({"action": "catalog"})
        assert result.status == "success", result.result
        assert "advisor" in [a["id"] for a in result.result["agents"]]


def test_member_with_namespaced_agent_grant_can_use_and_find_advisor(env):
    svc, _, owner, _, _ = env
    svc.make_agent_tenant_shared(agent_id="advisor", actor_user_id=owner.user_id)
    svc.create_role(owner.user_id, owner.tenant_id, "advisor_user", "Advisor User",
                    permissions=["chat.use", "agent.use"], resource_grants=[{
                        "resource_kind": "agent", "resource_id": "agent:advisor", "action": "use"}])
    identity = member_identity(env, ["advisor_user"])
    assert svc.check_resource_action(identity.user_id, identity.tenant_id,
                                     "agent", "agent:advisor", "use", permission="agent.use")
    with use_identity(identity):
        tool = RequirementsDeliveryTool()
        assert tool.is_available()
        result = tool.execute({"action": "catalog"})
        assert result.status == "success", result.result
        assert "advisor" in [a["id"] for a in result.result["agents"]]


def test_tenant_admin_cannot_use_another_users_private_advisor(env):
    identity = member_identity(env, ["tenant_admin"])
    with use_identity(identity):
        assert not RequirementsDeliveryTool().is_available()
        assert RequirementsDeliveryTool().execute({"action": "catalog"}).status == "error"


def test_tenant_admin_tool_catalog_matches_execution_exemption(env, monkeypatch):
    svc, _, owner, _, _ = env
    svc.make_agent_tenant_shared(agent_id="advisor", actor_user_id=owner.user_id)
    svc.set_tenant_resource_grants(actor_user_id=owner.user_id, tenant_id=owner.tenant_id,
                                   grants=[{"resource_kind": "tool", "resource_id": "builtin:read", "action": "execute"}],
                                   expected_version=svc.get_tenant(owner.tenant_id)["version"])
    identity = member_identity(env, ["tenant_admin"])
    # Role-level tool grants can be absent while the tenant still has read.
    original = svc.check_resource_action
    monkeypatch.setattr(svc, "check_resource_action",
                        lambda u, t, kind, *a, **k: False if kind == "tool" else original(u, t, kind, *a, **k))
    with use_identity(identity):
        result = RequirementsDeliveryTool().execute({"action": "catalog"})
        assert result.status == "success", result.result
        assert "read" in result.result["tools"]


def test_failed_prompt_write_compensates_unbound_agent(env, monkeypatch):
    svc, admin, identity, _, source = env
    def fail(*a, **k):
        raise OSError("simulated full disk")
    monkeypatch.setattr(admin, "write_core_file", fail)
    with use_identity(identity):
        result = RequirementsDeliveryTool().execute(spec())
    assert result.status == "error"
    assert len(admin.snapshot()["agents"]) == 1
    assert len(svc.agents_for_tenant(identity.tenant_id)) == 1
    assert list(source.parent.iterdir()) == [source]


def test_audio_path_cannot_escape_and_transcription_is_not_fabricated(env, tmp_path, monkeypatch):
    from bridge.bridge import Bridge
    from bridge.reply import Reply, ReplyType
    _, _, identity, _, source = env
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"audio")
    (source / "escaped.wav").symlink_to(outside)
    inside = source / "meeting.wav"
    inside.write_bytes(b"audio")
    monkeypatch.setattr(Bridge(), "fetch_voice_to_text", lambda path: Reply(ReplyType.TEXT, "会议内容"))
    tool = RequirementsDeliveryTool()
    with use_identity(identity):
        for path in (str(outside), "escaped.wav"):
            assert tool.execute({"action": "transcribe_audio", "audio_path": path}).status == "error"
        result = tool.execute({"action": "transcribe_audio", "audio_path": "meeting.wav"})
        assert result.result["text"] == "会议内容"
        assert result.result["timestamps_available"] is False
        monkeypatch.setattr(Bridge(), "fetch_voice_to_text", lambda path: Reply(ReplyType.ERROR, "no provider"))
        assert tool.execute({"action": "transcribe_audio", "audio_path": "meeting.wav"}).status == "error"
