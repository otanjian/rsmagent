# encoding:utf-8
"""Trusted delivery targets for the Web scheduler create flow.

Why this exists
---------------
The upstream console gained three scheduler endpoints -- ``instances``,
``recipients`` and ``create`` -- so a member can hand-author a task that
delivers to someone on an external IM channel. Upstream's version answered all
three from the *configured* channel list and the global recipient directory, so
the browser received every instance and every contact and filtered them itself.
That is wrong on this deployment twice over:

* a channel *instance* is an authorized object with a scope (``tenant`` or a
  member's own ``user``) and an owner, and the range a caller may see is already
  decided by :meth:`IdentityService.list_tenant_channel_instances`. Shipping the
  global list to the client and hiding rows in JavaScript would leak the
  existence and names of other members' connections;
* the recipient directory is keyed by ``(instance_id, receiver)`` because the
  same receiver id on two WeChat logins is two different people, so a receiver
  is only a legal target on an instance the caller is allowed to use. A forged
  ``receiver`` (or an instance the caller cannot reach) must be refused on the
  server, not filtered in the browser.

This module is that single server-side resolver, shared by the three endpoints.
It does not define a new authorization model: the instance range comes from the
identity service, the Agent comes from the instance's *current* binding, and the
recipient comes from the trusted directory. The caller cannot name tenant,
owner, Agent or receiver identity -- every one of those is derived here.

Deliverability
--------------
An authorized row is not automatically deliverable: a disabled instance, one
the tenant has governance-stopped, a type with no adapter, or a row whose Agent
is no longer bound/enabled cannot carry a delivery. Those are filtered out of
the *listing* and re-checked in :meth:`SchedulerTargetService.resolve_target`,
so a task can never be created against a target that would silently drop.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

#: Recipient fields the Web console is allowed to see (``TaskRecipient`` in
#: ``desktop/src/renderer/src/types.ts``). Everything else the directory keeps
#: (nothing sensitive today, but the projection is the contract) stays server
#: side: the console must not start depending on an internal field.
RECIPIENT_FIELDS: Tuple[str, ...] = (
    "channel_type",
    "instance_id",
    "receiver",
    "name",
    "is_group",
    "session_id",
    "instance_name",
    "last_seen_at",
)

#: Channel types that are never a scheduler delivery target: the Web console's
#: own session is ephemeral, and ``unknown`` names no adapter at all.
_NON_DELIVERY_TYPES = frozenset({"", "web", "unknown"})

#: The synthetic target meaning "deliver into this very Web session". It is *not*
#: a channel instance and never enters the recipient directory: a Web session is
#: private to one member and its receiver is the session id itself, so the caller
#: may only name *which conversation*, while the server resolves *who receives*
#: from the authenticated session (see :meth:`SchedulerTargetService.resolve_self_target`).
#: The value matches ``channel_type='web'`` on purpose -- the delivery layer
#: already routes ``channel_type='web'`` back through the running Web channel, so
#: a self target needs no new delivery branch.
SELF_INSTANCE_ID = "web"

#: How the synthetic target is named in the console and stored on the task as
#: ``receiver_name``. It has to stand on its own: receipts and task lists rarely
#: render the instance label next to it.
SELF_TARGET_NAME = "本站（当前会话）"


def _refuse(code: str, status: int) -> None:
    """Raise the scheduler's stable refusal (lazy import avoids an import cycle)."""
    from agent.tools.scheduler.authorization import TaskAuthorizationError
    raise TaskAuthorizationError(code, status=status)


def _normalized_channel_type(channel_type: str) -> str:
    from channel.channel_instances import _normalize_type
    return _normalize_type(str(channel_type or "").strip())


