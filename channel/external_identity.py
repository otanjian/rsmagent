# encoding:utf-8
"""External IM identity resolution for database mode (task 4.x).

Inbound IM messages have no web session: the author is authenticated by the
provider's own identity and must be bound to a local account via
``external_identities`` (admin pre-binding). This module is the single entry
point every IM channel uses to turn an inbound author into a tenant-scoped
``RequestContext`` before any model/tool execution.

Rules (mirroring the Web chat gates, decision 4 in open-database-runtime):
  * The channel's bound Agent only selects the *workspace/routing* — it never
    grants identity. Execution always runs as the resolved tenant member.
  * The resolved user must be an active member of the Agent's bound tenant and
    hold the same gates as Web chat: functional ``chat.use`` + the Agent's
    ``agent.use`` resource grant (platform/tenant-admin bypass unchanged).
  * No binding / inactive user / missing membership / forced password change /
    missing grant => a fixed, user-facing notice and **no** model call.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("external_identity")


#: Stable deny reasons the channel layer turns into fixed, localizable notices.
UNBOUND = "external_unbound"
NOT_MEMBER = "external_not_member"
TENANT_UNBOUND = "external_agent_not_tenant_bound"
PASSWORD_CHANGE_REQUIRED = "external_password_change_required"
PERMISSION_DENIED = "external_permission_denied"
UNSUPPORTED_CHANNEL = "external_channel_unsupported"
AGENT_UNAVAILABLE = "external_agent_unavailable"

#: Member-owned (personal) channel denials (change enable-member-personal-console,
#: task 7.1). Kept distinct from the shared-instance reasons because the fix is
#: different: the member completes their own binding in the console, rather than
#: asking an administrator to bind their account.
PERSONAL_NOT_LINKED = "external_personal_not_linked"
PERSONAL_SENDER_MISMATCH = "external_personal_sender_mismatch"
PERSONAL_GROUP = "external_personal_group"
PERSONAL_UNAVAILABLE = "external_personal_unavailable"
PERSONAL_TARGET_INVALID = "external_personal_target_invalid"
PERSONAL_CODE_INVALID = "external_personal_code_invalid"
PERSONAL_LINKED = "external_personal_linked"


def personal_deny_reason(reason: str) -> str:
    """Map a service-side routing verdict onto a fixed, user-facing reason.

    The service answers with what it checked ("sender_not_owner",
    "target_not_owned", …); the author has to be told what to *do*, which is why
    this collapses those verdicts into the few notices an IM card can carry. An
    unknown verdict is treated as "not available" rather than as permission.
    """
    return {
        "not_found": PERSONAL_UNAVAILABLE,
        "not_personal": PERSONAL_UNAVAILABLE,
        "instance_disabled": PERSONAL_UNAVAILABLE,
        "governance_disabled": PERSONAL_UNAVAILABLE,
        "credential_revoked": PERSONAL_UNAVAILABLE,
        "member_inactive": NOT_MEMBER,
        "not_linked": PERSONAL_NOT_LINKED,
        "sender_not_owner": PERSONAL_SENDER_MISMATCH,
        "group_not_personal": PERSONAL_GROUP,
        "no_target_agent": PERSONAL_TARGET_INVALID,
        "target_not_owned": PERSONAL_TARGET_INVALID,
        "target_not_authorized": PERSONAL_TARGET_INVALID,
        "identity_unavailable": PERSONAL_NOT_LINKED,
        "channel_type_not_ready": PERSONAL_UNAVAILABLE,
        "lookup_failed": PERSONAL_UNAVAILABLE,
        "forbidden": PERSONAL_UNAVAILABLE,
    }.get(reason, PERSONAL_UNAVAILABLE)


def is_database_mode() -> bool:
    """True when the deployment runs database identity mode.

    After retire-legacy-identity-mode, database is the only mode. Explicit
    ``identity_mode=legacy`` is refused at boot; missing/other values run as
    database.
    """
    return True


def deny_notice(reason: str) -> str:
    """Fixed, user-facing reply for a denied IM inbound message.

    Bilingual like the other channel notices; deliberately terse so it survives
    IM length limits and points the user at the administrator.
    """
    zh, en = {
        UNBOUND: (
            "你的账号尚未与系统绑定，请联系管理员完成绑定后重试。",
            "Your account is not bound yet. Ask an administrator to bind it, then retry.",
        ),
        NOT_MEMBER: (
            "你的账号不属于该助手所在的组织，请联系管理员。",
            "Your account does not belong to this assistant's organization. Contact an administrator.",
        ),
        TENANT_UNBOUND: (
            "该助手未绑定任何组织，请联系管理员。",
            "This assistant is not bound to an organization. Contact an administrator.",
        ),
        PASSWORD_CHANGE_REQUIRED: (
            "请先在网页端修改初始密码后，再通过消息渠道对话。",
            "Please set your password in the web console before chatting here.",
        ),
        PERMISSION_DENIED: (
            "你暂时没有使用该助手的权限，请联系管理员开通。",
            "You do not have permission to use this assistant yet. Ask an administrator to grant access.",
        ),
        UNSUPPORTED_CHANNEL: (
            "该消息渠道在数据库模式下尚未开放，请联系管理员。",
            "This channel is not open in database mode yet. Contact an administrator.",
        ),
        AGENT_UNAVAILABLE: (
            "该组织尚未配置可用的智能体，请联系管理员。",
            "No Agent is available for this organization yet. Contact an administrator.",
        ),
        PERSONAL_NOT_LINKED: (
            "你的账号尚未绑定这个个人渠道，请在网页端生成绑定码后从这里发送。",
            "This personal channel is not linked to you yet. Generate a binding code "
            "in the web console and send it here.",
        ),
        PERSONAL_SENDER_MISMATCH: (
            "这个个人助手只服务其所有者本人，无法为你处理消息。",
            "This personal assistant serves its owner only and cannot answer you.",
        ),
        PERSONAL_GROUP: (
            "个人助手仅支持私聊，请单独向它发送消息。",
            "A personal assistant only answers private chats. Message it directly.",
        ),
        PERSONAL_UNAVAILABLE: (
            "该个人渠道当前不可用，请在网页端查看状态。",
            "This personal channel is not available right now. Check its status in the web console.",
        ),
        PERSONAL_TARGET_INVALID: (
            "该个人渠道绑定的智能体已不可用，请在网页端重新选择。",
            "The Agent this personal channel points at is no longer usable. "
            "Choose another one in the web console.",
        ),
        PERSONAL_CODE_INVALID: (
            "绑定码无效或已过期，请在网页端重新生成后重试。",
            "The binding code is invalid or expired. Generate a new one in the web console.",
        ),
        PERSONAL_LINKED: (
            "绑定成功，现在可以在这里与你的个人助手对话了。",
            "Linked. You can now chat with your personal assistant here.",
        ),
    }.get(reason) or (
        "消息无法处理，请联系管理员。",
        "Message could not be processed. Contact an administrator.",
    )
    return f"{zh}\n{en}"


def instance_tenant_id(context: dict) -> str:
    """The owning tenant of the channel instance that carried this message.

    Read from the instance row in the identity store, never from a context
    field: the stamp on the context is for observability only and could be
    stale or forged, and authorization must not follow it. Returns ``""`` when
    the message carries no instance id, when the id is unknown (e.g. a platform
    roster instance that is not tenant-owned), or when the store cannot be
    read — in all of those cases the caller keeps the previous behavior of
    anchoring on the routed Agent's binding.
    """
    return str((instance_row(context) or {}).get("tenant_id") or "").strip()


def instance_row(context: dict) -> Optional[dict]:
    """The stored row of the instance that carried this message, or ``None``.

    Same trust rule as :func:`instance_tenant_id`: the row is the source of
    truth for scope, owner and target. A lookup failure answers ``None`` so the
    caller falls back to the previous (Agent-anchored) behavior rather than
    treating an unreadable store as permission.
    """
    instance_id = str((context or {}).get("instance_id") or "").strip()
    if not instance_id:
        return None
    try:
        from auth.service import get_identity_service

        return get_identity_service().get_tenant_channel_instance_row(instance_id)
    except Exception as error:  # noqa: BLE001 - never let a lookup failure grant access
        logger.warning("[external_identity] instance lookup failed id=%s: %s",
                       instance_id, error)
        return None


def is_personal_instance(instance: Optional[dict]) -> bool:
    """Whether this instance belongs to one member rather than the tenant."""
    return str((instance or {}).get("scope") or "") == "user"


def personal_route_for(context: dict, instance: Optional[dict]) -> Optional[dict]:
    """The explicit personal route a *shared* instance carries for this sender.

    ``None`` means "no personal route claims this sender", and the shared
    instance keeps its own public behaviour. A dict means a route does claim
    them, and its verdict decides the message: an unusable route refuses rather
    than falling back to the shared Agent, because swapping the persona the
    member explicitly chose is the substitution the requirement forbids.

    Only private chats are consulted — a group has no single owner to prove —
    and a lookup failure answers with a refusal rather than ``None``: a store
    that cannot confirm the route must not silently downgrade it to the public
    Agent (fail-closed).
    """
    if instance is None or is_personal_instance(instance):
        return None
    if bool((context or {}).get("isgroup")):
        return None
    instance_id = str((instance or {}).get("id") or "").strip()
    if not instance_id:
        return None
    ext = (context or {}).get("external_identity") or {}
    try:
        from auth.service import get_identity_service

        return get_identity_service().resolve_shared_personal_route(
            instance_id=instance_id,
            provider=str(ext.get("provider") or ""),
            issuer=str(ext.get("issuer") or ""),
            subject=str(ext.get("subject") or ""),
        )
    except Exception as error:  # noqa: BLE001 - a broken store is not "no route"
        logger.warning("[external_identity] personal route lookup failed instance=%s: %s",
                       instance_id, error)
        return {"allowed": False, "reason": "lookup_failed",
                "user_id": "", "agent_id": "", "personal_route": True}


def stamp_external_identity(context: dict, *, provider: str, issuer: str,
                            subject: str) -> dict:
    """Attach the inbound author's identity triple to a context.

    Called by each IM channel right before ``produce``; ``issuer`` is the
    provider's corp/tenant identifier (e.g. a Feishu app id — empty string
    means "no issuer scope" for providers that are inherently single-tenant).
    """
    context["external_identity"] = {
        "provider": (provider or "").strip().lower(),
        "issuer": (issuer or "").strip(),
        "subject": (subject or "").strip(),
    }
    return context


# What a refused message may contribute to the administrator's pending list.
# Kept small so a chatty author cannot fill the store by pasting a wall of text.
_PREVIEW_LIMIT = 200

# A non-text message stores its payload path (or key) in ``content``. Showing
# that to an administrator would leak a server filesystem path and identify
# nothing, so such messages are summarised by kind instead.
_CONTENT_KIND_LABELS = {
    "VOICE": "[语音]",
    "IMAGE": "[图片]",
    "FILE": "[文件]",
    "VIDEO": "[视频]",
    "SHARING": "[分享]",
    "JOIN_GROUP": "[入群]",
    "EXIT_GROUP": "[退群]",
    "PATPAT": "[拍一拍]",
    "ACCEPT_FRIEND": "[同意好友]",
    "FUNCTION": "[函数调用]",
}


def attempt_evidence(context: dict) -> dict:
    """Best-effort "who said what" for a denied inbound, for the binding list.

    Read from the standard ``ChatMessage`` fields so every channel that fills
    them in gets the same console experience without a per-provider code path.
    Everything here is cosmetic: the caller is already refusing the message, so
    each field degrades **independently** — a channel that cannot name its
    sender still contributes the preview, and vice versa.
    """
    evidence = {"sender_name": "", "message_preview": "", "is_group": False}
    msg = context.get("msg")
    if msg is None:
        return evidence

    try:
        nickname = (getattr(msg, "actual_user_nickname", "")
                    or getattr(msg, "from_user_nickname", "") or "")
        if not nickname:
            # Optional channel hook: a provider that only knows the author's
            # opaque id (Feishu's open_id) can resolve a display name here. It is
            # asked lazily — only for a message that is being refused — so the
            # cost is never paid on the normal path, and a channel without the
            # hook (or a lookup that fails) simply contributes no name.
            resolver = getattr(msg, "resolve_sender_name", None)
            if callable(resolver):
                nickname = resolver() or ""
        evidence["sender_name"] = str(nickname).strip()[:_PREVIEW_LIMIT]
    except Exception:  # noqa: BLE001 - a name is a courtesy, never a gate
        logger.info("[external_identity] could not name the sender", exc_info=True)

    try:
        ctype = getattr(msg, "ctype", None)
        type_name = getattr(ctype, "name", "")
        if type_name and type_name != "TEXT":
            text = _CONTENT_KIND_LABELS.get(type_name, "")
        else:
            text = ""
            # Prefer the author's own words: ``content_with_quote`` wraps the
            # quoted parent message around them (Feishu does), which is noise
            # when the point is to recognise the sender.
            for reader in ("content", "content_with_quote"):
                value = getattr(msg, reader, None)
                if callable(value):
                    value = value()
                if value:
                    text = str(value)
                    break
        # Collapse newlines: the preview is rendered as one line in a list row.
        text = " ".join(str(text).split())
        evidence["message_preview"] = text[:_PREVIEW_LIMIT]
    except Exception:  # noqa: BLE001 - a preview is a courtesy, never a gate
        logger.info("[external_identity] could not read the message preview",
                    exc_info=True)

    try:
        # ``is_group`` is authoritative on the message; ``isgroup`` on the
        # context is the composed-context alias used by the group whitelist.
        is_group = getattr(msg, "is_group", None)
        if is_group is None:
            is_group = context.get("isgroup", False)
        evidence["is_group"] = bool(is_group)
    except Exception:  # noqa: BLE001
        pass
    return evidence


def looks_like_binding_code(context: dict) -> bool:
    """Whether a refused message looks like a self-service binding code (6.6).

    A challenge code is a short all-digit string, and the message carrying it is
    a *credential*: whoever reads it from an administrator's screen can bind the
    member's identity. The instance scope already keeps personal traffic out of
    the pending list, but a code can also arrive on a shared instance or before
    the instance row can be read, so the shape is checked independently and the
    attempt is stored with no content at all.

    Deliberately narrow (digits only, challenge length): a false positive costs
    an administrator one row's worth of preview on a message that was already
    being refused, while a false negative would leak a live credential.
    """
    return bool(binding_code_token(context))


def binding_code_token(context: dict) -> str:
    """The all-digit challenge-shaped token of a message, or ``""``.

    Split out from :func:`looks_like_binding_code` so the inbound path can
    *redeem* the very token the redaction rule detects (task 7.1) without
    re-reading the message body in a second place.
    """
    from auth.service import _CHALLENGE_CODE_LENGTH

    msg = context.get("msg")
    if msg is None:
        return ""
    text = ""
    for reader in ("content", "content_with_quote"):
        value = getattr(msg, reader, None)
        if callable(value):
            value = value()
        if value:
            text = str(value)
            break
    token = " ".join(text.split()).strip()
    if len(token) == _CHALLENGE_CODE_LENGTH and token.isdigit():
        return token
    return ""


def personal_owner_context(user_id: str, tenant_id: str):
    """Resolve an allowed personal route's owner into a ``RequestContext``.

    Returns ``(ctx, None)`` or ``(None, reason_code)``. The membership and the
    web-chat gates are re-decided here for the same reason the public path does
    them: the route proves *who* the sender is, not that they still hold the
    right to run an agent today.
    """
    from auth.service import get_identity_service

    try:
        from auth.runtime import IdentityContextError, member_context

        ctx = member_context(get_identity_service(), user_id, tenant_id)
    except IdentityContextError as error:
        if getattr(error, "code", "") == "password_change_required":
            return None, PASSWORD_CHANGE_REQUIRED
        return None, NOT_MEMBER
    if ctx.must_change_password:
        return None, PASSWORD_CHANGE_REQUIRED
    if not (ctx.is_platform_admin or ctx.is_tenant_admin
            or "chat.use" in (ctx.permissions or ())):
        return None, PERMISSION_DENIED
    return ctx, None


def resolve_actor_for_context(context: dict, agent_id: Optional[str],
                              instance_tenant_id: str = ""):
    """Resolve a DB-mode inbound context to its tenant member ``RequestContext``.

    Returns ``(ctx, None)`` on success and ``(None, reason_code)`` on a deny.
    Every deny already implies "do not call the model". ``agent_id`` is the
    channel-routed Agent (its binding fixes the tenant the member must belong
    to). Only call this when ``is_database_mode()`` and the context carries a
    stamped ``external_identity``.

    ``instance_tenant_id`` is the owning tenant of the channel instance that
    delivered the message, resolved from the store by the caller. When present
    it wins over the routed Agent's binding: an instance belongs to exactly one
    tenant, and a routing fallback (or a forged ``bound_agent_id``) must not be
    able to move the conversation — and therefore the member's identity — into
    a different organization.
    """
    from auth.service import get_identity_service

    ext = context.get("external_identity")
    if not ext:
        return None, UNSUPPORTED_CHANNEL
    provider = str(ext.get("provider") or "").strip().lower()
    issuer = str(ext.get("issuer") or "").strip()
    subject = str(ext.get("subject") or "").strip()
    if not provider or not subject:
        logger.warning("[external_identity] incomplete triple provider=%s subject=%s", provider, subject)
        return None, UNBOUND

    svc = get_identity_service()
    user = svc.find_user_for_external_identity(provider, issuer, subject)
    if not user:
        # Nobody has bound this account yet. On an instance that has *never* been
        # bound, the first private sender claims it as the channel's own member
        # (change auto-bind-channel-sender): the person who set the channel up
        # should not have to prove their account with a binding code. The claim
        # decides *whose* account is speaking — the instance's owner for a
        # personal one, its creator for a shared one — and a group chat, an
        # already-bound instance or a triple owned by someone else is refused, so
        # the rule can never be used to take over an established channel.
        instance_id = str((context or {}).get("instance_id") or "").strip()
        if instance_id:
            claim = svc.claim_instance_for_sender(
                instance_id=instance_id, provider=provider, issuer=issuer,
                subject=subject, is_group=bool((context or {}).get("isgroup")),
            )
            if claim.get("claimed"):
                logger.info(
                    f"[external_identity] instance '{instance_id}' auto-bound its"
                    f" first sender to {claim.get('user_id')}"
                )
                user = svc.find_user_for_external_identity(
                    provider, issuer, subject)
    if not user:
        return None, UNBOUND

    # The tenant anchor: the instance's owner when known, else the routed
    # Agent's binding (legacy behavior for platform/legacy channels).
    tenant_id = str(instance_tenant_id or "").strip()
    if not tenant_id:
        binding = svc.get_agent_binding(agent_id) if agent_id else None
        if not binding:
            return None, TENANT_UNBOUND
        tenant_id = binding["tenant_id"]

    try:
        from auth.runtime import IdentityContextError, member_context

        ctx = member_context(svc, user["id"], tenant_id)
    except IdentityContextError as error:
        if getattr(error, "code", "") == "password_change_required":
            return None, PASSWORD_CHANGE_REQUIRED
        return None, NOT_MEMBER
    if ctx.must_change_password:
        return None, PASSWORD_CHANGE_REQUIRED

    # Same gates as Web chat: functional chat.use and the agent.use resource grant.
    if not (ctx.is_platform_admin or ctx.is_tenant_admin
            or "chat.use" in (ctx.permissions or ())):
        return None, PERMISSION_DENIED
    if not svc.check_resource_action(
        ctx.user_id, ctx.tenant_id, "agent", f"agent:{agent_id}",
        "use", permission="agent.use",
    ):
        return None, PERMISSION_DENIED
    return ctx, None
