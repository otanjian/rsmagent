# encoding:utf-8
"""OA adapter: login probe, controlled actions and the OA tool provider.

Change ``add-external-system-access``, task group 7 (7.1-7.5) and the OA parts
of group 9 (9.4, 9.5, 9.7's ``outcome_unknown``). The behavioural reference is
OneAgent's ``channel/web/handlers/oa_connection.py``; these tests pin the
properties the OA spec makes non-negotiable, all through a stubbed HTTP layer so
no real OA server is needed:

* login and OpenAPI are separate capabilities — a working login without OpenAPI
  credentials is ``partial``, never ``ok``;
* each failure lands on a precise stage (network/tls/auth/protocol/policy/timeout);
* no secret, cookie or session id reaches a stage detail or probe metadata;
* the exact risk-catalogue action names are declared, and an undeclared action
  is refused;
* an interface that cannot serve an action is refused instead of approximated;
* a recipient that resolves to zero or several people is refused with the
  candidates, never guessed;
* a write whose response is lost is ``outcome_unknown`` and is not resent;
* the tool provider offers reads but not writes while the write class is closed.
"""

from __future__ import annotations

import json

import pytest

from integrations.external import registry
from integrations.external import risk as risk_module
from integrations.external.adapters import base as base_mod
from integrations.external.adapters.base import (
    AdapterError,
    Cancelled,
    ExecutionContext,
    STAGE_AUTH,
    STAGE_CONFIG,
    STAGE_NETWORK,
    STAGE_POLICY,
    STAGE_PROTOCOL,
    STAGE_TIMEOUT,
    STAGE_TLS,
)
from integrations.external.oa import transport as oa_transport
from integrations.external.oa.transport import HttpResult, TransportError

NODE = "https://oa.example.com"
PASSWORD = "P@ssw0rd-LOGIN-SECRET"
APP_SECRET = "APP-SECRET-VALUE"
SESSION_ID = "SESSIONID-VALUE"

OA_LOGIN_CONFIG = {"base_url": NODE, "username": "alice"}
OA_OPENAPI_CONFIG = {
    "base_url": NODE, "username": "alice", "app_key": "key-1",
    "corp_id": "corp-1",
}


# -- stubbed HTTP ------------------------------------------------------------

def _resp(status: int = 200, text: str = "", *, url: str = "",
          json_body=None, headers=None) -> HttpResult:
    if json_body is not None:
        text = json.dumps(json_body, ensure_ascii=False)
        headers = {"content-type": "application/json", **(headers or {})}
    return HttpResult(status_code=status, text=text, url=url,
                      headers=headers or {})


class ScriptedTransport:
    """A transport whose every answer is decided by the test's handler."""

    def __init__(self, handler, *, cookies=None) -> None:
        self._handler = handler
        self.cookie_jar = dict(cookies or {})
        self.calls = []

    # the PolicyTransport surface the client uses
    def get(self, url, **kw):
        return self._do("GET", url, kw)

    def post_form(self, url, data, **kw):
        return self._do("POST_FORM", url, {"data": data, **kw})

    def post_json(self, url, payload, **kw):
        return self._do("POST_JSON", url, {"payload": payload, **kw})

    def cookies(self):
        return dict(self.cookie_jar)

    def close(self):
        pass

    def _do(self, method, url, kw):
        self.calls.append({"method": method, "url": url, **kw})
        out = self._handler(self, method, url, kw)
        if isinstance(out, Exception):
            raise out
        return out

    def calls_to(self, fragment):
        return [call for call in self.calls if fragment in call["url"]]


@pytest.fixture
def install_transport(monkeypatch):
    def _install(handler, *, cookies=None):
        fake = ScriptedTransport(handler, cookies=cookies)
        monkeypatch.setattr(oa_transport, "open_transport", lambda ctx: fake)
        return fake
    return _install


# -- execution context -------------------------------------------------------

def _resolver(secrets):
    def resolve(slot):
        value = (secrets or {}).get(slot)
        if value:
            return value
        raise KeyError(slot)
    return resolve


def _context(config=None, *, secrets=None, **overrides) -> ExecutionContext:
    values = dict(
        kind=registry.KIND_OA, scope="tenant", tenant_id="tenant-a",
        owner_user_id=None, connection_id="conn-oa",
        config=dict(config or OA_LOGIN_CONFIG),
        secret_resolver=_resolver(
            {"password": PASSWORD} if secrets is None else secrets),
        config_version=3, secret_versions={}, actor_user_id="user-a",
        limits={},
    )
    values.update(overrides)
    return ExecutionContext(**values)


@pytest.fixture
def adapter():
    from integrations.external.adapters.oa import OaAdapter
    return OaAdapter()


