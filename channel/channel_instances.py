"""Resolve which channel instances to run, bridging legacy and multi-instance.

A legacy install configures channels through ``channel_type`` (a list of type
names) plus flat credentials in ``config.json``; every type maps to exactly one
running channel bound to the default Agent. This is the "shared by default"
world and must keep working untouched.

A multi-Agent install opts in by adding ``channel_instances`` to ``team.json``:
a list of records, each with its own ``instance_id``, ``channel_type``, the
``agent_id`` it routes to, and its own ``credentials``. Several records may
share a ``channel_type`` (e.g. two Feishu bots), which is exactly what a team of
independent AI employees needs.

``resolve_channel_instances`` returns a uniform list of :class:`ChannelInstance`
for both worlds, so the launcher never branch on which config style is in play. When ``channel_instances`` is absent it synthesizes one
instance per legacy ``channel_type`` with no credential override, i.e. the old
behavior byte-for-byte.
"""

from __future__ import annotations

import os
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from common import const
from common.log import logger

# Length of the random suffix in a generated instance id. 10 hex chars gives
# ~40 bits of entropy, plenty to avoid collisions across a user's own bots while
# staying short enough to read and type.
_INSTANCE_ID_RANDOM_LEN = 10


def new_instance_id(channel_type: str, taken: Iterable[str] = ()) -> str:
    """Return a stable, unique id for a freshly created channel instance.

    Shape is ``{channel_type}-{random}`` (e.g. ``feishu-a3f9c2e1b7``): the
    prefix keeps the type recognizable at a glance, the random suffix makes it
    unique and stable across restarts. Ids provided elsewhere (e.g. handed down
    by a remote controller) are honored as-is and never regenerated; this helper
    is only for the local "create a new instance" path.
    """
    ctype = _normalize_type((channel_type or "").strip()) or "channel"
    existing = set(taken)
    while True:
        candidate = f"{ctype}-{secrets.token_hex(_INSTANCE_ID_RANDOM_LEN // 2 + 1)[:_INSTANCE_ID_RANDOM_LEN]}"
        if candidate not in existing:
            return candidate


# Per channel type, the config keys that make up its credentials. Only these
# keys are copied into a per-instance override; everything else (ports, feature
# flags) stays global in conf(). Keep in sync with the channel classes' cfg()
# reads. Extend as more channel types gain multi-instance support.
CREDENTIAL_KEYS: Dict[str, tuple] = {
    const.FEISHU: (
        "feishu_app_id",
        "feishu_app_secret",
        "feishu_token",
        "feishu_bot_name",
    ),
    const.DINGTALK: (
        "dingtalk_client_id",
        "dingtalk_client_secret",
        "dingtalk_robot_code",
    ),
    const.WECOM_BOT: (
        "wecom_bot_id",
        "wecom_bot_secret",
        "wecom_bot_token",
        "wecom_bot_encoding_aes_key",
    ),
    const.WEIXIN: (
        "weixin_token",
        "weixin_base_url",
    ),
    const.QQ: (
        "qq_app_id",
        "qq_app_secret",
    ),
    const.TELEGRAM: (
        "telegram_token",
    ),
    const.SLACK: (
        "slack_bot_token",
        "slack_app_token",
    ),
    const.DISCORD: (
        "discord_token",
    ),
}

# Short human labels used only to seed a new instance's default name (e.g.
# "微信 2"). Purely cosmetic and easily overridden by the user, so an unknown
# type simply falls back to its raw channel_type string.
_CHANNEL_TYPE_LABELS: Dict[str, str] = {
    const.FEISHU: "飞书",
    const.DINGTALK: "钉钉",
    const.WECOM_BOT: "企微机器人",
    const.WEIXIN: "微信",
    const.QQ: "QQ",
    const.TELEGRAM: "Telegram",
    const.SLACK: "Slack",
    const.DISCORD: "Discord",
}

# Channel types that actually support running more than one instance today.
# Others may appear in channel_instances but will run as a single instance
# (their @singleton is not yet bypassed); we log and fall back gracefully.
MULTI_INSTANCE_READY = frozenset({
    const.FEISHU,
    const.DINGTALK,
    const.QQ,
    const.TELEGRAM,
    const.SLACK,
    const.DISCORD,
    const.WEIXIN,
    const.WECOM_BOT,
})


# Channel types whose adapter actually stamps the inbound author's external
# identity triple (provider / issuer / subject) — the precondition for *any*
# non-Web inbound in database identity mode, because
# ``channel.chat_channel._preflight_external_inbound`` refuses an unstamped
# context as UNSUPPORTED_CHANNEL *before* it looks up any binding, and without
# recording the attempt. A type outside this set can therefore never be talked
# to, however thoroughly it is configured — which is why it must not be offered
# for configuration either. Stamping call sites: ``feishu_channel``,
# ``dingtalk_channel``, ``wecom_bot_channel``; add a type here only in the same
# change that adds the stamp to its adapter.
INBOUND_IDENTITY_STAMPING_TYPES = frozenset({
    const.FEISHU,
    const.DINGTALK,
    const.WECOM_BOT,
})


def inbound_identity_admissible(channel_type: str) -> bool:
    """Whether this type's inbound can *ever* be accepted on this deployment.

    Deliberately mode-aware in form even though :func:`is_database_mode` is
    currently constant ``True``: the requirement being tested is "database
    identity mode has to prove the sender", so writing the mode into the
    predicate keeps it derived from the same fact the inbound gate checks, and
    lets it self-disable if a non-database path ever returns instead of
    permanently barring types that would be harmless there. Today it is exactly
    ``ctype in INBOUND_IDENTITY_STAMPING_TYPES``.
    """
    from channel.external_identity import is_database_mode
    if not is_database_mode():
        return True
    return _normalize_type((channel_type or "").strip()) in INBOUND_IDENTITY_STAMPING_TYPES


@dataclass(frozen=True)
class ChannelInstance:
    """One channel to bring up, with its identity, binding and credentials."""

    instance_id: str
    channel_type: str
    agent_id: str = ""
    #: Human-friendly label the user can edit (e.g. "运营微信"). Optional: when
    #: empty the UI falls back to a credential-derived name or the instance id, so
    #: an install that never sets one is unaffected.
    name: str = ""
    credentials: Dict[str, Any] = field(default_factory=dict)
    #: Other Agents sharing this channel's conversations. ``agent_id`` is the
    #: owner/leader that receives every inbound message; ``members`` are the
    #: teammates it may hand work to (via @mention or the agent_delegate tool),
    #: exactly like a Web team conversation. Empty means a solo bot. Owner is
    #: never listed here.
    members: List[str] = field(default_factory=list)
    #: True when synthesized from legacy channel_type rather than an explicit
    #: channel_instances record. Legacy instances carry no credential override.
    legacy: bool = True
    #: Owning tenant for a tenant-owned instance (empty for legacy / team.json
    #: instances). This is the **authoritative anchor** for inbound routing: it
    #: comes from the instance registration row, never from the message or from
    #: whatever Agent the instance happens to be bound to.
    tenant_id: str = ""


def _normalize_type(channel_type: str) -> str:
    if channel_type in (const.WEIXIN, "wx"):
        return const.WEIXIN
    return channel_type


