import pytest

from tests.test_coding_session_routes import web, _member_with_coding_access, _json, _status

FLASH = {"providerID": "deepseek", "modelID": "deepseek-flash"}
ZEN = {"providerID": "opencode", "modelID": "free-model"}


@pytest.fixture
def prefs(web):
    from config import conf
    conf()["opencode"].update(browser_sso=True, web_url=web.BASE + "/code/")
    return web


def request(web, token=None, body=None, origin=None):
    return web.request("/api/coding/model-preferences", "GET" if body is None else "POST", body,
                       tenant=False, headers={"Cookie": "cow_session=" + (token or ""),
                                              "Origin": web.BASE if origin is None else origin})


def test_web_settings_are_shared_with_a_fresh_login_and_survive_store_reopen(prefs):
    from auth.store import IdentityStore
    token = _member_with_coding_access(prefs, "alice")
    initial = {"user": [{**FLASH, "visibility": "show"}, {**ZEN, "visibility": "hide"}],
               "recent": [FLASH], "variant": {}}
    assert _status(request(prefs, token, {"initialize": initial})) == 200
    # The native client has another login token and no browser storage.
    second = prefs.login("alice")
    result = _json(request(prefs, second))
    assert result["preferences"] == {**initial, "session": {}}
    reopened = IdentityStore(prefs.service._store.db_path)
    assert reopened.execute("SELECT COUNT(*) AS n FROM coding_model_preferences")[0]["n"] == 1


def test_late_migration_never_overwrites_existing_web_preferences(prefs):
    token = prefs.login("root")
    request(prefs, token, {"operations": [{"type": "select", "model": FLASH}]})
    response = request(prefs, token, {"initialize": {"user": [], "recent": [ZEN]}})
    assert _json(response)["preferences"]["recent"] == [FLASH]
    assert _json(response)["revision"] == 1


def test_clients_merge_operations_without_replacing_each_others_changes(prefs):
    token = prefs.login("root")
    request(prefs, token, {"operations": [{"type": "visibility", "model": ZEN, "visible": False}]})
    request(prefs, token, {"operations": [{"type": "select", "model": FLASH}]})
    result = _json(request(prefs, token))["preferences"]
    assert result["user"] == [{**ZEN, "visibility": "hide"}, {**FLASH, "visibility": "show"}]
    assert result["recent"] == [FLASH]


def test_members_only_read_and_write_their_own_preferences(prefs):
    alice = _member_with_coding_access(prefs, "alice")
    bob = _member_with_coding_access(prefs, "bob")
    request(prefs, alice, {"operations": [{"type": "select", "model": FLASH}]})
    assert _json(request(prefs, bob))["preferences"] is None
    assert _status(request(prefs, bob, {"user_id": "alice", "operations": []})) == 400
    assert _json(request(prefs, alice))["preferences"]["recent"] == [FLASH]


def test_model_variant_and_session_selection_are_shared(prefs):
    token = prefs.login("root")
    result = request(prefs, token, {"operations": [
        {"type": "variant", "model": FLASH, "value": "high"},
        {"type": "session", "id": "ses_example", "value": {"agent": "build", "model": FLASH, "variant": None}}]})
    data = _json(result)["preferences"]
    assert data["variant"] == {"deepseek/deepseek-flash": "high"}
    assert data["session"]["ses_example"]["model"] == FLASH


def test_preferences_require_live_login_and_same_origin_for_writes(prefs):
    assert _status(request(prefs)) == 401
    token = prefs.login("root")
    assert _status(request(prefs, token, {"initialize": {}}, origin="https://evil.example")) == 403
    prefs.service.revoke_session(token)
    assert _status(request(prefs, token)) == 401


@pytest.mark.parametrize("operation", [
    {"type": "select", "model": {**FLASH, "apiKey": "do-not-store"}},
    {"type": "session", "id": "ses_example", "value": {"password": "do-not-store"}},
    {"type": "visibility", "model": FLASH, "visible": "false"},
    {"type": "config", "model": FLASH},
])
def test_preference_api_cannot_store_credentials_or_configure_the_service(prefs, operation):
    token = prefs.login("root")
    assert _status(request(prefs, token, {"operations": [operation]})) == 400
    assert _json(request(prefs, token))["preferences"] is None
