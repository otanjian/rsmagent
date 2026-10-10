"""Effective digital-employee capability resolution.

Resolves the *effective* capabilities of an Agent after applying its optional
scene binding, and provides the shared tool filter used by both tool assembly
and tool dispatch so that allow/deny cannot be bypassed.

The resolution model (mirrors OneAgent's EmployeeConfig.merge_with_scene):

- A non-empty Agent field wins over the scene.
- An empty Agent field inherits the scene value when one exists.
- ``tools_denylist`` = Agent denylist ∪ scene denylist.
- ``tools_allowlist``: if the Agent has a non-empty allowlist it wins over the
  scene tools; only when the Agent has no allowlist do we inherit scene tools.
"""
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from agent.registry import AgentProfile


@dataclass(frozen=True)
class EffectiveCapabilities:
    """The merged, effective capabilities for one Agent run."""

    persona_summary: Optional[str] = None
    greeting: Optional[str] = None
    scene_id: Optional[str] = None
    skills: Optional[Tuple[str, ...]] = None
    knowledge_ids: Optional[Tuple[str, ...]] = None
    sops: Tuple[str, ...] = ()
    tools_allowlist: Optional[Tuple[str, ...]] = None
    tools_denylist: Tuple[str, ...] = ()


def _scene_field(scene: Optional[dict], key: str):
    """Return a scene value only when it is a usable (non-empty) value."""
    if not scene:
        return None
    value = scene.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return value


def _scene_list(scene: Optional[dict], key: str) -> Tuple[str, ...]:
    if not scene:
        return ()
    value = scene.get(key)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(x).strip() for x in value if str(x).strip())


def _merge_denylist(profile_list: Sequence[str], scene_list: Sequence[str]) -> Tuple[str, ...]:
    seen = []
    for v in list(profile_list) + list(scene_list):
        v = v.strip()
        if v and v not in seen:
            seen.append(v)
    return tuple(seen)


def _as_optional_tuple(value) -> Optional[Tuple[str, ...]]:
    if value is None:
        return None
    if isinstance(value, str):
        return (value,) if value else None
    return tuple(str(x) for x in value)


def _released_overrides(profile: AgentProfile):
    """The released effective surface for ``profile``, or a denial marker.

    Returns ``None`` (no override, draft is fine) when governance does not apply,
    a dict when it does, and :data:`_DENY_ALL` when governance applies but the
    released surface cannot be resolved — the fail-closed case (task 5.3). The
    marker is deliberately not ``None`` so the caller cannot mistake it for
    "no governance" and fall back to the draft.
    """
    agent_id = getattr(profile, "id", None)
    if not agent_id:
        return None
    try:
        from auth.agent_release import released_capability_overrides
    except Exception:  # noqa: BLE001 - the default path must never be broken by this
        return None
    try:
        return released_capability_overrides(agent_id)
    except Exception:  # noqa: BLE001 - governance applies but is unverifiable
        return _DENY_ALL


#: Marker meaning "governance applies and the released surface is unavailable".
_DENY_ALL = object()


def resolve_effective_capabilities(
    profile: AgentProfile, scene: Optional[dict] = None, *,
    ignore_release: bool = False,
) -> EffectiveCapabilities:
    """Compute effective capabilities for ``profile`` given an optional ``scene``.

    ``scene`` may be ``None`` (no binding / scene missing). The caller resolves
    the scene dict by ``profile.scene_id`` beforehand.

    When shared-Agent release governance is enabled and ``profile`` is a
    *released* shared binding, the frozen released surface wins over both the
    editable roster and any scene: a member run must not see a draft edit. The
    release flow itself passes ``ignore_release=True`` so a re-publish captures
    the current draft rather than the previous release.
    """
    if not ignore_release:
        released = _released_overrides(profile)
        if released is _DENY_ALL:
            # Governance applies but no released surface can be resolved: deny,
            # do not fall back to the draft (task 5.3, fail-closed).
            return EffectiveCapabilities(
                skills=(), knowledge_ids=(), sops=(),
                tools_allowlist=("__released_surface_unavailable__",),
                tools_denylist=())
        if released is not None:
            return EffectiveCapabilities(
                persona_summary=released.get("persona_summary"),
                greeting=released.get("greeting"),
                scene_id=released.get("scene_id"),
                skills=_as_optional_tuple(released.get("skills")),
                knowledge_ids=_as_optional_tuple(released.get("knowledge_ids")),
                sops=tuple(released.get("sops") or ()),
                tools_allowlist=_as_optional_tuple(released.get("tools_allowlist")),
                tools_denylist=tuple(released.get("tools_denylist") or ()),
            )
    # persona_summary: non-empty Agent wins, else inherit scene system_prompt.
    persona = profile.persona_summary or _scene_field(scene, "system_prompt")
    greeting = profile.greeting or _scene_field(scene, "greeting")

    # knowledge_ids: non-empty Agent wins, else inherit scene knowledge if any.
    if profile.knowledge_ids is not None and len(profile.knowledge_ids) > 0:
        knowledge_ids = tuple(profile.knowledge_ids)
    else:
        knowledge_ids = None

    # sops: non-empty Agent wins, else inherit scene sops.
    sops = tuple(profile.sops) if profile.sops else _scene_list(scene, "sops")

    # tools_allowlist: Agent non-empty wins; else inherit scene tools (if any).
    if profile.tools_allowlist and len(profile.tools_allowlist) > 0:
        allowlist = tuple(profile.tools_allowlist)
    else:
        scene_tools = _scene_list(scene, "tools")
        allowlist = tuple(scene_tools) if scene_tools else None

    # tools_denylist: union of Agent and scene.
    denylist = _merge_denylist(profile.tools_denylist, _scene_list(scene, "tools_denylist"))

    # skills: keep the profile's explicit selection (None meaning "all").
    skills = profile.skills

    return EffectiveCapabilities(
        persona_summary=persona,
        greeting=greeting,
        scene_id=profile.scene_id,
        skills=skills,
        knowledge_ids=knowledge_ids,
        sops=sops,
        tools_allowlist=allowlist,
        tools_denylist=denylist,
    )


def filter_tools(tools: Iterable[str], effective: EffectiveCapabilities) -> List[str]:
    """Filter a tool-name list by the effective allow/deny lists.

    Both a non-empty allowlist and the denylist apply. When there is no
    allowlist, only the denylist constrains the set.
    """
    result = []
    for name in tools:
        if is_tool_allowed(name, effective):
            result.append(name)
    return result


def is_tool_allowed(name: str, effective: EffectiveCapabilities) -> bool:
    """Return True if ``name`` may be used under ``effective``.

    A non-empty allowlist requires the tool to be listed; otherwise default
    tools are all allowed. In either case a denylisted tool is rejected.
    """
    name = name.strip()
    if name in effective.tools_denylist:
        return False
    if effective.tools_allowlist:
        return name in effective.tools_allowlist
    return True
