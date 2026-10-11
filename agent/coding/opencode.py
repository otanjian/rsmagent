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

import base64
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple
from urllib.parse import quote

import requests

from agent.coding import (
    CODING_UPSTREAM_UNAVAILABLE,
    CODING_WORKSPACE_CONFLICT,
    CodingError,
    CodingSettings,
)

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

    def create_session(
        self,
        session_id: str,
        project_dir: str,
        permission: Optional[List[Mapping[str, str]]] = None,
    ) -> RemoteSession:
        """Create the session under a caller-chosen id, or adopt the existing one.

        ``session_id`` is derived from the verified identity plus the request id,
        so a retry after a lost response lands on the same session instead of
        creating a second one. The directory is the project recorded at creation
        time and is never the Agent's platform workspace.

        ``permission`` is a server-derived session ruleset (see
        ``agent.coding.permissions``): a ``deny`` in the project config hides a
        project tool from every session, and this ``allow`` makes it visible in
        the sessions the caller created. The service *accepts* it here and does
        not store it -- a session created with the ruleset in this body reads
        back with none -- so the caller must follow up with
        :meth:`set_permission`; this argument is kept only because the field is
        part of the route and costs nothing to send.
        """
        body: Dict[str, Any] = {
            "id": session_id,
            "location": {"directory": project_dir},
        }
        if permission:
            body["permission"] = [dict(rule) for rule in permission]
        response = self._request("POST", "/api/session", json=body)
        return self._remote(self._json(response, "create the session"))

    def prepare_workspace(
        self,
        base_project_dir: str,
        effective_directory: str,
        workspace_id: str,
    ) -> Mapping[str, Any]:
        """Ask the service to prepare the caller's personal directory under ``P``.

        The platform resolves ``P`` and ``U`` (see :mod:`agent.coding.workspace`)
        and the service confirms the project exists and creates ``U`` on its own
        host; the platform never creates a same-named local directory and never
        resolves the remote path itself. Idempotent on the service side, so a
        retry for the same binding resumes rather than producing a second tree.

        The body carries the platform's opaque ``workspace_id`` so the service
        can keep its minimal ``binding -> U`` ownership record and refuse a
        directory already claimed by a different binding. This shape was
        confirmed against the running RSMCode service (evidence section 7):
        ``POST /api/coding/workspace/prepare`` answers
        ``{directory, workspace_id, created, state}``.
        """
        body: Dict[str, Any] = {
            "base_project_dir": base_project_dir,
            "effective_directory": effective_directory,
            "workspace_id": workspace_id,
        }
        response = self._request(
            "POST", "/api/coding/workspace/prepare", json=body)
        if response.status_code == 409:
            # A remote-side conflict the platform cannot see on its own: the
            # directory is claimed by another binding, or the project already
            # tracks source under the personal container. Surfacing it as an
            # outage would hide a condition the operator can fix.
            raise CodingError(
                CODING_WORKSPACE_CONFLICT,
                "the personal directory cannot be prepared on this host",
                409,
            )
        payload = self._json(response, "prepare the personal workspace")
        return payload if isinstance(payload, Mapping) else {}

    def upload_file(
        self,
        workspace_id: str,
        relative_path: str,
        content: bytes,
    ) -> Mapping[str, Any]:
        """Write one file into the prepared personal directory for a binding.

        The location is the server's own binding record, so the platform sends
        the opaque ``workspace_id`` and a path *relative to U* and never a
        directory. ``content`` is base64-encoded because the server's typed JSON
        contract carries a JSON body; the server decodes it and enforces its own
        ceiling, and refuses a path that would escape ``U``.

        A ``400`` (a malformed relative path) is the caller's own refusal and is
        surfaced as a ``ValueError``; a ``409`` (the directory does not exist, or
        belongs to another binding) is a stable conflict; anything else is an
        outage, because the panel must not read "the service is down" as "your
        file was bad".
        """
        body: Dict[str, Any] = {
            "workspace_id": workspace_id,
            "relative_path": relative_path,
            "content_base64": base64.b64encode(content).decode("ascii"),
        }
        response = self._request(
            "POST", "/api/coding/workspace/upload", json=body)
        if response.status_code >= 400:
            if response.status_code == 400:
                raise ValueError(
                    "the coding service rejected the upload path")
            if response.status_code == 409:
                raise CodingError(
                    CODING_WORKSPACE_CONFLICT,
                    "the personal directory is not available for this upload",
                    409,
                )
            raise _upstream_unavailable(
                "the coding service refused to accept the upload"
                f" (HTTP {response.status_code})"
            )
        payload = self._json(response, "upload the file")
        return payload if isinstance(payload, Mapping) else {}

    def set_permission(
        self,
        session_id: str,
        project_dir: str,
        permission: List[Mapping[str, str]],
    ) -> None:
        """Replace a session's own permission ruleset upstream.

        This is the only route that *persists* a ruleset, so it carries every
        ruleset the platform applies: for a session created through
        :meth:`create_session`, where the create body's ``permission`` is
        accepted and dropped, and for a conversation the user opened inside
        OpenCode and then attached, which was created with no ruleset at all.
        The ruleset is server-derived, never client-supplied. The directory is
        the one recorded on the link, mirroring :meth:`rename_session`.
        """
        response = self._request(
            "PATCH",
            f"/session/{session_id}",
            params={"directory": project_dir},
            json={"permission": [dict(rule) for rule in permission]},
        )
        self._no_content(response, "set the session permission")

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

    # -- filesystem --------------------------------------------------------

    def location(self, directory: str) -> Mapping[str, Any]:
        """Resolve one directory on the service's host.

        The answer confirms which directory the location points at (and which
        project owns it). The platform reads it to *verify* a personal
        directory, never to address a file: every file call still names the
        location explicitly.
        """
        response = self._request(
            "GET", "/api/location", params={"location[directory]": directory})
        payload = self._json(response, "resolve the location")
        return payload if isinstance(payload, Mapping) else {}

    def list_files(self, directory: str, path: str = "") -> List[Mapping[str, Any]]:
        """Direct children of ``path`` (relative to ``directory``).

        Measured shape (evidence section 7): ``data`` holds ``{path, type}``
        rows, each ``path`` relative to the location using the *host*
        separator, and a directory carries a trailing separator. A missing
        directory answers ``500`` on this service, where to the caller it is
        simply not there -- so an unreachable path becomes a not-found rather
        than an outage (see :meth:`_fs_payload`).
        """
        params: Dict[str, Any] = {"location[directory]": directory}
        if path:
            params["path"] = path
        response = self._request("GET", "/api/fs/list", params=params)
        return self._entries(
            self._fs_payload(response, path, "list the directory"))

    def find_files(
        self,
        directory: str,
        query: str,
        *,
        kind: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Mapping[str, Any]]:
        """Search below ``directory`` by name, one flat result list.

        The same ``{path, type}`` rows as :meth:`list_files`, though here the
        measured ``path`` uses ``/`` rather than the host separator; the DTO
        layer normalizes both (evidence section 7, note 3).
        """
        params: Dict[str, Any] = {"location[directory]": directory, "query": query}
        if kind:
            params["type"] = kind
        if limit:
            params["limit"] = str(limit)
        response = self._request("GET", "/api/fs/find", params=params)
        return self._entries(
            self._fs_payload(response, query, "search the directory"))

    def read_file(self, directory: str, path: str) -> Tuple[bytes, str]:
        """Raw bytes of one file, plus its content type.

        ``path`` is encoded whole (separators included) because the route takes
        it as a single path segment. A missing file, a directory and an
        escaping path are all a ``500`` here -- the caller maps them to
        not-found (see :meth:`_fs_payload`).
        """
        response = self._request(
            "GET",
            f"/api/fs/read/{quote(path, safe='')}",
            params={"location[directory]": directory},
        )
        if response.status_code >= 400:
            self._fs_fail(path, response.status_code, "read the file")
        content = getattr(response, "content", b"") or b""
        if not isinstance(content, (bytes, bytearray)):
            content = bytes(content)
        headers = getattr(response, "headers", None) or {}
        content_type = str(headers.get("Content-Type") or "application/octet-stream")
        return bytes(content), content_type

    @staticmethod
    def _entries(payload: Any) -> List[Mapping[str, Any]]:
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, list):
            return []
        return [entry for entry in data if isinstance(entry, Mapping)]

    def _fs_payload(self, response, path: str, action: str) -> Any:
        """Decode a filesystem route's JSON body, translating its refusals.

        This service answers a path it cannot serve -- missing, a directory
        where a file was asked for, or one that escapes the location -- with
        an internal ``500``. To a caller that is *this path is not there*, not
        *the service is broken*, and reporting it as an outage would dress a
        panel typo up as an infrastructure failure. A real outage never gets
        this far: it fails at the transport, as a 502/504.
        """
        if response.status_code >= 400:
            self._fs_fail(path, response.status_code, action)
        try:
            return response.json()
        except ValueError as exc:
            raise _upstream_unavailable(
                f"the coding service did not return JSON when asked to {action}"
            ) from exc

    def _fs_fail(self, path: str, status: int, action: str) -> None:
        if status == 400:
            raise ValueError(f"the coding service rejected the path: {path}")
        if status >= 500:
            raise FileNotFoundError(f"File not found: {path}")
        raise _upstream_unavailable(
            f"the coding service refused to {action} (HTTP {status})")

