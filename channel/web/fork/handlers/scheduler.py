"""Fork web layer (change adopt-upstream-web-split, design D2).

Fork-owned implementation, moved verbatim out of the former
channel/web/web_channel.py monolith. Upstream's api/ modules are not
edited. Imports inside function bodies are lazy so these modules can
reference each other without import cycles.
"""

from __future__ import annotations
from bridge.context import *
from common.log import logger
from typing import Any, Dict, List, Tuple, Optional, Iterator, NoReturn
import datetime
import json
import os
import web


def _scheduler_task_store(agent_id: str):
    """The one global task store, scoped to ``agent_id``.

    Upstream folded the per-Agent schedules into a single file whose tasks each
    carry the Agent they run as, so the console and the scheduler loop must read
    the same store: resolving a per-Agent path here would file console-created
    tasks where the loop never looks (and leave the console blind to tasks the
    loop created). ``AgentScopedTaskStore`` keeps the per-Agent call shape while
    the rows keep living in the one file.
    """
    from agent.tools.scheduler.integration import get_scoped_task_store

    return get_scoped_task_store(agent_id)


def _scheduler_run_store():
    """The run ledger's ``ConversationStore``, or a 503.

    Scheduled runs live in the same global ``runs`` table as every other unit of
    work, so the store is resolved from the registry's configured workspace --
    never from a client-supplied path. An unavailable store is a service fault
    (503), never an empty history: answering ``runs: []`` would tell the caller
    "you have no runs" when the truth is "the ledger could not be read".
    """
    from agent.memory import get_conversation_store
    from agent.tools.scheduler.run_repository import RUN_STORE_UNAVAILABLE
    from agent.tools.scheduler.authorization import TaskAuthorizationError

    try:
        store = get_conversation_store()
    except Exception as error:
        logger.error("[WebChannel] scheduler run store unavailable: %s",
                     type(error).__name__)
        raise TaskAuthorizationError(RUN_STORE_UNAVAILABLE, status=503) from error
    if store is None:
        raise TaskAuthorizationError(RUN_STORE_UNAVAILABLE, status=503)
    return store


def _scheduler_run_access(ctx):
    """The shared run-history authorization service for this request.

    ``actor_resolver`` is a *callable*, not an actor: the delete path re-resolves
    it inside its transaction, so a revocation that lands mid-request cannot
    delete the record. It rebuilds from the same verified session/tenant as the
    rest of the request.
    """
    from agent.tools.scheduler.run_access import RunAccessService
    from agent.tools.scheduler.run_repository import RunScopeRepository

    return RunAccessService(
        repository=RunScopeRepository(_scheduler_run_store()),
        task_access=_scheduler_access(ctx),
        actor_resolver=lambda: _scheduler_actor(ctx),
        agent_ids=_scheduler_agent_ids,
    )


