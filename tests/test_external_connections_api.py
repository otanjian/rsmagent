# encoding:utf-8
"""External-connection management API over the real WSGI application.

Change ``add-external-system-access``, tasks 3.1-3.7. These tests drive
``build_web_app()`` — the real route table, the real HTTP policy gate, real
sessions and the real ``identity.db`` — because the properties that matter here
are not the handler's arithmetic:

* the scope lives in the path, so a payload cannot re-point a connection at
  another tenant or owner;
* the tenant/permission and platform gates refuse *before* the handler runs, and
  a plain member reaches only their own mailbox;
* the write origin guard applies to every state change;
* a concurrent writer loses on the version, and a still-referenced connection is
  refused with the summary of what references it;
* a secret crosses the wire once (in), and never comes back out — only its
  ``configured`` state does;
* a platform connection is invisible to a tenant until it is explicitly granted,
  and a tenant override is a real second connection with its own credentials.

Unit-level behaviour of the service and the store is covered separately in
``tests/test_external_connection_service.py`` and
``tests/test_external_connection_schema.py``.
"""

from __future__ import annotations

import json

import pytest

from tests._helpers import WebAppHarness

MASTER_KEY = "api-test-master-key"


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", MASTER_KEY)


ERP_RFC = {
    "provider": "rfc", "ashost": "sap.example.com", "sysnr": "00",
    "client": "100", "user": "sapuser", "lang": "EN",
}
MCP_HEADER = {
    "transport": "streamable_http", "url": "https://mcp.example.com/mcp",
    "auth": "header", "header_name": "X-Api-Key",
}
EMAIL_BOTH = {
    "imap": {"enabled": True, "host": "imap.example.com",
             "user": "alice@example.com"},
    "smtp": {"enabled": True, "host": "smtp.example.com",
             "user": "alice@example.com", "from_addr": "alice@example.com"},
}
SECRET = "S3cretPassw0rd!"


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    harness = WebAppHarness(tmp_path_factory.mktemp("external-api"))
    harness.add_agent("shared-agent")
    harness.role("conn-reader", ["external.connections.read", "chat.use"])
    harness.role("no-conn-read", ["chat.use"])
    harness.role("conn-manager", ["external.connections.read",
                                  "external.connections.manage", "chat.use"])
    harness.member("reader", ["conn-reader"])
    harness.member("manager", ["conn-manager"])
    harness.member("plain", ["member"])
    harness.member("no-reader", ["no-conn-read"])
    harness.admin_token = harness.login("root")
    harness.reader_token = harness.login("reader")
    harness.manager_token = harness.login("manager")
    harness.plain_token = harness.login("plain")
    harness.no_reader_token = harness.login("no-reader")
    yield harness
    harness.close()


def _json(response):
    return json.loads(response.data.decode("utf-8"))


# -- catalogue reads -------------------------------------------------------

def test_a_member_can_read_the_type_catalogue(web):
    response = web.get("/api/external-connections/types", token=web.plain_token)
    assert response.status == "200 OK"
    body = _json(response)
    by_kind = {item["kind"]: item for item in body["types"]}
    assert set(by_kind) == {"mcp", "erp", "oa", "email"}
    # Mail is the one type a plain member may create, and only personally.
    assert [s["scope"] for s in by_kind["email"]["scopes"]] == ["personal"]
    assert by_kind["email"]["scopes"][0]["available"] is True
    assert by_kind["erp"]["scopes"][0]["scope"] == "tenant"
    assert by_kind["erp"]["scopes"][0]["available"] is False


def test_the_type_catalogue_reports_test_and_execute_as_closed(web):
    body = _json(web.get("/api/external-connections/types",
                         token=web.manager_token))
    for item in body["types"]:
        assert item["capabilities"]["test_available"] is False
        assert item["capabilities"]["execute_available"] is False
        assert item["capabilities"]["unavailable_reason"].startswith("awaiting_")


def test_a_platform_catalogue_read_is_refused_for_a_non_admin(web):
    response = web.get("/api/external-connections/catalog?scope=platform",
                       token=web.manager_token)
    assert response.status == "403 Forbidden"
    assert _json(response)["code"] == "forbidden"


def test_a_tenant_catalogue_read_needs_the_read_permission(web):
    refused = web.get("/api/external-connections/catalog?scope=tenant",
                      token=web.no_reader_token)
    assert refused.status == "403 Forbidden"
    allowed = web.get("/api/external-connections/catalog?scope=tenant",
                      token=web.reader_token)
    assert allowed.status == "200 OK"
    assert _json(allowed)["scope"] == "tenant"
    # The default member role now grants catalogue reads; preserve that access.
    assert web.get("/api/external-connections/catalog?scope=tenant",
                   token=web.plain_token).status == "200 OK"


# -- personal mailbox ------------------------------------------------------

