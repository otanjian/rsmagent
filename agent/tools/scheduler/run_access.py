# encoding:utf-8
"""The access layer over attributed scheduler runs (fork-owned).

Task history is per-Agent data behind several callers (Web, Desktop); this
module is the one place that answers "which runs may this request see, and may
it delete them". It sits on top of :mod:`agent.tools.scheduler.run_repository`
(the SQL) and reuses :class:`TaskAccessService` for the *policy* — personal
owner-only, public view for members, public manage for administrators — so the
history view cannot drift from the task management view.

Why grants, and why rebuilt every request
-----------------------------------------
A page that was loaded while the member held a grant must not keep that grant
after it is revoked. So every call re-resolves the actor from the verified
identity, then derives a :class:`RunGrant` from the *current* tenant membership
and Agent bindings. Nothing from the request body (tenant, owner, agent list)
is trusted, and there is no cached grant to go stale.

Personal history survives ``agent.use`` revocation
--------------------------------------------------
An owner's right to their own history is an ownership right, not a use of the
Agent — the same rule ``TaskAccessService`` applies to a personal task. The
personal branch is therefore bounded by bound+enabled Agents and the recorded
owner, never by ``agent.use``.

What this module does NOT do
----------------------------
It never reads the delivered body. ``get_run`` returns the attributed preview
with ``full_output=None``; the safe session-scoped body read belongs to the
history HTTP layer (P6), which re-checks the session's durable ownership. It
also never queries ``ConversationStore.list_runs``/``get_run_detail``: those are
unscoped and time-proximity based, which is exactly the leak this layer
replaces.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

from agent.tools.scheduler.authorization import (
    ACTION_MANAGE,
    ACTION_VIEW,
    NOT_MEMBER,
    SCOPE_PERSONAL,
    SCOPE_PUBLIC,
    TaskActor,
    TaskAccessService,
    TaskAuthorizationError,
)
from agent.tools.scheduler.run_repository import (
    RUN_NOT_FOUND,
    RunGrant,
    RunQuery,
    RunScopeRepository,
)

logger = logging.getLogger("scheduler_run_access")

#: The coverage statement the history list always carries: only runs with a
#: trustworthy execution-time snapshot are shown, never backfilled ones.
HISTORY_SCOPE = "attributed_only"

#: Run columns projected onto the wire, in the shape the console already reads.
_RUN_FIELDS = (
    "run_id", "agent_id", "user_id", "session_id", "parent_run_id",
    "task_id", "task_source", "status", "started_at", "ended_at", "error",
)
#: The only ``extras`` keys a client sees; the raw sidecar (and the internal
#: scope snapshot) is never returned.
_EXTRA_FIELDS = (
    "task_name", "action_type", "channel_type", "instance_id", "trigger",
    "output_preview",
)


def _project_run(row: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten one joined row into the client-facing record shape.

    Drops the internal authorization aliases (``scope_*``) and the raw
    ``extras`` dict, lifting only the whitelisted snapshot fields.
    """
    projected: Dict[str, Any] = {field: row.get(field) for field in _RUN_FIELDS}
    extras = row.get("extras") or {}
    if not isinstance(extras, dict):
        extras = {}
    for field in _EXTRA_FIELDS:
        projected[field] = extras.get(field, "")
    return projected


