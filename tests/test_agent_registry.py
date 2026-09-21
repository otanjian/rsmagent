from pathlib import Path

import pytest

from agent.registry import AgentProfile, AgentRegistry, AgentRegistryError


def test_legacy_config_synthesizes_default_agent(tmp_path):
    workspace = tmp_path / "cow"
    registry = AgentRegistry.from_config({"agent_workspace": str(workspace)})

    profile = registry.get()
    assert profile.id == "default"
    assert profile.name == "RongAI"
    assert profile.workspace_path == workspace.resolve()
    assert registry.default_agent_id == "default"


@pytest.mark.parametrize("legacy_name", ["CowAgent", "cowagent", " COWAGENT "])
def test_legacy_product_name_is_normalized_without_moving_the_agent(tmp_path, legacy_name):
    settings = {
        "agent_workspace": str(tmp_path),
        "default_agent_id": "main",
        "agents": [
            {"id": "main", "name": legacy_name},
            {"id": "erpnext", "name": "ERPnext助手"},
        ],
    }
    registry = AgentRegistry.from_config(settings)

    assert registry.get().name == "RongAI"
    assert registry.get().id == "main"
    assert registry.get().workspace_path == tmp_path.resolve()
    assert registry.get("erpnext").name == "ERPnext助手"
    assert settings["agents"][0]["name"] == legacy_name


def test_configured_agents_keep_separate_workspaces(tmp_path):
    registry = AgentRegistry.from_config(
        {
            "default_agent_id": "writer",
            "agents": [
                {"id": "writer", "name": "Writer", "workspace": str(tmp_path / "writer")},
                {
                    "id": "research",
                    "name": "Research",
                    "workspace": str(tmp_path / "research"),
                    "model": "gpt-5",
                    "bot_type": "openai",
                },
            ],
        }
    )

    assert registry.get().id == "writer"
    assert registry.get("research").model == "gpt-5"
    assert registry.get("research").bot_type == "openai"
    assert [profile.id for profile in registry.list()] == ["research", "writer"]


def test_omitted_default_falls_back_to_first_enabled_agent(tmp_path):
    registry = AgentRegistry.from_config(
        {
            "agents": [
                {"id": "writer", "workspace": str(tmp_path / "writer"), "enabled": False},
                {"id": "research", "workspace": str(tmp_path / "research")},
            ]
        }
    )

    assert registry.default_agent_id == "research"


def test_an_omitted_workspace_is_derived_from_the_instance_root(tmp_path):
    """Adding an Agent should cost a name, not a path. The default Agent keeps
    the instance root because that is where a single-Agent install already has
    everything: writing an `agents` list around an existing workspace must not
    relocate it."""
    root = tmp_path / "cow"
    registry = AgentRegistry.from_config(
        {
            "agent_workspace": str(root),
            "agents": [{"id": "main", "name": "Main"}, {"id": "sales", "name": "Sales"}],
        }
    )

    assert registry.get("main").workspace_path == root.resolve()
    assert registry.get("sales").workspace_path == (root / "agents" / "sales").resolve()


def test_the_explicit_default_is_the_one_that_keeps_the_instance_root(tmp_path):
    root = tmp_path / "cow"
    registry = AgentRegistry.from_config(
        {
            "agent_workspace": str(root),
            "default_agent_id": "sales",
            "agents": [{"id": "main"}, {"id": "sales"}],
        }
    )

    assert registry.get("sales").workspace_path == root.resolve()
    assert registry.get("main").workspace_path == (root / "agents" / "main").resolve()


def test_an_explicit_workspace_still_wins_and_an_empty_one_still_fails(tmp_path):
    registry = AgentRegistry.from_config(
        {
            "agent_workspace": str(tmp_path / "cow"),
            "agents": [{"id": "main"}, {"id": "sales", "workspace": str(tmp_path / "elsewhere")}],
        }
    )
    assert registry.get("sales").workspace_path == (tmp_path / "elsewhere").resolve()

    with pytest.raises(AgentRegistryError, match="non-empty string"):
        AgentRegistry.from_config(
            {"agent_workspace": str(tmp_path / "cow"), "agents": [{"id": "main", "workspace": ""}]}
        )


