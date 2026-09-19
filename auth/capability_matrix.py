# encoding:utf-8
"""The single server-side registry of what is open, and why.

Why one module
--------------
Before this, "is this capability open?" was answered in three unrelated places
that could disagree:

* ``channel/web/route_registry.py`` — a ``closed`` policy made the HTTP gate
  answer 503 before the handler ran;
* ``IdentityService._consumer_availability()`` — a static dict the console reads
  to decide whether to render a page or an "unavailable" notice;
* the console's own page/verb projection and the per-slice runtime switches
  (``personal_runtime_enabled`` and friends).

A slice could therefore be reachable but reported closed, or rendered open and
answer 503. The measured case is scheduler management: route ``closed``,
consumer ``deferred``, and a Web page that could not say which.

What this declares
------------------
One :class:`Slice` per recovered capability, carrying:

``implemented``  the code path exists in this build;
``accepted``     the real acceptance evidence for the classes this slice
                 *actually declares* exists (an end-to-end run through the real
                 dispatch seam, a real provider scan, a real client). A unit test
                 that calls the same authorization helper is not acceptance.

                 Scoping matters, because the word alone is ambiguous: a slice
                 that declares only ``read``/``config`` actions is claiming
                 acceptance for those, and nothing about a consequence class it
                 does not serve -- ``weixin_scan`` is the case in point, serving
                 the scan *configuration* pipeline while its execution class is
                 withheld by the runtime switches. The other half of the rule is
                 mechanical rather than a claim: an ``execute`` action may not be
                 declared at all unless the slice is accepted
                 (:data:`_EXECUTE_REQUIRES_ACCEPTANCE`, enforced by
                 :func:`check_consistency`), so "accepted but unproven execution"
                 cannot be declared by accident.
``open``         the actions currently served, as ``action -> access`` where
                 access is ``read``, ``config`` or ``execute``.

The route table, the consumer projection and the page projection all read this
declaration, so "route open but projection closed" and "projection open but
route 503" are impossible by construction, and the remaining disagreement is a
missing declaration rather than a silent drift. :func:`check_consistency`
audits that, and the test suite calls it.

Deliberately not a permission service
-------------------------------------
Nothing here grants access. Every handler still resolves the request context and
enforces owner/tenant/resource authorization per request; this only decides
whether the *feature* is served at all, and reports the reason when it is not.
"""

from __future__ import annotations

import os
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

#: The three access classes, ordered from least to most consequential.
ACCESS_READ = "read"
ACCESS_CONFIG = "config"
ACCESS_EXECUTE = "execute"

ACCESS_CLASSES: Tuple[str, ...] = (ACCESS_READ, ACCESS_CONFIG, ACCESS_EXECUTE)

#: The HTTP policy a slice's route carries while the action is open.
DEFAULT_POLICY = "tenant"
DEFAULT_PERSONAL_POLICY = "personal"


