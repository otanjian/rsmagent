# encoding:utf-8
"""External capabilities as agent tools: one declaration, one dispatcher.

A connection type contributes tools by declaring them here, not by reaching
into the tool manager. Why the indirection matters:

* **The declaration and the dispatch cannot disagree.** A tool is declared with
  the ``(kind, action)`` it maps to, and dispatch goes back through the same
  pair. There is no second place where a tool name is translated into an
  action, which is where "the tool list offers something the executor refuses"
  comes from.
* **Authorization is per call, not per listing.** Listing tools says what
  *could* be offered; every invocation re-derives the connection, the actor and
  the deployment's open classes through :class:`ConnectionRuntime`. A tool
  being listed is never itself permission to run it (spec: 工具发现不等于调用
  授权).
* **A tool with no authorization is absent, not disabled.** A member who cannot
  use a tenant ERP connection does not see its tools at all, so the model cannot
  be talked into trying one.

The providers are registered by the type adapters' modules, so a build without
an adapter simply has no tools for that kind.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence

from common.log import logger

from integrations.external import assignment, authorization, registry
from integrations.external.errors import forbidden, not_found

#: Capability label the identity layer's tool projection uses.
CAPABILITY_TOOL = "tool"

#: The hook an adapter module exposes to (re)register its provider and
#: dispatcher. Import-time registration runs exactly once per process, so a
#: module cache hit is *not* evidence that the provider map is still populated
#: — see :func:`load_providers`.
REGISTRATION_HOOK = "register_tools"


@dataclass(frozen=True)
class ExternalTool:
    """One offerable external capability."""

    name: str
    kind: str
    action: str
    description: str = ""
    #: True for an action that changes the remote system. Drives the approval
    #: requirement and the risk catalogue lookup; never the only gate.
    write: bool = False
    #: Extra, non-secret hints for the model and the console (e.g. the target
    #: system's own action name).
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def declared_resource_id(self) -> str:
        """The grantable id of this capability, keyed on ``(kind, action)``.

        The same value the runtime authorizes against
        (``integrations.external.authorization.resource_id_for``), so the
        catalogue cannot offer a grant the dispatch does not read. This is
        keyed on the *capability* rather than on ``name``: a bound tool's name
        carries its connection id, and a grant must survive that connection
        being replaced.
        """
        from integrations.external import authorization
        return authorization.resource_id_for(self.kind, self.action)

    @property
    def source(self) -> str:
        return "external:%s" % self.kind

    def as_projection(self, connection_id: str) -> Dict[str, Any]:
        return {
            "resource_id": self.declared_resource_id,
            "name": self.name,
            "capability": CAPABILITY_TOOL,
            "source": self.source,
            "description": self.description,
            "kind": self.kind,
            "action": self.action,
            "write": self.write,
            "connection_id": connection_id,
        }


@dataclass(frozen=True)
class ToolBinding:
    """A tool bound to the connection it will actually run against."""

    tool: ExternalTool
    connection_id: str
    connection_name: str = ""
    scope: str = ""

    def as_dict(self) -> Dict[str, Any]:
        payload = self.tool.as_projection(self.connection_id)
        payload["connection_name"] = self.connection_name
        payload["scope"] = self.scope
        return payload


#: (tenant_id, actor_user_id) -> bound tools the actor may use right now.
ToolProvider = Callable[[Optional[str], str], Iterable[ToolBinding]]

_PROVIDERS: Dict[str, ToolProvider] = {}
_PROVIDER_LOCK = threading.Lock()


def register_tool_provider(kind: str, provider: ToolProvider) -> None:
    """Register the provider for one kind. Last registration wins."""
    kind = str(kind or "").strip()
    if not kind:
        raise ValueError("a tool provider needs a kind")
    with _PROVIDER_LOCK:
        _PROVIDERS[kind] = provider


def providers() -> Dict[str, ToolProvider]:
    with _PROVIDER_LOCK:
        return dict(_PROVIDERS)


def load_providers() -> None:
    """Import the built-in adapter modules so their providers self-register.

    Registration is an *import side effect*, so it happens exactly once per
    process. This therefore does not treat "the import succeeded" as "the
    provider is registered": for every built-in kind whose provider is missing
    but whose module is already imported, the module's
    :data:`REGISTRATION_HOOK` is re-run.

    That second step is what makes the registry rebuildable. Without it, a
    provider map that was ever cleared — :func:`reset`, a module reload, a
    startup order that imported the module before the tools registry existed —
    stays empty for the life of the process, and every external tool silently
    disappears in a way that is indistinguishable from "this build has none".
    """
    from integrations.external.adapters.base import _builtin_adapters
    from integrations.external.adapters.base import load_adapters

    load_adapters()
    for kind, module_path in _builtin_adapters().items():
        if kind in providers():
            continue
        module = sys.modules.get(module_path)
        if module is None:
            continue  # the import failed; ``adapter_import_error`` explains it
        hook = getattr(module, REGISTRATION_HOOK, None)
        if not callable(hook):
            continue
        try:
            hook()
        except Exception:  # noqa: BLE001 - one type must not hide the rest
            logger.exception("[external] tool registration failed for %r", kind)


def available_tools(*, tenant_id: Optional[str], actor_user_id: str,
                    connection_id: Optional[str] = None) -> List[ToolBinding]:
    """Bound tools the actor could reach, before execution authorization.

    A provider that raises is skipped rather than failing the whole listing: one
    unreachable type must not empty the actor's tool list.

    This is the *discovery* half of the pair, and it deliberately answers only
    "which capabilities exist for this tenant, bound to which connection". It
    does **not** apply the resource-execution authorization: that needs the
    identity service, and this function is reachable from layers that hold a
    connection store without holding one (the adapter providers, the migration
    tooling, the console's connection view). Applying it here would make the
    answer depend on ambient process state rather than on the argument, which is
    how a filter starts answering a different question than the caller asked.

    The authorization is applied where the *actor-facing* answer is built
    (:func:`integrations.external.adapters.mcp`'s providers stay discovery-only;
    ``external_tools_for`` and ``ConnectionRuntime.invoke`` apply it), so the
    model-visible list and the call agree while the discovery API stays honest.
    """
    load_providers()
    out: List[ToolBinding] = []
    taken: Dict[str, str] = {}
    for _kind, provider in providers().items():
        try:
            for binding in provider(tenant_id, actor_user_id) or ():
                if connection_id and binding.connection_id != connection_id:
                    continue
                seen = taken.get(binding.tool.name)
                if seen is not None and seen != binding.connection_id:
                    # Two connections claiming one name is the case the names
                    # are namespaced to make impossible; if it happens anyway,
                    # the model would hold one name for two capabilities and
                    # dispatch would answer with whichever connection it
                    # resolved first. Offering one of them is the smaller lie.
                    logger.warning(
                        "[external] connections %s and %s compose the same"
                        " tool name %r; the second is not offered",
                        seen, binding.connection_id, binding.tool.name)
                    continue
                taken[binding.tool.name] = binding.connection_id
                out.append(binding)
        except Exception:  # noqa: BLE001 - one type failing must not hide the rest
            continue
    return out


def authorized_tools(*, tenant_id: Optional[str], actor_user_id: str,
                     identity: Any = None, agent_id: str = "",
                     connection_id: Optional[str] = None) -> List[ToolBinding]:
    """The subset of :func:`available_tools` the actor may actually execute.

    ``identity`` is passed in rather than looked up globally so the caller
    decides which database the grant is read from — the same reason
    ``ConnectionRuntime`` reaches its identity service through the service it
    was built over. A caller that has no identity service gets an empty list:
    without grant evidence the only safe answer about what may be *executed* is
    "nothing", and an empty list is not a lie (the actor may hold no grant).
    """
    if identity is None:
        identity = _identity_service()
    bindings = available_tools(tenant_id=tenant_id, actor_user_id=actor_user_id,
                               connection_id=connection_id)
    if identity is None:
        return []
    out: List[ToolBinding] = []
    for binding in bindings:
        if authorization.may_execute(
                identity, actor_user_id=actor_user_id, tenant_id=tenant_id,
                kind=binding.tool.kind, action=binding.tool.action,
                scope=binding.scope, agent_id=agent_id,
                connection_id=binding.connection_id):
            out.append(binding)
    return _assigned_to_agent(out, identity=identity, tenant_id=tenant_id,
                              agent_id=agent_id)


def _assigned_to_agent(bindings: List[ToolBinding], *, identity: Any,
                       tenant_id: Optional[str],
                       agent_id: str) -> List[ToolBinding]:
    """Drop the connections this trusted Agent context is not assigned to.

    The rule itself lives in :mod:`integrations.external.assignment`; this is
    only the batch plumbing, so the projection and the final dispatch cannot
    disagree. A binding that cannot be *proven* allowed is dropped — an
    unknown row, a missing state and an unreadable store all answer "no", which
    is what stops a lookup failure from widening the list (spec: 缺状态/查询异常
    拒绝). Personal mail is outside the regime and is returned untouched.
    """
    store = getattr(identity, "_store", None)
    if store is None:
        # Nothing to ask. This is the discovery layer without a connection
        # store; the pre-existing authorization is still the only gate, which
        # is the behaviour a build with no assignment state has always had.
        return list(bindings)
    try:
        logical = assignment.logical_ids_for(
            store, [b.connection_id for b in bindings])
    except Exception:  # noqa: BLE001 - an unreadable store proves nothing
        logical = {}
    out: List[ToolBinding] = []
    for binding in bindings:
        if not assignment.call_regime_applies(
                binding.tool.kind, binding.scope, tenant_id):
            out.append(binding)
            continue
        logical_id = logical.get(str(binding.connection_id))
        if not logical_id:
            continue
        try:
            allowed, _reason = assignment.assignment_allows(
                store, tenant_id=tenant_id, logical_id=logical_id,
                agent_id=agent_id)
        except Exception:  # noqa: BLE001 - a failed lookup is a refusal
            continue
        if allowed:
            out.append(binding)
    return out


def _identity_service():
    """The identity service the grant is read from, or ``None``.

    Reached through the *connection* service — the same one the built-in
    providers read ``external_connections`` from
    (``integrations.external.adapters.mcp._mcp_service``) — rather than through
    ``auth.service.get_identity_service()``. Two reasons:

    * the grants and the connections must come from one database. Reading them
      from two accessors is how "the connection is there but its grant is not"
      starts happening, and which of the two wins would depend on deployment
      configuration rather than on policy.
    * the connection service is the accessor a caller (or a test) can point at
      its own store without also re-pointing the whole identity layer.
    """
    try:
        from integrations.external.service import (
            get_external_connection_service)
        return get_external_connection_service()._identity  # noqa: SLF001
    except Exception:  # noqa: BLE001 - no service, no grant evidence
        return None


def tool_projection(*, tenant_id: Optional[str], actor_user_id: str,
                    agent_id: str = "", identity: Any = None
                    ) -> List[Dict[str, Any]]:
    """The shape the identity layer's tool projection consumes.

    The *actor-facing* projection, so it is the authorized subset rather than
    bare discovery: a capability the caller may not execute must not appear in a
    list a console or a model reads as "what you can use". It went through
    :func:`available_tools` and forwarded ``agent_id`` to a function that does
    not accept it, so it raised for every caller — and nothing called it, which
    is why the wrong answer stayed invisible. It now asks the same question the
    dispatch path asks, with the same arguments.

    ``identity`` selects the store the grant is read from; omitted, the
    connection service's own identity is used, which is the one the built-in
    providers read the connections from.
    """
    return [binding.as_dict()
            for binding in authorized_tools(
                tenant_id=tenant_id, actor_user_id=actor_user_id,
                identity=identity, agent_id=agent_id)]


def declared_tool_projection() -> List[Dict[str, Any]]:
    """Every external capability this build declares, without a connection.

    This is what the *grant catalogue* needs: a stable list of grantable
    resource ids that does not depend on which connections happen to exist for
    one tenant, so an administrator can grant a capability before a connection
    is created and the grant does not flicker as connections come and go.
    Whether a grant can actually be exercised is decided per call by
    :func:`dispatch` through :class:`ConnectionRuntime`.
    """
    from integrations.external.adapters.base import adapter_for, registered_kinds

    out: List[Dict[str, Any]] = []
    for kind in registered_kinds():
        try:
            adapter = adapter_for(kind)
        except Exception:  # noqa: BLE001
            continue
        for action in sorted(adapter.actions):
            tool = ExternalTool(
                name="%s.%s" % (kind, action), kind=kind, action=action,
                write=action in adapter.write_actions)
            out.append(tool.as_projection(""))
    return out


def find_binding(name: str, *, tenant_id: Optional[str],
                 actor_user_id: str) -> Optional[ToolBinding]:
    for binding in available_tools(tenant_id=tenant_id,
                                   actor_user_id=actor_user_id):
        if binding.tool.name == name:
            return binding
    return None


#: Provider-supplied dispatcher: (service, binding, params, context) ->
#: InvokeResult. Registered per kind so the type owns its own action semantics.
Dispatcher = Callable[..., Any]

_DISPATCHERS: Dict[str, Dispatcher] = {}
_DISPATCH_LOCK = threading.Lock()


def register_dispatcher(kind: str, dispatcher: Dispatcher) -> None:
    kind = str(kind or "").strip()
    if not kind:
        raise ValueError("a dispatcher needs a kind")
    with _DISPATCH_LOCK:
        _DISPATCHERS[kind] = dispatcher


def dispatcher_for(kind: str) -> Optional[Dispatcher]:
    with _DISPATCH_LOCK:
        return _DISPATCHERS.get(kind)


def dispatch(service, name: str, params: Mapping[str, Any], *,
             tenant_id: Optional[str], actor_user_id: str,
             agent_id: str = "", run_id: str = "",
             approval: Optional[Mapping[str, Any]] = None):
    """Run one external tool by name, re-authorizing at call time.

    Raises :class:`~integrations.external.errors.ExternalConnectionError` when
    the tool is not available to this actor now — which is the answer when a
    permission was revoked between listing and calling, not a silent no-op.
    """
    binding = find_binding(name, tenant_id=tenant_id,
                           actor_user_id=actor_user_id)
    if binding is None:
        # Discovery, not authorization: this answers "no such capability for
        # this actor", which is also the honest answer for a capability the
        # actor holds no grant for — naming it would turn the refusal into a
        # discovery oracle. The grant itself is checked one layer down, in
        # ``ConnectionRuntime.invoke``, which is what every dispatcher reaches.
        raise not_found("tool %r is not available to this caller" % name)
    dispatcher = dispatcher_for(binding.tool.kind)
    if dispatcher is None:
        raise forbidden("no dispatcher is registered for %r"
                        % binding.tool.kind, code="no_dispatcher")
    return dispatcher(service, binding, params, tenant_id=tenant_id,
                      actor_user_id=actor_user_id, agent_id=agent_id,
                      run_id=run_id, approval=approval)


def reset() -> None:
    """Drop providers and dispatchers. For tests."""
    with _PROVIDER_LOCK:
        _PROVIDERS.clear()
    with _DISPATCH_LOCK:
        _DISPATCHERS.clear()
