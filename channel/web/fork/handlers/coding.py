# encoding:utf-8
"""Fork web layer: the coding-session HTTP entry (change add-opencode-coding-agents).

Four endpoints, one per intent, and no fifth: create (or resume a create), open,
attach, sync. They are the *only* way the platform reaches the shared OpenCode
service, which keeps the boundary auditable — the service client is constructed
here and nowhere else in the web layer.

Authorization is not re-invented. Every request reuses the existing gates in the
same order the console's own chat and session endpoints use them:

    route policy (tenant) -> ``_db_scope()`` -> permission -> Agent binding
    -> private-owner scoping -> durable owner of the named session

Two properties are specific to this feature and are enforced here rather than in
the service, because they are about *the platform's* trust boundary:

* **ownership comes from the request identity, never the body.** ``tenant_id``
  and ``user_id`` are read from the verified ``RequestContext`` and passed down;
  a client cannot name an owner, and no endpoint accepts a credential, an
  upstream URL or a service id.
* **a coding session is only reachable by the member who owns it.** The link is
  looked up under the addressed Agent *and* the ``sessions`` row must match the
  caller's own user, tenant and ``channel_type`` — a same-named row belonging to
  another member, or another Agent's link, is a 404 that does not confirm it
  exists.

A coding Agent is refused everywhere else in the platform (``coding_web_only``);
the mirrored rule lives here: these endpoints serve coding Agents only, and
answer a plain 400 for anything else. Both refusals happen *after* the
permission gates, so a caller who lacks the grant always gets the same 403 they
would get for any Agent, and the type never becomes a hint.
"""

from __future__ import annotations

import json
import web

from agent.coding import (
    CODING_INVALID_REQUEST,
    CODING_NOT_LINKED,
    CODING_PROJECT_MISMATCH,
    CODING_DISABLED,
    CODING_SERVICE_CHANGED,
    CODING_UPSTREAM_UNAVAILABLE,
    CODING_WEB_ONLY,
)
from channel.web.fork.authorization import (
    _db_scope,
    _require_agent_action,
    _require_private_owner,
    _require_read_permission,
    _require_tenant_agent_binding,
    _storage_agent_key,
)
from common.log import logger


def _coding_error(message: str, status: str, code: str) -> None:
    """Refuse with a real HTTP status and the ``{status,code,message}`` body.

    The new surface never uses the legacy "200 + status=error" shape: the
    console branches on the HTTP status alone, and a broad ``except Exception``
    downstream must not be able to turn a refusal into a success.
    """
    raise web.HTTPError(
        status, {"Content-Type": "application/json; charset=utf-8"},
        json.dumps({"status": "error", "message": message, "code": code},
                   ensure_ascii=False))


def _body() -> dict:
    raw = web.data()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        _coding_error("invalid JSON", "400 Bad Request", CODING_INVALID_REQUEST)
    if not isinstance(parsed, dict):
        _coding_error("JSON object required", "400 Bad Request",
                      CODING_INVALID_REQUEST)
    return parsed


def _requested_agent(body: dict, params=None) -> str:
    """The addressed Agent, from the query and/or body, which must agree.

    Confirming they match stops a client from smuggling a second target past the
    one the server resolved: the *same* value has to be authorized and then used.
    """
    from channel.web.fork.runtime import _request_agent_id

    query_agent = _request_agent_id(params) if params is not None else ""
    body_agent = _request_agent_id(body)
    if query_agent and body_agent and query_agent != body_agent:
        _coding_error("agent_id mismatch", "400 Bad Request", CODING_INVALID_REQUEST)
    return body_agent or query_agent


def _conversation_store(agent_id: str):
    """The addressed Agent's conversation store — the one the list reads from."""
    from channel.web.fork.handlers.sessions import _conversation_store_for

    return _conversation_store_for(agent_id)


def _coding_service(store):
    """A service bound to this Agent's store and the operator's global settings."""
    from agent.coding.sessions import CodingSessionService

    return CodingSessionService(store)


def _require_coding_profile(agent_id: str):
    """The Agent's profile, refusing a non-coding target with a plain 400.

    Called after the permission gates: an unauthorized caller must never learn
    the Agent's type from this endpoint's answer.
    """
    from agent.registry import get_agent_registry

    try:
        profile = get_agent_registry().get(agent_id, require_enabled=False)
    except KeyError:
        _coding_error(f"agent '{agent_id}' not found", "404 Not Found",
                      CODING_NOT_LINKED)
    if not profile.is_coding:
        _coding_error(f"agent '{profile.id}' is not a coding agent",
                      "400 Bad Request", CODING_INVALID_REQUEST)
    return profile


