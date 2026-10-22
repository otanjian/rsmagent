"""Fork web layer (change adopt-upstream-web-split, design D2).

Fork-owned implementation, moved verbatim out of the former
channel/web/web_channel.py monolith. Upstream's api/ modules are not
edited. Imports inside function bodies are lazy so these modules can
reference each other without import cycles.
"""

from __future__ import annotations
from agent.permission import (
    MODES as PERMISSION_MODES,
    global_mode as permission_global_mode,
    normalize_mode as permission_normalize_mode,
)
from bridge.context import *
from common.log import logger
import json
import time
from typing import NoReturn, Optional
import web


def _conversation_store_for(agent_id: Optional[str]):
    """Open the conversation store of the addressed Agent.

    Conversations live one database per Agent workspace
    (``<workspace>/memory/long-term/index.db``): the history list merges those
    workspaces (``_list_sessions_across_agents``) and the ownership check reads
    the addressed Agent's workspace (``_require_owned_session``), so every
    read/write addressed by session id has to open the *same* file.

    ``_get_workspace_root`` must NOT be used for this. In database mode it
    resolves the caller's tenant *shared* root (that is its job: the file
    panel, preview and uploads are tenant-scoped), so a session write would
    land in a different database than the list it came from and answer
    ``session not found`` for a session the user can plainly see.
    """
    from agent.memory import get_conversation_store
    from agent.registry import get_agent_registry

    return get_conversation_store(get_agent_registry().get(agent_id or None).workspace)


def _enterprise_interview_enabled() -> bool:
    """The single switch for the cross-owner read and the report entry."""
    try:
        from config import conf

        return bool(conf().get("enterprise_interview_management_enabled", False))
    except Exception:  # noqa: BLE001 - a config read must not break history
        return False


def _audit_history_read(ctx, agent_id: str, scope: str,
                        session_id: Optional[str]) -> None:
    """Record one cross-owner read, redacted, without letting audit fail it.

    ``audit-log`` owns the event/action vocabulary; this is the recorded action
    named by the change (``session.history.read_all``). The payload keeps the
    scope and ids only — never the question/answer text.
    """
    if ctx is None or not getattr(ctx, "tenant_id", None):
        return
    try:
        from auth.service import get_identity_service

        get_identity_service().record_audit(
            action="session.history.read_all",
            target=f"agent:{agent_id}",
            actor_user_id=ctx.user_id,
            actor_username=getattr(ctx, "username", "") or "",
            tenant_id=ctx.tenant_id,
            redacted_changes={"scope": scope, "session_id": session_id or ""},
        )
    except Exception as e:  # noqa: BLE001 - best-effort per the audit contract
        logger.warning(f"[WebChannel] history read audit skipped: {e}")


def _coding_service_for(agent_id: Optional[str]):
    """The coding service over the addressed Agent's store.

    Imported lazily like every other cross-module dependency in this package, so
    a deployment with the capability absent pays nothing for importing it.
    """
    from channel.web.fork.handlers.coding import _coding_service

    return _coding_service(_conversation_store_for(agent_id))


def _coding_link(agent_id: Optional[str], session_id: str):
    """The link when this session is a coding one, else None.

    A *probe*, not a decision: by the time this is called the caller's ownership
    and the tenant binding are already established, so the only question left is
    which of the two session kinds this row is. It therefore never raises — a
    rename of an ordinary session must not start failing because the coding
    capability is misconfigured or the store was built without the table.
    """
    try:
        return _coding_service_for(agent_id).link_for(session_id)
    except Exception:  # noqa: BLE001 - a probe must not fail the request
        return None


def _agent_is_coding(agent_id: Optional[str]) -> bool:
    """Whether the addressed Agent's type is ``coding``. Absent/unknown: no."""
    try:
        from agent.registry import get_agent_registry

        profile = get_agent_registry().get(agent_id or None, require_enabled=False)
    except Exception:  # noqa: BLE001 - a type read must not fail a delete
        return False
    return bool(getattr(profile, "is_coding", False))


def _coding_link_for_delete(agent_id: Optional[str], session_id: str):
    """The coding link for a delete, or None for an ordinary/absent session.

    Unlike :func:`_coding_link` this must not fail open. A store read error
    raises a distinguishable refusal instead of looking like "not a coding
    session", and a coding Agent's *surviving* record without a link is refused
    rather than cleaned up as an ordinary conversation — the two are different
    facts, and only the latter (a repeated delete of an already-removed session)
    keeps the idempotent ordinary path.
    """
    service = _coding_service_for(agent_id)
    try:
        link = service.link_for_delete(session_id)
    except Exception as error:  # noqa: BLE001 - report, never downgrade
        from agent.coding import CODING_LINK_UNAVAILABLE
        from channel.web.fork.handlers.coding import _coding_error

        logger.error(f"[WebChannel] Coding link read failed: {error}")
        _coding_error("could not read the coding session link",
                      "500 Internal Server Error", CODING_LINK_UNAVAILABLE)
    if link is not None:
        return link
    if _agent_is_coding(agent_id) and service.store.session_exists(session_id):
        from agent.coding import CODING_NOT_LINKED
        from channel.web.fork.handlers.coding import _coding_error

        _coding_error("the coding session's link is missing",
                      "409 Conflict", CODING_NOT_LINKED)
    return None


def _coding_refusal(error) -> NoReturn:
    """Report a service refusal with its own status and stable code."""
    from channel.web.fork.handlers.coding import _coding_error, _reason

    _coding_error(error.message, f"{error.status} {_reason(error.status)}", error.code)


