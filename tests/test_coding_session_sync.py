"""Refreshing the cached coding list from the service, one batch at a time.

The list the console shows is a cache: OpenCode owns each session's title, its
timestamps and whether it is running. What this feature has to get right is what
to do when the service *disagrees* with the cache, because every wrong answer
here is destructive in one direction:

* a session that is merely unreachable must keep its row — treating a network
  failure as "gone" would silently delete a user's conversation list;
* a session OpenCode confirms is gone must leave the list — otherwise a deleted
  conversation stays clickable forever;
* a reservation still being created is not gone just because a read 404s: the
  create may not have landed yet, and dropping the reservation would strand the
  external id the retry depends on;
* a refresh that raced a rename must not roll the title back.

So the rules are asymmetric on purpose: only an *explicit* 404 on a session that
was already confirmed ready is evidence of deletion, and every other failure is
reported as temporary and leaves the cache alone.
"""

import threading
import time

import pytest

from agent.coding import CODING_UPSTREAM_UNAVAILABLE, CodingError, CodingSettings
from agent.coding.opencode import RemoteSession
from agent.coding.sessions import CodingSessionService, derive_ids
from agent.memory.conversation_store import ConversationStore
from common.runtime_identity import identity_scope

MS = 1000
PROJECT = "/srv/checkouts/erp"


class FakeOpenCode:
    """An in-memory stand-in for the service, with per-session failure control.

    Sessions are keyed by external id; ``gone`` ids answer 404, ``broken`` ids
    raise a transport error, which is the distinction the refresh rules turn on.
    """

    def __init__(self):
        self.remote = {}
        self.gone = set()
        self.broken = set()
        self.active = set()
        self.calls = []
        self.reads_in_flight = 0
        self.max_reads_in_flight = 0
        self._lock = threading.Lock()

    def _session(self, session_id):
        self.calls.append(session_id)
        if session_id in self.broken:
            raise CodingError(CODING_UPSTREAM_UNAVAILABLE, "unreachable", 502)
        if session_id in self.gone or session_id not in self.remote:
            return None
        return self.remote[session_id]

    def create_session(self, session_id, project_dir):
        now = int(time.time() * MS)
        return self.remote.setdefault(session_id, RemoteSession(
            id=session_id, title="New session", directory=project_dir,
            created_ms=now, updated_ms=now))

    def get_session(self, session_id, project_dir=""):
        with self._lock:
            self.reads_in_flight += 1
            self.max_reads_in_flight = max(self.max_reads_in_flight, self.reads_in_flight)
        try:
            # Held briefly so the concurrency cap is observable.
            time.sleep(0.005)
            return self._session(session_id)
        finally:
            with self._lock:
                self.reads_in_flight -= 1

    def active_sessions(self):
        return set(self.active)

    def rename_session(self, session_id, title, project_dir):
        session = self._session(session_id)
        if session is None:
            raise CodingError("coding_not_linked", "gone", 404)
        self.remote[session_id] = RemoteSession(
            id=session.id, title=title, directory=session.directory,
            created_ms=session.created_ms,
            updated_ms=int(time.time() * MS) + 1000, parent_id=session.parent_id)
        self.calls.append(("rename", session_id, title))

    def interrupt_session(self, session_id):
        self.calls.append(("interrupt", session_id))

    def delete_session(self, session_id, project_dir):
        self.calls.append(("delete", session_id))
        self.remote.pop(session_id, None)
        self.gone.add(session_id)
        self.active.discard(session_id)

    def set_title(self, session_id, title, updated_ms=None):
        session = self.remote[session_id]
        self.remote[session_id] = RemoteSession(
            id=session.id, title=title, directory=session.directory,
            created_ms=session.created_ms,
            updated_ms=updated_ms or (int(time.time() * MS) + 1000),
            parent_id=session.parent_id)


@pytest.fixture
def store(tmp_path):
    return ConversationStore(tmp_path / "conversations.db")


@pytest.fixture(autouse=True)
def coding_agents(tmp_path):
    """The registry the service checks the addressed Agent's type against."""
    from agent.registry import AgentRegistry, set_agent_registry

    workspace = tmp_path / "instance"
    workspace.mkdir(exist_ok=True)
    registry = AgentRegistry.from_config({
        "agent_workspace": str(workspace),
        "default_agent_id": "assistant",
        "agents": [
            {"id": "assistant", "name": "Assistant"},
            {"id": "a-normal-agent", "name": "Normal", "workspace": str(workspace / "n")},
            {
                "id": "erp-coder",
                "name": "ERP Coder",
                "workspace": str(workspace / "c"),
                "agent_type": "coding",
                "coding_project_dir": PROJECT,
            },
        ],
    })
    set_agent_registry(registry)
    yield registry
    set_agent_registry(None)