def _project_dir(profile) -> str:
    """The Agent's configured project, which the client never supplies.

    The directory is what the conversation is *about*: letting the caller send
    it would let one Agent's page open another checkout. It is read from the
    profile at creation time and stored on the link, so later edits to the
    Agent's default move future sessions only.
    """
    project = (profile.coding_project_dir or "").strip()
    if not project:
        _coding_error("the coding agent has no project directory configured",
                      "400 Bad Request", CODING_INVALID_REQUEST)
    return project


def _owned_link(ctx, session_id: str, agent_id: str, store):
    """The caller's own link for ``session_id``, or a 404 that reveals nothing.

    Two facts have to hold, and both are checked in one place so the open,
    attach and management paths cannot disagree: the session must be linked
    under the addressed Agent, and its ``sessions`` row must be this caller's
    own (user, tenant, web). The tenant predicate admits the empty bucket for
    the same reason the context endpoints do — a freshly composed row is
    tenant-stamped by the composer, and only the boot backfill repairs the older
    ones — while another tenant's real id stays excluded.

    A same-named session owned by another member, or another Agent's link, is
    *no row* here: existence is not confirmed to a caller who cannot see it.
    """
    link = store.get_coding_link(session_id)
    if link is None:
        _coding_error("session not found", "404 Not Found", CODING_NOT_LINKED)
    with store._lock:
        con = store._connect()
        try:
            row = con.execute(
                "SELECT owner, tenant_id, channel_type FROM sessions"
                " WHERE session_id=? AND agent_id=?",
                (session_id, _storage_agent_key(store)),
            ).fetchone()
        finally:
            con.close()
    if row is None or row[0] != ctx.user_id or row[2] != "web":
        _coding_error("session not found", "404 Not Found", CODING_NOT_LINKED)
    if ctx.tenant_id and row[1] not in (ctx.tenant_id, ""):
        _coding_error("session not found", "404 Not Found", CODING_NOT_LINKED)
    return link


def _run_coding(call):
    """Translate a service refusal into its HTTP answer, and log the rest.

    A ``CodingError`` already carries the status and code the contract
    promises, so it is passed through as-is. Anything else is a defect: it is
    logged and reported as a 500 rather than dressed up as a coding failure,
    which would hide a bug behind a plausible code.
    """
    from agent.coding import CodingError

    try:
        return call()
    except CodingError as error:
        _coding_error(error.message, f"{error.status} {_reason(error.status)}",
                      error.code)
    except web.HTTPError:
        raise
    except Exception as error:  # noqa: BLE001 - reported, never disguised
        logger.error(f"[WebChannel] Coding session error: {error}")
        raise


def _reason(status: int) -> str:
    """The HTTP reason phrase for a refusal, kept in one table.

    Shared with the rest of the fork layer so a status never renders two
    different phrases depending on which handler refused.
    """
    from channel.web.fork.runtime import _HTTP_STATUS_TEXT

    return _HTTP_STATUS_TEXT.get(status, "Bad Request")


def _success(payload: dict) -> str:
    web.header("Content-Type", "application/json; charset=utf-8")
    return json.dumps({"status": "success", **payload}, ensure_ascii=False)


class CodingSessionsHandler:
    """``POST /api/coding/sessions`` — create a session, or resume its creation.

    The same request is sent on the first click and on every retry, so this is
    where the retry contract is observable: the same ``(agent, request_id)``
    returns the same ``session_id`` and never creates a second conversation.

    Execution is authorized like starting a chat, not like reading history: the
    caller is about to give an Agent work, so ``chat.use`` and that Agent's
    ``agent.use`` grant are both required, and a revoked grant blocks the very
    next request.
    """

    def POST(self):
        from channel.web.fork.handlers.chat import _require_chat_use

        body = _body()

        def work():
            with _db_scope() as ctx:
                agent_id = _require_tenant_agent_binding(
                    ctx, _requested_agent(body))
                _require_private_owner(ctx, agent_id)
                _require_chat_use(ctx)
                _require_agent_action(ctx, agent_id, "use", "agent.use")
                profile = _require_coding_profile(agent_id)
                store = _conversation_store(agent_id)
                service = _coding_service(store)
                result = service.reserve(
                    agent_id=agent_id,
                    project_dir=_project_dir(profile),
                    request_id=str(body.get("request_id") or "").strip(),
                    tenant_id=getattr(ctx, "tenant_id", "") or "",
                    user_id=getattr(ctx, "user_id", "") or "",
                )
                return _success(result)

        return _run_coding(work)


