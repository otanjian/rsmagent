# encoding:utf-8
"""The four coding endpoints, driven through the real WSGI app.

These tests exist for the properties that only appear on the wire: who is
allowed to reach the endpoints, what happens when the same request arrives
twice, and — the part a unit test cannot show — that a refusal never confirms
another member's session exists.

The OpenCode client is the only thing replaced. The route policy, the identity
database, the tenant binding, the owner check and the conversation store are all
real, because those are exactly the layers a handler-level test with patched
``web`` would skip.
"""

import time

import pytest

from agent.coding.opencode import RemoteSession
from agent.coding.sessions import encode_directory
from tests._helpers import WebAppHarness

PROJECT_DIR = "/srv/checkouts/erp"
REQUEST_ID = "b347bcfb-48d9-4cf2-8849-ec49ab29a144"


class FakeOpenCode:
    """An in-memory service, so no socket is opened.

    Failures are injected per operation so the management paths can be shown to
    keep their record when the service refuses, which is the property a
    "delete succeeded" assertion alone would not prove.
    """

    def __init__(self):
        self.remote = {}
        self.gone = set()
        self.active = set()
        self.calls = []
        self.fail_create = False
        self.fail_rename = False
        self.fail_delete = False
        self.interrupts = []

    def _fail(self):
        from agent.coding import CODING_UPSTREAM_UNAVAILABLE, CodingError

        raise CodingError(CODING_UPSTREAM_UNAVAILABLE, "service unreachable", 502)

    def create_session(self, session_id, project_dir):
        if self.fail_create:
            self._fail()
        now = int(time.time() * 1000)
        return self.remote.setdefault(session_id, RemoteSession(
            id=session_id, title="New session", directory=project_dir,
            created_ms=now, updated_ms=now))

    def get_session(self, session_id, project_dir=""):
        if session_id in self.gone or session_id not in self.remote:
            return None
        return self.remote[session_id]

    def active_sessions(self):
        return set(self.active)

    def rename_session(self, session_id, title, project_dir):
        if self.fail_rename:
            self._fail()
        session = self.remote.get(session_id)
        if session is None:
            from agent.coding import CODING_NOT_LINKED, CodingError

            raise CodingError(CODING_NOT_LINKED, "gone", 404)
        self.calls.append(("rename", session_id, title))
        self.remote[session_id] = RemoteSession(
            id=session.id, title=title, directory=session.directory,
            created_ms=session.created_ms,
            updated_ms=int(time.time() * 1000) + 1000)

    def interrupt_session(self, session_id):
        self.calls.append(("interrupt", session_id))
        self.interrupts.append(session_id)

    def delete_session(self, session_id, project_dir):
        if self.fail_delete:
            self._fail()
        self.calls.append(("delete", session_id))
        self.remote.pop(session_id, None)
        self.gone.add(session_id)
        self.active.discard(session_id)


@pytest.fixture
def web(tmp_path, monkeypatch):
    # A real checkout on disk, so "the delete never touches the project" is
    # asserted against something that can actually be inspected afterwards.
    project = tmp_path / "checkout"
    project.mkdir()
    (project / "main.py").write_text("print('hello')\n", encoding="utf-8")

    harness = WebAppHarness(tmp_path / "instance", settings={"opencode": {
        "enabled": True,
        "service_id": "default",
        "api_url": "http://127.0.0.1:4096",
        "web_url": "https://code.example.com",
    }})
    harness.add_agent("shared-agent")
    harness.add_coding_agent("erp-coder", str(project))
    harness.project_dir = str(project)

    client = FakeOpenCode()
    from agent.coding import CodingSettings
    from agent.coding.sessions import CodingSessionService
    import channel.web.fork.handlers.coding as coding_handler

    def service(store):
        return CodingSessionService(
            store,
            settings=CodingSettings(
                enabled=True, service_id="default",
                api_url="http://127.0.0.1:4096",
                web_url="https://code.example.com"),
            client=client)

    monkeypatch.setattr(coding_handler, "_coding_service", service)

    harness.opencode = client
    yield harness
    harness.close()


def _json(response):
    return WebAppHarness.json(response)


def _status(response):
    return int(response.status.split()[0])


