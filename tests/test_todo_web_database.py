"""Real database sessions exercising the todo HTTP/storage boundary."""

import json

import pytest
import web

from agent.registry import AgentProfile, AgentRegistry
from auth.service import IdentityService
from channel.web import todo_handlers


@pytest.fixture
def api(tmp_path, monkeypatch):
    svc = IdentityService(str(tmp_path / "identity.db"))
    tenants = []
    tokens = []
    for code in ("alpha", "beta"):
        tenants.append(svc.bootstrap(
            tenant_code=code, tenant_name=code, admin_username=code,
            admin_display=code, admin_password="TestTodoPassword1!",
            shared_root=str(tmp_path / "workspaces" / code), allow_weak=True,
        )["id"])
        tokens.append(svc.login(code, "TestTodoPassword1!").token)
    registry = AgentRegistry([
        AgentProfile(id="default", name="Default", workspace=str(tmp_path / "workspaces" / "alpha")),
    ], "default")
    monkeypatch.setattr("agent.registry.get_agent_registry", lambda: registry)
    monkeypatch.setattr("config.get_data_root", lambda: str(tmp_path / "private"))
    monkeypatch.setattr(todo_handlers, "_get_identity_service", lambda: svc)
    # Exercise the deployment default rather than forcing the feature on.
    monkeypatch.setattr("config.conf", lambda: {})
    app = web.application((
        "/api/todos", "TodosHandler",
        "/api/todos/summary", "TodoSummaryHandler",
        "/api/todos/([^/]+)/events", "TodoEventsHandler",
        "/api/todos/([^/]+)", "TodoDetailHandler",
    ), vars(todo_handlers), autoreload=False)

    def request(path="/api/todos", *, tenant=0, user=0, method="GET", data=None):
        headers = {}
        if tenant is not None:
            headers["X-Tenant-ID"] = tenants[tenant]
        if user is not None:
            headers["Authorization"] = "Bearer " + tokens[user]
        response = app.request(path, method=method, headers=headers,
                               data=json.dumps(data) if data is not None else None)
        return int(response.status.split()[0]), json.loads(response.data)

    return request, svc, tenants, tmp_path


def test_database_list_and_summary_use_private_store(api):
    request, _, tenants, root = api
    assert request() == (200, {"items": [], "total": 0, "page": 1,
                             "page_size": 0, "has_more": False, "status": "success"})
    status, summary = request("/api/todos/summary")
    assert status == 200 and summary["enabled"] and summary["bound"]
    assert summary["open"] == 0
    assert (root / "private" / "tenants" / tenants[0] / "todo" / "todos.db").is_file()
    assert not list((root / "workspaces").rglob("todos.db"))
    assert not (root / "private" / "todo" / "todos.db").exists()


def test_create_update_and_history_remain_owner_and_tenant_scoped(api):
    request, svc, tenants, root = api
    status, created = request(method="POST", data={"title": "Private task", "create_key": "test-key"})
    assert status == 200 and created["status"] == "success"
    item = created["item"]
    path = "/api/todos/" + item["id"]
    assert request()[1]["total"] == 1
    assert request("/api/todos/summary")[1]["open"] == 1
    status, updated = request(path, method="PATCH", data={
        "expected_version": item["version"], "status": "completed",
    })
    assert status == 200 and updated["item"]["status"] == "completed"
    assert request(path + "/events")[0] == 200
    assert request("/api/todos/summary")[1]["open"] == 0
    assert request(path, method="PATCH", data={
        "expected_version": item["version"], "status": "in_progress",
    })[0] == 409

    # A different tenant cannot list or retrieve the first tenant's item.
    assert request(user=1, tenant=1)[1]["total"] == 0
    assert request(path, user=1, tenant=1)[0] == 404
    assert (root / "private" / "tenants" / tenants[1] / "todo" / "todos.db").is_file()
    # Bind the second user as a member of the first tenant: still personal.
    admin = next(u for u in svc.list_platform_users() if u["username"] == "alpha")
    svc.create_member(actor_user_id=admin["id"], tenant_id=tenants[0],
                      operation="bind-existing", username="beta", display_name="Beta",
                      temporary_password="", roles=["member"])
    assert request("/api/todos?status=all", user=1)[1]["total"] == 0
    assert request(path, user=1)[0] == 404
    assert request("/api/todos/summary", user=1)[1]["open"] == 0


def test_database_identity_errors_have_real_http_status(api):
    request, svc, tenants, _ = api
    assert request(user=None)[0] == 401
    assert request(tenant=None)[0] == 401
    assert request(tenant=1)[0] == 403
    with svc._tx() as con:
        con.execute("UPDATE memberships SET active=0 WHERE tenant_id=?", (tenants[0],))
    assert request()[0] == 403


def test_password_change_restriction_precedes_storage(api):
    request, svc, _, root = api
    # A restricted account carries a finite temp-password deadline; without one it
    # is treated as an unusable credential (rejected as 401), never silently
    # usable. Here we give it a valid deadline so we exercise the 403 whitelist.
    import time
    with svc._tx() as con:
        con.execute(
            "UPDATE users SET must_change_password=1, temp_password_expires_at=? "
            "WHERE username='alpha'", (int(time.time()) + 3600,))
    status, body = request()
    assert status == 403 and body["code"] == "password_change_required"
    assert not (root / "private").exists()


def test_unavailable_private_directory_returns_503(api):
    request, svc, _, root = api
    with svc._tx() as con:
        con.execute("UPDATE tenants SET shared_root=? WHERE code='alpha'", (str(root / "private"),))
    for path in ("/api/todos", "/api/todos/summary"):
        status, body = request(path)
        assert status == 503 and body["code"] == "unavailable"
    assert not (root / "private").exists()


def test_disabled_feature_does_not_create_store(api, monkeypatch):
    request, _, _, root = api
    monkeypatch.setattr("config.conf", lambda: {"todo_enabled": False})
    assert request()[0] == 404
    status, summary = request("/api/todos/summary")
    assert status == 200 and not summary["enabled"]
    assert not (root / "private").exists()


def test_internal_error_is_500_without_internal_details(api, monkeypatch):
    request, _, _, _ = api
    def fail():
        raise RuntimeError("private internal path")
    monkeypatch.setattr(todo_handlers, "_build_service", fail)
    status, body = request()
    assert status == 500 and body["code"] == "internal"
    assert "private internal path" not in body["message"]


def test_unauthenticated_request_is_401(api):
    request, _, _, _ = api
    assert request(user=None)[0] == 401