@pytest.mark.parametrize("agent_id", ["", "has space", "/root", "x" * 65])
def test_invalid_agent_ids_are_rejected(tmp_path, agent_id):
    with pytest.raises(AgentRegistryError, match="agent id"):
        AgentRegistry.from_config(
            {"agents": [{"id": agent_id, "workspace": str(tmp_path / "one")}]}
        )


def test_duplicate_ids_and_workspaces_are_rejected(tmp_path):
    with pytest.raises(AgentRegistryError, match="duplicate agent id"):
        AgentRegistry.from_config(
            {
                "agents": [
                    {"id": "one", "workspace": str(tmp_path / "one")},
                    {"id": "one", "workspace": str(tmp_path / "two")},
                ],
                "default_agent_id": "one",
            }
        )

    with pytest.raises(AgentRegistryError, match="share workspace"):
        AgentRegistry.from_config(
            {
                "agents": [
                    {"id": "one", "workspace": str(tmp_path / "shared")},
                    {"id": "two", "workspace": str(tmp_path / "shared")},
                ],
                "default_agent_id": "one",
            }
        )


def test_default_agent_must_exist_and_be_enabled(tmp_path):
    with pytest.raises(AgentRegistryError, match="not configured"):
        AgentRegistry.from_config(
            {
                "agents": [{"id": "one", "workspace": str(tmp_path / "one")}],
                "default_agent_id": "missing",
            }
        )

    with pytest.raises(AgentRegistryError, match="disabled"):
        AgentRegistry.from_config(
            {
                "agents": [
                    {"id": "one", "workspace": str(tmp_path / "one"), "enabled": False}
                ],
                "default_agent_id": "one",
            }
        )


def test_registry_mutations_preserve_default_invariants(tmp_path):
    registry = AgentRegistry.from_config({"agent_workspace": str(tmp_path / "default")})
    second = AgentProfile(
        id="second",
        name="Second",
        workspace=str((tmp_path / "second").resolve()),
    )
    registry.upsert(second)

    registry.set_default("second")
    registry.set_enabled("default", False)
    assert registry.get().id == "second"
    assert registry.get_or_default("default").id == "second"

    with pytest.raises(AgentRegistryError, match="default agent cannot be disabled"):
        registry.set_enabled("second", False)
    with pytest.raises(AgentRegistryError, match="default agent cannot be removed"):
        registry.remove("second")

    removed = registry.remove("default")
    assert removed.id == "default"
    assert [profile.id for profile in registry.list()] == ["second"]


def test_profile_to_dict_omits_empty_overrides(tmp_path):
    profile = AgentProfile(
        id="default",
        name="Default",
        workspace=str(Path(tmp_path).resolve()),
    )
    assert profile.to_dict() == {
        "id": "default",
        "name": "Default",
        "workspace": str(Path(tmp_path).resolve()),
        "enabled": True,
    }


# --- Agent types ---------------------------------------------------------
#
# A ``coding`` Agent is an entry point into a shared OpenCode service, not a
# second kind of runtime. Two properties carry the whole boundary: reading an
# old profile must not invent a type (or rewrite the file), and no ordinary
# runtime path may ever be handed a coding Agent.


def test_agents_without_a_type_are_normal_and_unrewritten(tmp_path):
    """The compatibility red line: loading must not change what is on disk."""
    settings = {
        "agent_workspace": str(tmp_path / "cow"),
        "default_agent_id": "main",
        "agents": [{"id": "main", "name": "Main"}, {"id": "sales", "name": "Sales"}],
    }
    registry = AgentRegistry.from_config(settings)

    assert registry.get("main").agent_type == "normal"
    assert registry.get("main").is_coding is False
    # Absent stays absent: a normal Agent's type is never written back.
    assert "agent_type" not in registry.get("main").to_dict()
    assert "agent_type" not in settings["agents"][0]
    assert "coding_project_dir" not in registry.get("main").to_dict()


