# encoding:utf-8
"""The controlled call point between the control plane and an adapter.

Everything an adapter needs is assembled here, from a connection row that the
service already authorized, and nothing else. That is deliberate: it means an
adapter cannot reach a connection the caller was not authorized for, cannot
pick its own secret, and cannot choose its own network policy.

Three entry points, mirroring the adapter contract plus the recording the spec
requires:

``describe(connection_id)``
    Capability/limits projection for one connection, without any I/O. Used by
    the detail view and by the page's disabled-state reasons.
``probe(...)``
    A bounded connectivity/authentication test. The result is bound to the
    connection's ``version`` and the secret versions it read, is persisted as
    a redacted summary, and is audited. A late result is *rejected*, not
    written onto a newer configuration ("结果绑定版本...过期不变量").
``invoke(...)``
    A real business call, with the risk catalogue and approval binding applied
    before the adapter is reached.

Why the version check is at write time rather than at read time
--------------------------------------------------------------
A probe can take seconds; the console can save the form while it is in
flight. Checking the version when the result comes back, inside the same
transaction that writes it, is the only way the stored summary is guaranteed
to describe the configuration the user is looking at.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, replace
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from integrations.external import assignment, authorization, maintenance, registry
from integrations.external.adapters import (
    AdapterError,
    CapabilityReport,
    ExecutionContext,
    InvokeResult,
    NetworkPolicy,
    ProbeResult,
    STAGE_CONFIG,
    STAGE_INTERNAL,
    STAGE_POLICY,
    current_policy,
    invoke_failed,
    pool_for_deployment,
    run_probe,
)
from integrations.external.adapters import pool as pool_module
from integrations.external.adapters.base import adapter_for
from integrations.external.errors import conflict, forbidden, invalid, not_found

#: Overall test outcomes, matching the persisted ``result`` column.
TEST_OK = "ok"
TEST_PARTIAL = "partial"
TEST_FAILED = "failed"

#: What a scrubbed credential becomes in a stored summary. Named rather than
#: blank so a reader can tell redaction happened and did not simply lose text.
REDACTED = "[redacted]"

#: Shortest value worth scrubbing. A one- or two-character "secret" is both
#: implausible as a credential and impossible to remove without mangling every
#: other field, so the floor keeps the scrubber from corrupting the summary.
_SCRUB_MIN_LENGTH = 3

#: The single refusal text for "you may not see this connection".
#:
#: A connection that does not exist and a connection that belongs to somebody
#: else answer with this exact string, code and type, so a caller cannot
#: enumerate other members' personal connections by watching which ids produce
#: a different refusal. Sharing the constant is what keeps the two answers from
#: drifting apart as either call site is edited.
_NOT_FOUND_MESSAGE = "connection not found"


def _scrub_secrets(text: str, secrets: Sequence[str]) -> str:
    """Remove the given values from ``text`` before it is persisted.

    A blunt substring replacement, deliberately: exact-match removal would miss
    the shape that actually leaks — an error message built by interpolating a
    credential into a sentence — and the values here come from the credential
    store, so a false positive would mean some other stored secret happens to be
    a substring of the detail.
    """
    for value in secrets or ():
        value = str(value or "")
        if len(value) < _SCRUB_MIN_LENGTH:
            continue
        text = text.replace(value, REDACTED)
    return text


def _scrub_payload(value: Any, secrets: Sequence[str]) -> Any:
    """Recursively scrub a result payload of the values it was given.

    The draft path hands its result straight back to a caller, so the same
    reasoning as :func:`_scrub_secrets` applies: an adapter's own redaction is
    expected, and this keeps a forgetful one from echoing a one-shot credential
    back to the console that just typed it.
    """
    if not secrets:
        return value
    if isinstance(value, str):
        return _scrub_secrets(value, secrets)
    if isinstance(value, Mapping):
        return {k: _scrub_payload(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_payload(v, secrets) for v in value]
    return value


def _with_declared_secrets(runtime: "ConnectionRuntime",
                           snapshot: "ConnectionSnapshot",
                           actor_user_id: str,
                           already: Sequence[str]) -> List[str]:
    """The scrub list: what the adapter resolved, plus the type's other slots.

    The resolver sink already covers the normal path — an adapter obtains a
    credential through ``ctx.secret(...)`` and the value is captured there. This
    adds the connection's *remaining* declared slots so the guarantee does not
    rest on the adapter having used the intended path: a summary is scrubbed
    against every credential the connection holds.

    A slot that cannot be read is skipped rather than raised: a probe must not
    fail to record its result because some unrelated slot is unreadable, and an
    unreadable slot is by definition not a value that can be leaked into the
    text.
    """
    values = [str(v) for v in already or () if v]
    spec = registry.TYPE_SPECS.get(snapshot.kind)
    for slot in sorted(spec.secret_slots if spec else ()):
        try:
            value = runtime._service.resolve_secret(  # noqa: SLF001
                connection_id=snapshot.id, slot=slot, scope=snapshot.scope,
                tenant_id=snapshot.tenant_id, actor_user_id=actor_user_id)
        except Exception:  # noqa: BLE001 - absent slot, not a failure to record
            continue
        if value:
            values.append(str(value))
    return values



@dataclass(frozen=True)
class ConnectionSnapshot:
    """The immutable facts one attempt is built from.

    Read once, inside the authorizing transaction, so a concurrent update
    cannot change the configuration between the authorization decision and the
    secret read.
    """

    id: str
    kind: str
    scope: str
    tenant_id: Optional[str]
    owner_user_id: Optional[str]
    name: str
    config: Mapping[str, Any]
    version: int
    enabled: bool
    base_connection_id: Optional[str]
    #: How this row relates to a platform template: ``"platform"`` (the template
    #: itself, read as a platform administrator), ``"inherited"`` (a granted
    #: template a tenant consumer reached directly, with no override of its own),
    #: ``"override"`` (the tenant's own row standing in for a template) or
    #: ``"created"`` (a plain tenant/platform/personal row with no template).
    source: str = ""
    #: The platform template's id when ``source`` is ``inherited`` or ``override``.
    #: Empty otherwise. Named separately from :attr:`id` so an audit or a
    #: persistence record can say *which* platform row the effective one derives
    #: from when the effective row is the tenant's own.
    source_connection_id: str = ""
    #: The template's ``version`` at read time, so a platform edit is visible in
    #: whatever the caller records (``external_connection_tests`` stores the
    #: versions a result was produced under).
    source_version: int = 0


def slot_markers(rows) -> Dict[str, Dict[str, int]]:
    """Per-connection ``{slot: marker}`` from already-read secret-ref rows.

    ``rows`` are ``external_connection_secret_refs`` with ``connection_id``,
    ``slot``, ``secret_version``, ``credential_id``, ``platform_secret_id`` and a
    ``marker`` alias for ``COALESCE(credential_id, platform_secret_id, '')``.

    Split out of :meth:`ConnectionRuntime.secret_versions` so a page of cards can
    read every connection's markers in one query. The digest rule lives here and
    only here: two implementations of "did this credential move?" is how a badge
    and an approval end up disagreeing about whether a result still applies.
    """
    import hashlib

    out: Dict[str, Dict[str, int]] = {}
    for row in rows:
        slots = out.setdefault(str(row["connection_id"]), {})
        marker = str(row["marker"] or "")
        if not marker:
            slots[row["slot"]] = 0
            continue
        material = "%s@%s" % (marker, int(row["secret_version"] or 0))
        digest = hashlib.sha256(material.encode("utf-8")).digest()
        slots[row["slot"]] = int.from_bytes(digest[:4], "big")
    return out


class ConnectionRuntime:
    """Adapter-facing runtime built on an :class:`ExternalConnectionService`.

    Instantiated per request with the service the caller already has, so the
    runtime inherits the service's store, clock and audit sink rather than
    reaching for a global one.
    """

    def __init__(self, service) -> None:
        self._service = service
        self._store = service._store  # noqa: SLF001 - deliberate: same layer

    @property
    def _identity(self):
        """The identity service the authorization decisions run against.

        Reached through the connection service rather than from a module
        global so a runtime built over a test's store decides against *that*
        store's grants and memberships. A global lookup would silently answer
        from the process-wide database and make every authorization test pass
        for the wrong reason.
        """
        return self._service._identity  # noqa: SLF001 - deliberate: same layer

    # -- snapshot ------------------------------------------------------------

    def snapshot(self, connection_id: str, *,
                 tenant_id: Optional[str] = None) -> ConnectionSnapshot:
        """The facts one attempt is built from, with inheritance resolved.

        A tenant consumer must be able to reach a platform MCP template it was
        granted, and must stop being able to the moment that grant is revoked —
        including through an override it created earlier. Reading only the row
        that carries the id cannot express either: the template's own row has
        ``tenant_id IS NULL``, so the tenant-scoped lookup cannot see it at all,
        and the tenant's override row says nothing about whether the platform
        side is still available.

        So the resolution is: the tenant's own override of that template, else
        the granted template itself, else nothing. Whichever row wins, its
        availability is *ANDed* with the template's — a platform row that was
        disabled, un-granted or soft-deleted turns the effective snapshot
        ``enabled=False`` rather than falling back to the tenant's stale copy.
        That is the runtime half of 有效可用性仍与平台源 enabled/tenant-access 相与.
        """
        row = self._service._fetch_row(  # noqa: SLF001
            connection_id, tenant_id=tenant_id)
        if row is None:
            row = self._inherited_row(connection_id, tenant_id=tenant_id)
        if row is None:
            raise not_found(_NOT_FOUND_MESSAGE)
        try:
            config = json.loads(row["config_json"] or "{}")
        except (TypeError, ValueError) as exc:
            raise invalid("stored configuration is unreadable",
                          code="config_unreadable") from exc
        scope = row["scope"]
        source, source_id, source_version, source_available = (
            self._source_of(row, tenant_id=tenant_id))
        return ConnectionSnapshot(
            id=row["id"], kind=row["kind"], scope=scope,
            tenant_id=row["tenant_id"], owner_user_id=row["owner_user_id"],
            name=row["name"], config=config if isinstance(config, dict) else {},
            version=int(row["version"]),
            # A tenant row with a template behind it is only as usable as the
            # template: a revoked grant has to close the tenant's own override
            # too, or revoking a capability would leave the copy running.
            enabled=bool(row["enabled"]) and source_available,
            base_connection_id=row["base_connection_id"],
            source=source, source_connection_id=source_id,
            source_version=source_version)

    def _inherited_row(self, connection_id: str, *,
                       tenant_id: Optional[str]) -> Optional[Mapping[str, Any]]:
        """The tenant's effective row when it names a platform template's id.

        Two shapes, in the order the console presents them: the tenant's own
        override (``base_connection_id`` points at the template), else the
        granted template row itself. Returning the *override* here is what makes
        the template's id and the override's id interchangeable for a caller
        that only ever saw the template id — ``_cards`` publishes
        ``effective_id`` for the console, and this is the same rule for the
        runtime, so the two cannot disagree about which row wins.

        A tenant that was never granted the template gets ``None``, and the
        caller turns that into the same not-found a non-existent id gets: a
        tenant must not be able to enumerate the platform catalogue by id.
        """
        if not tenant_id:
            return None
        override = self._store.execute(
            "SELECT * FROM external_connections WHERE tenant_id=?"
            " AND base_connection_id=? AND deleted_at IS NULL",
            (tenant_id, connection_id))
        if override:
            return override[0]
        granted = self._store.execute(
            "SELECT * FROM external_connections WHERE id=? AND scope='platform'"
            " AND kind=? AND enabled=1 AND deleted_at IS NULL AND id IN"
            " (SELECT platform_connection_id FROM"
            "  external_connection_tenant_access"
            "  WHERE tenant_id=? AND enabled=1)",
            (connection_id, registry.KIND_MCP, tenant_id))
        return granted[0] if granted else None

    def _source_of(self, row: Mapping[str, Any], *,
                   tenant_id: Optional[str]) -> Tuple[str, str, int, bool]:
        """``(source, source_connection_id, source_version, available)``.

        ``available`` is False only for a tenant row whose template is gone,
        disabled or no longer granted. Everything else is available by
        construction, and a row with no template is available on its own terms —
        saying anything about a platform source there would invent one.
        """
        base_id = row["base_connection_id"]
        if not base_id:
            if row["scope"] == registry.SCOPE_PLATFORM and tenant_id:
                # A granted template read for a tenant that never overrode it:
                # the template *is* the effective connection, secrets included.
                return "inherited", str(row["id"]), int(row["version"]), True
            if row["scope"] == registry.SCOPE_PLATFORM:
                return "platform", "", 0, True
            return "created", "", 0, True
        base = self._store.execute(
            "SELECT id, enabled, version FROM external_connections"
            " WHERE id=? AND scope='platform' AND deleted_at IS NULL",
            (base_id,))
        if not base:
            return "override", str(base_id), 0, False
        base = base[0]
        granted = bool(self._store.execute(
            "SELECT 1 FROM external_connection_tenant_access"
            " WHERE platform_connection_id=? AND tenant_id=? AND enabled=1",
            (base_id, tenant_id)))
        return ("override", str(base_id), int(base["version"]),
                bool(base["enabled"]) and granted)

    def secret_versions(self, connection_id: str) -> Dict[str, int]:
        """A per-slot marker for the secrets currently referenced.

        Used to bind a result to the secret it was produced from, so rotating a
        password invalidates a stale "connected" badge instead of leaving it
        green against a credential that no longer exists — and, because the
        number is part of the approval digest, so an approval issued before a
        rotation cannot authorise the request afterwards.

        The marker is derived from the stored ``secret_version`` **and** the
        underlying credential/platform-secret id, and both are needed:

        * the id alone never moves when a password is rotated in place (the
          credential row is updated, not replaced), so it would not notice a
          rotation at all — the case this exists for;
        * the version alone restarts at ``1`` whenever a slot is cleared and
          re-added, so a different credential could present the same number.

        The digest is SHA-256 rather than :func:`hash`, which is salted per
        process: an approval minted by one worker has to verify in another, so
        the value must not depend on ``PYTHONHASHSEED``.

        A reference written before versions were recorded has no number to
        compare, and it still changes when the credential it points at changes
        (or is cleared), which is the conservative direction.
        """
        rows = self._store.execute(
            "SELECT connection_id, slot, secret_version, credential_id,"
            " platform_secret_id,"
            " COALESCE(credential_id, platform_secret_id, '') AS marker"
            " FROM external_connection_secret_refs WHERE connection_id=?"
            " ORDER BY slot", (connection_id,))
        return slot_markers(rows).get(str(connection_id), {})

    # -- context -------------------------------------------------------------

    def build_context(self, snapshot: ConnectionSnapshot, *,
                      actor_user_id: str, agent_id: str = "",
                      run_id: str = "", draft: bool = False,
                      limits: Optional[Mapping[str, Any]] = None,
                      extra: Optional[Mapping[str, Any]] = None,
                      policy: Optional[NetworkPolicy] = None,
                      secret_sink: Optional[List[str]] = None
                      ) -> ExecutionContext:
        """Assemble the context, including the resolved deployment policy.

        ``secret_sink``, when given, collects every value the resolver handed to
        the adapter. It exists so the summary a *persisted* result is built from
        can be scrubbed with the same values the adapter was given: an adapter is
        supposed to redact its own detail strings, and this is the layer that
        does not have to trust that it did.
        """
        active_policy = policy or current_policy()
        pool_limits = pool_for_deployment().limits
        merged_limits: Dict[str, Any] = {
            "policy": active_policy,
            "test_timeout": pool_limits.timeout,
            "uninterruptible_ok": pool_limits.uninterruptible_ok,
            **active_policy.limits(),
        }
        if limits:
            merged_limits.update(dict(limits))

        def _resolver(slot: str) -> str:
            spec = registry.TYPE_SPECS.get(snapshot.kind)
            allowed = spec.secret_slots if spec else frozenset()
            if slot not in allowed:
                raise AdapterError(
                    "connection type %r has no secret slot %r"
                    % (snapshot.kind, slot),
                    code="unknown_secret_slot", stage=STAGE_CONFIG)
            try:
                value = self._service.resolve_secret(
                    connection_id=snapshot.id, slot=slot, scope=snapshot.scope,
                    tenant_id=snapshot.tenant_id, actor_user_id=actor_user_id)
            except Exception as exc:  # noqa: BLE001 - normalised by the caller
                raise AdapterError(
                    "secret slot %r is not configured" % slot,
                    code="secret_unavailable", stage=STAGE_CONFIG) from exc
            if secret_sink is not None and value:
                secret_sink.append(str(value))
            return value

        return ExecutionContext(
            kind=snapshot.kind, scope=snapshot.scope,
            tenant_id=snapshot.tenant_id, owner_user_id=snapshot.owner_user_id,
            connection_id=snapshot.id, config=dict(snapshot.config),
            secret_resolver=_resolver, config_version=snapshot.version,
            secret_versions=self.secret_versions(snapshot.id),
            actor_user_id=actor_user_id, agent_id=agent_id, run_id=run_id,
            draft=draft, limits=merged_limits, extra=dict(extra or {}))

    # -- describe ------------------------------------------------------------

    def describe(self, connection_id: str, *,
                 tenant_id: Optional[str] = None,
                 adapter_report: bool = True) -> Dict[str, Any]:
        """Runtime limits and adapter capabilities for one connection."""
        snapshot = self.snapshot(connection_id, tenant_id=tenant_id)
        present = self._adapter_present(snapshot.kind)
        report: Dict[str, Any] = {}
        if adapter_report and present:
            ctx = self.build_context(snapshot, actor_user_id="",
                                     draft=True)
            try:
                report = adapter_for(snapshot.kind) \
                    .describe_capabilities(ctx).as_dict()
            except AdapterError as exc:
                report = {"error": exc.code, "detail": str(exc)}
        return {
            "connection_id": snapshot.id,
            "kind": snapshot.kind,
            "config_version": snapshot.version,
            "adapter_present": present,
            "limits": {
                "test_timeout": pool_module.pool_for_deployment().limits.timeout,
                "uninterruptible_ok":
                    pool_module.pool_for_deployment().limits.uninterruptible_ok,
                "network": current_policy().as_dict(),
            },
            "capabilities": report,
        }

    def _adapter_present(self, kind: str) -> bool:
        from integrations.external.adapters.base import adapter_available
        return adapter_available(kind)

    # -- probe ---------------------------------------------------------------

    def probe(self, connection_id: str, *, actor_user_id: str,
              tenant_id: Optional[str] = None, agent_id: str = "",
              run_id: str = "", draft: bool = False,
              expected_version: Optional[int] = None,
              record: bool = True) -> Dict[str, Any]:
        """Run a bounded test and, when it applies to a saved connection,
        persist the redacted summary bound to the version it tested.

        ``draft=True`` probes an unsaved form. It persists nothing — not the
        connection, not the secret, not a test summary — which is what the spec
        means by "草稿测试不写业务域".
        """
        snapshot = self.snapshot(connection_id, tenant_id=tenant_id)
        if expected_version is not None and int(expected_version) != snapshot.version:
            raise conflict("the connection changed while you were editing it",
                           code="version_conflict")
        if not snapshot.enabled and not draft:
            raise forbidden("this connection is disabled",
                            code="connection_disabled")

        capabilities = registry.open_classes(snapshot.kind)
        if "test" not in capabilities:
            # Fail closed with the deployment's own reason. A caller must not
            # be able to obtain a test result the deployment has not opened.
            raise forbidden(
                "testing %s connections is not open in this deployment"
                % snapshot.kind,
                code="test_not_available")
        if not self._adapter_present(snapshot.kind):
            raise invalid("no adapter is installed for %r" % snapshot.kind,
                          code="adapter_not_installed")

        # Every value the adapter is handed, so the stored summary can be
        # scrubbed with the same strings the adapter saw. The declared slots are
        # added alongside the sink: an adapter is *supposed* to obtain a secret
        # only through the resolver, and the summary is scrubbed against the
        # connection's whole secret set so a leak does not depend on the adapter
        # having gone through the intended path.
        resolved_secrets: List[str] = []
        ctx = self.build_context(snapshot, actor_user_id=actor_user_id,
                                 agent_id=agent_id, run_id=run_id, draft=draft,
                                 secret_sink=resolved_secrets)
        adapter = adapter_for(snapshot.kind)
        started = time.monotonic()
        result = run_probe(adapter, ctx,
                           uninterruptible=self._uninterruptible(snapshot.kind))
        duration_ms = int((time.monotonic() - started) * 1000)

        payload = result.as_dict()
        payload["duration_ms"] = duration_ms
        payload["connection_id"] = snapshot.id
        payload["config_version"] = snapshot.version
        outcome = result.outcome

        if record and not draft:
            stored = self._record_test(
                snapshot, result, outcome, actor_user_id=actor_user_id,
                duration_ms=duration_ms,
                secrets=_with_declared_secrets(
                    self, snapshot, actor_user_id, resolved_secrets))
            payload["recorded"] = stored
            payload["stale"] = not stored
            if stored:
                # Answer "and what is the state now?" from the record that was
                # just written. Without this the console has only `outcome` to go
                # on, and a successful probe of a saved connection came back
                # looking identical to one that saved nothing.
                state = test_summary_payload(
                    int(time.time()),
                    self.last_test(snapshot.id, tenant_id=snapshot.tenant_id) or {})
                payload["test_status"] = state["status"]
                payload["tested_at"] = state["ran_at"]
            if not stored:
                # The configuration moved while the probe ran. The caller is
                # told, because showing this result as current would be a lie.
                raise conflict(
                    "the connection changed while the test was running; "
                    "the result was discarded",
                    code="test_result_stale")
        else:
            payload["recorded"] = False
        return payload

    def probe_draft(self, kind: str, config: Mapping[str, Any], *,
                    secrets: Optional[Mapping[str, Any]] = None,
                    actor_user_id: str, tenant_id: Optional[str] = None,
                    scope: str = "tenant",
                    owner_user_id: Optional[str] = None) -> Dict[str, Any]:
        """Test an *unsaved* form. Persists nothing, by construction.

        Why this is a separate entry point rather than ``probe`` on a
        not-yet-saved row: a connection that does not exist has no version to
        bind a result to and no secret reference to rotate, so "record the
        summary" has no meaning for it. Rather than teach ``probe`` to
        sometimes be meaningless, the draft path builds a throwaway snapshot
        and always runs with ``record=False``.

        What it therefore cannot do, and does not:

        * create, update or delete a connection row;
        * resolve a secret from the credential store (the values are supplied
          by the caller for this one attempt and are never stored);
        * overwrite a saved connection's tested version, or be found by
          :meth:`last_test` — it writes no ``external_connection_tests`` row.

        The gates are the same three the saved path applies — the deployment's
        ``test`` class, an installed adapter, and the type's own config
        validation — because "it is only a draft" is not a reason to skip the
        deployment's switch or to build an outbound connection from a shape the
        type would never accept.
        """
        from integrations.external.adapters.base import (
            adapter_available,
            adapter_for,
        )
        from integrations.external import registry as registry_module

        kind = str(kind or "").strip()
        # ``validate_config`` raises the type's own 400, which is also what
        # ``spec_for`` raises for an unknown kind.
        normalized = registry_module.validate_config(kind, dict(config or {}))

        capabilities = registry_module.open_classes(kind)
        if "test" not in capabilities:
            raise forbidden(
                "testing %s connections is not open in this deployment" % kind,
                code="test_not_available")
        if not adapter_available(kind):
            raise invalid("no adapter is installed for %r" % kind,
                          code="adapter_not_installed")

        snapshot = ConnectionSnapshot(
            id="", kind=kind, scope=str(scope or "tenant"),
            tenant_id=tenant_id, owner_user_id=owner_user_id,
            name="", config=normalized, version=0, enabled=True,
            base_connection_id=None)

        spec = registry_module.TYPE_SPECS.get(kind)
        allowed_slots = spec.secret_slots if spec else frozenset()
        supplied = {str(k): v for k, v in dict(secrets or {}).items()}
        for slot in supplied:
            if slot not in allowed_slots:
                raise invalid(
                    "connection type %r has no secret slot %r" % (kind, slot),
                    code="unknown_secret_slot", fields={"secrets": "unknown"})

        def _resolver(slot: str) -> str:
            if slot not in supplied:
                raise AdapterError(
                    "secret slot %r is not configured" % slot,
                    code="secret_unavailable", stage=STAGE_CONFIG)
            value = supplied[slot]
            return "" if value is None else str(value)

        ctx = self.build_context(
            snapshot, actor_user_id=actor_user_id, draft=True,
            extra={})
        # ``build_context`` resolves secrets through the store; the draft must
        # not, so the resolver is replaced with the caller's values after the
        # rest of the context (policy, limits, deadline) has been assembled.
        ctx = replace(ctx, secret_resolver=_resolver)

        adapter = adapter_for(kind)
        started = time.monotonic()
        result = run_probe(adapter, ctx,
                           uninterruptible=self._uninterruptible(kind))
        duration_ms = int((time.monotonic() - started) * 1000)

        payload = _scrub_payload(
            result.as_dict(),
            [v for v in (str(s) for s in supplied.values()) if v])
        payload["duration_ms"] = duration_ms
        payload["connection_id"] = ""
        payload["config_version"] = None
        # Stated rather than omitted: a caller must be able to tell a draft
        # result from a recorded one without knowing which endpoint it called.
        payload["draft"] = True
        payload["recorded"] = False
        return payload

    def _digest_key(self) -> str:
        """The key the approval digest is keyed with.

        Derived from the deployment's credential key so a digest can prove
        "same request" without being reversible to a password, and so two
        deployments do not produce interchangeable digests. Empty when the
        deployment has no key — the digest is then an unkeyed hash, which the
        risk module accepts rather than failing closed on a missing key.
        """
        try:
            from config import conf
            key = str((conf() or {}).get("credential_key") or "")
            if key:
                return key
        except Exception:  # noqa: BLE001
            pass
        try:
            from auth.crypto import get_credential_key
            return str(get_credential_key() or "")
        except Exception:  # noqa: BLE001
            return ""

    def _uninterruptible(self, kind: str) -> bool:
        """Whether this kind's probe may ignore cancellation.

        Only kinds that use a blocking SDK — SAP RFC is the one in this build —
        are marked uninterruptible, and even then the pool refuses them unless
        the deployment has accepted the isolation story.
        """
        if kind == registry.KIND_ERP:
            return False  # the ADT SQL path is cancellable; RFC is not started
        return False

    def _record_test(self, snapshot: ConnectionSnapshot, result: ProbeResult,
                     outcome: str, *, actor_user_id: str,
                     duration_ms: int,
                     secrets: Sequence[str] = ()) -> bool:
        """Persist the redacted summary, if the version still holds.

        Returns ``False`` when the connection moved on; the caller turns that
        into a conflict rather than a stored result.

        ``secrets`` are the values the adapter was given for this attempt. They
        are scrubbed out of the serialised detail before it is stored, so a
        stored summary cannot become a place a credential survives — an adapter
        that forgets to redact its own error text is a bug, but it must not be a
        credential leak as well.
        """
        failure = result.failed_stage()
        detail = {
            "outcome": outcome,
            "stages": [s.as_dict() for s in result.stages],
            "metadata": dict(result.metadata),
            "duration_ms": duration_ms,
            "cancelled": result.cancelled,
        }
        detail_json = _scrub_secrets(
            json.dumps(detail, ensure_ascii=False, sort_keys=True), secrets)
        now = int(time.time())
        test_id = "ect_" + uuid.uuid4().hex[:24]
        with self._service._tx() as con:  # noqa: SLF001
            current = self._service._row_in_tx(  # noqa: SLF001
                con, snapshot.id, scope=snapshot.scope,
                tenant_id=snapshot.tenant_id)
            if current is None or int(current["version"]) != snapshot.version:
                return False
            con.execute(
                "INSERT INTO external_connection_tests"
                " (test_id, connection_id, actor_user_id, scope, tenant_id,"
                "  config_version, secret_versions_json, stage, result, code,"
                "  detail_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (test_id, snapshot.id, actor_user_id, snapshot.scope,
                 snapshot.tenant_id, snapshot.version,
                 json.dumps(dict(result.secret_versions), sort_keys=True),
                 failure.stage if failure else "",
                 "ok" if outcome == "ok" else outcome,
                 failure.code if failure else "",
                 detail_json, now))
            self._service._audit_in_tx(  # noqa: SLF001
                con, actor_user_id=actor_user_id,
                tenant_id=snapshot.tenant_id,
                target_tenant_id=snapshot.tenant_id,
                action="external_connection.test",
                target="external_connection:%s" % snapshot.id,
                redacted_changes={"result": outcome,
                                  "stage": failure.stage if failure else "",
                                  "code": failure.code if failure else "",
                                  "config_version": snapshot.version},
                result="success" if outcome == "ok" else "failure")
            con.commit()
        return True

    #: How many of a connection's newest summaries are considered before giving
    #: up on finding one that still applies. Mirrors the window the single-row
    #: reader has always used, and is applied in SQL so a connection with a long
    #: test history costs no more to read than one with a short one.
    _TEST_LOOKBACK = 5

    def test_observations(self, wanted: Mapping[str, int]
                          ) -> Dict[str, Dict[str, Any]]:
        """For each connection, the newest summary that still applies.

        ``wanted`` maps a connection id to the config version in force for it.
        Callers pass the *effective* row's version, so a tenant override answers
        for itself while an inherited template answers as the template — the
        tenant consumes the template's configuration and its credentials, so
        that is the record that describes what the tenant would actually run.

        Returns ``{id: {"record": <summary|None>, "stale_at": <int|None>}}``: the
        matching summary in the caller-facing shape, plus the time of the newest
        summary that no longer matches. Both are returned because "you tested
        this and the configuration has moved since" is a different fact from
        "you never tested this", and reporting the first as the second is what
        makes an operator re-test a connection that was working a minute ago.

        Reading a page of cards costs two statements for the whole page, whatever
        its size: the version map comes from the caller, then the secret markers
        and the summaries are read with one ``IN (...)`` each. Both reads are
        bounded — ``_TEST_LOOKBACK`` rows per connection, and one row per slot.
        """
        wanted = {str(k): int(v) for k, v in dict(wanted or {}).items()}
        out: Dict[str, Dict[str, Any]] = {
            cid: {"record": None, "stale_at": None} for cid in wanted}
        if not wanted:
            return out
        placeholders = ",".join("?" for _ in wanted)
        ids = tuple(sorted(wanted))
        markers = slot_markers(self._store.execute(
            "SELECT connection_id, slot, secret_version, credential_id,"
            " platform_secret_id,"
            " COALESCE(credential_id, platform_secret_id, '') AS marker"
            " FROM external_connection_secret_refs WHERE connection_id IN (%s)"
            " ORDER BY connection_id, slot" % placeholders, ids))
        rows = self._store.execute(
            # `rowid` is only named inside the window (it is the tie-breaker the
            # single-row reader has always used); outside, the rank it produced
            # already carries the same order.
            "SELECT * FROM ("
            " SELECT *, ROW_NUMBER() OVER (PARTITION BY connection_id"
            "  ORDER BY created_at DESC, rowid DESC) AS lookback_rank"
            " FROM external_connection_tests WHERE connection_id IN (%s)"
            ") WHERE lookback_rank <= %d"
            " ORDER BY connection_id, lookback_rank"
            % (placeholders, self._TEST_LOOKBACK), ids)
        # One pass over rows that are already newest-first *within* each
        # connection, so the first match is the answer and no per-connection
        # query is ever issued.
        saw_newest: Set[str] = set()
        settled: Set[str] = set()
        for row in rows:
            cid = str(row["connection_id"])
            if cid not in out or cid in settled:
                continue
            if cid not in saw_newest:
                saw_newest.add(cid)
                # The newest summary of any kind, kept in case nothing matches.
                out[cid]["stale_at"] = int(row["created_at"])
            if int(row["config_version"]) != wanted[cid]:
                continue
            try:
                recorded = json.loads(row["secret_versions_json"] or "{}")
            except (TypeError, ValueError):
                recorded = {}
            if recorded != markers.get(cid, {}):
                continue
            out[cid]["record"] = self._summary_of(row)
            out[cid]["stale_at"] = None
            settled.add(cid)
        return out

    @staticmethod
    def _summary_of(row: Optional[Mapping[str, Any]]
                    ) -> Optional[Dict[str, Any]]:
        """The caller-facing summary shape for a recorded row."""
        if row is None:
            return None
        detail: Any = {}
        try:
            detail = json.loads(row["detail_json"] or "{}")
        except (TypeError, ValueError):
            detail = {}
        return {
            "test_id": row["test_id"],
            "result": row["result"],
            "stage": row["stage"],
            "code": row["code"],
            "config_version": row["config_version"],
            "ran_at": row["created_at"],
            "actor_user_id": row["actor_user_id"],
            "detail": detail,
        }

    def last_test(self, connection_id: str, *,
                  tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The most recent recorded summary for the *current* version.

        A summary from an older version, or from a different secret, is not
        returned: the console shows ``untested`` instead of a stale green.
        """
        snapshot = self.snapshot(connection_id, tenant_id=tenant_id)
        records = self.test_observations({str(snapshot.id): snapshot.version})
        return records[str(snapshot.id)]["record"]

    # -- invoke --------------------------------------------------------------

    def invoke(self, connection_id: str, action: str, params: Mapping[str, Any],
               *, actor_user_id: str, tenant_id: Optional[str] = None,
               agent_id: str = "", run_id: str = "",
               approval: Optional[Mapping[str, Any]] = None,
               risk_check: Optional[Callable[[str, str, Mapping[str, Any]],
                                             None]] = None) -> InvokeResult:
        """Run a business action through the adapter.

        Order matters and is the point of this method:

        1. a personal connection must belong to the caller;
        2. an Agent-scoped call must arrive through an Agent bound to the
           connection's tenant;
        3. the action must be declared by the adapter (unknown actions are
           refused rather than passed through);
        4. the deployment must have opened the matching execution class;
        5. the caller must hold the resource-execution authorization for the
           capability (grant + ``tool.execute``, or the narrowed tenant-admin
           exemption);
        6. the caller's risk catalogue entry for this action must permit it, and
           a write must carry a matching approval decision;
        7. only then is the adapter reached.

        Steps 4–5 are ordered deliberately: a closed class is a fact about the
        deployment, so it answers first, and a grant can never be used to open
        one (spec: 不绕过执行条件).
        """
        snapshot = self.snapshot(connection_id, tenant_id=tenant_id)
        if snapshot.scope == registry.SCOPE_PERSONAL \
                and snapshot.owner_user_id != actor_user_id:
            # Ownership first, and before every other refusal, so no later
            # branch can report anything about a connection the caller does not
            # own.
            raise not_found(_NOT_FOUND_MESSAGE)
        if maintenance.paused(scope=snapshot.scope,
                              tenant_id=snapshot.tenant_id,
                              service=self._service):
            # A maintenance window pauses *new executions* in its scope as well
            # as configuration writes (change ``add-external-system-access``,
            # task 12.2). Both halves are needed: the window makes the import's
            # view of "what was here" knowable, and an execution that starts
            # mid-import would run against a row the import is about to replace
            # with one carrying different credentials.
            #
            # Placed after the ownership check so a caller cannot probe another
            # member's connection by its pause state, and before the class and
            # grant gates because "this scope is paused" is true regardless of
            # what the caller holds -- reporting ``execution_not_available``
            # would send them to ask for a grant that would not help.
            return invoke_failed(
                maintenance.PAUSED, stage=STAGE_POLICY,
                message="this scope is in a maintenance window; executions are"
                        " paused until it closes")
        if agent_id and not authorization.agent_in_scope(
                self._identity, tenant_id=snapshot.tenant_id, agent_id=agent_id):
            # A connection reached *through* an Agent must be reached through an
            # Agent bound to the tenant that owns it. This is what keeps the
            # tenant-admin exemption below from becoming a way to name another
            # tenant's connection.
            return invoke_failed(
                authorization.NOT_AUTHORIZED, stage=STAGE_POLICY,
                message=authorization.describe_refusal(
                    snapshot.kind, action))
        if not snapshot.enabled:
            return invoke_failed("connection_disabled", stage=STAGE_CONFIG,
                                 message="this connection is disabled")
        if not self._adapter_present(snapshot.kind):
            return invoke_failed("adapter_not_installed", stage=STAGE_CONFIG)
        adapter = adapter_for(snapshot.kind)
        if action not in adapter.actions:
            return invoke_failed("unsupported_action", stage=STAGE_CONFIG,
                                 message="%s does not support %r"
                                         % (snapshot.kind, action))

        opened = registry.open_classes(snapshot.kind)
        is_write = action in adapter.write_actions
        required = "write_execute" if is_write else "read_execute"
        if required not in opened:
            return invoke_failed(
                "execution_not_available", stage=STAGE_POLICY,
                message="%s execution is not open in this deployment" % required)

        if not authorization.may_execute(
                self._identity, actor_user_id=actor_user_id,
                tenant_id=snapshot.tenant_id, kind=snapshot.kind,
                action=action, scope=snapshot.scope, agent_id=agent_id,
                connection_id=snapshot.id):
            # The caller's own authorization for this capability: the grant and
            # the functional permission, or the narrowed tenant-admin
            # exemption. Checked *after* the class so a closed class reports
            # itself honestly, and before the risk gate so an unauthorized write
            # is refused as "not yours to make" rather than as "you did not
            # attach an approval" -- the second would invite a caller to go and
            # mint one.
            return invoke_failed(
                authorization.NOT_AUTHORIZED, stage=STAGE_POLICY,
                message=authorization.describe_refusal(snapshot.kind, action))

        # The per-connection Agent assignment. It is asked *after* the caller's
        # own authorization so a capability that was never theirs still reports
        # as unauthorized, and *before* the risk/approval gate so an unassigned
        # write is refused as "not yours to make" rather than as "you did not
        # attach an approval" — the second would invite the caller to go and
        # mint one for a connection they may not use at all.
        #
        # The consuming tenant — the argument, not the row's own ``tenant_id``,
        # which is ``None`` for an inherited platform template — is what the
        # relation is keyed on, so 继承 and 覆盖 share one set.
        consuming_tenant = str(tenant_id or snapshot.tenant_id or "")
        if assignment.call_regime_applies(snapshot.kind, snapshot.scope,
                                          consuming_tenant):
            logical_id = assignment.logical_connection_id(snapshot)
            try:
                allowed, reason = assignment.assignment_allows(
                    self._store, tenant_id=consuming_tenant,
                    logical_id=logical_id, agent_id=agent_id)
            except Exception:  # noqa: BLE001 - a failed lookup proves nothing
                allowed, reason = False, assignment.REASON_LOOKUP_FAILED
            if not allowed:
                return invoke_failed(
                    reason, stage=STAGE_POLICY,
                    message="this connection is not available to the Agent in"
                            " this context")

        if risk_check is not None:
            from integrations.external.errors import ExternalConnectionError as _ExtErr

            def _refuse(exc) -> InvokeResult:
                code = getattr(exc, "code", "risk_refused")
                stage = getattr(exc, "stage", STAGE_POLICY) or STAGE_POLICY
                return invoke_failed(code, stage=stage, message=str(exc))

            try:
                risk_check(
                    snapshot.kind, action, params,
                    connection_id=snapshot.id,
                    config_version=snapshot.version,
                    secret_versions=self.secret_versions(snapshot.id),
                    actor_user_id=actor_user_id, tenant_id=snapshot.tenant_id,
                    target=params.get("target") if isinstance(params, Mapping)
                    else None,
                    approval=approval,
                    digest_key=self._digest_key())
            except (AdapterError, _ExtErr) as exc:
                return _refuse(exc)
            except TypeError:
                # A gate that does not accept the richer facts (a test double,
                # or a narrower deployment gate) still gets the conservative
                # positional form rather than being skipped.
                try:
                    risk_check(snapshot.kind, action, params)
                except (AdapterError, _ExtErr) as exc:
                    return _refuse(exc)

        ctx = self.build_context(snapshot, actor_user_id=actor_user_id,
                                 agent_id=agent_id, run_id=run_id)
        if approval is not None:
            ctx.extra = {**dict(ctx.extra), "approval": dict(approval)}
        try:
            result = adapter.invoke(ctx, action, params)
        except AdapterError as exc:
            return invoke_failed(exc.code, stage=exc.stage, message=str(exc))
        except Exception as exc:  # noqa: BLE001 - a bug is reported, not leaked
            return invoke_failed("adapter_error", stage=STAGE_INTERNAL,
                                 message=type(exc).__name__)

        self._audit_invoke(snapshot, action, result, actor_user_id)
        return result

    def _audit_invoke(self, snapshot: ConnectionSnapshot, action: str,
                      result: InvokeResult, actor_user_id: str) -> None:
        outcome = "success" if result.ok else (
            "unknown" if result.outcome_unknown else "failure")
        try:
            with self._service._tx() as con:  # noqa: SLF001
                self._service._audit_in_tx(  # noqa: SLF001
                    con, actor_user_id=actor_user_id,
                    tenant_id=snapshot.tenant_id,
                    target_tenant_id=snapshot.tenant_id,
                    action="external_connection.invoke",
                    target="external_connection:%s" % snapshot.id,
                    redacted_changes={
                        "action": action,
                        "outcome_unknown": bool(result.outcome_unknown),
                        "duration_ms": int(result.duration_ms),
                    },
                    result=outcome)
                con.commit()
        except Exception:  # noqa: BLE001 - audit must not mask the call result
            pass


