# encoding:utf-8
"""The external-connection control service.

One authoritative service for MCP / ERP / OA / personal-email connections,
backed by the identity database. The console, the scenes and (later) the
runtime all call this — there is no per-surface copy of a connection, and no
second secret store.

What the service is responsible for
-----------------------------------
* **Ownership is server-derived.** ``scope``/``tenant_id``/``owner_user_id``
  come from the caller's verified context and the requested route, never from a
  request body; the ``CHECK`` constraints in migration 28 back the same rule at
  the storage layer. A client can name a connection, not its owner.
* **One row per fact, versioned.** Every mutation is a single
  ``BEGIN IMMEDIATE`` transaction that writes the configuration, the secret
  **references**, the catalog revision and the audit event together, guarded by
  ``version`` (If-Match). Concurrent writers get a conflict instead of
  overwriting each other.
* **Secrets are referenced, never copied.** Tenant/personal connections point at
  the existing ``credentials``/``credential_versions`` rows; platform MCP
  connections point at the platform-owned ``platform_connection_secrets`` pair.
  Configuration (``config_json``) is non-secret by construction — the registry
  refuses a secret-shaped key — and read projections expose only *presence*,
  never plaintext.
* **Explicit secret semantics.** ``keep`` (omitted / blank), ``replace`` (a
  value) and ``clear`` (explicit ``None``) are three different requests. A blank
  or masked value is never stored as a new secret.
* **References block deletion.** A connection still named as an ERP default, as
  a tenant override's platform source or in a platform tenant-access list is
  refused with the reference summary; nothing is silently re-pointed or
  cascade-erased.
* **Idempotent creation.** A create carries an ``Idempotency-Key`` scoped to
  actor+scope+endpoint; the same key with the same payload returns the original
  (redacted) result, a different payload conflicts.

What it deliberately does not do
--------------------------------
No probing, no execution, no tool dispatch: the capability projection reports
``test``/``execute`` as closed with the deployment reason (design §7). Those
arrive in later task groups with their own acceptance evidence.
"""

from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from auth.audit import AuditStore
from common.log import logger
from integrations.external import assignment, maintenance, registry
from integrations.external.errors import (
    conflict,
    forbidden,
    invalid,
    not_found,
    unavailable,
)

#: How long a create-idempotency record stays answerable.
IDEMPOTENCY_TTL_SECONDS = 24 * 3600

#: Hard caps so a hostile body cannot grow the projection without bound.
MAX_PAGE_SIZE = 100
MAX_NAME_LENGTH = 128


def _now() -> int:
    return int(time.time())


def _scope_key(scope: str, tenant_id: Optional[str]) -> str:
    if scope == registry.SCOPE_PLATFORM:
        return "platform"
    return "%s:%s" % (scope, tenant_id or "")


