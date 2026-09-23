# encoding:utf-8
"""The type a create request names is the type that gets stored.

The roster, the admin service and the registry all understand ``agent_type``;
the console sends it on every create. Nothing joined those two ends, so a
request that asked for a coding Agent was answered with a normal one -- same
name, same workspace, no error, and a project directory that silently went
nowhere. That is the worst shape a boundary bug can take, because the user sees
a created Agent and only finds out later that it is the wrong kind.

These drive the real WSGI app for the same reason the sibling file does: the
claim is about what the HTTP boundary does with the body, which a service-level
test cannot see.

Two harness details are load-bearing:

* the agent id is derived from ``tmp_path``, because the Agent store is a file
  under a workspace shared by the session -- a literal id makes the second test
  in the file collide with the first one's leftover;
* create failures answer HTTP 200 with ``status: "error"`` in the body, which is
  this route's convention (the console branches on the body). Asserting a 4xx
  here would encode a contract the product does not have.
"""

import pytest

from tests._helpers import WebAppHarness

PROJECT_DIR = "/tmp/projects/erp"


@pytest.fixture
def web(tmp_path):
    harness = WebAppHarness(tmp_path / "instance")
    harness.add_agent("shared-agent")
    yield harness
    harness.close()


@pytest.fixture
def agent_id(tmp_path):
    """A name no other test *or run* can collide with.

    The Agent store is a file under a workspace the session shares, so an id has
    to be unique across the whole run and against whatever an earlier run left
    behind; ``tmp_path`` carries both the test name and pytest's per-run base.
    """
    return _uid(tmp_path, "boundary")


def _uid(tmp_path, prefix):
    stem = f"{tmp_path.parent.name}-{tmp_path.name}".replace("_", "-").lower()
    return f"{prefix}-" + stem.removeprefix("pytest-")


def _json(response):
    return WebAppHarness.json(response)


def _status(response):
    return int(response.status.split()[0])


@pytest.fixture
def bare_web(tmp_path):
    """A tenant with no Agents at all.

    The first-Agent appointment only runs when the tenant had none, so a
    harness that pre-binds an Agent cannot see it.
    """
    harness = WebAppHarness(tmp_path / "bare")
    yield harness
    harness.close()


def _create(web, agent_id, **fields):
    body = {"action": "create", "id": agent_id, "name": "Boundary Agent"}
    body.update(fields)
    return web.post("/api/agents", body, token=web.login("root"))


def _catalog(web):
    return _json(web.get("/api/agents", token=web.login("root")))

def _profile(web, agent_id):
    for item in _catalog(web).get("agents", []):
        if item["id"] == agent_id:
            return item
    raise AssertionError(f"{agent_id} is not in the roster projection")


def test_a_coding_agent_can_be_created_with_its_project(web, agent_id):
    response = _create(web, agent_id, agent_type="coding",
                       coding_project_dir=PROJECT_DIR)

    assert _status(response) == 200, response.data
    assert _json(response)["status"] == "success"
    stored = _profile(web, agent_id)
    assert stored["agent_type"] == "coding"
    assert stored["coding_project_dir"] == PROJECT_DIR


def test_a_coding_agent_without_a_project_is_refused_and_not_stored(web, agent_id):
    """The type does not stand alone: it names a service that needs a directory
    to open, so an incomplete pair must be refused -- and refused cleanly, with
    nothing half-created for the console to show."""
    response = _create(web, agent_id, agent_type="coding")

    assert _json(response)["status"] == "error"
    assert "coding_project_dir" in _json(response)["message"]
    assert agent_id not in {a["id"] for a in _catalog(web).get("agents", [])}


def test_an_omitted_type_still_creates_a_normal_agent(web, agent_id):
    """Older clients and the pre-type console must keep working unchanged."""
    response = _create(web, agent_id)

    assert _status(response) == 200, response.data
    stored = _profile(web, agent_id)
    assert stored["agent_type"] == "normal"
    assert stored["coding_project_dir"] is None


def test_a_normal_create_does_not_store_a_project(web, agent_id):
    """A project on a normal Agent is dropped rather than kept as dead config."""
    response = _create(web, agent_id, agent_type="normal",
                       coding_project_dir=PROJECT_DIR)

    assert _status(response) == 200, response.data
    stored = _profile(web, agent_id)
    assert stored["agent_type"] == "normal"
    assert stored["coding_project_dir"] is None


def test_an_update_that_repeats_the_type_is_accepted(web, agent_id):
    """The console round-trips the fields it read, so repeating the stored type
    has to be a no-op rather than a refusal."""
    created = _create(web, agent_id, agent_type="coding",
                      coding_project_dir=PROJECT_DIR)
    revision = _json(created).get("revision") or _catalog(web).get("revision")

    saved = web.post("/api/agents", {
        "action": "update", "id": agent_id, "name": "Renamed",
        "agent_type": "coding", "revision": revision,
    }, token=web.login("root"))

    assert _status(saved) == 200, saved.data
    stored = _profile(web, agent_id)
    assert stored["name"] == "Renamed"
    assert stored["agent_type"] == "coding"
    assert stored["coding_project_dir"] == PROJECT_DIR


