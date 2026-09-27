# encoding:utf-8
"""Per-connection Agent assignment: the relation, and who may use what.

The console and the runtime must not disagree about "may this Agent use this
connection", so the rule lives here, once, over the raw store. Two callers read
it — :class:`~integrations.external.service.ExternalConnectionService` (the
console's lists and saves) and :class:`~integrations.external.runtime.ConnectionRuntime`
(the tool projection and the final dispatch) — and neither owns a second copy.

Three facts decide the answer, and the reason is returned alongside it so a
refusal can be diagnosed without turning into a discovery oracle:

``configured=False`` (or **no state row at all**, which only a pre-migration
database can produce) means 沿用原权限: the connection keeps the authorization it
always had. This is the compatibility half of the change, and it is deliberately
per-connection — going live with one connection does not narrow a tenant.

``configured=True`` with the Agent in the relation means the Agent may use the
connection (subject to every pre-existing gate: user authorization, connection
enabled, tool allow/deny, open execution class, risk and quota).

``configured=True`` without the Agent — or with no Agent in the trusted context —
is a refusal. An *empty* configured set is therefore a real "nobody", not a
fallback to the old rule: 清空分配表示禁止所有智能体使用.

What this module deliberately does **not** do: read a credential, touch the
connection's ``version``, or consult a tool allow/deny list. Adding or removing
an assignment must not invalidate a test badge, rotate a secret or widen a tool
range, so the only tables named here are the two this change added.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

from integrations.external import registry

#: Connection kinds that participate in Agent assignment. Personal email is
#: excluded by both scope and kind: 本期个人邮箱保持原有行为.
ASSIGNMENT_KINDS = frozenset(
    {registry.KIND_MCP, registry.KIND_ERP, registry.KIND_OA})

#: The refusal reasons, shared so the console, the runtime and the tests branch
#: on one vocabulary instead of three similar-looking strings.
REASON_UNCONFIGURED = "unconfigured"
REASON_ASSIGNED = "assigned"
REASON_NOT_ASSIGNED = "agent_not_assigned"
REASON_NO_AGENT = "agent_context_missing"
REASON_STATE_MISSING = "assignment_state_missing"
REASON_LOOKUP_FAILED = "assignment_lookup_failed"

#: The hard cap on explicit changes in one save request (spec: 单次最多 100 项).
MAX_ASSIGNMENT_CHANGE = 100


def logical_connection_id(row: Any) -> str:
    """The id the assignment set is keyed on for one connection row.

    A tenant MCP override stands in for its platform template, so it uses the
    **template's** id: the template and the override share one set, and removing
    the override (恢复继承) does not orphan the assignments. Every other row —
    a tenant's own ERP/OA/MCP, or a platform template read directly by a
    consumer tenant — keys on its own id.
    """
    base = _field(row, "base_connection_id")
    return str(base or _field(row, "id") or "")


def assignment_regime_applies(kind: Any, scope: Any) -> bool:
    """Whether a connection is subject to per-Agent assignment at all.

    Platform/personal scopes are out: a platform template is assigned *per
    consuming tenant* (the tenant-scoped row is what carries the relation), and
    a personal mailbox keeps its existing owner rule.
    """
    return (str(kind or "") in ASSIGNMENT_KINDS
            and str(scope or "") == registry.SCOPE_TENANT)


def call_regime_applies(kind: Any, scope: Any, tenant_id: Any) -> bool:
    """Whether one *call* is subject to per-Agent assignment.

    The same rule as :func:`assignment_regime_applies`, with the one case a
    single row cannot express: a platform MCP template reached directly by a
    consuming tenant (no override of its own) is a ``platform`` row, but the
    relation that governs it is the tenant's. The consuming tenant — never the
    row's own ``tenant_id``, which is ``None`` there — decides, so the gate is
    the tenant's own assignment set for the template's logical id.

    A platform admin reading the platform catalogue names no tenant and is
    therefore outside the regime: there is nothing per-Agent to enforce there.
    """
    if str(kind or "") not in ASSIGNMENT_KINDS:
        return False
    if str(scope or "") == registry.SCOPE_PERSONAL:
        return False
    if str(scope or "") == registry.SCOPE_TENANT:
        return True
    return bool(str(tenant_id or ""))


def logical_ids_for(store, connection_ids) -> Dict[str, str]:
    """Map connection id -> logical id for the given ids, in one query.

    An id that is not in the result is one whose row could not be read; the
    caller must treat that as "cannot be proven assigned" rather than assuming
    the old rule. One query, never one per row, so a large tool list stays a
    constant number of reads.
    """
    wanted = sorted({str(c) for c in connection_ids if c})
    if not wanted:
        return {}
    placeholders = ",".join("?" for _ in wanted)
    out: Dict[str, str] = {}
    for row in store.execute(
            "SELECT id, base_connection_id FROM external_connections"
            " WHERE id IN (%s)" % placeholders, tuple(wanted)):
        out[str(row["id"])] = logical_connection_id(row)
    return out


def assignment_state(store, *, tenant_id: str,
                     logical_id: str) -> Optional[Mapping[str, Any]]:
    """The ``(configured, revision)`` row, or ``None`` when there is none."""
    rows = store.execute(
        "SELECT configured, revision FROM"
        " external_connection_agent_assignment_sets"
        " WHERE tenant_id=? AND logical_connection_id=?",
        (tenant_id, logical_id),
    )
    return rows[0] if rows else None


def is_assigned(store, *, tenant_id: str, logical_id: str,
                agent_id: str) -> bool:
    """Whether one Agent is in the connection's relation right now."""
    if not agent_id:
        return False
    return bool(store.execute(
        "SELECT 1 FROM external_connection_agent_assignments"
        " WHERE tenant_id=? AND logical_connection_id=? AND agent_id=? LIMIT 1",
        (tenant_id, logical_id, agent_id),
    ))


def assigned_agent_ids(store, *, tenant_id: str,
                       logical_id: str) -> list:
    """Every Agent id in the relation, ordered for stable presentation."""
    return [str(r["agent_id"]) for r in store.execute(
        "SELECT agent_id FROM external_connection_agent_assignments"
        " WHERE tenant_id=? AND logical_connection_id=? ORDER BY agent_id",
        (tenant_id, logical_id),
    )]


def assignment_allows(store, *, tenant_id: str, logical_id: str,
                      agent_id: str) -> Tuple[bool, str]:
    """The runtime decision for one call.

    Returns ``(allowed, reason)``. A missing state row is a refusal, not a
    silent 沿用原权限: the migration backfills every existing connection, so a
    live managed connection with no state is a corruption signal, and treating
    it as "unconfigured" would be the bypass the spec forbids. Only an explicit
    ``configured=0`` row means compatibility.
    """
    if not tenant_id or not logical_id:
        return False, REASON_STATE_MISSING
    state = assignment_state(store, tenant_id=tenant_id, logical_id=logical_id)
    if state is None:
        return False, REASON_STATE_MISSING
    if not int(state["configured"]):
        return True, REASON_UNCONFIGURED
    if not agent_id:
        return False, REASON_NO_AGENT
    if is_assigned(store, tenant_id=tenant_id, logical_id=logical_id,
                   agent_id=agent_id):
        return True, REASON_ASSIGNED
    return False, REASON_NOT_ASSIGNED


def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    try:
        return obj[name]
    except (TypeError, KeyError, IndexError):
        return getattr(obj, name, None)