class Slice:
    """One recovered capability and the actions currently served for it."""

    __slots__ = ("id", "capability", "consumer", "page", "scope", "open",
                 "declared_open", "implemented", "accepted", "reason", "policy",
                 "permission")

    def __init__(self, id: str, *, capability: str, consumer: str,
                 page: Optional[str], scope: FrozenSet[str],
                 open: Mapping[str, str], implemented: bool, accepted: bool,
                 reason: str = "", policy: str = DEFAULT_POLICY,
                 permission: str = "") -> None:
        self.id = id
        self.capability = capability
        self.consumer = consumer
        self.page = page
        self.scope = frozenset(scope)
        #: What the declaration serves before the deployment narrows it.
        #: ``RDAI_DISABLED_ACTIONS`` only ever *removes* from this map; keeping
        #: it separate makes :func:`finalize` idempotent instead of cumulative.
        self.declared_open = dict(open)
        self.open = dict(open)
        self.implemented = bool(implemented)
        self.accepted = bool(accepted)
        self.reason = reason
        self.policy = policy
        self.permission = permission

    @property
    def enabled(self) -> bool:
        """Whether any action of this slice is served at all."""
        return bool(self.open)

    def is_open(self, action: str) -> bool:
        return action in self.open

    def access(self, action: str) -> Optional[str]:
        return self.open.get(action)

    def route(self, action: str, *, policy: Optional[str] = None,
              permission: Optional[str] = None,
              comment: str = "") -> Dict[str, Any]:
        """The route-registry policy entry for one action.

        An action that is not open becomes ``closed``: the gate then refuses
        before any handler runs, which is the only refusal that cannot be
        bypassed by a handler forgetting its own check.
        """
        if not self.is_open(action):
            return _closed_entry(comment or "%s: %s not open"
                                 % (self.id, action))
        entry: Dict[str, Any] = {
            "policy": policy or self.policy,
            "comment": comment or "%s: %s (%s)" % (self.id, action,
                                                   self.open[action]),
        }
        permission = self.permission if permission is None else permission
        if permission:
            entry["permission"] = permission
        return entry

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "capability": self.capability,
            "scope": sorted(self.scope),
            "implemented": self.implemented,
            "accepted": self.accepted,
            "open": dict(self.open),
            "reason": self.reason,
        }


def _closed_entry(reason: str) -> Dict[str, Any]:
    return {"policy": "closed", "comment": reason}


#: Actions that must not be served before the slice has real acceptance.
_EXECUTE_REQUIRES_ACCEPTANCE = True

