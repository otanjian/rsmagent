# encoding:utf-8
"""Connection types: the finite catalogue, scope rules and config validation.

Why this is not spread across the handlers
------------------------------------------
Every entry point — the console's type picker, the four CRUD surfaces, the
loader used by the (later) type adapters — has to agree on what a valid MCP /
ERP / OA / email configuration *is*. A per-handler copy is how "the picker
offers a field the save rejects" happens, and the spec makes the type
catalogue itself a server-side fact ("连接类型与归属创建后不可改").

What a connection's non-secret configuration may contain is therefore declared
here, once:

* :data:`TYPE_SPECS` — which ``(kind, scope)`` combinations exist, the config
  keys each accepts, and the secret slots a kind can carry;
* :func:`validate_config` — the normalizer/validator, returning the canonical
  config that is what actually gets stored. It rejects unknown keys, any key
  that looks like a secret (those must use the credential mechanism, not
  ``config_json``), malformed URLs (userinfo is refused, so a URL can never
  smuggle a credential) and out-of-range ports/timeouts.
* :func:`secret_slots` — the slots a kind may reference, so a client cannot
  invent an arbitrary slot name or point at an arbitrary credential id.

Readiness is deliberately **not** claimed here: :data:`READINESS` reports the
capability classes this build serves, and at this stage every type is
``configure`` only. Test and execute classes arrive with their own evidence
(design §7) — a page must never present a type as testable because a form
exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

from integrations.external.adapters.base import adapter_available
from integrations.external.errors import invalid

# -- kind / scope vocabulary -------------------------------------------------

KIND_MCP = "mcp"
KIND_ERP = "erp"
KIND_OA = "oa"
KIND_EMAIL = "email"

KINDS: Tuple[str, ...] = (KIND_MCP, KIND_ERP, KIND_OA, KIND_EMAIL)

SCOPE_PLATFORM = "platform"
SCOPE_TENANT = "tenant"
SCOPE_PERSONAL = "personal"

SCOPES: Tuple[str, ...] = (SCOPE_PLATFORM, SCOPE_TENANT, SCOPE_PERSONAL)

#: Secret slot names a kind may own. A slot is referenced by name (never by a
#: client-chosen credential id), and a kind that has no slot cannot be given
#: one — which is what stops a request pointing a platform connection at a
#: tenant credential.
SLOTS: Dict[str, FrozenSet[str]] = {
    KIND_MCP: frozenset({"header", "env", "oauth"}),
    KIND_ERP: frozenset({"password"}),
    KIND_OA: frozenset({"password", "app_secret"}),
    KIND_EMAIL: frozenset({"imap_password", "smtp_password"}),
}

#: Slots that must be present (non-empty) for a connection of this kind to be
#: usable. They are not required to *save* a draft configuration (the console
#: saves and tests separately), but a saved connection missing them reports
#: ``secret_missing`` in its capability projection.
REQUIRED_SLOTS: Dict[str, FrozenSet[str]] = {
    KIND_MCP: frozenset(),
    KIND_ERP: frozenset({"password"}),
    KIND_OA: frozenset({"password"}),
    KIND_EMAIL: frozenset(),
}

#: Capability classes every kind serves unconditionally. ``configure`` is the
#: control plane, which is this build's delivered and accepted slice.
BASELINE_CLASSES: FrozenSet[str] = frozenset({"configure"})

#: Capability classes a kind *can* serve, and which the deployment opens by
#: policy. This is the server-side type slice switch of design §7: it defaults
#: to closed, so a deployment that has not accepted the corresponding evidence
#: reports the class as unavailable with a reason rather than exposing it.
#:
#: The code for every class below exists in this build; what the switch gates
#: is whether *this deployment* has the environment and the accepted evidence
#: for it. Keeping the gate here (and not as a missing code path) is what makes
#: "开关默认关闭" a checkable fact rather than a comment.
OPENABLE_CLASSES: Dict[str, FrozenSet[str]] = {
    KIND_MCP: frozenset({"test", "read_execute"}),
    KIND_ERP: frozenset({"test", "read_execute"}),
    KIND_OA: frozenset({"test", "read_execute", "write_execute"}),
    KIND_EMAIL: frozenset({"test", "read_execute", "write_execute"}),
}

#: Classes that additionally require a second, independent acceptance. A
#: stdio MCP server runs a local program, so its test and execute classes stay
#: closed even when the deployment opens MCP testing, until the isolation
#: story is accepted (design §7 "stdio 另有 execution-isolation").
SECOND_GATE: Dict[str, str] = {
    # (kind, class) -> the deployment switch that must also be on.
}

#: Classes that remain closed in this build because the *code* path is not the
#: thing missing — an external acceptance is. Kept as an explicit list so the
#: projection can name each one instead of emitting a vague "not ready".
WITHHELD: Dict[str, FrozenSet[str]] = {
    # Nothing is withheld by construction. A class is closed because the
    # deployment has not opened it (see :func:`open_classes`), which is a
    # configuration fact the console can print and an operator can change.
}

READINESS_REASON = {
    KIND_MCP: "awaiting_mcp_test_environment",
    KIND_ERP: "awaiting_sap_test_environment",
    KIND_OA: "awaiting_oa_test_environment",
    KIND_EMAIL: "awaiting_mail_test_environment",
}

#: Kinds whose production adapter is not a real integration. Kept explicit so
#: the migration of a legacy U9/金蝶 row can be labelled and disabled instead of
#: being offered as a working connection type (spec ``erp-connection-integration``).
UNSUPPORTED_ERP_PROVIDERS = frozenset({"u9", "kingdee", "k3", "eas"})

_MAX_STRING = 512
_MAX_LIST = 64
_MAX_URL = 1024
_SECRETISH = re.compile(r"(pass|secret|token|key|credential|authorization)", re.I)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_EMAIL_ADDR = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: What a ``tool_name_prefix`` may contain. Whitespace, the ``:`` id separator
#: and the ``/`` in a path are excluded: the prefix is prepended to a tool name
#: that then appears in a prompt, a log line and a grant id, and any of those
#: three would become ambiguous. Kept next to ``_ENV_NAME`` so the two
#: "this string becomes part of an identifier" rules read together.
_TOOL_NAME_PREFIX = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


@dataclass(frozen=True)
class TypeSpec:
    """One connection type: where it may live, what it accepts, what it owns."""

    kind: str
    label: str
    label_key: str
    scopes: FrozenSet[str]
    config_keys: FrozenSet[str]
    secret_slots: FrozenSet[str]
    #: Keys a client may never move after creation (ownership is server-derived).
    immutable_keys: FrozenSet[str] = field(default_factory=frozenset)


_SPECS: Tuple[TypeSpec, ...] = (
    TypeSpec(
        kind=KIND_MCP,
        label="MCP",
        label_key="ext_conn_type_mcp",
        scopes=frozenset({SCOPE_PLATFORM, SCOPE_TENANT}),
        config_keys=frozenset({
            "transport", "url", "auth", "header_name", "command", "args",
            "env_keys", "oauth_provider", "tool_name_prefix",
        }),
        secret_slots=SLOTS[KIND_MCP],
    ),
    TypeSpec(
        kind=KIND_ERP,
        label="ERP (SAP)",
        label_key="ext_conn_type_erp",
        scopes=frozenset({SCOPE_TENANT}),
        config_keys=frozenset({
            "provider", "ashost", "sysnr", "client", "user", "lang",
            "base_url", "verify_ssl", "timeout",
        }),
        secret_slots=SLOTS[KIND_ERP],
    ),
    TypeSpec(
        kind=KIND_OA,
        label="OA",
        label_key="ext_conn_type_oa",
        scopes=frozenset({SCOPE_TENANT}),
        config_keys=frozenset({
            "base_url", "username", "tenant_key", "custom_page_config_id",
            "app_key", "corp_id", "attachment_dirs",
        }),
        secret_slots=SLOTS[KIND_OA],
    ),
    TypeSpec(
        kind=KIND_EMAIL,
        label="Email",
        label_key="ext_conn_type_email",
        scopes=frozenset({SCOPE_PERSONAL}),
        config_keys=frozenset({
            "imap", "smtp", "attachment_dirs",
        }),
        secret_slots=SLOTS[KIND_EMAIL],
    ),
)

TYPE_SPECS: Dict[str, TypeSpec] = {spec.kind: spec for spec in _SPECS}


def spec_for(kind: str) -> TypeSpec:
    spec = TYPE_SPECS.get(str(kind or "").strip())
    if spec is None:
        raise invalid("unknown connection type", code="unknown_type")
    return spec


def scopes_for(kind: str) -> FrozenSet[str]:
    return spec_for(kind).scopes


def secret_slots(kind: str) -> FrozenSet[str]:
    return spec_for(kind).secret_slots


def validate_scope(kind: str, scope: str) -> None:
    """Refuse a ``(kind, scope)`` pair the product does not have."""
    if scope not in spec_for(kind).scopes:
        raise invalid(
            "connection type %r is not available in scope %r" % (kind, scope),
            code="unsupported_scope")


# -- config normalization ----------------------------------------------------

def _string(config: Mapping[str, Any], key: str, *, required: bool = False,
            default: str = "", max_len: int = _MAX_STRING) -> str:
    raw = config.get(key, None)
    if raw is None:
        if required and not default:
            raise invalid("%s is required" % key, code="field_required",
                          fields={key: "required"})
        return default
    if not isinstance(raw, str):
        raise invalid("%s must be a string" % key, code="field_type",
                      fields={key: "type"})
    value = raw.strip()
    if len(value) > max_len:
        raise invalid("%s is too long" % key, code="field_too_long",
                      fields={key: "too_long"})
    if required and not value:
        raise invalid("%s is required" % key, code="field_required",
                      fields={key: "required"})
    return value


def _boolean(config: Mapping[str, Any], key: str, *, default: bool) -> bool:
    raw = config.get(key, None)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    raise invalid("%s must be a boolean" % key, code="field_type",
                  fields={key: "type"})


def _integer(config: Mapping[str, Any], key: str, *, default: int,
             low: int, high: int) -> int:
    raw = config.get(key, None)
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise invalid("%s must be an integer" % key, code="field_type",
                      fields={key: "type"})
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise invalid("%s must be an integer" % key, code="field_type",
                      fields={key: "type"})
    if value < low or value > high:
        raise invalid("%s is out of range" % key, code="field_range",
                      fields={key: "range"})
    return value


def _string_list(config: Mapping[str, Any], key: str, *, default=(),
                 max_items: int = _MAX_LIST, pattern: Optional[re.Pattern] = None,
                 item_max: int = _MAX_STRING) -> List[str]:
    raw = config.get(key, None)
    if raw is None:
        return list(default)
    if not isinstance(raw, (list, tuple)):
        raise invalid("%s must be a list" % key, code="field_type",
                      fields={key: "type"})
    if len(raw) > max_items:
        raise invalid("%s has too many entries" % key, code="field_too_long",
                      fields={key: "too_long"})
    out: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise invalid("%s entries must be strings" % key, code="field_type",
                          fields={key: "type"})
        value = item.strip()
        if not value or len(value) > item_max:
            raise invalid("%s entry is invalid" % key, code="field_invalid",
                          fields={key: "invalid"})
        if pattern is not None and not pattern.match(value):
            raise invalid("%s entry is invalid" % key, code="field_invalid",
                          fields={key: "invalid"})
        out.append(value)
    return out


def _tool_name_prefix(config: Mapping[str, Any]) -> str:
    """A validated ``tool_name_prefix``, or ``""`` when absent.

    Returns the empty string rather than raising when the key is missing: the
    prefix is optional, and "no prefix" is the same as "the empty prefix" for
    every consumer. A *present but unusable* value is refused, because silently
    ignoring it would re-name the server's tools — a change to what an existing
    grant points at, made by a validator.
    """
    raw = config.get("tool_name_prefix")
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    if not _TOOL_NAME_PREFIX.match(text):
        raise invalid("tool_name_prefix must be 1-64 characters of letters, "
                      "digits, dot, dash or underscore", code="field_invalid",
                      fields={"tool_name_prefix": "invalid"})
    return text


def _http_url(value: str, key: str, *, allow_path: bool = True) -> str:
    """A remote address with no embedded credentials.

    ``userinfo`` is refused outright rather than stripped: a URL that carries
    ``user:pass@`` is a credential in a non-secret field, and the spec forbids
    exactly that ("源 URL 禁止 userinfo").
    """
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise invalid("%s must be an http(s) URL" % key, code="field_invalid",
                      fields={key: "invalid"})
    if parsed.username or parsed.password:
        raise invalid("%s must not embed credentials" % key,
                      code="field_invalid", fields={key: "invalid"})
    if not allow_path and parsed.path not in ("", "/"):
        raise invalid("%s must not carry a path" % key, code="field_invalid",
                      fields={key: "invalid"})
    return value


def _reject_unknown_config_keys(config: Mapping[str, Any], allowed: Iterable[str],
                                kind: str) -> None:
    allowed = set(allowed)
    for key in config:
        name = str(key)
        if name in allowed:
            continue
        if _SECRETISH.search(name):
            # A secret belongs in the credential mechanism, never in the
            # non-secret projection; refusing (rather than dropping) is what
            # makes a client's mistake visible instead of silently unset.
            raise invalid(
                "%s must be stored as a secret, not in the configuration" % name,
                code="secret_in_config", fields={name: "secret_in_config"})
        raise invalid("unknown configuration field %r for %s" % (name, kind),
                      code="unknown_field", fields={name: "unknown"})


def _validate_mcp(config: Mapping[str, Any]) -> Dict[str, Any]:
    spec = spec_for(KIND_MCP)
    _reject_unknown_config_keys(config, spec.config_keys, KIND_MCP)
    transport = _string(config, "transport", required=True)
    if transport not in ("stdio", "sse", "streamable_http"):
        raise invalid("transport must be stdio, sse or streamable_http",
                      code="field_invalid", fields={"transport": "invalid"})
    out: Dict[str, Any] = {"transport": transport}
    # ``tool_name_prefix`` is part of *identity*, not display: the tool name it
    # composes is the resource id an existing grant and an Agent allowlist quote
    # (see ``mcp_identity``). It is therefore validated and carried, never
    # dropped -- a migration that lost it would silently re-name every tool the
    # server contributes and orphan the grants filed under the old names.
    prefix = _tool_name_prefix(config)
    if prefix:
        out["tool_name_prefix"] = prefix
    # ``tool_name_prefix`` is optional and carried above. There is deliberately
    # no per-tool list here: which remote tools a connection publishes is
    # discovered from the server, and a configuration field naming them would be
    # a second, hand-maintained copy of that answer -- one the operator would
    # have to write without being told what the server publishes.
    if transport == "stdio":
        for forbidden in ("url", "auth", "header_name", "oauth_provider"):
            if config.get(forbidden) not in (None, ""):
                raise invalid("%s is not used by a stdio connection" % forbidden,
                              code="field_invalid", fields={forbidden: "not_allowed"})
        out["command"] = _string(config, "command", required=True)
        out["args"] = _string_list(config, "args")
        out["env_keys"] = _string_list(config, "env_keys", pattern=_ENV_NAME)
        return out
    for forbidden in ("command", "args", "env_keys"):
        if config.get(forbidden) not in (None, "", []):
            raise invalid("%s is not used by a remote connection" % forbidden,
                          code="field_invalid", fields={forbidden: "not_allowed"})
    out["url"] = _http_url(_string(config, "url", required=True, max_len=_MAX_URL),
                           "url")
    auth = _string(config, "auth", default="none") or "none"
    if auth not in ("none", "header", "oauth"):
        raise invalid("auth must be none, header or oauth", code="field_invalid",
                      fields={"auth": "invalid"})
    out["auth"] = auth
    if auth == "header":
        out["header_name"] = _string(config, "header_name", required=True)
    elif config.get("header_name") not in (None, ""):
        raise invalid("header_name requires auth=header", code="field_invalid",
                      fields={"header_name": "not_allowed"})
    if auth == "oauth":
        out["oauth_provider"] = _string(config, "oauth_provider", default="generic")
    return out


def _validate_erp(config: Mapping[str, Any]) -> Dict[str, Any]:
    spec = spec_for(KIND_ERP)
    _reject_unknown_config_keys(config, spec.config_keys, KIND_ERP)
    provider = _string(config, "provider", required=True)
    if provider in UNSUPPORTED_ERP_PROVIDERS:
        raise invalid(
            "provider %r has no production adapter in this build" % provider,
            code="unsupported_provider", fields={"provider": "unsupported"})
    if provider not in ("rfc", "adt_sql"):
        raise invalid("provider must be rfc or adt_sql", code="field_invalid",
                      fields={"provider": "invalid"})
    out: Dict[str, Any] = {"provider": provider}
    if provider == "rfc":
        for key in ("base_url", "verify_ssl", "timeout"):
            if config.get(key) not in (None, ""):
                raise invalid("%s is not used by an RFC connection" % key,
                              code="field_invalid", fields={key: "not_allowed"})
        out["ashost"] = _string(config, "ashost", required=True)
        out["sysnr"] = _string(config, "sysnr", required=True)
        out["client"] = _string(config, "client", required=True)
        out["user"] = _string(config, "user", required=True)
        out["lang"] = _string(config, "lang", default="EN") or "EN"
        return out
    for key in ("ashost", "sysnr", "lang"):
        if config.get(key) not in (None, ""):
            raise invalid("%s is not used by an ADT SQL connection" % key,
                          code="field_invalid", fields={key: "not_allowed"})
    out["base_url"] = _http_url(
        _string(config, "base_url", required=True, max_len=_MAX_URL), "base_url")
    out["client"] = _string(config, "client", required=True)
    out["user"] = _string(config, "user", required=True)
    out["verify_ssl"] = _boolean(config, "verify_ssl", default=True)
    out["timeout"] = _integer(config, "timeout", default=30, low=1, high=300)
    return out


def _validate_oa(config: Mapping[str, Any]) -> Dict[str, Any]:
    spec = spec_for(KIND_OA)
    _reject_unknown_config_keys(config, spec.config_keys, KIND_OA)
    out: Dict[str, Any] = {
        "base_url": _http_url(
            _string(config, "base_url", required=True, max_len=_MAX_URL), "base_url"),
        "username": _string(config, "username", required=True),
    }
    for key in ("tenant_key", "custom_page_config_id", "app_key", "corp_id"):
        value = _string(config, key)
        if value:
            out[key] = value
    # ``attachment_dirs`` bounds where a fetched attachment may be written. Its
    # deeper checks (relative, inside the workspace, no duplicates) live in the
    # adapter's ``validate_config``; the shape check is here so a non-list never
    # reaches storage.
    raw_dirs = config.get("attachment_dirs")
    if raw_dirs not in (None, "", [], {}):
        if not isinstance(raw_dirs, (list, tuple)):
            raise invalid("attachment_dirs must be a list", code="field_type",
                          fields={"attachment_dirs": "type"})
        out["attachment_dirs"] = [str(entry).strip() for entry in raw_dirs
                                 if str(entry).strip()]
    # A site URL and account are what "登录型" means; the OpenAPI fields stay
    # optional so a login-only connection can be saved, and the capability
    # projection reports the OpenAPI class as not ready rather than refusing the
    # save (spec: 登录正常但 OpenAPI 未配置).
    return out


def _validate_mail_side(config: Mapping[str, Any], key: str, *,
                        default_port: int, needs_from: bool) -> Dict[str, Any]:
    raw = config.get(key)
    if raw is None:
        return {"enabled": False}
    if not isinstance(raw, Mapping):
        raise invalid("%s must be an object" % key, code="field_type",
                      fields={key: "type"})
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise invalid("%s.enabled must be a boolean" % key, code="field_type",
                      fields={key: "type"})
    if not enabled:
        return {"enabled": False}
    out: Dict[str, Any] = {
        "enabled": True,
        "host": _string(raw, "host", required=True),
        "port": _integer(raw, "port", default=default_port, low=1, high=65535),
        "user": _string(raw, "user", required=True),
        "reject_unauthorized": _boolean(raw, "reject_unauthorized", default=True),
    }
    if needs_from:
        from_addr = _string(raw, "from_addr", required=True)
        if not _EMAIL_ADDR.match(from_addr):
            raise invalid("from_addr must be an email address",
                          code="field_invalid", fields={"from_addr": "invalid"})
        out["from_addr"] = from_addr
        tls_mode = _string(raw, "tls_mode", default="implicit_tls") or "implicit_tls"
        if tls_mode not in ("implicit_tls", "starttls", "none"):
            raise invalid("tls_mode is invalid", code="field_invalid",
                          fields={"tls_mode": "invalid"})
        out["tls_mode"] = tls_mode
    else:
        out["tls"] = _boolean(raw, "tls", default=True)
        out["mailbox"] = _string(raw, "mailbox", default="INBOX") or "INBOX"
    allowed = {"enabled", "host", "port", "user", "reject_unauthorized"} | (
        {"from_addr", "tls_mode"} if needs_from else {"tls", "mailbox"})
    for name in raw:
        if str(name) not in allowed:
            raise invalid("unknown %s field %r" % (key, name), code="unknown_field",
                          fields={str(name): "unknown"})
    return out


def _validate_email(config: Mapping[str, Any]) -> Dict[str, Any]:
    spec = spec_for(KIND_EMAIL)
    _reject_unknown_config_keys(config, spec.config_keys, KIND_EMAIL)
    imap = _validate_mail_side(config, "imap", default_port=993, needs_from=False)
    smtp = _validate_mail_side(config, "smtp", default_port=465, needs_from=True)
    if not imap.get("enabled") and not smtp.get("enabled"):
        raise invalid("at least one of imap or smtp must be enabled",
                      code="no_protocol", fields={"imap": "required",
                                                  "smtp": "required"})
    dirs = _string_list(config, "attachment_dirs")
    if len(dirs) > 8:
        raise invalid("attachment_dirs has too many entries", code="field_too_long",
                      fields={"attachment_dirs": "too_long"})
    for entry in dirs:
        if entry.startswith(("/", "\\")) or ".." in entry.replace("\\", "/").split("/"):
            raise invalid("attachment_dirs must be relative and inside the workspace",
                          code="field_invalid", fields={"attachment_dirs": "invalid"})
    return {"imap": imap, "smtp": smtp, "attachment_dirs": dirs}


_VALIDATORS = {
    KIND_MCP: _validate_mcp,
    KIND_ERP: _validate_erp,
    KIND_OA: _validate_oa,
    KIND_EMAIL: _validate_email,
}


def validate_config(kind: str, config: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return the canonical non-secret configuration for ``kind``.

    Raises :class:`ExternalConnectionError` (400, code ``invalid`` and friends)
    on anything the type does not accept. The returned dict is what gets
    persisted, so a caller can never store an unvalidated key.
    """
    spec_for(kind)
    if config is None:
        config = {}
    if not isinstance(config, Mapping):
        raise invalid("config must be an object", code="field_type",
                      fields={"config": "type"})
    return _VALIDATORS[kind](config)


