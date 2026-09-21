"""Reserving, confirming and reading back one platform↔OpenCode association.

This module owns the one genuinely hard question of the feature: how to make
"start a conversation" repeatable. A browser retries a POST, a response is lost,
the platform restarts mid-request — and the user must still end up with exactly
one conversation, never two, and never somebody else's.

The answer is that upstream is never asked to invent an id. Both ids are derived
from the *verified* identity (service instance, tenant, user, Agent) plus the
client's own ``request_id``, so:

* the same request always names the same session, which is what makes a retry
  adopt whatever the previous attempt created;
* a client UUID reused by another subject, Agent or tenant derives different
  ids, so it cannot reach across a boundary;
* the derivation is a pure function of data that survives a restart, so the
  reservation in the database is the only state that matters.

Around that, this module is deliberately thin: it reserves the link and its
cache row, asks the client to create or read, and mirrors the answer back into
the cache. It does not open a second connection, queue work, or keep a session
body — the cache and the association both live in the conversation database, and
OpenCode stays the authority for everything inside a session.
"""

from __future__ import annotations

import base64
import hashlib
import sqlite3
import time
from typing import Any, Dict, Optional

from agent.coding import (
    CODING_INVALID_REQUEST,
    CODING_NOT_LINKED,
    CODING_PROJECT_MISMATCH,
    CodingError,
    CodingSettings,
    coding_disabled,
    coding_service_changed,
    resolve_settings,
)
from agent.coding.opencode import OpenCodeClient, RemoteSession

#: Prefixes keep the two namespaces apart in a log line and make it obvious
#: which one belongs to the platform and which one to OpenCode.
SESSION_PREFIX = "oc_"
EXTERNAL_PREFIX = "ses_rsm_"

#: SHA-256 is truncated to 128 bits for the id: collision resistance is not the
#: property being bought here (the identity is not secret), predictability
#: across processes is, and 32 hex characters stay a readable id.
_DIGEST_CHARS = 32

#: Separator that cannot appear in any of the contributing values, so no two
#: different identity tuples can encode to the same string.
_FIELD_SEP = "\x1f"

#: One refresh batch. Bounded so a single round cannot walk a whole tenant, and
#: the client consumes ``next_cursor`` until it is exhausted.
SYNC_BATCH = 50

#: Concurrent remote reads inside one batch. The poll runs every five seconds,
#: so an unbounded fan-out would open one request per linked session.
SYNC_CONCURRENCY = 4


def derive_ids(
    *,
    service_id: str,
    tenant_id: str,
    user_id: str,
    agent_id: str,
    request_id: str,
) -> tuple:
    """``(session_id, external_session_id)`` for one verified request.

    Each field is length-prefixed before joining, so ``("ab", "c")`` and
    ``("a", "bc")`` cannot encode alike — otherwise two different subjects could
    share a session by choosing their ids carefully.
    """
    parts = []
    for value in (service_id, tenant_id, user_id, agent_id, request_id):
        text = "" if value is None else str(value)
        parts.append(f"{len(text)}:{text}")
    canonical = _FIELD_SEP.join(parts)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
    return f"{SESSION_PREFIX}{digest}", f"{EXTERNAL_PREFIX}{digest}"


def encode_directory(project_dir: str) -> str:
    """The project directory as OpenCode's own router spells it in a URL.

    base64url over the UTF-8 bytes with the padding removed (
    ``packages/core/src/util/encode.ts`` in the OpenCode tree). Copying the rule
    rather than re-deriving a slug means the embedded app resolves the same
    project the platform recorded.
    """
    raw = base64.urlsafe_b64encode(project_dir.encode("utf-8")).decode("ascii")
    return raw.rstrip("=")


def session_url(web_url: str, external_session_id: str, project_dir: str) -> str:
    """The embed URL for one session.

    Built only from the operator-configured ``web_url`` and the real session:
    the console cannot submit an address, so it cannot point the iframe at
    another service. ``rsm_embed`` is the marker the embedded app uses to hide
    its own navigation and settings.

    ``rsm_parent_origin`` and ``rsm_channel`` are deliberately *not* added here:
    both are properties of one iframe mount (the tab's own origin and a fresh
    channel per mount), so the page supplies them when it mounts the frame, and
    the child validates notifications against them.
    """
    base = (web_url or "").rstrip("/")
    return (
        f"{base}/{encode_directory(project_dir)}/session/{external_session_id}"
        f"?rsm_embed=1"
    )