# -- handlers ----------------------------------------------------------------

def _login_ok(t, method, url, kw):
    """Standard E9 login succeeds and a session cookie is issued."""
    if url.endswith("/login/Login.jsp") and method == "GET":
        return _resp(200, "<html><body>login</body></html>", url=url)
    if url.endswith("/VerifyLogin.jsp") and method == "POST_FORM":
        t.cookie_jar["JSESSIONID"] = SESSION_ID
        return _resp(200, '["1",""]', url=url)
    if url.endswith("/api/") and method == "GET":
        return _resp(200, '{"status":true}', url=url)
    return _resp(404, "not found", url=url)


def _openapi_ready(t, method, url, kw):
    if url.endswith("/papi/openapi/oauth2/authorize"):
        return _resp(200, json_body={"code": "AUTH-CODE"}, url=url)
    if url.endswith("/papi/openapi/oauth2/access_token") \
            and method == "POST_JSON":
        return _resp(200, json_body={"accessToken": "ACCESS-TOKEN"}, url=url)
    return _login_ok(t, method, url, kw)


def _login_rejected(t, method, url, kw):
    if url.endswith("/login/Login.jsp") and method == "GET":
        return _resp(200, "<html><body>login</body></html>", url=url)
    if url.endswith("/VerifyLogin.jsp") and method == "POST_FORM":
        return _resp(200, '["0",""]', url=url)
    return _resp(404, "not found", url=url)


def _password_ok_no_session(t, method, url, kw):
    if url.endswith("/login/Login.jsp") and method == "GET":
        return _resp(200, "<html><body>login</body></html>", url=url)
    if url.endswith("/VerifyLogin.jsp") and method == "POST_FORM":
        # Right password, but Passport validated it without a session.
        return _resp(200, '["1",""]', url=url)
    return _resp(403, "forbidden", url=url)


def _everything_forbidden(t, method, url, kw):
    return _resp(403, "forbidden", url=url)


def _e9_list_handler(t, method, url, kw):
    if url.endswith("/api/workflow/reqlist/splitPageKey"):
        return _resp(200, json_body={"sessionkey": "SK"}, url=url)
    if url.endswith("/api/ec/dev/table/datas"):
        return _resp(200, json_body={
            "datas": [{"requestid": "100",
                       "requestnamespan": "<span>差旅报销</span>",
                       "creator": "张三", "currentnodename": "部门审批"}],
            "pageSize": 20, "total": 1,
        }, url=url)
    return _login_ok(t, method, url, kw)


def _e9_read_handler(t, method, url, kw):
    if url.endswith("/api/workflow/reqform/loadForm"):
        return _resp(200, json_body={
            "status": True,
            "params": {"requestid": "100", "workflowid": "9", "nodeid": "5"},
        }, url=url)
    return _login_ok(t, method, url, kw)


def _e9_submit_handler(t, method, url, kw):
    if url.endswith("/api/workflow/reqform/rightMenu"):
        return _resp(200, json_body={
            "rightMenus": [{"menuName": "同意", "systemMenuType": "APPROVE"}]},
            url=url)
    if url.endswith("/api/workflow/reqform/requestOperation"):
        return _resp(200, json_body={"data": {"type": "SUCCESS"}}, url=url)
    return _e9_read_handler(t, method, url, kw)


def _e9_no_approve_menu(t, method, url, kw):
    if url.endswith("/api/workflow/reqform/rightMenu"):
        return _resp(200, json_body={
            "rightMenus": [{"menuName": "打印", "systemMenuType": "PRINT"}]},
            url=url)
    return _e9_read_handler(t, method, url, kw)


def _forward_handler(resources):
    def handler(t, method, url, kw):
        if url.endswith("/api/workflow/reqform/rightMenu"):
            return _resp(200, json_body={"rightMenus": [
                {"menuName": "转发", "systemMenuType": "FORWARD"},
                {"menuName": "转办", "systemMenuType": "TURN_TO"},
                {"menuName": "传阅", "systemMenuType": "CHUANYUE"},
                {"menuName": "加签", "systemMenuType": "REMARKADVICE"},
            ]}, url=url)
        if url.endswith("/api/public/browser/complete/17"):
            return _resp(200, json_body={"datas": resources}, url=url)
        if url.endswith("/api/workflow/reqform/signInput"):
            return _resp(200, json_body={
                "IsSubmitedOpinion": "1", "IsBeForwardTodo": "1"}, url=url)
        if url.endswith("/api/workflow/reqform/remarkOperate"):
            return _resp(200, json_body={"success": True}, url=url)
        return _e9_read_handler(t, method, url, kw)
    return handler


