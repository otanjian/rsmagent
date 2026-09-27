# encoding:utf-8
"""A recorded test result reaches the console: catalogue, detail and response.

Change ``fix-connection-test-state-projection``. The property under test is not
"a probe runs" — that is ``tests/test_external_test_binding.py``, which owns
*when a result is recorded and bound*. What is measured here is the other half
of the same requirement, which never existed: the result is **read back**.

``external-system-access-console`` requires the card to show "最近测试结果与
时间" and requires "编辑参数后旧测试 SHALL 标记已过期";
``external-connection-management`` requires the result to be bound to the
configuration and secret versions it tested. The decision function
``test_summary_payload`` already says it is the one place that decides what the
test state is — "so the catalogue, the detail view and the page cannot
disagree" — but the catalogue and the detail view never called it, and the test
response never carried it. So a passing test produced
``test_status: "untested"`` on the card and a "未测试" toast.

Three facts are pinned here:

* **The read paths agree.** Catalogue card, detail view and
  ``test_state()`` answer the same thing for the same connection.
* **Stale is its own state.** After an edit or a credential rotation the card
  says ``expired`` — distinguishable from never-tested — and MUST NOT hand back
  the stale verdict's stage, code or detail.
* **The read is bounded.** A page of cards resolves its states in a fixed
  number of queries rather than one round trip per card, and only the newest
  few summaries per connection are considered — the same window the single-row
  reader has always used, so "the last result" means one thing everywhere.
"""

from __future__ import annotations

import pytest

from integrations.external import registry
from integrations.external.adapters import base as base_mod
from integrations.external.adapters.base import (
    ConnectionAdapter,
    ProbeResult,
    register_adapter,
    stage_failed,
    stage_ok,
    STAGE_NETWORK,
)

from tests import _external_oneagent as oneagent
from tests._helpers import build_identity

MASTER_KEY = "unit-test-master-key"

#: A real kind, with its adapter replaced by a scripted one: the row, the
#: validator and the DB constraint stay the product's, and only the network is
#: taken out (the same device ``test_external_test_binding.py`` uses).
KIND = "mcp"
CONFIG = dict(oneagent.MCP_REMOTE_CONFIG)
SLOT = "header"
SECRET = oneagent.MCP_REMOTE_SECRET[SLOT]
ACTION = "tools.list"


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("COW_CREDENTIAL_MASTER_KEY", MASTER_KEY)


@pytest.fixture(autouse=True)
def _open_test_class(monkeypatch):
    """Open the ``test`` class the way a deployment does: in configuration."""
    from config import conf

    monkeypatch.setitem(
        conf().setdefault("external_connections", {}),
        "readiness", {KIND: {"test": True}})


@pytest.fixture
def probe_kind(monkeypatch):
    """Replace the real MCP adapter with a scripted one."""
    state = {"result": ProbeResult(stages=(stage_ok("handshake"),))}

    real_classes = dict(base_mod._ADAPTER_CLASSES)  # noqa: SLF001
    real_instances = dict(base_mod._ADAPTERS)  # noqa: SLF001

    class _ProjectionAdapter(ConnectionAdapter):
        kind = KIND
        actions = frozenset({ACTION})
        write_actions = frozenset()

        def probe(self, ctx):
            return state["result"]

    register_adapter(_ProjectionAdapter)
    try:
        yield state
    finally:
        base_mod._ADAPTER_CLASSES.clear()  # noqa: SLF001
        base_mod._ADAPTER_CLASSES.update(real_classes)  # noqa: SLF001
        base_mod._ADAPTERS.clear()  # noqa: SLF001
        base_mod._ADAPTERS.update(real_instances)  # noqa: SLF001


@pytest.fixture
def stack(tmp_path):
    return build_identity(tmp_path)


@pytest.fixture
def svc(stack):
    from integrations.external.service import ExternalConnectionService
    return ExternalConnectionService(stack.service)


def _create(svc, stack, **overrides):
    values = dict(
        actor_user_id=stack.root, scope=registry.SCOPE_TENANT,
        tenant_id=stack.tenant_id, kind=KIND, name="Projection target",
        config=dict(CONFIG), secrets={SLOT: SECRET})
    values.update(overrides)
    return svc.create_connection(**values)


def _run_test(svc, stack, connection_id, *, tenant_id=None):
    return svc.runtime().probe(
        connection_id, actor_user_id=stack.root,
        tenant_id=stack.tenant_id if tenant_id is None else tenant_id)


def _card(svc, stack, connection_id):
    page = svc.list_catalog(
        actor_user_id=stack.root, scope=registry.SCOPE_TENANT,
        tenant_id=stack.tenant_id)
    for card in page["items"]:
        if card["id"] == connection_id:
            return card
    raise AssertionError("the card is missing: %s" % connection_id)


def _detail(svc, stack, connection_id):
    return svc.get_connection(
        actor_user_id=stack.root, scope=registry.SCOPE_TENANT,
        tenant_id=stack.tenant_id, connection_id=connection_id)


