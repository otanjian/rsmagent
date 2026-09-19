# encoding:utf-8
"""The Agent's ``assign`` action.

The Agent may hand the user's own todo to a colleague, but only when the user
named that colleague in the current turn. Everything else — scanning a member
directory, inferring from history, picking for the user, acting on somebody
else's item, or reaching outside the tenant — is refused.

Note what is *not* here: ``recall`` and ``reject``. Returning work is the Web
console's job, so the tool refuses those verbs outright rather than quietly
supporting them.
"""

import json

import pytest

from agent.registry import AgentProfile, AgentRegistry
from agent.tools.todo.todo_tool import TodoTool
from auth.service import IdentityService
from channel.web import web_channel
from common.runtime_identity import RuntimeIdentity, use_identity

PASSWORD = "TodoTestPassword1!"


@pytest.fixture
def identities(tmp_path, monkeypatch):
    svc = IdentityService(str(tmp_path / "identity.db"))
    settings = {
        "identity_mode": "database",
        "identity_db_path": str(tmp_path / "identity.db"),
        "todo_enabled": True,
    }
    monkeypatch.setattr("config.conf", lambda: settings)
    monkeypatch.setattr(web_channel, "conf", lambda: settings)
    monkeypatch.setattr("config.get_data_root", lambda: str(tmp_path / "private"))

    workspace = str(tmp_path / "workspaces" / "alpha")
    alpha = svc.bootstrap(
        tenant_code="alpha", tenant_name="Alpha", admin_username="alpha",
        admin_display="Alpha", admin_password=PASSWORD, shared_root=workspace,
        allow_weak=True)
    svc.bind_agent(tenant_id=alpha["id"], agent_id="alpha")
    admin_login = svc.login("alpha", PASSWORD)
    svc.change_password(admin_login.token, PASSWORD, PASSWORD + "x")

    # Members of alpha: the colleagues a delegation may target.
    members = {}
    for name in ("bob", "carol"):
        svc.create_member(
            actor_user_id=admin_login.user_id, tenant_id=alpha["id"],
            operation="create-new", username=name, display_name=name.title(),
            temporary_password="MemTempPass1", roles=["member"])
        token = svc.login(name, "MemTempPass1").token
        svc.change_password(token, "MemTempPass1", PASSWORD)
        final = svc.login(name, PASSWORD)
        members[name] = (final, RuntimeIdentity(
            agent_id="alpha", user_id=final.user_id, tenant_id=alpha["id"],
            session_id="chat-" + name,
            web_auth_session_id=svc.verify_session(final.token)["session"]["id"],
        ))

    # A second tenant, so "somebody else's account" is a real thing to refuse.
    beta = svc.bootstrap(
        tenant_code="beta", tenant_name="Beta", admin_username="outsider",
        admin_display="Outsider", admin_password=PASSWORD,
        shared_root=str(tmp_path / "workspaces" / "beta"), allow_weak=True)
    beta_login = svc.login("outsider", PASSWORD)
    svc.change_password(beta_login.token, PASSWORD, PASSWORD + "x")

    registry = AgentRegistry(
        [AgentProfile(id="alpha", name="alpha", workspace=workspace)], "alpha")
    monkeypatch.setattr("agent.registry.get_agent_registry", lambda: registry)
    return svc, alpha["id"], beta["id"], members


def _bob(identities):
    return identities[3]["bob"]


def _user_id(svc, username):
    return [u for u in svc.list_platform_users() if u["username"] == username][0]["id"]


def test_assign_moves_the_item_and_is_attributed_to_the_agent(identities):
    svc, tenant_id, _, members = identities
    bob_login, bob = _bob(identities)

    with use_identity(bob):
        created = TodoTool().execute({"action": "create", "title": "Send the report"})
        assert created.status == "success", created.result
        assigned = TodoTool().execute({
            "action": "assign", "todo_id": created.result["todo_id"],
            "assignee": "carol",
        })
    assert assigned.status == "success", assigned.result
    assert assigned.result["assignee"] == "Carol"

    _, carol = members["carol"]
    with use_identity(carol):
        assert TodoTool().execute({"action": "list"}).result["total"] == 1
    with use_identity(bob):
        assert TodoTool().execute({"action": "list"}).result["total"] == 0
        assert TodoTool().execute({"action": "get",
                                   "todo_id": created.result["todo_id"]}).status == "success"

    # Recorded as an Agent acting for the user, not as the user.
    with use_identity(bob):
        tool = TodoTool()
        events = tool._service().events(created.result["todo_id"])["items"]
    assigned_events = [e for e in events if e["action"] == "assign"]
    assert len(assigned_events) == 1
    assert assigned_events[0]["operator_id"] == "alpha"
    assert assigned_events[0]["operator_kind"] == "agent"
    assert assigned_events[0]["changed"]["from_assignee"] == bob.user_id
    assert assigned_events[0]["changed"]["to_assignee"] == _user_id(svc, "carol")

    audit = svc._audit.query_tenant(tenant_id)
    delegation = [e for e in audit if e["action"] == "todo.assign"]
    assert delegation and delegation[0]["result"] == "success"
    assert delegation[0]["actor_username"] == "bob"


