"""OpenAI-compatible HTTP adapter for 容大AI chat completions."""

from __future__ import annotations

from common.utils import constant_time_equals
import json
import queue
import threading
import time
import uuid
from typing import Callable, Iterator

from common.log import logger
from config import conf


class OpenAIAPIError(Exception):
    """A public API error with an explicit HTTP status."""

    def __init__(self, status_code: int, message: str, code: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


_SESSION_LOCKS = tuple(threading.Lock() for _ in range(64))
_STREAM_END = object()
_STREAM_ERROR = object()
_FIRST_EVENT_TIMEOUT_SECONDS = 30


def _authenticate(authorization: str, external_api_token: str) -> None:
    token = str(external_api_token or "")
    if not token:
        raise OpenAIAPIError(
            503,
            "The external chat completions API is disabled.",
            "api_disabled",
        )
    scheme, separator, credential = str(authorization or "").partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not credential
        or not constant_time_equals(credential.strip(), token)
    ):
        raise OpenAIAPIError(
            401, "Invalid authentication credentials.", "invalid_api_key"
        )


def _request_values(payload: dict, completion_id: str):
    if not isinstance(payload, dict):
        raise OpenAIAPIError(
            400, "Request body must be a JSON object.", "invalid_request"
        )

    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise OpenAIAPIError(
            400, "'model' must be a non-empty string.", "invalid_request"
        )

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise OpenAIAPIError(
            400, "'messages' must be a non-empty array.", "invalid_request"
        )

    query = None
    for message in messages:
        if not isinstance(message, dict):
            raise OpenAIAPIError(
                400, "Each message must be an object.", "invalid_request"
            )
        role = message.get("role")
        content = message.get("content")
        if role not in ("system", "user", "assistant"):
            raise OpenAIAPIError(
                400, "Only text chat messages are supported.", "invalid_request"
            )
        if not isinstance(content, str):
            raise OpenAIAPIError(
                400, "Message content must be a string.", "invalid_request"
            )
        if role == "user" and content.strip():
            query = content
    if query is None:
        raise OpenAIAPIError(
            400,
            "'messages' must contain a non-empty user message.",
            "invalid_request",
        )

    conversation_id = payload.get("conversation_id")
    user = payload.get("user")
    if conversation_id is not None and (
        not isinstance(conversation_id, str) or not conversation_id.strip()
    ):
        raise OpenAIAPIError(
            400, "'conversation_id' must be a non-empty string.", "invalid_request"
        )
    if user is not None and (not isinstance(user, str) or not user.strip()):
        raise OpenAIAPIError(
            400, "'user' must be a non-empty string.", "invalid_request"
        )

    if conversation_id:
        session_id = f"openai:conversation:{conversation_id.strip()}"
    elif user:
        session_id = f"openai:user:{user.strip()}"
    else:
        session_id = f"openai:request:{completion_id}"

    stream = payload.get("stream", False)
    if not isinstance(stream, bool):
        raise OpenAIAPIError(400, "'stream' must be a boolean.", "invalid_request")
    return model.strip(), query, session_id, stream


def _base_chunk(
    completion_id: str,
    created: int,
    model: str,
    delta: dict,
    finish_reason=None,
    cow_event: dict | None = None,
) -> dict:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    if cow_event is not None:
        payload["cow_event"] = cow_event
    return payload


def _tool_events(chunk: dict) -> list[dict]:
    chunk_type = chunk.get("chunk_type")
    if chunk_type == "tool_start":
        return [
            {
                "type": "tool_start",
                "id": chunk.get("tool_id"),
                "name": chunk.get("tool"),
                "arguments": chunk.get("arguments") or {},
            }
        ]
    if chunk_type == "tool_calls":
        return [
            {
                "type": "tool_result",
                "id": item.get("id"),
                "name": item.get("name"),
                "arguments": item.get("arguments") or {},
                "result": item.get("result", ""),
                "status": item.get("status"),
                "elapsed": item.get("elapsed"),
            }
            for item in chunk.get("tool_calls") or []
        ]
    if chunk_type in ("subagent_step", "artifact"):
        event = dict(chunk)
        event["type"] = event.pop("chunk_type")
        return [event]
    return []