def test_coding_agent_keeps_its_remote_project_verbatim(tmp_path):
    """The project directory lives on the OpenCode host, not on this one.

    Resolving or normalising it here would silently rewrite a remote POSIX path
    into whatever the platform host thinks it means, so it is carried as given
    and never created locally.
    """
    remote = "/srv/checkouts/erp"
    registry = AgentRegistry.from_config(
        {
            "agent_workspace": str(tmp_path / "cow"),
            "default_agent_id": "main",
            "agents": [
                {"id": "main", "name": "Main"},
                {
                    "id": "erp-coder",
                    "name": "ERP Coder",
                    "agent_type": "coding",
                    "coding_project_dir": remote,
                },
            ],
        }
    )

    profile = registry.get("erp-coder")
    assert profile.is_coding is True
    assert profile.coding_project_dir == remote
    assert profile.to_dict()["agent_type"] == "coding"
    assert profile.to_dict()["coding_project_dir"] == remote
    # Nothing was scaffolded for it: its workspace is the platform state
    # directory, and that is not where the code lives.
    assert not Path(remote).exists()


def test_coding_agent_without_a_project_is_a_config_error(tmp_path):
    """Without a project there is nothing to open, so the profile is unusable."""
    with pytest.raises(AgentRegistryError, match="requires a coding_project_dir"):
        AgentRegistry.from_config(
            {
                "agent_workspace": str(tmp_path / "cow"),
                "agents": [{"id": "erp-coder", "agent_type": "coding"}],
            }
        )


def test_unknown_agent_type_is_refused_rather_than_defaulted(tmp_path):
    """A typo must be reported, not silently answered by a normal runtime."""
    with pytest.raises(AgentRegistryError, match="agent_type must be one of"):
        AgentRegistry.from_config(
            {
                "agent_workspace": str(tmp_path / "cow"),
                "agents": [{"id": "main", "agent_type": "codeing"}],
            }
        )


def test_a_normal_agent_never_keeps_a_remote_project(tmp_path):
    """A project submitted for a normal Agent is dropped, not stored.

    Keeping it would make the field mean two things depending on the type, and
    a later type-agnostic reader could not tell which one it was looking at.
    """
    registry = AgentRegistry.from_config(
        {
            "agent_workspace": str(tmp_path / "cow"),
            "agents": [
                {
                    "id": "main",
                    "name": "Main",
                    "coding_project_dir": "/srv/checkouts/erp",
                }
            ],
        }
    )

    assert registry.get("main").coding_project_dir is None
    assert "coding_project_dir" not in registry.get("main").to_dict()


def test_coding_agent_cannot_be_the_default(tmp_path):
    """An instance default is what every unaddressed message lands on."""
    with pytest.raises(AgentRegistryError, match="cannot be the default agent"):
        AgentRegistry.from_config(
            {
                "agent_workspace": str(tmp_path / "cow"),
                "default_agent_id": "erp-coder",
                "agents": [
                    {"id": "main", "name": "Main"},
                    {
                        "id": "erp-coder",
                        "agent_type": "coding",
                        "coding_project_dir": "/srv/checkouts/erp",
                    },
                ],
            }
        )


def test_promoting_a_coding_agent_to_default_is_refused(tmp_path):
    """The same rule at runtime: no path may appoint one after startup."""
    registry = AgentRegistry.from_config(
        {
            "agent_workspace": str(tmp_path / "cow"),
            "default_agent_id": "main",
            "agents": [
                {"id": "main", "name": "Main"},
                {
                    "id": "erp-coder",
                    "agent_type": "coding",
                    "coding_project_dir": "/srv/checkouts/erp",
                },
            ],
        }
    )

    with pytest.raises(AgentRegistryError, match="cannot be the default agent"):
        registry.set_default("erp-coder")
    assert registry.default_agent_id == "main"


def test_normal_profiles_are_the_only_ordinary_runtime_candidates(tmp_path):
    """Every non-Web consumer starts from ``normal()``, not ``list()``."""
    registry = AgentRegistry.from_config(
        {
            "agent_workspace": str(tmp_path / "cow"),
            "default_agent_id": "main",
            "agents": [
                {"id": "main", "name": "Main"},
                {"id": "stopped", "name": "Stopped", "enabled": False},
                {
                    "id": "erp-coder",
                    "agent_type": "coding",
                    "coding_project_dir": "/srv/checkouts/erp",
                },
            ],
        }
    )

    assert [p.id for p in registry.normal()] == ["main"]
    # ``include_disabled`` still means what it says, minus the coding Agent.
    assert [p.id for p in registry.normal(include_disabled=True)] == ["main", "stopped"]
    assert "erp-coder" in [p.id for p in registry.list(include_disabled=True)]