def runtime_for(service) -> ConnectionRuntime:
    return ConnectionRuntime(service)


def test_summary_payload(tested_at: int, record: Mapping[str, Any], *,
                         stale_at: Optional[int] = None) -> Dict[str, Any]:
    """The shape the console consumes for a connection's test state.

    One place decides what "the test state" is, so the catalogue, the detail
    view and the page cannot disagree.

    ``stale_at`` is the time of the newest summary that no longer applies to the
    current configuration (:meth:`ConnectionRuntime.test_observations`). When
    nothing applies but such a summary exists, the state is ``expired``: the
    connection was tested and has moved since, which is worth saying out loud
    rather than showing the same "never tested" as a connection nobody has
    touched. The expired payload deliberately carries no stage, code or detail
    from the stale summary — the one thing a stale result must not do is keep
    telling the operator what the old configuration did.
    """
    if not record:
        if stale_at is not None:
            return {"status": "expired", "stage": "", "code": "",
                    "ran_at": int(stale_at), "detail": {}}
        return {"status": "untested", "stage": "", "code": "",
                "ran_at": None, "detail": {}}
    result = str(record.get("result") or "failed")
    status = {"ok": "ok", "partial": "partial"}.get(result, "failed")
    return {
        "status": status,
        "stage": record.get("stage") or "",
        "code": record.get("code") or "",
        "ran_at": record.get("ran_at"),
        "config_version": record.get("config_version"),
        "detail": record.get("detail") or {},
    }
