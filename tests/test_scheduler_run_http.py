# encoding:utf-8
"""HTTP surface tests for scheduled-run history (P6).

The ledger is read and mutated over the *real* WSGI app, because the contract
these endpoints owe is an authorization one: which member sees which run, which
run's transcript may be read, and what a refusal looks like on the wire. A
handler-level test with a patched ``web`` module would prove the parsing and
nothing about the answer.

Two rules the tests below fix in place:

* ``/api/scheduler/runs`` never returns a partner's run. Authorization is inside
  the SQL ``WHERE``, so it holds under paging too -- a post-filter over a page of
  everyone's rows would leak by *omission* (a short page a caller cannot
  distinguish from "you have nothing").
* ``full_output`` is a second, independent grant. Being allowed to see a run does
  not imply being allowed to read the conversation it delivered into, so the body
  is filled in only after the exact durable session match.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest

from agent.tools.scheduler.run_repository import RunScope, RunScopeRepository
from tests._helpers import WebAppHarness, open_capability_actions

#: The three capability actions these endpoints sit behind; the production
#: declaration opens them once the batch has acceptance evidence, so a test opens
#: them for its own duration instead of depending on that.
_ACTIONS = {
    "scheduler_runs_list": {"list": "read"},
    "scheduler_runs_detail": {"detail": "read"},
    "scheduler_runs_delete": {"delete": "config"},
}

SHARED = "shared-agent"
BASE_TIME = 1000


@contextmanager
def _history(tmp_path, *, open_actions=True):
    """A real app over a seeded run ledger.

    The harness is built *inside* the capability context on purpose:
    ``build_web_app`` resolves the URL table and the route policy at build time,
    so an app constructed before the actions were opened would still carry the
    closed entries and answer 503 to every request in the block.
    """
    if open_actions:
        with open_capability_actions(_ACTIONS):
            with _seeded_app(tmp_path) as built:
                yield built
    else:
        with _seeded_app(tmp_path) as built:
            yield built


@contextmanager
def _fresh_history(tmp_path):
    """The same endpoints on a deployment that has **never run a task**.

    Not a variant of ``_history``: the attribution table is owned by the *write*
    path, so a fresh install has the ``runs`` ledger (every chat turn writes one)
    and no scope rows at all -- and, until this case was fixed, no scope table
    either. The fixture therefore deliberately does not seed and does not call
    ``ensure_schema``: pre-creating the table here would hide exactly the state a
    real first deployment is in.
    """
    with open_capability_actions(_ACTIONS):
        with _fresh_app(tmp_path) as built:
            yield built


@contextmanager
def _fresh_app(tmp_path):
    web = WebAppHarness(tmp_path)
    try:
        web.add_agent(SHARED)
        web.member("alice", ["member"])
        web.member("bob", ["member"])
        from agent.memory import get_conversation_store

        yield web, get_conversation_store()
    finally:
        web.close()


@contextmanager
def _seeded_app(tmp_path):
    web = WebAppHarness(tmp_path)
    try:
        web.add_agent(SHARED)
        web.member("alice", ["member"])
        web.member("bob", ["member"])
        store, repository = _ledger()
        _seed(store, repository, web)
        yield web, store, repository
    finally:
        web.close()


def _ledger():
    """The same global store the handlers resolve, plus its scope table.

    The run ledger is one file shared by every Agent (``agent_id`` is a column),
    so seeding it directly is seeding what the request will read -- no patching of
    the store lookup, which is the seam the test would otherwise be trusting.
    """
    from agent.memory import get_conversation_store

    store = get_conversation_store()
    repository = RunScopeRepository(store)
    repository.ensure_schema()
    return store, repository


def _seed(store, repository, web):
    """Alice 3 personal runs, Bob 3, one public, one unattributed legacy row."""
    for index in range(3):
        _add_run(store, repository, "alice-%d" % index, web,
                 owner=web.user_id("alice"), tenant=web.tenant_id,
                 started_at=BASE_TIME + index)
        _add_run(store, repository, "bob-%d" % index, web,
                 owner=web.user_id("bob"), tenant=web.tenant_id,
                 started_at=BASE_TIME + index)
    _add_run(store, repository, "public-0", web, owner="",
             tenant=web.tenant_id, scope="public", started_at=BASE_TIME + 10)
    # A ``runs`` row with no attribution snapshot: it must stay invisible, which
    # is the difference between "not attributed" and "attributed to nobody".
    _add_run(store, repository, "legacy-0", web, record=False,
             started_at=BASE_TIME + 11)


def _add_run(store, repository, run_id, web, *, owner="", tenant="",
             scope="personal", agent_id=SHARED, task_id="task-1",
             session_id="", started_at=None, record=True, status="done",
             extras=None):
    assert store.create_run(
        run_id,
        agent_id=agent_id,
        session_id=session_id or run_id,
        task_id=task_id,
        task_source="scheduler",
        extras=extras,
    )
    if started_at is not None:
        _set_started_at(store, run_id, started_at)
    if record:
        repository.record(RunScope(
            run_id=run_id, tenant_id=tenant, owner_user_id=owner, scope=scope,
            agent_id=agent_id, task_id=task_id,
            session_id=session_id or run_id,
        ))
    if status is not None:
        store.finish_run(run_id, status=status)
    return run_id


def _set_started_at(store, run_id, value):
    conn = sqlite3.connect(str(store._db_path))
    try:
        conn.execute("UPDATE runs SET started_at = ? WHERE run_id = ?",
                     (int(value), run_id))
        conn.commit()
    finally:
        conn.close()


def _add_message(web, session_id, run_id, *texts, owner, tenant,
                 agent_id=SHARED, role="assistant"):
    """Append assistant text tagged with ``run_id`` to one web session.

    Written through the public door so the stored shape (a JSON ``content`` cell,
    a ``sessions`` row with ``channel_type='web'``, and the Agent's *storage* key
    in the ``agent_id`` column) is the real one, and the read path is exercised
    against real rows rather than a hand-built fixture that could agree with a
    wrong query.

    The store is opened from the target Agent's workspace -- not the ambient one
    -- because a bound handle, not the ambient identity, decides which Agent's
    rows it writes. The owner and tenant come from an identity scope for the same
    reason: they are stamped per turn, not passed in.
    """
    from agent.memory import get_conversation_store
    from common.runtime_identity import identity_scope

    store = get_conversation_store(web.agent_workspace(agent_id))
    with identity_scope(user_id=owner, tenant_id=tenant):
        assert store.append_messages(
            session_id,
            [{"role": role, "content": text, "run_id": run_id} for text in texts],
            channel_type="web",
        )


def _runs(web, token, query=""):
    response = web.get("/api/scheduler/runs" + query, token=token)
    return response, WebAppHarness.json(response)


def _get_detail(web, token, run_id):
    response = web.get("/api/scheduler/runs/detail?run_id=" + run_id, token=token)
    return response, WebAppHarness.json(response)


def _scope_row_count(store, run_id):
    conn = sqlite3.connect(str(store._db_path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM fork_scheduler_run_scopes WHERE run_id = ?",
            (run_id,)).fetchone()[0]
    finally:
        conn.close()


def _scope_table_exists(store):
    """Whether the attribution table exists at all.

    Used to keep the fresh-deployment fixture honest: if the table were already
    there, the tests below would silently stop covering the state they exist for.
    """
    conn = sqlite3.connect(str(store._db_path))
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='fork_scheduler_run_scopes'").fetchone() is not None
    finally:
        conn.close()


def _message_count(store, session_id):
    conn = sqlite3.connect(str(store._db_path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?",
            (session_id,)).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The list: scoped, paged, and honest about what it shows
# ---------------------------------------------------------------------------


def test_history_shows_only_the_callers_own_runs(tmp_path):
    with _history(tmp_path) as (web, _store, _repository):
        response, body = _runs(web, web.login("alice"))

    assert response.status == "200 OK"
    ids = {run["run_id"] for run in body["runs"]}
    # The public run is visible to every member; Bob's three are not. The
    # unattributed legacy row is invisible to everyone, including whoever the task
    # happens to belong to: no snapshot means no ownership evidence.
    assert ids == {"alice-0", "alice-1", "alice-2", "public-0"}
    assert "legacy-0" not in ids


def test_history_reports_the_attributed_only_scope(tmp_path):
    with _history(tmp_path) as (web, _store, _repository):
        _response, body = _runs(web, web.login("alice"))

    # The label is part of the contract: a client must be able to tell the user
    # that older, unattributable runs are not in the page.
    assert body["history_scope"] == "attributed_only"


def test_paging_narrows_the_authorized_rows_not_a_global_page(tmp_path):
    with _history(tmp_path) as (web, _store, _repository):
        token = web.login("alice")
        # Newest first: public-0 (1010), then alice-2 (1002), alice-1 (1001).
        _response, first = _runs(web, token, "?limit=2&offset=0")
        _response, second = _runs(web, token, "?limit=2&offset=2")

    assert [run["run_id"] for run in first["runs"]] == ["public-0", "alice-2"]
    # Alice has four visible runs, so the second page holds the remainder -- and
    # still none of Bob's, even though Bob's rows interleave with hers in the
    # ledger's own ordering. A post-filter would have returned short pages here.
    assert [run["run_id"] for run in second["runs"]] == ["alice-1", "alice-0"]


def test_task_filter_is_applied_inside_the_authorized_set(tmp_path):
    with _history(tmp_path) as (web, store, repository):
        _add_run(store, repository, "alice-other", web,
                 owner=web.user_id("alice"), tenant=web.tenant_id,
                 task_id="task-2", started_at=BASE_TIME + 20)
        _response, body = _runs(web, web.login("alice"), "?task_id=task-2")

    assert [run["run_id"] for run in body["runs"]] == ["alice-other"]


def test_since_bound_is_inclusive(tmp_path):
    with _history(tmp_path) as (web, _store, _repository):
        _response, body = _runs(web, web.login("alice"), "?since=1002")

    assert {run["run_id"] for run in body["runs"]} == {"alice-2", "public-0"}


@pytest.mark.parametrize("query", [
    "?limit=0", "?limit=-1", "?limit=501", "?limit=many",
    "?offset=-1", "?offset=abc", "?since=-2", "?since=later",
])
def test_a_malformed_page_is_a_400(tmp_path, query):
    with _history(tmp_path) as (web, _store, _repository):
        response, body = _runs(web, web.login("alice"), query)

    # Refused, not clamped: a silently reduced page size reads as "that is all
    # there is", which is the failure this check exists to prevent.
    assert response.status == "400 Bad Request"
    assert body["code"] == "invalid_request"


def test_a_session_is_required(tmp_path):
    with _history(tmp_path) as (web, _store, _repository):
        # No cookie at all: the gate refuses before the handler runs.
        response, _body = _runs(web, None)

    assert response.status in ("401 Unauthorized", "403 Forbidden")


# ---------------------------------------------------------------------------
# Detail: the ledger grant and the transcript grant are separate
# ---------------------------------------------------------------------------


def test_detail_of_a_partner_run_is_not_found(tmp_path):
    with _history(tmp_path) as (web, _store, _repository):
        response, body = _get_detail(web, web.login("alice"), "bob-0")

    # 404, not 403: the caller must not learn that Bob's run exists.
    assert response.status == "404 Not Found"
    assert body["code"] == "run_not_found"


def test_detail_without_a_run_id_is_a_400(tmp_path):
    with _history(tmp_path) as (web, _store, _repository):
        response = web.get("/api/scheduler/runs/detail", token=web.login("alice"))

    assert response.status == "400 Bad Request"
    assert WebAppHarness.json(response)["code"] == "invalid_request"


def test_detail_rejects_a_foreign_run_id(tmp_path):
    with _history(tmp_path) as (web, _store, _repository):
        response, _body = _get_detail(web, web.login("bob"), "alice-0")

    assert response.status == "404 Not Found"


def test_detail_withholds_the_body_when_the_session_is_not_the_callers(tmp_path):
    with _history(tmp_path) as (web, store, repository):
        # The run is public, so every member may see it -- but its transcript
        # lives in a session that is not the caller's. Being able to see the run
        # must not become a read of that conversation.
        _add_run(store, repository, "public-foreign", web, owner="",
                 tenant=web.tenant_id, scope="public",
                 session_id="private-session", started_at=BASE_TIME + 30)
        _add_message(web, "private-session", "public-foreign",
                     "the private transcript", owner=web.user_id("bob"),
                     tenant=web.tenant_id)
        _response, body = _get_detail(web, web.login("alice"), "public-foreign")

    assert body["run"]["run_id"] == "public-foreign"
    # The preview survives; only the transcript is withheld.
    assert body["run"]["full_output"] is None


def test_detail_returns_the_body_for_the_session_owner(tmp_path):
    with _history(tmp_path) as (web, store, repository):
        session_id = "alice-session"
        _add_run(store, repository, "alice-chatty", web,
                 owner=web.user_id("alice"), tenant=web.tenant_id,
                 session_id=session_id, started_at=BASE_TIME + 40)
        _add_message(web, session_id, "alice-chatty", "first", "second",
                     owner=web.user_id("alice"), tenant=web.tenant_id)
        _response, body = _get_detail(web, web.login("alice"), "alice-chatty")

    assert body["run"]["full_output"] == "first\nsecond"


def test_detail_does_not_borrow_a_neighbouring_runs_transcript(tmp_path):
    with _history(tmp_path) as (web, store, repository):
        session_id = "shared-session"
        _add_run(store, repository, "alice-quiet", web,
                 owner=web.user_id("alice"), tenant=web.tenant_id,
                 session_id=session_id, started_at=BASE_TIME + 50)
        _add_run(store, repository, "alice-earlier", web,
                 owner=web.user_id("alice"), tenant=web.tenant_id,
                 session_id=session_id, started_at=BASE_TIME + 49)
        # The text belongs to the *other* run in the same session; matching on
        # the session alone would hand it to the wrong record.
        _add_message(web, session_id, "alice-earlier", "not this run",
                     owner=web.user_id("alice"), tenant=web.tenant_id)
        _response, body = _get_detail(web, web.login("alice"), "alice-quiet")

    assert body["run"]["full_output"] is None


# ---------------------------------------------------------------------------
# Delete: the record goes, what was already said stays
# ---------------------------------------------------------------------------


def test_delete_removes_the_record_and_keeps_the_transcript(tmp_path):
    with _history(tmp_path) as (web, store, repository):
        session_id = "alice-doomed-session"
        _add_run(store, repository, "alice-doomed", web,
                 owner=web.user_id("alice"), tenant=web.tenant_id,
                 session_id=session_id, started_at=BASE_TIME + 60)
        _add_message(web, session_id, "alice-doomed", "already said",
                     owner=web.user_id("alice"), tenant=web.tenant_id)
        token = web.login("alice")

        response = web.post("/api/scheduler/runs/delete",
                            {"run_id": "alice-doomed"}, token=token)
        assert response.status == "200 OK"
        assert WebAppHarness.json(response) == {"status": "success"}
        _response, body = _runs(web, token)

        assert "alice-doomed" not in {run["run_id"] for run in body["runs"]}
        assert _scope_row_count(store, "alice-doomed") == 0
        # History cleanup must not retract delivered words: the ledger row is
        # gone, the message is not.
        assert _message_count(store, session_id) == 1


def test_delete_of_a_partner_run_is_not_found(tmp_path):
    with _history(tmp_path) as (web, store, _repository):
        response = web.post("/api/scheduler/runs/delete", {"run_id": "bob-0"},
                            token=web.login("alice"))

        assert response.status == "404 Not Found"
        assert WebAppHarness.json(response)["code"] == "run_not_found"
        # Refused means untouched, not "deleted quietly".
        assert _scope_row_count(store, "bob-0") == 1


def test_delete_of_a_running_run_is_refused(tmp_path):
    with _history(tmp_path) as (web, store, repository):
        _add_run(store, repository, "alice-running", web,
                 owner=web.user_id("alice"), tenant=web.tenant_id,
                 status=None, started_at=BASE_TIME + 70)
        response = web.post("/api/scheduler/runs/delete",
                            {"run_id": "alice-running"},
                            token=web.login("alice"))

    # A run still in flight is not history yet; deleting it would race the writer
    # that is about to close the row.
    assert response.status == "409 Conflict"
    assert WebAppHarness.json(response)["code"] == "run_running"


def test_delete_without_a_run_id_is_a_400(tmp_path):
    with _history(tmp_path) as (web, _store, _repository):
        response = web.post("/api/scheduler/runs/delete", {},
                            token=web.login("alice"))

    assert response.status == "400 Bad Request"
    assert WebAppHarness.json(response)["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# A deployment that has never run a task
#
# The attribution table is created by the write path, so the very first history
# read on a fresh install happens with no such table. That is a *readable* store
# with nothing in it -- not a storage fault -- and the difference matters:
# answering 503 (or, worse, a raw 500) would tell an operator their database is
# broken when the truth is "no scheduled task has run yet".
# ---------------------------------------------------------------------------


def test_a_fresh_deployment_really_has_no_attribution_table(tmp_path):
    # Guards the three tests below: they only prove something about the
    # fresh-install state while that state actually is fresh.
    with _fresh_history(tmp_path) as (web, store):
        assert _scope_table_exists(store) is False


def test_history_on_a_fresh_deployment_is_an_empty_page(tmp_path):
    with _fresh_history(tmp_path) as (web, store):
        response, body = _runs(web, web.login("alice"))

    assert response.status == "200 OK", body
    assert body["runs"] == []
    # The page still declares its scope; "empty" must not degrade into "unknown".
    assert body["history_scope"] == "attributed_only"
    # The read materialised the table rather than failing on it, so the next
    # request -- and the scheduler's own write path -- is a normal query.
    assert _scope_table_exists(store) is True


def test_detail_on_a_fresh_deployment_is_not_found(tmp_path):
    with _fresh_history(tmp_path) as (web, _store):
        response, body = _get_detail(web, web.login("alice"), "never-ran")

    # Nothing is attributed yet, so the run does not exist for this caller.
    assert response.status == "404 Not Found", body
    assert body["code"] == "run_not_found"


def test_delete_on_a_fresh_deployment_is_not_found(tmp_path):
    with _fresh_history(tmp_path) as (web, _store):
        response = web.post("/api/scheduler/runs/delete",
                            {"run_id": "never-ran"}, token=web.login("alice"))

    # 404, never 503: the store answered fine, it just has no such run. A 503
    # here would be the repository reporting its own missing schema as a fault.
    assert response.status == "404 Not Found", WebAppHarness.json(response)


# ---------------------------------------------------------------------------
# Still closed by default
# ---------------------------------------------------------------------------


def test_history_is_unavailable_until_the_action_is_opened(tmp_path):
    # No capability opened: the declaration keeps these slices closed until the
    # batch has acceptance evidence, and a closed *action* must answer 503 rather
    # than 404 (the route exists; the feature is not switched on).
    with _history(tmp_path, open_actions=False) as (web, _store, _repository):
        response = web.get("/api/scheduler/runs", token=web.login("alice"))

    assert response.status == "503 Service Unavailable"


def test_closed_detail_and_delete_do_not_reach_the_handler(tmp_path):
    with _history(tmp_path, open_actions=False) as (web, _store, _repository):
        token = web.login("alice")
        detail = web.get("/api/scheduler/runs/detail?run_id=bob-0", token=token)
        delete = web.post("/api/scheduler/runs/delete", {"run_id": "bob-0"},
                          token=token)

    # Both 503, not 404/403: the refusal is the capability gate, and it happens
    # before any authorization question is asked.
    assert detail.status == "503 Service Unavailable"
    assert delete.status == "503 Service Unavailable"