def _member_with_coding_access(web, name):
    """A member who may chat and use this Agent, as the product intends."""
    code = f"{name}-coding"
    web.role(code, ["chat.use", "history.read", "agent.read", "agent.use"],
             grants=[("agent", "agent:erp-coder", "use")])
    web.member(name, [code])
    return web.login(name)


# -- create ----------------------------------------------------------------


def test_the_owner_can_create_a_coding_session(web):
    token = _member_with_coding_access(web, "alice")

    response = web.post("/api/coding/sessions",
                        {"agent_id": "erp-coder", "request_id": REQUEST_ID},
                        token=token)

    assert _status(response) == 200
    body = _json(response)
    assert body["status"] == "success"
    assert body["session_id"].startswith("oc_")
    assert body["state"] == "ready"
    assert body["agent_id"] == "erp-coder"
    # The frame URL comes from the operator's configured web_url, never from the
    # request, and names the real remote session on OpenCode's own route shape.
    assert body["iframe_url"].startswith("https://code.example.com/")
    assert "/session/" in body["iframe_url"]
    assert "rsm_embed=1" in body["iframe_url"]
    assert "127.0.0.1:4096" not in body["iframe_url"]


def test_the_same_request_twice_returns_one_session(web):
    """The browser's retry, which must not create a second conversation."""
    token = _member_with_coding_access(web, "alice")
    payload = {"agent_id": "erp-coder", "request_id": REQUEST_ID}

    first = _json(web.post("/api/coding/sessions", payload, token=token))
    second = _json(web.post("/api/coding/sessions", payload, token=token))

    assert first["session_id"] == second["session_id"]
    assert len(web.opencode.remote) == 1


def test_two_members_sharing_a_request_id_do_not_share_a_session(web):
    alice = _member_with_coding_access(web, "alice")
    bob = _member_with_coding_access(web, "bob")
    payload = {"agent_id": "erp-coder", "request_id": REQUEST_ID}

    hers = _json(web.post("/api/coding/sessions", payload, token=alice))
    his = _json(web.post("/api/coding/sessions", payload, token=bob))

    assert hers["session_id"] != his["session_id"]


def test_a_request_without_a_request_id_is_refused(web):
    token = _member_with_coding_access(web, "alice")

    response = web.post("/api/coding/sessions", {"agent_id": "erp-coder"},
                        token=token)

    assert _status(response) == 400
    assert _json(response)["code"] == "coding_invalid_request"


def test_a_normal_agent_is_not_a_coding_target(web):
    """The mirrored rule: these endpoints serve coding Agents only."""
    web.role("worker", ["chat.use", "history.read", "agent.use"],
             grants=[("agent", "agent:shared-agent", "use")])
    web.member("alice", ["worker"])

    response = web.post("/api/coding/sessions",
                        {"agent_id": "shared-agent", "request_id": REQUEST_ID},
                        token=web.login("alice"))

    assert _status(response) == 400
    assert _json(response)["code"] == "coding_invalid_request"


def test_a_caller_without_the_agents_use_grant_is_refused(web):
    """Execution is authorized like starting a chat, not like reading a list."""
    web.role("viewer", ["chat.use", "history.read", "agent.read"])
    web.member("bob", ["viewer"])

    response = web.post("/api/coding/sessions",
                        {"agent_id": "erp-coder", "request_id": REQUEST_ID},
                        token=web.login("bob"))

    assert _status(response) == 403


def test_an_unauthenticated_caller_never_reaches_the_service(web):
    response = web.post("/api/coding/sessions",
                        {"agent_id": "erp-coder", "request_id": REQUEST_ID})

    assert _status(response) in (401, 403)
    assert web.opencode.remote == {}


def test_a_disabled_capability_refuses_without_leaving_a_row(web, monkeypatch):
    """``enabled=false`` closes creation, and writes nothing on the way out."""
    from agent.coding import CODING_DISABLED
    from agent.coding import CodingSettings
    from agent.coding.sessions import CodingSessionService
    import channel.web.fork.handlers.coding as coding_handler

    monkeypatch.setattr(coding_handler, "_coding_service", lambda store: (
        CodingSessionService(
            store,
            settings=CodingSettings(
                enabled=False, service_id="default",
                api_url="http://127.0.0.1:4096",
                web_url="https://code.example.com"),
            client=web.opencode)))
    token = _member_with_coding_access(web, "alice")

    response = web.post("/api/coding/sessions",
                        {"agent_id": "erp-coder", "request_id": REQUEST_ID},
                        token=token)

    assert _status(response) == 503
    assert _json(response)["code"] == CODING_DISABLED
    assert web.opencode.remote == {}
    listed = _json(web.get("/api/sessions?agent_id=erp-coder", token=token))
    assert listed["sessions"] == []


