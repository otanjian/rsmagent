"""Fork web layer (change adopt-upstream-web-split, design D2).

Fork-owned implementation, moved verbatim out of the former
channel/web/web_channel.py monolith. Upstream's api/ modules are not
edited. Imports inside function bodies are lazy so these modules can
reference each other without import cycles.
"""

from __future__ import annotations
from auth.runtime import authorized_target, authorized_target_scope
from bridge.context import *
from contextlib import contextmanager
import json
import re
import time
import uuid
import web


def _require_chat_use(ctx: "Optional[RequestContext]") -> None:
    """Require the functional ``chat.use`` permission to run a chat.

    The chat consumer is gated by ``chat.use``; a member holding ``agent.use``
    but not ``chat.use`` cannot start a conversation. Platform/tenant admins and
    legacy mode pass. Recomputed per request (never cached).
    """
    if ctx is None:
        return
    if ctx.is_platform_admin or ctx.is_tenant_admin:
        return
    if "chat.use" not in ctx.permissions:
        raise web.HTTPError("403 Forbidden", {"Content-Type": "application/json"},
                            json.dumps({"status": "error", "message": "forbidden",
                                        "code": "forbidden"}))


def _chat_error(message: str, status: str = "403 Forbidden", code: str = "forbidden"):
    raise web.HTTPError(status, {"Content-Type": "application/json; charset=utf-8"},
                        json.dumps({"status": "error", "message": message, "code": code}))


def _chat_body() -> dict:
    try:
        body = json.loads(web.data() or b"{}")
    except (TypeError, ValueError):
        _chat_error("invalid JSON", "400 Bad Request", "bad_request")
    if not isinstance(body, dict):
        _chat_error("JSON object required", "400 Bad Request", "bad_request")
    return body


def _require_chat_csrf() -> None:
    # Reuse the unified credential selection so a same-value repeated cookie+
    # bearer is still treated as a cookie request (origin check applies), and a
    # different-value pair is a hard 400 mixed_credentials rather than a bypass.
    from channel.web.auth_handlers import _csrf_ok
    if not _csrf_ok():
        _chat_error("invalid request origin", code="csrf_failed")


