# encoding:utf-8
"""Desktop native authorization: browser consent + authorization code + PKCE S256.

Why this exists
---------------
The desktop client renders from a ``file://`` origin, where a cross-origin
session Cookie is not sent reliably, so the shell used to be handed a reusable
Bearer token straight out of ``POST /auth/login``. That made the Web login
response an authorization grant for a native session: any script that could read
one response owned a session, and the token then had to live in the renderer
(``localStorage``) and travel in query strings for header-less requests.

Design D8 replaces it with a standard native-app flow:

1. the main process generates a high-entropy ``code_verifier``, a ``state`` and
   a per-attempt loopback callback path, and starts listening on the literal
   ``127.0.0.1`` / ``[::1]`` address *before* it opens the system browser;
2. the browser authorizes against ``/auth/desktop/authorize`` -- after an
   ordinary Web login and a completed forced password change, and only when the
   user explicitly confirms (a bare GET, an existing Cookie, a Desktop
   User-Agent or the public ``client_id`` grant nothing);
3. the browser is redirected to the loopback callback with a short-lived code;
4. the main process exchanges the code plus the verifier at
   ``/auth/desktop/token`` for an *independent* native ``AuthSession``.

What the server guarantees
--------------------------
* The client id is the fixed public value ``cowagent-desktop``. It identifies
  the client and proves nothing: PKCE proves the caller holds the verifier, and
  neither is a client-binary attestation.
* Only registered literal loopback redirects are accepted (see
  :func:`is_registered_redirect_uri`); the exact ``redirect_uri`` is bound into
  the code and re-checked at exchange time, so a code cannot be redirected to
  another port, path or host.
* An authorization code is stored as a digest only, expires after 60 seconds and
  is consumed atomically: replay refuses, and a failed exchange (wrong verifier,
  wrong redirect, expired, unknown) never consumes it.
* The code is bound to the user, the *initiating* Web ``AuthSession``, the
  client id, the exact redirect and the PKCE challenge. Revoking the initiating
  session before the exchange refuses the exchange.
* A restricted (forced-password-change) session never mints a code, so a
  temporary credential cannot be traded for a long-lived native session.
* ``code``, ``verifier``, ``state`` and the issued token are never logged and
  never returned by the consent page or the login response.

Storage
-------
Two tables in the same ``identity.db`` (``desktop_auth_requests`` for the
pending consent, ``desktop_auth_codes`` for the issued grants). They are created
idempotently by this module rather than through a numbered migration, because
the coordinating change owns the migration sequence; the tables carry no
identity truth and are safe to recreate. ``BEGIN IMMEDIATE`` gives the
consumption/session-insert pair a single commit, so the code is single-use
across workers and the native session cannot exist without a consumed code.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import re
import secrets
import time
from typing import Any, Dict, Tuple
from urllib.parse import quote, urlsplit

from auth.session import (generate_token, hash_token, session_ttl_seconds)

#: The fixed public client id (design D8). Public by definition.
CLIENT_ID = "cowagent-desktop"

#: The browser-facing consent endpoint and the exact-origin token endpoint.
AUTHORIZE_PATH = "/auth/desktop/authorize"
TOKEN_PATH = "/auth/desktop/token"

#: Lifetime of an authorization code (design D8: 60 seconds).
CODE_TTL_SECONDS = 60

#: Lifetime of a pending consent record: enough for a Web login round trip,
#: short enough that a stale form cannot be replayed much later.
REQUEST_TTL_SECONDS = 600

#: PKCE: the only accepted transform.
PKCE_METHOD = "S256"

_CODE_CHALLENGE = re.compile(r"\A[A-Za-z0-9\-_]{43,128}\Z")
_CODE_VERIFIER = re.compile(r"\A[A-Za-z0-9\-._~]{43,128}\Z")
_STATE = re.compile(r"\A[A-Za-z0-9\-._~]{0,256}\Z")
_REDIRECT_PATH = re.compile(r"\A[A-Za-z0-9/_.\-]+\Z")

_LOOPBACK_HOSTS = ("127.0.0.1", "::1")


def _now() -> int:
    """Current unix time. Indirected so tests can drive code expiry."""
    return int(time.time())


def _digest(value: str) -> str:
    """The SHA-256 hex digest of a secret (the only form ever stored)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def s256_challenge(verifier: str) -> str:
    """RFC 7636 S256: base64url(sha256(verifier)) without padding."""
    return _b64url_no_pad(hashlib.sha256(verifier.encode("ascii")).digest())