def _coding_unsupported(message: str) -> NoReturn:
    """Refuse an operation a coding session has no platform-side meaning for.

    Clearing context and deleting an individual message are operations on the
    platform's own mirror of a conversation. A coding conversation has no such
    mirror — OpenCode holds every turn — so the honest answer is a refusal, not
    a success that changed nothing.
    """
    from agent.coding import CODING_WEB_ONLY
    from channel.web.fork.handlers.coding import _coding_error

    _coding_error(message, "400 Bad Request", CODING_WEB_ONLY)


def _coding_write(agent_id: Optional[str], action, *args, **kwargs):
    """Run one coding management action, mapping a refusal to its HTTP answer."""
    from agent.coding import CodingError

    try:
        return action(*args, **kwargs)
    except CodingError as error:
        _coding_refusal(error)

def _forget_session_side_stores(session_id: str) -> None:
    """Drop the per-session side rows a deleted conversation leaves behind.

    A stale project binding would keep inflating the "how many spaces are in
    use" count that decides how the session list is grouped.
    """
    try:
        from agent.workspace import project_store, session_prefs

        project_store.forget_session(session_id)
        session_prefs.forget_session(session_id)
    except Exception as e:  # noqa: BLE001 - best-effort cleanup
        logger.debug(f"[WebChannel] Session side-store cleanup skipped: {e}")


class SessionsHandler:
    def GET(self):
        started = time.perf_counter()
        from channel.web.fork.runtime import _agent_badge
        from channel.web.fork.runtime import _annotate_coding_sessions
        from channel.web.fork.runtime import _annotate_sessions_with_projects
        from channel.web.web_channel import _db_scope
        from channel.web.fork.runtime import _list_sessions_across_agents
        from channel.web.fork.runtime import _request_agent_id
        from channel.web.web_channel import _require_read_permission
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'private, no-store')
        try:
            with _db_scope() as ctx:
                _require_read_permission(ctx, "history.read")
                params = web.input(
                    page='1', page_size='50', agent_id='', agent='', scope='', q='',
                    archived='', read_scope=''
                )
                from agent.memory.conversation_store import normalize_session_search_query
                try:
                    q = normalize_session_search_query(params.q)
                except ValueError as e:
                    web.ctx.status = '400 Bad Request'
                    return json.dumps({"status": "error", "code": "invalid_query",
                                       "message": str(e)}, ensure_ascii=False)
                page = int(params.page)
                page_size = int(params.page_size)
                archived = str(params.archived or '').strip().lower() in ('1', 'true', 'yes')
                read_scope = str(params.read_scope or '').strip()
                if read_scope == 'agent_all':
                    if not _enterprise_interview_enabled():
                        raise web.HTTPError(
                            "403 Forbidden",
                            {"Content-Type": "application/json"},
                            json.dumps({"status": "error", "code": "feature_disabled",
                                        "message": "feature disabled"}))
                    from channel.web.fork.authorization import _require_history_read_all
                    from auth.service import get_identity_service
                    from agent.registry import get_agent_registry
                    target = _require_history_read_all(
                        ctx, str(params.agent_id or '').strip())
                    store = _conversation_store_for(target)
                    result = store.list_sessions(
                        channel_type="web", page=page, page_size=page_size,
                        all_owners=True, archived=archived,
                        **({"q": q} if q else {}))
                    result.setdefault("group_mode", "time")
                    owners = {s.get("owner") for s in result.get("sessions") or []
                              if s.get("owner")}
                    names = (get_identity_service().member_display_names(
                        ctx.tenant_id, owners) if ctx else {})
                    badge = _agent_badge(
                        get_agent_registry().get(target or None, require_enabled=False))
                    for session in result.get("sessions") or []:
                        session["creator"] = names.get(session.get("owner") or "", "")
                        session["agent"] = badge
                        session["read_only"] = True
                    if q:
                        result["query"] = q
                    _audit_history_read(ctx, target, "list", None)
                    return json.dumps({"status": "success", **result},
                                      ensure_ascii=False)

                if (params.scope or '').strip() == 'all':
                    if q:
                        result = _list_sessions_across_agents(page, page_size, ctx, q=q,
                                                              archived=archived)
                    elif ctx is not None:
                        result = _list_sessions_across_agents(page, page_size, ctx,
                                                              archived=archived)
                    else:
                        result = _list_sessions_across_agents(page, page_size,
                                                              archived=archived)
                    if q:
                        result["query"] = q
                    return json.dumps({"status": "success", **result}, ensure_ascii=False)

                agent_id = _request_agent_id(params)
                from agent.registry import get_agent_registry
                store = _conversation_store_for(agent_id)
                search_args = {"q": q} if q else {}
                result = store.list_sessions(
                    channel_type="web",
                    page=page,
                    page_size=page_size,
                    user_id=ctx.user_id if ctx else None,
                    archived=archived,
                    **search_args,
                )
                _annotate_sessions_with_projects(
                    store, result, agent_id, user_id=ctx.user_id if ctx else None,
                )
                _annotate_coding_sessions(store, result, agent_id)
                badge = _agent_badge(
                    get_agent_registry().get(agent_id or None, require_enabled=False)
                )
                for session in result.get("sessions") or []:
                    session["agent"] = badge
                if q:
                    result["query"] = q
                return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Sessions API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            web.header('Server-Timing', f'history;dur={elapsed_ms:.1f}')
            if elapsed_ms >= 500:
                logger.info(f"[WebChannel] Slow history list: {elapsed_ms:.1f}ms")