class ExternalConnectionService:
    """Connection lifecycle, secrets and the catalogue projection."""

    def __init__(self, identity):
        # ``identity`` is an ``auth.service.IdentityService``: the service reuses
        # its store (the same identity.db), its membership/permission predicates
        # and the ``credentials`` tables, and adds no second authority.
        self._identity = identity
        self._store = identity._store
        self._audit = AuditStore(self._store.db_path)

    # -- authorization ------------------------------------------------------

    def _permissions(self, actor_user_id: str, tenant_id: str) -> set:
        try:
            return set(self._identity.permissions_for(actor_user_id, tenant_id))
        except Exception:  # noqa: BLE001 - an unreadable permission set denies
            return set()

    def _is_platform_admin(self, actor_user_id: str) -> bool:
        return bool(self._identity.is_platform_admin_user(actor_user_id))

    def _is_tenant_admin(self, actor_user_id: str, tenant_id: str) -> bool:
        return bool(self._identity._is_control(actor_user_id, tenant_id))

    def require_platform_admin(self, actor_user_id: str) -> None:
        if not self._is_platform_admin(actor_user_id):
            raise forbidden()

    def require_tenant_read(self, actor_user_id: str, tenant_id: str) -> None:
        if self._is_platform_admin(actor_user_id):
            return
        if self._is_tenant_admin(actor_user_id, tenant_id):
            return
        if not self._identity._member_active(actor_user_id, tenant_id):
            raise forbidden()
        if "external.connections.read" not in self._permissions(
                actor_user_id, tenant_id):
            raise forbidden()

    def require_tenant_manage(self, actor_user_id: str, tenant_id: str) -> None:
        if self._is_platform_admin(actor_user_id):
            return
        if self._is_tenant_admin(actor_user_id, tenant_id):
            return
        if not self._identity._member_active(actor_user_id, tenant_id):
            raise forbidden()
        if "external.connections.manage" not in self._permissions(
                actor_user_id, tenant_id):
            raise forbidden()

    def require_personal(self, actor_user_id: str, tenant_id: str) -> None:
        """Managing one's own email needs membership, not administration.

        Ownership is the qualification (there is no ``user_id`` parameter on the
        personal surface), so an administrator gets nothing extra here — the
        spec's "管理员不能代用他人邮箱" is structural.
        """
        if not self._identity._member_active(actor_user_id, tenant_id):
            raise forbidden()

    # -- row helpers --------------------------------------------------------

    @staticmethod
    def _row_to_card(row: Mapping[str, Any], *,
                     actions: Sequence[str],
                     state: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        state = state or {}
        return {
            "id": row["id"],
            # ``effective_id`` is the row that actually applies to a consumer.
            # A tenant override (handled by its own card) and an inherited
            # platform row resolve to different ids, so the console never has to
            # guess which one a scene will use.
            "effective_id": row["id"],
            "kind": row["kind"],
            "scope": row["scope"],
            "name": row["name"],
            "enabled": bool(row["enabled"]),
            "source": row["source"],
            "version": int(row["version"]),
            # The state is resolved by the caller against the row that is *in
            # force*, because a card cannot see the override that supersedes it.
            "test_status": str(state.get("status") or "untested"),
            "tested_at": state.get("ran_at"),
            "actions": list(actions),
            "base_connection_id": row["base_connection_id"],
        }

    def _fetch_row(self, connection_id: str, *, tenant_id: Optional[str] = None,
                   scope: Optional[str] = None, include_deleted: bool = False
                   ) -> Optional[Mapping[str, Any]]:
        sql = "SELECT * FROM external_connections WHERE id=?"
        params: List[Any] = [connection_id]
        if tenant_id is not None:
            sql += " AND tenant_id=?"
            params.append(tenant_id)
        if scope is not None:
            sql += " AND scope=?"
            params.append(scope)
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        rows = self._store.execute(sql, tuple(params))
        return rows[0] if rows else None

    def _secret_presence(self, connection_id: str) -> Dict[str, bool]:
        rows = self._store.execute(
            "SELECT slot FROM external_connection_secret_refs WHERE connection_id=?",
            (connection_id,),
        )
        return {row["slot"]: True for row in rows}

    def _detail(self, row: Mapping[str, Any], *, source: str,
                actions: Sequence[str]) -> Dict[str, Any]:
        config = json.loads(row["config_json"] or "{}")
        presence = self._secret_presence(row["id"])
        spec = registry.TYPE_SPECS.get(row["kind"])
        slots = {
            slot: {"configured": bool(presence.get(slot))}
            for slot in sorted(spec.secret_slots if spec else ())
        }
        capabilities = registry.capability_projection(
            row["kind"], config=config, has_secret=presence)
        state = self._test_state_for(row)
        return {
            "id": row["id"],
            "kind": row["kind"],
            "scope": row["scope"],
            "tenant_id": row["tenant_id"],
            "owner_user_id": row["owner_user_id"],
            "name": row["name"],
            "config": config,
            "secrets": slots,
            "enabled": bool(row["enabled"]),
            "version": int(row["version"]),
            "source": source,
            "base_connection_id": row["base_connection_id"],
            "actions": list(actions),
            "capabilities": capabilities,
            "test_status": str(state.get("status") or "untested"),
            "tested_at": state.get("ran_at"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # -- types projection ---------------------------------------------------

    def types_projection(self, *, actor_user_id: str,
                         tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Which types this caller may create, in which scope, and why not.

        The reason is the deployment's, not a placeholder: a type whose tenant
        singleton already exists is reported as ``singleton_exists`` so the
        console opens the editor instead of offering a second one.

        A scope in a maintenance window reports ``maintenance_window`` as its
        reason (task 12.2). That is why the window is surfaced here rather than
        added as a new route: this projection already answers "may this caller
        write this scope right now, and if not, why", and "a cutover is running"
        is an answer to that question, not a new one. The enforcement is in the
        write methods; this is only so the console shows the reason instead of a
        save button that fails.
        """
        try:
            windows = maintenance.pause_projection(actor_user_id=actor_user_id,
                                                   tenant_id=tenant_id,
                                                   service=self)
        except Exception:  # noqa: BLE001 - an unreadable pause state is display-only
            windows = {}
        platform_admin = self._is_platform_admin(actor_user_id)
        can_tenant = False
        if tenant_id:
            try:
                self.require_tenant_manage(actor_user_id, tenant_id)
                can_tenant = True
            except Exception:  # noqa: BLE001 - not eligible, still list types
                can_tenant = False
        can_personal = bool(tenant_id) and self._identity._member_active(
            actor_user_id, tenant_id)
        existing = {"oa": False, "email": False}
        if tenant_id:
            for kind in ("oa",):
                existing[kind] = bool(self._store.execute(
                    "SELECT 1 FROM external_connections WHERE tenant_id=?"
                    " AND kind=? AND deleted_at IS NULL LIMIT 1",
                    (tenant_id, kind)))
            existing["email"] = bool(self._store.execute(
                "SELECT 1 FROM external_connections WHERE tenant_id=?"
                " AND kind='email' AND owner_user_id=? AND deleted_at IS NULL"
                " LIMIT 1", (tenant_id, actor_user_id)))
        items: List[Dict[str, Any]] = []
        for spec in registry.TYPE_SPECS.values():
            scopes = []
            if registry.SCOPE_PLATFORM in spec.scopes:
                paused = bool(windows.get(registry.SCOPE_PLATFORM, {})
                              .get("paused"))
                scopes.append({
                    "scope": registry.SCOPE_PLATFORM,
                    "available": platform_admin and not paused,
                    "reason": ("maintenance_window" if paused else
                               "" if platform_admin else
                               "platform_admin_required"),
                })
            if registry.SCOPE_TENANT in spec.scopes:
                reason = ""
                if not can_tenant:
                    reason = "management_required"
                elif bool(windows.get(registry.SCOPE_TENANT, {}).get("paused")):
                    # Checked before ``singleton_exists``: a paused scope is
                    # paused for *every* type, and reporting "one already
                    # exists" would send the operator looking for the wrong
                    # thing while the cutover is running.
                    reason = "maintenance_window"
                elif spec.kind in existing and existing[spec.kind]:
                    reason = "singleton_exists"
                scopes.append({
                    "scope": registry.SCOPE_TENANT,
                    "available": can_tenant and not reason,
                    "reason": reason,
                })
            if registry.SCOPE_PERSONAL in spec.scopes:
                reason = ""
                if not can_personal:
                    reason = "membership_required"
                elif bool(windows.get(registry.SCOPE_PERSONAL, {}).get("paused")):
                    reason = "maintenance_window"
                elif existing.get(spec.kind):
                    reason = "singleton_exists"
                scopes.append({
                    "scope": registry.SCOPE_PERSONAL,
                    "available": can_personal and not reason,
                    "reason": reason,
                })
            items.append({
                "kind": spec.kind,
                "label": spec.label,
                "label_key": spec.label_key,
                "config_keys": sorted(spec.config_keys),
                "secret_slots": sorted(spec.secret_slots),
                "scopes": scopes,
                "form_version": 1,
                "capabilities": registry.capability_projection(
                    spec.kind, config={}, has_secret={}),
            })
        return {"types": items, "form_version": 1,
                "maintenance": maintenance.pause_projection(
                    actor_user_id=actor_user_id, tenant_id=tenant_id,
                    service=self)}

    # -- catalogue ----------------------------------------------------------

    def list_catalog(self, *, actor_user_id: str, scope: str,
                     tenant_id: Optional[str] = None, kind: Optional[str] = None,
                     status: Optional[str] = None, q: Optional[str] = None,
                     limit: int = 50, cursor: Optional[str] = None
                     ) -> Dict[str, Any]:
        """The cards this caller may see, in exactly one scope.

        Statistics and filters cover the visible rows only, so a member's page
        never counts another member's mailbox (spec: 数量统计 MUST 仅覆盖获准数据).
        """
        limit = max(1, min(MAX_PAGE_SIZE, int(limit or 50)))
        offset = 0
        if cursor:
            try:
                offset = max(0, int(str(cursor)))
            except (TypeError, ValueError):
                raise invalid("invalid cursor", code="bad_cursor")
        if scope == registry.SCOPE_PLATFORM:
            self.require_platform_admin(actor_user_id)
            where = "scope='platform' AND deleted_at IS NULL"
            params: List[Any] = []
        elif scope == registry.SCOPE_PERSONAL:
            if not tenant_id:
                raise invalid("tenant is required", code="missing_tenant")
            self.require_personal(actor_user_id, tenant_id)
            where = ("scope='personal' AND tenant_id=? AND owner_user_id=?"
                     " AND deleted_at IS NULL")
            params = [tenant_id, actor_user_id]
        elif scope == registry.SCOPE_TENANT:
            if not tenant_id:
                raise invalid("tenant is required", code="missing_tenant")
            self.require_tenant_read(actor_user_id, tenant_id)
            # The tenant's own rows *plus* the platform MCP templates it has been
            # granted. An unauthorized or disabled platform row is absent
            # entirely: a tenant must not learn the platform catalogue by
            # reading its own list (spec ``mcp-connection-integration``).
            where = (
                "deleted_at IS NULL AND ("
                " (scope='tenant' AND tenant_id=?)"
                " OR (scope='platform' AND kind='mcp' AND enabled=1"
                "     AND id IN (SELECT platform_connection_id FROM"
                "                external_connection_tenant_access"
                "                WHERE tenant_id=? AND enabled=1))"
                ")"
            )
            params = [tenant_id, tenant_id]
        else:
            raise invalid("unknown scope", code="unknown_scope")
        if kind:
            where += " AND kind=?"
            params.append(kind)
        if status in ("enabled", "disabled"):
            where += " AND enabled=?"
            params.append(1 if status == "enabled" else 0)
        elif status:
            raise invalid("unknown status filter", code="bad_filter")
        if q:
            where += " AND name LIKE ?"
            params.append("%%%s%%" % str(q)[:64])
        rows = self._store.execute(
            "SELECT * FROM external_connections WHERE " + where +
            " ORDER BY kind, name, id LIMIT ? OFFSET ?",
            tuple(params) + (limit + 1, offset),
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        cards = self._cards(actor_user_id, rows, tenant_id=tenant_id,
                            scope=scope)
        total = self._store.execute(
            "SELECT COUNT(*) AS c FROM external_connections WHERE " + where,
            tuple(params),
        )[0]["c"]
        next_cursor = str(offset + limit) if has_more else None
        return {"items": cards, "total": int(total), "next_cursor": next_cursor,
                "scope": scope}

    def _cards(self, actor_user_id: str, rows: Sequence[Mapping[str, Any]],
               *, tenant_id: Optional[str], scope: str) -> List[Dict[str, Any]]:
        """Cards with the source/effective-id semantics the console renders.

        A platform template the tenant is granted appears as ``inherited``; if
        the tenant has its own override, the inherited card is marked
        ``overridden`` and points ``effective_id`` at the row that actually
        wins, so nothing has to be inferred from ordering.
        """
        overrides: Dict[str, Mapping[str, Any]] = {}
        if scope == registry.SCOPE_TENANT and tenant_id:
            for row in self._store.execute(
                    "SELECT id, base_connection_id, enabled, version FROM"
                    " external_connections WHERE tenant_id=? AND"
                    " base_connection_id IS NOT NULL AND deleted_at IS NULL",
                    (tenant_id,)):
                overrides[row["base_connection_id"]] = row
        cards: List[Dict[str, Any]] = []
        # Resolve every state in a single batch, keyed by the row that is *in
        # force*: a tenant override answers for itself, and an inherited template
        # answers as the template, because that is the configuration and those
        # are the credentials the tenant would actually run.
        effective: Dict[str, Mapping[str, Any]] = {}
        for row in rows:
            override = (overrides.get(row["id"])
                        if row["scope"] == registry.SCOPE_PLATFORM else None)
            effective[str(row["id"])] = override or row
        states = self._test_states(
            {cid: int(target["version"]) for cid, target in effective.items()})
        for row in rows:
            target = effective[str(row["id"])]
            card = self._row_to_card(
                row, actions=self._card_actions(actor_user_id, row, tenant_id),
                state=states.get(str(target["id"])))
            if row["scope"] == registry.SCOPE_PLATFORM:
                override = overrides.get(row["id"])
                if override is not None:
                    card["source"] = "overridden"
                    card["effective_id"] = override["id"]
                    card["effective_enabled"] = bool(override["enabled"])
                    card["runtime_enabled"] = bool(override["enabled"])
                else:
                    card["source"] = "inherited"
                    card["effective_id"] = row["id"]
                    card["effective_enabled"] = bool(row["enabled"])
                    card["runtime_enabled"] = bool(row["enabled"])
            else:
                card["runtime_enabled"] = bool(row["enabled"])
            cards.append(card)
        if scope == registry.SCOPE_TENANT and tenant_id and cards:
            # Batch the assignment summary for the whole page: three queries,
            # never one per card (spec: 摘要查询不逐卡片重复鉴权).
            summaries = self._assignment_summaries(actor_user_id, tenant_id, rows)
            for card in cards:
                summary = summaries.get(str(card["id"]))
                if summary is not None:
                    card["agent_assignment"] = summary
        return cards

    def _card_actions(self, actor_user_id: str, row: Mapping[str, Any],
                      tenant_id: Optional[str]) -> List[str]:
        if row["scope"] == registry.SCOPE_PLATFORM:
            return ["read", "manage"] if self._is_platform_admin(actor_user_id) else ["read"]
        if row["scope"] == registry.SCOPE_PERSONAL:
            return ["read", "manage"] if row["owner_user_id"] == actor_user_id else []
        if tenant_id and self._is_platform_admin(actor_user_id):
            return ["read", "manage"]
        if tenant_id:
            try:
                self.require_tenant_manage(actor_user_id, tenant_id)
                return ["read", "manage"]
            except Exception:  # noqa: BLE001 - read-only range
                return ["read"]
        return []

    # -- per-connection Agent assignment -----------------------------------

    def _assignment_connection(self, actor_user_id: str, tenant_id: str,
                               connection_id: str, *, manage: bool):
        """Authorize and resolve the connection an assignment request addresses.

        Returns ``(row, logical_id)``. The connection must be a tenant MCP/ERP/OA
        or a platform MCP this tenant is granted; ``row`` is the row that is in
        force (the tenant's override, else the granted template) and
        ``logical_id`` is the id the relation is keyed on.
        """
        if manage:
            self.require_tenant_manage(actor_user_id, tenant_id)
        else:
            self.require_tenant_read(actor_user_id, tenant_id)
        row = self._fetch_row(connection_id, tenant_id=tenant_id,
                              scope=registry.SCOPE_TENANT)
        if row is None:
            row = self._fetch_inherited(connection_id, tenant_id)
        if row is None:
            raise not_found()
        if row["kind"] not in assignment.ASSIGNMENT_KINDS:
            # Personal email never appears on a tenant route, and a non-managed
            # kind (none today) has no relation to save.
            raise invalid("this connection type does not support agent"
                          " assignment", code="assignment_unsupported")
        return row, assignment.logical_connection_id(row)

    def _tenant_agent_bindings(self, tenant_id: str
                               ) -> Dict[str, Mapping[str, Any]]:
        """The tenant's bindings, once per request (not once per row)."""
        return {str(b["agent_id"]): b
                for b in self._identity.list_agent_bindings(tenant_id)}

    @staticmethod
    def _agent_profiles() -> Dict[str, Dict[str, Any]]:
        """Display facts per Agent id from the roster; absent ids fall back.

        Read defensively: the assignment surface must still answer by id when a
        build has no roster, and one unreadable profile must not blank the page.
        """
        out: Dict[str, Dict[str, Any]] = {}
        try:
            from agent.registry import get_agent_registry
            for profile in get_agent_registry().list(include_disabled=True):
                out[str(profile.id)] = {
                    "name": str(getattr(profile, "name", "") or profile.id),
                    "category": str(getattr(profile, "category", "") or ""),
                    "enabled": bool(getattr(profile, "enabled", True)),
                }
        except Exception:  # noqa: BLE001 - no roster is not a failed read
            return {}
        return out

    def _agent_visible(self, actor_user_id: str, tenant_id: str,
                       binding: Mapping[str, Any]) -> bool:
        """Whether the caller may *read* this Agent (spec: 目标可见).

        The object-scope rule, unmodified: a shared Agent is visible to every
        member, a private one only to its owner — an administrator does not see a
        colleague's private Agent, which is what keeps 不可见对象 out of the
        list, the count and the search.
        """
        if str(binding.get("tenant_id") or "") != tenant_id:
            return False
        owner = binding.get("private_owner_user_id")
        return owner is None or owner == actor_user_id

    def _agent_manageable(self, actor_user_id: str, tenant_id: str,
                          binding: Mapping[str, Any], *,
                          reader: Optional[Mapping[str, Any]] = None) -> bool:
        """Whether the caller may add/remove this Agent's assignment.

        ``reader`` is the per-request answer to "is this caller a platform or
        tenant administrator", resolved once by :meth:`_assignment_reader`.
        Asked per row instead, a 5,000-Agent tenant would cost two identity
        queries for every Agent in it — the 逐行鉴权查询 the spec forbids.
        """
        if str(binding.get("tenant_id") or "") != tenant_id:
            return False
        owner = binding.get("private_owner_user_id")
        if owner is None:
            if reader is None:
                reader = self._assignment_reader(actor_user_id, tenant_id)
            return bool(reader.get("platform_admin") or reader.get("tenant_admin"))
        return owner == actor_user_id

    def _assignment_reader(self, actor_user_id: str,
                           tenant_id: str) -> Dict[str, Any]:
        """The facts every row in one assignment request shares.

        Read once per request: the caller's administering standing. Everything
        else the per-row checks need is already in the binding row.
        """
        return {
            "platform_admin": self._is_platform_admin(actor_user_id),
            "tenant_admin": self._is_tenant_admin(actor_user_id, tenant_id),
        }

    def _agent_item(self, agent_id: str, binding: Mapping[str, Any],
                    profile: Optional[Mapping[str, Any]], *,
                    actor_user_id: str, tenant_id: str,
                    assigned: bool,
                    reader: Optional[Mapping[str, Any]] = None
                    ) -> Dict[str, Any]:
        profile = profile or {}
        return {
            "id": agent_id,
            "name": str(profile.get("name") or agent_id),
            "category": str(profile.get("category") or ""),
            "enabled": bool(profile.get("enabled", True)),
            "visibility": ("private"
                           if binding.get("private_owner_user_id") else "tenant"),
            "assigned": bool(assigned),
            "manageable": self._agent_manageable(
                actor_user_id, tenant_id, binding, reader=reader),
        }

    def _assignment_snapshot(self, actor_user_id: str, tenant_id: str,
                             logical_id: str) -> Dict[str, Any]:
        """The whole visible assignment picture in a fixed number of reads.

        The state row, the relation, the tenant's bindings and the caller's
        administering standing — never a query per Agent, so a 5,000-Agent tenant
        costs the same as a small one. Filtering and paging happen in Python over
        those results.
        """
        state_row = assignment.assignment_state(
            self._store, tenant_id=tenant_id, logical_id=logical_id)
        state = dict(state_row) if state_row else {}
        assigned = set(assignment.assigned_agent_ids(
            self._store, tenant_id=tenant_id, logical_id=logical_id))
        return {
            "configured": bool(state.get("configured")),
            "revision": int(state.get("revision") or 0),
            "assigned": assigned,
            "bindings": self._tenant_agent_bindings(tenant_id),
            "profiles": self._agent_profiles(),
            "reader": self._assignment_reader(actor_user_id, tenant_id),
        }

    def list_agent_assignments(self, *, actor_user_id: str, tenant_id: str,
                               connection_id: str, q: Optional[str] = None,
                               page: int = 1, page_size: int = 20
                               ) -> Dict[str, Any]:
        """Visible assigned Agents, filtered by name/id and paged."""
        _row, logical_id = self._assignment_connection(
            actor_user_id, tenant_id, connection_id, manage=False)
        snapshot = self._assignment_snapshot(actor_user_id, tenant_id, logical_id)
        needle = str(q or "").strip().lower()
        items: List[Dict[str, Any]] = []
        for agent_id in snapshot["assigned"]:
            binding = snapshot["bindings"].get(agent_id)
            if binding is None or not self._agent_visible(
                    actor_user_id, tenant_id, binding):
                continue
            item = self._agent_item(
                agent_id, binding, snapshot["profiles"].get(agent_id),
                actor_user_id=actor_user_id, tenant_id=tenant_id, assigned=True,
                reader=snapshot["reader"])
            if needle and not _agent_matches(needle, item):
                continue
            items.append(item)
        return self._assignment_page(
            items, page=page, page_size=page_size,
            configured=snapshot["configured"], revision=snapshot["revision"],
            can_assign=self._may_manage_connection(actor_user_id, tenant_id))

    def search_agent_candidates(self, *, actor_user_id: str, tenant_id: str,
                                connection_id: str, q: str,
                                page: int = 1, page_size: int = 20
                                ) -> Dict[str, Any]:
        """Visible Agents matching a non-empty term, with their assigned flag.

        The assigned flag is read from the *whole* relation, not the loaded page,
        so an Agent already assigned on an unloaded page cannot be added twice.
        """
        needle = str(q or "").strip().lower()
        if not needle:
            raise invalid("a search term is required", code="field_required",
                          fields={"q": "required"})
        _row, logical_id = self._assignment_connection(
            actor_user_id, tenant_id, connection_id, manage=False)
        snapshot = self._assignment_snapshot(actor_user_id, tenant_id, logical_id)
        items: List[Dict[str, Any]] = []
        for agent_id, binding in snapshot["bindings"].items():
            if not self._agent_visible(actor_user_id, tenant_id, binding):
                continue
            item = self._agent_item(
                agent_id, binding, snapshot["profiles"].get(agent_id),
                actor_user_id=actor_user_id, tenant_id=tenant_id,
                assigned=agent_id in snapshot["assigned"],
                reader=snapshot["reader"])
            if not _agent_matches(needle, item):
                continue
            items.append(item)
        return self._assignment_page(
            items, page=page, page_size=page_size,
            configured=snapshot["configured"], revision=snapshot["revision"],
            can_assign=self._may_manage_connection(actor_user_id, tenant_id))

    def save_agent_assignments(self, *, actor_user_id: str, tenant_id: str,
                               connection_id: str, expected_revision: int,
                               add_agent_ids: Sequence[str] = (),
                               remove_agent_ids: Sequence[str] = ()
                               ) -> Dict[str, Any]:
        """Apply an add/remove delta in one transaction, guarded by revision.

        Only the explicitly named relations change: the current page, the visible
        set and the whole relation are never treated as a full replacement, so a
        caller that loaded one page cannot clear the pages it did not see.
        """
        row, logical_id = self._assignment_connection(
            actor_user_id, tenant_id, connection_id, manage=True)
        adds = _validated_agent_ids(add_agent_ids, "add_agent_ids")
        removes = _validated_agent_ids(remove_agent_ids, "remove_agent_ids")
        if set(adds) & set(removes):
            raise invalid("an agent cannot be both added and removed",
                          code="conflicting_change")
        if len(adds) + len(removes) > assignment.MAX_ASSIGNMENT_CHANGE:
            raise invalid("too many changes in one request",
                          code="too_many_changes",
                          fields={"changes": "too_many"})
        # Re-derived at save time, not taken from the list the caller loaded:
        # a target that lost its qualification between open and save must be
        # refused (spec: 保存时重验全部目标, 任一失败整笔拒绝).
        bindings = self._tenant_agent_bindings(tenant_id)
        reader = self._assignment_reader(actor_user_id, tenant_id)
        for agent_id in list(adds) + list(removes):
            binding = bindings.get(agent_id)
            if binding is None or not self._agent_manageable(
                    actor_user_id, tenant_id, binding, reader=reader):
                raise forbidden("agent %r is not available to assign"
                                % agent_id, code="agent_not_assignable")
        now = _now()
        with self._tx() as con:
            live = self._row_in_tx(con, row["id"], scope=row["scope"],
                                   tenant_id=row["tenant_id"])
            if live is None:
                raise not_found()
            sets = con.execute(
                "SELECT configured, revision FROM"
                " external_connection_agent_assignment_sets"
                " WHERE tenant_id=? AND logical_connection_id=?",
                (tenant_id, logical_id)).fetchone()
            current = int(sets["revision"]) if sets else 0
            if int(expected_revision) != current:
                raise conflict("assignments changed; reload before saving",
                               code="assignment_version_conflict")
            added = self._apply_assignment_adds(con, tenant_id, logical_id,
                                                adds, actor_user_id, now)
            removed = self._apply_assignment_removes(con, tenant_id, logical_id,
                                                     removes)
            if sets is not None and int(sets["configured"]) \
                    and not added and not removed:
                # Already configured with no net change: answer the current
                # state instead of bumping the revision for nothing.
                con.commit()
                return {"configured": True, "revision": current,
                        "added": 0, "removed": 0, "unchanged": True}
            new_revision = current + 1
            con.execute(
                "INSERT INTO external_connection_agent_assignment_sets"
                " (tenant_id, logical_connection_id, configured, revision,"
                "  updated_at) VALUES (?,?,1,?,?)"
                " ON CONFLICT(tenant_id, logical_connection_id) DO UPDATE SET"
                " configured=1, revision=excluded.revision,"
                " updated_at=excluded.updated_at",
                (tenant_id, logical_id, new_revision, now))
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id,
                action="external_connection.agent_assignment",
                target="external_connection:%s" % connection_id,
                redacted_changes={
                    "logical_connection_id": logical_id,
                    "added": added, "removed": removed, "configured": True,
                    "revision": new_revision,
                },
                result="success")
            con.commit()
        return {"configured": True, "revision": new_revision,
                "added": added, "removed": removed, "unchanged": False}

    @staticmethod
    def _apply_assignment_adds(con, tenant_id: str, logical_id: str,
                               adds: Sequence[str], actor_user_id: str,
                               now: int) -> int:
        added = 0
        for agent_id in adds:
            exists = con.execute(
                "SELECT 1 FROM external_connection_agent_assignments WHERE"
                " tenant_id=? AND logical_connection_id=? AND agent_id=?",
                (tenant_id, logical_id, agent_id)).fetchone()
            if exists is not None:
                continue
            con.execute(
                "INSERT INTO external_connection_agent_assignments"
                " (tenant_id, logical_connection_id, agent_id, created_by,"
                "  created_at) VALUES (?,?,?,?,?)",
                (tenant_id, logical_id, agent_id, actor_user_id, now))
            added += 1
        return added

    @staticmethod
    def _apply_assignment_removes(con, tenant_id: str, logical_id: str,
                                  removes: Sequence[str]) -> int:
        removed = 0
        for agent_id in removes:
            cursor = con.execute(
                "DELETE FROM external_connection_agent_assignments WHERE"
                " tenant_id=? AND logical_connection_id=? AND agent_id=?",
                (tenant_id, logical_id, agent_id))
            removed += int(cursor.rowcount or 0)
        return removed

    def _may_manage_connection(self, actor_user_id: str,
                               tenant_id: str) -> bool:
        try:
            self.require_tenant_manage(actor_user_id, tenant_id)
            return True
        except Exception:  # noqa: BLE001 - read-only range
            return False

    @staticmethod
    def _assignment_page(items: List[Dict[str, Any]], *, page: int,
                         page_size: int, configured: bool, revision: int,
                         can_assign: bool) -> Dict[str, Any]:
        page = max(1, int(page or 1))
        page_size = max(1, min(MAX_PAGE_SIZE, int(page_size or 20)))
        # 权限先过滤(by the callers below), then a stable order by the visible
        # display name and the id, so paging cannot skip or repeat a row.
        items.sort(key=lambda item: (_normalize_name(item["name"]), item["id"]))
        total = len(items)
        start = (page - 1) * page_size
        window = items[start:start + page_size]
        return {
            "items": window,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": start + page_size < total,
            "configured": bool(configured),
            "revision": int(revision),
            "can_assign": bool(can_assign),
        }

    def _assignment_summaries(self, actor_user_id: str, tenant_id: str,
                              rows: Sequence[Mapping[str, Any]]
                              ) -> Dict[str, Dict[str, Any]]:
        """Per-card assignment summary for a catalogue page, in three queries.

        Batch, not per-card: a page of connections costs one state read, one
        relation read and one binding read, so the summary cannot become the
        thing that makes a large catalogue slow.
        """
        logical_by_card = {
            str(row["id"]): assignment.logical_connection_id(row) for row in rows}
        wanted = sorted(set(logical_by_card.values()))
        if not wanted:
            return {}
        placeholders = ",".join("?" for _ in wanted)
        params: tuple = tuple([tenant_id] + wanted)
        states: Dict[str, Mapping[str, Any]] = {
            str(r["logical_connection_id"]): r for r in self._store.execute(
                "SELECT logical_connection_id, configured, revision FROM"
                " external_connection_agent_assignment_sets WHERE tenant_id=?"
                " AND logical_connection_id IN (%s)" % placeholders, params)}
        can_assign = self._may_manage_connection(actor_user_id, tenant_id)
        bindings = self._tenant_agent_bindings(tenant_id)
        counts: Dict[str, int] = {logical: 0 for logical in wanted}
        for r in self._store.execute(
                "SELECT logical_connection_id, agent_id FROM"
                " external_connection_agent_assignments WHERE tenant_id=?"
                " AND logical_connection_id IN (%s)" % placeholders, params):
            binding = bindings.get(str(r["agent_id"]))
            if binding is None or not self._agent_visible(
                    actor_user_id, tenant_id, binding):
                continue
            counts[str(r["logical_connection_id"])] += 1
        out: Dict[str, Dict[str, Any]] = {}
        for card_id, logical in logical_by_card.items():
            state = states.get(logical)
            out[card_id] = {
                "configured": bool(state["configured"]) if state else False,
                "revision": int(state["revision"]) if state else 0,
                "visible_count": counts.get(logical, 0),
                "can_assign": can_assign,
            }
        return out

    @staticmethod
    def _ensure_assignment_set(con, *, tenant_id: str, logical_id: str,
                               configured: bool = True) -> None:
        """Create the state row for a new connection/consumption relation.

        ``DO NOTHING`` on conflict is deliberate: re-granting a template a tenant
        held before (撤销后重新授予) and creating an override of an existing
        template must **preserve** the set the tenant already has, never reset
        its assignments or its configured flag.
        """
        con.execute(
            "INSERT INTO external_connection_agent_assignment_sets"
            " (tenant_id, logical_connection_id, configured, revision,"
            "  updated_at) VALUES (?,?,?,1,?)"
            " ON CONFLICT(tenant_id, logical_connection_id) DO NOTHING",
            (tenant_id, logical_id, 1 if configured else 0, _now()))

    def _purge_assignment_in_tx(self, con, *, row: Mapping[str, Any],
                                tenant_id: Optional[str]) -> int:
        """Delete a logical connection's relations and state, returning removed.

        An override is skipped: it shares its template's set, so deleting it (the
        same act as 恢复继承) must leave the tenant's assignments intact.
        """
        if not tenant_id or row["base_connection_id"]:
            return 0
        if not assignment.assignment_regime_applies(row["kind"], row["scope"]):
            return 0
        removed = int(con.execute(
            "SELECT COUNT(*) c FROM external_connection_agent_assignments"
            " WHERE tenant_id=? AND logical_connection_id=?",
            (tenant_id, row["id"])).fetchone()["c"])
        con.execute(
            "DELETE FROM external_connection_agent_assignments"
            " WHERE tenant_id=? AND logical_connection_id=?",
            (tenant_id, row["id"]))
        con.execute(
            "DELETE FROM external_connection_agent_assignment_sets"
            " WHERE tenant_id=? AND logical_connection_id=?",
            (tenant_id, row["id"]))
        return removed

    # -- create / read / update / delete -----------------------------------

    def create_connection(self, *, actor_user_id: str, scope: str,
                          kind: str, name: str, config: Mapping[str, Any],
                          tenant_id: Optional[str] = None,
                          secrets: Optional[Mapping[str, Any]] = None,
                          idempotency_key: Optional[str] = None,
                          base_connection_id: Optional[str] = None,
                          connection_id: Optional[str] = None) -> Dict[str, Any]:
        """Create one connection in the given scope; ownership is enforced.

        ``connection_id`` lets the *migration* reuse the id the legacy store
        already used (change ``add-external-system-access``, task 12.2: ERP 复用
        原 ID). Any other reference to that id — a scene's saved default, an
        operator's runbook, an incident note — keeps pointing at the same
        connection, instead of silently resolving to nothing after the cutover
        while the ledger says the import succeeded.

        It is an explicit parameter rather than a caller-supplied field on a
        generic API on purpose: the console and the HTTP surface never pass it,
        so a client cannot choose its own primary key, and the one caller that
        does (the import) is already the caller that knows the legacy id.
        """
        registry.validate_scope(kind, scope)
        name = self._validated_name(name)
        normalized = registry.validate_config(kind, config)
        endpoint = "POST /api/external-connections:%s:%s" % (scope, kind)
        scope_key = _scope_key(scope, tenant_id)
        request_payload = {"scope": scope, "kind": kind, "name": name,
                           "config": normalized, "secrets": dict(secrets or {}),
                           "base_connection_id": base_connection_id,
                           # Part of the fingerprint so a replay of the same
                           # import cannot be answered with a row created under
                           # a different id.
                           "reuse_id": str(connection_id or "")}
        if idempotency_key:
            replay = self._idempotent_replay(
                actor_user_id=actor_user_id, scope_key=scope_key,
                endpoint=endpoint, key=idempotency_key, payload=request_payload)
            if replay is not None:
                return replay
        if scope == registry.SCOPE_PLATFORM:
            self.require_platform_admin(actor_user_id)
        elif scope == registry.SCOPE_TENANT:
            if not tenant_id:
                raise invalid("tenant is required", code="missing_tenant")
            self.require_tenant_manage(actor_user_id, tenant_id)
        else:
            if not tenant_id:
                raise invalid("tenant is required", code="missing_tenant")
            self.require_personal(actor_user_id, tenant_id)
        self._refuse_if_paused(scope=scope, tenant_id=tenant_id, owner_user_id=actor_user_id,
                               what="connection changes")
        owner_user_id = actor_user_id if scope == registry.SCOPE_PERSONAL else None
        connection_id = _validated_connection_id(connection_id) \
            or "conn_%s" % _random_token()
        with self._tx() as con:
            if connection_id:
                # An explicitly reused id must not collide. Checked rather than
                # left to the PRIMARY KEY so the refusal is a stable code the
                # import can report instead of a raw integrity error, and so the
                # collision is found before anything else in this transaction
                # has run.
                taken = con.execute(
                    "SELECT id FROM external_connections WHERE id=?",
                    (connection_id,)).fetchone()
                if taken is not None:
                    raise conflict(
                        "connection id is already used",
                        code="connection_id_taken")
            if base_connection_id is not None:
                if scope != registry.SCOPE_TENANT or kind != registry.KIND_MCP:
                    raise invalid("only a tenant MCP connection can override a"
                                  " platform template", code="unsupported_override")
                self._require_platform_source(con, base_connection_id)
            self._ensure_singleton_free(
                con, scope=scope, kind=kind, tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                base_connection_id=base_connection_id)
            now = _now()
            con.execute(
                "INSERT INTO external_connections"
                " (id, kind, scope, tenant_id, owner_user_id, name, config_json,"
                "  enabled, version, base_connection_id, source, created_by,"
                "  created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,1,1,?,?,?,?,?)",
                (connection_id, kind, scope, tenant_id, owner_user_id, name,
                 json.dumps(normalized, ensure_ascii=False), base_connection_id,
                 "override" if base_connection_id else "created",
                 actor_user_id, now, now),
            )
            self._write_secrets(
                con, connection_id=connection_id, kind=kind, scope=scope,
                tenant_id=tenant_id, owner_user_id=owner_user_id,
                actor_user_id=actor_user_id, secrets=secrets, require_values=False)
            if kind == registry.KIND_ERP and scope == registry.SCOPE_TENANT:
                self._ensure_catalog_row(con, scope_key, kind)
            if scope == registry.SCOPE_TENANT \
                    and kind in assignment.ASSIGNMENT_KINDS:
                # A new managed connection starts 已配置且空分配: nothing may use
                # it until an Agent is explicitly assigned. An override of an
                # existing template keeps the set the tenant already has.
                self._ensure_assignment_set(
                    con, tenant_id=tenant_id,
                    logical_id=str(base_connection_id or connection_id))
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="external_connection.create",
                target="external_connection:%s" % connection_id,
                redacted_changes={"kind": kind, "scope": scope, "name": name,
                                  "base_connection_id": base_connection_id,
                                  "slots": sorted(self._secret_presence_in_tx(
                                      con, connection_id))},
                result="success")
            con.commit()
        result = self.get_connection(
            actor_user_id=actor_user_id, scope=scope, connection_id=connection_id,
            tenant_id=tenant_id)
        if idempotency_key:
            self._remember_idempotent(
                actor_user_id=actor_user_id, scope_key=scope_key, endpoint=endpoint,
                key=idempotency_key, payload=request_payload, result=result)
        return result

    def get_connection(self, *, actor_user_id: str, scope: str,
                       connection_id: str,
                       tenant_id: Optional[str] = None) -> Dict[str, Any]:
        if scope == registry.SCOPE_PLATFORM:
            self.require_platform_admin(actor_user_id)
            row = self._fetch_row(connection_id, tenant_id=None, scope=scope)
            if row is None:
                raise not_found()
            return self._detail(row, source="platform", actions=["read", "manage"])
        if not tenant_id:
            raise invalid("tenant is required", code="missing_tenant")
        if scope == registry.SCOPE_PERSONAL:
            self.require_personal(actor_user_id, tenant_id)
            row = self._fetch_row(connection_id, tenant_id=tenant_id, scope=scope)
            if row is None or row["owner_user_id"] != actor_user_id:
                # Another member's mailbox answers exactly like a missing one.
                raise not_found()
            return self._detail(row, source="personal", actions=["read", "manage"])
        self.require_tenant_read(actor_user_id, tenant_id)
        row = self._fetch_row(connection_id, tenant_id=tenant_id, scope=scope)
        if row is None:
            # An inherited platform template is readable by a granted tenant, but
            # only through this scope and only while the grant is live — the
            # platform scope itself stays admin-only.
            row = self._fetch_inherited(connection_id, tenant_id)
            if row is None:
                raise not_found()
            return self._detail(row, source="inherited", actions=["read"])
        actions = self._card_actions(actor_user_id, row, tenant_id)
        source = "override" if row["base_connection_id"] else "created"
        return self._detail(row, source=source, actions=actions)

    def _fetch_inherited(self, connection_id: str,
                         tenant_id: str) -> Optional[Mapping[str, Any]]:
        rows = self._store.execute(
            "SELECT c.* FROM external_connections c"
            " JOIN external_connection_tenant_access a"
            "   ON a.platform_connection_id = c.id"
            " WHERE c.id=? AND c.deleted_at IS NULL AND c.scope='platform'"
            "   AND c.kind='mcp' AND c.enabled=1 AND a.tenant_id=? AND a.enabled=1",
            (connection_id, tenant_id))
        return rows[0] if rows else None

    def update_connection(self, *, actor_user_id: str, scope: str,
                          connection_id: str, expected_version: int,
                          tenant_id: Optional[str] = None,
                          name: Optional[str] = None,
                          config: Optional[Mapping[str, Any]] = None,
                          secrets: Optional[Mapping[str, Any]] = None,
                          enabled: Optional[bool] = None,
                          default_handling: Optional[Mapping[str, Any]] = None
                          ) -> Dict[str, Any]:
        if scope == registry.SCOPE_PLATFORM:
            self.require_platform_admin(actor_user_id)
        elif scope == registry.SCOPE_PERSONAL:
            if not tenant_id:
                raise invalid("tenant is required", code="missing_tenant")
            self.require_personal(actor_user_id, tenant_id)
        else:
            if not tenant_id:
                raise invalid("tenant is required", code="missing_tenant")
            self.require_tenant_manage(actor_user_id, tenant_id)
        self._refuse_if_paused(scope=scope, tenant_id=tenant_id, owner_user_id=actor_user_id,
                               what="connection changes")
        with self._tx() as con:
            row = self._row_in_tx(con, connection_id, scope=scope,
                                  tenant_id=tenant_id)
            if row is None:
                raise not_found()
            if scope == registry.SCOPE_PERSONAL and row["owner_user_id"] != actor_user_id:
                raise not_found()
            if int(row["version"]) != int(expected_version):
                raise conflict("connection was modified by another writer",
                               code="version_conflict")
            new_name = row["name"]
            if name is not None:
                new_name = self._validated_name(name)
            new_config = json.loads(row["config_json"] or "{}")
            if config is not None:
                new_config = registry.validate_config(row["kind"], config)
            new_enabled = bool(row["enabled"]) if enabled is None else bool(enabled)
            # A default ERP connection cannot be turned off without an explicit
            # decision about the default; nothing silently picks another one.
            if (int(row["enabled"]) == 1 and not new_enabled
                    and row["kind"] == registry.KIND_ERP
                    and row["scope"] == registry.SCOPE_TENANT):
                self._handle_default_loss(
                    con, row=row, tenant_id=tenant_id,
                    handling=default_handling)
            con.execute(
                "UPDATE external_connections SET name=?, config_json=?, enabled=?,"
                " version=version+1, updated_at=? WHERE id=?",
                (new_name, json.dumps(new_config, ensure_ascii=False),
                 1 if new_enabled else 0, _now(), connection_id),
            )
            if secrets:
                self._write_secrets(
                    con, connection_id=connection_id, kind=row["kind"], scope=scope,
                    tenant_id=tenant_id, owner_user_id=row["owner_user_id"],
                    actor_user_id=actor_user_id, secrets=secrets,
                    require_values=False)
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id,
                action="external_connection.enable" if new_enabled
                and not bool(row["enabled"]) else
                ("external_connection.disable" if not new_enabled
                 and bool(row["enabled"]) else "external_connection.update"),
                target="external_connection:%s" % connection_id,
                redacted_changes={"name": new_name, "enabled": new_enabled,
                                  "slots": sorted(self._secret_presence_in_tx(
                                      con, connection_id))},
                result="success")
            con.commit()
            updated = self._fetch_row(connection_id, scope=scope)
        actions = (["read", "manage"] if scope != registry.SCOPE_TENANT
                   else self._card_actions(actor_user_id, updated, tenant_id))
        source = "override" if updated["base_connection_id"] else (
            "platform" if scope == registry.SCOPE_PLATFORM else
            ("personal" if scope == registry.SCOPE_PERSONAL else "created"))
        return self._detail(updated, source=source, actions=actions)

    def delete_connection(self, *, actor_user_id: str, scope: str,
                          connection_id: str, expected_version: int,
                          tenant_id: Optional[str] = None,
                          default_handling: Optional[Mapping[str, Any]] = None
                          ) -> Dict[str, Any]:
        if scope == registry.SCOPE_PLATFORM:
            self.require_platform_admin(actor_user_id)
        elif scope == registry.SCOPE_PERSONAL:
            if not tenant_id:
                raise invalid("tenant is required", code="missing_tenant")
            self.require_personal(actor_user_id, tenant_id)
        else:
            if not tenant_id:
                raise invalid("tenant is required", code="missing_tenant")
            self.require_tenant_manage(actor_user_id, tenant_id)
        self._refuse_if_paused(scope=scope, tenant_id=tenant_id, owner_user_id=actor_user_id,
                               what="connection changes")
        with self._tx() as con:
            row = self._row_in_tx(con, connection_id, scope=scope,
                                  tenant_id=tenant_id)
            if row is None:
                raise not_found()
            if scope == registry.SCOPE_PERSONAL and row["owner_user_id"] != actor_user_id:
                raise not_found()
            if int(row["version"]) != int(expected_version):
                raise conflict("connection was modified by another writer",
                               code="version_conflict")
            # A live reference blocks the delete — except the ERP default, where
            # an explicit ``default_handling`` *is* the resolution the spec asks
            # for. Anything else (a tenant override, a tenant-access grant) is
            # refused with its summary so the console can send the user to the
            # blocking item instead of silently orphaning it.
            references = self._references_in_tx(con, row)
            resolved_by_handling = False
            if references and default_handling and all(
                    ref["kind"] == "erp_default" for ref in references):
                self._handle_default_loss(
                    con, row=row, tenant_id=tenant_id, handling=default_handling)
                resolved_by_handling = True
                references = self._references_in_tx(con, row)
            if references and not resolved_by_handling:
                raise self._referenced(references)
            cleared = self._purge_assignment_in_tx(con, row=row,
                                                   tenant_id=tenant_id)
            self._deactivate_secrets_in_tx(con, connection_id)
            con.execute(
                "UPDATE external_connections SET deleted_at=?, enabled=0,"
                " version=version+1, updated_at=? WHERE id=?",
                (_now(), _now(), connection_id),
            )
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="external_connection.delete",
                target="external_connection:%s" % connection_id,
                redacted_changes={"kind": row["kind"], "scope": row["scope"],
                                  "agent_assignments_cleared": cleared},
                result="success")
            con.commit()
        return {"id": connection_id, "deleted": True}

    def _refuse_if_paused(self, *, scope: str, tenant_id: Optional[str],
                          what: str, owner_user_id: Optional[str] = None) -> None:
        """Refuse a configuration write while the scope is in a window.

        Called *after* the authority check in every write method, deliberately:
        a caller who may not manage the scope gets the authority refusal, so the
        pause state of a scope they cannot see is not reported to them. The
        window exists to make the cutover's view of "what was here" knowable, and
        it cannot do that if it also answers "is this tenant migrating" for
        anyone who asks.

        Read from the store per write rather than cached on the service: the
        console and the CLI open and close windows in separate processes, so a
        cached "not paused" would be the one stale answer that silently lets a
        write through the window.
        """
        from integrations.external import maintenance

        maintenance.refuse_if_paused(scope=scope, tenant_id=tenant_id, owner_user_id=owner_user_id,
                                     service=self, what=what)

    @staticmethod
    def _referenced(references: List[Dict[str, Any]]):
        """Refuse a delete that would orphan live references.

        The error carries the summary (and the explicit way out) so the console
        can send the user to the blocking item instead of a dead end.
        """
        from integrations.external.errors import ExternalConnectionError
        error = ExternalConnectionError(
            "connection is still referenced", code="referenced", status=409)
        error.references = references  # type: ignore[attr-defined]
        return error

    # -- ERP default --------------------------------------------------------

    def get_erp_default(self, *, actor_user_id: str, tenant_id: str) -> Dict[str, Any]:
        self.require_tenant_read(actor_user_id, tenant_id)
        row = self._store.execute(
            "SELECT revision, default_connection_id FROM"
            " external_connection_catalog_versions WHERE scope_key=? AND kind='erp'",
            (_scope_key(registry.SCOPE_TENANT, tenant_id),),
        )
        revision = int(row[0]["revision"]) if row else 0
        connection_id = row[0]["default_connection_id"] if row else None
        return {"connection_id": connection_id, "revision": revision}

    def set_erp_default(self, *, actor_user_id: str, tenant_id: str,
                        connection_id: Optional[str], expected_revision: int
                        ) -> Dict[str, Any]:
        """Atomically set or clear the tenant's default ERP connection (CAS)."""
        self.require_tenant_manage(actor_user_id, tenant_id)
        self._refuse_if_paused(scope=registry.SCOPE_TENANT,
                               tenant_id=tenant_id, what="connection changes")
        scope_key = _scope_key(registry.SCOPE_TENANT, tenant_id)
        with self._tx() as con:
            current = con.execute(
                "SELECT revision, default_connection_id FROM"
                " external_connection_catalog_versions"
                " WHERE scope_key=? AND kind='erp'", (scope_key,),
            ).fetchone()
            revision = int(current["revision"]) if current else 1
            if int(expected_revision) != revision:
                raise conflict("catalog changed; reload before saving",
                               code="catalog_version_conflict")
            if connection_id is not None:
                target = self._row_in_tx(con, connection_id,
                                         scope=registry.SCOPE_TENANT,
                                         tenant_id=tenant_id)
                if target is None or target["kind"] != registry.KIND_ERP:
                    raise invalid("default must be an ERP connection in this tenant",
                                  code="default_not_erp")
                if not int(target["enabled"]):
                    raise invalid("a disabled connection cannot be the default",
                                  code="default_disabled")
            new_revision = revision + 1
            con.execute(
                "INSERT INTO external_connection_catalog_versions"
                " (scope_key, kind, revision, default_connection_id, updated_at)"
                " VALUES (?, 'erp', ?, ?, ?)"
                " ON CONFLICT(scope_key, kind) DO UPDATE SET"
                " revision=excluded.revision,"
                " default_connection_id=excluded.default_connection_id,"
                " updated_at=excluded.updated_at",
                (scope_key, new_revision, connection_id, _now()),
            )
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id, action="external_connection.erp_default",
                target="external_connection_catalog:%s" % scope_key,
                redacted_changes={"default_connection_id": connection_id,
                                  "revision": new_revision},
                result="success")
            con.commit()
        return {"connection_id": connection_id, "revision": new_revision}

    def _handle_default_loss(self, con, *, row: Mapping[str, Any], tenant_id: str,
                             handling: Optional[Mapping[str, Any]]) -> None:
        """Require an explicit decision when the default ERP link is affected."""
        scope_key = _scope_key(registry.SCOPE_TENANT, tenant_id)
        current = con.execute(
            "SELECT revision, default_connection_id FROM"
            " external_connection_catalog_versions"
            " WHERE scope_key=? AND kind='erp'", (scope_key,),
        ).fetchone()
        if not current or current["default_connection_id"] != row["id"]:
            return
        if not handling:
            raise conflict(
                "this connection is the ERP default; choose a replacement or"
                " confirm clearing it", code="default_requires_decision")
        action = str(handling.get("action") or "")
        if action == "clear":
            replacement = None
        elif action == "replace":
            replacement = str(handling.get("connection_id") or "")
            target = self._row_in_tx(con, replacement,
                                     scope=registry.SCOPE_TENANT,
                                     tenant_id=tenant_id)
            if (target is None or target["kind"] != registry.KIND_ERP
                    or not int(target["enabled"]) or target["id"] == row["id"]):
                raise invalid("replacement default must be another enabled ERP"
                              " connection in this tenant",
                              code="default_replacement_invalid")
        else:
            raise invalid("default_handling.action must be clear or replace",
                          code="default_handling_invalid")
        con.execute(
            "UPDATE external_connection_catalog_versions SET revision=revision+1,"
            " default_connection_id=?, updated_at=? WHERE scope_key=? AND kind='erp'",
            (replacement, _now(), scope_key),
        )

    # -- platform override / tenant access ---------------------------------

    def create_override(self, *, actor_user_id: str, tenant_id: str,
                        platform_connection_id: str, name: str,
                        config: Mapping[str, Any],
                        secrets: Optional[Mapping[str, Any]] = None,
                        idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """A tenant's explicit override of a platform MCP template.

        The override carries the tenant's *own* complete configuration and
        credentials; the platform template's secret is never copied, read or
        borrowed (spec ``mcp-connection-integration``).
        """
        return self.create_connection(
            actor_user_id=actor_user_id, scope=registry.SCOPE_TENANT,
            kind=registry.KIND_MCP, tenant_id=tenant_id, name=name,
            config=config, secrets=secrets, idempotency_key=idempotency_key,
            base_connection_id=platform_connection_id)

    def restore_inheritance(self, *, actor_user_id: str, tenant_id: str,
                            platform_connection_id: str,
                            expected_version: int) -> Dict[str, Any]:
        """Atomically remove this tenant's override and fall back to inheriting."""
        self.require_tenant_manage(actor_user_id, tenant_id)
        self._refuse_if_paused(scope=registry.SCOPE_TENANT,
                               tenant_id=tenant_id, what="connection changes")
        with self._tx() as con:
            rows = con.execute(
                "SELECT * FROM external_connections WHERE tenant_id=?"
                " AND base_connection_id=? AND deleted_at IS NULL",
                (tenant_id, platform_connection_id),
            ).fetchall()
            if not rows:
                raise not_found()
            row = rows[0]
            if int(row["version"]) != int(expected_version):
                raise conflict("connection was modified by another writer",
                               code="version_conflict")
            self._deactivate_secrets_in_tx(con, row["id"])
            con.execute(
                "UPDATE external_connections SET deleted_at=?, enabled=0,"
                " version=version+1, updated_at=? WHERE id=?",
                (_now(), _now(), row["id"]),
            )
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=tenant_id,
                target_tenant_id=tenant_id,
                action="external_connection.override_restore",
                target="external_connection:%s" % row["id"],
                redacted_changes={"platform_connection_id": platform_connection_id},
                result="success")
            con.commit()
        return {"id": row["id"], "restored": True}

    def list_tenant_access(self, *, actor_user_id: str,
                           platform_connection_id: str) -> Dict[str, Any]:
        self.require_platform_admin(actor_user_id)
        row = self._fetch_row(platform_connection_id, scope=registry.SCOPE_PLATFORM)
        if row is None:
            raise not_found()
        rows = self._store.execute(
            "SELECT * FROM external_connection_tenant_access"
            " WHERE platform_connection_id=? ORDER BY tenant_id",
            (platform_connection_id,),
        )
        revision = int(max((r["revision"] for r in rows), default=1))
        return {
            "platform_connection_id": platform_connection_id,
            "revision": revision,
            "tenants": [{"tenant_id": r["tenant_id"], "enabled": bool(r["enabled"])}
                        for r in rows],
        }

    def set_tenant_access(self, *, actor_user_id: str,
                          platform_connection_id: str,
                          tenant_ids: Iterable[str], expected_revision: int
                          ) -> Dict[str, Any]:
        """Replace the explicit list of tenants allowed to consume a platform MCP.

        Replacing the whole list (rather than toggling one row) is what makes a
        revocation atomic and auditable: a tenant left out is refused on the
        next use, and the revision the console read is enforced.
        """
        self.require_platform_admin(actor_user_id)
        self._refuse_if_paused(scope=registry.SCOPE_PLATFORM, tenant_id=None,
                               what="connection changes")
        wanted = sorted({str(t).strip() for t in tenant_ids if str(t).strip()})
        for tenant_id in wanted:
            if not self._store.execute("SELECT 1 FROM tenants WHERE id=? AND active=1",
                                       (tenant_id,)):
                raise invalid("unknown tenant", code="unknown_tenant",
                              fields={"tenant_ids": "unknown"})
        with self._tx() as con:
            row = self._row_in_tx(con, platform_connection_id,
                                  scope=registry.SCOPE_PLATFORM, tenant_id=None)
            if row is None or row["kind"] != registry.KIND_MCP:
                raise not_found()
            existing = con.execute(
                "SELECT tenant_id, revision FROM external_connection_tenant_access"
                " WHERE platform_connection_id=?", (platform_connection_id,),
            ).fetchall()
            revision = int(max((r["revision"] for r in existing), default=1))
            if int(expected_revision) != revision:
                raise conflict("tenant access changed; reload before saving",
                               code="catalog_version_conflict")
            new_revision = revision + 1
            con.execute(
                "DELETE FROM external_connection_tenant_access"
                " WHERE platform_connection_id=?", (platform_connection_id,))
            for tenant_id in wanted:
                con.execute(
                    "INSERT INTO external_connection_tenant_access"
                    " (platform_connection_id, tenant_id, enabled, revision,"
                    "  updated_at) VALUES (?,?,1,?,?)",
                    (platform_connection_id, tenant_id, new_revision, _now()),
                )
                if row["kind"] == registry.KIND_MCP:
                    # 平台模板首次新授予某租户: 初始已配置且空分配. A re-grant
                    # preserves whatever state the tenant already had.
                    self._ensure_assignment_set(
                        con, tenant_id=tenant_id,
                        logical_id=platform_connection_id)
            self._audit_in_tx(
                con, actor_user_id=actor_user_id, tenant_id=None,
                target_tenant_id=None,
                action="external_connection.tenant_access",
                target="external_connection:%s" % platform_connection_id,
                redacted_changes={"tenant_count": len(wanted),
                                  "revision": new_revision},
                result="success")
            con.commit()
        return {"platform_connection_id": platform_connection_id,
                "revision": new_revision, "tenant_ids": wanted}

    # -- secrets ------------------------------------------------------------

    def _secret_name(self, connection_id: str, slot: str) -> str:
        return "conn:%s:%s" % (connection_id, slot)

    def _write_secrets(self, con, *, connection_id: str, kind: str, scope: str,
                       tenant_id: Optional[str], owner_user_id: Optional[str],
                       actor_user_id: str,
                       secrets: Optional[Mapping[str, Any]],
                       require_values: bool) -> None:
        """Apply keep / replace / clear to the connection's secret slots."""
        if not secrets:
            return
        allowed = registry.secret_slots(kind)
        for raw_slot, value in secrets.items():
            slot = str(raw_slot or "").strip()
            if slot not in allowed:
                raise invalid("unknown secret slot %r for %s" % (slot, kind),
                              code="unknown_secret_slot",
                              fields={slot: "unknown"})
            if value is None:
                self._clear_secret_in_tx(con, connection_id=connection_id,
                                         slot=slot, tenant_id=tenant_id)
                continue
            if not isinstance(value, str):
                raise invalid("secret values must be strings",
                              code="field_type", fields={slot: "type"})
            text = value
            if "••••" in text:
                # A masked projection is not a secret; storing it would replace
                # the real credential with its own mask.
                raise invalid("refusing to store a masked value",
                              code="masked_secret", fields={slot: "masked"})
            text = text.strip()
            if not text:
                # Blank means "unchanged", the same as an omitted field.
                continue
            if scope == registry.SCOPE_PLATFORM:
                self._put_platform_secret(con, connection_id=connection_id,
                                          slot=slot, value=text,
                                          actor_user_id=actor_user_id)
            else:
                self._put_credential_secret(
                    con, connection_id=connection_id, slot=slot,
                    tenant_id=tenant_id, owner_user_id=owner_user_id,
                    actor_user_id=actor_user_id, value=text)

    def _put_credential_secret(self, con, *, connection_id: str, slot: str,
                               tenant_id: Optional[str],
                               owner_user_id: Optional[str],
                               actor_user_id: str, value: str) -> int:
        from auth.crypto import encrypt_secret

        name = self._secret_name(connection_id, slot)
        try:
            ciphertext = encrypt_secret(value)
        except Exception as error:  # noqa: BLE001 - no key => refuse to store
            raise unavailable("credential encryption unavailable: %s" % error,
                              code="credential_crypto")
        existing = con.execute(
            "SELECT id, version FROM credentials WHERE tenant_id=? AND name=?",
            (tenant_id, name),
        ).fetchone()
        if existing:
            credential_id = existing["id"]
            next_version = con.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 v FROM credential_versions"
                " WHERE credential_id=?", (credential_id,)).fetchone()["v"]
            con.execute(
                "INSERT INTO credential_versions(credential_id, version,"
                " ciphertext, action, changed_by) VALUES (?,?,?,?,?)",
                (credential_id, next_version, ciphertext, "rotated", actor_user_id))
            con.execute(
                "UPDATE credentials SET ciphertext=?, version=?, active=1,"
                " updated_at=unixepoch() WHERE id=?",
                (ciphertext, next_version, credential_id))
        else:
            credential_id = "cred_%s" % _random_token()
            con.execute(
                "INSERT INTO credentials(id, tenant_id, name, resource_kind,"
                " resource_id, ciphertext, active, version, created_by,"
                " owner_user_id) VALUES (?,?,?,'external_connection',?,?,1,1,?,?)",
                (credential_id, tenant_id, name, connection_id, ciphertext,
                 actor_user_id, owner_user_id))
            con.execute(
                "INSERT INTO credential_versions(credential_id, version, ciphertext,"
                " action, changed_by) VALUES (?,1,?,?,?)",
                (credential_id, ciphertext, "create", actor_user_id))
            next_version = 1
        con.execute(
            "INSERT INTO external_connection_secret_refs"
            " (connection_id, slot, credential_id, platform_secret_id,"
            "  secret_version, updated_at) VALUES (?,?,?,NULL,?,?)"
            " ON CONFLICT(connection_id, slot) DO UPDATE SET"
            " credential_id=excluded.credential_id, platform_secret_id=NULL,"
            " secret_version=excluded.secret_version, updated_at=excluded.updated_at",
            (connection_id, slot, credential_id, next_version, _now()),
        )
        return next_version

    def _put_platform_secret(self, con, *, connection_id: str, slot: str,
                             value: str, actor_user_id: str) -> int:
        from auth.crypto import encrypt_secret

        name = self._secret_name(connection_id, slot)
        try:
            ciphertext = encrypt_secret(value)
        except Exception as error:  # noqa: BLE001
            raise unavailable("credential encryption unavailable: %s" % error,
                              code="credential_crypto")
        existing = con.execute(
            "SELECT id, version FROM platform_connection_secrets WHERE name=?",
            (name,)).fetchone()
        if existing:
            secret_id = existing["id"]
            next_version = con.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 v FROM"
                " platform_connection_secret_versions WHERE platform_secret_id=?",
                (secret_id,)).fetchone()["v"]
            con.execute(
                "INSERT INTO platform_connection_secret_versions"
                " (platform_secret_id, version, ciphertext, action, changed_by)"
                " VALUES (?,?,?,?,?)",
                (secret_id, next_version, ciphertext, "rotated", actor_user_id))
            con.execute(
                "UPDATE platform_connection_secrets SET ciphertext=?, version=?,"
                " active=1, updated_at=unixepoch() WHERE id=?",
                (ciphertext, next_version, secret_id))
        else:
            secret_id = "psec_%s" % _random_token()
            con.execute(
                "INSERT INTO platform_connection_secrets"
                " (id, name, ciphertext, active, version, created_by)"
                " VALUES (?,?,?,1,1,?)",
                (secret_id, name, ciphertext, actor_user_id))
            con.execute(
                "INSERT INTO platform_connection_secret_versions"
                " (platform_secret_id, version, ciphertext, action, changed_by)"
                " VALUES (?,1,?,?,?)",
                (secret_id, ciphertext, "create", actor_user_id))
            next_version = 1
        con.execute(
            "INSERT INTO external_connection_secret_refs"
            " (connection_id, slot, credential_id, platform_secret_id,"
            "  secret_version, updated_at) VALUES (?,?,NULL,?,?,?)"
            " ON CONFLICT(connection_id, slot) DO UPDATE SET"
            " credential_id=NULL, platform_secret_id=excluded.platform_secret_id,"
            " secret_version=excluded.secret_version, updated_at=excluded.updated_at",
            (connection_id, slot, secret_id, next_version, _now()),
        )
        return next_version

    def _clear_secret_in_tx(self, con, *, connection_id: str, slot: str,
                            tenant_id: Optional[str]) -> None:
        ref = con.execute(
            "SELECT credential_id, platform_secret_id FROM"
            " external_connection_secret_refs WHERE connection_id=? AND slot=?",
            (connection_id, slot)).fetchone()
        if ref is None:
            return
        if ref["credential_id"]:
            con.execute("UPDATE credentials SET active=0, updated_at=unixepoch()"
                        " WHERE id=?", (ref["credential_id"],))
        if ref["platform_secret_id"]:
            con.execute("UPDATE platform_connection_secrets SET active=0,"
                        " updated_at=unixepoch() WHERE id=?",
                        (ref["platform_secret_id"],))
        con.execute("DELETE FROM external_connection_secret_refs"
                    " WHERE connection_id=? AND slot=?", (connection_id, slot))

    def _deactivate_secrets_in_tx(self, con, connection_id: str) -> None:
        for ref in con.execute(
                "SELECT credential_id, platform_secret_id FROM"
                " external_connection_secret_refs WHERE connection_id=?",
                (connection_id,)).fetchall():
            if ref["credential_id"]:
                con.execute("UPDATE credentials SET active=0, updated_at=unixepoch()"
                            " WHERE id=?", (ref["credential_id"],))
            if ref["platform_secret_id"]:
                con.execute("UPDATE platform_connection_secrets SET active=0,"
                            " updated_at=unixepoch() WHERE id=?",
                            (ref["platform_secret_id"],))
        con.execute("DELETE FROM external_connection_secret_refs"
                    " WHERE connection_id=?", (connection_id,))

    @staticmethod
    def _secret_presence_in_tx(con, connection_id: str) -> Dict[str, bool]:
        return {
            row["slot"]: True for row in con.execute(
                "SELECT slot FROM external_connection_secret_refs"
                " WHERE connection_id=?", (connection_id,)).fetchall()
        }

    def resolve_secret(self, *, connection_id: str, slot: str,
                       scope: str, tenant_id: Optional[str] = None,
                       actor_user_id: Optional[str] = None) -> str:
        """Resolve one secret slot at a controlled call point.

        Deliberately **not** reachable over HTTP at this stage: the only caller
        is a type adapter running under a verified execution context (later task
        groups), which passes the connection/scope it has already authorized.
        Tenant/personal secrets go through the identity credential table; the
        platform secret is read from its own table, so a tenant credential API
        can never return it.

        A personal connection additionally requires the caller to *be* its
        owner. Without this the resolver would hand a mailbox password to anyone
        who could name the connection id, and the protection would rest entirely
        on the listing layer having re-derived the right bindings — a
        defence-in-depth gap rather than a rule. The row is fetched here rather
        than in the caller so every present and future call point gets the same
        check.
        """
        from auth.crypto import decrypt_secret

        if scope == registry.SCOPE_PERSONAL:
            row = self._store.execute(
                "SELECT owner_user_id FROM external_connections WHERE id=?",
                (connection_id,))
            if not row:
                raise not_found("secret slot is not configured")
            owner = row[0]["owner_user_id"]
            if not actor_user_id or owner != actor_user_id:
                # ``not_found`` rather than ``forbidden``: another member's
                # mailbox must not be confirmable by probing id.
                raise not_found("secret slot is not configured")

        ref = self._store.execute(
            "SELECT * FROM external_connection_secret_refs"
            " WHERE connection_id=? AND slot=?", (connection_id, slot))
        if not ref:
            raise not_found("secret slot is not configured")
        ref = ref[0]
        if scope == registry.SCOPE_PLATFORM:
            row = self._store.execute(
                "SELECT ciphertext, active FROM platform_connection_secrets"
                " WHERE id=?", (ref["platform_secret_id"],))
            if not row or not row[0]["active"]:
                raise not_found("secret slot is not configured")
            return decrypt_secret(row[0]["ciphertext"])
        row = self._store.execute(
            "SELECT ciphertext, active FROM credentials WHERE id=?",
            (ref["credential_id"],))
        if not row or not row[0]["active"]:
            raise not_found("secret slot is not configured")
        return decrypt_secret(row[0]["ciphertext"])

    # -- internals ----------------------------------------------------------

    def _validated_name(self, name: Any) -> str:
        value = str(name or "").strip()
        if not value:
            raise invalid("name is required", code="field_required",
                          fields={"name": "required"})
        if len(value) > MAX_NAME_LENGTH:
            raise invalid("name is too long", code="field_too_long",
                          fields={"name": "too_long"})
        return value

    @contextmanager
    def _tx(self):
        """One writer transaction: ``BEGIN IMMEDIATE`` so writers serialise.

        The guard closes the connection on exit; the explicit commit inside the
        block is the one that matters, and the extra commit here is a no-op.
        """
        with self._store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
            except Exception:
                con.rollback()
                raise
            con.commit()

    def _audit_in_tx(self, con, **kwargs) -> None:
        self._audit.record(con=con, **kwargs)

    def _row_in_tx(self, con, connection_id: str, *, scope: str,
                   tenant_id: Optional[str]) -> Optional[Mapping[str, Any]]:
        sql = ("SELECT * FROM external_connections WHERE id=? AND scope=?"
               " AND deleted_at IS NULL")
        params: List[Any] = [connection_id, scope]
        if tenant_id is not None:
            sql += " AND tenant_id=?"
            params.append(tenant_id)
        row = con.execute(sql, tuple(params)).fetchone()
        return row

    def _ensure_catalog_row(self, con, scope_key: str, kind: str) -> None:
        con.execute(
            "INSERT INTO external_connection_catalog_versions"
            " (scope_key, kind, revision, default_connection_id)"
            " VALUES (?, ?, 1, NULL)"
            " ON CONFLICT(scope_key, kind) DO NOTHING",
            (scope_key, kind),
        )

    def _ensure_singleton_free(self, con, *, scope: str, kind: str,
                               tenant_id: Optional[str],
                               owner_user_id: Optional[str],
                               base_connection_id: Optional[str] = None) -> None:
        """Refuse a second OA / email row before the index has to.

        The partial unique indexes are the real guarantee (they also hold under
        concurrency); this pre-check turns the collision into the machine code
        the console expects instead of a raw integrity error.
        """
        if scope == registry.SCOPE_TENANT and kind == registry.KIND_OA:
            if con.execute(
                    "SELECT 1 FROM external_connections WHERE tenant_id=? AND"
                    " kind='oa' AND deleted_at IS NULL", (tenant_id,)).fetchone():
                raise conflict("this tenant already has an OA connection",
                               code="singleton_exists")
        if scope == registry.SCOPE_PERSONAL and kind == registry.KIND_EMAIL:
            if con.execute(
                    "SELECT 1 FROM external_connections WHERE tenant_id=? AND"
                    " owner_user_id=? AND kind='email' AND deleted_at IS NULL",
                    (tenant_id, owner_user_id)).fetchone():
                raise conflict("you already have a mailbox connection",
                               code="singleton_exists")
        if (scope == registry.SCOPE_TENANT and kind == registry.KIND_MCP
                and base_connection_id):
            if con.execute(
                    "SELECT 1 FROM external_connections WHERE tenant_id=? AND"
                    " base_connection_id=? AND deleted_at IS NULL",
                    (tenant_id, base_connection_id)).fetchone():
                raise conflict(
                    "this tenant already overrides that platform connection",
                    code="override_exists")

    def _require_platform_source(self, con, platform_connection_id: str) -> None:
        row = con.execute(
            "SELECT kind, scope, enabled FROM external_connections WHERE id=?"
            " AND deleted_at IS NULL", (platform_connection_id,)).fetchone()
        if row is None or row["scope"] != registry.SCOPE_PLATFORM \
                or row["kind"] != registry.KIND_MCP:
            raise not_found("platform connection not found")
        if not int(row["enabled"]):
            raise conflict("the platform connection is disabled",
                           code="platform_source_disabled")

    def _references_in_tx(self, con, row: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """Active references that make an unforced delete unsafe."""
        refs: List[Dict[str, Any]] = []
        if row["scope"] == registry.SCOPE_TENANT and row["kind"] == registry.KIND_ERP:
            default = con.execute(
                "SELECT scope_key FROM external_connection_catalog_versions"
                " WHERE default_connection_id=?", (row["id"],)).fetchone()
            if default:
                refs.append({"kind": "erp_default",
                             "scope_key": default["scope_key"],
                             "next_action": "set_erp_default"})
        if row["scope"] == registry.SCOPE_PLATFORM:
            overrides = con.execute(
                "SELECT COUNT(*) c FROM external_connections WHERE"
                " base_connection_id=? AND deleted_at IS NULL", (row["id"],),
            ).fetchone()["c"]
            if overrides:
                refs.append({"kind": "tenant_overrides", "count": int(overrides),
                             "next_action": "restore_inheritance"})
            access = con.execute(
                "SELECT COUNT(*) c FROM external_connection_tenant_access"
                " WHERE platform_connection_id=?", (row["id"],)).fetchone()["c"]
            if access:
                refs.append({"kind": "tenant_access", "count": int(access),
                             "next_action": "set_tenant_access"})
        return refs

    # -- idempotency --------------------------------------------------------

    def _idempotency_key_id(self, key: str) -> str:
        from auth.crypto import fingerprint
        return fingerprint(str(key))

    def _payload_fingerprint(self, payload: Mapping[str, Any]) -> str:
        from auth.crypto import fingerprint
        return fingerprint(json.dumps(payload, sort_keys=True, default=str,
                                      ensure_ascii=False))

    def _idempotent_replay(self, *, actor_user_id: str, scope_key: str,
                           endpoint: str, key: str,
                           payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        key_id = self._idempotency_key_id(key)
        rows = self._store.execute(
            "SELECT payload_fingerprint, result_json, created_at FROM"
            " external_connection_idempotency WHERE actor_user_id=? AND"
            " scope_key=? AND endpoint=? AND key_id=?",
            (actor_user_id, scope_key, endpoint, key_id),
        )
        if not rows:
            return None
        row = rows[0]
        if _now() - int(row["created_at"]) > IDEMPOTENCY_TTL_SECONDS:
            return None
        if row["payload_fingerprint"] != self._payload_fingerprint(payload):
            raise conflict("this idempotency key was used with a different body",
                           code="idempotency_conflict")
        return json.loads(row["result_json"])

    def _remember_idempotent(self, *, actor_user_id: str, scope_key: str,
                             endpoint: str, key: str,
                             payload: Mapping[str, Any],
                             result: Mapping[str, Any]) -> None:
        with self._tx() as con:
            con.execute(
                "DELETE FROM external_connection_idempotency WHERE actor_user_id=?"
                " AND scope_key=? AND endpoint=? AND created_at<?",
                (actor_user_id, scope_key, endpoint,
                 _now() - IDEMPOTENCY_TTL_SECONDS),
            )
            con.execute(
                "INSERT OR REPLACE INTO external_connection_idempotency"
                " (key_id, actor_user_id, scope_key, endpoint,"
                "  payload_fingerprint, result_json, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (self._idempotency_key_id(key), actor_user_id, scope_key,
                 endpoint, self._payload_fingerprint(payload),
                 json.dumps(dict(result), ensure_ascii=False, default=str),
                 _now()),
            )
            con.commit()

    # -- runtime access ------------------------------------------------------

    def runtime(self):
        """The adapter-facing runtime over this service.

        A method rather than a module-level singleton so the runtime inherits
        *this* service's store, clock and audit sink — the console request and
        the agent call then write to the same database without either reaching
        for a global.
        """
        from integrations.external.runtime import ConnectionRuntime
        return ConnectionRuntime(self)

    def probe_connection(self, connection_id: str, *, actor_user_id: str,
                         tenant_id: Optional[str] = None,
                         agent_id: str = "", run_id: str = "",
                         draft: bool = False,
                         expected_version: Optional[int] = None,
                         record: bool = True) -> Dict[str, Any]:
        """Bounded connectivity/authentication test for one connection.

        Refused with ``test_not_available`` unless the deployment opened the
        type's ``test`` class, so this cannot become a way around the slice
        switch.
        """
        return self.runtime().probe(
            connection_id, actor_user_id=actor_user_id, tenant_id=tenant_id,
            agent_id=agent_id, run_id=run_id, draft=draft,
            expected_version=expected_version, record=record)

    def probe_draft(self, kind: str, config: Mapping[str, Any], *,
                    secrets: Optional[Mapping[str, Any]] = None,
                    actor_user_id: str, tenant_id: Optional[str] = None,
                    scope: str = registry.SCOPE_TENANT,
                    owner_user_id: Optional[str] = None) -> Dict[str, Any]:
        """Test an unsaved form without persisting the config or the secret.

        The secret values are supplied by the caller and used for this one
        attempt: they are never written to the credential store, and no test
        summary is recorded. See :meth:`ConnectionRuntime.probe_draft`.
        """
        return self.runtime().probe_draft(
            kind, config, secrets=secrets, actor_user_id=actor_user_id,
            tenant_id=tenant_id, scope=scope, owner_user_id=owner_user_id)

    def describe_connection(self, connection_id: str, *,
                            tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Runtime limits, network policy and adapter capabilities."""
        return self.runtime().describe(connection_id, tenant_id=tenant_id)

    def _test_states(self, wanted: Mapping[str, Any]
                     ) -> Dict[str, Dict[str, Any]]:
        """The current test state per connection id, from the single decision point.

        States are resolved in one batch and keyed by the row that is *in force*,
        so a page of cards costs a bounded number of queries no matter how many
        connections it holds.

        A failure to read is reported as ``untested``: a badge is not worth
        failing the whole catalogue over, and losing the verdict is better than
        losing the page. The warning is emitted so the degradation is not silent.
        """
        from integrations.external.runtime import test_summary_payload

        wanted = {str(k): int(v) for k, v in dict(wanted or {}).items()}
        found: Mapping[str, Any] = {}
        if wanted:
            try:
                found = self.runtime().test_observations(wanted)
            except Exception as error:  # noqa: BLE001 - degrade, do not fail
                logger.warning(
                    "[ExternalConnections] test state lookup failed: %s", error)
        out: Dict[str, Dict[str, Any]] = {}
        for connection_id in wanted:
            observation = found.get(connection_id) or {}
            out[connection_id] = test_summary_payload(
                _now(), observation.get("record") or {},
                stale_at=observation.get("stale_at"))
        return out

    def _test_state_for(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        """The test state for one row, bound to that row's version."""
        states = self._test_states({str(row["id"]): int(row["version"])})
        return states[str(row["id"])]

    def test_state(self, connection_id: str, *,
                   tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """The console's test badge for one connection, version-bound.

        Resolved against the *effective* row, so an inherited template reports
        the platform result it will actually run with, and an override never
        borrows the template's verdict.
        """
        snapshot = self.runtime().snapshot(connection_id, tenant_id=tenant_id)
        states = self._test_states({str(snapshot.id): int(snapshot.version)})
        return states[str(snapshot.id)]

    def invoke_action(self, connection_id: str, action: str,
                      params: Mapping[str, Any], *, actor_user_id: str,
                      tenant_id: Optional[str] = None, agent_id: str = "",
                      run_id: str = "",
                      approval: Optional[Mapping[str, Any]] = None):
        """Run a business action, re-authorizing the connection per call."""
        from integrations.external.risk import check_invocation
        return self.runtime().invoke(
            connection_id, action, params, actor_user_id=actor_user_id,
            tenant_id=tenant_id, agent_id=agent_id, run_id=run_id,
            approval=approval, risk_check=check_invocation)


def _random_token() -> str:
    import secrets
    return secrets.token_urlsafe(12)


#: What an explicitly supplied agent id may look like. Narrower than "any text"
#: on purpose: the id is a primary-key component of the relation and is echoed
#: back in audit targets, so a hostile body must not be able to store an
#: unbounded blob. The binding lookup still decides whether it names anything.
_AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")


def _validated_agent_ids(values: Any, field: str) -> List[str]:
    """A de-duplicated list of shape-valid agent ids, or a 400.

    A malformed or over-long id refuses the whole request rather than being
    silently dropped: 整笔拒绝 is the spec's answer for an illegal delta.
    """
    out: List[str] = []
    if values is None:
        return out
    if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
        raise invalid("expected a list of agent ids", code="field_type",
                      fields={field: "type"})
    seen = set()
    for raw in values:
        text = str(raw or "").strip()
        if not text or len(text) > 128 or not _AGENT_ID_PATTERN.match(text):
            raise invalid("invalid agent id", code="field_invalid",
                          fields={field: "invalid"})
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _normalize_name(name: Any) -> str:
    return str(name or "").casefold()


def _agent_matches(needle: str, item: Mapping[str, Any]) -> bool:
    return (needle in _normalize_name(item.get("name"))
            or needle in str(item.get("id") or "").lower())


#: What an explicitly supplied connection id may look like.
#:
#: Deliberately narrower than "any text": the value is the row's primary key,
#: it is what every stored reference points at, and it ends up in audit targets
#: and URLs. A legacy id that does not match is *not* an error — the caller
#: falls back to a generated id and the ledger records the mapping — because
#: refusing the import over an id shape would leave the record unmigrated, which
#: is worse than the reference churn the reuse is trying to avoid.
_CONNECTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:~-]{0,159}$")


def _validated_connection_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not _CONNECTION_ID_PATTERN.match(text):
        return ""
    return text


def reusable_connection_id(value: Any) -> bool:
    """Whether ``value`` may be reused as a connection id (used by migration)."""
    return bool(_validated_connection_id(value))


_SERVICE_CACHE: Dict[str, "ExternalConnectionService"] = {}


def get_external_connection_service() -> "ExternalConnectionService":
    """The process-wide service over the configured identity database.

    The path is resolved exactly as ``channel/web/auth_handlers._get_service``
    resolves it (configured ``identity_db_path``, else ``<data root>/identity.db``)
    so the HTTP layer and this service can never end up on two different
    databases — which would show up as "the connection I just saved is gone".
    """
    from auth.service import IdentityService
    from config import conf, get_data_root
    import os

    db_path = conf().get("identity_db_path") or os.path.join(
        get_data_root(), "identity.db")
    service = _SERVICE_CACHE.get(db_path)
    if service is None:
        service = ExternalConnectionService(IdentityService(db_path))
        _SERVICE_CACHE[db_path] = service
    return service