class DesktopAuthError(RuntimeError):
    """A refused step of the native flow, with the HTTP shape to report."""

    def __init__(self, message: str, code: str = "invalid_request",
                 status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def is_registered_redirect_uri(uri: str) -> bool:
    """Whether ``uri`` is one of the registered loopback callback forms.

    Registered means: ``http``, a *literal* ``127.0.0.1`` or ``[::1]`` host with
    an explicit port, a single absolute path, no credentials, no query string,
    no fragment, no traversal and no control characters. A wildcard host, an
    arbitrary domain, ``localhost`` (which can resolve elsewhere) and any
    user-supplied https origin are refused.
    """
    if not isinstance(uri, str) or not uri or len(uri) > 512:
        return False
    if any(ord(ch) < 0x20 or ch == " " for ch in uri):
        return False
    try:
        parsed = urlsplit(uri)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "http":
        return False
    if parsed.query or parsed.fragment:
        return False
    if parsed.username or parsed.password:
        return False
    if not port:  # an explicit port is required (the main process binds it first)
        return False
    host = (parsed.hostname or "").lower()
    if host not in _LOOPBACK_HOSTS:
        return False
    # Refuse a netloc that is not the canonical literal form, e.g. a trailing
    # dot, or an empty/decorated host that urlsplit would normalise.
    bare = "127.0.0.1" if host == "127.0.0.1" else "[::1]"
    if parsed.netloc != "%s:%d" % (bare, port):
        return False
    path = parsed.path
    if len(path) < 2 or not path.startswith("/"):
        return False
    if "//" in path or ".." in path.split("/"):
        return False
    if not _REDIRECT_PATH.fullmatch(path):
        return False
    return True


def _validate_client(client_id: str) -> None:
    if client_id != CLIENT_ID:
        raise DesktopAuthError("unknown client", "invalid_client", 400)


def _validate_challenge(challenge: str, method: str) -> None:
    if method != PKCE_METHOD:
        raise DesktopAuthError("unsupported code challenge method",
                               "invalid_request", 400)
    if not _CODE_CHALLENGE.fullmatch(challenge or ""):
        raise DesktopAuthError("invalid code challenge", "invalid_request", 400)


def _validate_state(state: str) -> None:
    if not _STATE.fullmatch(state or ""):
        raise DesktopAuthError("invalid state", "invalid_request", 400)


def with_query(uri: str, params: Dict[str, str]) -> str:
    """Append a query string to an already-validated callback.

    The redirect is validated as query-free before this runs, so the result is
    always ``<uri>?<encoded>`` and no attacker-controlled fragment survives.
    """
    return uri + "?" + "&".join(
        "%s=%s" % (key, quote(str(value), safe=""))
        for key, value in params.items())


def validate_server_origin(raw: str) -> str:
    """Validate a replacement remote server; the desktop validates it again."""
    value = raw.strip()
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and parsed.path in ("", "/") and not parsed.query
                 and not parsed.fragment and parsed.port != 0
                 and not re.search(r"[\s\\]", value) and len(value) <= 2048)
    except ValueError:
        valid = False
    if not valid:
        raise DesktopAuthError("请输入 HTTPS 服务器地址，不包含路径、账号或查询参数。",
                               "invalid_server_origin", 400)
    return value.rstrip("/")