def _sse_frame(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _request_cancel_scope(
    request_id: str,
    session_id: str | None = None,
    agent_id: str | None = None,
) -> tuple[str, str | None]:
    """Return the request key and session group used by ChatService."""
    from bridge.bridge import Bridge

    agent_bridge = Bridge().get_agent_bridge()
    resolved_agent_id = agent_bridge._resolve_agent_id(agent_id)
    default_agent_id = agent_bridge.agent_registry.default_agent_id
    request_key = agent_bridge._cancel_key(
        resolved_agent_id, request_id, default_agent_id
    )
    session_key = (
        agent_bridge._cancel_key(resolved_agent_id, session_id, default_agent_id)
        if session_id
        else None
    )
    return request_key, session_key


def _request_cancel_key(request_id: str, agent_id: str | None = None) -> str:
    """Return the Agent-scoped cancellation key for one request."""
    return _request_cancel_scope(request_id, agent_id=agent_id)[0]


def _cancel_agent_request(request_id: str, agent_id: str | None = None) -> bool:
    """Cancel one request without affecting newer runs in the same session."""
    from agent.protocol import get_cancel_registry

    return get_cancel_registry().cancel_request(
        _request_cancel_key(request_id, agent_id)
    )


def _stream_completion(
    run_chat: Callable,
    query: str,
    session_id: str,
    completion_id: str,
    created: int,
    model: str,
    agent_id: str | None = None,
) -> Iterator[str]:
    from agent.protocol import get_cancel_registry

    registry = get_cancel_registry()
    cancel_key, scoped_session_key = _request_cancel_scope(
        completion_id, session_id, agent_id
    )
    registry.register(cancel_key, session_id=scoped_session_key)

    output = queue.Queue(maxsize=256)
    closed = threading.Event()

    def publish(item) -> None:
        while not closed.is_set():
            try:
                output.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    def send_chunk(chunk: dict) -> None:
        chunk_type = chunk.get("chunk_type")
        if chunk_type == "content" and chunk.get("delta"):
            publish(
                _base_chunk(completion_id, created, model, {"content": chunk["delta"]})
            )
        elif chunk_type == "reasoning" and chunk.get("delta"):
            publish(
                _base_chunk(
                    completion_id,
                    created,
                    model,
                    {"reasoning_content": chunk["delta"]},
                    cow_event={"type": "reasoning", "delta": chunk["delta"]},
                )
            )
        else:
            for event in _tool_events(chunk):
                publish(_base_chunk(completion_id, created, model, {}, cow_event=event))

    def execute() -> None:
        try:
            lock = _SESSION_LOCKS[hash(session_id) % len(_SESSION_LOCKS)]
            with lock:
                if closed.is_set():
                    return
                run_chat(
                    query,
                    session_id,
                    send_chunk,
                    channel_type="openai_api",
                    agent_id=agent_id,
                    request_id=completion_id,
                )
        except Exception:  # noqa: BLE001 - worker boundary becomes an SSE error
            logger.exception("[OpenAI API] Chat completion failed")
            publish(_STREAM_ERROR)
        finally:
            try:
                publish(_STREAM_END)
            finally:
                registry.unregister(cancel_key)

    # Run the chat in a context-copied worker so a database-mode caller's
    # resolved RuntimeIdentity (user/tenant/web session) is inherited by the
    # model-authorization and tenant path scoping inside the run.
    from common.runtime_identity import wrap

    worker = threading.Thread(
        target=wrap(execute), name="openai-chat-completion", daemon=True
    )
    try:
        worker.start()
    except Exception:
        registry.unregister(cancel_key)
        raise
    try:
        first_item = output.get(timeout=_FIRST_EVENT_TIMEOUT_SECONDS)
    except queue.Empty as error:
        closed.set()
        _cancel_agent_request(completion_id, agent_id)
        raise OpenAIAPIError(
            500, "容大AI timed out before producing a response.", "timeout"
        ) from error
    if first_item is _STREAM_ERROR:
        closed.set()
        raise OpenAIAPIError(
            500, "容大AI failed to complete the request.", "internal_error"
        )

    def frames() -> Iterator[str]:
        finish_reason = "stop"
        completed = False
        item = first_item
        try:
            yield _sse_frame(
                _base_chunk(completion_id, created, model, {"role": "assistant"})
            )
            while item is not _STREAM_END:
                if item is _STREAM_ERROR:
                    finish_reason = "error"
                    yield _sse_frame(
                        _base_chunk(
                            completion_id,
                            created,
                            model,
                            {},
                            cow_event={
                                "type": "error",
                                "message": "容大AI failed to complete the request.",
                            },
                        )
                    )
                else:
                    yield _sse_frame(item)
                item = output.get()
            completed = True
            yield _sse_frame(
                _base_chunk(
                    completion_id,
                    created,
                    model,
                    {},
                    finish_reason=finish_reason,
                )
            )
            yield "data: [DONE]\n\n"
        finally:
            closed.set()
            if not completed:
                _cancel_agent_request(completion_id, agent_id)

    return frames()


def _non_stream_completion(
    run_chat: Callable,
    query: str,
    session_id: str,
    completion_id: str,
    created: int,
    model: str,
    agent_id: str | None = None,
) -> dict:
    content = []
    reasoning = []
    tool_trace = []

    def send_chunk(chunk: dict) -> None:
        chunk_type = chunk.get("chunk_type")
        if chunk_type == "content":
            content.append(chunk.get("delta") or "")
        elif chunk_type == "reasoning":
            reasoning.append(chunk.get("delta") or "")
        else:
            tool_trace.extend(_tool_events(chunk))

    try:
        lock = _SESSION_LOCKS[hash(session_id) % len(_SESSION_LOCKS)]
        with lock:
            run_chat(
                query,
                session_id,
                send_chunk,
                channel_type="openai_api",
                agent_id=agent_id,
                request_id=completion_id,
            )
    except Exception as error:
        logger.exception("[OpenAI API] Chat completion failed")
        raise OpenAIAPIError(
            500, "容大AI failed to complete the request.", "internal_error"
        ) from error

    message = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tool_trace:
        message["tool_trace"] = tool_trace
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "stop",
            }
        ],
    }