#: The declaration. Every slice starts closed and is opened by the task that
#: lands its implementation *and* its acceptance evidence; a slice left closed
#: here is reported as such by the route gate, the consumer projection and the
#: page projection at the same time.
SLICES: Tuple[Slice, ...] = (
    Slice(
        "scheduler",
        capability="database-scheduler-console",
        consumer="scheduler",
        page="workbench.schedules",
        scope=frozenset({"personal", "tenant"}),
        # list/read, config (toggle/update/delete) and execute (run) are
        # separate classes on purpose: a member must be able to pause or delete
        # their own task even when execution itself is refused.
        #
        # Opened by tasks 3.7 and 4.3: the five management methods now share one
        # authorization service across the Web handlers, the Agent tool and the
        # background loop (``agent/tools/scheduler/authorization.py``), the writes
        # go through the store's revision guard and write lease, and the
        # historical-task migration has run. The acceptance behind it is R1
        # (``tests/test_scheduler_tool_dispatch.py``: the six actions reached
        # through ``ToolManager`` under a real identity database, including the
        # non-owner admin, the revoked member, the quota and the cross-tenant
        # refusals), 3.6 (``tests/test_scheduler_web_transport.py``) and 4.3
        # (``tests/test_scheduler_task_migration.py``).
        open={
            "list": ACCESS_READ,
            "toggle": ACCESS_CONFIG,
            "update": ACCESS_CONFIG,
            "delete": ACCESS_CONFIG,
            "run": ACCESS_EXECUTE,
        },
        implemented=True,
        accepted=True,
        reason="",
        # ``tenant`` (not ``personal``): a task belongs to one tenant, and the
        # registry's slice declares no route-level permission, so the gate
        # resolves the session *and* the tenant selection and then hands the
        # context to the handler -- which is exactly what the handlers ask for
        # themselves (``_require_context(require_tenant=True)``). A ``personal``
        # route would ignore the tenant header at the gate and leave the handler
        # to reject the request, i.e. two answers to one question.
        policy=DEFAULT_POLICY,
    ),
    Slice(
        "memory_browse",
        capability="database-memory-console",
        consumer="memory_console",
        page="admin.memory",
        scope=frozenset({"personal", "tenant"}),
        # Opened by task 5.1-5.3: the two legacy reads are served by
        # ``channel/web/memory_console.py`` as a scope-adapted compatibility
        # surface over the delivered personal memory service — an explicit
        # ``personal`` | ``private_agent`` | ``shared`` target, the owner check
        # *before* the read, and a stable machine code (never a 200 carrying an
        # error message) for every refusal. The member's *own* memory writes
        # stay where they already are (``POST /api/memory/personal``); the
        # Agent-domain edit/delete/clear verbs declared below are the second
        # half of task 5.1.
        #
        # Acceptance: tests/test_memory_console_scope.py drives the real
        # ``build_web_app()`` application — the member's own personal list/read,
        # the personal-scope-only fallback refusal, a same-tenant other member
        # and a tenant administrator refused on a private Agent *with the
        # memory service replaced by a tripwire* (refuse-before-read), a
        # foreign-tenant selection and a second membership refused,
        # unknown scope/category/entry refused, the symlink cases, and the
        # regression of the already-open ``/api/memory/personal*`` endpoints.
        # The write verbs are covered by ``tests/test_memory_console_write.py``
        # (owner/admin allowed, other member and read-only categories refused
        # with the mutation never reached, revision conflicts, and the
        # tombstone masking that keeps a deleted entry out of retrieval).
        open={"list": ACCESS_READ, "content": ACCESS_READ,
              # Task 5.1 second half: the same page's edit/delete/clear. They
              # are ``config``, not ``execute``, following this registry's own
              # convention: an action that writes/adjusts a resource is config
              # (channel creation, a skill edit), while ``execute`` is reserved
              # for actions that *run* something (a tool import). Editing memory
              # writes stored data; nothing about the page starts running — and
              # marking it execute would tell the console an execution surface
              # had opened on a page that only edits files.
              #
              # One access class for all three, because the *authorization* is
              # not a permission id: it is the caller's range on the resolved
              # target (owner for a private Agent, tenant-administration
              # qualification for a shared one), re-checked on every request by
              # ``memory_console.resolve_target`` before any mutation. No stored
              # ``memory.write`` is introduced — ``knowledge.write`` was
              # deliberately retired in favour of management qualification, and
              # the same mistake here would make a role grant able to widen
              # whose memory may be rewritten.
              "save": ACCESS_CONFIG, "delete": ACCESS_CONFIG,
              "clear": ACCESS_CONFIG},
        implemented=True,
        accepted=True,
        reason="",
        policy=DEFAULT_POLICY,
        permission="memory.read",
    ),
    Slice(
        "project_browse",
        capability="scoped-project-browser",
        consumer="project_browse",
        page=None,
        scope=frozenset({"personal"}),
        # ``browse`` is the personal-root directory listing. Entry is the current
        # tenant+user's own projects root, identifiers are relative to it, and
        # every read and selection re-resolves the real path through
        # ``common.safe_fs`` against the ambient identity -- there is no
        # ``agent.read``/platform-``all``/string-prefix bypass, no absolute path
        # is returned, and the owner check happens before the directory is read
        # (the module-level ``session_owner`` re-check covers selection time).
        #
        # Acceptance: tests/test_scoped_project_browse.py drives the real
        # ``build_web_app()`` application — the owner's own root, a same-tenant
        # other member, a non-owner tenant administrator and a cross-tenant
        # caller refused, ``..``/absolute/encoded-separator/drive-letter input
        # refused, the symlink swapped in between listing and selection refused,
        # the member-private directory nested in the platform root never exposed,
        # and the historical absolute path accepted inside the root and refused
        # outside.
        #
        # ``import`` is the controlled local-directory copy path (task 6.3/6.4),
        # and its access class is deliberately different from ``browse``: it
        # writes into the member's root. Four conditions are required before any
        # path the client named is resolved -- loopback, the per-start token, the
        # verified database identity, and the single-use handle the preview
        # issued for that exact source -- so a browser-supplied absolute path
        # with a session (or the token) alone is refused with ``handle_required``.
        # Publishing goes through staging + one rename with quota pre-reservation
        # and failure compensation, so nothing half-imported becomes selectable.
        #
        # Acceptance (execute action, so acceptance is required): the same
        # tests/test_scoped_project_browse.py drives the real ``build_web_app()``
        # application for preview target/counts/conflict, the missing-token,
        # remote-peer, forwarded-header and missing/foreign/replayed-handle
        # refusals, the atomic single-use publish, the never-overwrite and
        # changed-source refusals, quota refusal before staging, cancel, mid-flight
        # cancel rollback, failed-publish compensation, and the multipart upload
        # transport (manifest traversal refused, no server path interpreted, and
        # identity/origin still required). What it does *not* verify is the
        # desktop native picker itself, which lives outside this repository; see
        # evidence/6-scoped-project-browser.md.
        open={"browse": ACCESS_READ, "import": ACCESS_EXECUTE},
        implemented=True,
        accepted=True,
        reason="",
        policy=DEFAULT_POLICY,
    ),
    Slice(
        "weixin_scan",
        capability="channel-scan-onboarding",
        consumer="weixin_qr",
        page=None,
        # Narrowed to what the handler actually serves. ``platform`` is *not*
        # open: a channel instance always belongs to a tenant, and there is no
        # service path that creates one outside a tenant, so a platform-scope
        # scan is refused with an explicit reason (``scope_not_supported``)
        # instead of fabricating a tenant ownership for a platform admin.
        scope=frozenset({"tenant", "personal"}),
        # ``GET`` mints a per-initiator scan session and one provider QR;
        # ``POST`` advances it (poll / refresh / cancel) and commits the
        # instance through the identity service's own write transaction. Both
        # are the *configuration* class: the route-level action is ``poll`` for
        # the whole POST, whose body action is one of those four.
        open={"qr": ACCESS_CONFIG, "poll": ACCESS_CONFIG},
        # The configuration pipeline exists and is exercised end to end over the
        # real WSGI app in tests/test_weixin_qr_flow.py (session binding, one-time
        # authorization, receipt idempotency, atomic commit, refusals).
        implemented=True,
        # Acceptance here is for the *configuration* class this slice serves, and
        # nothing in it claims the execution class: the vendor connection is
        # gated by ``personal_runtime_enabled()`` (deployment switch, off by
        # default) plus the per-type ``PERSONAL_RUNTIME_ACCEPTED_TYPES`` record
        # (empty). Tasks 7.8 (a real provider scan, a real connection and real
        # send/receive) and 7.10 (opening the accepted types) are NOT verified;
        # see evidence/7-scan-onboarding.md. Task 7.9 *is* delivered -- the
        # applicable action-approval consumer is wired at the two real dispatch
        # seams (agent/approval_gate.py, consumed by the Agent tool dispatch and
        # the scheduler's outbound delivery), with the deployment's
        # ``approval_required_actions`` empty by default. No ``execute`` action is
        # declared here, so an unverified execution cannot be reached through this
        # slice.
        accepted=True,
        reason="",
        policy=DEFAULT_POLICY,
    ),
    Slice(
        "desktop_tenant_context",
        capability="desktop-tenant-context",
        consumer="desktop_enterprise",
        page=None,
        scope=frozenset({"personal", "tenant", "platform"}),
        # No business action of its own: the native flow's two routes are
        # authenticated by the code + PKCE verifier pair (or by the ordinary Web
        # Cookie on the consent page), never by a route-level tenant/permission
        # gate, so they are registered as ``public`` in the route table and the
        # server re-authorizes every request they lead to. An empty ``open`` map
        # therefore keeps the consumer closed rather than silently enabling it.
        open={},
        # The backend half of the protocol (login compatibility, consent,
        # 60-second single-use code, atomic PKCE exchange, revocation) is
        # implemented and covered by tests/test_desktop_auth_flow.py, and the
        # main-process broker / narrowed IPC / renderer context seam exist in
        # ``desktop/``. Task 8.7 -- the drill that drives a real packaged Desktop
        # client over two tenants, two members, an expired identity and a
        # reconnect -- has NOT been performed, so this is implemented but not
        # accepted, and no "permanent deferred" wording is left behind.
        #
        # The acceptance is carried by ``complete-desktop-and-scan-real-acceptance``
        # (tasks 3.1-3.3), which is where this capability's spec delta now lives:
        # ``complete-database-capability-parity`` was archived partially on
        # 2026-09-15 with only the accepted slices merged into the main specs, so
        # opening this one requires that change's real-client evidence, not this
        # comment being outdated.
        implemented=True,
        accepted=False,
        reason="awaiting_acceptance",
    ),
    Slice(
        "external_connections",
        capability="external-connection-management",
        consumer="external_connections",
        # The console page (task group 10 of ``add-external-system-access``): the
        # "模型与接入 / 外部系统接入" entry, registered as the view
        # ``external_connections`` in ``console.js`` and served by
        # ``static/js/external-connections.js``.
        #
        # The id is signed rather than left empty so the entry is gated like every
        # other admin page: ``_viewNavDenied`` and the sidebar gate read
        # ``console_pages['admin.external_connections']``, and a page the registry
        # does not sign is deliberately left *as-is* — visible to an identity with
        # no read grant, and reachable by direct URL. Not signing it would make the
        # projection silent about a page that exists.
        page="admin.external_connections",
        scope=frozenset({"platform", "tenant", "personal"}),
        # Read, config, and the two runtime-facing reads the page needs.
        #
        # ``test`` and ``runtime`` are declared here as *route* actions, which is
        # what lets the page ask for a test at all. Whether a test actually runs
        # is a second, independent gate: ``registry.open_classes(kind)`` reads the
        # deployment's ``external_connections.readiness`` block (default closed)
        # and ``service.probe_connection`` refuses with ``test_not_available``
        # when the type's class is shut. Declaring the route therefore does not
        # open anything — it only stops the page from being unable to ask, so the
        # console can render the deployment's real reason instead of a missing
        # button. ``execute`` is deliberately NOT declared: a business action has
        # no HTTP entry point at all, it goes through the tool/runtime path where
        # the risk catalogue and the approval binding live.
        open={
            "catalog": ACCESS_READ,
            "types": ACCESS_READ,
            "detail": ACCESS_READ,
            "create": ACCESS_CONFIG,
            "update": ACCESS_CONFIG,
            "delete": ACCESS_CONFIG,
            # The tenant's default ERP pointer and a platform connection's tenant
            # grant list are both *configuration*: they change what a consumer
            # resolves, they do not run anything.
            "erp_default": ACCESS_CONFIG,
            "tenant_access": ACCESS_CONFIG,
            # A bounded connectivity/authentication probe and the runtime limits
            # projection. Both are reads of the external system's state, not
            # writes to it, and both are refused by the service when the
            # deployment has not opened the type's test class.
            "test": ACCESS_READ,
            # Testing an unsaved form. Declared as a route action of the same
            # class as ``test`` — it is the same bounded probe against the same
            # deployment switch — and the route demands the *manage* permission
            # because a draft has no saved row whose ownership could authorize
            # it, so the caller must be the one entitled to define connections.
            "draft_test": ACCESS_READ,
            "runtime": ACCESS_READ,
        },
        # The control plane exists and is exercised over the real WSGI app in
        # ``tests/test_external_connections_api.py`` (session-derived ownership,
        # the tenant/permission and platform refusals, origin guard, version
        # conflicts, secret keep/replace/clear, platform-inheritance visibility
        # and the reference-blocked delete). That is acceptance for the read and
        # config classes this slice declares, and for nothing else — which is why
        # the storage/service half is covered separately by
        # ``tests/test_external_connection_schema.py`` and
        # ``tests/test_external_connection_service.py``.
        implemented=True,
        accepted=True,
        reason="",
        # ``personal`` by default: the page is a member surface (a member's own
        # mailbox) as much as an administrative one. The tenant and platform
        # addresses override the policy per route (see ``route_registry``), so a
        # tenant address still demands the read/manage permission and a platform
        # address still demands a platform admin.
        policy=DEFAULT_PERSONAL_POLICY,
        permission="external.connections.read",
    ),
    # -- Functional integration (change integrate-upstream-core-capabilities) --
    #
    # Eight *independent* slices, one action each, so a batch can be opened
    # action by action after its own real acceptance: the Web/Desktop context
    # controls and the six scheduler target/history interfaces ship on different
    # dates and MUST NOT share one switch (design D2, ``database-runtime-consumers``).
    #
    # They all start with ``open={}`` -- the code path exists, but the action is
    # not served, so the route gate answers 503 and ``/auth/context`` reports the
    # real reason. ``implemented=True, accepted=False`` is the honest state until
    # the batch's real entry acceptance (``tasks.md`` §8) has been performed; the
    # per-action projection is :func:`feature_action_availability`.
    #
    # The consumer name is the slice id, not a shared label: the declaration
    # requires one consumer per slice (``test_slice_ids_and_consumers_are_unique``),
    # and these eight actions open independently -- a shared consumer would make
    # "which entry point is live?" unanswerable from ``/auth/context.consumers``.
    Slice(
        "session_context_usage",
        capability="session-context-controls",
        consumer="session_context_usage",
        page=None,
        scope=frozenset({"personal"}),
        open={},
        implemented=True,
        accepted=False,
        reason="",
        policy=DEFAULT_POLICY,
    ),
    Slice(
        "session_context_compact",
        capability="session-context-controls",
        consumer="session_context_compact",
        page=None,
        scope=frozenset({"personal"}),
        open={},
        implemented=True,
        accepted=False,
        reason="",
        policy=DEFAULT_POLICY,
    ),
    Slice(
        "scheduler_instances",
        capability="database-scheduler-console",
        consumer="scheduler_instances",
        page=None,
        scope=frozenset({"personal", "tenant"}),
        open={},
        implemented=True,
        accepted=False,
        reason="",
        policy=DEFAULT_POLICY,
    ),
    Slice(
        "scheduler_recipients",
        capability="database-scheduler-console",
        consumer="scheduler_recipients",
        page=None,
        scope=frozenset({"personal", "tenant"}),
        open={},
        implemented=True,
        accepted=False,
        reason="",
        policy=DEFAULT_POLICY,
    ),
    Slice(
        "scheduler_create",
        capability="database-scheduler-console",
        consumer="scheduler_create",
        page=None,
        scope=frozenset({"personal", "tenant"}),
        open={},
        implemented=True,
        accepted=False,
        reason="",
        policy=DEFAULT_POLICY,
    ),
    Slice(
        "scheduler_runs_list",
        capability="database-scheduler-console",
        consumer="scheduler_runs_list",
        page=None,
        scope=frozenset({"personal", "tenant"}),
        open={},
        implemented=True,
        accepted=False,
        reason="",
        policy=DEFAULT_POLICY,
    ),
    Slice(
        "scheduler_runs_detail",
        capability="database-scheduler-console",
        consumer="scheduler_runs_detail",
        page=None,
        scope=frozenset({"personal", "tenant"}),
        open={},
        implemented=True,
        accepted=False,
        reason="",
        policy=DEFAULT_POLICY,
    ),
    Slice(
        "scheduler_runs_delete",
        capability="database-scheduler-console",
        consumer="scheduler_runs_delete",
        page=None,
        scope=frozenset({"personal", "tenant"}),
        open={},
        implemented=True,
        accepted=False,
        reason="",
        policy=DEFAULT_POLICY,
    ),
)

