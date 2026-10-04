"""Offline identity contracts; no browser, SAP, gateway or account writes."""
from dataclasses import replace
import asyncio
import json

import pytest

from Scene.sap_workbench.backend.configuration import WorkbenchError
from Scene.sap_workbench.backend.identity import (
    VerifiedLoginBinding,
    VerifiedSapIdentity,
    ADT_IDENTITY_PATH,
    ADT_IDENTITY_ACCEPT,
    ServerSapIdentity,
    adt_identity_request,
    bind_verified_login,
    parse_adt_identity_response,
    relogin_plan,
    require_verified_login,
    require_same_sap_account,
    verify_configured_adt_identity,
    verify_system_status,
)


def evidence(**changes):
    return {"source": "system_status", "sysid": "DEV", "client": "001",
            "username": "ALICE", "revision": "revision-1", "epoch": 2,
            "login_generation": 3, **changes}


def verify(payload=None, **changes):
    context = {"expected_sysid": "DEV", "expected_client": "001", "expected_username": "ALICE",
               "revision": "revision-1", "epoch": 2, "login_generation": 3, **changes}
    return verify_system_status(evidence() if payload is None else payload, **context)


def binding(identity=None, **changes):
    args = {"config_version": 8, "credential_ref": "private-credential-handle",
            "mcp_connection_ids": {"sap-abap": "private-adt-id", "sap-pyrfc": "private-rfc-id"}, **changes}
    return bind_verified_login(verify() if identity is None else identity, **args)


def require(bound, identity=None, **changes):
    args = {"config_version": 8, "credential_ref": "private-credential-handle",
            "mcp_connection_ids": {"sap-abap": "private-adt-id", "sap-pyrfc": "private-rfc-id"}, **changes}
    return require_verified_login(bound, verify() if identity is None else identity, **args)


def test_exact_collector_identity_is_canonical_and_not_derived_from_expectations():
    identity = verify(evidence(sysid="dev", username="alice"), expected_sysid="dev", expected_username="alice")
    assert identity == VerifiedSapIdentity("DEV", "001", "ALICE", "revision-1", 2, 3)
    assert verify(evidence(username="BOB"), expected_username=None).username == "BOB"


@pytest.mark.parametrize("source", ["gateway_metadata", "registry", "config", "url", "title", "adt", "icf", "", None])
def test_registry_configuration_and_unestablished_sources_cannot_verify_sap(source):
    with pytest.raises(WorkbenchError, match="sap_identity_unverified"):
        verify(evidence(source=source))


@pytest.mark.parametrize("payload", [None, {}, "DEV/001/ALICE", {"sysid": "DEV", "client": "001", "username": "ALICE"}])
def test_missing_or_partial_proof_is_unverified_even_with_all_expected_values(payload):
    with pytest.raises(WorkbenchError, match="sap_identity_unverified"):
        verify_system_status(payload, expected_sysid="DEV", expected_client="001", expected_username="ALICE",
                             revision="revision-1", epoch=2, login_generation=3)


@pytest.mark.parametrize("field", ["password", "cookie", "token", "connection_id", "origin", "authenticated"])
def test_unknown_or_secret_bearing_fields_are_not_accepted_as_identity_contract(field):
    with pytest.raises(WorkbenchError) as error:
        verify(evidence(**{field: "test-private-marker"}))
    assert str(error.value) == "sap_identity_unverified"
    assert "test-private-marker" not in str(error.value)


@pytest.mark.parametrize("field,value", [
    ("sysid", "PRD"), ("client", "002"), ("username", "BOB"),
])
def test_wrong_verified_sap_identity_is_not_silently_rebound(field, value):
    with pytest.raises(WorkbenchError, match="sap_identity_mismatch"):
        verify(evidence(**{field: value}))


@pytest.mark.parametrize("field,value", [
    ("sysid", ""), ("sysid", "DEV123"), ("sysid", "D E"), ("client", 1),
    ("client", "1"), ("client", "01 "), ("client", "００１"),
    ("username", ""), ("username", "ALICE\n"), ("username", " ALICE"),
    ("username", "ß"), ("username", "A" * 65), ("username", {"name": "ALICE"}),
])
def test_identity_fields_have_explicit_bounded_shapes_without_coercion(field, value):
    with pytest.raises(WorkbenchError, match="sap_identity_invalid"):
        verify(evidence(**{field: value}))


