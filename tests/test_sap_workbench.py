"""Exercise scene configuration, real HTTP authorization and additive storage."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import sqlite3
from pathlib import Path

import pytest

from Scene.sap_workbench.backend.configuration import (
    DEFAULT_CONFIG, WorkbenchError, capabilities, validate_config,
)
from Scene.sap_workbench.backend.store import WorkbenchStore
from tests._helpers import WebAppHarness

API = "/api/scenes/sap-workbench"


def configured():
    value = deepcopy(DEFAULT_CONFIG)
    value["coding_agent_id"] = "sap-coder"
    value["sap"].update(system_id="test-sap", web_gui_url="https://sap.example.test/webgui?sap-client=200",
                        client="200")
    value["browser_service_ref"] = "sap-browser-worker"
    return value


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", "12" * 32)
    harness = WebAppHarness(tmp_path / "instance")
    harness.add_agent("normal-agent")
    harness.add_coding_agent("sap-coder", "/srv/sap-workbench")
    harness.sap_role = harness.role("sap-use", ["chat.use", "agent.use", "agent.read"],
                                   grants=[("agent", "agent:sap-coder", "use")])
    harness.member("alice", ["sap-use"])
    harness.member("bob", ["sap-use"])
    # Keep handler contract tests isolated from the developer's live services.
    async def probe(*args):
        return [{'id': 'opencode', 'verification': 'passed'}]
    monkeypatch.setattr('Scene.sap_workbench.backend.probe.probe_connections', probe)
    yield harness
    harness.close()


def payload(app, response, status=200):
    assert int(response.status.split()[0]) == status, response.data
    return app.json(response)


def test_catalog_assets_and_scene_open_do_not_create_sessions(app, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Opening scene configuration must not create coding sessions")
    monkeypatch.setattr("agent.coding.sessions.CodingSessionService.reserve", unexpected)
    token = app.login("root")
    catalog = payload(app, app.get("/api/scenes", token=token))
    scene = next(s for s in catalog["scenes"] if s["id"] == "sap_workbench")
    assert scene["has_workbench"] and not scene.get("sub_scenes")
    data = payload(app, app.get(API + "/config", token=token))
    assert data["version"] == 0 and not data["capabilities"]["visual"]
    assert [v["id"] for v in data["coding_options"]] == ["sap-coder"]
    asset = app.get("/scene-assets/runtime.js")
    assert asset.status == "200 OK" and b"window.SapWorkbench" in asset.data
    assert app.get("/scene-assets/sap_workbench/backend/http.py").status == "404 Not Found"
    assert app.get("/scene-assets/sap_workbench/frontend/workbench.css").status == "200 OK"


def test_the_scene_catalog_allocates_no_session_and_starts_no_engine(app, monkeypatch):
    """Browsing the catalog is a read: no binding reserved, and no engine started.

    The scene used to warm a Bun host from this read, because the create that
    followed paid for an engine cold start. It no longer starts any engine: the
    conversation is a platform coding session the page mounts from its own embed
    URL. So the catalog read must still not reserve anything, and the warm-up
    hook must now stay silent even for a fully configured, visually ready scene.
    """
    from Scene.sap_workbench.backend import prewarm

    def unexpected(*args, **kwargs):
        raise AssertionError("A scene read must not create coding sessions")
    monkeypatch.setattr("agent.coding.sessions.CodingSessionService.reserve", unexpected)
    monkeypatch.delenv("SAP_WORKBENCH_PREWARM", raising=False)
    warmed = []
    monkeypatch.setattr(prewarm, "kick", lambda: warmed.append(True))
    token = app.login("root")
    assert payload(app, app.get("/api/scenes", token=token))["status"] == "success"
    assert warmed == [], "an unconfigured scene must not start an engine"
    config = configured(); config.update(enabled=True)
    payload(app, app.put(API + "/config", {"version": 0, "config": config}, token=token))
    assert payload(app, app.get("/api/scenes", token=token))["status"] == "success"
    assert warmed == [], "the scene has no engine of its own to warm"
    assert payload(app, app.get(API + "/config", token=token))["capabilities"]["visual"] is True


def test_cancelling_a_warm_up_frees_its_process_and_is_always_safe(app):
    """A create can arrive while the warm-up still runs, and must win.

    The card entry reads the configuration and creates a session in the same
    breath, so the warm-up is usually still starting an engine when the real one
    begins. Leaving it would put two engine starts on the same CPU and disk, so
    the real start cancels the warm-up first. Cancelling must be safe when
    nothing is running, including twice, and must invalidate the aborted run so
    it is never reported as a completed warm-up.
    """
    import shutil
    import subprocess
    import sys
    import tempfile
    from Scene.sap_workbench.backend import prewarm

    prewarm.cancel()  # nothing in flight
    with prewarm._registry_lock:
        generation = prewarm._generation
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    base = tempfile.mkdtemp(prefix="sap-workbench-prewarm-cancel-test-")
    try:
        with prewarm._registry_lock:
            prewarm._live.append((process, base))
        prewarm.cancel()
        process.wait(timeout=10)
        assert process.poll() is not None, "the warm-up process must be stopped"
        with prewarm._registry_lock:
            assert prewarm._generation > generation, "the aborted run must not report itself ready"
        prewarm.cancel()  # idempotent
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        with prewarm._registry_lock:
            prewarm._live[:] = [item for item in prewarm._live if item[0] is not process]
        shutil.rmtree(base, ignore_errors=True)


def test_admin_save_is_persistent_versioned_and_audited(app):
    token = app.login("root")
    body = {"version": 0, "config": configured()}
    saved = payload(app, app.put(API + "/config", body, token=token))
    assert saved["version"] == 1
    assert saved["config"]["mcp"]["connections"][0]["credential_source"] == "scene_config"
    assert saved["coding"]["project_dir"] == "/srv/sap-workbench"
    assert payload(app, app.get(API + "/config", token=token))["config"] == saved["config"]
    assert payload(app, app.put(API + "/config", body, token=token), 409)["code"] == "config_conflict"
    events = app.service.list_audit(app.tenant_id)
    assert any(e["action"] == "sap_workbench.config.update" for e in events)


def test_saving_a_binding_installs_the_skill_and_plugin_into_the_project(app, tmp_path):
    """The coding project's `.opencode` is where a standard coding service
    discovers skills and plugins, so a saved binding must leave them there."""
    project = tmp_path / "sap-workbench-project"
    project.mkdir()
    app.add_coding_agent("sap-coder", str(project))
    token = app.login("root")
    saved = payload(app, app.put(API + "/config", {"version": 0, "config": configured()}, token=token))
    report = saved["project_toolkit"]
    assert report["conflicts"] == []
    outcomes = {item["path"]: item["result"] for item in report["artifacts"]}
    assert set(outcomes.values()) == {"installed"}
    assert (project / ".opencode/skills/sap-workbench/SKILL.md").is_file()
    assert (project / ".opencode/plugins/rsm-sap-workbench-navigation.js").is_file()
    # Saving the same binding again reconciles instead of duplicating.
    again = payload(app, app.put(API + "/config", {"version": 1, "config": configured()}, token=token))
    assert {item["result"] for item in again["project_toolkit"]["artifacts"]} == {"unchanged"}


def test_an_unreachable_project_directory_does_not_fail_the_save(app):
    """A binding whose project directory is not reachable from this host is still
    a valid binding. The install reports the truth instead of claiming success."""
    token = app.login("root")
    saved = payload(app, app.put(API + "/config", {"version": 0, "config": configured()}, token=token))
    # The harness binds sap-coder to /srv/sap-workbench, which does not exist here.
    assert saved["project_toolkit"] == {"artifacts": [], "conflicts": [],
                                        "error": "project_directory_missing"}
    assert payload(app, app.get(API + "/config", token=token))["version"] == 1


def test_invalid_browser_reference_is_rejected_and_legacy_config_can_be_repaired(app, monkeypatch):
    token = app.login('root')
    config = configured()
    config.update(browser_service_ref='retired-node', enabled=True)
    result = payload(app, app.put(API+'/config', {'version':0, 'config':config}, token=token), 400)
    assert result['code'] == 'browser_service_unavailable'
    store = WorkbenchStore(Path(app.data_root) / 'scenes/sap_workbench.sqlite3')
    store.save_config(app.tenant_id, app.admin_id, 0, config, audit=lambda _: None)
    projected = payload(app, app.get(API+'/config', token=token))
    assert projected['can_manage'] and projected['config']['browser_service_ref'] == 'retired-node'
    assert not projected['capabilities']['visual']
    assert not projected['capabilities']['automation']
    def forbidden(*args, **kwargs): raise AssertionError('Unknown node must not allocate a binding')
    monkeypatch.setattr(WorkbenchStore, 'reserve_session', forbidden)
    result = payload(app, app.post(API+'/sessions', {'request_id':'test'}, token=token), 503)
    assert result['code'] == 'browser_service_unavailable'
    repaired = payload(app, app.put(API+'/config', {'version':1,'config':configured()}, token=token))
    assert repaired['version'] == 2


def test_legacy_local_node_keeps_capabilities_without_configuration_rewrite(app):
    token = app.login('root')
    config = configured()
    config.update(browser_service_ref='local', enabled=True, automation_enabled=True)
    saved = payload(app, app.put(API+'/config', {'version':0,'config':config}, token=token))
    assert saved['config']['browser_service_ref'] == 'local'
    assert saved['capabilities']['visual'] and saved['capabilities']['automation']


def test_inaccessible_coding_option_does_not_poison_successful_config_status(app):
    other = app.stack.other_tenant()
    app.add_coding_agent('foreign-coder', '/private-project', tenant_id=other['tenant_id'])
    data = payload(app, app.get(API+'/config', token=app.login('root')))
    assert [item['id'] for item in data['coding_options']] == ['sap-coder']
    assert '/private-project' not in str(data)


def test_admin_can_repair_retired_coding_without_leaking_its_project(app, monkeypatch):
    token = app.login('root')
    payload(app, app.put(API+'/config', {'version':0,'config':configured()}, token=token))
    from Scene.sap_workbench.backend import http
    import web
    def retired(*args):
        raise web.HTTPError('404 Not Found', {'Content-Type':'application/json'}, '{}')
    monkeypatch.setattr(http, '_coding', retired)
    data = payload(app, app.get(API+'/config', token=token))
    assert data['can_manage'] and data['coding'] is None and data['coding_options'] == []


def test_platform_agent_routing_metadata_is_ignored(app):
    """The console fetch shim injects `agent_id` into every same-origin JSON body.

    A real browser save therefore arrives as `{version, config, agent_id}`. The
    scene must treat that routing metadata as transport, not as an unknown field
    that fails the save with `invalid_request`.
    """
    token = app.login("root")
    body = {"version": 0, "config": configured(), "agent_id": "sap-coder"}
    saved = payload(app, app.put(API + "/config", body, token=token))
    assert saved["version"] == 1
    checked = payload(app, app.post(API + "/check", {"agent_id": "sap-coder"}, token=token))
    assert checked["network_tested"] is True


def test_member_projection_excludes_config_and_cannot_write(app):
    root, alice = app.login("root"), app.login("alice")
    payload(app, app.put(API + "/config", {"version": 0, "config": configured()}, token=root))
    data = payload(app, app.get(API + "/config", token=alice))
    assert "config" not in data and "coding_options" not in data
    assert "api_url" not in data["opencode"] and "password_env" not in data["opencode"]
    assert data["credential_source"] == "scene_config"
    assert not data["can_manage"]
    payload(app, app.put(API + "/config", {"version": 1, "config": configured()}, token=alice), 403)
    payload(app, app.post(API + "/check", {}, token=alice), 403)


def test_auth_csrf_and_cross_tenant_parameters_are_rejected(app):
    token = app.login("root")
    payload(app, app.get(API + "/config"), 401)
    payload(app, app.get(API + "/config", token=token, tenant=False), 400)
    body = {"version": 0, "config": configured()}
    payload(app, app.put(API + "/config", body, token=token, headers={"Origin":"https://foreign.test"}), 403)
    payload(app, app.put(API + "/config", {**body, "tenant_id":"other"}, token=token), 400)
    assert payload(app, app.get(API + "/config", token=token))["version"] == 0


def test_member_cannot_choose_a_foreign_tenant_or_skip_chat_permission(app):
    alice = app.login("alice")
    payload(app, app.get(API + "/config", token=alice, headers={"X-Tenant-ID":"foreign-tenant"}), 403)
    app.role("no-chat", ["agent.use"], grants=[("agent", "agent:sap-coder", "use")])
    app.member("nochat", ["no-chat"])
    payload(app, app.get(API + "/config", token=app.login("nochat")), 403)


def test_logout_invalidates_configuration_access(app):
    token = app.login("alice")
    app.service.revoke_session(token)
    payload(app, app.get(API + "/config", token=token), 401)


def test_agent_grant_rechecked_and_normal_agent_not_accepted(app):
    root, alice = app.login("root"), app.login("alice")
    payload(app, app.put(API + "/config", {"version": 0, "config": configured()}, token=root))
    app.revoke_grants(app.sap_role)
    payload(app, app.get(API + "/config", token=alice), 403)
    config = configured(); config["coding_agent_id"] = "normal-agent"
    payload(app, app.put(API + "/config", {"version": 1, "config": config}, token=root), 400)


def test_session_rejects_invalid_request_key_and_password(app, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Unverified runtime allocated resources")
    monkeypatch.setattr("agent.coding.sessions.CodingSessionService.reserve", unexpected)
    root = app.login("root")
    config = configured(); config.update(enabled=True, automation_enabled=True, commit_enabled=True)
    saved = payload(app, app.put(API + "/config", {"version": 0, "config": config}, token=root))
    # Page operations follow the implemented adapter flag; business submission
    # has no execution tool and cannot be enabled by configuration alone.
    assert saved["capabilities"]["visual"] is True
    assert saved["capabilities"]["automation"] is True
    assert saved["capabilities"]["commit"] is False
    assert "browser_runtime_unavailable" not in saved["capabilities"]["blockers"]
    denied = payload(app, app.post(API + "/sessions", {"request_id":"one"}, token=root), 400)
    assert denied["code"] == "invalid_request_id"
    payload(app, app.post(API + "/sessions", {"password":"not-a-real-secret"}, token=root), 400)
    checked = payload(app, app.post(API + "/check", {}, token=root))
    assert checked["network_tested"] is True
    assert checked['checks'] == [{'id': 'opencode', 'verification': 'passed'}]


def test_unbound_browser_cannot_bypass_session_authorization(app, monkeypatch):
    def unexpected():
        raise AssertionError('Unbound screen must not allocate a runtime')
    monkeypatch.setattr('Scene.sap_workbench.browser_service.runner.browser_gateway.start', unexpected)
    for name in ['root', 'alice']:
        response = payload(app, app.post(API + '/browser', {}, token=app.login(name)), 410)
        assert response['code'] == 'bound_session_required'


@pytest.mark.parametrize("section,key,value", [
    (None, "password", "not-a-real-secret"),
    ("sap", "password", "not-a-real-secret"),
    ("sap", "web_gui_url", "https://u:p@sap.example.test/webgui"),
    ("sap", "web_gui_url", "https://sap.example.test/webgui?auth_token=secret"),
    ("sap", "web_gui_url", "javascript:alert(1)"),
    ("sap", "web_gui_url", "https://sap.example.test/webgui?sap-client=100"),
    ("sap", "login_mode", {}),
    (None, "enabled", "false"),
    (None, "max_sessions", True),
])
def test_invalid_or_secret_config_never_persists(section, key, value, tmp_path):
    config = configured()
    (config[section] if section else config)[key] = value
    store = WorkbenchStore(tmp_path / "store.sqlite3")
    with pytest.raises(WorkbenchError):
        store.save_config("tenant", "user", 0, config, audit=lambda _: None)
    assert store.read_config("tenant")["version"] == 0


@pytest.mark.parametrize("override", [
    {"username":"another"}, {"password":"secret"}, {"credential_source":"global"},
    {"transport":{}}, {"transport_auth":[]}, {"command":["arbitrary-command"]},
])
def test_mcp_cannot_override_sap_identity_or_launch_commands(override):
    config = configured(); config["mcp"]["connections"][0].update(override)
    with pytest.raises(WorkbenchError):
        validate_config(config)


@pytest.mark.parametrize("override", [
    {"url": "https://other.example/mcp"}, {"id": "other"}, {"transport": "stdio"},
    {"transport_auth": "sap_session"}, {"profile_ref": "other"},
])
def test_fixed_mcp_endpoints_cannot_be_replaced_through_configuration(app, override):
    token = app.login("root")
    config = configured()
    config["mcp"]["connections"][0].update(override)
    response = payload(app, app.put(API + "/config", {"version": 0, "config": config}, token=token), 400)
    assert response["code"] == "mcp_connection_fixed"
    assert payload(app, app.get(API + "/config", token=token))["version"] == 0


def test_fixed_mcp_defaults_and_enabled_state_survive_reload(app):
    token = app.login("root")
    before = payload(app, app.get(API + "/config", token=token))["config"]
    assert [(c["id"], c["url"]) for c in before["mcp"]["connections"]] == [
        ("sap-abap", "http://127.0.0.1:8110/mcp"), ("sap-pyrfc", "http://127.0.0.1:8200/mcp"),
    ]
    before["mcp"]["connections"][0]["enabled"] = False
    payload(app, app.put(API + "/config", {"version": 0, "config": before}, token=token))
    after = payload(app, app.get(API + "/config", token=token))["config"]
    assert after == before
    # Earlier drafts with no MCP rows acquire the two fixed defaults.
    assert validate_config({"mcp": {"connections": []}})["mcp"] == DEFAULT_CONFIG["mcp"]


def test_mcp_password_is_write_only_encrypted_and_blank_preserves_it(app):
    import json
    from auth.crypto import decrypt_secret
    from config import get_data_root
    from pathlib import Path
    token = app.login("root")
    config = configured(); config["mcp"]["username"] = "ALICE"
    secret = "test-only-mcp-password"
    saved = payload(app, app.put(API + "/config", {"version": 0, "config": config,
                            "mcp_password": secret}, token=token))
    assert saved["mcp_password_configured"] is True
    assert secret not in json.dumps(saved)
    assert "ciphertext" not in json.dumps(saved)
    after = payload(app, app.get(API + "/config", token=token))
    assert after["mcp_password_configured"] is True and secret not in json.dumps(after)
    store = WorkbenchStore(Path(get_data_root()) / "scenes" / "sap_workbench.sqlite3")
    with sqlite3.connect(store.path) as db:
        record = db.execute('SELECT config_json FROM "cj-sap_workbench-configs"').fetchone()[0]
        ciphertext = db.execute('SELECT ciphertext FROM "cj-sap_workbench-credentials"').fetchone()[0]
    assert secret not in record and secret not in ciphertext
    assert decrypt_secret(ciphertext) == secret
    assert secret not in json.dumps(app.service.list_audit(app.tenant_id))
    saved = payload(app, app.put(API + "/config", {"version": 1, "config": config,
                            "mcp_password": ""}, token=token))
    assert saved["mcp_password_configured"] is True
    assert store.resolve_mcp_credentials(app.tenant_id, 2)[1] == secret
    member = payload(app, app.get(API + "/config", token=app.login("alice")))
    assert "config" not in member and "mcp_password_configured" not in member


@pytest.mark.parametrize("change", ["username", "client", "web_gui_url", "system_id", "clear"])
def test_mcp_password_is_revoked_when_account_target_changes_or_explicitly_cleared(app, change):
    token = app.login("root")
    config = configured(); config["mcp"]["username"] = "ALICE"
    payload(app, app.put(API + "/config", {"version": 0, "config": config,
                        "mcp_password": "test-only-password"}, token=token))
    if change == "username":
        config["mcp"]["username"] = "BOB"
    elif change == "client":
        config["sap"].update(client="201", web_gui_url="https://sap.example.test/webgui?sap-client=201")
    elif change == "web_gui_url":
        config["sap"][change] = "https://other.example.test/webgui?sap-client=200"
    elif change == "system_id":
        config["sap"][change] = "OTHER"
    saved = payload(app, app.put(API + "/config", {"version": 1, "config": config,
                        "clear_mcp_password": change == "clear"}, token=token))
    assert saved["mcp_password_configured"] is False


def test_mcp_secret_write_requires_admin_and_working_encryption(app, monkeypatch):
    config = configured(); config["mcp"]["username"] = "ALICE"
    body = {"version": 0, "config": config, "mcp_password": "test-only-password"}
    payload(app, app.put(API + "/config", body, token=app.login("alice")), 403)
    monkeypatch.delenv("COW_CREDENTIAL_MASTER_KEY", raising=False)
    root = app.login("root")
    response = payload(app, app.put(API + "/config", body, token=root), 503)
    assert response["code"] == "credential_crypto_unavailable"
    assert payload(app, app.get(API + "/config", token=root))["version"] == 0


def test_mcp_secret_rotation_is_atomic_with_version_and_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", "12" * 32)
    store = WorkbenchStore(tmp_path / "scene.sqlite3")
    config = configured(); config["mcp"]["username"] = "ALICE"
    store.save_config("one", "admin", 0, config, mcp_password="old-test-secret", audit=lambda _: None)
    with pytest.raises(WorkbenchError, match="config_conflict"):
        store.save_config("one", "admin", 0, config, mcp_password="conflicting-test-secret", audit=lambda _: None)
    def fail(_):
        raise RuntimeError("audit unavailable")
    with pytest.raises(RuntimeError):
        store.save_config("one", "admin", 1, config, mcp_password="uncommitted-test-secret", audit=fail)
    assert store.resolve_mcp_credentials("one", 1)[1] == "old-test-secret"
    assert store.read_config("two")["mcp_password_configured"] is False
    with pytest.raises(WorkbenchError):
        store.resolve_mcp_credentials("two", 1)
    store.save_config("one", "admin", 1, config, mcp_password="new-test-secret", audit=lambda _: None)
    assert store.resolve_mcp_credentials("one", 2)[1] == "new-test-secret"
    with pytest.raises(WorkbenchError, match="config_conflict"):
        store.resolve_mcp_credentials("one", 1)


def test_additive_migration_concurrent_config_versions_and_tenant_scope(tmp_path):
    path = tmp_path / "store.sqlite3"
    store = WorkbenchStore(path)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE existing_chat (id TEXT)")
        db.execute("INSERT INTO existing_chat VALUES ('original')")
    WorkbenchStore(path)
    def save(_):
        try:
            return store.save_config("tenant-a", "user", 0, DEFAULT_CONFIG, audit=lambda _: None)["version"]
        except WorkbenchError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, range(2)))
    assert sorted(map(str, results)) == ["1", "config_conflict"]
    assert store.read_config("tenant-b")["version"] == 0
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id FROM existing_chat").fetchone()[0] == "original"
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"cj-sap_workbench-configs", "cj-sap_workbench-session_links", "cj-sap_workbench-actions"} <= tables


def test_audit_failure_rolls_back_configuration(tmp_path):
    store = WorkbenchStore(tmp_path / "store.sqlite3")
    def fail(_):
        raise RuntimeError("audit unavailable")
    with pytest.raises(RuntimeError):
        store.save_config("tenant", "user", 0, DEFAULT_CONFIG, audit=fail)
    assert store.read_config("tenant")["version"] == 0
    assert not capabilities(DEFAULT_CONFIG)["visual"]


def test_capabilities_publish_the_honest_limitations_table():
    reported = capabilities({**DEFAULT_CONFIG, "enabled": True})
    # The scene's *own* commit channel is still absent. That is a different
    # path from the mediated business channel, and the two are not conflated.
    assert reported["commit"] is False
    # The business row is published from the stored credential state, not from
    # the channel's existence: with no saved MCP account the server cannot
    # connect, so claiming "available" here would be the "registered therefore
    # usable" mistake this table exists to avoid.
    assert reported["notes"] == ["navigation_limited", "mcp_credentials_missing",
                                "page_readwrite_unavailable"]
    # A saved MCP account is what makes the row available.
    configured = capabilities({**DEFAULT_CONFIG, "enabled": True}, mcp_credentials=True)
    assert configured["notes"] == ["navigation_limited", "mcp_business_available",
                                  "page_readwrite_unavailable"]
    # `sap_data_call` reaches BAPIs through `call_rfc`, so "business submission
    # is unavailable" is no longer true and must not be published.
    assert "business_submission_unavailable" not in reported["blockers"]