def render_consent_page(*, backend_origin: str, username: str,
                        display_name: str, redirect_uri: str,
                        request_id: str, csrf: str) -> str:
    """The explicit Desktop confirmation page (never approves on a bare GET)."""
    host = urlsplit(redirect_uri).netloc
    body = """
        <div class="auth-heading">
            <span class="eyebrow">DESKTOP ACCESS</span>
            <h1 id="auth-title">连接你的桌面工作空间</h1>
            <p>确认以下账号，让容大 AI 在桌面端与你继续协作。</p>
        </div>
        <div class="account-card">
            <div class="account-avatar" aria-hidden="true">{initial}</div>
            <div class="account-identity"><strong>{account}</strong><span>@{user}</span></div>
            <span class="account-status"><i aria-hidden="true"></i>已登录</span>
        </div>
        <div class="account-actions">
            <button id="desktop-switch-account" class="account-switch" type="button">换个账号登录</button>
        </div>
        <p id="desktop-switch-error" class="form-error" role="alert" hidden></p>
        <dl class="connection-details">
            <div><dt>服务地址</dt><dd class="server-address"><span>{origin}</span>
                <button id="desktop-edit-server" class="account-switch" type="button"
                    aria-expanded="false" aria-controls="desktop-server-form">修改</button>
            </dd></div>
            <div><dt>桌面客户端</dt><dd>{host}</dd></div>
        </dl>
        <form id="desktop-server-form" class="server-form" method="post" action="{path}" hidden>
            <input type="hidden" name="request_id" value="{request_id}">
            <input type="hidden" name="csrf" value="{csrf}">
            <input type="hidden" name="decision" value="change_server">
            <label for="desktop-server-origin">服务器地址</label>
            <input id="desktop-server-origin" name="server_origin" type="url" value="{origin}"
                required maxlength="2048" spellcheck="false" autocomplete="url"
                aria-describedby="desktop-server-hint desktop-server-error" placeholder="https://ai.example.com">
            <p id="desktop-server-hint">填写 HTTPS 服务器地址，保存后将在该服务器重新登录并授权。</p>
            <p id="desktop-server-error" class="form-error" role="alert" hidden></p>
            <div class="server-form-actions">
                <button class="button button-primary" type="submit">保存并连接</button>
                <button id="desktop-cancel-server" class="button button-secondary" type="button">取消修改</button>
            </div>
        </form>
        <div class="auth-notice">
            <svg class="icon" aria-hidden="true"><use href="#icon-shield"></use></svg>
            <p>授权后，桌面端将以此账号访问服务。<br>请仅在你主动发起桌面端登录时确认。</p>
        </div>
        <form method="post" action="{path}" class="consent-form">
            <input type="hidden" name="request_id" value="{request_id}">
            <input type="hidden" name="csrf" value="{csrf}">
            <button class="button button-primary" type="submit" name="decision" value="allow">
                确认授权并继续<svg class="icon" aria-hidden="true"><use href="#icon-arrow"></use></svg>
            </button>
            <button class="button button-secondary" type="submit" name="decision" value="deny">取消授权</button>
        </form>
        <p class="form-footer">确认后，将自动返回桌面端</p>
    """.format(origin=html.escape(backend_origin),
             account=html.escape(display_name or username),
             initial=html.escape((display_name or username or "U")[0].upper()),
             user=html.escape(username), host=html.escape(host),
             path=html.escape(AUTHORIZE_PATH),
             request_id=html.escape(request_id), csrf=html.escape(csrf))
    return _page("确认桌面端授权", body, step="consent")


def render_notice_page(*, title: str, message: str,
                       console_path: str = "/chat") -> str:
    """A browser notice sharing the sign-in and consent presentation."""
    body = """
        <div class="auth-heading">
            <span class="eyebrow">ACCOUNT VERIFICATION</span>
            <h1 id="auth-title">{title}</h1><p>{message}</p>
        </div>
        <div class="auth-notice">
            <svg class="icon" aria-hidden="true"><use href="#icon-shield"></use></svg>
            <p>完成账号验证后，请从桌面端重新发起登录。</p>
        </div>
        <a class="button button-primary" href="{console}">前往 Web 控制台
            <svg class="icon" aria-hidden="true"><use href="#icon-arrow"></use></svg>
        </a>
    """.format(title=html.escape(title), message=html.escape(message),
             console=html.escape(console_path))
    return _page(title, body)


def render_sign_in_page() -> str:
    """Same-origin sign-in for an unauthenticated Desktop authorize GET.

    The Desktop broker opens authorize on its backend origin (``localhost`` or
    ``127.0.0.1``). A Web console Cookie on the *other* loopback name is a
    different browser origin, so "open the console, then start again" left
    users stuck. Collecting the password here keeps the Cookie on the exact
    host the consent POST will need, then reloads this authorize URL.
    """
    from channel.web.core import template

    # Share the Web console's login markup and stylesheet. Only the completion
    # action differs: reload this validated authorize URL to ask for consent.
    return template.render("templates/auth/desktop-login.html")