class CodingSessionOpenHandler:
    """``GET /api/coding/sessions/{sid}/open`` — the frame to mount, and its summary.

    A GET never creates remote state. A reservation still being created is
    reported as retriable so the console can resume it through the create
    endpoint; if this call created sessions, reloading the page would silently
    start new conversations.
    """

    def GET(self, session_id: str):
        params = web.input(agent_id="")

        def work():
            with _db_scope() as ctx:
                agent_id = _require_tenant_agent_binding(
                    ctx, _requested_agent({}, params))
                _require_private_owner(ctx, agent_id)
                _require_read_permission(ctx, "history.read")
                _require_agent_action(ctx, agent_id, "use", "agent.use")
                _require_coding_profile(agent_id)
                store = _conversation_store(agent_id)
                _owned_link(ctx, session_id, agent_id, store)
                result = _coding_service(store).open(
                    session_id=session_id, agent_id=agent_id)
                return _success(result)

        return _run_coding(work)


class CodingSessionAttachHandler:
    """``POST /api/coding/sessions/attach`` — register a session seen inside OpenCode.

    The source platform session is authorized as the caller's own *before* the
    target is looked at, which is what establishes the owner, Agent and project
    the target is then verified against. Nothing in the body is trusted: the
    notification says which session changed, and the server re-reads the remote
    session to decide whether it may be registered.
    """

    def POST(self):
        body = _body()

        def work():
            with _db_scope() as ctx:
                agent_id = _require_tenant_agent_binding(
                    ctx, _requested_agent(body))
                _require_private_owner(ctx, agent_id)
                _require_read_permission(ctx, "history.read")
                _require_agent_action(ctx, agent_id, "use", "agent.use")
                _require_coding_profile(agent_id)
                store = _conversation_store(agent_id)
                source_session_id = str(body.get("source_session_id") or "").strip()
                if not source_session_id:
                    _coding_error("source_session_id required", "400 Bad Request",
                                  CODING_INVALID_REQUEST)
                _owned_link(ctx, source_session_id, agent_id, store)
                result = _coding_service(store).attach(
                    source_session_id=source_session_id,
                    external_session_id=str(
                        body.get("external_session_id") or "").strip(),
                    agent_id=agent_id,
                    tenant_id=getattr(ctx, "tenant_id", "") or "",
                    user_id=getattr(ctx, "user_id", "") or "",
                )
                return _success(result)

        return _run_coding(work)


class CodingSessionSyncHandler:
    """``POST /api/coding/sessions/sync`` — refresh one batch of the cached list.

    Mirrors the sessions list's authorization: reading history is
    ``history.read`` scoped to the caller's own rows, and the batch only ever
    contains links whose ``sessions`` row is the caller's.
    """

    def POST(self):
        body = _body()

        def work():
            with _db_scope() as ctx:
                requested = _requested_agent(body)
                if not requested:
                    # The tenant default is never a coding Agent, so an
                    # Agent-less sync could only resolve to one that cannot be
                    # synced. Say so plainly instead of reporting a type error.
                    _coding_error("agent_id required", "400 Bad Request",
                                  CODING_INVALID_REQUEST)
                agent_id = _require_tenant_agent_binding(ctx, requested)
                _require_private_owner(ctx, agent_id)
                _require_read_permission(ctx, "history.read")
                _require_coding_profile(agent_id)
                store = _conversation_store(agent_id)
                result = _coding_service(store).sync(
                    agent_id=agent_id,
                    cursor=(str(body.get("cursor") or "").strip() or None),
                    owner=getattr(ctx, "user_id", "") or "",
                    tenant_id=getattr(ctx, "tenant_id", "") or "",
                )
                return _success(result)

        return _run_coding(work)


class CodingSettingsHandler:
    """``GET /api/coding/settings`` — the read-only projection of the service.

    The console shows which service it is talking to and whether it is usable.
    The password, its environment variable name and any upstream credential are
    absent by construction (``settings_for_console``): the form displays the
    service, it never submits it, so an operator's secret has no path through
    this endpoint.
    """

    def GET(self):
        from agent.coding import settings_for_console
        from agent.registry import get_agent_registry

        def work():
            with _db_scope() as ctx:
                _require_read_permission(ctx, "agent.read")
                projection = settings_for_console()
                # Which Agents can be used right now, so the console does not
                # present a coding card as openable while the capability is off.
                projection["agents"] = sorted(
                    profile.id for profile in get_agent_registry().list()
                    if profile.enabled and profile.is_coding)
                return _success(projection)

        return _run_coding(work)
