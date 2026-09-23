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
