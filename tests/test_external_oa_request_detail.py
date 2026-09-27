# encoding:utf-8
"""OA 流程详情深化: 审批记录、关联流程、附件访问, and the 已读 side effect.

Change ``add-external-system-access``, task 7.4:

    实现流程详情、审批记录、获准关联流程与附件访问，遵循远端权限和本地对象范围；
    纯查询不隐式标记已读。

What each test pins, and why it is not a restatement of the code:

* **审批记录** is two remote calls and a paging cursor that belongs to the
  remote (``maxrequestlogid``). The tests use a two-page response, so a
  single-page implementation fails rather than passing on a one-row fixture.
* **关联流程** is discovered from the form's *visible* browser fields. The
  exclusion cases matter as much as the inclusion ones: 借款流程 is the same
  ``htmltype=3`` widget, and treating it as a related application would read a
  request that has nothing to do with the one asked for.
* **附件** is a list plus an explicitly requested fetch. The fetch is refused for
  a link another host serves — the adapter is not a proxy — and refused when no
  directory is configured, because an "helpful" default would be an arbitrary
  write.
* **纯查询不隐式标记已读** is asserted by *counting remote calls*: reading a
  request must not issue a ``doReadIt`` at all, and the mark must travel only
  through the declared write action.

All through the same stubbed transport the rest of the OA suite uses, so no real
OA server is needed; the real-machine half of 7.4's parent group is 7.6.
"""

from __future__ import annotations

import json
import os

import pytest

from integrations.external import registry
from integrations.external.adapters.base import STAGE_POLICY, STAGE_PROTOCOL
from integrations.external.oa import actions as actions_module
from integrations.external.oa import transport as oa_transport
from integrations.external.oa.transport import HttpResult

NODE = "https://oa.example.com"
PASSWORD = "P@ssw0rd-LOGIN-SECRET"
SESSION_ID = "SESSIONID-VALUE"

OA_LOGIN_CONFIG = {"base_url": NODE, "username": "alice"}


def _resp(status: int = 200, text: str = "", *, url: str = "",
          json_body=None, headers=None, body: bytes = None) -> HttpResult:
    if json_body is not None:
        text = json.dumps(json_body, ensure_ascii=False)
        headers = {"content-type": "application/json", **(headers or {})}
    return HttpResult(status_code=status, text=text, url=url,
                      headers=headers or {},
                      body=body if body is not None else text.encode("utf-8"))


class ScriptedTransport:
    """A transport whose every answer is decided by the test's handler."""

    def __init__(self, handler, *, cookies=None) -> None:
        self._handler = handler
        self.cookie_jar = dict(cookies or {})
        self.calls = []

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


# -- login -------------------------------------------------------------------

def _login_ok(t, method, url, kw):
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


# -- form fixtures -----------------------------------------------------------

#: A main table with a file field and one *related* browser field, plus an
#: unrelated loan browser that must not be followed.
FORM = {
    "status": True,
    "params": {"requestid": "100", "workflowid": "9", "nodeid": "5",
               "f_weaver_belongto_userid": "77",
               "signatureAttributesStr": "SIG", "authStr": "AUTH"},
    "tableInfo": {
        "main": {"fieldinfomap": {
            "1": {"fieldid": "1", "fieldlabel": "合同附件",
                  "fieldname": "htfj"},
            "7": {"fieldid": "7", "fieldlabel": "关联事前申请",
                  "fieldname": "glsqsq", "htmltype": "3", "detailtype": "16"},
            "8": {"fieldid": "8", "fieldlabel": "借款流程",
                  "fieldname": "jklc", "htmltype": "3"},
            "9": {"fieldid": "9", "fieldlabel": "费用金额",
                  "fieldname": "fyje"},
        }},
        "detail_1": {"fieldinfomap": {
            "3": {"fieldid": "3", "fieldlabel": "关联出差申请",
                  "fieldname": "glccsq", "htmltype": "3", "detailtype": "161"},
        }},
    },
    "maindata": {
        "field1": {"specialobj": {"filedatas": [
            {"filename": "合同.pdf", "filesize": "1024",
             "fileExtendName": "pdf", "uploaddate": "2026-01-02",
             "fileid": "f-1", "loadlink": "/weaver/weaver.file.FileDownload"
                                          "?fileid=f-1",
             "filelink": NODE + "/spa/document/index.jsp?id=f-1"},
            # Another host: the adapter must not become a fetcher for it.
            {"filename": "外部.pdf", "filesize": "20", "fileid": "f-9",
             "loadlink": "https://files.example.net/外部.pdf"},
        ]}},
        "field7": {"specialobj": {"id": "501", "name": "2026年差旅事前申请"}},
        "field8": {"specialobj": {"id": "888", "name": "借款单8001"}},
        "field9": {"value": "1200.00"},
    },
}

