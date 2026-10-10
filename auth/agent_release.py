# encoding:utf-8
"""Shared-Agent release governance: state, immutable snapshots, content hash.

Change ``add-shared-agent-release-governance``.

A tenant-shared Agent is only visible to and runnable by members after an
explicit, approved, audited *release*. This module owns the parts that are pure
and testable in isolation:

* the release **state** vocabulary and the deployment **switch**;
* the **snapshot** of an Agent's effective, member-visible surface and its
  stable **content hash** — the identity a single approval is bound to;
* the single predicate that decides whether a binding is member-visible under
  governance; and
* the runtime **override lookup** that makes a member run read the released
  snapshot rather than the editable draft.

The stateful flow (request approval, publish, roll back, suspend, backfill)
lives in :class:`auth.service.IdentityService`, reusing the existing approval
engine, audit log, resource quota and enterprise enablement. Nothing here opens
a transaction or writes a row.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

#: Deployment switch (default off). See ``config.available_setting``.
FLAG_KEY = "shared_agent_release_governance"

#: Binding release states. Empty/``None`` means "never released" (the state every
#: binding has before governance is enabled).
STATE_DRAFT = "draft"
STATE_RELEASED = "released"
STATE_SUSPENDED = "suspended"
RELEASE_STATES = (STATE_DRAFT, STATE_RELEASED, STATE_SUSPENDED)

#: Approval action ids. They are declared on the deployment's
#: ``approval_required_actions`` list like every other gated action.
ACTION_RELEASE = "agent.release"
ACTION_ROLLBACK = "agent.release.rollback"
ACTION_SUSPEND = "agent.release.suspend"

#: Roster fields copied into a snapshot's ``profile`` section, in a fixed order.
PROFILE_FIELDS: Tuple[str, ...] = (
    "name", "description", "model", "bot_type", "avatar",
    "skills", "knowledge", "knowledge_ids", "sops",
    "tools_allowlist", "tools_denylist",
    "position", "category", "tags", "greeting",
    "usage_hint", "suggested_questions", "persona_summary", "scene_id",
    "agent_type",
)

#: Effective-capability fields stored in the snapshot's ``effective`` section.
EFFECTIVE_FIELDS: Tuple[str, ...] = (
    "persona_summary", "greeting", "scene_id", "skills",
    "knowledge_ids", "sops", "tools_allowlist", "tools_denylist",
)

#: Fields whose values are lists of strings and normalize as a sorted set.
_LIST_FIELDS = frozenset((
    "skills", "knowledge", "knowledge_ids", "sops",
    "tools_allowlist", "tools_denylist", "tags", "suggested_questions",
))


def release_governance_enabled(config: Optional[Mapping[str, Any]] = None) -> bool:
    """Whether the deployment enforces release governance.

    Unreadable configuration means *disabled*: the feature is opt-in, and a
    broken config must not start hiding every shared Agent.
    """
    if config is None:
        try:
            from config import conf
            config = conf() or {}
        except Exception:  # noqa: BLE001 - policy must stay importable without config
            return False
    return bool(config.get(FLAG_KEY, False))


def normalize_state(value: Any) -> str:
    """A release state, or ``""`` for "never released"/unknown."""
    state = str(value or "").strip().lower()
    return state if state in RELEASE_STATES else ""


def _normalize_string_list(value: Any) -> Optional[list]:
    if value is None:
        return None
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted({str(x).strip() for x in value if str(x).strip()})
    text = str(value).strip()
    return [text] if text else []


def _normalize_value(key: str, value: Any) -> Any:
    if key in _LIST_FIELDS:
        return _normalize_string_list(value)
    return value


def normalize_profile(data: Mapping[str, Any]) -> Dict[str, Any]:
    """Project a roster mapping onto the snapshot's fixed field set."""
    out: Dict[str, Any] = {}
    for key in PROFILE_FIELDS:
        if key not in data:
            continue
        value = _normalize_value(key, data.get(key))
        # Skip a *list* field that is absent-meaning-``None`` so "all/empty"
        # keeps its distinct meaning; a scalar that is falsy is still recorded
        # because some of them are meaningful when blank.
        if value is None and key in _LIST_FIELDS:
            continue
        out[key] = value
    return out


