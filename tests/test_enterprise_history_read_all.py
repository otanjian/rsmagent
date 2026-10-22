# encoding:utf-8
"""Cross-owner history read authorization (enterprise interview management scene).

Covers tasks 1.2-1.4 and the backend half of 2.1: the registered permission and
Agent action, the strict grant lookup (no platform-all substitute), the read
gate, and the owner-scoped store query.
"""

import pathlib
import tempfile

import pytest

from auth.policy import (
    MEMBER_DEFAULT_PERMISSIONS,
    PERMISSION_CATALOG,
    PERMISSION_METADATA,
    RESOURCE_ACTIONS,
    TENANT_ADMIN_DEFAULT_PERMISSIONS,
)
from auth.service import IdentityService
from agent.memory.conversation_store import ConversationStore


# --------------------------------------------------------------------------
# Policy registration
# --------------------------------------------------------------------------

def test_permission_and_action_are_registered_without_auto_granting():
    assert "history.read_all" in PERMISSION_CATALOG
    meta = PERMISSION_METADATA["history.read_all"]
    assert meta["group"] == "会话"
    assert meta["scope"] == "tenant"
    assert meta["assignable"] is True
    assert "history_read_all" in RESOURCE_ACTIONS["agent"]

    # The new permission/action is explicit: neither built-in set gains it, and
    # no action is silently derived from the existing read/use grants.
    assert "history.read_all" not in MEMBER_DEFAULT_PERMISSIONS
    assert "history.read_all" not in TENANT_ADMIN_DEFAULT_PERMISSIONS
    assert RESOURCE_ACTIONS["agent"] == (
        "read", "use", "edit", "enable", "history_read_all")


# --------------------------------------------------------------------------
# Service: strict grant lookup and minimal identity projection
# --------------------------------------------------------------------------

def _seed_service():
    svc = IdentityService(pathlib.Path(tempfile.mkdtemp()) / "identity.db")
    svc.bootstrap(
        tenant_code="acme", tenant_name="Acme Corp",
        admin_username="root", admin_display="Root",
        admin_password="Str0ngAdminPass", shared_root="/s/acme", allow_weak=True)
    root = [u for u in svc.list_platform_users() if u["username"] == "root"][0]
    tenant = svc.list_tenants()[0]
    svc.bind_agent(tenant_id=tenant["id"], agent_id="shared",
                   origin="admin_created")
    return svc, root, tenant


def test_explicit_grant_is_required_for_platform_all_and_tenant_admin():
    svc, root, tenant = _seed_service()

    # A platform admin is NOT a substitute for the resource grant.
    assert svc.has_explicit_resource_grant(
        root["id"], tenant["id"], "agent", "agent:shared", "history_read_all") is False
    assert svc.explicit_resource_ids(
        root["id"], tenant["id"], "agent", "history_read_all") == set()

    role = svc.create_role(
        root["id"], tenant["id"], "interview_reader", "调研管理者",
        ["history.read", "history.read_all"],
        resource_grants=[{"resource_kind": "agent", "resource_id": "agent:shared",
                          "action": "history_read_all"}])
    member = svc.create_member(
        actor_user_id=root["id"], tenant_id=tenant["id"], operation="create-new",
        username="reader", display_name="Reader",
        temporary_password="TmpPass123!", roles=[role["code"]])
    member_id = member["user_id"]

    assert svc.has_explicit_resource_grant(
        member_id, tenant["id"], "agent", "agent:shared", "history_read_all") is True
    assert svc.explicit_resource_ids(
        member_id, tenant["id"], "agent", "history_read_all") == {"agent:shared"}
    # Only the granted triple matches; another action on the same Agent does not.
    assert svc.has_explicit_resource_grant(
        member_id, tenant["id"], "agent", "agent:shared", "read") is False

    # The member directory is not required to resolve the creator's name.
    assert svc.member_display_names(tenant["id"], [member_id]) == {member_id: "Reader"}
    assert svc.member_display_names(tenant["id"], [root["id"]]) == {root["id"]: "Root"}


# --------------------------------------------------------------------------
# Authorization gate
# --------------------------------------------------------------------------

class _FakeService:
    def __init__(self, grant=True, private=False):
        self.grant = grant
        self.private = private

    def get_agent_binding(self, agent_id):
        return {"agent_id": agent_id, "tenant_id": "t1",
                "private_owner_user_id": "owner" if self.private else None}

    def has_explicit_resource_grant(self, user_id, tenant_id, kind, resource_id, action):
        return self.grant

    def explicit_resource_ids(self, *args, **kwargs):
        return {"agent:shared"}


def _ctx(**overrides):
    from types import SimpleNamespace

    values = dict(user_id="u1", tenant_id="t1",
                  permissions={"history.read", "history.read_all"},
                  is_platform_admin=False, is_tenant_admin=False)
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def gate(monkeypatch):
    import auth.service
    import channel.web.fork.handlers.sessions as sessions
    import web

    # ``web.HTTPError`` writes its headers through the request context; give the
    # unit test the one attribute it needs instead of standing up an app.
    web.ctx.headers = []

    def install(svc):
        monkeypatch.setattr(auth.service, "get_identity_service", lambda: svc)
        monkeypatch.setattr(sessions, "_agent_is_coding", lambda agent_id: False)

    return install