# -- open ------------------------------------------------------------------


def _create(web, token):
    return _json(web.post("/api/coding/sessions",
                          {"agent_id": "erp-coder", "request_id": REQUEST_ID},
                          token=token))


def test_the_owner_can_open_their_own_session(web):
    token = _member_with_coding_access(web, "alice")
    created = _create(web, token)

    response = web.get(
        f"/api/coding/sessions/{created['session_id']}/open?agent_id=erp-coder",
        token=token)

    assert _status(response) == 200
    body = _json(response)
    assert body["session_id"] == created["session_id"]
    assert body["external_session_id"] == created["external_session_id"]
    assert body["retryable"] is False
    assert body["iframe_url"] == created["iframe_url"]


def test_open_never_creates_a_remote_session(web):
    """A reload must not start a conversation; only the create path may."""
    token = _member_with_coding_access(web, "alice")
    created = _create(web, token)
    before = dict(web.opencode.remote)

    web.get(f"/api/coding/sessions/{created['session_id']}/open?agent_id=erp-coder",
            token=token)
    web.get(f"/api/coding/sessions/{created['session_id']}/open?agent_id=erp-coder",
            token=token)

    assert web.opencode.remote == before


def test_another_member_cannot_open_it(web):
    """Not a 403: a 404 that does not confirm the session exists."""
    alice = _member_with_coding_access(web, "alice")
    bob = _member_with_coding_access(web, "bob")
    created = _create(web, alice)

    response = web.get(
        f"/api/coding/sessions/{created['session_id']}/open?agent_id=erp-coder",
        token=bob)

    assert _status(response) == 404
    assert _json(response)["code"] == "coding_not_linked"


def test_an_unknown_session_is_a_404(web):
    token = _member_with_coding_access(web, "alice")

    response = web.get(
        "/api/coding/sessions/oc_missing/open?agent_id=erp-coder", token=token)

    assert _status(response) == 404


# -- attach ----------------------------------------------------------------


def _remote_session(web, external_id="ses_rsm_fork", directory=None,
                    parent_id=None, project_id=""):
    """A session that exists *only* inside OpenCode, as a fork or new session does."""
    client = web.opencode
    now = int(time.time() * 1000)
    client.remote[external_id] = RemoteSession(
        id=external_id, title="Forked", directory=directory or web.project_dir,
        created_ms=now, updated_ms=now, parent_id=parent_id,
        project_id=project_id)
    return external_id


def test_a_fork_inside_opencode_joins_the_callers_history(web):
    """The one path by which a session the platform never created is registered."""
    token = _member_with_coding_access(web, "alice")
    created = _create(web, token)
    external = _remote_session(web)

    response = web.post(
        "/api/coding/sessions/attach",
        {"agent_id": "erp-coder", "source_session_id": created["session_id"],
         "external_session_id": external},
        token=token)

    assert _status(response) == 200
    body = _json(response)
    assert body["session_id"].startswith("oc_")
    assert body["session_id"] != created["session_id"]
    assert body["external_session_id"] == external
    assert body["state"] == "ready"
    # The new session is now part of the ordinary list, alongside the first.
    listed = _json(web.get("/api/sessions?agent_id=erp-coder", token=token))
    assert {s["session_id"] for s in listed["sessions"]} >= {
        created["session_id"], body["session_id"]}


def test_attaching_the_same_session_twice_is_idempotent(web):
    token = _member_with_coding_access(web, "alice")
    created = _create(web, token)
    external = _remote_session(web)
    payload = {"agent_id": "erp-coder",
               "source_session_id": created["session_id"],
               "external_session_id": external}

    first = _json(web.post("/api/coding/sessions/attach", payload, token=token))
    second = _json(web.post("/api/coding/sessions/attach", payload, token=token))

    assert first["session_id"] == second["session_id"]


