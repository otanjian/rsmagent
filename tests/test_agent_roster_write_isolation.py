# encoding:utf-8
"""A web test must never write the developer's real Agent roster.

The console's Agent admin service is built from the **data root**, not from the
patched ``conf`` this suite installs: ``_agent_admin_service()`` calls
``get_data_root()`` and hands ``AgentAdminService`` a bare ``config.json`` path,
so the service reads and writes ``team.team_file(<real config.json>)``. Every
test that creates, updates or deletes an Agent over the wire therefore landed in
the developer's own ``<instance root>/agents/team.json``.

Two visible consequences, both of which have happened:

* the roster accumulated hundreds of ``boundary-*`` / ``*-alice-NNN`` entries
  from test runs, so the console's Agent list was mostly test debris;
* running the same tests from a checkout where ``config.json`` is *absent* (a
  ``git worktree``, where the file is gitignored) made ``_load()`` return ``{}``.
  The writer then knew only the built-in ``default`` Agent, and still resolved
  ``team_file`` to the real ``~/cow`` roster — so the write **replaced** it,
  discarding every real Agent. ``team.json.bak-clobbered-20260913`` and
  ``team.json.bak-clobbered-20260926-173946`` are the two recorded occurrences.

The guard is a content comparison rather than an mtime one: a rewrite producing
the same bytes costs nothing, and it is the *content* that carries the Agents.
The fixture snapshots and restores the file, so a failing run reports the leak
without leaving the damage behind.
"""

import json
import os
from pathlib import Path

import pytest

from tests._helpers import WebAppHarness


def _developers_roster():
    """The roster file an unpatched Agent admin service resolves to.

    Deliberately *not* ``team.team_file(conf())``: the session fixture in
    ``conftest.py`` rewrites ``conf()["agent_workspace"]``, so every path
    reached through ``conf()`` already lands in a temp tree — which is exactly
    why the leak stays invisible to a naive assertion. The service under test
    never reads ``conf()``; it builds ``AgentAdminService`` from the data root
    and reads ``<data root>/config.json`` itself, so this mirrors that instead.
    """
    import config as config_module
    from agent import team

    cfg_path = Path(os.environ.get("COW_DATA_DIR") or config_module.get_root()) / "config.json"
    data = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    return Path(team.team_file(data))


@pytest.fixture
def real_roster():
    """The developer's roster, restored afterwards if the flow under test leaks."""
    path = _developers_roster()
    existed = path.exists()
    original = path.read_bytes() if existed else None
    yield path, original
    # Self-healing: a leaked write must not survive the run that caught it.
    current = path.read_bytes() if path.exists() else None
    if current != original:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)


def _is_sandboxed(path) -> bool:
    """Whether the roster already lives in a throwaway tree.

    A CI image or an agent sandbox may point the instance root at a temp
    directory, in which case there is nothing of the developer's to protect and
    the guard would only prove that ``/tmp`` stayed in ``/tmp``.
    """
    text = str(path)
    return text.startswith(("/tmp/", "/private/tmp/", "/var/folders/"))


def test_a_wire_create_and_update_stay_out_of_the_real_roster(real_roster, tmp_path):
    path, original = real_roster
    if _is_sandboxed(path):
        pytest.skip(f"the instance root is already a throwaway tree: {path}")

    harness = WebAppHarness(tmp_path / "instance")
    roster_path = os.path.join(harness.instance_root, "agents", "team.json")
    try:
        token = harness.login("root")
        # Two Agents, each with an explicit workspace: the default Agent inherits
        # the instance root otherwise, and then any wire-created Agent nested
        # under ``<instance root>/agents/<id>`` is refused as sitting inside it.
        # A real multi-Agent roster looks the same way.
        harness.write_roster([
            {"id": "roster-anchor", "name": "Roster Anchor",
             "workspace": os.path.join(harness.instance_root, "agents",
                                       "roster-anchor")},
            {"id": "existing-a", "name": "Existing A"},
        ], default="existing-a")
        created = harness.post("/api/agents",
                               {"action": "create", "id": "isolation-probe",
                                "name": "Isolation Probe"},
                               token=token)
        assert harness.json(created)["status"] == "success", created.data
        updated = harness.post("/api/agents",
                               {"action": "update", "id": "isolation-probe",
                                "usage_hint": "leak guard"},
                               token=token)
        assert harness.json(updated)["status"] == "success", updated.data
        deleted = harness.post("/api/agents",
                               {"action": "delete", "id": "isolation-probe"},
                               token=token)
        assert harness.json(deleted)["status"] == "success", deleted.data

        # Every one of those rewrites the roster it read, so the roster in the
        # harness's own instance root is where the results belong -- and what
        # the service read there is what it wrote back.
        roster = json.loads(Path(roster_path).read_text(encoding="utf-8"))
        ids = [a["id"] for a in roster["agents"]]
        assert "roster-anchor" in ids, ids
        assert "existing-a" in ids, ids
        assert "isolation-probe" not in ids, ids
    finally:
        harness.close()

    assert path.exists() == (original is not None), (
        f"a wire write created or removed the developer's roster at {path}")
    assert (path.read_bytes() if path.exists() else None) == original, (
        f"a wire write reached the developer's roster at {path}; the test "
        f"harness must pin the data root to its temp tree")
