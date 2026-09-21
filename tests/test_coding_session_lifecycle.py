"""Reserving, creating and retrying a coding session.

The hard part of this feature is not talking to OpenCode, it is making "create
a session" safe to repeat. A browser retries, a network drops the response, a
platform restarts mid-request, and in every case the user must end up with one
conversation rather than two — and never with somebody else's.

The design answers that by never asking upstream to invent an id. The platform
derives both ids from the *verified* identity plus the client's ``request_id``,
so the same request always names the same session, a retry adopts whatever was
already created, and two different subjects who happen to use the same
``request_id`` cannot collide. This file pins that contract down.
"""

import sqlite3
import threading
import time

import pytest

from agent.coding import (
    CODING_DISABLED,
    CODING_SERVICE_CHANGED,
    CodingError,
    CodingSettings,
)
from agent.coding.opencode import RemoteSession
from agent.coding.sessions import CodingSessionService, derive_ids, session_url
from agent.memory.conversation_store import ConversationStore
from common.runtime_identity import identity_scope

MS = 1000
PROJECT = "/srv/checkouts/erp"


class _FakeClient:
    """Stands in for OpenCode: records calls, answers from a script."""

    def __init__(self, *, fail=None, fail_once=None):
        self.calls = []
        self.fail = fail
        self.fail_once = fail_once
        self.created = {}

    def create_session(self, session_id, project_dir):
        self.calls.append({"op": "create", "id": session_id, "dir": project_dir})
        if self.fail_once is not None:
            error, self.fail_once = self.fail_once, None
            raise error
        if self.fail is not None:
            raise self.fail
        created_ms = self.created.setdefault(session_id, int(time.time() * MS))
        return RemoteSession(
            id=session_id,
            title="New session - 2026-09-20T06:00:00.000Z",
            directory=project_dir,
            created_ms=created_ms,
            updated_ms=created_ms,
        )


def _settings(**overrides):
    values = {
        "enabled": True,
        "service_id": "default",
        "api_url": "http://127.0.0.1:4096",
        "web_url": "https://code.example.com",
    }
    values.update(overrides)
    return CodingSettings(**values)


@pytest.fixture
def store(tmp_path):
    return ConversationStore(tmp_path / "conversations.db")


@pytest.fixture
def client():
    return _FakeClient()


def _service(store, client, **overrides):
    return CodingSessionService(store, settings=_settings(**overrides), client=client)


def _reserve(service, *, user="u-1", tenant="t-1", agent="erp-coder",
             request_id="req-1", project=PROJECT):
    with identity_scope(agent_id=agent, user_id=user, tenant_id=tenant):
        return service.reserve(agent_id=agent, project_dir=project,
                               request_id=request_id)


# --- id derivation --------------------------------------------------------


def test_the_same_request_always_derives_the_same_ids():
    first = derive_ids(service_id="default", tenant_id="t-1", user_id="u-1",
                       agent_id="erp-coder", request_id="req-1")
    second = derive_ids(service_id="default", tenant_id="t-1", user_id="u-1",
                        agent_id="erp-coder", request_id="req-1")

    assert first == second
    session_id, external_id = first
    assert session_id.startswith("oc_")
    assert external_id.startswith("ses_rsm_")


@pytest.mark.parametrize(
    "changed",
    [
        {"service_id": "second"},
        {"tenant_id": "t-2"},
        {"user_id": "u-2"},
        {"agent_id": "other"},
        {"request_id": "req-2"},
    ],
)
def test_every_part_of_the_identity_changes_the_derived_ids(changed):
    """Two subjects reusing a client UUID must not land on one session."""
    base = {"service_id": "default", "tenant_id": "t-1", "user_id": "u-1",
            "agent_id": "erp-coder", "request_id": "req-1"}

    assert derive_ids(**base) != derive_ids(**{**base, **changed})


def test_the_encoding_cannot_be_confused_by_adjacent_fields():
    """Without length prefixes, ("ab", "c") and ("a", "bc") would hash alike."""
    assert derive_ids(service_id="default", tenant_id="t", user_id="ab",
                      agent_id="c", request_id="r") != derive_ids(
        service_id="default", tenant_id="t", user_id="a",
        agent_id="bc", request_id="r")