def test_a_member_creates_and_reads_their_own_mailbox(web):
    created = web.post("/api/external-connections/personal", {
        "kind": "email", "name": "我的邮箱", "config": EMAIL_BOTH,
        "secrets": {"imap_password": SECRET, "smtp_password": SECRET},
    }, token=web.plain_token)
    assert created.status == "200 OK", created.data
    body = _json(created)
    assert body["status"] == "success"
    connection_id = body["id"]
    assert connection_id.startswith("conn_")

    detail = web.get("/api/external-connections/personal/%s" % connection_id,
                     token=web.plain_token)
    assert detail.status == "200 OK"
    payload = _json(detail)
    assert payload["secrets"]["imap_password"] == {"configured": True}
    # The plaintext never leaves the server, and neither does the ciphertext.
    assert SECRET not in detail.data.decode("utf-8")
    assert MASTER_KEY not in detail.data.decode("utf-8")

    # A second connection for the same member is refused: one mailbox each.
    again = web.post("/api/external-connections/personal", {
        "kind": "email", "name": "第二个", "config": EMAIL_BOTH,
    }, token=web.plain_token)
    assert again.status == "409 Conflict"
    assert _json(again)["code"] == "singleton_exists"


def test_another_member_cannot_read_someone_elses_mailbox(web):
    created = _json(web.post("/api/external-connections/personal", {
        "kind": "email", "name": "manager mail", "config": EMAIL_BOTH,
        "secrets": {"imap_password": SECRET, "smtp_password": SECRET},
    }, token=web.manager_token))
    connection_id = created["id"]

    refused = web.get("/api/external-connections/personal/%s" % connection_id,
                      token=web.plain_token)
    # Another member's mailbox answers exactly like a missing one.
    assert refused.status == "404 Not Found"
    assert _json(refused)["code"] == "not_found"


def test_personal_scope_ignores_a_body_owner(web):
    """Owner comes from the session; a body cannot name another member."""
    response = web.post("/api/external-connections/personal", {
        "kind": "email", "name": "reader mail", "config": EMAIL_BOTH,
        "owner_user_id": web.user_id("manager"),
        "tenant_id": "tenant-somewhere-else",
    }, token=web.reader_token)
    assert response.status == "200 OK"
    connection_id = _json(response)["id"]
    assert _json(web.get("/api/external-connections/personal/%s" % connection_id,
                         token=web.reader_token))["owner_user_id"] == \
        web.user_id("reader")


# -- tenant scope ----------------------------------------------------------

def test_a_plain_member_cannot_create_a_tenant_connection(web):
    response = web.post("/api/external-connections/tenant", {
        "kind": "erp", "name": "SAP", "config": ERP_RFC,
        "secrets": {"password": SECRET},
    }, token=web.plain_token)
    assert response.status == "403 Forbidden"


def _create_tenant_erp(web, name="SAP 生产"):
    created = web.post("/api/external-connections/tenant", {
        "kind": "erp", "name": name, "config": ERP_RFC,
        "secrets": {"password": SECRET},
    }, token=web.manager_token)
    assert created.status == "200 OK", created.data
    return _json(created)


def test_a_manager_creates_a_tenant_erp_connection(web):
    connection = _create_tenant_erp(web, "SAP 生产")
    connection_id = connection["id"]
    assert connection["scope"] == "tenant"
    assert connection["secrets"]["password"] == {"configured": True}

    detail = _json(web.get("/api/external-connections/tenant/%s" % connection_id,
                           token=web.manager_token))
    assert detail["config"]["provider"] == "rfc"
    assert SECRET not in json.dumps(detail)


def test_a_read_only_role_cannot_update_but_can_read(web):
    connection_id = _create_tenant_erp(web, "SAP read-only")["id"]
    readable = web.get("/api/external-connections/tenant/%s" % connection_id,
                       token=web.reader_token)
    assert readable.status == "200 OK"
    assert _json(readable)["actions"] == ["read"]

    refused = web.post(
        "/api/external-connections/tenant/%s/update" % connection_id,
        {"expected_version": 1, "name": "renamed"}, token=web.reader_token)
    assert refused.status == "403 Forbidden"

    refused_delete = web.post(
        "/api/external-connections/tenant/%s/delete" % connection_id,
        {"expected_version": 1}, token=web.reader_token)
    assert refused_delete.status == "403 Forbidden"


def test_a_stale_version_over_http_is_a_409(web):
    connection_id = _create_tenant_erp(web, "SAP stale")["id"]
    first = web.post("/api/external-connections/tenant/%s/update" % connection_id,
                     {"expected_version": 1, "name": "first"},
                     token=web.manager_token)
    assert first.status == "200 OK"
    stale = web.post("/api/external-connections/tenant/%s/update" % connection_id,
                     {"expected_version": 1, "name": "second"},
                     token=web.manager_token)
    assert stale.status == "409 Conflict"
    assert _json(stale)["code"] == "version_conflict"


def test_secret_keep_replace_and_clear_over_http(web):
    created = _json(web.post("/api/external-connections/tenant", {
        "kind": "oa", "name": "OA", "config": {
            "base_url": "https://oa.example.com", "username": "alice"},
        "secrets": {"password": SECRET},
    }, token=web.manager_token))
    connection_id = created["id"]

    # A blank entry keeps the stored secret (a form re-submit is not a rotation).
    kept = _json(web.post(
        "/api/external-connections/tenant/%s/update" % connection_id,
        {"expected_version": 1, "name": "OA", "secrets": {"password": ""}},
        token=web.manager_token))
    assert kept["secrets"]["password"] == {"configured": True}

    # A masked value is refused rather than stored as the new credential.
    masked = web.post(
        "/api/external-connections/tenant/%s/update" % connection_id,
        {"expected_version": 2, "secrets": {"password": "ERP •••• rd"}},
        token=web.manager_token)
    assert masked.status == "400 Bad Request"
    assert _json(masked)["code"] == "masked_secret"

    # A value replaces it, and the version moves on.
    replaced = _json(web.post(
        "/api/external-connections/tenant/%s/update" % connection_id,
        {"expected_version": 2, "secrets": {"password": "Replaced1!"}},
        token=web.manager_token))
    assert replaced["secrets"]["password"] == {"configured": True}

    # An explicit null clears it, and the projection says the slot is missing.
    cleared = _json(web.post(
        "/api/external-connections/tenant/%s/update" % connection_id,
        {"expected_version": 3, "secrets": {"password": None}},
        token=web.manager_token))
    assert cleared["secrets"]["password"] == {"configured": False}
    assert cleared["capabilities"]["missing_secret_slots"] == ["password"]