@pytest.mark.parametrize("field,value", [
    ("revision", "old-revision"), ("epoch", 1), ("epoch", True), ("epoch", 2.0),
    ("login_generation", 2), ("login_generation", 3.0), ("login_generation", True),
])
def test_late_collector_responses_and_counter_type_confusion_fail(field, value):
    with pytest.raises(WorkbenchError, match="sap_identity_stale"):
        verify(evidence(**{field: value}))


@pytest.mark.parametrize("context", [
    {"epoch": -1}, {"epoch": True}, {"epoch": float("nan")},
    {"login_generation": 0}, {"login_generation": True}, {"login_generation": 1 << 63},
    {"revision": ""}, {"revision": "bad\nrevision"}, {"revision": "r" * 161},
    {"expected_sysid": "ConfiguredAlias"}, {"expected_client": 1}, {"expected_username": " ALICE"},
])
def test_trusted_context_still_requires_valid_types_and_actual_sid(context):
    with pytest.raises(WorkbenchError, match="sap_identity_invalid"):
        verify(**context)


def test_new_revision_is_valid_with_same_identity_epoch_and_login_generation():
    previous = binding()
    fresh = verify(evidence(revision="revision-2"), revision="revision-2")
    assert require(previous, fresh).identity.revision == "revision-2"
    assert previous.identity.revision == "revision-1"


@pytest.mark.parametrize("changes", [
    {"sysid": "PRD"}, {"client": "002"}, {"username": "BOB"},
    {"epoch": 3}, {"login_generation": 4},
])
def test_account_login_or_control_changes_revoke_old_binding(changes):
    fresh = replace(verify(), **changes)
    with pytest.raises(WorkbenchError, match="sap_login_changed"):
        require(binding(), fresh)


@pytest.mark.parametrize("changes", [
    {"config_version": 9}, {"credential_ref": "rotated-credential"},
    {"mcp_connection_ids": {"sap-abap": "new-connection"}},
    {"mcp_connection_ids": {"sap-abap": "private-adt-id", "sap-pyrfc": "new-rfc-connection"}},
])
def test_password_rotation_config_clear_or_connection_replacement_rejects_old_use(changes):
    with pytest.raises(WorkbenchError, match="sap_identity_binding_changed"):
        require(binding(), **changes)


def test_lost_evidence_cannot_reuse_last_verified_login():
    with pytest.raises(WorkbenchError, match="sap_identity_unverified"):
        require_verified_login(binding(), None, config_version=8, credential_ref="private-credential-handle",
                               mcp_connection_ids={"sap-abap": "private-adt-id"})


def test_binding_copies_private_connection_handles_and_does_not_project_them():
    handles = {"sap-abap": "private-adt-id", "sap-pyrfc": "private-rfc-id"}
    bound = binding(mcp_connection_ids=handles)
    handles["sap-abap"] = "caller-mutated-id"
    assert dict(bound.mcp_connection_ids)["sap-abap"] == "private-adt-id"
    assert bound.status() == {"browser_identity_verified": True, "login_generation": 3,
                              "mcp_credential_source": "scene_config", "mcp_identity_verified": False}
    for marker in ("private-credential-handle", "private-adt-id", "private-rfc-id", "ALICE"):
        assert marker not in str(bound.status())
    for marker in ("private-credential-handle", "private-adt-id", "private-rfc-id"):
        assert marker not in repr(bound)
    assert require(bound, mcp_connection_ids={"sap-pyrfc": "private-rfc-id", "sap-abap": "private-adt-id"})


@pytest.mark.parametrize("changes", [
    {"identity": {"sysid": "DEV", "client": "001", "username": "ALICE"}},
    {"config_version": 0}, {"config_version": True}, {"credential_ref": ""},
    {"credential_ref": "handle\nsecret"}, {"credential_ref": "x" * 257},
    {"mcp_connection_ids": {}}, {"mcp_connection_ids": {"generic-playwright": "private-id"}},
    {"mcp_connection_ids": {"sap-abap": ""}}, {"mcp_connection_ids": "private-connection"},
    {"mcp_connection_ids": {False: "private-connection"}},
])
def test_private_binding_shape_is_closed(changes):
    with pytest.raises(WorkbenchError):
        binding(**changes)