class RunAccessService:
    """Authorize scheduled-run history for one request at a time.

    ``repository`` is the SQL seam; ``task_access`` is the existing
    ``TaskAccessService`` built from the request's ``_db_scope``; ``actor_resolver``
    rebuilds the :class:`TaskActor` from the verified session/tenant each time it
    is called (also inside the delete transaction); ``agent_ids`` is the same
    ``_scheduler_agent_ids`` used by task management, so both views agree on
    which Agents the actor can address.
    """

    def __init__(
        self,
        repository: RunScopeRepository,
        task_access: TaskAccessService,
        actor_resolver: Callable[[], TaskActor],
        agent_ids: Callable[[TaskActor], Sequence[str]],
        *,
        audit: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._repository = repository
        self._task_access = task_access
        self._actor_resolver = actor_resolver
        self._agent_ids = agent_ids
        self._audit_hook = audit

    # -- reads -----------------------------------------------------------

    def list_runs(self, query: RunQuery) -> Dict[str, Any]:
        """The HTTP success body for one page of authorized history."""
        actor = self._actor_resolver()
        self._require_member(actor)
        grant = self._grant(actor, ACTION_VIEW)
        rows = self._repository.list_visible(grant, query)
        return {
            "status": "success",
            "runs": [_project_run(row) for row in rows],
            "history_scope": HISTORY_SCOPE,
        }

    def get_run(self, run_id: str) -> Dict[str, Any]:
        """One authorized run's preview, with ``full_output`` left for the caller.

        The safe body read needs the session's durable ownership check, which
        lives in the history HTTP layer; this method returns ``full_output=None``
        so the caller can fill it in only after that check passes.
        """
        actor = self._actor_resolver()
        self._require_member(actor)
        grant = self._grant(actor, ACTION_VIEW)
        row = self._repository.get_visible(grant, run_id)
        if row is None:
            raise TaskAuthorizationError(RUN_NOT_FOUND, status=404)
        detail = _project_run(row)
        detail["full_output"] = None
        return detail

    # -- delete ----------------------------------------------------------

    def delete_run(self, run_id: str) -> None:
        """Delete one authorized run record, auditing ids and outcome only.

        The manage grant is resolved again inside the repository transaction so
        a revocation that lands mid-request cannot delete the record.
        """
        actor = self._actor_resolver()
        self._require_member(actor)

        def resolve_grant() -> RunGrant:
            current = self._actor_resolver()
            self._require_member(current)
            return self._grant(current, ACTION_MANAGE)

        try:
            deleted = self._repository.delete_visible(resolve_grant, run_id)
        except TaskAuthorizationError as error:
            self._audit(actor, "run_delete", run_id, "", "denied", error.code)
            raise
        self._audit(actor, "run_delete", deleted.get("run_id", run_id),
                    deleted.get("task_id", ""), "success", "")

    # -- internals -------------------------------------------------------

    def _require_member(self, actor: TaskActor) -> None:
        # A non-member gets no grant at all: returning an empty page would read
        # as "no history" and hide a revoked account behind a success response.
        if not actor.is_member:
            raise TaskAuthorizationError(NOT_MEMBER, status=403)

    def _grant(self, actor: TaskActor, action: str) -> RunGrant:
        """Derive the trusted grant for one action from the current state.

        The personal/public split reuses ``TaskAccessService.decide`` with a
        minimal task carrying the snapshot's intended shape (scope + owner), so
        the authorization rules have exactly one implementation. In particular
        the "public manage for administrators" predicate is not copied here.
        """
        if not actor.is_member:
            return RunGrant(
                tenant_id="", user_id="",
                personal_agent_ids=(), public_agent_ids=())
        agent_ids: List[str] = []
        for agent_id in self._agent_ids(actor) or ():
            agent_id = (agent_id or "").strip()
            if agent_id and agent_id not in agent_ids:
                agent_ids.append(agent_id)
        personal: List[str] = []
        public: List[str] = []
        for agent_id in agent_ids:
            personal_task = {
                "scope": SCOPE_PERSONAL,
                "agent_id": agent_id,
                "owner": {
                    "user_id": actor.user_id,
                    "tenant_id": actor.tenant_id,
                    "agent_id": agent_id,
                },
            }
            public_task = {"scope": SCOPE_PUBLIC, "agent_id": agent_id}
            if self._task_access.decide(personal_task, actor, agent_id, action)[0]:
                personal.append(agent_id)
            if self._task_access.decide(public_task, actor, agent_id, action)[0]:
                public.append(agent_id)
        return RunGrant(
            tenant_id=actor.tenant_id,
            user_id=actor.user_id,
            personal_agent_ids=tuple(personal),
            public_agent_ids=tuple(public),
        )

    def _audit(self, actor: TaskActor, action: str, run_id: str, task_id: str,
               result: str, reason: str) -> None:
        """Record a redacted delete outcome (ids and result only)."""
        if self._audit_hook is not None:
            try:
                self._audit_hook(
                    actor=actor, action=action, run_id=run_id, task_id=task_id,
                    result=result, reason=reason)
                return
            except Exception as error:
                logger.warning("[scheduler_run_access] audit hook failed: %s", error)
        service = None
        try:
            service = self._task_access.service
        except Exception:
            service = None
        if service is None or not actor.is_member:
            return
        try:
            service.record_business_audit(
                actor_user_id=actor.user_id,
                tenant_id=actor.tenant_id,
                action="scheduler.%s" % action,
                target="run:%s" % run_id,
                redacted_changes={"task_id": task_id, "reason": reason or None},
                result=result,
            )
        except Exception as error:
            # The operation already happened; a failed audit must be loud.
            logger.error("[scheduler_run_access] audit write failed: %s", error)
