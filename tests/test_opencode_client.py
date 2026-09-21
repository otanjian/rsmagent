"""The OpenCode HTTP client, pinned to the shapes measured in phase 1.

Every path, verb, query parameter and response field asserted here was observed
against a real local OpenCode instance and is recorded in the change's
``evidence.md``. Nothing is guessed: the upstream accepts an unheard-of session
id, an unknown directory and even a malformed id without complaint, so a wrong
request shape fails silently upstream and would only show up as an empty
conversation later.

The client is deliberately the only place that speaks HTTP to OpenCode. It
carries no provider abstraction, no message translation and no retry queue: the
callers above it own the linking and cache decisions, and this layer owns
"which request, which credential, and how a status maps to a stable code".
"""

import base64

import pytest
import requests

from agent.coding import (
    CODING_UPSTREAM_UNAVAILABLE,
    CodingError,
    CodingSettings,
)
from agent.coding.opencode import OpenCodeClient, RemoteSession


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = b"" if payload is None else b"x"
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class _Transport:
    """Records every call and answers from a scripted queue."""

    def __init__(self, *responses, error=None):
        self.responses = list(responses)
        self.error = error
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError(f"unexpected extra call: {method} {url}")
        return self.responses.pop(0)


def _client(transport, **overrides):
    values = {
        "enabled": True,
        "service_id": "default",
        "api_url": "http://127.0.0.1:4096",
        "web_url": "https://code.example.com",
        "username": "opencode",
        "password": "s3cret",
    }
    values.update(overrides)
    return OpenCodeClient(CodingSettings(**values), transport=transport)


def _session_payload(session_id="ses_rsm_abc", directory="/srv/checkouts/erp"):
    return {
        "data": {
            "id": session_id,
            "projectID": "global",
            "title": "New session - 2026-09-20T06:00:00.000Z",
            "time": {"created": 1789887569063, "updated": 1789887569063},
            "location": {"directory": directory},
            "subpath": directory.lstrip("/"),
        }
    }


# --- create ---------------------------------------------------------------


def test_create_sends_the_fixed_id_and_project_directory():
    """The fixed external id is what makes a lost response retryable."""
    transport = _Transport(_Response(200, _session_payload()))
    client = _client(transport)

    session = client.create_session("ses_rsm_abc", "/srv/checkouts/erp")

    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://127.0.0.1:4096/api/session"
    assert call["json"] == {
        "id": "ses_rsm_abc",
        "location": {"directory": "/srv/checkouts/erp"},
    }
    assert session.id == "ses_rsm_abc"
    assert session.directory == "/srv/checkouts/erp"
    assert session.title.startswith("New session")
    assert session.created_ms == 1789887569063
    assert session.updated_ms == 1789887569063
    assert session.parent_id is None


def test_create_reading_back_an_existing_session_is_not_an_error():
    """Phase 1 measured that the upstream adopts the id instead of failing.

    Retrying a create whose response was lost therefore lands on the same
    session, which is the whole reason the id is derived from the request
    rather than generated upstream.
    """
    transport = _Transport(_Response(200, _session_payload()), _Response(200, _session_payload()))
    client = _client(transport)

    first = client.create_session("ses_rsm_abc", "/srv/checkouts/erp")
    second = client.create_session("ses_rsm_abc", "/srv/checkouts/erp")

    assert first.created_ms == second.created_ms
    assert len(transport.calls) == 2
    assert transport.calls[0]["json"] == transport.calls[1]["json"]


# --- read -----------------------------------------------------------------


def test_get_session_parses_the_v2_envelope():
    transport = _Transport(_Response(200, _session_payload()))
    client = _client(transport)

    session = client.get_session("ses_rsm_abc", "/srv/checkouts/erp")

    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "http://127.0.0.1:4096/api/session/ses_rsm_abc"
    assert session.title.startswith("New session")
    assert session.directory == "/srv/checkouts/erp"


def test_get_session_keeps_a_child_sessions_parent():
    """``parentID`` marks an internal subtask, which never joins the history."""
    payload = _session_payload()
    payload["data"]["parentID"] = "ses_rsm_parent"
    transport = _Transport(_Response(200, payload))
    client = _client(transport)

    session = client.get_session("ses_rsm_abc")

    assert session.parent_id == "ses_rsm_parent"


def test_a_missing_session_reads_as_none_rather_than_an_error():
    """Only this module turns 404 into an answer: for one session, "gone" is
    a normal state that the caller handles, not a service failure."""
    transport = _Transport(_Response(404, {"_tag": "SessionNotFoundError"}))
    client = _client(transport)

    assert client.get_session("ses_rsm_gone") is None


def test_active_sessions_returns_the_running_ids():
    transport = _Transport(_Response(200, {"data": {
        "ses_rsm_running": {"type": "running"},
        "ses_rsm_other": {"type": "idle"},
    }}))
    client = _client(transport)

    active = client.active_sessions()

    assert transport.calls[0]["url"] == "http://127.0.0.1:4096/api/session/active"
    assert active == {"ses_rsm_running"}


def test_active_sessions_is_empty_when_nothing_is_running():
    transport = _Transport(_Response(200, {"data": {}}))
    client = _client(transport)

    assert client.active_sessions() == set()


# --- manage ---------------------------------------------------------------