def test_the_iframe_url_names_the_session_on_the_embedded_route():
    """The directory is encoded the way OpenCode's own router encodes it
    (base64url, no padding), so the app opens the right project and session."""
    url = session_url("https://code.example.com", "ses_rsm_abc", PROJECT)

    assert url.startswith("https://code.example.com/L3Nydi9jaGVja291dHMvZXJw/session/ses_rsm_abc")
    assert "rsm_embed=1" in url


# --- reserve and create ---------------------------------------------------


def test_a_first_request_reserves_then_confirms_the_session(store, client):
    service = _service(store, client)
    result = _reserve(service)

    session_id, external_id = derive_ids(
        service_id="default", tenant_id="t-1", user_id="u-1",
        agent_id="erp-coder", request_id="req-1")
    assert result["session_id"] == session_id
    assert result["state"] == "ready"
    assert result["iframe_url"].startswith("https://code.example.com/")
    assert client.calls == [{"op": "create", "id": external_id, "dir": PROJECT}]

    link = store.get_coding_link(session_id)
    assert (link["state"], link["project_dir"]) == ("ready", PROJECT)
    assert link["request_id"] == "req-1"
    assert link["external_session_id"] == external_id


def test_the_confirmed_title_and_time_reach_the_list_cache(store, client):
    service = _service(store, client)
    _reserve(service)

    with identity_scope(agent_id="erp-coder", user_id="u-1", tenant_id="t-1"):
        page = store.list_sessions(user_id="u-1")

    assert page["sessions"][0]["title"].startswith("New session")
    assert page["sessions"][0]["msg_count"] == 0


def test_repeating_a_finished_request_returns_the_same_session(store, client):
    """This is the browser's retry, and it must not create a second session."""
    service = _service(store, client)
    first = _reserve(service)
    second = _reserve(service)

    assert first["session_id"] == second["session_id"]
    assert len(client.calls) == 1


def test_a_lost_response_is_retried_under_the_same_external_id(store, client):
    """The reservation survives the failure, so the retry adopts the session
    the first attempt created upstream instead of making another one."""
    client = _FakeClient(fail_once=CodingError("coding_upstream_unavailable", "timed out", 504))
    service = _service(store, client)

    with pytest.raises(CodingError):
        _reserve(service)

    session_id, external_id = derive_ids(
        service_id="default", tenant_id="t-1", user_id="u-1",
        agent_id="erp-coder", request_id="req-1")
    link = store.get_coding_link(session_id)
    assert link["state"] == "creating"

    retried = _reserve(service)

    assert retried["session_id"] == session_id
    assert retried["state"] == "ready"
    assert [call["id"] for call in client.calls] == [external_id, external_id]


def test_a_platform_restart_continues_the_same_reservation(store, client, tmp_path):
    """Nothing lives in memory: a fresh service over the same database picks up
    the same link, the same external id and the same request."""
    service = _service(store, client)
    client.fail_once = CodingError("coding_upstream_unavailable", "restart", 504)
    with pytest.raises(CodingError):
        _reserve(service)

    reopened = ConversationStore(tmp_path / "conversations.db")
    restarted = _service(reopened, client)
    result = _reserve(restarted)

    session_id, external_id = derive_ids(
        service_id="default", tenant_id="t-1", user_id="u-1",
        agent_id="erp-coder", request_id="req-1")
    assert result["session_id"] == session_id
    assert [call["id"] for call in client.calls] == [external_id, external_id]