def _recorded_at(svc, connection_id):
    rows = svc._store.execute(  # noqa: SLF001
        "SELECT created_at FROM external_connection_tests WHERE connection_id=?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1", (connection_id,))
    return int(rows[0]["created_at"]) if rows else None


# -- the read paths agree --------------------------------------------------

def test_the_catalogue_card_shows_a_recorded_pass_and_its_time(
        svc, stack, probe_kind):
    """The card is what the spec means by 最近测试结果与时间."""
    connection = _create(svc, stack)
    _run_test(svc, stack, connection["id"])

    card = _card(svc, stack, connection["id"])

    assert card["test_status"] == "ok"
    assert card["tested_at"] == _recorded_at(svc, connection["id"])


def test_a_failed_test_reaches_the_card_as_failed(svc, stack, probe_kind):
    """A recorded failure is shown rather than hidden behind 未测试."""
    probe_kind["result"] = ProbeResult(stages=(
        stage_failed("handshake", "connection_refused", stage=STAGE_NETWORK),))
    connection = _create(svc, stack)
    _run_test(svc, stack, connection["id"])

    assert _card(svc, stack, connection["id"])["test_status"] == "failed"


def test_the_card_the_detail_and_the_test_state_endpoint_all_agree(
        svc, stack, probe_kind):
    """One place decides the state: three read paths may not disagree."""
    connection = _create(svc, stack)
    _run_test(svc, stack, connection["id"])

    card = _card(svc, stack, connection["id"])
    detail = _detail(svc, stack, connection["id"])
    state = svc.test_state(connection["id"], tenant_id=stack.tenant_id)

    assert card["test_status"] == state["status"] == "ok"
    assert detail["test_status"] == state["status"]
    assert card["tested_at"] == detail["tested_at"] == state["ran_at"]


def test_the_saved_test_response_carries_the_state_it_recorded(
        svc, stack, probe_kind):
    """The console's toast reads ``payload.test_status``; it must not be absent."""
    connection = _create(svc, stack)

    payload = _run_test(svc, stack, connection["id"])

    assert payload["recorded"] is True
    assert payload["test_status"] == "ok"
    assert payload["tested_at"] == _recorded_at(svc, connection["id"])


def test_a_draft_test_still_reports_no_saved_state(svc, stack, probe_kind):
    """A draft persists nothing, so it may not stamp a state onto anything."""
    connection = _create(svc, stack)

    payload = svc.probe_draft(KIND, dict(CONFIG), secrets={SLOT: SECRET},
                              actor_user_id=stack.root,
                              tenant_id=stack.tenant_id)

    assert payload["recorded"] is False
    assert "test_status" not in payload
    assert _card(svc, stack, connection["id"])["test_status"] == "untested"


# -- stale is its own state ------------------------------------------------

def test_editing_the_connection_marks_the_previous_result_expired(
        svc, stack, probe_kind):
    """Old result must not stand, and must not read as never-tested either."""
    connection = _create(svc, stack)
    _run_test(svc, stack, connection["id"])
    recorded_at = _recorded_at(svc, connection["id"])

    svc.update_connection(
        actor_user_id=stack.root, scope=registry.SCOPE_TENANT,
        connection_id=connection["id"],
        expected_version=int(connection["version"]),
        tenant_id=stack.tenant_id, name="Projection target (renamed)")

    card = _card(svc, stack, connection["id"])
    assert card["test_status"] == "expired"
    assert card["tested_at"] == recorded_at


def test_rotating_a_credential_marks_the_previous_result_expired(
        svc, stack, probe_kind):
    """A verdict produced with another secret is not evidence about this one."""
    connection = _create(svc, stack)
    _run_test(svc, stack, connection["id"])

    svc.update_connection(
        actor_user_id=stack.root, scope=registry.SCOPE_TENANT,
        connection_id=connection["id"],
        expected_version=int(connection["version"]),
        tenant_id=stack.tenant_id, secrets={SLOT: "a-different-secret"})

    assert _card(svc, stack, connection["id"])["test_status"] == "expired"


def test_an_expired_state_carries_no_verdict_from_the_stale_record():
    """`expired` says the old result is void — not what it said."""
    from integrations.external.runtime import test_summary_payload

    state = test_summary_payload(0, {}, stale_at=1790477563)

    assert state["status"] == "expired"
    assert state["ran_at"] == 1790477563
    assert state["stage"] == "" and state["code"] == ""
    assert state["detail"] == {}


def test_never_tested_stays_untested(svc, stack, probe_kind):
    """No record at all is a different fact from a stale one."""
    connection = _create(svc, stack)

    card = _card(svc, stack, connection["id"])
    assert card["test_status"] == "untested"
    assert card["tested_at"] is None
    assert svc.test_state(connection["id"],
                          tenant_id=stack.tenant_id)["status"] == "untested"


# -- inheritance resolves through the effective row ------------------------

