# encoding:utf-8
"""Scope, origin and runtime tests for the P2 session-context endpoints.

The two endpoints answer "how full is this session's context, and compact it":
a question whose answer is only meaningful for the caller who owns the session.
These tests therefore drive the **real WSGI app** (``tests/conftest.py``'s
``web_app`` fixture over ``build_web_app()``) rather than a patched ``web``
module, so the route policy, the database identity context, the durable session
row and the per-Agent use grant all have to agree before a handler runs.

The phase's route registration is still owned centrally (``implementation.md``
§4 / design D3 place both paths ahead of the ``/api/sessions/(.*)`` wildcard), so
each test registers the two routes itself through the test-only
``open_capability_actions`` helper and inserts them *before* the wildcard, which
is the placement production will use. Sessions are seeded through the bound
``ConversationStore`` under an ``identity_scope`` — never by faking the
handler's authorization result — because the durable owner row is exactly what
the new check reads.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

from agent.memory import get_conversation_store
from agent.protocol.cancel import get_cancel_registry
from common.runtime_identity import RuntimeIdentity, use_identity
from tests._helpers import IdentityStack, open_capability_actions

#: One consumer per slice (see ``auth.capability_matrix``): the actions the two
#: endpoints are gated by. The routes themselves are registered centrally in
#: ``channel/web/route_registry.py`` ahead of the session wildcard (design D3);
#: only these actions stay closed until the batch's acceptance evidence exists.
_ACTIONS = {
    "session_context_usage": {"usage": "read"},
    "session_context_compact": {"compact": "execute"},
}

#: The permissions a member needs to reach both endpoints: ``history.read`` for
#: the read, plus ``chat.use``/``agent.use`` for compaction.
_MEMBER_PERMISSIONS = ["chat.use", "agent.use", "agent.read", "history.read"]

#: The turn count the handler must pass down regardless of what the client asks.
_SERVER_KEEP_RECENT_TURNS = 2


@contextmanager
def _context_open():
    """Open the two capability actions for the duration of one test.

    The routes are registered centrally (``channel/web/route_registry.py``), so
    the only thing a test has to do is open the two actions: while closed the
    derived policy is ``closed`` and the HTTP gate answers 503 before any
    handler runs. Everything else goes through the real app.
    """
    with open_capability_actions(_ACTIONS):
        yield


@contextmanager
def _roster(app):
    """Point the process-wide Agent bridge at this harness's roster.

    ``AgentBridge`` caches the registry it was constructed with and the
    ``Bridge`` singleton outlives one harness's ``conf`` patch, so without this
    a handler's ``peek_agent`` would resolve the test's Agent ids against a
    previous test's (now deleted) workspace. The previous registry is put back
    on the way out, and any live instance the test installed is dropped: the
    singleton's instance map would otherwise leak a stub into the next test that
    happens to reuse an Agent + session id.
    """
    from agent.registry import get_agent_registry, set_agent_registry
    from bridge.bridge import Bridge

    bridge = Bridge().get_agent_bridge()
    previous = bridge.agent_registry
    with bridge._agents_lock:
        before = set(bridge._agent_instances)
    set_agent_registry(None)
    bridge.agent_registry = get_agent_registry()
    try:
        yield bridge
    finally:
        with bridge._agents_lock:
            for key in set(bridge._agent_instances) - before:
                bridge._agent_instances.pop(key, None)
        bridge.agent_registry = previous
        set_agent_registry(None)


def _status(response) -> int:
    """The HTTP status as an int (web.py reports "200 OK" and "403" shapes)."""
    raw = str(getattr(response, "status", "") or "")
    return int(raw.split(" ", 1)[0]) if raw[:3].isdigit() else 0


def _member(app, username, agents):
    """A member who may chat and use exactly *agents* (with history read)."""
    role = app.role(
        f"{username}-role", _MEMBER_PERMISSIONS,
        grants=[("agent", f"agent:{agent_id}", "use") for agent_id in agents])
    return app.member(username, [role["code"]])


def _seed(app, agent_id, session_id, owner_id, *, channel_type="web"):
    """Write a real session row through the Agent's bound ``ConversationStore``.

    ``get_conversation_store`` resolves the *storage* Agent key from the
    workspace (``''`` for the default Agent), which is the key the endpoints
    match on — seeding through the same handle is what makes the test read the
    row production would write.
    """
    store = get_conversation_store(app.agent_workspace(agent_id))
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    ]
    identity = RuntimeIdentity(
        agent_id=agent_id, user_id=owner_id, tenant_id=app.tenant_id)
    with use_identity(identity):
        # A web session carrying a durable owner row is the shape the endpoints
        # require; a machine session without one is refused (see below).
        assert store.append_messages(session_id, messages,
                                     channel_type=channel_type)
    return store


def _foreign_agent(app, agent_id="globex-agent"):
    """An Agent bound to a *different* tenant, for the cross-tenant refusal."""
    other = app.service.create_tenant(
        actor_user_id=app.admin_id, code="globex", name="Globex",
        admin_username="globex-root", admin_display="Globex Root",
        admin_password=IdentityStack.ROOT_PASSWORD,
        recent_password=IdentityStack.ROOT_PASSWORD,
        shared_root=os.path.join(app.root, "tenants", "globex"),
    )["id"]
    app.service.bind_agent(tenant_id=other, agent_id=agent_id)
    return other


class _StubAgent:
    """A live-instance stand-in exposing the two runtime methods the two
    endpoints call. It is injected into the bridge's instance map so the
    handler's real ``peek_agent`` lookup (and the real cancel-key check) run."""

    def __init__(self, result=None):
        self.result = result or {
            "ok": True, "reason": "compacted", "compacted_turns": 2,
            "before": 6, "after": 4,
        }
        self.calls = []

    def get_context_usage(self):
        return {
            "available": True, "estimated": True, "model": "stub",
            "window": 1000, "limit": 1000, "used": 10, "messages": 4,
            "breakdown": {"system": 1, "tools": 2, "history": 7, "free": 990},
        }

    def compact_context(self, keep_recent_turns=2):
        self.calls.append(keep_recent_turns)
        return dict(self.result)


