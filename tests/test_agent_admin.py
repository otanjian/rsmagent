import json
from pathlib import Path

import pytest

from agent.admin import (
    AgentAdminError,
    AgentAdminService,
    StaleAgentFileError,
    StaleRosterError,
)
from agent import team
from agent.registry import AgentRegistry, set_agent_registry
from common import state_dir


def _pin(settings):
    """Point state_dir at this test's config instead of the developer's own.

    Without this, resolving a shared directory falls through to the real
    ``agent_workspace`` and the test scaffolds skills into it.
    """
    set_agent_registry(AgentRegistry.from_config(team.resolve(settings)))


def _saved(root):
    """The roster as the app reads it, wherever it is being kept."""
    return team.read({"agent_workspace": str(root)})


@pytest.fixture
def admin(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    settings = {
        "agent_workspace": str(tmp_path),
        "default_agent_id": "primary",
        "agents": [
            {
                "id": "primary",
                "name": "Primary",
                "workspace": str(primary),
                "enabled": True,
            }
        ],
        "channel_instances": [],
        "unrelated_setting": "preserved",
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(settings), encoding="utf-8")
    _pin(settings)
    try:
        yield AgentAdminService(str(config_path)), tmp_path, config_path
    finally:
        set_agent_registry(None)


def test_create_agent_bootstraps_persona_without_forking_shared_assets(admin):
    service, root, config_path = admin
    workspace = root / "research"

    created = service.create_agent("research", "Research", str(workspace))

    assert created["workspace"] == str(workspace.resolve())
    for filename in ("AGENT.md", "USER.md", "RULE.md", "MEMORY.md", "BOOTSTRAP.md"):
        assert (workspace / filename).is_file()
    assert (workspace / "scheduler").is_dir()
    # The shared assets must NOT be materialised here: an Agent opts out of the
    # shared copy by having its own directory, so creating these empty would
    # leave the new Agent with no skills and no knowledge at all.
    for dirname in ("skills", "knowledge"):
        assert not (workspace / dirname).exists()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["unrelated_setting"] == "preserved"
    # The roster left config.json entirely rather than being duplicated there.
    assert "agents" not in config
    assert [item["id"] for item in _saved(root)["agents"]] == ["primary", "research"]


@pytest.mark.parametrize("submitted_scene_id", [None, "retired-scene"])
def test_retired_scene_does_not_block_editing_an_existing_agent(admin, monkeypatch, submitted_scene_id):
    service, root, _ = admin
    monkeypatch.setattr("agent.admin._scene_exists", lambda _id: True)
    service.create_agent("research", "Research", str(root / "research"), scene_id="retired-scene")
    monkeypatch.setattr("agent.admin._scene_exists", lambda _id: False)

    updated = service.update_agent("research", name="Updated research", scene_id=submitted_scene_id)
    assert updated["name"] == "Updated research"
    assert updated["scene_id"] == "retired-scene"
    with pytest.raises(AgentAdminError, match="does not exist"):
        service.update_agent("research", scene_id="another-missing-scene")
    cleared = service.update_agent("research", scene_id="")
    assert cleared.get("scene_id") is None


def test_new_agent_reads_the_installed_shared_skills(admin):
    service, root, _ = admin
    (root / "primary" / "skills" / "web-search").mkdir(parents=True)

    service.create_agent("research", "Research", str(root / "research"))

    from common.runtime_identity import RuntimeIdentity
    from common import state_dir

    _pin({"agent_workspace": str(root)})
    resolved = state_dir.skills_dir(RuntimeIdentity(agent_id="research"))
    assert resolved == root / "primary" / "skills"
    assert [p.name for p in resolved.iterdir()] == ["web-search"]


def test_workspace_defaults_to_the_derived_per_agent_location(admin):
    service, root, _ = admin

    created = service.create_agent("sales", "Sales")

    assert created["workspace"] == str((root / "agents" / "sales").resolve())
    assert (root / "agents" / "sales" / "AGENT.md").is_file()


def test_clone_copies_persona_but_not_secrets_history_or_nested_agents(admin):
    service, root, _ = admin
    source = root / "primary"
    (source / "AGENT.md").write_text("# Primary persona", encoding="utf-8")
    (source / "MEMORY.md").write_text("what I learned about my user", encoding="utf-8")
    (source / ".env").write_text("OPENAI_API_KEY=sk-secret", encoding="utf-8")
    (source / "memory" / "long-term").mkdir(parents=True)
    (source / "memory" / "long-term" / "index.db").write_text("history", encoding="utf-8")

    clone = root / "clone"
    service.create_agent("clone", "Clone", str(clone), clone_from="primary")

    assert (clone / "AGENT.md").read_text(encoding="utf-8") == "# Primary persona"
    assert not (clone / ".env").exists()
    assert not (clone / "memory" / "long-term" / "index.db").exists()
    # MEMORY.md is scaffolded from the template, not inherited from the source.
    assert "what I learned about my user" not in (clone / "MEMORY.md").read_text(
        encoding="utf-8"
    )


def test_cloning_the_default_agent_into_its_own_subtree_terminates(tmp_path):
    """A whole-tree copy here recursed until the path length was rejected.

    On the default layout the first Agent's workspace *is* the instance root, so
    every later Agent lands inside it and cloning the first one means copying a
    directory into a directory beneath itself. Asking for a copy of the Agent
    you already have is the most ordinary thing a user can do.
    """
    settings = {"agent_workspace": str(tmp_path)}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(settings), encoding="utf-8")
    _pin(settings)
    try:
        service = AgentAdminService(str(config_path))
        (tmp_path / "AGENT.md").write_text("# Root persona", encoding="utf-8")
        (tmp_path / "skills" / "web-search").mkdir(parents=True)
        (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-secret", encoding="utf-8")

        created = service.create_agent("assistant", "Assistant", clone_from="default")

        nested = tmp_path / "agents" / "assistant"
        assert created["workspace"] == str(nested.resolve())
        assert (nested / "AGENT.md").read_text(encoding="utf-8") == "# Root persona"
        assert not (nested / "agents").exists()
        assert not (nested / "skills").exists()
        assert not (nested / ".env").exists()
    finally:
        set_agent_registry(None)


def test_workspace_overlapping_another_agent_is_rejected(admin):
    service, root, config_path = admin
    before = config_path.read_text(encoding="utf-8")

    with pytest.raises(AgentAdminError):
        service.create_agent("nested", "Nested", str(root / "primary" / "inside"))
    with pytest.raises(AgentAdminError):
        service.create_agent("outer", "Outer", str(root))

    assert config_path.read_text(encoding="utf-8") == before


def test_archive_disables_profile_without_deleting_workspace(admin):
    service, root, _ = admin
    workspace = root / "research"
    service.create_agent("research", "Research", str(workspace))

    archived = service.archive_agent("research")

    assert archived["enabled"] is False
    assert workspace.is_dir()
    assert next(
        item for item in service.snapshot()["agents"] if item["id"] == "research"
    )["enabled"] is False


def test_default_agent_cannot_be_archived(admin):
    service, _, _ = admin
    with pytest.raises(Exception):
        service.archive_agent("primary")


def test_delete_agent_removes_roster_entry_and_own_workspace(admin):
    service, root, _ = admin
    workspace = root / "agents" / "research"
    service.create_agent("research", "Research")
    assert workspace.is_dir()

    result = service.delete_agent("research")

    assert result["deleted"] is True
    assert [item["id"] for item in service.snapshot()["agents"]] == ["primary"]
    assert not workspace.exists()


def test_delete_agent_refuses_while_a_channel_instance_routes_to_it(admin):
    """Task 4.3 replaced the old silent unbind with a conflict.

    The previous behaviour cleared the instance's ``agent_id`` so it would "fall
    back to the default Agent" — which moved live IM traffic to an Agent nobody
    chose. The spec forbids automatic rebinding (存在运行或有效渠道引用时 SHALL 返回
    冲突，不自动终止、改绑或回落), so the delete is refused until the operator
    unlinks the channel, and the refusal must leave *both* stores untouched.
    """
    from agent.admin import AgentInUseError

    service, root, config_path = admin
    service.create_agent("research", "Research")

    # Seed a channel instance bound to the Agent about to be deleted, writing it
    # into the roster wherever the app keeps it (team.json once migrated).
    settings = {"agent_workspace": str(root)}
    roster = team.read(settings)
    roster["channel_instances"] = [
        {"instance_id": "feishu-ops", "channel_type": "feishu", "agent_id": "research"}
    ]
    team.write(settings, roster)
    service._settings = None

    with pytest.raises(AgentInUseError) as excinfo:
        service.delete_agent("research")

    assert excinfo.value.code == "conflict"
    assert "feishu-ops" in str(excinfo.value), (
        "the refusal names the dependency, so the operator knows what to unlink")

    # Nothing moved: the Agent is still there, the channel still points at it.
    assert "research" in [item["id"] for item in service.snapshot()["agents"]]
    instances = _saved(root).get("channel_instances") or []
    assert [i.get("agent_id") for i in instances if i.get("instance_id") == "feishu-ops"] \
        == ["research"]


def test_delete_agent_proceeds_once_the_channel_is_unlinked(admin):
    """The conflict is a "not yet", not a "never": unlink, then delete."""
    from agent.deletion_guard import deletion_conflicts

    service, root, config_path = admin
    service.create_agent("research", "Research")
    settings = {"agent_workspace": str(root)}
    roster = team.read(settings)
    roster["channel_instances"] = [
        {"instance_id": "feishu-ops", "channel_type": "feishu", "agent_id": "research"}
    ]
    team.write(settings, roster)
    service._settings = None

    # The operator unlinks first — the same write the console does on disconnect.
    roster = team.read(settings)
    roster["channel_instances"][0]["agent_id"] = ""
    team.write(settings, roster)
    service._settings = None

    assert deletion_conflicts("research") == []
    result = service.delete_agent("research")

    assert result["deleted"] is True
    assert "research" not in [item["id"] for item in service.snapshot()["agents"]]


def test_delete_agent_purges_session_prefs_orphans_and_team_members(admin):
    """Deleting an Agent must leave no dangling references in session prefs.

    Two kinds of ghost would otherwise linger: the Agent's own session
    overrides (keyed ``{id}::*``), and its id sitting in another Agent's team
    roster. Both have to go, while an unrelated session is left untouched.
    """
    from agent.workspace import session_prefs

    service, _, _ = admin
    service.create_agent("research", "Research")

    # The deleted Agent owns a session override, is a teammate in the primary
    # Agent's team conversation, and an unrelated session must survive intact.
    session_prefs.set_prefs("s1", agent_id="research", model="claude-sonnet-5")
    session_prefs.set_prefs("s2", agent_id="primary", members=["research", "primary"])
    session_prefs.set_prefs("s3", agent_id="primary", model="gpt-5")

    service.delete_agent("research")

    assert session_prefs.get_prefs("s1", agent_id="research") == {}
    assert session_prefs.get_prefs("s2", agent_id="primary")["members"] == ["primary"]
    assert session_prefs.get_prefs("s3", agent_id="primary") == {"model": "gpt-5"}


def test_default_agent_cannot_be_deleted(admin):
    service, _, _ = admin
    with pytest.raises(Exception):
        service.delete_agent("primary")


def test_core_file_write_is_allowlisted_atomic_and_revision_guarded(admin):
    service, root, _ = admin
    workspace = root / "research"
    service.create_agent("research", "Research", str(workspace))
    original = service.read_core_file("research", "AGENT.md")

    saved = service.write_core_file(
        "research", "AGENT.md", "# Updated persona\n", original["revision"]
    )

    assert saved["revision"] != original["revision"]
    assert (workspace / "AGENT.md").read_text(encoding="utf-8") == "# Updated persona\n"
    with pytest.raises(StaleAgentFileError):
        service.write_core_file(
            "research", "AGENT.md", "stale", original["revision"]
        )
    with pytest.raises(AgentAdminError):
        service.read_core_file("research", "../config.json")


def test_duplicate_or_nonempty_workspace_is_rejected_without_config_change(admin):
    service, root, config_path = admin
    occupied = root / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("keep", encoding="utf-8")
    before = config_path.read_text(encoding="utf-8")

    with pytest.raises(AgentAdminError):
        service.create_agent("research", "Research", str(occupied))

    assert config_path.read_text(encoding="utf-8") == before
    assert (occupied / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_a_concurrent_unrelated_setting_survives_a_roster_write(admin):
    """The console writes one config file from several pages.

    Reading the whole file, editing the roster and writing the whole thing back
    drops anything another page saved in between, which is silent data loss the
    user only notices later.
    """
    service, root, config_path = admin
    service.snapshot()

    stored = json.loads(config_path.read_text(encoding="utf-8"))
    stored["model"] = "chosen-on-another-page"
    config_path.write_text(json.dumps(stored), encoding="utf-8")
    service._settings = None  # drop the cache the way a fresh request would

    service.create_agent("research", "Research", str(root / "research"))

    assert (
        json.loads(config_path.read_text(encoding="utf-8"))["model"]
        == "chosen-on-another-page"
    )
    assert [item["id"] for item in _saved(root)["agents"]] == ["primary", "research"]


def test_a_stale_roster_revision_is_refused(admin):
    service, root, _ = admin
    stale = service.snapshot()["revision"]
    service.create_agent("first", "First", str(root / "first"))

    with pytest.raises(StaleRosterError):
        service.create_agent(
            "second", "Second", str(root / "second"), revision=stale
        )


def test_per_agent_asset_selection_round_trips(admin):
    service, root, _ = admin

    def stored(agent_id):
        return next(
            item for item in _saved(root)["agents"] if item["id"] == agent_id
        )

    service.create_agent(
        "research", "Research", str(root / "research"), skills=["web-search"]
    )
    assert stored("research")["skills"] == ["web-search"]

    # Absent means "everything", so it must not be confused with an empty
    # selection, which means "nothing".
    assert "skills" not in stored("primary")

    service.update_agent("research", skills=[])
    assert stored("research")["skills"] == []

    # Explicit None means "all of them" again, and is distinct from omitting
    # the argument (which would leave the empty list in place).
    service.update_agent("research", skills=None)
    assert "skills" not in stored("research")


def test_an_agents_model_travels_with_its_provider(admin):
    """A model asked of the wrong vendor is an error, so the two move together."""
    service, root, _ = admin
    service.create_agent("research", "Research", str(root / "research"))

    def stored():
        return next(
            item for item in _saved(root)["agents"] if item["id"] == "research"
        )

    service.update_agent("research", model="claude-sonnet-4", bot_type="claude")
    assert (stored()["model"], stored()["bot_type"]) == ("claude-sonnet-4", "claude")

    # Back to following the configured model: the provider goes with it, rather
    # than lingering to route somebody else's model.
    service.update_agent("research", model="")
    assert "model" not in stored() and "bot_type" not in stored()


def test_the_default_agent_follows_the_configured_model(admin):
    service, root, _ = admin
    with pytest.raises(AgentAdminError):
        service.update_agent("primary", model="claude-sonnet-4")


def test_promotion_drops_the_agents_own_model(admin):
    """Otherwise settings and the Agent would both claim to set the model."""
    service, root, _ = admin
    service.create_agent("research", "Research", str(root / "research"))
    service.update_agent("research", model="claude-sonnet-4", bot_type="claude")

    service.update_agent("research", make_default=True)
    promoted = next(
        item for item in _saved(root)["agents"] if item["id"] == "research"
    )
    assert "model" not in promoted and "bot_type" not in promoted


def test_a_description_round_trips(admin):
    service, root, _ = admin
    service.create_agent(
        "research", "Research", str(root / "research"), description="Digs up sources"
    )

    def stored():
        return next(
            item for item in _saved(root)["agents"] if item["id"] == "research"
        )

    assert stored()["description"] == "Digs up sources"
    service.update_agent("research", description="")
    assert "description" not in stored()


def test_new_agent_can_start_with_its_own_knowledge(admin):
    service, root, _ = admin
    service.create_agent("research", "Research", str(root / "research"), knowledge_mode="own")
    kdir = root / "research" / "knowledge"
    assert kdir.is_dir() and not kdir.is_symlink()
    assert (kdir / "index.md").is_file()
    assert service.knowledge_mode("research") == "own"


def test_new_agent_inherits_the_operator_profile(admin):
    service, root, _ = admin
    # The default Agent's USER.md is what a fresh Agent should carry over.
    (root / "primary" / "USER.md").write_text("# Operator\n- name: Zhang", encoding="utf-8")

    service.create_agent("research", "Research", str(root / "research"))

    seeded = (root / "research" / "USER.md").read_text(encoding="utf-8")
    assert "Zhang" in seeded


def test_knowledge_mode_defaults_to_shared(admin):
    service, root, _ = admin
    service.create_agent("research", "Research", str(root / "research"))
    # No knowledge/ dir of its own -> reads the shared base.
    assert service.knowledge_mode("research") == "shared"
    assert not (root / "research" / "knowledge").exists()


def test_switching_to_own_creates_a_real_knowledge_dir(admin):
    service, root, _ = admin
    service.create_agent("research", "Research", str(root / "research"))

    result = service.set_knowledge_mode("research", "own")

    assert result == {"id": "research", "mode": "own", "changed": True}
    kdir = root / "research" / "knowledge"
    assert kdir.is_dir() and not kdir.is_symlink()
    assert (kdir / "index.md").is_file()
    assert service.knowledge_mode("research") == "own"


def test_switching_back_to_shared_links_the_shared_base(admin):
    service, root, _ = admin
    service.create_agent("research", "Research", str(root / "research"))
    service.set_knowledge_mode("research", "own")
    # Empty own base is safe to swap for the shared link.
    result = service.set_knowledge_mode("research", "shared")

    assert result["mode"] == "shared"
    kdir = root / "research" / "knowledge"
    assert kdir.is_symlink()
    assert service.knowledge_mode("research") == "shared"


def test_shared_sets_aside_a_non_empty_own_base_and_own_restores_it(admin):
    service, root, _ = admin
    service.create_agent("research", "Research", str(root / "research"))
    service.set_knowledge_mode("research", "own")
    kdir = root / "research" / "knowledge"
    (kdir / "note.md").write_text("keep me", encoding="utf-8")

    # Shared is only a reference: the switch is allowed, the data is kept aside.
    result = service.set_knowledge_mode("research", "shared")
    assert result["mode"] == "shared" and result["changed"] is True
    assert kdir.is_symlink()
    stash = root / "research" / "knowledge.own"
    assert (stash / "note.md").read_text(encoding="utf-8") == "keep me"

    # Going back to own brings the very same base back, nothing recreated.
    result = service.set_knowledge_mode("research", "own")
    assert result["mode"] == "own" and result["changed"] is True
    assert kdir.is_dir() and not kdir.is_symlink()
    assert (kdir / "note.md").read_text(encoding="utf-8") == "keep me"
    assert not stash.exists()


def _write_roster(root, agents, default_id):
    """Build a config with an explicit roster and pin the registry to it."""
    settings = {
        "agent_workspace": str(root),
        "default_agent_id": default_id,
        "agents": agents,
        "channel_instances": [],
    }
    config_path = root / "config.json"
    config_path.write_text(json.dumps(settings), encoding="utf-8")
    _pin(settings)
    return AgentAdminService(str(config_path))


def test_default_agent_at_the_instance_root_owns_the_shared_base(tmp_path):
    """The classic single-Agent layout, and the case the old rule was written for.

    The default Agent's workspace *is* the instance root, so its ``knowledge/``
    is the shared base itself. It must keep reporting "shared", and switching it
    must be refused — ``shared`` mode would rename the whole team's base to
    ``knowledge.own`` and replace it with an empty directory.
    """
    root = tmp_path
    (root / "knowledge").mkdir(parents=True)
    (root / "knowledge" / "shared.md").write_text("# shared\n", encoding="utf-8")
    service = _write_roster(root, [
        {"id": "primary", "name": "Primary", "workspace": str(root), "enabled": True},
    ], "primary")
    try:
        assert service.knowledge_mode("primary") == "shared"

        with pytest.raises(AgentAdminError):
            service.set_knowledge_mode("primary", "own")

        # Untouched: not moved aside, not replaced by a symlink.
        assert (root / "knowledge" / "shared.md").is_file()
        assert not (root / "knowledge").is_symlink()
        assert not (root / "knowledge.own").exists()
    finally:
        set_agent_registry(None)


def test_default_agent_moved_into_a_private_workspace_reports_and_switches_own(
        tmp_path, monkeypatch):
    """A default Agent given a workspace of its own reads its own base.

    Reporting "shared" here lied about the data root and blocked the switch, so
    the console showed a mode the Agent did not have and offered no way out.
    ``shared_root`` is pinned to the instance root to stand in for the
    tenant-aware resolution the console runs under — this deployment's layout:
    the shared root is the instance root while the default Agent lives in
    ``agents/``.
    """
    root = tmp_path
    monkeypatch.setattr(state_dir, "shared_root", lambda: root)
    ws = root / "agents" / "primary"
    (ws / "knowledge").mkdir(parents=True)
    (ws / "knowledge" / "note.md").write_text("keep me\n", encoding="utf-8")
    service = _write_roster(root, [
        {"id": "primary", "name": "Primary", "workspace": str(ws), "enabled": True},
    ], "primary")
    try:
        assert service.knowledge_mode("primary") == "own"

        result = service.set_knowledge_mode("primary", "shared")
        assert result["mode"] == "shared"
        assert (ws / "knowledge").is_symlink()
        assert (ws / "knowledge.own" / "note.md").read_text(encoding="utf-8").strip() == "keep me"

        service.set_knowledge_mode("primary", "own")
        assert service.knowledge_mode("primary") == "own"
        assert (ws / "knowledge" / "note.md").read_text(encoding="utf-8").strip() == "keep me"
    finally:
        set_agent_registry(None)


def test_agent_whose_workspace_is_the_instance_root_is_not_own(tmp_path, monkeypatch):
    """A non-default Agent parked on the instance root reads the shared base too.

    Reporting it as "own" (the old rule only special-cased the *default* Agent)
    would let a switch move the team's base aside. ``shared_root`` is pinned to
    the instance root so that is the base under test.
    """
    root = tmp_path
    monkeypatch.setattr(state_dir, "shared_root", lambda: root)
    (root / "knowledge").mkdir(parents=True)
    (root / "knowledge" / "shared.md").write_text("# shared\n", encoding="utf-8")
    service = _write_roster(root, [
        {"id": "primary", "name": "Primary",
         "workspace": str(root / "agents" / "primary"), "enabled": True},
        {"id": "rogue", "name": "Rogue", "workspace": str(root), "enabled": True},
    ], "primary")
    try:
        assert service.knowledge_mode("rogue") == "shared"

        with pytest.raises(AgentAdminError):
            service.set_knowledge_mode("rogue", "own")

        assert (root / "knowledge" / "shared.md").is_file()
        assert not (root / "knowledge").is_symlink()
        assert not (root / "knowledge.own").exists()
    finally:
        set_agent_registry(None)


def test_snapshot_reports_knowledge_mode(admin):
    service, root, _ = admin
    service.create_agent("research", "Research", str(root / "research"))
    service.set_knowledge_mode("research", "own")

    modes = {a["id"]: a.get("knowledge_mode") for a in service.snapshot()["agents"]}
    assert modes["primary"] == "shared"
    assert modes["research"] == "own"


# --- Coding Agents --------------------------------------------------------
#
# A coding Agent is created, edited and cloned through the same service, so the
# console does not need a second write path. What these tests pin down is that
# the type is stated once, kept, and never smuggled into a normal Agent.


def test_coding_agent_requires_a_project_and_is_saved(admin):
    service, root, _ = admin

    created = service.create_agent(
        "erp-coder",
        "ERP Coder",
        agent_type="coding",
        coding_project_dir="  /srv/checkouts/erp  ",
    )

    assert created["agent_type"] == "coding"
    assert created["coding_project_dir"] == "/srv/checkouts/erp"
    assert created["workspace"] == str(root / "agents" / "erp-coder")
    saved = {a["id"]: a for a in _saved(root)["agents"]}
    assert saved["erp-coder"]["agent_type"] == "coding"
    assert saved["erp-coder"]["coding_project_dir"] == "/srv/checkouts/erp"
    # The project directory is the OpenCode host's business: this side never
    # creates it, and the Agent's own workspace stays a derived platform path.
    assert "workspace" not in saved["erp-coder"]
    assert not Path("/srv/checkouts/erp").exists()


def test_coding_agent_without_a_project_is_refused(admin):
    service, root, _ = admin

    with pytest.raises(AgentAdminError, match="coding_project_dir"):
        service.create_agent("erp-coder", "ERP Coder", agent_type="coding")

    assert [a["id"] for a in service.snapshot()["agents"]] == ["primary"]
    # The refusal left no half-built Agent behind on disk.
    assert not (root / "agents" / "erp-coder").exists()


def test_normal_agent_ignores_a_submitted_project_dir(admin):
    """A project sent for a normal Agent is dropped rather than stored."""
    service, root, _ = admin

    created = service.create_agent(
        "research",
        "Research",
        str(root / "research"),
        coding_project_dir="/srv/checkouts/erp",
    )

    assert created["agent_type"] == "normal"
    assert created["coding_project_dir"] is None
    saved = {a["id"]: a for a in _saved(root)["agents"]}
    assert "coding_project_dir" not in saved["research"]
    assert "agent_type" not in saved["research"]


def test_agent_type_cannot_be_changed_after_creation(admin):
    """The type decides what every other field means, so it is not editable."""
    service, root, _ = admin
    service.create_agent("research", "Research", str(root / "research"))

    with pytest.raises(AgentAdminError, match="cannot be changed"):
        service.update_agent("research", agent_type="coding")

    with pytest.raises(AgentAdminError, match="must be one of"):
        service.update_agent("research", agent_type="codeing")


def test_editing_a_coding_agents_default_project_keeps_the_type(admin):
    service, root, _ = admin
    service.create_agent(
        "erp-coder", "ERP Coder",
        agent_type="coding", coding_project_dir="/srv/checkouts/erp",
    )

    updated = service.update_agent("erp-coder", coding_project_dir="/srv/checkouts/erp-v2")

    assert updated["agent_type"] == "coding"
    assert updated["coding_project_dir"] == "/srv/checkouts/erp-v2"
    saved = {a["id"]: a for a in _saved(root)["agents"]}
    assert saved["erp-coder"]["agent_type"] == "coding"
    assert saved["erp-coder"]["coding_project_dir"] == "/srv/checkouts/erp-v2"
    # An omitted project leaves the stored one alone.
    service.update_agent("erp-coder", name="ERP Coder Renamed")
    saved = {a["id"]: a for a in _saved(root)["agents"]}
    assert saved["erp-coder"]["coding_project_dir"] == "/srv/checkouts/erp-v2"


def test_coding_agent_cannot_become_the_default(admin):
    service, root, _ = admin
    service.create_agent(
        "erp-coder", "ERP Coder",
        agent_type="coding", coding_project_dir="/srv/checkouts/erp",
    )

    with pytest.raises(AgentAdminError, match="cannot be the default agent"):
        service.update_agent("erp-coder", make_default=True)

    assert _saved(root)["default_agent_id"] == "primary"


def test_cloning_a_coding_agent_copies_configuration_not_conversations(admin):
    service, root, _ = admin
    service.create_agent(
        "erp-coder", "ERP Coder",
        agent_type="coding", coding_project_dir="/srv/checkouts/erp",
    )

    clone = service.clone_agent("erp-coder", "erp-coder-2", "ERP Coder 2")

    assert clone["agent_type"] == "coding"
    assert clone["coding_project_dir"] == "/srv/checkouts/erp"
    saved = {a["id"]: a for a in _saved(root)["agents"]}
    assert saved["erp-coder-2"]["agent_type"] == "coding"


def test_cloning_a_normal_agent_stays_normal(admin):
    service, root, _ = admin
    service.create_agent("research", "Research", str(root / "research"))

    clone = service.clone_agent("research", "research-2", "Research 2")

    assert clone["agent_type"] == "normal"
    assert clone["coding_project_dir"] is None