def _scheduler_run_int_param(params, name: str, default, *, minimum: int,
                             maximum: Optional[int] = None) -> Optional[int]:
    """One bounded integer query parameter, or a 400.

    ``limit``/``offset``/``since`` reach SQL, so they are validated here rather
    than coerced: a negative offset, a non-numeric limit or a limit above the
    page ceiling is a malformed request, not something to silently clamp. A
    silent clamp would let a client believe it had paged through everything when
    the server had quietly reduced the page size.
    """
    raw = getattr(params, name, None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise _run_http_error("invalid_request", 400, "invalid %s" % name) from None
    if value < minimum or (maximum is not None and value > maximum):
        raise _run_http_error("invalid_request", 400, "invalid %s" % name)
    return value


def _run_http_error(code: str, status: int, message: str = ""):
    from channel.web.web_channel import _SCHEDULER_STATUS_LINES
    return web.HTTPError(
        _SCHEDULER_STATUS_LINES.get(status, "%d Error" % status),
        {"Content-Type": "application/json; charset=utf-8"},
        json.dumps({"status": "error", "code": code,
                    "message": message or code}, ensure_ascii=False))


def _scheduler_run_body(ctx, run: dict) -> Optional[str]:
    """The delivered text of one run, or ``None`` when it cannot be proven.

    The body lives in the receiver's session, which is a *different* resource
    from the run ledger, so it needs its own authorization: the caller must pass
    the exact durable session check (tenant, owner, storage Agent key, session
    id) **and** the messages must carry this run's id. Anything less -- a
    non-web session, an ambiguous match, a session the caller cannot read --
    degrades to the preview rather than guessing from timestamps or neighbours.
    """
    from channel.web.fork.authorization import _require_session_scope, _storage_agent_key
    from agent.registry import get_agent_registry

    session_id = str(run.get("session_id") or "").strip()
    agent_id = str(run.get("agent_id") or "").strip()
    run_id = str(run.get("run_id") or "").strip()
    if not session_id or not agent_id or not run_id:
        return None
    try:
        resolved = _require_session_scope(ctx, session_id, agent_id)
        profile = get_agent_registry().get(resolved)
    except Exception:
        # A refusal here is expected (a public run whose session is private);
        # the detail itself stays readable, only the body is withheld.
        return None
    try:
        store = _scheduler_run_store()
    except Exception:
        return None
    storage_key = _storage_agent_key(store)
    try:
        with store._lock:
            con = store._connect()
            try:
                rows = con.execute(
                    "SELECT content FROM messages"
                    " WHERE tenant_id=? AND owner=? AND session_id=?"
                    " AND agent_id=? AND run_id=? AND role='assistant'"
                    " ORDER BY seq",
                    (ctx.tenant_id, ctx.user_id, session_id, storage_key, run_id),
                ).fetchall()
            finally:
                con.close()
    except Exception as error:
        logger.debug("[WebChannel] scheduler run body read failed: %s",
                     type(error).__name__)
        return None
    if not rows:
        return None
    parts = []
    for row in rows:
        # The store's connections have no ``row_factory``: rows are plain
        # tuples, so the single selected column is index 0.
        text = _message_text(row[0])
        if text:
            parts.append(text)
    return "\n".join(parts) if parts else None


def _message_text(content) -> str:
    """Flatten one stored ``messages.content`` cell into plain text.

    ``content`` is JSON: either a string or a list of blocks. Only text blocks
    contribute, so a run's tool calls and images never leak into the transcript
    the console shows.
    """
    import json as _json
    try:
        parsed = _json.loads(content) if isinstance(content, str) else content
    except Exception:
        return ""
    if isinstance(parsed, str):
        return parsed
    if not isinstance(parsed, list):
        return ""
    out = []
    for block in parsed:
        if isinstance(block, str):
            out.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            out.append(str(block.get("text") or ""))
    return "".join(out)


def _scheduler_agent_ids(actor) -> List[str]:
    """Every Agent whose schedule the acting member may be shown.

    Authoritative for the aggregate view: the caller's tenant decides the list
    (``tenant_agent_ids``), and an Agent that the registry does not have enabled
    is not opened at all - so an Agent of another tenant is never read and then
    filtered.
    """
    from agent.registry import get_agent_registry
    from auth.service import get_identity_service

    enabled = {p.id for p in get_agent_registry().list(include_disabled=False)}
    bound = get_identity_service().tenant_agent_ids(actor.tenant_id)
    return [agent_id for agent_id in bound if agent_id in enabled]


def _scheduler_scope_resolver(actor, agent_id: str) -> Dict:
    """Binding + ``agent.use`` for one Agent, for the authorization service."""
    from auth.service import get_identity_service

    service = get_identity_service()
    scope: Dict = {"agent_id": agent_id}
    try:
        binding = service.get_agent_binding(agent_id)
        scope["bound_tenant"] = (binding or {}).get("tenant_id") or ""
    except Exception as error:
        logger.debug("[WebChannel] scheduler binding lookup %r: %s", agent_id, error)
    if actor.is_admin:
        scope["can_use"] = True
    else:
        try:
            scope["can_use"] = bool(service.check_resource_action(
                actor.user_id, actor.tenant_id, "agent", f"agent:{agent_id}",
                "use", permission="agent.use"))
        except Exception:
            scope["can_use"] = False
    return scope


def _scheduler_service_for_web(agent_id: str):
    """The scheduler runtime for one Agent, for the manual-run path."""
    from agent.tools.scheduler.integration import get_scheduler_service
    return get_scheduler_service(agent_id=agent_id)


def _scheduler_access(ctx=None) -> "TaskAccessService":
    """Build the shared task authorization service for this request.

    One construction for all five handlers, so the HTTP path cannot diverge from
    the tool and background paths (tasks 3.1/3.3).
    """
    from channel.web.web_channel import _web_auth_session_id
    from agent.tools.scheduler.authorization import (
        TaskActor, TaskAccessService, actor_from_identity_context,
    )

    if ctx is not None:
        actor = actor_from_identity_context(ctx, source="http")
    else:
        from channel.web.auth_handlers import _require_context
        actor = actor_from_identity_context(_require_context(require_tenant=True),
                                            source="http")
    actor.session_id = _web_auth_session_id()

    def quota_gate(actor_, agent_id, count):
        from auth.service import get_identity_service
        get_identity_service().check_scheduled_task_quota(
            user_id=actor_.user_id, tenant_id=actor_.tenant_id,
            would_be_count=count)

    return TaskAccessService(
        store_resolver=lambda actor_, agent_id: _scheduler_task_store(agent_id),
        agent_ids=_scheduler_agent_ids,
        scope_resolver=_scheduler_scope_resolver,
        quota_check=quota_gate,
        run_hook=lambda actor_, agent_id, task_id: _scheduler_run_hook(
            actor_, agent_id, task_id),
        coordinator="http",
    )


def _scheduler_run_hook(actor, agent_id: str, task_id: str) -> None:
    """Fire one task now through the scheduler runtime.

    The runtime is the same object the timer uses, so a manual run cannot take a
    path a scheduled fire would refuse; the authorization service has already
    re-checked the caller's own grants before calling this. A runtime that is not
    up is reported as ``run_unavailable`` (503), not as a silent success: the
    console must not tell a member their task ran when nothing was queued.
    """
    from channel.web.web_channel import _scheduler_service_for_web
    from agent.tools.scheduler.authorization import (
        RUN_UNAVAILABLE, TaskAuthorizationError,
    )

    service = _scheduler_service_for_web(agent_id)
    if service is None:
        raise TaskAuthorizationError(
            RUN_UNAVAILABLE, status=503, message="Scheduler service is not running")
    service.run_task_now(task_id)


def _scheduler_error(error):
    """The HTTP response for a refused management call.

    Raised as a ``web.HTTPError`` rather than returned with ``web.status`` set:
    the status has to survive the handler's own ``except`` clauses and the
    request's context teardown, and raising is the only form that does. ``code``
    is the stable machine value the console branches on; the status distinguishes
    "not yours" (403) from "no such task" (404) and "someone else edited it
    first" (409).
    """
    from channel.web.web_channel import _SCHEDULER_STATUS_LINES
    status = int(getattr(error, "status", 403) or 403)
    raise web.HTTPError(
        _SCHEDULER_STATUS_LINES.get(status, "%d Error" % status),
        {"Content-Type": "application/json; charset=utf-8"},
        json.dumps(error.payload(), ensure_ascii=False),
    )


class SchedulerHandler:
    def GET(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _int_param
        from channel.web.web_channel import _request_agent_id
        web.header('Content-Type', 'application/json; charset=utf-8')
        from agent.tools.scheduler.authorization import TaskAuthorizationError
        try:
            params = web.input(agent_id='', page='1', page_size='20')
            requested = _request_agent_id(params)
            with _db_scope() as ctx:
                service = _scheduler_access(ctx)
                page = service.list_tasks(
                    _scheduler_actor(ctx), agent_id=requested,
                    page=_int_param(params, "page", 1),
                    page_size=_int_param(params, "page_size", 20),
                )
            return json.dumps({"status": "success", **page}, ensure_ascii=False)
        except TaskAuthorizationError as error:
            raise _scheduler_error(error)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


def _scheduler_actor(ctx):
    from channel.web.web_channel import _web_auth_session_id
    from agent.tools.scheduler.authorization import actor_from_identity_context
    actor = actor_from_identity_context(ctx, source="http")
    actor.session_id = _web_auth_session_id()
    return actor


class SchedulerRunHandler:
    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _request_agent_id
        web.header('Content-Type', 'application/json; charset=utf-8')
        from agent.tools.scheduler.authorization import TaskAuthorizationError
        try:
            body = json.loads(web.data())
            agent_id = _request_agent_id(body)
            task_id = body.get("task_id")
            # The client's "same request" proof (task 3.4). A retry after a lost
            # response resends the same key and is answered from the accepted
            # outcome instead of queueing a second fire; a click without a key
            # keeps the older behaviour (refused while the task is running).
            run_key = str(body.get("run_key") or "").strip()
            if not task_id:
                raise web.HTTPError(
                    "400 Bad Request",
                    {"Content-Type": "application/json; charset=utf-8"},
                    json.dumps({"status": "error", "message": "task_id required"}))
            with _db_scope() as ctx:
                service = _scheduler_access(ctx)
                service.run_task(_scheduler_actor(ctx), agent_id, task_id,
                                 run_key=run_key or None)
            return json.dumps({
                "status": "success",
                "message": f"Task '{task_id}' queued for immediate execution",
            }, ensure_ascii=False)
        except TaskAuthorizationError as error:
            raise _scheduler_error(error)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler manual run error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerToggleHandler:
    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _request_agent_id
        web.header('Content-Type', 'application/json; charset=utf-8')
        from agent.tools.scheduler.authorization import TaskAuthorizationError
        try:
            body = json.loads(web.data())
            task_id = body.get("task_id")
            enabled = body.get("enabled", True)
            if not task_id:
                raise web.HTTPError(
                    "400 Bad Request",
                    {"Content-Type": "application/json; charset=utf-8"},
                    json.dumps({"status": "error", "message": "task_id required"}))
            with _db_scope() as ctx:
                service = _scheduler_access(ctx)
                task = service.set_enabled(_scheduler_actor(ctx),
                                          _request_agent_id(body), task_id,
                                          bool(enabled))
            return json.dumps({"status": "success", "task": task}, ensure_ascii=False)
        except TaskAuthorizationError as error:
            raise _scheduler_error(error)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler toggle error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerUpdateHandler:
    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _request_agent_id
        web.header('Content-Type', 'application/json; charset=utf-8')
        from agent.tools.scheduler.authorization import (
            FORGED_FIELD, TaskAuthorizationError,
        )
        try:
            body = json.loads(web.data())
            task_id = body.get("task_id")
            if not task_id:
                raise web.HTTPError(
                    "400 Bad Request",
                    {"Content-Type": "application/json; charset=utf-8"},
                    json.dumps({"status": "error", "message": "task_id required"}))

            with _db_scope() as ctx:
                service = _scheduler_access(ctx)
                actor = _scheduler_actor(ctx)
                agent_id = _request_agent_id(body)
                original = service.get_task(actor, agent_id, task_id)
                # Every other body key is handed to the service as a patch, so a
                # field the caller may not set (``owner``, ``scope``,
                # ``agent_id``, ...) is refused by name (400) instead of being
                # quietly dropped here — a silent drop would let a caller believe
                # a rename happened when the field they really wanted was ignored.
                patch = {k: v for k, v in body.items()
                         if k not in ("task_id", "agent_id", "revision")}
                if "schedule" in patch:
                    # Recompute next_run_at for the merged task, and refuse a
                    # schedule that cannot produce one (an unparseable cron, or a
                    # one-off time already past) — the same check the editor has
                    # always relied on, now before the write rather than after.
                    from agent.tools.scheduler.scheduler_service import SchedulerService
                    from agent.tools.scheduler.task_store import TaskStore
                    merged = dict(original)
                    merged.update(patch)
                    if "action" in patch and isinstance(patch["action"], dict):
                        action = dict(original.get("action") or {})
                        action.update(patch["action"])
                        merged["action"] = action
                    dry = SchedulerService(TaskStore(os.devnull), lambda t: None)
                    next_run = dry._calculate_next_run(merged, datetime.now())
                    if not next_run:
                        raise web.HTTPError(
                            "400 Bad Request",
                            {"Content-Type": "application/json; charset=utf-8"},
                            json.dumps({
                                "status": "error",
                                "message": "Cannot calculate next run time. Please check the schedule config (e.g., cron expression format, or whether the one-time task time has already passed).",
                            }, ensure_ascii=False))
                    patch["next_run_at"] = next_run.isoformat()
                elif "action" in patch and not original.get("next_run_at"):
                    from agent.tools.scheduler.scheduler_service import SchedulerService
                    from agent.tools.scheduler.task_store import TaskStore
                    merged = dict(original)
                    merged.update(patch)
                    dry = SchedulerService(TaskStore(os.devnull), lambda t: None)
                    next_run = dry._calculate_next_run(merged, datetime.now())
                    if next_run:
                        patch["next_run_at"] = next_run.isoformat()

                revision = body.get("revision")
                task = service.update_task(
                    actor, agent_id, task_id, patch,
                    expected_revision=int(revision) if revision is not None else None)
            return json.dumps({"status": "success", "task": task}, ensure_ascii=False)
        except TaskAuthorizationError as error:
            raise _scheduler_error(error)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler update error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerDeleteHandler:
    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _request_agent_id
        web.header('Content-Type', 'application/json; charset=utf-8')
        from agent.tools.scheduler.authorization import TaskAuthorizationError
        try:
            body = json.loads(web.data())
            task_id = body.get("task_id")
            if not task_id:
                raise web.HTTPError(
                    "400 Bad Request",
                    {"Content-Type": "application/json; charset=utf-8"},
                    json.dumps({"status": "error", "message": "task_id required"}))
            with _db_scope() as ctx:
                service = _scheduler_access(ctx)
                service.delete_task(_scheduler_actor(ctx),
                                    _request_agent_id(body), task_id)
            return json.dumps({"status": "success"}, ensure_ascii=False)
        except TaskAuthorizationError as error:
            raise _scheduler_error(error)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler delete error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


# =============================================================================
# Task authoring: trusted instances, recipients and creation (change
# integrate-upstream-core-capabilities, P4)
# =============================================================================
#
# The three handlers below add the console's task-authoring surface (pick an
# instance, pick a trusted recipient, create a personal task) on top of the same
# authorization service the five management verbs use. They resolve *what* the
# caller may deliver through and *who* is a legal receiver server-side; the
# browser never receives the global channel list or the global contact
# directory to filter. See ``channel/web/fork/scheduler_targets.py`` for the
# resolution itself; the handlers only parse, scope, call and serialize.


#: Top-level create fields a caller may never set. Ownership, tenant, revision,
#: the task id and the scope are all derived from the verified actor, so
#: accepting one would let a caller forge them. ``agent_id`` is deliberately
#: absent here: it is accepted only when it agrees with the instance's resolved
#: binding, which the handler checks *after* resolution.
_PROTECTED_CREATE_FIELDS = frozenset({
    "owner", "tenant_id", "user_id", "revision", "write_coordinator",
    "scope", "quarantine", "path", "store_path", "id", "task_id", "public",
})

#: The action types the console may create. Anything else would be a task the
#: scheduler loop could not execute.
_CREATE_ACTION_TYPES = ("send_message", "agent_task")


def _scheduler_target_service():
    """The shared instance/recipient resolver for the three authoring endpoints."""
    from auth.service import get_identity_service
    from agent.tools.scheduler.integration import get_recipient_store
    from channel.web.fork.scheduler_targets import SchedulerTargetService

    return SchedulerTargetService(get_identity_service(), get_recipient_store())


def _create_refuse(code: str, status: int) -> None:
    """Raise a stable business refusal, answered by ``_scheduler_error``."""
    from agent.tools.scheduler.authorization import TaskAuthorizationError
    raise TaskAuthorizationError(code, status=status)


def _scheduler_refusal(error):
    """Translate an identity-layer refusal into the scheduler's stable shape.

    ``list_tenant_channel_instances`` refuses a caller with no active membership
    rather than answering an empty list, so that refusal reaches these handlers as
    an ``IdentityServiceError``. Re-emitting it as the scheduler refusal keeps the
    status and the machine ``code``, instead of letting the broad handler turn an
    authorization gap into a 500.
    """
    from agent.tools.scheduler.authorization import TaskAuthorizationError
    return TaskAuthorizationError(
        str(getattr(error, "code", "") or "forbidden"),
        status=int(getattr(error, "status", 403) or 403))


def _internal_error():
    """The 500 for an unexpected failure, with no internal detail in the body.

    A traceback, a filesystem path or a database error would tell an
    unauthenticated-ish caller how the deployment is laid out; the client only
    needs to know the server failed.
    """
    raise web.HTTPError(
        "500 Internal Server Error",
        {"Content-Type": "application/json; charset=utf-8"},
        json.dumps({"status": "error", "code": "internal_error",
                    "message": "Internal server error"}, ensure_ascii=False))


def _create_payload() -> dict:
    raw = web.data()
    try:
        body = json.loads(raw) if raw else {}
    except Exception:
        body = None
    if not isinstance(body, dict):
        _create_refuse("invalid_request", 400)
    return body


def _create_name(body: dict) -> str:
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        _create_refuse("invalid_request", 400)
    return name.strip()


def _create_enabled(body: dict) -> bool:
    # Absent keeps the console's historical default (enabled); a present value
    # must be a real bool so ``"false"`` cannot be read as truthy.
    if "enabled" in body and not isinstance(body["enabled"], bool):
        _create_refuse("invalid_request", 400)
    return bool(body.get("enabled", True))


def _create_schedule(body: dict) -> dict:
    schedule = body.get("schedule")
    if not isinstance(schedule, dict) or not str(schedule.get("type") or "").strip():
        _create_refuse("invalid_request", 400)
    return schedule


def _create_action_in(body: dict) -> dict:
    action = body.get("action")
    if not isinstance(action, dict) or action.get("type") not in _CREATE_ACTION_TYPES:
        _create_refuse("invalid_request", 400)
    return action


def _create_whitelisted_action(action_in: dict, recipient: dict) -> dict:
    """The stored action, assembled only from trusted values.

    The receiver identity (channel type, instance, receiver, group flag,
    notify session) is taken from the directory, never from the request; the
    client may only contribute the text that belongs to the chosen type, and the
    other type's field is dropped so a task cannot carry both.
    """
    action_type = action_in["type"]
    action = {
        "type": action_type,
        "receiver": recipient["receiver"],
        "receiver_name": recipient.get("name") or recipient["receiver"],
        "is_group": bool(recipient.get("is_group")),
        "channel_type": recipient["channel_type"],
        "instance_id": recipient["instance_id"],
        "notify_session_id": recipient.get("session_id") or recipient["receiver"],
    }
    if action_type == "send_message":
        content = str(action_in.get("content") or "").strip()
        if not content:
            _create_refuse("invalid_request", 400)
        action["content"] = content
    else:
        description = str(action_in.get("task_description") or "").strip()
        if not description:
            _create_refuse("invalid_request", 400)
        action["task_description"] = description
        if action_in.get("silent"):
            action["silent"] = True
    return action


def _create_task_data(name: str, enabled: bool, schedule: dict, action: dict,
                      agent_id: str) -> dict:
    """Build the personal task, with the existing schedule arithmetic.

    ``next_run_at`` comes from ``SchedulerService._calculate_next_run`` -- the
    same call every other entry point uses -- so a schedule this console accepts
    cannot be one the loop would refuse, and an empty/past result is
    ``invalid_schedule`` rather than a stored task that never fires. Owner,
    revision and write coordinator are deliberately not set here; the
    authorization service stamps them.
    """
    import uuid
    from datetime import datetime
    from agent.tools.scheduler.scheduler_service import SchedulerService
    from agent.tools.scheduler.task_store import TaskStore

    now = datetime.now()
    task_data = {
        "id": uuid.uuid4().hex,
        "name": name,
        "agent_id": agent_id,
        "enabled": enabled,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "schedule": schedule,
        "action": action,
    }
    dry = SchedulerService(TaskStore(os.devnull), lambda task: None)
    next_run = dry._calculate_next_run(task_data, now)
    if not next_run:
        _create_refuse("invalid_schedule", 400)
    task_data["next_run_at"] = next_run.isoformat()
    return task_data


def _create_task_for(ctx, body: dict) -> dict:
    """The fixed creation order (implementation.md §6, steps 1-6).

    Parse/type-check -> scope -> reject forged fields -> resolve the trusted
    target -> whitelist the action -> validate the schedule -> derive the id and
    personal task -> the one authorized write. ``resolve_target`` runs *before*
    the action is assembled, so a target the caller cannot use never reaches a
    task, and ``create_task`` is the only thing that writes.
    """
    actor = _scheduler_actor(ctx)
    if _PROTECTED_CREATE_FIELDS & set(body):
        _create_refuse("invalid_request", 400)
    name = _create_name(body)
    enabled = _create_enabled(body)
    schedule = _create_schedule(body)
    action_in = _create_action_in(body)

    instance_id = str(action_in.get("instance_id") or "").strip()
    receiver = str(action_in.get("receiver") or "").strip()
    if not instance_id or not receiver:
        # ``instance_id`` is mandatory and never falls back to a channel type or
        # the default Agent: a missing one is a malformed request, not a target.
        _create_refuse("invalid_request", 400)

    agent_id, recipient = _scheduler_target_service().resolve_target(
        ctx, instance_id, receiver)

    supplied_agent = body.get("agent_id")
    if supplied_agent is not None and not isinstance(supplied_agent, str):
        _create_refuse("invalid_request", 400)
    if isinstance(supplied_agent, str) and supplied_agent.strip() \
            and supplied_agent.strip() != agent_id:
        _create_refuse("invalid_request", 400)
    supplied_channel = str(action_in.get("channel_type") or "").strip()
    if supplied_channel and supplied_channel != recipient["channel_type"]:
        _create_refuse("invalid_target", 400)

    action = _create_whitelisted_action(action_in, recipient)
    task_data = _create_task_data(name, enabled, schedule, action, agent_id)
    return _scheduler_access(ctx).create_task(actor, agent_id, task_data)


class SchedulerInstancesHandler:
    """GET /api/scheduler/instances -- delivery targets the caller may use."""

    def GET(self):
        from channel.web.web_channel import _db_scope
        from auth.service import IdentityServiceError
        web.header('Content-Type', 'application/json; charset=utf-8')
        from agent.tools.scheduler.authorization import TaskAuthorizationError
        try:
            with _db_scope() as ctx:
                instances = _scheduler_target_service().list_instances(ctx)
            return json.dumps({"status": "success", "instances": instances},
                              ensure_ascii=False)
        except web.HTTPError:
            raise
        except TaskAuthorizationError as error:
            raise _scheduler_error(error)
        except IdentityServiceError as error:
            raise _scheduler_error(_scheduler_refusal(error))
        except Exception as error:
            logger.error("[WebChannel] Scheduler instances error: %s",
                         type(error).__name__)
            _internal_error()


class SchedulerRecipientsHandler:
    """GET /api/scheduler/recipients -- trusted receivers on those instances."""

    def GET(self):
        from channel.web.web_channel import _db_scope
        from auth.service import IdentityServiceError
        web.header('Content-Type', 'application/json; charset=utf-8')
        from agent.tools.scheduler.authorization import TaskAuthorizationError
        try:
            params = web.input(instance_id='')
            requested = str(getattr(params, "instance_id", "") or "").strip()
            with _db_scope() as ctx:
                recipients = _scheduler_target_service().list_recipients(
                    ctx, requested or None)
            return json.dumps({"status": "success", "recipients": recipients},
                              ensure_ascii=False)
        except web.HTTPError:
            raise
        except TaskAuthorizationError as error:
            raise _scheduler_error(error)
        except IdentityServiceError as error:
            raise _scheduler_error(_scheduler_refusal(error))
        except Exception as error:
            logger.error("[WebChannel] Scheduler recipients error: %s",
                         type(error).__name__)
            _internal_error()


class SchedulerCreateHandler:
    """POST /api/scheduler/create -- a personal task for a trusted target."""

    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.auth_handlers import require_management_write
        from auth.service import IdentityServiceError
        web.header('Content-Type', 'application/json; charset=utf-8')
        from agent.tools.scheduler.authorization import TaskAuthorizationError
        try:
            # The console's existing origin/CSRF protection for writes (and the
            # bearer exemption for the desktop client) runs before anything is
            # parsed, so a cross-site request never reaches the target resolver.
            require_management_write()
            body = _create_payload()
            with _db_scope() as ctx:
                task = _create_task_for(ctx, body)
            return json.dumps({"status": "success", "task": task},
                              ensure_ascii=False)
        except web.HTTPError:
            # Re-raised before the broad handler: a 403/404/409 must keep its
            # status, not be wrapped into a 200 carrying an error body.
            raise
        except TaskAuthorizationError as error:
            raise _scheduler_error(error)
        except IdentityServiceError as error:
            raise _scheduler_error(_scheduler_refusal(error))
        except Exception as error:
            logger.error("[WebChannel] Scheduler create error: %s",
                         type(error).__name__)
            _internal_error()


class SchedulerRunsHandler:
    """GET /api/scheduler/runs -- one page of the caller's run history.

    ``history_scope`` is always ``attributed_only``: the page reports that it
    shows only runs whose ownership was captured at execution time. It is not a
    filter the client may relax -- the SQL is already limited to the caller's
    authorisation, and paging happens inside that limit, never before it.
    """

    def GET(self):
        from channel.web.web_channel import _db_scope
        from agent.tools.scheduler.run_repository import RunQuery
        from agent.tools.scheduler.authorization import TaskAuthorizationError
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(agent_id='', task_id='', limit='', offset='', since='')
            query = RunQuery(
                # An empty agent_id is the *aggregate* of what the caller may
                # see -- never a global, unscoped read and never the default
                # Agent only.
                agent_id=str(getattr(params, "agent_id", "") or "").strip(),
                task_id=str(getattr(params, "task_id", "") or "").strip(),
                since=_scheduler_run_since(params),
                limit=_scheduler_run_int_param(params, "limit", 100, minimum=1,
                                               maximum=500),
                offset=_scheduler_run_int_param(params, "offset", 0, minimum=0),
            )
            with _db_scope() as ctx:
                body = _scheduler_run_access(ctx).list_runs(query)
            return json.dumps(body, ensure_ascii=False)
        except web.HTTPError:
            raise
        except TaskAuthorizationError as error:
            raise _scheduler_error(error)
        except Exception as error:
            logger.error("[WebChannel] Scheduler runs list error: %s",
                         type(error).__name__)
            _internal_error()


class SchedulerRunDetailHandler:
    """GET /api/scheduler/runs/detail -- one authorised run.

    The ledger grants the *preview*; the session body is filled in only after a
    second, independent check, so a caller who may see a public run still cannot
    read a private conversation it delivered into.
    """

    def GET(self):
        from channel.web.web_channel import _db_scope
        from agent.tools.scheduler.authorization import TaskAuthorizationError
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(run_id='')
            run_id = str(getattr(params, "run_id", "") or "").strip()
            if not run_id:
                raise _run_http_error("invalid_request", 400, "run_id required")
            with _db_scope() as ctx:
                run = _scheduler_run_access(ctx).get_run(run_id)
                run["full_output"] = _scheduler_run_body(ctx, run)
            return json.dumps({"status": "success", "run": run},
                              ensure_ascii=False)
        except web.HTTPError:
            raise
        except TaskAuthorizationError as error:
            raise _scheduler_error(error)
        except Exception as error:
            logger.error("[WebChannel] Scheduler run detail error: %s",
                         type(error).__name__)
            _internal_error()


class SchedulerRunDeleteHandler:
    """POST /api/scheduler/runs/delete -- remove one authorised run record.

    Deletes the ledger row and its ownership metadata in one transaction. The
    delivered messages are a different resource and are deliberately left alone:
    a history cleanup must not retract what was already said.
    """

    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.auth_handlers import require_management_write
        from agent.tools.scheduler.authorization import TaskAuthorizationError
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            # Same write protection as every other scheduler mutation; the
            # desktop bearer path keeps its exemption inside the helper.
            require_management_write()
            body = _create_payload()
            run_id = str(body.get("run_id") or "").strip()
            if not run_id:
                raise _run_http_error("invalid_request", 400, "run_id required")
            with _db_scope() as ctx:
                _scheduler_run_access(ctx).delete_run(run_id)
            return json.dumps({"status": "success"}, ensure_ascii=False)
        except web.HTTPError:
            raise
        except TaskAuthorizationError as error:
            raise _scheduler_error(error)
        except Exception as error:
            logger.error("[WebChannel] Scheduler run delete error: %s",
                         type(error).__name__)
            _internal_error()


def _scheduler_run_since(params) -> Optional[int]:
    """``since`` as a non-negative Unix second, or a 400.

    The client dedupes by ``run_id`` on top of this, so a coarse boundary is
    safe; a malformed one is not -- it would silently widen or empty the page.
    """
    raw = getattr(params, "since", None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    return _scheduler_run_int_param(params, "since", None, minimum=0)