def _install_live(bridge, agent_id, session_id, stub):
    """Put *stub* where ``peek_agent`` looks for this Agent + session."""
    key = bridge._runtime_key(bridge._resolve_agent_id(agent_id), session_id)
    with bridge._agents_lock:
        bridge._agent_instances[key] = stub
    return key


# ---------------------------------------------------------------------------
# GET /api/sessions/{sid}/context_usage
# ---------------------------------------------------------------------------

def test_owner_reads_available_false_without_a_live_instance(web_app):
    """An owned session with no runtime is a successful, not-yet-available read.

    The endpoint must never build the Agent (``get_agent``) just to answer a
    hover, so the absence of a live instance is data (``available=false``), not
    an error.
    """
    with _context_open():
        app = web_app("usage-no-instance")
        app.add_agent("agent-a")
        _member(app, "alice", agents=("agent-a",))
        _seed(app, "agent-a", "sess-1", app.user_id("alice"))
        token = app.login("alice")

        with _roster(app):
            response = app.get(
                "/api/sessions/sess-1/context_usage?agent_id=agent-a",
                token=token)

        assert _status(response) == 200, response.data[:200]
        body = app.json(response)
        assert body["status"] == "success"
        assert body["available"] is False


def test_owner_reads_the_live_context_usage(web_app):
    """With a live instance the real usage payload is returned, +status."""
    with _context_open():
        app = web_app("usage-live")
        app.add_agent("agent-a")
        _member(app, "alice", agents=("agent-a",))
        _seed(app, "agent-a", "sess-1", app.user_id("alice"))
        token = app.login("alice")

        with _roster(app) as bridge:
            _install_live(bridge, "agent-a", "sess-1", _StubAgent())
            response = app.get(
                "/api/sessions/sess-1/context_usage?agent_id=agent-a",
                token=token)

        assert _status(response) == 200, response.data[:200]
        body = app.json(response)
        assert body["status"] == "success"
        assert body["available"] is True
        assert body["used"] == 10
        assert body["limit"] == 1000
        assert body["window"] == 1000
        assert body["messages"] == 4
        assert body["breakdown"]["free"] == 990


