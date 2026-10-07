from http.cookies import SimpleCookie
import json

import pytest

from agent.coding.browser_auth import issue, verify
from tests.test_coding_session_routes import web, _member_with_coding_access, _json, _status, REQUEST_ID

PASSWORD = "test-upstream-password"


@pytest.fixture
def browser(web, monkeypatch):
    from config import conf
    from channel.web.fork.handlers import coding_browser
    conf()["opencode"].update(browser_sso=True, web_url=web.BASE + "/code/")
    monkeypatch.setenv("RSM_OPENCODE_PASSWORD", PASSWORD)
    web.upstream_calls = []
    web.upstream_data = {"healthy": True}
    web.upstream_events = None

    class Reply:
        status_code = 200
        def __init__(self):
            self.headers = {"Content-Type": "text/event-stream" if web.upstream_events is not None else "application/json"}
        def json(self): return web.upstream_data
        def close(self): pass
        def raise_for_status(self): pass
        def iter_lines(self, **kwargs):
            for value in web.upstream_events:
                yield b"data: " + json.dumps(value).encode()
                yield b""

    class Client:
        trust_env = True
        def request(self, method, url, **kwargs):
            assert self.trust_env is False
            web.upstream_calls.append((method, url, kwargs))
            return Reply()
        def close(self): pass

    monkeypatch.setattr(coding_browser.requests, "Session", Client)
    return web


def connection(web, name="alice"):
    token = web.login("root") if name == "root" else _member_with_coding_access(web, name)
    response = web.post("/api/coding/sessions", {"agent_id": "erp-coder", "request_id": REQUEST_ID}, token=token)
    assert _status(response) == 200
    raw = response.headers.get("Set-Cookie", "")
    cookie = SimpleCookie()
    cookie.load(raw)
    assert cookie, response
    grant = next(iter(cookie.values()))
    assert grant["httponly"] and grant["samesite"] == "Strict"
    assert grant["path"] == "/coding-api/"
    assert PASSWORD not in response.data.decode() + raw
    result = _json(response)
    assert "rsm_platform_auth=1" in result["iframe_url"]
    return token, grant.key + "=" + grant.value, result["external_session_id"]


def check(web, token, grant, *, method="GET", path="/global/health", origin=None, body=None, **headers):
    return web.request("/coding-api" + path, method, body, tenant=False, headers={
        "Cookie": f"cow_session={token}; {grant}", "Origin": web.BASE if origin is None else origin, **headers})


def test_grant_requires_same_login_service_signature_and_deadline():
    _, value = issue(PASSWORD, "login", tenant="a", agent="b", session="c", service="default", now=100)
    assert verify(PASSWORD, "login", value, service="default", now=101)
    for token, key, signed, service, now in [
        ("other", PASSWORD, value, "default", 101),
        ("login", "rotated", value, "default", 101),
        ("login", PASSWORD, value + "x", "default", 101),
        ("login", PASSWORD, value, "other", 101),
        ("login", PASSWORD, value, "default", 3700),
    ]:
        assert verify(key, token, signed, service=service, now=now) is None


@pytest.mark.parametrize("name", ["root", "alice"])
def test_administrator_and_authorized_member_both_use_platform_login(browser, name):
    token, grant, sid = connection(browser, name)
    result = check(browser, token, grant)
    assert _status(result) == 200, result.data
    assert PASSWORD not in str(result.headers) + str(result.data)
    assert _status(check(browser, token, grant, method="POST", path=f"/session/{sid}/prompt_async", body={"parts": []})) == 200
    call = browser.upstream_calls[-1]
    assert call[2]["headers"]["Authorization"].startswith("Basic ")
    assert "Cookie" not in call[2]["headers"]
    assert call[2]["allow_redirects"] is False


def test_grant_cannot_be_copied_to_another_login(browser):
    token, grant, _ = connection(browser)
    other = _member_with_coding_access(browser, "bob")
    for login, cookie in [(other, grant), ("", grant), (token, "")]:
        assert _status(check(browser, login, cookie)) == 401
    assert browser.upstream_calls == []


def test_logout_is_rechecked(browser):
    token, grant, _ = connection(browser)
    browser.service.revoke_session(token)
    assert _status(check(browser, token, grant)) == 401
    assert browser.upstream_calls == []