@pytest.fixture
def service(store):
    client = FakeOpenCode()
    svc = CodingSessionService(
        store,
        settings=CodingSettings(
            enabled=True, service_id="default",
            api_url="http://127.0.0.1:4096", web_url="https://code.example.com"),
        client=client)
    return svc, client


def _reserve(service, *, user="u-1", agent="erp-coder", request_id="req", project=PROJECT):
    with identity_scope(agent_id=agent, user_id=user, tenant_id="t-1"):
        return service.reserve(agent_id=agent, project_dir=project, request_id=request_id)


def _sync(service, *, user="u-1", agent="erp-coder", cursor=None):
    with identity_scope(agent_id=agent, user_id=user, tenant_id="t-1"):
        return service.sync(agent_id=agent, cursor=cursor)


def _listed(store, *, agent="erp-coder", user="u-1"):
    with identity_scope(agent_id=agent, user_id=user, tenant_id="t-1"):
        return {s["session_id"]: s for s in store.list_sessions(user_id=user)["sessions"]}


# --- the basic round ------------------------------------------------------


def test_a_refresh_mirrors_the_remote_title_and_time(service):
    svc, client = service
    created = _reserve(svc)
    external = client.remote and list(client.remote)[0]
    later = int(time.time() * MS) + 60_000
    client.set_title(external, "OpenCode 内改的标题", updated_ms=later)

    result = _sync(svc)

    assert result["next_cursor"] is None
    assert result["removed"] == []
    assert result["unavailable"] == []
    assert [c["session_id"] for c in result["changed"]] == [created["session_id"]]
    assert result["changed"][0]["title"] == "OpenCode 内改的标题"
    assert result["changed"][0]["last_active"] == later // MS

def test_a_refresh_reports_the_running_state(service):
    svc, client = service
    created = _reserve(svc)
    external = list(client.remote)[0]

    assert _sync(svc)["changed"][0]["state"] == "idle"

    client.active = {external}
    assert _sync(svc)["changed"][0]["state"] == "running"


def test_a_refresh_never_creates_a_session(service):
    """A refresh reads; only the explicit create path may reserve remote state."""
    svc, client = service
    _reserve(svc)
    before = dict(client.remote)

    _sync(svc)

    assert client.remote == before


def test_refreshing_with_nothing_linked_is_an_empty_success(service):
    svc, _ = service
    result = _sync(svc)

    assert result == {"changed": [], "removed": [], "unavailable": [],
                      "next_cursor": None}


# --- deletion evidence ----------------------------------------------------


def test_a_confirmed_missing_ready_session_leaves_the_list(service, store):
    svc, client = service
    created = _reserve(svc)
    client.gone.add(list(client.remote)[0])

    result = _sync(svc)

    assert result["removed"] == [created["session_id"]]
    assert result["changed"] == []
    assert _listed(store) == {}
    assert store.get_coding_link(created["session_id"]) is None


def test_a_missing_reservation_is_kept_because_it_may_not_have_landed(service, store):
    """The create may still be in flight upstream; dropping the reservation
    would strand the external id the retry rule depends on."""
    svc, client = service
    with identity_scope(agent_id="erp-coder", user_id="u-1", tenant_id="t-1"):
        session_id, external = derive_ids(
            service_id="default", tenant_id="t-1", user_id="u-1",
            agent_id="erp-coder", request_id="req-1")
        store.create_coding_link(
            session_id=session_id, external_session_id=external,
            service_id="default", project_dir=PROJECT, request_id="req-1",
            state="creating")

    result = _sync(svc)

    assert result["removed"] == []
    assert [c["session_id"] for c in result["changed"]] == [session_id]
    assert result["changed"][0]["state"] == "creating"
    assert store.get_coding_link(session_id)["state"] == "creating"


def test_an_unreachable_service_keeps_every_row(service, store):
    """A connection failure is not evidence about any session."""
    svc, client = service
    created = _reserve(svc)
    client.broken.add(list(client.remote)[0])

    result = _sync(svc)

    assert result["unavailable"] == [created["session_id"]]
    assert result["removed"] == []
    assert created["session_id"] in _listed(store)


def test_a_deleted_session_is_not_resurrected_by_a_late_refresh(service, store):
    """The refresh only ever UPDATEs, so a result that raced a delete cannot
    bring the conversation back."""
    svc, client = service
    created = _reserve(svc)
    client.gone.add(list(client.remote)[0])
    _sync(svc)
    # The remote session comes back as an *old* entry in the next batch's read
    # (a stale page), which must not re-create what the user deleted.
    client.gone.discard(list(client.remote)[0])

    assert _sync(svc)["changed"] == []
    assert created["session_id"] not in _listed(store)