def _e10_ready(t, method, url, kw):
    if url.endswith("/api/baseserver/layout/baseTeams"):
        return _resp(200, json_body={
            "data": {"currentUser": {"id": "42"}}}, url=url)
    if url.endswith("/papi/openapi/workflow/v2/getInfoByID"):
        return _resp(200, json_body={
            "message": {"errcode": "0", "errmsg": "ok"},
            "data": {"id": "100", "status": "1"}}, url=url)
    return _openapi_ready(t, method, url, kw)


# ===========================================================================
# registration, declarations, validate_config
# ===========================================================================

def test_the_adapter_is_registered_for_oa(adapter):
    assert base_mod.adapter_available(registry.KIND_OA) is True
    assert isinstance(base_mod.adapter_for(registry.KIND_OA), type(adapter))
    assert adapter.kind == "oa"


def test_declared_actions_match_the_risk_catalogue_exactly(adapter):
    catalogue = {action for (kind, action) in risk_module.RISK_CATALOGUE
                 if kind == registry.KIND_OA}
    assert set(adapter.actions) == catalogue
    assert set(adapter.write_actions) == {
        "request.create", "request.submit", "request.reject",
        "request.forward", "request.circulate", "request.addsign",
        # Marking a 抄送 read changes what the user sees, so it is declared a
        # write rather than folding into a read as a hidden side effect.
        "cc.mark_read",
    }
    assert adapter.actions - adapter.write_actions == {
        "todos.list", "request.read", "request.flowlog", "request.related",
        "request.attachments",
    }


def test_validate_config_normalizes_a_pasted_login_url(adapter):
    out = adapter.validate_config(dict(OA_LOGIN_CONFIG,
                                       base_url=NODE + "/login/Login.jsp"))
    assert out["base_url"] == NODE
    assert out["username"] == "alice"


def test_validate_config_refuses_a_query_string_in_base_url(adapter):
    from integrations.external.errors import ExternalConnectionError
    with pytest.raises(ExternalConnectionError) as caught:
        adapter.validate_config({"base_url": NODE + "/?token=abc",
                                 "username": "alice"})
    assert caught.value.code == "field_invalid"


def test_validate_config_requires_corp_id_alongside_app_key(adapter):
    from integrations.external.errors import ExternalConnectionError
    with pytest.raises(ExternalConnectionError) as caught:
        adapter.validate_config(dict(OA_LOGIN_CONFIG, app_key="key-1"))
    assert caught.value.code == "field_invalid"
    with pytest.raises(ExternalConnectionError) as caught:
        adapter.validate_config(dict(OA_LOGIN_CONFIG, corp_id="corp-1"))
    assert caught.value.code == "field_invalid"


def test_validate_config_leaves_a_login_only_connection_saveable(adapter):
    # The OpenAPI fields are optional: a login-only connection must be saveable.
    assert adapter.validate_config(dict(OA_LOGIN_CONFIG))["base_url"] == NODE


# ===========================================================================
# probe: success, partial and each failure stage
# ===========================================================================

def test_probe_reports_partial_when_login_works_and_openapi_is_unconfigured(
        adapter, install_transport):
    install_transport(_login_ok)
    result = adapter.probe(_context(OA_LOGIN_CONFIG,
                                    secrets={"password": PASSWORD}))

    assert result.outcome == "partial"
    assert result.ok is False
    assert result.metadata["openapi_ready"] is False
    assert result.metadata["interface"] == "e9"
    assert [s.status for s in result.stages] == ["ok", "partial"]
    openapi = [s for s in result.stages if s.name == "openapi"][0]
    assert openapi.code == "openapi_not_configured"
    assert openapi.stage == STAGE_CONFIG


def test_probe_is_ok_when_login_and_openapi_both_succeed(
        adapter, install_transport):
    install_transport(_openapi_ready)
    result = adapter.probe(_context(
        OA_OPENAPI_CONFIG, secrets={"password": PASSWORD,
                                    "app_secret": APP_SECRET}))
    assert result.outcome == "ok"
    assert result.metadata["openapi_ready"] is True
    assert result.metadata["interface"] == "e10"


def test_probe_maps_an_unreachable_host_to_the_network_stage(
        adapter, install_transport):
    def handler(t, method, url, kw):
        raise TransportError("host unreachable", code="network_unreachable",
                             stage=STAGE_NETWORK, detail="dns lookup failed")
    install_transport(handler)
    result = adapter.probe(_context())
    assert result.outcome == "failed"
    assert result.failed_stage().stage == STAGE_NETWORK


