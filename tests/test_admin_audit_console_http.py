# encoding:utf-8
"""The 审计日志 / Token 消耗 console reads (change add-audit-and-token-console).

The two pages share one authorization decision, so it is asserted once and in
the place that owns it (``audit_read_scope``): a platform admin reads across
tenants and may *narrow* with ``?tenant=``; a tenant administrator reads their
own tenant and the parameter is ignored rather than refused — refusing would
confirm which tenant ids exist, which is exactly what a cross-tenant probe
wants. Everything else is refused outright, because answering a plain member
with an empty page would read as "no events" instead of "not yours".

The row projections are asserted too: they are the only place where a stored
``result="denied"`` becomes the console's ``status="failure"`` badge while
staying distinguishable in the payload.
"""

import os
import sys
import types
from unittest.mock import patch

import pytest
import web

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from channel.web.admin_audit_handlers import (  # noqa: E402
    AdminAuditActionsHandler,
    AdminAuditEventsHandler,
    _parse_day,
    _project,
    _split_target,
    audit_read_scope,
)
from channel.web.admin_token_usage_handlers import (  # noqa: E402
    AdminTokenUsageHandler,
    _log_row,
)


def _ctx(*, tenant_id="tnt_1", platform=False, tenant_admin=False, user="usr_1"):
    return types.SimpleNamespace(tenant_id=tenant_id, is_platform_admin=platform,
                                 is_tenant_admin=tenant_admin, user_id=user)


def _refusal(ctx):
    """Run the scope gate and return the ``HTTPError`` it raises.

    ``web.HTTPError`` writes its headers through a request-global, so the header
    call is stubbed rather than standing up a whole request just to ask "is this
    caller refused".
    """
    with patch("web.webapi.header"):
        try:
            audit_read_scope(ctx)
        except web.HTTPError as error:
            return error
    raise AssertionError("the gate admitted a caller it should have refused")


#: Every parameter either handler reads with a default, so a stub only has to
#: name the ones a test cares about.
_DEFAULTS = {
    "action": "", "actor": "", "result": "", "tenant": "",
    "start_time": "", "end_time": "", "since": "", "until": "",
    "limit": "200", "offset": "0",
    "type": "details", "start_date": "", "end_date": "",
    "model": "", "provider": "", "status": "",
}


def _query(**overrides):
    values = dict(_DEFAULTS)
    values.update(overrides)
    return types.SimpleNamespace(**values)


class _Service:
    """Identity-service stand-in that records the filters it was handed."""

    def __init__(self, events=None, actions=None):
        self.seen = {}
        self._events = events or []
        self._actions = actions if actions is not None else []

    def query_audit_events(self, **kwargs):
        self.seen.update(kwargs)
        return {"events": self._events, "total": len(self._events),
                "limit": kwargs["limit"], "offset": kwargs["offset"]}

    def audit_action_names(self, **kwargs):
        self.seen.update(kwargs)
        return self._actions


class _Manager:
    """Token-usage manager stand-in; ``seen`` maps a read to its filters."""

    def __init__(self, summary=None, details=None, users=None, logs=None,
                 values=None):
        self.seen = {}
        self._summary = summary or {}
        self._details = details or []
        self._users = users or []
        self._logs = logs or []
        self._values = values or []
        self.columns = []

    def summary(self, **kwargs):
        self.seen["summary"] = kwargs
        return self._summary

    def details(self, **kwargs):
        self.seen["details"] = kwargs
        return self._details

    def by_user(self, **kwargs):
        self.seen.setdefault("by_user", []).append(kwargs)
        return self._users

    def call_logs(self, **kwargs):
        self.seen["call_logs"] = kwargs
        return self._logs

    def distinct_values(self, column):
        self.columns.append(column)
        return self._values


_AUDIT = "channel.web.admin_audit_handlers"
_TOKEN = "channel.web.admin_token_usage_handlers"


