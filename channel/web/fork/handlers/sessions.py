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
        from channel.web.web_channel import _agent_badge
        from channel.web.web_channel import _annotate_coding_sessions
        from channel.web.web_channel import _annotate_sessions_with_projects
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _list_sessions_across_agents
        from channel.web.web_channel import _request_agent_id
        from channel.web.web_channel import _require_read_permission
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            with _db_scope() as ctx:
                _require_read_permission(ctx, "history.read")
                params = web.input(
                    page='1', page_size='50', agent_id='', agent='', scope='', q='',
                    archived=''
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


class SessionDetailHandler:
    def DELETE(self, session_id: str):
        from channel.web.web_channel import WebChannel
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _request_agent_id
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
                link = _coding_link(agent_id, session_id)
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
                    scoped = Bridge().get_agent_bridge().scoped_session_key(session_id)
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
                    channel.cancel_session(session_id)
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
        from channel.web.web_channel import _request_agent_id
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
        from channel.web.web_channel import _require_model_use
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
                        updates["members"] = None
                    elif isinstance(raw, list):
                        updates["members"] = [
                            str(item).strip() for item in raw if str(item).strip()
                        ]
                    else:
                        return json.dumps({
                            "status": "error",
                            "message": "members must be a list of agent ids",
                        })
                    # A team is a set of Agents answering in the same
                    # conversation, so a coding Agent can never be one: it has no
                    # ordinary runtime to take a turn.
                    from channel.web.web_channel import _reject_coding_agent
                    for member in updates.get("members") or []:
                        _reject_coding_agent(str(member).strip())
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

                session_prefs.set_prefs(session_id, agent_id, **updates)

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
        from channel.web.web_channel import _generate_session_title
        from channel.web.web_channel import _request_agent_id
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


class SessionClearContextHandler:
    def POST(self, session_id: str):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _request_agent_id
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

                new_seq = store.clear_context(session_id)

                # Delete the agent instance so a fresh one is created on the next message
                try:
                    from bridge.bridge import Bridge
                    bridge = Bridge()
                    ab = bridge.get_agent_bridge()
                    ab.clear_session(session_id, agent_id=agent_id)
                except Exception:
                    pass

                return json.dumps({"status": "success", "context_start_seq": new_seq})
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Clear context error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class HistoryHandler:
    def GET(self):
        from channel.web.web_channel import _add_delegate_displays
        from channel.web.web_channel import _add_subagent_displays
        from channel.web.web_channel import _artifacts_from_steps
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _request_agent_id
        from channel.web.web_channel import _require_read_permission
        from channel.web.web_channel import _require_session_owner
        from channel.web.web_channel import _require_tenant_agent_binding
        from channel.web.web_channel import _get_workspace_root
        from channel.web.web_channel import _rewrite_relative_media
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            params = web.input(session_id='', page='1', page_size='20', agent_id='')
            session_id = params.session_id.strip()
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            with _db_scope() as ctx:
                _require_read_permission(ctx, "history.read")
                agent_id = _require_tenant_agent_binding(ctx, _request_agent_id(params))
                _require_session_owner(ctx, session_id, agent_id)
                from agent.memory import get_conversation_store
                from agent.registry import get_agent_registry
                # Conversation persistence and the cross-Agent history list
                # use each Agent's own workspace. The tenant's shared working
                # directory can differ and contains no history for this Agent.
                store = get_conversation_store(
                    get_agent_registry().get(agent_id, require_enabled=False).workspace
                )
                result = store.load_history_page(
                    session_id=session_id,
                    page=int(params.page),
                    page_size=int(params.page_size),
                    user_id=ctx.user_id if ctx else None,
                )
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
                    artifacts = _artifacts_from_steps(msg.get("steps"), session_id)
                    if artifacts:
                        msg["artifacts"] = artifacts
                return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] History API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class MessageDeleteHandler:
    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _request_agent_id
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