def test_gate_requires_both_permissions_and_the_target_grant(gate, monkeypatch):
    import web
    from auth.service import get_identity_service  # noqa: F401
    from channel.web.fork.authorization import _require_history_read_all

    gate(_FakeService(grant=True))
    assert _require_history_read_all(_ctx(), "shared") == "shared"

    # Missing either functional permission refuses.
    for perms in ({"history.read"}, {"history.read_all"}, set()):
        with pytest.raises(web.HTTPError):
            _require_history_read_all(_ctx(permissions=perms), "shared")

    # Missing the explicit target grant refuses, even for a tenant admin.
    gate(_FakeService(grant=False))
    with pytest.raises(web.HTTPError):
        _require_history_read_all(_ctx(is_tenant_admin=True), "shared")


def test_platform_all_does_not_replace_the_grant(gate):
    import web
    from channel.web.fork.authorization import _require_history_read_all

    gate(_FakeService(grant=False))
    with pytest.raises(web.HTTPError):
        _require_history_read_all(_ctx(permissions=set(), is_platform_admin=True), "shared")

    gate(_FakeService(grant=True))
    assert _require_history_read_all(
        _ctx(permissions=set(), is_platform_admin=True), "shared") == "shared"


def test_cross_tenant_target_is_refused(gate, monkeypatch):
    import web
    from channel.web.fork.authorization import _require_history_read_all

    class Foreign(_FakeService):
        def get_agent_binding(self, agent_id):
            return {"agent_id": agent_id, "tenant_id": "t2",
                    "private_owner_user_id": None}

    gate(Foreign(grant=True))
    with pytest.raises(web.HTTPError):
        _require_history_read_all(_ctx(tenant_id="t1"), "shared")


def test_private_target_is_refused(gate):
    import web
    from channel.web.fork.authorization import _require_history_read_all

    gate(_FakeService(grant=True, private=True))
    with pytest.raises(web.HTTPError):
        _require_history_read_all(_ctx(), "shared")


def test_legacy_context_is_a_noop(gate):
    from channel.web.fork.authorization import _require_history_read_all

    gate(_FakeService(grant=False))
    assert _require_history_read_all(None, "shared") == "shared"


# --------------------------------------------------------------------------
# Store: owner scope is explicit, never "user_id=None means all"
# --------------------------------------------------------------------------

@pytest.fixture
def store():
    db = pathlib.Path(tempfile.mkdtemp()) / "history.db"
    store = ConversationStore(db, "shared")
    conn = store._connect()
    rows = [
        ("alice-s", "alice"), ("bob-s", "bob"),
    ]
    with conn:
        for session_id, owner in rows:
            conn.execute(
                "INSERT INTO sessions (session_id, channel_type, title, created_at,"
                " last_active, msg_count, pinned, archived, agent_id, owner, tenant_id)"
                " VALUES (?, 'web', ?, 1, 2, 1, 0, 0, 'shared', ?, 't1')",
                (session_id, session_id, owner))
            conn.execute(
                "INSERT INTO messages (session_id, seq, role, content, created_at,"
                " agent_id, owner, tenant_id)"
                " VALUES (?, 1, 'user', ?, 1, 'shared', ?, 't1')",
                (session_id, f"hello from {owner}", owner))
    conn.close()
    return store


def test_list_sessions_all_owners_excludes_ownerless_rows(store):
    conn = store._connect()
    with conn:
        conn.execute(
            "INSERT INTO sessions (session_id, channel_type, title, created_at,"
            " last_active, msg_count, pinned, archived, agent_id, owner, tenant_id)"
            " VALUES ('legacy', 'web', 'legacy', 1, 2, 1, 0, 0, 'shared', '', 't1')")
    conn.close()
    ids = {s["session_id"] for s in
           store.list_sessions(channel_type="web", all_owners=True)["sessions"]}
    assert "legacy" not in ids


def test_list_sessions_all_owners_is_opt_in(store):
    own = store.list_sessions(channel_type="web", user_id="alice")
    assert [s["session_id"] for s in own["sessions"]] == ["alice-s"]
    assert own["sessions"][0]["owner"] == "alice"

    everything = store.list_sessions(channel_type="web", all_owners=True)
    assert {s["session_id"] for s in everything["sessions"]} == {"alice-s", "bob-s"}
    assert {s["owner"] for s in everything["sessions"]} == {"alice", "bob"}


def test_load_history_page_all_owners_is_opt_in(store):
    filtered = store.load_history_page("bob-s", user_id="alice")
    assert filtered["messages"] == []

    crossed = store.load_history_page("bob-s", all_owners=True)
    assert len(crossed["messages"]) == 1
    assert "bob" in crossed["messages"][0]["content"]
    assert store.get_session_owner("bob-s") == "bob"
