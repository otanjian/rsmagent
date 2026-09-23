# encoding:utf-8
"""A team roster is validated as a whole before it is stored.

The console's picker keeps coding Agents out, but the picker is not the
control: a roster arrives as a request body, so the write path has to refuse an
owner or a member it cannot accept. The rule is *refusal, never repair* —
silently dropping an unusable id would leave the user believing their team was
saved while the conversation runs a different one.

These drive the real WSGI app because the claim is about what the HTTP boundary
does with a body: the tenant binding, the caller's ``agent.use`` grant, the
private-owner rule and the registry lookup are all applied inside ``_db_scope``,
and a handler-level test with stubbed guards would prove the branches exist
rather than that a caller reaches them.

The last two cases are the two halves of the "make 'saved' mean saved" rule:
a write that never reached disk answers a real error and leaves both the store
and the runtime as they were, while a legitimate cleanup of a stale roster
still succeeds.
"""

import json
from contextlib import contextmanager

import pytest

from tests._helpers import WebAppHarness

PROJECT_DIR = "/srv/checkouts/erp"


@pytest.fixture
def web(tmp_path):
    harness = WebAppHarness(tmp_path / "instance")
    harness.add_agent("shared-agent", "ops-agent")
    harness.add_coding_agent("erp-coder", PROJECT_DIR)
    harness.role("team-role", ["chat.use", "agent.use"], grants=[
        ("agent", "agent:shared-agent", "use"),
        ("agent", "agent:ops-agent", "use"),
    ])
    harness.member("alice", ["team-role"])
    yield harness
    harness.close()


def _json(response):
    return WebAppHarness.json(response)


def _status(response):
    return int(response.status.split()[0])


@contextmanager
def _as(web, username):
    """Run a helper as the tenant's identity, so state_dir resolves its root."""
    from common.runtime_identity import RuntimeIdentity, use_identity

    with use_identity(RuntimeIdentity(user_id=web.user_id(username),
                                     tenant_id=web.tenant_id)):
        yield


def _stored_members(web, session_id, agent_id):
    from agent.workspace import session_prefs

    with _as(web, "root"):
        return session_prefs.get_prefs(session_id, agent_id).get("members")


def _set_roster(web, session_id, body, username="alice"):
    return web.post(f"/api/sessions/{session_id}/settings", body,
                    token=web.login(username))


def _refusal(web, session_id, body, status, code=None, agent_id="shared-agent",
             message=None):
    """Assert the write was refused *and nothing was stored*.

    The second half is the point: a refusal that still wrote the acceptable
    part would pass a status-only assertion and still run the wrong team. An id
    that is unknown to the caller is refused by the tenant binding, which
    answers the ordinary 404 without naming what exists elsewhere (no ``code``).
    """
    response = _set_roster(web, session_id, body)
    assert _status(response) == status, response.data
    body_json = _json(response)
    if code:
        assert body_json["code"] == code, body_json
    if message:
        assert message in body_json["message"], body_json
    assert not _stored_members(web, session_id, agent_id), "a refused roster must not be stored"


def test_a_valid_roster_is_stored_and_read_back(web):
    """Non-vacuity: the same route the refusals use really does save a team."""
    response = _set_roster(web, "team-ok", {"agent_id": "shared-agent",
                                            "members": ["ops-agent"]})
    assert _status(response) == 200, response.data
    assert _json(response)["status"] == "success"
    assert _stored_members(web, "team-ok", "shared-agent") == ["ops-agent"]


def test_the_owner_is_stored_once_not_as_its_own_member(web):
    """The owner is the session's anchor; repeating it in members is normalized
    away rather than stored twice."""
    response = _set_roster(web, "team-owner", {"agent_id": "shared-agent",
                                               "members": ["shared-agent", "ops-agent"]})
    assert _status(response) == 200, response.data
    assert _stored_members(web, "team-owner", "shared-agent") == ["ops-agent"]


def test_an_unknown_member_is_refused_and_not_stored(web):
    """An id the caller cannot reach is refused by the tenant binding, which
    answers the ordinary 404 rather than naming what exists for someone else."""
    _refusal(web, "team-ghost", {"agent_id": "shared-agent", "members": ["ghost-agent"]},
             404, message="agent not found")


def test_a_member_missing_from_the_roster_is_refused_and_not_stored(web):
    """Bound to the tenant but gone from the registry: still a refusal, and now
    one that can name the field it refused."""
    web.bind_agent_to_tenant("ghost-agent", web.tenant_id)

    _refusal(web, "team-ghost-bound", {"agent_id": "shared-agent",
                                       "members": ["ghost-agent"]},
             404, "team_member_unknown")


def test_a_disabled_member_is_refused_and_not_stored(web):
    web.write_roster([
        {"id": "shared-agent", "name": "shared-agent"},
        {"id": "ops-agent", "name": "ops-agent", "enabled": False},
    ])

    _refusal(web, "team-off", {"agent_id": "shared-agent", "members": ["ops-agent"]},
             400, "team_member_disabled")


def test_a_member_from_another_tenant_is_refused_and_not_stored(web):
    other = web.stack.other_tenant(code="globex", agent="globex-agent")

    _refusal(web, "team-cross", {"agent_id": "shared-agent",
                                 "members": [other["agent_id"]]},
             404, message="agent not found")