def test_a_tenant_write_without_a_tenant_header_is_refused(web):
    response = web.post("/api/external-connections/tenant", {
        "kind": "erp", "name": "SAP", "config": ERP_RFC,
    }, token=web.manager_token, tenant=False)
    assert response.status in ("400 Bad Request", "403 Forbidden")


def test_a_cross_origin_write_is_refused(web):
    response = web.post("/api/external-connections/tenant", {
        "kind": "erp", "name": "SAP", "config": ERP_RFC,
    }, token=web.manager_token,
        headers={"Origin": "http://evil.example.com"})
    assert response.status == "403 Forbidden"
    assert _json(response)["code"] == "csrf_failed"


def test_the_erp_default_round_trips_and_blocks_deletion(web):
    first = _json(web.post("/api/external-connections/tenant", {
        "kind": "erp", "name": "ERP A", "config": ERP_RFC,
    }, token=web.manager_token))
    second = _json(web.post("/api/external-connections/tenant", {
        "kind": "erp", "name": "ERP B", "config": ERP_RFC,
    }, token=web.manager_token))

    before = _json(web.get("/api/external-connections/tenant/erp-default",
                           token=web.manager_token))
    assert before == {"status": "success", "connection_id": None, "revision": 1}

    set_default = web.post("/api/external-connections/tenant/erp-default", {
        "connection_id": first["id"], "expected_revision": before["revision"],
    }, token=web.manager_token)
    assert set_default.status == "200 OK", set_default.data
    assert _json(set_default)["revision"] == 2

    refused = web.post(
        "/api/external-connections/tenant/%s/delete" % first["id"],
        {"expected_version": 1}, token=web.manager_token)
    assert refused.status == "409 Conflict"
    body = _json(refused)
    assert body["code"] == "referenced"
    assert body["references"][0]["kind"] == "erp_default"
    assert body["references"][0]["next_action"] == "set_erp_default"

    swapped = web.post(
        "/api/external-connections/tenant/%s/delete" % first["id"],
        {"expected_version": 1,
         "default_handling": {"action": "replace",
                              "connection_id": second["id"]}},
        token=web.manager_token)
    assert swapped.status == "200 OK", swapped.data
    after = _json(web.get("/api/external-connections/tenant/erp-default",
                          token=web.manager_token))
    assert after["connection_id"] == second["id"]


# -- platform scope --------------------------------------------------------

def _create_platform(web, name="shared"):
    created = web.post("/api/external-connections/platform", {
        "kind": "mcp", "name": name, "config": MCP_HEADER,
        "secrets": {"header": SECRET},
    }, token=web.admin_token)
    assert created.status == "200 OK", created.data
    platform = _json(created)
    assert platform["scope"] == "platform"
    return platform["id"]


def _grant(web, platform_id, tenant_ids):
    current = _json(web.get(
        "/api/external-connections/platform/%s/tenant-access" % platform_id,
        token=web.admin_token))
    granted = web.post(
        "/api/external-connections/platform/%s/tenant-access" % platform_id,
        {"tenant_ids": list(tenant_ids),
         "expected_revision": current["revision"]}, token=web.admin_token)
    assert granted.status == "200 OK", granted.data
    return _json(granted)


def test_platform_scope_is_a_platform_admin_address(web):
    refused = web.post("/api/external-connections/platform", {
        "kind": "mcp", "name": "shared", "config": MCP_HEADER,
    }, token=web.manager_token)
    assert refused.status == "403 Forbidden"

    platform_id = _create_platform(web, "shared-admin-address")

    # A non-admin cannot read the platform detail either.
    assert web.get("/api/external-connections/platform/%s" % platform_id,
                   token=web.manager_token).status == "403 Forbidden"

    # The platform secret is never returned, only its presence.
    detail = _json(web.get("/api/external-connections/platform/%s" % platform_id,
                           token=web.admin_token))
    assert detail["secrets"]["header"] == {"configured": True}
    assert SECRET not in json.dumps(detail)


def test_a_platform_connection_is_invisible_until_granted(web):
    platform_id = _create_platform(web, "shared-invisible")

    before = _json(web.get("/api/external-connections/catalog?scope=tenant",
                           token=web.manager_token))
    assert platform_id not in [item["id"] for item in before["items"]]

    access = _json(web.get(
        "/api/external-connections/platform/%s/tenant-access" % platform_id,
        token=web.admin_token))
    assert access["tenants"] == []
    _grant(web, platform_id, [web.tenant_id])

    after = _json(web.get("/api/external-connections/catalog?scope=tenant",
                          token=web.manager_token))
    card = [item for item in after["items"] if item["id"] == platform_id][0]
    assert card["source"] == "inherited"
    assert card["effective_id"] == platform_id
    assert SECRET not in json.dumps(card)

    # The tenant reads the inherited detail through the tenant address.
    detail = web.get("/api/external-connections/tenant/%s" % platform_id,
                     token=web.manager_token)
    assert detail.status == "200 OK"
    assert _json(detail)["secrets"]["header"] == {"configured": True}

    # Revoking is atomic: the tenant's catalogue goes empty again.
    _grant(web, platform_id, [])
    revoked = _json(web.get("/api/external-connections/catalog?scope=tenant",
                            token=web.manager_token))
    assert platform_id not in [item["id"] for item in revoked["items"]]