class CodingSessionService:
    """Create, resume and refresh coding sessions for one configured service.

    ``store`` is the ordinary conversation store; the service never opens a
    database of its own. ``client`` is injectable so the whole lifecycle can be
    exercised without a socket.
    """

    def __init__(
        self,
        store: Any,
        *,
        settings: Optional[CodingSettings] = None,
        client: Any = None,
    ):
        self.store = store
        self.settings = settings or resolve_settings()
        self._client = client

    # -- wiring ------------------------------------------------------------

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = OpenCodeClient(self.settings)
        return self._client

    def _require_enabled(self) -> CodingSettings:
        """Refuse the capability, never the user: 503 and a stable code.

        A deployment with the capability off, or with no usable address, has no
        coding service to talk to; saying so is not an authorization failure and
        must not read like one.
        """
        if not self.settings.enabled or not self.settings.configured:
            raise coding_disabled()
        return self.settings

    def _link_or_service_error(self, session_id: str) -> Optional[Dict[str, Any]]:
        """The link, refusing when it belongs to another service instance.

        The derivation includes ``service_id``, so a link found under the same
        platform session id but a different service is not "the same session
        somewhere else" — it names a different conversation that happens to
        share the id, and reopening it would show the wrong code.
        """
        link = self.store.get_coding_link(session_id)
        if link is not None and link["service_id"] != self.settings.service_id:
            raise coding_service_changed()
        return link

    def _describe(self, link: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "session_id": link["session_id"],
            "agent_id": link["agent_id"],
            "external_session_id": link["external_session_id"],
            "state": link["state"],
            "project_dir": link["project_dir"],
            "iframe_url": session_url(
                self.settings.web_url, link["external_session_id"], link["project_dir"]),
        }

    # -- reserve / create --------------------------------------------------

    def reserve(
        self,
        *,
        agent_id: str,
        project_dir: str,
        request_id: str,
        tenant_id: str = "",
        user_id: str = "",
    ) -> Dict[str, Any]:
        """Create the session for one request, or hand back the one it already has.

        Called on the first click and on every retry of it. ``tenant_id`` and
        ``user_id`` default to the ambient verified identity rather than to
        anything the caller's request body said: ownership is decided by who is
        asking, never by what they asked with.
        """
        settings = self._require_enabled()
        if not str(request_id or "").strip():
            raise CodingError(
                "coding_invalid_request",
                "a coding session needs the request id the client is retrying",
                400,
            )
        project_dir = str(project_dir or "").strip()
        if not project_dir:
            raise CodingError(
                "coding_invalid_request",
                "a coding session needs the agent's project directory",
                400,
            )

        identity = _ambient_identity()
        tenant_id = tenant_id or identity.get("tenant_id", "")
        user_id = user_id or identity.get("user_id", "")
        session_id, external_id = derive_ids(
            service_id=settings.service_id,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            request_id=request_id,
        )

        link = self._link_or_service_error(session_id)
        if link is None:
            link = self._reserve_link(
                session_id, external_id, project_dir, request_id)
        elif link["state"] != "ready" and link["project_dir"] != project_dir:
            # An unfinished reservation is a promise about one project. Letting a
            # second click change the directory would point an id that may
            # already exist upstream at different code.
            raise CodingError(
                "coding_request_conflict",
                "this request reserved another project directory",
                400,
            )

        if link["state"] == "ready":
            return self._describe(link)

        return self._create_upstream(link, external_id)

    def _reserve_link(
        self,
        session_id: str,
        external_id: str,
        project_dir: str,
        request_id: str,
    ) -> Dict[str, Any]:
        """Write the reservation, or adopt the one a concurrent request wrote.

        The primary key is the arbiter: a second request that lost the insert
        reads the winner's row instead of failing or adding a link, which is
        what makes two tabs pressing once behave like one.
        """
        try:
            return self.store.create_coding_link(
                session_id=session_id,
                external_session_id=external_id,
                service_id=self.settings.service_id,
                project_dir=project_dir,
                request_id=request_id,
                state="creating",
            )
        except sqlite3.IntegrityError:
            existing = self.store.get_coding_link(session_id)
            if existing is None:
                # The conflict was on the external id, not the platform one:
                # another platform session already holds this external session.
                raise CodingError(
                    "coding_request_conflict",
                    "this request is already linked to another session",
                    409,
                )
            if existing["project_dir"] != project_dir:
                raise CodingError(
                    "coding_request_conflict",
                    "this request reserved another project directory",
                    400,
                )
            return existing

    def _create_upstream(self, link: Dict[str, Any], external_id: str) -> Dict[str, Any]:
        """Ask for the session, then mark the reservation ready.

        A failure propagates untouched and the reservation stays ``creating``:
        that is exactly the state a retry resumes from, and it is why a timeout
        leaves a reusable id rather than a duplicate session. The external id
        already exists upstream if the previous attempt got through, and
        OpenCode adopts it rather than creating a second session.
        """
        remote = self.client.create_session(external_id, link["project_dir"])
        self.store.set_coding_link_state(link["session_id"], "ready")
        if isinstance(remote, RemoteSession):
            self.store.touch_coding_link_cache(
                link["session_id"], title=remote.title,
                remote_updated_ms=remote.updated_ms)
        refreshed = self.store.get_coding_link(link["session_id"]) or link
        return self._describe(refreshed)

    # -- open --------------------------------------------------------------

    def open(self, *, session_id: str, agent_id: str = "") -> Dict[str, Any]:
        """The caller's own link for one session, ready to mount in an iframe.

        A GET never creates anything remotely: a reservation still ``creating``
        comes back as a retriable state for the *create* endpoint to finish, so
        opening a page cannot quietly start a second session. Everything the
        response needs — the URL, the project, the cached title — is read here,
        and the caller's ownership is established above this layer.
        """
        self._require_enabled()
        if not session_id:
            raise CodingError(CODING_INVALID_REQUEST, "session_id required", 400)
        link = self._link_or_service_error(session_id)
        if link is None or (agent_id and link["agent_id"] != agent_id):
            # Also a 404 for another Agent's session: not being a coding session
            # of *this* caller's Agent is not an answer worth distinguishing.
            raise CodingError(
                CODING_NOT_LINKED,
                "this session is not linked to the coding service",
                404,
            )
        described = self._describe(link)
        described["retryable"] = link["state"] != "ready"
        return described

    # -- attach ------------------------------------------------------------

    def attach(
        self,
        *,
        source_session_id: str,
        external_session_id: str,
        agent_id: str = "",
        tenant_id: str = "",
        user_id: str = "",
    ) -> Dict[str, Any]:
        """Register a session the user opened *inside* OpenCode.

        Navigating inside the embedded app — a fork, or a new session made in
        its own UI — creates a session upstream that the platform has never
        heard of. This is the one way such a session joins the platform's
        history, and every part of it is a verification:

        * the source session must already be one of the caller's coding links,
          which is what establishes the owner, the Agent and the project;
        * the target must exist upstream, because a notification is not evidence
          that a session does;
        * it must be a *root* session: OpenCode's internal subtasks carry a
          ``parentID`` and are not conversations a user navigates back to;
        * it must live in the same project as the source. Otherwise a
          notification from one agent's page could register a conversation
          belonging to code somewhere else;
        * and it must not already belong to another of the caller's sessions,
          because re-claiming would move a conversation's history entry.

        The platform id is derived from the *external* id, so attaching the same
        remote session twice is idempotent, and a different subject derives a
        different platform id rather than colliding on this one.
        """
        settings = self._require_enabled()
        if not external_session_id:
            raise CodingError(
                CODING_INVALID_REQUEST, "external_session_id required", 400)
        source = self._link_or_service_error(source_session_id)
        if source is None:
            raise CodingError(
                CODING_NOT_LINKED,
                "the source session is not a linked coding session",
                404,
            )

        existing = self.store.find_coding_link_by_external(
            settings.service_id, external_session_id)
        if existing is not None:
            # Already registered. If it is the caller's own, this is idempotent
            # — and it also covers the ordinary case of navigating *from* one
            # own session *to* another, where the answer is simply the session
            # it already belongs to. Someone else's is a 404, not a 409: a
            # refusal that says "taken" would confirm the session exists.
            if self._link_owner(existing["session_id"], user_id):
                return self._describe(existing)
            raise CodingError(
                CODING_NOT_LINKED, "session not found", 404)

        remote = self.client.get_session(external_session_id, source["project_dir"])
        if remote is None:
            raise CodingError(
                CODING_NOT_LINKED, "the coding session does not exist upstream", 404)
        if remote.parent_id:
            raise CodingError(
                CODING_INVALID_REQUEST,
                "an internal subtask cannot become a history entry",
                400,
            )
        if remote.directory or remote.project_id:
            # "The target lives in the same project as the source" is a question
            # only the service can answer, and it answers most precisely with its
            # own project id. The directory is a weaker substitute: it is the path
            # string a session was *created* with, so one checkout can be reported
            # unresolved for a session the platform made and resolved for one made
            # inside OpenCode (a fork of the same project under a symlink --
            # macOS ``/tmp`` -> ``/private/tmp``). Comparing that string against
            # the platform's own configured value would refuse a user their own
            # conversation, so the id decides whenever both sides carry one.
            #
            # The platform never resolves the configured path itself: the project
            # is a directory on the *service's* host (design: 不得对远端项目执行
            # 平台本地 Path.resolve()), so a local canonicalisation would be
            # meaningless here -- and wrong whenever the two run apart.
            parent = self.client.get_session(source["external_session_id"],
                                             source["project_dir"])
            same_project = None
            if remote.project_id and parent and parent.project_id:
                same_project = remote.project_id == parent.project_id
            if same_project is None:
                parent_dir = (parent.directory if parent and parent.directory
                              else source["project_dir"])
                same_project = not remote.directory or remote.directory == parent_dir
            if not same_project:
                raise CodingError(
                    CODING_PROJECT_MISMATCH,
                    "the coding session belongs to a different project",
                    400,
                )

        identity = _ambient_identity()
        tenant_id = tenant_id or identity.get("tenant_id", "")
        user_id = user_id or identity.get("user_id", "")
        session_id = _derive_attached_id(
            service_id=settings.service_id,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id or source["agent_id"],
            external_session_id=external_session_id,
        )
        try:
            link = self.store.create_coding_link(
                session_id=session_id,
                external_session_id=external_session_id,
                service_id=settings.service_id,
                project_dir=source["project_dir"],
                request_id="",
                state="ready",
                title=remote.title,
            )
        except sqlite3.IntegrityError:
            # Two notifications for one session raced. The winner's row is the
            # answer when it is the caller's; otherwise this caller still has no
            # claim to it, and says so the same way the check above does.
            link = self.store.get_coding_link(session_id)
            if link is None or not self._link_owner(session_id, user_id):
                raise CodingError(
                    CODING_NOT_LINKED, "session not found", 404) from None
        self.store.touch_coding_link_cache(
            session_id, title=remote.title, remote_updated_ms=remote.updated_ms)
        return self._describe(self.store.get_coding_link(session_id) or link)

    def _link_owner(self, session_id: str, user_id: str) -> bool:
        """Whether the durable ``sessions`` row of a link is this member's own.

        The link table is per-Agent, not per-member: a shared workspace can hold
        several members' coding sessions side by side, so "linked" is not the
        same as "mine" and this is the probe that separates them.
        """
        with self.store._lock:
            con = self.store._connect()
            try:
                row = con.execute(
                    "SELECT owner, channel_type FROM sessions"
                    " WHERE session_id=? AND agent_id=?",
                    (session_id, self.store._dimensions().get("agent_id", "")),
                ).fetchone()
            finally:
                con.close()
        return bool(row) and row[0] == user_id and row[1] == "web"

    # -- sync --------------------------------------------------------------

    def sync(
        self,
        *,
        agent_id: str = "",
        cursor: Optional[str] = None,
        owner: str = "",
        tenant_id: str = "",
    ) -> Dict[str, Any]:
        """Refresh one batch of the caller's cached list from the service.

        The rules are asymmetric on purpose, because each wrong answer is
        destructive in one direction:

        * ``ready`` + a confirmed 404 is the *only* evidence of deletion;
        * ``creating`` + 404 is not — the create may not have landed yet, and
          dropping the reservation would strand the id the retry depends on;
        * anything else (timeout, refusal, unreachable) keeps the row and is
          reported in ``unavailable``;
        * a link from another service instance is unavailable, not deleted, and
          not refreshed: its ids name sessions this service does not have.

        The running set is read once for the whole batch, and reads are bounded
        so one round cannot open a request per linked session.
        """
        settings = self._require_enabled()
        if agent_id:
            self._require_coding_agent(agent_id)
        identity = _ambient_identity()
        owner = owner or identity.get("user_id", "")
        tenant_id = tenant_id or identity.get("tenant_id", "")

        batch = self.store.list_coding_links(
            owner=owner, tenant_id=tenant_id, limit=SYNC_BATCH, after=cursor)

        try:
            active = self.client.active_sessions()
        except CodingError:
            # Without the running set nothing can be compared, but the cached
            # list is still valid: report the whole batch as unavailable rather
            # than guessing at any row's state.
            return {
                "changed": [],
                "removed": [],
                "unavailable": [link["session_id"] for link in batch["links"]],
                "next_cursor": batch["next_cursor"],
            }

        changed, removed, unavailable = [], [], []
        for link, remote in self._read_batch(batch["links"]):
            if remote is _UNREACHABLE:
                unavailable.append(link["session_id"])
                continue
            if remote is None:
                if link["state"] == "ready":
                    if self.store.delete_coding_link(link["session_id"]):
                        removed.append(link["session_id"])
                else:
                    # Reserved but never confirmed: still retryable, not gone.
                    changed.append(self._sync_entry(link, None, active))
                continue
            if not self.store.touch_coding_link_cache(
                link["session_id"], title=remote.title,
                remote_updated_ms=remote.updated_ms,
            ):
                # The row went away under us (deleted while we were reading);
                # a late refresh must not bring it back.
                continue
            changed.append(self._sync_entry(link, remote, active))
        return {
            "changed": changed,
            "removed": removed,
            "unavailable": unavailable,
            "next_cursor": batch["next_cursor"],
        }

    def _require_coding_agent(self, agent_id: str) -> None:
        """Refuse an endpoint addressed at an Agent that is not a coding one.

        The mirrored rule of ``coding_web_only``: a coding Agent may only be
        used through this entry, and this entry only serves coding Agents.
        """
        from agent.registry import get_agent_registry

        try:
            profile = get_agent_registry().get(agent_id, require_enabled=False)
        except KeyError:
            raise CodingError(
                CODING_INVALID_REQUEST, f"agent '{agent_id}' does not exist", 400)
        if not profile.is_coding:
            raise CodingError(
                CODING_INVALID_REQUEST,
                f"agent '{profile.id}' is not a coding agent",
                400,
            )

    # -- management --------------------------------------------------------

    def rename(self, *, session_id: str, title: str) -> Dict[str, Any]:
        """Rename a coding session on the service, then in the cache.

        Remote first, deliberately: if the service refuses, nothing local
        changes and the console keeps showing the old title rather than one that
        only exists on this screen. OpenCode owns the name.
        """
        self._require_enabled()
        link = self._link_or_service_error(session_id)
        if link is None:
            raise CodingError(CODING_NOT_LINKED, "session not found", 404)
        title = (title or "").strip()
        if not title:
            raise CodingError(CODING_INVALID_REQUEST, "title required", 400)
        self.client.rename_session(
            link["external_session_id"], title, link["project_dir"])
        try:
            remote = self.client.get_session(
                link["external_session_id"], link["project_dir"])
        except CodingError:
            # The rename succeeded; refreshing the timestamp is a nicety, and
            # the local write below still records what the user asked for.
            remote = None
        self.store.touch_coding_link_cache(
            session_id, title=title,
            remote_updated_ms=remote.updated_ms if remote else None)
        return self._describe(self.store.get_coding_link(session_id) or link)

    def delete(self, *, session_id: str) -> Dict[str, Any]:
        """Delete a coding session: interrupt if running, then remove upstream.

        A remote 404 is success — the session the user asked to be rid of is not
        there — so a repeated delete is idempotent and a half-finished one can be
        retried. Any other upstream failure keeps the local record: the console
        must not drop a conversation the service still holds.

        The project directory is untouched. Deleting a conversation is not
        deleting a checkout.
        """
        self._require_enabled()
        link = self._link_or_service_error(session_id)
        if link is None:
            raise CodingError(CODING_NOT_LINKED, "session not found", 404)
        try:
            active = self.client.active_sessions()
        except CodingError:
            active = set()
        if link["external_session_id"] in active:
            try:
                self.client.interrupt_session(link["external_session_id"])
            except CodingError:
                # Nothing to interrupt, or the service refused: the delete
                # below is still the right next step.
                pass
        self.client.delete_session(
            link["external_session_id"], link["project_dir"])
        self.store.delete_coding_link(session_id)
        return {"session_id": session_id, "deleted": True}

    def link_for(self, session_id: str):
        """The link, or None — the probe the management paths ask first.

        Unlike :meth:`_link_or_service_error` this never raises for a service
        mismatch: the session handlers need to answer "is this a coding
        session?" *before* choosing a path, and a link from another instance
        must not turn a rename into a 409 before the session's existence is even
        established. The service check happens on the path that then talks to
        that service, where it is actionable.
        """
        try:
            return self.store.get_coding_link(session_id)
        except Exception:  # noqa: BLE001 - a probe must not fail the request
            return None

    def _read_batch(self, links):
        """Read every remote session in the batch, bounded and order-stable.

        Returns ``(link, remote)`` pairs in the batch order, where ``remote`` is
        the session, None for a confirmed absence, or the sentinel for a failure
        that is not evidence about that session at all.
        """
        from concurrent.futures import ThreadPoolExecutor

        if not links:
            return []
        settings_id = self.settings.service_id
        results: Dict[int, Any] = {}

        def read(index: int, link: Dict[str, Any]) -> None:
            if link["service_id"] != settings_id:
                results[index] = _UNREACHABLE
                return
            try:
                results[index] = self.client.get_session(
                    link["external_session_id"], link["project_dir"])
            except CodingError:
                results[index] = _UNREACHABLE

        workers = min(SYNC_CONCURRENCY, len(links))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(read, index, link)
                       for index, link in enumerate(links)]
            for future in futures:
                future.result()
        return [(link, results[index]) for index, link in enumerate(links)]

    @staticmethod
    def _sync_entry(link: Dict[str, Any], remote: Any,
                    active: set) -> Dict[str, Any]:
        """One refreshed list entry, in the shape the console list already uses."""
        if remote is None:
            state = "creating"
            title = ""
            updated_ms = 0
        else:
            state = "running" if remote.id in active else "idle"
            title = remote.title
            updated_ms = remote.updated_ms
        return {
            "session_id": link["session_id"],
            "agent_id": link["agent_id"],
            "external_session_id": link["external_session_id"],
            "title": title,
            "project_dir": link["project_dir"],
            "state": state,
            # Seconds, the unit the existing session list uses: the console
            # re-reads that list after a refresh, so the two must agree.
            "last_active": updated_ms // 1000,
        }