def test_rename_patches_the_compatibility_route_with_the_project():
    """Renaming carries the old session's directory, never the Agent's current
    default: editing the default must not move an existing conversation."""
    transport = _Transport(_Response(200, {"id": "ses_rsm_abc", "title": "平台标题"}))
    client = _client(transport)

    client.rename_session("ses_rsm_abc", "平台标题", "/srv/checkouts/old")

    call = transport.calls[0]
    assert call["method"] == "PATCH"
    assert call["url"] == "http://127.0.0.1:4096/session/ses_rsm_abc"
    assert call["params"] == {"directory": "/srv/checkouts/old"}
    assert call["json"] == {"title": "平台标题"}


def test_interrupt_posts_and_accepts_an_empty_body():
    transport = _Transport(_Response(204, None))
    client = _client(transport)

    client.interrupt_session("ses_rsm_abc")

    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://127.0.0.1:4096/api/session/ses_rsm_abc/interrupt"


def test_delete_carries_the_project_and_treats_404_as_done():
    """A repeated delete has to stay idempotent: the caller retries freely."""
    transport = _Transport(_Response(200, True), _Response(404, {"name": "NotFoundError"}))
    client = _client(transport)

    client.delete_session("ses_rsm_abc", "/srv/checkouts/erp")
    client.delete_session("ses_rsm_abc", "/srv/checkouts/erp")

    call = transport.calls[0]
    assert call["method"] == "DELETE"
    assert call["url"] == "http://127.0.0.1:4096/session/ses_rsm_abc"
    assert call["params"] == {"directory": "/srv/checkouts/erp"}


def test_deleting_a_live_session_is_the_callers_decision_to_interrupt_first():
    """The client does not silently interrupt: the caller owns that ordering."""
    transport = _Transport(_Response(200, True))
    client = _client(transport)

    client.delete_session("ses_rsm_abc", "/srv/checkouts/erp")

    assert [call["method"] for call in transport.calls] == ["DELETE"]


# --- credential and transport policy --------------------------------------


def test_every_call_carries_basic_auth_but_no_credential_in_the_url():
    transport = _Transport(_Response(200, _session_payload()))
    client = _client(transport)

    client.get_session("ses_rsm_abc")

    call = transport.calls[0]
    expected = base64.b64encode(b"opencode:s3cret").decode("ascii")
    assert call["headers"]["Authorization"] == f"Basic {expected}"
    assert "s3cret" not in call["url"]
    assert "opencode:s3cret" not in call["url"]


def test_an_unauthenticated_service_sends_no_authorization_header():
    """The local development instance runs without a password."""
    transport = _Transport(_Response(200, _session_payload()))
    client = _client(transport, password=None)

    client.get_session("ses_rsm_abc")

    assert "Authorization" not in transport.calls[0]["headers"]


def test_every_call_uses_the_short_connect_and_read_timeouts():
    """A stalled service must not hold a web request open indefinitely."""
    transport = _Transport(_Response(200, _session_payload()))
    client = _client(transport)

    client.get_session("ses_rsm_abc")

    assert transport.calls[0]["timeout"] == (3, 10)


# --- failure classification -----------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 418, 500, 503])
def test_upstream_failures_become_one_stable_code(status):
    """401/403 included, and never reused as the platform's own 401/403.

    OpenCode rejecting the platform's credential says nothing about the signed
    in user, so it must not log them out; it is a broken capability.
    """
    transport = _Transport(_Response(status, {"message": "nope"}, text="nope"))
    client = _client(transport)

    with pytest.raises(CodingError) as caught:
        client.get_session("ses_rsm_abc")

    assert caught.value.code == CODING_UPSTREAM_UNAVAILABLE
    assert caught.value.status == 502


def test_a_timeout_is_reported_apart_from_a_refusal():
    transport = _Transport(error=requests.Timeout("too slow"))
    client = _client(transport)

    with pytest.raises(CodingError) as caught:
        client.active_sessions()

    assert caught.value.code == CODING_UPSTREAM_UNAVAILABLE
    assert caught.value.status == 504


def test_a_connection_failure_is_not_a_missing_session():
    transport = _Transport(error=requests.ConnectionError("refused"))
    client = _client(transport)

    with pytest.raises(CodingError) as caught:
        client.get_session("ses_rsm_abc")

    assert caught.value.status == 502


def test_an_unparsable_body_is_an_upstream_failure():
    """A proxy login page is not a session."""
    transport = _Transport(_Response(200, None, text="<html>sign in</html>"))
    client = _client(transport)

    with pytest.raises(CodingError) as caught:
        client.get_session("ses_rsm_abc")

    assert caught.value.code == CODING_UPSTREAM_UNAVAILABLE


def test_create_does_not_swallow_an_upstream_refusal():
    """Retrying a rejected create forever would be worse than reporting it."""
    transport = _Transport(_Response(502, {"message": "bad gateway"}, text="bad gateway"))
    client = _client(transport)

    with pytest.raises(CodingError) as caught:
        client.create_session("ses_rsm_abc", "/srv/checkouts/erp")

    assert caught.value.code == CODING_UPSTREAM_UNAVAILABLE
    assert caught.value.status == 502


def test_remote_session_is_a_plain_value():
    """Callers compare and store these, so no behaviour hides inside."""
    session = RemoteSession(
        id="ses_rsm_abc",
        title="Title",
        directory="/srv/checkouts/erp",
        created_ms=1,
        updated_ms=2,
        parent_id=None,
    )

    assert (session.id, session.title, session.directory) == (
        "ses_rsm_abc", "Title", "/srv/checkouts/erp")
    assert session.parent_id is None