def test_a_second_member_cannot_attach_through_someone_elses_source(web):
    alice = _member_with_coding_access(web, "alice")
    bob = _member_with_coding_access(web, "bob")
    created = _create(web, alice)
    external = _remote_session(web)

    response = web.post(
        "/api/coding/sessions/attach",
        {"agent_id": "erp-coder", "source_session_id": created["session_id"],
         "external_session_id": external},
        token=bob)

    assert _status(response) == 404
    assert _json(response)["code"] == "coding_not_linked"


def test_attach_refuses_an_unknown_remote_session(web):
    """A notification is not evidence that a session exists."""
    token = _member_with_coding_access(web, "alice")
    created = _create(web, token)

    response = web.post(
        "/api/coding/sessions/attach",
        {"agent_id": "erp-coder", "source_session_id": created["session_id"],
         "external_session_id": "ses_rsm_nope"},
        token=token)

    assert _status(response) == 404


def test_attach_refuses_an_internal_subtask(web):
    """A session with a parent is OpenCode's own bookkeeping, not a history entry."""
    token = _member_with_coding_access(web, "alice")
    created = _create(web, token)
    external = _remote_session(web, parent_id="ses_rsm_parent")

    response = web.post(
        "/api/coding/sessions/attach",
        {"agent_id": "erp-coder", "source_session_id": created["session_id"],
         "external_session_id": external},
        token=token)

    assert _status(response) == 400
    assert _json(response)["code"] == "coding_invalid_request"


def test_attach_refuses_a_session_from_another_project(web):
    """Otherwise one Agent's page could register another checkout's conversation."""
    token = _member_with_coding_access(web, "alice")
    created = _create(web, token)
    external = _remote_session(web, directory="/srv/checkouts/other")

    response = web.post(
        "/api/coding/sessions/attach",
        {"agent_id": "erp-coder", "source_session_id": created["session_id"],
         "external_session_id": external},
        token=token)

    assert _status(response) == 400
    assert _json(response)["code"] == "coding_project_mismatch"


def test_attach_accepts_the_same_project_under_two_spellings_of_its_path(web):
    """The service reports the path a session was *created* with, so one project
    comes back unresolved for a session the platform made and resolved for one
    made inside OpenCode (a fork under a symlink: macOS ``/tmp`` ->
    ``/private/tmp``). The project id is the service's own answer to "which
    project is this", and it is what the check has to use -- otherwise the user
    is refused their own conversation."""
    token = _member_with_coding_access(web, "alice")
    created = _create(web, token)
    resolved = "/private" + web.project_dir
    # Same project, different spelling: exactly what a fork of the source
    # reports after being created through the service's own route.
    web.opencode.remote[created["external_session_id"]] = RemoteSession(
        id=created["external_session_id"], title="New session",
        directory=web.project_dir, project_id="prj_erp",
        created_ms=int(time.time() * 1000), updated_ms=int(time.time() * 1000))

    external = _remote_session(web, directory=resolved, project_id="prj_erp")
    response = web.post(
        "/api/coding/sessions/attach",
        {"agent_id": "erp-coder", "source_session_id": created["session_id"],
         "external_session_id": external},
        token=token)

    assert _status(response) == 200, response.data
    assert _json(response)["status"] == "success"

    # A second checkout under the same resolved prefix is still a different
    # project: the comparison is an identity check, not a prefix match.
    other = _remote_session(web, external_id="ses_rsm_elsewhere",
                            directory=resolved + "-other", project_id="prj_other")
    refused = web.post(
        "/api/coding/sessions/attach",
        {"agent_id": "erp-coder", "source_session_id": created["session_id"],
         "external_session_id": other},
        token=token)
    assert _status(refused) == 400
    assert _json(refused)["code"] == "coding_project_mismatch"