def test_direct_binding_rejects_duplicate_gateway_entries():
    with pytest.raises(WorkbenchError, match="sap_identity_binding_invalid"):
        VerifiedLoginBinding(verify(), 8, "private-ref", (("sap-abap", "a"), ("sap-abap", "b")))


def test_explicit_relogin_plan_pauses_revokes_and_retains_configured_mcp_credentials():
    assert relogin_plan(3) == {"login_generation": 4, "control": "manual", "browser_identity_verified": False,
                              "revoke_mcp_connections": True, "mcp_credential_source": "scene_config",
                              "credential_transfer": False}
    assert relogin_plan(0)["login_generation"] == 1


@pytest.mark.parametrize("value", [-1, True, "3", 3.0, None, 1 << 63])
def test_relogin_generation_is_not_caller_coerced(value):
    with pytest.raises(WorkbenchError, match="sap_identity_invalid"):
        relogin_plan(value)


def test_generation_exhaustion_cannot_wrap_to_an_old_login():
    with pytest.raises(WorkbenchError, match="sap_identity_generation_exhausted"):
        relogin_plan((1 << 63) - 1)


def server_response(body=None, **changes):
    context = {"status": 200, "content_type": "application/json", "expected_origin": "https://sap.example:44300",
               "response_url": "https://sap.example:44300" + ADT_IDENTITY_PATH + "?sap-client=001",
               "expected_sysid": "DEV", "expected_client": "001", "expected_username": "ALICE", **changes}
    if body is None:
        body = json.dumps({"systemID": "DEV", "client": "001", "userName": "ALICE", "release": "test-version"})
    return parse_adt_identity_response(body, **context)


def test_fixed_adt_request_and_separate_server_identity_contract():
    assert adt_identity_request("001") == {"method": "GET", "path": ADT_IDENTITY_PATH,
        "query": {"sap-client": "001"}, "headers": {"Accept": ADT_IDENTITY_ACCEPT}}
    actual = server_response(content_type=ADT_IDENTITY_ACCEPT + "; charset=utf-8")
    assert actual == ServerSapIdentity("DEV", "001", "ALICE")
    assert type(actual) is not VerifiedSapIdentity
    assert require_same_sap_account(verify(), actual)


@pytest.mark.parametrize("changes", [
    {"status": 401}, {"status": 302}, {"status": True}, {"content_type": "text/html"},
    {"content_type": None}, {"response_url": "https://other.example" + ADT_IDENTITY_PATH},
    {"response_url": "https://sap.example:44300/sap/bc/adt/discovery"},
    {"response_url": "https://sap.example:44300" + ADT_IDENTITY_PATH + "?sap-client=002"},
    {"response_url": "https://sap.example:44300" + ADT_IDENTITY_PATH + "?sap-client=001&sap-client=001"},
    {"response_url": "https://sap.example:44300" + ADT_IDENTITY_PATH + "?password=private-marker"},
    {"response_url": "http://sap.example:44300" + ADT_IDENTITY_PATH},
    {"response_url": "https://user:private-marker@sap.example:44300" + ADT_IDENTITY_PATH},
    {"response_url": "https://sap.example:44300" + ADT_IDENTITY_PATH + "#fragment"},
    {"response_url": "https://sap.example:0" + ADT_IDENTITY_PATH},
    {"expected_origin": "https://sap.example:44300/not-an-origin"},
])
def test_http_report_is_not_accepted_from_login_error_redirect_or_another_target(changes):
    with pytest.raises(WorkbenchError) as error:
        server_response(**changes)
    assert str(error.value) == "sap_identity_unverified"
    assert "private-marker" not in str(error.value)


@pytest.mark.parametrize("body", [
    "{}", "[]", "not-json", "<html>login</html>",
    '{"user":"ALICE","client":"001","system":"DEV"}',
    '{"systemID":"DEV","client":"001"}',
    '{"systemID":"DEV","client":"001","userName":"ALICE","client":"002"}',
    '{"systemID":"DEV","client":"001","userName":"ALICE","other":NaN}',
    " " * 16385, b"\xff", {"systemID": "DEV", "client": "001", "userName": "ALICE"},
])
def test_registry_shaped_malformed_duplicate_or_unbounded_server_body_is_rejected(body):
    with pytest.raises(WorkbenchError, match="sap_identity_unverified"):
        server_response(body)