DETAIL_DATA = {
    "detail_1": {"rowDatas": {"0": {"field3": {
        "specialobj": {"id": "502", "name": "2026年3月出差申请"},
    }}}},
}

RELATED_DETAILS = {
    "501": {"requestid": "501", "workflowid": "11",
            "f_weaver_belongto_userid": "77"},
    "502": {"requestid": "502", "workflowid": "12",
            "f_weaver_belongto_userid": "77"},
}


def _form_handler(t, method, url, kw):
    """The E9 surface: loadForm, detailData, log base/list, attachments, mark."""
    if url.endswith("/api/workflow/reqform/loadForm"):
        rid = str((kw.get("data") or {}).get("requestid") or "")
        if rid == "100":
            return _resp(200, json_body=FORM, url=url)
        if rid in RELATED_DETAILS:
            return _resp(200, json_body={"status": True,
                                         "params": RELATED_DETAILS[rid]},
                         url=url)
        # A related target the session was never allowed to read.
        return _resp(200, json_body={"status": False, "msg": "无权限查看该流程"},
                     url=url)
    if url.endswith("/api/workflow/reqform/detailData"):
        return _resp(200, json_body=DETAIL_DATA, url=url)
    if url.endswith("/api/workflow/reqform/getRequestLogBaseInfo"):
        # Not a paging response: the cursor belongs to the *list* call, and a
        # fixture that seeded it here would hide a walk that never asked the
        # list for its next key.
        return _resp(200, json_body={
            "api_status": True, "logpagesize": "2", "orderbytype": "0",
        }, url=url)
    if url.endswith("/api/workflow/reqform/getRequestLogList"):
        # The cursor is the remote's own ``maxrequestlogid``: a caller that
        # ignores it sees only page one, so this fixture makes that visible.
        cursor = str((kw.get("data") or {}).get("maxrequestlogid") or "0")
        if cursor in ("0", ""):
            return _resp(200, json_body={
                "api_status": True, "totalCount": 2, "maxrequestlogid": "12",
                "loglist": [
                    {"logid": "12", "operationname": "提交", "log_nodename": "起草",
                     "displayname": "张三", "displaydepname": "财务部",
                     "log_operatedate": "2026-01-02",
                     "log_operatetime": "10:00:00",
                     "log_remarkHtml": "<p>提交审批<br/>请尽快</p>"},
                ]}, url=url)
        return _resp(200, json_body={
            "api_status": True, "totalCount": 2, "maxrequestlogid": "",
            "loglist": [
                {"logid": "20", "operationname": "同意", "log_nodename": "部门审批",
                 "displayname": "李四", "displaydepname": "业务部",
                 "log_operatedate": "2026-01-03", "log_operatetime": "09:30:00",
                 "log_remarkHtml": "同意",
                 "displaybyagentname": "王五"},
            ]}, url=url)
    if "/weaver/weaver.file.FileDownload" in url:
        # Matched by fragment: the link carries a query string, so a suffix
        # match on the path alone would miss it — which is exactly the kind of
        # mistake a fixture can hide.
        return _resp(200, "PDF-BYTES", url=url,
                     headers={"content-type": "application/pdf"})
    if url.endswith("/api/workflow/reqlist/doReadIt"):
        return _resp(200, json_body={"api_status": True}, url=url)
    return _login_ok(t, method, url, kw)