def test_probe_maps_a_certificate_failure_to_the_tls_stage(
        adapter, install_transport):
    def handler(t, method, url, kw):
        raise TransportError("certificate verify failed", code="tls_failed",
                             stage=STAGE_TLS, detail="self signed certificate")
    install_transport(handler)
    result = adapter.probe(_context())
    assert result.failed_stage().stage == STAGE_TLS


def test_probe_maps_a_timeout_to_the_timeout_stage(adapter, install_transport):
    def handler(t, method, url, kw):
        raise TransportError("no answer", code="timeout", stage=STAGE_TIMEOUT)
    install_transport(handler)
    result = adapter.probe(_context())
    assert result.failed_stage().stage == STAGE_TIMEOUT


def test_probe_maps_a_blocked_target_to_the_policy_stage(
        adapter, install_transport):
    def handler(t, method, url, kw):
        raise TransportError("target not allowed", code="target_not_allowed",
                             stage=STAGE_POLICY)
    install_transport(handler)
    result = adapter.probe(_context())
    assert result.failed_stage().stage == STAGE_POLICY


def test_probe_maps_bad_credentials_to_the_auth_stage(adapter, install_transport):
    install_transport(_login_rejected)
    result = adapter.probe(_context())
    assert result.outcome == "failed"
    failed = result.failed_stage()
    assert failed.stage == STAGE_AUTH
    assert failed.code == "login_rejected"


def test_probe_distinguishes_a_verified_password_without_a_session(
        adapter, install_transport):
    # Passport validates the password but issues no session: the credential is
    # right, the interface is not. Reporting "wrong password" would be wrong.
    install_transport(_password_ok_no_session)
    result = adapter.probe(_context())
    failed = result.failed_stage()
    assert failed.stage == STAGE_AUTH
    assert failed.code == "session_not_established"


def test_probe_maps_an_unhandled_login_page_to_the_protocol_stage(
        adapter, install_transport):
    install_transport(_everything_forbidden)
    result = adapter.probe(_context())
    failed = result.failed_stage()
    assert failed.stage == STAGE_PROTOCOL
    assert failed.code == "login_endpoint_not_recognised"


def test_probe_reports_a_failed_openapi_token_after_a_good_login(
        adapter, install_transport):
    def handler(t, method, url, kw):
        if url.endswith("/papi/openapi/oauth2/authorize"):
            return _resp(200, json_body={"errcode": "40001",
                                         "errmsg": "invalid app"}, url=url)
        return _login_ok(t, method, url, kw)
    install_transport(handler)
    result = adapter.probe(_context(
        OA_OPENAPI_CONFIG, secrets={"password": PASSWORD,
                                    "app_secret": APP_SECRET}))
    assert result.outcome == "failed"
    failed = result.failed_stage()
    assert failed.name == "openapi"
    assert failed.stage == STAGE_AUTH


def test_probe_reports_a_missing_password_as_a_config_failure(adapter):
    ctx = _context(secrets={})
    result = adapter.probe(ctx)
    assert result.failed_stage().stage == STAGE_CONFIG


# -- redaction ---------------------------------------------------------------

def test_no_secret_cookie_or_session_id_reaches_the_result(
        adapter, install_transport):
    def handler(t, method, url, kw):
        if url.endswith("/login/Login.jsp") and method == "GET":
            return _resp(200, "<html>login</html>", url=url)
        if url.endswith("/VerifyLogin.jsp") and method == "POST_FORM":
            # A hostile/buggy remote echoing the submitted password back.
            t.cookie_jar["JSESSIONID"] = SESSION_ID
            return _resp(200, '["0","%s"]' % PASSWORD, url=url)
        return _resp(404, "not found", url=url)

    install_transport(handler)
    result = adapter.probe(_context(
        OA_OPENAPI_CONFIG, secrets={"password": PASSWORD,
                                    "app_secret": APP_SECRET}))
    serialized = json.dumps(result.as_dict(), ensure_ascii=False)
    assert PASSWORD not in serialized
    assert APP_SECRET not in serialized
    assert SESSION_ID not in serialized


def test_a_transport_detail_that_echoes_the_password_is_redacted(
        adapter, install_transport):
    def handler(t, method, url, kw):
        raise TransportError(
            "boom", code="network_unreachable", stage=STAGE_NETWORK,
            detail="POST %s/login/VerifyLogin.jsp?userpassword=%s failed"
                   % (NODE, PASSWORD))
    install_transport(handler)
    result = adapter.probe(_context())
    serialized = json.dumps(result.as_dict(), ensure_ascii=False)
    assert PASSWORD not in serialized
    # Positive proof that redaction ran, not that the value merely never
    # appeared: the echoed password was replaced, not dropped, in the detail.
    assert "•••" in serialized
    failure = result.failed_stage()
    assert failure.stage == STAGE_NETWORK