def _get_audit_events(ctx, service, **params):
    """Invoke the events handler with the caller and service stubbed in."""
    with patch(f"{_AUDIT}._console_context", lambda: ctx), \
            patch(f"{_AUDIT}._get_service", lambda: service), \
            patch(f"{_AUDIT}.web.input", lambda **k: _query(**params)), \
            patch(f"{_AUDIT}.web.header"):
        return AdminAuditEventsHandler().GET()


def _get_token_usage(ctx, manager, **params):
    with patch(f"{_TOKEN}._console_context", lambda: ctx), \
            patch(f"{_TOKEN}._manager", lambda: manager), \
            patch(f"{_TOKEN}.web.input", lambda **k: _query(**params)), \
            patch(f"{_TOKEN}.web.header"):
        return AdminTokenUsageHandler().GET()


# --- scope ----------------------------------------------------------------

def test_a_platform_administrator_reads_across_tenants():
    assert audit_read_scope(_ctx(platform=True)) == ("all", None)
    # No tenant selected is normal for this caller, not a refusal.
    assert audit_read_scope(_ctx(platform=True, tenant_id="")) == ("all", None)


def test_a_tenant_administrator_reads_only_their_own_tenant():
    assert audit_read_scope(_ctx(tenant_admin=True)) == ("tenant", "tnt_1")


def test_an_ordinary_member_is_refused():
    assert str(_refusal(_ctx())).startswith("403")


def test_a_tenant_administrator_without_a_selected_tenant_is_refused():
    """The scope is the selected tenant; there is nothing to scope to."""
    assert str(_refusal(_ctx(tenant_admin=True, tenant_id=""))).startswith("403")


def test_an_absent_context_is_refused():
    assert str(_refusal(None)).startswith("403")


# --- filtering helpers ----------------------------------------------------

def test_a_date_bound_is_inclusive_of_its_own_day():
    """``to=2024-03-05`` means the end of the 5th, not its midnight.

    Treating the bound as midnight silently drops a day of events from every
    filtered view, and the operator has no way to tell that it happened.
    """
    start = _parse_day("2024-03-05", end=False)
    end = _parse_day("2024-03-05", end=True)
    assert end - start == 86399


def test_an_absent_bound_is_open_ended():
    assert _parse_day("", end=False) is None
    assert _parse_day("", end=True) is None


def test_a_unix_bound_passes_through():
    assert _parse_day("1700000000", end=False) == 1_700_000_000


def test_a_target_splits_into_the_console_resource_columns():
    assert _split_target("agent:agt_1") == ("agent", "agt_1")
    # A target with no separator stays whole rather than being guessed at.
    assert _split_target("agent") == ("agent", "")
    assert _split_target("") == ("", "")


# --- row projections ------------------------------------------------------

def test_a_denied_action_is_a_failure_badge_but_stays_readable():
    row = _project({"id": 7, "time": 1700000000, "tenant_id": "tnt_1",
                    "actor_user_id": "usr_1", "actor_username": "alice",
                    "action": "agent.delete", "target": "agent:agt_1",
                    "result": "denied", "changes": {"reason": "not owner"}})
    assert row["status"] == "failure"
    assert row["result"] == "denied"
    assert row["resource_type"] == "agent"
    assert row["resource_id"] == "agt_1"
    # The source console reads ``actor``; this fork stores ``actor_username``.
    assert row["actor"] == "alice"
    assert row["actor_username"] == "alice"
    assert row["tenant"] == "tnt_1"


def test_a_successful_action_carries_the_success_badge():
    row = _project({"id": 1, "result": "success",
                    "actor_username": "alice", "action": "auth.login"})
    assert row["status"] == "success"
    assert row["changes"] == {}


def test_audit_actor_shows_resolved_name_and_preserves_recorded_identity():
    row = _project({"actor_user_id": "usr_1", "actor_username": "alice",
                    "actor_display_name": "张三"})
    assert row["actor"] == "张三"
    assert row["actor_display_name"] == "张三"
    assert row["actor_username"] == "alice"
    assert row["actor_user_id"] == "usr_1"
    assert _project({"actor_user_id": "missing-user"})["actor"] == ""