def _encode_stream(stream: Iterator[str]) -> Iterator[bytes]:
    """Encode SSE frames while propagating client disconnects to the source."""
    try:
        for frame in stream:
            yield frame.encode("utf-8")
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()


def handle_chat_completions(
    payload: dict,
    authorization: str,
    external_api_token: str,
    run_chat: Callable,
    created: int | None = None,
    completion_id: str | None = None,
    *,
    preauthenticated: bool = False,
    agent_id: str | None = None,
):
    """Validate one request and return a completion dict or SSE iterator.

    ``preauthenticated`` marks a database-mode request whose identity was
    already resolved by the handler (DB session + X-Tenant-ID + agent binding);
    the legacy ``external_api_token`` check is then skipped.
    """
    if not preauthenticated:
        _authenticate(authorization, external_api_token)
    completion_id = completion_id or f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time()) if created is None else int(created)
    model, query, session_id, stream = _request_values(payload, completion_id)
    if stream:
        return _stream_completion(
            run_chat, query, session_id, completion_id, created, model, agent_id
        )
    return _non_stream_completion(
        run_chat, query, session_id, completion_id, created, model, agent_id
    )


def _is_database_mode() -> bool:
    """Database is the only identity mode after retire-legacy-identity-mode."""
    return True


def _db_request_identity_and_agent() -> "tuple":
    """Resolve identity + tenant-bound agent for a database-mode API request.

    Database mode accepts either:
    * a service-account API key (``Authorization: Bearer sak_…``), resolved to
      a real User + tenant membership; or
    * a database session token (Bearer/cookie) + ``X-Tenant-ID`` like the Web
      console.

    ``external_api_token`` has no meaning here. Raises ``OpenAIAPIError``.
    """
    import web

    from auth.runtime import IdentityContextError, member_context, to_runtime_identity
    from auth.service import IdentityServiceError, get_identity_service
    from channel.web.auth_handlers import _get_service, _resolve_ctx_svc, _session_token

    auth_header = (web.ctx.env.get("HTTP_AUTHORIZATION", "") or "").strip()
    bearer = ""
    if auth_header.lower().startswith("bearer "):
        bearer = auth_header[7:].strip()

    try:
        if bearer.startswith("sak_"):
            svc = get_identity_service()
            try:
                sak = svc.authenticate_service_account_api_key(bearer)
            except IdentityServiceError as error:
                status = getattr(error, "status", 401) or 401
                if status == 503:
                    raise OpenAIAPIError(
                        503, "Identity service unavailable.", "unavailable",
                    ) from error
                raise OpenAIAPIError(
                    401, "Authentication required.", "unauthorized",
                ) from error
            header_tenant = (web.ctx.env.get("HTTP_X_TENANT_ID", "") or "").strip()
            tenant_id = header_tenant or sak["tenant_id"]
            if header_tenant and header_tenant != sak["tenant_id"]:
                raise OpenAIAPIError(
                    403, "Service account is not a member of this tenant.", "forbidden",
                )
            ctx = member_context(svc, sak["user_id"], tenant_id)
        else:
            if not _session_token():
                raise OpenAIAPIError(401, "Authentication required.", "unauthorized")
            ctx = _resolve_ctx_svc(_get_service(), require_tenant=True)
            svc = get_identity_service()
    except IdentityContextError as error:
        raise OpenAIAPIError(
            error.status, error.args[0] if error.args else "authentication failed",
            getattr(error, "code", "error"),
        ) from error
    # Functional chat.use gate (platform/tenant admins pass).
    if not (ctx.is_platform_admin or ctx.is_tenant_admin
            or "chat.use" in (ctx.permissions or ())):
        raise OpenAIAPIError(
            403, "You are not authorized to use the chat API.", "forbidden"
        )
    bound_ids = svc.tenant_agent_ids(ctx.tenant_id)
    header_agent = (web.ctx.env.get("HTTP_X_AGENT_ID", "") or "").strip()
    if header_agent:
        if header_agent not in bound_ids:
            raise OpenAIAPIError(
                403, "Agent is not bound to your tenant.", "forbidden"
            )
        agent_id = header_agent
    elif len(bound_ids) == 1:
        agent_id = bound_ids[0]
    elif not bound_ids:
        raise OpenAIAPIError(404, "No agent is bound to your tenant.", "no_agent")
    else:
        raise OpenAIAPIError(
            400,
            "Multiple agents are available; select one with X-Agent-ID.",
            "agent_ambiguous",
        )
    if not svc.check_resource_action(
        ctx.user_id, ctx.tenant_id, "agent", f"agent:{agent_id}", "use",
        permission="agent.use",
    ):
        raise OpenAIAPIError(
            403, "You are not authorized to use this agent.", "forbidden"
        )
    return to_runtime_identity(ctx), agent_id