def test_the_redactor_masks_known_secrets_and_unknown_token_shapes():
    from integrations.external.oa.redact import Redactor

    redact = Redactor([PASSWORD, APP_SECRET, SESSION_ID])
    text = redact("userpassword=%s app_secret=%s JSESSIONID=%s"
                  % (PASSWORD, APP_SECRET, SESSION_ID))
    assert PASSWORD not in text
    assert APP_SECRET not in text
    assert SESSION_ID not in text
    # A token this attempt never supplied is still masked by shape (a remote
    # session key echoed back by a diagnostic, say).
    assert "REMOTE-ISSUED-KEY" not in redact("sessionkey=REMOTE-ISSUED-KEY")


# ===========================================================================
# capabilities
# ===========================================================================

@pytest.fixture
def readiness(monkeypatch):
    def _set(mapping):
        monkeypatch.setattr(registry, "_readiness_config", lambda: mapping)
    return _set


def test_capabilities_report_login_and_openapi_separately(adapter, readiness):
    readiness({})
    report = adapter.describe_capabilities(_context(OA_LOGIN_CONFIG))
    assert "read_execute" in report.classes
    assert report.actions["todos.list"] is True
    assert report.actions["request.read"] is True
    # No OpenAPI credentials: request.create stays shut with the reason.
    assert report.actions["request.create"] is False
    assert report.reasons["openapi"] == "openapi_not_configured"
    assert report.metadata["openapi_ready"] is False


def test_capabilities_keep_writes_closed_until_the_write_class_opens(
        adapter, readiness):
    readiness({})
    closed = adapter.describe_capabilities(_context(OA_OPENAPI_CONFIG))
    assert "write_execute" not in closed.classes
    assert closed.actions["request.submit"] is False
    assert closed.reasons["request.submit"] == "awaiting_oa_test_environment"

    readiness({"oa": {"read_execute": True, "write_execute": True}})
    opened = adapter.describe_capabilities(_context(
        OA_OPENAPI_CONFIG, secrets={"password": PASSWORD,
                                    "app_secret": APP_SECRET}))
    assert "write_execute" in opened.classes
    assert opened.actions["request.create"] is True
    assert opened.actions["request.forward"] is True


def test_capabilities_never_claim_writes_without_a_password(adapter, readiness):
    readiness({"oa": {"write_execute": True}})
    report = adapter.describe_capabilities(_context(
        OA_OPENAPI_CONFIG, secrets={"app_secret": APP_SECRET}))
    assert report.actions["request.submit"] is False
    assert report.reasons["request.submit"] == "secret_missing"


# ===========================================================================
# invoke: reads
# ===========================================================================

def test_todos_list_returns_real_rows_from_the_e9_surface(
        adapter, install_transport):
    install_transport(_e9_list_handler)
    result = adapter.invoke(_context(), "todos.list", {"view": "doing"})
    assert result.ok is True
    assert result.data["interface"] == "e9"
    assert result.data["items"][0]["requestId"] == "100"
    assert result.data["items"][0]["requestName"] == "差旅报销"


def test_todos_list_refuses_an_unknown_view(adapter):
    result = adapter.invoke(_context(), "todos.list", {"view": "bogus"})
    assert result.ok is False
    assert result.code == "invalid_view"
    assert result.stage == STAGE_CONFIG


def test_todos_list_refuses_a_view_the_openapi_surface_does_not_serve(
        adapter, install_transport):
    # E10 has no confirmed OpenAPI path for 我发起/抄送, so it is refused rather
    # than probed against the E9 endpoints an E10 site disables.
    install_transport(_e10_ready)
    ctx = _context(OA_OPENAPI_CONFIG,
                   secrets={"password": PASSWORD, "app_secret": APP_SECRET})
    result = adapter.invoke(ctx, "todos.list", {"view": "mine"})
    assert result.ok is False
    assert result.code == "unsupported_interface"
    assert result.data["supported_views"] == ["doing", "done"]


def test_todos_list_uses_the_openapi_surface_on_an_e10_site(
        adapter, install_transport):
    def handler(t, method, url, kw):
        if url.endswith("/papi/openapi/workflow/v2/getTodoData"):
            return _resp(200, json_body={
                "message": {"errcode": "0", "errmsg": "ok"},
                "requests": [{"id": "100", "name": "差旅报销", "serNum": "WF-1",
                              "creator": {"id": "42", "username": "张三"}}],
                "count": 1,
            }, url=url)
        return _e10_ready(t, method, url, kw)
    install_transport(handler)
    ctx = _context(OA_OPENAPI_CONFIG,
                   secrets={"password": PASSWORD, "app_secret": APP_SECRET})
    result = adapter.invoke(ctx, "todos.list", {"view": "doing"})
    assert result.ok is True
    assert result.data["interface"] == "e10"
    assert result.data["items"][0]["requestName"] == "差旅报销"
    assert result.data["items"][0]["creatorName"] == "张三"