def test_an_omitted_agent_id_resolves_the_tenant_default(web_app):
    """``agent_id`` is optional: the tenant default is addressed instead."""
    with _context_open():
        app = web_app("usage-default-agent")
        app.add_agent("agent-a")
        _member(app, "alice", agents=("agent-a",))
        _seed(app, "agent-a", "sess-1", app.user_id("alice"))
        token = app.login("alice")

        with _roster(app):
            response = app.get("/api/sessions/sess-1/context_usage", token=token)

        assert _status(response) == 200, response.data[:200]
        assert app.json(response)["available"] is False


def test_reading_another_members_session_is_hidden(web_app):
    """A non-owner gets the same 404 as a missing session (no existence leak)."""
    with _context_open():
        app = web_app("usage-non-owner")
        app.add_agent("agent-a")
        _member(app, "alice", agents=("agent-a",))
        _member(app, "bob", agents=("agent-a",))
        _seed(app, "agent-a", "sess-1", app.user_id("alice"))
        _seed(app, "agent-a", "bob-sess", app.user_id("bob"))
        bob_token = app.login("bob")

        with _roster(app):
            response = app.get(
                "/api/sessions/sess-1/context_usage?agent_id=agent-a",
                token=bob_token)

        assert _status(response) == 404
        body = app.json(response)
        assert body["status"] == "error"
        assert body["code"] == "session_not_found"


def test_an_agent_bound_to_another_tenant_is_refused(web_app):
    """A cross-tenant Agent can never satisfy the binding check."""
    with _context_open():
        app = web_app("usage-cross-tenant")
        app.add_agent("agent-a")
        _member(app, "alice", agents=("agent-a",))
        _seed(app, "agent-a", "sess-1", app.user_id("alice"))
        _foreign_agent(app)
        token = app.login("alice")

        with _roster(app):
            response = app.get(
                "/api/sessions/sess-1/context_usage?agent_id=globex-agent",
                token=token)

        assert _status(response) == 404
        # ``_require_tenant_agent_binding`` answers the generic agent-not-found
        # payload here (the Agent is not the caller's tenant's at all).
        assert app.json(response)["status"] == "error"


def test_same_named_sessions_are_scoped_to_the_owning_agent(web_app):
    """The owner-scope regression: two Agents, one session id, two owners.

    An id-only probe would either hide the caller's own row (the other Agent's
    same-named row matched first) or accept a row the caller does not own. The
    check has to match the store's storage Agent key *and* the owner.
    """
    with _context_open():
        app = web_app("usage-same-name")
        app.add_agent("agent-a", "team-agent")
        _member(app, "alice", agents=("agent-a", "team-agent"))
        _member(app, "bob", agents=("agent-a", "team-agent"))
        alice, bob = app.user_id("alice"), app.user_id("bob")
        # The same session id exists on both Agents, owned by different members.
        _seed(app, "agent-a", "shared-name", alice)
        _seed(app, "team-agent", "shared-name", bob)
        alice_token, bob_token = app.login("alice"), app.login("bob")

        with _roster(app):
            alice_own = app.get(
                "/api/sessions/shared-name/context_usage?agent_id=agent-a",
                token=alice_token)
            bob_own = app.get(
                "/api/sessions/shared-name/context_usage?agent_id=team-agent",
                token=bob_token)
            # Each owner addressing the other Agent's same-named row is hidden.
            alice_other = app.get(
                "/api/sessions/shared-name/context_usage?agent_id=team-agent",
                token=alice_token)
            bob_other = app.get(
                "/api/sessions/shared-name/context_usage?agent_id=agent-a",
                token=bob_token)

        assert _status(alice_own) == 200, alice_own.data[:200]
        assert app.json(alice_own)["available"] is False
        assert _status(bob_own) == 200, bob_own.data[:200]
        assert _status(alice_other) == 404
        assert _status(bob_other) == 404