def _authorize_chat_session(ctx, session_id, agent_id, *, create=False) -> str:
    """Authorize and, for a new chat, atomically claim its durable owner.

    Runtime/queue/cancellation keys include Agent and session but not user. A
    SELECT followed by asynchronous persistence would let two users race for
    the same new key. Claim it before dispatch, preserving all existing owners
    (including legacy owner='') and never borrowing another Agent's workspace.

    Database-mode authorization mirrors ``_workbench_chat_readiness`` exactly
    (functional ``chat.use`` + the target Agent's ``agent.use`` resource grant,
    platform/tenant admin bypass), so a caller that only reads the card can
    never start/resume a conversation. Recomputed on every call — never cached —
    so a revoked grant applies to the next send/poll/steer. Legacy mode
    (``ctx is None``) stays open.
    """
    from channel.web.web_channel import _require_agent_action
    from channel.web.web_channel import _require_chat_use
    from channel.web.web_channel import _require_private_owner
    from channel.web.web_channel import _require_tenant_agent_binding
    if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 256:
        _chat_error("valid session_id required", "400 Bad Request", "bad_request")
    agent_id = _require_tenant_agent_binding(ctx, agent_id)
    _require_private_owner(ctx, agent_id)
    from agent.registry import get_agent_registry
    from agent.memory import get_conversation_store
    try:
        profile = get_agent_registry().get(agent_id)
    except (KeyError, ValueError):
        _chat_error("agent not found", "404 Not Found", "not_found")

    def _execution_gates() -> None:
        """The gates a send/poll/steer re-runs once the session is the caller's.

        Never cached, so a revoked grant blocks the very next request. A coding
        Agent is refused only *after* the permission gates: a caller who may not
        use the Agent at all gets the same 403 they would get for any Agent, and
        the type never turns a permission answer into a hint. The refusal still
        precedes the INSERT, so a coding Agent gains no platform session row and
        no ordinary runtime is ever initialized for it.
        """
        _require_chat_use(ctx)
        _require_agent_action(ctx, agent_id, "use", "agent.use")
        if profile.is_coding:
            from agent.coding import coding_web_only

            error = coding_web_only(profile.id)
            _chat_error(error.message, f"{error.status} Bad Request", error.code)

    store = get_conversation_store(profile.workspace)
    with store._lock:
        con = store._connect()
        try:
            with con:
                # A session that exists but is not *this caller's own* is a 404:
                # masking its existence keeps one member from probing another
                # member's conversation ids. Only after the session is confirmed
                # to be the caller's (or brand new) do the execution gates run.
                #
                # Every Agent's conversations share one file now, so both the
                # read and the write below are scoped to the handle's Agent. An
                # unscoped SELECT would treat another Agent's same-id session as
                # this caller's, and an INSERT without ``agent_id`` would file
                # the row under the default Agent (``''``), leaving the session
                # invisible to the Agent that actually owns it.
                scope_agent = store._agent_id
                row = con.execute(
                    "SELECT owner, channel_type FROM sessions "
                    "WHERE session_id=? AND agent_id=?",
                    (session_id, scope_agent),
                ).fetchone()
                if row is not None and (row[0] != ctx.user_id or row[1] != "web"):
                    _chat_error("session not found", "404 Not Found", "not_found")
                if row is None:
                    # Brand-new session: this caller is its first claimant. The
                    # usage gates still run before INSERT so a denied caller
                    # never leaves an orphaned session row behind.
                    _execution_gates()
                    if create:
                        now = int(time.time())
                        # ``tenant_id`` is stamped here, at claim time, so the
                        # row a Web composer just created is complete. Leaving
                        # it out made every newly composed conversation
                        # owner-less in the tenancy dimension until the next
                        # boot backfill, which the context endpoints (and any
                        # other exact-tenant reader) read as "not this caller's
                        # row". ``INSERT OR IGNORE`` keeps an existing row --
                        # including a legacy one that carries ``''`` -- exactly
                        # as it is.
                        con.execute(
                            "INSERT OR IGNORE INTO sessions "
                            "(agent_id, session_id, channel_type, owner, tenant_id,"
                            " created_at, last_active, msg_count) "
                            "VALUES (?, ?, 'web', ?, ?, ?, ?, 0)",
                            (scope_agent, session_id, ctx.user_id,
                             getattr(ctx, "tenant_id", "") or "", now, now),
                        )
                        # Two callers may race to claim the same new key; only
                        # the winner's owner survives the IGNORE above.
                        claimed = con.execute(
                            "SELECT owner FROM sessions "
                            "WHERE session_id=? AND agent_id=?",
                            (session_id, scope_agent),
                        ).fetchone()
                        if not claimed or claimed[0] != ctx.user_id:
                            _chat_error("session not found", "404 Not Found", "not_found")
                else:
                    # Resuming the caller's own conversation re-validates the
                    # execution gates (never cached: a revoked grant blocks the
                    # very next send/poll/steer on this session).
                    _execution_gates()
        finally:
            con.close()
    return agent_id


def _owned_chat_request(channel, request_id):
    if not isinstance(request_id, str) or not request_id:
        _chat_error("request_id required", "400 Bad Request", "bad_request")
    # Tuples are captured by the authenticated /message route, never by the
    # event payload or client-supplied user/tenant/session identifiers.
    with channel._sse_streams_lock:
        owner = getattr(channel, "request_owners", {}).get(request_id)
    if owner is None:
        _chat_error("request not found", "404 Not Found", "not_found")
    return owner


def _authorize_chat_request(ctx, channel, request_id):
    from channel.web.web_channel import _authorize_chat_session
    tenant_id, user_id, agent_id, session_id = _owned_chat_request(channel, request_id)
    if tenant_id != ctx.tenant_id or user_id != ctx.user_id:
        _chat_error("request not found", "404 Not Found", "not_found")
    _authorize_chat_session(ctx, session_id, agent_id)
    return agent_id, session_id


@contextmanager
def _stream_identity_scope(channel, request_id):
    """Native EventSource sends cookies but cannot add X-Tenant-ID.

    Authenticate the login first, then resolve the recorded request's tenant
    and current membership. A supplied tenant selection must still agree.
    """
    from auth.runtime import resolve_context, to_runtime_identity, IdentityContextError
    from channel.web.auth_handlers import _get_service, _session_token
    from common.runtime_identity import use_identity
    svc, token = _get_service(), _session_token()
    try:
        personal = resolve_context(svc, token, None)
        owner = _owned_chat_request(channel, request_id)
        if personal.user_id != owner[1]:
            _chat_error("request not found", "404 Not Found", "not_found")
        query_tenant = web.input(tenant_id="").tenant_id
        for selected in (web.ctx.env.get("HTTP_X_TENANT_ID", ""), query_tenant):
            if selected and selected != owner[0]:
                _chat_error("conflicting tenant selection", "400 Bad Request", "conflicting_tenant")
        ctx = resolve_context(svc, token, owner[0])
    except IdentityContextError as e:
        from http import HTTPStatus
        _chat_error(str(e), f"{e.status} {HTTPStatus(e.status).phrase}", e.code)
    if ctx.must_change_password:
        _chat_error("password change required", code="password_change_required")
    with use_identity(to_runtime_identity(ctx)):
        yield ctx


