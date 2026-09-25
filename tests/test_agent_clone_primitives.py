# encoding:utf-8
"""Tests for the reusable clone primitive on ``AgentAdminService``.

Change ``copy-default-tenant-agents`` needs to copy an Agent that already
exists in the roster into a *new* Agent id + workspace, mirroring its
configuration, without going through the "new Agent" form. ``clone_agent`` is
that primitive: the tenant-provisioning orchestration drives it, while
``create_agent(clone_from=...)`` keeps its existing behaviour.

These tests are written before the primitive exists.
"""

import json

import pytest

from agent import team
from agent.admin import CLONED_FILES, AgentAdminError, AgentAdminService, StaleRosterError
from agent.registry import AgentRegistry, set_agent_registry
from common import i18n
from common.i18n import ZH


@pytest.fixture
def lang():
    """Restore the process language after each test; these tests flip it."""
    original = i18n.get_language()
    yield i18n.set_language
    i18n.set_language(original)


def _pin(settings):
    """Point state_dir at this test's config instead of the developer's own."""
    set_agent_registry(AgentRegistry.from_config(team.resolve(settings)))


def _saved(root):
    return team.read({"agent_workspace": str(root)})


@pytest.fixture
def admin(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    settings = {
        "agent_workspace": str(tmp_path),
        "default_agent_id": "primary",
        "agents": [
            {"id": "primary", "name": "Primary", "workspace": str(primary), "enabled": True}
        ],
        "channel_instances": [],
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(settings), encoding="utf-8")
    _pin(settings)
    try:
        yield AgentAdminService(str(config_path)), tmp_path, config_path
    finally:
        set_agent_registry(None)


def _profile_of(service, agent_id):
    return next(a for a in service.snapshot()["agents"] if a["id"] == agent_id)


def test_clone_mirrors_the_configuration_but_not_the_identity(admin):
    service, root, _ = admin
    service.create_agent(
        "sales", "Sales", description="Handles inbound leads", avatar="sales.png",
        skills=["web-search", "crm"], knowledge=["pricing"], position="Sales",
        category="Revenue", tags=["lead", "crm"], greeting="Hi!", 
        persona_summary="A friendly sales agent", knowledge_ids=["pricing"],
        sops=["qualify"], tools_allowlist=["web-search"],
        tools_denylist=["shell"],
    )

    clone = service.clone_agent("sales", "sales-globex")

    source = _profile_of(service, "sales")
    assert clone["id"] == "sales-globex"
    assert clone["workspace"] == str((root / "agents" / "sales-globex").resolve())
    # Design decision: keep the source's display name (no tenant suffix yet).
    assert clone["name"] == "Sales"
    for field in (
        "description", "avatar", "model", "bot_type", "skills", "knowledge",
        "enabled", "position", "category", "tags", "greeting", "persona_summary",
        "scene_id", "knowledge_ids", "sops", "tools_allowlist", "tools_denylist",
    ):
        assert clone.get(field) == source.get(field), field


def test_clone_carries_a_disabled_source_as_disabled(admin):
    """Candidates include disabled agents, so the mirroring must not force enable."""
    service, _, _ = admin
    service.create_agent("sales", "Sales")
    service.archive_agent("sales")

    clone = service.clone_agent("sales", "sales-globex")

    assert clone["enabled"] is False


def test_clone_copies_the_persona_and_leaves_runtime_state_behind(admin):
    service, root, _ = admin
    source = root / "primary"
    (source / "AGENT.md").write_text("# Primary persona", encoding="utf-8")
    (source / "RULE.md").write_text("# House rules", encoding="utf-8")
    (source / "MEMORY.md").write_text("what I learned about my user", encoding="utf-8")
    (source / ".env").write_text("OPENAI_API_KEY=sk-secret", encoding="utf-8")
    (source / "memory" / "long-term").mkdir(parents=True)
    (source / "memory" / "long-term" / "index.db").write_text("history", encoding="utf-8")
    (source / "sessions").mkdir()
    (source / "sessions" / "chat.db").write_text("transcript", encoding="utf-8")

    service.clone_agent("primary", "primary-globex")

    nested = root / "agents" / "primary-globex"
    assert (nested / "AGENT.md").read_text(encoding="utf-8") == "# Primary persona"
    assert (nested / "RULE.md").read_text(encoding="utf-8") == "# House rules"
    assert "what I learned about my user" not in (nested / "MEMORY.md").read_text(
        encoding="utf-8"
    )
    for forbidden in (".env", "sessions", "skills"):
        assert not (nested / forbidden).exists(), forbidden
    # ``memory/`` is part of the standard scaffold, but the source's history is
    # not: the clone starts with an empty memory tree.
    assert not (nested / "memory" / "long-term" / "index.db").exists()
    # The clone is a real workspace, so the standard scaffold is present.
    for filename in ("USER.md", "BOOTSTRAP.md"):
        assert (nested / filename).is_file()
    assert (nested / "scheduler").is_dir()
    assert set(CLONED_FILES) == {"AGENT.md", "USER.md", "RULE.md", "BOOTSTRAP.md"}


def test_clone_retargets_the_layout_section_to_its_own_workspace(admin, lang):
    """A copied RULE.md must not document the workspace it was copied from.

    ``{{WORKSPACE_LAYOUT}}`` is substituted when a workspace is scaffolded, so
    the source's RULE.md names the *source* directory. Copied verbatim, the
    clone reads that path as its own and would write its private files there.
    """
    lang(ZH)
    service, root, _ = admin
    service.create_agent("sales", "Sales")
    source_rule = (root / "agents" / "sales" / "RULE.md").read_text(encoding="utf-8")
    assert "agents/sales/" in source_rule

    service.clone_agent("sales", "sales-globex")

    rule = (root / "agents" / "sales-globex" / "RULE.md").read_text(encoding="utf-8")
    assert "agents/sales-globex/" in rule
    # The tree row is the directory the clone actually owns.
    assert "└── sales-globex/" in rule
    assert "agents/sales/" not in rule
    # The source is not retargeted: only the copy is.
    assert (root / "agents" / "sales" / "RULE.md").read_text(
        encoding="utf-8") == source_rule


def test_clone_keeps_text_appended_below_the_layout_section(admin, lang):
    """The section is the deployment's; what a source added under it is not.

    A workspace records its knowledge-base mode (among other things) right
    after the section, so re-rendering has to stop at the section's last line.
    """
    lang(ZH)
    service, root, _ = admin
    service.create_agent("sales", "Sales")
    rule_path = root / "agents" / "sales" / "RULE.md"
    lines = rule_path.read_text(encoding="utf-8").split("\n")
    closer = next(
        i for i, line in enumerate(lines) if line.startswith("- **每位用户私有**"))
    lines.insert(closer + 1, "本智能体当前为「独立」模式。")
    rule_path.write_text("\n".join(lines), encoding="utf-8")

    service.clone_agent("sales", "sales-globex")

    rule = (root / "agents" / "sales-globex" / "RULE.md").read_text(encoding="utf-8")
    assert "本智能体当前为「独立」模式。" in rule
    assert "agents/sales-globex/" in rule


def test_clone_turns_a_root_layout_into_the_clones_agent_layout(admin, lang):
    """Cloning the default Agent moves the copy out of the shared root.

    That is a different tree, not just a different name: the source's section
    says the workspace *is* the root, which is false for ``agents/<id>``.
    """
    lang(ZH)
    from agent.prompt.workspace import _get_rule_template

    service, root, _ = admin
    # The default Agent's workspace is not under ``agents/``, so the template
    # rendered for it describes the single-Agent layout.
    (root / "primary" / "RULE.md").write_text(
        _get_rule_template(str(root / "primary")), encoding="utf-8")

    service.clone_agent("primary", "primary-globex")

    rule = (root / "agents" / "primary-globex" / "RULE.md").read_text(encoding="utf-8")
    assert "你的工作区就是共享根" not in rule
    assert "agents/primary-globex/" in rule


def test_cloning_the_default_agent_into_its_own_subtree_terminates(admin):
    """The default Agent's workspace is the instance root, so the clone lands
    inside it — a whole-tree copy here would recurse until the OS refuses."""
    service, root, _ = admin
    (root / "AGENT.md").write_text("# Root persona", encoding="utf-8")

    clone = service.clone_agent("primary", "primary-globex")

    assert clone["workspace"] == str((root / "agents" / "primary-globex").resolve())
    assert not (root / "agents" / "primary-globex" / "agents").exists()


def test_clone_of_a_shared_knowledge_agent_keeps_sharing(admin):
    service, root, _ = admin
    service.create_agent("sales", "Sales")

    service.clone_agent("sales", "sales-globex")

    nested = root / "agents" / "sales-globex"
    assert not (nested / "knowledge").exists()
    assert not (nested / "skills").exists()


def test_clone_of_an_own_knowledge_agent_gets_its_own_empty_base(admin):
    """The source's knowledge *entity* files are not copied: only the mode is
    replicated, under the clone's own identity."""
    service, root, _ = admin
    service.create_agent("sales", "Sales", knowledge_mode="own")
    (root / "agents" / "sales" / "knowledge" / "private.md").write_text(
        "source-only knowledge", encoding="utf-8"
    )

    service.clone_agent("sales", "sales-globex")

    nested = root / "agents" / "sales-globex"
    assert (nested / "knowledge").is_dir()
    assert not (nested / "knowledge" / "private.md").exists()


def test_clone_is_registered_and_leaves_the_source_untouched(admin):
    service, root, config_path = admin
    service.create_agent("sales", "Sales")

    service.clone_agent("sales", "sales-globex")

    assert [a["id"] for a in _saved(root)["agents"]] == ["primary", "sales", "sales-globex"]
    assert _profile_of(service, "sales")["id"] == "sales"
    assert (root / "agents" / "sales" / "AGENT.md").is_file()
    # config.json only ever loses the roster; it is never rewritten with it.
    assert "agents" not in json.loads(config_path.read_text(encoding="utf-8"))


def test_clone_rejects_an_id_that_already_exists(admin):
    service, root, _ = admin
    service.create_agent("sales", "Sales")

    with pytest.raises(AgentAdminError):
        service.clone_agent("sales", "primary")


def test_clone_rejects_an_unknown_source(admin):
    service, _, _ = admin

    with pytest.raises(AgentAdminError):
        service.clone_agent("ghost", "ghost-copy")


def test_clone_refuses_a_workspace_that_is_not_empty(admin):
    service, root, _ = admin
    service.create_agent("sales", "Sales")
    occupied = root / "occupied"
    occupied.mkdir()
    (occupied / "AGENT.md").write_text("someone else", encoding="utf-8")

    with pytest.raises(AgentAdminError):
        service.clone_agent("sales", "sales-globex", workspace=str(occupied))

    assert (occupied / "AGENT.md").read_text(encoding="utf-8") == "someone else"


def test_failed_roster_commit_cleans_up_the_new_workspace(admin):
    """A failure after the workspace exists must not leave an orphan directory."""
    service, root, _ = admin
    service.create_agent("sales", "Sales")

    with pytest.raises(StaleRosterError):
        service.clone_agent("sales", "sales-globex", revision="stale-revision")

    assert not (root / "agents" / "sales-globex").exists()
    assert [a["id"] for a in _saved(root)["agents"]] == ["primary", "sales"]


def test_create_agent_clone_from_still_behaves_the_same(admin):
    """The refactor must not change the console's own clone path."""
    service, root, _ = admin
    source = root / "primary"
    (source / "AGENT.md").write_text("# Primary persona", encoding="utf-8")
    (source / "MEMORY.md").write_text("private memory", encoding="utf-8")
    (source / ".env").write_text("OPENAI_API_KEY=sk-secret", encoding="utf-8")

    created = service.create_agent("clone", "Clone", clone_from="primary")

    dest = root / "agents" / "clone"
    assert created["workspace"] == str(dest.resolve())
    assert (dest / "AGENT.md").read_text(encoding="utf-8") == "# Primary persona"
    assert not (dest / ".env").exists()
    assert "private memory" not in (dest / "MEMORY.md").read_text(encoding="utf-8")
    # create_agent still refuses a source that is not enabled in the roster.
    service.archive_agent("clone")
    with pytest.raises(Exception):
        service.create_agent("clone2", "Clone 2", clone_from="clone")