def _page(title: str, body: str, *, step: str = "login") -> str:
    from channel.web.core import template

    # ``same-origin`` (not ``no-referrer``): the Authorize form POST is
    # same-origin and needs a Referer/Origin for browsers that omit Origin on
    # navigational form posts, while the redirect to the loopback callback is
    # cross-origin so the referrer (and the authorize query string) is stripped.
    return template.render("templates/auth/desktop.html").format(
        title=html.escape(title), body=body,
        login_state='class="is-complete"' if step == "consent" else 'aria-current="step"',
        consent_state='aria-current="step"' if step == "consent" else '',
        login_number='&#10003;' if step == "consent" else '1',
    )


class DesktopAuthService:
    """The native-flow state machine, backed by ``identity.db``."""

    def __init__(self, db_path: str, identity_service) -> None:
        self._db_path = db_path
        self._svc = identity_service
        self._ensure_schema()

    # -- schema -------------------------------------------------------------

    def _ensure_schema(self) -> None:
        with self._svc._store.connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS desktop_auth_requests (
                    id             TEXT PRIMARY KEY,
                    csrf_hash      TEXT NOT NULL,
                    user_id        TEXT NOT NULL,
                    session_id     TEXT NOT NULL,
                    session_hash   TEXT NOT NULL,
                    client_id      TEXT NOT NULL,
                    redirect_uri   TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    challenge_method TEXT NOT NULL,
                    state          TEXT NOT NULL,
                    created_at     INTEGER NOT NULL,
                    expires_at     INTEGER NOT NULL,
                    confirmed_at   INTEGER
                );
                CREATE TABLE IF NOT EXISTS desktop_auth_codes (
                    id             TEXT PRIMARY KEY,
                    code_hash      TEXT NOT NULL UNIQUE,
                    user_id        TEXT NOT NULL,
                    session_id     TEXT NOT NULL,
                    session_hash   TEXT NOT NULL,
                    client_id      TEXT NOT NULL,
                    redirect_uri   TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    challenge_method TEXT NOT NULL,
                    created_at     INTEGER NOT NULL,
                    expires_at     INTEGER NOT NULL,
                    consumed_at    INTEGER
                );
                """
            )
            con.commit()

    # -- session resolution -------------------------------------------------

    def _live_session(self, session_token: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """The owner of ``session_token``, or a refusal.

        A missing/revoked/expired session is 401; a session that must still
        change its password is 403 ``password_change_required`` and may not mint
        a native session (design D8: a restricted Web session has no native
        grant).
        """
        if not session_token:
            raise DesktopAuthError("unauthorized", "unauthorized", 401)
        session = self._svc.verify_session(session_token)
        if not session:
            raise DesktopAuthError("unauthorized", "unauthorized", 401)
        user = session["user"]
        if user.get("must_change_password") or session["session"]["restricted"]:
            raise DesktopAuthError("password change required",
                                   "password_change_required", 403)
        return user, session["session"]

    def _initiating_session_live(self, con, *, session_id: str,
                                 session_hash: str,
                                 user_id: str) -> bool:
        """Re-verify the Web session a code was bound to, at exchange time.

        The caller holds the write lock, so a concurrent revocation cannot land
        between this check and the consumption that follows.
        """
        rows = con.execute(
            "SELECT * FROM auth_sessions WHERE id=?", (session_id,)).fetchall()
        if not rows:
            return False
        row = dict(rows[0])
        if row["revoked_at"] or row["expires_at"] <= _now():
            return False
        if row["restricted"]:
            return False
        if not hmac.compare_digest(row["token_hash"], session_hash):
            return False
        user = self._svc._find_user_by_id(user_id)
        if not user or not user["active"] or user["must_change_password"]:
            return False
        return True

    # -- step 1: the pending consent ---------------------------------------

    def precheck(self, *, client_id: str, redirect_uri: str, code_challenge: str,
                 code_challenge_method: str, state: str = "") -> None:
        """Validate the request shape without touching any session.

        Split out so the browser's GET can answer a malformed request (bad
        client, unregistered redirect, unsupported PKCE method) before it says
        anything about who is signed in.
        """
        _validate_client(client_id)
        if not is_registered_redirect_uri(redirect_uri):
            raise DesktopAuthError("unregistered redirect uri",
                                   "invalid_redirect_uri", 400)
        _validate_challenge(code_challenge, code_challenge_method)
        _validate_state(state)

    def begin(self, *, session_token: str, client_id: str, redirect_uri: str,
              code_challenge: str, code_challenge_method: str,
              state: str = "") -> Dict[str, str]:
        """Validate a Desktop authorization request and open a consent record."""
        self.precheck(client_id=client_id, redirect_uri=redirect_uri,
                      code_challenge=code_challenge,
                      code_challenge_method=code_challenge_method, state=state)
        user, session = self._live_session(session_token)

        request_id = secrets.token_urlsafe(24)
        csrf = secrets.token_urlsafe(32)
        now = _now()
        with self._svc._store.connect() as con:
            con.execute(
                "INSERT INTO desktop_auth_requests"
                " (id, csrf_hash, user_id, session_id, session_hash, client_id,"
                "  redirect_uri, code_challenge, challenge_method, state,"
                "  created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, _digest(csrf), user["id"], session["id"],
                 _digest(session_token), client_id, redirect_uri,
                 code_challenge, code_challenge_method, state or "", now,
                 now + REQUEST_TTL_SECONDS),
            )
            con.commit()
        return {"request_id": request_id, "csrf": csrf}

    def _claim(self, con, *, request_id: str, csrf: str,
               session_token: str) -> Dict[str, Any]:
        """Validate and atomically claim a consent record (single-use)."""
        user, _session = self._live_session(session_token)
        if not request_id or not csrf:
            raise DesktopAuthError("missing request confirmation",
                                   "invalid_request", 403)
        rows = con.execute(
            "SELECT * FROM desktop_auth_requests WHERE id=?", (request_id,)
        ).fetchall()
        if not rows:
            raise DesktopAuthError("unknown authorization request",
                                   "invalid_request", 403)
        row = dict(rows[0])
        if row["confirmed_at"]:
            raise DesktopAuthError("authorization already confirmed",
                                   "invalid_request", 403)
        if row["expires_at"] <= _now():
            raise DesktopAuthError("authorization request expired",
                                   "invalid_request", 403)
        if row["user_id"] != user["id"]:
            raise DesktopAuthError(
                "authorization request belongs to another account",
                "invalid_request", 403)
        if not hmac.compare_digest(row["session_hash"], _digest(session_token)):
            raise DesktopAuthError(
                "authorization request belongs to another session",
                "invalid_request", 403)
        if not hmac.compare_digest(row["csrf_hash"], _digest(csrf)):
            raise DesktopAuthError("invalid confirmation token",
                                   "invalid_request", 403)
        claimed = con.execute(
            "UPDATE desktop_auth_requests SET confirmed_at=?"
            " WHERE id=? AND confirmed_at IS NULL", (_now(), request_id))
        if getattr(claimed, "rowcount", 0) != 1:
            raise DesktopAuthError("authorization already confirmed",
                                   "invalid_request", 403)
        return row

    def confirm(self, *, request_id: str, csrf: str,
                session_token: str) -> Dict[str, str]:
        """Exchange a confirmed consent for an authorization code.

        The claim and the code insert commit in one transaction, so a replayed
        form (double submit, or two workers) can never mint two codes.
        """
        con = self._svc._tx()
        with con:
            row = self._claim(con, request_id=request_id, csrf=csrf,
                              session_token=session_token)
            code = secrets.token_urlsafe(32)
            now = _now()
            con.execute(
                "INSERT INTO desktop_auth_codes"
                " (id, code_hash, user_id, session_id, session_hash, client_id,"
                "  redirect_uri, code_challenge, challenge_method, created_at,"
                "  expires_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (secrets.token_urlsafe(18), _digest(code), row["user_id"],
                 row["session_id"], row["session_hash"], row["client_id"],
                 row["redirect_uri"], row["code_challenge"],
                 row["challenge_method"], now, now + CODE_TTL_SECONDS),
            )
            con.commit()
        return {"code": code, "redirect_uri": row["redirect_uri"],
                "state": row["state"], "client_id": row["client_id"]}

    def deny(self, *, request_id: str, csrf: str,
             session_token: str) -> Dict[str, str]:
        """Record a refusal: no code is minted and the record cannot be reused."""
        with self._svc._tx() as con:
            row = self._claim(con, request_id=request_id, csrf=csrf,
                              session_token=session_token)
            con.commit()
        return {"redirect_uri": row["redirect_uri"], "state": row["state"]}

    # -- step 2: the exact-origin exchange ---------------------------------

    def exchange(self, *, code: str, verifier: str, client_id: str,
                 redirect_uri: str, origin: str = "") -> Dict[str, Any]:
        """Trade a code + verifier for an independent native session.

        ``origin`` is the exact origin this token request was served at. It is
        recorded in ``desktop_native_origins`` *in the same transaction* that
        mints the native session (task 3.2), which is what a later Web-session
        bootstrap re-verifies (``auth.desktop_web_session``). It is taken from
        the live request -- never guessed from a header the client controls, and
        never inferred from a stored User-Agent.
        """
        _validate_client(client_id)
        if not is_registered_redirect_uri(redirect_uri):
            raise DesktopAuthError("unregistered redirect uri",
                                   "invalid_redirect_uri", 400)
        if not _CODE_VERIFIER.fullmatch(verifier or ""):
            raise DesktopAuthError("invalid code verifier", "invalid_grant", 400)
        if not code or not isinstance(code, str) or len(code) > 256:
            raise DesktopAuthError("invalid grant", "invalid_grant", 400)

        now = _now()
        con = self._svc._tx()
        with con:
            rows = con.execute(
                "SELECT * FROM desktop_auth_codes WHERE code_hash=?",
                (_digest(code),)).fetchall()
            row = dict(rows[0]) if rows else None
            if row is None or row["consumed_at"]:
                raise DesktopAuthError("invalid grant", "invalid_grant", 400)
            if row["expires_at"] <= now:
                raise DesktopAuthError("authorization code expired",
                                       "invalid_grant", 400)
            if row["client_id"] != client_id:
                raise DesktopAuthError("invalid grant", "invalid_grant", 400)
            if row["redirect_uri"] != redirect_uri:
                raise DesktopAuthError("invalid grant", "invalid_grant", 400)
            if not hmac.compare_digest(row["code_challenge"],
                                       s256_challenge(verifier)):
                raise DesktopAuthError("invalid grant", "invalid_grant", 400)
            if not self._initiating_session_live(
                    con, session_id=row["session_id"],
                    session_hash=row["session_hash"], user_id=row["user_id"]):
                raise DesktopAuthError("initiating session is no longer valid",
                                       "invalid_grant", 400)

            claimed = con.execute(
                "UPDATE desktop_auth_codes SET consumed_at=?"
                " WHERE id=? AND consumed_at IS NULL", (now, row["id"]))
            if getattr(claimed, "rowcount", 0) != 1:
                raise DesktopAuthError("invalid grant", "invalid_grant", 400)

            token = generate_token()
            native_session_id = secrets.token_urlsafe(18)
            native_hash = hash_token(token)
            con.execute(
                "INSERT INTO auth_sessions"
                " (id, token_hash, user_id, expires_at, restricted)"
                " VALUES (?,?,?,?,0)",
                (native_session_id, native_hash, row["user_id"],
                 now + session_ttl_seconds(False)),
            )
            # Task 3.2: register the origin this native session was minted at,
            # atomically with the session itself. A native session with no
            # registration cannot bootstrap a Web child (fail closed).
            if origin:
                con.execute(
                    "INSERT OR REPLACE INTO desktop_native_origins"
                    " (id, session_id, native_session_hash, origin, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (secrets.token_urlsafe(18), native_session_id, native_hash,
                     origin, now),
                )
            con.commit()

        user = self._svc._find_user_by_id(row["user_id"])
        return {
            "status": "success",
            "token": token,
            "token_type": "Bearer",
            "user": {
                "id": row["user_id"],
                "username": user["username"] if user else "",
                "display_name": user["display_name"] if user else "",
                "is_platform_admin": bool(user and user["is_platform_admin"]),
            },
            "tenants": self._svc._active_tenants_for(row["user_id"]),
        }


_SERVICES: Dict[str, DesktopAuthService] = {}


def service_for(identity_service) -> DesktopAuthService:
    """The native-flow service bound to one resolved ``IdentityService``."""
    return get_desktop_auth_service(identity_service._store.db_path,
                                    identity_service)


def get_desktop_auth_service(db_path: str, identity_service) -> DesktopAuthService:
    """The process-wide service for one identity database.

    Cached so the idempotent schema bootstrap runs once per database rather than
    on every request; the state itself lives in SQLite, so caching is only an
    optimisation and is safe across worker threads.
    """
    key = db_path or ""
    service = _SERVICES.get(key)
    if service is None:
        service = DesktopAuthService(db_path, identity_service)
        _SERVICES[key] = service
    return service
