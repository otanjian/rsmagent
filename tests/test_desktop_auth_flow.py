# encoding:utf-8
"""Desktop native auth: browser authorization code + PKCE S256 (task 8.2 / R2).

Drives the real ``build_web_app()`` over a private identity database (the
``web_app`` fixture) rather than calling handlers directly, because the whole
point of the seam is what the wire answers:

* ``/auth/login`` keeps its compatibility fields but never hands out a reusable
  token — only the identity session Cookie (design D8: "``token`` 始终为空").
  The Desktop client uses the browser flow instead of a login-response token.
* ``/auth/desktop/authorize`` is a *browser* consent page: it needs an ordinary
  Web login first (and a completed forced password change), it never mints a
  code from a bare GET, and its confirm POST validates CSRF/origin, the live
  session and a one-time request record.
* ``/auth/desktop/token`` is the exact-origin exchange: digest-stored,
  60-second, single-use authorization code bound to user + initiating session +
  client_id + exact redirect_uri + PKCE challenge. Replay, wrong verifier,
  wrong redirect_uri, wrong client and expiry all refuse, and a successful
  exchange creates an *independent* native AuthSession.

The read-back assertions are that the surfaces a client or a log can see (the
consent page, the login response, ``/auth/me``) never carry the code, the
verifier or a session token.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qs, urlencode, urlparse

#: The fixed public client id (design D8).
CLIENT_ID = "cowagent-desktop"
#: A registered literal loopback callback (the main process listens here first).
REDIRECT_URI = "http://127.0.0.1:38123/callback"
#: The IPv6 literal form is registered too.
REDIRECT_URI_V6 = "http://[::1]:38124/callback"


def _pkce():
    """A fresh (verifier, S256 challenge) pair."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _authorize_path(client_id=CLIENT_ID, redirect_uri=REDIRECT_URI,
                    challenge="c" * 43, state="state-1", method="S256"):
    return "/auth/desktop/authorize?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": method,
        "state": state,
    })


def _header(response, name):
    for header_name, value in (getattr(response, "header_items", None) or []):
        if header_name.lower() == name.lower():
            return value
    return ""


def _location(response):
    return _header(response, "Location")


def _cookie(response, name):
    from tests._helpers import cookie_value

    return cookie_value(response, name)


def _consent_fields(html):
    """The hidden form fields the consent page must carry (request id + CSRF)."""
    def field(name):
        match = re.search(r'name="%s"\s+value="([^"]*)"' % name, html)
        return match.group(1) if match else ""
    return field("request_id"), field("csrf")


def _code_from_redirect(location):
    return (parse_qs(urlparse(location).query).get("code") or [""])[0]


class _Flow:
    """Shared steps: log in, render the consent page, confirm, exchange."""

    def __init__(self, web, username="root", password=None):
        self.web = web
        self.username = username
        self.token = web.login(username, password)

    def consent(self, *, redirect_uri=REDIRECT_URI, client_id=CLIENT_ID,
                challenge=None, state="state-1", method="S256"):
        verifier = None
        if challenge is None:
            verifier, challenge = _pkce()
        return self.web.get(
            _authorize_path(client_id=client_id, redirect_uri=redirect_uri,
                            challenge=challenge, state=state, method=method),
            token=self.token), verifier, challenge

    def confirm(self, html, *, decision="allow", token=None, headers=None):
        request_id, csrf = _consent_fields(html)
        return self.web.post(
            "/auth/desktop/authorize",
            {"request_id": request_id, "csrf": csrf, "decision": decision},
            token=self.token if token is None else token, headers=headers)

    def exchange(self, code, verifier, *, redirect_uri=REDIRECT_URI,
                 client_id=CLIENT_ID, origin=None, headers=None):
        extra = dict(headers or {})
        if origin is not None:
            extra["Origin"] = origin
        return self.web.request(
            "/auth/desktop/token", "POST",
            {"client_id": client_id, "code": code,
             "code_verifier": verifier, "redirect_uri": redirect_uri},
            token=None, tenant=False, headers=extra)

    def issue_code(self):
        """One complete authorize + confirm, returning (verifier, code)."""
        resp, verifier, _challenge = self.consent()
        confirm = self.confirm(resp.data.decode("utf-8"))
        code = _code_from_redirect(_location(confirm))
        assert code, confirm.data
        return verifier, code