def test_an_estimated_call_is_marked_on_the_log_row():
    assert _log_row({"estimated": 1})["estimated"] is True
    assert _log_row({})["estimated"] is False
    # A row whose account was renamed still has something to show.
    assert _log_row({"actor_user_id": "usr_1"})["actor"] == "usr_1"


# --- the audit endpoint ---------------------------------------------------

def test_a_platform_administrator_may_narrow_to_one_tenant():
    service = _Service()
    _get_audit_events(_ctx(platform=True), service, tenant="tnt_9")
    assert (service.seen["scope"], service.seen["tenant_id"]) == ("tenant", "tnt_9")


def test_a_tenant_administrator_cannot_escape_their_tenant():
    """The cross-tenant attempt is ignored, not refused.

    A 403 here would confirm the requested tenant id exists, which is the
    information a probe is after; the caller's own scope is already the
    narrowest answer available.
    """
    service = _Service()
    _get_audit_events(_ctx(tenant_admin=True), service, tenant="tnt_other")
    assert (service.seen["scope"], service.seen["tenant_id"]) == ("tenant", "tnt_1")


def test_the_console_failure_filter_becomes_an_exclusion():
    """``status=failure`` means "everything that is not a success".

    ``denied`` and ``error`` are both failures to an operator, so a literal
    ``!= success`` is what they are asking for, and it keeps working when a new
    non-success result is introduced.
    """
    service = _Service()
    _get_audit_events(_ctx(tenant_admin=True), service, status="failure")
    assert service.seen["result"] is None
    assert service.seen["exclude_result"] == "success"


def test_a_literal_result_filter_is_still_accepted():
    service = _Service()
    _get_audit_events(_ctx(tenant_admin=True), service, status="denied")
    assert service.seen["result"] == "denied"
    assert service.seen["exclude_result"] is None


def test_the_console_action_and_actor_filters_reach_the_query():
    service = _Service()
    _get_audit_events(_ctx(tenant_admin=True), service,
                      action="agent.create,agent.delete", actor="alice",
                      start_time="2024-03-01", end_time="2024-03-05")
    assert service.seen["actions"] == ["agent.create", "agent.delete"]
    assert service.seen["actor_username"] == "alice"
    assert service.seen["end_time"] - service.seen["start_time"] == 5 * 86400 - 1


def test_a_bad_filter_is_a_400_not_a_500():
    """A malformed filter is the caller's mistake, not a server fault."""
    service = _Service()
    with patch(f"{_AUDIT}._console_context", lambda: _ctx(tenant_admin=True)), \
            patch(f"{_AUDIT}._get_service", lambda: service), \
            patch(f"{_AUDIT}.web.input", lambda **k: _query(limit="not-a-number")), \
            patch(f"{_AUDIT}.web.header"), \
            patch(f"{_AUDIT}._error") as error:
        error.return_value = {"code": "bad_request"}
        AdminAuditEventsHandler().GET()
    assert error.call_args.args[1] == 400
    assert service.seen == {}


def test_a_store_fault_is_reported_rather_than_shown_empty():
    """"The read broke" must not be rendered as "there is nothing here"."""
    service = _Service()

    def boom(**kwargs):
        raise RuntimeError("identity.db is locked")

    service.query_audit_events = boom
    with patch(f"{_AUDIT}._console_context", lambda: _ctx(tenant_admin=True)), \
            patch(f"{_AUDIT}._get_service", lambda: service), \
            patch(f"{_AUDIT}.web.input", lambda **k: _query()), \
            patch(f"{_AUDIT}.web.header"), \
            patch(f"{_AUDIT}._error") as error:
        error.return_value = {"code": "audit_query_failed"}
        AdminAuditEventsHandler().GET()
    assert error.call_args.args[1] == 500
    assert "locked" in error.call_args.args[0]