#: The per-action features projected through ``/auth/context.feature_actions``.
#: ``(public key, slice id, action)`` -- one entry per independently gated verb.
FEATURE_ACTIONS: Tuple[Tuple[str, str, str], ...] = (
    ("session_context.usage", "session_context_usage", "usage"),
    ("session_context.compact", "session_context_compact", "compact"),
    ("scheduler.instances", "scheduler_instances", "instances"),
    ("scheduler.recipients", "scheduler_recipients", "recipients"),
    ("scheduler.create", "scheduler_create", "create"),
    ("scheduler.runs.list", "scheduler_runs_list", "list"),
    ("scheduler.runs.detail", "scheduler_runs_detail", "detail"),
    ("scheduler.runs.delete", "scheduler_runs_delete", "delete"),
)

#: Environment key that may only *close* actions this build already serves.
DISABLED_ACTIONS_ENV = "RDAI_DISABLED_ACTIONS"

#: The stable public keys, for validation and client parity checks.
FEATURE_ACTION_KEYS: FrozenSet[str] = frozenset(k for k, _, _ in FEATURE_ACTIONS)

#: The deployment's parsed closure, filled by :func:`finalize` at import time.
_disabled_actions: FrozenSet[str] = frozenset()


class CapabilityConfigurationError(ValueError):
    """A deployment action list that cannot be honoured (unknown key)."""


