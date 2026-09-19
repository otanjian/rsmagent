# encoding:utf-8
"""Delegation over the real HTTP boundary.

Builds real WSGI apps over private identity + todo databases, with two tenants
and several members, so the checks run through session resolution, the tenant
header and the route table rather than around them.

The security claim being pinned: the receiver picker is served under
``todo.assign`` and is *not* the member directory, and the delegator's right to
read an item does not become a right to process it.
"""

import json

import pytest
import web

from auth.service import IdentityService
from channel.web import todo_handlers
from channel.web.route_registry import ROUTES


@pytest.fixture
def api(tmp_path, monkeypatch):
    svc = IdentityService(str(tmp_path / "identity.db"))
    tenants = {}
    for code in ("alpha", "beta"):
        tenants[code] = svc.bootstrap(
            tenant_code=code, tenant_name=code, admin_username=code,
            admin_display=code, admin_password="TestTodoPassword1!",
            shared_root=str(tmp_path / "workspaces" / code), allow_weak=True,
        )["id"]

    tokens = {}

    def login(username, password="TestTodoPassword1!"):
        tokens[username] = svc.login(username, password).token

    login("alpha")
    login("beta")
    alpha_admin = [u for u in svc.list_platform_users()
                   if u["username"] == "alpha"][0]
    # Members of alpha: the point of the feature is that a plain member can hand
    # work to a colleague without holding any tenant-administration permission.
    for name in ("alice", "bob"):
        svc.create_member(
            actor_user_id=alpha_admin["id"],
            tenant_id=tenants["alpha"], operation="create-new", username=name,
            display_name=name.title(), temporary_password="MemTempPass1",
            roles=["member"])
        login(name, "MemTempPass1")
        svc.change_password(tokens[name], "MemTempPass1", "MemPassFinal1")
        login(name, "MemPassFinal1")

    monkeypatch.setattr("config.get_data_root", lambda: str(tmp_path / "private"))
    monkeypatch.setattr(todo_handlers, "_get_identity_service", lambda: svc)
    # The handler resolves the audit database the same way it resolves the
    # identity service in production; point both at this fixture's file so the
    # assertions read the audit events this app actually wrote.
    monkeypatch.setattr(todo_handlers, "_identity_db_path",
                        lambda: str(tmp_path / "identity.db"))
    monkeypatch.setattr(todo_handlers, "default_enabled", lambda: True)

    app = web.application((
        "/api/todos", "TodosHandler",
        "/api/todos/summary", "TodoSummaryHandler",
        "/api/todos/delegated", "TodoDelegatedHandler",
        "/api/todos/assignees", "TodoAssigneesHandler",
        "/api/todos/([^/]+)/events", "TodoEventsHandler",
        "/api/todos/([^/]+)/delegation", "TodoDelegationHandler",
        "/api/todos/([^/]+)", "TodoDetailHandler",
    ), vars(todo_handlers), autoreload=False)

    def request(path="/api/todos", *, tenant="alpha", user="alice",
                method="GET", data=None):
        headers = {}
        if tenant is not None:
            headers["X-Tenant-ID"] = tenants[tenant]
        if user is not None:
            headers["Authorization"] = "Bearer " + tokens[user]
        response = app.request(
            path, method=method, headers=headers,
            data=json.dumps(data) if data is not None else None)
        return int(response.status.split()[0]), json.loads(response.data)

    return request, svc, tenants, tokens


def _user_id(svc, username):
    return [u for u in svc.list_platform_users() if u["username"] == username][0]["id"]


def _create(request, title="t", **kw):
    status, body = request(method="POST", data={"title": title, **kw})
    assert status == 200, body
    return body["item"]


def _delegate(request, item_id, action, version, *, user="alice",
              tenant="alpha", **body):
    return request("/api/todos/" + item_id + "/delegation", method="POST",
                   user=user, tenant=tenant,
                   data={"action": action, "expected_version": version, **body})


class TestRouteRegistration:
    def test_the_delegation_routes_are_registered_before_the_wildcard(self):
        paths = [entry.pattern for entry in ROUTES]
        assert "/api/todos/delegated" in paths
        assert "/api/todos/assignees" in paths
        assert "/api/todos/(.*)/delegation" in paths

        # A literal path registered after the {id} wildcard would be read as a
        # todo id, so ordering is part of the contract, not an accident.
        assert paths.index("/api/todos/delegated") < paths.index("/api/todos/(.*)")
        assert paths.index("/api/todos/assignees") < paths.index("/api/todos/(.*)")
        assert paths.index("/api/todos/(.*)/delegation") < paths.index("/api/todos/(.*)")


