# encoding:utf-8
"""Action risk catalogue and the pre-dispatch re-check.

The spec's requirements for this layer, and what each one turns into here:

``建立动作风险目录，未知 MCP 工具默认高风险，不信任远端 readonly 声明``
    :data:`RISK_CATALOGUE` classifies *our* actions. An MCP tool is classified
    from the protocol and our own policy, never from the server's
    ``readOnlyHint`` — a remote server's self-description is data, not
    authority. A tool nobody classified is :data:`RISK_HIGH`, not unknown-but-
    fine.

``将连接/秘密版本、主体/租户、目标对象、规范化参数和附件摘要绑定审批``
    :func:`approval_binding` builds the digest from exactly those facts, and
    :func:`check_invocation` recomputes it immediately before dispatch.

``实现派发前重校，覆盖审批人分离、过期、撤销和参数变化``
    :func:`verify_approval` refuses when the approver is the requester, when the
    approval expired, when it was revoked, or when the digest no longer
    matches. Each has its own code so the console can say which.

``验证管理页面、原工具入口、Channel、调度不能旁路同一动作规则``
    There is one :func:`check_invocation`, and every entry point reaches the
    adapter through ``ConnectionRuntime.invoke``, which calls it. A second entry
    point would have to bypass ``invoke`` altogether to skip this.

Deliberately not here: the approval *storage*. This module consumes the
existing ``agent/approval_gate`` records rather than keeping its own, so an
approval granted through the established flow is the same object this checks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from integrations.external import registry
from integrations.external.errors import forbidden, invalid

# -- risk vocabulary ---------------------------------------------------------

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_CRITICAL = "critical"

RISK_LEVELS: Tuple[str, ...] = (RISK_LOW, RISK_MEDIUM, RISK_HIGH, RISK_CRITICAL)

#: Levels that require a human approval before dispatch. ``critical`` and
#: ``high`` both do; the difference is how much the console has to show, not
#: whether a human is in the loop.
APPROVAL_REQUIRED: FrozenSet[str] = frozenset({RISK_HIGH, RISK_CRITICAL})

#: Levels a deployment may not leave open. A critical action stays closed even
#: when its execution class is open, unless an operator explicitly accepts it.
ACKNOWLEDGEMENT_REQUIRED: FrozenSet[str] = frozenset({RISK_CRITICAL})


@dataclass(frozen=True)
class RiskEntry:
    """One classified action."""

    kind: str
    action: str
    level: str
    #: Human-facing label key, so the console does not invent wording.
    label_key: str
    #: Whether this action changes the remote system. Comes from the adapter's
    #: own declaration, not from a guess about the action's name.
    write: bool = True
    #: Whether the action is reversible by the system itself.
    reversible: bool = True
    #: True when the action leaves the organisation (an email leaves the
    #: boundary), which is why it cannot be undone at all.
    leaves_boundary: bool = False
    #: Non-secret notes the approval screen shows.
    notes: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "action": self.action, "level": self.level,
            "label_key": self.label_key, "write": self.write,
            "reversible": self.reversible,
            "leaves_boundary": self.leaves_boundary, "notes": self.notes,
        }


#: The catalogue. Every write action an adapter declares must appear here; a
#: declared action with no entry is treated as :data:`RISK_HIGH` rather than
#: silently permitted, and a test pins that.
RISK_CATALOGUE: Dict[Tuple[str, str], RiskEntry] = {
    # -- MCP ---------------------------------------------------------------
    # An MCP tool's risk comes from what it does to *our* configuration and to
    # the remote system, which our policy decides. A remote ``readOnlyHint`` is
    # ignored on purpose: the server that declares it is the same server that
    # benefits from being trusted.
    (registry.KIND_MCP, "tools.list"): RiskEntry(
        kind=registry.KIND_MCP, action="tools.list", level=RISK_LOW,
        label_key="risk_mcp_tools_list", write=False, reversible=True,
        notes="List the tools a server offers. Read-only against the server."),
    (registry.KIND_MCP, "tools.read"): RiskEntry(
        kind=registry.KIND_MCP, action="tools.read", level=RISK_LOW,
        label_key="risk_mcp_tools_read", write=False, reversible=True,
        notes="Call an MCP tool the remote server publishes. Approval is not "
              "required. This is the only action a discovered tool is offered "
              "through, and nothing local decides whether a tool is safe: the "
              "remote server's own readOnlyHint grants and withholds nothing, "
              "and there is no per-tool declaration on the connection. What "
              "bounds it is the connection's assignment, the resource grant and "
              "the deployment's read_execute switch -- plus the requirement "
              "that the name be one this connection actually published."),
    (registry.KIND_MCP, "tools.call"): RiskEntry(
        kind=registry.KIND_MCP, action="tools.call", level=RISK_HIGH,
        label_key="risk_mcp_tools_call", write=True, reversible=False,
        notes="Call an MCP tool. The tool's own effect is not knowable from "
              "its name or its server's self-description, so this is treated "
              "as a write."),
    (registry.KIND_MCP, "resources.read"): RiskEntry(
        kind=registry.KIND_MCP, action="resources.read", level=RISK_LOW,
        label_key="risk_mcp_resources_read", write=False, reversible=True),
    # -- ERP ---------------------------------------------------------------
    (registry.KIND_ERP, "query"): RiskEntry(
        kind=registry.KIND_ERP, action="query", level=RISK_LOW,
        label_key="risk_erp_query", write=False, reversible=True,
        notes="Run a read-only query through the ERP provider."),
    (registry.KIND_ERP, "rfc.call"): RiskEntry(
        kind=registry.KIND_ERP, action="rfc.call", level=RISK_HIGH,
        label_key="risk_erp_rfc_call", write=True, reversible=False,
        notes="Invoke an SAP BAPI/RFC function. The function's own effect is "
              "not evaluated, so this is treated as a write."),
    # -- OA ----------------------------------------------------------------
    (registry.KIND_OA, "todos.list"): RiskEntry(
        kind=registry.KIND_OA, action="todos.list", level=RISK_LOW,
        label_key="risk_oa_todos_list", write=False, reversible=True),
    (registry.KIND_OA, "request.read"): RiskEntry(
        kind=registry.KIND_OA, action="request.read", level=RISK_LOW,
        label_key="risk_oa_request_read", write=False, reversible=True),
    (registry.KIND_OA, "request.flowlog"): RiskEntry(
        kind=registry.KIND_OA, action="request.flowlog", level=RISK_LOW,
        label_key="risk_oa_request_flowlog", write=False, reversible=True,
        notes="Read the approval trail. A read: it never marks the request "
              "read on the remote."),
    (registry.KIND_OA, "request.related"): RiskEntry(
        kind=registry.KIND_OA, action="request.related", level=RISK_LOW,
        label_key="risk_oa_request_related", write=False, reversible=True,
        notes="Follow the related applications a form links to. Each target is "
              "read through the caller's own remote session, so the remote's "
              "permission decides what comes back."),
    (registry.KIND_OA, "request.attachments"): RiskEntry(
        kind=registry.KIND_OA, action="request.attachments", level=RISK_LOW,
        label_key="risk_oa_request_attachments", write=False, reversible=True,
        notes="List a request's attachments, and fetch a named one into the "
              "caller's workspace. Only links this OA site serves are fetched."),
    (registry.KIND_OA, "cc.mark_read"): RiskEntry(
        kind=registry.KIND_OA, action="cc.mark_read", level=RISK_MEDIUM,
        label_key="risk_oa_cc_mark_read", write=True, reversible=True,
        notes="Mark a 抄送 as read. It removes the request from the user's "
              "待阅 list, which is a change they can see — so it is its own "
              "action rather than a side effect of reading the detail."),
    (registry.KIND_OA, "request.create"): RiskEntry(
        kind=registry.KIND_OA, action="request.create", level=RISK_MEDIUM,
        label_key="risk_oa_request_create", write=True, reversible=False,
        notes="Create a workflow request. It can be withdrawn, but the "
              "workflow has already seen it."),
    (registry.KIND_OA, "request.submit"): RiskEntry(
        kind=registry.KIND_OA, action="request.submit", level=RISK_HIGH,
        label_key="risk_oa_request_submit", write=True, reversible=False,
        notes="Approve or submit a workflow step. The decision is recorded and "
              "is visible to the people in the flow."),
    (registry.KIND_OA, "request.reject"): RiskEntry(
        kind=registry.KIND_OA, action="request.reject", level=RISK_HIGH,
        label_key="risk_oa_request_reject", write=True, reversible=False),
    (registry.KIND_OA, "request.forward"): RiskEntry(
        kind=registry.KIND_OA, action="request.forward", level=RISK_MEDIUM,
        label_key="risk_oa_request_forward", write=True, reversible=False,
        notes="Forward or delegate. The recipient is notified, so an "
              "ambiguous target is refused rather than guessed."),
    (registry.KIND_OA, "request.circulate"): RiskEntry(
        kind=registry.KIND_OA, action="request.circulate", level=RISK_MEDIUM,
        label_key="risk_oa_request_circulate", write=True, reversible=False),
    (registry.KIND_OA, "request.addsign"): RiskEntry(
        kind=registry.KIND_OA, action="request.addsign", level=RISK_HIGH,
        label_key="risk_oa_request_addsign", write=True, reversible=False),
    # -- email -------------------------------------------------------------
    (registry.KIND_EMAIL, "messages.search"): RiskEntry(
        kind=registry.KIND_EMAIL, action="messages.search", level=RISK_LOW,
        label_key="risk_mail_search", write=False, reversible=True),
    (registry.KIND_EMAIL, "messages.read"): RiskEntry(
        kind=registry.KIND_EMAIL, action="messages.read", level=RISK_LOW,
        label_key="risk_mail_read", write=False, reversible=True),
    (registry.KIND_EMAIL, "attachments.save"): RiskEntry(
        kind=registry.KIND_EMAIL, action="attachments.save", level=RISK_MEDIUM,
        label_key="risk_mail_attachments_save", write=False, reversible=True,
        notes="Write an attachment into the caller's workspace. Bounded to the "
              "operator's configured directories."),
    (registry.KIND_EMAIL, "messages.mark"): RiskEntry(
        kind=registry.KIND_EMAIL, action="messages.mark", level=RISK_MEDIUM,
        label_key="risk_mail_mark", write=True, reversible=True,
        notes="Change read/unread state on the server. Separately authorized "
              "from reading, because it is a write the mailbox owner sees."),
    (registry.KIND_EMAIL, "messages.send"): RiskEntry(
        kind=registry.KIND_EMAIL, action="messages.send", level=RISK_CRITICAL,
        label_key="risk_mail_send", write=True, reversible=False,
        leaves_boundary=True,
        notes="Send mail. It cannot be recalled once delivered, so it always "
              "requires a matching approval and is never retried blindly."),
}

#: Actions an adapter declares but the catalogue does not classify. Treated as
#: high risk and requiring approval: "未知 MCP 工具默认高风险".
DEFAULT_WRITE_LEVEL = RISK_HIGH
DEFAULT_READ_LEVEL = RISK_MEDIUM


def lookup(kind: str, action: str, *, write: bool) -> RiskEntry:
    """The risk entry for an action, classifying an unknown one conservatively.

    The fallback matters more than the table: a newly added adapter action that
    nobody classified must not be low-risk by accident.
    """
    entry = RISK_CATALOGUE.get((str(kind), str(action)))
    if entry is not None:
        return entry
    level = DEFAULT_WRITE_LEVEL if write else DEFAULT_READ_LEVEL
    return RiskEntry(
        kind=str(kind), action=str(action), level=level,
        label_key="risk_unclassified", write=bool(write), reversible=False,
        notes="This action has no explicit classification, so it is treated "
              "conservatively and needs approval.")


def catalogue_projection() -> List[Dict[str, Any]]:
    return [RISK_CATALOGUE[key].as_dict() for key in sorted(RISK_CATALOGUE)]


def catalogue_for(kind: str) -> List[Dict[str, Any]]:
    return [entry.as_dict() for (entry_kind, _action), entry
            in RISK_CATALOGUE.items() if entry_kind == str(kind)]


# -- request canonicalisation ------------------------------------------------

#: Parameter names whose values are secrets or bulk content. A digest must
#: prove "the same request" without becoming a place a password or an entire
#: attachment could be recovered from.
_MASKED_KEYS = frozenset({
    "password", "passwd", "pass", "secret", "token", "authorization",
    "api_key", "apikey", "access_token", "refresh_token", "client_secret",
    "app_secret", "imap_password", "smtp_password", "credential",
})

#: A content value is summarised by digest rather than included.
_BULK_KEYS = frozenset({"body", "body_html", "content", "attachment",
                        "attachments", "payload", "file", "data"})

_MAX_CANONICAL_DEPTH = 6
_MAX_CANONICAL_ITEMS = 200


def _canonicalise(value: Any, key: str = "", depth: int = 0) -> Any:
    """A stable, redacted form of a parameter tree.

    Sorted keys, strings normalised, secrets replaced by a marker and bulk
    content replaced by its digest. Two requests that would do the same thing
    produce the same canonical form; two that differ in a way that matters
    produce different ones.
    """
    if depth > _MAX_CANONICAL_DEPTH:
        return "<depth>"
    lowered = str(key).lower()
    if lowered in _MASKED_KEYS:
        return "<masked>" if value not in (None, "", [], {}) else ""
    if lowered in _BULK_KEYS:
        if value in (None, ""):
            return ""
        blob = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          default=str)
        return {"sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32],
                "length": len(blob)}
    if isinstance(value, Mapping):
        return {str(k): _canonicalise(v, str(k), depth + 1)
                for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonicalise(item, key, depth + 1)
                for item in list(value)[:_MAX_CANONICAL_ITEMS]]
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    return str(value)


def canonical_parameters(params: Mapping[str, Any]) -> Dict[str, Any]:
    return _canonicalise(dict(params or {}))


def attachment_digest(params: Mapping[str, Any]) -> str:
    """A digest of the attachment *set*, never of the attachment bytes.

    The approval binds to which files will leave, which is what a human needs
    to approve; storing the bytes would put message content in the audit trail.
    """
    entries: List[Any] = []
    for name in ("attachments", "attachment"):
        value = (params or {}).get(name)
        if value in (None, "", [], {}):
            continue
        entries.append(_canonicalise(value, name))
    blob = json.dumps(entries, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _digest(parts: Mapping[str, Any], *, key: str = "") -> str:
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    secret = str(key or "").encode("utf-8")
    if secret:
        return hmac.new(secret, blob.encode("utf-8"), hashlib.sha256).hexdigest()
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def approval_binding(*, kind: str, action: str, connection_id: str,
                     config_version: int,
                     secret_versions: Optional[Mapping[str, int]] = None,
                     actor_user_id: str, tenant_id: Optional[str],
                     target: Optional[Mapping[str, Any]] = None,
                     params: Optional[Mapping[str, Any]] = None,
                     key: str = "") -> Dict[str, Any]:
    """The facts an approval is bound to.

    Every input here is a fact that would make the approved action *different*
    if it changed: which connection at which version, which secret it would use,
    who is asking, for which tenant, which remote object, which normalized
    parameters and which attachments. Nothing else belongs in the digest — an
    approval that also bound, say, the request's timestamp would be impossible
    to satisfy.
    """
    canonical = canonical_parameters(params or {})
    binding = {
        "kind": str(kind),
        "action": str(action),
        "connection_id": str(connection_id),
        "config_version": int(config_version),
        "secret_versions": {str(k): int(v)
                            for k, v in sorted((secret_versions or {}).items())},
        "actor_user_id": str(actor_user_id),
        "tenant_id": str(tenant_id or ""),
        "target": _canonicalise(dict(target or {})),
        "parameters": canonical,
        "attachment_digest": attachment_digest(params or {}),
    }
    return {"binding": binding, "digest": _digest(binding, key=key)}


# -- the pre-dispatch re-check ----------------------------------------------

@dataclass(frozen=True)
class ApprovalDecision:
    """What the caller supplied as an approval, if anything."""

    approved: bool
    approver_user_id: str = ""
    digest: str = ""
    issued_at: int = 0
    expires_at: int = 0
    revoked: bool = False
    #: Approval scope the record declared, e.g. ``once``.
    scope: str = "once"

    @classmethod
    def parse(cls, raw: Optional[Mapping[str, Any]]) -> "ApprovalDecision":
        if not raw:
            return cls(approved=False)
        return cls(
            approved=bool(raw.get("approved")),
            approver_user_id=str(raw.get("approver_user_id") or ""),
            digest=str(raw.get("digest") or ""),
            issued_at=int(raw.get("issued_at") or 0),
            expires_at=int(raw.get("expires_at") or 0),
            revoked=bool(raw.get("revoked")),
            scope=str(raw.get("scope") or "once"),
        )


def verify_approval(decision: ApprovalDecision, *, digest: str,
                    actor_user_id: str, now: Optional[int] = None,
                    allow_self_approval: bool = False,
                    max_age_seconds: int = 900) -> None:
    """Refuse unless this exact action was approved by someone else, recently.

    Each refusal has its own code, because "the approver was the requester" and
    "the parameters changed after approval" need different actions from the
    reader — and conflating them hides a real separation-of-duties failure
    behind a generic "not approved".

    ``allow_self_approval`` exists for a single-operator deployment, and
    defaults to off: separation of duties is the point of the control.
    """
    now = int(now if now is not None else time.time())
    if not decision.approved:
        raise forbidden("this action requires an approval that has not been "
                        "granted", code="approval_required")
    if decision.revoked:
        raise forbidden("the approval for this action was revoked",
                        code="approval_revoked")
    if not allow_self_approval and decision.approver_user_id \
            and decision.approver_user_id == actor_user_id:
        raise forbidden("the approver and the requester are the same user",
                        code="approval_not_separated")
    if not decision.approver_user_id:
        raise forbidden("the approval does not name an approver",
                        code="approval_incomplete")
    if decision.expires_at and now > decision.expires_at:
        raise forbidden("the approval expired", code="approval_expired")
    if not decision.expires_at and decision.issued_at \
            and now - decision.issued_at > max_age_seconds:
        raise forbidden("the approval is older than the accepted window",
                        code="approval_expired")
    if not decision.digest:
        raise forbidden("the approval does not bind this action",
                        code="approval_incomplete")
    if not hmac.compare_digest(str(decision.digest), str(digest)):
        raise forbidden(
            "the approved action differs from this one (connection, secret, "
            "target, parameters or attachments changed after approval)",
            code="approval_binding_mismatch")
    if decision.scope not in ("once", "single_use"):
        raise forbidden("the approval's scope is not accepted for a write "
                        "action", code="approval_scope_refused")


@dataclass
class InvocationCheck:
    """The outcome of the pre-dispatch check, for the audit record."""

    entry: RiskEntry
    digest: str = ""
    approval_verified: bool = False
    acknowledged: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "risk_level": self.entry.level,
            "risk_action": self.entry.action,
            "binding_digest": self.digest,
            "approval_verified": self.approval_verified,
            "acknowledged": self.acknowledged,
        }


def acknowledged_levels() -> FrozenSet[str]:
    """Levels an operator explicitly accepted, from deployment configuration."""
    try:
        from config import conf
        raw = (conf() or {}).get("external_connections") or {}
        values = (raw.get("risk") or {}).get("acknowledged_levels") or []
        return frozenset(str(v).strip().lower() for v in values if str(v).strip())
    except Exception:  # noqa: BLE001
        return frozenset()


def max_age_seconds() -> int:
    try:
        from config import conf
        raw = (conf() or {}).get("external_connections") or {}
        value = (raw.get("risk") or {}).get("approval_max_age_seconds")
        return int(str(value).strip()) if value else 900
    except Exception:  # noqa: BLE001
        return 900


def check_invocation(kind: str, action: str, params: Mapping[str, Any], **kwargs) -> None:
    """The single pre-dispatch gate.

    Signature note: ``ConnectionRuntime.invoke`` calls this with
    ``(kind, action, params)`` and, when it has the richer facts, the keyword
    arguments below. The keyword form is what a caller with an approval should
    use; the positional form is the conservative default that refuses a write.
    """
    # Imported here rather than at module scope: the adapter registry imports
    # this module transitively, and a top-level import would be a cycle.
    from integrations.external.adapters.base import adapter_for

    write = False
    try:
        adapter = adapter_for(kind)
        write = action in adapter.write_actions
    except Exception:  # noqa: BLE001 - unknown adapter: treat as a write
        write = True

    entry = lookup(kind, action, write=write)
    if entry.level in ACKNOWLEDGEMENT_REQUIRED \
            and entry.level not in acknowledged_levels():
        raise forbidden(
            "%s.%s is a %s-risk action and this deployment has not "
            "acknowledged that risk" % (kind, action, entry.level),
            code="risk_not_acknowledged")
    if entry.level not in APPROVAL_REQUIRED:
        # A read, or a low/medium write the catalogue says is contained enough
        # to run without a human in the loop. Returning here is the *only*
        # path that reaches the adapter without an approval.
        return

    decision = ApprovalDecision.parse(kwargs.get("approval"))
    binding = approval_binding(
        kind=kind, action=action,
        connection_id=str(kwargs.get("connection_id") or ""),
        config_version=int(kwargs.get("config_version") or 0),
        secret_versions=kwargs.get("secret_versions") or {},
        actor_user_id=str(kwargs.get("actor_user_id") or ""),
        tenant_id=kwargs.get("tenant_id"),
        target=kwargs.get("target"), params=params,
        key=str(kwargs.get("digest_key") or ""))
    verify_approval(
        decision, digest=binding["digest"],
        actor_user_id=str(kwargs.get("actor_user_id") or ""),
        allow_self_approval=bool(kwargs.get("allow_self_approval")),
        max_age_seconds=max_age_seconds())


def required_level(kind: str, action: str, *, write: bool) -> str:
    return lookup(kind, action, write=write).level


def action_projection(kind: str, action: str, *, write: bool) -> Dict[str, Any]:
    """What the console shows for one action: level and what it will require."""
    entry = lookup(kind, action, write=write)
    return {
        **entry.as_dict(),
        "approval_required": entry.level in APPROVAL_REQUIRED,
        "acknowledged": entry.level not in ACKNOWLEDGEMENT_REQUIRED
        or entry.level in acknowledged_levels(),
    }