class SchedulerTargetService:
    """Authorized channel instances and their trusted recipients.

    ``identity_service`` answers the caller's instance range and the Agent
    bindings; ``recipient_store`` is the shared directory of contacts observed
    on inbound messages. Both are injected so the resolve path is testable
    without a global, and so the HTTP layer cannot substitute a second source of
    truth for either.
    """

    def __init__(self, identity_service: Any, recipient_store: Any) -> None:
        self._identity = identity_service
        self._recipients = recipient_store

    # -- public API --------------------------------------------------------

    def list_instances(self, ctx: Any) -> List[Dict[str, Any]]:
        """Every instance this caller may deliver through, ready or not.

        The range is the identity service's own
        (``list_tenant_channel_instances``): a member sees only their own
        ``user`` rows, a controller also sees the tenant's. Nothing here widens
        that range -- this only removes rows that cannot deliver.

        The synthetic "this session" target is always first and is offered even
        when the caller has no usable IM instance at all -- a member who has
        never connected a channel is exactly who needs it most.
        """
        granted = self._granted_instances(ctx)
        counts = self._recipient_counts({item["id"] for item in granted})
        return [self._self_instance()] + [
            self._project_instance(item, counts) for item in granted]

    def list_recipients(self, ctx: Any,
                        instance_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Trusted recipients, narrowed to the caller's authorized instances.

        An explicit ``instance_id`` outside that set is answered as
        ``target_not_found`` (404): whether the row belongs to another member or
        another tenant is deliberately not disclosed. Omitting it returns the
        union over every authorized instance, and no authorized instance returns
        ``[]`` without reading the directory at all.
        """
        by_id = {item["id"]: item for item in self._granted_instances(ctx)}
        explicit = str(instance_id or "").strip()
        if explicit:
            if explicit not in by_id:
                _refuse("target_not_found", 404)
            wanted = {explicit}
        else:
            wanted = set(by_id)
        if not wanted or self._recipients is None:
            return []
        entries = self._recipients.list(instance_ids=wanted)
        return [self._project_recipient(entry, by_id) for entry in entries]

    def resolve_target(self, ctx: Any, instance_id: str,
                       receiver: str,
                       agent_id: Optional[str] = None
                       ) -> Tuple[str, Dict[str, Any]]:
        """Re-verify one target and return ``(agent_id, recipient_metadata)``.

        This is the create path's whole trust boundary, so it re-derives every
        fact from the live services rather than trusting the caller's payload:
        the instance must still be in the caller's range and deliverable, its
        Agent binding must still resolve, and the receiver must still be in the
        directory *for that instance*. Any failure refuses -- a target that was
        valid when the picker rendered is not valid now.

        ``instance_id == SELF_INSTANCE_ID`` is the one non-directory target: it
        is a session fact, resolved from the authenticated session (see
        :meth:`resolve_self_target`), never from the recipient directory. The
        ``agent_id`` argument is an address hint used only there; for a channel
        target the Agent comes from the instance's own binding.
        """
        explicit = str(instance_id or "").strip()
        if explicit == SELF_INSTANCE_ID:
            # ``receiver`` carries the *session id* here, and is only evidence of
            # which conversation the caller means; ``resolve_self_target``
            # re-derives both the owner and the receiver.
            return self.resolve_self_target(ctx, receiver, agent_id)
        wanted_receiver = str(receiver or "").strip()
        if not explicit or not wanted_receiver:
            _refuse("invalid_target", 400)
        item = {entry["id"]: entry
                for entry in self._granted_instances(ctx)}.get(explicit)
        if item is None:
            _refuse("target_not_found", 404)
        agent_id = self._bound_agent(item)
        if not agent_id:
            # The row is ours but its binding no longer resolves (unbound,
            # switched to another tenant, or disabled): unusable, not "not
            # found", because the caller may legitimately see the instance.
            _refuse("invalid_target", 400)
        entry = self._recipients.get(explicit, wanted_receiver) \
            if self._recipients is not None else None
        if not entry:
            # A receiver absent from the directory is indistinguishable from a
            # forged one, so both answer the same way.
            _refuse("target_not_found", 404)
        stored_instance = str(entry.get("instance_id")
                              or entry.get("channel_type") or "")
        if stored_instance != explicit:
            _refuse("target_not_found", 404)
        if _normalized_channel_type(entry.get("channel_type")) in _NON_DELIVERY_TYPES:
            _refuse("target_not_found", 404)
        return agent_id, self._recipient_metadata(entry)

    def resolve_self_target(self, ctx: Any, session_id: str,
                            agent_id: Optional[str] = None
                            ) -> Tuple[str, Dict[str, Any]]:
        """Resolve the caller's own Web session as a delivery target.

        The trust boundary is the one the rest of the fork already uses --
        :func:`channel.web.fork.authorization._owned_context_target`, which
        applies ``_require_session_scope`` (tenant binding, Agent visibility and
        the owner probe) **and** requires the durable ``sessions`` row to match
        this caller with ``channel_type='web'``. Reusing it is deliberate: this
        adds a legal *target source*, not a second authorization model, and a
        session that does not exist can never become a target.

        The receiver is the session id by definition (a Web conversation's
        receiver *is* its session id), so it is derived here and never read from
        the request.

        Which Agent the session runs under is a session fact too. A hint is
        honored when the caller gives one (it is validated by the same helper as
        any other Agent address, and the create handler refuses a hint that
        disagrees with the resolution); with no hint -- the console's create
        payload names only the session -- the caller's own Agent range is tried
        in a deterministic order and the row itself decides. Only Agents the
        caller's tenant is bound to, and which the registry has enabled, are
        ever tried, so the loop cannot reach another tenant's conversation.
        """
        wanted = str(session_id or "").strip()
        if not wanted:
            _refuse("invalid_target", 400)
        from channel.web.fork.authorization import _owned_context_target

        enabled = self._enabled_agent_ids()
        if enabled is None:
            # No roster means no Agent can be proven able to run the task; the
            # same fail-closed rule ``_bound_agent`` applies to channel targets.
            _refuse("invalid_target", 400)
        for candidate in self._self_candidates(ctx, agent_id, enabled):
            try:
                resolved, _store = _owned_context_target(ctx, wanted, candidate)
            except Exception:
                # ``_owned_context_target`` refuses by raising the fork's own
                # HTTP error: 404 for a session that is absent, another member's
                # or not a web session, 403 when the Agent is not tenant-visible.
                # Keep looking at the other candidates -- but never report which
                # check failed, so "not yours" and "never existed" stay
                # indistinguishable to the caller.
                continue
            if resolved not in enabled:
                _refuse("invalid_target", 400)
            return resolved, {
                "channel_type": SELF_INSTANCE_ID,
                "instance_id": SELF_INSTANCE_ID,
                "receiver": wanted,
                "name": SELF_TARGET_NAME,
                "is_group": False,
                "session_id": wanted,
                "last_seen_at": "",
            }
        # No Agent the caller may address owns this session. The message is
        # actionable on purpose: a brand-new conversation has no ``sessions`` row
        # until its first message, which is the one common way to reach here.
        from agent.tools.scheduler.authorization import TaskAuthorizationError
        raise TaskAuthorizationError(
            "target_not_found", status=404,
            message="当前会话尚不可作为投递目标，请先在此对话中发送一条消息后再创建")

    def _self_candidates(self, ctx: Any, agent_id: Optional[str],
                         enabled: set) -> List[str]:
        """The Agents a session may be looked up under, in a stable order.

        Derived only from the caller's own tenant binding plus the enabled
        roster: a caller-supplied hint first (validated later by
        ``_owned_context_target`` like any other Agent address), then the
        tenant's bound default Agent, then the remaining bound Agents. The loop
        that consumes this does not *decide* anything -- the ``sessions`` row is
        still the only proof, and an Agent outside this list is never read.
        """
        hint = str(agent_id or "").strip()
        if hint:
            return [hint]
        bound = [item for item in self._tenant_agent_ids(ctx) if item in enabled]
        if not bound:
            return []
        default_id = ""
        try:
            from agent.registry import get_agent_registry
            default_id = get_agent_registry().default_agent_id or ""
        except Exception:  # noqa: BLE001 - no roster means no default to prefer
            default_id = ""
        ordered = ([default_id] if default_id in bound else []) + \
            [item for item in bound if item != default_id]
        return ordered

    def _tenant_agent_ids(self, ctx: Any) -> List[str]:
        """The Agents the caller's tenant is bound to (fail closed to none)."""
        tenant_id = str(getattr(ctx, "tenant_id", "") or "")
        if not tenant_id:
            return []
        try:
            return [str(item) for item in
                    (self._identity.tenant_agent_ids(tenant_id) or [])]
        except Exception:  # noqa: BLE001 - an unreadable binding is no candidate
            return []

    @staticmethod
    def _self_instance() -> Dict[str, Any]:
        """The synthetic "deliver to this session" instance (always first).

        It carries no ``recipient_count``: the recipient step is fixed to "me",
        so counting the directory would misreport the target.
        """
        return {
            "instance_id": SELF_INSTANCE_ID,
            "channel_type": SELF_INSTANCE_ID,
            "name": SELF_TARGET_NAME,
            "channel_label": SELF_TARGET_NAME,
            "agent_id": "",
            "recipient_count": 0,
            "is_self": True,
        }

    # -- range + deliverability -------------------------------------------

    def _granted_instances(self, ctx: Any) -> List[Dict[str, Any]]:
        """The caller's instances, minus anything that cannot deliver."""
        actor_user_id = str(getattr(ctx, "user_id", "") or "")
        tenant_id = str(getattr(ctx, "tenant_id", "") or "")
        listing = self._identity.list_tenant_channel_instances(
            actor_user_id=actor_user_id, tenant_id=tenant_id)
        items = (listing or {}).get("items") or []
        enabled_agents = self._enabled_agent_ids()
        granted: List[Dict[str, Any]] = []
        for item in items:
            if not self._deliverable(item, tenant_id, enabled_agents):
                continue
            granted.append(item)
        return granted

    def _deliverable(self, item: Dict[str, Any], tenant_id: str,
                     enabled_agents: Optional[set]) -> bool:
        """Whether one authorized row can actually carry a delivery.

        ``enabled_agents`` is the registry's enabled roster; ``None`` means the
        registry could not be resolved and nothing is deliverable (fail closed
        rather than create a task whose Agent might not exist at fire time).
        """
        if not bool(item.get("active")):
            return False
        if bool(item.get("governance_disabled")):
            return False
        channel_type = _normalized_channel_type(item.get("channel_type"))
        if channel_type in _NON_DELIVERY_TYPES:
            return False
        from channel.channel_instances import MULTI_INSTANCE_READY
        if channel_type not in MULTI_INSTANCE_READY:
            # No adapter can open this type (or no credential contract): it
            # would never deliver, so it is not offered as a target.
            return False
        return bool(self._bound_agent(item, tenant_id=tenant_id,
                                      enabled_agents=enabled_agents))

    def _bound_agent(self, item: Dict[str, Any], *, tenant_id: Optional[str] = None,
                     enabled_agents: Optional[set] = None) -> str:
        """The Agent an instance delivers as, or ``""`` when unresolvable.

        The binding is read from the identity service (never from the request),
        must belong to the caller's tenant, and must be enabled in the Agent
        registry -- the same two halves ``_scheduler_agent_ids`` applies to the
        task list, so the target list and the task list agree.
        """
        agent_id = str(item.get("agent_id") or "").strip()
        if not agent_id:
            return ""
        tenant_id = tenant_id if tenant_id is not None \
            else str(item.get("tenant_id") or "")
        try:
            binding = self._identity.get_agent_binding(agent_id)
        except Exception:  # noqa: BLE001 - an unreadable binding is unusable
            return ""
        if not binding or str(binding.get("tenant_id") or "") != tenant_id:
            return ""
        if enabled_agents is None:
            enabled_agents = self._enabled_agent_ids()
        if enabled_agents is None or agent_id not in enabled_agents:
            return ""
        return agent_id

    @staticmethod
    def _enabled_agent_ids() -> Optional[set]:
        try:
            from agent.registry import get_agent_registry
            return {profile.id
                    for profile in get_agent_registry().list(include_disabled=False)}
        except Exception:  # noqa: BLE001 - no roster is a closed target list
            return None

    # -- projections -------------------------------------------------------

    def _recipient_counts(self, allowed: set) -> Dict[str, int]:
        """Recipients per *authorized* instance (never the global directory)."""
        counts: Dict[str, int] = {}
        if not allowed or self._recipients is None:
            return counts
        for entry in self._recipients.list(instance_ids=allowed):
            instance_id = str(entry.get("instance_id")
                              or entry.get("channel_type") or "")
            if instance_id in allowed:
                counts[instance_id] = counts.get(instance_id, 0) + 1
        return counts

    @staticmethod
    def _instance_name(item: Dict[str, Any]) -> str:
        from channel.channel_instances import _CHANNEL_TYPE_LABELS
        channel_type = _normalized_channel_type(item.get("channel_type"))
        return (str(item.get("display_name") or "").strip()
                or _CHANNEL_TYPE_LABELS.get(channel_type)
                or channel_type
                or str(item.get("id") or ""))

    def _project_instance(self, item: Dict[str, Any],
                          counts: Dict[str, int]) -> Dict[str, Any]:
        from channel.channel_instances import _CHANNEL_TYPE_LABELS
        channel_type = _normalized_channel_type(item.get("channel_type"))
        instance_id = str(item.get("id") or "")
        return {
            "instance_id": instance_id,
            "channel_type": channel_type,
            "name": self._instance_name(item),
            "channel_label": _CHANNEL_TYPE_LABELS.get(channel_type, channel_type),
            "agent_id": str(item.get("agent_id") or ""),
            "recipient_count": int(counts.get(instance_id, 0)),
        }

    @staticmethod
    def _recipient_metadata(entry: Dict[str, Any]) -> Dict[str, Any]:
        """Trusted identity of one recipient (the whitelist, nothing else)."""
        return {
            "channel_type": _normalized_channel_type(entry.get("channel_type")),
            "instance_id": str(entry.get("instance_id")
                               or entry.get("channel_type") or ""),
            "receiver": str(entry.get("receiver") or ""),
            "name": str(entry.get("name") or entry.get("receiver") or ""),
            "is_group": bool(entry.get("is_group")),
            "session_id": str(entry.get("session_id")
                              or entry.get("receiver") or ""),
            "last_seen_at": str(entry.get("last_seen_at") or ""),
        }

    def _project_recipient(self, entry: Dict[str, Any],
                           by_id: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        metadata = self._recipient_metadata(entry)
        instance_id = metadata["instance_id"]
        item = by_id.get(instance_id) or {}
        metadata["instance_name"] = self._instance_name(item) if item else instance_id
        # Emit only the declared projection, in a stable order.
        return {field: metadata.get(field) for field in RECIPIENT_FIELDS}