def test_a_tenant_override_is_a_second_connection_with_its_own_secret(web):
    platform_id = _create_platform(web, "shared-override")
    _grant(web, platform_id, [web.tenant_id])

    override = web.post("/api/external-connections/tenant", {
        "kind": "mcp", "name": "tenant override", "config": MCP_HEADER,
        "secrets": {"header": "tenant-secret"},
        "base_connection_id": platform_id,
    }, token=web.manager_token)
    assert override.status == "200 OK", override.data
    override_id = _json(override)["id"]
    assert override_id != platform_id

    catalogue = _json(web.get("/api/external-connections/catalog?scope=tenant",
                              token=web.manager_token))
    by_id = {item["id"]: item for item in catalogue["items"]}
    assert by_id[platform_id]["source"] == "overridden"
    assert by_id[platform_id]["effective_id"] == override_id
    assert by_id[override_id]["source"] == "override"

    # The platform source cannot be deleted out from under the tenant's data.
    refused = web.post(
        "/api/external-connections/platform/%s/delete" % platform_id,
        {"expected_version": 1}, token=web.admin_token)
    assert refused.status == "409 Conflict"
    kinds = {ref["kind"] for ref in _json(refused)["references"]}
    assert kinds == {"tenant_overrides", "tenant_access"}

    # Dropping the override restores inheritance.
    restored = web.post(
        "/api/external-connections/tenant/%s/restore-inheritance" % platform_id,
        {"expected_version": 1}, token=web.manager_token)
    assert restored.status == "200 OK", restored.data
    after = _json(web.get("/api/external-connections/catalog?scope=tenant",
                          token=web.manager_token))
    assert [item["source"] for item in after["items"]
            if item["id"] == platform_id] == ["inherited"]


def test_idempotent_create_over_http(web):
    payload = {"kind": "erp", "name": "Idem", "config": ERP_RFC}
    first = web.post("/api/external-connections/tenant", payload,
                     token=web.manager_token,
                     headers={"Idempotency-Key": "http-key-1"})
    replay = web.post("/api/external-connections/tenant", payload,
                      token=web.manager_token,
                      headers={"Idempotency-Key": "http-key-1"})
    assert first.status == "200 OK" and replay.status == "200 OK"
    assert _json(first)["id"] == _json(replay)["id"]

    conflict = web.post("/api/external-connections/tenant",
                        dict(payload, name="Idem other"),
                        token=web.manager_token,
                        headers={"Idempotency-Key": "http-key-1"})
    assert conflict.status == "409 Conflict"
    assert _json(conflict)["code"] == "idempotency_conflict"


def test_an_invalid_body_is_refused_with_a_machine_code(web):
    response = web.post("/api/external-connections/tenant", {
        "kind": "erp", "name": "SAP",
        "config": dict(ERP_RFC, password="leaky"),
    }, token=web.manager_token)
    assert response.status == "400 Bad Request"
    body = _json(response)
    assert body["code"] == "secret_in_config"
    assert "password" in body["fields"]


def test_an_anonymous_request_is_refused(web):
    for path in ("/api/external-connections/types",
                 "/api/external-connections/catalog",
                 "/api/external-connections/personal"):
        response = web.get(path) if path != "/api/external-connections/personal" \
            else web.post(path, {"kind": "email", "name": "x", "config": EMAIL_BOTH})
        assert response.status.startswith(("401", "403")), (path, response.status)


def test_a_zero_tenant_platform_admin_can_manage_platform_connections(web):
    """A platform admin with no tenant selection still reaches the platform scope."""
    created = web.post("/api/external-connections/platform", {
        "kind": "mcp", "name": "zero-tenant", "config": MCP_HEADER,
    }, token=web.admin_token, tenant=False)
    assert created.status == "200 OK", created.data
    platform_id = _json(created)["id"]

    detail = web.get("/api/external-connections/platform/%s" % platform_id,
                     token=web.admin_token, tenant=False)
    assert detail.status == "200 OK"
    assert _json(detail)["scope"] == "platform"

    listing = web.get("/api/external-connections/catalog?scope=platform",
                      token=web.admin_token, tenant=False)
    assert listing.status == "200 OK"
    assert platform_id in [item["id"] for item in _json(listing)["items"]]

    # The same caller cannot reach a tenant address without selecting one.
    assert web.get("/api/external-connections/catalog?scope=tenant",
                   token=web.admin_token, tenant=False).status.startswith("400")