def test_another_members_private_agent_is_refused_and_not_stored(web):
    bob = web.member("bob", ["team-role"])
    web.private_agent(bob, "bob-private")

    _refusal(web, "team-private", {"agent_id": "shared-agent",
                                   "members": ["bob-private"]},
             403, "forbidden")


def test_revoking_the_use_grant_refuses_the_whole_write(web):
    """A roster that saves has to be a roster whose members can take a turn."""
    roles = [r for r in web.service.list_roles(web.tenant_id) if r["code"] == "team-role"]
    web.revoke_grants(roles[0])

    _refusal(web, "team-revoked", {"agent_id": "shared-agent",
                                   "members": ["ops-agent"]},
             403, "forbidden")


def test_a_coding_agent_is_refused_as_a_member_and_not_stored(web):
    """"It has a type" is not a reason to skip the whole-roster rule."""
    _refusal(web, "team-coder-member", {"agent_id": "shared-agent",
                                        "members": ["ops-agent", "erp-coder"]},
             400, "coding_web_only")


def test_a_coding_agent_is_refused_as_the_owner_and_not_stored(web):
    """The owner position is not a loophole: it is the Agent that answers."""
    response = _set_roster(web, "team-coder-owner",
                           {"agent_id": "erp-coder", "members": ["ops-agent"]})
    assert _status(response) == 400, response.data
    assert _json(response)["code"] == "coding_web_only"
    assert not _stored_members(web, "team-coder-owner", "erp-coder")


def test_a_roster_that_is_coding_all_the_way_through_is_refused(web):
    """Every position is checked, so a body cannot smuggle a coding Agent in by
    filling the *other* slot correctly: with coding on both sides nothing is
    stored and the session keeps no team at all."""
    response = _set_roster(web, "team-all-coding",
                           {"agent_id": "erp-coder", "members": ["erp-coder", "ops-agent"]})
    assert _status(response) == 400, response.data
    assert _json(response)["code"] == "coding_web_only"
    assert not _stored_members(web, "team-all-coding", "erp-coder")


def test_one_normal_member_is_stored(web):
    """The "at least two" rule belongs to starting a *new* team in the picker.
    The write path's own rule is the legality of each object, so one legal
    member is a legal roster -- asserted so the coding refusals above cannot be
    explained away by a blanket "small rosters are refused"."""
    response = _set_roster(web, "team-single", {"agent_id": "shared-agent",
                                                "members": ["ops-agent"]})
    assert _status(response) == 200, response.data
    assert _stored_members(web, "team-single", "shared-agent") == ["ops-agent"]


# -- "saved" has to mean saved ----------------------------------------------


def test_a_failed_persist_answers_an_error_and_changes_nothing(web, monkeypatch):
    from agent.workspace import session_prefs

    assert _status(_set_roster(web, "team-durable", {
        "agent_id": "shared-agent", "members": ["ops-agent"]})) == 200

    retired = []
    monkeypatch.setattr("channel.web.fork.handlers.sessions._drop_team_runtimes",
                        lambda *a, **k: retired.append(a))

    def _explode(*args, **kwargs):
        raise session_prefs.SessionPrefsError("disk is full")

    monkeypatch.setattr(session_prefs, "_save", _explode)

    response = _set_roster(web, "team-durable", {"agent_id": "shared-agent",
                                                 "members": []})

    assert _status(response) == 500, response.data
    assert _json(response)["code"] == "session_prefs_write_failed"
    # The store still holds the roster the caller last *succeeded* with, and no
    # runtime was retired for a change that never happened.
    assert _stored_members(web, "team-durable", "shared-agent") == ["ops-agent"]
    assert retired == []


def test_a_stale_invalid_roster_can_be_cleaned_up_explicitly(web):
    """An old roster may name something today's rule refuses (an Agent that was
    deleted since). It must not be repaired behind the user's back, but the user
    can still submit the remaining legal members and have that stored."""
    from agent.workspace import session_prefs

    with _as(web, "root"):
        session_prefs.set_prefs("team-stale", "shared-agent", members=["ghost-agent"])
    assert _stored_members(web, "team-stale", "shared-agent") == ["ghost-agent"]

    # Re-submitting the stale roster is refused, and the old value survives.
    response = _set_roster(web, "team-stale", {"agent_id": "shared-agent",
                                               "members": ["ghost-agent"]})
    assert _status(response) == 404, response.data
    assert "agent not found" in _json(response)["message"]
    assert _stored_members(web, "team-stale", "shared-agent") == ["ghost-agent"], \
        "a refused write leaves the stored value alone rather than repairing it"

    # Submitting only the legal members is the explicit cleanup.
    response = _set_roster(web, "team-stale", {"agent_id": "shared-agent",
                                               "members": ["ops-agent"]})
    assert _status(response) == 200, response.data
    assert _stored_members(web, "team-stale", "shared-agent") == ["ops-agent"]


def test_the_last_member_can_be_removed_back_to_a_solo_conversation(web):
    """The two-member rule belongs to the *create* flow: an existing team can
    still be reduced, and clearing the roster falls back to the solo meaning."""
    assert _status(_set_roster(web, "team-shrink", {
        "agent_id": "shared-agent", "members": ["ops-agent"]})) == 200

    response = _set_roster(web, "team-shrink", {"agent_id": "shared-agent", "members": []})
    assert _status(response) == 200, response.data
    assert not _stored_members(web, "team-shrink", "shared-agent")


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
