"""The only place that speaks HTTP to the shared OpenCode service.

The request shapes here were measured against a real instance in phase 1 and are
recorded in the change's ``evidence.md``. Two of those measurements shape this
module more than the rest:

* the upstream accepts a caller-chosen session id and *adopts* it if it already
  exists, which is what makes a create whose response was lost safe to retry;
* it validates neither the id nor the directory, so a wrong request shape is
  answered with a cheerful ``200`` and the mistake surfaces much later, as an
  empty conversation. Every path and field therefore comes from a measurement,
  not from a plausible-looking guess.

Reads and management are split by upstream route: creation, single-session reads
and the active set use the JSON API (``/api/session``, responses wrapped in
``data``), while rename and delete use the compatibility routes that take the
project directory as a query parameter (``/session/{id}``).

There is no retry loop here. A create is retried by the caller with the same
derived id, a rename is retried by the operator pressing the button again, and a
stalled service must not hold a web request open: this layer reports a stable
code and stops.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Set

import requests

from agent.coding import CODING_UPSTREAM_UNAVAILABLE, CodingError, CodingSettings

# Connect and read timeouts for every call, from the design's polling section:
# a stalled service costs one refresh round, not a hung request.
DEFAULT_TIMEOUT = (3, 10)


@dataclass(frozen=True)
class RemoteSession:
    """A session as OpenCode describes it.

    Only the fields the platform actually uses are carried. The title is the
    upstream one (which the platform mirrors rather than authors), the times are
    the upstream's own milliseconds, and ``parent_id`` marks an internal subtask
    — a session created by OpenCode inside another one, which never becomes a
    history entry of its own.

    ``project_id`` is the service's own identity for the project a session
    belongs to, and it is the reliable one: ``directory`` is whatever path string
    the session was created with, so the *same* checkout is reported unresolved
    for a session the platform created and resolved for one made inside OpenCode
    (a fork under ``/tmp`` comes back as ``/private/tmp``). Two sessions of one
    project can therefore disagree on ``directory`` while agreeing on
    ``project_id``.
    """

    id: str
    title: str
    directory: str
    created_ms: int
    updated_ms: int
    parent_id: Optional[str] = None
    project_id: str = ""


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _upstream_unavailable(reason: str) -> CodingError:
    return CodingError(CODING_UPSTREAM_UNAVAILABLE, reason, 502)


class OpenCodeClient:
    """A thin, synchronous client for one configured service.

    ``transport`` exists so tests can script responses without a socket; it is
    any object with a ``request(method, url, **kwargs)`` method, which is the
    part of ``requests.Session`` this module uses.
    """

    def __init__(
        self,
        settings: CodingSettings,
        *,
        transport: Any = None,
        timeout: tuple = DEFAULT_TIMEOUT,
    ):
        self.settings = settings
        self._transport = transport
        self._timeout = timeout
        self._url = (settings.api_url or "").rstrip("/")

    # -- transport ---------------------------------------------------------

    def _session(self) -> Any:
        if self._transport is not None:
            return self._transport
        if getattr(self, "_owned_transport", None) is None:
            self._owned_transport = requests.Session()
        return self._owned_transport

    def _request(self, method: str, path: str, **kwargs):
        if not self._url:
            raise _upstream_unavailable("the coding service address is not configured")
        kwargs.setdefault("headers", {}).update(self.settings.auth_headers())
        kwargs["timeout"] = self._timeout
        try:
            return self._session().request(method, self._url + path, **kwargs)
        except requests.Timeout as exc:
            # 504: the platform waited and gave up. Keeping this apart from 502
            # lets the console say "slow" rather than "broken".
            raise CodingError(
                CODING_UPSTREAM_UNAVAILABLE,
                f"the coding service did not answer in time: {exc}",
                504,
            ) from exc
        except requests.RequestException as exc:
            # Refused, DNS, TLS, reset mid-body: all the same to a caller, all
            # temporary, and none of them evidence about any particular session.
            raise _upstream_unavailable(
                f"the coding service is unreachable: {exc}"
            ) from exc

    def _no_content(self, response, action: str) -> None:
        """Accept the empty bodies the management routes answer with.

        Interrupt replies ``204`` with no payload at all, and the compatibility
        delete replies with a bare ``true``; demanding JSON from either would
        turn a successful call into a failure.
        """
        if response.status_code >= 400:
            raise _upstream_unavailable(
                f"the coding service refused to {action} (HTTP {response.status_code})"
            )

    def _json(self, response, action: str) -> Any:
        if response.status_code >= 400:
            if response.status_code >= 500:
                raise _upstream_unavailable(
                    f"the coding service failed to {action} (HTTP {response.status_code})"
                )
            # 401/403 included, and deliberately not passed through: upstream
            # rejecting the platform's own credential says nothing about the
            # signed-in user, so it must never become their 401/403.
            raise _upstream_unavailable(
                f"the coding service refused to {action} (HTTP {response.status_code})"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise _upstream_unavailable(
                f"the coding service did not return JSON when asked to {action}"
            ) from exc

    # -- parsing -----------------------------------------------------------

    @staticmethod
    def _remote(payload: Any) -> RemoteSession:
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            raise _upstream_unavailable("the coding service returned an unexpected body")
        time = data.get("time") if isinstance(data.get("time"), Mapping) else {}
        location = data.get("location") if isinstance(data.get("location"), Mapping) else {}
        return RemoteSession(
            id=_text(data.get("id")),
            title=_text(data.get("title")),
            directory=_text(location.get("directory")),
            created_ms=_int(time.get("created")),
            updated_ms=_int(time.get("updated")) or _int(time.get("created")),
            parent_id=(_text(data.get("parentID")) or None) if data.get("parentID") is not None else None,
            project_id=_text(data.get("projectID")),
        )

    # -- operations --------------------------------------------------------

    def create_session(self, session_id: str, project_dir: str) -> RemoteSession:
        """Create the session under a caller-chosen id, or adopt the existing one.

        ``session_id`` is derived from the verified identity plus the request id,
        so a retry after a lost response lands on the same session instead of
        creating a second one. The directory is the project recorded at creation
        time and is never the Agent's platform workspace.
        """
        response = self._request(
            "POST",
            "/api/session",
            json={"id": session_id, "location": {"directory": project_dir}},
        )
        return self._remote(self._json(response, "create the session"))

    def get_session(self, session_id: str, project_dir: str = "") -> Optional[RemoteSession]:
        """Read one session, or ``None`` when OpenCode confirms it is gone.

        ``project_dir`` is accepted for symmetry with the callers that hold it;
        the read route takes no directory because the id is globally unique in
        one service instance.
        """
        response = self._request("GET", f"/api/session/{session_id}")
        if response.status_code == 404:
            return None
        return self._remote(self._json(response, "read the session"))

    def active_sessions(self) -> Set[str]:
        """The ids OpenCode currently reports as running.

        Asked once per refresh round and reused for every link in the batch:
        the answer is service-wide, so per-session "is it running?" calls would
        cost one request per row for the same information.
        """
        response = self._request("GET", "/api/session/active")
        payload = self._json(response, "read the running sessions")
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            return set()
        active = set()
        for session_id, state in data.items():
            if isinstance(state, Mapping) and state.get("type") not in (None, "running"):
                continue
            active.add(_text(session_id))
        return active

    def rename_session(self, session_id: str, title: str, project_dir: str) -> None:
        """Rename upstream, carrying the project the session was created under.

        The directory is the one recorded in the link, not the Agent's current
        default: changing the default moves future sessions only.
        """
        response = self._request(
            "PATCH",
            f"/session/{session_id}",
            params={"directory": project_dir},
            json={"title": title},
        )
        self._no_content(response, "rename the session")

    def interrupt_session(self, session_id: str) -> None:
        """Stop the run, if any. An idle session answers an empty success."""
        response = self._request("POST", f"/api/session/{session_id}/interrupt")
        self._no_content(response, "stop the session")

    def delete_session(self, session_id: str, project_dir: str) -> None:
        """Delete upstream. A confirmed 404 means the deletion already happened.

        Idempotent by design so a retry cannot fail on work that is already done;
        the project's files are untouched, because they live in the operator's
        checkout rather than in the service.
        """
        response = self._request(
            "DELETE", f"/session/{session_id}", params={"directory": project_dir}
        )
        if response.status_code == 404:
            return
        self._no_content(response, "delete the session")