def test_target_must_be_named_and_is_never_chosen_for_the_user(identities):
    _, _, _, members = identities
    bob_login, bob = _bob(identities)

    with use_identity(bob):
        created = TodoTool().execute({"action": "create", "title": "Needs an owner"})
        todo_id = created.result["todo_id"]
        # No receiver at all.
        assert TodoTool().execute({"action": "assign", "todo_id": todo_id}).status == "error"
        # A blank one is still not a name.
        assert TodoTool().execute(
            {"action": "assign", "todo_id": todo_id, "assignee": "   "}).status == "error"
        # An unknown name is refused rather than resolved to the nearest match.
        assert TodoTool().execute(
            {"action": "assign", "todo_id": todo_id, "assignee": "nobody"}).status == "error"
        # And nothing moved.
        assert TodoTool().execute({"action": "list"}).result["total"] == 1


def test_a_name_from_another_tenant_is_refused_like_an_unknown_one(identities):
    _, _, _, members = identities
    bob_login, bob = _bob(identities)

    with use_identity(bob):
        created = TodoTool().execute({"action": "create", "title": "Tenant scoped"})
        todo_id = created.result["todo_id"]
        unknown = TodoTool().execute(
            {"action": "assign", "todo_id": todo_id, "assignee": "nobody"})
        foreign = TodoTool().execute(
            {"action": "assign", "todo_id": todo_id, "assignee": "outsider"})
        still_mine = TodoTool().execute({"action": "list"}).result["total"]

    # Indistinguishable answers: the tool must not become a membership oracle.
    assert unknown.status == foreign.status == "error"
    assert still_mine == 1


def test_somebody_elses_item_cannot_be_assigned(identities):
    svc, tenant_id, _, members = identities
    bob_login, bob = _bob(identities)
    _, carol = members["carol"]

    with use_identity(carol):
        item = TodoTool().execute({"action": "create", "title": "Carol's own"}).result

    with use_identity(bob):
        refused = TodoTool().execute(
            {"action": "assign", "todo_id": item["todo_id"], "assignee": "bob"})
    assert refused.status == "error"

    with use_identity(carol):
        assert TodoTool().execute({"action": "list"}).result["total"] == 1


def test_model_supplied_identity_is_not_authorization(identities):
    svc, tenant_id, beta_tenant, members = identities
    bob_login, bob = _bob(identities)

    with use_identity(bob):
        created = TodoTool().execute({"action": "create", "title": "Mine"}).result
        # Extra arguments naming another owner / tenant / member profile must be
        # ignored: identity comes from the runtime context, never the call.
        forged = TodoTool().execute({
            "action": "assign",
            "todo_id": created["todo_id"],
            "assignee": "carol",
            "owner_id": _user_id(svc, "outsider"),
            "tenant_id": beta_tenant,
            "user_id": _user_id(svc, "outsider"),
            "actor": "alpha",
        })
    assert forged.status == "success", forged.result
    # It landed in the acting tenant, under the acting user's item.
    assert forged.result["assignee"] == "Carol"
    assert forged.result["owner_id"] == bob.user_id


def test_recall_and_reject_are_not_tool_actions(identities):
    _, _, _, members = identities
    bob_login, bob = _bob(identities)

    with use_identity(bob):
        created = TodoTool().execute({"action": "create", "title": "Mine"}).result
        for verb in ("recall", "reject", "transfer"):
            result = TodoTool().execute({"action": verb, "todo_id": created["todo_id"]})
            assert result.status == "error", verb
        assert TodoTool().execute({"action": "list"}).result["total"] == 1


def test_without_todo_assign_the_action_is_refused(identities):
    svc, tenant_id, _, members = identities
    bob_login, bob = _bob(identities)
    _strip_permission(svc, tenant_id, "todo.assign")

    with use_identity(bob):
        created = TodoTool().execute({"action": "create", "title": "Mine"}).result
        refused = TodoTool().execute({
            "action": "assign", "todo_id": created["todo_id"], "assignee": "carol"})
        assert refused.status == "error"
        assert TodoTool().execute({"action": "list"}).result["total"] == 1


def _strip_permission(svc, tenant_id, permission):
    with svc._store.connect() as con:
        row = con.execute(
            "SELECT id, permissions_json FROM roles WHERE tenant_id=?"
            " AND code='member'", (tenant_id,)).fetchone()
        perms = [p for p in json.loads(row["permissions_json"] or "[]")
                 if p != permission]
        con.execute("UPDATE roles SET permissions_json=? WHERE id=?",
                    (json.dumps(perms), row["id"]))
        con.commit()


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