def test_request_create_uses_the_openapi_surface(
        adapter, install_transport):
    def handler(t, method, url, kw):
        if url.endswith("/papi/openapi/workflow/v2/createRequest"):
            return _resp(200, json_body={
                "message": {"errcode": "0", "errmsg": "ok"},
                "data": {"requestId": "200"},
            }, url=url)
        return _e10_ready(t, method, url, kw)
    install_transport(handler)
    ctx = _context(OA_OPENAPI_CONFIG,
                   secrets={"password": PASSWORD, "app_secret": APP_SECRET})
    result = adapter.invoke(ctx, "request.create",
                            {"workflow_id": "9", "title": "采购申请",
                             "form_data": {"amount": 100}})
    assert result.ok is True
    assert result.data["interface"] == "e10"


def test_request_read_returns_the_form_and_its_common_params(
        adapter, install_transport):
    install_transport(_e9_read_handler)
    result = adapter.invoke(_context(), "request.read", {"request_id": "100"})
    assert result.ok is True
    assert result.data["params"]["workflowid"] == "9"


def test_request_read_requires_a_request_id(adapter):
    result = adapter.invoke(_context(), "request.read", {})
    assert result.ok is False
    assert result.code == "field_required"


def test_an_undeclared_action_is_refused(adapter):
    with pytest.raises(AdapterError) as caught:
        adapter.invoke(_context(), "request.delete", {})
    assert caught.value.code == "unsupported_action"


# ===========================================================================
# invoke: interface selection and remote actionable state
# ===========================================================================

def test_an_e9_only_action_is_refused_on_an_openapi_site(
        adapter, install_transport):
    install_transport(_e10_ready)
    ctx = _context(OA_OPENAPI_CONFIG,
                   secrets={"password": PASSWORD, "app_secret": APP_SECRET})
    result = adapter.invoke(ctx, "request.forward",
                            {"request_id": "100", "recipient": "张三",
                             "remark": "请代办"})
    assert result.ok is False
    assert result.code == "unsupported_interface"
    assert result.stage == STAGE_POLICY


def test_an_openapi_only_action_is_refused_on_a_login_only_site(
        adapter, install_transport):
    install_transport(_login_ok)
    result = adapter.invoke(_context(), "request.create",
                            {"workflow_id": "1", "title": "x"})
    assert result.ok is False
    assert result.code == "unsupported_interface"


def test_a_write_is_refused_when_the_remote_node_cannot_do_it(
        adapter, install_transport):
    install_transport(_e9_no_approve_menu)
    result = adapter.invoke(_context(), "request.submit",
                            {"request_id": "100", "remark": "同意"})
    assert result.ok is False
    assert result.code == "not_actionable"
    assert result.stage == STAGE_POLICY


def test_a_submit_is_refused_when_the_remote_node_reports_failure(
        adapter, install_transport):
    def handler(t, method, url, kw):
        if url.endswith("/api/workflow/reqform/requestOperation"):
            return _resp(200, json_body={
                "data": {"type": "FAIL", "messageInfo": {"title": "流程已归档"}}},
                url=url)
        return _e9_submit_handler(t, method, url, kw)
    install_transport(handler)
    result = adapter.invoke(_context(), "request.submit",
                            {"request_id": "100", "remark": "同意"})
    assert result.ok is False
    assert result.code == "operation_refused"


def test_a_submit_succeeds_on_the_e9_surface(adapter, install_transport):
    install_transport(_e9_submit_handler)
    result = adapter.invoke(_context(), "request.submit",
                            {"request_id": "100", "remark": "同意"})
    assert result.ok is True
    assert result.data["operation"] == "submit"


# ===========================================================================
# invoke: target ambiguity is refused
# ===========================================================================

def test_an_ambiguous_recipient_is_refused_with_the_candidates(
        adapter, install_transport):
    fake = install_transport(_forward_handler([
        {"id": "7", "name": "张三", "departmentname": "财务部"},
        {"id": "8", "name": "张三", "departmentname": "销售部"},
    ]))
    result = adapter.invoke(_context(), "request.forward",
                            {"request_id": "100", "recipient": "张三",
                             "remark": "请代办"})
    assert result.ok is False
    assert result.code == "target_ambiguous"
    assert result.stage == STAGE_POLICY
    # The candidates come back so the caller can choose, and nothing was sent.
    assert len((result.data or {}).get("candidates") or []) == 2
    assert fake.calls_to("remarkOperate") == []