def test_attach_refuses_a_different_project_that_merely_shares_the_path(web):
    """The id outranks the path in the other direction too: a path string that
    matches while the service says the projects differ is not the same project."""
    token = _member_with_coding_access(web, "alice")
    created = _create(web, token)
    web.opencode.remote[created["external_session_id"]] = RemoteSession(
        id=created["external_session_id"], title="New session",
        directory=web.project_dir, project_id="prj_erp",
        created_ms=int(time.time() * 1000), updated_ms=int(time.time() * 1000))

    external = _remote_session(web, external_id="ses_rsm_lookalike",
                               directory=web.project_dir, project_id="prj_somewhere_else")
    response = web.post(
        "/api/coding/sessions/attach",
        {"agent_id": "erp-coder", "source_session_id": created["session_id"],
         "external_session_id": external},
        token=token)

    assert _status(response) == 400
    assert _json(response)["code"] == "coding_project_mismatch"


def test_attach_does_not_re_claim_a_session_that_is_already_linked(web):
    """Navigating from one own session to a registered one answers with the
    registered session, so the parent selects the conversation that exists
    instead of moving it to a second list entry."""
    token = _member_with_coding_access(web, "alice")
    first = _create(web, token)
    external = _remote_session(web)
    registered = _json(web.post(
        "/api/coding/sessions/attach",
        {"agent_id": "erp-coder", "source_session_id": first["session_id"],
         "external_session_id": external},
        token=token))
    # A second platform session for the same Agent, then the same notification
    # arriving through it.
    second = _json(web.post("/api/coding/sessions",
                            {"agent_id": "erp-coder", "request_id": "req-2"},
                            token=token))

    response = web.post(
        "/api/coding/sessions/attach",
        {"agent_id": "erp-coder", "source_session_id": second["session_id"],
         "external_session_id": external},
        token=token)

    assert _status(response) == 200
    body = _json(response)
    assert body["session_id"] == registered["session_id"]
    assert body["session_id"] != second["session_id"]
    # And no third row was created for the remote session: the list holds the
    # two sessions the caller made plus the one fork, once.
    listed = _json(web.get("/api/sessions?agent_id=erp-coder", token=token))
    ids = [s["session_id"] for s in listed["sessions"]]
    assert sorted(ids) == sorted({first["session_id"], second["session_id"],
                                  registered["session_id"]})


# -- sync ------------------------------------------------------------------


def test_sync_refreshes_the_callers_own_list(web):
    token = _member_with_coding_access(web, "alice")
    created = _create(web, token)
    external = created["external_session_id"]
    web.opencode.remote[external] = RemoteSession(
        id=external, title="OpenCode 内改的标题", directory=web.project_dir,
        created_ms=created["created_ms"] if "created_ms" in created else 1,
        updated_ms=int(time.time() * 1000) + 60_000)
    web.opencode.active = {external}

    response = web.post("/api/coding/sessions/sync",
                        {"agent_id": "erp-coder"}, token=token)

    assert _status(response) == 200
    body = _json(response)
    assert [c["session_id"] for c in body["changed"]] == [created["session_id"]]
    assert body["changed"][0]["title"] == "OpenCode 内改的标题"
    assert body["changed"][0]["state"] == "running"
    assert body["removed"] == []
    assert body["unavailable"] == []


def test_sync_only_sees_the_callers_own_sessions(web):
    alice = _member_with_coding_access(web, "alice")
    bob = _member_with_coding_access(web, "bob")
    hers = _create(web, alice)
    _create(web, bob)

    body = _json(web.post("/api/coding/sessions/sync", {"agent_id": "erp-coder"},
                          token=alice))

    assert [c["session_id"] for c in body["changed"]] == [hers["session_id"]]


def test_sync_drops_a_session_the_service_confirms_is_gone(web):
    token = _member_with_coding_access(web, "alice")
    created = _create(web, token)
    web.opencode.gone.add(created["external_session_id"])

    body = _json(web.post("/api/coding/sessions/sync", {"agent_id": "erp-coder"},
                          token=token))

    assert body["removed"] == [created["session_id"]]
    listed = _json(web.get("/api/sessions?agent_id=erp-coder", token=token))
    assert listed["sessions"] == []


def test_sync_requires_a_named_agent(web):
    """The tenant default is never a coding Agent, so an Agent-less sync has no
    meaningful target and must say so rather than report a type error."""
    token = _member_with_coding_access(web, "alice")

    response = web.post("/api/coding/sessions/sync", {}, token=token)

    assert _status(response) == 400
    assert _json(response)["code"] == "coding_invalid_request"