# ---------------------------------------------------------------------------
# POST /api/sessions/{sid}/compact_context
# ---------------------------------------------------------------------------

def test_compaction_without_a_live_instance_is_a_noop(web_app):
    """No runtime is still HTTP 200 with ``no_live_context`` (not an error)."""
    with _context_open():
        app = web_app("compact-no-instance")
        app.add_agent("agent-a")
        _member(app, "alice", agents=("agent-a",))
        _seed(app, "agent-a", "sess-1", app.user_id("alice"))
        token = app.login("alice")

        with _roster(app):
            response = app.post(
                "/api/sessions/sess-1/compact_context",
                {"agent_id": "agent-a"}, token=token)

        assert _status(response) == 200, response.data[:200]
        body = app.json(response)
        assert body["ok"] is False
        assert body["available"] is False
        assert body["reason"] == "no_live_context"


def test_compaction_compacts_a_live_instance_and_ignores_client_tuning(web_app):
    """The handler compacts with its own turn count, not the client's.

    ``keep_recent_turns`` is a server constant (design D3): a client-supplied
    value must not be able to change the history split.
    """
    with _context_open():
        app = web_app("compact-live")
        app.add_agent("agent-a")
        _member(app, "alice", agents=("agent-a",))
        _seed(app, "agent-a", "sess-1", app.user_id("alice"))
        token = app.login("alice")
        stub = _StubAgent()

        with _roster(app) as bridge:
            _install_live(bridge, "agent-a", "sess-1", stub)
            response = app.post(
                "/api/sessions/sess-1/compact_context?agent_id=agent-a",
                {"agent_id": "agent-a", "keep_recent_turns": 99},
                token=token)

        assert _status(response) == 200, response.data[:200]
        body = app.json(response)
        assert body["status"] == "success"
        assert body["ok"] is True
        assert body["available"] is True
        assert body["reason"] == "compacted"
        assert body["compacted_turns"] == 2
        assert body["usage"]["available"] is True
        assert stub.calls == [_SERVER_KEEP_RECENT_TURNS]


def test_a_context_changed_race_maps_to_409(web_app):
    """The abandoned stale commit is surfaced as a retryable 409."""
    with _context_open():
        app = web_app("compact-race")
        app.add_agent("agent-a")
        _member(app, "alice", agents=("agent-a",))
        _seed(app, "agent-a", "sess-1", app.user_id("alice"))
        token = app.login("alice")
        stub = _StubAgent(result={
            "ok": False, "reason": "context_changed",
            "compacted_turns": 0, "before": 6, "after": 6,
        })

        with _roster(app) as bridge:
            _install_live(bridge, "agent-a", "sess-1", stub)
            response = app.post(
                "/api/sessions/sess-1/compact_context",
                {"agent_id": "agent-a"}, token=token)

        assert _status(response) == 409, response.data[:200]
        assert app.json(response)["code"] == "context_changed"


def test_a_busy_session_is_refused_with_409(web_app):
    """A generation in flight for this Agent+session blocks compaction."""
    with _context_open():
        app = web_app("compact-busy")
        app.add_agent("agent-a")
        _member(app, "alice", agents=("agent-a",))
        _seed(app, "agent-a", "sess-1", app.user_id("alice"))
        token = app.login("alice")

        with _roster(app) as bridge:
            _install_live(bridge, "agent-a", "sess-1", _StubAgent())
            scoped = bridge.scoped_session_key("sess-1", "agent-a")
            registry = get_cancel_registry()
            # The real registry is keyed by the Agent-scoped session key.
            registry.register("req-busy", session_id=scoped)
            try:
                response = app.post(
                    "/api/sessions/sess-1/compact_context",
                    {"agent_id": "agent-a"}, token=token)
            finally:
                registry.unregister("req-busy")

        assert _status(response) == 409, response.data[:200]
        assert app.json(response)["code"] == "session_busy"