def _readiness_config() -> Mapping[str, Any]:
    """The deployment's readiness block, re-read on every call.

    Re-read rather than cached so opening a class takes effect without a
    restart, and so a test can open one for the duration of a case. A
    misconfigured block is treated as empty (everything closed) rather than
    raising: an unreadable policy must not become an open one.
    """
    try:
        from config import conf
        raw = (conf() or {}).get("external_connections") or {}
        if not isinstance(raw, Mapping):
            return {}
        block = raw.get("readiness") or {}
        return block if isinstance(block, Mapping) else {}
    except Exception:  # noqa: BLE001
        return {}


def open_classes(kind: str) -> FrozenSet[str]:
    """Classes this deployment currently serves for ``kind``.

    ``configure`` is always present. Every other class requires an explicit
    per-kind opt-in, so the default deployment matches the design's
    "开关默认关闭":

    ``{"external_connections": {"readiness": {"mcp": {"test": true}}}}``
    """
    spec_for(kind)
    open_now = set(BASELINE_CLASSES)
    configured = _readiness_config().get(kind) or {}
    if isinstance(configured, Mapping):
        allowed = OPENABLE_CLASSES.get(kind, frozenset())
        for name, value in configured.items():
            name = str(name)
            if name in allowed and value is True:
                open_now.add(name)
    return frozenset(open_now)