class SessionReadTargetsHandler:
    """The Agents a caller may read across owners, for the history selector.

    Answers the same set the read gate accepts — explicit ``history_read_all``
    grants on tenant-bound, shared, non-coding Agents — so the selector can
    never offer a target the list/detail endpoint would refuse.
    """

    def GET(self):
        from channel.web.web_channel import _db_scope
        from channel.web.fork.runtime import _agent_badge
        from channel.web.fork.authorization import _history_read_all_target_ids
        from agent.registry import get_agent_registry
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'private, no-store')
        try:
            with _db_scope() as ctx:
                if not _enterprise_interview_enabled():
                    raise web.HTTPError(
                        "403 Forbidden",
                        {"Content-Type": "application/json"},
                        json.dumps({"status": "error", "code": "feature_disabled",
                                    "message": "feature disabled"}))
                registry = get_agent_registry()
                targets = []
                for agent_id in _history_read_all_target_ids(ctx):
                    badge = _agent_badge(
                        registry.get(agent_id, require_enabled=False))
                    targets.append({"agent_id": agent_id,
                                    "name": (badge or {}).get("name") or agent_id})
                return json.dumps({"status": "success", "targets": targets},
                                  ensure_ascii=False)
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] history read targets error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

class SessionDetailHandler:
    def DELETE(self, session_id: str):
        from channel.web.web_channel import WebChannel
        from channel.web.web_channel import _db_scope
        from channel.web.fork.runtime import _request_agent_id
        from channel.web.web_channel import _require_read_permission
        from channel.web.web_channel import _require_session_scope
        web.header('Content-Type', 'application/json; charset=utf-8')
        logger.info(f"[WebChannel] DELETE session request: {session_id}")
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})
            params = web.input(agent_id='')
            with _db_scope() as ctx:
                _require_read_permission(ctx, "history.read")
                agent_id = _require_session_scope(
                    ctx, session_id, _request_agent_id(params))

                # A coding session is deleted through the service before
                # anything local changes: interrupt first if it is running, and
                # keep the record when the service refuses, so the console never
                # shows a conversation the service still holds. The runtime
                # teardown below is skipped deliberately — a coding session has
                # no platform runtime, queue or cancellation key to clear.
                #
                # The strict read is deliberate: a store failure or a surviving
                # coding record with no link must refuse here rather than fall
                # through to the ordinary cleanup below.
                link = _coding_link_for_delete(agent_id, session_id)
                if link is not None:
                    service = _coding_service_for(agent_id)
                    _coding_write(agent_id, service.delete, session_id=session_id)
                    _forget_session_side_stores(session_id)
                    logger.info(f"[WebChannel] Coding session deleted: {session_id}")
                    return json.dumps({"status": "success"})

                # Stop any in-flight run first: a reply that lands after the delete
                # would otherwise keep burning tokens for a session nobody can see.
                try:
                    from agent.protocol import get_cancel_registry
                    from bridge.bridge import Bridge
                    scoped = Bridge().get_agent_bridge().scoped_session_key(session_id, agent_id)
                    cancelled = get_cancel_registry().cancel_session(scoped)
                    if cancelled:
                        logger.info(
                            f"[WebChannel] Cancelled {cancelled} in-flight request(s) "
                            f"for deleted session {session_id}"
                        )
                except Exception as e:
                    logger.warning(f"[WebChannel] Cancel on delete failed: {e}")

                from agent.memory import get_conversation_store
                store = _conversation_store_for(agent_id)
                store.clear_session(session_id)

                # Drop the session's side stores too. Left behind, a stale project
                # binding would keep inflating the "how many spaces are in use"
                # count that decides how the session list is grouped.
                try:
                    from agent.workspace import project_store, session_prefs
                    project_store.forget_session(session_id)
                    session_prefs.forget_session(session_id)
                except Exception as e:
                    logger.debug(f"[WebChannel] Session side-store cleanup skipped: {e}")

                # Also remove the Agent instance from AgentBridge if exists
                try:
                    from bridge.bridge import Bridge
                    ab = Bridge().get_agent_bridge()
                    ab.clear_session(session_id, agent_id=agent_id)
                except Exception:
                    pass

                channel = WebChannel()
                # Drop messages still waiting in the channel queue: processing them
                # after the delete would recreate the session from scratch.
                try:
                    channel.cancel_session(session_id, agent_id=agent_id)
                except Exception as e:
                    logger.warning(f"[WebChannel] Failed to drain queue on delete: {e}")
                channel.session_queues.pop(
                    channel._session_queue_key(session_id, agent_id), None
                )

                logger.info(f"[WebChannel] Session deleted: {session_id}")
                return json.dumps({"status": "success"})
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Session delete error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def PUT(self, session_id: str):
        """Update a session's title, pinned flag and/or archived flag."""
        from channel.web.web_channel import _db_scope
        from channel.web.fork.runtime import _request_agent_id
        from channel.web.web_channel import _require_read_permission
        from channel.web.web_channel import _require_session_scope
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})
            body = json.loads(web.data())
            title = (body.get("title") or "").strip()
            pinned = body.get("pinned")
            archived = body.get("archived")
            if not title and pinned is None and archived is None:
                return json.dumps({"status": "error",
                                   "message": "title, pinned or archived required"})

            with _db_scope() as ctx:
                _require_read_permission(ctx, "history.read")
                agent_id = _require_session_scope(
                    ctx, session_id, _request_agent_id(body))

                from agent.memory import get_conversation_store
                store = _conversation_store_for(agent_id)

                # A coding session's name lives in OpenCode, so a rename goes
                # there first and only then to the cache: a refusal leaves the
                # old name in place rather than a title that exists only here.
                # Pinning and archiving stay local — they are the console's own
                # arrangement and mean nothing to the service.
                link = _coding_link(agent_id, session_id) if title else None
                found = True
                if link is not None:
                    service = _coding_service_for(agent_id)
                    _coding_write(agent_id, service.rename,
                                  session_id=session_id, title=title)
                elif title:
                    found = store.rename_session(session_id, title)
                if pinned is not None:
                    found = store.set_pinned(session_id, bool(pinned)) and found
                if archived is not None:
                    found = store.set_archived(session_id, bool(archived)) and found
                if not found:
                    # A session only gets a row once its first message is stored, so
                    # this is also what a pin on a brand-new empty chat looks like.
                    return json.dumps({"status": "error", "message": "session not found"})
                return json.dumps({"status": "success"})
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Session update error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