def test_an_illegal_origin_is_refused_on_the_write(web_app):
    """The write runs the shared origin/CSRF gate before any store work."""
    with _context_open():
        app = web_app("compact-origin")
        app.add_agent("agent-a")
        _member(app, "alice", agents=("agent-a",))
        _seed(app, "agent-a", "sess-1", app.user_id("alice"))
        token = app.login("alice")
        stub = _StubAgent()

        with _roster(app) as bridge:
            _install_live(bridge, "agent-a", "sess-1", stub)
            response = app.post(
                "/api/sessions/sess-1/compact_context",
                {"agent_id": "agent-a"}, token=token,
                headers={"Origin": "http://evil.example"})

        assert _status(response) == 403, response.data[:200]
        assert app.json(response)["code"] == "csrf_failed"
        assert stub.calls == [], "a refused origin must not compact"


def test_a_query_and_body_agent_id_must_agree(web_app):
    """Two disagreeing targets are a 400, never a silent winner."""
    with _context_open():
        app = web_app("compact-mismatch")
        app.add_agent("agent-a", "team-agent")
        _member(app, "alice", agents=("agent-a", "team-agent"))
        _seed(app, "agent-a", "sess-1", app.user_id("alice"))
        token = app.login("alice")

        with _roster(app):
            response = app.post(
                "/api/sessions/sess-1/compact_context?agent_id=agent-a",
                {"agent_id": "team-agent"}, token=token)

        assert _status(response) == 400, response.data[:200]
        assert app.json(response)["code"] == "invalid_request"


def test_a_machine_session_without_an_owner_row_is_not_addressable(web_app):
    """The durable owner row is required evidence, not an assumption.

    A session that exists structurally but was never stored for this caller
    (here: a non-web channel row) cannot be addressed through the context
    endpoints, even though ``_require_owned_session`` tolerates a missing row.
    """
    with _context_open():
        app = web_app("compact-no-owner-row")
        app.add_agent("agent-a")
        _member(app, "alice", agents=("agent-a",))
        # Recorded as a feishu session: the row exists but is not the caller's
        # web session, so the exact match must fail.
        _seed(app, "agent-a", "feishu-sess", app.user_id("alice"),
              channel_type="feishu")
        token = app.login("alice")

        with _roster(app):
            response = app.get(
                "/api/sessions/feishu-sess/context_usage?agent_id=agent-a",
                token=token)

        assert _status(response) == 404
        assert app.json(response)["code"] == "session_not_found"


# ---------------------------------------------------------------------------
# Service-failure mapping (503, not 500)
# ---------------------------------------------------------------------------

def test_a_store_fault_maps_to_503(web_app, monkeypatch):
    """A storage fault answers 503 ``store_unavailable``.

    The scope resolution is stubbed only to reach the mapping: the authorization
    result is not what is under test here, the HTTP class of a storage fault is.
    """
    import sqlite3

    from channel.web.fork import authorization

    def _fault(ctx, session_id, agent_id):
        raise sqlite3.OperationalError("database is locked")

    with _context_open():
        app = web_app("usage-store-fault")
        app.add_agent("agent-a")
        _member(app, "alice", agents=("agent-a",))
        _seed(app, "agent-a", "sess-1", app.user_id("alice"))
        token = app.login("alice")
        monkeypatch.setattr(authorization, "_owned_context_target", _fault)

        response = app.get(
            "/api/sessions/sess-1/context_usage?agent_id=agent-a", token=token)

    assert _status(response) == 503, response.data[:200]
    assert app.json(response)["code"] == "store_unavailable"