def test_a_retry_never_creates_a_second_link(store, client):
    """Two failed attempts on one request leave one reservation, not two."""
    client.fail = CodingError("coding_upstream_unavailable", "boom", 502)
    service = _service(store, client)
    for _ in range(2):
        with pytest.raises(CodingError):
            _reserve(service)

    conn = sqlite3.connect(store._db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM opencode_session_links").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    finally:
        conn.close()
    assert store.get_coding_link(
        derive_ids(service_id="default", tenant_id="t-1", user_id="u-1",
                   agent_id="erp-coder", request_id="req-1")[0]
    )["state"] == "creating"


def test_changing_the_project_refuses_to_reuse_an_unfinished_request(store, client):
    """"Resume the same request" is a claim about one project; accepting a
    different directory would silently point the reserved id at other code."""
    client.fail = CodingError("coding_upstream_unavailable", "boom", 502)
    service = _service(store, client)
    with pytest.raises(CodingError):
        _reserve(service)

    with pytest.raises(CodingError) as caught:
        _reserve(service, project="/srv/checkouts/other")

    assert caught.value.status == 400
    assert len(client.calls) == 1


def test_changing_the_project_does_not_move_a_finished_session(store, client):
    """Once confirmed, the request id is spent: a new project is a new request,
    and the existing conversation keeps its directory."""
    service = _service(store, client)
    first = _reserve(service)
    again = _reserve(service, project="/srv/checkouts/other")

    assert again["session_id"] == first["session_id"]
    assert store.get_coding_link(first["session_id"])["project_dir"] == PROJECT


def test_different_subjects_sharing_a_request_id_do_not_share_a_session(store, client):
    service = _service(store, client)

    one = _reserve(service, user="u-1", request_id="same-id")
    two = _reserve(service, user="u-2", request_id="same-id")

    assert one["session_id"] != two["session_id"]
    assert len(client.calls) == 2


def test_different_agents_sharing_a_request_id_do_not_share_a_session(store, client):
    service = _service(store, client)

    one = _reserve(service, agent="erp-coder", request_id="same-id")
    two = _reserve(service, agent="other-coder", request_id="same-id")

    assert one["session_id"] != two["session_id"]


def test_a_concurrent_duplicate_lands_on_the_reserved_link(store, client):
    """Two tabs pressing the same button must converge, not race.

    The database's primary key is the arbiter: whichever request loses the
    insert adopts the row the winner wrote instead of failing or adding a
    second link.
    """
    service = _service(store, client)
    session_id, external_id = derive_ids(
        service_id="default", tenant_id="t-1", user_id="u-1",
        agent_id="erp-coder", request_id="req-1")
    with identity_scope(agent_id="erp-coder", user_id="u-1", tenant_id="t-1"):
        store.create_coding_link(
            session_id=session_id, external_session_id=external_id,
            service_id="default", project_dir=PROJECT, request_id="req-1")

    result = _reserve(service)

    assert result["session_id"] == session_id
    assert result["state"] == "ready"
    assert [call["id"] for call in client.calls] == [external_id]


def test_two_threads_pressing_once_still_create_one_session(store, client):
    """The same race, but with the real interleaving rather than a pre-insert."""
    service = _service(store, client)
    results, errors = [], []

    def attempt():
        try:
            results.append(_reserve(service))
        except Exception as exc:  # noqa: BLE001 - the assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len({result["session_id"] for result in results}) == 1
    assert all(result["state"] == "ready" for result in results)
    conn = sqlite3.connect(store._db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM opencode_session_links").fetchone()[0] == 1
    finally:
        conn.close()


# --- capability and service identity --------------------------------------


def test_a_disabled_capability_refuses_before_touching_the_database(store, client):
    service = _service(store, client, enabled=False)

    with pytest.raises(CodingError) as caught:
        _reserve(service)

    assert caught.value.code == CODING_DISABLED
    assert caught.value.status == 503
    assert client.calls == []
    assert store.list_sessions(user_id="u-1")["sessions"] == []


def test_an_unconfigured_service_counts_as_disabled(store, client):
    service = _service(store, client, api_url="", web_url="")

    with pytest.raises(CodingError) as caught:
        _reserve(service)

    assert caught.value.code == CODING_DISABLED


def test_a_request_without_a_request_id_is_refused(store, client):
    """The id is derived from it: without one there is nothing to retry."""
    service = _service(store, client)

    with pytest.raises(CodingError) as caught:
        _reserve(service, request_id="")

    assert caught.value.status == 400
    assert client.calls == []


def test_a_service_change_gives_new_ids_rather_than_reusing_the_old_session(store, client):
    """``service_id`` names a data instance and is part of the derivation, so
    the same request after a move reserves a session on the new instance.

    The old link is kept, untouched, and reading it is what reports
    ``coding_service_changed`` (covered with the open path) — reusing it here
    would silently hand the user a conversation from another database.
    """
    service = _service(store, client)
    first = _reserve(service)

    moved = _service(store, client, service_id="second-instance")
    second = _reserve(moved)

    assert second["session_id"] != first["session_id"]
    assert second["state"] == "ready"
    assert store.get_coding_link(first["session_id"])["state"] == "ready"
    assert store.get_coding_link(first["session_id"])["service_id"] == "default"
    assert store.get_coding_link(second["session_id"])["service_id"] == "second-instance"