def test_sync_needs_the_history_permission(web):
    web.role("no-history", ["chat.use", "agent.use"],
             grants=[("agent", "agent:erp-coder", "use")])
    web.member("alice", ["no-history"])

    response = web.post("/api/coding/sessions/sync", {"agent_id": "erp-coder"},
                        token=web.login("alice"))

    assert _status(response) == 403


# -- management: rename, pin, delete --------------------------------------


def _create_named(web, token, request_id=REQUEST_ID):
    return _json(web.post("/api/coding/sessions",
                          {"agent_id": "erp-coder", "request_id": request_id},
                          token=token))


def test_an_old_session_keeps_its_project_after_the_agent_is_moved(web):
    """Moving an Agent's project must not re-point the conversations it already
    had. A link remembers the directory its session was created in, and ``open``
    reads that link rather than the Agent's current setting -- so the one place
    that could get this wrong is a read that reached for the live profile. If it
    did, every existing conversation would silently address a directory the
    session was never made in, which the service would either refuse or answer
    with someone else's tree."""
    token = _member_with_coding_access(web, "alice")
    created = _create(web, token)
    old_dir = web.project_dir

    # The administrator points the Agent at another checkout, as the
    # configuration page allows (the project is editable; the type is not).
    moved = "/tmp/rsm-coding-acceptance/other-checkout"
    web.add_coding_agent("erp-coder", moved)

    # The old conversation still opens against the project it was created in.
    response = web.get(
        f"/api/coding/sessions/{created['session_id']}/open?agent_id=erp-coder",
        token=token)
    assert _status(response) == 200, response.data
    reopened = _json(response)
    assert encode_directory(old_dir) in reopened["iframe_url"], \
        "the opened session must address the project it was created in"
    assert encode_directory(moved) not in reopened["iframe_url"]

    # A new conversation in the same Agent uses the new project.
    fresh = _json(web.post("/api/coding/sessions",
                           {"agent_id": "erp-coder", "request_id": "moved-1"},
                           token=token))
    assert _status(web.get(
        f"/api/coding/sessions/{fresh['session_id']}/open?agent_id=erp-coder",
        token=token)) == 200
    assert web.opencode.remote[fresh["external_session_id"]].directory == moved


def test_a_rename_goes_to_the_service_and_then_to_the_cache(web):
    token = _member_with_coding_access(web, "alice")
    created = _create_named(web, token)

    response = web.put(f"/api/sessions/{created['session_id']}",
                       {"agent_id": "erp-coder", "title": "重构结算模块"},
                       token=token)

    assert _status(response) == 200
    external = created["external_session_id"]
    assert web.opencode.remote[external].title == "重构结算模块"
    listed = _json(web.get("/api/sessions?agent_id=erp-coder", token=token))
    assert listed["sessions"][0]["title"] == "重构结算模块"


def test_a_refused_rename_keeps_the_old_title(web):
    """OpenCode owns the name, so a refusal must not leave a local-only title."""
    token = _member_with_coding_access(web, "alice")
    created = _create_named(web, token)
    before = _json(web.get("/api/sessions?agent_id=erp-coder",
                           token=token))["sessions"][0]["title"]
    web.opencode.fail_rename = True

    response = web.put(f"/api/sessions/{created['session_id']}",
                       {"agent_id": "erp-coder", "title": "只存在本地的标题"},
                       token=token)

    assert _status(response) == 502
    assert _json(response)["code"] == "coding_upstream_unavailable"
    after = _json(web.get("/api/sessions?agent_id=erp-coder",
                          token=token))["sessions"][0]["title"]
    assert after == before


def test_pinning_and_archiving_stay_local(web):
    """They are the console's own arrangement and mean nothing to the service."""
    token = _member_with_coding_access(web, "alice")
    created = _create_named(web, token)
    web.opencode.calls.clear()

    response = web.put(f"/api/sessions/{created['session_id']}",
                       {"agent_id": "erp-coder", "pinned": True, "archived": True},
                       token=token)

    assert _status(response) == 200
    assert web.opencode.calls == []
    listed = _json(web.get("/api/sessions?agent_id=erp-coder&archived=1",
                           token=token))
    row = listed["sessions"][0]
    assert row["pinned"] in (1, True)
    assert "archived" not in row or row["archived"] in (1, True)
    # An archived row is only listed when it is asked for.
    visible = _json(web.get("/api/sessions?agent_id=erp-coder", token=token))
    assert created["session_id"] not in [s["session_id"]
                                         for s in visible["sessions"]]