class MessageHandler:
    # Chat is now a request-scoped *tenant* consumer in database mode: it runs
    # inside _db_scope (which resolves the DB session + tenant and applies the
    # ambient identity) rather than being blocked, so the runtime path works for
    # a logged-in DB user. The derived identity is snapshotted in post_message
    # for the worker thread.
    def POST(self):
        from channel.web.web_channel import WebChannel
        from channel.web.web_channel import _authorize_chat_session
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _request_agent_id
        web.header("Content-Type", "application/json; charset=utf-8")
        web.header("Cache-Control", "no-store")
        with _db_scope() as ctx:
            _require_chat_csrf()
            body = _chat_body()
            if not isinstance(body.get("message", ""), str):
                _chat_error("message must be text", "400 Bad Request", "bad_request")
            session_id = body.get("session_id") or ("session_" + uuid.uuid4().hex)
            # /cancel and /steer are dispatched by post_message's fast path;
            # ownership must be verified before reaching either one.
            command = (body.get("message") or "").strip().lower()
            creating = not (command == "/cancel" or body.get("steer")
                            or re.match(r"^/steer(?:\s|$)", command))
            agent_id = _authorize_chat_session(
                ctx, session_id, _request_agent_id(body), create=creating,
            )
            with authorized_target_scope(
                auth_context=ctx, session=(agent_id, session_id),
            ):
                return WebChannel().post_message()


class PollHandler:
    def POST(self):
        from channel.web.web_channel import WebChannel
        from channel.web.web_channel import _authorize_chat_session
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _request_agent_id
        web.header("Content-Type", "application/json; charset=utf-8")
        web.header("Cache-Control", "no-store")
        with _db_scope() as ctx:
            _require_chat_csrf()
            body = _chat_body()
            session_id = body.get("session_id")
            agent_id = _authorize_chat_session(ctx, session_id, _request_agent_id(body))
            with authorized_target_scope(session=(agent_id, session_id)):
                return WebChannel().poll_response()


class CancelHandler:
    def POST(self):
        from channel.web.web_channel import WebChannel
        from channel.web.web_channel import _authorize_chat_session
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _request_agent_id
        web.header("Content-Type", "application/json; charset=utf-8")
        web.header("Cache-Control", "no-store")
        with _db_scope() as ctx:
            _require_chat_csrf()
            body, channel = _chat_body(), WebChannel()
            request_id = body.get("request_id")
            if request_id:
                agent_id, session_id = _authorize_chat_request(ctx, channel, request_id)
                if (body.get("session_id") and body["session_id"] != session_id
                        or body.get("agent_id") and body["agent_id"] != agent_id):
                    _chat_error("request/session mismatch", "400 Bad Request", "bad_request")
            else:
                session_id = body.get("session_id")
                agent_id = _authorize_chat_session(ctx, session_id, _request_agent_id(body))
            with authorized_target_scope(session=(agent_id, session_id)):
                return channel.cancel_request()


class StreamHandler:
    def GET(self):
        # Native EventSource authenticates by cookie; its recorded request
        # supplies the tenant for fresh membership and personal-owner checks.
        from channel.web.web_channel import WebChannel
        from channel.web.web_channel import _is_database_identity
        from channel.web.web_channel import _parse_sse_cursor
        from channel.web.web_channel import _stream_identity_scope
        params = web.input(request_id='', after_seq='')
        request_id = params.request_id
        if not request_id:
            raise web.badrequest()

        # Explicit query cursors are used by the frontend's manually-created
        # EventSource. Native EventSource reconnects remain compatible via the
        # standard Last-Event-ID request header.
        after_seq = _parse_sse_cursor(
            params.after_seq,
            web.ctx.env.get('HTTP_LAST_EVENT_ID', '0'),
        )

        channel = WebChannel()
        if _is_database_identity():
            with _stream_identity_scope(channel, request_id) as ctx:
                _authorize_chat_request(ctx, channel, request_id)

        web.header('Content-Type', 'text/event-stream; charset=utf-8')
        web.header('Cache-Control', 'no-cache')
        web.header('X-Accel-Buffering', 'no')
        if not _is_database_identity():
            web.header('Access-Control-Allow-Origin', '*')

        return channel.stream_response(request_id, after_seq)