def unavailable_reason(kind: str, capability: str) -> str:
    """Why ``capability`` is closed for ``kind``, in a renderable form."""
    if capability in BASELINE_CLASSES:
        return ""
    if capability not in OPENABLE_CLASSES.get(kind, frozenset()):
        return "not_supported_by_type"
    if capability in WITHHELD.get(kind, frozenset()):
        return "withheld_pending_acceptance"
    return READINESS_REASON.get(kind, "not_configured")


def required_slots(kind: str, config: Mapping[str, Any]) -> FrozenSet[str]:
    """Slots a *saved* connection of this kind needs to be usable.

    MCP needs a secret only for the auth mode it selected; email needs the
    password of each protocol it enabled; ERP/OA always need their account
    password. A missing slot is reported, not a save refusal — the console
    saves and tests separately.
    """
    kind = str(kind or "")
    if kind == KIND_MCP:
        auth = str((config or {}).get("auth") or "none")
        return frozenset({"header"}) if auth == "header" else frozenset()
    if kind == KIND_EMAIL:
        needed = set()
        imap = (config or {}).get("imap") or {}
        smtp = (config or {}).get("smtp") or {}
        if imap.get("enabled"):
            needed.add("imap_password")
        if smtp.get("enabled"):
            needed.add("smtp_password")
        return frozenset(needed)
    return REQUIRED_SLOTS.get(kind, frozenset())


