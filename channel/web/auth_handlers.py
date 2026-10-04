# encoding:utf-8
"""Web-channel HTTP handlers for the database identity & management UI.

These handlers are active only when ``identity_mode == "database"``. They extend
the legacy ``/auth/*`` endpoints with per-account, database-backed sessions and
add the four-admin-view API surface (tenants / members / roles / departments)
plus the restricted identity audit query.

Handlers share a small base that resolves the session token (cookie / bearer)
into an authorized ``RequestContext`` using the ``X-Tenant-ID`` header. All
authorization is enforced server-side here; the frontend only improves UX.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Dict, List, Optional

import web

from auth.service import IdentityService, IdentityServiceError
from auth.store import IdentityStoreError
from auth.runtime import (
    RequestContext,
    resolve_context,
    revalidate_context,
    IdentityContextError,
)
from auth.policy import PERMISSION_CATALOG, BUILTIN_ROLES
from auth.ratelimit import shared_login_limiter, RateLimitDecision

# --- login protection (task 2.7) ------------------------------------------
def reset_login_rate_limiter() -> None:
    """Reset the shared login rate limiter (used by tests to isolate state)."""
    shared_login_limiter.reset()


def _normalize_account(username: str) -> str:
    """Normalize the login account for rate-limiting (case/whitespace)."""
    return " ".join((username or "").split()).lower()


def _client_source() -> str:
    """Trusted request source for rate limiting.

    The direct connection address is the default source. A forwarded address is
    honored only when the immediate peer is a configured trusted proxy.
    """
    peer = web.ctx.env.get("REMOTE_ADDR", "") or getattr(web.ctx, "ip", "") or ""
    if not peer:
        peer = "unknown"
    trusted = config_trusted_proxies()
    if peer in trusted:
        xff = web.ctx.env.get("HTTP_X_FORWARDED_FOR", "")
        if xff:
            return xff.split(",")[0].strip()
    return peer


def config_trusted_proxies() -> set:
    try:
        from config import conf
        value = conf().get("trusted_proxies", []) or []
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        return {str(v).strip() for v in value}
    except Exception:
        return set()


def _login_denied_audit(account: str, source: str, decision: RateLimitDecision) -> None:
    """Record ONE aggregated denied-audit event per blocked key/window.

    Uses the shared limiter to deduplicate so a flood of failures cannot turn
    into an unbounded identity-db write, while still attributing the block.
    """
    if not shared_login_limiter.should_audit_denial(account, source):
        return
    try:
        svc = _get_service()
        svc.audit_denied_login(account, source, decision.category,
                               decision.retry_after)
    except Exception:
        # Audit failure must not change the rejection outcome.
        pass


def _origin_ok() -> bool:
    """Same-origin check for a cookie-authorized write (mirrors todo/branding).

    When the request carries an Origin or Referer header it must match the
    request Host. A missing header (same-origin browser navigation, some
    non-browser clients) is allowed, exactly as the todo/branding writes do.
    """
    origin = web.ctx.env.get("HTTP_ORIGIN", "") or web.ctx.env.get("HTTP_REFERER", "") or ""
    if not origin:
        return True
    from urllib.parse import urlparse
    try:
        parsed = urlparse(origin)
        origin_value = "%s://%s" % (parsed.scheme.lower(), parsed.netloc)
    except Exception:
        return False
    return origin_value == _request_origin_exact()


def _select_credential() -> "CredentialSelection":
    """Select the session credential using the unified cookie/bearer rules.

    Returns the ``CredentialSelection`` regardless of validity; callers inspect
    ``.mixed`` (hard 400) and ``.token`` to decide authentication. A malformed
    bearer (e.g. a bare string or inner whitespace) is treated as absent, never
    as a valid exemption. ``Source`` is "cookie"/"bearer"/"".
    """
    from auth.credential import select_credential, bearer_token_from_header
    cookie = web.cookies().get("cow_session", "")
    bearer = bearer_token_from_header(web.ctx.env.get("HTTP_AUTHORIZATION", "") or "")
    return select_credential(cookie, bearer)


def verify_credential_authenticates(token: str) -> bool:
    """True when ``token`` resolves to a valid database session (no side effects).

    Used by the CSRF decision to distinguish a *successful* bearer authentication
    (which may exempt the cookie-origin rule) from a bare/invalid bearer.
    """
    try:
        return bool(_get_service().verify_session(token))
    except Exception:
        return False


def _is_bearer_auth() -> bool:
    """True only when the request *selected* a bearer credential (not a cookie).

    This is a presentation check, not an authentication check. The CSRF decision
    must additionally confirm the bearer actually authenticates (see
    ``_csrf_ok``) so a bare-token header or an invalid bearer never exempts the
    cookie-origin rule.
    """
    sel = _select_credential()
    return sel.source == "bearer" and bool(sel.token)


def _origin_for_cookie_write_ok() -> bool:
    """Strict origin check for a cookie-authenticated WRITE.

    Unlike ``_origin_ok`` (which tolerates a missing Origin/Referer for GET,
    login and legacy-compat paths), a cookie-authenticated write must produce a
    real, matching origin. The auth console endpoints have no CSRF-token flow
    (the design explicitly declines to build one), so a missing source with no
    independent CSRF proof is rejected rather than silently allowed.
    """
    origin = web.ctx.env.get("HTTP_ORIGIN", "") or web.ctx.env.get("HTTP_REFERER", "") or ""
    if not origin:
        return False
    return _origin_ok()


def _csrf_ok() -> bool:
    """A write is CSRF-safe when it is cookie-origin-safe or a valid bearer.

    A cookie-authenticated request must satisfy the origin rule; a missing source
    without an independent CSRF proof is rejected. A request that is NOT
    cookie-authenticated may be exempt when it is actually authenticated via a
    valid Bearer token; a bare/invalid bearer or a repeated same-value credential
    (which is treated as a cookie) does not exempt the cookie check.
    """
    sel = _select_credential()
    if sel.source == "cookie":
        return _origin_for_cookie_write_ok()
    if sel.source == "bearer":
        try:
            return verify_credential_authenticates(sel.token)
        except Exception:
            return False
    # No cookie and no valid bearer: for state-changing routes that reach here
    # without any credential (e.g. login establishing a new session), fall back
    # to the origin rule — a brand-new login is itself origin-checked and has no
    # prior cookie session to protect.
    return _origin_ok()


def require_management_write() -> None:
    """Unified origin/CSRF gate for state-changing management requests.

    Every platform-console, tenant-console and scene write funnels through this
    one helper -- ``channel/web/admin_handlers.py``, the console write branches
    and ``scenes/api*.py`` -- instead of each surface re-deriving the rule (or,
    as ``channel/web/todo_handlers.py`` does, carrying its own legacy copy). The
    rule: a cookie-authenticated write must present an Origin/Referer matching the
    request host; a bearer-authenticated write (the desktop client, whose
    ``file://`` origin can never match) is exempt only when the bearer really
    authenticates.
    """
    if not _csrf_ok():
        raise web.HTTPError(
            "403 Forbidden",
            {"Content-Type": "application/json; charset=utf-8"},
            json.dumps({"status": "error", "message": "invalid request origin",
                        "code": "csrf_failed"}))


def _identity_unavailable() -> str:
    """Stable 503 payload when the identity store itself is unusable."""
    return _error("identity store unavailable", 503, "identity_db_unavailable")


def _get_service() -> IdentityService:
    from config import conf, get_data_root
    import os
    configured = conf().get("identity_db_path")
    db_path = configured or os.path.join(get_data_root(), "identity.db")
    from auth.service import IdentityService as _S
    return _S(db_path)


def _json(data: Any) -> str:
    web.header("Content-Type", "application/json; charset=utf-8")
    web.header("Cache-Control", "no-store")
    return json.dumps(data, ensure_ascii=False)


def _error(message: str, status: int = 400, code: str = "error") -> str:
    # Raise HTTPError so the HTTP status is actually delivered (web.status =
    # <int> is not reflected by the test/in-process transaction path). Callers
    # do ``return _error(...)``; the raise short-circuits the return.
    body = json.dumps({"status": "error", "message": message, "code": code},
                      ensure_ascii=False)
    raise web.HTTPError(str(status), {"Content-Type": "application/json; charset=utf-8"}, body)


def _rate_limited(decision: RateLimitDecision, account: str, source: str) -> str:
    """Return a 429 with a Retry-After header and an aggregated denial audit."""
    retry = decision.retry_after or 60
    _login_denied_audit(account, source, decision)
    body = json.dumps({
        "status": "error",
        "message": "Too many login attempts, please retry later",
        "code": "rate_limited",
        "retry_after": retry,
    }, ensure_ascii=False)
    raise web.HTTPError("429", {
        "Content-Type": "application/json; charset=utf-8",
        "Retry-After": str(retry),
    }, body)


def _service_error(e: Exception) -> str:
    if isinstance(e, IdentityServiceError):
        return _error(e.args[0], e.status, e.code)
    if isinstance(e, IdentityContextError):
        return _error(e.args[0], e.status, e.code)
    return _error("internal error", 500, "internal")


# --- token / context resolution -------------------------------------------

def _session_token() -> str:
    """Read the selected session token using the unified credential rules.

    In database mode only the ``cow_session`` cookie and a well-formed
    ``Authorization: Bearer`` header are trusted. Cookie+bearer with DIFFERENT
    values raise a 400 ``mixed_credentials`` (never falling back to either);
    cookie+bearer with the SAME value authenticate as the cookie. A malformed
    bearer is treated as absent. Old shared-password / URL query tokens never
    reach this layer.
    """
    sel = _select_credential()
    if sel.mixed:
        raise web.HTTPError(
            "400 Bad Request", {"Content-Type": "application/json"},
            _error("mixed credentials", 400, "mixed_credentials"))
    return sel.token


def verified_auth_session_id() -> str:
    """The **verified** login-session row id behind this request, or ``""``.

    The one-time grant a completed scan mints is bound to the login session that
    started the scan (``auth.scan_authorization``), so the write that redeems it
    has to name the same session. The bearer token never is that name: it is a
    credential, while the row id is an opaque, non-secret identifier — the same
    value a chat-delegation snapshot already carries.

    Non-raising by design, unlike ``WeixinQrHandler._auth_session_id``: the
    presence of a session is enforced by ``_require_context`` on every caller,
    and a handler that could not read one must fall back to "unbound" (which
    fails closed for a session-bound grant) rather than turn an unrelated write
    into a 401. The row is a ``sqlite3.Row``, so it is indexed — ``.get`` does
    not exist on it.
    """
    try:
        verified = _get_service().verify_session(_session_token())
    except Exception:  # noqa: BLE001 - no session, or an unreadable store
        return ""
    row = (verified or {}).get("session") if isinstance(verified, dict) else None
    try:
        return str(row["id"] or "") if row is not None else ""
    except Exception:  # noqa: BLE001 - an unreadable session is not a session
        return ""


def _tenant_header() -> str:
    return web.ctx.env.get("HTTP_X_TENANT_ID", "") or ""


def _resolve_ctx_svc(svc: IdentityService, require_tenant: bool) -> RequestContext:
    """Resolve the authoritative per-request context, raising the right error.

    Personal and platform endpoints ignore any residual ``X-Tenant-ID``; tenant
    endpoints require an explicit, valid selection. A failed session is a 401,
    a missing tenant is a 400, a missing membership/permission is a 403, and an
    unavailable identity store is a 503.

    When the HTTP policy gate already resolved this identity domain for the
    request, that context is reused: it is the same resolution, so re-reading the
    identity store would only add a round trip and a chance for the gate and the
    handler to disagree.
    """
    from auth.runtime import cached_gate_context

    cached = cached_gate_context(require_tenant)
    if cached is not None:
        return cached
    if require_tenant:
        tenant = _tenant_header()
        if not tenant:
            raise IdentityContextError("tenant selection required", code="missing_tenant", status=400)
    else:
        # personal / platform domain: ignore any residual X-Tenant-ID. The
        # request is authorized purely by its own identity; a stale tenant
        # selection must neither grant nor block platform/personal access.
        tenant = None
    token = _session_token()
    if not token:
        raise IdentityContextError("unauthorized", code="unauthorized", status=401)
    ctx = resolve_context(svc, token, tenant)
    # A forced-password-change (restricted) account may only reach the minimal
    # self-info / change-password / logout handlers — which call the service
    # directly and never go through ``_require_context``. Every other personal,
    # platform and tenant management/business endpoint is rejected here: a
    # restricted platform admin cannot use admin qualification to bypass it.
    if ctx.must_change_password:
        raise IdentityContextError(
            "password change required",
            code="password_change_required", status=403)
    return ctx


def _require_context(require_tenant: bool = False) -> RequestContext:
    """Resolve the current request context, or raise an HTTP error."""
    svc = _get_service()
    try:
        return _resolve_ctx_svc(svc, require_tenant)
    except IdentityContextError as e:
        raise web.HTTPError(str(e.status), {"Content-Type": "application/json"},
                            _error(e.args[0], e.status, e.code))


# --- legacy-compatible auth handlers (database mode) ----------------------

class DbAuthCheckHandler:
    def GET(self):
        svc = _get_service()
        token = _session_token()
        ctx = None
        if token:
            try:
                ctx = resolve_context(svc, token, _tenant_header() or None)
            except IdentityContextError:
                ctx = None
        if ctx is None:
            return _json({"status": "success", "auth_required": True, "authenticated": False,
                          "identity_mode": "database"})
        return _json({
            "status": "success", "auth_required": True, "authenticated": True,
            "identity_mode": "database",
            "must_change_password": bool(ctx.must_change_password),
            "user": {"username": ctx.username, "display_name": ctx.display_name,
                     "is_platform_admin": ctx.is_platform_admin},
        })


class DbAuthLoginHandler:
    def POST(self):
        # A login creates a cookie session (a state change) -> require same-origin
        if not _csrf_ok():
            return _error("cross-origin request rejected", 403, "cross_origin")
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        username = str(data.get("username", "") or "")
        password = str(data.get("password", "") or "")
        account = _normalize_account(username)
        source = _client_source()
        # Bounded, app-lifetime login rate limit (429) before touching the store.
        decision = shared_login_limiter.check(account, source)
        if not decision.allowed:
            return _rate_limited(decision, account, source)
        try:
            svc = _get_service()
            result = svc.login(username, password)
        except IdentityServiceError as e:
            if e.status == 401:
                # A failed credential counts against both the account and source.
                shared_login_limiter.record_failure(account, source)
                return _error("Invalid username or password", 401, "invalid_login")
            return _service_error(e)
        except (IdentityStoreError, Exception):
            # identity store unusable -> 503, never fall back to legacy auth
            return _identity_unavailable()
        web.setcookie("cow_session", result.token, expires=7 * 86400, path="/",
                      httponly=True, samesite="Lax",
                      secure=_request_origin_exact().startswith("https://"))
        # The compatibility ``token`` field stays in the payload but is ALWAYS
        # empty (design D8). Handing a reusable Bearer token out of the Web login
        # response made one readable response an authorization grant for a native
        # session, and it forced the desktop shell to keep the session in the
        # renderer. Native clients use the browser authorization-code + PKCE
        # exchange instead: /auth/desktop/authorize mints a 60-second, single-use
        # code, and /auth/desktop/token trades it for their own AuthSession.
        # A client that still needs a token here must upgrade rather than be
        # handed one (the old path is refused, not silently satisfied).
        return _json({
            "status": "success", "token": "", "identity_mode": "database",
            "must_change_password": result.must_change_password,
            "user": {"username": result.username, "display_name": result.display_name,
                     "is_platform_admin": result.is_platform_admin},
            "tenants": result.tenants,
        })


class DbAuthLogoutHandler:
    def POST(self):
        if not _csrf_ok():
            return _error("cross-origin request rejected", 403, "cross_origin")
        token = _session_token()
        try:
            svc = _get_service()
            # Resolve the subject *before* revoking: afterwards the session is
            # gone and the trail would have to record an anonymous logout.
            principal = svc.verify_session(token) if token else None
            if token:
                svc.revoke_session(token)
            if principal:
                svc.record_audit(
                    actor_user_id=principal["user"]["id"],
                    actor_username=principal["user"]["username"],
                    tenant_id=None,
                    action="auth.logout",
                    target=f"user:{principal['user']['id']}",
                    redacted_changes={},
                )
        except (IdentityStoreError, Exception):
            return _identity_unavailable()
        web.setcookie("cow_session", "", expires=-1, path="/")
        return _json({"status": "success"})

class DbAuthMeHandler:
    """GET /auth/me — single read-only self-account projection (database mode).

    Always resolves the subject from the current session; the client cannot
    select another user or member and ``X-Tenant-ID`` is ignored for the global
    identity read. A restricted (must_change_password) account receives only the
    minimal ``user`` projection and an empty ``tenants`` list.
    """

    def GET(self):
        svc = _get_service()
        token = _session_token()
        if not token:
            return _error("unauthorized", 401, "unauthorized")
        try:
            ctx = svc.self_context(token)
        except IdentityServiceError as e:
            # 401 unauthorized, 403 account_disabled, 503 service failure
            return _service_error(e)
        except (IdentityStoreError, Exception):
            return _identity_unavailable()
        return _json(ctx)


class DbAuthContextHandler:
    """GET /auth/context — current-tenant effective capability summary.

    Returns only the requesting user's effective_permissions, admin
    qualification and the static consumer availability for the explicitly
    selected ``X-Tenant-ID``. Zero-permission effective members get an empty
    permission set; it does not require ``tenant.info.read``, never returns
    tenant profile or other members, and is not itself an authorization grant.
    """

    def GET(self):
        svc = _get_service()
        token = _session_token()
        if not token:
            return _error("unauthorized", 401, "unauthorized")
        tenant = _tenant_header()
        if not tenant:
            return _error("tenant selection required", 400, "missing_tenant")
        try:
            ctx = svc.context_for_tenant(token, tenant)
        except IdentityServiceError as e:
            return _service_error(e)
        except (IdentityStoreError, Exception):
            return _identity_unavailable()
        return _json(ctx)


class DbAuthPasswordHandler:
    def POST(self):
        # Password change is a cookie state change -> same-origin / bearer-safe
        if not _csrf_ok():
            return _error("cross-origin request rejected", 403, "cross_origin")
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        token = _session_token()
        old_password = str(data.get("old_password", "") or "")
        new_password = str(data.get("new_password", "") or "")
        try:
            svc = _get_service()
            svc.change_password(token, old_password, new_password)
        except IdentityServiceError as e:
            # invalid_old stays on the form (401), weak_password correct in place
            # (400), unauthorized returns 401. All are real statuses.
            return _service_error(e)
        except (IdentityStoreError, Exception):
            return _identity_unavailable()
        # after change, the old session is revoked; clear the cookie so the
        # client must re-login, and signal re-login explicitly.
        web.setcookie("cow_session", "", expires=-1, path="/")
        return _json({"status": "success", "must_relogin": True})


# --- self profile edit (PATCH /auth/profile) + avatar ---------------------
# The account avatar reuses the on-disk ``avatars`` store but is keyed with a
# ``user-`` prefix so it can never collide with the agent avatar store. The
# ``users.avatar`` column is only a metadata flag; the bytes live at
# ``shared_root()/avatars/user-<user_id><ext>``.

_USER_AVATAR_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_MAX_USER_AVATAR_BYTES = 2 * 1024 * 1024


def _user_avatar_path(user_id: str) -> Optional[str]:
    from common.state_dir import shared_root

    base = shared_root() / "avatars"
    for suffix in _USER_AVATAR_TYPES:
        candidate = base / f"user-{user_id}{suffix}"
        if candidate.is_file():
            return str(candidate)
    return None


def _write_user_avatar(user_id: str, raw: bytes, suffix: str) -> str:
    from common.state_dir import shared_root

    base = shared_root() / "avatars"
    base.mkdir(parents=True, exist_ok=True)
    # Drop any other extension first, so one user never ends up with two avatar
    # files and a resolution order deciding which one wins.
    for other in _USER_AVATAR_TYPES:
        stale = base / f"user-{user_id}{other}"
        if other != suffix and stale.is_file():
            try:
                stale.unlink()
            except OSError:
                pass
    target = base / f"user-{user_id}{suffix}"
    tmp = base / f".user-{user_id}{suffix}.tmp"
    with open(tmp, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    return str(target)


def _read_uploaded_file_bytes(file_obj) -> bytes:
    """Return uploaded content as bytes across web.py upload object variants."""
    if isinstance(file_obj, bytes):
        return file_obj
    if isinstance(file_obj, str):
        return file_obj.encode("utf-8")

    content = None
    if hasattr(file_obj, "file") and hasattr(file_obj.file, "read"):
        content = file_obj.file.read()
    elif hasattr(file_obj, "read"):
        content = file_obj.read()
    elif hasattr(file_obj, "value"):
        content = file_obj.value
    if content is None:
        raise ValueError("Unable to read uploaded file content")
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode("utf-8")
    raise TypeError(f"Unsupported uploaded content type: {type(content).__name__}")


def _raw_web_input():
    """Return unprocessed multipart form data when web.py exposes rawinput."""
    rawinput = getattr(getattr(web, "webapi", None), "rawinput", None)
    if not callable(rawinput):
        raise RuntimeError("web.py rawinput is not available")
    try:
        return rawinput(method="post")
    except TypeError:
        return rawinput()


class DbSelfProfileHandler:
    """PATCH /auth/profile — self-service profile edit (database mode).

    Only the caller's own global display_name and (optionally) the selected
    tenant's member display_name / position are writable. Roles, department,
    tenant membership, username and platform-admin flag are never writable here,
    so a member cannot self-raise privileges. Restricted (must_change_password)
    accounts are rejected.
    """

    def PATCH(self):
        if not _csrf_ok():
            return _error("cross-origin request rejected", 403, "cross_origin")
        try:
            data = json.loads(web.data())
        except Exception:
            return _error("Invalid request", 400, "invalid_request")
        token = _session_token()
        if not token:
            return _error("unauthorized", 401, "unauthorized")
        tenant_id = _tenant_header() or None
        try:
            svc = _get_service()
            ctx = svc.update_self_profile(
                token,
                display_name=data.get("display_name"),
                member_display_name=data.get("member_display_name"),
                position_text=data.get("position_text"),
                tenant_id=tenant_id,
            )
        except IdentityServiceError as e:
            return _service_error(e)
        except (IdentityStoreError, Exception):
            return _identity_unavailable()
        return _json(ctx)


class DbSelfAvatarHandler:
    """GET /auth/profile/avatar & POST /auth/profile/avatar — self avatar.

    GET returns the caller's own avatar bytes (or JSON 404 when none). POST
    accepts a multipart ``avatar`` file, validates size/type, writes it to disk
    under the ``user-``-prefixed avatar store and flags ``users.avatar``.
    """

    def GET(self):
        token = _session_token()
        if not token:
            return _error("unauthorized", 401, "unauthorized")
        try:
            svc = _get_service()
            ctx = svc.self_context(token)
        except IdentityServiceError as e:
            return _service_error(e)
        except (IdentityStoreError, Exception):
            return _identity_unavailable()
        user = (ctx or {}).get("user") or {}
        user_id = user.get("id")
        if not user_id or not user.get("avatar"):
            web.ctx.status = "404 Not Found"
            web.header("Content-Type", "application/json; charset=utf-8")
            return json.dumps({"status": "error", "message": "no avatar"})
        path = _user_avatar_path(user_id)
        if not path:
            web.ctx.status = "404 Not Found"
            web.header("Content-Type", "application/json; charset=utf-8")
            return json.dumps({"status": "error", "message": "no avatar"})
        with open(path, "rb") as handle:
            data = handle.read()
        web.header("Content-Type", _USER_AVATAR_TYPES[os.path.splitext(path)[1].lower()])
        web.header("Cache-Control", "private, max-age=86400")
        return data

    def POST(self):
        # Avatar upload is a cookie state change -> same-origin / bearer-safe
        if not _csrf_ok():
            return _error("cross-origin request rejected", 403, "cross_origin")
        token = _session_token()
        if not token:
            return _error("unauthorized", 401, "unauthorized")
        try:
            svc = _get_service()
            ctx = svc.self_context(token)
        except IdentityServiceError as e:
            return _service_error(e)
        except (IdentityStoreError, Exception):
            return _identity_unavailable()
        user = (ctx or {}).get("user") or {}
        user_id = user.get("id")
        if not user_id:
            return _error("unauthorized", 401, "unauthorized")
        try:
            params = _raw_web_input()
        except Exception:
            return _error("invalid multipart body", 400, "invalid_request")
        upload = params.get("avatar")
        if upload is None:
            return _error("avatar file required", 400, "avatar_required")
        filename = getattr(upload, "filename", "") or ""
        raw = _read_uploaded_file_bytes(upload)
        if not raw:
            return _error("avatar file required", 400, "avatar_required")
        if len(raw) > _MAX_USER_AVATAR_BYTES:
            return _error("avatar exceeds 2 MiB", 400, "avatar_too_large")
        suffix = os.path.splitext(filename)[1].lower()
        if suffix not in _USER_AVATAR_TYPES:
            return _error(
                f"unsupported image type: {suffix or 'unknown'}",
                400, "unsupported_image_type",
            )
        try:
            _write_user_avatar(user_id, raw, suffix)
        except OSError:
            return _error("avatar write failed", 500, "avatar_write_failed")
        try:
            ctx = svc.set_self_avatar(token)
        except IdentityServiceError as e:
            return _service_error(e)
        except (IdentityStoreError, Exception):
            return _identity_unavailable()
        return _json(ctx)


class DbUserAvatarHandler:
    """GET /api/users/<user_id>/avatar — read an account avatar by user id.

    Serves the uploaded bytes to the account itself, a valid platform admin, or
    a caller sharing an active tenant membership with the target (the existing
    member-directory visibility). When the account has no uploaded picture it
    returns 404: the bundled default avatars are static assets the client
    references directly and are never proxied here. Read-only — it never mutates
    identity, membership, avatar or session state.
    """

    def GET(self, user_id):
        token = _session_token()
        if not token:
            return _error("unauthorized", 401, "unauthorized")
        try:
            svc = _get_service()
            actor = svc.self_context(token)
        except IdentityServiceError as e:
            return _service_error(e)
        except (IdentityStoreError, Exception):
            return _identity_unavailable()
        actor_id = ((actor or {}).get("user") or {}).get("id")
        if not actor_id:
            return _error("unauthorized", 401, "unauthorized")
        try:
            target = svc.authorize_user_avatar_read(actor_id, user_id)
        except IdentityServiceError as e:
            return _service_error(e)
        except (IdentityStoreError, Exception):
            return _identity_unavailable()
        path = _user_avatar_path(target["id"]) if target.get("avatar") else None
        if not path:
            web.ctx.status = "404 Not Found"
            web.header("Content-Type", "application/json; charset=utf-8")
            return json.dumps({"status": "error", "message": "no avatar"})
        with open(path, "rb") as handle:
            data = handle.read()
        web.header("Content-Type", _USER_AVATAR_TYPES[os.path.splitext(path)[1].lower()])
        web.header("Cache-Control", "private, max-age=86400")
        return data


# --- Desktop native authorization (task 8.2 / design D8) -------------------
#
# The Desktop shell cannot rely on a cross-origin Cookie (it renders from a
# file:// origin), so the native protocol replaces the old "login returns a
# Bearer token" path with a browser authorization-code + PKCE S256 exchange.
# The state machine and its SQLite-backed, digest-only storage live in
# :mod:`auth.desktop_auth`; these handlers only adapt HTTP to it.
#
# Neither endpoint is a session-bearing business route: ``authorize`` is a
# browser document/consent form authenticated by the ordinary Web Cookie, and
# ``token`` authenticates by the code + verifier pair alone. Both therefore
# carry the ``public`` route policy, and each handler enforces its own rules
# (CSRF/origin/session on the confirm, exact origin on the exchange).

def _desktop_service():
    """The native-flow service for the process's identity database."""
    from auth.desktop_auth import service_for

    return service_for(_get_service())


def _request_params() -> Dict[str, str]:
    """Query + body parameters as a flat ``{str: str}`` map.

    The consent form posts ``application/x-www-form-urlencoded`` from a browser,
    while the Desktop main process posts JSON to the token endpoint; both shapes
    are accepted, and the last value wins so a duplicated field cannot smuggle a
    second value past validation.
    """
    from urllib.parse import parse_qs

    out: Dict[str, str] = {}
    for key, values in parse_qs(web.ctx.env.get("QUERY_STRING", "") or "").items():
        if values:
            out[key] = values[-1]
    if (web.ctx.env.get("REQUEST_METHOD") or "GET").upper() == "GET":
        return out
    if "wsgi.input" not in web.ctx.env:
        return out
    try:
        raw = web.data()
    except KeyError:
        return out
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not raw:
        return out
    content_type = (web.ctx.env.get("CONTENT_TYPE") or "").lower()
    if "json" in content_type:
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            for key, value in parsed.items():
                out[str(key)] = "" if value is None else str(value)
        return out
    for key, values in parse_qs(raw).items():
        if values:
            out[key] = values[-1]
    return out


def _html(body: str, status: Optional[str] = None) -> str:
    """Serve a browser page with the no-cache/no-referrer policy D8 requires."""
    headers = {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
    }
    if status:
        raise web.HTTPError(status, headers, body)
    for name, value in headers.items():
        web.header(name, value)
    return body


def _desktop_error(e: Exception) -> str:
    from auth.desktop_auth import DesktopAuthError
    from auth.desktop_web_session import DesktopWebSessionError

    if isinstance(e, DesktopWebSessionError):
        return _error(e.args[0], e.status, e.code)
    if isinstance(e, DesktopAuthError):
        return _error(e.args[0], e.status, e.code)
    return _error("internal error", 500, "internal")


class DesktopAuthorizeHandler:
    """GET/POST ``/auth/desktop/authorize`` — the browser consent surface.

    GET only ever *renders*: with no Web session (or one that still has to change
    its password) it answers a notice page, otherwise it opens a one-time consent
    record and shows the backend, the account and the callback host. It never
    redirects to the callback, so a bare GET — however many Cookies the browser
    already holds — cannot mint a code. POST is the explicit confirmation: it
    requires the same origin (CSRF), a live unrestricted session, and the
    one-time ``request_id``/``csrf`` pair the page carried.
    """

    def GET(self):
        from auth.desktop_auth import (
            DesktopAuthError, render_consent_page, render_notice_page,
            render_sign_in_page,
        )

        params = _request_params()
        desktop = _desktop_service()
        try:
            desktop.precheck(
                client_id=params.get("client_id", ""),
                redirect_uri=params.get("redirect_uri", ""),
                code_challenge=params.get("code_challenge", ""),
                code_challenge_method=params.get("code_challenge_method", ""),
                state=params.get("state", ""),
            )
        except DesktopAuthError as e:
            return _desktop_error(e)

        session_token = _session_token()
        if not session_token:
            return _html(render_sign_in_page(), "401 Unauthorized")
        try:
            session = _get_service().verify_session(session_token)
        except (IdentityStoreError, Exception):
            return _identity_unavailable()
        if not session:
            return _html(render_sign_in_page(), "401 Unauthorized")
        user = session["user"]
        if user.get("must_change_password") or session["session"]["restricted"]:
            return _html(render_notice_page(
                title="请先更新登录密码",
                message="为保障账号安全，请前往 Web 控制台修改临时密码，再继续授权桌面端。",
            ), "403 Forbidden")

        try:
            opened = desktop.begin(
                session_token=session_token,
                client_id=params.get("client_id", ""),
                redirect_uri=params.get("redirect_uri", ""),
                code_challenge=params.get("code_challenge", ""),
                code_challenge_method=params.get("code_challenge_method", ""),
                state=params.get("state", ""),
            )
        except DesktopAuthError as e:
            return _desktop_error(e)
        return _html(render_consent_page(
            backend_origin=_request_origin(),
            username=user["username"],
            display_name=user.get("display_name", ""),
            redirect_uri=params.get("redirect_uri", ""),
            request_id=opened["request_id"],
            csrf=opened["csrf"],
        ))

    def POST(self):
        from auth.desktop_auth import DesktopAuthError, with_query

        # Consent pages ship ``Referrer-Policy: same-origin`` / historically
        # ``no-referrer``. A cookie write normally demands Origin/Referer via
        # ``_csrf_ok``, but a browser form POST under a tight referrer policy
        # may omit both and then surface as ``cross_origin`` even though the
        # one-time form ``csrf`` (validated below) is the CSRF proof. Reject
        # only a *mismatched* Origin/Referer; a missing source is allowed.
        if not _origin_ok():
            return _error("cross-origin request rejected", 403, "cross_origin")
        params = _request_params()
        request_id = params.get("request_id", "")
        csrf = params.get("csrf", "")
        decision = params.get("decision", "allow")
        desktop = _desktop_service()
        session_token = _session_token()
        try:
            if decision == "deny":
                info = desktop.deny(request_id=request_id, csrf=csrf,
                                    session_token=session_token)
                query = {"error": "access_denied"}
            else:
                info = desktop.confirm(request_id=request_id, csrf=csrf,
                                       session_token=session_token)
                query = {"code": info["code"]}
        except DesktopAuthError as e:
            return _desktop_error(e)
        if info.get("state"):
            query["state"] = info["state"]
        return _redirect(with_query(info["redirect_uri"], query))


class DesktopTokenHandler:
    """POST ``/auth/desktop/token`` — the exact-origin code exchange.

    Authenticated by the authorization code plus the PKCE verifier, never by a
    Cookie or a self-declared native marker. A request that carries an
    ``Origin``/``Referer`` must present the backend's own host, the response is
    never cacheable, and neither the code nor the verifier is logged.
    """

    def POST(self):
        params = _request_params()
        if not _origin_ok():
            return _error("cross-origin request rejected", 403, "cross_origin")
        try:
            result = _desktop_service().exchange(
                code=params.get("code", ""),
                verifier=params.get("code_verifier", ""),
                client_id=params.get("client_id", ""),
                redirect_uri=params.get("redirect_uri", ""),
                # Task 3.2: stamp the exact origin this exchange was served at,
                # in the same transaction that mints the native session.
                origin=_request_origin_exact(),
            )
        except Exception as e:
            return _desktop_error(e)
        return _json(result)


def _request_origin_exact() -> str:
    """The exact origin a desktop request was served at (scheme + Host).

    Used both when a native session is minted (``/auth/desktop/token``) and when
    it later bootstraps a Web child (``/auth/desktop/web-session``). Both go
    through this one function so the two values agree by construction; the
    scheme honours a trusted reverse proxy's ``X-Forwarded-Proto`` and otherwise
    reports the transport actually used, and never a client-supplied header.
    """
    env = web.ctx.env
    host = env.get("HTTP_HOST", "") or env.get("SERVER_NAME", "") or ""
    forwarded = (env.get("HTTP_X_FORWARDED_PROTO", "") or "").split(",")[0].strip().lower()
    peer = env.get("REMOTE_ADDR", "") or getattr(web.ctx, "ip", "") or ""
    if peer in config_trusted_proxies() and forwarded in ("http", "https"):
        scheme = forwarded
    else:
        scheme = "https" if (str(env.get("HTTPS", "")).lower() in ("on", "1", "true") or env.get("wsgi.url_scheme") == "https") else "http"
    return "%s://%s" % (scheme, host)


class DesktopWebSessionHandler:
    """``/auth/desktop/web-session`` — the native parent's Web child session.

    Change ``add-desktop-remote-web-workbench`` (tasks 3.3/3.4). Bearer-only: a
    Cookie session must never be able to ask for a child, and the pair can only
    be created from a native session whose origin was registered at mint time
    (``desktop_native_origins``), so an ordinary login Bearer is refused.

    The child secret leaves in a ``Set-Cookie`` and *only* there: it is popped
    out of the service result before the body is serialized, so no code path can
    accidentally include it in JSON. Every response is ``no-store``.
    """

    def _guard_enabled(self):
        from auth import capability_matrix
        # The capability is closed until its acceptance evidence exists; the
        # endpoint answers "why" instead of pretending to work.
        if not capability_matrix.slice_for("desktop_remote_web").enabled:
            return _error("desktop remote web is not available", 503,
                          "feature_unavailable")

    def _native_bearer(self) -> str:
        selection = _select_credential()
        if selection.mixed:
            return _error("conflicting credentials", 400, "invalid_request")
        if selection.source != "bearer" or not selection.token:
            # A Cookie (or nothing) is not a native session.
            return _error("a native bearer session is required", 401,
                          "auth_required")
        return selection.token

    def POST(self):
        self._guard_enabled()
        token = self._native_bearer()
        params = _request_params()
        try:
            result = _desktop_web_service().bootstrap(
                native_token=token,
                bootstrap_id=params.get("bootstrap_id", ""),
                instance_id=params.get("instance_id", ""),
                web_protocol=_as_int(params.get("web_protocol")),
                origin=_request_origin_exact(),
            )
        except Exception as e:
            return _desktop_error(e)
        secret = result.pop("web_token", "")
        # ``expires_at`` is *read* for the cookie's Max-Age, not removed: it is a
        # deadline, not a secret, and the desktop client requires it in the body
        # (``remote/web-session.ts`` refuses an answer without it). Popping it
        # here made every attach fail with "the bootstrap answer carried no
        # expiry" after the server had already minted the link.
        _set_web_session_cookie(secret, result.get("expires_at", 0))
        return _json({"status": "success", "data": result})

    def GET(self):
        self._guard_enabled()
        token = self._native_bearer()
        try:
            data = _desktop_web_service().status(native_token=token)
        except Exception as e:
            return _desktop_error(e)
        return _json({"status": "success", "data": data})


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _set_web_session_cookie(token: str, expires_at: int) -> None:
    """Deliver the Web child secret as a host-only, HttpOnly session Cookie.

    Built by hand rather than through ``web.setcookie`` so the attributes the
    contract fixes are guaranteed: no ``Domain`` (host-only), ``HttpOnly``,
    ``Secure``, ``SameSite=Lax`` and ``Path=/``. A response header is not a
    place secrets may be logged, and this function never logs.
    """
    import time as _time
    from auth.desktop_web_session import COOKIE_ATTRIBUTES, WEB_SESSION_COOKIE
    max_age = max(0, int(expires_at) - int(_time.time()))
    web.header(
        "Set-Cookie",
        "%s=%s; %s; Max-Age=%d"
        % (WEB_SESSION_COOKIE, token, COOKIE_ATTRIBUTES, max_age),
    )


def _desktop_web_service():
    from auth.desktop_web_session import service_for
    return service_for(_get_service())


def _request_origin() -> str:
    """The backend's own display origin, from the request's Host header."""
    host = web.ctx.env.get("HTTP_HOST", "") or "localhost"
    return "http://" + host


def _redirect(location: str):
    """A 302 to an already-validated loopback callback, never cacheable."""
    web.header("Cache-Control", "no-store")
    raise web.redirect(location, "302 Found")