def test_a_closed_action_has_no_route_at_all(web):
    """``execute`` has no HTTP address at all.

    Two different gates, and only one of them is a route that exists:

    * ``test``/``runtime`` are *route* actions, because the console has to be
      able to ask so it can render the deployment's reason. Asking is not
      running: the service refuses with ``test_not_available`` while the type's
      readiness class is closed, which the next test pins.
    * ``execute`` is not a declared route action, so no path serves it. A
      business action goes through the tool/runtime path, where the risk
      catalogue and the approval binding live; giving it an HTTP address would
      create a second entry point that could miss them.

    The gate is the *only* refusal that cannot be bypassed by a handler
    forgetting its own check, so the proof for ``execute`` is that the address
    does not exist.
    """
    connection_id = _json(web.post("/api/external-connections/tenant", {
        "kind": "erp", "name": "Closed action", "config": ERP_RFC,
    }, token=web.manager_token))["id"]
    for action in ("invoke", "execute", "run"):
        response = web.post(
            "/api/external-connections/tenant/%s/%s" % (connection_id, action),
            {}, token=web.manager_token)
        assert response.status == "404 Not Found", action


def test_a_test_route_exists_but_is_refused_while_the_type_is_closed(web):
    """The route answers, and the *deployment switch* is what refuses it.

    Declaring the route must not be mistaken for opening the class: with no
    readiness configured (the default), the probe is refused with the stable
    code ``test_not_available`` — not an empty success, and not a 404 that
    would leave the console unable to explain itself.
    """
    connection_id = _json(web.post("/api/external-connections/tenant", {
        "kind": "erp", "name": "Closed test", "config": ERP_RFC,
        "secrets": {"password": "S3cret!"},
    }, token=web.manager_token))["id"]
    response = web.post(
        "/api/external-connections/tenant/%s/test" % connection_id,
        {}, token=web.manager_token)
    assert response.status.startswith("403"), response.status
    body = _json(response)
    assert body["code"] == "test_not_available"


def test_opening_the_type_switch_makes_the_test_route_reachable(web, monkeypatch):
    """With the switch on, the refusal changes from the gate to the adapter.

    This proves the two gates are independent: an operator opening the class
    moves the answer past the deployment gate to the next honest reason (no
    adapter installed in this build), rather than being masked by it.
    """
    from config import conf
    monkeypatch.setitem(
        conf().setdefault("external_connections", {}),
        "readiness", {"erp": {"test": True}})
    connection_id = _json(web.post("/api/external-connections/tenant", {
        "kind": "erp", "name": "Open test", "config": ERP_RFC,
        "secrets": {"password": "S3cret!"},
    }, token=web.manager_token))["id"]
    response = web.post(
        "/api/external-connections/tenant/%s/test" % connection_id,
        {}, token=web.manager_token)
    assert not (response.status.startswith("403")
                and _json(response).get("code") == "test_not_available")


def test_a_test_route_never_leaks_the_secret_or_the_target_address(web, monkeypatch):
    """The runtime projection describes the policy, never a resolved address."""
    from config import conf
    monkeypatch.setitem(
        conf().setdefault("external_connections", {}),
        "readiness", {"erp": {"test": True}})
    connection_id = _json(web.post("/api/external-connections/tenant", {
        "kind": "erp", "name": "Runtime", "config": ERP_RFC,
        "secrets": {"password": "S3cret!"},
    }, token=web.manager_token))["id"]
    response = web.get(
        "/api/external-connections/tenant/%s/runtime" % connection_id,
        token=web.manager_token)
    assert response.status.startswith("200"), response.status
    raw = response.data.decode("utf-8")
    assert "S3cret!" not in raw
    body = _json(response)
    network = body["limits"]["network"]
    assert "allow_hosts" in network and "allow_networks" in network
    # The policy's *rules* are reported; a resolved address is not, so this
    # endpoint cannot be used to enumerate the internal network.
    assert "addresses" not in network and "resolved" not in network


def test_the_service_and_the_http_layer_share_one_database(web):
    """A connection created over HTTP is visible to the service directly."""
    from integrations.external.service import get_external_connection_service

    connection = _json(web.post("/api/external-connections/tenant", {
        "kind": "erp", "name": "Shared DB", "config": ERP_RFC,
    }, token=web.manager_token))
    service = get_external_connection_service()
    assert service._store.db_path == web.db_path
    row = service._fetch_row(connection["id"], tenant_id=web.tenant_id)
    assert row is not None and row["name"] == "Shared DB"


# -- per-connection Agent assignment over HTTP -----------------------------
#
# Change ``add-external-connection-agent-assignment``, task group 2. These drive
# the real route table so the properties that only exist on the wire are
# covered: the read is a tenant read, the save adds the manage permission *and*
# the origin guard, the path (never the body) names the connection, the target
# is re-qualified at save time under the Agent's own management rule, and the
# assignment's independent revision is what a concurrent writer loses on.



@pytest.fixture(scope="module")
def assign_web(tmp_path_factory):
    harness = WebAppHarness(tmp_path_factory.mktemp("external-assign-api"))
    harness.add_agent("agent-a", "agent-b", "agent-c")
    harness.role("assign-reader", ["external.connections.read", "chat.use"])
    harness.role("assign-manager", ["external.connections.read",
                                    "external.connections.manage", "chat.use"])
    # A member with neither connection permission: the one identity that must
    # be refused the assignment *read* at the service, not merely at the route.
    harness.role("assign-none", ["chat.use"])
    harness.member("assign_reader", ["assign-reader"])
    harness.member("assign_manager", ["assign-manager"])
    harness.member("assign_nobody", ["assign-none"])
    harness.member("assign_plain", ["member"])
    harness.admin_token = harness.login("root")
    harness.reader_token = harness.login("assign_reader")
    harness.manager_token = harness.login("assign_manager")
    harness.nobody_token = harness.login("assign_nobody")
    harness.plain_token = harness.login("assign_plain")
    # A private Agent per member: its owner may assign it, and nobody else —
    # not even the tenant admin — may see it as a candidate or name it.
    harness.private_agent(harness.user_id("assign_reader"), "reader-private")
    harness.private_agent(harness.user_id("assign_manager"), "manager-private")
    yield harness
    harness.close()