def _drop_team_runtimes(
    session_id: str, owner_agent_id: Optional[str], *rosters
) -> None:
    """Drop the runtimes a changed roster invalidates, so they rebuild.

    An Agent's toolset is assembled once, when its runtime is built: the
    ``agent_delegate`` gate in ``AgentInitializer._load_tools`` reads the team at
    that moment. The roster itself is read per turn, so an Agent invited after
    the first message would see the team in its prompt while holding no tool to
    hand work to it. Evicting the participants makes the next turn rebuild
    against the new roster; persisted history is restored on rebuild, so no
    conversation content is lost.

    Every participant goes, not just the owner: a teammate cached in this
    session carries a roster in its own prompt too. Unknown or archived ids are
    skipped — the roster may name an Agent that no longer resolves.
    """
    from bridge.bridge import Bridge

    bridge = Bridge().get_agent_bridge()
    # The owner may be unnamed: the console stores a session under the default
    # Agent, so an empty id has to reach clear_session and be resolved there.
    victims = [owner_agent_id]
    for roster in rosters:
        victims.extend(roster if isinstance(roster, (list, tuple)) else [roster])
    seen = set()
    for agent_id in victims:
        if agent_id is not None:
            agent_id = str(agent_id).strip()
            if not agent_id:
                continue
        if agent_id in seen:
            continue
        seen.add(agent_id)
        try:
            bridge.clear_session(session_id, agent_id)
        except Exception as e:  # a bad roster must not fail the settings write
            logger.debug(
                f"[WebChannel] Could not drop runtime for '{agent_id}': {e}"
            )