class TestAssignOverHttp:
    def test_assign_moves_the_item_and_audits_it(self, api):
        request, svc, _, _ = api
        item = _create(request)

        status, body = _delegate(request, item["id"], "assign", item["version"],
                                 assignee="bob")

        assert status == 200, body
        assert body["item"]["assignee_id"] == _user_id(svc, "bob")
        assert body["item"]["owner_id"] == _user_id(svc, "alice")

        # Off my list and my badge, onto his.
        assert request()[1]["total"] == 0
        assert request("/api/todos/summary")[1]["open"] == 0
        assert request(user="bob")[1]["total"] == 1
        assert request("/api/todos/summary", user="bob")[1]["open"] == 1

        # And it landed in the identity audit store.
        events = svc._audit.query_tenant(api[2]["alpha"])
        delegation = [e for e in events if e["action"] == "todo.assign"]
        assert delegation and delegation[0]["result"] == "success"
        assert delegation[0]["actor_username"] == "alice"

    def test_the_delegated_view_is_disjoint_from_the_personal_list(self, api):
        request, _, _, _ = api
        mine = _create(request, title="mine")
        handed = _create(request, title="handed", create_key="k2")
        assert _delegate(request, handed["id"], "assign",
                         handed["version"], assignee="bob")[0] == 200

        assert [i["id"] for i in request()[1]["items"]] == [mine["id"]]
        delegated = request("/api/todos/delegated")[1]
        assert [i["id"] for i in delegated["items"]] == [handed["id"]]
        assert delegated["total"] == 1
        # The delegator sees read-only facts, not actions the server would refuse.
        view = delegated["items"][0]
        assert view["assignee_id"] == _user_id(api[1], "bob")
        assert view["can_recall"] is True
        assert view["can_edit"] is False
        assert view["can_operate"]["complete"] is False

    def test_delegator_cannot_process_the_item_for_the_receiver(self, api):
        request, _, _, _ = api
        item = _create(request)
        status, body = _delegate(request, item["id"], "assign",
                                 item["version"], assignee="bob")
        assert status == 200

        version = body["item"]["version"]
        # Same 404 as an unknown id: a distinct error would disclose that the
        # item still exists and is merely held by somebody else.
        assert request("/api/todos/" + item["id"], method="PATCH", data={
            "expected_version": version, "status": "completed"})[0] == 404
        assert request("/api/todos/" + item["id"], method="PATCH", data={
            "expected_version": version, "fields": {"title": "nope"}})[0] == 404
        # Reading it is still allowed — that is what makes recall possible.
        assert request("/api/todos/" + item["id"])[0] == 200

    def test_receiver_can_process_it(self, api):
        request, _, _, _ = api
        item = _create(request)
        status, body = _delegate(request, item["id"], "assign",
                                 item["version"], assignee="bob")
        assert status == 200

        assert request("/api/todos/" + item["id"], user="bob", method="PATCH",
                       data={"expected_version": body["item"]["version"],
                             "status": "completed"})[0] == 200
        assert request("/api/todos/summary", user="bob")[1]["open"] == 0

    def test_recall_returns_it_to_me(self, api):
        request, _, _, _ = api
        item = _create(request)
        _status, body = _delegate(request, item["id"], "assign",
                                  item["version"], assignee="bob")

        status, back = _delegate(request, item["id"], "recall",
                                 body["item"]["version"])
        assert status == 200, back
        assert back["item"]["assignee_id"] == back["item"]["owner_id"]
        assert request()[1]["total"] == 1
        assert request(user="bob")[1]["total"] == 0
        assert request("/api/todos/delegated")[1]["total"] == 0

    def test_reject_returns_it_to_the_delegator(self, api):
        request, _, _, _ = api
        item = _create(request)
        _status, body = _delegate(request, item["id"], "assign",
                                  item["version"], assignee="bob")

        status, returned = _delegate(request, item["id"], "reject",
                                     body["item"]["version"], user="bob")
        assert status == 200, returned
        assert returned["item"]["assignee_id"] == returned["item"]["owner_id"]
        assert request()[1]["total"] == 1