def _create_assign_connection(web, name="ERP 指派"):
    # ERP (unlike OA) is not a per-tenant singleton, so each test gets its own
    # connection and the fixtures never fight over "already exists".
    created = web.post("/api/external-connections/tenant", {
        "kind": "erp", "name": name, "config": ERP_RFC,
        "secrets": {"password": SECRET},
    }, token=web.manager_token)
    assert created.status == "200 OK", created.data
    return _json(created)["id"]


def _assign(web, connection_id, action, token=None):
    return web.get("/api/external-connections/tenant/%s/%s"
                   % (connection_id, action), token=token or web.admin_token)


def _save(web, connection_id, body, token=None, **kwargs):
    return web.post("/api/external-connections/tenant/%s/agent-assignments"
                    % connection_id, body, token=token or web.admin_token,
                    **kwargs)


def test_assignment_read_is_a_tenant_read(assign_web):
    connection_id = _create_assign_connection(assign_web, "ERP 读权限")
    refused = _assign(assign_web, connection_id, "agent-assignments",
                      token=assign_web.nobody_token)
    assert refused.status == "403 Forbidden"
    # The same read is open to a member who holds the tenant read permission.
    assert _assign(assign_web, connection_id, "agent-assignments",
                   token=assign_web.reader_token).status.startswith("200")


def test_assignment_save_needs_the_manage_permission(assign_web):
    connection_id = _create_assign_connection(assign_web, "ERP 写权限")
    refused = _save(assign_web, connection_id,
                    {"expected_revision": 1, "add_agent_ids": ["agent-a"]},
                    token=assign_web.reader_token)
    assert refused.status == "403 Forbidden"


def test_assignment_save_applies_the_origin_guard(assign_web):
    connection_id = _create_assign_connection(assign_web, "ERP 跨域")
    refused = _save(assign_web, connection_id,
                    {"expected_revision": 1, "add_agent_ids": ["agent-a"]},
                    headers={"Origin": "http://evil.example.com"})
    assert refused.status == "403 Forbidden"
    assert _json(refused)["code"] == "csrf_failed"


def test_assignment_round_trips_over_http(assign_web):
    connection_id = _create_assign_connection(assign_web, "ERP 往返")

    fresh = _json(_assign(assign_web, connection_id, "agent-assignments"))
    assert fresh["configured"] is True and fresh["revision"] == 1
    assert fresh["total"] == 0 and fresh["can_assign"] is True

    saved = _save(assign_web, connection_id,
                  {"expected_revision": fresh["revision"],
                   "add_agent_ids": ["agent-a"]})
    assert saved.status == "200 OK", saved.data
    body = _json(saved)
    assert body["added"] == 1 and body["revision"] == 2 and body["configured"]

    listed = _json(_assign(assign_web, connection_id, "agent-assignments"))
    assert [item["id"] for item in listed["items"]] == ["agent-a"]
    item = listed["items"][0]
    assert item["assigned"] is True and item["manageable"] is True
    assert item["visibility"] == "tenant"

    # A read-only holder sees the same row but is told it cannot change it.
    read_only = _json(_assign(assign_web, connection_id, "agent-assignments",
                              token=assign_web.reader_token))
    assert read_only["can_assign"] is False
    assert read_only["items"][0]["assigned"] is True

    removed = _json(_save(assign_web, connection_id,
                          {"expected_revision": body["revision"],
                           "remove_agent_ids": ["agent-a"]}))
    assert removed["removed"] == 1
    assert _json(_assign(assign_web, connection_id,
                         "agent-assignments"))["total"] == 0


def test_a_connection_manager_sees_but_cannot_assign_a_shared_agent(assign_web):
    """Connection management is not Agent management (spec: 可见但不可管理)."""
    connection_id = _create_assign_connection(assign_web, "ERP 可见不可管理")
    listed = _json(_assign(assign_web, connection_id, "agent-assignments",
                           token=assign_web.manager_token))
    assert listed["can_assign"] is True  # they may manage the connection
    assign_web.private_agent(assign_web.user_id("assign_manager"), "manager-only")

    candidates = _json(assign_web.get(
        "/api/external-connections/tenant/%s/agent-candidates?q=agent"
        % connection_id, token=assign_web.manager_token))
    shared = [i for i in candidates["items"] if i["id"] == "agent-a"][0]
    assert shared["manageable"] is False

    refused = _save(assign_web, connection_id,
                    {"expected_revision": 1, "add_agent_ids": ["agent-a"]},
                    token=assign_web.manager_token)
    assert refused.status == "403 Forbidden"
    assert _json(refused)["code"] == "agent_not_assignable"
    # Their own private Agent is theirs to assign, so the rule is per-target.
    own = _save(assign_web, connection_id,
                {"expected_revision": 1, "add_agent_ids": ["manager-only"]},
                token=assign_web.manager_token)
    assert own.status == "200 OK", own.data