class SessionSettingsHandler:
    """Per-session model and permission overrides.

    Both are stored outside the sessions table (see session_prefs) so they can be
    set before the conversation has its first message, and both fall back to the
    global config when unset.
    """

    def GET(self, session_id: str):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _session_settings_state
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})
            params = web.input(agent='', agent_id='')
            # Apply the DB scope so the model projection sees the caller's
            # model.use grants. Legacy mode is a no-op.
            with _db_scope():
                state = _session_settings_state(
                    session_id, params.agent or params.agent_id or None
                )
            return json.dumps({"status": "success", **state}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Session settings read error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self, session_id: str):
        """Set or clear this session's model / permission.

        Send ``null`` for a field to drop the override and follow the global
        setting again. ``model`` and ``provider`` move together: a model without
        its provider would be routed by the global bot type.
        """
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _is_database_identity
        from channel.web.fork.authorization import _require_model_use
        from channel.web.web_channel import _session_settings_state
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            from agent.workspace import session_prefs
            body = json.loads(web.data() or b"{}")
            agent_id = body.get("agent") or body.get("agent_id")

            # One DB scope for the whole mutation: ``set_prefs`` resolves the
            # tenant's shared root and the echoed state projects the caller's
            # ``model.use`` grants, so both need the request identity active.
            # Running them outside the scope writes the pin to the default
            # agent's workspace and echoes an empty, unselectable state.
            with _db_scope() as ctx:
                updates = {}
                # database mode owns execution through the caller's role grants,
                # so a session permission override is not accepted (storing one
                # that does nothing would be a dead, misleading knob).
                if "permission" in body and not _is_database_identity():
                    mode = body.get("permission")
                    updates["permission"] = (
                        permission_normalize_mode(mode) if mode else None
                    )
                if "model" in body or "provider" in body:
                    model = (body.get("model") or "").strip() or None
                    provider = (body.get("provider") or "").strip() or None
                    # Fine-grained model authorization: an explicit session model
                    # must be within the caller's model.use grant set (platform all
                    # or legacy mode pass). An out-of-scope explicit choice is
                    # rejected here rather than silently rerouted at the call site.
                    if model:
                        _require_model_use(ctx, model)
                    # Clearing the model clears its provider too: a pinned provider
                    # with no model would route the global model to the wrong vendor.
                    updates["model"] = model
                    updates["provider"] = provider if model else None
                previous_members: list = []
                roster_changed = False
                if "members" in body:
                    previous_members = list(
                        session_prefs.get_prefs(session_id, agent_id).get("members") or []
                    )
                    raw = body.get("members")
                    if raw is None:
                        submitted: list = []
                    elif isinstance(raw, list):
                        submitted = [
                            str(item).strip() for item in raw if str(item).strip()
                        ]
                    else:
                        return json.dumps({
                            "status": "error",
                            "message": "members must be a list of agent ids",
                        })
                    # The owner and every member are validated as one roster
                    # before anything is written (task 2.3). A coding Agent, an
                    # unknown/disabled id, a cross-tenant or another member's
                    # private Agent, or one the caller may not use refuses the
                    # whole write — never "drop it and store the rest".
                    from channel.web.fork.common import _validate_team_roster
                    validated = _validate_team_roster(ctx, agent_id, submitted)
                    updates["members"] = validated or None
                    # Compared as sets: the invite order is only the console's
                    # business, and a reorder must not cost every participant a
                    # rebuild.
                    roster_changed = (
                        set(updates["members"] or []) != set(previous_members)
                    )

                if not updates:
                    return json.dumps({
                        "status": "error",
                        "message": "permission, model, provider or members required",
                    })

                try:
                    session_prefs.set_prefs(session_id, agent_id, **updates)
                except session_prefs.SessionPrefsError as e:
                    # "Saved" has to mean saved. A write that never reached disk
                    # must not answer success: the console would switch into a
                    # team whose roster the next read cannot find (task 2.4).
                    # Nothing was retired — the runtime still holds the old
                    # roster — and the store still holds the previous value.
                    logger.error(
                        f"[WebChannel] Session settings write failed: sid={session_id}: {e}"
                    )
                    from channel.web.auth_handlers import _error
                    _error("could not save session settings",
                           500,
                           "session_prefs_write_failed")

                if roster_changed:
                    # The team is part of what a runtime is assembled against, so
                    # a changed roster has to retire the old runtimes: otherwise
                    # the next turn reads the team but has no `agent_delegate` to
                    # hand work to it (see _drop_team_runtimes).
                    _drop_team_runtimes(
                        session_id,
                        agent_id,
                        previous_members,
                        updates.get("members") or [],
                    )

                # Retarget the live agent so the change lands on the next message
                # without waiting for a fresh get_agent.
                try:
                    from bridge.bridge import Bridge
                    ab = Bridge().get_agent_bridge()
                    agent = ab.get_cached_agent(session_id, agent_id)
                    if agent is not None:
                        ab.apply_session_prefs(agent, session_id, agent_id)
                except Exception as e:
                    logger.debug(f"[WebChannel] session prefs apply-to-agent skipped: {e}")

                logger.info(
                    f"[WebChannel] Session settings updated: sid={session_id}, {updates}"
                )
                state = _session_settings_state(session_id, agent_id)
            return json.dumps({"status": "success", **state}, ensure_ascii=False)
        except web.HTTPError:
            # A deliberate refusal (a coding Agent in the member list, an
            # out-of-scope session model) carries its own status and machine
            # code; degrading it into 200-with-status:error would leave the
            # console unable to tell it from a crash, which is the same reason
            # every other handler in this module re-raises it.
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Session settings update error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SessionTitleHandler:
    def POST(self, session_id: str):
        from channel.web.web_channel import _db_scope
        from channel.web.fork.runtime import _generate_session_title
        from channel.web.fork.runtime import _request_agent_id
        from channel.web.web_channel import _require_read_permission
        from channel.web.web_channel import _require_session_scope
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            body = json.loads(web.data())
            user_message = body.get("user_message", "")
            assistant_reply = body.get("assistant_reply", "")
            if not user_message:
                return json.dumps({"status": "error", "message": "user_message required"})

            title = _generate_session_title(user_message, assistant_reply, session_id)

            with _db_scope() as ctx:
                _require_read_permission(ctx, "history.read")
                agent_id = _require_session_scope(
                    ctx, session_id, _request_agent_id(body))

                from agent.memory import get_conversation_store
                store = _conversation_store_for(agent_id)
                # The name of a coding session is OpenCode's, and this endpoint
                # derives a title from a message pair the platform would have to
                # have stored itself. A coding session has no such messages, so
                # a local write here could only diverge from the service.
                if _coding_link(agent_id, session_id) is not None:
                    _coding_unsupported(
                        "the coding service owns this session's name")
                updated = store.rename_session(session_id, title)
                logger.info(f"[WebChannel] Session title set: sid={session_id}, title='{title}', db_updated={updated}")

                return json.dumps({"status": "success", "title": title}, ensure_ascii=False)
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Title generation error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class PromptOptimizeHandler:
    """Optimize a colloquial user prompt into a structured AI-ready instruction."""

    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b"{}")
            user_input = (body.get("input") or "").strip()
            if not user_input:
                return json.dumps({"status": "error", "message": "input required"})

            context_messages = body.get("context_messages", None)

            from agent.chat.session_service import optimize_prompt
            optimized = optimize_prompt(user_input, context_messages)

            return json.dumps(
                {"status": "success", "optimized": optimized},
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error(f"[WebChannel] Prompt optimization error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


REPORT_AGENT_ID = "enterprise-planning-advisor"
_REPORT_PAGE_SIZE = 200
_REPORT_MAX_PAGES = 50
_REPORT_MAX_CHARS = 60000
_REPORT_TIMEOUT = 120

_REPORT_SYSTEM_PROMPT = """你是企业流程调研分析师。只依据用户提供的访谈问答生成报告，不得编造事实、数据或系统能力。
输出 Markdown，并包含以下部分：
一、企业与岗位概览；
二、逐项工作（输入、作业方法、系统规则、输出与验收）；
三、频率与耗时（日/周/月、操作/等待/返工，明确未知项）；
四、错误、风险、发现与纠正、复核；
五、资料缺口（含未核验的附件/外部资料）；
六、候选智能体场景与试点建议（按频率、净可节省时间、收益、数据/接口成熟度和风险排序，并说明人工边界）。
每个结论后用 [#序号] 标注依据；区分「已确认」「未确认」「分析建议」。未知就写未知，不虚构 ROI 或项目接入能力。"""


def _report_error(status: str, code: str, message: str) -> None:
    raise web.HTTPError(
        status, {"Content-Type": "application/json"},
        json.dumps({"status": "error", "code": code, "message": message},
                   ensure_ascii=False))


def _load_interview_messages(store, session_id: str, all_owners: bool):
    """Every saved turn of one session, oldest first, plus a character count.

    Pages are newest-first, so each page is prepended. The loop is bounded so a
    pathological transcript cannot hold the request open.
    """
    messages = []
    page = 1
    while page <= _REPORT_MAX_PAGES:
        data = store.load_history_page(
            session_id=session_id, page=page, page_size=_REPORT_PAGE_SIZE,
            user_id=None, all_owners=all_owners)
        batch = data.get("messages") or []
        if not batch:
            break
        messages = batch + messages
        if not data.get("has_more"):
            break
        page += 1
    return messages


def _render_transcript(messages) -> str:
    lines = []
    for index, message in enumerate(messages, 1):
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        role = "受访者" if message.get("role") == "user" else "顾问"
        lines.append(f"[#{index}] {role}：{content.strip()}")
    return "\n\n".join(lines)


def _messages_signature(messages) -> str:
    import hashlib

    digest = hashlib.sha256()
    for message in messages:
        digest.update(str(message.get("role") or "").encode("utf-8", "ignore"))
        digest.update(b"\x00")
        content = message.get("content")
        digest.update((content if isinstance(content, str) else "").encode("utf-8", "ignore"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _build_report_prompt(transcript: str, count: int) -> str:
    return (
        f"以下是同一次企业访谈的全部已保存问答，共 {count} 条。"
        "请只依据这些内容生成调研与智能体规划报告，并在每条结论后标注 [#序号]。\n\n"
        f"<transcript>\n{transcript}\n</transcript>"
    )


def _call_report_model(prompt: str):
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeout
    from bridge.bridge import Bridge
    from models.session_manager import Session

    bot = Bridge().get_bot("chat")
    session = Session("__interview_report__", system_prompt=_REPORT_SYSTEM_PROMPT)
    session.messages = [{"role": "user", "content": prompt}]
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(bot.reply_text, session).result(timeout=_REPORT_TIMEOUT)
    except FutureTimeout:
        _report_error("504 Gateway Timeout", "report_timeout",
                      "报告生成超时，请重试")
    finally:
        pool.shutdown(wait=False)


class InterviewReportHandler:
    """One-shot report over one interview, returned to the page and never stored.

    Authorization is the read gate plus the caller's existing model conditions
    and hard quota. The model call uses the real requester's ambient identity,
    never a service account or the interviewee. The result is returned only
    after the source prefix is re-read and found unchanged.
    """

    def POST(self):
        import re
        from channel.web.web_channel import _db_scope
        from channel.web.fork.runtime import _request_agent_id
        from channel.web.web_channel import _require_read_permission
        from auth.service import get_identity_service

        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'private, no-store')
        try:
            if not _enterprise_interview_enabled():
                _report_error("403 Forbidden", "feature_disabled", "feature disabled")
            from channel.web.auth_handlers import _csrf_ok
            if not _csrf_ok():
                _report_error("403 Forbidden", "csrf_failed", "invalid request origin")
            body = json.loads(web.data() or b"{}")
            session_id = str(body.get("session_id") or "").strip()
            agent_id = str(body.get("agent_id") or _request_agent_id(body) or "").strip()
            if not session_id:
                _report_error("400 Bad Request", "session_id_required", "session_id required")
            if agent_id != REPORT_AGENT_ID:
                _report_error("400 Bad Request", "unsupported_agent",
                              "report is only available for the planning advisor")

            with _db_scope() as ctx:
                if ctx is None or not ctx.tenant_id:
                    _report_error("403 Forbidden", "forbidden", "forbidden")
                svc = get_identity_service()
                store = _conversation_store_for(agent_id)
                owner = store.get_session_owner(session_id) or ""
                if not owner:
                    _report_error("403 Forbidden", "forbidden", "forbidden")
                cross_owner = owner != ctx.user_id
                if cross_owner:
                    from channel.web.fork.authorization import _require_history_read_all
                    _require_history_read_all(ctx, agent_id)
                else:
                    _require_read_permission(ctx, "history.read")

                if not ctx.is_platform_admin:
                    if "model.use" not in ctx.permissions:
                        _report_error("403 Forbidden", "model_not_authorized",
                                      "model use is not authorized")
                    if not ctx.is_tenant_admin:
                        if not svc.explicit_resource_ids(
                                ctx.user_id, ctx.tenant_id, "model", "use"):
                            _report_error("403 Forbidden", "model_not_authorized",
                                          "no model grant for this member")

                if not svc.quota_available(
                        user_id=ctx.user_id, tenant_id=ctx.tenant_id,
                        metric="tokens", amount=1):
                    _report_error("429 Too Many Requests", "quota_exhausted",
                                  "token quota exhausted")

                messages = _load_interview_messages(store, session_id, cross_owner)
                transcript = _render_transcript(messages)
                if not transcript.strip():
                    _report_error("400 Bad Request", "no_content",
                                  "this session has no saved question/answer")
                if len(transcript) > _REPORT_MAX_CHARS:
                    _report_error("413 Payload Too Large", "input_too_long",
                                  "this interview is too long for one report")
                prefix_len = len(messages)
                signature = _messages_signature(messages)

                result = _call_report_model(
                    _build_report_prompt(transcript, prefix_len)) or {}
                raw = (result.get("content") or "").strip()
                tokens = int(result.get("total_tokens")
                             or result.get("completion_tokens") or 0)
                if tokens > 0:
                    try:
                        svc.consume_quota(
                            user_id=ctx.user_id, tenant_id=ctx.tenant_id,
                            metric="tokens", amount=tokens)
                    except Exception as error:  # noqa: BLE001 - meter, not content
                        logger.warning(f"[WebChannel] report quota record skipped: {error}")
                if not raw:
                    _report_error("502 Bad Gateway", "model_unavailable",
                                  "the model returned no report")

                after = _load_interview_messages(store, session_id, cross_owner)
                if (len(after) < prefix_len
                        or _messages_signature(after[:prefix_len]) != signature):
                    _report_error("409 Conflict", "source_changed",
                                  "the interview changed while the report was generated")

                report = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
                if not report:
                    _report_error("502 Bad Gateway", "model_unavailable",
                                  "the model returned no report")
                try:
                    svc.record_audit(
                        action="session.interview_report.generate",
                        target=f"agent:{agent_id}",
                        actor_user_id=ctx.user_id,
                        actor_username=getattr(ctx, "username", "") or "",
                        tenant_id=ctx.tenant_id,
                        redacted_changes={"session_id": session_id,
                                          "message_count": prefix_len,
                                          "tokens": tokens},
                    )
                except Exception as error:  # noqa: BLE001 - best-effort audit
                    logger.warning(f"[WebChannel] report audit skipped: {error}")

                return json.dumps({
                    "status": "success",
                    "report": report,
                    "source": {
                        "session_id": session_id,
                        "agent_id": agent_id,
                        "message_count": prefix_len,
                        "generated_at": int(time.time()),
                    },
                }, ensure_ascii=False)
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] interview report error: {e}")
            return json.dumps({"status": "error", "message": str(e)},
                              ensure_ascii=False)



class SessionClearContextHandler:
    def POST(self, session_id: str):
        from channel.web.web_channel import _db_scope
        from channel.web.fork.runtime import _request_agent_id
        from channel.web.web_channel import _require_read_permission
        from channel.web.web_channel import _require_session_scope
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})
            params = web.input(agent_id='')
            raw_body = web.data()
            body = json.loads(raw_body) if raw_body else {}
            requested = _request_agent_id(body) or _request_agent_id(params)

            with _db_scope() as ctx:
                _require_read_permission(ctx, "history.read")
                agent_id = _require_session_scope(ctx, session_id, requested)

                from agent.memory import get_conversation_store
                store = _conversation_store_for(agent_id)

                # A coding conversation has no platform context to clear: its
                # turns live in OpenCode, and emptying a mirror that holds
                # nothing would answer success without changing anything the
                # user can see. Refusing says so plainly.
                if _coding_link(agent_id, session_id) is not None:
                    _coding_unsupported(
                        "this session's context lives in the coding service")

                from agent.chat.session_service import SessionService
                service = SessionService(agent_id=agent_id)
                # Upstream clears the whole team. Validate every transcript
                # before the first write: a persisted roster may outlive a grant
                # or mention a same-named session owned by another user.
                for member_id in service._teammates(session_id, agent_id):
                    _require_session_scope(ctx, session_id, member_id)
                new_seq = service.clear_context(session_id, agent_id=agent_id)

                return json.dumps({"status": "success", "context_start_seq": new_seq})
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Clear context error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class HistoryHandler:
    def GET(self):
        from channel.web.fork.runtime import _add_delegate_displays
        from channel.web.fork.runtime import _add_subagent_displays
        from channel.web.fork.runtime import _artifacts_from_steps
        from channel.web.web_channel import _db_scope
        from channel.web.fork.runtime import _request_agent_id
        from channel.web.web_channel import _require_read_permission
        from channel.web.fork.authorization import _require_session_owner
        from channel.web.web_channel import _require_tenant_agent_binding
        from channel.web.web_channel import _get_workspace_root
        from channel.web.fork.runtime import _rewrite_relative_media
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            params = web.input(session_id='', page='1', page_size='20', agent_id='',
                               until_seq='', read_scope='')
            session_id = params.session_id.strip()
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            with _db_scope() as ctx:
                read_scope = str(params.read_scope or '').strip()
                cross_owner = read_scope == 'agent_all'
                if cross_owner:
                    if not _enterprise_interview_enabled():
                        raise web.HTTPError(
                            "403 Forbidden",
                            {"Content-Type": "application/json"},
                            json.dumps({"status": "error", "code": "feature_disabled",
                                        "message": "feature disabled"}))
                    from channel.web.fork.authorization import _require_history_read_all
                    agent_id = _require_history_read_all(
                        ctx, _request_agent_id(params))
                else:
                    _require_read_permission(ctx, "history.read")
                    agent_id = _require_tenant_agent_binding(
                        ctx, _request_agent_id(params))
                    _require_session_owner(ctx, session_id, agent_id)
                from agent.memory import get_conversation_store
                from agent.registry import get_agent_registry
                # Conversation persistence and the cross-Agent history list
                # use each Agent's own workspace. The tenant's shared working
                # directory can differ and contains no history for this Agent.
                store = get_conversation_store(
                    get_agent_registry().get(agent_id, require_enabled=False).workspace
                )
                until_seq = params.until_seq.strip()
                page = int(params.page)
                # A reply still streaming is shown as of its last stored point and
                # followed live from there, so nothing appears twice or goes missing.
                live = None
                if page == 1 and not until_seq and not cross_owner:
                    try:
                        live = WebChannel().resumable_stream(session_id, agent_id)
                    except Exception as e:
                        logger.debug(f"[WebChannel] resumable stream lookup skipped: {e}")
                result = store.load_history_page(
                    session_id=session_id,
                    page=page,
                    page_size=int(params.page_size),
                    until_seq=int(until_seq) if until_seq.lstrip('-').isdigit() else None,
                    user_id=None if cross_owner else (ctx.user_id if ctx else None),
                    all_owners=cross_owner,
                    max_seq=live["stored_seq"] if live else None,
                )
                if cross_owner:
                    from auth.service import get_identity_service
                    result["owner"] = store.get_session_owner(session_id) or ""
                    if not result["owner"]:
                        raise web.HTTPError(
                            "403 Forbidden",
                            {"Content-Type": "application/json"},
                            json.dumps({"status": "error", "code": "forbidden",
                                        "message": "forbidden"}))
                    names = (get_identity_service().member_display_names(
                        ctx.tenant_id, {result["owner"]}) if ctx else {})
                    result["creator"] = names.get(result["owner"], "")
                    result["read_only"] = True
                    _audit_history_read(ctx, agent_id, "detail", session_id)
                if live:
                    messages = result.get("messages") or []
                    last = messages[-1] if messages else {}
                    running = last.get("role") == "assistant" and last.get("run_state") == "running"
                    # Before the run starts there is nothing stored to line up
                    # with; once it has, only its own unfinished turn is followed.
                    if running or live["stored_seq"] is None:
                        result["active_request"] = {
                            "request_id": live["request_id"],
                            "after_seq": live["after_seq"],
                        }
                for msg in result.get("messages") or []:
                    if msg.get("role") != "assistant":
                        continue
                    # Same workspace-relative media rewrite the live SSE path
                    # applies, so images/videos survive a page reload for
                    # non-default agents.
                    if isinstance(msg.get("content"), str) and msg["content"]:
                        try:
                            msg["content"] = _rewrite_relative_media(
                                msg["content"],
                                _get_workspace_root(session_id, agent_id),
                            )
                        except Exception as e:
                            logger.debug(f"[WebChannel] history media rewrite skipped: {e}")
                    _add_subagent_displays(msg.get("steps"))
                    _add_delegate_displays(msg.get("steps"))
                    artifacts = _artifacts_from_steps(msg.get("steps"), session_id, agent_id)
                    if artifacts:
                        msg["artifacts"] = artifacts
                return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] History API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class MessageDeleteHandler:
    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.fork.runtime import _request_agent_id
        from channel.web.web_channel import _require_read_permission
        from channel.web.web_channel import _require_session_scope
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            data = json.loads(web.data())
            session_id = data.get('session_id', '').strip()
            user_seq = data.get('user_seq')
            delete_user = data.get('delete_user', True)
            cascade = data.get('cascade', False)
            
            if not session_id or user_seq is None:
                return json.dumps({"status": "error", "message": "session_id and user_seq required"})
            
            with _db_scope() as ctx:
                _require_read_permission(ctx, "history.read")
                agent_id = _require_session_scope(
                    ctx, session_id, _request_agent_id(data))

                # 1. Delete from database
                from agent.memory import get_conversation_store
                store = _conversation_store_for(agent_id)

                # Deleting one turn is an edit of the platform's copy of a
                # conversation; a coding session's turns are OpenCode's own
                # records, so this must not pretend to edit them.
                if _coding_link(agent_id, session_id) is not None:
                    _coding_unsupported(
                        "this session's messages live in the coding service")

                deleted = store.delete_message_pair(session_id, int(user_seq), delete_user=delete_user, cascade=cascade)

                # 2. Sync agent's in-memory context so its next turn sees the
                # same history as the DB. Handled by the agent_bridge helper.
                try:
                    from bridge.bridge import Bridge
                    Bridge().get_agent_bridge().sync_session_messages_from_store(
                        session_id, agent_id=agent_id
                    )
                except Exception as sync_err:
                    logger.warning(f"[WebChannel] Failed to sync agent memory: {sync_err}")

                return json.dumps({"status": "success", "deleted": deleted}, ensure_ascii=False)
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Message delete error: {e}")
            return json.dumps({"status": "error", "message": str(e)})




class UserMessagesHandler:
    """The upstream timeline index, scoped by the current database identity."""

    def GET(self):
        from channel.web.web_channel import _db_scope, _require_read_permission, _require_session_scope
        from channel.web.fork.runtime import _request_agent_id
        web.header('Content-Type', 'application/json; charset=utf-8')
        params = web.input(session_id='', agent_id='')
        session_id = params.session_id.strip()
        if not session_id:
            return json.dumps({"status": "error", "message": "session_id required"})
        with _db_scope() as ctx:
            _require_read_permission(ctx, "history.read")
            agent_id = _require_session_scope(ctx, session_id, _request_agent_id(params))
            result = _conversation_store_for(agent_id).list_user_messages(session_id)
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