def _bearer(token):
    return {"Authorization": "Bearer " + token}


def _restricted_session(web, username="fresh"):
    """A live session that still has to change its temporary password."""
    web.service.create_member(
        actor_user_id=web.admin_id, tenant_id=web.tenant_id,
        operation="create-new", username=username,
        display_name=username.title(), temporary_password="TempPass123!",
        roles=["member"])
    return web.service.login(username, "TempPass123!").token


class TestLoginCompatibility:
    """``/auth/login`` sets only the Cookie and an empty compatibility token."""

    def test_login_returns_empty_token_and_sets_cookie(self, web_app):
        web = web_app()
        resp = web.post("/auth/login",
                        {"username": "root", "password": web.ADMIN_PASSWORD},
                        token=None, tenant=False)
        data = web.json(resp)
        assert data["status"] == "success"
        assert data["token"] == ""
        assert _cookie(resp, "cow_session")

    def test_login_body_carries_no_session_secret(self, web_app):
        web = web_app()
        resp = web.post("/auth/login",
                        {"username": "root", "password": web.ADMIN_PASSWORD},
                        token=None, tenant=False)
        body = resp.data.decode("utf-8")
        assert '"token": ""' in body
        cookie = _cookie(resp, "cow_session")
        assert cookie
        assert cookie not in body

    def test_a_self_declared_desktop_client_still_only_gets_a_cookie(self, web_app):
        web = web_app()
        resp = web.post("/auth/login",
                        {"username": "root", "password": web.ADMIN_PASSWORD},
                        token=None, tenant=False,
                        headers={"User-Agent": "cowagent-desktop/2.1.6",
                                 "X-Client": "desktop"})
        data = web.json(resp)
        assert data["status"] == "success"
        assert data["token"] == ""
        assert _cookie(resp, "cow_session")


class TestAuthorizeEntry:
    """The consent page's entry rules."""

    def test_cookie_less_authorize_is_refused(self, web_app):
        web = web_app()
        resp = web.get(_authorize_path(), token=None, tenant=False)
        assert str(resp.status).startswith(("401", "403"))
        assert not _location(resp)

    def test_cookie_less_authorize_offers_same_origin_login(self, web_app):
        """Unauthenticated authorize must not send the user to a different host.

        Desktop opens ``/auth/desktop/authorize`` on the broker's backend
        origin (``127.0.0.1`` or ``localhost``). A Web console session on the
        *other* loopback name does not share cookies, so "open the console,
        then start again" left users stuck on Sign in to continue. The page
        itself must collect the password on this exact origin and reload.
        """
        web = web_app()
        resp = web.get(_authorize_path(), token=None, tenant=False)
        assert str(resp.status).startswith("401")
        html = resp.data.decode("utf-8")
        assert 'id="desktop-auth-login"' in html
        assert "/auth/login" in html
        assert "location.reload" in html
        assert "Open the Web console" not in html

    def test_authorize_requires_the_registered_client_id(self, web_app):
        web = web_app()
        resp = _Flow(web).consent(client_id="some-other-client")[0]
        assert str(resp.status).startswith("400")
        assert not _location(resp)

    def test_authorize_rejects_non_loopback_redirects(self, web_app):
        web = web_app()
        flow = _Flow(web)
        for bad in ("https://evil.example.com/callback",
                    "http://127.0.0.1.evil.com:38123/callback",
                    "http://localhost:38123/callback",
                    "http://0.0.0.0:38123/callback",
                    "http://127.0.0.1/callback",
                    "http://[::1]/callback"):
            resp = flow.consent(redirect_uri=bad)[0]
            assert str(resp.status).startswith("400"), bad

    def test_authorize_rejects_a_plain_pkce_method(self, web_app):
        web = web_app()
        resp = _Flow(web).consent(method="plain")[0]
        assert str(resp.status).startswith("400")

    def test_a_bare_get_never_mints_a_code(self, web_app):
        web = web_app()
        _verifier, challenge = _pkce()
        resp, _v, _c = _Flow(web).consent(challenge=challenge)
        assert str(resp.status).startswith("200")
        html = resp.data.decode("utf-8")
        assert not _location(resp)
        # The page shows the backend + the account and asks for confirmation.
        assert "root" in html
        request_id, csrf = _consent_fields(html)
        assert request_id and csrf
        assert challenge not in html
        assert "code=" not in html

    def test_authorize_accepts_the_ipv6_loopback_form(self, web_app):
        web = web_app()
        resp = _Flow(web).consent(redirect_uri=REDIRECT_URI_V6)[0]
        assert str(resp.status).startswith("200")
        assert _consent_fields(resp.data.decode("utf-8"))[0]

    def test_forced_password_change_cannot_reach_the_consent_form(self, web_app):
        web = web_app()
        restricted = _restricted_session(web)
        resp = web.get(_authorize_path(), token=restricted)
        assert str(resp.status).startswith("403")
        assert not _location(resp)
        assert not _consent_fields(resp.data.decode("utf-8"))[0]