def build_snapshot(
    profile: Mapping[str, Any],
    *,
    effective: Optional[Mapping[str, Any]] = None,
    connections: Sequence[str] = (),
    channels: Sequence[str] = (),
) -> Dict[str, Any]:
    """Assemble the frozen member-visible surface of one Agent.

    ``profile`` is a roster projection (``AgentProfile.to_dict()`` or an
    equivalent mapping); ``effective`` is the resolved capability set
    (``agent.effective_capabilities.resolve_effective_capabilities``, which
    already folds in any scene binding), so a later scene edit cannot move a
    released surface. ``connections``/``channels`` are the ids of the external
    connections and channel instances the Agent was bound to *at release time* —
    references, not copies.
    """
    payload: Dict[str, Any] = {"profile": normalize_profile(profile)}
    if effective is not None:
        payload["effective"] = {
            key: _normalize_value(key, effective.get(key)) for key in EFFECTIVE_FIELDS
        }
    payload["connections"] = sorted(
        {str(x).strip() for x in (connections or ()) if str(x).strip()})
    payload["channels"] = sorted(
        {str(x).strip() for x in (channels or ()) if str(x).strip()})
    return payload


def canonical_snapshot(snapshot: Mapping[str, Any]) -> str:
    """The one serialization both the hash and the stored row are derived from."""
    return json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    """The stable content hash of a snapshot (its version identity)."""
    return hashlib.sha256(canonical_snapshot(snapshot).encode("utf-8")).hexdigest()


def binding_member_visible(binding: Optional[Mapping[str, Any]], *,
                           enabled: bool) -> bool:
    """Whether a binding is visible to *members* under the current switch.

    Private bindings (a ``private_owner_user_id`` is set) are owner-scoped and
    outside this capability, so they are always visible. A shared binding is
    visible only while its release state is ``released``; a draft, a suspended
    release and a never-released row are not. When the switch is off the
    predicate is a no-op, which is what keeps the delivered behaviour.
    """
    if not enabled:
        return True
    if binding is None:
        return False
    if binding.get("private_owner_user_id") is not None:
        return True
    return normalize_state(binding.get("release_state")) == STATE_RELEASED


def release_target(agent_id: str) -> str:
    """The approval target of a release action (``agent:<id>``)."""
    return "agent:%s" % str(agent_id or "").strip()


class ReleasedSurfaceUnavailable(Exception):
    """Governance applies to this Agent, but its released surface is unreadable.

    Raised by :func:`released_capability_overrides` so a member run fails *closed*
    instead of silently falling back to the editable draft. It is deliberately
    distinct from "no governance applies" (which returns ``None``).
    """


def release_digest(action: str, agent_id: str, content_hash: str) -> str:
    """The approval digest binding an action to one candidate content hash.

    Reuses ``agent.approval_gate.request_digest`` so the release gate and every
    other gated action compute the digest the same way: the approval covers the
    candidate that was reviewed, and a later content change is a mismatch, not a
    guess.
    """
    from agent.approval_gate import request_digest
    return request_digest(action, release_target(agent_id),
                          {"content_hash": str(content_hash or "")})


def released_capability_overrides(agent_id: str) -> Optional[Dict[str, Any]]:
    """The released *effective* capabilities for a member run, or ``None``.

    Called from the tool-assembly/dispatch path. Returns ``None`` when governance
    does not apply at all — the switch is off, or the Agent is private/unbound —
    which keeps the delivered behaviour byte-for-byte. When governance *does*
    apply (a shared binding) but the released surface cannot be resolved — the
    binding is a draft/suspended/never-released row, or the snapshot is missing or
    unreadable — it raises :class:`ReleasedSurfaceUnavailable` so the caller fails
    closed instead of running the draft.
    """
    if not release_governance_enabled():
        return None
    try:
        from auth.service import get_identity_service
        service = get_identity_service()
    except Exception:  # noqa: BLE001 - no service means no governance at all
        return None
    try:
        binding = service.get_agent_binding(agent_id)
    except Exception as error:  # noqa: BLE001 - unreadable state is not a pass
        raise ReleasedSurfaceUnavailable(str(error))
    if not binding or binding.get("private_owner_user_id") is not None:
        return None
    if normalize_state(binding.get("release_state")) != STATE_RELEASED:
        raise ReleasedSurfaceUnavailable(
            "shared Agent %r is not released" % agent_id)
    try:
        snapshot = service.released_snapshot(agent_id)
    except Exception as error:  # noqa: BLE001
        raise ReleasedSurfaceUnavailable(str(error))
    if not snapshot or "effective" not in snapshot:
        raise ReleasedSurfaceUnavailable(
            "released surface for %r is missing" % agent_id)
    return dict(snapshot.get("effective") or {})