def test_deleting_interrupts_a_running_session_before_removing_it(web):
    """A turn left running would keep writing into a session nobody can see."""
    token = _member_with_coding_access(web, "alice")
    created = _create_named(web, token)
    external = created["external_session_id"]
    web.opencode.active = {external}
    web.opencode.calls.clear()

    response = web.delete(f"/api/sessions/{created['session_id']}?agent_id=erp-coder",
                          token=token)

    assert _status(response) == 200
    assert web.opencode.interrupts == [external]
    assert web.opencode.calls.index(("interrupt", external)) < \
        web.opencode.calls.index(("delete", external))
    assert external not in web.opencode.remote


def test_a_refused_delete_keeps_the_session(web):
    """The console must not drop a conversation the service still holds."""
    token = _member_with_coding_access(web, "alice")
    created = _create_named(web, token)
    web.opencode.fail_delete = True

    response = web.delete(f"/api/sessions/{created['session_id']}?agent_id=erp-coder",
                          token=token)

    assert _status(response) == 502
    assert _json(response)["code"] == "coding_upstream_unavailable"
    listed = _json(web.get("/api/sessions?agent_id=erp-coder", token=token))
    assert [s["session_id"] for s in listed["sessions"]] == [created["session_id"]]
    assert web.opencode.remote[created["external_session_id"]] is not None


def test_deleting_twice_is_idempotent(web):
    token = _member_with_coding_access(web, "alice")
    created = _create_named(web, token)
    web.opencode.calls.clear()

    first = web.delete(f"/api/sessions/{created['session_id']}?agent_id=erp-coder",
                       token=token)
    second = web.delete(f"/api/sessions/{created['session_id']}?agent_id=erp-coder",
                        token=token)

    assert _status(first) == 200
    assert _status(second) == 200
    # The remote delete was issued once; the repeat had nothing left to remove
    # and did not fail on the missing link.
    assert web.opencode.calls.count(("delete", created["external_session_id"])) == 1


def test_deleting_a_coding_session_never_touches_the_project(web):
    """Deleting a conversation is not deleting a checkout."""
    token = _member_with_coding_access(web, "alice")
    created = _create_named(web, token)

    web.delete(f"/api/sessions/{created['session_id']}?agent_id=erp-coder",
               token=token)

    import os

    assert os.path.isdir(web.project_dir)
    assert open(os.path.join(web.project_dir, "main.py")).read().startswith("print")


def test_clearing_the_context_of_a_coding_session_is_refused(web):
    """The platform holds no context to clear; a success would be a lie."""
    token = _member_with_coding_access(web, "alice")
    created = _create_named(web, token)

    response = web.post(f"/api/sessions/{created['session_id']}/clear_context",
                        {"agent_id": "erp-coder"}, token=token)

    assert _status(response) == 400
    assert _json(response)["code"] == "coding_web_only"


def test_deleting_a_message_of_a_coding_session_is_refused(web):
    token = _member_with_coding_access(web, "alice")
    created = _create_named(web, token)

    response = web.post("/api/messages/delete",
                        {"session_id": created["session_id"], "agent_id": "erp-coder",
                         "user_seq": 1},
                        token=token)

    assert _status(response) == 400
    assert _json(response)["code"] == "coding_web_only"


def test_the_session_list_badges_a_coding_conversation(web):
    """What lets the console route the click to the frame, not the composer."""
    token = _member_with_coding_access(web, "alice")
    created = _create_named(web, token)

    listed = _json(web.get("/api/sessions?agent_id=erp-coder", token=token))

    row = listed["sessions"][0]
    assert row["session_id"] == created["session_id"]
    assert row["agent"]["agent_type"] == "coding"
    # The persistent half of the link state, so the list can tell a finished
    # session from a reservation without asking again.
    assert row["sync_state"] == "ready"