def capability_projection(kind: str, *, config: Mapping[str, Any],
                          has_secret: Mapping[str, bool],
                          adapter_present: Optional[bool] = None,
                          adapter_report: Optional[Mapping[str, Any]] = None
                          ) -> Dict[str, Any]:
    """What a connection of this kind can do right now, and why not.

    Two independent facts are merged, and they are kept distinguishable:

    * **deployment readiness** — whether this deployment opened the class
      (:func:`open_classes`). Every kind ships ``configure``; ``test`` and the
      execution classes are opt-in per deployment.
    * **this configuration** — whether the adapter exists in the build and
      whether the connection's required secret slots are filled.

    A class is reported available only when both hold, so a console renders
    the real state instead of a button that always fails. ``unavailable_reason``
    names the *first* reason in that order, because "the deployment has not
    opened MCP testing" and "this connection is missing its password" call for
    different actions from the reader.
    """
    spec = spec_for(kind)
    opened = open_classes(kind)
    missing = sorted(slot for slot in required_slots(kind, config or {})
                     if not has_secret.get(slot))
    adapter_ok = adapter_present
    if adapter_ok is None:
        adapter_ok = adapter_available(kind)
    report = dict(adapter_report or {})

    def _class_state(capability: str) -> Tuple[bool, str]:
        # ``configure`` is served by the registry itself: its validators are
        # what produce a saveable connection, and they do not need an adapter.
        # Reporting it unavailable because a runtime adapter is absent would
        # contradict the control plane that is already working.
        if capability == "configure":
            return True, ""
        if capability not in opened:
            return False, unavailable_reason(kind, capability)
        if not adapter_ok:
            return False, "adapter_not_installed"
        if missing:
            return False, "secret_missing"
        return True, ""

    states = {name: _class_state(name) for name in
              ("configure", "test", "read_execute", "write_execute")}
    executions = [name for name in ("read_execute", "write_execute")
                  if name in opened]
    primary_reason = ""
    for name in ("test", "read_execute", "write_execute"):
        if not states[name][0]:
            primary_reason = states[name][1]
            break
    return {
        "kind": kind,
        "scopes": sorted(spec.scopes),
        "open": sorted(capability for capability in states if states[capability][0]),
        "classes": {name: {"available": state[0], "reason": state[1]}
                    for name, state in states.items()},
        "test_available": states["test"][0],
        "execute_available": any(states[name][0] for name in executions),
        "read_execute_available": states["read_execute"][0],
        "write_execute_available": states["write_execute"][0],
        "unavailable_reason": primary_reason,
        "adapter_present": bool(adapter_ok),
        "missing_secret_slots": missing,
        "adapter": report,
    }