def test_assignment_does_not_expire_the_connection_version(assign_web):
    """Assignment has its own CAS token; the connection's If-Match is untouched."""
    connection_id = _create_assign_connection(assign_web, "ERP 版本")
    saved = _save(assign_web, connection_id,
                  {"expected_revision": 1, "add_agent_ids": ["agent-a"]})
    assert saved.status == "200 OK"
    updated = assign_web.post(
        "/api/external-connections/tenant/%s/update" % connection_id,
        {"expected_version": 1, "name": "ERP 版本改"},
        token=assign_web.manager_token)
    assert updated.status == "200 OK"
    assert _json(updated)["version"] == 2


def test_a_stale_assignment_revision_over_http_is_a_409(assign_web):
    connection_id = _create_assign_connection(assign_web, "ERP 冲突")
    stale = _save(assign_web, connection_id,
                  {"expected_revision": 99, "add_agent_ids": ["agent-a"]})
    assert stale.status == "409 Conflict"
    assert _json(stale)["code"] == "assignment_version_conflict"


def test_a_page_save_never_clears_rows_outside_the_request(assign_web):
    """The delta names only what changed: an unloaded page survives."""
    connection_id = _create_assign_connection(assign_web, "ERP 分页")
    _save(assign_web, connection_id,
          {"expected_revision": 1, "add_agent_ids": ["agent-a", "agent-b"]})
    first_page = _json(assign_web.get(
        "/api/external-connections/tenant/%s/agent-assignments?page=1&page_size=1"
        % connection_id, token=assign_web.admin_token))
    assert first_page["total"] == 2 and len(first_page["items"]) == 1
    removed = _json(_save(assign_web, connection_id,
                          {"expected_revision": first_page["revision"],
                           "remove_agent_ids": [first_page["items"][0]["id"]]}))
    assert removed["removed"] == 1
    rest = _json(_assign(assign_web, connection_id, "agent-assignments"))
    assert rest["total"] == 1
    assert rest["items"][0]["id"] != first_page["items"][0]["id"]


def test_candidate_search_reports_assigned_and_hides_private_agents(assign_web):
    connection_id = _create_assign_connection(assign_web, "ERP 候选")
    _save(assign_web, connection_id,
          {"expected_revision": 1, "add_agent_ids": ["agent-a"]})

    candidates = _json(assign_web.get(
        "/api/external-connections/tenant/%s/agent-candidates?q=agent"
        % connection_id, token=assign_web.admin_token))
    by_id = {item["id"]: item for item in candidates["items"]}
    assert by_id["agent-a"]["assigned"] is True
    assert by_id["agent-b"]["assigned"] is False
    assert "reader-private" not in by_id and "manager-private" not in by_id

    # Nobody but the owner sees a private Agent — not even the tenant admin.
    hidden = _json(assign_web.get(
        "/api/external-connections/tenant/%s/agent-candidates?q=private"
        % connection_id, token=assign_web.admin_token))
    assert hidden["items"] == []
    own = _json(assign_web.get(
        "/api/external-connections/tenant/%s/agent-candidates?q=private"
        % connection_id, token=assign_web.manager_token))
    assert [item["id"] for item in own["items"]] == ["manager-private"]


def test_an_invisible_private_agent_cannot_be_assigned(assign_web):
    connection_id = _create_assign_connection(assign_web, "ERP 私有")
    refused = _save(assign_web, connection_id,
                    {"expected_revision": 1,
                     "add_agent_ids": ["reader-private"]})
    assert refused.status == "403 Forbidden"
    assert _json(refused)["code"] == "agent_not_assignable"
    # The whole request is refused, never partially applied.
    assert _json(_assign(assign_web, connection_id,
                         "agent-assignments"))["total"] == 0


def test_candidate_search_requires_a_term(assign_web):
    connection_id = _create_assign_connection(assign_web, "ERP 空搜索")
    refused = _assign(assign_web, connection_id, "agent-candidates")
    assert refused.status == "400 Bad Request"
    assert _json(refused)["code"] == "field_required"


def test_the_path_not_the_body_addresses_the_connection(assign_web):
    """A body cannot re-point the write at another connection or tenant."""
    target = _create_assign_connection(assign_web, "ERP 路径")
    other = _create_assign_connection(assign_web, "ERP 其他")
    response = _save(assign_web, target,
                     {"expected_revision": 1, "add_agent_ids": ["agent-a"],
                      "connection_id": other, "tenant_id": "someone-else"})
    assert response.status == "200 OK"
    assert _json(_assign(assign_web, target, "agent-assignments"))["total"] == 1
    assert _json(_assign(assign_web, other, "agent-assignments"))["total"] == 0


def test_an_unknown_connection_is_a_404(assign_web):
    refused_list = _assign(assign_web, "conn-missing", "agent-assignments")
    assert refused_list.status == "404 Not Found"
    refused_search = _assign(assign_web, "conn-missing",
                             "agent-candidates").status
    assert refused_search == "400 Bad Request"  # an empty term is refused first
    with_term = assign_web.get(
        "/api/external-connections/tenant/conn-missing/agent-candidates?q=agent",
        token=assign_web.admin_token)
    assert with_term.status == "404 Not Found"