class TestDelegationBoundary:
    def test_cross_tenant_identifier_is_refused(self, api):
        request, _, _, _ = api
        item = _create(request)

        # "beta" is a real account, but not a member of this tenant, so the name
        # does not resolve — the same answer as a name that does not exist.
        status, body = _delegate(request, item["id"], "assign",
                                 item["version"], assignee="beta")

        assert status == 422 and body["code"] == "invalid_field"
        assert request("/api/todos/" + item["id"])[1]["item"]["assignee_id"] \
            == _user_id(api[1], "alice")

    def test_an_unknown_name_is_indistinguishable_from_a_foreign_one(self, api):
        request, _, _, _ = api
        item = _create(request)
        missing = _delegate(request, item["id"], "assign",
                            item["version"], assignee="nobody-at-all")
        foreign = _delegate(request, item["id"], "assign",
                            item["version"], assignee="beta")

        assert missing[0] == foreign[0] == 422
        assert missing[1]["code"] == foreign[1]["code"]
        assert missing[1]["field"] == foreign[1]["field"]

    def test_another_members_item_id_is_not_found(self, api):
        request, _, _, _ = api
        item = _create(request)
        # bob has no relationship with this item at all.
        assert request("/api/todos/" + item["id"], user="bob")[0] == 404
        assert _delegate(request, item["id"], "recall", item["version"],
                         user="bob")[0] == 404

    def test_unknown_action_is_a_validation_error(self, api):
        request, _, _, _ = api
        item = _create(request)
        status, body = _delegate(request, item["id"], "escalate", item["version"])
        assert status == 422 and body["field"] == "action"

    def test_missing_version_is_refused(self, api):
        request, _, _, _ = api
        item = _create(request)
        status, body = request("/api/todos/" + item["id"] + "/delegation",
                               method="POST",
                               data={"action": "assign", "assignee": "bob"})
        assert status == 422 and body["field"] == "expected_version"

    def test_deactivating_the_target_refuses_later_delegation(self, api):
        request, svc, tenants, _ = api
        item = _create(request)
        with svc._store.connect() as con:
            con.execute(
                "UPDATE memberships SET active=0 WHERE tenant_id=? AND user_id=?",
                (tenants["alpha"], _user_id(svc, "bob")))
            con.commit()

        status, body = _delegate(request, item["id"], "assign",
                                 item["version"], assignee="bob")
        assert status == 422, body


class TestAssigneeProjection:
    def test_a_member_with_assign_but_no_member_directory_can_still_pick(self, api):
        request, svc, tenants, _ = api
        # alice is a plain member: todo.assign from the member defaults, and
        # deliberately no tenant.members.read.
        with svc._store.connect() as con:
            row = con.execute(
                "SELECT permissions_json FROM roles WHERE tenant_id=?"
                " AND code='member'", (tenants["alpha"],)).fetchone()
        perms = set(json.loads(row["permissions_json"] or "[]"))
        assert "todo.assign" in perms
        assert "tenant.members.read" not in perms

        status, body = request("/api/todos/assignees")
        assert status == 200, body
        names = {row["username"] for row in body["items"]}
        assert {"alice", "bob"} <= names
        for row in body["items"]:
            assert set(row) == {"username", "display_name"}

    def test_without_todo_assign_the_projection_is_refused(self, api):
        request, svc, tenants, _ = api
        with svc._store.connect() as con:
            row = con.execute(
                "SELECT id, permissions_json FROM roles WHERE tenant_id=?"
                " AND code='member'", (tenants["alpha"],)).fetchone()
            perms = [p for p in json.loads(row["permissions_json"] or "[]")
                     if p != "todo.assign"]
            con.execute("UPDATE roles SET permissions_json=? WHERE id=?",
                        (json.dumps(perms), row["id"]))
            con.commit()

        status, body = request("/api/todos/assignees")
        assert status == 403 and body["code"] == "forbidden"

        # And the action itself is refused too: fail-closed, not merely hidden.
        item = _create(request)
        assert _delegate(request, item["id"], "assign",
                         item["version"], assignee="bob")[0] == 403

    def test_the_projection_does_not_require_the_todo_store(self, api, monkeypatch):
        request, _, _, root = api
        # Fails the store build outright; the picker must still work because it
        # reads identity data only.
        def unavailable():
            raise AssertionError("the picker must not build a todo service")
        monkeypatch.setattr(todo_handlers, "_build_service", unavailable)

        assert request("/api/todos/assignees")[0] == 200