def parse_disabled_actions(raw: Optional[str]) -> FrozenSet[str]:
    """Parse ``RDAI_DISABLED_ACTIONS`` and refuse unknown keys.

    A typo must not silently leave a feature open: the shutdown switch is the
    incident-response tool, so a key the registry does not declare is a
    configuration error at startup rather than a no-op.
    """
    keys = frozenset(part.strip() for part in (raw or "").split(",")
                     if part.strip())
    unknown = sorted(keys - FEATURE_ACTION_KEYS)
    if unknown:
        raise CapabilityConfigurationError(
            "unknown %s entries: %s" % (DISABLED_ACTIONS_ENV, ", ".join(unknown)))
    return keys


def finalize(disabled: Optional[FrozenSet[str]] = None) -> None:
    """Apply deployment-level closure to the declaration (idempotent).

    Recomputes every slice's ``open`` from its ``declared_open`` minus the
    disabled keys, so calling it twice -- or calling it after a test opened a
    slice -- cannot accumulate. ``implemented``/``accepted`` are untouched: the
    switch narrows what is served, it never claims acceptance.
    """
    global _disabled_actions
    _disabled_actions = frozenset(disabled or ())
    # The switch speaks the *public* action keys (``scheduler.create``), which is
    # the same vocabulary ``/auth/context`` and the clients use -- not the
    # internal ``slice_id.action`` pair, which would make an operator's first
    # guess wrong and a typo indistinguishable from a misspelling of a real key.
    disabled_pairs = {(slice_id, action)
                      for key, slice_id, action in FEATURE_ACTIONS
                      if key in _disabled_actions}
    for spec in SLICES:
        spec.open = {action: access
                     for action, access in spec.declared_open.items()
                     if (spec.id, action) not in disabled_pairs}