@pytest.mark.parametrize("field,value", [("systemID", "PRD"), ("client", "002"), ("userName", "BOB")])
def test_actual_http_server_identity_must_match_configured_three_values(field, value):
    report = {"systemID": "DEV", "client": "001", "userName": "ALICE", field: value}
    with pytest.raises(WorkbenchError, match="sap_identity_mismatch"):
        server_response(json.dumps(report))


def test_mcp_registry_or_different_browser_cannot_replace_independently_verified_account():
    with pytest.raises(WorkbenchError, match="sap_identity_unverified"):
        require_same_sap_account(verify(), {"user": "ALICE", "client": "001"})
    with pytest.raises(WorkbenchError, match="sap_identity_mismatch"):
        require_same_sap_account(replace(verify(), username="BOB"), server_response())


class Probe:
    """Actual httpx request pipeline with an in-memory transport only."""
    def __init__(self, *, status=200, report=None, headers=None, chunks=None, delay=0, error=None):
        import httpx
        self.requests, self.options, self.clients, self.streams = [], [], [], []
        self.status, self.headers, self.delay, self.error = status, headers or {}, delay, error
        self.parts = chunks or [json.dumps(report or {"systemID": "DEV", "client": "001", "userName": "ALICE"}).encode()]
        outer = self

        class Stream(httpx.AsyncByteStream):
            def __init__(self):
                self.closed = False
                outer.streams.append(self)

            async def __aiter__(self):
                for part in outer.parts:
                    if outer.delay:
                        await asyncio.sleep(outer.delay)
                    yield part

            async def aclose(self):
                self.closed = True

        async def handler(request):
            outer.requests.append(request)
            if outer.error:
                raise outer.error
            return httpx.Response(outer.status, headers={"content-type": "application/json", **outer.headers}, stream=Stream())

        self.transport = httpx.MockTransport(handler)

    def factory(self, **options):
        import httpx
        self.options.append(options)
        client = httpx.AsyncClient(transport=self.transport, **options)
        self.clients.append(client)
        return client

    async def call(self, *, url="https://sap.example:44300/webgui?sap-client=001", **changes):
        return await verify_configured_adt_identity(url, sysid="DEV", client="001", username="ALICE",
            password="test-only-private-password", client_factory=self.factory, **changes)


@pytest.mark.parametrize("url,verify_tls", [
    ("https://sap.example:44300/webgui?sap-client=001", True),
    ("https://sap.goodsap.cn:44300/sap/bc/gui/sap/its/webgui/?sap-client=001", False),
    ("https://sap.goodsap.cn:44301/webgui", True),
    ("https://sap.goodsap.cn/webgui", True),
])
def test_probe_sends_only_fixed_get_and_auth_to_bound_origin_with_existing_tls_rule(url, verify_tls):
    async def run():
        import base64
        probe = Probe()
        assert await probe.call(url=url) == ServerSapIdentity("DEV", "001", "ALICE")
        assert len(probe.requests) == 1
        request = probe.requests[0]
        assert request.method == "GET" and request.url.path == ADT_IDENTITY_PATH
        assert str(request.url.query, "ascii") == "sap-client=001"
        assert request.headers["accept"] == ADT_IDENTITY_ACCEPT
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["authorization"] == "Basic " + base64.b64encode(b"ALICE:test-only-private-password").decode()
        assert request.content == b""
        options = probe.options[0]
        assert options["verify"] is verify_tls
        assert options["trust_env"] is False and options["follow_redirects"] is False
        assert all(client.is_closed for client in probe.clients)
        assert all(stream.closed for stream in probe.streams)
    asyncio.run(run())


@pytest.mark.parametrize("status", [301, 302, 307, 401, 403, 500])
def test_probe_never_follows_redirect_or_reports_authenticated_on_error(status):
    async def run():
        probe = Probe(status=status, headers={"location": "https://outside.example/private-login"})
        with pytest.raises(WorkbenchError, match="sap_identity_unverified"):
            await probe.call()
        assert len(probe.requests) == 1 and probe.clients[0].is_closed
    asyncio.run(run())


