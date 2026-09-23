import json
from pathlib import Path

import pytest

from agent.admin import AgentAdminService
from agent import team
from agent.registry import AgentRegistry, set_agent_registry
from agent.skills.loader import SkillLoader
from auth.service import IdentityService
from scripts.install_rfq_agent import install


@pytest.fixture
def environment(tmp_path):
    root = tmp_path/"instance"
    root.mkdir()
    (root/"USER.md").write_text("UNRELATED PRIVATE PROFILE")
    config = tmp_path/"config.json"
    settings = {"agent_workspace": str(root), "identity_mode": "database", "channel_instances": []}
    config.write_text(json.dumps(settings))
    service = IdentityService(str(tmp_path/"identity.db"))
    default = service.bootstrap(tenant_code="default", tenant_name="Default", admin_username="admin", admin_display="Admin", admin_password="Test1234!", shared_root=str(root), allow_weak=True)
    actor = service.list_platform_users()[0]["id"]
    target = service.create_tenant(actor_user_id=actor, code="test15", name="AI租户1", shared_root=str(tmp_path/"tenant"), admin_username="test15", admin_display="Test", admin_password="Test1234!", recent_password="Test1234!")
    user = service._find_user_by_username("test15")["id"]
    set_agent_registry(AgentRegistry.from_config(settings))
    yield AgentAdminService(str(config)), service, target["id"], user, default["id"]
    set_agent_registry(None)


def test_install_is_tenant_scoped_discoverable_and_idempotent(environment):
    admin, svc, tenant, actor, other = environment
    before_default = svc.tenant_default_agent_id(tenant)
    result = install(admin, svc, tenant_id=tenant, actor_user_id=actor, agent_id="rfq-quote-test15")
    assert result["status"] == "installed"
    assert svc.get_agent_binding(result["id"])["tenant_id"] == tenant
    assert result["id"] not in svc.tenant_agent_ids(other)
    assert svc.tenant_default_agent_id(tenant) == before_default
    assert svc._member_can_use_agent(actor, tenant, result["id"])
    assert "builtin:rfq-quote" in svc.resource_ids_for(actor, tenant, "skill", "use", permission="skill.use")
    workspace = Path(result["workspace"])
    assert workspace.is_relative_to(Path(svc.tenant_shared_root(tenant)))
    assert "UNRELATED PRIVATE PROFILE" not in (workspace/"USER.md").read_text()
    assert not (workspace/"BOOTSTRAP.md").exists()
    loaded = SkillLoader().load_skills_from_dir(str(workspace/"skills"), "custom")
    assert [s.name for s in loaded.skills] == ["rfq-quote"]
    (workspace/"AGENT.md").write_text("User customisation")
    assert install(admin,svc,tenant_id=tenant,actor_user_id=actor,agent_id=result["id"])["status"] == "already_installed"
    assert (workspace/"AGENT.md").read_text() == "User customisation"


def test_binding_failure_rolls_back_only_new_resources(environment, monkeypatch):
    admin, svc, tenant, actor, _ = environment
    before = admin.snapshot()
    def fail(**kwargs): raise RuntimeError("binding failed")
    monkeypatch.setattr(svc,"bind_agent",fail)
    with pytest.raises(RuntimeError): install(admin,svc,tenant_id=tenant,actor_user_id=actor,agent_id="rfq-quote-test15")
    assert [a["id"] for a in admin.snapshot()["agents"]] == [a["id"] for a in before["agents"]]
    assert not (Path(svc.tenant_shared_root(tenant))/"agents/rfq-quote-test15").exists()


def test_actor_from_another_tenant_cannot_install(environment):
    admin, svc, tenant, actor, other = environment
    with pytest.raises(ValueError, match="管理员"):
        install(admin,svc,tenant_id=other,actor_user_id=actor,agent_id="rfq-wrong")