def _scoped_run_chat(ident, agent_id: str) -> Callable:
    """Wrap the chat runner with the request's database runtime identity.

    The ambient ``RuntimeIdentity`` (user + tenant + web session) is applied for
    the duration of each run so downstream model.authorization and tenant path
    scoping read the API caller — never a pooled/worker ambient. The Agent is
    pinned to the tenant-bound ``agent_id`` (defaults resolved by the caller).
    """

    def _run(*args, **kwargs):
        from common.runtime_identity import use_identity

        kwargs["agent_id"] = agent_id
        with use_identity(ident):
            try:
                return _run_chat_service(*args, **kwargs)
            except RuntimeError as error:
                # The runtime model/agent authorization gate denies the caller.
                raise OpenAIAPIError(403, str(error), "forbidden") from error

    return _run


def _run_chat_service(*args, **kwargs):
    from agent.chat.service import ChatService
    from bridge.bridge import Bridge

    return ChatService(Bridge().get_agent_bridge()).run(*args, **kwargs)


def _error_body(error: OpenAIAPIError) -> str:
    return json.dumps(
        {
            "error": {
                "message": error.message,
                "type": (
                    "invalid_request_error" if error.status_code == 400 else "api_error"
                ),
                "code": error.code,
            }
        },
        ensure_ascii=False,
    )


class OpenAIChatCompletionsHandler:
    """web.py handler for ``POST /v1/chat/completions``."""

    def POST(self):
        import web

        from config import conf

        # Database identity mode shares the Web console identity stack (design
        # decision 11): DB session Bearer/cookie + X-Tenant-ID (+ optional
        # X-Agent-ID) authorize the call; the legacy bare external_api_token is
        # a legacy-mode-only contract.
        run_chat = _run_chat_service
        preauthenticated = False
        ident = None
        agent_id = None
        try:
            if _is_database_mode():
                ident, agent_id = _db_request_identity_and_agent()
                preauthenticated = True
                run_chat = _scoped_run_chat(ident, agent_id)

            raw_body = web.data()
            try:
                payload = json.loads(raw_body) if raw_body else {}
            except (TypeError, ValueError) as error:
                raise OpenAIAPIError(
                    400, "Request body must be valid JSON.", "invalid_json"
                ) from error

            if preauthenticated:
                from common.runtime_identity import use_identity
                with use_identity(ident):
                    result = handle_chat_completions(
                        payload,
                        authorization="",
                        external_api_token="",
                        run_chat=run_chat,
                        preauthenticated=True,
                        agent_id=agent_id,
                    )
            else:
                result = handle_chat_completions(
                    payload,
                    authorization=web.ctx.env.get("HTTP_AUTHORIZATION", ""),
                    external_api_token=conf().get("external_api_token", ""),
                    run_chat=_run_chat_service,
                )
        except OpenAIAPIError as error:
            statuses = {
                400: "400 Bad Request",
                401: "401 Unauthorized",
                403: "403 Forbidden",
                404: "404 Not Found",
                500: "500 Internal Server Error",
                503: "503 Service Unavailable",
            }
            raise web.HTTPError(
                statuses[error.status_code],
                {"Content-Type": "application/json; charset=utf-8"},
                _error_body(error),
            )

        if isinstance(result, dict):
            web.header("Content-Type", "application/json; charset=utf-8")
            return json.dumps(result, ensure_ascii=False)

        web.header("Content-Type", "text/event-stream; charset=utf-8")
        web.header("Cache-Control", "no-cache")
        web.header("X-Accel-Buffering", "no")
        return _encode_stream(result)