def _platform_template(svc, stack):
    template = svc.create_connection(
        actor_user_id=stack.root, scope=registry.SCOPE_PLATFORM,
        kind=KIND, name="Shared projection template",
        config=dict(CONFIG), secrets={SLOT: SECRET})
    svc.set_tenant_access(
        actor_user_id=stack.root, platform_connection_id=template["id"],
        tenant_ids=[stack.tenant_id], expected_revision=1)
    return template


def test_an_inherited_template_shows_the_platform_rows_result(
        svc, stack, probe_kind):
    """The tenant consumes the template, so the template's health is the answer."""
    template = _platform_template(svc, stack)
    _run_test(svc, stack, template["id"], tenant_id=None)

    card = _card(svc, stack, template["id"])

    assert card["source"] == "inherited"
    assert card["test_status"] == "ok"


def test_an_override_does_not_inherit_the_templates_verdict(
        svc, stack, probe_kind):
    """The override is a different configuration with different credentials."""
    template = _platform_template(svc, stack)
    _run_test(svc, stack, template["id"], tenant_id=None)

    override = svc.create_override(
        actor_user_id=stack.root, tenant_id=stack.tenant_id,
        platform_connection_id=template["id"], name="Tenant projection override",
        config=dict(CONFIG), secrets={SLOT: SECRET})

    card = _card(svc, stack, template["id"])
    assert card["source"] == "overridden"
    assert card["effective_id"] == override["id"]
    # The inherited template is green; the row actually in force is not tested.
    assert card["test_status"] == "untested"


# -- the read is bounded --------------------------------------------------

def _inject_summaries(svc, connection_id, count, *, config_version, from_time):
    """Fabricate ``count`` newer summaries that cannot apply to the row.

    Written the way the runtime writes them, so what is under test is the
    *reader*: these rows are legitimate records for a configuration the row no
    longer has.
    """
    for index in range(count):
        svc._store.execute(  # noqa: SLF001
            "INSERT INTO external_connection_tests"
            " (test_id, connection_id, actor_user_id, scope, tenant_id,"
            "  config_version, secret_versions_json, stage, result, code,"
            "  detail_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ect_probe_%d_%d" % (from_time, index), connection_id, "usr_probe",
             "tenant", None, config_version, "{}", "", "ok", "", "{}",
             from_time + index))


def test_the_lookback_window_is_five_summaries_and_is_counted_in_sql(
        svc, stack, probe_kind):
    """An ancient green is not resurrected, and the window is not open-ended.

    The single-row reader has always considered the newest five summaries, and a
    page of cards has to mean the same thing by "the last result" — otherwise a
    connection could read ``ok`` on the catalogue and ``untested`` on the
    detail view, which is the disagreement this change exists to remove.
    """
    connection = _create(svc, stack)
    _run_test(svc, stack, connection["id"])
    newest = _recorded_at(svc, connection["id"])

    # Four newer summaries that do not apply: the matching record is still the
    # fifth-newest, so it is inside the window.
    _inject_summaries(svc, connection["id"], 4, config_version=99,
                      from_time=newest + 1)
    assert _card(svc, stack, connection["id"])["test_status"] == "ok"

    # One more, and the matching record falls outside the window: the card says
    # the result is stale rather than reaching further back for a verdict.
    _inject_summaries(svc, connection["id"], 1, config_version=99,
                      from_time=newest + 100)
    assert _card(svc, stack, connection["id"])["test_status"] == "expired"
    assert svc.test_state(connection["id"],
                          tenant_id=stack.tenant_id)["status"] == "expired"


class _CountingStore:
    """Counts the queries that resolve test state, per page."""

    WATCHED = ("external_connection_tests", "external_connection_secret_refs")

    def __init__(self, real):
        self._real = real
        self.state_queries = 0

    def __getattr__(self, name):
        return getattr(self._real, name)

    def execute(self, sql, params=()):
        if any(table in sql for table in self.WATCHED):
            self.state_queries += 1
        return self._real.execute(sql, params)


def test_a_page_of_cards_resolves_each_state_in_a_bounded_number_of_queries(
        svc, stack, probe_kind):
    """A card costs no round trip of its own: five cards, not five reads."""
    one = _create(svc, stack, name="Single card")
    _run_test(svc, stack, one["id"])

    counting = _CountingStore(svc._store)  # noqa: SLF001
    svc._store = counting  # noqa: SLF001
    svc.list_catalog(actor_user_id=stack.root, scope=registry.SCOPE_TENANT,
                     tenant_id=stack.tenant_id, q="Single card")
    single_page = counting.state_queries

    for index in range(4):
        extra = _create(svc, stack, name="Extra card %d" % index)
        _run_test(svc, stack, extra["id"])

    counting.state_queries = 0
    page = svc.list_catalog(actor_user_id=stack.root, scope=registry.SCOPE_TENANT,
                            tenant_id=stack.tenant_id)
    multi_page = counting.state_queries

    assert len(page["items"]) == 5
    assert all(card["test_status"] == "ok" for card in page["items"])
    assert multi_page == single_page, (
        "the states are read per page, not per card: %d vs %d"
        % (multi_page, single_page))