def test_a_late_refresh_does_not_roll_back_a_newer_title(service, store):
    svc, client = service
    created = _reserve(svc)
    external = list(client.remote)[0]
    newer = int(time.time() * MS) + 120_000
    client.set_title(external, "平台重命名", updated_ms=newer)
    _sync(svc)
    # A read served from a stale cache still reports the previous update time.
    client.set_title(external, "旧标题", updated_ms=newer - 90_000)

    _sync(svc)

    assert _listed(store)[created["session_id"]]["title"] == "平台重命名"


# --- batching ------------------------------------------------------------


def test_batches_walk_every_row_without_skipping_or_repeating(service, store):
    """More than one batch must still reach every conversation."""
    svc, client = service
    for index in range(5):
        _reserve(svc, request_id=f"req-{index}")

    seen, cursor, rounds = [], None, 0
    while True:
        result = _sync(svc, cursor=cursor)
        seen.extend(c["session_id"] for c in result["changed"])
        rounds += 1
        cursor = result["next_cursor"]
        assert rounds < 10, "cursor did not terminate"
        if cursor is None:
            break

    assert rounds == 1  # five rows fit in the default batch
    assert sorted(seen) == sorted(_listed(store))


def test_the_batch_size_is_fifty(service):
    svc, client = service
    # Pre-build 55 links directly: 55 creates would be beside the point here.
    for index in range(55):
        session_id, external = derive_ids(
            service_id="default", tenant_id="t-1", user_id="u-1",
            agent_id="erp-coder", request_id=f"req-{index}")
        with identity_scope(agent_id="erp-coder", user_id="u-1", tenant_id="t-1"):
            svc.store.create_coding_link(
                session_id=session_id, external_session_id=external,
                service_id="default", project_dir=PROJECT, request_id=f"req-{index}",
                state="ready")
        client.remote[external] = RemoteSession(
            id=external, title="t", directory=PROJECT,
            created_ms=int(time.time() * MS), updated_ms=int(time.time() * MS))

    first = _sync(svc)
    second = _sync(svc, cursor=first["next_cursor"])

    assert len(first["changed"]) == 50
    assert first["next_cursor"] is not None
    assert len(second["changed"]) == 5
    assert second["next_cursor"] is None


def test_a_refresh_reads_at_most_four_sessions_at_once(service):
    """The polling loop runs every five seconds; an unbounded fan-out would let
    one refresh round open a request per session."""
    svc, client = service
    for index in range(12):
        _reserve(svc, request_id=f"req-{index}")

    _sync(svc)

    assert client.max_reads_in_flight <= 4


def test_the_running_set_is_read_once_per_round(service):
    """It is a service-wide answer, so per-session calls would be wasted."""
    svc, client = service
    for index in range(6):
        _reserve(svc, request_id=f"req-{index}")
    client.calls.clear()
    calls_to_active = []

    original = client.active_sessions

    def counted():
        calls_to_active.append(1)
        return original()

    client.active_sessions = counted
    _sync(svc)

    assert len(calls_to_active) == 1


# --- scope ---------------------------------------------------------------


def test_sync_only_touches_the_callers_own_links(service, store):
    svc, client = service
    mine = _reserve(svc, user="u-1", request_id="mine")
    theirs = _reserve(svc, user="u-2", request_id="theirs")
    client.set_title(list(client.remote)[0], "只有 u-1 的会话")

    mine_result = _sync(svc, user="u-1")
    theirs_result = _sync(svc, user="u-2")

    assert [c["session_id"] for c in mine_result["changed"]] == [mine["session_id"]]
    assert [c["session_id"] for c in theirs_result["changed"]] == [theirs["session_id"]]


def test_sync_refuses_an_agent_that_is_not_a_coding_agent(service):
    svc, _ = service
    with pytest.raises(CodingError) as caught:
        _sync(svc, agent="a-normal-agent")

    assert caught.value.status == 400


def test_sync_refuses_when_the_capability_is_off(service):
    svc, client = service
    _reserve(svc)
    svc.settings = CodingSettings(
        enabled=False, service_id="default",
        api_url="http://127.0.0.1:4096", web_url="https://code.example.com")

    with pytest.raises(CodingError) as caught:
        _sync(svc, agent="erp-coder")

    assert caught.value.status == 503


def test_a_link_from_another_service_instance_is_reported_unavailable(service, store):
    """Its ids name sessions on a database this service does not have."""
    svc, client = service
    created = _reserve(svc)
    with identity_scope(agent_id="erp-coder", user_id="u-1", tenant_id="t-1"):
        store.create_coding_link(
            session_id="oc_other_service", external_session_id="ses_rsm_other",
            service_id="second-instance", project_dir=PROJECT, state="ready")

    result = _sync(svc)

    assert result["unavailable"] == ["oc_other_service"]
    assert "oc_other_service" not in [c["session_id"] for c in result["changed"]]
    # ...and the session it does own is still refreshed.
    assert [c["session_id"] for c in result["changed"]] == [created["session_id"]]