def disabled_actions() -> FrozenSet[str]:
    """The deployment closure currently in force (for diagnostics/tests)."""
    return _disabled_actions


def _action_reason(spec: Slice, action: str) -> str:
    """Why an action is unavailable, in the spec'd precedence order."""
    if not spec.implemented:
        return "not_implemented"
    if not spec.accepted:
        return "not_accepted"
    if not spec.is_open(action):
        return "disabled_by_deployment"
    return ""


def feature_action_availability() -> Dict[str, Dict[str, Any]]:
    """The eight per-action availability entries for ``/auth/context``.

    ``available`` describes the *service*, never the caller's rights: a
    ``True`` here only means the client may ask, and the handler still resolves
    tenant/owner/resource authorization on every request (design D2).
    """
    out: Dict[str, Dict[str, Any]] = {}
    for key, slice_id, action in FEATURE_ACTIONS:
        spec = slice_for(slice_id)
        reason = _action_reason(spec, action)
        out[key] = {"available": reason == "", "reason": reason}
    return out


finalize(parse_disabled_actions(os.environ.get(DISABLED_ACTIONS_ENV)))


_BY_ID: Dict[str, Slice] = {s.id: s for s in SLICES}

def slice_for(slice_id: str) -> Slice:
    try:
        return _BY_ID[slice_id]
    except KeyError:
        raise KeyError("unknown capability slice: %r" % slice_id) from None


