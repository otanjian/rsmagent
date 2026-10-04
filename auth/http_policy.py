# encoding:utf-8
"""HTTP method policy + shared app factory for the web console.

The processor is installed on the real ``build_web_app()`` application so the
running web channel and the test harness share the same gate. It classifies each
route (path pattern + HTTP method) into an identity domain:

* ``public``  — anonymous allowed (login, health, static assets, oauth callback).
* ``personal`` — any authenticated session; the handler decides owner-scoping.
* ``platform`` — requires the resolved context to be a valid platform admin.
* ``tenant``  — requires an authenticated session, explicit tenant selection and
  the listed business permission (or tenant_admin qualification).
* ``closed``  — deferred/not-yet-adapted consumer; returns 503 in database mode
  and never reaches a downstream handler, regardless of admin status.

Rules:

* A URL that matches the route table but is not listed for the requested HTTP
  method is rejected (registration completeness), so a handler cannot silently
  be reached through an unguarded method (e.g. a GET on a write-only endpoint).
* Unknown URLs stay 404 (web.py notfound is preserved).
* In legacy identity mode the shared-password path continues to work; the
  database-domain gateway only enforces the tenant/platform/personal/closed
  semantics when ``identity_mode == database``.

The policy is a *route-completeness* gate, not a replacement for the handler's
own session/tenant/owner checks: each handler still resolves its context and
enforces resource ownership, permission and field whitelisting.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional, Tuple

import web

from channel.web.route_registry import derive_route_policy

logger = logging.getLogger("http_policy")

#: Route method -> policy entry, DERIVED from the single authoritative
#: route registry (``channel.web.route_registry``). Do not add entries here:
#: register a ``RouteEntry`` in the registry so this table and the
#: ``web.application`` URL table (``channel.web.web_channel._WEB_URLS``)
#: cannot drift apart. The registry also carries the three-leg coverage
#: invariant that cross-checks the registered method set against the handler
#: implementations.
ROUTE_POLICY: Dict[str, Dict[str, dict]] = derive_route_policy()


def _is_database_mode() -> bool:
    """Database is the only identity mode after retire-legacy-identity-mode."""
    return True


_REASON_PHRASES = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


def _json_error(message: str, status: int, code: str) -> str:
    body = json.dumps({"status": "error", "message": message, "code": code},
                      ensure_ascii=False)
    reason = _REASON_PHRASES.get(status, "")
    raise web.HTTPError(f"{status} {reason}".strip(),
                        {"Content-Type": "application/json; charset=utf-8"}, body)


def _match_policy(path: str, method: str) -> Tuple[Optional[dict], bool]:
    """Return ``(policy_entry, matched)`` for a request path+method.

    ``matched`` is True when the path matches any route in the table (regardless
    of method), so an unknown URL can keep returning 404 while a known URL with
    an unregistered method is rejected as not-allowed.
    """
    method = method.upper()
    for pattern, methods in ROUTE_POLICY.items():
        # exact string match first (fast path), then regex for parameterised URLs
        url_pattern = rf"^{pattern}\Z"
        import re
        if re.match(url_pattern, path):
            entry = methods.get(method)
            if entry is None:
                return None, True
            return entry, True
    return None, False


def _normalize_gate_error(exc: web.HTTPError) -> str:
    """Re-emit a gate resolution error through :func:`_json_error`.

    ``auth_handlers._require_context`` raises ``web.HTTPError(str(status), ...)``
    with the machine-readable ``code`` in the JSON body but no reason phrase.
    Normalising here keeps every gate rejection in one shape
    (``{"status","message","code"}`` with a proper ``"400 Bad Request"`` status)
    without duplicating the resolution rules.
    """
    try:
        status = int(str(exc.args[0]).split()[0])
    except Exception:
        raise exc
    code = ""
    message = ""
    payload = getattr(exc, "data", "")
    if payload:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", "replace")
        try:
            parsed = json.loads(payload)
            code = str(parsed.get("code", "") or "")
            message = str(parsed.get("message", "") or "")
        except Exception:
            raise exc
    return _json_error(message or "request rejected", status, code or "rejected")


def _enforce_context_gate(handler, policy: str, entry: dict):
    """Resolve + authorize the request context, then run the handler.

    Mandate (design D2/D3, task 3.5): context existence, identity domain and the
    route's declared permission. Object-level ownership/resource checks stay in
    the handler -- the context they need is guaranteed to exist here.
    """
    from auth.runtime import IdentityContextError, gate_context_scope

    require_tenant = policy == "tenant" and not entry.get("tenant_from_resource")
    try:
        from channel.web.auth_handlers import _require_context
        ctx = _require_context(require_tenant=require_tenant)
    except web.HTTPError as exc:
        return _normalize_gate_error(exc)
    except IdentityContextError as exc:
        return _json_error(str(exc), exc.status, exc.code)
    except Exception as exc:
        # The obsolete observation-mode flag cannot grant access on failure.
        # Exception text may contain credentials or database paths.
        logger.warning("[http-gate] identity resolution failed closed (%s)", type(exc).__name__)
        return _json_error("identity resolution failed", 503, "identity_unavailable")

    if policy == "platform" and not ctx.is_platform_admin:
        _record_gate_denial("platform-domain", web.ctx.path, ctx)
        return _json_error("forbidden", 403, "forbidden")

    permission = entry.get("permission")
    if permission and not ctx.is_platform_admin:
        if not ctx.tenant_id or permission not in ctx.permissions:
            _record_gate_denial("permission:%s" % permission, web.ctx.path, ctx)
            return _json_error("forbidden", 403, "forbidden")

    with gate_context_scope(ctx, require_tenant):
        return handler()


def _record_gate_denial(kind: str, path: str, ctx) -> None:
    """Best-effort observability for a *deterministic* authorization denial."""
    try:
        from common.security_events import record_denial
        record_denial("http-gate", reason=kind, action="http.access.denied",
                      target=path, user_id=getattr(ctx, "user_id", None),
                      tenant_id=getattr(ctx, "tenant_id", None))
    except Exception:  # pragma: no cover - telemetry must never change the decision
        pass


def enforce_http_policy(handler):
    """web.py processor enforcing the route/method policy.

    Installed by ``build_web_app`` so the production server and the test harness
    share the same gate. It runs before the matched handler, so a rejected method
    or a closed/deferred consumer never reaches downstream logic. ``handler`` is
    the rest-of-the-chain continuation; this function returns its result, or
    raises an HTTPError for rejected routes.

    In database identity mode a ``tenant``/``platform`` route is additionally
    gated *here*: the session token and tenant selection are resolved into a
    ``RequestContext``, the identity domain and declared permission are checked,
    and the context is published for the handler (see :func:`_enforce_context_gate`).
    ``public``/``personal``/``closed`` routes are not resolved, and legacy
    identity mode remains a pass-through.

    NOTE (web.py contract): a processor is ``p(handler)`` and the *return value
    of the processor call* is the response. We therefore return ``handler()``
    (or an HTTPError) directly, never a closure.
    """
    path = web.ctx.path
    method = getattr(web.ctx, "method", None) or web.ctx.env.get("REQUEST_METHOD", "GET")
    entry, matched = _match_policy(path, method)
    if not matched:
        # genuine unknown URL -> 404 via web.py notfound
        return handler()
    if entry is None:
        # A real route exists but the requested method is not registered.
        # Reject before reaching the handler (completeness gate).
        return _json_error("method not allowed", 405, "method_not_allowed")
    policy = entry.get("policy", "closed")
    if policy == "closed" and _is_database_mode():
        # A deferred/not-yet-adapted consumer is closed in database mode and
        # must never reach a downstream handler, regardless of admin status.
        return _json_error("unavailable in database identity mode", 503,
                           "database_unavailable")
    if policy in ("tenant", "platform") and _is_database_mode():
        return _enforce_context_gate(handler, policy, entry)
    return handler()