def test_the_list_names_an_unfinished_reservation_as_retryable(web):
    """A reservation whose create never landed is not an openable conversation."""
    token = _member_with_coding_access(web, "alice")
    web.opencode.fail_create = True

    response = web.post("/api/coding/sessions",
                        {"agent_id": "erp-coder", "request_id": REQUEST_ID},
                        token=token)
    assert _status(response) == 502

    listed = _json(web.get("/api/sessions?agent_id=erp-coder", token=token))
    row = listed["sessions"][0]
    assert row["sync_state"] == "creating"
    # And it is exactly the same reservation a click resumes once the service
    # answers again: the retry reuses the external id rather than starting over.
    web.opencode.fail_create = False
    resumed = web.post("/api/coding/sessions",
                       {"agent_id": "erp-coder", "request_id": REQUEST_ID},
                       token=token)
    assert _json(resumed)["session_id"] == row["session_id"]


def test_the_running_state_is_not_published_by_the_list(web):
    """It is transient, so the list must not claim to know it."""
    token = _member_with_coding_access(web, "alice")
    created = _create_named(web, token)
    web.opencode.active = {created["external_session_id"]}

    row = _json(web.get("/api/sessions?agent_id=erp-coder",
                        token=token))["sessions"][0]

    assert row["sync_state"] == "ready"
    assert "running" != row.get("sync_state")
    assert "state" not in row or row["state"] != "running"


def test_a_normal_agent_gets_no_coding_markers(web):
    web.role("plain-agent", ["chat.use", "history.read", "agent.use"],
             grants=[("agent", "agent:shared-agent", "use")])
    web.member("bob", ["plain-agent"])
    token = web.login("bob")
    from agent.memory import get_conversation_store
    from agent.registry import get_agent_registry
    from common.runtime_identity import RuntimeIdentity, use_identity

    store = get_conversation_store(
        get_agent_registry().get("shared-agent", require_enabled=False).workspace)
    with use_identity(RuntimeIdentity(agent_id="shared-agent",
                                      user_id=web.user_id("bob"),
                                      tenant_id=web.tenant_id)):
        store.append_messages("plain-2", [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ], channel_type="web")
    # And the agent has an ordinary session that must not be claimed.
    assert store.get_coding_link("plain-2") is None

    row = _json(web.get("/api/sessions?agent_id=shared-agent",
                        token=token))["sessions"][0]

    assert row["agent"]["agent_type"] == "normal"
    assert "sync_state" not in row


def test_an_ordinary_session_is_not_claimed_by_the_coding_path(web):
    """The branch is decided by the link alone, so a normal conversation is
    never diverted into the service.

    Asserted at the seam rather than by driving an ordinary rename/delete
    through the app: that path builds the process-global ``Bridge``, which
    ``WebAppHarness`` cannot unpin, and a Bridge pinned to this harness's
    configuration makes *other* files' channel-routing tests fail. The ordinary
    rename/delete behaviour itself is covered by
    ``test_session_store_resolution.py`` and ``test_session_archive.py``.
    """
    token = _member_with_coding_access(web, "alice")
    created = _create_named(web, token)
    from agent.memory import get_conversation_store
    from agent.registry import get_agent_registry
    from channel.web.fork.handlers.sessions import _coding_link

    store = get_conversation_store(
        get_agent_registry().get("erp-coder", require_enabled=False).workspace)
    assert _coding_link("erp-coder", created["session_id"]) is not None
    # A session with no link in the same store is not a coding session, and the
    # probe answers for it without raising -- so the handlers take their own
    # branch and never reach the service.
    assert store.get_coding_link("plain-1") is None
    assert _coding_link("erp-coder", "plain-1") is None
    assert web.opencode.calls == []


# -- the settings projection ----------------------------------------------


def test_the_settings_projection_never_carries_a_credential(web):
    """The console displays the service; it never submits it."""
    monkeypatch = None
    response = web.get("/api/coding/settings", token=web.login("root"))

    assert _status(response) == 200
    body = _json(response)
    assert body["enabled"] is True
    assert body["web_url"] == "https://code.example.com"
    assert "erp-coder" in body["agents"]
    for forbidden in ("password", "password_env", "api_url", "username"):
        assert forbidden not in body