def test_the_type_cannot_be_changed_by_an_update(web, agent_id):
    """A rejected update must not half-apply: the type it tried to change stays
    exactly as stored."""
    _create(web, agent_id, agent_type="coding", coding_project_dir=PROJECT_DIR)

    changed = web.post("/api/agents", {
        "action": "update", "id": agent_id, "name": "Boundary Agent",
        "agent_type": "normal", "revision": _catalog(web).get("revision"),
    }, token=web.login("root"))

    assert _json(changed)["status"] == "error"
    assert "cannot be changed" in _json(changed)["message"]
    assert _profile(web, agent_id)["agent_type"] == "coding"


def test_the_coding_project_can_be_moved_by_an_update(web, agent_id):
    """The directory is the one part of the pair that stays editable."""
    _create(web, agent_id, agent_type="coding", coding_project_dir=PROJECT_DIR)

    moved = web.post("/api/agents", {
        "action": "update", "id": agent_id, "name": "Boundary Agent",
        "coding_project_dir": "/tmp/projects/other",
        "revision": _catalog(web).get("revision"),
    }, token=web.login("root"))

    assert _status(moved) == 200, moved.data
    assert _profile(web, agent_id)["coding_project_dir"] == "/tmp/projects/other"


# -- a coding Agent is never the default -------------------------------------


def test_a_coding_agent_created_first_does_not_become_the_tenant_default(bare_web, tmp_path):
    """The first Agent an admin creates is appointed the tenant default, so a
    tenant that starts empty has something to chat with. A coding Agent must be
    left out of that: appointed, it becomes the target of every ordinary chat,
    and each one is then refused for being coding -- a console that looks broken
    rather than one that says "pick an Agent"."""
    coder = _uid(tmp_path, "first-coder")
    response = _create(bare_web, coder, agent_type="coding",
                       coding_project_dir=PROJECT_DIR)

    assert _json(response)["status"] == "success", response.data
    body = _catalog(bare_web)
    assert body["default_agent_id"] != coder
    assert (body.get("default_resolution") or {}).get("agent_id") != coder


def test_a_coding_agent_is_never_resolved_as_the_default_even_alone(bare_web, tmp_path):
    """The runtime chain is the second half: the tenant's only bound Agent is a
    coding one, so the fallback has nothing eligible and must answer "none"
    rather than hand an ordinary send to a coding Agent."""
    coder = _uid(tmp_path, "only-coder")
    created = _create(bare_web, coder, agent_type="coding",
                      coding_project_dir=PROJECT_DIR)
    assert _json(created)["status"] == "success", created.data

    resolved = bare_web.service.resolved_default_agent_id(bare_web.tenant_id)
    assert resolved != coder
    assert (bare_web.service.resolve_default_agent(bare_web.tenant_id) or {}).get(
        "agent_id") != coder


def test_a_normal_agent_created_first_still_becomes_the_default(bare_web, tmp_path):
    """Non-vacuity: the appointment rule itself must survive the coding guard."""
    plain = _uid(tmp_path, "first-plain")
    response = _create(bare_web, plain)

    assert _json(response)["status"] == "success", response.data
    body = _catalog(bare_web)
    assert body["default_agent_id"] == plain
    assert bare_web.service.resolved_default_agent_id(bare_web.tenant_id) == plain


# -- the type boundary inside a team roster ---------------------------------


def _set_team(web, session_id, agent_id, members, username="root"):
    return web.post(f"/api/sessions/{session_id}/settings",
                    {"agent_id": agent_id, "members": members},
                    token=web.login(username))


def _stored_members(web, session_id, agent_id):
    """The roster as the *tenant* stored it, not as the developer's own root."""
    from agent.workspace import session_prefs
    from common.runtime_identity import RuntimeIdentity, use_identity

    with use_identity(RuntimeIdentity(user_id=web.admin_id, tenant_id=web.tenant_id)):
        return session_prefs.get_prefs(session_id, agent_id).get("members") or []


def test_a_coding_agent_is_refused_as_a_team_member(web, tmp_path):
    """Writing a roster validates every member as a whole, and a coding Agent
    has no ordinary runtime to take a turn: it is refused for what it *is*, so
    the conversation cannot be left believing it joined."""
    coder = _uid(tmp_path, "team-coder")
    web.add_agent("team-peer")
    web.add_coding_agent(coder, PROJECT_DIR)

    response = _set_team(web, "type-team-member", "shared-agent", ["team-peer", coder])

    assert _status(response) == 400, response.data
    assert _json(response)["code"] == "coding_web_only"
    assert _stored_members(web, "type-team-member", "shared-agent") == []


def test_a_coding_agent_is_refused_as_the_team_owner(web, tmp_path):
    """The owner position is not a loophole, and an administrator passes no
    bypass: the type rule precedes any grant."""
    coder = _uid(tmp_path, "team-owner-coder")
    web.add_agent("team-peer")
    web.add_coding_agent(coder, PROJECT_DIR)

    response = _set_team(web, "type-team-owner", coder, ["team-peer"])

    assert _status(response) == 400, response.data
    assert _json(response)["code"] == "coding_web_only"
    assert _stored_members(web, "type-team-owner", coder) == []


def test_a_roster_of_normal_agents_is_still_stored(web):
    """Non-vacuity: the coding refusals above are about the type, not about the
    roster path being closed to everyone."""
    web.add_agent("team-peer")

    response = _set_team(web, "type-team-ok", "shared-agent", ["team-peer"])

    assert _status(response) == 200, response.data
    assert _stored_members(web, "type-team-ok", "shared-agent") == ["team-peer"]