def test_an_over_limit_delta_is_refused(assign_web):
    connection_id = _create_assign_connection(assign_web, "ERP 超限")
    refused = _save(assign_web, connection_id,
                    {"expected_revision": 1,
                     "add_agent_ids": ["agent-%03d" % i for i in range(101)]})
    assert refused.status == "400 Bad Request"
    assert _json(refused)["code"] == "too_many_changes"


def test_a_tenant_assignment_address_without_a_tenant_header_is_refused(assign_web):
    connection_id = _create_assign_connection(assign_web, "ERP 无租户头")
    refused = assign_web.get(
        "/api/external-connections/tenant/%s/agent-assignments" % connection_id,
        token=assign_web.admin_token, tenant=False)
    assert refused.status in ("400 Bad Request", "403 Forbidden")


def test_an_object_of_another_scope_is_not_addressable(assign_web):
    """A personal mailbox id is not a tenant connection, and vice versa."""
    personal = _json(assign_web.post("/api/external-connections/personal", {
        "kind": "email", "name": "我的邮箱", "config": EMAIL_BOTH,
        "secrets": {"imap_password": SECRET, "smtp_password": SECRET},
    }, token=assign_web.admin_token))["id"]
    refused = _assign(assign_web, personal, "agent-assignments")
    assert refused.status == "404 Not Found"


def test_revoked_connection_manage_qualification_blocks_the_save(assign_web):
    """Loaded, then lost the connection qualification before saving."""
    role = assign_web.role("assign-temp", ["external.connections.read",
                                           "external.connections.manage",
                                           "chat.use"])
    assign_web.member("assign_temp", ["assign-temp"])
    token = assign_web.login("assign_temp")
    connection_id = _create_assign_connection(assign_web, "ERP 失权")

    assert _assign(assign_web, connection_id, "agent-assignments",
                   token=token).status.startswith("200")
    current = [r for r in assign_web.service.list_roles(assign_web.tenant_id)
               if r["code"] == "assign-temp"][0]
    assign_web.service.update_role(
        actor_user_id=assign_web.admin_id, tenant_id=assign_web.tenant_id,
        role_id=current["id"], name=current["name"],
        permissions=["external.connections.read", "chat.use"],
        expected_version=current["version"], resource_grants=[])
    refused = _save(assign_web, connection_id,
                    {"expected_revision": 1, "add_agent_ids": ["agent-a"]},
                    token=token)
    assert refused.status == "403 Forbidden"


def test_a_legacy_connection_is_configured_by_the_first_save(assign_web):
    """The pre-existing connection starts 沿用原权限 and locks on first save."""
    connection_id = _create_assign_connection(assign_web, "ERP 存量")
    assign_web.service._store.execute(
        "UPDATE external_connection_agent_assignment_sets SET configured=0"
        " WHERE tenant_id=? AND logical_connection_id=?",
        (assign_web.tenant_id, connection_id))

    legacy = _json(_assign(assign_web, connection_id, "agent-assignments"))
    assert legacy["configured"] is False and legacy["revision"] == 1

    # An empty delta is still an explicit "configure empty", not a no-op.
    saved = _json(_save(assign_web, connection_id, {"expected_revision": 1}))
    assert saved["configured"] is True and saved["revision"] == 2
    assert saved["added"] == 0 and saved["removed"] == 0
    after = _json(_assign(assign_web, connection_id, "agent-assignments"))
    assert after["configured"] is True and after["total"] == 0


def test_counts_never_include_an_invisible_assignment(assign_web):
    """A colleague's private assignment must not leak through the count."""
    role = assign_web.role("assign-private", ["external.connections.read",
                                              "external.connections.manage",
                                              "chat.use"])
    assign_web.member("assign_owner", ["assign-private"])
    owner_token = assign_web.login("assign_owner")
    assign_web.private_agent(assign_web.user_id("assign_owner"), "owner-private")
    connection_id = _create_assign_connection(assign_web, "ERP 隐藏计数")

    saved = _save(assign_web, connection_id,
                  {"expected_revision": 1, "add_agent_ids": ["owner-private"]},
                  token=owner_token)
    assert saved.status == "200 OK", saved.data

    own = _json(_assign(assign_web, connection_id, "agent-assignments",
                        token=owner_token))
    assert own["total"] == 1 and own["items"][0]["id"] == "owner-private"
    for token in (assign_web.reader_token, assign_web.admin_token):
        hidden = _json(_assign(assign_web, connection_id, "agent-assignments",
                               token=token))
        assert hidden["total"] == 0 and hidden["items"] == []
        search = _json(assign_web.get(
            "/api/external-connections/tenant/%s/agent-candidates?q=owner"
            % connection_id, token=token))
        assert search["items"] == []


def test_the_catalogue_card_carries_the_assignment_summary(assign_web):
    """The card entry the console renders needs no per-card assignment call."""
    connection_id = _create_assign_connection(assign_web, "ERP 卡片摘要")
    _save(assign_web, connection_id,
          {"expected_revision": 1, "add_agent_ids": ["agent-a", "agent-b"]})

    cards = _json(assign_web.get("/api/external-connections/catalog?scope=tenant",
                                 token=assign_web.reader_token))["items"]
    card = [c for c in cards if c["id"] == connection_id][0]
    assert card["agent_assignment"]["configured"] is True
    assert card["agent_assignment"]["visible_count"] == 2
    assert card["agent_assignment"]["can_assign"] is False  # read-only caller
    assert card["agent_assignment"]["revision"] == 2
