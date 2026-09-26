"""Shared-asset maintenance guidance in the system prompt.

Change ``guard-shared-knowledge-skill-writes`` adds prompt-only guidance that
tells the model which assets are shared and whether the current user may
maintain them. These tests pin the facts it reads (runtime identity, Agent
binding, state_dir data roots, existing admin qualification) and the wording it
emits, including the conservative fallbacks. They deliberately assert prompt
content, not enforcement: this capability adds no tool gate.
"""

from pathlib import Path

import pytest

from agent.prompt import builder as builder_mod
from agent.prompt import shared_assets as sa
from common.runtime_identity import RuntimeIdentity


class _FakeService:
    def __init__(self, *, platform_admin=False, tenant_admin=False, binding=None):
        self._platform_admin = platform_admin
        self._tenant_admin = tenant_admin
        self._binding = binding

    def is_platform_admin(self, user_id):
        return bool(self._platform_admin)

    def is_tenant_admin(self, user_id, tenant_id):
        return bool(self._tenant_admin)

    def get_agent_binding(self, agent_id):
        return self._binding


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A workspace + shared root with every state_dir fact monkeypatched."""
    shared_root = tmp_path / "shared"
    shared_root.mkdir()
    (shared_root / "knowledge").mkdir()
    (shared_root / "skills").mkdir()
    workspace = shared_root / "agents" / "alpha"
    workspace.mkdir(parents=True)
    (workspace / "knowledge").mkdir()
    (workspace / "skills").mkdir()

    def shared_root_fn(identity=None):
        return shared_root

    def knowledge_dir(identity=None, ensure=False, base=None):
        base = Path(base) if base is not None else workspace
        own = base / "knowledge"
        return own if own.exists() else shared_root / "knowledge"

    def skills_dir(identity=None, ensure=False, base=None):
        base = Path(base) if base is not None else workspace
        own = base / "skills"
        return own if own.exists() else shared_root / "skills"

    def outputs_dir(identity=None, ensure=False, base=None):
        user_id = getattr(identity, "user_id", None)
        if not user_id:
            return None
        return workspace / "user" / user_id / "outputs"

    from common import state_dir

    monkeypatch.setattr(state_dir, "shared_root", shared_root_fn)
    monkeypatch.setattr(state_dir, "knowledge_dir", knowledge_dir)
    monkeypatch.setattr(state_dir, "skills_dir", skills_dir)
    monkeypatch.setattr(state_dir, "agent_user_outputs_dir", outputs_dir)
    return {"shared": shared_root, "workspace": workspace}


def _install_service(monkeypatch, service):
    import auth.service

    monkeypatch.setattr(auth.service, "get_identity_service", lambda: service)


# --- fact resolution ------------------------------------------------------


def test_member_on_shared_agent_is_read_only(state, monkeypatch):
    _install_service(monkeypatch, _FakeService(binding={
        "tenant_id": "t1", "private_owner_user_id": None}))
    ident = RuntimeIdentity(agent_id="alpha", user_id="u1", tenant_id="t1")

    scope = sa.resolve_shared_asset_scope(str(state["shared"]), identity=ident)

    assert scope.resolved
    assert not scope.knowledge_maintainable
    assert not scope.skills_maintainable


def test_private_agents_own_root_is_maintainable(state, monkeypatch):
    _install_service(monkeypatch, _FakeService(binding={
        "tenant_id": "t1", "private_owner_user_id": "u1"}))
    ident = RuntimeIdentity(agent_id="alpha", user_id="u1", tenant_id="t1")

    scope = sa.resolve_shared_asset_scope(
        str(state["workspace"]), identity=ident)

    assert scope.resolved
    assert scope.knowledge_maintainable
    assert scope.skills_maintainable
    assert scope.outputs_dir.endswith("u1/outputs")


def test_private_agent_on_public_fallback_is_read_only(state, monkeypatch):
    """A private Agent whose knowledge falls back to the shared copy stays shared."""
    _install_service(monkeypatch, _FakeService(binding={
        "tenant_id": "t1", "private_owner_user_id": "u1"}))
    # Drop the Agent's own knowledge/skills so the shared fallback applies.
    import shutil
    shutil.rmtree(state["workspace"] / "knowledge")
    shutil.rmtree(state["workspace"] / "skills")
    ident = RuntimeIdentity(agent_id="alpha", user_id="u1", tenant_id="t1")

    scope = sa.resolve_shared_asset_scope(
        str(state["workspace"]), identity=ident)

    assert not scope.knowledge_maintainable
    assert not scope.skills_maintainable


def test_tenant_admin_may_maintain_shared(state, monkeypatch):
    _install_service(monkeypatch, _FakeService(
        tenant_admin=True, binding={"tenant_id": "t1", "private_owner_user_id": None}))
    ident = RuntimeIdentity(agent_id="alpha", user_id="u1", tenant_id="t1")

    scope = sa.resolve_shared_asset_scope(str(state["shared"]), identity=ident)

    assert scope.knowledge_maintainable
    assert scope.skills_maintainable


def test_unresolved_facts_stay_conservative(state, monkeypatch):
    import auth.service

    def boom():
        raise RuntimeError("no store")

    monkeypatch.setattr(auth.service, "get_identity_service", boom)
    ident = RuntimeIdentity(agent_id="alpha", user_id="u1", tenant_id="t1")

    scope = sa.resolve_shared_asset_scope(str(state["workspace"]), identity=ident)

    assert not scope.knowledge_maintainable
    assert not scope.skills_maintainable


def test_no_identity_is_conservative(state, monkeypatch):
    _install_service(monkeypatch, _FakeService())
    scope = sa.resolve_shared_asset_scope(str(state["shared"]), identity=RuntimeIdentity())
    assert not scope.knowledge_maintainable


def test_each_turn_reads_its_own_facts(state, monkeypatch):
    """No cross-turn carry-over: an admin's turn must not leak into a member's."""
    service = _FakeService(
        tenant_admin=True, binding={"tenant_id": "t1", "private_owner_user_id": None})
    _install_service(monkeypatch, service)

    admin = RuntimeIdentity(agent_id="alpha", user_id="admin", tenant_id="t1")
    member = RuntimeIdentity(agent_id="alpha", user_id="u1", tenant_id="t1")

    admin_scope = sa.resolve_shared_asset_scope(str(state["shared"]), identity=admin)
    # Qualification changes between turns; the next call must not reuse the first.
    service._tenant_admin = False
    member_scope = sa.resolve_shared_asset_scope(str(state["shared"]), identity=member)

    assert admin_scope.knowledge_maintainable
    assert not member_scope.knowledge_maintainable


# --- wording --------------------------------------------------------------


def test_read_only_guidance_forbids_all_write_paths():
    scope = sa.SharedAssetScope(
        knowledge_maintainable=False, skills_maintainable=False,
        outputs_dir="/tmp/u/outputs", resolved=True)
    text = "\n".join(sa.build_shared_asset_guidance("/x", "zh", scope=scope))

    assert "只读" in text
    assert "新增、修改、删除或重命名" in text
    assert "write" in text and "edit" in text and "bash" in text
    assert "自称管理员" in text
    assert "/tmp/u/outputs" in text
    assert "只在当前维护范围允许时适用" in text


def test_read_only_without_outputs_dir_asks_for_text_only():
    scope = sa.SharedAssetScope(
        knowledge_maintainable=False, skills_maintainable=False,
        outputs_dir=None, resolved=True)
    text = "\n".join(sa.build_shared_asset_guidance("/x", "zh", scope=scope))
    assert "只返回文字建议" in text


def test_maintainable_guidance_keeps_existing_rules():
    scope = sa.SharedAssetScope(
        knowledge_maintainable=True, skills_maintainable=True, resolved=True)
    text = "\n".join(sa.build_shared_asset_guidance("/x", "en", scope=scope))
    assert "maintenance scope" in text
    # The prohibition block belongs to the read-only case only.
    assert "you must not create, modify, delete" not in text


def test_conservative_note_bilingual():
    assert "只读" in sa.conservative_maintenance_note("zh")
    assert "read-only" in sa.conservative_maintenance_note("en")


# --- assembly -------------------------------------------------------------


def _build(monkeypatch, workspace_dir, scope, language="zh"):
    monkeypatch.setattr(builder_mod, "resolve_shared_asset_scope", lambda wd: scope)
    return builder_mod.build_agent_system_prompt(
        workspace_dir=str(workspace_dir), language=language)


def test_constraint_present_even_without_index(tmp_path, monkeypatch):
    """Empty/shared knowledge base must not drop the read-only constraint."""
    empty_ws = tmp_path / "empty"
    empty_ws.mkdir()
    from common import state_dir
    monkeypatch.setattr(state_dir, "knowledge_dir", lambda identity=None, ensure=False, base=None: empty_ws / "knowledge")

    scope = sa.SharedAssetScope(
        knowledge_maintainable=False, skills_maintainable=False, resolved=True)
    prompt = _build(monkeypatch, empty_ws, scope)

    # The prohibition sentence exists only in the guidance block, so it proves
    # the constraint was appended rather than cross-referenced from elsewhere.
    assert "当前共享内容在本轮为只读" in prompt
    assert "知识系统" not in prompt  # no index -> section skipped


def test_read_only_knowledge_section_has_no_mandatory_write(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    knowledge = workspace / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "index.md").write_text("# index\n", encoding="utf-8")
    from common import state_dir
    monkeypatch.setattr(state_dir, "knowledge_dir", lambda identity=None, ensure=False, base=None: knowledge)

    scope = sa.SharedAssetScope(
        knowledge_maintainable=False, skills_maintainable=False, resolved=True)
    prompt = _build(monkeypatch, workspace, scope)

    assert "写入规则（受维护范围约束）" in prompt
    assert "自动写入规则（mandatory）" not in prompt
    assert "当前共享内容在本轮为只读" in prompt


def test_maintainable_knowledge_section_keeps_mandatory_rules(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    knowledge = workspace / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "index.md").write_text("# index\n", encoding="utf-8")
    from common import state_dir
    monkeypatch.setattr(state_dir, "knowledge_dir", lambda identity=None, ensure=False, base=None: knowledge)

    scope = sa.SharedAssetScope(
        knowledge_maintainable=True, skills_maintainable=True, resolved=True)
    prompt = _build(monkeypatch, workspace, scope)

    assert "自动写入规则（mandatory）" in prompt
    assert "写入规则（受维护范围约束）" not in prompt
    # The maintainable branch of the guidance block, unique to it.
    assert "这些内容在你当前的维护范围内" in prompt


def test_default_rule_template_is_conditional(tmp_path):
    from agent.prompt import workspace as ws
    from common import i18n
    from common.i18n import EN, ZH

    original = i18n.get_language()
    try:
        i18n.set_language(ZH)
        rendered = ws._get_rule_template(str(tmp_path / "root"))
        # The old unconditional heading must be gone from the default template.
        assert "### 自动写入（不要询问，直接写入）" not in rendered
        assert "### 写入规则（受维护资格约束）" in rendered

        i18n.set_language(EN)
        rendered_en = ws._get_rule_template(str(tmp_path / "root"))
        assert "bounded by your maintenance qualification" in rendered_en
        assert "### Auto-write (don't ask, just write)" not in rendered_en
    finally:
        i18n.set_language(original)


# --- integration: the prompt actually reaches the model request -----------


class _CapturingModel:
    """A model double that records the request the executor builds."""

    def __init__(self):
        self.requests = []

    @property
    def model(self):
        return "capture-model"

    def call_stream(self, request):
        self.requests.append(request)
        raise _Stop("captured")

    def use_fallback(self):
        return False


class _Stop(Exception):
    pass


def _reduced_executor(model, monkeypatch, system_prompt):
    """The real request-building path, with the transport replaced."""
    from agent.protocol.agent_stream import AgentStreamExecutor

    executor = AgentStreamExecutor.__new__(AgentStreamExecutor)
    executor.model = model
    executor.agent = None
    executor.messages = []
    executor.tools = {}
    executor.system_prompt = system_prompt
    monkeypatch.setattr(executor, "_validate_and_fix_messages", lambda: None)
    monkeypatch.setattr(executor, "_prepare_messages", lambda: [])
    monkeypatch.setattr(executor, "_identify_complete_turns", lambda: [])
    monkeypatch.setattr(executor, "_emit_event", lambda *a, **k: None)
    monkeypatch.setattr(executor, "_is_thinking_enabled", lambda: False)
    monkeypatch.setattr("agent.protocol.agent_stream.time.sleep", lambda s: None)
    return executor


def test_guidance_reaches_the_llm_request(monkeypatch, tmp_path):
    """The assembled prompt must ride on the request, not just exist in code."""
    workspace = tmp_path / "ws"
    knowledge = workspace / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "index.md").write_text("# index\n", encoding="utf-8")
    from common import state_dir
    monkeypatch.setattr(
        state_dir, "knowledge_dir",
        lambda identity=None, ensure=False, base=None: knowledge)

    from agent.protocol.agent import Agent
    from agent.protocol import agent as agent_mod

    agent = Agent.__new__(Agent)
    agent.system_prompt = ""
    agent.workspace_dir = str(workspace)
    agent.skill_manager = None
    agent.skip_context_files = True
    agent.tools = []
    agent.memory_manager = None
    agent.runtime_info = None
    agent.project_dir = None
    agent.extra_system_suffix = None
    monkeypatch.setattr(agent, "effective_permission_mode", lambda: None)

    prompt = agent.get_full_system_prompt()
    assert "当前共享内容在本轮为只读" in prompt

    model = _CapturingModel()
    executor = _reduced_executor(model, monkeypatch, prompt)
    with pytest.raises(_Stop):
        executor._call_llm_stream(retry_on_empty=False, max_retries=0)

    assert model.requests, "the transport should have received a request"
    assert model.requests[0].system == prompt
    assert "当前共享内容在本轮为只读" in model.requests[0].system


def test_cached_prompt_fallback_still_reaches_the_request(monkeypatch, tmp_path):
    """A failed rebuild must send the conservative note, not the stale prompt."""
    from agent.protocol.agent import Agent

    agent = Agent.__new__(Agent)
    agent.system_prompt = "STALE BASE"
    agent.extra_system_suffix = None
    # Force the rebuild to fail.
    agent.workspace_dir = str(tmp_path / "ws")
    agent.skill_manager = None
    agent.skip_context_files = False
    agent.tools = []
    agent.memory_manager = None
    agent.runtime_info = None
    agent.project_dir = None
    monkeypatch.setattr(
        "agent.prompt.load_context_files",
        lambda _wd: (_ for _ in ()).throw(RuntimeError("disk gone")))

    prompt = agent.get_full_system_prompt()
    assert prompt.startswith("STALE BASE")
    assert "本轮无法重新确认维护范围" in prompt

    model = _CapturingModel()
    executor = _reduced_executor(model, monkeypatch, prompt)
    with pytest.raises(_Stop):
        executor._call_llm_stream(retry_on_empty=False, max_retries=0)

    assert "本轮无法重新确认维护范围" in model.requests[0].system


# --- concurrency ----------------------------------------------------------


def test_concurrent_turns_do_not_share_facts(state, monkeypatch):
    """Two users in parallel must get their own scope, never the other's."""
    import threading

    from common.runtime_identity import use_identity

    service = _FakeService(
        tenant_admin=True, binding={"tenant_id": "t1", "private_owner_user_id": None})
    _install_service(monkeypatch, service)

    admin = RuntimeIdentity(agent_id="alpha", user_id="admin", tenant_id="t1")
    member = RuntimeIdentity(agent_id="alpha", user_id="u1", tenant_id="t1")
    barrier = threading.Barrier(2)
    results = {}

    def run(name, ident):
        with use_identity(ident):
            barrier.wait()
            # Both threads resolve at the same instant; each must read its own.
            scope = sa.resolve_shared_asset_scope(str(state["shared"]))
            results[name] = scope.knowledge_maintainable

    # The admin is qualified; the member is not. Make the qualification follow
    # the thread's own user id rather than a fixed answer.
    service._tenant_admin = False
    original = service.is_tenant_admin

    def per_user(user_id, tenant_id):
        return user_id == "admin"

    service.is_tenant_admin = per_user

    threads = [
        threading.Thread(target=run, args=("admin", admin)),
        threading.Thread(target=run, args=("member", member)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    service.is_tenant_admin = original
    assert results == {"admin": True, "member": False}


def test_cached_prompt_fallback_appends_conservative_note():
    """A failed per-turn rebuild must still carry the conservative instruction."""
    from agent.protocol.agent import Agent

    class Dummy:
        system_prompt = "BASE PROMPT"
        _cached_prompt_with_conservative_scope = (
            Agent._cached_prompt_with_conservative_scope
        )

    out = Dummy()._cached_prompt_with_conservative_scope()

    assert out.startswith("BASE PROMPT")
    assert "共享内容维护范围" in out or "Shared content maintenance scope" in out