def test_an_unknown_recipient_is_refused(adapter, install_transport):
    fake = install_transport(_forward_handler([]))
    result = adapter.invoke(_context(), "request.circulate",
                            {"request_id": "100", "recipient": "李四",
                             "remark": "请阅"})
    assert result.ok is False
    assert result.code == "target_not_found"
    assert fake.calls_to("remarkOperate") == []


def test_a_unique_recipient_is_used(adapter, install_transport):
    install_transport(_forward_handler([{"id": "7", "name": "张三"}]))
    result = adapter.invoke(_context(), "request.forward",
                            {"request_id": "100", "recipient": "张三",
                             "remark": "请代办"})
    assert result.ok is True
    assert result.data["recipients"] == ["7"]
    assert result.data["forward_kind"] == "forward"


def test_forward_refuses_a_missing_remark(adapter, install_transport):
    install_transport(_forward_handler([{"id": "7", "name": "张三"}]))
    result = adapter.invoke(_context(), "request.addsign",
                            {"request_id": "100", "recipient_ids": ["7"]})
    assert result.ok is False
    assert result.code == "remark_required"


# ===========================================================================
# invoke: outcome_unknown (task 9.7)
# ===========================================================================

def test_a_written_submit_whose_response_times_out_is_unknown_not_failed(
        adapter, install_transport):
    def handler(t, method, url, kw):
        if url.endswith("/api/workflow/reqform/requestOperation"):
            raise TransportError("no answer", code="timeout",
                                 stage=STAGE_TIMEOUT)
        return _e9_submit_handler(t, method, url, kw)
    fake = install_transport(handler)
    result = adapter.invoke(_context(), "request.submit",
                            {"request_id": "100", "remark": "同意"})
    assert result.ok is False
    assert result.outcome_unknown is True
    assert result.code == "outcome_unknown"
    assert result.stage == STAGE_TIMEOUT
    # The reconciliation hint says what to re-read...
    assert "request.read" in result.message
    assert "request_id" in result.message
    # ...and the write was attempted exactly once.
    assert len(fake.calls_to("requestOperation")) == 1


def test_a_submit_cancelled_mid_flight_is_unknown_not_failed(
        adapter, install_transport):
    def handler(t, method, url, kw):
        if url.endswith("/api/workflow/reqform/requestOperation"):
            raise Cancelled()
        return _e9_submit_handler(t, method, url, kw)
    fake = install_transport(handler)
    result = adapter.invoke(_context(), "request.submit",
                            {"request_id": "100", "remark": "同意"})
    assert result.outcome_unknown is True
    assert len(fake.calls_to("requestOperation")) == 1


def test_a_lost_openapi_submit_response_is_also_unknown(
        adapter, install_transport):
    def handler(t, method, url, kw):
        if url.endswith("/papi/openapi/api/workflow/core/paService/v1/submitRequest"):
            raise TransportError("no answer", code="timeout",
                                 stage=STAGE_TIMEOUT)
        return _e10_ready(t, method, url, kw)
    install_transport(handler)
    ctx = _context(OA_OPENAPI_CONFIG,
                   secrets={"password": PASSWORD, "app_secret": APP_SECRET})
    result = adapter.invoke(ctx, "request.submit",
                            {"request_id": "100", "remark": "同意"})
    assert result.outcome_unknown is True
    assert result.stage == STAGE_TIMEOUT


def test_a_read_timeout_is_a_plain_failure_not_unknown(
        adapter, install_transport):
    def handler(t, method, url, kw):
        if url.endswith("/api/workflow/reqlist/splitPageKey"):
            raise TransportError("no answer", code="timeout",
                                 stage=STAGE_TIMEOUT)
        return _e9_list_handler(t, method, url, kw)
    install_transport(handler)
    result = adapter.invoke(_context(), "todos.list", {"view": "doing"})
    assert result.ok is False
    assert result.outcome_unknown is False
    assert result.stage == STAGE_TIMEOUT


# ===========================================================================
# service/runtime integration: the write class gate and the tool provider
# ===========================================================================