def test_the_action_filter_list_is_scoped_like_the_events():
    """The dropdown must not offer another tenant's action names."""
    service = _Service(actions=["auth.login", "agent.create"])
    with patch(f"{_AUDIT}._console_context", lambda: _ctx(platform=True)), \
            patch(f"{_AUDIT}._get_service", lambda: service), \
            patch(f"{_AUDIT}.web.header"):
        AdminAuditActionsHandler().GET()
    assert (service.seen["scope"], service.seen["tenant_id"]) == ("all", None)

# --- the token-usage endpoint ---------------------------------------------

def test_each_console_tab_reads_its_own_aggregation():
    for panel, read in (("summary", "summary"), ("details", "details"),
                        ("by-user", "by_user"), ("call_logs", "call_logs")):
        manager = _Manager()
        _get_token_usage(_ctx(tenant_admin=True), manager, type=panel)
        assert read in manager.seen, panel


def test_the_actor_dropdown_is_not_narrowed_by_the_actor_filter():
    """Otherwise picking one name would leave that name as the only option.

    The 按用户汇总 read *is* narrowed (it is the tab the filter applies to), but
    the list that populates the dropdown is not — the user has to be able to get
    back to "all users".
    """
    manager = _Manager(users=[{"actor_username": "alice"}])
    _get_token_usage(_ctx(tenant_admin=True), manager, type="actors", actor="alice")
    assert manager.seen["by_user"][-1] == {
        "start_date": None, "end_date": None, "tenant_id": "tnt_1"}

    manager = _Manager()
    _get_token_usage(_ctx(tenant_admin=True), manager, type="by-user", actor="alice")
    assert manager.seen["by_user"][-1]["actor_username"] == "alice"


def test_the_call_log_filters_reach_the_query():
    manager = _Manager()
    _get_token_usage(_ctx(tenant_admin=True), manager, type="call_logs",
                     actor="alice", model="deepseek-chat", provider="deepseek",
                     status="error", start_date="2024-03-01", end_date="2024-03-05")
    call = manager.seen["call_logs"]
    assert call["actor_username"] == "alice"
    assert call["model"] == "deepseek-chat"
    assert call["provider"] == "deepseek"
    assert call["status"] == "error"
    assert str(call["start_date"]) == "2024-03-01"
    assert str(call["end_date"]) == "2024-03-05"
    assert call["tenant_id"] == "tnt_1"


def test_the_model_and_provider_lookups_read_the_right_column():
    manager = _Manager()
    _get_token_usage(_ctx(tenant_admin=True), manager, type="models")
    _get_token_usage(_ctx(tenant_admin=True), manager, type="providers")
    assert manager.columns == ["model", "provider"]


def test_a_tenant_administrator_cannot_escape_their_tenant_here_either():
    manager = _Manager()
    _get_token_usage(_ctx(tenant_admin=True), manager, type="details",
                     tenant="tnt_other")
    assert manager.seen["details"]["tenant_id"] == "tnt_1"


def test_a_platform_administrator_may_narrow_here():
    manager = _Manager()
    _get_token_usage(_ctx(platform=True), manager, type="details", tenant="tnt_9")
    assert manager.seen["details"]["tenant_id"] == "tnt_9"


def test_an_unknown_panel_type_is_a_400():
    manager = _Manager()
    with patch(f"{_TOKEN}._console_context", lambda: _ctx(tenant_admin=True)), \
            patch(f"{_TOKEN}._manager", lambda: manager), \
            patch(f"{_TOKEN}.web.input", lambda **k: _query(type="everything")), \
            patch(f"{_TOKEN}.web.header"), \
            patch(f"{_TOKEN}._error") as error:
        error.return_value = {"code": "bad_request"}
        AdminTokenUsageHandler().GET()
    assert error.call_args.args[1] == 400
    assert manager.seen == {}


def test_a_member_cannot_reach_the_token_page():
    """The refusal happens before any read, so there is no "empty tenant" case."""
    manager = _Manager()
    with pytest.raises(web.HTTPError):
        # ``webapi.header`` is where ``HTTPError`` writes its headers; patching
        # only ``web.header`` would leave the raise needing a request context.
        with patch("web.webapi.header"):
            _get_token_usage(_ctx(), manager)
    assert manager.seen == {}