class TestConsentConfirm:
    """The confirm POST validates CSRF, origin, session and the request record."""

    def test_confirm_redirects_with_code_and_state(self, web_app):
        web = web_app()
        flow = _Flow(web)
        resp, _verifier, _challenge = flow.consent()
        location = _location(flow.confirm(resp.data.decode("utf-8")))
        assert location.startswith(REDIRECT_URI)
        assert "state=state-1" in location
        assert _code_from_redirect(location)

    def test_confirm_requires_a_live_session(self, web_app):
        web = web_app()
        flow = _Flow(web)
        resp, _verifier, _challenge = flow.consent()
        request_id, csrf = _consent_fields(resp.data.decode("utf-8"))
        anon = web.post("/auth/desktop/authorize",
                        {"request_id": request_id, "csrf": csrf,
                         "decision": "allow"}, token=None, tenant=False)
        assert str(anon.status).startswith(("401", "403"))
        assert not _location(anon)

    def test_confirm_requires_a_matching_csrf_field(self, web_app):
        web = web_app()
        flow = _Flow(web)
        resp, _verifier, _challenge = flow.consent()
        request_id, csrf = _consent_fields(resp.data.decode("utf-8"))
        for bad in ("", "not-the-token", csrf[:-1] + ("x" if csrf[-1] != "x" else "y")):
            refused = web.post("/auth/desktop/authorize",
                               {"request_id": request_id, "csrf": bad,
                                "decision": "allow"}, token=flow.token)
            assert str(refused.status).startswith("403"), bad
            assert not _location(refused)

    def test_confirm_requires_a_same_origin_request(self, web_app):
        web = web_app()
        flow = _Flow(web)
        resp, _verifier, _challenge = flow.consent()
        request_id, csrf = _consent_fields(resp.data.decode("utf-8"))
        cross = web.post("/auth/desktop/authorize",
                         {"request_id": request_id, "csrf": csrf,
                          "decision": "allow"}, token=flow.token,
                         headers={"Origin": "https://evil.example.com"})
        assert cross.status == "403"
        assert "cross_origin" in cross.data.decode("utf-8")
        assert not _location(cross)

    def test_confirm_allows_missing_origin_when_form_csrf_present(self, web_app):
        """Consent pages use a tight Referrer-Policy; some browsers then omit
        both Origin and Referer on the Authorize form POST. The one-time form
        ``csrf`` field is the CSRF proof — a missing Origin must not surface as
        ``cross_origin`` and strand the Desktop login.
        """
        web = web_app()
        flow = _Flow(web)
        resp, _verifier, _challenge = flow.consent()
        confirm = flow.confirm(resp.data.decode("utf-8"), headers={"Origin": ""})
        assert str(confirm.status).startswith("302"), confirm.data
        assert _location(confirm).startswith(REDIRECT_URI)

    def test_confirm_refuses_another_accounts_session(self, web_app):
        web = web_app()
        web.member("alice", ["member"])
        flow = _Flow(web, "alice")
        resp, _verifier, _challenge = flow.consent()
        request_id, csrf = _consent_fields(resp.data.decode("utf-8"))
        stolen = web.post("/auth/desktop/authorize",
                          {"request_id": request_id, "csrf": csrf,
                           "decision": "allow"}, token=web.login("root"))
        assert str(stolen.status).startswith("403")
        assert not _location(stolen)

    def test_confirm_refuses_a_replayed_request_record(self, web_app):
        web = web_app()
        flow = _Flow(web)
        resp, _verifier, _challenge = flow.consent()
        html = resp.data.decode("utf-8")
        assert _code_from_redirect(_location(flow.confirm(html)))
        second = flow.confirm(html)
        assert str(second.status).startswith("4")
        assert not _location(second)

    def test_denying_authorization_returns_an_error_not_a_code(self, web_app):
        web = web_app()
        flow = _Flow(web)
        resp, _verifier, _challenge = flow.consent()
        denied = flow.confirm(resp.data.decode("utf-8"), decision="deny")
        location = _location(denied)
        assert location.startswith(REDIRECT_URI)
        assert "error=access_denied" in location
        assert not _code_from_redirect(location)

    def test_forced_password_change_session_cannot_mint_a_code(self, web_app):
        web = web_app()
        restricted = _restricted_session(web)
        flow = _Flow(web)
        resp, _verifier, _challenge = flow.consent()
        request_id, csrf = _consent_fields(resp.data.decode("utf-8"))
        refused = web.post("/auth/desktop/authorize",
                           {"request_id": request_id, "csrf": csrf,
                            "decision": "allow"}, token=restricted)
        assert str(refused.status).startswith("403")
        assert not _location(refused)

    def test_the_form_encoded_browser_body_is_accepted(self, web_app):
        web = web_app()
        flow = _Flow(web)
        resp, _verifier, _challenge = flow.consent()
        request_id, csrf = _consent_fields(resp.data.decode("utf-8"))
        confirm = web.post(
            "/auth/desktop/authorize",
            urlencode({"request_id": request_id, "csrf": csrf,
                       "decision": "allow"}),
            token=flow.token,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert _code_from_redirect(_location(confirm))


class TestTokenExchange:
    """The exchange is exact-origin, digest-stored, single-use and expiring."""

    def test_exchange_returns_a_working_native_session(self, web_app):
        web = web_app()
        flow = _Flow(web)
        verifier, code = flow.issue_code()
        resp = flow.exchange(code, verifier)
        data = web.json(resp)
        assert data["status"] == "success"
        token = data["token"]
        assert token
        assert not _location(resp)
        business = web.request("/api/scheduler", "GET", None, token=None,
                               tenant=True, headers=_bearer(token))
        assert str(business.status).startswith("200")
        assert web.json(business)["status"] == "success"

    def test_exchange_response_is_not_cacheable(self, web_app):
        web = web_app()
        flow = _Flow(web)
        verifier, code = flow.issue_code()
        resp = flow.exchange(code, verifier)
        assert "no-store" in _header(resp, "Cache-Control")

    def test_exchange_reads_back_no_code_or_verifier(self, web_app):
        web = web_app()
        flow = _Flow(web)
        verifier, code = flow.issue_code()
        resp = flow.exchange(code, verifier)
        body = resp.data.decode("utf-8")
        assert code not in body
        assert verifier not in body
        token = web.json(resp)["token"]
        me = web.request("/auth/me", "GET", None, token=None, tenant=False,
                         headers=_bearer(token))
        me_body = me.data.decode("utf-8")
        assert web.json(me)["status"] == "success"
        assert token not in me_body
        assert verifier not in me_body

    def test_exchange_requires_the_exact_redirect_uri(self, web_app):
        web = web_app()
        flow = _Flow(web)
        verifier, code = flow.issue_code()
        for bad in (REDIRECT_URI_V6, "http://127.0.0.1:38123/other",
                    "http://127.0.0.1:9/callback",
                    "https://evil.example.com/callback"):
            resp = flow.exchange(code, verifier, redirect_uri=bad)
            assert str(resp.status).startswith("400"), bad
        # None of the refusals consumed the code.
        assert web.json(flow.exchange(code, verifier))["status"] == "success"

    def test_exchange_requires_the_registered_client(self, web_app):
        web = web_app()
        flow = _Flow(web)
        verifier, code = flow.issue_code()
        resp = flow.exchange(code, verifier, client_id="some-other-client")
        assert str(resp.status).startswith("400")

    def test_exchange_requires_the_matching_verifier(self, web_app):
        web = web_app()
        flow = _Flow(web)
        verifier, code = flow.issue_code()
        other, _challenge = _pkce()
        assert other != verifier
        resp = flow.exchange(code, other)
        assert str(resp.status).startswith("400")
        assert web.json(flow.exchange(code, verifier))["status"] == "success"

    def test_exchange_is_single_use(self, web_app):
        web = web_app()
        flow = _Flow(web)
        verifier, code = flow.issue_code()
        first = web.json(flow.exchange(code, verifier))
        assert first["status"] == "success"
        replay = flow.exchange(code, verifier)
        assert str(replay.status).startswith("400")
        assert "invalid_grant" in replay.data.decode("utf-8")
        assert first["token"] not in replay.data.decode("utf-8")

    def test_exchange_rejects_an_unknown_code(self, web_app):
        web = web_app()
        resp = _Flow(web).exchange("not-a-real-code", "v" * 50)
        assert str(resp.status).startswith("400")

    def test_code_expires_after_sixty_seconds(self, web_app, monkeypatch):
        import auth.desktop_auth as desktop_auth

        web = web_app()
        flow = _Flow(web)
        verifier, code = flow.issue_code()
        real_now = desktop_auth._now
        monkeypatch.setattr(desktop_auth, "_now", lambda: real_now() + 61)
        resp = flow.exchange(code, verifier)
        assert str(resp.status).startswith("400")
        assert "invalid_grant" in resp.data.decode("utf-8")

    def test_code_survives_inside_the_sixty_second_window(self, web_app,
                                                         monkeypatch):
        import auth.desktop_auth as desktop_auth

        web = web_app()
        flow = _Flow(web)
        verifier, code = flow.issue_code()
        real_now = desktop_auth._now
        monkeypatch.setattr(desktop_auth, "_now", lambda: real_now() + 59)
        assert web.json(flow.exchange(code, verifier))["status"] == "success"

    def test_code_is_refused_when_the_initiating_session_was_revoked(self, web_app):
        web = web_app()
        flow = _Flow(web)
        verifier, code = flow.issue_code()
        web.service.revoke_session(flow.token)
        resp = flow.exchange(code, verifier)
        assert str(resp.status).startswith("4")

    def test_exchange_rejects_a_foreign_origin(self, web_app):
        web = web_app()
        flow = _Flow(web)
        verifier, code = flow.issue_code()
        resp = flow.exchange(code, verifier, origin="https://evil.example.com")
        assert resp.status == "403"

    def test_token_endpoint_ignores_a_web_cookie(self, web_app):
        """The exchange authenticates by code+verifier, never by a Cookie."""
        web = web_app()
        flow = _Flow(web)
        resp = web.post("/auth/desktop/token",
                        {"client_id": CLIENT_ID, "code": "x",
                         "code_verifier": "y" * 50,
                         "redirect_uri": REDIRECT_URI},
                        token=flow.token, tenant=False)
        assert str(resp.status).startswith("400")

    def test_the_minted_session_can_be_revoked(self, web_app):
        web = web_app()
        flow = _Flow(web)
        verifier, code = flow.issue_code()
        token = web.json(flow.exchange(code, verifier))["token"]
        logout = web.request("/auth/logout", "POST", {}, token=None,
                             tenant=False, headers=_bearer(token))
        assert web.json(logout)["status"] == "success"
        after = web.request("/api/scheduler", "GET", None, token=None,
                            tenant=True, headers=_bearer(token))
        assert str(after.status).startswith("401")

    def test_two_authorizations_mint_distinct_sessions(self, web_app):
        web = web_app()
        first = _Flow(web)
        v1, c1 = first.issue_code()
        second = _Flow(web)
        v2, c2 = second.issue_code()
        a = web.json(first.exchange(c1, v1))["token"]
        b = web.json(second.exchange(c2, v2))["token"]
        assert a and b and a != b


class TestRedirectUriRule:
    """The registered loopback forms are a pure function; pinned directly too."""

    def test_registered_forms(self):
        from auth.desktop_auth import is_registered_redirect_uri

        assert is_registered_redirect_uri(REDIRECT_URI)
        assert is_registered_redirect_uri(REDIRECT_URI_V6)
        assert is_registered_redirect_uri("http://127.0.0.1:1/a/b")
        for bad in ("https://127.0.0.1:38123/callback",
                    "http://localhost:38123/callback",
                    "http://127.0.0.1:38123",
                    "http://127.0.0.1:38123/a?x=1",
                    "http://127.0.0.1:38123/a#f",
                    "http://127.0.0.1:38123/../a",
                    "http://127.0.0.1:38123/callback\r\nX-Evil: 1",
                    ""):
            assert not is_registered_redirect_uri(bad), bad