def route(slice_id: str, action: str, **kwargs) -> Dict[str, Any]:
    """Convenience wrapper used by ``channel/web/route_registry.py``."""
    return slice_for(slice_id).route(action, **kwargs)


def consumer_availability() -> Dict[str, Dict[str, Any]]:
    """The availability entries this registry owns.

    Merged into ``IdentityService._consumer_availability()`` rather than
    replacing it: consumers that predate this registry keep their own entry, and
    everything declared here reports from one place.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for spec in SLICES:
        if spec.enabled:
            out[spec.consumer] = {"available": True, "reason": ""}
        else:
            # No registry ``reason`` is set for the action slices, so derive the
            # honest label rather than the generic "not_implemented": a new
            # action whose code exists but whose batch has not been accepted is
            # ``not_accepted``, and the console must not tell the operator a
            # feature is missing when it is merely not switched on yet.
            out[spec.consumer] = {
                "available": False,
                "reason": spec.reason or ("not_implemented"
                                          if not spec.implemented
                                          else "not_accepted"),
            }
    return out


def page_availability() -> Dict[str, Dict[str, Any]]:
    """Per-page availability for pages this registry owns.

    A page that any slice serves is available; the reason reported for a closed
    page is the slice's own reason, so the console never shows "deferred" where
    the real answer is "awaiting acceptance".
    """
    out: Dict[str, Dict[str, Any]] = {}
    for spec in SLICES:
        if not spec.page:
            continue
        entry = out.setdefault(spec.page, {"available": False, "reason": ""})
        if spec.enabled:
            entry["available"] = True
            entry["reason"] = ""
        elif not entry["available"]:
            entry["reason"] = spec.reason or "not_implemented"
    return out


def page_states(page_id: str) -> Dict[str, bool]:
    """Which access classes a registry-owned page actually serves.

    ``read``/``config``/``execute`` are reported separately because they are
    separate refusals: a member who may read their task list must keep it (and
    keep pausing or deleting their own task) even when *executing* one is
    refused, and a page whose execute class is not accepted must not present
    itself as a live surface.

    A page can be served by more than one slice, so the union is reported: the
    class is served when *any* slice offering that page serves it. An unknown
    page, like a page with no open slice, reports all three as false -- the
    projection never invents a capability.
    """
    states = {"read": False, "config": False, "execution": False}
    for spec in SLICES:
        if spec.page != page_id:
            continue
        for action, access in spec.open.items():
            if access == ACCESS_READ:
                states["read"] = True
            elif access == ACCESS_CONFIG:
                states["config"] = True
            elif access == ACCESS_EXECUTE:
                states["execution"] = True
    return states


def check_consistency() -> List[str]:
    """Audit the declaration against itself; empty list means consistent.

    These are the invariants a future edit is most likely to break, and each one
    has a real failure mode behind it:

    * an open action on an unimplemented slice would serve a route with no code;
    * an open ``execute`` action without acceptance would run side effects on the
      strength of a unit test;
    * an unknown access class would be silently treated as read.
    """
    problems: List[str] = []
    for spec in SLICES:
        for action, access in sorted(spec.open.items()):
            if access not in ACCESS_CLASSES:
                problems.append("%s.%s has unknown access class %r"
                                % (spec.id, action, access))
            if not spec.implemented:
                problems.append("%s.%s is open but the slice is not implemented"
                                % (spec.id, action))
            if (access == ACCESS_EXECUTE and _EXECUTE_REQUIRES_ACCEPTANCE
                    and not spec.accepted):
                problems.append(
                    "%s.%s is an execute action but the slice has no real "
                    "acceptance evidence" % (spec.id, action))
            if (spec.scope and spec.scope <= {"platform"}
                    and access == ACCESS_EXECUTE):
                problems.append(
                    "%s.%s is a platform execute action; platform slices do not "
                    "own business execution" % (spec.id, action))
    return problems


def all_slices() -> List[Dict[str, Any]]:
    """The declaration in JSON-ready form, for diagnostics and tests."""
    return [spec.as_dict() for spec in SLICES]
