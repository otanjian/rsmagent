# encoding:utf-8
"""A coding Agent is refused by every path that would run it normally.

The roster refuses to *store* a coding Agent as a tenant-wide default, but that
is not the whole rule: an id can also arrive from a forged request body, from a
member's own preference, from a team's member list or from a channel binding.
Those are all resolved against the roster at request time, so the only way to
know the product actually refuses them is to drive the real WSGI app and read
the answer off the wire — a handler-level test with a fake registry would prove
the branch exists, not that a caller reaches it.

Two states are asserted for the same agent, because "it refuses" and "it refuses
for the right reason" are different claims: a caller with no `agent.use` grant
must get the ordinary 403 they would get for *any* Agent (the type must not turn
a permission answer into a hint), while an authorized caller who submits a
coding Agent as an execution target must get the coding-only reason.
"""

import pytest

from tests._helpers import WebAppHarness

PROJECT_DIR = "/srv/checkouts/erp"


@pytest.fixture
def web(tmp_path):
    harness = WebAppHarness(tmp_path / "instance")
    harness.add_agent("shared-agent")
    harness.add_coding_agent("erp-coder", PROJECT_DIR)
    yield harness
    harness.close()


def _json(response):
    return WebAppHarness.json(response)


def _status(response):
    return int(response.status.split()[0])


# -- the roster and the projections ---------------------------------------


def test_a_coding_agent_is_a_legal_roster_entry(web):
    """It exists, is bound, and is visible — refusing to *store* it is not the rule."""
    from agent.registry import get_agent_registry

    profile = get_agent_registry().get("erp-coder")
    assert profile.is_coding
    assert profile.coding_project_dir == PROJECT_DIR


def test_the_tenant_projection_carries_the_type(web):
    """The console has to tell the two kinds apart from the list alone."""
    body = _json(web.get("/api/agents", token=web.login("root")))
    agents = {item["id"]: item for item in body.get("agents", [])}
    assert agents["erp-coder"]["agent_type"] == "coding"
    assert agents["shared-agent"]["agent_type"] == "normal"


# -- the generic-default refusals -----------------------------------------


@pytest.mark.parametrize("action", ["set_default", "set_user_default"])
def test_a_coding_agent_cannot_become_a_generic_default(web, action):
    """Neither the tenant default nor a member's own default may be coding."""
    before = _json(web.get("/api/agents", token=web.login("root")))

    response = web.post(
        "/api/agents",
        {"action": action, "id": "erp-coder"},
        token=web.login("root"),
    )

    assert _status(response) == 400
    assert _json(response)["code"] == "coding_web_only"
    # The existing normal default is untouched, not merely reported as unchanged.
    after = _json(web.get("/api/agents", token=web.login("root")))
    assert after["default_agent_id"] == before["default_agent_id"]


# -- the forged normal-execution refusals ---------------------------------


def test_a_forged_normal_send_to_a_coding_agent_is_refused(web):
    """The composer's own filter is not the control; the server is."""
    web.role("erp-coder-role", ["chat.use", "agent.use"],
             grants=[("agent", "agent:erp-coder", "use")])
    web.member("alice", ["erp-coder-role"])

    response = web.post(
        "/message",
        {"session_id": "forged-1", "agent_id": "erp-coder",
         "message": "hello", "stream": True},
        token=web.login("alice"),
    )

    assert _status(response) == 400
    body = _json(response)
    assert body["code"] == "coding_web_only"
    # Refused before the claim: a coding Agent must not gain a platform
    # conversation row it would never be able to fill.
    from agent.memory import get_conversation_store
    from agent.registry import get_agent_registry

    workspace = get_agent_registry().get("erp-coder", require_enabled=False).workspace
    store = get_conversation_store(workspace)
    with store._lock:
        con = store._connect()
        try:
            row = con.execute(
                "SELECT session_id FROM sessions WHERE session_id=?",
                ("forged-1",),
            ).fetchone()
        finally:
            con.close()
    assert row is None


def test_an_unauthorized_caller_gets_the_ordinary_permission_answer(web):
    """A coding Agent must not turn a 403 into a type-specific hint.

    The caller here may see the Agent but holds no `agent.use` grant for it, so
    the answer has to be the same shape it is for a normal Agent — and no OpenCode
    work may be attempted on the way.
    """
    web.role("viewer", ["chat.use"])
    web.member("bob", ["viewer"])
    response = web.post(
        "/message",
        {"session_id": "forged-2", "agent_id": "erp-coder",
         "message": "hello", "stream": True},
        token=web.login("bob"),
    )

    assert _status(response) == 403
    assert _json(response)["code"] != "coding_web_only"


def test_a_coding_agent_cannot_join_a_team(web):
    """A team turn runs every member through the normal runtime."""
    response = web.post(
        "/api/sessions/team-check/settings",
        {"agent": "shared-agent", "members": ["shared-agent", "erp-coder"]},
        token=web.login("root"),
    )

    assert _status(response) == 400
    assert _json(response)["code"] == "coding_web_only"