def _log_refused_handler(t, method, url, kw):
    if url.endswith("/api/workflow/reqform/getRequestLogBaseInfo"):
        return _resp(200, json_body={"api_status": False, "msg": "无权限"},
                     url=url)
    return _form_handler(t, method, url, kw)


# -- stack -------------------------------------------------------------------

@pytest.fixture
def readiness(monkeypatch):
    def _set(mapping):
        monkeypatch.setattr(registry, "_readiness_config", lambda: mapping)
    return _set


@pytest.fixture
def oa_stack(tmp_path, monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", "unit-test-master-key")
    # ``attachments.fetch`` writes through the mail guard, whose root comes from
    # the resolved limits; pointing it at the test's tmp_path keeps the test off
    # the developer's real workspace.
    monkeypatch.setattr("config.conf", lambda: {"agent_workspace": str(tmp_path)})
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
    legacy_connection_rule(service, stack.tenant_id, connection["id"])
    return stack, service, connection


def _configure_attachment_dirs(service, stack, connection,
                               dirs=("downloads",)):
    """Give the connection somewhere to write, at a new config version.

    The directory is relative on purpose: the guard resolves it against the
    workspace and refuses anything that resolves outside it, so a test that
    passed an absolute path would be testing a different rule.
    """
    updated = service.update_connection(
        actor_user_id=stack.root, scope="tenant",
        connection_id=connection["id"], tenant_id=stack.tenant_id,
        expected_version=connection["version"],
        config=dict(OA_LOGIN_CONFIG, attachment_dirs=list(dirs)))
    connection["version"] = updated["version"]
    return updated


def _invoke(service, stack, connection, action, params):
    return service.invoke_action(
        connection["id"], action, params,
        actor_user_id=stack.root, tenant_id=stack.tenant_id)


@pytest.fixture
def read_open(readiness):
    readiness({"oa": {"read_execute": True}})


# ===========================================================================
# 审批记录
# ===========================================================================

def test_the_flow_log_follows_the_remotes_own_cursor(
        oa_stack, read_open, install_transport, tmp_path):
    """Both pages are read, and the walk follows ``maxrequestlogid``.

    A one-page implementation returns a single row here rather than silently
    passing, which is the property the remote's cursor exists for.
    """
    stack, service, connection = oa_stack
    fake = install_transport(_form_handler)
    result = _invoke(service, stack, connection, "request.flowlog",
                     {"request_id": "100"})
    assert result.ok is True, result.message
    assert [item["logId"] for item in result.data["flow_log"]] == ["12", "20"]
    assert result.data["totalCount"] == 2
    assert result.data["hasMore"] is False
    assert result.data["pages"] == 2
    assert len(fake.calls_to("getRequestLogList")) == 2


def test_the_flow_log_is_read_oldest_first_and_can_be_reversed(
        oa_stack, read_open, install_transport):
    stack, service, connection = oa_stack
    install_transport(_form_handler)
    ascending = _invoke(service, stack, connection, "request.flowlog",
                        {"request_id": "100"})
    assert [item["operatedAt"] for item in ascending.data["flow_log"]] == [
        "2026-01-02 10:00:00", "2026-01-03 09:30:00"]
    descending = _invoke(service, stack, connection, "request.flowlog",
                         {"request_id": "100", "order": "desc"})
    # ``desc`` is handed to the remote rather than reversed locally, so the
    # returned order is the remote's — and the cursor walk must still terminate.
    assert descending.data["order"] == "desc"
    assert descending.data["itemCount"] == 2


def test_a_flow_log_row_carries_the_opinion_without_the_html(
        oa_stack, read_open, install_transport):
    stack, service, connection = oa_stack
    install_transport(_form_handler)
    result = _invoke(service, stack, connection, "request.flowlog",
                     {"request_id": "100"})
    first = result.data["flow_log"][0]
    assert first["remark"] == "提交审批\n请尽快"
    assert "<" not in first["remark"]
    assert first["nodeLabel"] == "[起草/提交]"
    assert first["department"] == "财务部"
    # An opinion written on someone's behalf names them, so the trail does not
    # read as if the agent had decided personally.
    assert "王五" in result.data["flow_log"][1]["remark"]


def test_a_flow_log_refusal_is_a_policy_failure_not_an_empty_trail(
        oa_stack, read_open, install_transport):
    """An unreadable log is refused, never reported as "no approvals yet"."""
    stack, service, connection = oa_stack
    install_transport(_log_refused_handler)
    result = _invoke(service, stack, connection, "request.flowlog",
                     {"request_id": "100"})
    assert result.ok is False
    assert result.code == "remote_refused"
    assert result.stage == STAGE_POLICY


def test_the_log_walk_is_bounded_when_the_remote_never_stops(
        oa_stack, read_open, install_transport):
    """A cursor that never clears cannot loop past the declared page bound."""
    stack, service, connection = oa_stack

    def _never_ends(t, method, url, kw):
        if url.endswith("/api/workflow/reqform/getRequestLogList"):
            return _resp(200, json_body={
                "api_status": True, "totalCount": 999,
                "maxrequestlogid": "99",
                "loglist": [{"logid": "1", "operationname": "同意",
                             "log_operatedate": "2026-01-01",
                             "log_operatetime": "00:00:00"}]}, url=url)
        return _form_handler(t, method, url, kw)

    fake = install_transport(_never_ends)
    result = _invoke(service, stack, connection, "request.flowlog",
                     {"request_id": "100"})
    assert result.ok is True
    assert result.data["pages"] == actions_module.MAX_LOG_PAGES
    assert result.data["hasMore"] is True
    assert len(fake.calls_to("getRequestLogList")) == actions_module.MAX_LOG_PAGES


# ===========================================================================
# 关联流程
# ===========================================================================

def test_related_applications_are_discovered_from_main_and_detail_tables(
        oa_stack, read_open, install_transport):
    stack, service, connection = oa_stack
    install_transport(_form_handler)
    result = _invoke(service, stack, connection, "request.related",
                     {"request_id": "100"})
    assert result.ok is True, result.message
    assert [item["linkedRequestId"] for item in result.data["related"]] == [
        "501", "502"]
    assert {item["source"] for item in result.data["related"]} == {
        "main", "detail_1"}


def test_a_loan_browser_is_not_a_related_application(
        oa_stack, read_open, install_transport):
    """同是 ``htmltype=3`` 的借款流程 must not be followed as a related request."""
    stack, service, connection = oa_stack
    install_transport(_form_handler)
    result = _invoke(service, stack, connection, "request.related",
                     {"request_id": "100"})
    assert "888" not in {item["linkedRequestId"]
                         for item in result.data["related"]}


def test_related_details_are_read_only_when_asked_for(
        oa_stack, read_open, install_transport):
    """Listing the links is a read; reading each target is a second, opt-in one."""
    stack, service, connection = oa_stack
    fake = install_transport(_form_handler)
    listed = _invoke(service, stack, connection, "request.related",
                     {"request_id": "100"})
    assert listed.data["detailRequested"] is False
    assert all(item["detail"] is None for item in listed.data["related"])
    form_reads = len(fake.calls_to("loadForm"))

    read = _invoke(service, stack, connection, "request.related",
                   {"request_id": "100", "read_detail": True})
    assert read.data["detailRequested"] is True
    assert read.data["related"][0]["detailRead"] == "read"
    assert read.data["related"][0]["detail"]["requestid"] == "501"
    assert len(fake.calls_to("loadForm")) > form_reads


def test_a_related_target_the_session_may_not_read_is_recorded_not_fatal(
        oa_stack, read_open, install_transport):
    """远端权限 decides each target; one refusal must not hide the others."""
    stack, service, connection = oa_stack

    def _one_refused(t, method, url, kw):
        if url.endswith("/api/workflow/reqform/loadForm") \
                and str((kw.get("data") or {}).get("requestid")) == "501":
            return _resp(200, json_body={"status": False, "msg": "无权限查看该流程"},
                         url=url)
        return _form_handler(t, method, url, kw)

    install_transport(_one_refused)
    result = _invoke(service, stack, connection, "request.related",
                     {"request_id": "100", "read_detail": True})
    assert result.ok is True
    by_id = {item["linkedRequestId"]: item for item in result.data["related"]}
    assert by_id["501"]["detailRead"] == "refused"
    # The refusal text is the remote's own reason, so the caller can act on it.
    assert "无权限" in by_id["501"]["detailReason"]
    assert by_id["502"]["detailRead"] == "read"


def test_the_related_detail_read_is_bounded_and_says_so(
        oa_stack, read_open, install_transport):
    stack, service, connection = oa_stack
    install_transport(_form_handler)
    result = _invoke(service, stack, connection, "request.related",
                     {"request_id": "100", "read_detail": True, "max_items": 1})
    assert result.data["detailLimit"] == 1
    assert result.data["truncated"] is True
    flags = {item["linkedRequestId"]: item["withinLimit"]
             for item in result.data["related"]}
    assert flags == {"501": True, "502": False}
    # Over the limit is *reported as unread*, never returned as read-nothing.
    assert result.data["related"][1]["detailRead"] is None


# ===========================================================================
# 附件
# ===========================================================================

def test_attachments_are_listed_with_only_the_sites_own_links_fetchable(
        oa_stack, read_open, install_transport):
    stack, service, connection = oa_stack
    install_transport(_form_handler)
    result = _invoke(service, stack, connection, "request.attachments",
                     {"request_id": "100"})
    assert result.ok is True, result.message
    assert result.data["count"] == 2
    first, second = result.data["attachments"]
    assert first["fieldLabel"] == "合同附件"
    assert first["index"] == 1 and second["index"] == 2
    assert first["fetchable"] is True
    # Another host's link: reported as refused rather than quietly dropped, so
    # the caller can see the file exists and that the system will not fetch it.
    assert second["fetchable"] is False
    assert second["originRefused"] is True
    assert "/weaver/weaver.file.FileDownload" in first["downloadPath"]


def test_listing_attachments_writes_nothing(oa_stack, read_open,
                                            install_transport, tmp_path):
    """Listing is a read; a read that quietly wrote files would be a side effect."""
    stack, service, connection = oa_stack
    install_transport(_form_handler)
    result = _invoke(service, stack, connection, "request.attachments",
                     {"request_id": "100"})
    assert result.data["saved"] == []
    assert result.data["requested"] == []
    # Nothing was written anywhere under the workspace, not merely "no file at
    # the path we happened to think of".
    assert not [name for name in os.listdir(str(tmp_path))
                if os.path.isdir(os.path.join(str(tmp_path), name))]


def test_a_fetched_attachment_lands_in_the_configured_directory(
        oa_stack, read_open, install_transport, tmp_path):
    stack, service, connection = oa_stack
    directory = tmp_path / "downloads"
    _configure_attachment_dirs(service, stack, connection)
    install_transport(_form_handler)
    result = _invoke(service, stack, connection, "request.attachments",
                     {"request_id": "100", "index": 1})
    assert result.ok is True, result.message
    assert result.data["requested"] == [1]
    saved = result.data["saved"][0]
    assert saved["filename"] == "合同.pdf"
    assert saved["bytes"] == len("PDF-BYTES")
    assert os.path.isfile(saved["path"])
    assert os.path.realpath(saved["path"]).startswith(
        os.path.realpath(str(directory)))
    with open(saved["path"], "rb") as handle:
        assert handle.read() == b"PDF-BYTES"


def test_fetching_without_a_configured_directory_is_refused(
        oa_stack, read_open, install_transport):
    """没有目录配置时明确拒绝, rather than picking a directory for the operator."""
    stack, service, connection = oa_stack
    install_transport(_form_handler)
    result = _invoke(service, stack, connection, "request.attachments",
                     {"request_id": "100", "index": 1})
    assert result.ok is False
    assert result.code == "no_attachment_dir"
    assert result.stage == STAGE_POLICY


def test_fetching_another_hosts_attachment_is_refused(
        oa_stack, read_open, install_transport, tmp_path):
    """The adapter is not a fetcher: an out-of-scope link is a refusal."""
    stack, service, connection = oa_stack
    _configure_attachment_dirs(service, stack, connection)
    fake = install_transport(_form_handler)
    result = _invoke(service, stack, connection, "request.attachments",
                     {"request_id": "100", "index": 2})
    assert result.ok is False
    assert result.code == "attachment_out_of_scope"
    assert fake.calls_to("files.example.net") == []


def test_an_unknown_attachment_selection_is_refused_not_ignored(
        oa_stack, read_open, install_transport, tmp_path):
    stack, service, connection = oa_stack
    _configure_attachment_dirs(service, stack, connection)
    install_transport(_form_handler)
    result = _invoke(service, stack, connection, "request.attachments",
                     {"request_id": "100", "filename": "不存在.pdf"})
    assert result.ok is False
    assert result.code == "attachment_missing"


# ===========================================================================
# 纯查询不隐式标记已读
# ===========================================================================

def test_reading_a_request_never_marks_it_read(
        oa_stack, read_open, install_transport):
    """The spec's 纯查询不隐式标记已读, asserted by counting remote calls."""
    stack, service, connection = oa_stack
    fake = install_transport(_form_handler)
    for action, params in (("request.read", {"request_id": "100"}),
                           ("request.flowlog", {"request_id": "100"}),
                           ("request.related", {"request_id": "100"}),
                           ("request.attachments", {"request_id": "100"})):
        result = _invoke(service, stack, connection, action, params)
        assert result.ok is True, (action, result.message)
    assert fake.calls_to("doReadIt") == []


def test_marking_a_cc_read_is_its_own_declared_write(
        oa_stack, readiness, install_transport):
    stack, service, connection = oa_stack
    readiness({"oa": {"read_execute": True, "write_execute": True}})
    fake = install_transport(_form_handler)
    result = _invoke(service, stack, connection, "cc.mark_read",
                     {"request_id": "100", "belong_user_id": "77"})
    assert result.ok is True, result.message
    assert result.data["markedAsRead"] is True
    calls = fake.calls_to("doReadIt")
    assert len(calls) == 1
    assert calls[0]["data"]["requestid"] == "100"
    assert calls[0]["data"]["opertype"] == "single"


def test_marking_a_cc_read_needs_the_write_class(
        oa_stack, read_open, install_transport):
    """A side effect the user sees does not ride in on the read grant."""
    stack, service, connection = oa_stack
    fake = install_transport(_form_handler)
    result = _invoke(service, stack, connection, "cc.mark_read",
                     {"request_id": "100"})
    assert result.ok is False
    assert result.code == "execution_not_available"
    assert result.stage == STAGE_POLICY
    assert fake.calls_to("doReadIt") == []


def test_an_unconfirmed_mark_read_is_a_failure_not_a_silent_success(
        oa_stack, readiness, install_transport):
    stack, service, connection = oa_stack
    readiness({"oa": {"read_execute": True, "write_execute": True}})

    def _unconfirmed(t, method, url, kw):
        if url.endswith("/api/workflow/reqlist/doReadIt"):
            return _resp(200, json_body={"api_status": False,
                                         "msg": "该流程不在抄送列表"},
                         url=url)
        return _form_handler(t, method, url, kw)

    install_transport(_unconfirmed)
    result = _invoke(service, stack, connection, "cc.mark_read",
                     {"request_id": "100", "belong_user_id": "77"})
    assert result.ok is False
    assert result.stage == STAGE_POLICY


# ===========================================================================
# E10 站点: 明确关闭而不是近似
# ===========================================================================

@pytest.mark.parametrize("action", ["request.flowlog", "request.related",
                                    "request.attachments", "cc.mark_read"])
def test_an_openapi_site_refuses_the_e9_only_reads(
        tmp_path, monkeypatch, readiness, install_transport, action):
    """E10 站点不支持这些读取时必须明确说明，不得以其他接口代替."""
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", "unit-test-master-key")
    monkeypatch.setattr("config.conf",
                        lambda: {"agent_workspace": str(tmp_path)})
    from tests._helpers import build_identity, legacy_connection_rule
    from integrations.external.service import ExternalConnectionService
    from integrations.external.oa import store as oa_store

    stack = build_identity(tmp_path)
    service = ExternalConnectionService(stack.service)
    monkeypatch.setattr(oa_store, "_service", lambda: service)
    readiness({"oa": {"read_execute": True, "write_execute": True}})
    connection = service.create_connection(
        actor_user_id=stack.root, scope="tenant", tenant_id=stack.tenant_id,
        kind=registry.KIND_OA, name="OA-E10",
        config={"base_url": NODE, "username": "alice", "app_key": "key-1",
                "corp_id": "corp-1"},
        secrets={"password": PASSWORD, "app_secret": "APP-SECRET-VALUE"})
    legacy_connection_rule(service, stack.tenant_id, connection["id"])
    install_transport(_openapi_ready)
    result = _invoke(service, stack, connection, action, {"request_id": "100"})
    assert result.ok is False
    assert result.code == "unsupported_interface"
    assert result.stage == STAGE_POLICY
    assert result.data["interface"] == "e10"


# ===========================================================================
# protocol vocabulary
# ===========================================================================

def test_an_id_outside_the_namespace_is_not_a_related_request():
    """A browser value that is not a positive integer is not a request id."""
    assert actions_module._linked_request_id({"value": "0"}) == ""
    assert actions_module._linked_request_id({"value": "-3"}) == ""
    assert actions_module._linked_request_id({"value": "abc"}) == ""
    assert actions_module._linked_request_id({"value": "12"}) == "12"


def test_detail_marks_come_from_the_forms_own_table_info():
    assert actions_module.discover_detail_marks(
        {"main": {}, "detail_2": {"a": 1}, "detail_1": {"b": 1}}) == [
            "detail_1", "detail_2"]
    assert actions_module.discover_detail_marks("not-a-mapping") == []


def test_an_absolute_attachment_url_must_match_the_sites_own_origin():
    assert actions_module.same_origin_path(
        NODE + "/weaver/x?a=1", NODE) == "/weaver/x?a=1"
    assert actions_module.same_origin_path("/weaver/x", NODE) == "/weaver/x"
    for hostile in ("https://files.example.net/x", "http://oa.example.com/x",
                    "//files.example.net/x", "weaver/x", "", None,
                    "/weaver\\x", "ftp://oa.example.com/x"):
        assert actions_module.same_origin_path(hostile, NODE) == "", hostile


def test_the_flow_log_paging_keys_come_from_the_remote_not_the_caller():
    """``requestid`` is the requested one; everything else is the form's own."""
    params = actions_module.build_request_log_params(
        {"requestid": "999", "workflowid": "9", "nodeid": "5"}, request_id="100")
    assert params["requestid"] == "999"
    assert params["workflowid"] == "9"
    assert params["pgnumber"] == "1"
    assert params["maxrequestlogid"] == "0"
    # A form with no requestid still pages the *requested* request.
    assert actions_module.build_request_log_params(
        {}, request_id="100")["requestid"] == "100"