def test_agent_access_revocation_is_rechecked(browser):
    token, grant, _ = connection(browser)
    role = next(row for row in browser.service.list_roles(browser.tenant_id) if row["code"] == "alice-coding")
    browser.revoke_grants(role)
    assert _status(check(browser, token, grant)) == 401
    assert browser.upstream_calls == []


@pytest.mark.parametrize("options", [
    {"method": "POST", "path": "/session/ses_other/prompt_async", "origin": "https://evil.example"},
    {"method": "POST", "path": "/session/ses_other/prompt_async", "origin": ""},
    {"method": "PATCH", "path": "/config"},
    {"method": "PATCH", "path": "/global/config"},
    {"method": "POST", "path": "/global/dispose"},
    {"path": "/pty/123/connect", "Upgrade": "websocket"},
    {"path": "/file/content?path=../../secret"},
    {"path": "/file/content?path=/etc/passwd"},
    {"path": "/config?directory=/other/project"},
    {"path": "/config?workspaceID=other"},
])
def test_scope_cross_origin_and_administration_refusals_do_not_reach_upstream(browser, options):
    token, grant, _ = connection(browser)
    assert _status(check(browser, token, grant, **options)) == 403
    assert browser.upstream_calls == []


def test_other_members_session_cannot_be_read_written_or_listed(browser):
    alice, grant, sid = connection(browser)
    _, _, other = connection(browser, "bob")
    for method in ["GET", "POST", "PATCH", "DELETE"]:
        assert _status(check(browser, alice, grant, method=method, path=f"/session/{other}")) == 404
    assert browser.upstream_calls == []
    browser.upstream_data = [{"id": sid}, {"id": other}]
    assert _json(check(browser, alice, grant, path="/session")) == [{"id": sid}]
    browser.upstream_data = {sid: {"type": "busy"}, other: {"type": "busy"}}
    assert list(_json(check(browser, alice, grant, path="/session/status"))) == [sid]
    browser.upstream_data = [{"id": "mine", "sessionID": sid}, {"id": "theirs", "sessionID": other}]
    assert [item["id"] for item in _json(check(browser, alice, grant, path="/permission"))] == ["mine"]


def test_global_events_only_include_owned_session_messages(browser):
    token, grant, sid = connection(browser)
    _, _, other = connection(browser, "bob")
    def event(session, text):
        return {"directory": browser.project_dir, "payload": {"type": "message.part.updated",
            "properties": {"part": {"sessionID": session, "text": text}}}}
    browser.upstream_events = [event(sid, "mine"), event(other, "other user secret"),
        {"directory": browser.project_dir, "payload": {"type": "sync", "properties": {"data": "global secret"}}}]
    result = check(browser, token, grant, path="/global/event")
    assert _status(result) == 200
    assert b"mine" in result.data
    assert b"other user secret" not in result.data
    assert b"global secret" not in result.data


def test_connection_metadata_never_returns_upstream_secrets(browser):
    token, grant, _ = connection(browser)
    browser.upstream_data = {"all": [{"id": "sap", "key": PASSWORD, "options": {"apiKey": PASSWORD, "headers": {"Authorization": PASSWORD}}}]}
    result = check(browser, token, grant, path="/provider")
    assert PASSWORD not in result.data.decode()


@pytest.mark.parametrize("path", ["/config", "/global/config"])
def test_bootstrap_config_only_exposes_display_preferences(browser, path):
    token, grant, _ = connection(browser)
    browser.upstream_data = {"model": "provider/model", "provider": {"options": {"apiKey": PASSWORD}},
                             "mcp": {"sap": {"environment": {"PASSWORD": PASSWORD}}}, "instructions": ["private"]}
    assert _json(check(browser, token, grant, path=path)) == {"model": "provider/model"}


def test_bootstrap_resources_are_available_to_authorized_members(browser):
    token, grant, _ = connection(browser)
    browser.upstream_data = {"sap/resource": {"name": "SAP", "uri": "sap://reference"}}
    response = check(browser, token, grant, path="/experimental/resource")
    assert _status(response) == 200
    assert _json(response) == browser.upstream_data