#: Sentinel for "the read told us nothing about this session" as opposed to
#: "the session is gone". A per-session failure must never be mistaken for
#: deletion evidence.
_UNREACHABLE = object()


def _derive_attached_id(
    *,
    service_id: str,
    tenant_id: str,
    user_id: str,
    agent_id: str,
    external_session_id: str,
) -> str:
    """The platform session id for a session first seen on the OpenCode side.

    Derived from the external id rather than a client request id, because there
    is no client request to retry: the same remote session must map to the same
    platform session every time the notification arrives, and a different
    subject must not derive the same one.
    """
    parts = []
    for value in (service_id, tenant_id, user_id, agent_id, external_session_id):
        text = "" if value is None else str(value)
        parts.append(f"{len(text)}:{text}")
    digest = hashlib.sha256(_FIELD_SEP.join(parts).encode("utf-8")).hexdigest()
    return f"{SESSION_PREFIX}{digest[:_DIGEST_CHARS]}"


def _ambient_identity() -> Dict[str, str]:
    try:
        from common.runtime_identity import current_identity

        ident = current_identity()
    except Exception:  # pragma: no cover - defensive, identity may not be set
        return {}
    return {
        "user_id": str(getattr(ident, "user_id", "") or ""),
        "tenant_id": str(getattr(ident, "tenant_id", "") or ""),
    }