def _parse_channel_type(raw) -> List[str]:
    """Legacy channel_type -> list of type names (string / CSV / list)."""
    if isinstance(raw, list):
        return [str(ch).strip() for ch in raw if str(ch).strip()]
    if isinstance(raw, str):
        return [ch.strip() for ch in raw.split(",") if ch.strip()]
    return []


def _legacy_instances(settings: Mapping[str, Any]) -> List[ChannelInstance]:
    """One instance per legacy channel_type, no credential override.

    Byte-for-byte the pre-multi-instance behavior: instance_id == channel_type,
    credentials read from the global conf() at runtime, routing left to the
    existing config-based AgentRouter (agent_id empty here).
    """
    out: List[ChannelInstance] = []
    for name in _parse_channel_type(settings.get("channel_type", "")):
        ctype = _normalize_type(name)
        out.append(ChannelInstance(instance_id=ctype, channel_type=ctype))
    return out


def _explicit_instances(raw_list: list) -> List[ChannelInstance]:
    out: List[ChannelInstance] = []
    seen_ids = set()
    for index, raw in enumerate(raw_list):
        if not isinstance(raw, Mapping):
            logger.warning(f"[ChannelInstances] entry #{index} is not an object, skipping")
            continue
        channel_type = _normalize_type(str(raw.get("channel_type") or "").strip())
        if not channel_type:
            logger.warning(f"[ChannelInstances] entry #{index} has no channel_type, skipping")
            continue
        instance_id = str(raw.get("instance_id") or "").strip() or channel_type
        if instance_id in seen_ids:
            logger.warning(
                f"[ChannelInstances] duplicate instance_id '{instance_id}', skipping"
            )
            continue
        seen_ids.add(instance_id)

        agent_id = str(raw.get("agent_id") or "").strip()
        name = str(raw.get("name") or "").strip()

        raw_members = raw.get("members")
        members: List[str] = []
        if isinstance(raw_members, list):
            for m in raw_members:
                mid = str(m or "").strip()
                # The owner is never a member of its own team, and ids appear
                # once: a duplicate here would show the same teammate twice.
                if mid and mid != agent_id and mid not in members:
                    members.append(mid)

        # Credentials may be given inline under "credentials", or as flat keys
        # on the record itself; only the known keys for this type are kept.
        raw_creds = raw.get("credentials")
        source = raw_creds if isinstance(raw_creds, Mapping) else raw
        creds: Dict[str, Any] = {}
        for key in CREDENTIAL_KEYS.get(channel_type, ()):
            if source.get(key) is not None:
                creds[key] = source.get(key)

        if channel_type not in MULTI_INSTANCE_READY:
            logger.info(
                f"[ChannelInstances] '{channel_type}' does not support multiple "
                f"instances yet; '{instance_id}' will run as a single instance"
            )

        out.append(
            ChannelInstance(
                instance_id=instance_id,
                channel_type=channel_type,
                agent_id=agent_id,
                name=name,
                credentials=creds,
                members=members,
                legacy=False,
            )
        )
    return out


def resolve_channel_instances(
    settings: Mapping[str, Any],
    tenant_instances: Optional[List[ChannelInstance]] = None,
) -> List[ChannelInstance]:
    """Channel instances to run for *settings*.

    Prefers explicit ``channel_instances`` (multi-Agent); otherwise synthesizes
    the legacy set from ``channel_type`` **only in legacy identity mode**.
    In database identity mode, ``channel_type`` alone MUST NOT start channels —
    only explicit roster/tenant registrations run. The ``web`` console is
    intentionally not represented here — it is managed separately by the
    launcher.

    *tenant_instances* are tenant-owned channels resolved from the identity
    store (see :func:`load_tenant_channel_instances`) and are appended to the
    roster list, so one uniform list drives the launcher. An ``instance_id``
    already present in the roster is skipped: the roster is the source of truth
    for a given id, and starting it twice would open two identical connections.
    """
    def _is_database() -> bool:
        return True

    raw_list = settings.get("channel_instances")
    if isinstance(raw_list, list) and raw_list:
        instances = _explicit_instances(raw_list)
        if not instances:
            logger.warning(
                "[ChannelInstances] channel_instances present but yielded nothing; "
                "database mode refuses legacy channel_type fallback"
            )
            instances = []
    else:
        instances = []

    if not tenant_instances:
        return instances

    seen_ids = {inst.instance_id for inst in instances}
    merged = list(instances)
    for inst in tenant_instances:
        if inst.instance_id in seen_ids:
            logger.warning(
                f"[ChannelInstances] tenant instance '{inst.instance_id}' collides "
                f"with a roster instance of the same id, skipping"
            )
            continue
        seen_ids.add(inst.instance_id)
        merged.append(inst)
    return merged


# ---------------------------------------------------------------------------
# Persistence helpers for the explicit multi-instance list.
#
# These edit the ``channel_instances`` array in the roster file (team.json) and
# leave the legacy ``channel_type`` + flat credentials path completely alone, so
# an install that never opts into multiple instances is never touched.
# ---------------------------------------------------------------------------

def _filtered_credentials(channel_type: str, source: Mapping[str, Any]) -> Dict[str, Any]:
    """Keep only the credential keys meaningful for *channel_type*."""
    ctype = _normalize_type((channel_type or "").strip())
    creds: Dict[str, Any] = {}
    for key in CREDENTIAL_KEYS.get(ctype, ()):
        value = source.get(key)
        if value is not None:
            creds[key] = value
    return creds


def bootstrap_legacy_instances(
    settings: Mapping[str, Any],
    roster: Mapping[str, Any],
    default_agent_id: str = "",
) -> List[Dict[str, Any]]:
    """Return existing ``channel_instances`` only — never synthesize from channel_type.

    Kept as a named entry point for team roster writes; database identity requires
    explicit registration, so flat ``channel_type`` credentials are ignored.
    """
    del settings, default_agent_id  # retained for call-site compatibility
    return [
        dict(item)
        for item in (roster.get("channel_instances") or [])
        if isinstance(item, Mapping)
    ]


def _carry_weixin_credentials_file(instance_id: str) -> None:
    """Copy the legacy Weixin token file to the per-instance path, once.

    Best-effort and idempotent: if the legacy default file exists and the
    per-instance file does not yet, the token (and persisted context tokens) are
    copied so the bootstrapped instance stays logged in. Never overwrites an
    existing per-instance file, and never raises into the write path.
    """
    try:
        import shutil
        from config import get_weixin_credentials_path

        legacy = get_weixin_credentials_path()
        target = get_weixin_credentials_path(instance_id)
        if legacy == target:
            return
        if os.path.exists(legacy) and not os.path.exists(target):
            shutil.copy2(legacy, target)
            logger.info(
                f"[ChannelInstances] carried Weixin credentials '{legacy}' -> "
                f"'{target}' so the bootstrapped instance stays logged in"
            )
    except Exception as e:
        logger.warning(f"[ChannelInstances] Weixin credentials carry-over skipped: {e}")