@pytest.fixture
def oa_stack(tmp_path, monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", "unit-test-master-key")
    from tests._helpers import build_identity, legacy_connection_rule
    from integrations.external.service import ExternalConnectionService
    from integrations.external.oa import store as oa_store

    stack = build_identity(tmp_path)
    service = ExternalConnectionService(stack.service)
    monkeypatch.setattr(oa_store, "_service", lambda: service)
    connection = service.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind=registry.KIND_OA, name="OA",
        config=dict(OA_LOGIN_CONFIG), secrets={"password": PASSWORD})
    # These tests are about the adapter, the transport and the runtime's other
    # gates, and they call with no trusted Agent context: keep the connection on
    # the pre-assignment 沿用原权限 rule so the refusal under test is the one the
    # test names.
    legacy_connection_rule(service, stack.tenant_id, connection["id"])
    return stack, service, connection


def test_runtime_refuses_a_write_while_the_write_class_is_closed(
        oa_stack, readiness):
    stack, service, connection = oa_stack
    readiness({"oa": {"read_execute": True}})
    result = service.invoke_action(
        connection["id"], "request.forward",
        {"request_id": "100", "recipient": "张三", "remark": "请代办"},
        actor_user_id=stack.root, tenant_id=stack.tenant_id)
    assert result.ok is False
    assert result.code == "execution_not_available"
    assert result.stage == STAGE_POLICY


def test_runtime_runs_a_read_action_through_the_adapter(
        oa_stack, readiness, install_transport):
    stack, service, connection = oa_stack
    readiness({"oa": {"read_execute": True}})
    install_transport(_e9_list_handler)
    result = service.invoke_action(
        connection["id"], "todos.list", {"view": "doing"},
        actor_user_id=stack.root, tenant_id=stack.tenant_id)
    assert result.ok is True
    assert result.data["items"][0]["requestId"] == "100"


def test_the_tool_provider_offers_reads_but_not_writes_when_closed(
        oa_stack, readiness):
    from integrations.external import tools as tools_module
    from integrations.external.adapters.oa import register_tools

    stack, service, connection = oa_stack
    register_tools()  # an earlier test may have reset the providers
    readiness({"oa": {"read_execute": True}})
    bindings = tools_module.available_tools(
        tenant_id=stack.tenant_id, actor_user_id=stack.root)
    oa_tools = [b for b in bindings if b.tool.kind == registry.KIND_OA]
    assert {b.tool.action for b in oa_tools} == {
        "todos.list", "request.read", "request.flowlog", "request.related",
        "request.attachments"}
    assert all(b.tool.write is False for b in oa_tools)
    assert {b.connection_id for b in oa_tools} == {connection["id"]}


def test_the_tool_provider_offers_writes_with_their_risk_level_when_open(
        oa_stack, readiness):
    from integrations.external import tools as tools_module
    from integrations.external.adapters.oa import register_tools

    stack, service, connection = oa_stack
    register_tools()
    readiness({"oa": {"read_execute": True, "write_execute": True}})
    bindings = tools_module.available_tools(
        tenant_id=stack.tenant_id, actor_user_id=stack.root)
    oa_tools = {b.tool.action: b for b in bindings
                if b.tool.kind == registry.KIND_OA}
    assert "request.submit" in oa_tools
    assert oa_tools["request.submit"].tool.write is True
    risk = oa_tools["request.submit"].tool.metadata["risk"]
    assert risk["level"] == "high"
    assert risk["approval_required"] is True
    assert oa_tools["request.forward"].tool.metadata["risk"]["level"] == "medium"
    # Marking a 抄送 read is a write but needs no human: the user's own action on
    # their own inbox, reversible and visible.
    assert oa_tools["cc.mark_read"].tool.write is True
    assert oa_tools["cc.mark_read"].tool.metadata["risk"] == {
        "kind": "oa", "action": "cc.mark_read", "level": "medium",
        "label_key": "risk_oa_cc_mark_read", "write": True, "reversible": True,
        "leaves_boundary": False, "approval_required": False,
        "acknowledged": True,
        "notes": "Mark a 抄送 as read. It removes the request from the user's "
                 "待阅 list, which is a change they can see — so it is its own "
                 "action rather than a side effect of reading the detail.",
    }


def test_the_tool_provider_offers_nothing_when_read_execute_is_closed(
        oa_stack, readiness):
    from integrations.external import tools as tools_module
    from integrations.external.adapters.oa import register_tools

    stack, service, connection = oa_stack
    register_tools()
    readiness({"oa": {"write_execute": True}})
    bindings = tools_module.available_tools(
        tenant_id=stack.tenant_id, actor_user_id=stack.root)
    assert [b for b in bindings if b.tool.kind == registry.KIND_OA] == []


def test_the_tool_provider_needs_a_tenant(oa_stack, readiness):
    from integrations.external.adapters.oa import oa_tool_provider
    stack, service, connection = oa_stack
    readiness({"oa": {"read_execute": True, "write_execute": True}})
    assert oa_tool_provider(None, stack.root) == []
