# encoding:utf-8
"""The WeChat provider's half of the scan-onboarding pipeline (tasks 7.3-7.5).

``channel/web/scan_onboarding.py`` owns the parts of a scan that are the same
for every provider: the per-initiator session, the state machine, the idempotent
receipt ledger, the one-time authorization ordering and the atomic commit. This
module owns the parts that are *WeChat's*: what a scanned bot's login answer
means, which of its fields are credentials, how the vendor client is reached,
who may read the ciphertext of one instance, what "connected" means for an
instance, and which service calls the commit is wired to.

Why a separate module rather than a branch inside the handler
-------------------------------------------------------------
The identity service, the quota/policy transaction, the per-instance ciphertext
and the runtime work item are all *delivered* code from the sibling changes
(``tenant-owned-message-channels``, ``enable-member-personal-console``). The
adapter's whole job is to be the seam that reuses them verbatim: nothing here
opens the identity database, defines an instance row, holds a vendor token, or
decides an authorization rule. A future provider adds a sibling module and
reuses the same pipeline.

What this module refuses to do
------------------------------
* It never writes a global configuration value. The scanned token is written by
  the identity service into the instance's own encrypted bundle, and the runtime
  reads it back per explicit instance through
  :func:`channel.channel_instances.load_tenant_channel_instances`; ``conf()``,
  ``weixin_token`` and the id-less shared credential file are not a truth source
  for a scanned instance (``GLOBAL_CONFIG_KEYS`` names the values this pipeline
  must leave untouched, and the tests assert they are untouched).
* It never lets a caller move the vendor endpoint. The base URL a QR was minted
  against is bound to the session; a commit that names a different one is
  refused rather than followed (:func:`resolve_base_url`).
* It never binds an arbitrary external subject to a member. The personal path
  goes through the delivered owner-forced create and the delivered binding
  challenge; the scan only proves control of the *bot*.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional

from common.log import logger

#: The provider name used in every binding, receipt key and audit record.
PROVIDER = "weixin"
#: The channel type this adapter onboards. ``const.WEIXIN`` and ``"wx"`` both
#: normalize to it (``channel.channel_instances._normalize_type``).
CHANNEL_TYPE = "weixin"
#: The one purpose this adapter starts sessions for. A session exists to bind
#: *one scanned bot* to an instance; it is not a general vendor credential
#: broker, so the purpose is deliberately not a free-form client field.
PURPOSE_CREATE = "create"

#: Global configuration values a scan must never write (task 7.3). ``cfg()``
#: resolves an instance's own credential bundle first, so these keys are the
#: *legacy singleton* surface and stay that way: a scanned instance carries its
#: own token, and a deployment that cannot share state must refuse to serve a
#: scan instead of falling back to a process-global token.
GLOBAL_CONFIG_KEYS = ("weixin_token", "weixin_base_url", "weixin_bot_id")

#: Vendor status values of ``get_qrcode_status`` (see ``channel/weixin``).
VENDOR_WAIT = "wait"
VENDOR_SCANED = "scaned"
VENDOR_CONFIRMED = "confirmed"
VENDOR_EXPIRED = "expired"

#: The vendor field names a confirmed login answer carries. Only the first two
#: are credentials; the vendor's ``ilink_bot_id`` / ``ilink_user_id`` are
#: deliberately *not* persisted — they are not in the type's credential contract
#: and nothing at runtime reads them, so copying them into the encrypted bundle
#: would widen it for no purpose.
VENDOR_TOKEN_FIELD = "bot_token"
VENDOR_BASE_URL_FIELD = "baseurl"

#: Quota metrics this pipeline does not consume, and why (task 7.2).
#:
#: The applicable quota for a channel instance is enforced *inside* the create's
#: own ``BEGIN IMMEDIATE`` transaction — ``IdentityService._enforce_personal_instance_policy``
#: counts the member's slots and inserts in one transaction, and the tenant
#: surface has no channel-instance meter at all. Reserving a *different* metric
#: here (``messages``, ``storage_bytes``) would spend an unrelated allowance, and
#: re-implementing the slot count would be a second copy of a delivered rule.
#: The pre-write reservation the pipeline offers is therefore not wired, and the
#: refusal path that matters — over quota, authorization kept, no row, no
#: receipt — is exercised through the real service in the tests.
QUOTA_RESERVATION_WIRED = False


class WeixinScanAdapterError(RuntimeError):
    """A refusal from this adapter, carrying a machine-readable ``code``."""

    def __init__(self, message: str, *, code: str = "adapter_refused",
                 status: int = 400,
                 context: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.context = dict(context or {})


# ---------------------------------------------------------------------------
# The vendor client (the one external seam, injected for tests)
# ---------------------------------------------------------------------------


def qr_client(base_url: str = "") -> Any:
    """The vendor client used for the QR handshake.

    A single named seam on purpose: a test drives the whole pipeline through the
    real HTTP route with a fake *provider* (the same way the Feishu tests inject
    a fake SDK), and never by making the handler claim a scan succeeded.
    """
    from channel.weixin.weixin_api import DEFAULT_BASE_URL, WeixinApi

    return WeixinApi(base_url=base_url or DEFAULT_BASE_URL)


def default_base_url() -> str:
    from channel.weixin.weixin_api import DEFAULT_BASE_URL

    return DEFAULT_BASE_URL


def fetch_qr(*, base_url: str = "") -> Dict[str, Any]:
    """Mint one QR at the vendor. Returns the vendor answer unchanged."""
    return qr_client(base_url).fetch_qr_code()


def poll_qr(*, qrcode: str, base_url: str = "", timeout: int = 10) -> Dict[str, Any]:
    """Read one QR's status at the vendor. Returns the vendor answer unchanged.

    A vendor-side failure is *not* swallowed here: the caller maps it to a
    refusal, because "the provider could not be reached" and "the operator has
    not scanned yet" must not be reported as the same thing.
    """
    return qr_client(base_url).poll_qr_status(qrcode, timeout=timeout)


def resolve_base_url(*, session_base_url: str, requested: str = "") -> str:
    """The vendor base URL a commit may use: the session's, never the caller's.

    The QR was minted against one endpoint; a later request that names another
    one is not a preference but an attempt to redirect where the bot token gets
    pointed, so it is refused. An empty ``requested`` (the ordinary case) simply
    means "the one bound at scan time".
    """
    bound = str(session_base_url or "").strip()
    wanted = str(requested or "").strip()
    if wanted and wanted != bound:
        raise WeixinScanAdapterError(
            "the scan is bound to a different vendor endpoint",
            code="global_override_refused", status=409)
    return bound or default_base_url()


# ---------------------------------------------------------------------------
# What a scanned login means
# ---------------------------------------------------------------------------


def provider_result(answer: Mapping[str, Any]) -> Dict[str, str]:
    """Narrow a confirmed vendor answer to the type's declared credentials.

    The whole answer is *not* stored: it carries vendor-only fields
    (``ilink_bot_id``, ``ilink_user_id``) that the identity service would reject
    as undeclared, and copying them in would make the encrypted bundle a wider
    document than the credential contract. A confirmed answer with no token is a
    refusal, not an empty bundle: it means the vendor did not actually hand out
    a usable credential, and storing "nothing" would leave an instance that can
    never connect while reporting a saved scan.
    """
    if not isinstance(answer, Mapping):
        raise WeixinScanAdapterError(
            "the vendor login answer is malformed", code="bad_provider_result")
    token = str(answer.get(VENDOR_TOKEN_FIELD) or "").strip()
    if not token:
        raise WeixinScanAdapterError(
            "the vendor confirmed the scan but handed out no credential",
            code="bad_provider_result", status=502)
    base_url = (str(answer.get(VENDOR_BASE_URL_FIELD) or "").strip()
                or default_base_url())
    result = {"weixin_token": token, "weixin_base_url": base_url}
    assert_result_matches_contract(result)
    return result


#: The vendor fields that name *who scanned*, which is not a credential and is
#: therefore not part of the type's credential contract: ``ilink_bot_id`` is the
#: bot the scan authorized (the ``issuer`` half of a binding triple, the same
#: value the channel's own inbound stamp would carry) and ``ilink_user_id`` is
#: the account that completed the scan (the ``subject`` half).
VENDOR_BOT_ID_FIELD = "ilink_bot_id"
VENDOR_USER_ID_FIELD = "ilink_user_id"


def scanner_identity(answer: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    """The scanning account's identity triple, when the vendor names it.

    A completed scan proves presence (which is what the one-time grant rests on)
    *and* says which account was present, so that account can be bound when the
    instance is created instead of waiting for its first message. The two halves
    are read from the same fields this type's inbound would stamp with, so the
    fact stored here is the fact a later message is compared against.

    ``None`` when the vendor omits either half. An identity that cannot be
    matched is worse than none: it would close the first-sender rule while still
    refusing the owner, which is exactly the "配置就绪 ≠ 执行就绪" trap the runtime
    switches already exist to avoid. Whether the deployment can *use* such an
    identity at all is :func:`bind_scanner_identity`'s decision, not this one's.
    """
    if not isinstance(answer, Mapping):
        return None
    issuer = str(answer.get(VENDOR_BOT_ID_FIELD) or "").strip()
    subject = str(answer.get(VENDOR_USER_ID_FIELD) or "").strip()
    if not issuer or not subject:
        return None
    return {"provider": PROVIDER, "issuer": issuer, "subject": subject}


def bind_scanner_identity(service: Any, *, instance_id: str, tenant_id: str,
                          identity: Mapping[str, Any]) -> Dict[str, Any]:
    """Bind the scanning account to the instance a completed scan just created.

    Called after the create committed, and never raising: the instance exists and
    the scan succeeded, so a binding that cannot be written must not turn a saved
    channel into a failed scan. Nothing is lost by that — the owner still has the
    binding code, and an instance left unbound here is still claimed by its first
    sender.

    The service decides everything that matters (which member the account
    belongs to, whether the instance is still claimable, and — importantly —
    whether this channel type's inbound carries an identity stamp at all). This
    function only reports the outcome, because a *skipped* bind is the answer
    most deployments get and it has to be readable in the log: "my first message
    is still refused" must not require reproducing the scan.
    """
    if not instance_id:
        return {"bound": False, "reason": "no_instance", "user_id": ""}
    try:
        result = service.bind_scanner_identity(
            instance_id=instance_id, tenant_id=tenant_id,
            provider=str((identity or {}).get("provider") or PROVIDER),
            issuer=str((identity or {}).get("issuer") or ""),
            subject=str((identity or {}).get("subject") or ""),
        )
    except Exception as error:  # noqa: BLE001 - the row already committed
        logger.warning(
            f"[WeixinScan] scanner identity bind failed for '{instance_id}'"
            f" ({type(error).__name__})"
        )
        return {"bound": False, "reason": "bind_failed", "user_id": ""}
    if not result.get("bound"):
        logger.info(
            f"[WeixinScan] scanner identity not bound for '{instance_id}':"
            f" {result.get('reason')}"
        )
    return result


def credential_contract() -> Dict[str, Any]:
    """This type's credential declaration, from the one place that owns it."""
    from channel.channel_instances import credential_contract as _contract

    return _contract(CHANNEL_TYPE)


def provider_secret_keys() -> tuple:
    """The credential keys ``attach_provider_result`` must treat as secrets."""
    return tuple(credential_contract()["secret_keys"])


def provider_result_keys() -> tuple:
    """Every credential key this type declares (the narrowing whitelist).

    The bundle handed to the session store carries the whole credential, not
    only its secret half: WeChat's ``weixin_base_url`` is part of the same
    credential and is declared, so it must be storable while
    :func:`provider_secret_keys` still marks the token as the secret.
    """
    return tuple(credential_contract()["keys"])


def assert_result_matches_contract(result: Mapping[str, Any]) -> None:
    """Refuse a provider result that is not exactly this type's keys.

    ``attach_provider_result`` already refuses undeclared fields; this check runs
    earlier so the refusal is attributed to the adapter's narrowing step rather
    than to the session store.
    """
    declared = set(credential_contract()["keys"])
    unknown = sorted(set(str(key) for key in (result or {})) - declared)
    if unknown:
        raise WeixinScanAdapterError(
            "the provider result carries fields this channel type does not"
            " declare", code="bad_provider_result",
            context={"unknown": unknown[:8]})


# ---------------------------------------------------------------------------
# The one-time authorization
# ---------------------------------------------------------------------------


def mint_scan_grant(*, actor_user_id: str, tenant_id: str) -> str:
    """Mint the one-time create authorization a completed scan earns.

    Kept beside the provider because the *scan outcome* is what justifies the
    grant: it stands in for the operator's presence at the console (the recent
    password), and nothing else about the create is waived by it.
    """
    from auth import scan_authorization

    return scan_authorization.mint(actor_user_id=actor_user_id,
                                   tenant_id=tenant_id, channel_type=CHANNEL_TYPE)


def verify_scan_grant(ticket: str, *, actor_user_id: str,
                      tenant_id: str) -> bool:
    """Whether *ticket* is a live grant for exactly this actor/tenant/type."""
    from auth import scan_authorization

    return bool(scan_authorization.verify(
        ticket, actor_user_id=actor_user_id, tenant_id=tenant_id,
        channel_type=CHANNEL_TYPE))


# ---------------------------------------------------------------------------
# Wiring the commit into the delivered services
# ---------------------------------------------------------------------------


def default_display_name(service: Any, *, scope: str, tenant_id: str,
                         actor_user_id: str) -> str:
    """The name a scan-created instance carries when the client names none.

    A scan has no form to fill in: the operator points a phone at a QR code and
    the instance appears, so the server has to resolve the name itself — the
    type's own label plus the first free ordinal, the same name the console's
    form pre-fills. The names already in use come from the delivered list calls
    (one per scope) rather than from a query of our own, so this cannot drift
    from what the console shows.

    Resolved once, at scan start, and carried on the session: a retried submit
    must present the *same* name or its normalized digest would change and the
    receipt read-back would be refused as a different operation.
    """
    from channel.channel_instances import scan_default_display_name
    from channel.web import scan_onboarding

    taken: Any = []
    try:
        if str(scope or "").strip() == scan_onboarding.SCOPE_PERSONAL:
            listing = service.list_personal_channel_instances(
                actor_user_id=actor_user_id, tenant_id=tenant_id)
        else:
            listing = service.list_tenant_channel_instances(
                actor_user_id=actor_user_id, tenant_id=tenant_id)
        taken = [item.get("display_name")
                 for item in (listing.get("items") or ())
                 if str(item.get("channel_type") or "") == CHANNEL_TYPE]
    except Exception as error:  # noqa: BLE001 - an unreadable list is not a name
        logger.warning(
            "[WeixinScan] cannot read the existing instance names"
            f" ({type(error).__name__}); falling back to the type label"
        )
    return scan_default_display_name(CHANNEL_TYPE, taken)


def create_instance_callable(service: Any, *, scope: str) -> Callable[..., Any]:
    """The create the scan pipeline calls, wired to the identity service.

    ``commit_scan_binding`` passes exactly the identity service's own keyword
    arguments, so the row, its encrypted bundle, the credential version, the
    quota/policy transaction and the create audit are the delivered
    ``create_tenant_channel_instance`` — one write path, not a second one.

    The only additions are the two the delivered *personal* entry point (task
    6.1) also performs: the onboarding switch is re-checked, and the runtime is
    reconciled after the row lands. A withdrawn switch must refuse a new personal
    scan binding here, while edits and revocations stay reachable.
    """
    from channel.web import scan_onboarding

    personal = str(scope or "").strip() == scan_onboarding.SCOPE_PERSONAL

    def _create(**kwargs: Any) -> Any:
        if personal:
            service.require_personal_capability("personal_channel_onboarding")
        created = service.create_tenant_channel_instance(**kwargs)
        if personal and isinstance(created, Mapping) and created.get("id"):
            # The delivered personal path reconciles here; reuse the same public
            # seam (never the service's private helper) so a connection that
            # cannot follow is *reported* through the runtime state instead of
            # failing a write that already committed.
            from channel.channel_instances import reconcile_instance_runtime

            reconcile_instance_runtime(str(created["id"]))
        return created

    return _create


def audit_hook(service: Any) -> Callable[..., Any]:
    """The scan-binding audit event, written through the delivered audit API.

    The pipeline hands over a field list that already contains only non-secret
    facts (``secret_present`` is a boolean); this adapter maps it to the service
    signature and adds nothing of its own.
    """

    def _audit(**kwargs: Any) -> Any:
        return service.record_audit(
            actor_user_id=str(kwargs.get("actor_user_id") or ""),
            tenant_id=str(kwargs.get("tenant_id") or ""),
            action=str(kwargs.get("action") or "channel.scan.bind"),
            target=str(kwargs.get("target") or "channel_scan"),
            redacted_changes=dict(kwargs.get("changes") or {}),
            result="success",
        )

    return _audit


def readback_guard(service: Any, *, resolve_context: Callable[[], Any]) -> Callable[[Any], bool]:
    """The re-authorization a 24h receipt read-back must pass (task 7.2).

    A receipt answers "my submit response was lost", possibly hours later, so
    reading it back re-establishes everything the ledger cannot: that the auth
    session is still current, that the initiator is still the owner the receipt
    was bound to, that the member is still active, and that the instance is not
    stopped, governance-disabled or (for a personal instance) now someone
    else's. ``resolve_context`` re-resolves the request's identity; anything it
    raises is treated as a refusal by the pipeline, never as consent.
    """

    def _guard(readback: Any) -> bool:
        ctx = resolve_context()
        owner = str(getattr(ctx, "user_id", "") or "")
        tenant = str(getattr(ctx, "tenant_id", "") or "")
        if not owner or owner != readback.owner_user_id:
            return False
        if not tenant or tenant != readback.tenant_id:
            return False
        instance_id = str((readback.result or {}).get("instance_id") or "")
        if not instance_id:
            # A receipt without an instance id cannot be re-authorized against a
            # target, so it is not readable. (The pipeline never stores one with
            # an empty id; this is the belt to that braces.)
            return False
        row = service.get_tenant_channel_instance_row(instance_id)
        if not row or str(row.get("tenant_id") or "") != tenant:
            return False
        if not row.get("active") or row.get("governance_disabled_at") is not None:
            return False
        if str(row.get("scope") or "tenant") == "user":
            # A personal receipt is readable by its owner only, and only while
            # that owner is still an active member of the tenant.
            if str(row.get("owner_user_id") or "") != owner:
                return False
            checker = getattr(service, "member_is_active", None)
            if not callable(checker) or not checker(owner, tenant):
                return False
            return True
        # A tenant/public receipt: the caller must still hold the tenant's
        # channel administration. ``list_tenant_channel_instances`` raises for a
        # non-controller, which the pipeline turns into a refusal.
        listing = service.list_tenant_channel_instances(
            actor_user_id=owner, tenant_id=tenant)
        return instance_id in {str(item.get("id") or "")
                               for item in listing.get("items") or ()}

    return _guard


def connection_state(service: Any, *, instance_id: str) -> Dict[str, Any]:
    """Saved-vs-connected state for one instance, from the delivered seam."""
    from channel.channel_instances import instance_connection_state

    return instance_connection_state(instance_id, service=service)


def apply_connection(instance_id: str) -> Dict[str, Any]:
    """The post-commit, per-instance, recoverable connection work item.

    Serialized per instance by the delivered runtime (``_instance_restart_lock``)
    and never raising, so a retried submit re-applies the same instance instead
    of starting a second connection. It reports the outcome; it does not decide
    whether the instance *may* connect — that gate belongs to the runtime, and
    keeping it there is what lets this call report "saved, not connected" for a
    type whose execution is not accepted yet.
    """
    from channel.channel_instances import reconcile_instance_runtime

    try:
        return reconcile_instance_runtime(str(instance_id or ""))
    except Exception as error:  # noqa: BLE001 - the row already committed
        logger.error(
            f"[WeixinScan] connection apply failed for '{instance_id}': {error}")
        return {"applied": False, "pending": True,
                "error": f"runtime apply failed: {error}"}


def log_refusal(stage: str, error: BaseException) -> None:
    """One line per refused stage, with the code and never a credential."""
    code = getattr(error, "code", "") or type(error).__name__
    logger.warning(f"[WeixinScan] {stage} refused ({code})")