def read_raw_instances(settings: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The raw ``channel_instances`` records as stored, or an empty list."""
    from agent import team

    roster = team.read(settings)
    raw = roster.get("channel_instances")
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, Mapping)]
    return []


def _clean_members(members, owner_id: str) -> list:
    """Normalize a members list: strings, de-duped, no owner, order preserved."""
    out: list = []
    owner = (owner_id or "").strip()
    for m in members or []:
        mid = str(m or "").strip()
        if mid and mid != owner and mid not in out:
            out.append(mid)
    return out


def default_instance_name(channel_type: str, records: Iterable[Mapping[str, Any]]) -> str:
    """A friendly default label for a freshly created instance of *channel_type*.

    Shape is "<type label>" for the first instance of a type and "<type label> N"
    for later ones, so a second WeChat bot reads as "微信 2" rather than a random
    id. Only used to seed the field; the user can rename it afterwards.
    """
    ctype = _normalize_type((channel_type or "").strip())
    label = _CHANNEL_TYPE_LABELS.get(ctype, ctype)
    same_type = sum(
        1
        for r in records
        if _normalize_type(str(r.get("channel_type") or "").strip()) == ctype
    )
    return label if same_type == 0 else f"{label} {same_type + 1}"


def upsert_instance(
    settings: Mapping[str, Any],
    channel_type: str,
    instance_id: str = "",
    agent_id: Optional[str] = None,
    credentials: Optional[Mapping[str, Any]] = None,
    members: Optional[list] = None,
    name: Optional[str] = None,
) -> ChannelInstance:
    """Create or update one channel instance record and persist it.

    Matching is by ``instance_id``. When it is empty a new stable id is
    generated. ``agent_id``, ``credentials``, ``members`` and ``name`` are merged
    onto any existing record so a partial update (e.g. credentials only) does not
    drop the binding, the team or the label. Pass ``members=None`` to leave the
    team untouched, or ``members=[]`` to clear it. Pass ``name=None`` to leave the
    label untouched; a brand-new record with no name is seeded with a friendly
    default. Returns the resulting :class:`ChannelInstance`.
    """
    from agent import team

    ctype = _normalize_type((channel_type or "").strip())
    records = read_raw_instances(settings)
    taken = {str(r.get("instance_id") or "").strip() for r in records}

    target_id = (instance_id or "").strip()
    if not target_id:
        target_id = new_instance_id(ctype, taken)

    incoming_creds = _filtered_credentials(ctype, credentials or {})

    updated = False
    for record in records:
        if str(record.get("instance_id") or "").strip() != target_id:
            continue
        record["channel_type"] = ctype
        if agent_id is not None:
            record["agent_id"] = str(agent_id).strip()
        if name is not None:
            new_name = str(name).strip()
            if new_name:
                record["name"] = new_name
            else:
                record.pop("name", None)
        if incoming_creds:
            merged = dict(record.get("credentials") or {})
            merged.update(incoming_creds)
            record["credentials"] = merged
        if members is not None:
            cleaned = _clean_members(members, record.get("agent_id") or "")
            if cleaned:
                record["members"] = cleaned
            else:
                record.pop("members", None)
        updated = True
        result_record = record
        break

    if not updated:
        # Seed a friendly default label for a new instance so the console has
        # something readable to show before the user renames it. Computed against
        # the records *before* this one is appended, so the first is unnumbered.
        seeded_name = str(name).strip() if name is not None else ""
        if not seeded_name:
            seeded_name = default_instance_name(ctype, records)
        result_record = {
            "instance_id": target_id,
            "channel_type": ctype,
            "agent_id": str(agent_id).strip() if agent_id is not None else "",
            "name": seeded_name,
            "credentials": incoming_creds,
        }
        cleaned = _clean_members(members, result_record["agent_id"])
        if cleaned:
            result_record["members"] = cleaned
        records.append(result_record)

    roster = team.read(settings)
    roster["channel_instances"] = records
    team.write(settings, roster)

    return ChannelInstance(
        instance_id=target_id,
        channel_type=ctype,
        agent_id=str(result_record.get("agent_id") or ""),
        name=str(result_record.get("name") or ""),
        credentials=dict(result_record.get("credentials") or {}),
        members=list(result_record.get("members") or []),
        legacy=False,
    )


def remove_instance(settings: Mapping[str, Any], instance_id: str) -> bool:
    """Drop one instance record by id. Returns True if something was removed."""
    from agent import team

    target_id = (instance_id or "").strip()
    if not target_id:
        return False
    records = read_raw_instances(settings)
    kept = [r for r in records if str(r.get("instance_id") or "").strip() != target_id]
    if len(kept) == len(records):
        return False
    roster = team.read(settings)
    roster["channel_instances"] = kept
    team.write(settings, roster)
    return True


def get_instance(settings: Mapping[str, Any], instance_id: str) -> Optional[ChannelInstance]:
    """Resolve one instance by id, or None.

    Overlays the roster file first so the lookup works even when the caller
    passes bare ``conf()`` (channel_instances lives in team.json, not config).
    """
    from agent import team

    target_id = (instance_id or "").strip()
    resolved = team.resolve(settings)
    for inst in resolve_channel_instances(resolved):
        if inst.instance_id == target_id:
            return inst
    return None


# ===========================================================================
# UPSTREAM REGION (D4b). Shared and upstream-owned symbols live here.
# Upstream-only additions such as `_CHANNEL_TYPE_LABELS` and
# `default_instance_name` belong above the divider below.
# ===========================================================================


# PARTITION DIVIDER (D4b): add fork symbols below this line only; upstream symbols above.


# ===========================================================================
# FORK REGION (D4b). Fork-only symbols live below this line.
# Credential-minimum sets, tenant runtime state and the tenant console
# contract are added here, so an upstream merge that adds its own symbols
# at the anchors above never collides with a fork addition in place.
# ===========================================================================


#: Subset of :data:`CREDENTIAL_KEYS` without which a channel instance cannot
#: start. Kept beside the full key list rather than replacing it: the full list
#: still decides which field names a write may carry, so an optional field
#: (a verification token, a display name) stays storable and is simply not
#: required.
#:
#: Every entry is the *startup guard the channel class itself enforces*, verified
#: by reading that class, not guessed from the field names:
#:   feishu      feishu_channel.py     "app_id ... or app_secret" guard
#:   wecom_bot   wecom_bot_channel.py  "wecom_bot_id and wecom_bot_secret" guard
#:                                     (websocket mode; the webhook transport is
#:                                      single-instance and refuses extra
#:                                      instances, so token/aes are not needed
#:                                      for a tenant-owned bot)
#:   qq          qq_channel.py         "qq_app_id and qq_app_secret" guard
#:   telegram    telegram_channel.py   "telegram_token is required" guard
#:   slack       slack_channel.py      "slack_bot_token and slack_app_token" guard
#:   discord     discord_channel.py    "discord_token is required" guard
#:
#: ``dingtalk`` and ``weixin`` are deliberately absent: their minimum set has not
#: been verified against the channel classes yet, and inventing one would reject
#: writes that work today. A type without an entry here keeps the previous rule
#: (at least one declared, non-empty field).
REQUIRED_CREDENTIAL_KEYS: Dict[str, tuple] = {
    const.FEISHU: (
        "feishu_app_id",
        "feishu_app_secret",
    ),
    const.WECOM_BOT: (
        "wecom_bot_id",
        "wecom_bot_secret",
    ),
    const.QQ: (
        "qq_app_id",
        "qq_app_secret",
    ),
    const.TELEGRAM: (
        "telegram_token",
    ),
    const.SLACK: (
        "slack_bot_token",
        "slack_app_token",
    ),
    const.DISCORD: (
        "discord_token",
    ),
}


def required_credential_keys(channel_type: str) -> tuple:
    """Keys that must be present and non-empty for *channel_type* to start.

    Empty for a type whose minimum set has not been verified yet (see
    :data:`REQUIRED_CREDENTIAL_KEYS`), which leaves that type's validation to the
    "at least one declared field" rule it had before.
    """
    return REQUIRED_CREDENTIAL_KEYS.get(_normalize_type(channel_type), ())


def credential_contract(channel_type: str) -> Dict[str, Any]:
    """The credential declaration for one channel type (task 7.3).

    One shape for the three things a scan-driven create has to know before it
    stores a vendor answer: which fields the type accepts, which of them are
    secrets, and which are mandatory. Assembled here rather than in the caller
    so the scan pipeline cannot keep a second copy that drifts from the form and
    from ``_validated_channel_bundle``.

    ``secret_keys`` is the same hint test the console's field contract uses
    (``_SECRET_KEY_HINTS``); it marks a field as write-only, it is not a
    statement about a particular value.
    """
    ctype = _normalize_type(channel_type)
    keys = tuple(CREDENTIAL_KEYS.get(ctype) or ())
    return {
        "channel_type": ctype,
        "keys": keys,
        "secret_keys": tuple(key for key in keys
                             if any(hint in key for hint in _SECRET_KEY_HINTS)),
        "required": tuple(REQUIRED_CREDENTIAL_KEYS.get(ctype) or ()),
    }


def instance_connection_state(
    instance_id: str, *, service: Any = None
) -> Dict[str, Any]:
    """Saved-vs-connected state for one instance, as two separate facts (task 7.3).

    ``saved`` answers "is there a committed row" and comes from the identity
    store; ``connected`` answers "is a vendor connection up right now" and comes
    from the per-instance runtime state. They are reported apart on purpose: a
    row committed a second ago is *saved*, and calling it *connected* would be
    exactly the confusion D9 forbids. A row that exists but has no recorded
    runtime outcome yet reports ``pending`` — "not connected yet, nothing
    failed" — which is what a console-only process legitimately returns.

    Never raises: an unreadable store or an unevaluable switch reports
    ``state="unknown"`` with ``saved=False``, so a caller cannot mistake a
    lookup failure for "nothing to run".
    """
    out = {"instance_id": str(instance_id or ""), "saved": False,
           "connected": False, "state": "missing", "reason": ""}
    if not instance_id:
        return out
    if service is None:
        try:
            from auth.service import get_identity_service

            service = get_identity_service()
        except Exception as error:  # noqa: BLE001 - reported, never raised
            logger.warning(
                f"[ChannelInstances] cannot resolve the identity service for"
                f" connection state: {error}")
            out["state"] = "unknown"
            out["reason"] = "identity store unavailable"
            return out
    try:
        row = service.get_tenant_channel_instance_row(instance_id)
    except Exception as error:  # noqa: BLE001 - reported, never raised
        logger.warning(
            f"[ChannelInstances] cannot read instance '{instance_id}': {error}")
        out["state"] = "unknown"
        out["reason"] = "instance lookup failed"
        return out
    if row is None:
        return out

    out["saved"] = True
    if not row.get("active"):
        out["state"] = "stopped"
        return out
    if row.get("governance_disabled_at") is not None:
        out["state"] = "stopped"
        out["reason"] = "governance stop"
        return out

    ctype = _normalize_type(str(row.get("channel_type") or ""))
    personal = str(row.get("scope") or "tenant") == "user"
    if personal and not personal_runtime_enabled(ctype):
        # The configuration is saved and stays manageable; the connection is
        # closed until this type has a recorded acceptance. Saying "connected"
        # here would claim acceptance the deployment has not got (task 7.4).
        out["state"] = "not_connected"
        out["reason"] = "personal runtime is not enabled for this channel type"
        return out

    runtime = instance_runtime_state(instance_id)
    if runtime.get("applied"):
        out["connected"] = True
        out["state"] = "connected"
        return out
    if runtime.get("pending"):
        out["state"] = "pending"
        out["reason"] = "the connection has not been applied in this process yet"
        return out
    out["state"] = "failed"
    out["reason"] = str(runtime.get("error") or "the connection did not start")
    return out


# ---------------------------------------------------------------------------
# Runtime state for tenant-owned instances.
#
# Restarting a channel is a *process* concern, not durable state: the record
# lives here rather than in the identity store, and is rebuilt by the startup
# synthesis after a restart. It exists so a save can report an honest
# "not applied yet, and here is why" instead of silently leaving the previous
# credentials in service.
# ---------------------------------------------------------------------------

_runtime_state: Dict[str, Dict[str, Any]] = {}
_runtime_state_guard = threading.Lock()

#: One lock per instance id, so two concurrent saves of the same instance cannot
#: start/stop it twice. Created on first use and never removed (a bounded set:
#: one entry per instance the process has touched).
_restart_locks: Dict[str, threading.Lock] = {}
_restart_locks_guard = threading.Lock()


def _instance_restart_lock(instance_id: str) -> threading.Lock:
    with _restart_locks_guard:
        lock = _restart_locks.get(instance_id)
        if lock is None:
            lock = threading.Lock()
            _restart_locks[instance_id] = lock
        return lock


def _record_runtime_state(instance_id: str, *, applied: bool,
                          pending: bool = False, error: str = "") -> Dict[str, Any]:
    state = {"applied": bool(applied), "pending": bool(pending),
             "error": str(error or "")}
    with _runtime_state_guard:
        _runtime_state[instance_id] = state
    return dict(state)


def instance_runtime_state(instance_id: str) -> Dict[str, Any]:
    """The last observed runtime outcome for one instance (never raises)."""
    with _runtime_state_guard:
        state = _runtime_state.get(instance_id)
        return dict(state) if state else {"applied": False, "pending": True, "error": ""}


def _runtime_manager():
    """The process's ChannelManager, or None when this process runs no channels.

    Resolved through the app module the same way the platform channel handlers
    do, so an imported ``channel_instances`` never has to own the manager.
    """
    import sys

    app_module = sys.modules.get("__main__") or sys.modules.get("app")
    return getattr(app_module, "_channel_mgr", None) if app_module else None


def _stop_instance_runtime(instance_id: str) -> None:
    mgr = _runtime_manager()
    if mgr is None:
        return
    try:
        remover = getattr(mgr, "remove_channel", None)
        if callable(remover):
            remover(instance_id)
        else:
            mgr.stop(instance_id)
    except Exception as e:  # stopping is best-effort; the caller reports state
        logger.warning(f"[ChannelInstances] failed to stop '{instance_id}': {e}")


def _owner_is_active_member(service, row) -> bool:
    """Whether a member-owned instance's owner may still run it.

    Shared and platform instances have no owner to check, so they answer True.
    A store that cannot answer answers False: an unevaluable membership is not a
    licence to keep a member's connection alive.
    """
    if str(row.get("scope") or "tenant") != "user":
        return True
    owner = str(row.get("owner_user_id") or "")
    tenant_id = str(row.get("tenant_id") or "")
    if not owner or not tenant_id:
        return False
    checker = getattr(service, "member_is_active", None)
    if not callable(checker):
        return False
    try:
        return bool(checker(owner, tenant_id))
    except Exception:  # noqa: BLE001 - an unevaluable answer is not consent
        return False


def _personal_target_usable(service, row) -> Tuple[bool, str]:
    """Whether a member-owned instance's stored target may still be routed to.

    The third gate of a personal *connection* (task 2.5), alongside the member
    check and the runtime switch. The target is re-derived here rather than
    trusted from the row, because the facts it rests on — the Agent's private
    ownership, its registry presence, its enabled flag, the owner's ``use``
    grant — all change outside this instance and none of them are stored on it.

    Returns ``(usable, reason)``. A service that cannot answer is *not* consent:
    an unevaluable target keeps the connection closed, which is the same
    fail-closed posture as :func:`_owner_is_active_member`.
    """
    resolver = getattr(service, "personal_target_state", None)
    if not callable(resolver):
        return False, "target_state_unavailable"
    try:
        state = resolver(row) or {}
    except Exception as e:  # noqa: BLE001 - an unevaluable answer is not consent
        logger.warning(
            f"[ChannelInstances] cannot evaluate personal target for "
            f"'{row.get('id')}': {e}")
        return False, "target_state_unavailable"
    if str(state.get("state") or "") == "ok":
        return True, ""
    return False, str(state.get("reason") or "personal_agent_forbidden")


def apply_tenant_instance_runtime(instance_id: str) -> Dict[str, Any]:
    """Make one tenant instance's running state match its stored state, now.

    Called after a create / edit / enable / disable so the change takes effect
    without a maintenance window. Serialized per instance, and it never raises:
    the credential write has already committed, so a runtime failure must be
    *reported* (``applied`` / ``pending`` / ``error``) rather than turned into a
    failed save.

    The old run is always stopped on the way in, including when the new
    credentials cannot be decrypted or started. That is deliberate: leaving the
    previous run up would be "silently keeping the old credential in service",
    which the requirement forbids.

    Identity mode is deliberately not consulted. This is only reachable from a
    tenant-channel write path, which exists in database mode alone, and the row
    lookup below already answers "is there anything to run" honestly; a mode
    check would instead report a stored instance as applied when it never was.
    """
    with _instance_restart_lock(instance_id):
        try:
            from auth.service import get_identity_service

            service = get_identity_service()
            row = service.get_tenant_channel_instance_row(instance_id)
        except Exception as e:
            _stop_instance_runtime(instance_id)
            logger.error(
                f"[ChannelInstances] cannot read instance '{instance_id}': {e}")
            return _record_runtime_state(
                instance_id, applied=False, error=f"instance lookup failed: {e}")

        if row is None or not row.get("active"):
            # Deleted or disabled: nothing should be running under this id.
            _stop_instance_runtime(instance_id)
            return _record_runtime_state(instance_id, applied=True)

        if (str(row.get("scope") or "tenant") == "user"
                and not _owner_is_active_member(service, row)):
            # A member-owned instance only runs while its owner is an active
            # member of the tenant (task 7.3): a deactivated member must not keep
            # a live vendor connection serving a persona they may no longer use.
            _stop_instance_runtime(instance_id)
            return _record_runtime_state(
                instance_id, applied=False,
                error="the owning member is not an active member of this tenant")

        if str(row.get("scope") or "tenant") == "user":
            usable, reason = _personal_target_usable(service, row)
            if not usable:
                # An unusable target is not a temporary hiccup: the persona the
                # member chose is gone, disabled or no longer theirs, and running
                # the connection anyway would either fail at every message or
                # serve the wrong Agent. Stopped and reported as a failure so the
                # console shows "repair the target" rather than "connecting".
                _stop_instance_runtime(instance_id)
                return _record_runtime_state(
                    instance_id, applied=False,
                    error=f"personal target unusable: {reason}")

        if (str(row.get("scope") or "tenant") == "user"
                and not personal_runtime_enabled(str(row.get("channel_type") or ""))):
            # A personal instance is *stored* before it is *connected* (task 7.5):
            # the console collects the configuration while no real end-to-end
            # acceptance has been recorded for this type, so the switch keeps the
            # vendor connection closed. Reported as pending — not applied — so the
            # member is told "saved, not connected" instead of being lied to in
            # either direction, and a previously running connection is stopped.
            _stop_instance_runtime(instance_id)
            return _record_runtime_state(
                instance_id, applied=False, pending=True,
                error="personal runtime is not enabled for this channel type")

        try:
            credentials = service.channel_instance_credentials(
                row["tenant_id"], instance_id)
        except Exception as e:
            _stop_instance_runtime(instance_id)
            logger.error(
                f"[ChannelInstances] instance '{instance_id}' credentials "
                f"unusable, stopping it: {e}")
            return _record_runtime_state(
                instance_id, applied=False,
                error=f"credentials unusable: {getattr(e, 'code', '') or e}")

        inst = ChannelInstance(
            instance_id=instance_id,
            channel_type=_normalize_type(str(row.get("channel_type") or "")),
            agent_id=str(row.get("agent_id") or ""),
            credentials=credentials,
            legacy=False,
            tenant_id=str(row.get("tenant_id") or ""),
        )

        mgr = _runtime_manager()
        if mgr is None:
            # A console-only process (or a test harness) with no channels: the
            # credentials are fine, the effect is simply not observable here.
            return _record_runtime_state(instance_id, applied=False, pending=True)
        # ``restart`` stops the previous run itself, but the guarantee "the old
        # credential is never left serving" must not depend on another object's
        # internal ordering: stop it here first, so a failure to start still
        # leaves nothing running under the previous credentials.
        _stop_instance_runtime(instance_id)
        try:
            mgr.restart(inst)
        except Exception as e:
            logger.error(
                f"[ChannelInstances] instance '{instance_id}' did not start: {e}",
                exc_info=True)
            return _record_runtime_state(
                instance_id, applied=False, error=str(e) or e.__class__.__name__)
        logger.info(f"[ChannelInstances] instance '{instance_id}' applied immediately")
        return _record_runtime_state(instance_id, applied=True)


def reconcile_instance_runtime(instance_id: str) -> Dict[str, Any]:
    """Make the running state match the stored state, and never raise.

    The identity domain owns the row; the channel runtime owns the connection.
    A write that has already committed must not be turned into a failed save
    because the connection could not follow it, so this wrapper records the
    outcome instead of propagating it. Every personal-channel mutation calls it,
    which is what makes "disable / revoke / governance / unlink takes effect for
    the next message" a property of the write itself rather than of whichever
    console handler happened to remember to reconcile.
    """
    try:
        return apply_tenant_instance_runtime(instance_id)
    except Exception as e:  # noqa: BLE001 - the write already committed
        logger.error(
            f"[ChannelInstances] reconcile failed for '{instance_id}': {e}")
        return _record_runtime_state(
            instance_id, applied=False, pending=True,
            error=f"runtime reconcile failed: {e}")


def load_tenant_channel_instances() -> List[ChannelInstance]:
    """Tenant-owned channel instances to run, from the identity store.

    Thin adapter over the identity layer, imported lazily so ``channel/`` never
    hard-depends on ``auth`` at import time (``channel/external_identity.py``
    does the same). Returns ``[]`` outside database mode.

    This runs during startup, *before* the Web console serves. A failure to
    read the store — or a single instance whose credential cannot be decrypted
    — is therefore logged and degraded, never fatal: the remaining channels and
    the console still come up.
    """
    from channel.external_identity import is_database_mode

    if not is_database_mode():
        return []

    try:
        from auth.service import get_identity_service

        service = get_identity_service()
        rows = service.list_enabled_tenant_channel_instances()
    except Exception as e:
        logger.error(
            f"[ChannelInstances] cannot read tenant channel instances; starting "
            f"without them: {e}"
        )
        return []

    out: List[ChannelInstance] = []
    for row in rows:
        instance_id = str(row.get("id") or "")
        tenant_id = str(row.get("tenant_id") or "")
        if str(row.get("scope") or "tenant") == "user":
            # A member-owned instance is *stored* before it is *connected* (task
            # 7.5). The startup synthesis is a connection path just like the hot
            # restart is, so the same two gates hold here (task 9.1) — otherwise
            # a deployment that never recorded a personal acceptance, or a switch
            # withdrawn for a rollback, would still bring member connections up
            # on boot. Both refusals are logged with their reason and skip only
            # that instance.
            ctype = _normalize_type(str(row.get("channel_type") or ""))
            if not personal_runtime_enabled(ctype):
                logger.info(
                    f"[ChannelInstances] personal instance '{instance_id}' "
                    f"({ctype}) not started: personal runtime is not enabled "
                    f"for this channel type"
                )
                continue
            if not _owner_is_active_member(service, row):
                logger.info(
                    f"[ChannelInstances] personal instance '{instance_id}' "
                    f"not started: its owner is not an active member of tenant "
                    f"'{tenant_id}'"
                )
                continue
            target_ok, target_reason = _personal_target_usable(service, row)
            if not target_ok:
                # The same gate the hot path applies: an instance whose target
                # became unusable must not come back up on boot, or a restart
                # would silently re-open a route the console reports as needing
                # repair (task 2.5).
                logger.info(
                    f"[ChannelInstances] personal instance '{instance_id}' "
                    f"not started: its target is unusable ({target_reason})"
                )
                continue
        try:
            credentials = service.channel_instance_credentials(tenant_id, instance_id)
        except Exception as e:
            logger.error(
                f"[ChannelInstances] tenant instance '{instance_id}' (tenant "
                f"'{tenant_id}') has unusable credentials, skipping: {e}"
            )
            continue
        out.append(
            ChannelInstance(
                instance_id=instance_id,
                channel_type=_normalize_type(str(row.get("channel_type") or "")),
                agent_id=str(row.get("agent_id") or ""),
                credentials=credentials,
                legacy=False,
                # Ownership comes from the registration row, so inbound routing
                # never has to infer a tenant from the bound Agent.
                tenant_id=tenant_id,
            )
        )
    if out:
        logger.info(
            f"[ChannelInstances] loaded {len(out)} tenant-owned channel instance(s)"
        )
    return out


#: Display labels for the channel types a tenant may own, kept beside
#: ``CREDENTIAL_KEYS`` so the console never has to carry a second copy of the
#: field contract. A type missing here still works (its code is shown).
TENANT_CHANNEL_LABELS: Dict[str, Dict[str, str]] = {
    const.FEISHU: {"zh": "飞书", "en": "Feishu"},
    const.DINGTALK: {"zh": "钉钉", "en": "DingTalk"},
    const.WECOM_BOT: {"zh": "企微智能机器人", "en": "WeCom Bot"},
    const.WEIXIN: {"zh": "微信", "en": "WeChat"},
    const.QQ: {"zh": "QQ 机器人", "en": "QQ Bot"},
    const.TELEGRAM: {"zh": "Telegram", "en": "Telegram"},
    const.SLACK: {"zh": "Slack", "en": "Slack"},
    const.DISCORD: {"zh": "Discord", "en": "Discord"},
}

#: Icon + colour per channel type, so the console renders the tenant cards with
#: the same appearance as the platform cards without keeping a second copy of
#: this mapping in JavaScript. A type missing here still renders (generic icon).
TENANT_CHANNEL_APPEARANCE: Dict[str, Dict[str, str]] = {
    const.FEISHU: {"icon": "fa-paper-plane", "color": "blue"},
    const.DINGTALK: {"icon": "fa-comments", "color": "blue"},
    const.WECOM_BOT: {"icon": "fa-robot", "color": "emerald"},
    const.WEIXIN: {"icon": "fa-comment", "color": "emerald"},
    const.QQ: {"icon": "fa-comment", "color": "blue"},
    const.TELEGRAM: {"icon": "fa-paper-plane", "color": "sky"},
    const.SLACK: {"icon": "fa-hashtag", "color": "purple"},
    const.DISCORD: {"icon": "fa-discord", "color": "indigo"},
}

#: Credential field labels for the tenant form. A key missing here falls back to
#: its own name, so adding a key to CREDENTIAL_KEYS never breaks the form.
CREDENTIAL_FIELD_LABELS: Dict[str, Dict[str, str]] = {
    "feishu_app_id": {"zh": "App ID", "en": "App ID"},
    "feishu_app_secret": {"zh": "App Secret", "en": "App Secret"},
    "feishu_token": {"zh": "校验 Token（可选）", "en": "Verification Token (optional)"},
    "feishu_bot_name": {"zh": "机器人名称（可选）", "en": "Bot name (optional)"},
    "dingtalk_client_id": {"zh": "Client ID", "en": "Client ID"},
    "dingtalk_client_secret": {"zh": "Client Secret", "en": "Client Secret"},
    "dingtalk_robot_code": {"zh": "机器人编码", "en": "Robot code"},
    "wecom_bot_id": {"zh": "Bot ID", "en": "Bot ID"},
    "wecom_bot_secret": {"zh": "Secret", "en": "Secret"},
    "wecom_bot_token": {"zh": "Token", "en": "Token"},
    "wecom_bot_encoding_aes_key": {"zh": "EncodingAESKey", "en": "EncodingAESKey"},
    "weixin_token": {"zh": "Token", "en": "Token"},
    "weixin_base_url": {"zh": "回调基址", "en": "Callback base URL"},
    "qq_app_id": {"zh": "App ID", "en": "App ID"},
    "qq_app_secret": {"zh": "App Secret", "en": "App Secret"},
    "telegram_token": {"zh": "Bot Token", "en": "Bot Token"},
    "slack_bot_token": {"zh": "Bot Token (xoxb-)", "en": "Bot Token (xoxb-)"},
    "slack_app_token": {"zh": "App Token (xapp-)", "en": "App Token (xapp-)"},
    "discord_token": {"zh": "Bot Token", "en": "Bot Token"},
}

#: Credential keys whose value must never be echoed back to a client.
_SECRET_KEY_HINTS = ("secret", "token", "aes", "key", "password")

#: The credential key(s) that name the **external application** an instance
#: connects as (change enable-member-personal-console, task 6.3). Two instances
#: holding the same application would fight over one vendor connection, so the
#: service refuses the second active one — including when one is the tenant's
#: shared instance and the other is a member's personal one, which is exactly
#: the conflict a member could otherwise create by pasting the tenant's App ID.
#:
#: A type missing here has no declared application identity, so no cross-instance
#: conflict can be detected for it; that is a deliberate "unknown" rather than a
#: guess, and the readiness list keeps such a type out of personal onboarding.
APP_IDENTITY_KEYS: Dict[str, tuple] = {
    const.FEISHU: ("feishu_app_id",),
    const.DINGTALK: ("dingtalk_client_id",),
    const.WECOM_BOT: ("wecom_bot_id",),
    # WeChat's QR login hands back one bot token per scanned bot (task 7.3):
    # the token *is* the application, exactly like Telegram/Slack/Discord below.
    # The earlier revision used the callback base URL, which is the same
    # well-known vendor host for every bot — so a member's personal create was
    # refused as "this application is already connected" whenever *any* weixin
    # instance was active, while the value that actually names one bot (the
    # token) was never compared.
    const.WEIXIN: ("weixin_token",),
    const.QQ: ("qq_app_id",),
    # For a bot-token provider the token *is* the application: it names the bot
    # and its installation, and two instances cannot share one.
    const.TELEGRAM: ("telegram_token",),
    const.SLACK: ("slack_bot_token",),
    const.DISCORD: ("discord_token",),
}


def app_identity(channel_type: str, credentials: Optional[Dict[str, Any]]) -> str:
    """A comparable digest naming the external application behind a bundle.

    Returns ``""`` when the type declares no application keys or none is
    configured — "unknown", never "same as everyone else" — so an undeclared
    type is never blocked by a comparison it cannot support.
    """
    keys = APP_IDENTITY_KEYS.get(_normalize_type(channel_type)) or ()
    parts = []
    for key in keys:
        value = str((credentials or {}).get(key) or "").strip()
        if value:
            parts.append(f"{key}={value}")
    if not parts:
        return ""
    from auth.crypto import fingerprint

    try:
        return fingerprint(_normalize_type(channel_type) + "\x1f" + "\x1f".join(parts))
    except Exception as e:  # noqa: BLE001 - an unusable master key is reported elsewhere
        logger.warning(f"[ChannelInstances] cannot fingerprint application: {e}")
        return ""


def scan_default_display_name(channel_type: str, taken: Any = ()) -> str:
    """The name an instance gets when the operator types none (task 7.3).

    A scan has no form to fill in: the operator points their phone at a QR and
    the instance appears. The name therefore has to come from the type itself,
    which is what the console's own form pre-fills — the type's label — with the
    first free ordinal appended when that name is taken, because the identity
    store refuses a second *active* instance of one type with one name.

    ``taken`` is the set of names already in use for the same type, scope and
    owner, passed in by the caller (it owns the store lookup). ``2`` is the first
    ordinal on purpose: "微信" is the first instance and "微信 2" the second, so a
    console list reads like a user wrote it.
    """
    ctype = _normalize_type((channel_type or "").strip())
    label = (TENANT_CHANNEL_LABELS.get(ctype) or {}).get("zh") or ctype or "channel"
    used = {str(name or "").strip() for name in (taken or ())}
    if label not in used:
        return label
    ordinal = 2
    while f"{label} {ordinal}" in used:
        ordinal += 1
    return f"{label} {ordinal}"


def tenant_channel_types() -> List[Dict[str, Any]]:
    """Channel types a tenant may own, with the fields its form must collect.

    This mirrors the server-side gate exactly — a type is offered only when it
    is both multi-instance ready and has declared credential keys — so the
    console cannot present a type that ``create_tenant_channel_instance`` would
    then reject. Labels are display-only; the credential *keys* are the contract
    (the server rejects any field outside them).

    This list deliberately serves two masters, and the two must not be
    conflated: it is the candidate set for a *new* connection, and it is also
    the field contract the console looks up for a row that already exists. A
    type whose inbound can never be accepted (``inbound_admissible`` false) must
    therefore stay listed *with its fields* — dropping the entry would render an
    existing row's form with no fields at all — and carries its own verdict
    instead, which the candidate side narrows on. The personal ``ready`` verdict
    is not a substitute: it also carries member-only meanings (deployment
    narrowing, tenant policy) that must not decide the administrator's surface.
    """
    out: List[Dict[str, Any]] = []
    for channel_type in sorted(MULTI_INSTANCE_READY):
        keys = CREDENTIAL_KEYS.get(channel_type) or ()
        if not keys:
            continue
        required = set(REQUIRED_CREDENTIAL_KEYS.get(channel_type) or ())
        out.append({
            "channel_type": channel_type,
            # Whether a *new* connection of this type could ever receive a
            # message here. False means "do not offer it as a candidate, but its
            # row (if one exists) still has this contract".
            "inbound_admissible": inbound_identity_admissible(channel_type),
            "label": TENANT_CHANNEL_LABELS.get(
                channel_type, {"zh": channel_type, "en": channel_type}),
            # Appearance is presentation-only, but keeping it server-side stops
            # the console from carrying a second copy that drifts from this one.
            "icon": (TENANT_CHANNEL_APPEARANCE.get(channel_type) or {}).get(
                "icon", "fa-tower-broadcast"),
            "color": (TENANT_CHANNEL_APPEARANCE.get(channel_type) or {}).get(
                "color", "primary"),
            "credential_fields": [
                {
                    "key": key,
                    "label": CREDENTIAL_FIELD_LABELS.get(
                        key, {"zh": key, "en": key}),
                    # Secret-looking fields render as password inputs and are
                    # never sent back down; the bundle is write-only.
                    "secret": any(hint in key for hint in _SECRET_KEY_HINTS),
                    # Required-ness travels with the same declaration the server
                    # validates against, so the console never keeps a second
                    # copy of the minimum set that could drift from this one.
                    "required": key in required,
                }
                for key in keys
            ],
        })
    return out


#: Channel types whose **personal** (member-owned) onboarding boundary is
#: declared ready (change enable-member-personal-console, task 6.3).
#:
#: Being multi-instance ready is the *first* of two requirements, not both of
#: them: running several instances is a different fact from being able to prove
#: which sender a message came from, and :func:`personal_channel_ready` applies
#: the second one (:func:`inbound_identity_admissible`) on top of this set.
#: Seeding this from :data:`MULTI_INSTANCE_READY` is only the "runs several
#: instances" half — do not read it as a readiness verdict. Being listed here
#: makes a type *offered*; it does not make it run. Each type still has to record
#: a real inbound acceptance (task 7.5) before the personal-runtime switch is
#: opened for it, and an operator may narrow this set per deployment with
#: ``personal_channel_ready_types``.
PERSONAL_READY_CHANNEL_TYPES = frozenset(MULTI_INSTANCE_READY)

#: Actionable reason codes for a type that is not open for personal access.
#: A bare "bad request" would leave the member guessing whether they mistyped
#: the type or the deployment has not opened it.
PERSONAL_NOT_READY_REASONS = frozenset({
    # The provider cannot dispatch per instance yet: one connection only.
    "not_multi_instance",
    # Runs several instances, but the adapter never stamps the sender, so its
    # inbound is refused before any binding lookup (see
    # :data:`INBOUND_IDENTITY_STAMPING_TYPES`).
    "no_inbound_identity",
    # Multi-instance, but its per-instance sender boundary is not declared.
    "not_declared",
    # Declared as not open for this deployment.
    "not_allowed",
    # Known type, but its credential contract is incomplete.
    "no_credential_contract",
})


def _configured_personal_types() -> Optional[set]:
    """Operator narrowing of :data:`PERSONAL_READY_CHANNEL_TYPES`, or None.

    Only ever narrows: a type absent from the base set cannot be added back by
    configuration, so a config mistake cannot open an unverified channel.
    """
    try:
        from config import conf
        raw = conf().get("personal_channel_ready_types")
    except Exception:
        return None
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return None
    return {_normalize_type(str(item or "").strip()) for item in raw if str(item or "").strip()}


def personal_channel_ready(channel_type: str):
    """``(ready, reason)`` for personal onboarding of *channel_type*.

    ``ready`` is about the **declaration**, not the runtime: a ready type may be
    configured even while the personal-runtime switch keeps it from connecting,
    which is what lets the console collect a configuration without pretending
    the channel is live. The declaration has two halves — the type can run
    several instances *and* its inbound can prove which sender a message came
    from (:func:`inbound_identity_admissible`); a type missing either one is not
    configurable, because the connection it produced could never be talked to.
    """
    ctype = _normalize_type((channel_type or "").strip())
    if not ctype:
        return False, "no_credential_contract"
    if not CREDENTIAL_KEYS.get(ctype):
        # Unknown type, or one with no declared credential contract.
        return False, ("not_multi_instance" if ctype not in MULTI_INSTANCE_READY
                       else "no_credential_contract")
    if ctype not in MULTI_INSTANCE_READY:
        return False, "not_multi_instance"
    if not inbound_identity_admissible(ctype):
        # Checked before the declaration list below so the member is told the
        # fact that actually blocks the type — the adapter cannot prove who sent
        # a message — rather than the deployment-narrowing reason that would
        # otherwise answer first and be wrong.
        return False, "no_inbound_identity"
    if ctype not in PERSONAL_READY_CHANNEL_TYPES:
        return False, "not_declared"
    narrowed = _configured_personal_types()
    if narrowed is not None and ctype not in narrowed:
        return False, "not_allowed"
    return True, ""


def personal_channel_types() -> List[Dict[str, Any]]:
    """The personal-onboarding type list, readiness included.

    Same field contract as :func:`tenant_channel_types` (one declaration, no
    second copy in the console), plus the readiness verdict so the member sees
    either the form or the reason the type is not open yet.
    """
    out: List[Dict[str, Any]] = []
    for entry in tenant_channel_types():
        ready, reason = personal_channel_ready(entry["channel_type"])
        item = dict(entry)
        item["ready"] = ready
        item["reason"] = reason
        out.append(item)
    return out


#: Channel types with a **recorded real end-to-end acceptance** for personal
#: execution (change enable-member-personal-console, task 7.5).
#:
#: Readiness (`personal_channel_ready`) answers "may this type be *configured*".
#: This answers "may it *connect*", and the two are deliberately separate: the
#: console can collect and validate a configuration while no vendor traffic has
#: ever been proven to reach the right member. Opening a type means a real
#: provider round trip was observed (task 7.5 raises the record), so this set is
#: empty until that evidence exists — a declaration mistake must not be able to
#: start routing a member's private conversations on an unverified boundary.
#:
#: ``wecom_bot`` was recorded here on 2026-09-20 after the acceptance listed in
#: ``openspec/changes/enable-personal-wecom-bot-runtime/evidence/1-runtime-acceptance.md``
#: (a real tenant-owned bot connection observed in a process that actually
#: assembles ``_channel_mgr``, with the adapter's identity stamp proven by
#: ``inbound_identity_admissible``). Adding a type here only permits a member's
#: own connection to *start*; the deployment master switch
#: (``personal_channel_runtime``) still has to be on, and the console still has
#: to bind the sender's account before any message routes.
PERSONAL_RUNTIME_ACCEPTED_TYPES: frozenset = frozenset({const.WECOM_BOT})

#: Channel types that may carry a member's own private-chat route on a **shared**
#: instance (task 7.2). Same reasoning as above: empty until a real acceptance
#: exists, and further narrowed per deployment, never widened, by
#: ``public_personal_ingress_types``.
PUBLIC_PERSONAL_INGRESS_TYPES: frozenset = frozenset()


def _configured_subset(base: frozenset, key: str) -> set:
    """Deployment narrowing of *base*, or ``base`` itself when unset.

    Only ever narrows: a value naming a type outside *base* cannot add it back,
    so a config typo can never open an unverified boundary.
    """
    try:
        from config import conf
        raw = conf().get(key)
    except Exception:
        return set(base)
    if raw is None:
        return set(base)
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return set(base)
    wanted = {_normalize_type(str(item or "").strip()) for item in raw}
    return {ctype for ctype in base if ctype in wanted}


def _personal_runtime_capability_enabled() -> bool:
    """The deployment-wide master switch for personal connections (task 9.1).

    Read through policy so the console projection, the connection gate and the
    inbound verifier can never disagree. An unevaluable switch is a *closed*
    switch: nothing about this boundary may fail open.
    """
    try:
        from auth.policy import personal_capability_enabled
        return personal_capability_enabled("personal_channel_runtime")
    except Exception:  # noqa: BLE001 - fail closed
        return False


def personal_runtime_enabled(channel_type: str) -> bool:
    """Whether a personal instance of this type may hold a live connection.

    False keeps the vendor connection closed while the configuration stays
    stored and inspectable, which is the honest posture for a type whose real
    inbound acceptance has not been recorded yet.

    Two independent conditions must both hold: the deployment-wide master switch
    (``personal_channel_runtime``) and a recorded per-type acceptance. The master
    is checked first so a rollback can stop every personal connection at once
    without editing the per-type record.
    """
    ctype = _normalize_type((channel_type or "").strip())
    if not ctype:
        return False
    if not _personal_runtime_capability_enabled():
        return False
    return ctype in _configured_subset(
        PERSONAL_RUNTIME_ACCEPTED_TYPES, "personal_channel_runtime_types")


def public_personal_ingress_ready(channel_type: str) -> bool:
    """Whether a shared instance of this type may carry personal routes."""
    ctype = _normalize_type((channel_type or "").strip())
    if not ctype:
        return False
    if not personal_channel_ready(ctype)[0]:
        # A type that cannot prove senders per instance cannot tell one member's
        # private chat from another's, so it cannot host personal routes either.
        return False
    if not _personal_runtime_capability_enabled():
        # Shared-instance personal routes are still a live personal connection:
        # the same deployment-wide master governs them (task 9.1).
        return False
    return ctype in _configured_subset(
        PUBLIC_PERSONAL_INGRESS_TYPES, "public_personal_ingress_types")