@pytest.mark.parametrize("settings", [
    {"headers": {"content-encoding": "gzip"}},
    {"headers": {"content-type": "text/html"}}, {"chunks": [b"A" * 4096] * 5},
    {"report": {"systemID": "PRD", "client": "001", "userName": "ALICE"}},
])
def test_probe_rejects_unbounded_compressed_login_or_wrong_system_responses(settings):
    async def run():
        probe = Probe(**settings)
        with pytest.raises(WorkbenchError):
            await probe.call()
        assert len(probe.requests) == 1 and probe.clients[0].is_closed
        assert probe.streams[0].closed
    asyncio.run(run())


@pytest.mark.parametrize("timeout", [0, -1, True, 11, float("nan"), "10"])
def test_invalid_probe_budget_fails_before_constructing_client(timeout):
    async def run():
        probe = Probe()
        with pytest.raises(WorkbenchError, match="sap_identity_probe_invalid"):
            await probe.call(timeout=timeout)
        assert not probe.requests and not probe.clients
    asyncio.run(run())


def test_absolute_probe_deadline_covers_slow_chunked_body_and_closes_http():
    async def run():
        probe = Probe(chunks=[b" "] * 100, delay=0.005)
        with pytest.raises(WorkbenchError, match="sap_identity_probe_timeout"):
            await probe.call(timeout=0.015)
        assert probe.clients[0].is_closed and probe.streams[0].closed
    asyncio.run(run())


def test_probe_network_diagnostics_are_sanitized_and_caller_cancellation_is_preserved():
    async def run():
        import httpx
        probe = Probe(error=httpx.ConnectError("test-only-private-password in remote diagnostics"))
        with pytest.raises(WorkbenchError) as error:
            await probe.call()
        assert str(error.value) == "sap_identity_probe_failed" and error.value.__suppress_context__
        assert probe.clients[0].is_closed
        interrupted = Probe(chunks=[b" "] * 100, delay=0.02)
        task = asyncio.create_task(interrupted.call())
        while not interrupted.streams:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert interrupted.clients[0].is_closed and interrupted.streams[0].closed
    asyncio.run(run())


@pytest.mark.parametrize("url", [
    "http://sap.example/webgui", "https://user:private-marker@sap.example/webgui",
    "https://sap.example:0/webgui", "https://sap.example\\outside/webgui",
])
def test_invalid_probe_origin_fails_before_constructing_credential_consumer(url):
    async def run():
        probe = Probe()
        with pytest.raises(WorkbenchError, match="sap_identity_unverified"):
            await probe.call(url=url)
        assert not probe.requests and not probe.clients
    asyncio.run(run())


@pytest.mark.parametrize("slow", [False, True])
def test_probe_cleanup_is_bounded_and_failure_does_not_report_verified_success(monkeypatch, slow):
    import httpx
    import Scene.sap_workbench.backend.identity as module
    monkeypatch.setattr(module, "ADT_IDENTITY_CLOSE_TIMEOUT", 0.005)
    closed = []

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        url = "https://sap.example" + ADT_IDENTITY_PATH + "?sap-client=001"

        async def aiter_raw(self, chunk_size):
            yield b'{"systemID":"DEV","client":"001","userName":"ALICE"}'

        async def aclose(self):
            try:
                if slow:
                    await asyncio.sleep(100)
                else:
                    raise RuntimeError("private-marker cleanup diagnostics")
            finally:
                closed.append("response")

    class Http:
        def build_request(self, method, url, **options):
            return httpx.Request(method, url)

        async def send(self, request, **options):
            return Response()

        async def aclose(self):
            closed.append("client")

    async def run():
        with pytest.raises(WorkbenchError) as error:
            await verify_configured_adt_identity("https://sap.example/webgui", sysid="DEV", client="001",
                username="ALICE", password="test-only-password", client_factory=lambda **options: Http())
        assert str(error.value) == "sap_identity_probe_cleanup_failed"
        assert closed == ["response", "client"]
    asyncio.run(run())
